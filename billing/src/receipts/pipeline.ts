/**
 * The automatic receipt flow:
 *
 *   verified PAYMENT.SALE.COMPLETED ─▶ receipt row (pending, numbered) ─▶ job receipt-generate
 *     ─▶ provider issues the PDF ─▶ stored ─▶ generated ─▶ job receipt-email ─▶ customer (+ cc) ─▶ sent
 *
 * PAYMENT.SALE.REFUNDED runs the same flow for a refund document. Every job retries with backoff;
 * when the last attempt fails the receipt becomes `failed` and the admin gets an alert. A sweep job
 * re-queues anything stuck and creates receipts for payments that somehow have none, so no payment
 * is ever left without a receipt. Repeated triggers never create a second receipt (ReceiptService).
 */
import { and, eq, inArray, isNull, lt, sql } from "drizzle-orm";
import type { Db } from "../db/client.js";
import { emailLogs, payments, receipts, subscriptions, users } from "../db/schema.js";
import { renderReceiptEmail } from "../email/templates/receipt-email.js";
import { adminAlertMail, type Mailer } from "../email/mailer.js";
import type { ReceiptView, Seller } from "../pdf/receipt-document.js";
import type { ReceiptProvider } from "../providers/types.js";
import { JOBS, type JobInfo, type JobQueue } from "../queue/queue.js";
import type { LinkSigner } from "../storage/signed-links.js";
import type { Storage } from "../storage/storage.js";
import { formatNumber } from "./numbering.js";
import type { ReceiptSettings, ReceiptSettingsService, SettingsPatch } from "./settings.js";
import { renderTemplate, type TemplateVars } from "./templates.js";
import { ReceiptService, type ReceiptRow } from "./service.js";
import { T, asLocale, date, documentTitle, money, monthYear } from "./texts.js";

export interface PipelineDeps {
  db: Db;
  receipts: ReceiptService;
  provider: ReceiptProvider;
  storage: Storage;
  mailer: Mailer;
  links: LinkSigner;
  seller: Seller;
  queue: JobQueue;
  /** "My subscription" page, linked from the email */
  accountUrl: string;
  /** who hears about receipts that failed for good */
  adminEmail?: string | null;
  /** gets a hidden copy (Bcc) of every receipt email: the business's own record */
  copyTo?: string | null;
  /** the admin panel's "Automatic sending" settings (src/receipts/settings.ts) */
  settings?: ReceiptSettingsService;
  /** how long the PDF link inside an email stays valid */
  emailLinkTtlSeconds?: number;
  log?: { info: (o: object, m?: string) => void; warn: (o: object, m?: string) => void; error: (o: object, m?: string) => void };
  now?: () => Date;
}

const STUCK_AFTER_MS = 10 * 60_000;
/** automatic retries of a failed receipt (when the setting is on): this many rounds, this far apart */
export const AUTO_RETRY = { rounds: 3, everyMs: 60 * 60_000 };
type Trigger = "auto" | "resend" | "admin" | "retry" | "test";
const AUTOMATIC: Trigger[] = ["auto", "retry"];

export class ReceiptPipeline {
  private readonly log: NonNullable<PipelineDeps["log"]>;
  private readonly now: () => Date;

  constructor(private readonly d: PipelineDeps) {
    this.log = d.log ?? { info: () => {}, warn: () => {}, error: () => {} };
    this.now = d.now ?? (() => new Date());
  }

  /** Registers the workers and the sweep (every 5 minutes). */
  async start(): Promise<void> {
    await this.d.queue.work(JOBS.generate, (data, info) => this.generate(String(data.receiptId), info));
    await this.d.queue.work(JOBS.email, (data, info) => this.email(String(data.logId), info));
    await this.d.queue.work(JOBS.sweep, async () => void (await this.sweep()));
    await this.d.queue.schedule(JOBS.sweep, "*/5 * * * *");
  }

  // ---- triggers (called by BillingService after a verified webhook) -------------------------

  async onPayment(paymentId: string): Promise<ReceiptRow> {
    const { receipt, created } = await this.d.receipts.forPayment(paymentId);
    if (created) await this.d.queue.send(JOBS.generate, { receiptId: receipt.id });
    return receipt;
  }

  async onRefund(paymentId: string, refundId: string, refundMinor: number): Promise<ReceiptRow> {
    const { receipt, created } = await this.d.receipts.forRefund(paymentId, refundId, refundMinor);
    if (created) await this.d.queue.send(JOBS.generate, { receiptId: receipt.id });
    return receipt;
  }

  /** "Send again" from the account page: a new email (logged separately) with the same document. */
  async resend(receiptId: string, userId: string): Promise<boolean> {
    const r = await this.d.receipts.get(receiptId);
    if (!r || r.userId !== userId) return false;
    if (r.status === "pending" || !r.pdfKey) {
      await this.d.queue.send(JOBS.generate, { receiptId: r.id }); // its email follows automatically
      return true;
    }
    await this.queueEmail(r, "resend");
    return true;
  }

  /**
   * From the admin panel: email this receipt again, to the customer (and their accountant copy) or to one
   * address the admin typed (already validated). "not_ready" while the PDF doesn't exist yet: retry() first.
   */
  async adminSend(receiptId: string, to?: string): Promise<"queued" | "not_found" | "not_ready"> {
    const r = await this.d.receipts.get(receiptId);
    if (!r) return "not_found";
    if (!r.pdfKey || r.status === "pending") return "not_ready";
    await this.queueEmail(r, "admin", to);
    return "queued";
  }

  /** From the admin panel: a receipt that failed (PDF or email, after all automatic retries) gets another go. */
  async retry(receiptId: string): Promise<"queued" | "not_found" | "not_failed"> {
    return this.retryOne(receiptId);
  }

  private async retryOne(receiptId: string): Promise<"queued" | "not_found" | "not_failed"> {
    const r = await this.d.receipts.get(receiptId);
    if (!r) return "not_found";
    if (r.status !== "failed") return "not_failed";
    if (!r.pdfKey) {
      await this.d.receipts.setStatus(r.id, "pending", { lastError: null });
      await this.d.queue.send(JOBS.generate, { receiptId: r.id }); // its email follows automatically
    } else {
      await this.d.receipts.setStatus(r.id, "generated", { lastError: null });
      await this.queueEmail(r, "retry");
    }
    return "queued";
  }

  /** "Automatic sending" settings, or the defaults when this pipeline runs without them. */
  async config(): Promise<ReceiptSettings> {
    return this.d.settings ? this.d.settings.get()
      : { autoSend: true, autoRetry: false, copyTo: null, subjectHe: null, subjectEn: null, introHe: null, introEn: null, updatedAt: new Date(0), updatedBy: null };
  }

  /**
   * Failed receipts get another round (PDF or email) once an hour, up to AUTO_RETRY.rounds times, when the
   * "retry automatically" setting is on. A cause that lasts (a wrong address) still ends as failed for the admin.
   */
  private async autoRetry(): Promise<number> {
    if (!(await this.config()).autoRetry) return 0;
    const failed = await this.d.db.select().from(receipts).where(eq(receipts.status, "failed"));
    let n = 0;
    for (const r of failed) {
      const logs = await this.d.db.select().from(emailLogs).where(eq(emailLogs.receiptId, r.id));
      const emailRounds = logs.filter((l) => l.trigger === "retry").length;
      const pdfRounds = r.pdfKey ? 0 : Math.max(0, Math.floor(r.attempts / 5) - 1);
      const rounds = r.pdfKey ? emailRounds : pdfRounds;
      if (rounds >= AUTO_RETRY.rounds) continue;
      const last = Math.max(r.issuedAt.getTime() + rounds * AUTO_RETRY.everyMs, ...logs.map((l) => l.updatedAt.getTime()));
      if (this.now().getTime() - last < AUTO_RETRY.everyMs) continue;
      if ((await this.retryOne(r.id)) === "queued") n++;
    }
    return n;
  }

  /**
   * "Send me a test" in the admin panel: the newest receipt, worded with `draft` (unsaved settings), to `to`
   * only (no copy), subject marked as a test. Logged like any email.
   */
  async sendTest(to: string, draft: SettingsPatch = {}): Promise<"queued" | "no_receipts"> {
    const [r] = await this.d.db.select().from(receipts).where(and(eq(receipts.kind, "receipt"), sql`${receipts.pdfKey} is not null`)).orderBy(sql`${receipts.issuedAt} desc`).limit(1);
    if (!r) return "no_receipts";
    this.testDrafts.set(r.id, draft);
    await this.queueEmail(r, "test", to);
    return "queued";
  }
  private readonly testDrafts = new Map<string, SettingsPatch>();

  /** Subject and opening line with the current settings (or a draft): for the panel's preview and every email. */
  async preview(draft: SettingsPatch = {}, locale: "he" | "en" = "he"): Promise<{ subject: string; intro: string }> {
    const [r] = await this.d.db.select().from(receipts).where(eq(receipts.kind, "receipt")).orderBy(sql`${receipts.issuedAt} desc`).limit(1);
    const settings = { ...(await this.config()), ...draft };
    const vars: TemplateVars = r ? this.vars(r) : { name: "דנה כהן", number: "000123", amount: "₪29.00", month: monthYear(this.now(), locale), plan: T[locale].plan.monthly, business: this.d.seller.name };
    const t = T[locale];
    const subjectT = locale === "he" ? settings.subjectHe : settings.subjectEn;
    const introT = locale === "he" ? settings.introHe : settings.introEn;
    return {
      subject: subjectT ? renderTemplate("subject", subjectT, vars) : t.subject(vars.month),
      intro: introT ? renderTemplate("intro", introT, vars) : t.thanks,
    };
  }

  private vars(r: ReceiptRow): TemplateVars {
    const locale = asLocale(r.locale);
    return {
      name: r.customerName ?? "", number: r.providerDocumentNumber ?? formatNumber(r.number), amount: money(r.amountMinor, r.currency),
      month: monthYear(r.issuedAt, locale), plan: T[locale].plan[r.planInterval], business: this.d.seller.name,
    };
  }

  // ---- workers ----------------------------------------------------------------------------------

  async generate(receiptId: string, info: JobInfo): Promise<void> {
    const r = await this.d.receipts.get(receiptId);
    if (!r) return;
    if (r.pdfKey && r.status !== "failed") {
      // already generated (a duplicate job): make sure its email exists, never issue twice
      if (r.status === "generated" && (await this.config()).autoSend && !(await this.hasEmail(r.id, AUTOMATIC))) await this.queueEmail(r, "auto");
      return;
    }
    await this.d.db.update(receipts).set({ attempts: sql`${receipts.attempts} + 1` }).where(eq(receipts.id, r.id));
    try {
      const view = await this.view(r);
      const doc = await this.d.provider.issue({ receiptId: r.id, view, seller: this.d.seller, existingDocumentId: r.providerDocumentId });
      // record the provider's document id first: a retry must reuse it, not issue a second legal document
      if (doc.documentId && doc.documentId !== r.providerDocumentId) {
        await this.d.receipts.setStatus(r.id, r.status, { providerDocumentId: doc.documentId, providerDocumentNumber: doc.documentNumber });
      }
      const key = `receipts/${r.issuedAt.getUTCFullYear()}/${r.kind}-${formatNumber(r.number)}-${r.id}.pdf`;
      await this.d.storage.put(key, doc.pdf, "application/pdf");
      await this.d.receipts.setStatus(r.id, "generated", { pdfKey: key, providerDocumentId: doc.documentId, providerDocumentNumber: doc.documentNumber, lastError: null });
      this.log.info({ receipt: r.id, kind: r.kind, number: r.number }, "receipt generated");
      if ((await this.config()).autoSend) await this.queueEmail({ ...r, pdfKey: key }, "auto");
      else this.log.info({ receipt: r.id }, "automatic sending is off: the receipt waits for a manual send");
    } catch (e) {
      await this.fail(r, e, info, "generate");
    }
  }

  async email(logId: string, info: JobInfo): Promise<void> {
    const [entry] = await this.d.db.select().from(emailLogs).where(eq(emailLogs.id, logId));
    if (!entry || entry.status === "sent") return;
    const r = await this.d.receipts.get(entry.receiptId);
    if (!r?.pdfKey) return;
    await this.d.db.update(emailLogs).set({ attempts: sql`${emailLogs.attempts} + 1`, updatedAt: this.now() }).where(eq(emailLogs.id, entry.id));
    try {
      const pdf = await this.d.storage.get(r.pdfKey);
      if (!pdf) throw new Error(`stored PDF missing: ${r.pdfKey}`);
      const [to, ...cc] = entry.toAddresses.split(",");
      const settings = await this.config();
      const draft = entry.trigger === "test" ? this.testDrafts.get(r.id) : undefined;
      const mail = await this.composeEmail(r, draft ? { ...settings, ...draft } : settings);
      if (entry.trigger === "test") {
        mail.subject = `[בדיקה / test] ${mail.subject}`;
        this.testDrafts.delete(r.id);
      }
      const copy = entry.trigger === "test" ? undefined : (settings.copyTo ?? this.d.copyTo)?.trim().toLowerCase();
      const bcc = copy && copy !== to && !cc.includes(copy) ? [copy] : [];
      const result = await this.d.mailer.send({ to: to!, cc, bcc, ...mail, attachments: [{ filename: this.fileName(r), content: pdf }] });
      const now = this.now();
      await this.d.db.update(emailLogs).set({ status: "sent", providerMessageId: result?.id ?? null, error: null, updatedAt: now }).where(eq(emailLogs.id, entry.id));
      if (r.status !== "sent") await this.d.receipts.setStatus(r.id, "sent", { lastError: null });
      this.log.info({ receipt: r.id, trigger: entry.trigger }, "receipt emailed");
    } catch (e) {
      const msg = (e as Error).message.slice(0, 500);
      await this.d.db.update(emailLogs).set({ error: msg, status: info.isLastAttempt ? "failed" : "queued", updatedAt: this.now() }).where(eq(emailLogs.id, entry.id));
      if (AUTOMATIC.includes(entry.trigger as Trigger)) await this.fail(r, e, info, "email");
      else throw e;
    }
  }

  /**
   * Safety net (every 5 minutes): payments with no receipt get one; receipts stuck in pending or
   * generated (a crash between steps, a lost job) are queued again. Failed ones are left for the admin.
   */
  async sweep(): Promise<{ created: number; requeued: number }> {
    const orphans = await this.d.db
      .select({ id: payments.id })
      .from(payments)
      .leftJoin(receipts, eq(receipts.paypalTransactionId, payments.paypalSaleId))
      .where(isNull(receipts.id));
    let created = 0;
    for (const p of orphans) {
      try {
        await this.onPayment(p.id);
        created++;
      } catch (e) {
        this.log.error({ payment: p.id, err: (e as Error).message }, "sweep could not create a receipt");
      }
    }
    const cutoff = new Date(this.now().getTime() - STUCK_AFTER_MS);
    const stuck = await this.d.db
      .select()
      .from(receipts)
      .where(and(inArray(receipts.status, ["pending", "generated"]), lt(receipts.issuedAt, cutoff)));
    for (const r of stuck) {
      if (r.status === "pending") await this.d.queue.send(JOBS.generate, { receiptId: r.id });
      else if (!(await this.hasEmail(r.id, AUTOMATIC, ["queued", "sent"]))) {
        if ((await this.config()).autoSend) await this.queueEmail(r, "auto"); // off: it waits for a manual send
      }
      else {
        // only an email nobody touched for a while: one still inside its retry backoff is left alone
        const [pendingLog] = await this.d.db
          .select()
          .from(emailLogs)
          .where(and(eq(emailLogs.receiptId, r.id), eq(emailLogs.status, "queued"), lt(emailLogs.updatedAt, cutoff)));
        if (pendingLog) await this.d.queue.send(JOBS.email, { logId: pendingLog.id });
      }
    }
    const retried = await this.autoRetry();
    if (created || stuck.length || retried) this.log.warn({ created, requeued: stuck.length, retried }, "receipt sweep fixed things");
    return { created, requeued: stuck.length };
  }

  // ---- helpers -------------------------------------------------------------------------------

  private async queueEmail(r: ReceiptRow, trigger: Trigger, only?: string) {
    const [user] = await this.d.db.select().from(users).where(eq(users.id, r.userId));
    const to = only ?? [r.customerEmail, ...(user?.receiptCcEmail ? [user.receiptCcEmail] : [])].join(",");
    const [entry] = await this.d.db.insert(emailLogs).values({ receiptId: r.id, trigger, toAddresses: to }).returning();
    await this.d.queue.send(JOBS.email, { logId: entry!.id });
  }

  private async hasEmail(receiptId: string, triggers: Trigger[], statuses: ("queued" | "sent" | "failed")[] = ["queued", "sent", "failed"]) {
    const rows = await this.d.db
      .select({ id: emailLogs.id })
      .from(emailLogs)
      .where(and(eq(emailLogs.receiptId, receiptId), inArray(emailLogs.trigger, triggers), inArray(emailLogs.status, statuses)));
    return rows.length > 0;
  }

  private async fail(r: ReceiptRow, e: unknown, info: JobInfo, step: "generate" | "email"): Promise<void> {
    const msg = (e instanceof Error ? e.message : String(e)).slice(0, 500);
    this.log.warn({ receipt: r.id, step, attempt: info.attempt, err: msg }, "receipt step failed");
    if (!info.isLastAttempt) {
      await this.d.receipts.setStatus(r.id, r.status, { lastError: msg });
      throw e; // the queue retries with backoff
    }
    await this.d.receipts.setStatus(r.id, "failed", { lastError: `${step}: ${msg}` });
    this.log.error({ receipt: r.id, step, err: msg }, "receipt failed after all retries");
    if (this.d.adminEmail) {
      const details = `Receipt ${r.kind} #${r.number} (${r.id}) failed at "${step}" after ${info.attempt} attempts.\nCustomer: ${r.customerEmail}\nPayPal transaction: ${r.paypalTransactionId}\nError: ${msg}\n\nFix the cause, then set the receipt back to pending (or run the sweep) to retry.`;
      try {
        await this.d.mailer.send(adminAlertMail(this.d.adminEmail, `receipt ${r.number} failed`, details));
      } catch (alertError) {
        this.log.error({ err: (alertError as Error).message }, "admin alert email failed too");
      }
    }
  }

  private async view(r: ReceiptRow): Promise<ReceiptView> {
    let creditsNumber: number | null = null;
    let creditsProviderDocumentId: string | null = null;
    if (r.creditsReceiptId) {
      const original = await this.d.receipts.get(r.creditsReceiptId);
      creditsNumber = original?.number ?? null;
      creditsProviderDocumentId = original?.providerDocumentId ?? null;
    }
    return {
      kind: r.kind,
      number: r.number,
      providerDocumentNumber: r.providerDocumentNumber,
      issuedAt: r.issuedAt,
      locale: asLocale(r.locale),
      customerName: r.customerName,
      customerEmail: r.customerEmail,
      planInterval: r.planInterval,
      periodStart: r.periodStart,
      periodEnd: r.periodEnd,
      amountMinor: r.amountMinor,
      netMinor: r.netMinor,
      vatMinor: r.vatMinor,
      vatRateBp: r.vatRateBp,
      currency: r.currency,
      paypalTransactionId: r.paypalTransactionId,
      creditsNumber,
      creditsProviderDocumentId,
    };
  }

  private fileName(r: ReceiptRow) {
    return `kabala-${r.kind}-${r.providerDocumentNumber ?? formatNumber(r.number)}.pdf`;
  }

  private async composeEmail(r: ReceiptRow, settings: ReceiptSettings): Promise<{ subject: string; html: string; text: string }> {
    const locale = asLocale(r.locale);
    const t = T[locale];
    const seller = this.d.seller;
    let nextCharge: string | null = null;
    if (r.kind === "receipt") {
      const [row] = await this.d.db.select({ end: subscriptions.currentPeriodEnd, status: subscriptions.status }).from(payments).innerJoin(subscriptions, eq(subscriptions.id, payments.subscriptionId)).where(eq(payments.id, r.paymentId));
      if (row?.end && (row.status === "active" || row.status === "trial")) nextCharge = date(row.end, locale);
    }
    const title = documentTitle(r.kind, seller.businessType, locale);
    const month = monthYear(r.issuedAt, locale);
    const s = settings;
    // the panel's templates apply to receipts; refunds keep their own fixed wording
    const subjectT = r.kind === "receipt" ? (locale === "he" ? s.subjectHe : s.subjectEn) : null;
    const introT = r.kind === "receipt" ? (locale === "he" ? s.introHe : s.introEn) : null;
    const intro = introT ? renderTemplate("intro", introT, this.vars(r)) : null;
    const { html, text } = await renderReceiptEmail({
      intro,
      locale,
      kind: r.kind,
      customerName: r.customerName,
      documentTitle: title,
      documentNumber: r.providerDocumentNumber ?? formatNumber(r.number),
      issued: date(r.issuedAt, locale),
      plan: t.plan[r.planInterval],
      period: `${date(r.periodStart, locale)} – ${date(r.periodEnd, locale)}`,
      amount: money(r.amountMinor, r.currency),
      vat: r.vatRateBp > 0 ? { label: t.vat((r.vatRateBp / 100).toString()), value: money(r.vatMinor, r.currency) } : null,
      nextCharge,
      paypalTransactionId: r.paypalTransactionId,
      accountUrl: this.d.accountUrl,
      sellerName: seller.name,
      sellerFooter: [seller.name, `${t.businessType[seller.businessType]}${seller.taxId ? ` ${seller.taxId}` : ""}`, seller.address].filter(Boolean).join(" · "),
    });
    const pdfLink = this.d.links.url(r.id, this.d.emailLinkTtlSeconds ?? 30 * 86_400);
    const linkLine = locale === "he" ? `\n\nלהורדת הקבלה (הקישור תקף 30 יום): ${pdfLink}` : `\n\nDownload the receipt (link valid for 30 days): ${pdfLink}`;
    const subject = subjectT ? renderTemplate("subject", subjectT, this.vars(r)) : r.kind === "refund" ? t.subjectRefund(month) : t.subject(month);
    return { subject, html, text: text + linkLine };
  }
}
