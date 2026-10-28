import { buildApp } from "./app.js";
import { createContext } from "./context.js";
import { googleVerifier } from "./services/google.js";

const ctx = await createContext();
const { env } = ctx;
if (!env.GOOGLE_CLIENT_ID) throw new Error("GOOGLE_CLIENT_ID missing (the desktop app's OAuth client id)");

const app = await buildApp({
  billing: ctx.billing,
  tokens: ctx.tokens,
  verifyGoogle: googleVerifier(env.GOOGLE_CLIENT_ID),
  paypalClientId: env.paypal.clientId ?? null,
  publicUrl: env.PUBLIC_URL,
  deepLink: env.APP_DEEP_LINK,
  production: env.NODE_ENV === "production",
  receipts: { storage: ctx.storage, links: ctx.links, resend: (id, uid) => ctx.pipeline.resend(id, uid) },
  keys: ctx.keys,
  admin: env.ADMIN_PANEL_PUBLIC_KEY ? { db: ctx.database.db, publicKeyPem: env.ADMIN_PANEL_PUBLIC_KEY, keys: ctx.keys, receipts: { pipeline: ctx.pipeline, storage: ctx.storage, settings: ctx.receiptSettings }, gmail: ctx.mailer } : undefined,
});

const close = async () => {
  await app.close();
  await ctx.queue.stop();
  await ctx.database.close();
  process.exit(0);
};
process.on("SIGINT", close);
process.on("SIGTERM", close);

await app.listen({ port: env.PORT, host: "0.0.0.0" });
app.log.info(`Kabala billing (${env.paypal.mode}) on ${env.PUBLIC_URL}`);
