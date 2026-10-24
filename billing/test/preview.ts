/**
 * Local UI preview without PayPal credentials: simulated PayPal (fake plans + a sample subscriber),
 * real pages and API. `npx tsx test/preview.ts` → prints links to the three pages.
 * The checkout page loads PayPal's public sandbox buttons ("sb" client id) for a visual check only.
 */
import { setup } from "./helpers.js";

// "sb" = PayPal's public sandbox client id: real PayPal buttons render, nothing can be charged
const t = await setup({ paypalClientId: "sb" });
const { userId } = await t.login("dana@gmail.com");
const plan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;
const sub = t.paypal.approve(plan, userId, { nextBillingTime: "2026-11-21T10:00:00Z" });
await t.billing.confirmCheckout(userId, sub.id);
await t.webhook("PAYMENT.SALE.COMPLETED", { id: "SALE-1", billing_agreement_id: sub.id, amount: { total: "29.00", currency: "ILS" }, create_time: "2026-09-21T10:00:00Z" });
await t.webhook("PAYMENT.SALE.COMPLETED", { id: "SALE-2", billing_agreement_id: sub.id, amount: { total: "29.00", currency: "ILS" }, create_time: "2026-10-21T10:00:00Z" });
const free = await t.login("new@gmail.com");

const link = async (uid: string, page: string) => `http://localhost:8080/${page}#t=${encodeURIComponent(await t.tokens.sign("checkout", { sub: uid }, 3600))}`;

await t.app.listen({ port: 8080, host: "127.0.0.1" });
console.log("pricing:  http://localhost:8080/pricing");
console.log(`checkout: ${await link(free.userId, "checkout")}`);
console.log(`account:  ${await link(userId, "account")}`);
