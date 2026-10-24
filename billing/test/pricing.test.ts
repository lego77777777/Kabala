import { describe, expect, it } from "vitest";
import { pricingConfig } from "../src/config/pricing.config.js";
import { buildPricing, fromMajor, priceKey, quote, toMajor } from "../src/pricing/pricing.js";

describe("money", () => {
  it("converts between minor units and PayPal strings without floating point errors", () => {
    expect(toMajor(2900)).toBe("29.00");
    expect(toMajor(27900)).toBe("279.00");
    expect(toMajor(5)).toBe("0.05");
    expect(fromMajor("29.00")).toBe(2900);
    expect(fromMajor("29.9")).toBe(2990);
    expect(fromMajor("0.07")).toBe(7);
    expect(() => fromMajor("-1")).toThrow();
    expect(() => toMajor(1.5)).toThrow();
  });
});

describe("quotes come only from pricing.config.ts", () => {
  const monthly = pricingConfig.tiers.pro.prices.ILS.monthly;
  const yearly = pricingConfig.tiers.pro.prices.ILS.yearly;

  it("monthly", () => {
    const q = quote("ILS", "monthly");
    expect(q.amountMinor).toBe(monthly);
    expect(q.amount).toBe(toMajor(monthly));
    expect(q.savingsMinor).toBe(0);
  });

  it("yearly shows the saving vs. 12 monthly payments (~20%)", () => {
    const q = quote("ILS", "yearly");
    expect(q.amountMinor).toBe(yearly);
    expect(q.savingsMinor).toBe(monthly * 12 - yearly);
    expect(q.monthlyEquivalentMinor).toBe(Math.round(yearly / 12));
    expect(q.savingsPercent).toBeGreaterThanOrEqual(15);
    expect(q.savingsPercent).toBeLessThanOrEqual(25);
  });

  it("the configured prices are the agreed ones (₪29 / ₪279)", () => {
    expect(monthly).toBe(2900);
    expect(yearly).toBe(27900);
    expect(quote("ILS", "yearly").savingsPercent).toBe(20);
  });

  it("buildPricing attaches synced plan ids by exact price", () => {
    const ids = new Map([[priceKey("ILS", "monthly", monthly), "P-M"]]);
    const r = buildPricing("ILS", ids);
    expect(r.tiers.pro.prices.monthly.paypalPlanId).toBe("P-M");
    expect(r.tiers.pro.prices.yearly.paypalPlanId).toBeNull();
    expect(r.tiers.free.limits.monthlyReceipts).toBe(50);
    expect(r.tiers.pro.limits.monthlyReceipts).toBeNull();
    expect(() => buildPricing("USD")).toThrow(); // not offered until added to `currencies`
  });
});
