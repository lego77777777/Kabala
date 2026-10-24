/**
 * Creates receipt records: exactly one per PayPal transaction, numbered without gaps.
 *
 * A receipt is created only for a payment we already recorded from a verified webhook
 * (PAYMENT.SALE.COMPLETED), and its amount is the amount PayPal actually charged, checked against
 * the plan synced from pricing.config.ts. A refund (PAYMENT.SALE.REFUNDED) gets its own document that
 * credits the original receipt. Rendering the PDF and sending the email happen later, in the queue.
 */
import { and, eq } from "drizzle-orm";
import type { Db } from "../db/client.js";
import { payments, plans, receipts, subscriptions, users } from "../db/schema.js";
import { periodEnd, splitVat, type TaxPolicy } from "./amounts.js";
import { takeNumber } from "./numbering.js";

export type ReceiptRow = typeof receipts.$inferSelect;

export class ReceiptError extends Error {
  constructor(readonly code: "payment_not_found" | "amount_mismatch" | "refund_exceeds_payment", message?: string) {
    super(message ?? code);
  }
}

export interface ReceiptServiceDeps {
  db: Db;
  tax: TaxPolicy;
  /** name of the provider that will issue the document (env RECEIPT_PROVIDER) */
  provider: string;
  now?: () => Date;
}

export class ReceiptService {
  private readonly now: () => Date;

  constructor(private readonly d: ReceiptServiceDeps) {
    this.now = d.now ?? (() => new Date());
  }

  /**
   * The receipt for a recorded payment. Idempotent: calling it again (a repeated webhook, a retry,
   * the reconcile sweep) returns the existing row and `created: false`.
   */
  async forPayment(paymentId: string): Promise<{ receipt: ReceiptRow; created: boolean }> {
    return this.d.db.transaction(async (tx) => {
      // Lock the payment: two deliveries of the same webhook serialize here, so the second one
      // sees the first one's receipt instead of taking a second number.
      const [payment] = await tx.select().from(payments).where(eq(payments.id, paymentId)).for("update");
      if (!payment) throw new ReceiptError("payment_not_found");
      const [existing] = await tx.select().from(receipts).where(eq(receipts.paypalTransactionId, payment.paypalSaleId));
      if (existing) return { receipt: existing, created: false };

      const [row] = await tx
        .select()
        .from(subscriptions)
        .innerJoin(plans, eq(plans.id, subscriptions.planId))
        .innerJoin(users, eq(users.id, subscriptions.userId))
        .where(eq(subscriptions.id, payment.subscriptionId));
      if (!row) throw new ReceiptError("payment_not_found", "payment without subscription");
      const { plans: plan, users: user } = row;
      // The charged amount must be exactly the plan's price (the plan mirrors pricing.config.ts,
      // including grandfathered prices). Never a typed-in number.
      if (payment.currency !== plan.currency || payment.amountMinor !== plan.amountMinor) {
        throw new ReceiptError("amount_mismatch", `paid ${payment.amountMinor} ${payment.currency}, plan ${plan.amountMinor} ${plan.currency}`);
      }

      const amounts = splitVat(payment.amountMinor, this.d.tax);
      const number = await takeNumber(tx, "receipt");
      const [receipt] = await tx
        .insert(receipts)
        .values({
          kind: "receipt",
          number,
          userId: user.id,
          paymentId: payment.id,
          paypalTransactionId: payment.paypalSaleId,
          ...amounts,
          currency: payment.currency,
          planInterval: plan.interval,
          periodStart: payment.paidAt,
          periodEnd: periodEnd(payment.paidAt, plan.interval),
          customerName: user.name,
          customerEmail: user.email,
          locale: user.locale,
          provider: this.d.provider,
          issuedAt: this.now(),
        })
        .returning();
      return { receipt: receipt!, created: true };
    });
  }

  /**
   * The credit document for a PayPal refund of a recorded payment. Idempotent per refund id.
   * Uses the VAT rate of the original receipt, so a refund always mirrors what was charged.
   */
  async forRefund(paymentId: string, refundId: string, refundMinor: number): Promise<{ receipt: ReceiptRow; created: boolean }> {
    const original = (await this.forPayment(paymentId)).receipt; // a refund always credits an existing receipt
    return this.d.db.transaction(async (tx) => {
      const [payment] = await tx.select().from(payments).where(eq(payments.id, paymentId)).for("update");
      if (!payment) throw new ReceiptError("payment_not_found");
      const [existing] = await tx.select().from(receipts).where(eq(receipts.paypalTransactionId, refundId));
      if (existing) return { receipt: existing, created: false };

      const earlier = await tx.select().from(receipts).where(and(eq(receipts.kind, "refund"), eq(receipts.paymentId, paymentId)));
      const alreadyRefunded = earlier.reduce((sum, r) => sum + r.amountMinor, 0);
      if (refundMinor <= 0 || alreadyRefunded + refundMinor > original.amountMinor) {
        throw new ReceiptError("refund_exceeds_payment", `refund ${refundMinor} + ${alreadyRefunded} > ${original.amountMinor}`);
      }
      const amounts = splitVat(refundMinor, { businessType: original.vatRateBp ? "licensed" : "exempt", vatRateBp: original.vatRateBp });
      const number = await takeNumber(tx, "refund");
      const [receipt] = await tx
        .insert(receipts)
        .values({
          kind: "refund",
          number,
          userId: original.userId,
          paymentId,
          paypalTransactionId: refundId,
          creditsReceiptId: original.id,
          ...amounts,
          currency: original.currency,
          planInterval: original.planInterval,
          periodStart: original.periodStart,
          periodEnd: original.periodEnd,
          customerName: original.customerName,
          customerEmail: original.customerEmail,
          locale: original.locale,
          provider: this.d.provider,
          issuedAt: this.now(),
        })
        .returning();
      return { receipt: receipt!, created: true };
    });
  }

  async get(id: string): Promise<ReceiptRow | null> {
    const [r] = await this.d.db.select().from(receipts).where(eq(receipts.id, id));
    return r ?? null;
  }

  /** Status moves only forward (pending → generated → sent), except to "failed" and back to retry. */
  async setStatus(id: string, status: ReceiptRow["status"], patch: Partial<Pick<ReceiptRow, "pdfKey" | "providerDocumentId" | "providerDocumentNumber" | "lastError">> = {}) {
    const now = this.now();
    await this.d.db
      .update(receipts)
      .set({ status, ...patch, ...(status === "generated" ? { generatedAt: now } : {}), ...(status === "sent" ? { sentAt: now } : {}) })
      .where(eq(receipts.id, id));
  }
}
