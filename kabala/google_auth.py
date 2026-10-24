"""
Gmail sign-in for a desktop app, done the way Google and RFC 8252 recommend:

* the consent page opens in the user's own browser (never an embedded webview),
* Google redirects back to a one-shot HTTP server on 127.0.0.1 with a random port (loopback),
* PKCE (S256) + a random `state` protect the code exchange,
* the only Gmail scope is gmail.readonly, so the app can never change or delete mail
  (plus openid/email, for the plan server to know who the user is),
* the refresh token is stored in the OS credential vault (Windows Credential Manager / macOS Keychain).

Built on google-auth-oauthlib (Apache-2.0) for PKCE and token exchange, and keyring (MIT) for storage.
The loopback server is our own so the user can cancel while the browser is open.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import keyring
import keyring.errors
import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# openid + email give a Google ID token that proves the user's identity to the Kabala plan server.
# The server only ever sees that ID token, never a Gmail access token.
SCOPES = [GMAIL_SCOPE, "openid", "https://www.googleapis.com/auth/userinfo.email"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
PROFILE_URI = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
KEYRING_SERVICE = "Kabala"
KEYRING_USER = "google"
CLIENT_FILE = "google_client.json"
AUTH_TIMEOUT_S = 300

log = logging.getLogger(__name__)

# Google may echo granted scopes in a different order/form; that is not an error for us.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class AuthError(Exception):
    """`code` is a stable key the UI maps to a friendly message."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


# ---- OAuth client configuration ---------------------------------------------


def client_config_locations() -> list[Path]:
    """Where we look for the Desktop OAuth client JSON downloaded from Google Cloud Console."""
    from kabala.settings import config_dir

    paths = []
    if env := os.environ.get("KABALA_GOOGLE_CLIENT"):
        paths.append(Path(env))
    paths.append(config_dir() / CLIENT_FILE)
    if hasattr(sys, "_MEIPASS"):  # bundled into the installer by PyInstaller
        paths.append(Path(sys._MEIPASS) / CLIENT_FILE)  # type: ignore[attr-defined]
    paths.append(Path(__file__).resolve().parent.parent / CLIENT_FILE)  # project folder (development)
    return paths


def load_client_config() -> dict | None:
    for path in client_config_locations():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "installed" in data and data["installed"].get("client_id"):
            return data
    log.info("Google client config not found in: %s", ", ".join(str(p) for p in client_config_locations()))
    return None


def check_client_file(path: Path) -> str:
    """'' if `path` is a usable Desktop OAuth client file, else an AuthError code explaining why not."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "client_unreadable"
    if "web" in data:
        return "client_web_type"  # created as "Web application" instead of "Desktop app"
    if not ("installed" in data and data["installed"].get("client_id") and data["installed"].get("client_secret")):
        return "client_unreadable"
    return ""


def import_client_file(path: Path) -> None:
    """Validates a downloaded client_secret_*.json and copies it to the app's settings folder."""
    from kabala.settings import config_dir

    if problem := check_client_file(path):
        raise AuthError(problem)
    target = config_dir() / CLIENT_FILE
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    log.info("Imported Google client config from %s", path)


def find_downloaded_client() -> Path | None:
    """The newest valid client_secret_*.json in the user's Downloads (what Google Cloud Console saves)."""
    downloads = Path.home() / "Downloads"
    try:
        candidates = sorted(downloads.glob("client_secret*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return next((p for p in candidates if not check_client_file(p)), None)


def ensure_client_config() -> bool:
    """True if a client config is available, importing one from Downloads if the user just saved it there."""
    if load_client_config() is not None:
        return True
    if found := find_downloaded_client():
        import_client_file(found)
        return True
    return False


# ---- Browser ------------------------------------------------------------------


def _chrome_path() -> str | None:
    if sys.platform == "win32":
        bases = [os.environ.get("PROGRAMFILES", r"C:\Program Files"), os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        for base in bases:
            p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if base and p.exists():
                return str(p)
    elif sys.platform == "darwin":
        p = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if p.exists():
            return str(p)
    return None


def open_in_browser(url: str) -> bool:
    """Opens Google Chrome if installed (the user's real browser, with their Google session), else the default browser."""
    chrome = _chrome_path()
    if chrome:
        try:
            subprocess.Popen([chrome, url], close_fds=True)
            log.info("Opened consent page in Chrome")
            return True
        except OSError:
            log.warning("Could not start Chrome; falling back to the default browser")
    ok = webbrowser.open(url)
    log.info("Opened consent page in the default browser: %s", ok)
    return ok


# ---- Loopback redirect server -----------------------------------------------

_DONE_PAGE = """<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><title>Kabala</title>
<style>
:root{{color-scheme:light dark}}
body{{margin:0;height:100vh;display:grid;place-items:center;font-family:"Segoe UI",system-ui,sans-serif;
background:#fafaf9;color:#18181b}}
@media (prefers-color-scheme:dark){{body{{background:#0d0d10;color:#ececf1}}.card{{background:#16161b;border-color:#2e2e38}}}}
.card{{text-align:center;padding:40px 48px;border:1px solid #e4e4e7;border-radius:18px;background:#fff;max-width:420px}}
.icon{{width:56px;height:56px;border-radius:50%;display:grid;place-items:center;margin:0 auto 16px;
background:{color};color:#fff;font-size:28px}}
h1{{font-size:22px;margin:0 0 8px}} p{{margin:4px 0;opacity:.7}}
</style></head><body><div class="card"><div class="icon">{icon}</div>
<h1>{title_he}</h1><p>{body_he}</p><p dir="ltr" style="margin-top:14px">{body_en}</p></div></body></html>"""


def _page(ok: bool) -> bytes:
    if ok:
        page = _DONE_PAGE.format(color="#15803d", icon="✓", title_he="מחובר בהצלחה",
                                 body_he="אפשר לסגור את הלשונית ולחזור ל-Kabala.",
                                 body_en="Connected. You can close this tab and return to Kabala.")
    else:
        page = _DONE_PAGE.format(color="#c81e1e", icon="!", title_he="החיבור לא הושלם",
                                 body_he="חזור ל-Kabala ונסה שוב.",
                                 body_en="The connection was not completed. Return to Kabala and try again.")
    return page.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.server.result = params  # type: ignore[attr-defined]
        body = _page("code" in params and "error" not in params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:  # keep the console quiet
        pass


# ---- Sign-in ----------------------------------------------------------------


@dataclass
class SignInResult:
    credentials: Credentials
    email: str


IDENTITY_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]


def identity_token(cancel: threading.Event, open_browser=None, timeout: float = AUTH_TIMEOUT_S) -> str:
    """
    Google ID token only (openid + email), for the plan server to know who is paying. Used by
    accounts connected over IMAP. These are basic scopes, so Google shows no "unverified app" screen,
    and nothing is stored: no Gmail access is requested at all.
    """
    creds = _authorize(cancel, IDENTITY_SCOPES, open_browser, timeout, offline=False)
    if not getattr(creds, "id_token", None):
        raise AuthError("exchange", "no id_token")
    return creds.id_token


def sign_in(cancel: threading.Event, open_browser=None, timeout: float = AUTH_TIMEOUT_S) -> SignInResult:
    """Runs the full browser flow. Blocks; call from a worker thread. Raises AuthError."""
    creds = _authorize(cancel, SCOPES, open_browser, timeout, offline=True)
    granted = set(creds.granted_scopes or SCOPES)
    if GMAIL_SCOPE not in granted:
        raise AuthError("scope")
    email = fetch_email(creds)
    save_credentials(creds, email)
    return SignInResult(creds, email)


def _authorize(cancel: threading.Event, scopes: list[str], open_browser, timeout: float, offline: bool) -> Credentials:
    config = load_client_config() if ensure_client_config() else None
    if config is None:
        raise AuthError("config_missing")

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.timeout = 0.25
    server.result = None  # type: ignore[attr-defined]
    try:
        port = server.server_address[1]
        flow = Flow.from_client_config(config, scopes=scopes, redirect_uri=f"http://127.0.0.1:{port}/")
        state = secrets.token_urlsafe(32)
        extra = {"access_type": "offline", "prompt": "consent"} if offline else {"prompt": "select_account"}
        url, _ = flow.authorization_url(state=state, **extra)
        if not (open_browser or open_in_browser)(url):
            raise AuthError("browser")

        deadline = time.monotonic() + timeout
        while server.result is None:  # type: ignore[attr-defined]
            if cancel.is_set():
                raise AuthError("canceled")
            if time.monotonic() > deadline:
                raise AuthError("timeout")
            server.handle_request()
        params: dict[str, str] = server.result  # type: ignore[attr-defined]
    finally:
        server.server_close()

    if params.get("state") != state:
        raise AuthError("state")
    if "error" in params:
        raise AuthError("denied" if params["error"] == "access_denied" else "google", params["error"])
    if cancel.is_set():
        raise AuthError("canceled")

    try:
        flow.fetch_token(code=params["code"])
    except Exception as e:  # noqa: BLE001 - network / invalid_grant / bad client
        raise AuthError("exchange", str(e)) from e
    return flow.credentials


def fetch_email(creds: Credentials) -> str:
    try:
        resp = AuthorizedSession(creds).get(PROFILE_URI, timeout=20)
        resp.raise_for_status()
        return resp.json()["emailAddress"]
    except Exception as e:  # noqa: BLE001
        raise AuthError("network", str(e)) from e


# ---- Stored credentials -----------------------------------------------------


def save_credentials(creds: Credentials, email: str) -> None:
    if not creds.refresh_token:
        raise AuthError("no_refresh_token")
    blob = json.dumps({"refresh_token": creds.refresh_token, "email": email, "scopes": sorted(creds.granted_scopes or SCOPES)})
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, blob)
    except keyring.errors.KeyringError as e:
        raise AuthError("keyring", str(e)) from e


def load_stored() -> tuple[str, Credentials] | None:
    """Returns (email, credentials) from the OS vault without touching the network."""
    config = load_client_config()
    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.KeyringError:
        return None
    if not raw or config is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    installed = config["installed"]
    creds = Credentials(
        token=None,
        refresh_token=data["refresh_token"],
        token_uri=installed.get("token_uri", TOKEN_URI),
        client_id=installed["client_id"],
        client_secret=installed.get("client_secret"),
        scopes=data.get("scopes") or [GMAIL_SCOPE],  # only what was actually granted; asking for more on refresh fails
    )
    return data["email"], creds


def refresh(creds: Credentials) -> None:
    """Gets a fresh access token. Raises AuthError('expired') if the user revoked access."""
    try:
        creds.refresh(Request())
    except Exception as e:  # noqa: BLE001
        raise AuthError("expired", str(e)) from e


def take_stored_token() -> str | None:
    """
    Deletes the stored grant from the OS vault right away and returns its refresh token (to revoke).
    Done synchronously and first, so a slow revoke can never delete a grant from a newer sign-in.
    """
    raw: str | None = None
    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.KeyringError:
        pass
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.KeyringError:
        pass  # nothing stored
    try:
        return json.loads(raw)["refresh_token"] if raw else None
    except (ValueError, KeyError):
        return None


def revoke(token: str | None) -> bool:
    """
    Revokes the grant at Google (RFC 7009): the token, and every access token made from it, stop
    working at once and the app disappears from myaccount.google.com/permissions. Best effort: True
    when Google confirmed. Offline it fails, and the user can still remove access on that page.
    """
    if not token:
        return False
    try:
        r = requests.post(REVOKE_URI, data={"token": token}, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 - offline: the local copy is already gone
        return False


def forget(revoke_at_google: bool = True) -> bool:
    """Deletes the stored grant and (optionally) revokes it at Google. Returns whether Google confirmed."""
    token = take_stored_token()
    return revoke(token) if revoke_at_google else False


__all__ = ["AuthError", "SignInResult", "sign_in", "identity_token", "load_stored", "refresh", "forget", "take_stored_token", "revoke", "load_client_config"]
