/** Wires real dependencies from the environment (used by the server and the scripts). */
import { createHash } from "node:crypto";
import { loadEnv, type EnvConfig } from "./config/env.js";
import { openDatabase, type Database } from "./db/client.js";
import { ConsoleMailer, mailerFromEnv, type Mailer } from "./email/mailer.js";
import type { PayPalGateway } from "./paypal/gateway.js";
import { SdkGateway } from "./paypal/sdk-gateway.js";
import type { Seller } from "./pdf/receipt-document.js";
import { InternalProvider } from "./providers/internal.js";
import { MorningProvider } from "./providers/morning.js";
import type { ReceiptProvider } from "./providers/types.js";
import { startQueue, type JobQueue } from "./queue/queue.js";
import { taxPolicy } from "./receipts/amounts.js";
import { ReceiptPipeline } from "./receipts/pipeline.js";
import { ReceiptService } from "./receipts/service.js";
import { LicenseKeyService } from "./licenses/keys.js";
import { ReceiptSettingsService } from "./receipts/settings.js";
import { BillingService } from "./services/billing.js";
import { TokenService } from "./services/tokens.js";
import { LinkSigner } from "./storage/signed-links.js";
import { LocalStorage, S3Storage, type Storage } from "./storage/storage.js";

export interface Context {
  env: EnvConfig;
  database: Database;
  paypal: PayPalGateway;
  tokens: TokenService;
  billing: BillingService;
  mailer: Mailer;
  storage: Storage;
  links: LinkSigner;
  queue: JobQueue;
  pipeline: ReceiptPipeline;
  keys: LicenseKeyService;
  receiptSettings: ReceiptSettingsService;
}

export function paypalFromEnv(env: EnvConfig): PayPalGateway {
  const { clientId, clientSecret, webhookId, mode, apiBase } = env.paypal;
  if (!clientId || !clientSecret) {
    throw new Error(`PayPal ${mode} credentials missing: set PAYPAL_${mode.toUpperCase()}_CLIENT_ID and PAYPAL_${mode.toUpperCase()}_CLIENT_SECRET`);
  }
  return new SdkGateway({ mode, clientId, clientSecret, webhookId, apiBase });
}

export function sellerFromEnv(env: EnvConfig): Seller {
  return { name: env.SELLER_NAME, taxId: env.SELLER_TAX_ID, address: env.SELLER_ADDRESS, email: env.SELLER_EMAIL, businessType: env.BUSINESS_TYPE, logo: env.SELLER_LOGO ?? null };
}

function storageFromEnv(env: EnvConfig): Storage {
  if (env.STORAGE === "s3") {
    if (!env.S3_BUCKET || !env.S3_ACCESS_KEY_ID || !env.S3_SECRET_ACCESS_KEY) throw new Error("STORAGE=s3 needs S3_BUCKET, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY");
    return new S3Storage(env.S3_BUCKET, { region: env.S3_REGION, endpoint: env.S3_ENDPOINT, accessKeyId: env.S3_ACCESS_KEY_ID, secretAccessKey: env.S3_SECRET_ACCESS_KEY, sse: env.S3_SSE });
  }
  return new LocalStorage(env.STORAGE_DIR);
}

function providerFromEnv(env: EnvConfig): ReceiptProvider {
  if (env.RECEIPT_PROVIDER === "morning") {
    if (!env.MORNING_API_KEY_ID || !env.MORNING_API_SECRET) throw new Error("RECEIPT_PROVIDER=morning needs MORNING_API_KEY_ID and MORNING_API_SECRET");
    return new MorningProvider({ apiUrl: env.MORNING_API_URL, keyId: env.MORNING_API_KEY_ID, secret: env.MORNING_API_SECRET, businessType: env.BUSINESS_TYPE });
  }
  return new InternalProvider();
}

export async function createContext(opts: { workers?: boolean } = {}): Promise<Context> {
  const env = loadEnv();
  if (!env.LICENSE_PRIVATE_KEY || !env.LICENSE_PUBLIC_KEY) {
    throw new Error("LICENSE_PRIVATE_KEY / LICENSE_PUBLIC_KEY missing: run `npm run setup` (or keys:generate) and add them to .env");
  }
  const tokens = await TokenService.fromPem(env.LICENSE_PRIVATE_KEY.replace(/\\n/g, "\n"), env.LICENSE_PUBLIC_KEY.replace(/\\n/g, "\n"));
  const database = openDatabase(env.DATABASE_URL, env.NODE_ENV === "production");
  await database.migrate();
  const paypal = paypalFromEnv(env);
  const log = { info: (o: object, m?: string) => console.info(m, o), warn: (o: object, m?: string) => console.warn(m, o), error: (o: object, m?: string) => console.error(m, o) };
  const mailer = mailerFromEnv(env);
  if (mailer instanceof ConsoleMailer) {
    // without a real sender, receipts would be marked "sent" while no customer gets them
    if (env.NODE_ENV === "production") throw new Error("Gmail is not connected: run `npm run gmail:authorize` (all email goes through the Gmail API)");
    console.warn("[mail] Gmail is not connected: receipts are only logged. Run `npm run gmail:authorize` to send through the Gmail API.");
  }
  const storage = storageFromEnv(env);
  // the link secret never leaves the server; without its own value, it is derived from the license private key
  const linkSecret = env.RECEIPT_LINK_SECRET ?? createHash("sha256").update(`receipt-links:${env.LICENSE_PRIVATE_KEY}`).digest("hex");
  const links = new LinkSigner(linkSecret, env.PUBLIC_URL);
  const queue = await startQueue(database.queue, { attempts: 5, delaySeconds: env.RECEIPT_RETRY_DELAY_SECONDS }, { onError: (e) => log.error({ err: e.message }, "queue error"), schedules: opts.workers !== false });
  const receipts = new ReceiptService({ db: database.db, tax: taxPolicy(env.BUSINESS_TYPE, env.VAT_RATE_PERCENT), provider: env.RECEIPT_PROVIDER });
  const receiptSettings = new ReceiptSettingsService({ db: database.db });
  const pipeline = new ReceiptPipeline({
    db: database.db,
    receipts,
    provider: providerFromEnv(env),
    storage,
    mailer,
    links,
    seller: sellerFromEnv(env),
    queue,
    accountUrl: `${env.PUBLIC_URL.replace(/\/$/, "")}/account`,
    adminEmail: env.ADMIN_EMAIL ?? null,
    copyTo: env.RECEIPT_COPY_TO ?? null,
    settings: receiptSettings,
    log,
  });
  if (opts.workers !== false) await pipeline.start();
  const keySecret = env.LICENSE_KEY_SECRET ?? createHash("sha256").update(`license-keys:${env.LICENSE_PRIVATE_KEY}`).digest("hex");
  const keys = new LicenseKeyService({ db: database.db, secret: keySecret });
  const billing = new BillingService({ db: database.db, paypal, tokens, receipts: pipeline, mailer, licenseTtlDays: env.LICENSE_TTL_DAYS, keys, log });
  return { env, database, paypal, tokens, billing, mailer, storage, links, queue, pipeline, keys, receiptSettings };
}
