/**
 * Receipt email settings, edited in the admin panel ("Automatic sending"): on/off, a hidden copy of every
 * receipt, the subject and opening line (mustache templates, src/receipts/templates.ts) and automatic retries.
 * One row in receipt_settings; read on every send, so a change applies to the very next receipt.
 */
import { eq } from "drizzle-orm";
import type { Db } from "../db/client.js";
import { receiptSettings } from "../db/schema.js";
import { isMailbox, normalizeMailbox } from "../email/address.js";
import { TemplateError, checkTemplate } from "./templates.js";

export type ReceiptSettings = Omit<typeof receiptSettings.$inferSelect, "id">;
export type SettingsPatch = Partial<Pick<ReceiptSettings, "autoSend" | "copyTo" | "subjectHe" | "subjectEn" | "introHe" | "introEn" | "autoRetry">>;

const DEFAULTS: ReceiptSettings = {
  autoSend: true, copyTo: null, subjectHe: null, subjectEn: null, introHe: null, introEn: null, autoRetry: true, updatedAt: new Date(0), updatedBy: null,
};

/** Checks a patch before it is saved: templates must parse, the copy address must be one plain mailbox. */
export function checkPatch(p: SettingsPatch): SettingsPatch {
  const out: SettingsPatch = { ...p };
  for (const [key, field] of [["subjectHe", "subject"], ["subjectEn", "subject"], ["introHe", "intro"], ["introEn", "intro"]] as const) {
    const v = out[key];
    if (typeof v === "string") {
      const t = v.trim();
      out[key] = t || null;
      if (t) checkTemplate(field, t);
    }
  }
  if (typeof out.copyTo === "string") {
    const c = out.copyTo.trim();
    if (c && !isMailbox(c)) throw new TemplateError("copyTo", "כתובת המייל לעותק לא תקינה");
    out.copyTo = c ? normalizeMailbox(c) : null;
  }
  return out;
}

export class ReceiptSettingsService {
  constructor(private readonly d: { db: Db; now?: () => Date }) {}

  async get(): Promise<ReceiptSettings> {
    const [row] = await this.d.db.select().from(receiptSettings).where(eq(receiptSettings.id, 1));
    if (!row) return DEFAULTS;
    const { id: _id, ...rest } = row;
    return rest;
  }

  async update(patch: SettingsPatch, actor: string): Promise<ReceiptSettings> {
    const values = { ...checkPatch(patch), updatedAt: this.d.now?.() ?? new Date(), updatedBy: actor };
    await this.d.db.insert(receiptSettings).values({ id: 1, ...values }).onConflictDoUpdate({ target: receiptSettings.id, set: values });
    return this.get();
  }
}
