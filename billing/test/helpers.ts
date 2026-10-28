import { randomUUID } from "node:crypto";
import { buildApp } from "../src/app.js";
import { openDatabase } from "../src/db/client.js";
import { ConsoleMailer, type Mailer } from "../src/email/mailer.js";
import type { Seller } from "../src/pdf/receipt-document.js";
import { InternalProvider } from "../src/providers/internal.js";
import type { ReceiptProvider } from "../src/providers/types.js";
import { LicenseKeyService } from "../src/licenses/keys.js";
import { InlineQueue } from "../src/queue/inline.js";
import { taxPolicy, type BusinessType } from "../src/receipts/amounts.js";
import { ReceiptPipeline } from "../src/receipts/pipeline.js";
import { ReceiptService } from "../src/receipts/service.js";
import { BillingService } from "../src/services/billing.js";
import { syncPricing } from "../src/services/sync-pricing.js";
import { TokenService } from "../src/services/tokens.js";
import { LinkSigner } from "../src/storage/signed-links.js";
import { MemoryStorage } from "../src/storage/storage.js";
import { FakePayPal, VALID_HEADERS } from "./fakes.js";

export const SELLER: Seller = { name: "Kabala", taxId: "123456789", address: "תל אביב", email: "billing@kabala.test", businessType: "exempt" };

export async function setup(opts: { sync?: boolean; paypalClientId?: string; provider?: ReceiptProvider; mailer?: Mailer; businessType?: BusinessType; adminPublicKeyPem?: string } = {}) {
  const database = openDatabase("pglite:memory");
  await database.migrate();
  const paypal = new FakePayPal();
  const tokens = await TokenService.generate();
  const mailer = new ConsoleMailer();
  let clock = new Date("2026-10-21T10:00:00Z");
  const businessType = opts.businessType ?? "exempt";
  const queue = new InlineQueue(5);
  const storage = new MemoryStorage();
  const links = new LinkSigner("test-secret-test-secret-test-secret-1234", "http://localhost:8080");
  const receiptService = new ReceiptService({ db: database.db, tax: taxPolicy(businessType, 18), provider: (opts.provider ?? new InternalProvider()).name, now: () => clock });
  const pipeline = new ReceiptPipeline({
    db: database.db,
    receipts: receiptService,
    provider: opts.provider ?? new InternalProvider(),
    storage,
    mailer: opts.mailer ?? mailer,
    links,
    seller: { ...SELLER, businessType },
    queue,
    accountUrl: "http://localhost:8080/account",
    adminEmail: "admin@kabala.test",
    copyTo: "records@kabala.test",
    now: () => clock,
  });
  await pipeline.start();
  const keys = new LicenseKeyService({ db: database.db, secret: "test-license-key-secret-0123456789abcdef", now: () => clock });
  const billing = new BillingService({ db: database.db, paypal, tokens, receipts: pipeline, mailer, licenseTtlDays: 7, keys, now: () => clock });
  if (opts.sync !== false) await syncPricing(database.db, paypal);
  const app = await buildApp({
    billing,
    tokens,
    verifyGoogle: async (t) => {
      if (!t.startsWith("google:")) throw new Error("bad token");
      return { email: t.slice(7) };
    },
    paypalClientId: opts.paypalClientId ?? "sandbox-client-id",
    publicUrl: "http://localhost:8080",
    deepLink: "kabala://payment-success",
    production: false,
    logger: false,
    receipts: { storage, links, resend: (id, uid) => pipeline.resend(id, uid) },
    keys,
    admin: opts.adminPublicKeyPem ? { db: database.db, publicKeyPem: opts.adminPublicKeyPem, keys, receipts: { pipeline, storage } } : undefined,
  });

  async function login(email = "dana@gmail.com") {
    const r = await app.inject({ method: "POST", url: "/api/session", payload: { id_token: `google:${email}` } });
    const body = r.json() as { session: string; license: string };
    const claims = await tokens.verify(body.session, "session");
    return { session: body.session, license: body.license, userId: claims.sub, auth: { authorization: `Bearer ${body.session}` } };
  }

  async function webhook(event_type: string, resource: Record<string, unknown>, opts: { id?: string; sig?: string } = {}) {
    return app.inject({
      method: "POST",
      url: "/api/webhooks/paypal",
      headers: { ...VALID_HEADERS, "paypal-transmission-sig": opts.sig ?? "valid" },
      payload: { id: opts.id ?? `WH-${randomUUID()}`, event_type, resource_type: "x", resource },
    });
  }

  return {
    database,
    db: database.db,
    paypal,
    tokens,
    mailer,
    billing,
    keys,
    pipeline,
    receipts: receiptService,
    queue,
    storage,
    links,
    app,
    login,
    webhook,
    setNow: (d: Date) => (clock = d),
    now: () => clock,
    async close() {
      await app.close();
      await database.close();
    },
  };
}
