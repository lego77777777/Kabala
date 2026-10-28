"""
Sign-in by email code: proves the user owns the address they connected in the app (e.g. Gmail over IMAP
with an app password, which has no Google ID token), then the plan is looked up by that address.

POST /api/email/start   {email}         a 6-digit code is emailed, valid for 10 minutes
POST /api/email/verify  {email, code}   session token + plan, same response as /api/session

The codes are HOTP (RFC 4226) from pyotp, copied into kabala_server/vendor/pyotp: every challenge has a
random secret and a counter that moves on each resend, so only the latest code works, and the check is
constant-time. 5 wrong tries end a challenge; an address gets at most 5 codes an hour.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Protocol

from kabala_server.vendor import pyotp

log = logging.getLogger("kabala_server.email")

CODE_TTL_S = 10 * 60
MAX_ATTEMPTS = 5
MAX_SENDS = 5
SEND_WINDOW_S = 3600

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class Mailer(Protocol):
    """gmail_api.Gmail: every email goes through the Gmail API."""

    def send(self, to: str, subject: str, text: str) -> None: ...


def normalize_email(email: str) -> str | None:
    email = email.strip().lower()
    return email if len(email) <= 254 and _EMAIL.match(email) else None


def normalize_code(code: str) -> str:
    """Accepts '123 456', '123-456' and full-width digits (some keyboards and copy-paste from mail apps)."""
    return re.sub(r"[\s-]", "", unicodedata.normalize("NFKC", code))


def make_code(secret: str, counter: int) -> str:
    return pyotp.HOTP(secret, digits=6).at(counter)


def check_code(secret: str, counter: int, code: str) -> bool:
    code = normalize_code(code)
    return len(code) == 6 and code.isdigit() and pyotp.HOTP(secret, digits=6).verify(code, counter)


def new_secret() -> str:
    return pyotp.random_base32()


def code_email(code: str) -> tuple[str, str]:
    subject = f"קוד הכניסה שלך ל-Kabala: {code} / Your Kabala sign-in code"
    text = (
        f"קוד הכניסה שלך ל-Kabala הוא: {code}\n"
        "הקוד תקף ל-10 דקות. אם לא ביקשת אותו, אפשר להתעלם מהמייל הזה.\n"
        "\n"
        f"Your Kabala sign-in code is: {code}\n"
        "It is valid for 10 minutes. If you didn't ask for it, you can ignore this email.\n"
    )
    return subject, text
