"""Signing out: the grant leaves the vault at once, is revoked at Google, and nothing keeps using it."""

import json
import threading

import keyring.errors
import pytest

from kabala import google_auth, imap_gmail
from kabala.models import Account


class FakeVault:
    def __init__(self):
        self.data = {}

    def get_password(self, service, user):
        return self.data.get((service, user))

    def set_password(self, service, user, value):
        self.data[(service, user)] = value

    def delete_password(self, service, user):
        if (service, user) not in self.data:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.data[(service, user)]


@pytest.fixture
def vault(monkeypatch):
    v = FakeVault()
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(google_auth.keyring, name, getattr(v, name))
    return v


def grant(token):
    return json.dumps({"refresh_token": token, "email": "dana@gmail.com", "scopes": [google_auth.GMAIL_SCOPE]})


def test_revoke_posts_the_token_to_google_and_reports_success(vault, monkeypatch):
    calls = []

    class Resp:
        status_code = 200

    monkeypatch.setattr(google_auth.requests, "post", lambda url, **kw: calls.append((url, kw)) or Resp())
    vault.set_password(google_auth.KEYRING_SERVICE, google_auth.KEYRING_USER, grant("rt-1"))
    assert google_auth.forget() is True
    assert calls[0][0] == google_auth.REVOKE_URI
    assert calls[0][1]["data"] == {"token": "rt-1"}  # in the body, never in the URL
    assert vault.data == {}


def test_offline_sign_out_still_deletes_the_local_grant(vault, monkeypatch):
    def offline(*_a, **_k):
        raise OSError("no network")

    monkeypatch.setattr(google_auth.requests, "post", offline)
    vault.set_password(google_auth.KEYRING_SERVICE, google_auth.KEYRING_USER, grant("rt-1"))
    assert google_auth.forget() is False
    assert vault.data == {}
    assert google_auth.take_stored_token() is None  # nothing left, and no error


def test_a_slow_revoke_never_deletes_a_newer_sign_in(tmp_path, qapp, vault, monkeypatch):
    """The old bug: revoke + delete ran together in the background and could wipe a fresh grant."""
    from kabala.backend import Backend
    from kabala.store import Store

    release = threading.Event()
    revoked = []
    monkeypatch.setattr(google_auth, "revoke", lambda token: (release.wait(5), revoked.append(token)))
    monkeypatch.setattr(google_auth, "load_stored", lambda: None)
    monkeypatch.setattr(imap_gmail, "load_login", lambda: None)
    b = Backend(store=Store(tmp_path / "k.db"), license_url=None)
    b.account = Account(email="dana@gmail.com", provider="gmail")
    vault.set_password(google_auth.KEYRING_SERVICE, google_auth.KEYRING_USER, grant("old"))

    b.disconnect_account()
    assert vault.data == {}  # gone before the network call
    # the user signs in again while Google is still answering the revoke
    vault.set_password(google_auth.KEYRING_SERVICE, google_auth.KEYRING_USER, grant("new"))
    release.set()
    for _ in range(50):
        if revoked:
            break
        threading.Event().wait(0.05)
    assert revoked == ["old"]
    assert json.loads(vault.data[(google_auth.KEYRING_SERVICE, google_auth.KEYRING_USER)])["refresh_token"] == "new"


def test_sign_out_stops_a_running_scan_and_a_pending_browser_sign_in(tmp_path, qapp, vault, monkeypatch):
    from kabala.backend import Backend
    from kabala.store import Store

    monkeypatch.setattr(google_auth, "revoke", lambda token: True)
    monkeypatch.setattr(google_auth, "load_stored", lambda: None)
    monkeypatch.setattr(imap_gmail, "load_login", lambda: None)
    b = Backend(store=Store(tmp_path / "k.db"), license_url=None)
    b.account = Account(email="dana@gmail.com", provider="gmail")
    b._cancel.clear()
    b._cancel_auth.clear()
    b.disconnect_account()
    assert b._cancel.is_set() and b._cancel_auth.is_set()
    assert b.account is None and b.credentials is None
    assert b.signed_out_email == "dana@gmail.com" and b.signed_out_provider == "gmail"


def test_app_password_sign_out_forgets_it_and_says_how_to_finish(tmp_path, qapp, monkeypatch):
    from kabala.backend import Backend
    from kabala.store import Store

    forgotten = []
    monkeypatch.setattr(imap_gmail, "forget_login", lambda: forgotten.append(True))
    monkeypatch.setattr(google_auth, "load_stored", lambda: None)
    monkeypatch.setattr(imap_gmail, "load_login", lambda: ("dana@gmail.com", "abcdefghijklmnop"))
    b = Backend(store=Store(tmp_path / "k.db"), license_url=None)
    assert b.account.provider == "imap"
    b.disconnect_account()
    assert forgotten == [True] and b.imap_password is None
    assert b.signed_out_provider == "imap"  # the welcome screen links to the App passwords page
