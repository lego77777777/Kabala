/**
 * Billing service: the only place that changes subscriptions, payments and invoices.
 * Pro is granted only from data fetched from PayPal's API (checkout confirmation, reconcile)
 * or from a webhook whose signature PayPal verified. Nothing the browser sends is trusted.
 */
import { randomUUID } from "node:crypto";
import { and, desc, eq, inArray, isNull, ne } from "drizzle-orm";
import { pricingConfig, type Currency, type Interval, type Limits, type Tier } from "../config/pricing.config.js";
import type { Db } from "../db/client.js";
import { payments, plans, receipts, subscriptions, users, webhookEvents } from "../db/schema.js";
import { licenseKeyMail, paymentFailedMail, type Mailer } from "../email/mailer.js";
import type { LicenseKeyService } from "../licenses/keys.js";
import { ReceiptError } from "../receipts/service.js";
import { PayPalError, type PayPalGateway, type PayPalSubscription, type WebhookHeaders } from "../paypal/gateway.js";
import { buildPricing, fromMajor, priceKey, toMajor, type PricingResponse } from "../pricing/pricing.js";
import { expireIfDue, fromPayPal, isEntitled, transition, type SubEvent, type SubState, type SubStatus } from "./subscription-state.js";
import type { TokenService } from "./tokens.js";

export class BillingError extends Error {
  constructor(
    readonly code:
      | "plan_not_synced"
      | "subscription_not_found"
      | "subscription_mismatch"
      | "not_active_yet"
      | "no_subscription"
      | "paypal_unavailable"
      | "invalid_currency",
    message?: string,
  ) {
    super(message ?? code);
  }
}

/** What happens after a verified payment or refund: the receipt pipeline (src/receipts/pipeline.ts). */
export interface ReceiptHooks {
  onPayment(paymentId: string): Promise<unknown>;
  onRefund(paymentId: string, refundId: string, refundMinor: number): Promise<unknown>;
}

export interface BillingDeps {
  db: Db;
  paypal: PayPalGateway;
  tokens: TokenService;
  receipts?: ReceiptHooks;
  mailer: Mailer;
  licenseTtlDays: number;
  /** license keys (src/licenses/keys.ts): issued with the first payment */
  keys?: LicenseKeyService;
  now?: () => Date;
  log?: { info: (o: object, msg?: string) => void; warn: (o: object, msg?: string) => void; error: (o: object, msg?: string) => void };
}

type SubRow = typeof subscriptions.$inferSelect;
type PlanRow = typeof plans.$inferSelect;

const PRO_STATUSES: SubStatus[] = ["trial", "active", "past_due", "cancelled"];
const toState = (s: SubRow): SubState => ({ status: s.status, currentPeriodEnd: s.currentPeriodEnd, graceUntil: s.graceUntil, cancelledAt: s.cancelledAt });

export class BillingService {
  private readonly now: () => Date;
  private readonly log: NonNullable<BillingDeps["log"]>;

  constructor(private readonly d: BillingDeps) {
    this.now = d.now ?? (() => new Date());
    this.log = d.log ?? { info: () => {}, warn: () => {}, error: () => {} };
  }

  get mode() {
    return this.d.paypal.mode;
  }

  // ---- pricing -------------------------------------------------------------------

  async activePlans(): Promise<PlanRow[]> {
    return this.d.db.select().from(plans).where(and(eq(plans.mode, this.mode), eq(plans.status, "active")));
  }

  async pricing(currency: Currency = pricingConfig.defaultCurrency): Promise<PricingResponse> {
    if (!pricingConfig.currencies.includes(currency)) throw new BillingError("invalid_currency");
    const map = new Map((await this.activePlans()).map((p) => [priceKey(p.currency as Currency, p.interval, p.amountMinor), p.paypalPlanId]));
    return buildPricing(currency, map);
  }

  /** The PayPal plan a NEW subscriber must use: always the one matching the current config price. */
  async planFor(interval: Interval, currency: Currency): Promise<PlanRow> {
    const amount = pricingConfig.tiers.pro.prices[currency]?.[interval];
    if (amount === undefined || !pricingConfig.currencies.includes(currency)) throw new BillingError("invalid_currency");
    const [plan] = await this.d.db
      .select()
      .from(plans)
      .where(and(eq(plans.mode, this.mode), eq(plans.status, "active"), eq(plans.tier, "pro"), eq(plans.interval, interval), eq(plans.currency, currency), eq(plans.amountMinor, amount)));
    if (!plan) throw new BillingError("plan_not_synced", "run `npm run sync-pricing`");
    return plan;
  }

  // ---- users & licenses --------------------------------------------------------------

  async userByEmail(email: string) {
    const normalized = email.trim().toLowerCase();
    await this.d.db.insert(users).values({ email: normalized }).onConflictDoNothing();
    const [u] = await this.d.db.select().from(users).where(eq(users.email, normalized));
    return u!;
  }

  async user(id: string) {
    const [u] = await this.d.db.select().from(users).where(eq(users.id, id));
    return u ?? null;
  }

  /** The user's most relevant subscription (an entitling one first, else the newest). */
  async currentSubscription(userId: string): Promise<(SubRow & { plan: PlanRow }) | null> {
    const rows = await this.d.db
      .select()
      .from(subscriptions)
      .innerJoin(plans, eq(plans.id, subscriptions.planId))
      .where(and(eq(subscriptions.userId, userId), ne(subscriptions.status, "pending")))
      .orderBy(desc(subscriptions.createdAt));
    const now = this.now();
    const all = [];
    for (const r of rows) all.push({ ...(await this.housekeep(r.subscriptions, now)), plan: r.plans });
    return all.find((s) => isEntitled(toState(s), now)) ?? all[0] ?? null;
  }

  private async housekeep(sub: SubRow, now: Date): Promise<SubRow> {
    const next = expireIfDue(toState(sub), now);
    if (next.status === sub.status) return sub;
    await this.d.db.update(subscriptions).set({ status: next.status, updatedAt: now }).where(eq(subscriptions.id, sub.id));
    return { ...sub, status: next.status };
  }

  async entitlement(userId: string): Promise<{ tier: Tier; limits: Limits; sub: (SubRow & { plan: PlanRow }) | null }> {
    const sub = await this.currentSubscription(userId);
    const pro = sub !== null && isEntitled(toState(sub), this.now());
    const tier: Tier = pro ? "pro" : "free";
    return { tier, limits: { ...pricingConfig.tiers[tier].limits }, sub };
  }

  async license(userId: string): Promise<string> {
    const user = await this.user(userId);
    if (!user) throw new BillingError("no_subscription", "unknown user");
    const { tier, limits, sub } = await this.entitlement(userId);
    const paidUntil = sub?.status === "past_due" ? sub.graceUntil : sub?.currentPeriodEnd;
    return this.d.tokens.license(
      { sub: user.id, email: user.email, plan: tier, limits, paidUntil: tier === "pro" && paidUntil ? paidUntil.toISOString() : null, status: sub?.status ?? null },
      this.d.licenseTtlDays,
      this.now(),
    );
  }

  // ---- checkout -------------------------------------------------------------------------

  /** Values the PayPal button needs. The plan (and so the amount) is chosen here, never by the browser. */
  async checkoutStart(userId: string, interval: Interval, currency: Currency) {
    const plan = await this.planFor(interval, currency);
    return { planId: plan.paypalPlanId, customId: userId };
  }

  /**
   * Called after the buyer approves in PayPal's window. Fetches the subscription from PayPal and only
   * then grants Pro: it must be ours (one of our plans, in this mode), belong to this user and be ACTIVE.
   */
  async confirmCheckout(userId: string, paypalSubscriptionId: string) {
    let remote: PayPalSubscription;
    try {
      remote = await this.d.paypal.getSubscription(paypalSubscriptionId);
    } catch (e) {
      if (e instanceof PayPalError && e.status === 404) throw new BillingError("subscription_not_found");
      throw new BillingError("paypal_unavailable", (e as Error).message);
    }
    if (remote.customId !== userId) throw new BillingError("subscription_mismatch", "subscription belongs to another user");
    const plan = await this.planByPayPalId(remote.planId);
    if (!plan) throw new BillingError("subscription_mismatch", "unknown plan");
    if (remote.status !== "ACTIVE") throw new BillingError("not_active_yet", remote.status);
    const sub = await this.applyRemote(remote, plan, userId);
    return { status: sub.status, entitled: isEntitled(toState(sub), this.now()) };
  }

  private async planByPayPalId(paypalPlanId: string): Promise<PlanRow | null> {
    const [p] = await this.d.db.select().from(plans).where(and(eq(plans.paypalPlanId, paypalPlanId), eq(plans.mode, this.mode)));
    return p ?? null;
  }

  /** Upserts our row from PayPal's authoritative view of a subscription. */
  private async applyRemote(remote: PayPalSubscription, plan: PlanRow, userId: string): Promise<SubRow> {
    const now = this.now();
    const next = remote.nextBillingTime ? new Date(remote.nextBillingTime) : null;
    const [existing] = await this.d.db.select().from(subscriptions).where(eq(subscriptions.paypalSubscriptionId, remote.id));
    const base: SubState = existing ? toState(existing) : { status: "pending", currentPeriodEnd: null, graceUntil: null, cancelledAt: null };
    let event = fromPayPal(remote.status, remote.inTrial);
    if (event?.type === "activated") event = { ...event, nextBillingTime: next };
    // ACTIVE with failed charges = PayPal is still retrying: that is past_due for us, not paid.
    if (event?.type === "activated" && remote.failedPayments > 0 && base.status !== "pending") event = { type: "payment_failed" };
    let state = event ? transition(base, event, now, pricingConfig.gracePeriodDays) : base;
    if (next && state.status !== "expired") state = { ...state, currentPeriodEnd: next };
    if (remote.subscriberName) {
      // the name receipts are addressed to; only filled when we don't have one yet
      await this.d.db.update(users).set({ name: remote.subscriberName }).where(and(eq(users.id, userId), isNull(users.name)));
    }
    if (existing) {
      await this.d.db.update(subscriptions).set({ ...state, updatedAt: now }).where(eq(subscriptions.id, existing.id));
      return { ...existing, ...state };
    }
    const [row] = await this.d.db
      .insert(subscriptions)
      .values({ userId, planId: plan.id, paypalSubscriptionId: remote.id, ...state })
      .onConflictDoNothing()
      .returning();
    if (row) return row;
    const [again] = await this.d.db.select().from(subscriptions).where(eq(subscriptions.paypalSubscriptionId, remote.id));
    return again!;
  }

  // ---- account ------------------------------------------------------------------------

  async account(userId: string) {
    const { tier, limits, sub } = await this.entitlement(userId);
    const pays = await this.d.db.select().from(payments).where(eq(payments.userId, userId)).orderBy(desc(payments.paidAt));
    const docs = await this.d.db.select().from(receipts).where(eq(receipts.userId, userId)).orderBy(desc(receipts.issuedAt));
    const user = await this.user(userId);
    return {
      tier,
      limits,
      licenseKey: (await this.d.keys?.forOwner(userId)) ?? null,
      subscription: sub && {
        status: sub.status,
        interval: sub.plan.interval,
        amount: toMajor(sub.plan.amountMinor),
        currency: sub.plan.currency,
        nextBillingDate: sub.status === "active" || sub.status === "trial" ? sub.currentPeriodEnd?.toISOString() ?? null : null,
        accessUntil: sub.status === "cancelled" ? sub.currentPeriodEnd?.toISOString() ?? null : sub.status === "past_due" ? sub.graceUntil?.toISOString() ?? null : null,
        graceUntil: sub.graceUntil?.toISOString() ?? null,
        canCancel: sub.status === "active" || sub.status === "trial" || sub.status === "past_due",
      },
      payments: pays.map((p) => {
        const doc = docs.find((r) => r.paymentId === p.id && r.kind === "receipt");
        return {
          id: p.id,
          amount: toMajor(p.amountMinor),
          currency: p.currency,
          status: p.status,
          paidAt: p.paidAt.toISOString(),
          refunded: toMajor(p.refundedMinor),
          receipt: doc ? { id: doc.id, number: doc.number, status: doc.status } : null,
        };
      }),
      receipts: docs.map((r) => ({
        id: r.id,
        kind: r.kind,
        number: r.number,
        documentNumber: r.providerDocumentNumber,
        amount: toMajor(r.amountMinor),
        currency: r.currency,
        issuedAt: r.issuedAt.toISOString(),
        status: r.status,
        ready: Boolean(r.pdfKey),
      })),
      receiptSettings: { email: user?.email ?? null, ccEmail: user?.receiptCcEmail ?? null, locale: user?.locale ?? "he" },
    };
  }

  /** Any receipt by id: only for callers that already checked a signed link. */
  async receiptById(receiptId: string) {
    const [r] = await this.d.db.select().from(receipts).where(eq(receipts.id, receiptId));
    return r ?? null;
  }

  /** The user's own receipt (for download); null for someone else's. */
  async receiptFor(userId: string, receiptId: string) {
    const [r] = await this.d.db.select().from(receipts).where(and(eq(receipts.id, receiptId), eq(receipts.userId, userId)));
    return r ?? null;
  }

  /** Extra address for receipts (the accountant) and the language they are written in. */
  async updateReceiptSettings(userId: string, settings: { ccEmail?: string | null; locale?: "he" | "en" }) {
    const patch: Partial<typeof users.$inferInsert> = {};
    if (settings.ccEmail !== undefined) patch.receiptCcEmail = settings.ccEmail ? settings.ccEmail.trim().toLowerCase() : null;
    if (settings.locale) patch.locale = settings.locale;
    if (Object.keys(patch).length) await this.d.db.update(users).set(patch).where(eq(users.id, userId));
  }

  async cancel(userId: string) {
    const sub = await this.currentSubscription(userId);
    if (!sub || !["active", "trial", "past_due"].includes(sub.status)) throw new BillingError("no_subscription");
    try {
      await this.d.paypal.cancelSubscription(sub.paypalSubscriptionId, "Cancelled by the user in Kabala");
    } catch (e) {
      throw new BillingError("paypal_unavailable", (e as Error).message);
    }
    await this.applyEvent(sub, { type: "cancelled" });
  }

  private async applyEvent(sub: SubRow, event: SubEvent): Promise<SubRow> {
    const now = this.now();
    const state = transition(toState(sub), event, now, pricingConfig.gracePeriodDays);
    await this.d.db.update(subscriptions).set({ ...state, updatedAt: now }).where(eq(subscriptions.id, sub.id));
    return { ...sub, ...state };
  }

  // ---- webhooks --------------------------------------------------------------------------

  /**
   * Verifies, audits and processes one webhook. Returns the HTTP status to send PayPal:
   * 400 for unverified (not retried usefully), 200 when handled or already handled,
   * 500 to make PayPal retry after a transient failure.
   */
  async handleWebhook(headers: WebhookHeaders, rawEvent: unknown): Promise<{ status: number; outcome: string }> {
    const event = rawEvent as { id?: string; event_type?: string; resource?: Record<string, unknown> };
    if (!event?.id || !event.event_type) return { status: 400, outcome: "malformed" };

    let verified = false;
    try {
      verified = await this.d.paypal.verifyWebhookSignature(headers, rawEvent);
    } catch (e) {
      this.log.error({ err: (e as Error).message }, "webhook verification call failed");
      return { status: 500, outcome: "verify_unavailable" };
    }
    const resourceId = typeof event.resource?.id === "string" ? event.resource.id : null;
    if (!verified) {
      // audited under a separate key so a forged event can never occupy a real event id
      // the body is not kept: it is attacker-controlled (could fill the database) and may hold someone's personal data
      await this.d.db.insert(webhookEvents).values({ id: `unverified:${randomUUID()}`, eventType: event.event_type, resourceId, verified: false, payload: {}, error: "signature verification failed" });
      this.log.warn({ eventType: event.event_type }, "rejected unverified webhook");
      return { status: 400, outcome: "unverified" };
    }

    // audit BEFORE processing; the PayPal event id is the primary key → idempotency
    await this.d.db.insert(webhookEvents).values({ id: event.id, eventType: event.event_type, resourceId, verified: true, payload: rawEvent as object }).onConflictDoNothing();
    const [row] = await this.d.db.select().from(webhookEvents).where(eq(webhookEvents.id, event.id));
    if (row?.processedAt) return { status: 200, outcome: "duplicate" };

    try {
      const outcome = await this.process(event.event_type, event.resource ?? {});
      await this.d.db.update(webhookEvents).set({ processedAt: this.now(), error: null }).where(eq(webhookEvents.id, event.id));
      return { status: 200, outcome };
    } catch (e) {
      const msg = (e as Error).message;
      await this.d.db.update(webhookEvents).set({ error: msg.slice(0, 500) }).where(eq(webhookEvents.id, event.id));
      this.log.error({ eventType: event.event_type, err: msg }, "webhook processing failed");
      // permanent problems (not ours) are acknowledged; transient ones are retried by PayPal
      return e instanceof BillingError && e.code !== "paypal_unavailable" ? { status: 200, outcome: `ignored:${e.code}` } : { status: 500, outcome: "retry" };
    }
  }

  private async process(type: string, resource: Record<string, unknown>): Promise<string> {
    switch (type) {
      case "BILLING.SUBSCRIPTION.ACTIVATED":
      case "BILLING.SUBSCRIPTION.CANCELLED":
      case "BILLING.SUBSCRIPTION.SUSPENDED":
      case "BILLING.SUBSCRIPTION.EXPIRED":
        return this.onSubscriptionChanged(String(resource.id));
      case "BILLING.SUBSCRIPTION.PAYMENT.FAILED":
        return this.onPaymentFailed(String(resource.id));
      case "PAYMENT.SALE.COMPLETED":
        return this.onSaleCompleted(resource);
      case "PAYMENT.SALE.REFUNDED":
        return this.onSaleRefunded(resource);
      default:
        return "ignored";
    }
  }

  /** Re-reads the subscription from PayPal: correct even when events arrive out of order. */
  private async onSubscriptionChanged(paypalSubscriptionId: string): Promise<string> {
    const remote = await this.fetchRemote(paypalSubscriptionId);
    const plan = await this.planByPayPalId(remote.planId);
    if (!plan) throw new BillingError("subscription_mismatch", "unknown plan");
    const [existing] = await this.d.db.select().from(subscriptions).where(eq(subscriptions.paypalSubscriptionId, remote.id));
    const userId = existing?.userId ?? remote.customId;
    if (!userId || !(await this.user(userId))) throw new BillingError("subscription_mismatch", "no matching user");
    const sub = await this.applyRemote(remote, plan, userId);
    return `subscription:${sub.status}`;
  }

  private async onPaymentFailed(paypalSubscriptionId: string): Promise<string> {
    const [sub] = await this.d.db.select().from(subscriptions).where(eq(subscriptions.paypalSubscriptionId, paypalSubscriptionId));
    if (!sub) throw new BillingError("subscription_not_found");
    const wasPastDue = sub.status === "past_due";
    const updated = await this.applyEvent(sub, { type: "payment_failed" });
    if (!wasPastDue && updated.status === "past_due" && updated.graceUntil) {
      const user = await this.user(sub.userId);
      if (user) await this.safeMail(async () => void (await this.d.mailer.send(paymentFailedMail(user.email, updated.graceUntil!))));
    }
    return `subscription:${updated.status}`;
  }

  private async onSaleCompleted(sale: Record<string, unknown>): Promise<string> {
    const saleId = String(sale.id);
    const agreement = String(sale.billing_agreement_id ?? "");
    const amount = sale.amount as { total?: string; currency?: string } | undefined;
    if (!agreement || !amount?.total || !amount.currency) throw new BillingError("subscription_mismatch", "sale without subscription");
    const [row] = await this.d.db
      .select()
      .from(subscriptions)
      .innerJoin(plans, eq(plans.id, subscriptions.planId))
      .where(eq(subscriptions.paypalSubscriptionId, agreement));
    if (!row) {
      // payment before we ever saw the subscription: learn it from PayPal first
      await this.onSubscriptionChanged(agreement);
      return this.onSaleCompleted(sale);
    }
    const { subscriptions: sub, plans: plan } = row;
    const paidMinor = fromMajor(amount.total);
    // Every amount is checked against OUR plan, never trusted from outside.
    if (amount.currency !== plan.currency || paidMinor !== plan.amountMinor) {
      throw new BillingError("subscription_mismatch", `amount ${amount.total} ${amount.currency} does not match plan ${toMajor(plan.amountMinor)} ${plan.currency}`);
    }
    const paidAt = sale.create_time ? new Date(String(sale.create_time)) : this.now();
    await this.d.db
      .insert(payments)
      .values({ userId: sub.userId, subscriptionId: sub.id, paypalSaleId: saleId, amountMinor: paidMinor, currency: amount.currency, paidAt })
      .onConflictDoNothing();
    const [payment] = await this.d.db.select().from(payments).where(eq(payments.paypalSaleId, saleId));
    let next: Date | null = null;
    try {
      const remote = await this.d.paypal.getSubscription(agreement);
      next = remote.nextBillingTime ? new Date(remote.nextBillingTime) : null;
    } catch {
      // period end will be corrected by reconcile
    }
    await this.applyEvent(sub, { type: "payment_completed", nextBillingTime: next });
    // Receipt: created (idempotently) and queued; the PDF and the email follow in the background.
    // A failure here answers 500, so PayPal redelivers; the payment row above is already safe.
    await this.d.receipts?.onPayment(payment!.id);
    // A license key with the first payment (later payments keep the same key). Idempotent: a redelivered
    // webhook finds the key already there and sends nothing.
    const key = await this.d.keys?.issue(sub.userId, sub.id);
    if (key) {
      const user = await this.user(sub.userId);
      if (user) await this.safeMail(async () => void (await this.d.mailer.send(licenseKeyMail(user.email, key))));
    }
    return "payment:recorded";
  }

  private async onSaleRefunded(refund: Record<string, unknown>): Promise<string> {
    const saleId = String(refund.sale_id ?? "");
    const amount = refund.amount as { total?: string } | undefined;
    const [pay] = await this.d.db.select().from(payments).where(eq(payments.paypalSaleId, saleId));
    if (!pay || !amount?.total) throw new BillingError("subscription_not_found", "refund for unknown sale");
    const refunded = Math.min(pay.amountMinor, pay.refundedMinor + fromMajor(amount.total));
    const full = refunded >= pay.amountMinor;
    await this.d.db.update(payments).set({ refundedMinor: refunded, status: full ? "refunded" : "partially_refunded" }).where(eq(payments.id, pay.id));
    if (full) {
      const [sub] = await this.d.db.select().from(subscriptions).where(eq(subscriptions.id, pay.subscriptionId));
      if (sub) await this.applyEvent(sub, { type: "refunded_in_full" });
    }
    const refundId = String(refund.id ?? "");
    if (refundId) {
      try {
        await this.d.receipts?.onRefund(pay.id, refundId, fromMajor(amount.total));
      } catch (e) {
        // a refund larger than what is left to credit is a data problem, not something PayPal can fix by retrying
        if (!(e instanceof ReceiptError)) throw e;
        this.log.error({ refund: refundId, err: e.message }, "refund document not issued");
      }
    }
    return full ? "payment:refunded" : "payment:partially_refunded";
  }

  private async fetchRemote(id: string): Promise<PayPalSubscription> {
    try {
      return await this.d.paypal.getSubscription(id);
    } catch (e) {
      if (e instanceof PayPalError && e.status === 404) throw new BillingError("subscription_not_found");
      throw new BillingError("paypal_unavailable", (e as Error).message);
    }
  }

  private async safeMail(send: () => Promise<void>) {
    try {
      await send();
    } catch (e) {
      this.log.warn({ err: (e as Error).message }, "email failed");
    }
  }

  // ---- reconcile ------------------------------------------------------------------------------

  /** Daily job: compares every live subscription with PayPal and fixes drift; expires lapsed ones. */
  async reconcile(): Promise<{ checked: number; fixed: { id: string; from: string; to: string }[]; errors: number }> {
    const live = await this.d.db
      .select()
      .from(subscriptions)
      .innerJoin(plans, eq(plans.id, subscriptions.planId))
      .where(and(inArray(subscriptions.status, [...PRO_STATUSES, "pending"]), eq(plans.mode, this.mode)));
    const fixed: { id: string; from: string; to: string }[] = [];
    let errors = 0;
    for (const { subscriptions: sub, plans: plan } of live) {
      try {
        const remote = await this.d.paypal.getSubscription(sub.paypalSubscriptionId);
        let updated = await this.applyRemote(remote, plan, sub.userId);
        updated = await this.housekeep(updated, this.now());
        if (updated.status !== sub.status) fixed.push({ id: sub.paypalSubscriptionId, from: sub.status, to: updated.status });
      } catch (e) {
        errors++;
        this.log.warn({ id: sub.paypalSubscriptionId, err: (e as Error).message }, "reconcile failed for subscription");
      }
    }
    return { checked: live.length, fixed, errors };
  }
}
