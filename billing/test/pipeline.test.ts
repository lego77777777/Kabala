import { eq } from "drizzle-orm";
import { describe, expect, it } from "vitest";
import { emailLogs, payments, receipts, users } from "../src/db/schema.js";
import { ConsoleMailer, type Mail } from "../src/email/mailer.js";
import { renderReceiptEmail, SAMPLE } from "../src/email/templates/receipt-email.js";
import { InternalProvider } from "../src/providers/internal.js";
import { startQueue } from "../src/queue/queue.js";
import { taxPolicy } from "../src/receipts/amounts.js";
import { ReceiptPipeline } from "../src/receipts/pipeline.js";
import { ReceiptService } from "../src/receipts/service.js";
import { MemoryStorage } from "../src/storage/storage.js";
import { SELLER, setup } from "./helpers.js";

type Ctx = Awaited<ReturnType<typeof setup>>;

async function paidCustomer(t: Ctx, email = "dana@gmail.com") {
  const { userId, auth } = await t.login(email);
  const plan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;
  const sub = t.paypal.approve(plan, userId, { nextBillingTime: "2026-11-21T10:00:00Z" });
  await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
  return { userId, auth, sub };
}

const sale = (subId: string, id = "SALE-1", total = "29.00") => ({ id, billing_agreement_id: subId, amount: { total, currency: "ILS" }, create_time: "2026-10-21T10:00:00Z" });
const receiptMails = (m: ConsoleMailer) => m.sent.filter((x) => x.attachments?.length);

/** Fails the first `failures` sends to customers; admin alerts always go through. */
class FlakyMailer extends ConsoleMailer {
  calls = 0;
  constructor(private failures: number) {
    super();
  }
  override async send(mail: Mail) {
    if (!mail.subject.startsWith("[Kabala billing]") && this.calls++ < this.failures) throw new Error("SMTP 451 temporary failure");
    return super.send(mail);
  }
}

describe("payment → receipt → customer's inbox", () => {
  it("a verified payment emails the customer a Hebrew receipt with the PDF attached", async () => {
    const t = await setup();
    const { sub } = await paidCustomer(t);
    expect(receiptMails(t.mailer)).toHaveLength(0); // ACTIVATED alone is not a payment: no receipt

    expect((await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id))).json()).toEqual({ outcome: "payment:recorded" });

    const [mail] = receiptMails(t.mailer);
    expect(mail).toBeDefined();
    expect(mail!.to).toBe("dana@gmail.com");
    expect(mail!.bcc).toEqual(["records@kabala.test"]); // the business keeps a hidden copy
    expect(mail!.subject).toBe("הקבלה שלך מ-Kabala – אוקטובר 2026");
    expect(mail!.html).toContain('dir="rtl"');
    expect(mail!.html).toContain("₪29.00");
    expect(mail!.html).toContain("צפה במנוי שלי");
    expect(mail!.text).toContain("₪29.00"); // plain-text twin
    expect(mail!.text).toMatch(/http:\/\/localhost:8080\/r\/[0-9a-f-]+\/\d+\/[\w-]+\.pdf/); // signed link
    expect(mail!.attachments![0]!.filename).toBe("kabala-receipt-000001.pdf");
    expect(mail!.attachments![0]!.content.subarray(0, 4).toString()).toBe("%PDF");

    const [r] = await t.db.select().from(receipts);
    expect(r).toMatchObject({ status: "sent", number: 1, amountMinor: 2900, customerName: "דנה כהן", provider: "internal" });
    const [log] = await t.db.select().from(emailLogs);
    expect(log).toMatchObject({ status: "sent", trigger: "auto", attempts: 1, toAddresses: "dana@gmail.com" });
    await t.close();
  });

  it("one receipt and one email per payment, however many times PayPal notifies", async () => {
    const t = await setup();
    const { sub } = await paidCustomer(t);
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id), { id: "WH-1" });
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id), { id: "WH-1" }); // same delivery
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id), { id: "WH-2" }); // re-notification
    expect(await t.db.select().from(receipts)).toHaveLength(1);
    expect(receiptMails(t.mailer)).toHaveLength(1);
    // a renewal is a new payment: a new receipt with the next number
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id, "SALE-2"));
    expect((await t.db.select().from(receipts)).map((r) => r.number).sort()).toEqual([1, 2]);
    expect(receiptMails(t.mailer)).toHaveLength(2);
    await t.close();
  });

  it("copies the accountant and writes in the user's language", async () => {
    const t = await setup();
    const { sub, auth } = await paidCustomer(t);
    const saved = await t.app.inject({ method: "POST", url: "/api/account/receipt-settings", headers: auth, payload: { ccEmail: "CPA@Office.co.il", locale: "en" } });
    expect(saved.json().receiptSettings).toMatchObject({ ccEmail: "cpa@example.com", locale: "en" });
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id));
    const [mail] = receiptMails(t.mailer);
    expect(mail!.cc).toEqual(["cpa@example.com"]);
    expect(mail!.subject).toBe("Your Kabala receipt – October 2026");
    expect(mail!.html).toContain('dir="ltr"');
    expect((await t.app.inject({ method: "POST", url: "/api/account/receipt-settings", headers: auth, payload: { ccEmail: "not-an-email" } })).statusCode).toBe(400);
    await t.close();
  });

  it("retries a failing send with backoff and records every attempt", async () => {
    const mailer = new FlakyMailer(2);
    const t = await setup({ mailer });
    const { sub } = await paidCustomer(t);
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id));
    expect(receiptMails(mailer)).toHaveLength(1);
    const [log] = await t.db.select().from(emailLogs);
    expect(log).toMatchObject({ status: "sent", attempts: 3, error: null });
    expect((await t.db.select().from(receipts))[0]!.status).toBe("sent");
    await t.close();
  });

  it("after 5 failed attempts: receipt marked failed and the admin is alerted", async () => {
    const mailer = new FlakyMailer(99);
    const t = await setup({ mailer });
    const { sub } = await paidCustomer(t);
    expect((await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id))).statusCode).toBe(200); // the payment itself is safe
    const [r] = await t.db.select().from(receipts);
    expect(r).toMatchObject({ status: "failed" });
    expect(r!.lastError).toContain("SMTP 451");
    const [log] = await t.db.select().from(emailLogs);
    expect(log).toMatchObject({ status: "failed", attempts: 5 });
    const alert = mailer.sent.find((m) => m.to === "admin@kabala.test");
    expect(alert?.subject).toBe("[Kabala billing] receipt 1 failed");
    expect(alert?.text).not.toMatch(/secret|card/i);
    await t.close();
  });

  it("a refund issues a credit document and emails it", async () => {
    const t = await setup();
    const { sub } = await paidCustomer(t);
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id));
    await t.webhook("PAYMENT.SALE.REFUNDED", { id: "REFUND-1", sale_id: "SALE-1", amount: { total: "29.00", currency: "ILS" } });
    await t.webhook("PAYMENT.SALE.REFUNDED", { id: "REFUND-1", sale_id: "SALE-1", amount: { total: "29.00", currency: "ILS" } });
    const refunds = await t.db.select().from(receipts).where(eq(receipts.kind, "refund"));
    expect(refunds).toHaveLength(1);
    expect(refunds[0]).toMatchObject({ number: 1, amountMinor: 2900, status: "sent", paypalTransactionId: "REFUND-1" });
    const mail = receiptMails(t.mailer).at(-1)!;
    expect(mail.subject).toBe("אישור החזר כספי מ-Kabala – אוקטובר 2026");
    expect(mail.attachments![0]!.filename).toBe("kabala-refund-000001.pdf");
    await t.close();
  });
});

describe("account: receipts list, download, send again, signed links", () => {
  it("lists receipts, downloads the PDF, re-sends on request, and protects other users", async () => {
    const t = await setup();
    const { sub, auth } = await paidCustomer(t);
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id));
    const acc = (await t.app.inject({ url: "/api/account", headers: auth })).json();
    expect(acc.receipts).toEqual([expect.objectContaining({ kind: "receipt", number: 1, amount: "29.00", status: "sent", ready: true })]);
    const id = acc.receipts[0].id;

    const pdf = await t.app.inject({ url: `/api/receipts/${id}/pdf`, headers: auth });
    expect(pdf.headers["content-disposition"]).toContain("kabala-receipt-000001.pdf");
    expect(pdf.rawPayload.subarray(0, 4).toString()).toBe("%PDF");

    expect((await t.app.inject({ method: "POST", url: `/api/receipts/${id}/resend`, headers: auth })).json()).toEqual({ ok: true });
    expect(receiptMails(t.mailer)).toHaveLength(2);
    expect((await t.db.select().from(emailLogs)).map((l) => l.trigger).sort()).toEqual(["auto", "resend"]);

    const eve = await t.login("eve@gmail.com");
    expect((await t.app.inject({ url: `/api/receipts/${id}/pdf`, headers: eve.auth })).statusCode).toBe(404);
    expect((await t.app.inject({ method: "POST", url: `/api/receipts/${id}/resend`, headers: eve.auth })).statusCode).toBe(404);
    expect((await t.app.inject({ url: `/api/receipts/${id}/pdf` })).statusCode).toBe(401);
    await t.close();
  });

  it("signed links work without login, but not altered, for another receipt, or after expiry", async () => {
    const t = await setup();
    const { sub } = await paidCustomer(t);
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id));
    const [r] = await t.db.select().from(receipts);
    const path = (u: string) => u.replace("http://localhost:8080", "");
    const good = path(t.links.url(r!.id, 3600));
    expect((await t.app.inject({ url: good })).rawPayload.subarray(0, 4).toString()).toBe("%PDF");
    const parts = good.split("/");
    const longer = [...parts.slice(0, 3), String(Number(parts[3]) + 86_400), parts[4]].join("/");
    expect((await t.app.inject({ url: longer })).statusCode).toBe(403); // extended expiry
    expect((await t.app.inject({ url: good.replace(r!.id, "00000000-0000-4000-8000-000000000000") })).statusCode).toBe(403);
    const expired = path(t.links.url(r!.id, -10));
    expect((await t.app.inject({ url: expired })).statusCode).toBe(403);
    await t.close();
  });
});

describe("no payment is left without a receipt", () => {
  it("the sweep issues receipts for payments that have none and re-queues stuck ones", async () => {
    const t = await setup();
    const { sub, userId } = await paidCustomer(t);
    t.queue.autoRun = false; // simulate a crash: jobs are queued but never run
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id));
    const [stuck] = await t.db.select().from(receipts);
    expect(stuck!.status).toBe("pending");
    // and a payment whose receipt row was never created at all
    const [p] = await t.db.select().from(payments);
    await t.db.insert(payments).values({ userId, subscriptionId: p!.subscriptionId, paypalSaleId: "SALE-ORPHAN", amountMinor: 2900, currency: "ILS", paidAt: new Date() });

    t.queue.autoRun = true;
    t.setNow(new Date("2026-10-21T11:00:00Z")); // stuck for an hour
    const result = await t.pipeline.sweep();
    expect(result.created).toBe(1);
    expect((await t.db.select().from(receipts)).map((r) => r.status).sort()).toEqual(["sent", "sent"]);
    expect(receiptMails(t.mailer)).toHaveLength(2);
    // running it again changes nothing
    expect(await t.pipeline.sweep()).toEqual({ created: 0, requeued: 0 });
    expect(receiptMails(t.mailer)).toHaveLength(2);
    await t.close();
  });
});

describe("email template", () => {
  it("renders Hebrew RTL HTML with dark-mode support and a plain-text version", async () => {
    const { html, text } = await renderReceiptEmail(SAMPLE);
    expect(html).toContain('lang="he"');
    expect(html).toContain('dir="rtl"');
    expect(html).toContain("prefers-color-scheme: dark");
    expect(html).toContain("שלום דנה כהן,");
    expect(text).toContain("₪279.00");
    expect(text).toContain("צפה במנוי שלי");
    expect(text).not.toContain("<");
  });
});

describe("pg-boss on PGlite (the real queue)", () => {
  it("runs the whole flow in the background: payment → sent", async () => {
    const t = await setup();
    const { sub, userId } = await paidCustomer(t);
    const queue = await startQueue(t.database.queue, { attempts: 2, delaySeconds: 1 }, { pollingIntervalSeconds: 0.5, schedules: false });
    const mailer = new ConsoleMailer();
    const pipeline = new ReceiptPipeline({
      db: t.db,
      receipts: new ReceiptService({ db: t.db, tax: taxPolicy("exempt", 18), provider: "internal" }),
      provider: new InternalProvider(),
      storage: new MemoryStorage(),
      mailer,
      links: t.links,
      seller: SELLER,
      queue,
      accountUrl: "http://localhost:8080/account",
    });
    await pipeline.start();
    await t.db.update(users).set({ name: "Dana" }).where(eq(users.id, userId));
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id, "SALE-BG")); // the helper's inline pipeline sends one…
    const [paid] = await t.db.select().from(payments).where(eq(payments.paypalSaleId, "SALE-BG"));
    // …and a second payment goes through the real pg-boss pipeline
    const [p2] = await t.db.insert(payments).values({ userId, subscriptionId: paid!.subscriptionId, paypalSaleId: "SALE-BG-2", amountMinor: 2900, currency: "ILS", paidAt: new Date() }).returning();
    const started = Date.now();
    const r = await pipeline.onPayment(p2!.id);
    expect(r.status).toBe("pending"); // returned immediately; the work happens in the background
    let status = "pending";
    while (status !== "sent" && Date.now() - started < 30_000) {
      await new Promise((res) => setTimeout(res, 300));
      status = (await t.db.select().from(receipts).where(eq(receipts.id, r.id)))[0]!.status;
    }
    expect(status).toBe("sent");
    expect(mailer.sent).toHaveLength(1);
    expect(Date.now() - started).toBeLessThan(60_000); // spec: in the inbox within a minute
    await queue.stop();
    await t.close();
  }, 60_000);
});
