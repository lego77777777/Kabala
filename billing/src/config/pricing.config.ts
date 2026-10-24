/**
 * THE single source of truth for Kabala's prices and plan limits.
 *
 * Everything else reads from here: `npm run sync-pricing` (PayPal Product + Billing Plans),
 * GET /api/pricing (desktop app, landing page, checkout page), server-side amount checks and tests.
 *
 * Changing a price: edit the number, run `npm run sync-pricing`. A NEW PayPal plan is created
 * (PayPal plan prices are never edited in place); new subscribers get the new price and existing
 * subscribers stay on their old plan (grandfathering) unless `grandfather` is set to false.
 *
 * Amounts are in minor units (agorot / cents) to avoid floating point: 2900 = ₪29.00.
 */

export type Currency = "ILS" | "USD";
export type Interval = "monthly" | "yearly";
export type Tier = "free" | "pro";

export interface Limits {
  /** receipts that may be saved per calendar month; null = unlimited */
  monthlyReceipts: number | null;
  /** connected mailboxes; null = unlimited */
  mailAccounts: number | null;
  export: boolean;
  backgroundScan: boolean;
}

export const pricingConfig = {
  product: {
    /** stable key; the PayPal product id is stored in the DB */
    key: "kabala-pro",
    name: "Kabala Pro",
    description: "Kabala Pro – unlimited receipts, multiple mailboxes, background scans, Excel/CSV export",
    type: "DIGITAL" as const,
    category: "SOFTWARE" as const,
  },
  defaultCurrency: "ILS" as Currency,
  /** currencies offered for sale; add "USD" here (with prices below) to sell in dollars too */
  currencies: ["ILS"] as Currency[],
  /** keep existing subscribers on their old price when a price changes */
  grandfather: true,
  /** days of access after a failed payment before falling back to Free */
  gracePeriodDays: 7,
  tiers: {
    free: {
      name: "Free",
      limits: { monthlyReceipts: 50, mailAccounts: 1, export: false, backgroundScan: false } satisfies Limits,
    },
    pro: {
      name: "Pro",
      limits: { monthlyReceipts: null, mailAccounts: null, export: true, backgroundScan: true } satisfies Limits,
      trialDays: 0,
      prices: {
        ILS: { monthly: 2900, yearly: 27900 },
        USD: { monthly: 800, yearly: 7700 },
      } satisfies Record<Currency, Record<Interval, number>>,
    },
  },
} as const;

export type PricingConfig = typeof pricingConfig;
