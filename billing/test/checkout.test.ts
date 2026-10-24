import { describe, expect, it } from "vitest";
import { setup } from "./helpers.js";

describe("GET /api/pricing", () => {
  it("serves prices from pricing.config.ts with the synced PayPal plan ids", async () => {
    const t = await setup();
    const r = await t.app.inject({ url: "/api/pricing" });
    expect(r.statusCode).toBe(200);
    const body = r.json();
    expect(body.currency).toBe("ILS");
    expect(body.tiers.pro.prices.monthly).toMatchObject({ amount: "29.00", paypalPlanId: expect.stringMatching(/^P-/) });
    expect(body.tiers.pro.prices.yearly).toMatchObject({ amount: "279.00", savingsMinor: 6900, savingsPercent: 20 });
    expect((await t.app.inject({ url: "/api/client-config" })).json()).toMatchObject({ paypalClientId: "sandbox-client-id", mode: "sandbox" });
    await t.close();
  });
});

describe("checkout: Pro only after PayPal confirms", () => {
  it("full flow: session → link → start → PayPal approve → confirm → Pro license", async () => {
    const t = await setup();
    const { auth, license, userId } = await t.login();
    expect((await t.tokens.verify(license, "license")).plan).toBe("free");

    const link = (await t.app.inject({ method: "POST", url: "/api/links", headers: auth, payload: { page: "checkout" } })).json();
    expect(link.url).toMatch(/^http:\/\/localhost:8080\/checkout#t=/);
    const linkToken = decodeURIComponent(link.url.split("#t=")[1]);
    const lauth = { authorization: `Bearer ${linkToken}` };

    const start = (await t.app.inject({ method: "POST", url: "/api/checkout/start", headers: lauth, payload: { interval: "yearly" } })).json();
    expect(start.customId).toBe(userId);
    expect(t.paypal.plans.get(start.planId)).toMatchObject({ price: "279.00", interval: "yearly" }); // chosen by the server

    const sub = t.paypal.approve(start.planId, start.customId); // buyer approves in PayPal's window
    const confirm = await t.app.inject({ method: "POST", url: "/api/checkout/confirm", headers: lauth, payload: { subscriptionId: sub.id } });
    expect(confirm.statusCode).toBe(200);
    expect(confirm.json()).toMatchObject({ status: "active", entitled: true, deepLink: "kabala://payment-success?status=pro" });
    expect(t.paypal.calls).toContain("getSubscription"); // verified against PayPal, not trusted from the browser

    const lic = (await t.app.inject({ url: "/api/license", headers: auth })).json();
    const claims = await t.tokens.verify(lic.license, "license");
    expect(claims).toMatchObject({ plan: "pro", limits: { monthlyReceipts: null, export: true } });
    expect(claims.exp! - claims.iat!).toBe(7 * 86_400); // offline for up to 7 days
    await t.close();
  });

  it("rejects someone else's subscription, foreign plans, and unpaid approvals", async () => {
    const t = await setup();
    const dana = await t.login("dana@gmail.com");
    const eve = await t.login("eve@gmail.com");
    const plan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;

    const danasSub = t.paypal.approve(plan, dana.userId);
    const steal = await t.app.inject({ method: "POST", url: "/api/checkout/confirm", headers: eve.auth, payload: { subscriptionId: danasSub.id } });
    expect(steal.statusCode).toBe(403);

    const foreign = t.paypal.approve("P-SOMEONE-ELSES-PLAN", eve.userId);
    expect((await t.app.inject({ method: "POST", url: "/api/checkout/confirm", headers: eve.auth, payload: { subscriptionId: foreign.id } })).statusCode).toBe(403);

    const pending = t.paypal.approve(plan, eve.userId, { status: "APPROVAL_PENDING" });
    expect((await t.app.inject({ method: "POST", url: "/api/checkout/confirm", headers: eve.auth, payload: { subscriptionId: pending.id } })).json()).toEqual({ error: "not_active_yet" });

    expect((await t.billing.entitlement(eve.userId)).tier).toBe("free");
    await t.close();
  });

  it("requires auth and validates input", async () => {
    const t = await setup();
    expect((await t.app.inject({ method: "POST", url: "/api/checkout/start", payload: { interval: "monthly" } })).statusCode).toBe(401);
    expect((await t.app.inject({ method: "POST", url: "/api/session", payload: { id_token: "forged-token-xyz" } })).statusCode).toBe(401);
    const { auth } = await t.login();
    expect((await t.app.inject({ method: "POST", url: "/api/checkout/start", headers: auth, payload: { interval: "weekly" } })).statusCode).toBe(400);
    expect((await t.app.inject({ method: "POST", url: "/api/checkout/confirm", headers: auth, payload: { subscriptionId: "../../etc" } })).statusCode).toBe(400);
    await t.close();
  });

  it("returns a friendly error when PayPal is down, and grants nothing", async () => {
    const t = await setup();
    const { auth, userId } = await t.login();
    const sub = t.paypal.approve((await t.billing.planFor("monthly", "ILS")).paypalPlanId, userId);
    t.paypal.down = true;
    const r = await t.app.inject({ method: "POST", url: "/api/checkout/confirm", headers: auth, payload: { subscriptionId: sub.id } });
    expect(r.statusCode).toBe(502);
    expect(r.json()).toEqual({ error: "paypal_unavailable" });
    expect((await t.billing.entitlement(userId)).tier).toBe("free");
    await t.close();
  });
});

describe("account: history, receipts, one-click cancel", () => {
  it("cancels at PayPal and keeps Pro until the paid period ends", async () => {
    const t = await setup();
    const { auth, userId } = await t.login();
    const plan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;
    const sub = t.paypal.approve(plan, userId, { nextBillingTime: "2026-11-21T10:00:00Z" });
    await t.billing.confirmCheckout(userId, sub.id);
    await t.webhook("PAYMENT.SALE.COMPLETED", { id: "SALE-1", billing_agreement_id: sub.id, amount: { total: "29.00", currency: "ILS" }, create_time: "2026-10-21T10:00:00Z" });

    const acc = (await t.app.inject({ url: "/api/account", headers: auth })).json();
    expect(acc.subscription).toMatchObject({ status: "active", nextBillingDate: "2026-11-21T10:00:00.000Z", canCancel: true });
    expect(acc.payments).toHaveLength(1);
    const receiptId = acc.payments[0].receipt.id;
    const pdf = await t.app.inject({ url: `/api/receipts/${receiptId}/pdf`, headers: auth });
    expect(pdf.headers["content-type"]).toBe("application/pdf");
    expect(pdf.rawPayload.subarray(0, 4).toString()).toBe("%PDF");

    const cancelled = (await t.app.inject({ method: "POST", url: "/api/account/cancel", headers: auth })).json();
    expect(t.paypal.subs.get(sub.id)!.status).toBe("CANCELLED");
    expect(cancelled.subscription).toMatchObject({ status: "cancelled", accessUntil: "2026-11-21T10:00:00.000Z", canCancel: false });
    expect(cancelled.tier).toBe("pro");
    t.setNow(new Date("2026-11-22T00:00:00Z"));
    expect((await t.billing.entitlement(userId)).tier).toBe("free");

    // someone else can't read this user's receipt
    const other = await t.login("eve@gmail.com");
    expect((await t.app.inject({ url: `/api/receipts/${receiptId}/pdf`, headers: other.auth })).statusCode).toBe(404);
    await t.close();
  });
});
