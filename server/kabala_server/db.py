from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT UNIQUE,
    subscription_id TEXT,
    subscription_status TEXT,
    current_period_end INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
-- sign-in by email code (email_login.py): one open challenge per address
CREATE TABLE IF NOT EXISTS email_codes (
    email TEXT PRIMARY KEY,
    secret TEXT NOT NULL,
    counter INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    window_start REAL NOT NULL,
    sends INTEGER NOT NULL
);
"""

PRO_STATUSES = {"active", "trialing", "past_due"}  # past_due keeps access during Stripe's retry window


@dataclass
class User:
    id: int
    email: str
    plan: str
    stripe_customer_id: str | None
    subscription_status: str | None
    current_period_end: int | None


class Database:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def _one(self, sql: str, args: tuple) -> User | None:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        if not row:
            return None
        return User(row["id"], row["email"], row["plan"], row["stripe_customer_id"], row["subscription_status"], row["current_period_end"])

    def get_or_create(self, email: str) -> User:
        email = email.strip().lower()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO users(email, created_at) VALUES (?, ?)",
                (email, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        user = self.by_email(email)
        assert user is not None
        return user

    def by_email(self, email: str) -> User | None:
        return self._one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))

    def by_id(self, user_id: int) -> User | None:
        return self._one("SELECT * FROM users WHERE id=?", (user_id,))

    def by_customer(self, customer_id: str) -> User | None:
        return self._one("SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,))

    def link_customer(self, user_id: int, customer_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (customer_id, user_id))

    def set_subscription(self, user_id: int, subscription_id: str | None, status: str | None, period_end: int | None) -> None:
        plan = "pro" if status in PRO_STATUSES else "free"
        with self._lock, self._db:
            self._db.execute(
                "UPDATE users SET plan=?, subscription_id=?, subscription_status=?, current_period_end=? WHERE id=?",
                (plan, subscription_id, status, period_end, user_id),
            )

    def first_time_event(self, event_id: str) -> bool:
        """Stripe may deliver a webhook more than once; process each event id only once."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO stripe_events(id, received_at) VALUES (?, ?)",
                (event_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            return cur.rowcount == 1

    # ---- email codes ------------------------------------------------------------------

    def issue_email_code(self, email: str, new_secret: str, now: float, ttl: float, max_sends: int, window: float) -> tuple[str, int] | None:
        """
        Opens (or renews) the challenge for `email` and returns (secret, counter) for the code to send.
        A resend keeps the secret and moves the counter, so only the latest code works, and resets the tries.
        None when the address already got `max_sends` codes within `window` seconds.
        """
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM email_codes WHERE email=?", (email,)).fetchone()
            if row and now - row["window_start"] < window:
                if row["sends"] >= max_sends:
                    return None
                secret, counter, start, sends = row["secret"], row["counter"] + 1, row["window_start"], row["sends"] + 1
            else:
                secret, counter, start, sends = new_secret, 0, now, 1
            self._db.execute(
                "INSERT OR REPLACE INTO email_codes(email, secret, counter, expires_at, attempts, window_start, sends) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (email, secret, counter, now + ttl, start, sends),
            )
        return secret, counter

    def take_email_attempt(self, email: str, now: float, max_attempts: int) -> tuple[str, int] | None:
        """
        Counts one try *before* the code is checked (so parallel guesses can't exceed the limit) and returns
        (secret, counter), or None when there is no open challenge, it expired, or it is out of tries.
        """
        with self._lock, self._db:
            row = self._db.execute(
                "UPDATE email_codes SET attempts = attempts + 1 WHERE email=? AND expires_at > ? AND attempts < ? RETURNING secret, counter",
                (email, now, max_attempts),
            ).fetchone()
        return (row["secret"], row["counter"]) if row else None

    def close_email_code(self, email: str) -> None:
        """The code was used: no further tries (the send counter for the hour is kept)."""
        with self._lock, self._db:
            self._db.execute("UPDATE email_codes SET expires_at=0 WHERE email=?", (email,))
