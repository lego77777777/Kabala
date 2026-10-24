/**
 * npm run reconcile — daily: compare every live subscription with PayPal, fix drift,
 * expire past-due subscriptions after the grace period and cancelled ones after their paid period.
 * Schedule it once a day (cron / Render cron job / GitHub Actions schedule).
 */
import { createContext } from "../context.js";

// jobs it queues (receipts for payments without one) are run by the server's workers
const ctx = await createContext({ workers: false });
try {
  const r = await ctx.billing.reconcile();
  console.log(`reconcile (${ctx.paypal.mode}): checked ${r.checked}, fixed ${r.fixed.length}, errors ${r.errors}`);
  for (const f of r.fixed) console.log(`  ${f.id}: ${f.from} → ${f.to}`);
  const s = await ctx.pipeline.sweep();
  console.log(`receipts: ${s.created} created for payments without one, ${s.requeued} re-queued`);
  process.exitCode = r.errors ? 1 : 0;
} finally {
  await ctx.queue.stop();
  await ctx.database.close();
}
