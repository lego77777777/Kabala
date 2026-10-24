/**
 * Sending through the Gmail API (users.messages.send) with OAuth: no SMTP password anywhere.
 * The account owner authorizes the `gmail.send` scope once (`npm run gmail:authorize`); the server
 * keeps only the refresh token, trades it for short-lived access tokens, and sends each message as
 * raw MIME built by Nodemailer's composer. Google signs it (SPF/DKIM of gmail.com), so it reaches
 * the inbox. Limits are Gmail's: about 500 recipients a day on a personal account.
 */
import MailComposer from "nodemailer/lib/mail-composer/index.js";
import type { Mail, Mailer } from "./mailer.js";

export const GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send";
const TOKEN_URL = "https://oauth2.googleapis.com/token";
const SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send";

export interface GmailApiOptions {
  clientId: string;
  clientSecret: string;
  refreshToken: string;
  /** "Kabala <you@gmail.com>": Gmail sends as the authorized account */
  from: string;
  replyTo?: string;
  fetchImpl?: typeof fetch;
}

export class GmailApiMailer implements Mailer {
  private access: { token: string; until: number } | null = null;
  private readonly fetch: typeof fetch;

  constructor(private readonly o: GmailApiOptions) {
    this.fetch = o.fetchImpl ?? fetch;
  }

  private async accessToken(): Promise<string> {
    if (this.access && this.access.until > Date.now()) return this.access.token;
    const r = await this.fetch(TOKEN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: this.o.clientId, client_secret: this.o.clientSecret, refresh_token: this.o.refreshToken, grant_type: "refresh_token" }),
    });
    const body = (await r.json().catch(() => ({}))) as { access_token?: string; expires_in?: number; error?: string };
    if (!r.ok || !body.access_token) {
      // invalid_grant = the owner revoked access or the token expired: run `npm run gmail:authorize` again
      throw new Error(`Gmail authorization failed (${r.status} ${body.error ?? ""})`.trim());
    }
    this.access = { token: body.access_token, until: Date.now() + ((body.expires_in ?? 3600) - 120) * 1000 };
    return body.access_token;
  }

  async send(mail: Mail): Promise<{ id?: string }> {
    const node = new MailComposer({
      from: this.o.from,
      replyTo: this.o.replyTo,
      to: mail.to,
      cc: mail.cc,
      // Gmail delivers to Bcc recipients and strips the header before sending
      bcc: mail.bcc,
      subject: mail.subject,
      text: mail.text,
      html: mail.html,
      attachments: mail.attachments?.map((a) => ({ filename: a.filename, content: a.content, contentType: "application/pdf" })),
    }).compile();
    // The composer drops Bcc by default (SMTP passes it in the envelope instead); the Gmail API reads
    // recipients from the headers, so keep it: Gmail delivers to it and strips it before sending.
    node.keepBcc = true;
    const raw = (await node.build()).toString("base64url");
    let r = await this.post(raw);
    if (r.status === 401) {
      this.access = null; // token revoked mid-flight: refresh once
      r = await this.post(raw);
    }
    const body = (await r.json().catch(() => ({}))) as { id?: string; error?: { message?: string; status?: string } };
    if (!r.ok) throw new Error(`Gmail send failed (${r.status} ${body.error?.status ?? ""}): ${body.error?.message ?? ""}`.trim());
    return { id: body.id };
  }

  private async post(raw: string) {
    return this.fetch(SEND_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${await this.accessToken()}`, "Content-Type": "application/json" },
      body: JSON.stringify({ raw }),
    });
  }
}
