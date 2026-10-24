"""
Local SQLite index: saved receipts, emails waiting for review, and which Gmail messages were already
handled (so a rescan only looks at new mail). Email bodies are never stored, only extracted fields.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from kabala.models import Receipt, ReviewItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    account TEXT NOT NULL,
    gmail_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    company TEXT NOT NULL,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    invoice_number TEXT,
    confidence REAL NOT NULL,
    source TEXT NOT NULL,
    subject TEXT NOT NULL,
    sender TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_account ON receipts(account, date);
CREATE INDEX IF NOT EXISTS receipts_hash ON receipts(account, file_hash);
CREATE INDEX IF NOT EXISTS receipts_msg ON receipts(account, message_id);

CREATE TABLE IF NOT EXISTS review (
    gmail_id TEXT NOT NULL,
    account TEXT NOT NULL,
    message_id TEXT NOT NULL,
    company TEXT NOT NULL,
    date TEXT NOT NULL,
    amount REAL,
    currency TEXT NOT NULL,
    invoice_number TEXT,
    confidence REAL NOT NULL,
    subject TEXT NOT NULL,
    sender TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (account, gmail_id)
);

CREATE TABLE IF NOT EXISTS processed (
    account TEXT NOT NULL,
    gmail_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (account, gmail_id)
);

CREATE TABLE IF NOT EXISTS meta (
    account TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (account, key)
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, args)

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---- processed messages ----------------------------------------------------

    def processed_ids(self, account: str) -> set[str]:
        return {r["gmail_id"] for r in self._query("SELECT gmail_id FROM processed WHERE account=?", (account,))}

    def mark_processed(self, account: str, gmail_id: str, decision: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO processed(account, gmail_id, decision, processed_at) VALUES (?,?,?,?)",
            (account, gmail_id, decision, datetime.now().isoformat(timespec="seconds")),
        )

    # ---- receipts ----------------------------------------------------------------

    def has_message(self, account: str, message_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM receipts WHERE account=? AND message_id=? LIMIT 1", (account, message_id)))

    def path_for_hash(self, account: str, file_hash: str) -> str | None:
        rows = self._query("SELECT file_path FROM receipts WHERE account=? AND file_hash=? LIMIT 1", (account, file_hash))
        return rows[0]["file_path"] if rows else None

    def add_receipt(self, account: str, gmail_id: str, r: Receipt, source: str, file_hash: str) -> None:
        self._exec(
            """INSERT INTO receipts(account, gmail_id, message_id, company, date, amount, currency, invoice_number,
               confidence, source, subject, sender, file_path, file_hash, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account, gmail_id, r.message_id, r.company, r.date, r.amount, r.currency, r.invoice_number, r.confidence,
             source, r.subject, r.sender, r.file_path, file_hash, datetime.now().isoformat(timespec="seconds")),
        )

    def saved_this_month(self, account: str) -> int:
        prefix = datetime.now().strftime("%Y-%m")
        rows = self._query("SELECT COUNT(*) AS n FROM receipts WHERE account=? AND created_at LIKE ?", (account, f"{prefix}%"))
        return int(rows[0]["n"])

    def receipts(self, account: str) -> list[Receipt]:
        rows = self._query("SELECT * FROM receipts WHERE account=? ORDER BY date, id", (account,))
        return [
            Receipt(
                id=f"db{r['id']}", message_id=r["message_id"], company=r["company"], date=r["date"], amount=r["amount"],
                currency=r["currency"], subject=r["subject"], sender=r["sender"], confidence=r["confidence"],
                invoice_number=r["invoice_number"] or None, file_path=r["file_path"],
            )
            for r in rows
        ]

    # ---- review --------------------------------------------------------------------

    def add_review(self, account: str, gmail_id: str, item: ReviewItem) -> None:
        self._exec(
            """INSERT OR REPLACE INTO review(gmail_id, account, message_id, company, date, amount, currency,
               invoice_number, confidence, subject, sender, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gmail_id, account, item.message_id, item.company, item.date, item.amount or None, item.currency,
             item.invoice_number, item.confidence, item.subject, item.sender, item.reason),
        )

    def review_items(self, account: str) -> list[ReviewItem]:
        rows = self._query("SELECT * FROM review WHERE account=? ORDER BY date DESC", (account,))
        return [
            ReviewItem(
                id=r["gmail_id"], message_id=r["message_id"], company=r["company"], date=r["date"],
                amount=r["amount"] or 0.0, currency=r["currency"], subject=r["subject"], sender=r["sender"],
                confidence=r["confidence"], invoice_number=r["invoice_number"] or None, reason=r["reason"],
            )
            for r in rows
        ]

    def remove_review(self, account: str, gmail_id: str) -> None:
        self._exec("DELETE FROM review WHERE account=? AND gmail_id=?", (account, gmail_id))

    # ---- meta ------------------------------------------------------------------------

    def get_meta(self, account: str, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE account=? AND key=?", (account, key))
        return rows[0]["value"] if rows else None

    def set_meta(self, account: str, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO meta(account, key, value) VALUES (?,?,?)", (account, key, value))

    def wipe(self) -> None:
        """Deletes the whole index. Saved PDF files are left untouched."""
        with self._lock, self._db:
            for table in ("receipts", "review", "processed", "meta"):
                self._db.execute(f"DELETE FROM {table}")
