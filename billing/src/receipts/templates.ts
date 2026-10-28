/**
 * The receipt email's subject and opening line, editable in the admin panel, as mustache templates
 * (src/vendor/mustache): "הקבלה שלך מ-Kabala – {{month}}".
 *
 * Only plain {{variable}} tags from VARIABLES are allowed. Sections, partials, unescaped {{{ }}} and changed
 * delimiters are refused with a message that names the tag, so a typo can't silently send a broken email.
 * Values go in as plain text: React escapes the HTML version itself, and line breaks are removed from the
 * subject (it becomes a mail header).
 */
import mustache from "../vendor/mustache/mustache.mjs";

export const VARIABLES = {
  name: "שם הלקוח",
  number: "מספר הקבלה",
  amount: "הסכום",
  month: "החודש",
  plan: "המסלול",
  business: "שם העסק",
} as const;
export type TemplateVars = Record<keyof typeof VARIABLES, string>;

export const LIMITS = { subject: 150, intro: 1000 } as const;

export class TemplateError extends Error {
  constructor(public readonly field: string, message: string) {
    super(message);
  }
}

/** Throws TemplateError when the template uses anything but known {{variables}}. */
export function checkTemplate(field: "subject" | "intro", template: string): void {
  if (template.length > LIMITS[field]) throw new TemplateError(field, `ארוך מדי (עד ${LIMITS[field]} תווים)`);
  let tokens: unknown[][];
  try {
    tokens = mustache.parse(template) as unknown as unknown[][];
  } catch (e) {
    throw new TemplateError(field, `תבנית לא תקינה: ${(e as Error).message}`);
  }
  for (const [kind, value] of tokens) {
    if (kind === "text") continue;
    if (kind !== "name") throw new TemplateError(field, `מותר רק {{משתנה}} פשוט (לא "${String(kind)}${String(value)}")`);
    if (!(String(value) in VARIABLES)) throw new TemplateError(field, `אין משתנה בשם {{${String(value)}}}. אפשר: ${Object.keys(VARIABLES).map((k) => `{{${k}}}`).join(" ")}`);
  }
}

export function renderTemplate(field: "subject" | "intro", template: string, vars: TemplateVars): string {
  checkTemplate(field, template);
  const out = mustache.render(template, vars, undefined, { escape: (v) => String(v) });
  return field === "subject" ? out.replace(/[\r\n]+/g, " ").trim() : out.trim();
}
