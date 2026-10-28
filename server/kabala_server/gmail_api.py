"""
Sending email through the Gmail API (users.messages.send) instead of SMTP: sign-in codes, receipts with their PDF,
and admin alerts. OAuth with google-auth (already a dependency, for Google sign-in); the access token is refreshed
automatically from a refresh token that only allows sending (scope gmail.send), never reading the mailbox.

Setup, once, on your own computer:
  1. Google Cloud Console → enable the Gmail API → OAuth client of type "Desktop app".
  2. GMAIL_CLIENT_ID=… GMAIL_CLIENT_SECRET=… python -m kabala_server.gmail_api authorize
     A browser opens, you sign in to the sending Gmail account, and the refresh token is printed.
  3. On the server set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN (and MAIL_FROM with that address).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import webbrowser
from email.message import EmailMessage
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

from kabala_server.receipts import Mail, receipt_message

SCOPE = "https://www.googleapis.com/auth/gmail.send"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class GmailApiError(OSError):
    """A refused send. The text is Google's (status and reason), never a token."""


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


# --- one-time authorization (python -m kabala_server.gmail_api authorize) ---

def auth_url(client_id: str, redirect_uri: str, state: str, verifier: str) -> str:
    """Offline access (a refresh token) for sending only, with PKCE."""
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return AUTH_URI + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
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


if __name__ == "__main__":
    if sys.argv[1:] != ["authorize"]:
        sys.exit("usage: GMAIL_CLIENT_ID=… GMAIL_CLIENT_SECRET=… python -m kabala_server.gmail_api authorize")
    cid, csecret = os.environ.get("GMAIL_CLIENT_ID", ""), os.environ.get("GMAIL_CLIENT_SECRET", "")
    if not cid or not csecret:
        sys.exit("set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET (the OAuth client of type Desktop app)")
    print("GMAIL_REFRESH_TOKEN=" + authorize(cid, csecret))
