/**
 * Everything a receipt says, in Hebrew and English: shared by the PDF and the email so they never
 * disagree. Money and dates are formatted without bidi control characters (embedded PDF fonts don't
 * have glyphs for them); the layout's direction handles placement.
 */
import type { BusinessType } from "./amounts.js";

export type Locale = "he" | "en";
export const asLocale = (l: string | null | undefined): Locale => (l === "en" ? "en" : "he");

const SYMBOLS: Record<string, string> = { ILS: "₪", USD: "$", EUR: "€" };
const TZ = "Asia/Jerusalem";

export function money(minor: number, currency: string): string {
  const value = (minor / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${SYMBOLS[currency] ?? `${currency} `}${value}`;
}

export function date(d: Date, locale: Locale): string {
  if (locale === "he") return new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "2-digit", month: "2-digit", year: "numeric" }).format(d);
  return new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "numeric", month: "short", year: "numeric" }).format(d);
}

export function monthYear(d: Date, locale: Locale): string {
  return new Intl.DateTimeFormat(locale === "he" ? "he-IL" : "en-US", { timeZone: TZ, month: "long", year: "numeric" }).format(d);
}

export function documentTitle(kind: "receipt" | "refund", business: BusinessType, locale: Locale): string {
  const titles = {
    he: { receipt: { exempt: "קבלה", licensed: "חשבונית מס / קבלה" }, refund: { exempt: "קבלה על החזר כספי", licensed: "חשבונית מס זיכוי" } },
    en: { receipt: { exempt: "Receipt", licensed: "Tax Invoice / Receipt" }, refund: { exempt: "Refund Receipt", licensed: "Credit Note" } },
  } as const;
  return titles[locale][kind][business];
}

export const T = {
  he: {
    dir: "rtl" as const,
    businessType: { exempt: "עוסק פטור", licensed: "עוסק מורשה" },
    taxId: "ע.מ. / ח.פ.",
    number: "מספר",
    issued: "תאריך הפקה",
    billedTo: "לכבוד",
    description: "תיאור",
    period: "תקופת חיוב",
    amount: "סכום",
    subtotal: "סכום לפני מע\"מ",
    vat: (pct: string) => `מע"מ ${pct}%`,
    total: "סה\"כ שולם",
    totalRefund: "סה\"כ הוחזר",
    paymentMethod: "אמצעי תשלום",
    paypalTx: "מספר עסקה (PayPal)",
    credits: "זיכוי עבור קבלה מס'",
    // Hebrew first: a line's direction follows its first strong letter (bidi rule P2)
    plan: { monthly: "מנוי חודשי – Kabala Pro", yearly: "מנוי שנתי – Kabala Pro" },
    exemptNote: "עוסק פטור: המחיר אינו כולל מע\"מ.",
    footer: "המסמך הופק אוטומטית ונשלח לכתובת המייל של החשבון.",
    // email
    subject: (month: string) => `הקבלה שלך מ-Kabala – ${month}`,
    subjectRefund: (month: string) => `אישור החזר כספי מ-Kabala – ${month}`,
    preview: (amount: string) => `קיבלנו את התשלום שלך: ${amount}. הקבלה מצורפת.`,
    hello: (name: string | null) => (name ? `שלום ${name},` : "שלום,"),
    thanks: "תודה שבחרת ב-Kabala Pro! התשלום התקבל, והקבלה מצורפת למייל הזה כקובץ PDF.",
    refundThanks: "ההחזר הכספי בוצע. המסמך המעודכן מצורף למייל הזה כקובץ PDF.",
    summary: "סיכום התשלום",
    nextCharge: "החיוב הבא",
    viewSubscription: "צפה במנוי שלי",
    signoff: "צוות Kabala",
    whyEmail: "קיבלת את המייל כי שילמת על מנוי Kabala Pro בחשבון הזה.",
  },
  en: {
    dir: "ltr" as const,
    businessType: { exempt: "Exempt dealer (Israel)", licensed: "Licensed dealer (Israel)" },
    taxId: "Tax ID",
    number: "No.",
    issued: "Issued",
    billedTo: "Billed to",
    description: "Description",
    period: "Billing period",
    amount: "Amount",
    subtotal: "Subtotal",
    vat: (pct: string) => `VAT ${pct}%`,
    total: "Total paid",
    totalRefund: "Total refunded",
    paymentMethod: "Payment method",
    paypalTx: "PayPal transaction",
    credits: "Credits receipt no.",
    plan: { monthly: "Kabala Pro – monthly plan", yearly: "Kabala Pro – yearly plan" },
    exemptNote: "Exempt dealer: no VAT is charged.",
    footer: "This document was issued automatically and sent to the account's email address.",
    subject: (month: string) => `Your Kabala receipt – ${month}`,
    subjectRefund: (month: string) => `Your Kabala refund – ${month}`,
    preview: (amount: string) => `We received your payment of ${amount}. Your receipt is attached.`,
    hello: (name: string | null) => (name ? `Hi ${name},` : "Hi,"),
    thanks: "Thank you for choosing Kabala Pro! Your payment was received and the receipt is attached as a PDF.",
    refundThanks: "Your refund was processed. The updated document is attached as a PDF.",
    summary: "Payment summary",
    nextCharge: "Next charge",
    viewSubscription: "View my subscription",
    signoff: "The Kabala team",
    whyEmail: "You received this email because you paid for Kabala Pro on this account.",
  },
};
