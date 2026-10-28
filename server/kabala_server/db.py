from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from kabala_server.crypto import Secrets, box

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
-- sign-in by email code (email_login.py): one open challenge per address.
-- secret is Fernet-encrypted (crypto.py), and emptied once the code is used.
CREATE TABLE IF NOT EXISTS email_codes (
    email TEXT PRIMARY KEY,
    secret TEXT NOT NULL,
    counter INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    window_start REAL NOT NULL,
    sends INTEGER NOT NULL
);
-- admin panel (admin.py): who may sign in to /admin, and everything they did there
CREATE TABLE IF NOT EXISTS admins (
    email TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('viewer', 'admin', 'owner')),
    added_by TEXT,
    created_at TEXT NOT NULL
);
-- automatic receipts after Stripe payments (receipts.py). The PDF is Fernet-encrypted (it holds personal data).
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    number INTEGER NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    stripe_invoice_id TEXT NOT NULL UNIQUE,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    plan_interval TEXT,
    period_start INTEGER,
    period_end INTEGER,
    customer_email TEXT NOT NULL,
    customer_name TEXT,
    locale TEXT NOT NULL DEFAULT 'he',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'generated', 'sent', 'failed')),
    pdf TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_rounds INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    issued_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS receipt_emails (
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    to_addresses TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    error TEXT,
    draft TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS receipt_emails_receipt ON receipt_emails(receipt_id);
CREATE TABLE IF NOT EXISTS receipt_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    auto_send INTEGER NOT NULL DEFAULT 1,
    copy_to TEXT,
    subject_he TEXT, subject_en TEXT, intro_he TEXT, intro_en TEXT,
    auto_retry INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    ip TEXT
);
"""

ROLES = ("viewer", "admin", "owner")  # each role can do everything the previous one can

# past_due keeps access during Stripe's retry window; comp = Pro given by hand in the admin panel
PRO_STATUSES = {"active", "trialing", "past_due", "comp"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class User:
    id: int
    email: str
    plan: str
    stripe_customer_id: str | None
    subscription_status: str | None
    current_period_end: int | None


class Database:
    def __init__(self, path: str, secrets: Secrets) -> None:
        if path != ":memory:" and not os.path.exists(path):
            # owner-only from the start: emails and Stripe ids of every user live here
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._codes = box(secrets, "email-code")
        with self._lock, self._db:
            # deleted or overwritten rows (used sign-in secrets) are zeroed on disk, not left in free pages
            self._db.execute("PRAGMA secure_delete = ON")
            self._db.executescript(SCHEMA)
            columns = {r["name"] for r in self._db.execute("PRAGMA table_info(users)")}
            if "last_seen_at" not in columns:  # added with the admin panel
                self._db.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")

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

    def forget_event(self, event_id: str) -> None:
        """Un-records a webhook event that couldn't be handled yet, so Stripe's next delivery is processed."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM stripe_events WHERE id=?", (event_id,))

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
            old = self._codes.decrypt(row["secret"]) if row and row["secret"] else None
            if row and now - row["window_start"] < window:
                if row["sends"] >= max_sends:
                    return None
                start, sends = row["window_start"], row["sends"] + 1
                # a resend keeps the secret and moves the counter; after a used code, a fresh secret
                secret, counter = (old, row["counter"] + 1) if old else (new_secret, 0)
            else:
                secret, counter, start, sends = new_secret, 0, now, 1
            self._db.execute(
                "INSERT OR REPLACE INTO email_codes(email, secret, counter, expires_at, attempts, window_start, sends) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (email, self._codes.encrypt(secret), counter, now + ttl, start, sends),
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
        secret = self._codes.decrypt(row["secret"]) if row and row["secret"] else None
        return (secret, row["counter"]) if secret else None

    def close_email_code(self, email: str) -> None:
        """The code was used: no further tries, and the secret is erased (the send counter for the hour is kept)."""
        with self._lock, self._db:
            self._db.execute("UPDATE email_codes SET expires_at=0, secret='' WHERE email=?", (email,))

    # ---- generic access for modules with their own tables (receipts.py) ---------------------

    def run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, args)

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        found = self.rows(sql, args)
        return found[0] if found else None

    @contextmanager
    def transaction(self):
        """The connection, locked, inside one transaction (e.g. to take the next receipt number safely)."""
        with self._lock, self._db:
            yield self._db

    # ---- admin panel ------------------------------------------------------------------

    def touch_seen(self, user_id: int) -> None:
        """"Last seen" for the admin panel; written at most once a minute per user."""
        now = _now()
        with self._lock, self._db:
            self._db.execute("UPDATE users SET last_seen_at=? WHERE id=? AND (last_seen_at IS NULL OR last_seen_at < ?)",
                             (now, user_id, now[:17]))

    def user_rows(self, q: str = "", plan: str = "", status: str = "", paid: bool = False,
                  limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        where, args = [], []
        if q:
            where.append("email LIKE ? ESCAPE '\\'")
            args.append("%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if plan in ("free", "pro"):
            where.append("plan=?")
            args.append(plan)
        if status:
            where.append("subscription_status IS NULL" if status == "none" else "subscription_status=?")
            args += [] if status == "none" else [status]
        if paid:  # ever paid through Stripe (a customer exists), whatever the subscription's state now
            where.append("stripe_customer_id IS NOT NULL")
        sql_where = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM users {sql_where}", args).fetchone()[0]
            rows = self._db.execute(f"SELECT * FROM users {sql_where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                                    [*args, limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    def user_row(self, user_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def stats(self, now: datetime | None = None) -> dict:
        from datetime import timedelta

        now = now or datetime.now(timezone.utc)
        week = (now - timedelta(days=7)).isoformat(timespec="seconds")
        month_start = (now - timedelta(days=29)).date().isoformat()
        with self._lock:
            one = lambda sql, *a: self._db.execute(sql, a).fetchone()[0]  # noqa: E731
            data = {
                "users": one("SELECT COUNT(*) FROM users"),
                "pro": one("SELECT COUNT(*) FROM users WHERE plan='pro'"),
                "paying": one("SELECT COUNT(*) FROM users WHERE subscription_status IN ('active', 'trialing', 'past_due')"),
                "past_due": one("SELECT COUNT(*) FROM users WHERE subscription_status='past_due'"),
                "comp": one("SELECT COUNT(*) FROM users WHERE subscription_status='comp'"),
                "new_week": one("SELECT COUNT(*) FROM users WHERE created_at >= ?", week),
                "active_week": one("SELECT COUNT(*) FROM users WHERE last_seen_at >= ?", week),
            }
            by_day = dict(self._db.execute(
                "SELECT substr(created_at, 1, 10) AS d, COUNT(*) FROM users WHERE created_at >= ? GROUP BY d", (month_start,)).fetchall())
        data["free"] = data["users"] - data["pro"]
        data["signups"] = [{"day": (now - timedelta(days=i)).date().isoformat(),
                            "count": by_day.get((now - timedelta(days=i)).date().isoformat(), 0)} for i in range(29, -1, -1)]
        return data

    def admin_role(self, email: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT role FROM admins WHERE email=?", (email.strip().lower(),)).fetchone()
        return row["role"] if row else None

    def admins(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM admins ORDER BY created_at, email")]

    def set_admin(self, email: str, role: str, added_by: str | None) -> None:
        assert role in ROLES
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO admins(email, role, added_by, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET role=excluded.role",
                (email.strip().lower(), role, added_by, _now()),
            )

    def ensure_owner(self, email: str) -> None:
        """Bootstrap from KABALA_OWNER_EMAILS: adds the owner, never demotes or re-adds over a change made in the panel."""
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO admins(email, role, added_by, created_at) VALUES (?, 'owner', 'KABALA_OWNER_EMAILS', ?)",
                             (email.strip().lower(), _now()))

    def remove_admin(self, email: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM admins WHERE email=?", (email.strip().lower(),))

    def audit(self, actor: str, action: str, target: str | None = None, ip: str | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO admin_audit(at, actor, action, target, ip) VALUES (?, ?, ?, ?, ?)",
                             (_now(), actor, action, target, ip))

    def audit_rows(self, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0]
            rows = self._db.execute("SELECT * FROM admin_audit ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(r) for r in rows], total
