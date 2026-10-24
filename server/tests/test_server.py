import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server import tokens  # noqa: E402
from kabala_server.app import create_app  # noqa: E402
from kabala_server.config import Config  # noqa: E402

SECRET = "x" * 40


class FakeStripe:
    """Mimics the parts of stripe.StripeClient the server uses."""

    def __init__(self):
        self.created = []
        self.events = {}
        from types import SimpleNamespace as ns
        self.v1 = ns(
            checkout=ns(sessions=ns(create=self._checkout)),
            billing_portal=ns(sessions=ns(create=lambda params: {"url": f"https://billing.stripe.test/{params['customer']}"})),
            subscriptions=ns(retrieve=lambda sid: {"id": sid, "status": "active", "items": {"data": [{"current_period_end": 1900000000}]}}),
        )

    def _checkout(self, params):
        self.created.append(params)
        return {"url": "https://checkout.stripe.test/s1"}

    def construct_event(self, payload, sig, secret):
        if sig != "good":
            raise ValueError("bad signature")
        import json

        return json.loads(payload)


@pytest.fixture
def env(tmp_path):
    cfg = Config(secret=SECRET, google_client_id="cid", stripe_secret_key="sk_test", stripe_price_pro="price_pro",
                 stripe_webhook_secret="whsec", db_path=str(tmp_path / "s.db"))

    def verify(tok):
        if tok == "bad":
            raise ValueError("bad token")
        return {"email": tok, "email_verified": True}

    stripe = FakeStripe()
    return TestClient(create_app(cfg, verify, stripe)), stripe


def login(client, email="dana@gmail.com"):
    r = client.post("/api/session", json={"id_token": email})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()


def webhook(client, event, sig="good"):
    import json

    return client.post("/api/stripe/webhook", content=json.dumps(event), headers={"Stripe-Signature": sig})


def test_new_user_starts_free_with_limits(env):
    client, _ = env
    auth, body = login(client)
    assert body["plan"] == "free" and body["limits"]["monthly_receipts"] == 50 and body["limits"]["export"] is False
    assert client.get("/api/me", headers=auth).json()["email"] == "dana@gmail.com"


def test_rejects_bad_google_token_and_bad_session(env):
    client, _ = env
    assert client.post("/api/session", json={"id_token": "bad"}).status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    forged = tokens.issue("y" * 40, 1, "dana@gmail.com", 30)
    assert client.get("/api/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_expired_session_rejected():
    tok = tokens.issue(SECRET, 1, "a@b.c", days=1, now=0)
    assert tokens.verify(SECRET, tok) is None


def test_checkout_then_webhooks_upgrade_and_downgrade(env):
    client, stripe = env
    auth, _ = login(client)
    r = client.post("/api/checkout", headers=auth)
    assert r.json()["url"].startswith("https://checkout.stripe.test")
    params = stripe.created[0]
    assert params["mode"] == "subscription" and params["customer_email"] == "dana@gmail.com"
    uid = params["client_reference_id"]

    # payment done
    assert webhook(client, {"id": "evt_1", "type": "checkout.session.completed",
                            "data": {"object": {"client_reference_id": uid, "customer": "cus_1", "subscription": "sub_1"}}}).status_code == 200
    me = client.get("/api/me", headers=auth).json()
    assert me["plan"] == "pro" and me["limits"]["monthly_receipts"] is None and me["current_period_end"] == 1900000000

    # duplicate delivery is ignored
    assert webhook(client, {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {}}}).json()["duplicate"]

    # portal works once a customer exists
    assert client.post("/api/portal", headers=auth).json()["url"].endswith("cus_1")
    assert client.post("/api/checkout", headers=auth).status_code == 409

    # canceled at period end → back to free
    webhook(client, {"id": "evt_2", "type": "customer.subscription.deleted",
                     "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled"}}})
    assert client.get("/api/me", headers=auth).json()["plan"] == "free"


def test_webhook_signature_required(env):
    client, _ = env
    assert webhook(client, {"id": "evt_x", "type": "x", "data": {"object": {}}}, sig="forged").status_code == 400


def test_version_endpoint(env):
    client, _ = env
    assert client.get("/api/version").json()["latest"]
