/**
 * The Gmail account every email of this server is sent from, through the Gmail API only.
 *
 * Connected from the admin panel: when an owner connects Gmail on the panel's Gmail page, the panel server
 * (server/kabala_server/admin.py) sends the account here over the signed admin API (owner role, body-bound,
 * single-use token). The client secret and refresh token are stored encrypted (AES-256-GCM, key derived with HKDF).
 * Without a connected account, GMAIL_* from `npm run gmail:authorize` are used; without either, sending fails
 * (in production) or is only logged (development), so a receipt is never marked "sent" when nobody got it.
 */
import { createCipheriv, createDecipheriv, hkdfSync, randomBytes } from "node:crypto";
import { eq } from "drizzle-orm";
import type { Db } from "../db/client.js";
import { gmailAccount } from "../db/schema.js";
import { GmailApiMailer } from "./gmail-api.js";
import type { Mail, Mailer } from "./mailer.js";

export interface GmailEnv {
  MAIL_FROM: string;
  MAIL_REPLY_TO?: string;
  GMAIL_CLIENT_ID?: string;
  GMAIL_CLIENT_SECRET?: string;
  GMAIL_REFRESH_TOKEN?: string;
}

export interface GmailConnection {
  email: string;
  clientId: string;
  clientSecret: string;
  refreshToken: string;
}

export interface GmailStatus {
  connected: boolean;
  email: string | null;
  source: "panel" | "env" | null;
  connectedAt: string | null;
  connectedBy: string | null;
  lastError: string | null;
  lastSentAt: string | null;
}

/** "Kabala <x@y>" or "Kabala" → the display name shown next to the connected address. */
export function senderName(mailFrom: string): string {
  const m = /^\s*"?([^"<]*?)"?\s*<[^>]+>\s*$/.exec(mailFrom);
  if (m?.[1]) return m[1].trim();
  return mailFrom.includes("@") ? "Kabala" : mailFrom.trim() || "Kabala";
}

export class GmailAccount implements Mailer {
  private readonly key: Buffer;
  private current: { refreshToken: string; mailer: Mailer } | null = null;
  lastError: string | null = null;
  lastSentAt: Date | null = null;

  constructor(private readonly d: {
    db: Db; secret: string; env: GmailEnv;
    /** used when nothing is connected (development); without it, sending throws */
    fallback?: Mailer;
    fetchImpl?: typeof fetch; now?: () => Date;
  }) {
    this.key = Buffer.from(hkdfSync("sha256", d.secret, "", "kabala:gmail-account", 32));
  }

  private encrypt(text: string): string {
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.key, iv);
    const ct = Buffer.concat([cipher.update(text, "utf8"), cipher.final()]);
    return `v1.${Buffer.concat([iv, cipher.getAuthTag(), ct]).toString("base64url")}`;
  }

  private decrypt(stored: string): string | null {
    try {
      const raw = Buffer.from(stored.slice(3), "base64url");
      const decipher = createDecipheriv("aes-256-gcm", this.key, raw.subarray(0, 12));
      decipher.setAuthTag(raw.subarray(12, 28));
      return Buffer.concat([decipher.update(raw.subarray(28)), decipher.final()]).toString("utf8");
    } catch {
      return null;
    }
  }

  private async stored() {
    const [row] = await this.d.db.select().from(gmailAccount).where(eq(gmailAccount.id, 1));
    const secrets = row ? this.decrypt(row.secretsEncrypted) : null;
    if (!row || !secrets) return null;
    const { clientSecret, refreshToken } = JSON.parse(secrets) as { clientSecret: string; refreshToken: string };
    return { ...row, clientSecret, refreshToken };
  }

  /** The account to send from: the one connected in the panel, else the environment's. */
  private async account(): Promise<(GmailConnection & { source: "panel" | "env" }) | null> {
    const row = await this.stored();
    if (row) return { email: row.email, clientId: row.clientId, clientSecret: row.clientSecret, refreshToken: row.refreshToken, source: "panel" };
    const e = this.d.env;
    if (e.GMAIL_CLIENT_ID && e.GMAIL_CLIENT_SECRET && e.GMAIL_REFRESH_TOKEN) {
      const email = /<([^>]+)>/.exec(e.MAIL_FROM)?.[1] ?? (e.MAIL_FROM.includes("@") ? e.MAIL_FROM.trim() : "");
      return { email, clientId: e.GMAIL_CLIENT_ID, clientSecret: e.GMAIL_CLIENT_SECRET, refreshToken: e.GMAIL_REFRESH_TOKEN, source: "env" };
    }
    return null;
  }

  async ready(): Promise<boolean> {
    return (await this.account()) !== null;
  }

  async status(): Promise<GmailStatus> {
    const row = await this.stored();
    const acc = await this.account();
    return {
      connected: acc !== null, email: acc?.email || null, source: acc?.source ?? null,
      connectedAt: row?.connectedAt.toISOString() ?? null, connectedBy: row?.connectedBy ?? null,
      lastError: this.lastError, lastSentAt: this.lastSentAt?.toISOString() ?? null,
    };
  }

  async connect(c: GmailConnection, by: string): Promise<void> {
    const values = { email: c.email, clientId: c.clientId, secretsEncrypted: this.encrypt(JSON.stringify({ clientSecret: c.clientSecret, refreshToken: c.refreshToken })), connectedAt: this.d.now?.() ?? new Date(), connectedBy: by };
    await this.d.db.insert(gmailAccount).values({ id: 1, ...values }).onConflictDoUpdate({ target: gmailAccount.id, set: values });
    this.current = null;
    this.lastError = null;
  }

  /** Forgets the account here. The panel server revokes the token at Google (it is the same token). */
  async disconnect(): Promise<void> {
    await this.d.db.delete(gmailAccount).where(eq(gmailAccount.id, 1));
    this.current = null;
  }

  async send(mail: Mail): Promise<{ id?: string } | void> {
    const acc = await this.account();
    if (!acc) {
      if (this.d.fallback) return this.d.fallback.send(mail);
      this.lastError = "Gmail is not connected";
      throw new Error("Gmail is not connected (admin panel → Gmail, or `npm run gmail:authorize`)");
    }
    if (this.current?.refreshToken !== acc.refreshToken) {
      const from = acc.email ? `${senderName(this.d.env.MAIL_FROM)} <${acc.email}>` : this.d.env.MAIL_FROM;
      this.current = { refreshToken: acc.refreshToken, mailer: new GmailApiMailer({ clientId: acc.clientId, clientSecret: acc.clientSecret, refreshToken: acc.refreshToken, from, replyTo: this.d.env.MAIL_REPLY_TO, fetchImpl: this.d.fetchImpl }) };
    }
    try {
      const out = await this.current.mailer.send(mail);
      this.lastError = null;
      this.lastSentAt = this.d.now?.() ?? new Date();
      return out;
    } catch (e) {
      this.lastError = (e as Error).message.slice(0, 300);
      throw e;
    }
  }
}
