/**
 * Everything Kabala needs from PayPal, behind one interface so services and tests don't care
 * whether calls go through the official SDK or plain REST.
 */
import type { Currency, Interval } from "../config/pricing.config.js";

export type PayPalSubscriptionStatus = "APPROVAL_PENDING" | "APPROVED" | "ACTIVE" | "SUSPENDED" | "CANCELLED" | "EXPIRED";

export interface PayPalSubscription {
  id: string;
  planId: string;
  status: PayPalSubscriptionStatus;
  customId: string | null;
  subscriberEmail: string | null;
  /** the payer's name as PayPal has it (shown on receipts) */
  subscriberName: string | null;
  nextBillingTime: string | null;
  /** true while the trial billing cycle is running */
  inTrial: boolean;
  lastPayment: { amount: string; currency: string; time: string } | null;
  /** PayPal keeps a subscription ACTIVE while it retries failed charges; this counts them */
  failedPayments: number;
}

export interface PayPalPlan {
  id: string;
  productId: string;
  status: "CREATED" | "ACTIVE" | "INACTIVE";
  interval: Interval;
  currency: Currency;
  /** regular-cycle price, e.g. "29.00" */
  price: string;
}

export interface CreatePlanInput {
  productId: string;
  name: string;
  description: string;
  interval: Interval;
  currency: Currency;
  price: string;
  trialDays: number;
  /** idempotency key for the create call */
  requestId: string;
}

export interface WebhookHeaders {
  "paypal-auth-algo"?: string;
  "paypal-cert-url"?: string;
  "paypal-transmission-id"?: string;
  "paypal-transmission-sig"?: string;
  "paypal-transmission-time"?: string;
}

export interface PayPalGateway {
  readonly mode: "sandbox" | "live";
  createProduct(input: { name: string; description: string; type: string; category: string; requestId: string }): Promise<{ id: string }>;
  getProduct(id: string): Promise<{ id: string } | null>;
  createPlan(input: CreatePlanInput): Promise<PayPalPlan>;
  getPlan(id: string): Promise<PayPalPlan>;
  deactivatePlan(id: string): Promise<void>;
  activatePlan(id: string): Promise<void>;
  getSubscription(id: string): Promise<PayPalSubscription>;
  cancelSubscription(id: string, reason: string): Promise<void>;
  /** POST /v1/notifications/verify-webhook-signature; false for anything not "SUCCESS" */
  verifyWebhookSignature(headers: WebhookHeaders, event: unknown): Promise<boolean>;
}

export class PayPalError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
    /** PayPal's error name, e.g. RESOURCE_NOT_FOUND */
    readonly code: string | null = null,
  ) {
    super(message);
  }
}
