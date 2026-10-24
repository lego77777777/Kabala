"""The browser sign-in flow, end to end against the real loopback server, with Google faked out."""

import threading
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from kabala import google_auth
from kabala.google_auth import AuthError, sign_in

CONFIG = {
    "installed": {
        "client_id": "test-client.apps.googleusercontent.com",
        "client_secret": "test-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


@pytest.fixture(autouse=True)
def fake_google(monkeypatch):
    monkeypatch.setattr(google_auth, "load_client_config", lambda: CONFIG)
    saved = {}

    def fake_fetch_token(self, **kwargs):
        saved["code"] = kwargs["code"]
        saved["verifier"] = self.code_verifier
        self.oauth2session.token = {"access_token": "at", "refresh_token": "rt", "token_type": "Bearer", "scope": google_auth.SCOPES, "expires_at": 4102444800}
        return self.oauth2session.token

    monkeypatch.setattr(google_auth.Flow, "fetch_token", fake_fetch_token)
    monkeypatch.setattr(google_auth, "fetch_email", lambda creds: "me@gmail.com")
    monkeypatch.setattr(google_auth, "save_credentials", lambda creds, email: saved.update(email=email, rt=creds.refresh_token))
    return saved


def browser_that_redirects(**override):
    """Pretends to be the user's browser: checks the consent URL, then follows Google's redirect."""
    seen = {}

    def open_browser(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        seen.update(q)
        params = {"code": "auth-code", "state": q["state"], **override}
        threading.Thread(target=lambda: requests.get(q["redirect_uri"], params=params, timeout=5), daemon=True).start()
        return True

    return open_browser, seen


def test_happy_path_uses_pkce_loopback_and_readonly_scope(fake_google):
    open_browser, seen = browser_that_redirects()
    result = sign_in(threading.Event(), open_browser, timeout=10)

    assert result.email == "me@gmail.com"
    assert seen["scope"].split() == ["https://www.googleapis.com/auth/gmail.readonly", "openid",
                                     "https://www.googleapis.com/auth/userinfo.email"]
    assert seen["code_challenge_method"] == "S256" and len(seen["code_challenge"]) >= 43
    assert seen["access_type"] == "offline"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:")
    assert fake_google["code"] == "auth-code"
    assert fake_google["verifier"]  # the matching PKCE verifier was sent with the code
    assert fake_google["rt"] == "rt"


def test_rejects_wrong_state():
    open_browser, _ = browser_that_redirects(state="forged")
    with pytest.raises(AuthError) as e:
        sign_in(threading.Event(), open_browser, timeout=10)
    assert e.value.code == "state"


def test_user_denied():
    open_browser, _ = browser_that_redirects(error="access_denied")
    with pytest.raises(AuthError) as e:
        sign_in(threading.Event(), open_browser, timeout=10)
    assert e.value.code == "denied"


def test_cancel_while_waiting_for_browser():
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    with pytest.raises(AuthError) as e:
        sign_in(cancel, lambda url: True, timeout=10)
    assert e.value.code == "canceled"


def test_missing_client_config(monkeypatch):
    monkeypatch.setattr(google_auth, "load_client_config", lambda: None)
    monkeypatch.setattr(google_auth, "find_downloaded_client", lambda: None)
    with pytest.raises(AuthError) as e:
        sign_in(threading.Event(), lambda url: True)
    assert e.value.code == "config_missing"


# ---- importing the client file downloaded from Google Cloud --------------------------------


def _write(path, data):
    import json

    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_check_client_file_types(tmp_path):
    assert google_auth.check_client_file(_write(tmp_path / "ok.json", CONFIG)) == ""
    web = {"web": dict(CONFIG["installed"])}
    assert google_auth.check_client_file(_write(tmp_path / "web.json", web)) == "client_web_type"
    assert google_auth.check_client_file(_write(tmp_path / "other.json", {"hello": 1})) == "client_unreadable"
    (tmp_path / "bad.json").write_text("not json")
    assert google_auth.check_client_file(tmp_path / "bad.json") == "client_unreadable"


def test_import_and_auto_find_from_downloads(tmp_path, monkeypatch):
    import kabala.settings

    downloads = tmp_path / "home" / "Downloads"
    downloads.mkdir(parents=True)
    conf = tmp_path / "conf"
    conf.mkdir()
    monkeypatch.setattr(google_auth.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setattr(kabala.settings, "config_dir", lambda: conf)

    _write(downloads / "client_secret_123-web.apps.googleusercontent.com.json", {"web": CONFIG["installed"]})
    assert google_auth.find_downloaded_client() is None  # wrong type is ignored
    good = _write(downloads / "client_secret_456-desktop.apps.googleusercontent.com.json", CONFIG)
    assert google_auth.find_downloaded_client() == good

    google_auth.import_client_file(good)
    assert (conf / "google_client.json").exists()
    with pytest.raises(AuthError) as e:
        google_auth.import_client_file(downloads / "client_secret_123-web.apps.googleusercontent.com.json")
    assert e.value.code == "client_web_type"


def test_open_in_browser_prefers_chrome(monkeypatch):
    launched = []
    monkeypatch.setattr(google_auth, "_chrome_path", lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(google_auth.subprocess, "Popen", lambda args, **kw: launched.append(args))
    assert google_auth.open_in_browser("https://accounts.google.com/x")
    assert launched == [[r"C:\Chrome\chrome.exe", "https://accounts.google.com/x"]]

    opened = []
    monkeypatch.setattr(google_auth, "_chrome_path", lambda: None)
    monkeypatch.setattr(google_auth.webbrowser, "open", lambda url: opened.append(url) or True)
    assert google_auth.open_in_browser("https://accounts.google.com/y") and opened == ["https://accounts.google.com/y"]
