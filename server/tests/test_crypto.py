import base64
import hashlib
import hmac
import json
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server import tokens  # noqa: E402
from kabala_server.app import create_app  # noqa: E402
from kabala_server.config import Config  # noqa: E402
from kabala_server.crypto import Box  # noqa: E402
from kabala_server.db import Database  # noqa: E402

SECRET = "x" * 40
NEW = "n" * 40


def test_box_round_trip_and_tamper_detection():
    b = Box(SECRET, "test")
    token = b.encrypt("dana@gmail.com")
    assert "dana" not in token and b.decrypt(token) == "dana@gmail.com"
    assert b.encrypt("same") != b.encrypt("same")  # random IV per message
    flipped = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
    assert b.decrypt(flipped) is None
    assert b.decrypt("not a token") is None


def test_each_purpose_has_its_own_key():
    assert Box(SECRET, "session").decrypt(Box(SECRET, "email-code").encrypt("x")) is None


def test_key_rotation_keeps_old_data_readable():
    old_token = Box(SECRET, "p").encrypt("hello")
    rotated = Box((NEW, SECRET), "p")
    assert rotated.decrypt(old_token) == "hello"
    assert Box(SECRET, "p").decrypt(rotated.encrypt("new")) is None  # new data uses the new key only


def test_session_token_hides_the_email_and_expires():
    tok = tokens.issue(SECRET, 7, "dana@gmail.com", 30)
    assert "dana" not in base64.urlsafe_b64decode(tok + "==").decode("latin-1")
    assert tokens.verify(SECRET, tok)["email"] == "dana@gmail.com"
    assert tokens.verify("y" * 40, tok) is None
    assert tokens.verify(SECRET, tokens.issue(SECRET, 7, "a@b.c", 1, now=0)) is None
    assert tokens.verify((NEW, SECRET), tok)["uid"] == 7  # survives a key rotation


def test_tokens_from_before_encryption_still_work_until_they_expire():
    def legacy(exp):
        body = base64.urlsafe_b64encode(json.dumps({"uid": 1, "email": "a@b.c", "exp": exp}).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        return f"{body}.{sig}"

    assert tokens.verify(SECRET, legacy(4_000_000_000))["uid"] == 1
    assert tokens.verify(SECRET, legacy(1)) is None
    assert tokens.verify("y" * 40, legacy(4_000_000_000)) is None


def test_sign_in_secret_is_encrypted_in_the_db_file_and_erased_after_use(tmp_path):
    path = tmp_path / "s.db"
    db = Database(str(path), SECRET)
    secret, counter = db.issue_email_code("a@b.co", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", 100.0, 600, 5, 3600)
    stored = sqlite3.connect(path).execute("SELECT secret FROM email_codes").fetchone()[0]
    assert secret == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP" and secret not in stored
    assert db.take_email_attempt("a@b.co", 101.0, 5) == (secret, 0)
    db.close_email_code("a@b.co")
    assert sqlite3.connect(path).execute("SELECT secret FROM email_codes").fetchone()[0] == ""
    assert secret.encode() not in path.read_bytes()  # secure_delete: not left behind in free pages
    # next code in the same hour gets a fresh secret
    assert db.issue_email_code("a@b.co", "K" * 32, 102.0, 600, 5, 3600) == ("K" * 32, 0)


def test_db_file_is_owner_only(tmp_path):
    path = tmp_path / "s.db"
    Database(str(path), SECRET)
    assert path.stat().st_mode & 0o077 == 0


def test_secrets_never_appear_in_the_printed_config():
    cfg = Config(secret=SECRET, google_client_id="cid", stripe_secret_key="sk_live_abc", stripe_webhook_secret="whsec_abc",
                 gmail_client_secret="GOCSPX-abc", gmail_refresh_token="1//rtok", old_secrets=("o" * 40,))
    text = repr(cfg)
    assert all(s not in text for s in (SECRET, "sk_live_abc", "whsec_abc", "GOCSPX-abc", "1//rtok", "o" * 40))


def test_behind_a_proxy_only_https_is_served(tmp_path):
    cfg = Config(secret=SECRET, google_client_id="cid", db_path=str(tmp_path / "s.db"), proxy_hops=1)
    c = TestClient(create_app(cfg, lambda tok: {}))
    assert c.get("/api/version", headers={"X-Forwarded-Proto": "http"}).status_code == 403
    assert c.get("/api/version").status_code == 403  # no header: not from our proxy
    r = c.get("/api/version", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200 and r.headers["strict-transport-security"].startswith("max-age=")
    assert c.get("/health").status_code == 200  # the platform's health check uses plain http
