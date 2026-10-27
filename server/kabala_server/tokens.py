"""
Session tokens, encrypted with Fernet (kabala_server/crypto.py): the payload (user id, email, expiry) can be
neither read nor changed by anyone holding the token. Stateless: the server keeps no session table.

Tokens from before encryption (signed HMAC-SHA256, readable payload) are still accepted until they expire,
so nobody is signed out by the upgrade.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from kabala_server.crypto import Secrets, box

PURPOSE = "session"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue(secret: Secrets, user_id: int, email: str, days: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = {"uid": user_id, "email": email, "exp": int(now + days * 86400)}
    return box(secret, PURPOSE).encrypt(json.dumps(payload, separators=(",", ":")), now=int(now))


def verify(secret: Secrets, token: str, now: float | None = None) -> dict | None:
    if "." in token:  # Fernet tokens are plain base64url; a dot means the old signed format
        payload = _verify_signed(secret, token)
    else:
        raw = box(secret, PURPOSE).decrypt(token)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
    if not isinstance(payload, dict) or payload.get("exp", 0) < (time.time() if now is None else now):
        return None
    return payload


def _verify_signed(secret: Secrets, token: str) -> dict | None:
    """The pre-encryption format: base64(payload).HMAC-SHA256 signature."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    for s in (secret,) if isinstance(secret, str) else secret:
        expected = _b64(hmac.new(s.encode(), body.encode(), hashlib.sha256).digest())
        if hmac.compare_digest(sig, expected):
            try:
                return json.loads(_unb64(body))
            except ValueError:
                return None
    return None
