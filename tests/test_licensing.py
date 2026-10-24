import pytest
import requests

from kabala import licensing
from kabala.licensing import FREE, LicenseClient, Plan


class Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class FakeHTTP:
    def __init__(self):
        self.plan = "free"
        self.offline = False
        self.calls = []

    def _payload(self):
        pro = self.plan == "pro"
        return {"email": "dana@gmail.com", "plan": self.plan, "status": "active" if pro else None, "current_period_end": None,
                "limits": {"monthly_receipts": None if pro else 50, "export": pro, "background_scan": pro}}

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url))
        if self.offline:
            raise requests.ConnectionError()
        if url.endswith("/api/session"):
            return Resp(200, {"token": "tok-1", **self._payload()}) if json["id_token"] == "good" else Resp(401)
        if url.endswith("/api/checkout"):
            return Resp(200, {"url": "https://checkout.stripe.test/s"}) if headers else Resp(401)
        return Resp(404)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        if self.offline:
            raise requests.ConnectionError()
        if headers and headers.get("Authorization") == "Bearer tok-1":
            return Resp(200, self._payload())
        return Resp(401)


@pytest.fixture(autouse=True)
def fake_vault(monkeypatch):
    vault = {}
    monkeypatch.setattr(licensing, "_kr_get", lambda user: vault.get(user))
    monkeypatch.setattr(licensing, "_kr_set", lambda user, value: vault.pop(user, None) if value is None else vault.__setitem__(user, value))
    return vault


def test_login_then_status_and_upgrade():
    http = FakeHTTP()
    client = LicenseClient("https://api.kabala.test", http)
    plan = client.login("good")
    assert plan.managed and plan.plan == "free" and plan.monthly_receipts == 50 and not plan.export
    assert client.checkout_url().startswith("https://checkout.stripe.test")

    http.plan = "pro"  # Stripe webhook upgraded the account
    plan = client.status()
    assert plan.is_pro and plan.monthly_receipts is None and plan.export and plan.background_scan


def test_rejected_google_token():
    client = LicenseClient("https://api.kabala.test", FakeHTTP())
    with pytest.raises(licensing.LicenseError):
        client.login("bad")


def test_offline_keeps_cached_plan_then_expired_session_falls_back_to_free(fake_vault):
    http = FakeHTTP()
    http.plan = "pro"
    client = LicenseClient("https://api.kabala.test", http)
    client.login("good")
    http.offline = True
    assert client.status().is_pro  # cached within the grace period
    http.offline = False
    fake_vault["license_session"] = "stale"
    assert client.status() == FREE  # server says 401 → signed out, Free
    assert client.token is None


def test_server_url_from_env(monkeypatch):
    monkeypatch.setenv("KABALA_SERVER_URL", "https://api.kabala.test/")
    assert licensing.server_url() == "https://api.kabala.test"


def test_plan_from_api_defaults():
    p = Plan.from_api({"plan": "free"})
    assert p.monthly_receipts == 50 and not p.export
