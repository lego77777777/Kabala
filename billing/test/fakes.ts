import type { CreatePlanInput, PayPalGateway, PayPalPlan, PayPalSubscription, WebhookHeaders } from "../src/paypal/gateway.js";
import { PayPalError } from "../src/paypal/gateway.js";

/** In-memory PayPal. Webhooks verify when paypal-transmission-sig === "valid". */
export class FakePayPal implements PayPalGateway {
  readonly mode = "sandbox" as const;
  products = new Map<string, { id: string }>();
  plans = new Map<string, PayPalPlan>();
  subs = new Map<string, PayPalSubscription>();
  calls: string[] = [];
  down = false;
  private n = 0;

  private guard(call: string) {
    this.calls.push(call);
    if (this.down) throw new PayPalError("PayPal unavailable", 503);
  }

  async createProduct(input: { requestId: string }) {
    this.guard("createProduct");
    const existing = [...this.products.values()].find((p) => p.id === `PROD-${input.requestId}`);
    if (existing) return existing;
    const p = { id: `PROD-${input.requestId}` };
    this.products.set(p.id, p);
    return p;
  }

  async getProduct(id: string) {
    this.guard("getProduct");
    return this.products.get(id) ?? null;
  }

  async createPlan(input: CreatePlanInput) {
    this.guard("createPlan");
    const plan: PayPalPlan = { id: `P-${++this.n}${input.interval.toUpperCase()}`, productId: input.productId, status: "ACTIVE", interval: input.interval, currency: input.currency, price: input.price };
    this.plans.set(plan.id, plan);
    return plan;
  }

  async getPlan(id: string) {
    this.guard("getPlan");
    const p = this.plans.get(id);
    if (!p) throw new PayPalError("not found", 404);
    return p;
  }

  async deactivatePlan(id: string) {
    this.guard("deactivatePlan");
    this.plans.get(id)!.status = "INACTIVE";
  }

  async activatePlan(id: string) {
    this.guard("activatePlan");
    this.plans.get(id)!.status = "ACTIVE";
  }

  async getSubscription(id: string) {
    this.guard("getSubscription");
    const s = this.subs.get(id);
    if (!s) throw new PayPalError("not found", 404);
    return { ...s };
  }

  async cancelSubscription(id: string) {
    this.guard("cancelSubscription");
    const s = this.subs.get(id);
    if (!s) throw new PayPalError("not found", 404);
    s.status = "CANCELLED";
  }

  async verifyWebhookSignature(headers: WebhookHeaders) {
    this.guard("verifyWebhookSignature");
    return headers["paypal-transmission-sig"] === "valid";
  }

  /** What PayPal's popup does when the buyer approves. */
  approve(planId: string, customId: string, opts: Partial<PayPalSubscription> = {}): PayPalSubscription {
    const sub: PayPalSubscription = {
      id: `I-${String(++this.n).padStart(10, "0")}`,
      planId,
      status: "ACTIVE",
      customId,
      subscriberEmail: "buyer@example.com",
      subscriberName: "דנה כהן",
      nextBillingTime: new Date(Date.now() + 30 * 86_400_000).toISOString(),
      inTrial: false,
      lastPayment: null,
      failedPayments: 0,
      ...opts,
    };
    this.subs.set(sub.id, sub);
    return sub;
  }
}

export const VALID_HEADERS = {
  "paypal-auth-algo": "SHA256withRSA",
  "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT",
  "paypal-transmission-id": "tx-1",
  "paypal-transmission-sig": "valid",
  "paypal-transmission-time": new Date().toISOString(),
};
