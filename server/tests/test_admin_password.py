"""Admin panel sign-in by username and password (passwords.py, vendor/werkzeug_security)."""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server import passwords  # noqa: E402
from kabala_server.vendor.werkzeug_security import check_password_hash, generate_password_hash  # noqa: E402
from test_admin import OWNER, H, client, env, sign_in  # noqa: E402,F401

PW = "Kabala-panel-2026!"


def set_pw(c, username="dana", password=PW, current=""):
    return c.post("/admin/api/me/password", headers=H, json={"username": username, "password": password, "current": current})


def login(env, username, password):
    c = client(env)
    return c, c.post("/admin/api/login/password", json={"username": username, "password": password})


def test_right_username_and_password_signs_in_wrong_does_not(env):
    owner = sign_in(env)
    r = set_pw(owner)
    assert r.status_code == 200 and r.json()["username"] == "dana" and r.json()["hasPassword"]
    c, r = login(env, "dana", PW)
    assert r.json() == {"email": OWNER, "role": "owner"} and c.get("/admin/api/me").status_code == 200
    c, r = login(env, "DANA ", PW)  # usernames are case-insensitive; the email works too
    assert r.status_code == 200
    assert login(env, OWNER, PW)[1].status_code == 200
    for user, pw in [("dana", "wrong-password-1"), ("dana", PW.lower()), ("nobody", PW), ("", ""), ("dana", "")]:
        c, r = login(env, user, pw)
        assert r.status_code == 401 and r.json()["detail"] == "wrong username or password"  # same answer for every reason
        assert c.get("/admin/api/me").status_code == 401


def test_admin_without_a_password_cant_sign_in_with_one(env):
    sign_in(env)
    assert login(env, OWNER, "")[1].status_code == 401
    assert login(env, OWNER, PW)[1].status_code == 401


def test_five_wrong_passwords_lock_the_account_for_15_minutes(env, monkeypatch):
    set_pw(sign_in(env))
    for _ in range(passwords.LOCK_AFTER):
        assert login(env, "dana", "wrong-password-1")[1].status_code == 401
    assert login(env, "dana", PW)[1].status_code == 401  # even the right password, while locked
    later = time.time() + passwords.LOCK_S + 1
    monkeypatch.setattr("kabala_server.admin.time.time", lambda: later)
    assert login(env, "dana", PW)[1].status_code == 200
    actions = [e["action"] for e in sign_in(env).get("/admin/api/audit").json()["entries"]]
    assert actions.count("login_failed") == passwords.LOCK_AFTER and "login_locked" in actions and "login_password" in actions


def test_changing_the_password_needs_the_current_one(env):
    owner = sign_in(env)
    set_pw(owner)
    assert set_pw(owner, password="Another-pass-77").status_code == 403
    assert set_pw(owner, password="Another-pass-77", current="wrong").status_code == 403
    assert set_pw(owner, username="dana2", password="Another-pass-77", current=PW).status_code == 200
    assert login(env, "dana", PW)[1].status_code == 401
    assert login(env, "dana2", "Another-pass-77")[1].status_code == 200


@pytest.mark.parametrize("username,password", [
    ("dana", "short1!"), ("dana", "1234567890"), ("dana", "aaaaaaaaaaaa"), ("dana", "my-dana-password"),
    ("dana", "owner-is-me-123"), ("a", PW), ("dana smith", PW), ("דנה", PW), ("dana", "x" * 129),
])
def test_weak_passwords_and_bad_usernames_are_refused(env, username, password):
    assert set_pw(sign_in(env), username, password).status_code == 400


def test_usernames_are_unique_and_cant_be_another_admins_email(env):
    owner = sign_in(env)
    owner.post("/admin/api/admins", headers=H, json={"email": "ops@kabala.app", "role": "admin"})
    set_pw(owner)
    ops = sign_in(env, "ops@kabala.app")
    assert set_pw(ops, "dana", "Strong-pass-2026").status_code == 409
    assert set_pw(ops, OWNER, "Strong-pass-2026").status_code == 400  # "@" isn't allowed in a username anyway
    assert set_pw(ops, "ops", "Strong-pass-2026").status_code == 200


def test_owner_resets_a_forgotten_password_and_the_list_never_shows_hashes(env):
    owner = sign_in(env)
    owner.post("/admin/api/admins", headers=H, json={"email": "ops@kabala.app", "role": "admin"})
    set_pw(sign_in(env, "ops@kabala.app"), "ops", "Strong-pass-2026")
    rows = owner.get("/admin/api/admins").json()["admins"]
    assert "password_hash" not in str(rows) and "scrypt" not in str(rows)
    assert next(a for a in rows if a["email"] == "ops@kabala.app")["has_password"]
    ops = sign_in(env, "ops@kabala.app")
    assert ops.post("/admin/api/admins/password/reset", headers=H, json={"email": OWNER}).status_code == 403
    r = owner.post("/admin/api/admins/password/reset", headers=H, json={"email": "ops@kabala.app"})
    assert not next(a for a in r.json()["admins"] if a["email"] == "ops@kabala.app")["has_password"]
    assert login(env, "ops", "Strong-pass-2026")[1].status_code == 401


def test_password_is_stored_only_as_a_scrypt_hash(env, tmp_path):
    set_pw(sign_in(env))
    raw = Path(env[0].state.receipts.db._db.execute("PRAGMA database_list").fetchone()[2]).read_bytes()
    assert PW.encode() not in raw and b"scrypt:32768:8:1$" in raw


def test_vendored_hash_refuses_foreign_or_damaged_hashes():
    h = generate_password_hash(PW)
    assert check_password_hash(h, PW) and not check_password_hash(h, PW + "x")
    for bad in ["", "plain", "pbkdf2:sha256:1$salt$" + "0" * 64, "scrypt:1073741824:8:1$s$00", "scrypt:x:8:1$s$00", "md5$a$b"]:
        assert check_password_hash(bad, PW) is False
