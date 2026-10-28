import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server import email_login  # noqa: E402
from kabala_server.app import create_app  # noqa: E402
from kabala_server.config import Config  # noqa: E402
from kabala_server.vendor import pyotp  # noqa: E402


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, text):
        self.sent.append((to, subject, text))

    def last_code(self):
        return re.search(r"\b(\d{6})\b", self.sent[-1][2]).group(1)


@pytest.fixture
def env(tmp_path):
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "s.db"))
    mailer = FakeMailer()
    return TestClient(create_app(cfg, lambda tok: {}, mailer=mailer)), mailer


def start(client, email="Dana@Example.co.il"):
    return client.post("/api/email/start", json={"email": email})


def verify(client, code, email="dana@example.co.il"):
    return client.post("/api/email/verify", json={"email": email, "code": code})


def test_code_signs_in_and_returns_the_plan_of_that_email(env):
    client, mailer = env
    assert start(client).json() == {"ok": True, "expires_in": 600}
    to, subject, _ = mailer.sent[0]
    assert to == "dana@example.co.il"
    code = mailer.last_code()
    assert code in subject
    r = verify(client, code)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "dana@example.co.il" and body["plan"] == "free"
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"}).json()
    assert me["email"] == "dana@example.co.il"


def test_code_works_once(env):
    client, mailer = env
    start(client)
    code = mailer.last_code()
    assert verify(client, code).status_code == 200
    assert verify(client, code).status_code == 401


def test_wrong_code_and_wrong_email(env):
    client, mailer = env
    start(client)
    wrong = f"{(int(mailer.last_code()) + 1) % 1_000_000:06d}"
    assert verify(client, wrong).status_code == 401
    assert verify(client, mailer.last_code(), email="other@example.com").status_code == 401
    assert verify(client, mailer.last_code()).status_code == 200


def test_five_wrong_tries_end_the_challenge(env):
    client, mailer = env
    start(client)
    for _ in range(5):
        assert verify(client, "000000" if mailer.last_code() != "000000" else "111111").status_code == 401
    assert verify(client, mailer.last_code()).status_code == 401  # even the right code now


def test_resend_invalidates_the_previous_code_and_resets_tries(env):
    client, mailer = env
    start(client)
    first = mailer.last_code()
    for _ in range(4):
        verify(client, "x")
    start(client)
    second = mailer.last_code()
    assert first != second
    assert verify(client, first).status_code == 401
    assert verify(client, second).status_code == 200


def test_expired_code(env, monkeypatch):
    client, mailer = env
    start(client)
    import time

    later = time.time() + 601
    monkeypatch.setattr(time, "time", lambda: later)
    assert verify(client, mailer.last_code()).status_code == 401


def test_sends_per_ip_are_limited(env):
    client, _ = env
    statuses = [start(client, f"a{i}@b.co").status_code for i in range(6)]
    assert statuses == [200] * 5 + [429]


def test_per_address_limit_in_database(tmp_path):
    from kabala_server.db import Database

    db = Database(str(tmp_path / "d.db"), "x" * 40)
    issued = [db.issue_email_code("a@b.co", "S" * 32, 100.0, 600, 5, 3600) for _ in range(6)]
    assert [i is not None for i in issued] == [True] * 5 + [False]
    assert [i[1] for i in issued[:5]] == [0, 1, 2, 3, 4]  # same secret, moving counter
    assert db.issue_email_code("a@b.co", "T" * 32, 100.0 + 3601, 600, 5, 3600) == ("T" * 32, 0)


def test_invalid_email_and_not_configured(env, tmp_path):
    client, _ = env
    assert start(client, "not-an-email").status_code == 400
    cfg = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "n.db"))  # Gmail not connected
    assert start(TestClient(create_app(cfg, lambda tok: {}))).status_code == 503


def test_code_format_tolerance():
    secret = pyotp.random_base32()
    code = email_login.make_code(secret, 3)
    for typed in (code, f"{code[:3]} {code[3:]}", f"{code[:3]}-{code[3:]}", code.translate(str.maketrans("0123456789", "０１２３４５６７８９"))):
        assert email_login.check_code(secret, 3, typed)
    assert not email_login.check_code(secret, 2, code)
    assert not email_login.check_code(secret, 3, code + "1")


def test_hotp_matches_rfc4226_test_vectors():
    # RFC 4226 appendix D: secret "12345678901234567890"
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert [pyotp.HOTP(secret).at(i) for i in range(3)] == ["755224", "287082", "359152"]
