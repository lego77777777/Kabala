"""
Stage 2 of detection: a language model confirms the email is a receipt and extracts the fields.

Providers are swappable behind `Classifier`:
* AnthropicClassifier – Claude Haiku 5.5 through the official `anthropic` SDK (structured JSON output).
* OllamaClassifier   – a model running locally in Ollama; nothing leaves the computer.

Privacy: only the subject, sender, date and a redacted excerpt of the text are sent. Attachments never are.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

import keyring
import keyring.errors
import requests

from kabala.detect.message import ParsedEmail
from kabala.detect.result import Classification

ANTHROPIC_MODEL = "claude-haiku-5-5"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:7b"
KEYRING_SERVICE = "Kabala"
ANTHROPIC_KEY_USER = "anthropic_api_key"
MAX_EXCERPT_CHARS = 4000


class LLMError(Exception):
    pass


SYSTEM_PROMPT = """You check emails for a bookkeeping app that files receipts and invoices into folders.

Decide whether the email documents a completed payment or a bill: a receipt, invoice, tax invoice \
(חשבונית מס / חשבונית מס קבלה), payment confirmation (אישור תשלום) or order confirmation that states \
an amount charged. Marketing, coupons, shipping updates without a price, password resets, newsletters \
and quotes that were not paid are not receipts.

Emails may be in Hebrew or English. Extract:
- company: the merchant the user paid, as the brand people know it (e.g. "Netflix", "חברת החשמל"), \
not the payment processor (Stripe, PayPal, Tranzila, Cardcom) unless the merchant is unknown. \
No legal suffixes (Inc, Ltd, בע"מ) and no domain endings.
- date: the payment/invoice date as YYYY-MM-DD; use the email date if the text has none.
- amount: the final total actually charged, as a number, including VAT. 0 if none is stated.
- currency: ISO 4217 code (₪ / ש"ח / NIS → ILS, $ → USD, € → EUR). Empty if unknown.
- invoice_number: the invoice/receipt/order number, or empty.
- confidence: 0 to 1, how sure you are of is_receipt and the amount together.

Parts of the text may be masked as [CARD] or [EMAIL] for privacy; that is expected."""

SCHEMA = {
    "type": "object",
    "properties": {
        "is_receipt": {"type": "boolean"},
        "company": {"type": "string"},
        "date": {"type": "string"},
        "amount": {"type": "number"},
        "currency": {"type": "string"},
        "invoice_number": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["is_receipt", "company", "date", "amount", "currency", "invoice_number", "confidence"],
    "additionalProperties": False,
}

_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ID_RE = re.compile(r"\b\d{9}\b")  # Israeli ID numbers (ת.ז.)


def redact(text: str) -> str:
    text = _CARD_RE.sub("[CARD]", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    return _ID_RE.sub("[ID]", text)


def build_prompt(email: ParsedEmail) -> str:
    """The minimum the model needs. Long emails: the start plus every line that mentions money."""
    body = email.text
    if len(body) > MAX_EXCERPT_CHARS:
        money_lines = [ln for ln in body.splitlines() if re.search(r"\d", ln) and re.search(
            r"total|סה|סכום|amount|paid|שולם|₪|\$|€|ש\"ח|invoice|חשבונית|receipt|קבלה", ln, re.IGNORECASE)]
        head = body[: MAX_EXCERPT_CHARS // 2]
        tail = "\n".join(money_lines)[: MAX_EXCERPT_CHARS // 2]
        body = f"{head}\n…\n{tail}"
    attachments = ", ".join(a.filename for a in email.attachments if a.filename) or "none"
    return (
        f"Subject: {email.subject}\n"
        f"From: {email.sender_name} <{email.sender_domain}>\n"
        f"Email date: {email.date}\n"
        f"Attachments: {attachments}\n\n"
        f"Text:\n{redact(body)}"
    )


def _to_classification(data: dict, source: str, email: ParsedEmail) -> Classification:
    amount = float(data.get("amount") or 0)
    return Classification(
        is_receipt=bool(data["is_receipt"]),
        company=str(data.get("company", "")).strip(),
        date=str(data.get("date") or email.date)[:10],
        amount=amount if amount > 0 else None,
        currency=str(data.get("currency", "")).upper()[:3],
        invoice_number=str(data.get("invoice_number", "")).strip(),
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0)))),
        source=source,
    )


class Classifier(Protocol):
    name: str

    def classify(self, email: ParsedEmail) -> Classification: ...


# ---- Anthropic --------------------------------------------------------------


def get_anthropic_key() -> str | None:
    try:
        key = keyring.get_password(KEYRING_SERVICE, ANTHROPIC_KEY_USER)
    except keyring.errors.KeyringError:
        key = None
    return key or os.environ.get("ANTHROPIC_API_KEY")


def set_anthropic_key(key: str | None) -> None:
    """Stores the key in the OS credential vault (or removes it when empty)."""
    try:
        if key:
            keyring.set_password(KEYRING_SERVICE, ANTHROPIC_KEY_USER, key.strip())
        else:
            keyring.delete_password(KEYRING_SERVICE, ANTHROPIC_KEY_USER)
    except keyring.errors.PasswordDeleteError:
        pass


@dataclass
class AnthropicClassifier:
    client: object  # anthropic.Anthropic (or a test double with the same .messages.create)
    model: str = ANTHROPIC_MODEL
    name: str = "anthropic"

    @classmethod
    def from_keyring(cls) -> "AnthropicClassifier | None":
        key = get_anthropic_key()
        if not key:
            return None
        import anthropic

        return cls(anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=3))

    def classify(self, email: ParsedEmail) -> Classification:
        import anthropic

        try:
            response = self.client.messages.create(  # type: ignore[attr-defined]
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_prompt(email)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("auth") from e
        except anthropic.RateLimitError as e:
            raise LLMError("rate_limit") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"api_{e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("network") from e

        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(response.stop_reason)
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return _to_classification(json.loads(text), self.name, email)
        except (ValueError, KeyError) as e:
            raise LLMError("bad_json") from e


# ---- Ollama (local) ---------------------------------------------------------


@dataclass
class OllamaClassifier:
    model: str = OLLAMA_MODEL
    url: str = OLLAMA_URL
    name: str = "ollama"

    def classify(self, email: ParsedEmail) -> Classification:
        body = {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(email)},
            ],
        }
        try:
            resp = requests.post(self.url, json=body, timeout=120)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
            return _to_classification(json.loads(content), self.name, email)
        except requests.RequestException as e:
            raise LLMError("network") from e
        except (ValueError, KeyError) as e:
            raise LLMError("bad_json") from e


def make_classifier(provider: str) -> Classifier | None:
    """The configured verifier, or None (local rules only) if it isn't set up."""
    if provider == "anthropic":
        return AnthropicClassifier.from_keyring()
    if provider == "ollama":
        return OllamaClassifier()
    return None
