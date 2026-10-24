/**
 * npm run sync-pricing            → make PayPal match pricing.config.ts, then verify
 * npm run sync-pricing -- --check → only verify; exits 1 on any mismatch (use in CI / deploy)
 */
import { createContext } from "../context.js";
import { checkPricing, syncPricing } from "../services/sync-pricing.js";

const checkOnly = process.argv.includes("--check");
const ctx = await createContext({ workers: false });
let exitCode = 0;
try {
  if (!checkOnly) {
    const r = await syncPricing(ctx.database.db, ctx.paypal);
    console.log(`PayPal ${r.mode} · product ${r.productId}`);
    for (const c of r.created) console.log(`  + created ${c.interval} ${c.price} ${c.currency} → ${c.planId}`);
    for (const id of r.reactivated) console.log(`  ↺ reactivated ${id}`);
    for (const d of r.deactivated) console.log(`  − deactivated ${d.planId} (${d.price}); ${d.subscribersKept} existing subscriber(s) keep this price`);
    for (const id of r.unchanged) console.log(`  = unchanged ${id}`);
    if (r.toMigrate.length) console.log(`  ! grandfather=false: ${r.toMigrate.length} subscription(s) still on old prices need a revise: ${r.toMigrate.join(", ")}`);
  }
  const problems = await checkPricing(ctx.database.db, ctx.paypal);
  if (problems.length) {
    exitCode = 1;
    console.error("Pricing mismatch between pricing.config.ts and PayPal:");
    for (const p of problems) console.error(`  ✗ ${p.interval} ${p.currency}: expected ${p.expected} — ${p.problem}`);
  } else {
    console.log("✓ PayPal plans match pricing.config.ts");
  }
} finally {
  await ctx.queue.stop();
  await ctx.database.close();
}
process.exit(exitCode);
