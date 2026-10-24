from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Classification:
    """What we know about one email. Same shape whether it came from local rules or the model."""

    is_receipt: bool
    company: str
    date: str  # YYYY-MM-DD, "" if unknown
    amount: float | None
    currency: str  # ISO 4217 ("ILS", "USD"), "" if unknown
    invoice_number: str
    confidence: float  # 0..1
    source: str = "rules"  # "rules" | "anthropic" | "ollama"
    reason: str = ""  # why it needs review, when it does
