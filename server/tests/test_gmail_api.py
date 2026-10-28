"""Sending through the Gmail API (gmail_api.py), with a fake HTTP session."""

import base64
import email
import sys
from email import policy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

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


OWNER, VIEWER = "owner@kabala.app", "viewer@kabala.app"
H = {"X-Kabala-Admin": "1"}


class Google:
    """Google's token and revoke endpoints, the ID token check, and the Gmail send API."""

    def __init__(self):
        self.account, self.scope, self.revoked, self.sessions = OWNER, f"openid email {gmail_api.SCOPE}", [], {}
        self.n = 0

    def post(self, url, data, timeout):
        if url == gmail_api.REVOKE_URI:
            self.revoked.append(data["token"])
            return Response(200, {})
        assert url == gmail_api.TOKEN_URI and data["grant_type"] == "authorization_code" and data["code_verifier"]
        if data["code"] == "bad":
            return Response(400, {"error": "invalid_grant"})
        self.n += 1
        return Response(200, {"refresh_token": f"1//rt{self.n}", "id_token": f"idt:{self.account}", "scope": self.scope})

    def verify(self, token):
        return {"email": token.split(":", 1)[1], "email_verified": True}

    def session(self, refresh_token):
        return self.sessions.setdefault(refresh_token, Session())


class Billing:
    """The PayPal billing server's admin API (BillingClient's get/post), keeping the Gmail account it was given."""

    def __init__(self):
        self.account, self.calls, self.down = None, [], False

    def get(self, path, email, role, params=None):
        self._check()
        assert path == "/api/admin/gmail"
        a = self.account
        return {"connected": bool(a), "email": a and a["email"], "source": a and "panel", "lastError": None, "lastSentAt": None}

    def post(self, path, email, role, body=None):
        self._check()
        self.calls.append((path, email, role, body))
        if path == "/api/admin/gmail":
            self.account = body
        elif path == "/api/admin/gmail/disconnect":
            self.account = None
        elif path == "/api/admin/gmail/test":
            return {"ok": True, "to": email}
        return {"connected": bool(self.account)}

    def _check(self):
        from kabala_server.admin_billing import BillingUnavailable
        if self.down:
            raise BillingUnavailable("billing server unreachable")


def make(tmp_path, billing=None, **kw):
    google = Google()
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "g.db"), owner_emails=(OWNER,),
                 gmail_client_id="gid.apps.googleusercontent.com", gmail_client_secret="GOCSPX-s", **kw)
    from kabala_server.db import Database
    gm = gmail_api.Gmail(Database(cfg.db_path, cfg.secrets), cfg, post=google.post, verify_id_token=google.verify,
                         session_factory=google.session)
    app = create_app(cfg, lambda t: {}, receipt_worker=False, gmail=gm, billing_client=billing)
    return TestClient(app), google, gm


def connect_via(client, path, account_headers=None, code="c1"):
    r = client.post(path, headers=H)
    assert r.status_code == 200, r.text
    q = {k: v[0] for k, v in parse_qs(urlparse(r.json()["url"]).query).items()}
    assert q["scope"] == f"openid email {gmail_api.SCOPE}" and q["redirect_uri"].endswith("/admin/api/gmail/callback")
    return client.get("/admin/api/gmail/callback", params={"state": q["state"], "code": code}), q["state"]


def test_nothing_is_sent_until_gmail_is_connected(tmp_path):
    c, _, gm = make(tmp_path)
    assert c.get("/admin/api/mail-status").json() == {"ready": False, "clientConfigured": True}
    assert c.post("/api/email/start", json={"email": "dana@example.com"}).status_code == 503
    assert c.post("/admin/api/login/start", json={"email": OWNER}).status_code == 503
    with pytest.raises(gmail_api.GmailNotConnected):
        gm.send("a@example.com", "s", "t")


def test_owner_connects_from_the_sign_in_page_and_is_signed_in(tmp_path):
    c, google, gm = make(tmp_path)
    page, _ = connect_via(c, "/admin/api/gmail/setup")
    assert page.status_code == 200 and "Gmail חובר" in page.text
    assert c.get("/admin/api/me").json() == {"email": OWNER, "role": "owner"}  # Google proved who it is
    st = c.get("/admin/api/gmail").json()
    assert st["connected"] and st["email"] == OWNER and st["source"] == "panel" and st["connectedBy"] == OWNER
    # every email now goes through the Gmail API, as the connected account
    assert c.post("/api/email/start", json={"email": "dana@example.com"}).status_code == 200
    sent = google.sessions["1//rt1"].sent()
    assert sent["To"] == "dana@example.com" and sent["From"] == "Kabala <owner@kabala.app>"
    assert b"1//rt1" not in Path(gm.config.db_path).read_bytes()  # the token is stored encrypted
    # once connected, the sign-in page can't connect another account
    assert c.post("/admin/api/gmail/setup").status_code == 409


def test_first_connection_must_be_an_owners_google_account(tmp_path):
    c, google, gm = make(tmp_path)
    google.account = "stranger@gmail.com"
    page, _ = connect_via(c, "/admin/api/gmail/setup")
    assert page.status_code == 400 and "אינו בעלים" in page.text
    assert not gm.ready and google.revoked == ["1//rt1"] and c.get("/admin/api/me").status_code == 401


def test_refused_expired_and_replayed_connections(tmp_path):
    c, google, gm = make(tmp_path)
    page, _ = connect_via(c, "/admin/api/gmail/setup", code="bad")
    assert page.status_code == 400 and "invalid_grant" in page.text
    google.scope = "openid email"  # the Gmail box was left unticked
    page, _ = connect_via(c, "/admin/api/gmail/setup")
    assert page.status_code == 400 and "send email" in page.text
    google.scope = f"openid email {gmail_api.SCOPE}"
    page, state = connect_via(c, "/admin/api/gmail/setup")
    assert page.status_code == 200
    assert c.get("/admin/api/gmail/callback", params={"state": state, "code": "c1"}).status_code == 400  # single use
    assert c.get("/admin/api/gmail/callback", params={"state": "made-up", "code": "c1"}).status_code == 400
    assert c.get("/admin/api/gmail/callback", params={"error": "access_denied"}).status_code == 400


def test_owner_switches_account_tests_and_disconnects_from_the_panel(tmp_path):
    c, google, gm = make(tmp_path)
    connect_via(c, "/admin/api/gmail/setup")
    google.account = "billing@kabala.app"  # a sending account that isn't an admin is fine from inside the panel
    page, _ = connect_via(c, "/admin/api/gmail/connect")
    assert page.status_code == 200
    assert c.get("/admin/api/gmail").json()["email"] == "billing@kabala.app" and google.revoked == ["1//rt1"]
    r = c.post("/admin/api/gmail/test", headers=H)
    assert r.json() == {"ok": True, "to": OWNER}
    assert google.sessions["1//rt2"].sent()["From"] == "Kabala <billing@kabala.app>"
    st = c.post("/admin/api/gmail/disconnect", headers=H).json()
    assert not st["connected"] and google.revoked == ["1//rt1", "1//rt2"]
    actions = [a["action"] for a in c.get("/admin/api/audit").json()["entries"]]
    assert {"gmail_connect", "gmail_test", "gmail_disconnect", "login_google"} <= set(actions)


def test_only_owners_connect_or_disconnect(tmp_path):
    c, google, gm = make(tmp_path)
    connect_via(c, "/admin/api/gmail/setup")
    c.post("/admin/api/admins", headers=H, json={"email": VIEWER, "role": "viewer"})
    v = TestClient(c.app)
    assert v.post("/admin/api/login/start", json={"email": VIEWER}).status_code == 200
    code = google.sessions["1//rt1"].sent()["Subject"].split(": ")[-1]  # the sign-in code, sent through Gmail
    assert v.post("/admin/api/login/verify", json={"email": VIEWER, "code": code}).status_code == 200
    assert v.get("/admin/api/gmail").json()["connected"]
    assert v.post("/admin/api/gmail/connect", headers=H).status_code == 403
    assert v.post("/admin/api/gmail/disconnect", headers=H).status_code == 403
    assert v.post("/admin/api/gmail/test", headers=H).status_code == 403


def test_refresh_token_from_the_environment(tmp_path):
    c, google, gm = make(tmp_path, gmail_refresh_token="1//envtok", mail_from="Kabala <me@gmail.com>")
    st = c.get("/admin/api/mail-status").json()
    assert st["ready"]
    gm.send("a@example.com", "s", "t")
    assert google.sessions["1//envtok"].sent()["From"] == "Kabala <me@gmail.com>"


def test_refresh_token_without_its_client_is_refused(tmp_path):
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "g.db"), gmail_refresh_token="1//x")
    with pytest.raises(RuntimeError, match="GMAIL_CLIENT_ID"):
        create_app(cfg, lambda t: {}, receipt_worker=False)


def test_sender_name():
    assert gmail_api.sender_name("Kabala") == "Kabala"
    assert gmail_api.sender_name("Kabala Billing <x@y.z>") == "Kabala Billing"
    assert gmail_api.sender_name("x@y.z") == "Kabala"


def test_the_paypal_server_gets_the_same_account(tmp_path):
    billing = Billing()
    c, google, gm = make(tmp_path, billing=billing)
    page, _ = connect_via(c, "/admin/api/gmail/setup")
    assert "גם משרת ה-PayPal" in page.text
    path, actor, role, body = billing.calls[0]
    assert (path, actor, role) == ("/api/admin/gmail", OWNER, "owner")
    assert body == {"email": OWNER, "clientId": "gid.apps.googleusercontent.com", "clientSecret": "GOCSPX-s", "refreshToken": "1//rt1"}
    pp = c.get("/admin/api/gmail").json()["paypal"]
    assert pp["reachable"] and pp["sameAccount"] and pp["sync"]["ok"]
    assert c.post("/admin/api/gmail/test-paypal", headers=H).json() == {"ok": True, "to": OWNER}
    # switching the account updates it there too; disconnecting tells it to forget it
    google.account = "billing@kabala.app"
    connect_via(c, "/admin/api/gmail/connect")
    assert billing.account["email"] == "billing@kabala.app" and billing.account["refreshToken"] == "1//rt2"
    c.post("/admin/api/gmail/disconnect", headers=H)
    assert billing.account is None and billing.calls[-1][0] == "/api/admin/gmail/disconnect"


def test_paypal_server_down_then_synced_by_hand(tmp_path):
    billing = Billing()
    billing.down = True
    c, google, gm = make(tmp_path, billing=billing)
    page, _ = connect_via(c, "/admin/api/gmail/setup")
    assert page.status_code == 200 and "שרת ה-PayPal לא עודכן" in page.text  # Gmail itself is connected
    pp = c.get("/admin/api/gmail").json()["paypal"]
    assert pp == {"reachable": False, "error": "billing server unreachable", "sync": {"ok": False, "error": pp["sync"]["error"], "at": pp["sync"]["at"]}}
    assert c.post("/admin/api/gmail/sync-paypal", headers=H).status_code == 502
    billing.down = False
    r = c.post("/admin/api/gmail/sync-paypal", headers=H).json()
    assert r["ok"] and r["paypal"]["sameAccount"] and billing.account["refreshToken"] == "1//rt1"
