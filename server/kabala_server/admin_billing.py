"""
The admin panel's view of the PayPal billing server (../billing, src/admin/admin-api.ts).

Each request is signed with the panel's Ed25519 private key as a JWT (PyJWT, copied into vendor/jwt): issuer
kabala-admin, audience kabala-billing, the admin's email and role, bound to one method + path, valid for
60 seconds, with a unique id so it can't be replayed. The billing server holds only the public key.

Setup:  python -m kabala_server.admin_billing keygen
  → KABALA_BILLING_ADMIN_KEY (private, for this server) and ADMIN_PANEL_PUBLIC_KEY (public, for billing/.env),
  plus KABALA_BILLING_URL=https://<billing server>.
"""

from __future__ import annotations

import sys
import time
import uuid
from urllib.parse import urlencode, urlparse

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kabala_server.vendor import jwt

ISSUER, AUDIENCE, TTL_S = "kabala-admin", "kabala-billing", 60


class BillingUnavailable(Exception):
    pass


class BillingNotFound(Exception):
    pass


class BillingClient:
    def __init__(self, base_url: str, private_key_pem: str, http: requests.Session | None = None) -> None:
        u = urlparse(base_url)
        if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
            raise RuntimeError("KABALA_BILLING_URL must be https (http only for localhost)")
        self.base = base_url.rstrip("/")
        self.key = serialization.load_pem_private_key(private_key_pem.replace("\\n", "\n").encode(), password=None)
        if not isinstance(self.key, Ed25519PrivateKey):
            raise RuntimeError("KABALA_BILLING_ADMIN_KEY must be an Ed25519 private key (run: python -m kabala_server.admin_billing keygen)")
        self.http = http or requests.Session()

    def token(self, method: str, path: str, admin_email: str, role: str) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": admin_email, "role": role, "htm": method, "htu": path,
                  "iat": now, "exp": now + TTL_S, "jti": uuid.uuid4().hex}
        return jwt.encode(claims, self.key, algorithm="EdDSA")

    def get(self, path: str, admin_email: str, role: str, params: dict | None = None) -> dict:
        return self._call("GET", path, admin_email, role, params)

    def post(self, path: str, admin_email: str, role: str) -> dict:
        return self._call("POST", path, admin_email, role)

    def _call(self, method: str, path: str, admin_email: str, role: str, params: dict | None = None) -> dict:
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{self.base}{path}" + (f"?{urlencode(query)}" if query else "")
        headers = {"Authorization": f"Bearer {self.token(method, path, admin_email, role)}"}
        try:
            r = self.http.request(method, url, headers=headers, timeout=10)
        except requests.RequestException as e:
            raise BillingUnavailable("billing server unreachable") from e
        if r.status_code == 404:
            raise BillingNotFound(path)
        if r.status_code != 200:
            raise BillingUnavailable(f"billing server answered {r.status_code}")
        return r.json()


def keygen() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


if __name__ == "__main__":
    if sys.argv[1:] != ["keygen"]:
        sys.exit("usage: python -m kabala_server.admin_billing keygen")
    private, public = keygen()
    one_line = lambda pem: pem.strip().replace("\n", "\\n")  # noqa: E731 - env files want one line
    print("# server (admin panel), keep secret:")
    print(f'KABALA_BILLING_ADMIN_KEY="{one_line(private)}"')
    print("\n# billing/.env:")
    print(f'ADMIN_PANEL_PUBLIC_KEY="{one_line(public)}"')
