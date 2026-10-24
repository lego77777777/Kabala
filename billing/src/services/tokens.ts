/**
 * Signed tokens (EdDSA / Ed25519, private key only on the server):
 *  - license:  what the desktop app needs to work offline for up to 7 days (plan + limits + exp)
 *  - session:  the desktop app's API credential (30 days)
 *  - checkout: a short-lived link the app opens in the browser for payment / account pages
 * The desktop app verifies license tokens with the PUBLIC key only.
 */
import { SignJWT, exportPKCS8, exportSPKI, generateKeyPair, importPKCS8, importSPKI, jwtVerify, type JWTPayload } from "jose";
import type { Limits, Tier } from "../config/pricing.config.js";

const ALG = "EdDSA";
const ISSUER = "kabala-billing";

export type Audience = "license" | "session" | "checkout";

export interface LicenseClaims {
  sub: string;
  email: string;
  plan: Tier;
  limits: Limits;
  /** paid-through date of the subscription (ISO), if any */
  paidUntil: string | null;
  status: string | null;
}

export class TokenService {
  private constructor(
    private readonly privateKey: CryptoKey,
    private readonly publicKey: CryptoKey,
    readonly publicKeyPem: string,
  ) {}

  static async fromPem(privatePem: string, publicPem: string): Promise<TokenService> {
    return new TokenService(await importPKCS8(privatePem, ALG), await importSPKI(publicPem, ALG), publicPem);
  }

  /** Ephemeral keys for tests and first local runs. */
  static async generate(): Promise<TokenService & { privateKeyPem: string }> {
    const { privateKey, publicKey } = await generateKeyPair(ALG, { crv: "Ed25519", extractable: true });
    const svc = new TokenService(privateKey, publicKey, await exportSPKI(publicKey));
    return Object.assign(svc, { privateKeyPem: await exportPKCS8(privateKey) });
  }

  async sign(aud: Audience, claims: JWTPayload & { sub: string }, ttlSeconds: number, now = new Date()): Promise<string> {
    const iat = Math.floor(now.getTime() / 1000);
    return new SignJWT({ ...claims })
      .setProtectedHeader({ alg: ALG, typ: "JWT" })
      .setIssuer(ISSUER)
      .setAudience(aud)
      .setSubject(claims.sub)
      .setIssuedAt(iat)
      .setExpirationTime(iat + ttlSeconds)
      .sign(this.privateKey);
  }

  async verify<T extends JWTPayload>(token: string, aud: Audience, now = new Date()): Promise<T & { sub: string }> {
    const { payload } = await jwtVerify(token, this.publicKey, { issuer: ISSUER, audience: aud, currentDate: now });
    if (!payload.sub) throw new Error("token without subject");
    return payload as T & { sub: string };
  }

  license(claims: LicenseClaims, ttlDays: number, now = new Date()): Promise<string> {
    return this.sign("license", { ...claims }, ttlDays * 86_400, now);
  }
}
