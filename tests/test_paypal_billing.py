"""Desktop side of PayPal billing: offline license verification, server client, deep links."""

import base64
import json
import time

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kabala import licensing, paypal_billing
from kabala.deeplink import link_from_argv, register_protocol
from kabala.licensing import FREE, LicenseError
from kabala.paypal_billing import PayPalLicenseClient, verify_license


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Signer:
    """Signs license JWTs exactly like the Node server (EdDSA / Ed25519)."""

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.public_pem = self.key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()

    def token(self, plan="pro", exp_in=7 * 86400, **over):
        header = b64(json.dumps({"alg": "EdDSA", "typ": "JWT"}).encode())
        now = int(time.time())
        claims = {
            "sub": "u1", "email": "dana@gmail.com", "plan": plan, "iss": "kabala-billing", "aud": "license",
            "iat": now, "exp": now + exp_in, "status": "active", "paidUntil": "2026-11-21T10:00:00.000Z",
            "limits": {"monthlyReceipts": None if plan == "pro" else 50, "mailAccounts": None, "export": plan == "pro", "backgroundScan": plan == "pro"},
            **over,
        }
        payload = b64(json.dumps(claims).encode())
        sig = b64(self.key.sign(f"{header}.{payload}".encode()))
        return f"{header}.{payload}.{sig}"


@pytest.fixture(autouse=True)
def vault(monkeypatch):
    store = {}
    get = lambda user: store.get(user)  # noqa: E731
    put = lambda user, value: store.pop(user, None) if value is None else store.__setitem__(user, value)  # noqa: E731
    monkeypatch.setattr(licensing, "_kr_get", get)
    monkeypatch.setattr(licensing, "_kr_set", put)
    monkeypatch.setattr(paypal_billing, "_kr_get", get)
    monkeypatch.setattr(paypal_billing, "_kr_set", put)
    return store


def test_valid_license_verifies_offline():
    s = Signer()
    claims = verify_license(s.token(), s.public_pem)
    plan = paypal_billing.plan_from_claims(claims)
    assert plan.is_pro and plan.monthly_receipts is None and plan.export and plan.background_scan
    assert plan.current_period_end is not None


@pytest.mark.parametrize("case", ["expired", "tampered", "other_key", "wrong_aud"])
def test_bad_licenses_rejected(case):
    s = Signer()
    if case == "expired":
        tok = s.token(exp_in=-1)
    elif case == "tampered":
        h, p, sig = s.token(plan="free").split(".")
        claims = json.loads(base64.urlsafe_b64decode(p + "=="))
        claims["plan"] = "pro"  # try to upgrade yourself
        tok = ".".join([h, b64(json.dumps(claims).encode()), sig])
    elif case == "other_key":
        tok = Signer().token()
    else:
        tok = s.token(aud="session")
    with pytest.raises(LicenseError):
        verify_license(tok, s.public_pem)


class Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class FakeServer:
    def __init__(self, signer):
        self.signer = signer
        self.offline = False
        self.plan = "free"

    def post(self, url, json=None, headers=None, timeout=None):
        if self.offline:
            raise requests.ConnectionError()
        if url.endswith("/api/session"):
            return Resp(200, {"session": "S1", "license": self.signer.token(self.plan)})
        if url.endswith("/api/links"):
            return Resp(200, {"url": f"https://billing.test/{json['page']}#t=abc"})
        return Resp(404)

    def get(self, url, headers=None, timeout=None):
        if self.offline:
            raise requests.ConnectionError()
        if url.endswith("/api/pricing"):
            return Resp(200, {"currency": "ILS", "tiers": {"pro": {"prices": {"monthly": {"amount": "29.00"}}}}})
        if url.endswith("/api/license"):
            if (headers or {}).get("Authorization") != "Bearer S1":
                return Resp(401)
            return Resp(200, {"license": self.signer.token(self.plan), "publicKey": self.signer.public_pem})
        return Resp(404)


def test_client_flow_upgrade_and_offline(vault):
    s = Signer()
    server = FakeServer(s)
    client = PayPalLicenseClient("https://billing.test", server, public_key_pem=s.public_pem)
    assert not client.login("google-id-token").is_pro
    assert client.checkout_url() == "https://billing.test/checkout#t=abc"
    assert client.portal_url() == "https://billing.test/account#t=abc"
    assert client.pricing()["tiers"]["pro"]["prices"]["monthly"]["amount"] == "29.00"

    server.plan = "pro"  # PayPal webhook / checkout confirm upgraded the account on the server
    assert client.status().is_pro

    server.offline = True  # no internet: the stored, signed license keeps Pro working
    assert client.status().is_pro

    vault["paypal_license"] = s.token(exp_in=-10)  # …but only until it expires
    assert client.status() == FREE


def test_forged_server_response_does_not_grant_pro(vault):
    real = Signer()
    attacker = Signer()
    server = FakeServer(attacker)  # e.g. a fake server on the network
    client = PayPalLicenseClient("https://billing.test", server, public_key_pem=real.public_pem)
    vault["paypal_session"] = "S1"
    server.plan = "pro"
    assert client.status() == FREE


def test_deep_link_parsing_and_protocol_registration(tmp_path, qapp):
    from PySide6.QtCore import QSettings

    assert link_from_argv(["kabala.exe", "kabala://payment-success?status=pro"]) == "kabala://payment-success?status=pro"
    assert link_from_argv(["kabala.exe", "--background"]) is None
    s = QSettings(str(tmp_path / "reg.ini"), QSettings.Format.IniFormat)
    register_protocol(s)
    assert s.value("URL Protocol") == ""
    assert '"%1"' in s.value("shell/open/command/Default")


def test_single_instance_forwards_deep_link(qapp):
    """The browser launches a second Kabala process with the link; it must reach the running one."""
    import subprocess
    import sys
    from pathlib import Path

    from kabala.deeplink import SingleInstance

    first = SingleInstance(name="kabala-test-instance")
    assert not first.secondary
    received = []
    first.message.connect(received.append)
    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from PySide6.QtCore import QCoreApplication; app = QCoreApplication([]);"
        "from kabala.deeplink import SingleInstance; s = SingleInstance(name='kabala-test-instance');"
        "sys.exit(0 if s.secondary and s.send('kabala://payment-success?status=pro') else 1)" % root
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.time() + 15
    while (proc.poll() is None or not received) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert proc.wait(5) == 0
    assert received == ["kabala://payment-success?status=pro"]


def test_upgrade_needs_a_real_account_and_deep_link_starts_checkout(tmp_path, qapp, monkeypatch):
    from kabala.backend import Backend
    from kabala.store import Store

    monkeypatch.setenv("KABALA_SERVER_URL", "https://billing.test")
    monkeypatch.setenv("KABALA_BILLING_PROVIDER", "paypal")
    b = Backend(store=Store(tmp_path / "k.db"))
    errors, opened = [], []
    b.billing_failed.connect(errors.append)
    b.open_url.connect(opened.append)

    b.connect_demo()
    b.start_checkout()
    b.handle_deep_link("kabala://upgrade")  # the website's "Upgrade" button
    assert errors == ["sign_in", "sign_in"] and opened == []


def test_server_config_accepts_bom(tmp_path, monkeypatch):
    import kabala.settings

    monkeypatch.delenv("KABALA_SERVER_URL", raising=False)
    monkeypatch.setattr(kabala.settings, "config_dir", lambda: tmp_path)
    (tmp_path / licensing.SERVER_FILE).write_bytes(b'\xef\xbb\xbf{"url": "http://localhost:8080/", "provider": "paypal"}')
    assert licensing.server_config() == {"url": "http://localhost:8080", "provider": "paypal"}


@pytest.mark.parametrize(("url", "ok"), [
    ("https://billing.kabala.app", True),
    ("http://localhost:8080", True),
    ("http://127.0.0.1:8080", True),
    ("http://billing.kabala.app", False),  # payments and licenses never over plain http
    ("http://192.168.1.5:8080", False),
])
def test_plan_server_must_be_https(url, ok, monkeypatch):
    monkeypatch.setenv("KABALA_SERVER_URL", url)
    monkeypatch.setenv("KABALA_BILLING_PROVIDER", "paypal")
    assert (licensing.server_config() is not None) is ok
