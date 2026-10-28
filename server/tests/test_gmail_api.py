"""Sending through the Gmail API (gmail_api.py), with a fake HTTP session."""

import base64
import email
import sys
from email import policy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server import gmail_api  # noqa: E402
from kabala_server.app import create_app  # noqa: E402
from kabala_server.config import Config  # noqa: E402
from kabala_server.receipts import Mail  # noqa: E402


class Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class Session:
    def __init__(self, status=200, body=None):
        self.calls, self.status, self.body = [], status, body or {"id": "18c"}

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        return Response(self.status, self.body)

    def sent(self, i=-1):
        return email.message_from_bytes(base64.urlsafe_b64decode(self.calls[i][1]["raw"]), policy=policy.default)


def mailer(session):
    return gmail_api.GmailApiMailer("cid", "csecret", "rtoken", "Kabala <billing@kabala.app>", session=session)


def test_sign_in_code_goes_through_the_api():
    s = Session()
    mailer(s).send("dana@example.com", "קוד הכניסה שלך: 123456", "הקוד: 123456")
    url, _ = s.calls[0]
    assert url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    msg = s.sent()
    assert msg["To"] == "dana@example.com" and msg["From"] == "Kabala <billing@kabala.app>"
    assert "123456" in msg["Subject"] and "123456" in msg.get_body().get_content()


def test_receipt_with_pdf_html_and_hidden_copy():
    s = Session()
    mailer(s).send_mail(Mail(to="dana@example.com", subject="הקבלה שלך", text="שלום", html="<p dir=rtl>שלום</p>",
                             bcc=["cpa@office.co.il"], attachments=[("kabala-receipt-500001.pdf", b"%PDF-1.7 test")]))
    msg = s.sent()
    assert msg["Bcc"] == "cpa@office.co.il"  # Gmail delivers it and strips it from the sent message
    assert msg.get_body(("html",)).get_content().strip() == "<p dir=rtl>שלום</p>"
    [pdf] = list(msg.iter_attachments())
    assert pdf.get_filename() == "kabala-receipt-500001.pdf" and pdf.get_content() == b"%PDF-1.7 test"


def test_no_bcc_header_without_a_copy():
    s = Session()
    mailer(s).send_mail(Mail(to="a@example.com", subject="s", text="t", html="<p>t</p>"))
    assert s.sent()["Bcc"] is None


def test_refused_send_raises_with_googles_reason():
    s = Session(403, {"error": {"status": "PERMISSION_DENIED", "message": "Request had insufficient scopes."}})
    with pytest.raises(gmail_api.GmailApiError, match="403 PERMISSION_DENIED"):
        mailer(s).send("a@example.com", "s", "t")


def test_auth_url_asks_only_to_send_with_pkce():
    q = {k: v[0] for k, v in parse_qs(urlparse(gmail_api.auth_url("cid", "http://127.0.0.1:5555/", "st", "v" * 64)).query).items()}
    assert q["scope"] == "https://www.googleapis.com/auth/gmail.send"
    assert q["access_type"] == "offline" and q["code_challenge_method"] == "S256" and q["state"] == "st"
    assert q["redirect_uri"] == "http://127.0.0.1:5555/" and "client_secret" not in q


def test_app_uses_gmail_when_configured(tmp_path):
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "g.db"), smtp_url="smtps://u:p@h",
                 gmail_client_id="gid", gmail_client_secret="gsecret", gmail_refresh_token="grt")
    app = create_app(cfg, lambda t: {}, receipt_worker=False)
    assert isinstance(app.state.receipts.mailer, gmail_api.GmailApiMailer)
    assert "gsecret" not in repr(cfg) and "grt" not in repr(cfg)


def test_partial_gmail_config_is_refused(tmp_path):
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "g.db"), gmail_client_id="gid")
    with pytest.raises(RuntimeError, match="GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN"):
        create_app(cfg, lambda t: {}, receipt_worker=False)


def test_oauth_client_from_env_or_the_desktop_apps_file(tmp_path):
    f = tmp_path / "google_client.json"
    assert gmail_api.oauth_client({"GMAIL_CLIENT_ID": "a", "GMAIL_CLIENT_SECRET": "b"}, f) == ("a", "b")
    f.write_text('{"installed": {"client_id": "desk.apps.googleusercontent.com", "client_secret": "s"}}')
    assert gmail_api.oauth_client({}, f) == ("desk.apps.googleusercontent.com", "s")
    with pytest.raises(SystemExit):
        gmail_api.oauth_client({}, tmp_path / "missing.json")
