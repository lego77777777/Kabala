import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server.admin import csv_cell, mask  # noqa: E402
from kabala_server.app import create_app  # noqa: E402
from kabala_server.config import Config  # noqa: E402

OWNER = "owner@kabala.app"
H = {"X-Kabala-Admin": "1"}


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, text):
        self.sent.append((to, text))

    def code_for(self, to):
        return re.search(r"\b(\d{6})\b", [t for (a, t) in self.sent if a == to][-1]).group(1)


@pytest.fixture
def env(tmp_path):
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "s.db"), owner_emails=(OWNER,))
    mailer = FakeMailer()
    app = create_app(cfg, lambda tok: {"email": tok, "email_verified": True}, mailer=mailer)
    return app, mailer


def client(env):
    return TestClient(env[0])


def sign_in(env, email=OWNER):
    c = client(env)
    assert c.post("/admin/api/login/start", json={"email": email}).json() == {"ok": True}
    r = c.post("/admin/api/login/verify", json={"email": email, "code": env[1].code_for(email)})
    assert r.status_code == 200, r.text
    return c


def add_users(env, *emails):
    c = client(env)
    for e in emails:
        assert c.post("/api/session", json={"id_token": e}).status_code == 200


def test_page_and_static_files_are_served_with_strict_headers(env):
    c = client(env)
    r = c.get("/admin")
    assert r.status_code == 200 and "פאנל הניהול" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert c.get("/admin/static/admin.js").status_code == 200
    assert c.get("/admin/static/..%2Fadmin.py").status_code == 404


def test_owner_signs_in_with_an_emailed_code(env):
    c = sign_in(env)
    assert c.get("/admin/api/me").json() == {"email": OWNER, "role": "owner"}
    cookie = c.cookies.get("kabala_admin")
    assert OWNER not in cookie  # encrypted
    assert "httponly" in c.post("/admin/api/login/verify", json={"email": OWNER, "code": "0"}).headers.get("set-cookie", "httponly")


def test_unknown_address_gets_the_same_answer_and_no_email(env):
    c = client(env)
    assert c.post("/admin/api/login/start", json={"email": "stranger@example.com"}).json() == {"ok": True}
    assert env[1].sent == []
    assert c.post("/admin/api/login/verify", json={"email": "stranger@example.com", "code": "123456"}).status_code == 401


def test_a_user_code_does_not_open_the_admin_panel(env):
    c = client(env)
    c.post("/api/email/start", json={"email": OWNER})
    assert c.post("/admin/api/login/verify", json={"email": OWNER, "code": env[1].code_for(OWNER)}).status_code == 401


def test_everything_needs_a_session(env):
    c = client(env)
    for path in ("/admin/api/me", "/admin/api/stats", "/admin/api/users", "/admin/api/admins", "/admin/api/audit", "/admin/api/users.csv"):
        assert c.get(path).status_code == 401, path


def test_post_without_the_admin_header_is_refused(env):
    c = sign_in(env)
    assert c.post("/admin/api/admins", json={"email": "x@y.co", "role": "viewer"}).status_code == 403
    assert c.post("/admin/api/admins", json={"email": "x@y.co", "role": "viewer"}, headers=H).status_code == 200


def test_stats_and_user_list(env):
    add_users(env, "dana@gmail.com", "yossi@walla.co.il", "noa@outlook.com")
    c = sign_in(env)
    s = c.get("/admin/api/stats").json()
    assert s["users"] == 3 and s["free"] == 3 and s["new_week"] == 3 and len(s["signups"]) == 30 and s["signups"][-1]["count"] == 3
    d = c.get("/admin/api/users", params={"q": "walla"}).json()
    assert d["total"] == 1 and d["users"][0]["email"] == "yossi@walla.co.il"
    assert c.get("/admin/api/users", params={"q": "%"}).json()["total"] == 0  # LIKE wildcards are escaped


def test_viewer_sees_masked_emails_and_cannot_change_anything(env):
    add_users(env, "dana.levi@gmail.com")
    owner = sign_in(env)
    owner.post("/admin/api/admins", json={"email": "view@kabala.app", "role": "viewer"}, headers=H)
    v = sign_in(env, "view@kabala.app")
    users = v.get("/admin/api/users", params={"q": "dana"}).json()["users"]
    assert [u["email"] for u in users] == ["da***@gmail.com"]  # and the search was ignored
    uid = users[0]["id"]
    assert v.get("/admin/api/users.csv").status_code == 403
    assert v.post(f"/admin/api/users/{uid}/comp", json={"on": True}, headers=H).status_code == 403
    assert v.get("/admin/api/admins").status_code == 403
    assert v.get("/admin/api/audit").status_code == 403


def test_admin_can_comp_and_export_but_not_manage_admins(env):
    add_users(env, "dana@gmail.com", "=cmd|' /C calc'!A0@evil.com")
    owner = sign_in(env)
    owner.post("/admin/api/admins", json={"email": "ops@kabala.app", "role": "admin"}, headers=H)
    a = sign_in(env, "ops@kabala.app")
    uid = next(u["id"] for u in a.get("/admin/api/users").json()["users"] if u["email"] == "dana@gmail.com")
    r = a.post(f"/admin/api/users/{uid}/comp", json={"on": True}, headers=H)
    assert r.json()["plan"] == "pro" and r.json()["status"] == "comp"
    assert a.post(f"/admin/api/users/{uid}/comp", json={"on": False}, headers=H).json()["plan"] == "free"
    text = a.get("/admin/api/users.csv").text
    assert "dana@gmail.com" in text and "\n'=cmd" in text  # formula neutralised
    assert a.get("/admin/api/admins").status_code == 403
    assert a.post("/admin/api/admins", json={"email": "x@y.co", "role": "owner"}, headers=H).status_code == 403


def test_comp_never_overrides_a_stripe_subscription(tmp_path):
    import json
    from types import SimpleNamespace as ns

    class FakeStripe:
        v1 = ns(subscriptions=ns(retrieve=lambda sid: {"id": sid, "status": "active", "current_period_end": 1900000000}))

        def construct_event(self, payload, sig, secret):
            return json.loads(payload)

    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "s.db"), owner_emails=(OWNER,),
                 stripe_secret_key="sk_test", stripe_price_pro="price", stripe_webhook_secret="whsec")
    mailer = FakeMailer()
    app_env = (create_app(cfg, lambda tok: {"email": tok, "email_verified": True}, FakeStripe(), mailer), mailer)
    add_users(app_env, "payer@gmail.com")
    owner = sign_in(app_env)
    uid = owner.get("/admin/api/users").json()["users"][0]["id"]
    event = {"id": "evt_1", "type": "checkout.session.completed",
             "data": {"object": {"client_reference_id": str(uid), "customer": "cus_1", "subscription": "sub_1"}}}
    assert client(app_env).post("/api/stripe/webhook", content=json.dumps(event), headers={"Stripe-Signature": "x"}).status_code == 200
    assert owner.post(f"/admin/api/users/{uid}/comp", json={"on": False}, headers=H).status_code == 409
    paid = owner.get("/admin/api/users", params={"paid": True}).json()["users"]
    assert [(u["email"], u["status"], u["stripe_customer"]) for u in paid] == [("payer@gmail.com", "active", "cus_1")]


def test_removed_admin_loses_access_at_once_and_last_owner_stays(env):
    owner = sign_in(env)
    owner.post("/admin/api/admins", json={"email": "ops@kabala.app", "role": "admin"}, headers=H)
    ops = sign_in(env, "ops@kabala.app")
    assert ops.get("/admin/api/me").status_code == 200
    owner.post("/admin/api/admins/remove", json={"email": "ops@kabala.app"}, headers=H)
    assert ops.get("/admin/api/me").status_code == 401
    assert owner.post("/admin/api/admins/remove", json={"email": OWNER}, headers=H).status_code == 409
    assert owner.post("/admin/api/admins", json={"email": OWNER, "role": "admin"}, headers=H).status_code == 409


def test_everything_is_audited(env):
    owner = sign_in(env)
    client(env).post("/admin/api/login/verify", json={"email": OWNER, "code": "000000"})
    owner.post("/admin/api/admins", json={"email": "ops@kabala.app", "role": "viewer"}, headers=H)
    owner.get("/admin/api/users.csv")
    actions = [e["action"] for e in owner.get("/admin/api/audit").json()["entries"]]
    assert actions[:4] == ["export_csv", "admin_set:viewer", "login_failed", "login"]


def test_security_page_lists_and_lifts_bans(env):
    bad = client(env)
    for _ in range(10):
        bad.post("/admin/api/login/verify", json={"email": OWNER, "code": "1"})
    assert bad.get("/admin").status_code == 403  # banned by the Shield
    owner_client = TestClient(env[0], client=("10.0.0.9", 1234))
    owner_client.post("/admin/api/login/start", json={"email": OWNER})
    owner_client.post("/admin/api/login/verify", json={"email": OWNER, "code": env[1].code_for(OWNER)})
    bans = owner_client.get("/admin/api/security").json()["bans"]
    assert [b["ip"] for b in bans] == ["testclient"]
    assert owner_client.post("/admin/api/security/unban", json={"ip": "testclient"}, headers=H).json()["bans"] == []
    assert bad.get("/admin").status_code == 200


def test_helpers():
    assert mask("dana.levi@gmail.com") == "da***@gmail.com"
    assert csv_cell("=1+1") == "'=1+1" and csv_cell("+972") == "'+972" and csv_cell("dana") == "dana" and csv_cell(None) == ""


# ---- PayPal billing server ------------------------------------------------------------------------

KEY_ID = "0b9e1c3a-1111-4222-8333-944445555666"


class FakeBilling:
    """Plays the billing server: checks the panel's JWT exactly like src/admin/admin-api.ts, then answers."""

    def __init__(self, public_key, down=False):
        self.public_key, self.down, self.claims, self.bodies = public_key, down, [], []

    def receipts(self, method, path, data):
        import json
        from types import SimpleNamespace as ns

        rid = "11111111-2222-4333-8444-555555555555"
        if path.endswith("/pdf"):
            return ns(status_code=200, content=b"%PDF-1.7 fake", headers={"content-type": "application/pdf"})
        if method == "POST" and path.endswith("/send"):
            to = json.loads(data).get("to")
            if to and "," in to:
                return ns(status_code=400, headers={"content-type": "application/json"}, json=lambda: {"error": "invalid_request"})
            return ns(status_code=200, headers={"content-type": "application/json"}, json=lambda: {"ok": True})
        row = {"id": rid, "email": "dana.levi@gmail.com", "status": "sent", "number": "000001",
               "lastEmail": {"status": "sent", "trigger": "auto", "to": "dana.levi@gmail.com, cpa@office.co.il"}}
        body = {"receipts": [row], "total": 1, "page": 1, "pages": 1} if path == "/api/admin/receipts" else {
            **row, "emails": [{"status": "sent", "trigger": "auto", "to": "dana.levi@gmail.com, cpa@office.co.il"}]}
        return ns(status_code=200, headers={"content-type": "application/json"}, json=lambda: body)

    def request(self, method, url, headers=None, data=None, timeout=None):
        import requests
        from types import SimpleNamespace as ns
        from urllib.parse import urlparse

        from kabala_server.vendor import jwt

        if self.down:
            raise requests.ConnectionError()
        claims = jwt.decode(headers["Authorization"].removeprefix("Bearer "), self.public_key, algorithms=["EdDSA"],
                            audience="kabala-billing", issuer="kabala-admin")
        assert claims["htm"] == method and claims["htu"] == urlparse(url).path and claims["exp"] - claims["iat"] == 60
        if data is not None:  # the token is bound to this exact body
            import base64
            import hashlib

            assert claims["bh"] == base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        else:
            assert "bh" not in claims
        self.claims.append(claims)
        self.bodies.append(data)
        path = urlparse(url).path
        if path.startswith("/api/admin/receipts"):
            return self.receipts(method, path, data)
        if method == "POST" and path.endswith("/revoke"):
            return ns(status_code=200 if KEY_ID in path else 404, json=lambda: {"ok": True})
        if path == "/api/admin/keys":
            body = {"keys": [{"id": KEY_ID, "email": "dana.levi@gmail.com", "hint": "KBL-…-7QX2M", "status": "active"}],
                    "total": 1, "page": 1, "pages": 1}
        else:
            body = {"subscribers": [{"email": "dana.levi@gmail.com", "status": "active"}], "total": 1, "page": 1, "pages": 1}
        return ns(status_code=200, json=lambda: body)


def paypal_env(tmp_path, down=False):
    from kabala_server.admin_billing import BillingClient, keygen

    private, public = keygen()
    from cryptography.hazmat.primitives import serialization

    fake = FakeBilling(serialization.load_pem_public_key(public.encode()), down)
    client_ = BillingClient("https://billing.kabala.test", private, http=fake)
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "s.db"), owner_emails=(OWNER,))
    mailer = FakeMailer()
    return (create_app(cfg, lambda tok: {}, mailer=mailer, billing_client=client_), mailer), fake


def test_paypal_requests_are_signed_per_admin_and_role(tmp_path):
    env_, fake = paypal_env(tmp_path)
    owner = sign_in(env_)
    assert owner.get("/admin/api/paypal/status").json() == {"connected": True}
    subs = owner.get("/admin/api/paypal/subscribers", params={"q": "dana"}).json()["subscribers"]
    assert subs[0]["email"] == "dana.levi@gmail.com"
    assert fake.claims[-1]["sub"] == OWNER and fake.claims[-1]["role"] == "owner"
    assert len({c["jti"] for c in fake.claims}) == len(fake.claims)


def test_paypal_viewer_gets_masked_emails_even_if_billing_does_not_mask(tmp_path):
    env_, fake = paypal_env(tmp_path)
    sign_in(env_).post("/admin/api/admins", json={"email": "view@kabala.app", "role": "viewer"}, headers=H)
    v = sign_in(env_, "view@kabala.app")
    assert v.get("/admin/api/paypal/subscribers").json()["subscribers"][0]["email"] == "da***@gmail.com"
    assert fake.claims[-1]["role"] == "viewer"


def test_paypal_not_connected_or_down(tmp_path, env):
    assert sign_in(env).get("/admin/api/paypal/status").json() == {"connected": False}
    assert sign_in(env).get("/admin/api/paypal/stats").status_code == 404
    env_, _ = paypal_env(tmp_path, down=True)
    assert sign_in(env_).get("/admin/api/paypal/stats").status_code == 502


def test_billing_url_must_be_https():
    from kabala_server.admin_billing import BillingClient, keygen

    with pytest.raises(RuntimeError):
        BillingClient("http://billing.example.com", keygen()[0])
    BillingClient("http://localhost:8080", keygen()[0])


def test_paypal_license_keys_list_and_revoke(tmp_path):
    env_, fake = paypal_env(tmp_path)
    owner = sign_in(env_)
    keys = owner.get("/admin/api/paypal/keys").json()["keys"]
    assert keys[0]["hint"] == "KBL-…-7QX2M" and keys[0]["email"] == "dana.levi@gmail.com"
    owner.post("/admin/api/admins", json={"email": "view@kabala.app", "role": "viewer"}, headers=H)
    v = sign_in(env_, "view@kabala.app")
    assert v.get("/admin/api/paypal/keys").json()["keys"][0]["email"] == "da***@gmail.com"
    assert v.post(f"/admin/api/paypal/keys/{KEY_ID}/revoke", headers=H).status_code == 403
    assert owner.post(f"/admin/api/paypal/keys/{KEY_ID}/revoke", headers=H).json() == {"ok": True}
    assert fake.claims[-1]["htm"] == "POST" and fake.claims[-1]["htu"].endswith("/revoke")
    assert owner.post("/admin/api/paypal/keys/../../admins/revoke", headers=H).status_code in (400, 404)
    assert owner.post("/admin/api/paypal/keys/00000000-0000-4000-8000-000000000000/revoke", headers=H).status_code == 404
    assert "key_revoked" in [e["action"] for e in owner.get("/admin/api/audit").json()["entries"]]


RID = "11111111-2222-4333-8444-555555555555"


def test_paypal_receipts_list_send_and_download(tmp_path):
    env_, fake = paypal_env(tmp_path)
    owner = sign_in(env_)
    r = owner.get("/admin/api/paypal/receipts").json()["receipts"][0]
    assert r["email"] == "dana.levi@gmail.com" and r["lastEmail"]["to"] == "dana.levi@gmail.com, cpa@office.co.il"
    assert owner.post(f"/admin/api/paypal/receipts/{RID}/send", json={}, headers=H).json() == {"ok": True}
    assert fake.bodies[-1] == b"{}"
    assert owner.post(f"/admin/api/paypal/receipts/{RID}/send", json={"to": "cpa@office.co.il"}, headers=H).json() == {"ok": True}
    assert fake.bodies[-1] == b'{"to":"cpa@office.co.il"}'
    assert owner.post(f"/admin/api/paypal/receipts/{RID}/send", json={"to": "a@b.co,evil@x.com"}, headers=H).status_code == 409
    pdf = owner.get(f"/admin/api/paypal/receipts/{RID}/pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert owner.post("/admin/api/paypal/receipts/not-an-id/send", json={}, headers=H).status_code == 400
    actions = [e["action"] for e in owner.get("/admin/api/audit").json()["entries"]]
    assert actions[:3] == ["receipt_downloaded", "receipt_sent", "receipt_sent"]


def test_paypal_receipts_viewer_masked_and_read_only(tmp_path):
    env_, fake = paypal_env(tmp_path)
    sign_in(env_).post("/admin/api/admins", json={"email": "view@kabala.app", "role": "viewer"}, headers=H)
    v = sign_in(env_, "view@kabala.app")
    r = v.get("/admin/api/paypal/receipts").json()["receipts"][0]
    assert r["email"] == "da***@gmail.com" and r["lastEmail"]["to"] == "da***@gmail.com, cp***@office.co.il"
    detail = v.get(f"/admin/api/paypal/receipts/{RID}").json()
    assert detail["email"] == "da***@gmail.com" and detail["emails"][0]["to"] == "da***@gmail.com, cp***@office.co.il"
    assert v.post(f"/admin/api/paypal/receipts/{RID}/send", json={}, headers=H).status_code == 403
    assert v.get(f"/admin/api/paypal/receipts/{RID}/pdf").status_code == 403
