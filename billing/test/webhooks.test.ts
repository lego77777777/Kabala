import { describe, expect, it } from "vitest";
import { eq } from "drizzle-orm";
import { payments, receipts, webhookEvents } from "../src/db/schema.js";
import { setup } from "./helpers.js";

async function subscribed(t: Awaited<ReturnType<typeof setup>>, interval: "monthly" | "yearly" = "monthly") {
  const { userId } = await t.login();
  const plan = (await t.billing.planFor(interval, "ILS")).paypalPlanId;
  const sub = t.paypal.approve(plan, userId, { nextBillingTime: "2026-11-21T10:00:00Z" });
  return { userId, sub };
}

const sale = (subId: string, id = "SALE-1", total = "29.00", currency = "ILS") => ({
  id,
  billing_agreement_id: subId,
  amount: { total, currency },
  state: "completed",
  create_time: "2026-10-21T10:00:00Z",
});

describe("POST /api/webhooks/paypal", () => {
  it("rejects unverified webhooks, audits them, and changes nothing", async () => {
    const t = await setup();
    const { userId, sub } = await subscribed(t);
    const r = await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id }, { sig: "forged", id: "WH-REAL-ID" });
    expect(r.statusCode).toBe(400);
    expect((await t.billing.entitlement(userId)).tier).toBe("free");
    const audit = await t.db.select().from(webhookEvents);
    expect(audit).toHaveLength(1);
    expect(audit[0]).toMatchObject({ verified: false, processedAt: null });
    expect(audit[0]!.id).toMatch(/^unverified:/); // a forgery can't occupy the real event id
    // the genuine event with that id is still processed later
    expect((await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id }, { id: "WH-REAL-ID" })).json()).toEqual({ outcome: "subscription:active" });
    await t.close();
  });

  it("ACTIVATED (verified) grants Pro even if the browser never came back", async () => {
    const t = await setup();
    const { userId, sub } = await subscribed(t);
    const r = await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id, status: "ACTIVE" });
    expect(r.json()).toEqual({ outcome: "subscription:active" });
    expect((await t.billing.entitlement(userId)).tier).toBe("pro");
    await t.close();
  });

  it("is idempotent: the same event id is processed once", async () => {
    const t = await setup();
    const { sub } = await subscribed(t);
    await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
    const first = await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id), { id: "WH-PAY-1" });
    const second = await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id), { id: "WH-PAY-1" });
    expect(first.json()).toEqual({ outcome: "payment:recorded" });
    expect(second.json()).toEqual({ outcome: "duplicate" });
    expect(await t.db.select().from(payments)).toHaveLength(1);
    expect(await t.db.select().from(receipts)).toHaveLength(1);
    expect(t.mailer.sent.filter((m) => m.subject.includes("קבלה"))).toHaveLength(1);
    // a different event about the same sale (PayPal re-notification) doesn't double-book either
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id), { id: "WH-PAY-1-again" });
    expect(await t.db.select().from(payments)).toHaveLength(1);
    await t.close();
  });

  it("records payments with running receipt numbers and emails the PDF", async () => {
    const t = await setup();
    const { sub } = await subscribed(t);
    await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id, "SALE-1"));
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id, "SALE-2"));
    const nums = (await t.db.select().from(receipts)).map((i) => i.number).sort();
    expect(nums).toEqual([nums[0], nums[0]! + 1]);
    const mail = t.mailer.sent.find((m) => m.attachments?.length);
    expect(mail?.attachments?.[0]?.content.subarray(0, 4).toString()).toBe("%PDF");
    await t.close();
  });

  it("checks every amount against our plan, never trusting the payload", async () => {
    const t = await setup();
    const { userId, sub } = await subscribed(t);
    const r = await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id, "SALE-X", "1.00"));
    expect(r.json()).toEqual({ outcome: "ignored:subscription_mismatch" });
    expect(await t.db.select().from(payments)).toHaveLength(0);
    const [audit] = await t.db.select().from(webhookEvents).where(eq(webhookEvents.resourceId, "SALE-X"));
    expect(audit!.error).toContain("does not match plan");
    void userId;
    await t.close();
  });

  it("PAYMENT.FAILED → 7 days of grace + one email, then back to Free", async () => {
    const t = await setup();
    const { userId, sub } = await subscribed(t);
    await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
    await t.webhook("BILLING.SUBSCRIPTION.PAYMENT.FAILED", { id: sub.id });
    await t.webhook("BILLING.SUBSCRIPTION.PAYMENT.FAILED", { id: sub.id });
    const acc = await t.billing.account(userId);
    expect(acc.subscription).toMatchObject({ status: "past_due", graceUntil: "2026-10-28T10:00:00.000Z" });
    expect(acc.tier).toBe("pro");
    expect(t.mailer.sent.filter((m) => m.subject.includes("לא עבר"))).toHaveLength(1);
    t.setNow(new Date("2026-10-28T10:00:01Z"));
    expect((await t.billing.entitlement(userId)).tier).toBe("free");
    await t.close();
  });

  it("CANCELLED, SUSPENDED and EXPIRED follow PayPal's real state", async () => {
    const t = await setup();
    const { userId, sub } = await subscribed(t);
    await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
    t.paypal.subs.get(sub.id)!.status = "SUSPENDED";
    expect((await t.webhook("BILLING.SUBSCRIPTION.SUSPENDED", { id: sub.id })).json()).toEqual({ outcome: "subscription:past_due" });
    t.paypal.subs.get(sub.id)!.status = "CANCELLED";
    expect((await t.webhook("BILLING.SUBSCRIPTION.CANCELLED", { id: sub.id })).json()).toEqual({ outcome: "subscription:cancelled" });
    t.paypal.subs.get(sub.id)!.status = "EXPIRED";
    expect((await t.webhook("BILLING.SUBSCRIPTION.EXPIRED", { id: sub.id })).json()).toEqual({ outcome: "subscription:expired" });
    expect((await t.billing.entitlement(userId)).tier).toBe("free");
    // an old ACTIVATED arriving late can't bring it back
    t.paypal.subs.get(sub.id)!.status = "ACTIVE";
    await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
    expect((await t.billing.entitlement(userId)).tier).toBe("free");
    await t.close();
  });

  it("PAYMENT.SALE.REFUNDED in full ends Pro; partial refunds are recorded", async () => {
    const t = await setup();
    const { userId, sub } = await subscribed(t);
    await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
    await t.webhook("PAYMENT.SALE.COMPLETED", sale(sub.id));
    expect((await t.webhook("PAYMENT.SALE.REFUNDED", { id: "REF-1", sale_id: "SALE-1", amount: { total: "9.00", currency: "ILS" } })).json()).toEqual({ outcome: "payment:partially_refunded" });
    expect((await t.billing.entitlement(userId)).tier).toBe("pro");
    expect((await t.webhook("PAYMENT.SALE.REFUNDED", { id: "REF-2", sale_id: "SALE-1", amount: { total: "20.00", currency: "ILS" } })).json()).toEqual({ outcome: "payment:refunded" });
    expect((await t.billing.entitlement(userId)).tier).toBe("free");
    await t.close();
  });

  it("asks PayPal to retry (500) when PayPal itself is unavailable", async () => {
    const t = await setup();
    const { sub } = await subscribed(t);
    t.paypal.down = true;
    expect((await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id }, { id: "WH-RETRY" })).statusCode).toBe(500);
    t.paypal.down = false;
    expect((await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id }, { id: "WH-RETRY" })).json()).toEqual({ outcome: "subscription:active" });
    await t.close();
  });
});

describe("reconcile", () => {
  it("fixes drift from PayPal and expires lapsed grace periods", async () => {
    const t = await setup();
    const { userId, sub } = await subscribed(t);
    await t.billing.confirmCheckout(userId, sub.id);
    // PayPal cancelled it but the webhook was lost
    t.paypal.subs.get(sub.id)!.status = "CANCELLED";
    const r = await t.billing.reconcile();
    expect(r.fixed).toEqual([{ id: sub.id, from: "active", to: "cancelled" }]);

    // a second user whose payments keep failing while PayPal still says ACTIVE
    const bob = await t.login("bob@gmail.com");
    const s2 = t.paypal.approve((await t.billing.planFor("monthly", "ILS")).paypalPlanId, bob.userId);
    await t.billing.confirmCheckout(bob.userId, s2.id);
    t.paypal.subs.get(s2.id)!.failedPayments = 2;
    await t.billing.reconcile();
    expect((await t.billing.account(bob.userId)).subscription?.status).toBe("past_due");
    t.setNow(new Date("2026-11-01T00:00:00Z"));
    const later = await t.billing.reconcile();
    expect(later.fixed).toContainEqual({ id: s2.id, from: "past_due", to: "expired" });
    await t.close();
  });
});
