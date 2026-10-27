"""
Encryption for data the server keeps or hands out, with Fernet (kabala_server/vendor/fernet, copied from
pyca/cryptography): AES + HMAC, so the data can neither be read nor changed without the key.

Every purpose ("session", "email-code", …) gets its own key, derived from KABALA_SECRET with HKDF-SHA256,
so a key for one use can never decrypt another's data. After changing KABALA_SECRET, list the old value in
KABALA_OLD_SECRETS: new data uses the new key and old data stays readable (MultiFernet).
"""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from kabala_server.vendor.fernet import Fernet, InvalidToken, MultiFernet

Secrets = str | tuple[str, ...]


def _key(secret: str, purpose: str) -> bytes:
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"kabala:{purpose}".encode()).derive(secret.encode())
    return base64.urlsafe_b64encode(raw)


class Box:
    """Encrypts strings for one purpose. `secrets`: the current KABALA_SECRET first, then old ones."""

    def __init__(self, secrets: Secrets, purpose: str) -> None:
        keys = (secrets,) if isinstance(secrets, str) else secrets
        self._f = MultiFernet([Fernet(_key(s, purpose)) for s in keys if s])

    def encrypt(self, text: str, now: int | None = None) -> str:
        data = text.encode()
        token = self._f.encrypt(data) if now is None else self._f.encrypt_at_time(data, int(now))
        return token.decode()

    def decrypt(self, token: str) -> str | None:
        """None when the token was made with another key, was changed, or isn't a Fernet token at all."""
        try:
            return self._f.decrypt(token).decode()
        except (InvalidToken, TypeError, ValueError):
            return None


@lru_cache(maxsize=32)
def box(secrets: Secrets, purpose: str) -> Box:
    return Box(secrets, purpose)
