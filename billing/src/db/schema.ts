import { sql } from "drizzle-orm";
import {
  boolean,
  index,
  integer,
  jsonb,
  pgEnum,
  pgTable,
  serial,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

export const paypalMode = pgEnum("paypal_mode", ["sandbox", "live"]);
export const billingInterval = pgEnum("billing_interval", ["monthly", "yearly"]);
export const planStatus = pgEnum("plan_status", ["active", "inactive"]);
export const subscriptionStatus = pgEnum("subscription_status", [
  "pending", // created, waiting for the user to approve at PayPal
  "trial",
  "active",
  "past_due", // payment failed, inside the grace period
  "cancelled", // paid until current_period_end, will not renew
  "expired",
]);
export const paymentStatus = pgEnum("payment_status", ["completed", "refunded", "partially_refunded"]);

export const users = pgTable("users", {
  id: uuid("id").primaryKey().defaultRandom(),
  email: text("email").notNull().unique(),
  /** shown on receipts; learned from PayPal's subscriber name when known */
  name: text("name"),
  /** "he" | "en": language of receipts and emails */
  locale: text("locale").notNull().default("he"),
  /** optional second address that also gets every receipt (e.g. the user's accountant) */
  receiptCcEmail: text("receipt_cc_email"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

/** Mirrors pricing.config.ts; one row per PayPal billing plan ever created. */
export const plans = pgTable(
  "plans",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    mode: paypalMode("mode").notNull(),
    tier: text("tier").notNull(),
    interval: billingInterval("interval").notNull(),
    currency: text("currency").notNull(),
    amountMinor: integer("amount_minor").notNull(),
    paypalProductId: text("paypal_product_id").notNull(),
    paypalPlanId: text("paypal_plan_id").notNull().unique(),
    status: planStatus("status").notNull().default("active"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("plans_price_uq").on(t.mode, t.tier, t.interval, t.currency, t.amountMinor),
    index("plans_active_idx").on(t.mode, t.status),
  ],
);

export const subscriptions = pgTable(
  "subscriptions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id").notNull().references(() => users.id),
    planId: uuid("plan_id").notNull().references(() => plans.id),
    paypalSubscriptionId: text("paypal_subscription_id").notNull().unique(),
    status: subscriptionStatus("status").notNull().default("pending"),
    currentPeriodEnd: timestamp("current_period_end", { withTimezone: true }),
    graceUntil: timestamp("grace_until", { withTimezone: true }),
    cancelledAt: timestamp("cancelled_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("subscriptions_user_idx").on(t.userId)],
);

export const payments = pgTable(
  "payments",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id").notNull().references(() => users.id),
    subscriptionId: uuid("subscription_id").notNull().references(() => subscriptions.id),
    paypalSaleId: text("paypal_sale_id").notNull().unique(),
    amountMinor: integer("amount_minor").notNull(),
    currency: text("currency").notNull(),
    refundedMinor: integer("refunded_minor").notNull().default(0),
    status: paymentStatus("status").notNull().default("completed"),
    paidAt: timestamp("paid_at", { withTimezone: true }).notNull(),
  },
  (t) => [index("payments_user_idx").on(t.userId)],
);

/** Audit log of every webhook delivery, written BEFORE processing. Primary key = PayPal event id → idempotency. */
export const webhookEvents = pgTable("webhook_events", {
  id: text("id").primaryKey(),
  eventType: text("event_type").notNull(),
  resourceId: text("resource_id"),
  verified: boolean("verified").notNull(),
  payload: jsonb("payload").notNull(),
  receivedAt: timestamp("received_at", { withTimezone: true }).notNull().defaultNow(),
  processedAt: timestamp("processed_at", { withTimezone: true }),
  error: text("error"),
});

// ---- receipts ------------------------------------------------------------------------------

/** receipt = a payment was received; refund = money returned (credit document for a receipt) */
export const receiptKind = pgEnum("receipt_kind", ["receipt", "refund"]);
export const receiptStatus = pgEnum("receipt_status", ["pending", "generated", "sent", "failed"]);
export const emailStatus = pgEnum("email_status", ["queued", "sent", "failed"]);

/**
 * One row per document kind holding the next number. Taking a number is an UPDATE … RETURNING inside
 * the same transaction that inserts the receipt: the row lock serializes concurrent issuers, and a
 * rollback returns the number, so the sequence has no gaps and no duplicates.
 */
export const receiptSequences = pgTable("receipt_sequences", {
  kind: receiptKind("kind").primaryKey(),
  nextNumber: integer("next_number").notNull().default(1),
});

export const receipts = pgTable(
  "receipts",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    kind: receiptKind("kind").notNull(),
    /** running number within its kind (from receipt_sequences), never reused */
    number: integer("number").notNull(),
    userId: uuid("user_id").notNull().references(() => users.id),
    paymentId: uuid("payment_id").notNull().references(() => payments.id),
    /** PayPal sale id for a receipt, PayPal refund id for a refund: one document per transaction */
    paypalTransactionId: text("paypal_transaction_id").notNull().unique(),
    /** for a refund: the receipt it credits */
    creditsReceiptId: uuid("credits_receipt_id"),
    amountMinor: integer("amount_minor").notNull(),
    netMinor: integer("net_minor").notNull(),
    vatMinor: integer("vat_minor").notNull(),
    /** VAT rate in basis points (1800 = 18%); 0 for an exempt dealer */
    vatRateBp: integer("vat_rate_bp").notNull(),
    currency: text("currency").notNull(),
    planInterval: billingInterval("plan_interval").notNull(),
    periodStart: timestamp("period_start", { withTimezone: true }).notNull(),
    periodEnd: timestamp("period_end", { withTimezone: true }).notNull(),
    customerName: text("customer_name"),
    customerEmail: text("customer_email").notNull(),
    locale: text("locale").notNull(),
    status: receiptStatus("status").notNull().default("pending"),
    provider: text("provider").notNull(),
    /** the legal document at an Israeli provider (Morning), when one is used */
    providerDocumentId: text("provider_document_id"),
    providerDocumentNumber: text("provider_document_number"),
    /** storage key of the PDF (local disk / S3 / R2); served only through signed links */
    pdfKey: text("pdf_key"),
    attempts: integer("attempts").notNull().default(0),
    lastError: text("last_error"),
    issuedAt: timestamp("issued_at", { withTimezone: true }).notNull().defaultNow(),
    generatedAt: timestamp("generated_at", { withTimezone: true }),
    sentAt: timestamp("sent_at", { withTimezone: true }),
  },
  (t) => [uniqueIndex("receipts_kind_number_uq").on(t.kind, t.number), index("receipts_user_idx").on(t.userId), index("receipts_status_idx").on(t.status)],
);

export const emailLogs = pgTable(
  "email_logs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    receiptId: uuid("receipt_id").notNull().references(() => receipts.id),
    /** "auto" = sent after the payment, "resend" = the user asked for it again */
    trigger: text("trigger").notNull(),
    toAddresses: text("to_addresses").notNull(),
    status: emailStatus("status").notNull().default("queued"),
    attempts: integer("attempts").notNull().default(0),
    providerMessageId: text("provider_message_id"),
    error: text("error"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [index("email_logs_receipt_idx").on(t.receiptId)],
);

export const licenseKeyStatus = pgEnum("license_key_status", ["active", "revoked"]);

/**
 * License keys (src/licenses/keys.ts): one active key per user, issued with the first payment.
 * The key itself is never stored in plain text: key_hash (HMAC-SHA256) finds it on activation, key_encrypted
 * (AES-256-GCM) lets its owner see it again. The admin panel only ever gets `hint` (the last group).
 */
export const licenseKeys = pgTable(
  "license_keys",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id").notNull().references(() => users.id),
    subscriptionId: uuid("subscription_id").references(() => subscriptions.id),
    keyHash: text("key_hash").notNull().unique(),
    keyEncrypted: text("key_encrypted").notNull(),
    hint: text("hint").notNull(),
    status: licenseKeyStatus("status").notNull().default("active"),
    activations: integer("activations").notNull().default(0),
    lastUsedAt: timestamp("last_used_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    revokedAt: timestamp("revoked_at", { withTimezone: true }),
    revokedBy: text("revoked_by"),
  },
  (t) => [index("license_keys_user_idx").on(t.userId), uniqueIndex("license_keys_one_active_uq").on(t.userId).where(sql`status = 'active'`)],
);

/** @deprecated replaced by `receipts`. Kept so older databases still migrate; nothing new is written to it. */
export const invoices = pgTable("invoices", {
  id: uuid("id").primaryKey().defaultRandom(),
  /** running invoice number, never reused */
  number: serial("number").notNull().unique(),
  userId: uuid("user_id").notNull().references(() => users.id),
  paymentId: uuid("payment_id").notNull().unique().references(() => payments.id),
  amountMinor: integer("amount_minor").notNull(),
  currency: text("currency").notNull(),
  provider: text("provider").notNull().default("local-pdf"),
  externalId: text("external_id"),
  pdfPath: text("pdf_path"),
  issuedAt: timestamp("issued_at", { withTimezone: true }).notNull().default(sql`now()`),
});
