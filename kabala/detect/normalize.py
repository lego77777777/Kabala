"""
Company name normalization, so "Netflix.com", "NETFLIX INC" and "Netflix" all land in one folder.

Order of operations: clean → strip domains and legal suffixes → look up a known-brand alias →
fall back to a tidy version of what's left. When the display name is generic ("no-reply",
"Billing") the sender's domain is used instead.
"""

from __future__ import annotations

import re
import unicodedata

# key (lowercase, no punctuation) -> canonical folder name
ALIASES: dict[str, str] = {
    "netflix": "Netflix",
    "spotify": "Spotify",
    "amazon": "Amazon",
    "amzn": "Amazon",
    "amazonmktplace": "Amazon",
    "apple": "Apple",
    "itunes": "Apple",
    "appstore": "Apple",
    "google": "Google",
    "googleone": "Google",
    "googleplay": "Google",
    "youtube": "YouTube",
    "microsoft": "Microsoft",
    "msft": "Microsoft",
    "adobe": "Adobe",
    "paypal": "PayPal",
    "wolt": "Wolt",
    "uber": "Uber",
    "ubereats": "Uber",
    "aliexpress": "AliExpress",
    "alibaba": "AliExpress",
    "ebay": "eBay",
    "shein": "SHEIN",
    "temu": "Temu",
    "ikea": "IKEA",
    "airbnb": "Airbnb",
    "booking": "Booking.com",
    "github": "GitHub",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "zoom": "Zoom",
    "dropbox": "Dropbox",
    "canva": "Canva",
    "steam": "Steam",
    "steampowered": "Steam",
    "disney": "Disney+",
    "disneyplus": "Disney+",
    "partner": "Partner",
    "cellcom": "Cellcom",
    "hotmobile": "HOT",
    "hot": "HOT",
    "yes": "yes",
    "bezeq": "בזק",
    "בזק": "בזק",
    "bezeqint": "בזק בינלאומי",
    "iec": "חברת החשמל",
    "חברתהחשמל": "חברת החשמל",
    "חברתהחשמללישראל": "חברת החשמל",
    "superpharm": "סופר-פארם",
    "סופרפארם": "סופר-פארם",
    "shufersal": "שופרסל",
    "שופרסל": "שופרסל",
    "rami-levy": "רמי לוי",
    "ramilevy": "רמי לוי",
    "רמילוי": "רמי לוי",
    "10bis": "תן ביס",
    "תןביס": "תן ביס",
    "gett": "Gett",
    "ksp": "KSP",
    "ivory": "Ivory",
}

# Legal-entity and boilerplate words dropped from the end (and sometimes the start) of a name.
_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "bv",
    "sa", "sarl", "plc", "pty", "ag", "srl", "spa", "kk", "group", "holdings", "international", "intl",
    "services", "service", "payments", "payment", "billing", "store", "online", "digital", "europe", "emea",
    "israel", "il", "com", "net", "בעמ", "בע\"מ", "בע״מ", "ישראל",
}

# Display names that say nothing about the company.
_GENERIC_SENDERS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications", "notification", "billing", "receipts",
    "receipt", "invoice", "invoices", "info", "support", "service", "customerservice", "payments", "orders",
    "order", "accounts", "account", "team", "hello", "mail", "mailer", "news", "help", "admin", "sales",
}

# Second-level labels that are part of a country suffix (super-pharm.co.il → super-pharm).
_SLD = {"co", "com", "org", "net", "gov", "ac", "org", "ltd", "muni"}

_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)/?$", re.IGNORECASE)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("‏", "").replace("‎", "")
    text = re.sub(r"[\"'`’״׳]", "", text)  # quotes, incl. Hebrew geresh/gershayim (בע"מ → בעמ)
    return re.sub(r"\s+", " ", text).strip(" .,-|:")


def _key(text: str) -> str:
    return re.sub(r"[^0-9a-z֐-׿]", "", text.lower())


def brand_from_domain(domain: str) -> str:
    """'account.netflix.com' → 'netflix', 'super-pharm.co.il' → 'super-pharm'."""
    labels = [p for p in domain.lower().strip(".").split(".") if p]
    if len(labels) < 2:
        return labels[0] if labels else ""
    labels = labels[:-1]  # drop TLD
    if len(labels) >= 2 and labels[-1] in _SLD:
        labels = labels[:-1]
    return labels[-1]


def _strip_affixes(words: list[str]) -> list[str]:
    while words and _key(words[-1]) in _SUFFIXES | {""}:
        words = words[:-1]
    while len(words) > 1 and _key(words[0]) in {"the"}:
        words = words[1:]
    return words


def _tidy_case(name: str) -> str:
    """NETFLIX → Netflix, netflix → Netflix; mixed case (eBay, GitHub) and Hebrew are kept."""
    if re.fullmatch(r"[A-Z0-9 &+.-]+", name) and len(name) > 4:
        return name.title()
    if re.fullmatch(r"[a-z0-9 &+.-]+", name):
        return name.title()
    return name


def normalize_company(name: str) -> str:
    """Canonical folder name for a company name as written in an email or by the model."""
    text = _clean(name or "")
    if not text:
        return ""
    if m := _DOMAIN_RE.match(text):
        text = brand_from_domain(m.group(1))
    words = _strip_affixes(re.split(r"[ ]+", text))
    if not words:
        return ""
    # Strip a trailing ".com" glued to the last word ("Netflix.com", "Amazon.com").
    words[-1] = re.sub(r"\.(com|net|io|co|co\.il|org|il)$", "", words[-1], flags=re.IGNORECASE)
    joined = " ".join(words)
    for candidate in (joined, words[0], "".join(words)):
        if (alias := ALIASES.get(_key(candidate))) is not None:
            return alias
    return _tidy_case(joined)


def is_generic_sender(display_name: str) -> bool:
    key = _key(display_name.replace(" ", ""))
    return not key or key in {_key(g) for g in _GENERIC_SENDERS}


def company_from_sender(display_name: str, email: str) -> str:
    """Best company guess from the From header alone."""
    if display_name and not is_generic_sender(display_name):
        # "Netflix <info@...>", "Wolt via Stripe" → first part
        primary = re.split(r"\s+(?:via|from|דרך)\s+", display_name, maxsplit=1, flags=re.IGNORECASE)[0]
        name = normalize_company(primary)
        if name:
            return name
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return normalize_company(brand_from_domain(domain)) if domain else ""
