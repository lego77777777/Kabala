import { randomUUID } from "node:crypto";
import { SignJWT, exportSPKI, generateKeyPair } from "jose";
import { describe, expect, it } from "vitest";
import { AUDIENCE, ISSUER, maskEmail } from "../src/admin/admin-api.js";
import { setup } from "./helpers.js";

async function panel() {
  const { publicKey, privateKey } = await generateKeyPair("EdDSA", { extractable: true });
  const t = await setup({ adminPublicKeyPem: await exportSPKI(publicKey) });
  const token = (method: string, path: string, role = "owner", extra: Record<string, unknown> = {}, key = privateKey) =>
    new SignJWT({ role, htm: method, htu: path, ...extra })
      .setProtectedHeader({ alg: "EdDSA" }).setIssuer(ISSUER).setAudience(AUDIENCE).setSubject("owner@kabala.app")
      .setIssuedAt().setExpirationTime("60s").setJti(randomUUID()).sign(key);
  const get = async (url: string, role = "owner") =>
    t.app.inject({ method: "GET", url, headers: { authorization: `Bearer ${await token("GET", url.split("?")[0]!, role)}` } });
  return { t, token, get, privateKey };
}

async function paidUser(t: Awaited<ReturnType<typeof setup>>, email: string) {
  const { userId } = await t.login(email);
  const plan = (await t.billing.planFor("monthly", "ILS")).paypalPlanId;
  const sub = t.paypal.approve(plan, userId, { nextBillingTime: "2026-11-21T10:00:00Z" });
  await t.webhook("BILLING.SUBSCRIPTION.ACTIVATED", { id: sub.id });
  await t.webhook("PAYMENT.SALE.COMPLETED", { id: `SALE-${randomUUID().slice(0, 8)}`, billing_agreement_id: sub.id, amount: { total: "29.00", currency: "ILS" }, state: "completed", create_time: new Date().toISOString() });
}

describe("admin panel API", () => {
  it("returns stats, subscribers and payments for a token signed by the panel", async () => {
    const { t, get } = await panel();
    await paidUser(t, "dana@gmail.com");
    await t.login("free@gmail.com");
    const stats = (await get("/api/admin/stats")).json();
    expect(stats).toMatchObject({ mode: "sandbox", users: 2, subscriptions: { active: 1 } });
    expect(stats.signups).toHaveLength(30);
    expect(stats.revenueThisMonth).toEqual([{ currency: "ILS", netMinor: 2900, payments: 1 }]);
    const subs = (await get("/api/admin/subscribers")).json();
    expect(subs.subscribers).toEqual([expect.objectContaining({ email: "dana@gmail.com", status: "active", interval: "monthly", currency: "ILS", amountMinor: 2900 })]);
    const pays = (await get("/api/admin/payments")).json();
    expect(pays.payments).toEqual([expect.objectContaining({ email: "dana@gmail.com", amountMinor: 2900, status: "completed" })]);
    expect((await get("/api/admin/subscribers?q=nobody")).json().total).toBe(0);
    await t.close();
  });

  it("masks emails and hides PayPal ids for a viewer, and ignores its search", async () => {
    const { t, get } = await panel();
    await paidUser(t, "dana.levi@gmail.com");
    const subs = (await get("/api/admin/subscribers?q=zzz", "viewer")).json();
    expect(subs.subscribers.map((s: { email: string; paypalSubscriptionId: string | null }) => [s.email, s.paypalSubscriptionId])).toEqual([["da***@gmail.com", null]]);
    expect((await get("/api/admin/payments", "viewer")).json().payments[0].email).toBe("da***@gmail.com");
    await t.close();
  });

  it("rejects missing, forged, replayed, expired and misdirected tokens", async () => {
    const { t, token, privateKey } = await panel();
    const call = (auth?: string, url = "/api/admin/stats") => t.app.inject({ method: "GET", url, headers: auth ? { authorization: `Bearer ${auth}` } : {} });
    expect((await call()).statusCode).toBe(401);
    const other = await generateKeyPair("EdDSA");
    expect((await call(await token("GET", "/api/admin/stats", "owner", {}, other.privateKey))).statusCode).toBe(401); // not the panel's key
    const once = await token("GET", "/api/admin/stats");
    expect((await call(once)).statusCode).toBe(200);
    expect((await call(once)).statusCode).toBe(401); // replay
    expect((await call(await token("GET", "/api/admin/payments"))).statusCode).toBe(401); // token for another path
    expect((await call(await token("GET", "/api/admin/stats", "superuser"))).statusCode).toBe(401); // unknown role
    const old = await new SignJWT({ role: "owner", htm: "GET", htu: "/api/admin/stats" }).setProtectedHeader({ alg: "EdDSA" })
      .setIssuer(ISSUER).setAudience(AUDIENCE).setSubject("owner@kabala.app").setIssuedAt(Math.floor(Date.now() / 1000) - 600)
      .setExpirationTime(Math.floor(Date.now() / 1000) - 500).setJti(randomUUID()).sign(privateKey); // right key, but expired
    expect((await call(old)).statusCode).toBe(401);
    await t.close();
  });

  it("is off when no panel key is configured", async () => {
    const t = await setup();
    expect((await t.app.inject({ method: "GET", url: "/api/admin/stats" })).statusCode).toBe(404);
    await t.close();
  });

  it("masks like the panel", () => {
    expect(maskEmail("dana.levi@gmail.com")).toBe("da***@gmail.com");
  });
});
