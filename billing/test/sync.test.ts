import { afterEach, describe, expect, it } from "vitest";
import { eq } from "drizzle-orm";
import { pricingConfig } from "../src/config/pricing.config.js";
import { plans, subscriptions } from "../src/db/schema.js";
import { checkPricing, syncPricing } from "../src/services/sync-pricing.js";
import { setup } from "./helpers.js";

const prices = pricingConfig.tiers.pro.prices.ILS as { monthly: number; yearly: number };
const original = { ...prices };
afterEach(() => Object.assign(prices, original));

describe("sync-pricing: pricing.config.ts → PayPal", () => {
  it("creates one product and a plan per interval, then is a no-op", async () => {
    const t = await setup({ sync: false });
    const first = await syncPricing(t.db, t.paypal);
    expect(t.paypal.products.size).toBe(1);
    expect(first.created.map((c) => [c.interval, c.price])).toEqual([["monthly", "29.00"], ["yearly", "279.00"]]);
    const second = await syncPricing(t.db, t.paypal);
    expect(second.created).toEqual([]);
    expect(second.unchanged).toHaveLength(2);
    expect(await checkPricing(t.db, t.paypal)).toEqual([]);
    await t.close();
  });

  it("a price change creates a NEW plan; existing subscribers keep the old one (grandfathering)", async () => {
    const t = await setup();
    const { userId } = await t.login();
    const oldPlan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;
    const sub = t.paypal.approve(oldPlan, userId);
    await t.billing.confirmCheckout(userId, sub.id);

    prices.monthly = 3500; // ₪35
    const r = await syncPricing(t.db, t.paypal);
    expect(r.created).toEqual([expect.objectContaining({ interval: "monthly", price: "35.00" })]);
    expect(r.deactivated).toEqual([{ planId: oldPlan, price: "29.00 ILS", subscribersKept: 1 }]);
    expect(t.paypal.plans.get(oldPlan)!.price).toBe("29.00"); // never edited in place
    expect(t.paypal.plans.get(oldPlan)!.status).toBe("INACTIVE");

    // new checkouts get the new price; the existing subscription still points at the old plan
    expect((await t.billing.planFor("monthly", "ILS")).amountMinor).toBe(3500);
    const pricing = await t.billing.pricing();
    expect(pricing.tiers.pro.prices.monthly.amount).toBe("35.00");
    const [row] = await t.db.select().from(subscriptions).innerJoin(plans, eq(plans.id, subscriptions.planId));
    expect(row!.plans.paypalPlanId).toBe(oldPlan);
    expect(await t.billing.entitlement(userId)).toMatchObject({ tier: "pro" });

    // going back to the old price reactivates the old plan instead of creating a third
    prices.monthly = 2900;
    const back = await syncPricing(t.db, t.paypal);
    expect(back.reactivated).toEqual([oldPlan]);
    expect(back.created).toEqual([]);
    await t.close();
  });

  it("check fails when the config price doesn't match PayPal", async () => {
    const t = await setup();
    prices.yearly = 29900; // edited the file but forgot to run sync
    expect(await checkPricing(t.db, t.paypal)).toEqual([expect.objectContaining({ interval: "yearly", problem: expect.stringContaining("sync-pricing") })]);
    prices.yearly = original.yearly;
    const plan = await t.billing.planFor("monthly", "ILS");
    t.paypal.plans.get(plan.paypalPlanId)!.price = "19.00"; // someone changed it in the PayPal dashboard
    expect(await checkPricing(t.db, t.paypal)).toEqual([expect.objectContaining({ interval: "monthly", problem: expect.stringContaining("19.00") })]);
    await t.close();
  });
});
