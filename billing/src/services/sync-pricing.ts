/**
 * Makes PayPal match pricing.config.ts.
 *
 * - One PayPal Product for Kabala Pro.
 * - One ACTIVE Billing Plan per (currency × interval) at the configured price.
 * - A price change never edits an existing plan: a NEW plan is created and the old one is
 *   deactivated. Deactivated plans keep billing their existing subscribers (grandfathering);
 *   only new checkouts use the new plan.
 */
import { and, eq } from "drizzle-orm";
import { pricingConfig, type Currency, type Interval } from "../config/pricing.config.js";
import type { Db } from "../db/client.js";
import { plans, subscriptions } from "../db/schema.js";
import type { PayPalGateway } from "../paypal/gateway.js";
import { fromMajor, toMajor } from "../pricing/pricing.js";

const INTERVALS: Interval[] = ["monthly", "yearly"];

export interface SyncReport {
  mode: "sandbox" | "live";
  productId: string;
  created: { interval: Interval; currency: Currency; price: string; planId: string }[];
  reactivated: string[];
  deactivated: { planId: string; price: string; subscribersKept: number }[];
  unchanged: string[];
  /** subscriptions left on old prices when grandfather=false (need a revise with buyer consent) */
  toMigrate: string[];
}

async function productId(db: Db, paypal: PayPalGateway): Promise<string> {
  const [existing] = await db.select({ id: plans.paypalProductId }).from(plans).where(eq(plans.mode, paypal.mode)).limit(1);
  if (existing && (await paypal.getProduct(existing.id))) return existing.id;
  const p = pricingConfig.product;
  const created = await paypal.createProduct({
    name: p.name,
    description: p.description,
    type: p.type,
    category: p.category,
    // same request id → PayPal returns the same product if a previous run died half-way
    requestId: `${p.key}-${paypal.mode}-product-v1`,
  });
  return created.id;
}

export async function syncPricing(db: Db, paypal: PayPalGateway, now = new Date()): Promise<SyncReport> {
  const product = await productId(db, paypal);
  const report: SyncReport = { mode: paypal.mode, productId: product, created: [], reactivated: [], deactivated: [], unchanged: [], toMigrate: [] };
  const pro = pricingConfig.tiers.pro;

  for (const currency of ["ILS", "USD"] as Currency[]) {
    const offered = pricingConfig.currencies.includes(currency);
    for (const interval of INTERVALS) {
      const amount = offered ? pro.prices[currency][interval] : null;
      const rows = await db
        .select()
        .from(plans)
        .where(and(eq(plans.mode, paypal.mode), eq(plans.tier, "pro"), eq(plans.interval, interval), eq(plans.currency, currency)));

      let target = amount === null ? undefined : rows.find((r) => r.amountMinor === amount);
      if (amount !== null && !target) {
        const price = toMajor(amount);
        const plan = await paypal.createPlan({
          productId: product,
          name: `Kabala Pro – ${interval === "monthly" ? "Monthly" : "Yearly"} (${price} ${currency})`,
          description: `Kabala Pro, ${interval}, ${price} ${currency}`,
          interval,
          currency,
          price,
          trialDays: pro.trialDays,
          requestId: `kabala-plan-${paypal.mode}-${interval}-${currency}-${amount}`,
        });
        if (plan.status !== "ACTIVE") await paypal.activatePlan(plan.id);
        const [row] = await db
          .insert(plans)
          .values({ mode: paypal.mode, tier: "pro", interval, currency, amountMinor: amount, paypalProductId: product, paypalPlanId: plan.id, status: "active", createdAt: now })
          .returning();
        target = row!;
        report.created.push({ interval, currency, price, planId: plan.id });
      } else if (target && target.status === "inactive") {
        await paypal.activatePlan(target.paypalPlanId);
        await db.update(plans).set({ status: "active" }).where(eq(plans.id, target.id));
        report.reactivated.push(target.paypalPlanId);
      } else if (target) {
        report.unchanged.push(target.paypalPlanId);
      }

      // older prices (or currencies no longer offered): stop new sign-ups, keep billing existing subscribers
      for (const old of rows) {
        if (old.id === target?.id || old.status === "inactive") continue;
        await paypal.deactivatePlan(old.paypalPlanId);
        await db.update(plans).set({ status: "inactive" }).where(eq(plans.id, old.id));
        const subs = await db.select({ id: subscriptions.paypalSubscriptionId, status: subscriptions.status }).from(subscriptions).where(eq(subscriptions.planId, old.id));
        const live = subs.filter((s) => ["trial", "active", "past_due"].includes(s.status));
        report.deactivated.push({ planId: old.paypalPlanId, price: `${toMajor(old.amountMinor)} ${old.currency}`, subscribersKept: live.length });
        if (!pricingConfig.grandfather) report.toMigrate.push(...live.map((s) => s.id));
      }
    }
  }
  return report;
}

export interface PricingMismatch {
  interval: Interval;
  currency: Currency;
  expected: string;
  problem: string;
}

/** Fails (returns mismatches) when the configured price isn't what PayPal will actually charge. */
export async function checkPricing(db: Db, paypal: PayPalGateway): Promise<PricingMismatch[]> {
  const problems: PricingMismatch[] = [];
  for (const currency of pricingConfig.currencies) {
    for (const interval of INTERVALS) {
      const amount = pricingConfig.tiers.pro.prices[currency][interval];
      const expected = `${toMajor(amount)} ${currency}`;
      const [row] = await db
        .select()
        .from(plans)
        .where(and(eq(plans.mode, paypal.mode), eq(plans.status, "active"), eq(plans.interval, interval), eq(plans.currency, currency), eq(plans.amountMinor, amount)));
      if (!row) {
        problems.push({ interval, currency, expected, problem: "no active plan for this price — run `npm run sync-pricing`" });
        continue;
      }
      const remote = await paypal.getPlan(row.paypalPlanId);
      if (remote.status !== "ACTIVE") problems.push({ interval, currency, expected, problem: `PayPal plan ${remote.id} is ${remote.status}` });
      if (remote.currency !== currency || fromMajor(remote.price) !== amount || remote.interval !== interval) {
        problems.push({ interval, currency, expected, problem: `PayPal plan ${remote.id} charges ${remote.price} ${remote.currency} ${remote.interval}` });
      }
    }
  }
  return problems;
}
