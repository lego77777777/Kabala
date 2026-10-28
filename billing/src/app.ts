/**
 * HTTP API + the three web pages (pricing, checkout, account).
 *
 *   GET  /api/pricing                 prices from pricing.config.ts + synced PayPal plan ids
 *   GET  /api/client-config           PayPal *client id* and mode for the browser (never the secret)
 *   POST /api/session                 Google ID token → session + license (desktop app)
 *   GET  /api/license                 fresh signed license (desktop app, every launch / daily)
 *   POST /api/links                   short-lived browser link for checkout or the account page
 *   POST /api/checkout/start          { interval, currency } → PayPal plan id chosen by the server
 *   POST /api/checkout/confirm        { subscriptionId } → verified with PayPal, then Pro
 *   GET  /api/account                 plan, next billing date, payments, receipts, receipt settings
 *   POST /api/account/cancel          cancel at PayPal (Pro until the end of the paid period)
 *   POST /api/account/receipt-settings { ccEmail, locale } extra address for receipts, their language
 *   GET  /api/receipts/:id/pdf        the user's own receipt PDF
 *   POST /api/receipts/:id/resend     email the receipt again
 *   GET  /r/:id/:expires/:sig.pdf     signed, expiring receipt link (used in emails), no login
 *   POST /api/webhooks/paypal         PayPal webhooks (signature verified, idempotent, audited)
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from "fastify";
import helmet from "@fastify/helmet";
import rateLimit from "@fastify/rate-limit";
import fastifyStatic from "@fastify/static";
import { z } from "zod";
import { registerAdminApi } from "./admin/admin-api.js";
import type { Currency } from "./config/pricing.config.js";
import type { Db } from "./db/client.js";
import { pricingConfig } from "./config/pricing.config.js";
import { BillingError, type BillingService } from "./services/billing.js";
import type { GoogleVerifier } from "./services/google.js";
import type { TokenService } from "./services/tokens.js";
import type { WebhookHeaders } from "./paypal/gateway.js";
import type { LinkSigner } from "./storage/signed-links.js";
import type { Storage } from "./storage/storage.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export interface AppDeps {
  billing: BillingService;
  tokens: TokenService;
  verifyGoogle: GoogleVerifier;
  paypalClientId: string | null;
  publicUrl: string;
  deepLink: string;
  production: boolean;
  logger?: boolean;
  /** receipt PDFs and the "send again" action (src/receipts/pipeline.ts) */
  receipts?: { storage: Storage; links: LinkSigner; resend(receiptId: string, userId: string): Promise<boolean> };
  /** read-only API for the admin panel (src/admin/admin-api.ts); off without ADMIN_PANEL_PUBLIC_KEY */
  admin?: { db: Db; publicKeyPem: string };
}

const ERROR_STATUS: Record<BillingError["code"], number> = {
  plan_not_synced: 503,
  subscription_not_found: 404,
  subscription_mismatch: 403,
  not_active_yet: 409,
  no_subscription: 409,
  paypal_unavailable: 502,
  invalid_currency: 400,
};

export const redactUrl = (url: string) => url.replace(/^\/r\/([^/]+)\/([^/]+)\/[^/?#]+/, "/r/$1/$2/[redacted]");

export async function buildApp(d: AppDeps): Promise<FastifyInstance> {
  const app = Fastify({
    logger: d.logger === false ? false : {
      level: "info",
      // never log credentials or payment payloads
      redact: ["req.headers.authorization", 'req.headers["paypal-transmission-sig"]', 'req.headers["paypal-cert-url"]', "req.body"],
      serializers: {
        // /r/<id>/<expires>/<sig>.pdf downloads a customer's receipt without logging in: never write the signature
        req: (req: { method: string; url: string }) => ({ method: req.method, url: redactUrl(req.url) }),
      },
    },
    trustProxy: true,
    bodyLimit: 256 * 1024,
  });

  await app.register(helmet, {
    contentSecurityPolicy: {
      directives: {
        defaultSrc: ["'self'"],
        scriptSrc: ["'self'", "https://*.paypal.com", "https://*.paypalobjects.com"],
        frameSrc: ["https://*.paypal.com"],
        connectSrc: ["'self'", "https://*.paypal.com"],
        imgSrc: ["'self'", "data:", "https://*.paypal.com", "https://*.paypalobjects.com"],
        styleSrc: ["'self'", "'unsafe-inline'"],
        formAction: ["'self'"],
        upgradeInsecureRequests: d.production ? [] : null,
      },
    },
    hsts: d.production,
  });
  await app.register(rateLimit, { max: 120, timeWindow: "1 minute" });

  // HTTPS only in production (behind a proxy that sets x-forwarded-proto)
  if (d.production) {
    app.addHook("onRequest", async (req, reply) => {
      if (req.protocol !== "https") return reply.code(403).send({ error: "https_required" });
    });
  }

  await app.register(fastifyStatic, { root: path.join(ROOT, "web"), prefix: "/", index: false });
  // the official PayPal JS loader, served from our origin
  await app.register(fastifyStatic, {
    root: path.join(ROOT, "node_modules/@paypal/paypal-js/dist/iife"),
    prefix: "/vendor/paypal-js/",
    decorateReply: false,
  });
  for (const page of ["pricing", "checkout", "account"]) {
    app.get(`/${page}`, (_req, reply) => reply.sendFile(`${page}.html`));
  }

  app.setErrorHandler((err, req, reply) => {
    if (err instanceof BillingError) return reply.code(ERROR_STATUS[err.code]).send({ error: err.code });
    if (err instanceof z.ZodError) return reply.code(400).send({ error: "invalid_request" });
    if ((err as { statusCode?: number }).statusCode === 429) return reply.code(429).send({ error: "rate_limited" });
    req.log.error({ err: (err as Error).message }, "unhandled error");
    return reply.code(500).send({ error: "internal" });
  });

  // ---- auth helpers --------------------------------------------------------------------

  const bearer = (req: FastifyRequest) => (req.headers.authorization ?? "").replace(/^Bearer\s+/i, "");

  /** desktop session token, or a short-lived browser link token */
  async function userId(req: FastifyRequest, reply: FastifyReply, allowLink = false): Promise<string | null> {
    const token = bearer(req);
    for (const aud of allowLink ? (["session", "checkout"] as const) : (["session"] as const)) {
      try {
        return (await d.tokens.verify(token, aud)).sub;
      } catch {
        /* try next audience */
      }
    }
    await reply.code(401).send({ error: "unauthorized" });
    return null;
  }

  // ---- public ---------------------------------------------------------------------------------

  app.get("/health", async () => ({ ok: true, mode: d.billing.mode }));

  app.get("/api/pricing", async (req) => {
    const q = z.object({ currency: z.enum(["ILS", "USD"]).optional() }).parse(req.query);
    return d.billing.pricing((q.currency ?? pricingConfig.defaultCurrency) as Currency);
  });

  app.get("/api/client-config", async () => ({
    paypalClientId: d.paypalClientId,
    mode: d.billing.mode,
    currency: pricingConfig.defaultCurrency,
    deepLink: d.deepLink,
  }));

  // ---- desktop app ------------------------------------------------------------------------------

  app.post("/api/session", { config: { rateLimit: { max: 20, timeWindow: "1 minute" } } }, async (req, reply) => {
    const { id_token } = z.object({ id_token: z.string().min(10) }).parse(req.body);
    let email: string;
    try {
      ({ email } = await d.verifyGoogle(id_token));
    } catch {
      return reply.code(401).send({ error: "invalid_google_token" });
    }
    const user = await d.billing.userByEmail(email);
    return { session: await d.tokens.sign("session", { sub: user.id, email: user.email }, 30 * 86_400), license: await d.billing.license(user.id) };
  });

  app.get("/api/license", async (req, reply) => {
    const uid = await userId(req, reply);
    if (!uid) return;
    return { license: await d.billing.license(uid), publicKey: d.tokens.publicKeyPem };
  });

  app.post("/api/links", { config: { rateLimit: { max: 30, timeWindow: "1 minute" } } }, async (req, reply) => {
    const uid = await userId(req, reply);
    if (!uid) return;
    const { page } = z.object({ page: z.enum(["checkout", "account"]) }).parse(req.body);
    const token = await d.tokens.sign("checkout", { sub: uid }, 30 * 60);
    // token in the URL fragment: never sent to any server, not in logs or Referer headers
    return { url: `${d.publicUrl.replace(/\/$/, "")}/${page}#t=${encodeURIComponent(token)}` };
  });

  // ---- checkout (browser, link token) -------------------------------------------------------------

  app.post("/api/checkout/start", { config: { rateLimit: { max: 30, timeWindow: "1 minute" } } }, async (req, reply) => {
    const uid = await userId(req, reply, true);
    if (!uid) return;
    const body = z.object({ interval: z.enum(["monthly", "yearly"]), currency: z.enum(["ILS", "USD"]).default(pricingConfig.defaultCurrency) }).parse(req.body);
    return d.billing.checkoutStart(uid, body.interval, body.currency);
  });

  app.post("/api/checkout/confirm", { config: { rateLimit: { max: 30, timeWindow: "1 minute" } } }, async (req, reply) => {
    const uid = await userId(req, reply, true);
    if (!uid) return;
    const { subscriptionId } = z.object({ subscriptionId: z.string().regex(/^I-[A-Z0-9]{6,32}$/) }).parse(req.body);
    const result = await d.billing.confirmCheckout(uid, subscriptionId);
    return { ...result, deepLink: `${d.deepLink}?status=${result.entitled ? "pro" : result.status}` };
  });

  // ---- account ---------------------------------------------------------------------------------------

  app.get("/api/account", async (req, reply) => {
    const uid = await userId(req, reply, true);
    if (!uid) return;
    return d.billing.account(uid);
  });

  app.post("/api/account/cancel", { config: { rateLimit: { max: 10, timeWindow: "1 minute" } } }, async (req, reply) => {
    const uid = await userId(req, reply, true);
    if (!uid) return;
    await d.billing.cancel(uid);
    return d.billing.account(uid);
  });

  app.post("/api/account/receipt-settings", { config: { rateLimit: { max: 20, timeWindow: "1 minute" } } }, async (req, reply) => {
    const uid = await userId(req, reply, true);
    if (!uid) return;
    const body = z
      .object({ ccEmail: z.union([z.string().trim().email().max(254), z.literal(""), z.null()]).optional(), locale: z.enum(["he", "en"]).optional() })
      .parse(req.body ?? {});
    await d.billing.updateReceiptSettings(uid, { ccEmail: body.ccEmail === "" ? null : body.ccEmail, locale: body.locale });
    return d.billing.account(uid);
  });

  const sendPdf = async (reply: FastifyReply, r: { id: string; kind: string; number: number; pdfKey: string | null; providerDocumentNumber: string | null }) => {
    const pdf = r.pdfKey && d.receipts ? await d.receipts.storage.get(r.pdfKey) : null;
    if (!pdf) return reply.code(404).send({ error: "not_ready" });
    const name = `kabala-${r.kind}-${r.providerDocumentNumber ?? String(r.number).padStart(6, "0")}.pdf`;
    return reply
      .header("Content-Type", "application/pdf")
      .header("Content-Disposition", `attachment; filename="${name}"`)
      .header("Cache-Control", "private, no-store")
      .header("X-Robots-Tag", "noindex")
      .send(pdf);
  };

  app.get("/api/receipts/:id/pdf", async (req, reply) => {
    const uid = await userId(req, reply, true);
    if (!uid) return;
    const { id } = z.object({ id: z.string().uuid() }).parse(req.params);
    const r = await d.billing.receiptFor(uid, id);
    if (!r) return reply.code(404).send({ error: "not_found" });
    return sendPdf(reply, r);
  });

  app.post("/api/receipts/:id/resend", { config: { rateLimit: { max: 5, timeWindow: "10 minutes" } } }, async (req, reply) => {
    const uid = await userId(req, reply, true);
    if (!uid) return;
    const { id } = z.object({ id: z.string().uuid() }).parse(req.params);
    if (!d.receipts || !(await d.receipts.resend(id, uid))) return reply.code(404).send({ error: "not_found" });
    return { ok: true };
  });

  // Link inside the receipt email: works without logging in, but only for that receipt and until it expires.
  app.get("/r/:id/:expires/:sig", { config: { rateLimit: { max: 30, timeWindow: "1 minute" } } }, async (req, reply) => {
    const p = z.object({ id: z.string().uuid(), expires: z.coerce.number().int(), sig: z.string().regex(/^[A-Za-z0-9_-]{20,100}\.pdf$/) }).safeParse(req.params);
    if (!p.success || !d.receipts?.links.verify(p.data.id, p.data.expires, p.data.sig.slice(0, -4))) {
      return reply.code(403).type("text/plain; charset=utf-8").send("הקישור פג תוקף או שאינו תקין. אפשר להוריד את הקבלה מ\"המנוי שלי\".");
    }
    const r = await d.billing.receiptById(p.data.id);
    if (!r) return reply.code(404).send({ error: "not_found" });
    return sendPdf(reply, r);
  });

  if (d.admin) await registerAdminApi(app, { ...d.admin, mode: d.billing.mode });

  // ---- PayPal webhooks ----------------------------------------------------------------------------------

  app.post("/api/webhooks/paypal", { config: { rateLimit: { max: 600, timeWindow: "1 minute" } } }, async (req, reply) => {
    const h = req.headers;
    const headers: WebhookHeaders = {
      "paypal-auth-algo": h["paypal-auth-algo"] as string | undefined,
      "paypal-cert-url": h["paypal-cert-url"] as string | undefined,
      "paypal-transmission-id": h["paypal-transmission-id"] as string | undefined,
      "paypal-transmission-sig": h["paypal-transmission-sig"] as string | undefined,
      "paypal-transmission-time": h["paypal-transmission-time"] as string | undefined,
    };
    const result = await d.billing.handleWebhook(headers, req.body);
    req.log.info({ eventType: (req.body as { event_type?: string })?.event_type, outcome: result.outcome }, "paypal webhook");
    return reply.code(result.status).send({ outcome: result.outcome });
  });

  return app;
}
