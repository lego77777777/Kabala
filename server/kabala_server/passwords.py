"""
Admin panel sign-in by username and password: if they match, the admin is signed in; if not, nothing happens.

Passwords are hashed with scrypt by werkzeug.security (Werkzeug/Flask), copied into vendor/werkzeug_security
(changes in its KABALA_CHANGES.md): a random salt per password and a constant-time comparison. The password itself
is never stored or logged. An unknown username still runs a full hash, so the time taken doesn't reveal who exists.
After 5 wrong passwords in a row the account is locked for 15 minutes (on top of the per-IP limits in shield.py).
"""

from __future__ import annotations

import re
import secrets
from functools import lru_cache

from kabala_server.vendor.werkzeug_security import check_password_hash, generate_password_hash

MIN_LENGTH, MAX_LENGTH = 10, 128
LOCK_AFTER, LOCK_S = 5, 15 * 60
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
COMMON = {"1234567890", "12345678910", "123456789012", "0123456789", "password123", "password1234", "qwertyuiop",
          "1q2w3e4r5t", "qwerty123456", "abcdefghij", "1111111111", "0000000000", "iloveyou12", "admin12345",
          "administrator", "kabala12345", "letmein1234", "welcome1234", "passw0rd123"}


@lru_cache(maxsize=1)
def _dummy() -> str:
    return generate_password_hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify(stored: str | None, password: str) -> bool:
    """True only for the right password. No stored hash (unknown user, no password set) costs the same time."""
    if len(password) > MAX_LENGTH:
        return False
    if not stored:
        check_password_hash(_dummy(), password)
        return False
    return check_password_hash(stored, password)


def normalize_username(value: str) -> str:
    return value.strip().lower()


def username_error(username: str) -> str | None:
    if not USERNAME.match(username):
        return "שם משתמש: 3 עד 32 תווים, אותיות באנגלית, ספרות ו-. _ - (מתחיל באות או ספרה)"
    return None


def password_error(password: str, username: str, email: str) -> str | None:
    if len(password) < MIN_LENGTH:
        return f"הסיסמה קצרה מדי: לפחות {MIN_LENGTH} תווים"
    if len(password) > MAX_LENGTH:
        return f"הסיסמה ארוכה מדי: עד {MAX_LENGTH} תווים"
    lowered = password.lower()
    if lowered in COMMON or len(set(password)) < 4:
        return "הסיסמה נפוצה או פשוטה מדי"
    local = email.split("@")[0].lower()
    if username and username in lowered or len(local) >= 3 and local in lowered:
        return "הסיסמה לא יכולה להכיל את שם המשתמש או את המייל"
    return None
