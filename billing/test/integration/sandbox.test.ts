/**
 * Runs against the real PayPal Sandbox. Skipped unless PAYPAL_SANDBOX_CLIENT_ID/SECRET are set.
 *   npm run test:sandbox
 * Uses a throw-away PGlite DB, so it creates its own sandbox product/plans (harmless in sandbox).
 */
import { describe, expect, it } from "vitest";
import { loadEnv } from "../../src/config/env.js";
import { openDatabase } from "../../src/db/client.js";
import { PayPalError } from "../../src/paypal/gateway.js";
import { SdkGateway } from "../../src/paypal/sdk-gateway.js";
import { checkPricing, syncPricing } from "../../src/services/sync-pricing.js";

const env = loadEnv({ ...process.env, PAYPAL_MODE: "sandbox" });
const configured = Boolean(env.paypal.clientId && env.paypal.clientSecret);

describe.skipIf(!configured)("PayPal Sandbox", () => {
  const paypal = () => new SdkGateway({ ...env.paypal, clientId: env.paypal.clientId!, clientSecret: env.paypal.clientSecret! });

  it("sync-pricing creates real plans whose prices match pricing.config.ts", async () => {
    const database = openDatabase("pglite:memory");
    await database.migrate();
    const gw = paypal();
    const report = await syncPricing(database.db, gw);
    expect(report.created.length + report.unchanged.length).toBe(2);
    expect(await checkPricing(database.db, gw)).toEqual([]);
    for (const c of report.created) {
      const plan = await gw.getPlan(c.planId);
      expect(plan).toMatchObject({ status: "ACTIVE", price: c.price, currency: c.currency, interval: c.interval });
    }
    await database.close();
  }, 60_000);

  it("rejects a webhook whose signature PayPal didn't produce", async () => {
    if (!env.paypal.webhookId) return;
    const ok = await paypal().verifyWebhookSignature(
      {
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-360caa42-fca2a594-a5cafa77",
        "paypal-transmission-id": "00000000-0000-0000-0000-000000000000",
        "paypal-transmission-sig": "forged",
        "paypal-transmission-time": new Date().toISOString(),
      },
      { id: "WH-FORGED", event_type: "BILLING.SUBSCRIPTION.ACTIVATED", resource: { id: "I-FORGED" } },
    );
    expect(ok).toBe(false);
  }, 30_000);

  it("unknown subscriptions are a clean 404", async () => {
    await expect(paypal().getSubscription("I-DOESNOTEXIST000")).rejects.toBeInstanceOf(PayPalError);
  }, 30_000);
});
