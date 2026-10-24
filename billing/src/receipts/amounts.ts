/**
 * Receipt amounts, in integer minor units (agorot / cents) so nothing is ever rounded twice.
 * Prices in pricing.config.ts are what the customer pays, VAT included.
 */

export type BusinessType = "exempt" | "licensed";

export interface TaxPolicy {
  /** exempt = עוסק פטור (no VAT on the document); licensed = עוסק מורשה / company */
  businessType: BusinessType;
  /** VAT rate in basis points (1800 = 18%); ignored for an exempt dealer */
  vatRateBp: number;
}

export interface Amounts {
  amountMinor: number;
  netMinor: number;
  vatMinor: number;
  vatRateBp: number;
}

export function taxPolicy(businessType: BusinessType, vatRatePercent: number): TaxPolicy {
  return { businessType, vatRateBp: Math.round(vatRatePercent * 100) };
}

/** Splits a VAT-inclusive total into net + VAT. net + vat always equals the total exactly. */
export function splitVat(totalMinor: number, policy: TaxPolicy): Amounts {
  if (!Number.isInteger(totalMinor) || totalMinor < 0) throw new Error(`invalid amount ${totalMinor}`);
  if (policy.businessType === "exempt" || policy.vatRateBp === 0) {
    return { amountMinor: totalMinor, netMinor: totalMinor, vatMinor: 0, vatRateBp: 0 };
  }
  const netMinor = Math.round((totalMinor * 10_000) / (10_000 + policy.vatRateBp));
  return { amountMinor: totalMinor, netMinor, vatMinor: totalMinor - netMinor, vatRateBp: policy.vatRateBp };
}

/** End of the billing period a payment covers (same day next month / next year, clamped to month end). */
export function periodEnd(start: Date, interval: "monthly" | "yearly"): Date {
  const end = new Date(start);
  const day = end.getUTCDate();
  end.setUTCDate(1);
  if (interval === "monthly") end.setUTCMonth(end.getUTCMonth() + 1);
  else end.setUTCFullYear(end.getUTCFullYear() + 1);
  const lastDay = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth() + 1, 0)).getUTCDate();
  end.setUTCDate(Math.min(day, lastDay));
  return end;
}
