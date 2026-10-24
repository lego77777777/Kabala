/**
 * npm run gmail:authorize [-- you@gmail.com]
 *
 * One-time setup for sending receipts through the Gmail API: opens Google's consent page in the
 * browser for the `gmail.send` scope (send only: it can't read, change or delete mail), receives the
 * code on a loopback address (PKCE + state), and writes the refresh token into billing/.env together
 * with MAIL_FROM. Uses the Google Cloud OAuth client the desktop app already has (../google_client.json).
 * Run it again whenever Google reports invalid_grant (access revoked).
 */
import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { resolve } from "node:path";
import { GMAIL_SEND_SCOPE } from "../email/gmail-api.js";

const root = resolve(import.meta.dirname, "../..");
const envPath = resolve(root, ".env");
const hint = process.argv[2];

// The dedicated "Kabala Receipts Mailer" client (GMAIL_CLIENT_ID/SECRET in .env) when set,
// otherwise the desktop app's client (../google_client.json).
const envText = existsSync(envPath) ? readFileSync(envPath, "utf8") : "";
const fromEnv = (key: string) => envText.match(new RegExp(`^${key}=(.+)$`, "m"))?.[1]?.trim().replace(/^"|"$/g, "") || null;
let clientId = fromEnv("GMAIL_CLIENT_ID");
let clientSecret = fromEnv("GMAIL_CLIENT_SECRET");
if (!clientId || !clientSecret) {
  const clientFile = resolve(root, "../google_client.json");
  if (!existsSync(clientFile)) throw new Error(`OAuth client not found: set GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET or add ${clientFile}`);
  ({ client_id: clientId, client_secret: clientSecret } = JSON.parse(readFileSync(clientFile, "utf8")).installed as { client_id: string; client_secret: string });
}

const verifier = randomBytes(48).toString("base64url");
const challenge = createHash("sha256").update(verifier).digest("base64url");
const state = randomBytes(24).toString("base64url");

const code = await new Promise<{ code: string; redirect: string }>((done, fail) => {
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    if (url.pathname !== "/") return void res.writeHead(404).end();
    const ok = url.searchParams.get("state") === state && url.searchParams.get("code");
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }).end(
      `<!doctype html><html dir="rtl"><body style="font-family:Segoe UI,sans-serif;text-align:center;padding:60px"><h2>${ok ? "✓ Kabala יכול לשלוח קבלות מהחשבון" : "החיבור לא הושלם"}</h2><p>אפשר לסגור את הלשונית.</p></body></html>`,
    );
    server.close();
    if (ok) done({ code: url.searchParams.get("code")!, redirect: redirectUri });
    else fail(new Error(`authorization failed: ${url.searchParams.get("error") ?? "state mismatch"}`));
  });
  let redirectUri = "";
  server.listen(0, "127.0.0.1", () => {
    const port = (server.address() as { port: number }).port;
    redirectUri = `http://127.0.0.1:${port}/`;
    const auth = new URL("https://accounts.google.com/o/oauth2/v2/auth");
    auth.search = new URLSearchParams({
      client_id: clientId,
      redirect_uri: redirectUri,
      response_type: "code",
      scope: `${GMAIL_SEND_SCOPE} openid email`,
      access_type: "offline",
      prompt: "consent",
      state,
      code_challenge: challenge,
      code_challenge_method: "S256",
      ...(hint ? { login_hint: hint } : {}),
    }).toString();
    console.log(`Opening Google in the browser… if it doesn't open, paste this link:\n${auth}\n`);
    if (process.platform === "win32") spawn("rundll32", ["url.dll,FileProtocolHandler", auth.toString()], { detached: true, stdio: "ignore" }).unref();
    else spawn(process.platform === "darwin" ? "open" : "xdg-open", [auth.toString()], { detached: true, stdio: "ignore" }).unref();
  });
  setTimeout(() => fail(new Error("timed out after 15 minutes: run `npm run gmail:authorize` again")), 15 * 60_000).unref();
});

const r = await fetch("https://oauth2.googleapis.com/token", {
  method: "POST",
  headers: { "Content-Type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ code: code.code, client_id: clientId, client_secret: clientSecret, redirect_uri: code.redirect, grant_type: "authorization_code", code_verifier: verifier }),
});
const tokens = (await r.json()) as { refresh_token?: string; id_token?: string; scope?: string; error?: string };
if (!r.ok || !tokens.refresh_token) throw new Error(`token exchange failed: ${tokens.error ?? r.status}`);
if (!tokens.scope?.includes(GMAIL_SEND_SCOPE)) throw new Error("the 'send email' permission was not granted; run again and tick it");
const email = tokens.id_token ? (JSON.parse(Buffer.from(tokens.id_token.split(".")[1]!, "base64url").toString()) as { email?: string }).email : hint;
if (!email) throw new Error("could not tell which account was authorized");

let env = existsSync(envPath) ? readFileSync(envPath, "utf8") : "";
const set = (key: string, value: string) => {
  const line = `${key}=${value}`;
  env = new RegExp(`^${key}=.*$`, "m").test(env) ? env.replace(new RegExp(`^${key}=.*$`, "m"), () => line) : `${env.trimEnd()}\n${line}\n`;
};
set("GMAIL_CLIENT_ID", clientId);
set("GMAIL_CLIENT_SECRET", clientSecret);
set("GMAIL_REFRESH_TOKEN", tokens.refresh_token);
set("MAIL_FROM", `"Kabala <${email}>"`);
writeFileSync(envPath, env);
console.log(`✓ receipts will be sent from ${email} through the Gmail API (saved in billing/.env). Restart the server.`);
