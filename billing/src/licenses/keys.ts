/**
 * License keys: KBL-XXXXX-XXXXX-XXXXX-XXXXX, issued with a user's first payment, sent by email, shown in the
 * account page, and usable in the desktop app instead of signing in (POST /api/license/activate).
 *
 * Generation: nanoid (src/vendor/nanoid) over the Crockford base32 alphabet: 20 characters = 100 random bits,
 * no I/L/O/U so 0/O and 1/I can't be confused. Typed keys are forgiving: lower case, spaces, missing dashes,
 * O for 0 and I/L for 1 all work.
 *
 * Storage: never the key itself.
 *   key_hash       HMAC-SHA256 (a server secret, not a plain hash: a stolen table can't be checked offline)
 *   key_encrypted  AES-256-GCM, so the owner can see the key again on the account page
 *   hint           the last group, for the admin panel and support ("…-7QX2M")
 * Both keys are derived with HKDF from LICENSE_KEY_SECRET (or, without it, from the license signing key).
 */
import { createCipheriv, createDecipheriv, createHmac, hkdfSync, randomBytes } from "node:crypto";
import { and, count, desc, eq, ilike, sql } from "drizzle-orm";
import type { Db } from "../db/client.js";
import { licenseKeys, users } from "../db/schema.js";
import { customAlphabet } from "../vendor/nanoid/index.js";

export const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const random20 = customAlphabet(CROCKFORD, 20);
const PREFIX = "KBL";

export type KeyRow = typeof licenseKeys.$inferSelect;

export function generateKey(): string {
  const body = random20();
  return `${PREFIX}-${body.match(/.{5}/g)!.join("-")}`;
}

/** The 20 key characters, or null if the input can't be a Kabala key. */
export function normalizeKey(input: string): string | null {
  const s = input.toUpperCase().replace(/[\s-]/g, "").replace(/^KBL/, "").replace(/O/g, "0").replace(/[IL]/g, "1");
  return s.length === 20 && [...s].every((c) => CROCKFORD.includes(c)) ? s : null;
}

const format = (normalized: string) => `${PREFIX}-${normalized.match(/.{5}/g)!.join("-")}`;

export class LicenseKeyService {
  private readonly hashKey: Buffer;
  private readonly encKey: Buffer;
  private readonly now: () => Date;

  constructor(private readonly d: { db: Db; secret: string; now?: () => Date }) {
    if (d.secret.length < 32) throw new Error("license key secret must be at least 32 characters");
    const derive = (info: string) => Buffer.from(hkdfSync("sha256", d.secret, "", `kabala:${info}`, 32));
    this.hashKey = derive("license-key-hash");
    this.encKey = derive("license-key-encrypt");
    this.now = d.now ?? (() => new Date());
  }

  hash(normalized: string): string {
    return createHmac("sha256", this.hashKey).update(normalized).digest("base64url");
  }

  encrypt(key: string): string {
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.encKey, iv);
    const ct = Buffer.concat([cipher.update(key, "utf8"), cipher.final()]);
    return `v1.${Buffer.concat([iv, cipher.getAuthTag(), ct]).toString("base64url")}`;
  }

  decrypt(stored: string): string | null {
    try {
      const raw = Buffer.from(stored.slice(3), "base64url");
      const decipher = createDecipheriv("aes-256-gcm", this.encKey, raw.subarray(0, 12));
      decipher.setAuthTag(raw.subarray(12, 28));
      return Buffer.concat([decipher.update(raw.subarray(28)), decipher.final()]).toString("utf8");
    } catch {
      return null; // changed in the database, or encrypted with another secret
    }
  }

  private async create(userId: string, subscriptionId: string | null): Promise<string> {
    const key = generateKey();
    const n = normalizeKey(key)!;
    await this.d.db.insert(licenseKeys).values({
      userId, subscriptionId, keyHash: this.hash(n), keyEncrypted: this.encrypt(key), hint: n.slice(-5), createdAt: this.now(),
    });
    return key;
  }

  async active(userId: string): Promise<KeyRow | null> {
    const [row] = await this.d.db.select().from(licenseKeys).where(and(eq(licenseKeys.userId, userId), eq(licenseKeys.status, "active")));
    return row ?? null;
  }

  /** After a payment: a key for a user who has none. Returns it only when it was just created (to email it). */
  async issue(userId: string, subscriptionId: string | null): Promise<string | null> {
    if (await this.active(userId)) return null;
    try {
      return await this.create(userId, subscriptionId);
    } catch (e) {
      // the same payment delivered twice at once: the unique index keeps one active key, the other insert fails
      if (await this.active(userId)) return null;
      throw e;
    }
  }

  /** "New key" on the account page: the old key stops working. */
  async rotate(userId: string, actor = "user"): Promise<string> {
    const current = await this.active(userId);
    if (current) await this.revoke(current.id, actor);
    return this.create(userId, current?.subscriptionId ?? null);
  }

  async revoke(id: string, actor: string): Promise<boolean> {
    const r = await this.d.db.update(licenseKeys).set({ status: "revoked", revokedAt: this.now(), revokedBy: actor })
      .where(and(eq(licenseKeys.id, id), eq(licenseKeys.status, "active"))).returning({ id: licenseKeys.id });
    return r.length > 0;
  }

  /** A key typed in the desktop app → its user, or null. Counts the activation. */
  async activate(input: string): Promise<{ userId: string; keyId: string } | null> {
    const n = normalizeKey(input);
    if (!n) return null;
    const [row] = await this.d.db.update(licenseKeys)
      .set({ activations: sql`${licenseKeys.activations} + 1`, lastUsedAt: this.now() })
      .where(and(eq(licenseKeys.keyHash, this.hash(n)), eq(licenseKeys.status, "active")))
      .returning({ userId: licenseKeys.userId, keyId: licenseKeys.id });
    return row ?? null;
  }

  /** For the owner's account page: the full key (decrypted), or null. */
  async forOwner(userId: string): Promise<{ key: string; createdAt: string; activations: number } | null> {
    const row = await this.active(userId);
    const key = row && this.decrypt(row.keyEncrypted);
    return row && key ? { key: format(normalizeKey(key)!), createdAt: row.createdAt.toISOString(), activations: row.activations } : null;
  }

  /** For the admin panel: never the key, only its hint. */
  async adminList(opts: { q?: string; status?: "active" | "revoked"; page: number; size?: number }) {
    const size = opts.size ?? 25;
    const where = and(
      opts.status ? eq(licenseKeys.status, opts.status) : undefined,
      opts.q ? ilike(users.email, `%${opts.q.replace(/[\\%_]/g, (m) => `\\${m}`)}%`) : undefined,
    );
    const [{ total } = { total: 0 }] = await this.d.db.select({ total: count() }).from(licenseKeys).innerJoin(users, eq(users.id, licenseKeys.userId)).where(where);
    const rows = await this.d.db.select({ k: licenseKeys, email: users.email }).from(licenseKeys)
      .innerJoin(users, eq(users.id, licenseKeys.userId)).where(where)
      .orderBy(desc(licenseKeys.createdAt)).limit(size).offset((opts.page - 1) * size);
    return {
      total: Number(total), page: opts.page, pages: Math.max(1, Math.ceil(Number(total) / size)),
      keys: rows.map(({ k, email }) => ({
        id: k.id, email, hint: `${PREFIX}-…-${k.hint}`, status: k.status, activations: k.activations,
        lastUsedAt: k.lastUsedAt, createdAt: k.createdAt, revokedAt: k.revokedAt, revokedBy: k.revokedBy,
      })),
    };
  }
}
