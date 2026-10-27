"""Sign-in by email code (server/kabala_server/email_login.py) from the desktop side."""

import threading

import pytest
import requests

from kabala import google_auth, imap_gmail, licensing
from kabala.licensing import LicenseClient, LicenseError

CODE = "482913"


class Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class FakeServer:
    """The two email endpoints plus /api/me and /api/checkout of the plan server."""

    def __init__(self, configured=True):
        self.configured = configured
        self.sent_to = []
        self.offline = False

    def _plan(self, email):
        return {"email": email, "plan": "pro", "status": "active", "current_period_end": None,
                "limits": {"monthly_receipts": None, "export": True, "background_scan": True}}

    def post(self, url, json=None, headers=None, timeout=None):
        if self.offline:
            raise requests.ConnectionError()
        if url.endswith("/api/email/start"):
            if not self.configured:
                return Resp(503)
            self.sent_to.append(json["email"])
            return Resp(200, {"ok": True, "expires_in": 600})
        if url.endswith("/api/email/verify"):
            if json["code"] != CODE or json["email"] not in self.sent_to:
                return Resp(401)
            return Resp(200, {"token": "tok-email", **self._plan(json["email"])})
        if url.endswith("/api/checkout"):
            return Resp(200, {"url": "https://checkout.stripe.test/s"}) if headers else Resp(401)
        return Resp(404)

    def get(self, url, headers=None, timeout=None):
        if headers and headers.get("Authorization") == "Bearer tok-email":
            return Resp(200, self._plan(self.sent_to[-1]))
        return Resp(401)


@pytest.fixture(autouse=True)
def fake_vault(monkeypatch):
    vault = {}
    monkeypatch.setattr(licensing, "_kr_get", lambda user: vault.get(user))
    monkeypatch.setattr(licensing, "_kr_set", lambda user, value: vault.pop(user, None) if value is None else vault.__setitem__(user, value))
    return vault


# ---- LicenseClient -----------------------------------------------------------------------------


def test_client_start_then_login_gives_the_plan_of_that_email():
    server = FakeServer()
    client = LicenseClient("https://api.kabala.test", server)
    client.email_start("dana@gmail.com")
    assert server.sent_to == ["dana@gmail.com"]
    plan = client.email_login("dana@gmail.com", CODE)
    assert plan.is_pro and plan.email == "dana@gmail.com"
    assert client.token == "tok-email" and client.status().is_pro


@pytest.mark.parametrize(("server", "call", "code"), [
    (FakeServer(), lambda c: c.email_login("dana@gmail.com", "000000"), "bad_code"),
    (FakeServer(configured=False), lambda c: c.email_start("dana@gmail.com"), "not_configured"),
])
def test_client_errors_map_to_codes(server, call, code):
    with pytest.raises(LicenseError) as e:
        call(LicenseClient("https://api.kabala.test", server))
    assert str(e.value) == code


def test_client_offline():
    server = FakeServer()
    server.offline = True
    with pytest.raises(LicenseError) as e:
        LicenseClient("https://api.kabala.test", server).email_start("dana@gmail.com")
    assert str(e.value) == "network"


# ---- Backend ------------------------------------------------------------------------------------


def wait(qapp, event, rounds=50):
    while not event.is_set() and rounds:
        qapp.processEvents()
        event.wait(0.1)
        rounds -= 1
    qapp.processEvents()
    return event.is_set()


@pytest.fixture
def imap_backend(tmp_path, monkeypatch, qapp):
    """A Backend whose plan server is FakeServer, with fake IMAP login."""
    from kabala.backend import Backend
    from kabala.store import Store

    monkeypatch.setattr(google_auth, "load_stored", lambda: None)
    monkeypatch.setattr(imap_gmail, "load_login", lambda: None)
    monkeypatch.setattr(imap_gmail, "save_login", lambda a, p: None)

    class FakeImap:
        def __init__(self, address, password):
            pass

        def close(self):
            pass

    monkeypatch.setattr(imap_gmail, "ImapGmailClient", FakeImap)

    def make(server):
        b = Backend(store=Store(tmp_path / "k.db"), license_url="https://api.kabala.test")
        b.license.http = server
        backends.append(b)
        return b

    backends = []
    before = set(threading.enumerate())
    yield make
    # A worker thread can hold the last reference to the Backend; if it is dropped there, Qt destroys the
    # QObject off the main thread and the next test crashes. Let the workers finish first.
    for t in set(threading.enumerate()) - before:
        while t.is_alive():
            qapp.processEvents()
            t.join(0.05)
    backends.clear()


def test_imap_connect_asks_for_the_emailed_code_then_shows_that_plan(qapp, imap_backend):
    server = FakeServer()
    b = imap_backend(server)
    asked, verified = threading.Event(), threading.Event()
    asked_for = []
    b.email_code_sent.connect(lambda email: (asked_for.append(email), asked.set()))
    b.email_verified.connect(verified.set)

    b.connect_imap("dana@gmail.com", "abcd efgh ijkl mnop")
    assert wait(qapp, asked)
    assert asked_for == ["dana@gmail.com"] and server.sent_to == ["dana@gmail.com"]

    b.submit_email_code(f" {CODE} ")
    assert wait(qapp, verified)
    assert b.plan.is_pro and b.plan.email == "dana@gmail.com"


def test_wrong_code_reports_an_error(qapp, imap_backend):
    server = FakeServer()
    b = imap_backend(server)
    asked, failed = threading.Event(), threading.Event()
    errors = []
    b.email_code_sent.connect(lambda _e: asked.set())
    b.email_code_failed.connect(lambda code: (errors.append(code), failed.set()))
    b.connect_imap("dana@gmail.com", "abcd efgh ijkl mnop")
    assert wait(qapp, asked)
    b.submit_email_code("111111")
    assert wait(qapp, failed)
    assert errors == ["bad_code"] and not b.plan.is_pro


def test_upgrade_on_imap_waits_for_the_code_then_opens_checkout(qapp, imap_backend, monkeypatch):
    server = FakeServer()
    b = imap_backend(server)
    asked, opened = threading.Event(), threading.Event()
    urls = []
    b.email_code_sent.connect(lambda _e: asked.set())
    b.open_checkout.connect(lambda url: (urls.append(url), opened.set()))
    monkeypatch.setattr(google_auth, "identity_token", lambda cancel: pytest.fail("no browser sign-in when email works"))
    connected = threading.Event()
    b.connected.connect(lambda _a: connected.set())
    b.connect_imap("dana@gmail.com", "abcd efgh ijkl mnop")
    assert wait(qapp, connected) and wait(qapp, asked)
    b.cancel_email_code()  # user closed the first dialog ("Later")
    asked.clear()

    b.start_checkout()
    assert wait(qapp, asked)
    assert not urls  # nothing opens before the address is proven
    b.submit_email_code(CODE)
    assert wait(qapp, opened)
    assert urls == ["https://checkout.stripe.test/s"]


def test_server_without_email_falls_back_to_the_google_identity_sign_in(qapp, imap_backend, monkeypatch):
    server = FakeServer(configured=False)
    b = imap_backend(server)
    used = threading.Event()

    def identity(cancel):
        used.set()
        raise google_auth.AuthError("canceled")

    monkeypatch.setattr(google_auth, "identity_token", identity)
    asked = threading.Event()
    b.email_code_sent.connect(lambda _e: asked.set())
    connected = threading.Event()
    b.connected.connect(lambda _a: connected.set())
    b.connect_imap("dana@gmail.com", "abcd efgh ijkl mnop")
    assert wait(qapp, connected)
    b.start_checkout()
    assert wait(qapp, used)
    assert not asked.is_set()
