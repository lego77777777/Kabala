"""
Stage 1 demo mailbox: simulates a scan with realistic data so the whole UI can be exercised
before Gmail is wired up. Replaced by the real scanner in stages 2–4.
"""

from __future__ import annotations

import random
import time
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Callable

from kabala.models import Receipt, ReviewItem, ScanOptions, ScanProgress
from kabala.paths import receipt_path_segments


@dataclass(frozen=True)
class _Template:
    company: str
    sender: str
    subject: str
    amount: tuple[float, float]
    currency: str
    monthly: bool
    max_per_month: int = 1


TEMPLATES = [
    _Template("Netflix", "info@account.netflix.com", "Your Netflix receipt", (54.9, 54.9), "ILS", True),
    _Template("Spotify", "no-reply@spotify.com", "הקבלה שלך מ-Spotify", (23.9, 23.9), "ILS", True),
    _Template("חברת החשמל", "noreply@iec.co.il", "חשבונית חשמל לתקופה", (280, 640), "ILS", True),
    _Template("בזק", "invoice@bezeq.co.il", "חשבונית מס מבזק", (89, 129), "ILS", True),
    _Template("Partner", "billing@partner.co.il", "חשבונית חודשית - פרטנר", (59, 59), "ILS", True),
    _Template("Apple", "no_reply@email.apple.com", "Your receipt from Apple", (9.9, 39.9), "ILS", True),
    _Template("Google", "payments-noreply@google.com", "Google One: קבלה", (7.9, 7.9), "ILS", True),
    _Template("Adobe", "mail@mail.adobe.com", "Adobe invoice", (22.99, 22.99), "USD", True),
    _Template("Wolt", "info@wolt.com", "הקבלה שלך מ-Wolt", (48, 190), "ILS", False, 4),
    _Template("Amazon", "auto-confirm@amazon.com", "Your Amazon.com order confirmation", (19, 240), "USD", False, 2),
    _Template("סופר-פארם", "noreply@super-pharm.co.il", "אישור תשלום - סופר-פארם", (35, 320), "ILS", False, 2),
]

NOISE = [
    ("Wolt", "promo@wolt.com", "20% הנחה על ההזמנה הבאה שלך", "not_sure"),
    ("PayPal", "service@paypal.com", "You sent a payment", "no_amount"),
    ("IKEA", "news@ikea.co.il", "אישור הזמנה - איסוף מהחנות", "not_sure"),
]


def generate_mailbox(today: date | None = None, seed: int = 42) -> list[Receipt]:
    """Six months of receipts ending today. Deterministic for a given seed and date."""
    today = today or date.today()
    rnd = random.Random(seed)
    out: list[Receipt] = []
    n = 0
    for back in range(5, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        last_day = today.day if back == 0 else monthrange(y, m)[1]
        for t in TEMPLATES:
            count = 1 if t.monthly else rnd.randint(0, t.max_per_month)
            for _ in range(count):
                day = min(3 + (len(t.company) * 2) % 25, last_day) if t.monthly else rnd.randint(1, last_day)
                lo, hi = t.amount
                amount = round(lo + rnd.random() * (hi - lo), 2)
                n += 1
                confidence = 0.55 + rnd.random() * 0.15 if rnd.random() > 0.92 else 0.88 + rnd.random() * 0.11
                out.append(
                    Receipt(
                        id=f"r{n}",
                        message_id=f"<demo-{n}@kabala.local>",
                        company=t.company,
                        date=f"{y}-{m:02d}-{day:02d}",
                        amount=amount,
                        currency=t.currency,
                        subject=t.subject,
                        sender=t.sender,
                        confidence=confidence,
                        invoice_number=str(rnd.randint(100000, 999999)) if rnd.random() > 0.2 else None,
                    )
                )
    return sorted(out, key=lambda r: r.date)


def run_demo_scan(
    options: ScanOptions,
    known_ids: set[str],
    on_progress: Callable[[ScanProgress], None],
    is_canceled: Callable[[], bool],
    delay: float = 1.0,
) -> tuple[list[Receipt], list[ReviewItem]]:
    """Walks the fake mailbox, reporting progress. Returns (receipts, items needing review)."""
    drafts = generate_mailbox()
    found: list[Receipt] = []
    review: list[ReviewItem] = []

    # Pretend the keyword search matched ~3 emails per real receipt.
    analyze_steps = len(drafts) * 3 + len(NOISE)
    search_steps = round(analyze_steps * 0.15)
    total = search_steps + analyze_steps
    last_company: str | None = None

    def emit(phase: str, processed: int) -> None:
        on_progress(ScanProgress(phase, processed, total, len(found), last_company))  # type: ignore[arg-type]

    processed = 0
    while processed < search_steps and not is_canceled():
        emit("searching", processed)
        time.sleep(0.018 * delay)
        processed += 1

    di = ni = 0
    while processed < total and not is_canceled():
        i = processed - search_steps
        if i % 3 == 0 and di < len(drafts):
            d = drafts[di]
            di += 1
            if d.message_id not in known_ids:
                d.path_segments = receipt_path_segments(d.company, d.date, d.amount, d.currency, options.layout)
                if d.confidence < options.threshold:
                    review.append(ReviewItem(**d.__dict__, reason="low_confidence"))
                else:
                    found.append(d)
                    last_company = d.company
        elif i % 17 == 5 and ni < len(NOISE):
            company, sender, subject, reason = NOISE[ni]
            ni += 1
            review.append(
                ReviewItem(
                    id=f"n{ni}",
                    message_id=f"<demo-noise-{ni}@kabala.local>",
                    company=company,
                    date=drafts[min(di, len(drafts) - 1)].date,
                    amount=0.0,
                    currency="ILS",
                    subject=subject,
                    sender=sender,
                    confidence=0.4 + ni * 0.05,
                    reason=reason,
                )
            )
        emit("analyzing", processed)
        time.sleep(0.028 * delay)
        processed += 1

    if not is_canceled():
        emit("saving", processed)
        time.sleep(0.7 * delay)
    emit("done", processed)
    return found, review
