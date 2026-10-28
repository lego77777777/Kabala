import { randomUUID } from "node:crypto";
import { SignJWT, exportSPKI, generateKeyPair } from "jose";
import { describe, expect, it } from "vitest";
import { AUDIENCE, ISSUER, bodyHash } from "../src/admin/admin-api.js";
import { gmailAccount } from "../src/db/schema.js";
import { GmailAccount, senderName } from "../src/email/gmail-account.js";
import { setup } from "./helpers.js";

/** Google's token endpoint and the Gmail send API; records what was sent. */
function fakeGoogle() {
  const sent: string[] = [];
  const fetchImpl = (async (url: string, init?: RequestInit) => {
    if (url.includes("oauth2.googleapis.com/token")) {
      const p = new URLSearchParams(String(init?.body));
      return new Response(JSON.stringify(p.get("refresh_token") === "1//revoked" ? { error: "invalid_grant" } : { access_token: "at", expires_in: 3600 }),
        { status: p.get("refresh_token") === "1//revoked" ? 400 : 200 });
    }
    sent.push(Buffer.from(JSON.parse(String(init?.body)).raw, "base64url").toString());
    return new Response(JSON.stringify({ id: `m${sent.length}` }), { status: 200 });
  }) as typeof fetch;
  return { sent, fetchImpl };
}

const ACCOUNT = { email: "billing@kabala.app", clientId: "123-web.apps.googleusercontent.com", clientSecret: "GOCSPX-secret", refreshToken: "1//panel-token" };

async function panel() {
  const google = fakeGoogle();
  const { publicKey, privateKey } = await generateKeyPair("EdDSA", { extractable: true });
  const t = await setup({ adminPublicKeyPem: await exportSPKI(publicKey), gmailFetch: google.fetchImpl });
  const call = async (method: "GET" | "POST", url: string, role: string, body?: unknown, signedBody: unknown = body) => {
    const token = await new SignJWT({ role, htm: method, htu: url, ...(signedBody === undefined ? {} : { bh: bodyHash(signedBody) }) })
      .setProtectedHeader({ alg: "EdDSA" }).setIssuer(ISSUER).setAudience(AUDIENCE).setSubject("owner@kabala.app")
      .setIssuedAt().setExpirationTime("60s").setJti(randomUUID()).sign(privateKey);
    return t.app.inject({ method, url, payload: body as object | undefined, headers: { authorization: `Bearer ${token}` } });
  };
  return { t, call, google };
}

describe("Gmail account connected from the admin panel", () => {
  it("is connected by an owner from the panel, stored encrypted, and used for every email", async () => {
    const { t, call, google } = await panel();
    expect((await call("GET", "/api/admin/gmail", "viewer")).json()).toMatchObject({ connected: false, source: null });
    const r = await call("POST", "/api/admin/gmail", "owner", ACCOUNT);
    expect(r.json()).toMatchObject({ connected: true, email: "billing@kabala.app", source: "panel", connectedBy: "owner@kabala.app" });
    const stored = JSON.stringify(await t.db.select().from(gmailAccount));
    expect(stored).not.toContain("1//panel-token");
    expect(stored).not.toContain("GOCSPX-secret");
    expect((await call("POST", "/api/admin/gmail/test", "admin")).json()).toEqual({ ok: true, to: "owner@kabala.app" });
    expect(google.sent.at(-1)).toMatch(/^From: Kabala <billing@kabala\.app>/m);
    expect(google.sent.at(-1)).toMatch(/^To: owner@kabala\.app/m);
    await t.close();
  });

  it("only an owner connects or disconnects, and the token is bound to its body", async () => {
    const { t, call } = await panel();
    expect((await call("POST", "/api/admin/gmail", "admin", ACCOUNT)).statusCode).toBe(403);
    expect((await call("POST", "/api/admin/gmail", "owner", { ...ACCOUNT, refreshToken: "1//attacker" }, ACCOUNT)).statusCode).toBe(401);
    expect((await call("POST", "/api/admin/gmail", "owner", { ...ACCOUNT, clientId: "evil" })).statusCode).toBe(400);
    expect((await call("POST", "/api/admin/gmail", "owner", { ...ACCOUNT, extra: 1 })).statusCode).toBe(400);
    await call("POST", "/api/admin/gmail", "owner", ACCOUNT);
    expect((await call("POST", "/api/admin/gmail/disconnect", "admin")).statusCode).toBe(403);
    expect((await call("POST", "/api/admin/gmail/test", "viewer")).statusCode).toBe(403);
    expect((await call("POST", "/api/admin/gmail/disconnect", "owner")).json()).toMatchObject({ connected: false });
    expect((await call("POST", "/api/admin/gmail/test", "admin")).json()).toMatchObject({ error: "not_connected" });
    await t.close();
  });

  it("a revoked token shows Google's error to the panel", async () => {
    const { t, call } = await panel();
    await call("POST", "/api/admin/gmail", "owner", { ...ACCOUNT, refreshToken: "1//revoked" });
    const r = await call("POST", "/api/admin/gmail/test", "admin");
    expect(r.statusCode).toBe(409);
    expect(r.json().message).toContain("invalid_grant");
    expect((await call("GET", "/api/admin/gmail", "viewer")).json().lastError).toContain("invalid_grant");
    await t.close();
  });
});

describe("GmailAccount without the panel", () => {
  it("falls back to GMAIL_* from the environment, else refuses to pretend an email was sent", async () => {
    const { t } = await panel();
    const google = fakeGoogle();
    const fromEnv = new GmailAccount({ db: t.db, secret: "s".repeat(40), fetchImpl: google.fetchImpl,
      env: { MAIL_FROM: "Kabala <me@gmail.com>", GMAIL_CLIENT_ID: "c.apps.googleusercontent.com", GMAIL_CLIENT_SECRET: "x", GMAIL_REFRESH_TOKEN: "1//env" } });
    expect(await fromEnv.status()).toMatchObject({ connected: true, source: "env", email: "me@gmail.com" });
    await fromEnv.send({ to: "a@example.com", subject: "s", text: "t", html: "<p>t</p>" });
    expect(google.sent[0]).toMatch(/^From: Kabala <me@gmail\.com>/m);
    const none = new GmailAccount({ db: t.db, secret: "s".repeat(40), env: { MAIL_FROM: "Kabala" } });
    await expect(none.send({ to: "a@example.com", subject: "s", text: "t", html: "" })).rejects.toThrow("Gmail is not connected");
    expect(senderName("Kabala Billing <x@y.z>")).toBe("Kabala Billing");
    expect(senderName("x@y.z")).toBe("Kabala");
    await t.close();
  });
});
