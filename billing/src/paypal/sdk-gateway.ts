/**
 * Production PayPalGateway.
 *  - Billing plans & subscriptions: official @paypal/paypal-server-sdk (SubscriptionsController).
 *  - Catalog products & webhook signature verification: not in the SDK → REST with fetch + zod.
 */
import {
  ApiError,
  Client,
  Environment,
  IntervalUnit,
  LogLevel,
  SubscriptionsController,
  TenureType,
  type BillingPlan,
  type Subscription,
} from "@paypal/paypal-server-sdk";
import { z } from "zod";
import type { Currency, Interval } from "../config/pricing.config.js";
import { fromMajor, toMajor } from "../pricing/pricing.js";
import {
  PayPalError,
  type CreatePlanInput,
  type PayPalGateway,
  type PayPalPlan,
  type PayPalSubscription,
  type PayPalSubscriptionStatus,
  type WebhookHeaders,
} from "./gateway.js";

const TokenResponse = z.object({ access_token: z.string(), expires_in: z.number() });
const ProductResponse = z.object({ id: z.string() });
const VerifyResponse = z.object({ verification_status: z.string() });

export interface SdkGatewayOptions {
  mode: "sandbox" | "live";
  clientId: string;
  clientSecret: string;
  webhookId?: string;
  apiBase: string;
  fetchImpl?: typeof fetch;
}

function wrap(e: unknown): never {
  if (e instanceof ApiError) {
    const body = (e.result ?? {}) as { name?: string; message?: string };
    throw new PayPalError(body.message ?? e.message, e.statusCode, body.name ?? null);
  }
  throw e;
}

export class SdkGateway implements PayPalGateway {
  readonly mode: "sandbox" | "live";
  private readonly subs: SubscriptionsController;
  private token: { value: string; expires: number } | null = null;
  private readonly fetch: typeof fetch;

  constructor(private readonly opts: SdkGatewayOptions) {
    this.mode = opts.mode;
    this.fetch = opts.fetchImpl ?? fetch;
    const client = new Client({
      clientCredentialsAuthCredentials: { oAuthClientId: opts.clientId, oAuthClientSecret: opts.clientSecret },
      environment: opts.mode === "live" ? Environment.Production : Environment.Sandbox,
      timeout: 20_000,
      // Request/response bodies can contain payer details: never log them.
      logging: { logLevel: LogLevel.Warn, logRequest: { logBody: false }, logResponse: { logBody: false } },
    });
    this.subs = new SubscriptionsController(client);
  }

  // ---- REST helpers (products, webhook verification) -----------------------------

  private async accessToken(): Promise<string> {
    if (this.token && this.token.expires > Date.now() + 60_000) return this.token.value;
    const basic = Buffer.from(`${this.opts.clientId}:${this.opts.clientSecret}`).toString("base64");
    const r = await this.fetch(`${this.opts.apiBase}/v1/oauth2/token`, {
      method: "POST",
      headers: { Authorization: `Basic ${basic}`, "Content-Type": "application/x-www-form-urlencoded" },
      body: "grant_type=client_credentials",
    });
    if (!r.ok) throw new PayPalError(`oauth failed (${r.status})`, r.status);
    const t = TokenResponse.parse(await r.json());
    this.token = { value: t.access_token, expires: Date.now() + t.expires_in * 1000 };
    return t.access_token;
  }

  private async rest(method: string, path: string, body?: unknown, extra: Record<string, string> = {}): Promise<Response> {
    return this.fetch(`${this.opts.apiBase}${path}`, {
      method,
      headers: { Authorization: `Bearer ${await this.accessToken()}`, "Content-Type": "application/json", ...extra },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  async createProduct(input: { name: string; description: string; type: string; category: string; requestId: string }) {
    const r = await this.rest("POST", "/v1/catalogs/products", {
      name: input.name,
      description: input.description,
      type: input.type,
      category: input.category,
    }, { "PayPal-Request-Id": input.requestId });
    if (!r.ok) throw new PayPalError(`create product failed (${r.status})`, r.status);
    return ProductResponse.parse(await r.json());
  }

  async getProduct(id: string) {
    const r = await this.rest("GET", `/v1/catalogs/products/${encodeURIComponent(id)}`);
    if (r.status === 404) return null;
    if (!r.ok) throw new PayPalError(`get product failed (${r.status})`, r.status);
    return ProductResponse.parse(await r.json());
  }

  async verifyWebhookSignature(headers: WebhookHeaders, event: unknown): Promise<boolean> {
    if (!this.opts.webhookId) throw new PayPalError("PAYPAL webhook id is not configured", null);
    const required = ["paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig", "paypal-transmission-time"] as const;
    if (required.some((h) => !headers[h])) return false;
    const r = await this.rest("POST", "/v1/notifications/verify-webhook-signature", {
      auth_algo: headers["paypal-auth-algo"],
      cert_url: headers["paypal-cert-url"],
      transmission_id: headers["paypal-transmission-id"],
      transmission_sig: headers["paypal-transmission-sig"],
      transmission_time: headers["paypal-transmission-time"],
      webhook_id: this.opts.webhookId,
      webhook_event: event,
    });
    if (!r.ok) return false;
    return VerifyResponse.parse(await r.json()).verification_status === "SUCCESS";
  }

  // ---- SDK: billing plans & subscriptions --------------------------------------------

  async createPlan(input: CreatePlanInput): Promise<PayPalPlan> {
    const unit = input.interval === "monthly" ? IntervalUnit.Month : IntervalUnit.Year;
    const cycles = [];
    let sequence = 1;
    if (input.trialDays > 0) {
      cycles.push({
        frequency: { intervalUnit: IntervalUnit.Day, intervalCount: input.trialDays },
        tenureType: TenureType.Trial,
        sequence: sequence++,
        totalCycles: 1,
      });
    }
    cycles.push({
      frequency: { intervalUnit: unit, intervalCount: 1 },
      tenureType: TenureType.Regular,
      sequence,
      totalCycles: 0, // renew until cancelled
      pricingScheme: { fixedPrice: { currencyCode: input.currency, value: input.price } },
    });
    try {
      const { result } = await this.subs.createBillingPlan({
        paypalRequestId: input.requestId,
        prefer: "return=representation",
        body: {
          productId: input.productId,
          name: input.name,
          description: input.description,
          billingCycles: cycles,
          // after 3 failed retries PayPal suspends the subscription (→ our grace period runs out)
          paymentPreferences: { autoBillOutstanding: true, paymentFailureThreshold: 3 },
        },
      });
      return toPlan(result);
    } catch (e) {
      return wrap(e);
    }
  }

  async getPlan(id: string): Promise<PayPalPlan> {
    try {
      return toPlan((await this.subs.getBillingPlan(id)).result);
    } catch (e) {
      return wrap(e);
    }
  }

  async deactivatePlan(id: string): Promise<void> {
    try {
      await this.subs.deactivateBillingPlan(id);
    } catch (e) {
      wrap(e);
    }
  }

  async activatePlan(id: string): Promise<void> {
    try {
      await this.subs.activateBillingPlan(id);
    } catch (e) {
      wrap(e);
    }
  }

  async getSubscription(id: string): Promise<PayPalSubscription> {
    try {
      return toSubscription((await this.subs.getSubscription({ id })).result);
    } catch (e) {
      return wrap(e);
    }
  }

  async cancelSubscription(id: string, reason: string): Promise<void> {
    try {
      await this.subs.cancelSubscription({ id, body: { reason } });
    } catch (e) {
      wrap(e);
    }
  }
}

function toPlan(p: BillingPlan): PayPalPlan {
  const regular = p.billingCycles?.find((c) => c.tenureType === TenureType.Regular);
  const price = regular?.pricingScheme?.fixedPrice;
  if (!p.id || !p.productId || !regular || !price) throw new PayPalError("unexpected plan shape from PayPal", null);
  const unit = regular.frequency.intervalUnit;
  return {
    id: p.id,
    productId: p.productId,
    status: (p.status ?? "CREATED") as PayPalPlan["status"],
    interval: (unit === IntervalUnit.Year ? "yearly" : "monthly") as Interval,
    currency: price.currencyCode as Currency,
    price: toMajor(fromMajor(price.value)), // PayPal answers "29.0"; we always use "29.00"
  };
}

function toSubscription(s: Subscription): PayPalSubscription {
  if (!s.id || !s.planId || !s.status) throw new PayPalError("unexpected subscription shape from PayPal", null);
  const cycles = s.billingInfo?.cycleExecutions ?? [];
  const last = s.billingInfo?.lastPayment;
  return {
    id: s.id,
    planId: s.planId,
    status: s.status as PayPalSubscriptionStatus,
    customId: s.customId ?? null,
    subscriberEmail: s.subscriber?.emailAddress ?? null,
    subscriberName: [s.subscriber?.name?.givenName, s.subscriber?.name?.surname].filter(Boolean).join(" ") || null,
    nextBillingTime: s.billingInfo?.nextBillingTime ?? null,
    inTrial: cycles.some((c) => c.tenureType === TenureType.Trial && (c.cyclesRemaining ?? 0) > 0 && (c.cyclesCompleted ?? 0) === 0),
    lastPayment: last?.amount ? { amount: last.amount.value, currency: last.amount.currencyCode, time: last.time ?? "" } : null,
    failedPayments: s.billingInfo?.failedPaymentsCount ?? 0,
  };
}
