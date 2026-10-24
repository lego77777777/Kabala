"""
Local, offline receipt scoring and field extraction (Hebrew + English).

Runs on every candidate email before anything is sent to a model: obvious non-receipts are dropped
here for free, and when no model is configured these results are used as-is.
"""

from __future__ import annotations

import re

from kabala.detect.message import ParsedEmail
from kabala.detect.normalize import company_from_sender
from kabala.detect.query import KNOWN_SENDERS
from kabala.detect.result import Classification

# ---- vocabulary -------------------------------------------------------------

RECEIPT_WORDS = [
    "קבלה", "חשבונית", "חשבונית מס", "אישור תשלום", "פירוט חיוב", "תודה על הרכישה", "תודה על ההזמנה",
    "סכום לתשלום", "שולם", "חויב", "אמצעי תשלום", "receipt", "invoice", "order confirmation",
    "payment confirmation", "payment received", "amount paid", "you paid", "your payment", "billing",
    "order total", "thanks for your order", "thank you for your purchase", "subscription renewed",
    "charged", "tax invoice", "paid",
]

TOTAL_WORDS = [
    'סה"כ', "סה״כ", "סהכ", "סך הכל", "סכום לתשלום", "סכום כולל", "לתשלום", "שולם", "סכום החיוב", "חויב",
    "total", "grand total", "amount paid", "amount due", "order total", "you paid", "charged", "paid",
]

PROMO_WORDS = [
    "הנחה", "מבצע", "קופון", "השבוע בלבד", "הצטרף", "ניוזלטר", "עדכון מוצרים", "sale", "% off", "discount",
    "coupon", "deal", "newsletter", "webinar", "limited time", "new arrivals", "recommended for you",
    "black friday", "save up to", "promo code",
]

SHIPPING_ONLY_WORDS = ["נשלחה", "בדרך אליך", "המשלוח", "has shipped", "out for delivery", "tracking number", "delivered"]

CURRENCY_SYMBOLS = {
    "₪": "ILS", 'ש"ח': "ILS", "ש״ח": "ILS", "שח": "ILS", "nis": "ILS", "ils": "ILS",
    "$": "USD", "usd": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}

_CUR = r'(₪|ש"ח|ש״ח|שח|NIS|ILS|US\$|USD|\$|€|EUR|£|GBP)'
_NUM = r"(\d{1,3}(?:[,  ]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_AMOUNT_RE = re.compile(rf"{_CUR}\s?{_NUM}|{_NUM}\s?{_CUR}", re.IGNORECASE)

_INVOICE_RES = [
    re.compile(r"(?:invoice|receipt|order|document)\s*(?:no\.?|number|num\.?|#|id)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})", re.IGNORECASE),
    re.compile(r"(?:חשבונית(?: מס)?(?: קבלה)?|קבלה|הזמנה|מסמך)\s*(?:מס['׳]?|מספר)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})", re.IGNORECASE),
    re.compile(r"#\s?(\d{5,})"),
]


def _has(text: str, words: list[str]) -> list[str]:
    low = text.lower()
    return [w for w in words if w.lower() in low]


def _to_float(num: str) -> float | None:
    try:
        return float(re.sub(r"[,  ]", "", num))
    except ValueError:
        return None


def extract_amount(text: str) -> tuple[float | None, str]:
    """
    Picks the amount the receipt is *for*: an amount on a line with a total word wins (the last such
    line, since totals usually come after line items); otherwise the largest amount in the email.
    """
    best_total: tuple[float, str] | None = None
    largest: tuple[float, str] | None = None
    for line in text.splitlines():
        is_total_line = bool(_has(line, TOTAL_WORDS))
        for m in _AMOUNT_RE.finditer(line):
            cur_raw = (m.group(1) or m.group(4) or "").strip()
            num = m.group(2) or m.group(3)
            value = _to_float(num) if num else None
            if value is None or value <= 0 or value > 1_000_000:
                continue
            currency = CURRENCY_SYMBOLS.get(cur_raw.lower(), CURRENCY_SYMBOLS.get(cur_raw, ""))
            if is_total_line:
                best_total = (value, currency)
            if largest is None or value > largest[0]:
                largest = (value, currency)
    chosen = best_total or largest
    return (chosen[0], chosen[1]) if chosen else (None, "")


def extract_invoice_number(text: str) -> str:
    for rx in _INVOICE_RES:
        if m := rx.search(text):
            return m.group(1).strip("-")
    return ""


def classify(email: ParsedEmail) -> Classification:
    subject = email.subject
    body = email.text[:20000]
    amount, currency = extract_amount(f"{subject}\n{body}")
    invoice = extract_invoice_number(f"{subject}\n{body}")

    score = 0.0
    if _has(subject, RECEIPT_WORDS):
        score += 0.35
    if _has(body, RECEIPT_WORDS):
        score += 0.15
    if amount is not None:
        score += 0.25
        if _has(body, TOTAL_WORDS):
            score += 0.1
    if invoice:
        score += 0.1
    pdf_names = " ".join(a.filename.lower() for a in email.pdfs)
    if pdf_names:
        score += 0.2 if re.search(r"invoice|receipt|קבלה|חשבונית|bill", pdf_names) else 0.08
    domain = email.sender_domain
    if any(domain == d or domain.endswith("." + d) for d in KNOWN_SENDERS):
        score += 0.12

    promo_hits = len(_has(f"{subject}\n{body[:3000]}", PROMO_WORDS))
    if promo_hits:
        score -= min(0.45, 0.2 * promo_hits)
    if _has(subject, SHIPPING_ONLY_WORDS) and amount is None:
        score -= 0.25

    confidence = max(0.0, min(1.0, score))
    return Classification(
        is_receipt=confidence >= 0.5,
        company=company_from_sender(email.sender_name, email.sender_email),
        date=email.date,
        amount=amount,
        currency=currency or ("ILS" if re.search(r"[֐-׿]", body) else ""),
        invoice_number=invoice,
        confidence=round(confidence, 3),
        source="rules",
    )
