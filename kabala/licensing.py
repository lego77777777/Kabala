"""
Plans and limits, backed by the Kabala server (server/). Free: 50 receipts a month, one account,
no export or background scans. Pro: unlimited.

When no server is configured (development builds), the app runs "unmanaged": every feature is open.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import keyring
import keyring.errors
import requests

log = logging.getLogger(__name__)

KEYRING_SERVICE = "Kabala"
SESSION_USER = "license_session"
CACHE_USER = "license_plan"
SERVER_FILE = "kabala_server.json"
OFFLINE_GRACE_S = 7 * 86400  # keep a paid plan working offline for a week
FREE_MONTHLY_RECEIPTS = 50


@dataclass
class Plan:
    managed: bool
    plan: str  # "free" | "pro"
    email: str = ""
    status: str | None = None
    current_period_end: int | None = None
    monthly_receipts: int | None = FREE_MONTHLY_RECEIPTS
    export: bool = False
    background_scan: bool = False

    @property
    def is_pro(self) -> bool:
        return self.plan == "pro"

    @classmethod
    def from_api(cls, data: dict) -> "Plan":
        limits = data.get("limits") or {}
        return cls(
            managed=True,
            plan=data.get("plan", "free"),
            email=data.get("email", ""),
            status=data.get("status"),
            current_period_end=data.get("current_period_end"),
            monthly_receipts=limits.get("monthly_receipts", FREE_MONTHLY_RECEIPTS),
            export=bool(limits.get("export")),
            background_scan=bool(limits.get("background_scan")),
        )


UNMANAGED = Plan(managed=False, plan="pro", monthly_receipts=None, export=True, background_scan=True)
FREE = Plan(managed=True, plan="free")


class LicenseError(Exception):
    pass


def secure_server_url(url: str) -> bool:
    """https only; plain http is allowed just for a server on this computer (development)."""
    from urllib.parse import urlparse

    u = urlparse(url)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))


def server_config() -> dict | None:
    """{"url": "...", "provider": "paypal" | "stripe"} from env or kabala_server.json. Never an unencrypted remote server."""
    cfg = _server_config()
    if cfg and not secure_server_url(cfg["url"]):
        logging.getLogger(__name__).warning("ignoring plan server %s: it must use https", cfg["url"])
        return None
    return cfg


def _server_config() -> dict | None:
    if env := os.environ.get("KABALA_SERVER_URL"):
        return {"url": env.rstrip("/"), "provider": os.environ.get("KABALA_BILLING_PROVIDER", "stripe")}
    from kabala.settings import config_dir

    candidates = [config_dir() / SERVER_FILE]
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / SERVER_FILE)  # type: ignore[attr-defined]
    candidates.append(Path(__file__).resolve().parent.parent / SERVER_FILE)
    for p in candidates:
        try:
            # utf-8-sig: Notepad and PowerShell often save JSON with a BOM
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        except ValueError as e:
            logging.getLogger(__name__).warning("ignoring invalid %s: %s", p, e)
            continue
        if data.get("url"):
            return {"url": data["url"].rstrip("/"), "provider": data.get("provider", "stripe")}
    return None


def server_url() -> str | None:
    cfg = server_config()
    return cfg["url"] if cfg else None


def _kr_get(user: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, user)
    except keyring.errors.KeyringError:
        return None


def _kr_set(user: str, value: str | None) -> None:
    try:
        if value is None:
            keyring.delete_password(KEYRING_SERVICE, user)
        else:
            keyring.set_password(KEYRING_SERVICE, user, value)
    except keyring.errors.KeyringError:
        pass


class LicenseClient:
    def __init__(self, base_url: str, http: requests.Session | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.http = http or requests.Session()

    # ---- session ------------------------------------------------------------

    @property
    def token(self) -> str | None:
        return _kr_get(SESSION_USER)

    def login(self, google_id_token: str) -> Plan:
        r = self.http.post(f"{self.base}/api/session", json={"id_token": google_id_token}, timeout=20)
        if r.status_code != 200:
            raise LicenseError(f"session {r.status_code}")
        data = r.json()
        _kr_set(SESSION_USER, data["token"])
        return self._cache(Plan.from_api(data))

    def logout(self) -> None:
        _kr_set(SESSION_USER, None)
        _kr_set(CACHE_USER, None)

    def _auth(self) -> dict:
        tok = self.token
        if not tok:
            raise LicenseError("no session")
        return {"Authorization": f"Bearer {tok}"}

    # ---- plan -----------------------------------------------------------------

    def _cache(self, plan: Plan) -> Plan:
        _kr_set(CACHE_USER, json.dumps({"plan": asdict(plan), "at": time.time()}))
        return plan

    def cached(self) -> Plan | None:
        raw = _kr_get(CACHE_USER)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if time.time() - data["at"] > OFFLINE_GRACE_S:
                return None
            return Plan(**data["plan"])
        except (ValueError, KeyError, TypeError):
            return None

    def status(self) -> Plan:
        """Current plan from the server; offline → the cached plan (for a week); otherwise Free."""
        try:
            r = self.http.get(f"{self.base}/api/me", headers=self._auth(), timeout=15)
        except LicenseError:
            return FREE
        except requests.RequestException:
            return self.cached() or FREE
        if r.status_code == 401:
            self.logout()
            return FREE
        if r.status_code != 200:
            return self.cached() or FREE
        return self._cache(Plan.from_api(r.json()))

    def checkout_url(self) -> str:
        return self._post_url("/api/checkout")

    def portal_url(self) -> str:
        return self._post_url("/api/portal")

    def _post_url(self, path: str) -> str:
        r = self.http.post(f"{self.base}{path}", headers=self._auth(), timeout=20)
        if r.status_code != 200:
            raise LicenseError(f"{path} {r.status_code}")
        return r.json()["url"]

    def latest_version(self) -> dict | None:
        try:
            r = self.http.get(f"{self.base}/api/version", timeout=10)
            return r.json() if r.status_code == 200 else None
        except requests.RequestException:
            return None
