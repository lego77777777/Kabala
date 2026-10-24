"""
The real mailbox scan: search → fetch (in parallel) → detect → save PDFs → index.

Read-only by construction: the Gmail client only issues GET requests, and the OAuth scope is
gmail.readonly, so nothing in the mailbox can be changed or deleted.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from kabala.detect.message import ParsedEmail, parse_gmail_message
from kabala.detect.pipeline import Detector
from kabala.detect.query import build_query
from kabala.detect.result import Classification
from kabala.files import email_to_pdf, sha256, write_unique
from kabala.gmail import GmailClient, GmailError
from kabala.models import Receipt, ReviewItem, ScanOptions, ScanProgress, ScanSummary, summarize
from kabala.paths import receipt_path_segments
from kabala.store import Store

log = logging.getLogger(__name__)

FIRST_SCAN_DAYS = 365
OVERLAP_DAYS = 3  # rescans look a little further back than the last scan, in case mail arrived late
_RECEIPT_PDF = re.compile(r"invoice|receipt|bill|קבלה|חשבונית", re.IGNORECASE)


@dataclass
class _Outcome:
    gmail_id: str
    email: ParsedEmail | None = None
    result: Classification | None = None
    pdfs: list[tuple[str, bytes]] = field(default_factory=list)
    error: str = ""


class Scanner:
    def __init__(
        self,
        make_gmail: Callable[[], GmailClient],
        store: Store,
        detector: Detector,
        account: str,
        workers: int = 4,
        render_pdf: Callable[[ParsedEmail], bytes] = email_to_pdf,
        quota: int | None = None,
    ) -> None:
        # Free plan: how many receipts may still be saved this month (None = unlimited). Receipts past
        # the quota are left unprocessed, so upgrading and rescanning picks them up.
        self.quota = quota
        self.limited = False
        self.make_gmail = make_gmail
        self.store = store
        self.detector = detector
        self.account = account
        self.workers = workers
        self.render_pdf = render_pdf
        self._local = threading.local()
        self.errors = 0

    def _gmail(self) -> GmailClient:
        # One HTTP session per worker thread.
        if not hasattr(self._local, "gmail"):
            self._local.gmail = self.make_gmail()
        return self._local.gmail

    # ---- worker side ---------------------------------------------------------------

    def _process(self, gmail_id: str) -> _Outcome:
        out = _Outcome(gmail_id)
        try:
            gmail = self._gmail()
            email = parse_gmail_message(gmail.message(gmail_id))
            out.email = email
            out.result = self.detector.detect(email)
            if out.result.is_receipt and not out.result.reason:
                pdfs = email.pdfs
                preferred = [a for a in pdfs if _RECEIPT_PDF.search(a.filename)] or pdfs
                for a in preferred:
                    data = a.data if a.data is not None else gmail.attachment(gmail_id, a.attachment_id or "")
                    if data.startswith(b"%PDF"):
                        out.pdfs.append((a.filename, data))
        except GmailError as e:
            if e.code == "auth":
                raise
            out.error = e.code
        except Exception as e:  # noqa: BLE001 - one bad email must not stop the scan
            log.exception("failed on %s", gmail_id)
            out.error = type(e).__name__
        return out

    # ---- main side ---------------------------------------------------------------------

    def run(self, options: ScanOptions, on_progress: Callable[[ScanProgress], None], is_canceled: Callable[[], bool]) -> ScanSummary:
        found: list[Receipt] = []
        last_company: str | None = None
        root = Path(options.folder)

        last = self.store.get_meta(self.account, "last_scan")
        after = (date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)) if last else date.today() - timedelta(days=FIRST_SCAN_DAYS)
        query = build_query(after=after)

        # 1) search
        ids: list[str] = []
        for gid in self._gmail().search(query):
            ids.append(gid)
            if len(ids) % 50 == 0:
                on_progress(ScanProgress("searching", len(ids), 0, 0))
            if is_canceled():
                break
        done = self.store.processed_ids(self.account)
        todo = [i for i in ids if i not in done]
        total = len(todo)
        on_progress(ScanProgress("analyzing", 0, total, 0))

        # 2) fetch + detect in parallel, save on this thread (one writer for files and the index)
        processed = 0
        pending: set[Future] = set()
        queue = iter(todo)
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="kabala-scan") as pool:

            def refill() -> None:
                while len(pending) < self.workers * 2 and not is_canceled():
                    gid = next(queue, None)
                    if gid is None:
                        return
                    pending.add(pool.submit(self._process, gid))

            refill()
            while pending:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in finished:
                    pending.discard(fut)
                    receipt = self._handle(fut.result(), options, root)
                    processed += 1
                    if receipt:
                        found.append(receipt)
                        last_company = receipt.company
                    on_progress(ScanProgress("analyzing", processed, total, len(found), last_company))
                if is_canceled():
                    for f in pending:
                        f.cancel()
                    pending = {f for f in pending if not f.cancelled()}
                    continue
                refill()

        canceled = is_canceled()
        if not canceled:
            on_progress(ScanProgress("saving", processed, total, len(found), last_company))
            # Only move the search window forward when everything found was handled; after a quota stop,
            # the next scan (e.g. after upgrading) must look back far enough to pick up the rest.
            if self.errors == 0 and not self.limited:
                self.store.set_meta(self.account, "last_scan", date.today().isoformat())
        on_progress(ScanProgress("done", processed, total, len(found), last_company))
        review = len(self.store.review_items(self.account))
        summary = summarize(found, review, options.folder, canceled)
        summary.limited = self.limited
        return summary

    def _handle(self, out: _Outcome, options: ScanOptions, root: Path) -> Receipt | None:
        if out.error or out.email is None or out.result is None:
            self.errors += 1  # not marked processed: retried on the next scan
            return None
        email, res = out.email, out.result
        if not res.is_receipt:
            self.store.mark_processed(self.account, out.gmail_id, "not_receipt")
            return None
        if res.reason:
            self.store.add_review(self.account, out.gmail_id, self._review_item(out.gmail_id, email, res))
            self.store.mark_processed(self.account, out.gmail_id, "review")
            return None
        if self.quota is not None and self.quota <= 0:
            self.limited = True
            return None
        receipt = self.save(out.gmail_id, email, res, out.pdfs, options.layout, root)
        self.store.mark_processed(self.account, out.gmail_id, "receipt" if receipt else "duplicate")
        if receipt and self.quota is not None:
            self.quota -= 1
        return receipt

    def save(self, gmail_id: str, email: ParsedEmail, res: Classification, pdfs: list[tuple[str, bytes]],
             layout: str, root: Path) -> Receipt | None:
        """Writes the receipt file(s) and indexes them. Returns None for duplicates."""
        if self.store.has_message(self.account, email.message_id):
            return None
        files = [data for _, data in pdfs] or [self.render_pdf(email)]
        if self.store.path_for_hash(self.account, sha256(files[0])):
            return None  # same document already saved (e.g. a forwarded copy)
        amount = res.amount or 0.0
        currency = res.currency or "ILS"
        segments = receipt_path_segments(res.company, res.date or email.date, amount, currency, layout)  # type: ignore[arg-type]
        paths = [write_unique(root, segments, data) for data in files]
        receipt = Receipt(
            id=gmail_id, message_id=email.message_id, company=res.company, date=res.date or email.date,
            amount=amount, currency=currency, subject=email.subject, sender=email.sender_email,
            confidence=res.confidence, invoice_number=res.invoice_number or None, path_segments=segments,
            file_path=str(paths[0]),
        )
        self.store.add_receipt(self.account, gmail_id, receipt, res.source, sha256(files[0]))
        return receipt

    def approve(self, item: ReviewItem, layout: str, root: Path) -> Receipt | None:
        """The user said "it's a receipt" on the review screen: fetch the email again and file it."""
        gmail = self._gmail()
        email = parse_gmail_message(gmail.message(item.id))
        pdfs = [(a.filename, a.data if a.data is not None else gmail.attachment(item.id, a.attachment_id or ""))
                for a in email.pdfs]
        res = Classification(True, item.company, item.date, item.amount or None, item.currency,
                             item.invoice_number or "", 1.0, source="user")
        receipt = self.save(item.id, email, res, [(n, d) for n, d in pdfs if d.startswith(b"%PDF")], layout, root)
        self.store.remove_review(self.account, item.id)
        self.store.mark_processed(self.account, item.id, "receipt")
        return receipt

    def reject(self, item: ReviewItem) -> None:
        self.store.remove_review(self.account, item.id)
        self.store.mark_processed(self.account, item.id, "not_receipt")

    @staticmethod
    def _review_item(gmail_id: str, email: ParsedEmail, res: Classification) -> ReviewItem:
        return ReviewItem(
            id=gmail_id, message_id=email.message_id, company=res.company, date=res.date or email.date,
            amount=res.amount or 0.0, currency=res.currency or "ILS", subject=email.subject,
            sender=email.sender_email, confidence=res.confidence, invoice_number=res.invoice_number or None,
            reason=res.reason,
        )
