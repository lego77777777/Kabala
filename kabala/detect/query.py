"""Stage 1 of detection: a wide Gmail search that catches receipts cheaply (precision comes later)."""

from __future__ import annotations

from datetime import date

KEYWORDS_HE = [
    "קבלה",
    "חשבונית",
    "חשבונית מס",
    "חשבונית מס קבלה",
    "אישור תשלום",
    "אישור הזמנה",
    "פירוט חיוב",
    "תודה על הרכישה",
    "תודה על ההזמנה",
]

KEYWORDS_EN = [
    "receipt",
    "invoice",
    "tax invoice",
    "order confirmation",
    "payment confirmation",
    "payment received",
    "your order",
    "billing statement",
    "purchase",
]

# Senders that only email you about money; matched by domain.
KNOWN_SENDERS = [
    "netflix.com", "spotify.com", "apple.com", "itunes.com", "amazon.com", "paypal.com", "wolt.com",
    "uber.com", "google.com", "adobe.com", "microsoft.com", "aliexpress.com", "ebay.com", "booking.com",
    "airbnb.com", "github.com", "openai.com", "anthropic.com", "zoom.us", "dropbox.com", "canva.com",
    "stripe.com", "iec.co.il", "bezeq.co.il", "partner.co.il", "cellcom.co.il", "hot.net.il", "yes.co.il",
    "super-pharm.co.il", "shufersal.co.il", "10bis.co.il", "gett.com",
]


def _term(word: str) -> str:
    return f'"{word}"' if " " in word else word


def build_query(after: date | None = None, before: date | None = None, extra_senders: list[str] | None = None) -> str:
    """
    One Gmail query: any receipt keyword (subject or body, Hebrew or English) OR a PDF attachment
    OR a known billing sender. Spam/trash are excluded by Gmail by default; chats explicitly.
    """
    words = [f"subject:{_term(w)}" for w in KEYWORDS_HE + KEYWORDS_EN]
    words += [_term(w) for w in ("חשבונית מס", "קבלה מס", "receipt", "invoice")]
    words.append("filename:pdf")
    words += [f"from:{d}" for d in KNOWN_SENDERS + (extra_senders or [])]
    query = "{" + " ".join(words) + "} -in:chats -in:drafts"
    if after:
        query += f" after:{after:%Y/%m/%d}"
    if before:
        query += f" before:{before:%Y/%m/%d}"
    return query
