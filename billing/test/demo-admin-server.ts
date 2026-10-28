// Demo only (not run by the test suite): the billing app with sample PayPal data and the admin API on, for
// trying the admin panel end to end. Usage: npx tsx test/demo-admin-server.ts <panel_public.pem> <port>
import { randomUUID } from "node:crypto";
import { appendFileSync, readFileSync } from "node:fs";
import { licenseKeys } from "../src/db/schema.js";
import { setup } from "./helpers.js";

const [pemPath, port = "8791", mailLog] = process.argv.slice(2);
// Google faked: the Gmail account the panel connects "sends" by appending to mailLog (or the console)
const gmailFetch = (async (url: string, init?: RequestInit) => {
  if (url.includes("oauth2.googleapis.com/token")) return new Response(JSON.stringify({ access_token: "demo", expires_in: 3600 }), { status: 200 });
  const raw = Buffer.from(JSON.parse(String(init?.body)).raw, "base64url").toString();
  const line = `[billing gmail] ${raw.match(/^From: .*$/m)?.[0]} ${raw.match(/^To: .*$/m)?.[0]} ${raw.match(/^Subject: .*$/m)?.[0]}\n`;
  if (mailLog) appendFileSync(mailLog, line);
  else process.stdout.write(line);
  return new Response(JSON.stringify({ id: "demo" }), { status: 200 });
}) as typeof fetch;
const t = await setup({ adminPublicKeyPem: readFileSync(pemPath!, "utf8"), gmailFetch });
const names = ["dana.levi", "yossi.cohen", "noa.mizrahi", "avi.peretz", "michal.biton", "itai.friedman", "shira.azulay", "omer.katz", "tamar.shapiro", "roni.dahan", "maya.gabay", "lior.amar"];
const pick = <T>(a: T[]) => a[Math.floor(Math.random() * a.length)]!;
for (let i = 0; i < 26; i++) {
  const userId = (await t.billing.userByEmail(`${pick(names)}${i}@${pick(["gmail.com", "walla.co.il", "outlook.com"])}`)).id; // not through /api/session: its rate limit (20/min) would stop the seeding
  if (Math.random() < 0.35) continue; // free user
  const interval = Math.random() < 0.3 ? "yearly" : "monthly";
  const plan = (await t.billing.planFor(interval, "ILS")).paypalPlanId;
  const sub = t.paypal.approve(plan, userId, { nextBillingTime: new Date(Date.now() + (5 + i) * 86_400_000).toISOString() });
  await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
  const total = interval === "yearly" ? "279.00" : "29.00";
  await t.webhook("PAYMENT.SALE.COMPLETED", { id: `SALE-${randomUUID().slice(0, 8)}`, billing_agreement_id: sub.id, amount: { total, currency: "ILS" }, state: "completed", create_time: new Date().toISOString() });
  const r = Math.random();
  if (r < 0.12) await t.webhook("BILLING.SUBSCRIPTION.PAYMENT.FAILED", { id: sub.id });
  else if (r < 0.25) await t.webhook("BILLING.SUBSCRIPTION.CANCELLED", { id: sub.id });
}
// one buyer's "My subscription" page, to see their license key
const [first] = await t.db.select().from(licenseKeys).limit(1);
if (first) console.log(`account http://127.0.0.1:${port}/account#t=${await t.tokens.sign("checkout", { sub: first.userId }, 3600)}`);
await t.app.listen({ port: Number(port), host: "127.0.0.1" });
console.log(`billing demo on ${port}`);
