from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Provider = Literal["gmail", "imap", "outlook"]  # imap = Gmail over IMAP with an app password
FolderLayout = Literal["company-first", "date-first"]
ScanPhase = Literal["searching", "analyzing", "saving", "done"]


@dataclass
class Account:
    email: str
    provider: Provider
    demo: bool = False  # True when the user chose demo mode instead of signing in


@dataclass
class Receipt:
    id: str
    message_id: str
    company: str
    date: str  # YYYY-MM-DD
    amount: float
    currency: str
    subject: str
    sender: str
    confidence: float  # 0..1, as returned by the classifier
    invoice_number: str | None = None
    path_segments: list[str] = field(default_factory=list)
    file_path: str = ""  # absolute path of the saved PDF ("" in demo mode)


@dataclass
class ReviewItem(Receipt):
    reason: str = "low_confidence"


@dataclass
class ScanProgress:
    phase: ScanPhase
    processed: int
    total: int
    found: int
    last_company: str | None = None


@dataclass
class ScanOptions:
    folder: str
    layout: FolderLayout
    threshold: float  # below this confidence a receipt goes to manual review


@dataclass
class ScanSummary:
    receipts: int
    companies: int
    needs_review: int
    totals_by_currency: dict[str, float]
    by_month: list[tuple[str, dict[str, float]]]  # [("2026-10", {"ILS": 412.3}), ...]
    folder: str
    canceled: bool
    limited: bool = False  # stopped saving at the Free plan's monthly limit


def totals_by_currency(receipts: list[Receipt]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for r in receipts:
        totals[r.currency] = totals.get(r.currency, 0.0) + r.amount
    return totals


def summarize(found: list[Receipt], needs_review: int, folder: str, canceled: bool) -> ScanSummary:
    months: dict[str, dict[str, float]] = {}
    for r in found:
        t = months.setdefault(r.date[:7], {})
        t[r.currency] = t.get(r.currency, 0.0) + r.amount
    return ScanSummary(
        receipts=len(found),
        companies=len({r.company for r in found}),
        needs_review=needs_review,
        totals_by_currency=totals_by_currency(found),
        by_month=sorted(months.items()),
        folder=folder,
        canceled=canceled,
    )
