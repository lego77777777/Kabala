import { randomUUID } from "node:crypto";
import { SignJWT, exportSPKI, generateKeyPair } from "jose";
import { describe, expect, it } from "vitest";
import { AUDIENCE, ISSUER, bodyHash } from "../src/admin/admin-api.js";
import { receipts } from "../src/db/schema.js";
import { ConsoleMailer, type Mail } from "../src/email/mailer.js";
import { AUTO_RETRY } from "../src/receipts/pipeline.js";
import { setup } from "./helpers.js";

class SwitchMailer extends ConsoleMailer {
  down = false;
  override async send(mail: Mail) {
    if (this.down && mail.attachments?.length) throw new Error("Gmail API 503 UNAVAILABLE");
    return super.send(mail);
  }
}

async function env(mailer?: ConsoleMailer) {
  const { publicKey, privateKey } = await generateKeyPair("EdDSA", { extractable: true });
  const t = await setup({ adminPublicKeyPem: await exportSPKI(publicKey), mailer });
  const call = async (method: "GET" | "POST", url: string, body?: unknown, role = "admin") =>
    t.app.inject({
      method, url, payload: body as object | undefined,
      headers: { authorization: `Bearer ${await new SignJWT({ role, htm: method, htu: url, ...(body === undefined ? {} : { bh: bodyHash(body) }) })
        .setProtectedHeader({ alg: "EdDSA" }).setIssuer(ISSUER).setAudience(AUDIENCE).setSubject("ops@kabala.app")
        .setIssuedAt().setExpirationTime("60s").setJti(randomUUID()).sign(privateKey)}` },
    });
  let sale = 0;
  const pay = async (email = "dana@gmail.com") => {
    const { userId } = await t.login(email);
    const plan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;
    const sub = t.paypal.approve(plan, userId, { nextBillingTime: "2026-11-21T10:00:00Z" });
    await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
    await t.webhook("PAYMENT.SALE.COMPLETED", { id: `SALE-${++sale}`, billing_agreement_id: sub.id, amount: { total: "29.00", currency: "ILS" }, state: "completed", create_time: "2026-10-21T10:00:00Z" });
  };
  const mails = () => (mailer ?? t.mailer).sent.filter((m) => m.attachments?.length);
  return { t, call, pay, mails };
}

describe("automatic sending settings (admin panel)", () => {
  it("defaults: on, built-in wording, automatic retry on", async () => {
    const { t, call } = await env();
    const s = (await call("GET", "/api/admin/receipt-settings")).json();
    expect(s).toMatchObject({ autoSend: true, autoRetry: true, copyTo: null, subjectHe: null, introHe: null });
    expect(Object.keys(s.variables)).toEqual(["name", "number", "amount", "month", "plan", "business"]);
    await t.close();
  });

  it("custom subject, opening line and a hidden copy apply to the next receipt", async () => {
    const { t, call, pay, mails } = await env();
    const r = await call("POST", "/api/admin/receipt-settings", { subjectHe: "קבלה {{number}} על {{amount}}", introHe: "היי {{name}}, תודה ש{{business}} איתך!", copyTo: "Archive@Kabala.app" });
    expect(r.json()).toMatchObject({ subjectHe: "קבלה {{number}} על {{amount}}", copyTo: "archive@kabala.app", updatedBy: "ops@kabala.app" });
    await pay();
    const [m] = mails();
    expect(m!.subject).toBe("קבלה 000001 על ₪29.00");
    expect(m!.text).toContain("תודה שKabala איתך!");
    expect(m!.html).toContain("תודה שKabala איתך!");
    expect(m!.bcc).toEqual(["archive@kabala.app"]);
    await t.close();
  });

  it("refuses broken or unsafe templates and bad copy addresses, saving nothing", async () => {
    const { t, call } = await env();
    for (const body of [{ subjectHe: "{{#name}}x{{/name}}" }, { introHe: "{{{name}}}" }, { introHe: "שלום {{nope}}" }, { subjectHe: "{{name" }, { copyTo: "a@b.co,evil@x.com" }]) {
      const r = await call("POST", "/api/admin/receipt-settings", body);
      expect(r.statusCode, JSON.stringify(body)).toBe(400);
    }
    expect((await call("POST", "/api/admin/receipt-settings", { introHe: "שלום {{nope}}" })).json()).toMatchObject({ field: "intro" });
    expect((await call("GET", "/api/admin/receipt-settings")).json()).toMatchObject({ subjectHe: null, introHe: null, copyTo: null });
    await t.close();
  });

  it("a subject can't add mail headers, even from a customer's name", async () => {
    const { t, call, pay, mails } = await env();
    await call("POST", "/api/admin/receipt-settings", { subjectHe: "קבלה ל-{{name}}" });
    await pay();
    await t.db.update(receipts).set({ customerName: "Dana\r\nBcc: evil@x.com" }); // e.g. a PayPal name with line breaks
    const [r] = await t.db.select().from(receipts);
    await t.pipeline.adminSend(r!.id);
    const last = mails().at(-1)!;
    expect(last.subject).toBe("קבלה ל-Dana Bcc: evil@x.com");
    expect(last.subject).not.toMatch(/[\r\n]/);
    await t.close();
  });

  it("with automatic sending off, the PDF is made but waits for a manual send", async () => {
    const { t, call, pay, mails } = await env();
    await call("POST", "/api/admin/receipt-settings", { autoSend: false });
    await pay();
    expect(mails()).toHaveLength(0);
    const [r] = (await call("GET", "/api/admin/receipts")).json().receipts;
    expect(r.status).toBe("generated");
    await t.pipeline.sweep(); // the safety net must not send it either
    expect(mails()).toHaveLength(0);
    expect((await call("POST", `/api/admin/receipts/${r.id}/send`, {})).json()).toEqual({ ok: true });
    expect(mails()).toHaveLength(1);
    await t.close();
  });

  it("preview and test email use the draft without saving it", async () => {
    const { t, call, pay, mails } = await env();
    expect((await call("POST", "/api/admin/receipt-settings/test", {})).statusCode).toBe(409); // no receipt yet
    await pay();
    const draft = { subjectHe: "טיוטה {{number}}", introHe: "שורת פתיחה חדשה" };
    expect((await call("POST", "/api/admin/receipt-settings/preview", draft)).json()).toEqual({ subject: "טיוטה 000001", intro: "שורת פתיחה חדשה" });
    expect((await call("POST", "/api/admin/receipt-settings/test", draft)).json()).toEqual({ ok: true, to: "ops@kabala.app" });
    const test = mails().at(-1)!;
    expect(test).toMatchObject({ to: "ops@kabala.app", subject: "[בדיקה / test] טיוטה 000001", bcc: [] });
    expect((await call("GET", "/api/admin/receipt-settings")).json().subjectHe).toBeNull(); // not saved
    await t.close();
  });

  it("failed receipts are retried automatically, an hour apart, a limited number of times", async () => {
    const mailer = new SwitchMailer();
    mailer.down = true;
    const { t, pay, mails, call } = await env(mailer);
    await pay();
    const status = async () => (await t.db.select().from(receipts))[0]!.status;
    expect(await status()).toBe("failed");
    await t.pipeline.sweep();
    expect(await status()).toBe("failed"); // not yet: less than an hour
    for (let round = 1; round <= AUTO_RETRY.rounds + 1; round++) {
      t.setNow(new Date(t.now().getTime() + AUTO_RETRY.everyMs + 60_000));
      await t.pipeline.sweep();
    }
    const logs = (await call("GET", `/api/admin/receipts/${(await t.db.select().from(receipts))[0]!.id}`)).json().emails;
    expect(logs.filter((l: { trigger: string }) => l.trigger === "retry")).toHaveLength(AUTO_RETRY.rounds); // and no more
    mailer.down = false;
    await call("POST", "/api/admin/receipt-settings", { autoRetry: false });
    t.setNow(new Date(t.now().getTime() + 10 * AUTO_RETRY.everyMs));
    await t.pipeline.sweep();
    expect(mails()).toHaveLength(0);
    await t.close();
  });

  it("the provider coming back during the retries delivers the receipt", async () => {
    const mailer = new SwitchMailer();
    mailer.down = true;
    const { t, pay, mails } = await env(mailer);
    await pay();
    mailer.down = false;
    t.setNow(new Date(t.now().getTime() + AUTO_RETRY.everyMs + 60_000));
    await t.pipeline.sweep();
    expect(mails()).toHaveLength(1);
    expect((await t.db.select().from(receipts))[0]!.status).toBe("sent");
    await t.close();
  });

  it("viewers can read the settings but not change or test them", async () => {
    const { t, call } = await env();
    await call("POST", "/api/admin/receipt-settings", { copyTo: "archive@kabala.app" });
    expect((await call("GET", "/api/admin/receipt-settings", undefined, "viewer")).json().copyTo).toBe("ar***@kabala.app");
    expect((await call("POST", "/api/admin/receipt-settings", { autoSend: false }, "viewer")).statusCode).toBe(403);
    expect((await call("POST", "/api/admin/receipt-settings/test", {}, "viewer")).statusCode).toBe(403);
    await t.close();
  });
});
