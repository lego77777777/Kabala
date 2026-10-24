// Shared helpers for the pricing / checkout / account pages. No prices live here: everything comes from /api/pricing.

export const MESSAGES = {
  network: "אין חיבור לשרת כרגע. בדוק את החיבור לאינטרנט ונסה שוב.",
  unauthorized: "הקישור פג תוקף. חזור לאפליקציה ולחץ שוב על \"שדרג ל-Pro\".",
  plan_not_synced: "התשלום לא זמין כרגע (התוכניות עוד לא הוגדרו). נסה שוב מאוחר יותר.",
  paypal_unavailable: "PayPal לא זמין כרגע. לא חויבת. נסה שוב בעוד כמה דקות.",
  subscription_mismatch: "לא הצלחנו לאמת את המנוי. לא הופעל Pro. פנה לתמיכה אם חויבת.",
  subscription_not_found: "המנוי לא נמצא ב-PayPal. לא חויבת.",
  not_active_yet: "PayPal עדיין מעבד את התשלום. זה לוקח בדרך כלל כמה שניות…",
  payment_declined: "התשלום נדחה. אפשר לנסות אמצעי תשלום אחר בחלון של PayPal.",
  paypal_load: "לא הצלחנו לטעון את PayPal. בדוק חוסם פרסומות או נסה דפדפן אחר.",
  cancelled_by_user: "התשלום בוטל. לא חויבת, ואפשר לנסות שוב בכל עת.",
  no_subscription: "אין מנוי פעיל לבטל.",
  rate_limited: "יותר מדי ניסיונות. המתן דקה ונסה שוב.",
  internal: "משהו השתבש אצלנו. לא חויבת. נסה שוב בעוד רגע.",
  invalid_email: "כתובת המייל לא תקינה. בדוק אותה ונסה שוב.",
  rate_limited: "שלחת הרבה בקשות בזמן קצר. נסה שוב בעוד כמה דקות.",
};

/** The short-lived link token arrives in the URL fragment; keep it for this tab and clean the URL. */
export function linkToken() {
  const m = location.hash.match(/t=([^&]+)/);
  if (m) {
    sessionStorage.setItem("kabala.t", decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname);
  }
  return sessionStorage.getItem("kabala.t");
}

export class ApiError extends Error {
  constructor(code, status) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

export async function api(path, { method = "GET", body, token } = {}) {
  let r;
  try {
    r = await fetch(path, {
      method,
      headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("network", 0);
  }
  if (r.status === 401) throw new ApiError("unauthorized", 401);
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw new ApiError(data?.error ?? "internal", r.status);
  return data;
}

export function message(err) {
  return MESSAGES[err?.code] ?? MESSAGES.internal;
}

const fmt = new Map();
export function money(amount, currency) {
  const key = currency;
  if (!fmt.has(key)) fmt.set(key, new Intl.NumberFormat("he-IL", { style: "currency", currency, maximumFractionDigits: 2, minimumFractionDigits: 0 }));
  return fmt.get(key).format(Number(amount));
}

export function date(iso) {
  return iso ? new Intl.DateTimeFormat("he-IL", { day: "numeric", month: "long", year: "numeric" }).format(new Date(iso)) : "—";
}

export function show(el, text, kind = "error") {
  el.textContent = text;
  el.className = `alert ${kind}`;
}

export function features(limits) {
  const out = [];
  out.push(limits.monthlyReceipts === null ? "קבלות ללא הגבלה" : `עד ${limits.monthlyReceipts} קבלות בחודש`);
  out.push(limits.mailAccounts === null ? "כמה חשבונות מייל" : limits.mailAccounts === 1 ? "חשבון מייל אחד" : `${limits.mailAccounts} חשבונות מייל`);
  out.push("סידור אוטומטי בתיקיות לפי חברה ותאריך");
  if (limits.backgroundScan) out.push("סריקה אוטומטית ברקע");
  if (limits.export) out.push("ייצוא ל-Excel ול-CSV לרואה החשבון");
  return out;
}
