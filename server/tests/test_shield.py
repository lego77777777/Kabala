import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server.app import create_app  # noqa: E402
from kabala_server.config import Config  # noqa: E402
from kabala_server.shield import Shield, ShieldConfig  # noqa: E402
from kabala_server.vendor.slowapi.util import client_ip  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(clock=None, **cfg):
    """A bare app behind the Shield only (no slowapi), to test the Shield's own layers."""
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/ping")
    def ping(status: int = 200):
        if status != 200:
            raise HTTPException(status, "nope")
        return {"ok": True}

    @app.post("/api/session")
    def session():
        raise HTTPException(401, "nope")

    @app.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    app.add_middleware(Shield, config=ShieldConfig(**cfg), clock=clock or Clock())
    return TestClient(app)


def real_app(tmp_path, **cfg):
    """The real Kabala API: slowapi limits inside the Shield."""
    config = Config(secret="x" * 40, google_client_id="cid", db_path=str(tmp_path / "s.db"), **cfg)

    def verify(tok):
        if tok == "bad":
            raise ValueError("bad token")
        return {"email": tok, "email_verified": True}

    return TestClient(create_app(config, verify))


# ---- slowapi (vendored) in the real app ----------------------------------------------------------


def test_session_route_limit(tmp_path):
    c = real_app(tmp_path)
    statuses = [c.post("/api/session", json={"id_token": "dana@gmail.com"}).status_code for _ in range(21)]
    assert statuses == [200] * 20 + [429]
    r = c.post("/api/session", json={"id_token": "dana@gmail.com"})
    assert r.json()["detail"].startswith("rate limit exceeded")
    assert int(r.headers["retry-after"]) >= 1
    assert c.get("/api/version").status_code == 200  # other routes have their own budget


def test_global_limit_and_headers(tmp_path):
    c = real_app(tmp_path, rate_limit_per_minute=5)
    r = c.get("/api/version")
    assert r.headers["x-ratelimit-limit"] == "5"
    assert r.headers["x-ratelimit-remaining"] == "4"
    assert [c.get("/api/version").status_code for _ in range(5)] == [200] * 4 + [429]
    assert all(c.get("/health").status_code == 200 for _ in range(10))  # exempt


def test_bad_logins_get_banned(tmp_path):
    c = real_app(tmp_path)
    assert [c.post("/api/session", json={"id_token": "bad"}).status_code for _ in range(11)] == [401] * 10 + [403]
    assert c.get("/api/version").status_code == 403


def test_spoofed_forwarded_for_cannot_dodge_limits(tmp_path):
    c = real_app(tmp_path, rate_limit_per_minute=2, proxy_hops=1)
    statuses = [c.get("/api/version", headers={"X-Forwarded-For": f"9.9.9.{i}, 2.2.2.2", "X-Forwarded-Proto": "https"}).status_code for i in range(3)]
    assert statuses == [200, 200, 429]


def test_disabled(tmp_path):
    c = real_app(tmp_path, rate_limit_per_minute=1, shield_enabled=False)
    assert all(c.get("/api/version").status_code == 200 for _ in range(5))


# ---- Shield layers ----------------------------------------------------------------------------------


def test_ban_expires_and_repeat_offender_waits_longer():
    clock = Clock()
    c = make(clock, max_strikes=3, ban_seconds=100)
    for _ in range(3):
        assert c.post("/api/session").status_code == 401
    r = c.get("/ping")
    assert r.status_code == 403
    assert r.headers["retry-after"] == "100"
    clock.t += 101
    assert c.get("/ping").status_code == 200
    for _ in range(3):
        c.post("/api/session")
    assert c.get("/ping").headers["retry-after"] == "200"


def test_any_429_is_a_strike_but_other_errors_are_not():
    c = make(max_strikes=3)
    assert [c.get("/ping?status=404").status_code for _ in range(5)] == [404] * 5
    assert [c.get("/ping?status=429").status_code for _ in range(4)] == [429, 429, 429, 403]


def test_body_size_limit():
    c = make(max_body_bytes=100)
    assert c.post("/echo", content=b"x" * 100).json() == {"n": 100}
    assert c.post("/echo", content=b"x" * 101).status_code == 413

    def chunks():  # no Content-Length: checked while streaming
        for _ in range(5):
            yield b"x" * 50

    assert c.post("/echo", content=chunks()).status_code == 413


def test_allowlist_and_exempt_paths():
    c = make(max_strikes=1, allow_ips=frozenset({"testclient"}))
    c.post("/api/session")
    assert c.get("/ping").status_code == 200
    c = make(max_strikes=1)
    c.post("/api/session")
    assert c.get("/ping").status_code == 403
    assert c.get("/health").status_code == 200


@pytest.mark.parametrize("hops,xff,expected", [
    (0, "1.1.1.1", "testclient"),             # proxy not trusted: header ignored
    (1, "6.6.6.6, 2.2.2.2", "2.2.2.2"),       # client-supplied 6.6.6.6 is ignored
    (2, "6.6.6.6, 2.2.2.2, 10.0.0.1", "2.2.2.2"),
])
def test_client_ip_behind_proxies(hops, xff, expected):
    scope = {"client": ("testclient", 1), "headers": [(b"x-forwarded-for", xff.encode())]}
    assert client_ip(scope, hops) == expected


def test_tracked_ips_are_bounded():
    s = Shield(lambda *a: None, ShieldConfig(max_tracked_ips=100))
    for i in range(1000):
        s.strike(f"10.0.{i // 256}.{i % 256}", 0, "test")
    assert len(s.jails) == 100
