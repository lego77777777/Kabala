"""
Desktop side of the PayPal billing server (kabala-py/billing).

- Identity: the Google ID token from the Gmail sign-in → POST /api/session → session token.
- License: a JWT signed by the server with Ed25519 (private key only on the server). The app verifies it
  with the PUBLIC key and keeps working offline until it expires (7 days), then needs a refresh.
- Prices: GET /api/pricing. The app never contains a price.
- Payment and subscription management happen in the browser (short-lived link from POST /api/links);
  PayPal secrets never touch the app.

Duck-type compatible with licensing.LicenseClient, so the Backend can use either server.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from kabala import licensing
from kabala.licensing import FREE, LicenseError, Plan, _kr_get, _kr_set

log = logging.getLogger(__name__)

SESSION_USER = "paypal_session"
LICENSE_USER = "paypal_license"
PUBLIC_KEY_USER = "paypal_license_pubkey"
ISSUER = "kabala-billing"
PUBLIC_KEY_FILE = "license_public_key.pem"


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def verify_license(token: str, public_key_pem: str, now: float | None = None) -> dict:
    """Checks signature (EdDSA), issuer, audience and expiry. Raises LicenseError."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64(header_b64))
        payload = json.loads(_b64(payload_b64))
        signature = _b64(sig_b64)
    except (ValueError, json.JSONDecodeError) as e:
        raise LicenseError("malformed license") from e
    if header.get("alg") != "EdDSA":
        raise LicenseError("unexpected algorithm")
    key = load_pem_public_key(public_key_pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise LicenseError("not an Ed25519 key")
    try:
        key.verify(signature, f"{header_b64}.{payload_b64}".encode())
    except InvalidSignature as e:
        raise LicenseError("bad signature") from e
    if payload.get("iss") != ISSUER or payload.get("aud") != "license":
        raise LicenseError("wrong issuer/audience")
    if payload.get("exp", 0) <= (time.time() if now is None else now):
        raise LicenseError("expired")
    return payload


def plan_from_claims(claims: dict) -> Plan:
    limits = claims.get("limits") or {}
    paid_until = claims.get("paidUntil")
    end = None
    if paid_until:
        from datetime import datetime

        end = int(datetime.fromisoformat(paid_until.replace("Z", "+00:00")).timestamp())
    return Plan(
        managed=True,
        plan=claims.get("plan", "free"),
        email=claims.get("email", ""),
        status=claims.get("status"),
        current_period_end=end,
        monthly_receipts=limits.get("monthlyReceipts"),
        export=bool(limits.get("export")),
        background_scan=bool(limits.get("backgroundScan")),
    )


def bundled_public_key() -> str | None:
    import sys

    from kabala.settings import config_dir

    candidates = [config_dir() / PUBLIC_KEY_FILE]
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / PUBLIC_KEY_FILE)  # type: ignore[attr-defined]
    candidates.append(Path(__file__).resolve().parent.parent / PUBLIC_KEY_FILE)
    for p in candidates:
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


class PayPalLicenseClient:
    def __init__(self, base_url: str, http: requests.Session | None = None, public_key_pem: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.http = http or requests.Session()
        # The bundled key is the trust anchor; a key fetched from the server is only used when none is bundled.
        self.public_key = public_key_pem or bundled_public_key() or _kr_get(PUBLIC_KEY_USER)

    # ---- session ---------------------------------------------------------------

    @property
    def token(self) -> str | None:
        return _kr_get(SESSION_USER)

    def _auth(self) -> dict:
        tok = self.token
        if not tok:
            raise LicenseError("no session")
        return {"Authorization": f"Bearer {tok}"}

    def login(self, google_id_token: str) -> Plan:
        r = self.http.post(f"{self.base}/api/session", json={"id_token": google_id_token}, timeout=20)
        if r.status_code != 200:
            raise LicenseError(f"session {r.status_code}")
        data = r.json()
        _kr_set(SESSION_USER, data["session"])
        return self._store_license(data["license"])

    def activate_key(self, key: str) -> Plan:
        """A license key (KBL-…) from the purchase email instead of signing in: same session + signed license."""
        try:
            r = self.http.post(f"{self.base}/api/license/activate", json={"key": key.strip()}, timeout=20)
        except requests.RequestException as e:
            raise LicenseError("network") from e
        if r.status_code != 200:
            raise LicenseError({400: "bad_key", 401: "bad_key", 429: "rate_limited"}.get(r.status_code, "network"))
        data = r.json()
        _kr_set(SESSION_USER, data["session"])
        return self._store_license(data["license"])

    def logout(self) -> None:
        for user in (SESSION_USER, LICENSE_USER):
            _kr_set(user, None)

    # ---- license ----------------------------------------------------------------

    def _store_license(self, token: str, public_key: str | None = None) -> Plan:
        if public_key and not self.public_key:
            self.public_key = public_key
            _kr_set(PUBLIC_KEY_USER, public_key)
        if not self.public_key:
            raise LicenseError("no license public key")
        claims = verify_license(token, self.public_key)
        _kr_set(LICENSE_USER, token)
        return plan_from_claims(claims)

    def cached(self) -> Plan | None:
        """The last license, if its signature is valid and it hasn't expired (offline use up to 7 days)."""
        tok = _kr_get(LICENSE_USER)
        if not tok or not self.public_key:
            return None
        try:
            return plan_from_claims(verify_license(tok, self.public_key))
        except LicenseError:
            return None

    def status(self) -> Plan:
        try:
            r = self.http.get(f"{self.base}/api/license", headers=self._auth(), timeout=15)
        except LicenseError:
            return FREE
        except requests.RequestException:
            return self.cached() or FREE  # offline: last license until it expires, then Free
        if r.status_code == 401:
            self.logout()
            return FREE
        if r.status_code != 200:
            return self.cached() or FREE
        data = r.json()
        try:
            return self._store_license(data["license"], data.get("publicKey"))
        except LicenseError as e:
            log.warning("license rejected: %s", e)
            return self.cached() or FREE

    # ---- pricing & links ------------------------------------------------------------

    def pricing(self) -> dict | None:
        try:
            r = self.http.get(f"{self.base}/api/pricing", timeout=10)
            return r.json() if r.status_code == 200 else None
        except requests.RequestException:
            return None

    def _link(self, page: str) -> str:
        r = self.http.post(f"{self.base}/api/links", headers=self._auth(), json={"page": page}, timeout=20)
        if r.status_code != 200:
            raise LicenseError(f"link {r.status_code}")
        return r.json()["url"]

    def checkout_url(self) -> str:
        return self._link("checkout")

    def portal_url(self) -> str:
        return self._link("account")

    def latest_version(self) -> dict | None:
        return None


def make_client(url: str, provider: str):
    return PayPalLicenseClient(url) if provider == "paypal" else licensing.LicenseClient(url)
