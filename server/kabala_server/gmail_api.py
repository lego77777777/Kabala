"""
All email goes through the Gmail API (users.messages.send): sign-in codes, receipts with their PDF, and admin alerts.
There is no SMTP. OAuth with google-auth (already a dependency, for Google sign-in); the access token is refreshed
automatically from a refresh token that only allows sending (scope gmail.send), never reading the mailbox.

Connecting the sending account, from the admin panel ("Gmail" page → "חבר Gmail"):
  - Google Cloud Console: enable the Gmail API, and create an OAuth client of type "Web application" whose
    authorized redirect URI is the one the panel shows (…/admin/api/gmail/callback). Set GMAIL_CLIENT_ID and
    GMAIL_CLIENT_SECRET on the server.
  - An owner clicks the button, signs in to the Gmail account and approves. The refresh token is stored encrypted
    in the database. Before any account is connected, the sign-in page offers the same button: the Google account
    must then be an owner's, and it signs that owner in.
Or without the panel: GMAIL_REFRESH_TOKEN from `python -m kabala_server.gmail_api authorize` (a Desktop app client,
or the desktop app's ../google_client.json). The PayPal billing server reads the same variables
(`npm run gmail:authorize`), so one token can serve both.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from kabala_server.crypto import box
from kabala_server.receipts import Mail, receipt_message

SCOPE = "https://www.googleapis.com/auth/gmail.send"
CONNECT_SCOPES = f"openid email {SCOPE}"  # the address of the account being connected, and sending
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
PENDING_S = 600  # an unfinished "connect" expires after 10 minutes
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GOOGLE_CLIENT_FILE = Path(__file__).resolve().parents[2] / "google_client.json"  # the desktop app's OAuth client


class GmailApiError(OSError):
    """A refused send. The text is Google's (status and reason), never a token."""


class GmailNotConnected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Gmail is not connected (admin panel → Gmail)")


class GmailApiMailer:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, sender: str, session: Any = None) -> None:
        creds = Credentials(None, refresh_token=refresh_token, token_uri=TOKEN_URI, client_id=client_id,
                            client_secret=client_secret, scopes=[SCOPE])
        self._session = session or AuthorizedSession(creds)  # refreshes the access token when needed
        self._lock = threading.Lock()  # the receipt worker and sign-in requests share one session
        self.sender = sender

    def send(self, to: str, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(text)
        self._post(msg)

    def send_mail(self, mail: Mail) -> None:
        msg = receipt_message(self.sender, mail)
        if mail.bcc:
            msg["Bcc"] = ", ".join(mail.bcc)  # Gmail delivers the hidden copy and removes the header from what is sent
        self._post(msg)

    def _post(self, msg: EmailMessage) -> None:
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        with self._lock:
            r = self._session.post(SEND_URL, json={"raw": raw}, timeout=20)
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                reason = f"{err.get('status', '')} {err.get('message', '')}".strip()
            except ValueError:
                reason = ""
            raise GmailApiError(f"Gmail API {r.status_code} {reason}"[:300])


class Gmail:
    """
    The server's one mailer. Sends as the account connected in the panel (stored encrypted), else GMAIL_REFRESH_TOKEN.
    `ready` is False until one exists; sending then raises GmailNotConnected.
    """

    def __init__(self, db: Any, config: Any, *, post: Callable[..., Any] = requests.post,
                 verify_id_token: Callable[[str], dict] | None = None, session_factory: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.db, self.config, self.clock = db, config, clock
        self._box = box(config.secrets, "gmail-token")
        self._post = post
        self._verify = verify_id_token or _google_id_verifier(config.gmail_client_id)
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._mailer: tuple[str, GmailApiMailer] | None = None  # (refresh token, mailer), rebuilt when it changes
        self._pending: dict[str, dict] = {}
        self.last_error: str | None = None
        self.last_sent_at: float | None = None

    # ---- status ----

    @property
    def client_configured(self) -> bool:
        return bool(self.config.gmail_client_id and self.config.gmail_client_secret)

    def _stored(self) -> dict | None:
        row = self.db.one("SELECT * FROM gmail_account WHERE id=1")
        token = self._box.decrypt(row["refresh_token"]) if row else None
        return {**row, "refresh_token": token} if row and token else None

    def _token(self) -> tuple[str, str, str] | None:
        """(refresh token, account address or "", source)."""
        if not self.client_configured:
            return None
        row = self._stored()
        if row:
            return row["refresh_token"], row["email"], "panel"
        if self.config.gmail_refresh_token:
            address = parseaddr(self.config.mail_from)[1]
            return self.config.gmail_refresh_token, address if "@" in address else "", "env"
        return None

    @property
    def ready(self) -> bool:
        return self._token() is not None

    def status(self) -> dict:
        row, token = self._stored(), self._token()
        return {"connected": token is not None, "email": token[1] if token else None, "source": token[2] if token else None,
                "connectedAt": row["connected_at"] if row else None, "connectedBy": row["connected_by"] if row else None,
                "clientConfigured": self.client_configured, "lastError": self.last_error,
                "lastSentAt": int(self.last_sent_at) if self.last_sent_at else None}

    # ---- sending ----

    def _current(self) -> GmailApiMailer:
        token = self._token()
        if not token:
            raise GmailNotConnected()
        refresh, address, _source = token
        with self._lock:
            if not self._mailer or self._mailer[0] != refresh:
                sender = formataddr((sender_name(self.config.mail_from), address)) if address else self.config.mail_from
                session = self._session_factory(refresh) if self._session_factory else None
                self._mailer = (refresh, GmailApiMailer(self.config.gmail_client_id, self.config.gmail_client_secret,
                                                        refresh, sender, session=session))
            return self._mailer[1]

    def _sending(self, fn: Callable[[GmailApiMailer], None]) -> None:
        try:
            fn(self._current())
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            raise
        self.last_error, self.last_sent_at = None, self.clock()

    def send(self, to: str, subject: str, text: str) -> None:
        self._sending(lambda m: m.send(to, subject, text))

    def send_mail(self, mail: Mail) -> None:
        self._sending(lambda m: m.send_mail(mail))

    # ---- connecting from the panel (OAuth for a Web application client, with PKCE) ----

    def start(self, redirect_uri: str, admin_email: str | None) -> str:
        """The Google consent URL. admin_email: the signed-in owner, or None before any account is connected."""
        if not self.client_configured:
            raise RuntimeError("set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET first")
        now = self.clock()
        self._pending = {k: v for k, v in self._pending.items() if v["until"] > now}
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        self._pending[state] = {"verifier": verifier, "admin": admin_email, "redirect": redirect_uri, "until": now + PENDING_S}
        return auth_url(self.config.gmail_client_id, redirect_uri, state, verifier, scope=CONNECT_SCOPES)

    def finish(self, state: str, code: str) -> tuple[str, str | None, str]:
        """Google's redirect → (account address, the owner who started it or None, refresh token). ValueError if refused."""
        pending = self._pending.pop(state, None)  # single use
        if not pending or pending["until"] < self.clock():
            raise ValueError("this link expired, start again")
        r = self._post(TOKEN_URI, data={
            "client_id": self.config.gmail_client_id, "client_secret": self.config.gmail_client_secret, "code": code,
            "code_verifier": pending["verifier"], "grant_type": "authorization_code", "redirect_uri": pending["redirect"],
        }, timeout=20)
        body = r.json() if r.status_code < 500 else {}
        if r.status_code >= 400 or not body.get("refresh_token") or not body.get("id_token"):
            raise ValueError(f"Google refused: {body.get('error', r.status_code)}")
        if SCOPE not in body.get("scope", "").split():
            raise ValueError("permission to send email was not granted (tick the Gmail box on Google's page)")
        claims = self._verify(body["id_token"])
        address = str(claims.get("email", "")).lower()
        if not address or not claims.get("email_verified"):
            raise ValueError("Google did not confirm the account's address")
        return address, pending["admin"], body["refresh_token"]

    def connect(self, address: str, refresh_token: str, by: str) -> None:
        old = self._stored()
        self.db.run("INSERT OR REPLACE INTO gmail_account (id, email, refresh_token, connected_at, connected_by) VALUES (1, ?, ?, ?, ?)",
                    (address, self._box.encrypt(refresh_token), _iso(self.clock()), by))
        self.last_error = None
        if old and old["refresh_token"] != refresh_token:
            self._revoke(old["refresh_token"])

    def disconnect(self) -> None:
        old = self._stored()
        self.db.run("DELETE FROM gmail_account WHERE id=1")
        if old:
            self._revoke(old["refresh_token"])

    def _revoke(self, token: str) -> None:
        try:  # best effort: the row is gone either way
            self._post(REVOKE_URI, data={"token": token}, timeout=10)
        except requests.RequestException:
            pass


def sender_name(mail_from: str) -> str:
    """MAIL_FROM is "Kabala" or "Kabala <x@y>": the name shown, next to the connected account (Gmail sends as it)."""
    name, address = parseaddr(mail_from)
    return name or (mail_from.strip() if "@" not in mail_from else "") or "Kabala"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _google_id_verifier(client_id: str) -> Callable[[str], dict]:
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token

    return lambda token: id_token.verify_oauth2_token(token, g_requests.Request(), audience=client_id)


# --- one-time authorization (python -m kabala_server.gmail_api authorize) ---

def auth_url(client_id: str, redirect_uri: str, state: str, verifier: str, scope: str = SCOPE) -> str:
    """Offline access (a refresh token) for sending only, with PKCE."""
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return AUTH_URI + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": scope,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str) -> str:
    r = requests.post(TOKEN_URI, data={
        "client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    }, timeout=20)
    r.raise_for_status()
    token = r.json().get("refresh_token")
    if not token:
        raise RuntimeError("Google returned no refresh token; remove the app's access in your Google account and retry")
    return token


def authorize(client_id: str, client_secret: str) -> str:
    """Opens the browser, waits for Google's redirect to a local port, and returns the refresh token."""
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            ok = q.get("state") == state and "code" in q
            if ok:
                got["code"] = q["code"]
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<p dir=rtl>אפשר לסגור את החלון ולחזור למסוף.</p>".encode() if ok else b"Authorization failed")

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    url = auth_url(client_id, redirect_uri, state, verifier)
    print(f"Opening the browser. If it doesn't open, visit:\n{url}\n", file=sys.stderr)
    webbrowser.open(url)
    while "code" not in got:
        server.handle_request()
    server.server_close()
    return exchange_code(client_id, client_secret, got["code"], redirect_uri, verifier)


def oauth_client(env: dict[str, str], client_file: Path = GOOGLE_CLIENT_FILE) -> tuple[str, str]:
    """GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET, else the desktop app's client file (like billing's gmail:authorize)."""
    if env.get("GMAIL_CLIENT_ID") and env.get("GMAIL_CLIENT_SECRET"):
        return env["GMAIL_CLIENT_ID"], env["GMAIL_CLIENT_SECRET"]
    if client_file.exists():
        installed = json.loads(client_file.read_text(encoding="utf-8"))["installed"]
        return installed["client_id"], installed["client_secret"]
    raise SystemExit("set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET (an OAuth client of type Desktop app), or add google_client.json")


if __name__ == "__main__":
    if sys.argv[1:] != ["authorize"]:
        sys.exit("usage: python -m kabala_server.gmail_api authorize")
    cid, csecret = oauth_client(dict(os.environ))
    print(f"GMAIL_CLIENT_ID={cid}\nGMAIL_CLIENT_SECRET=<the client secret>\nGMAIL_REFRESH_TOKEN={authorize(cid, csecret)}")
