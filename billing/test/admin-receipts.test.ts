import { randomUUID } from "node:crypto";
import { SignJWT, exportSPKI, generateKeyPair } from "jose";
import { describe, expect, it } from "vitest";
import { AUDIENCE, ISSUER, bodyHash } from "../src/admin/admin-api.js";
import { emailLogs } from "../src/db/schema.js";
import { isMailbox } from "../src/email/address.js";
import { ConsoleMailer, type Mail } from "../src/email/mailer.js";
import { setup } from "./helpers.js";

class FailingMailer extends ConsoleMailer {
  fail = true;
  override async send(mail: Mail) {
    if (this.fail && mail.attachments?.length) throw new Error("Gmail API 503 UNAVAILABLE");
    return super.send(mail);
  }
}

async function panel(mailer?: ConsoleMailer) {
  const { publicKey, privateKey } = await generateKeyPair("EdDSA", { extractable: true });
  const t = await setup({ adminPublicKeyPem: await exportSPKI(publicKey), mailer });
  const token = (method: string, path: string, role: string, body?: unknown) =>
    new SignJWT({ role, htm: method, htu: path, ...(body === undefined ? {} : { bh: bodyHash(body) }) })
      .setProtectedHeader({ alg: "EdDSA" }).setIssuer(ISSUER).setAudience(AUDIENCE).setSubject("ops@kabala.app")
      .setIssuedAt().setExpirationTime("60s").setJti(randomUUID()).sign(privateKey);
  const call = async (method: "GET" | "POST", url: string, opts: { role?: string; body?: unknown; signedBody?: unknown } = {}) =>
    t.app.inject({
      method, url, payload: opts.body as object | undefined,
      headers: { authorization: `Bearer ${await token(method, url.split("?")[0]!, opts.role ?? "admin", "signedBody" in opts ? opts.signedBody : opts.body)}` },
    });
  // a paying customer: the payment webhook creates the receipt and emails it automatically
  const { userId } = await t.login("dana@gmail.com");
  const plan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;
  const sub = t.paypal.approve(plan, userId, { nextBillingTime: "2026-11-21T10:00:00Z" });
  await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
  await t.webhook("PAYMENT.SALE.COMPLETED", { id: "SALE-1", billing_agreement_id: sub.id, amount: { total: "29.00", currency: "ILS" }, state: "completed", create_time: "2026-10-21T10:00:00Z" });
  const receiptMails = () => t.mailer.sent.concat(mailer ? mailer.sent : []).filter((m) => m.attachments?.length);
  return { t, call, receiptMails, mailer: mailer ?? t.mailer };
}

describe("admin panel: receipts", () => {
  it("lists the receipt the payment emailed automatically, with its email", async () => {
    const { t, call } = await panel();
    const list = (await call("GET", "/api/admin/receipts")).json();
    expect(list.receipts).toEqual([expect.objectContaining({ email: "dana@gmail.com", status: "sent", amountMinor: 2900, number: "000001",
      lastEmail: expect.objectContaining({ status: "sent", trigger: "auto", to: "dana@gmail.com" }) })]);
    const one = (await call("GET", `/api/admin/receipts/${list.receipts[0].id}`)).json();
    expect(one.emails).toHaveLength(1);
    await t.close();
  });

  it("sends it again to the customer, or to one address the admin typed", async () => {
    const { t, call, receiptMails } = await panel();
    const id = (await call("GET", "/api/admin/receipts")).json().receipts[0].id;
    expect((await call("POST", `/api/admin/receipts/${id}/send`, { body: {} })).json()).toEqual({ ok: true });
    expect((await call("POST", `/api/admin/receipts/${id}/send`, { body: { to: "CPA@Office.co.il" } })).json()).toEqual({ ok: true });
    expect(receiptMails().map((m) => m.to)).toEqual(["dana@gmail.com", "dana@gmail.com", "cpa@office.co.il"]);
    const logs = await t.db.select().from(emailLogs);
    expect(logs.filter((l) => l.trigger === "admin").map((l) => l.toAddresses).sort()).toEqual(["cpa@office.co.il", "dana@gmail.com"]);
    await t.close();
  });

  it("refuses addresses that could add recipients or headers", async () => {
    const { t, call, receiptMails } = await panel();
    const id = (await call("GET", "/api/admin/receipts")).json().receipts[0].id;
    for (const to of ["a@b.co,evil@x.com", "Dana <d@x.com>", "a@b.co\r\nBcc: evil@x.com", "a;b@x.com", "user@[127.0.0.1]", "nobody"]) {
      expect((await call("POST", `/api/admin/receipts/${id}/send`, { body: { to } })).statusCode, to).toBe(400);
    }
    expect(receiptMails()).toHaveLength(1);
    expect(isMailbox("dana.levi+receipts@walla.co.il")).toBe(true);
    await t.close();
  });

  it("a token signed for one recipient can't send to another", async () => {
    const { t, call } = await panel();
    const id = (await call("GET", "/api/admin/receipts")).json().receipts[0].id;
    const r = await call("POST", `/api/admin/receipts/${id}/send`, { body: { to: "evil@x.com" }, signedBody: { to: "cpa@office.co.il" } });
    expect(r.statusCode).toBe(401);
    await t.close();
  });

  it("a viewer sees masked addresses and can't send or download", async () => {
    const { t, call } = await panel();
    const list = (await call("GET", "/api/admin/receipts", { role: "viewer" })).json();
    expect(list.receipts[0].email).toBe("da***@gmail.com");
    expect(list.receipts[0].lastEmail.to).toBe("da***@gmail.com");
    const id = list.receipts[0].id;
    expect((await call("POST", `/api/admin/receipts/${id}/send`, { role: "viewer", body: {} })).statusCode).toBe(403);
    expect((await call("GET", `/api/admin/receipts/${id}/pdf`, { role: "viewer" })).statusCode).toBe(403);
    const pdf = await call("GET", `/api/admin/receipts/${id}/pdf`);
    expect(pdf.headers["content-type"]).toBe("application/pdf");
    expect(pdf.rawPayload.subarray(0, 4).toString()).toBe("%PDF");
    await t.close();
  });

  it("a receipt whose email failed every automatic retry can be retried from the panel", async () => {
    const mailer = new FailingMailer();
    const { t, call } = await panel(mailer);
    const [r] = (await call("GET", "/api/admin/receipts?status=failed")).json().receipts;
    expect(r).toMatchObject({ status: "failed" });
    expect(r.lastError).toContain("Gmail API 503");
    mailer.fail = false; // the mail provider is back
    expect((await call("POST", `/api/admin/receipts/${r.id}/retry`, { body: {} })).json()).toEqual({ ok: true });
    expect((await call("GET", `/api/admin/receipts/${r.id}`)).json()).toMatchObject({ status: "sent" });
    expect((await call("POST", `/api/admin/receipts/${r.id}/retry`, { body: {} })).statusCode).toBe(409); // not failed anymore
    await t.close();
  });
});
