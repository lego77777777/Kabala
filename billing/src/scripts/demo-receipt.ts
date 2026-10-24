/**
 * npm run demo:receipt -- [email] [he|en] [monthly|yearly]
 *
 * Development only: records a fake PayPal payment for `email` (as a verified PAYMENT.SALE.COMPLETED
 * would) and runs the real receipt flow: queue → PDF → storage → email. With SMTP_URL pointing at
 * Mailpit (smtp://127.0.0.1:1025) the email shows up at http://localhost:8025. Refuses to run in
 * production or with live PayPal.
 */
import { randomUUID } from "node:crypto";
import { and, eq } from "drizzle-orm";
import { createContext } from "../context.js";
import { payments, plans, receipts, subscriptions, users } from "../db/schema.js";

const [email = "dana@example.com", locale = "he", interval = "monthly"] = process.argv.slice(2);
const ctx = await createContext();
if (ctx.env.NODE_ENV === "production" || ctx.env.PAYPAL_MODE === "live") throw new Error("demo:receipt is for development only");

try {
  const [plan] = await ctx.database.db.select().from(plans).where(and(eq(plans.mode, "sandbox"), eq(plans.status, "active"), eq(plans.interval, interval as "monthly" | "yearly")));
  if (!plan) throw new Error("no synced plan: run `npm run sync-pricing` first");
  await ctx.database.db.insert(users).values({ email, name: locale === "en" ? "Dana Cohen" : "דנה כהן", locale }).onConflictDoUpdate({ target: users.email, set: { locale } });
  const [user] = await ctx.database.db.select().from(users).where(eq(users.email, email));
  const [sub] = await ctx.database.db
    .insert(subscriptions)
    .values({ userId: user!.id, planId: plan.id, paypalSubscriptionId: `I-DEMO${randomUUID().slice(0, 8).toUpperCase()}`, status: "active", currentPeriodEnd: new Date(Date.now() + (interval === "yearly" ? 365 : 30) * 86_400_000) })
    .returning();
  const [payment] = await ctx.database.db
    .insert(payments)
    .values({ userId: user!.id, subscriptionId: sub!.id, paypalSaleId: `DEMO-${randomUUID().slice(0, 12).toUpperCase()}`, amountMinor: plan.amountMinor, currency: plan.currency, paidAt: new Date() })
    .returning();

  const started = Date.now();
  const receipt = await ctx.pipeline.onPayment(payment!.id);
  console.log(`receipt #${receipt.number} created for ${email} (${plan.amountMinor / 100} ${plan.currency}); waiting for the worker…`);
  let status = receipt.status;
  while (status !== "sent" && status !== "failed" && Date.now() - started < 60_000) {
    await new Promise((r) => setTimeout(r, 500));
    status = (await ctx.database.db.select().from(receipts).where(eq(receipts.id, receipt.id)))[0]!.status;
  }
  console.log(`status: ${status} after ${((Date.now() - started) / 1000).toFixed(1)}s`);
  if (ctx.env.SMTP_URL?.includes("1025")) console.log("open http://localhost:8025 to see the email");
} finally {
  await ctx.queue.stop();
  await ctx.database.close();
}
