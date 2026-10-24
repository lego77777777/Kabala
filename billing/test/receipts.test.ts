import { randomUUID } from "node:crypto";
import { and, eq } from "drizzle-orm";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { payments, plans, receipts, subscriptions, users } from "../src/db/schema.js";
import { periodEnd, splitVat, taxPolicy } from "../src/receipts/amounts.js";
import { formatNumber, takeNumber } from "../src/receipts/numbering.js";
import { ReceiptError, ReceiptService } from "../src/receipts/service.js";
import { setup } from "./helpers.js";

type Ctx = Awaited<ReturnType<typeof setup>>;
let t: Ctx;
let svc: ReceiptService;

beforeEach(async () => {
  t = await setup();
  svc = new ReceiptService({ db: t.db, tax: taxPolicy("exempt", 18), provider: "internal", now: t.now });
});
afterEach(() => t.close());

/** A recorded payment, as onSaleCompleted leaves it after a verified webhook. */
async function payment(opts: { email?: string; interval?: "monthly" | "yearly"; amountMinor?: number; saleId?: string } = {}) {
  const interval = opts.interval ?? "monthly";
  const [plan] = await t.db.select().from(plans).where(and(eq(plans.interval, interval), eq(plans.currency, "ILS")));
  const [user] = await t.db.insert(users).values({ email: opts.email ?? `u${randomUUID().slice(0, 8)}@gmail.com`, name: "דנה כהן" }).returning();
  const [sub] = await t.db.insert(subscriptions).values({ userId: user!.id, planId: plan!.id, paypalSubscriptionId: `I-${randomUUID().slice(0, 12).toUpperCase()}`, status: "active" }).returning();
  const [pay] = await t.db
    .insert(payments)
    .values({ userId: user!.id, subscriptionId: sub!.id, paypalSaleId: opts.saleId ?? `SALE${randomUUID().slice(0, 10)}`, amountMinor: opts.amountMinor ?? plan!.amountMinor, currency: "ILS", paidAt: new Date("2026-10-21T10:00:00Z") })
    .returning();
  return { plan: plan!, user: user!, payment: pay! };
}

describe("amounts", () => {
  it("exempt dealer: no VAT, net = total", () => {
    expect(splitVat(2900, taxPolicy("exempt", 18))).toEqual({ amountMinor: 2900, netMinor: 2900, vatMinor: 0, vatRateBp: 0 });
  });

  it("licensed dealer: 18% VAT included, net + VAT is exactly the total", () => {
    expect(splitVat(2900, taxPolicy("licensed", 18))).toEqual({ amountMinor: 2900, netMinor: 2458, vatMinor: 442, vatRateBp: 1800 });
    expect(splitVat(27900, taxPolicy("licensed", 18))).toEqual({ amountMinor: 27900, netMinor: 23644, vatMinor: 4256, vatRateBp: 1800 });
    for (let total = 0; total < 50_000; total += 7) {
      const a = splitVat(total, taxPolicy("licensed", 18));
      expect(a.netMinor + a.vatMinor).toBe(total);
    }
  });

  it("billing period: next month / next year, clamped to the month's last day", () => {
    expect(periodEnd(new Date("2026-10-21T10:00:00Z"), "monthly").toISOString()).toBe("2026-11-21T10:00:00.000Z");
    expect(periodEnd(new Date("2026-01-31T10:00:00Z"), "monthly").toISOString()).toBe("2026-02-28T10:00:00.000Z");
    expect(periodEnd(new Date("2028-02-29T10:00:00Z"), "yearly").toISOString()).toBe("2029-02-28T10:00:00.000Z");
  });
});

describe("numbering", () => {
  it("is sequential per kind, starting at 1", async () => {
    const got = [];
    for (let i = 0; i < 5; i++) got.push((await svc.forPayment((await payment()).payment.id)).receipt.number);
    expect(got).toEqual([1, 2, 3, 4, 5]);
    expect(formatNumber(42)).toBe("000042");
  });

  it("concurrent issuing gives unique numbers with no gaps", async () => {
    const pays = await Promise.all(Array.from({ length: 20 }, () => payment()));
    const numbers = (await Promise.all(pays.map((p) => svc.forPayment(p.payment.id)))).map((r) => r.receipt.number);
    expect([...numbers].sort((a, b) => a - b)).toEqual(Array.from({ length: 20 }, (_, i) => i + 1));
  });

  it("a rolled-back transaction gives its number back (no gap)", async () => {
    await svc.forPayment((await payment()).payment.id); // #1
    await expect(
      t.db.transaction(async (tx) => {
        await takeNumber(tx, "receipt"); // would be #2
        throw new Error("boom");
      }),
    ).rejects.toThrow("boom");
    expect((await svc.forPayment((await payment()).payment.id)).receipt.number).toBe(2);
  });

  it("receipts and refunds have separate sequences", async () => {
    const { payment: p } = await payment();
    await svc.forPayment(p.id);
    const refund = await svc.forRefund(p.id, "REFUND1", 1000);
    expect(refund.receipt).toMatchObject({ kind: "refund", number: 1 });
  });
});

describe("idempotency", () => {
  it("one receipt per PayPal transaction, however many times it is requested", async () => {
    const { payment: p } = await payment();
    const results = await Promise.all([svc.forPayment(p.id), svc.forPayment(p.id), svc.forPayment(p.id)]);
    expect(results.filter((r) => r.created)).toHaveLength(1);
    expect(new Set(results.map((r) => r.receipt.id)).size).toBe(1);
    const again = await svc.forPayment(p.id);
    expect(again.created).toBe(false);
    expect(await t.db.select().from(receipts)).toHaveLength(1);
    // the next payment continues the sequence: repeats consumed no numbers
    expect((await svc.forPayment((await payment()).payment.id)).receipt.number).toBe(2);
  });

  it("one refund document per PayPal refund id", async () => {
    const { payment: p } = await payment();
    const [a, b] = await Promise.all([svc.forRefund(p.id, "REF-A", 2900), svc.forRefund(p.id, "REF-A", 2900)]);
    expect(a.receipt.id).toBe(b.receipt.id);
    expect(await t.db.select().from(receipts).where(eq(receipts.kind, "refund"))).toHaveLength(1);
  });
});

describe("receipt content", () => {
  it("takes amount, period and customer from the recorded payment and plan", async () => {
    const { payment: p, user, plan } = await payment({ interval: "yearly" });
    const { receipt } = await svc.forPayment(p.id);
    expect(receipt).toMatchObject({
      kind: "receipt",
      amountMinor: plan.amountMinor,
      currency: "ILS",
      vatMinor: 0,
      planInterval: "yearly",
      customerEmail: user.email,
      customerName: "דנה כהן",
      locale: "he",
      status: "pending",
      provider: "internal",
      paypalTransactionId: p.paypalSaleId,
    });
    expect(receipt.periodEnd.toISOString()).toBe("2027-10-21T10:00:00.000Z");
  });

  it("refuses an amount that is not the plan's price", async () => {
    const { payment: p } = await payment({ amountMinor: 100 });
    await expect(svc.forPayment(p.id)).rejects.toMatchObject({ code: "amount_mismatch" });
    expect(await t.db.select().from(receipts)).toHaveLength(0);
  });

  it("a licensed dealer's receipt shows the VAT part", async () => {
    const licensed = new ReceiptService({ db: t.db, tax: taxPolicy("licensed", 18), provider: "internal" });
    const { receipt } = await licensed.forPayment((await payment()).payment.id);
    expect(receipt).toMatchObject({ amountMinor: 2900, netMinor: 2458, vatMinor: 442, vatRateBp: 1800 });
  });

  it("a refund credits the original receipt, with its VAT rate, and never more than was paid", async () => {
    const licensed = new ReceiptService({ db: t.db, tax: taxPolicy("licensed", 18), provider: "internal" });
    const { payment: p } = await payment();
    const original = (await licensed.forPayment(p.id)).receipt;
    const partial = (await licensed.forRefund(p.id, "REF-1", 1000)).receipt;
    expect(partial).toMatchObject({ creditsReceiptId: original.id, amountMinor: 1000, vatRateBp: 1800, netMinor: 847, vatMinor: 153 });
    await expect(licensed.forRefund(p.id, "REF-2", 2000)).rejects.toBeInstanceOf(ReceiptError);
    expect((await licensed.forRefund(p.id, "REF-3", 1900)).receipt.number).toBe(2);
  });
});
