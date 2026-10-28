import { randomUUID } from "node:crypto";
import { SignJWT, exportSPKI, generateKeyPair } from "jose";
import { describe, expect, it } from "vitest";
import { AUDIENCE, ISSUER } from "../src/admin/admin-api.js";
import { licenseKeys } from "../src/db/schema.js";
import { CROCKFORD, generateKey, normalizeKey } from "../src/licenses/keys.js";
import { setup } from "./helpers.js";

async function pay(t: Awaited<ReturnType<typeof setup>>, email = "dana@gmail.com", saleId = "SALE-1") {
  const { userId, auth } = await t.login(email);
  const plan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;
  const sub = t.paypal.approve(plan, userId, { nextBillingTime: "2026-11-21T10:00:00Z" });
  await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
  const sale = { id: saleId, billing_agreement_id: sub.id, amount: { total: "29.00", currency: "ILS" }, state: "completed", create_time: "2026-10-21T10:00:00Z" };
  expect((await t.webhook("PAYMENT.SALE.COMPLETED", sale)).json()).toEqual({ outcome: "payment:recorded" });
  return { userId, auth, sub, sale };
}

const keyMails = (t: Awaited<ReturnType<typeof setup>>) => t.mailer.sent.filter((m) => m.subject.includes("מפתח הרישיון"));
const keyIn = (text: string) => /KBL(-[0-9A-Z]{5}){4}/.exec(text)![0];

describe("license keys", () => {
  it("look like KBL-XXXXX-XXXXX-XXXXX-XXXXX from the Crockford alphabet, and forgive typing", () => {
    const keys = new Set(Array.from({ length: 2000 }, generateKey));
    expect(keys.size).toBe(2000);
    for (const k of [...keys].slice(0, 50)) expect(k).toMatch(new RegExp(`^KBL(-[${CROCKFORD}]{5}){4}$`));
    const k = generateKey();
    const n = normalizeKey(k)!;
    expect(normalizeKey(k.toLowerCase().replace(/-/g, " "))).toBe(n);
    expect(normalizeKey(n.replace(/0/g, "O").replace(/1/g, "I"))).toBe(n);
    expect(normalizeKey("KBL-12345")).toBeNull();
    expect(normalizeKey("KBL-UUUUU-UUUUU-UUUUU-UUUUU")).toBeNull(); // U is not in the alphabet
  });

  it("is issued with the first payment, emailed once, and shown on the account page", async () => {
    const t = await setup();
    const { auth, sale } = await pay(t);
    expect(keyMails(t)).toHaveLength(1);
    const key = keyIn(keyMails(t)[0]!.text);
    const account = (await t.app.inject({ method: "GET", url: "/api/account", headers: auth })).json();
    expect(account.licenseKey).toMatchObject({ key, activations: 0 });
    // the same webhook again, and the next month's payment: same key, no new email
    await t.webhook("PAYMENT.SALE.COMPLETED", sale);
    await t.webhook("PAYMENT.SALE.COMPLETED", { ...sale, id: "SALE-2", create_time: "2026-11-21T10:00:00Z" });
    expect(keyMails(t)).toHaveLength(1);
    expect(await t.db.select().from(licenseKeys)).toHaveLength(1);
    await t.close();
  });

  it("is never stored in plain text", async () => {
    const t = await setup();
    await pay(t);
    const key = keyIn(keyMails(t)[0]!.text);
    const [row] = await t.db.select().from(licenseKeys);
    const stored = JSON.stringify(row);
    expect(stored).not.toContain(key);
    expect(stored).not.toContain(normalizeKey(key)!);
    expect(row!.hint).toBe(normalizeKey(key)!.slice(-5));
    expect(t.keys.decrypt(row!.keyEncrypted)).toBe(key);
    expect(t.keys.decrypt(row!.keyEncrypted.slice(0, -2) + "AA")).toBeNull(); // tampered ciphertext
    await t.close();
  });

  it("activates the desktop app like a sign-in, and counts activations", async () => {
    const t = await setup();
    const { userId } = await pay(t);
    const key = keyIn(keyMails(t)[0]!.text);
    const r = await t.app.inject({ method: "POST", url: "/api/license/activate", payload: { key: key.toLowerCase() } });
    expect(r.statusCode).toBe(200);
    expect((await t.tokens.verify(r.json().session, "session")).sub).toBe(userId);
    expect((await t.app.inject({ method: "POST", url: "/api/license/activate", payload: { key: generateKey() } })).statusCode).toBe(401);
    expect((await t.keys.forOwner(userId))!.activations).toBe(1);
    await t.close();
  });

  it("a new key from the account page replaces the old one", async () => {
    const t = await setup();
    const { auth } = await pay(t);
    const old = keyIn(keyMails(t)[0]!.text);
    const acc = (await t.app.inject({ method: "POST", url: "/api/account/license-key/rotate", headers: auth })).json();
    expect(acc.licenseKey.key).not.toBe(old);
    expect((await t.app.inject({ method: "POST", url: "/api/license/activate", payload: { key: old } })).statusCode).toBe(401);
    expect((await t.app.inject({ method: "POST", url: "/api/license/activate", payload: { key: acc.licenseKey.key } })).statusCode).toBe(200);
    await t.close();
  });

  it("free users get no key and can't make one", async () => {
    const t = await setup();
    const { auth } = await t.login("free@gmail.com");
    expect((await t.app.inject({ method: "GET", url: "/api/account", headers: auth })).json().licenseKey).toBeNull();
    expect((await t.app.inject({ method: "POST", url: "/api/account/license-key/rotate", headers: auth })).statusCode).toBe(409);
    await t.close();
  });

  it("the admin panel sees who has a key (hint only), and an admin can revoke it", async () => {
    const { publicKey, privateKey } = await generateKeyPair("EdDSA", { extractable: true });
    const t = await setup({ adminPublicKeyPem: await exportSPKI(publicKey) });
    await pay(t, "dana@gmail.com");
    const key = keyIn(keyMails(t)[0]!.text);
    const call = async (method: string, url: string, role = "admin") =>
      t.app.inject({ method: method as "GET", url, headers: { authorization: `Bearer ${await new SignJWT({ role, htm: method, htu: url.split("?")[0] })
        .setProtectedHeader({ alg: "EdDSA" }).setIssuer(ISSUER).setAudience(AUDIENCE).setSubject("ops@kabala.app")
        .setIssuedAt().setExpirationTime("60s").setJti(randomUUID()).sign(privateKey)}` } });
    const list = (await call("GET", "/api/admin/keys")).json();
    expect(list.keys).toEqual([expect.objectContaining({ email: "dana@gmail.com", status: "active", hint: `KBL-…-${normalizeKey(key)!.slice(-5)}` })]);
    expect(JSON.stringify(list)).not.toContain(key);
    const id = list.keys[0].id;
    expect((await call("POST", `/api/admin/keys/${id}/revoke`, "viewer")).statusCode).toBe(403);
    expect((await call("POST", `/api/admin/keys/${id}/revoke`)).statusCode).toBe(200);
    expect((await call("GET", "/api/admin/keys")).json().keys[0]).toMatchObject({ status: "revoked", revokedBy: "ops@kabala.app" });
    expect((await t.app.inject({ method: "POST", url: "/api/license/activate", payload: { key } })).statusCode).toBe(401);
    await t.close();
  });
});
