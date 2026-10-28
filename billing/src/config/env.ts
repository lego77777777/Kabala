/**
 * Environment configuration, validated at startup. Sandbox and Live use separate variables
 * (PAYPAL_SANDBOX_* / PAYPAL_LIVE_*), selected only by PAYPAL_MODE, so a sandbox key can never
 * be used against live or the other way round.
 */
import "dotenv/config";
import { z } from "zod";

const Env = z
  .object({
    NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
    PORT: z.coerce.number().int().default(8080),
    /** public https URL of this server (checkout page, PayPal return URLs) */
    PUBLIC_URL: z.string().url().default("http://localhost:8080"),
    /** postgres://… in production; "pglite:<dir>" or "pglite:memory" for local development and tests */
    DATABASE_URL: z.string().default("pglite:./.data/pglite"),

    PAYPAL_MODE: z.enum(["sandbox", "live"]).default("sandbox"),
    PAYPAL_SANDBOX_CLIENT_ID: z.string().optional(),
    PAYPAL_SANDBOX_CLIENT_SECRET: z.string().optional(),
    PAYPAL_SANDBOX_WEBHOOK_ID: z.string().optional(),
    PAYPAL_LIVE_CLIENT_ID: z.string().optional(),
    PAYPAL_LIVE_CLIENT_SECRET: z.string().optional(),
    PAYPAL_LIVE_WEBHOOK_ID: z.string().optional(),

    /** Ed25519 keys (PEM) for license tokens; generate with `npm run keys:generate` */
    LICENSE_PRIVATE_KEY: z.string().optional(),
    LICENSE_PUBLIC_KEY: z.string().optional(),
    LICENSE_TTL_DAYS: z.coerce.number().int().default(7),

    /** deep link the browser returns to after payment */
    APP_DEEP_LINK: z.string().default("kabala://payment-success"),
    /** Google OAuth client id: the desktop app proves the user's identity with a Google ID token */
    GOOGLE_CLIENT_ID: z.string().optional(),
    /** Ed25519 public key (PEM) of the admin panel: enables the read-only /api/admin/* API. The panel keeps the private key. */
    ADMIN_PANEL_PUBLIC_KEY: z.string().optional(),

    /** "exempt" (עוסק פטור: receipt, no VAT) or "licensed" (עוסק מורשה / company: tax invoice-receipt with VAT) */
    BUSINESS_TYPE: z.enum(["exempt", "licensed"]).default("exempt"),
    /** VAT percentage included in prices when BUSINESS_TYPE=licensed (Israel: 18 since 2025) */
    VAT_RATE_PERCENT: z.coerce.number().min(0).max(100).default(18),
    SELLER_NAME: z.string().default("Kabala"),
    /** ע.מ. / ח.פ. */
    SELLER_TAX_ID: z.string().default(""),
    SELLER_ADDRESS: z.string().default(""),
    SELLER_EMAIL: z.string().default(""),
    /** PNG/JPG shown on receipts (optional) */
    SELLER_LOGO: z.string().optional(),

    /** who issues the document: internal PDF, or Morning (Green Invoice), a certified Israeli service */
    RECEIPT_PROVIDER: z.enum(["internal", "morning"]).default("internal"),
    MORNING_API_KEY_ID: z.string().optional(),
    MORNING_API_SECRET: z.string().optional(),
    /** Morning's sandbox ("https://sandbox.d.greeninvoice.co.il/api/v1") or production ("https://api.greeninvoice.co.il/api/v1") */
    MORNING_API_URL: z.string().url().default("https://sandbox.d.greeninvoice.co.il/api/v1"),

    /** where PDFs are kept: local folder, or an S3-compatible bucket (Amazon S3, Cloudflare R2) */
    STORAGE: z.enum(["local", "s3"]).default("local"),
    STORAGE_DIR: z.string().default("./.data/files"),
    S3_BUCKET: z.string().optional(),
    S3_REGION: z.string().default("auto"),
    S3_ENDPOINT: z.string().url().optional(),
    S3_ACCESS_KEY_ID: z.string().optional(),
    S3_SECRET_ACCESS_KEY: z.string().optional(),
    /** encryption at rest for receipt PDFs: AES256 (default on AWS) or none (default with S3_ENDPOINT: R2 always encrypts) */
    S3_SSE: z.enum(["AES256", "none"]).optional(),
    /** HMAC key for expiring PDF links (32+ random characters); derived from the license key if empty */
    RECEIPT_LINK_SECRET: z.string().optional(),
    /** HMAC + AES key for license keys (32+ random characters); derived from the license signing key if empty */
    LICENSE_KEY_SECRET: z.string().optional(),

    /** email: SMTP_URL (Nodemailer: Mailpit, Gmail, SES, Postmark…) or RESEND_API_KEY; neither = log only */
    SMTP_URL: z.string().optional(),
    /** Gmail API sending (OAuth, gmail.send): written by `npm run gmail:authorize` */
    GMAIL_CLIENT_ID: z.string().optional(),
    GMAIL_CLIENT_SECRET: z.string().optional(),
    GMAIL_REFRESH_TOKEN: z.string().optional(),
    RESEND_API_KEY: z.string().optional(),
    MAIL_FROM: z.string().default("Kabala <billing@kabala.app>"),
    MAIL_REPLY_TO: z.string().optional(),
    /** gets an alert when a receipt fails after all retries */
    ADMIN_EMAIL: z.string().optional(),
    /** gets a hidden copy (Bcc) of every receipt sent to customers, e.g. the business's own inbox */
    RECEIPT_COPY_TO: z.string().email().optional(),
    /** first retry delay for receipt jobs; doubles each attempt (5 attempts) */
    RECEIPT_RETRY_DELAY_SECONDS: z.coerce.number().int().min(1).default(30),
  })
  .superRefine((env, ctx) => {
    if (env.NODE_ENV === "production" && !env.PUBLIC_URL.startsWith("https://")) {
      ctx.addIssue({ code: "custom", path: ["PUBLIC_URL"], message: "must be https in production" });
    }
    if (env.NODE_ENV === "production" && !env.SELLER_TAX_ID) {
      ctx.addIssue({ code: "custom", path: ["SELLER_TAX_ID"], message: "a receipt must show the business's tax id" });
    }
  });

export type EnvConfig = z.infer<typeof Env> & {
  paypal: { mode: "sandbox" | "live"; clientId?: string; clientSecret?: string; webhookId?: string; apiBase: string };
};

export function loadEnv(source: Record<string, string | undefined> = process.env): EnvConfig {
  const env = Env.parse(source);
  const live = env.PAYPAL_MODE === "live";
  return {
    ...env,
    paypal: {
      mode: env.PAYPAL_MODE,
      clientId: live ? env.PAYPAL_LIVE_CLIENT_ID : env.PAYPAL_SANDBOX_CLIENT_ID,
      clientSecret: live ? env.PAYPAL_LIVE_CLIENT_SECRET : env.PAYPAL_SANDBOX_CLIENT_SECRET,
      webhookId: live ? env.PAYPAL_LIVE_WEBHOOK_ID : env.PAYPAL_SANDBOX_WEBHOOK_ID,
      apiBase: live ? "https://api-m.paypal.com" : "https://api-m.sandbox.paypal.com",
    },
  };
}
