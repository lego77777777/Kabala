/**
 * Outgoing email behind one interface, chosen by environment (see mailerFromEnv):
 *   GMAIL_REFRESH_TOKEN → GmailApiMailer (Gmail API + OAuth, no password; `npm run gmail:authorize`)
 *   SMTP_URL        → SmtpMailer (Nodemailer): Mailpit locally, Gmail / Amazon SES / Postmark in production
 *   RESEND_API_KEY  → ResendMailer
 *   neither         → ConsoleMailer (development: logs recipient and subject only, never payment details)
 */
import nodemailer, { type Transporter } from "nodemailer";
import { GmailApiMailer } from "./gmail-api.js";

export interface Mail {
  to: string;
  /** extra recipients who get the same message (e.g. the user's accountant) */
  cc?: string[];
  /** hidden copies (e.g. the business's own archive), not visible to the other recipients */
  bcc?: string[];
  subject: string;
  text: string;
  html: string;
  attachments?: { filename: string; content: Buffer }[];
}

export interface Mailer {
  /** resolves with the provider's message id when it gives one */
  send(mail: Mail): Promise<{ id?: string } | void>;
}

export class ConsoleMailer implements Mailer {
  readonly sent: Mail[] = [];
  async send(mail: Mail): Promise<{ id: string }> {
    this.sent.push(mail);
    console.info(`[mail] to=${mail.to}${mail.cc?.length ? ` cc=${mail.cc.length}` : ""} subject="${mail.subject}" attachments=${mail.attachments?.length ?? 0}`);
    return { id: `console-${this.sent.length}` };
  }
}

export class SmtpMailer implements Mailer {
  private readonly transport: Transporter;

  /** e.g. smtp://localhost:1025 (Mailpit), smtps://user:app-password@smtp.gmail.com:465 */
  constructor(smtpUrl: string, private readonly from: string, private readonly replyTo?: string) {
    // requireTLS: with smtp:// the connection must upgrade with STARTTLS or fail, never send receipts and the
    // password in clear (smtps:// is TLS from the start). Only a local test server (Mailpit) may skip it.
    const local = /^smtps?:\/\/([^@/]*@)?(localhost|127\.0\.0\.1)[:/]?/i.test(smtpUrl);
    this.transport = nodemailer.createTransport({ url: smtpUrl, requireTLS: !local });
  }

  async send(mail: Mail): Promise<{ id: string }> {
    const info = await this.transport.sendMail({
      from: this.from,
      replyTo: this.replyTo,
      to: mail.to,
      cc: mail.cc,
      bcc: mail.bcc,
      subject: mail.subject,
      text: mail.text,
      html: mail.html,
      attachments: mail.attachments?.map((a) => ({ filename: a.filename, content: a.content, contentType: "application/pdf" })),
    });
    return { id: info.messageId };
  }
}

export class ResendMailer implements Mailer {
  constructor(private readonly apiKey: string, private readonly from: string, private readonly fetchImpl: typeof fetch = fetch) {}
  async send(mail: Mail): Promise<{ id?: string }> {
    const r = await this.fetchImpl("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: `Bearer ${this.apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: this.from,
        to: [mail.to],
        cc: mail.cc,
        bcc: mail.bcc,
        subject: mail.subject,
        text: mail.text,
        html: mail.html,
        attachments: mail.attachments?.map((a) => ({ filename: a.filename, content: a.content.toString("base64") })),
      }),
    });
    if (!r.ok) throw new Error(`email failed (${r.status})`);
    const body = (await r.json().catch(() => ({}))) as { id?: string };
    return { id: body.id };
  }
}

export function mailerFromEnv(env: {
  SMTP_URL?: string;
  RESEND_API_KEY?: string;
  MAIL_FROM: string;
  MAIL_REPLY_TO?: string;
  GMAIL_CLIENT_ID?: string;
  GMAIL_CLIENT_SECRET?: string;
  GMAIL_REFRESH_TOKEN?: string;
}): Mailer {
  if (env.GMAIL_REFRESH_TOKEN && env.GMAIL_CLIENT_ID && env.GMAIL_CLIENT_SECRET) {
    return new GmailApiMailer({ clientId: env.GMAIL_CLIENT_ID, clientSecret: env.GMAIL_CLIENT_SECRET, refreshToken: env.GMAIL_REFRESH_TOKEN, from: env.MAIL_FROM, replyTo: env.MAIL_REPLY_TO });
  }
  if (env.SMTP_URL) return new SmtpMailer(env.SMTP_URL, env.MAIL_FROM, env.MAIL_REPLY_TO);
  if (env.RESEND_API_KEY) return new ResendMailer(env.RESEND_API_KEY, env.MAIL_FROM);
  return new ConsoleMailer();
}

export function adminAlertMail(to: string, subject: string, details: string): Mail {
  return { to, subject: `[Kabala billing] ${subject}`, text: details, html: `<pre style="font-family:monospace">${details.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!)}</pre>` };
}

export function licenseKeyMail(to: string, key: string): Mail {
  return {
    to,
    subject: "מפתח הרישיון שלך ל-Kabala Pro",
    text: `תודה שהצטרפת ל-Kabala Pro!\n\nמפתח הרישיון שלך: ${key}\n\nPro כבר פעיל בתוכנה שממנה שילמת, אין צורך להקליד כלום. המפתח נחוץ רק במחשב אחר: מנוי ← "יש לי מפתח רישיון". אפשר לראות אותו שוב, או להחליף אותו, בדף "המנוי שלי".\nשמור עליו: מי שיש לו את המפתח יכול להפעיל את המנוי שלך.\n\nYour Kabala Pro license key: ${key}\nPro is already on in the app you paid from; the key is only for another computer.`,
    html: `<div dir="rtl" style="font-family:Segoe UI,Arial,sans-serif"><h2>תודה שהצטרפת ל-Kabala Pro!</h2><p>מפתח הרישיון שלך:</p><p dir="ltr" style="font:700 20px/1.4 Consolas,monospace;letter-spacing:1px;background:#f1f2f7;padding:12px 16px;border-radius:10px;display:inline-block">${key}</p><p>Pro כבר פעיל בתוכנה שממנה שילמת, אין צורך להקליד כלום. המפתח נחוץ רק במחשב אחר: <b>מנוי ← "יש לי מפתח רישיון"</b>. אפשר לראות אותו שוב, או להחליף אותו, בדף "המנוי שלי".</p><p style="color:#6b6f80">שמור עליו: מי שיש לו את המפתח יכול להפעיל את המנוי שלך.</p></div>`,
  };
}

export function paymentFailedMail(to: string, graceUntil: Date): Mail {
  const d = graceUntil.toISOString().slice(0, 10);
  return {
    to,
    subject: "Kabala Pro – התשלום לא עבר",
    text: `לא הצלחנו לחייב את אמצעי התשלום שלך ב-PayPal. Kabala Pro ימשיך לעבוד עד ${d}. עדכן את אמצעי התשלום ב-PayPal כדי לא לאבד גישה.\n\nWe couldn't charge your PayPal payment method. Pro stays active until ${d}.`,
    html: `<div dir="rtl" style="font-family:Segoe UI,Arial,sans-serif"><h2>התשלום לא עבר</h2><p>לא הצלחנו לחייב את אמצעי התשלום שלך ב-PayPal. Kabala Pro ימשיך לעבוד עד <b>${d}</b>.</p><p>עדכן את אמצעי התשלום בחשבון ה-PayPal שלך כדי לא לאבד גישה. הקבלות שכבר סודרו נשארות שלך.</p></div>`,
  };
}
