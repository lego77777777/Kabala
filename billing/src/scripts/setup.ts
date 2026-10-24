/**
 * npm run setup — one-time local setup. Safe to re-run: it never overwrites values that are already set.
 *   - creates .env from .env.example
 *   - fills LICENSE_PRIVATE_KEY / LICENSE_PUBLIC_KEY with a new Ed25519 pair (only if empty)
 *   - fills GOOGLE_CLIENT_ID from ../google_client.json
 *   - writes ../license_public_key.pem for the desktop app
 * Then it says which values still need to be filled in by hand.
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { TokenService } from "../services/tokens.js";

const root = resolve(import.meta.dirname, "../..");
const envPath = resolve(root, ".env");
const appDir = resolve(root, "..");

if (!existsSync(envPath)) writeFileSync(envPath, readFileSync(resolve(root, ".env.example"), "utf8"));
let env = readFileSync(envPath, "utf8");

const get = (key: string) => env.match(new RegExp(`^${key}=(.*)$`, "m"))?.[1]?.trim() ?? "";
const set = (key: string, value: string) => {
  env = env.match(new RegExp(`^${key}=`, "m"))
    ? env.replace(new RegExp(`^${key}=.*$`, "m"), () => `${key}=${value}`)
    : `${env.trimEnd()}\n${key}=${value}\n`;
};
const oneLine = (pem: string) => `"${pem.trim().replace(/\n/g, "\\n")}"`;

if (!get("LICENSE_PRIVATE_KEY")) {
  const keys = await TokenService.generate();
  set("LICENSE_PRIVATE_KEY", oneLine(keys.privateKeyPem));
  set("LICENSE_PUBLIC_KEY", oneLine(keys.publicKeyPem));
  console.log("✓ created license keys");
}
const publicPem = get("LICENSE_PUBLIC_KEY").replace(/^"|"$/g, "").replace(/\\n/g, "\n");
writeFileSync(resolve(appDir, "license_public_key.pem"), `${publicPem}\n`);
console.log("✓ wrote kabala-py/license_public_key.pem (bundled with the desktop app)");

const googleJson = resolve(appDir, "google_client.json");
if (!get("GOOGLE_CLIENT_ID") && existsSync(googleJson)) {
  const g = JSON.parse(readFileSync(googleJson, "utf8"));
  const id = (g.installed ?? g.web ?? {}).client_id;
  if (id) {
    set("GOOGLE_CLIENT_ID", id);
    console.log("✓ GOOGLE_CLIENT_ID from google_client.json");
  }
}

writeFileSync(envPath, env);

const mode = get("PAYPAL_MODE") === "live" ? "LIVE" : "SANDBOX";
const missing = [`PAYPAL_${mode}_CLIENT_ID`, `PAYPAL_${mode}_CLIENT_SECRET`, `PAYPAL_${mode}_WEBHOOK_ID`].filter((k) => !get(k));
console.log(missing.length ? `\nStill to fill in billing/.env:\n  ${missing.join("\n  ")}` : "\n✓ PayPal values are set");
