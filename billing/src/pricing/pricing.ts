/**
 * Pure price calculations, derived only from pricing.config.ts.
 */
import { pricingConfig, type Currency, type Interval, type Limits } from "../config/pricing.config.js";

export interface PriceQuote {
  interval: Interval;
  currency: Currency;
  /** minor units */
  amountMinor: number;
  /** "29.00", the exact format PayPal expects */
  amount: string;
  /** per-month cost in minor units (yearly / 12, rounded) */
  monthlyEquivalentMinor: number;
  /** saving vs. paying monthly for a year (0 for monthly) */
  savingsMinor: number;
  savingsPercent: number;
  /** PayPal billing plan id, when synced */
  paypalPlanId: string | null;
}

export function toMajor(minor: number): string {
  if (!Number.isInteger(minor) || minor < 0) throw new Error(`invalid amount ${minor}`);
  return (minor / 100).toFixed(2);
}

export function fromMajor(value: string): number {
  if (!/^\d+(\.\d{1,2})?$/.test(value)) throw new Error(`invalid amount ${value}`);
  const [whole, frac = ""] = value.split(".");
  return Number(whole) * 100 + Number(frac.padEnd(2, "0"));
}

export function priceMinor(currency: Currency, interval: Interval): number {
  const prices = pricingConfig.tiers.pro.prices[currency];
  if (!prices) throw new Error(`no Pro price for ${currency}`);
  return prices[interval];
}

export function quote(currency: Currency, interval: Interval, paypalPlanId: string | null = null): PriceQuote {
  const amountMinor = priceMinor(currency, interval);
  const monthly = priceMinor(currency, "monthly");
  const fullYear = monthly * 12;
  const savingsMinor = interval === "yearly" ? Math.max(0, fullYear - amountMinor) : 0;
  return {
    interval,
    currency,
    amountMinor,
    amount: toMajor(amountMinor),
    monthlyEquivalentMinor: interval === "yearly" ? Math.round(amountMinor / 12) : amountMinor,
    savingsMinor,
    savingsPercent: interval === "yearly" ? Math.round((savingsMinor / fullYear) * 100) : 0,
    paypalPlanId,
  };
}

/** Stable identity of a price: when this changes, a new PayPal plan is needed. */
export function priceKey(currency: Currency, interval: Interval, amountMinor: number): string {
  return `pro:${interval}:${currency}:${amountMinor}`;
}

export interface PricingResponse {
  currency: Currency;
  currencies: Currency[];
  gracePeriodDays: number;
  tiers: {
    free: { name: string; limits: Limits; prices: null };
    pro: { name: string; limits: Limits; trialDays: number; prices: Record<Interval, PriceQuote> };
  };
}

/**
 * The payload of GET /api/pricing. `planIds` maps priceKey → active PayPal plan id (from the DB).
 */
export function buildPricing(currency: Currency, planIds: Map<string, string> = new Map()): PricingResponse {
  if (!pricingConfig.currencies.includes(currency)) throw new Error(`currency ${currency} is not offered`);
  const pro = pricingConfig.tiers.pro;
  const q = (interval: Interval) => {
    const amount = priceMinor(currency, interval);
    return quote(currency, interval, planIds.get(priceKey(currency, interval, amount)) ?? null);
  };
  return {
    currency,
    currencies: [...pricingConfig.currencies],
    gracePeriodDays: pricingConfig.gracePeriodDays,
    tiers: {
      free: { name: pricingConfig.tiers.free.name, limits: { ...pricingConfig.tiers.free.limits }, prices: null },
      pro: { name: pro.name, limits: { ...pro.limits }, trialDays: pro.trialDays, prices: { monthly: q("monthly"), yearly: q("yearly") } },
    },
  };
}
