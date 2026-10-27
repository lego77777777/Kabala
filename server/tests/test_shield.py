import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kabala_server.shield import Rule, Shield, ShieldConfig, SlidingWindow  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(clock=None, **cfg):
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.post("/api/session")
    def session(ok: bool = True):
        if not ok:
            raise HTTPException(401, "nope")
        return {"ok": True}

    @app.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    cfg.setdefault("rules", (Rule("POST", "/api/session", 3, 60),))
    app.add_middleware(Shield, config=ShieldConfig(**cfg), clock=clock or Clock())
    return TestClient(app)


def test_global_limit_and_headers():
    c = make(global_limit=5)
    for i in range(5):
        r = c.get("/ping")
        assert r.status_code == 200
        assert r.headers["x-ratelimit-remaining"] == str(4 - i)
    r = c.get("/ping")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_route_rule_is_stricter_than_global():
    c = make()
    assert [c.post("/api/session").status_code for _ in range(4)] == [200, 200, 200, 429]
    assert c.get("/ping").status_code == 200  # other routes are unaffected


def test_window_slides_and_recovers():
    clock = Clock()
    c = make(clock, global_limit=2)
    assert c.get("/ping").status_code == 200
    assert c.get("/ping").status_code == 200
    assert c.get("/ping").status_code == 429
    clock.t += 121  # two full windows later nothing from before counts
    assert c.get("/ping").status_code == 200


def test_sliding_window_weights_previous_window():
    w = SlidingWindow(0)
    assert all(w.hit(t, 10, 60)[0] for t in range(10))
    # halfway through the next window, half of the previous 10 still counts
    allowed = sum(w.hit(90, 10, 60)[0] for _ in range(10))
    assert allowed == 5


def test_failed_logins_lead_to_a_ban_that_expires():
    clock = Clock()
    c = make(clock, max_strikes=3, ban_seconds=100, rules=())
    for _ in range(3):
        assert c.post("/api/session?ok=false").status_code == 401
    r = c.get("/ping")
    assert r.status_code == 403
    assert r.headers["retry-after"] == "100"
    clock.t += 101
    assert c.get("/ping").status_code == 200


def test_repeat_offender_is_banned_longer():
    clock = Clock()
    c = make(clock, max_strikes=2, ban_seconds=100, rules=())
    for _ in range(2):
        c.post("/api/session?ok=false")
    clock.t += 101
    for _ in range(2):
        c.post("/api/session?ok=false")
    assert c.get("/ping").headers["retry-after"] == "200"


def test_rate_limit_hits_count_as_strikes():
    c = make(global_limit=1, max_strikes=3)
    statuses = [c.get("/ping").status_code for _ in range(5)]
    assert statuses == [200, 429, 429, 429, 403]


def test_body_size_limit():
    c = make(max_body_bytes=100)
    assert c.post("/echo", content=b"x" * 100).json() == {"n": 100}
    assert c.post("/echo", content=b"x" * 101).status_code == 413

    def chunks():  # no Content-Length: checked while streaming
        for _ in range(5):
            yield b"x" * 50

    assert c.post("/echo", content=chunks()).status_code == 413


def test_allowlist_and_exempt_paths():
    c = make(global_limit=1, allow_ips=frozenset({"testclient"}))
    assert all(c.get("/ping").status_code == 200 for _ in range(5))
    c = make(global_limit=1)
    assert all(c.get("/health").status_code == 200 for _ in range(5))


def test_disabled():
    c = make(enabled=False, global_limit=1)
    assert all(c.get("/ping").status_code == 200 for _ in range(5))


@pytest.mark.parametrize("hops,xff,expected", [
    (0, "1.1.1.1", "testclient"),             # proxy not trusted: header ignored
    (1, "6.6.6.6, 2.2.2.2", "2.2.2.2"),       # client-supplied 6.6.6.6 is ignored
    (2, "6.6.6.6, 2.2.2.2, 10.0.0.1", "2.2.2.2"),
])
def test_client_ip_behind_proxies(hops, xff, expected):
    s = Shield(lambda *a: None, ShieldConfig(proxy_hops=hops))
    scope = {"client": ("testclient", 1), "headers": [(b"x-forwarded-for", xff.encode())]}
    assert s.client_ip(scope) == expected


def test_spoofed_forwarded_for_cannot_dodge_limits():
    c = make(global_limit=2, proxy_hops=1)
    statuses = [c.get("/ping", headers={"X-Forwarded-For": f"9.9.9.{i}, 2.2.2.2"}).status_code for i in range(3)]
    assert statuses == [200, 200, 429]


def test_tracked_ips_are_bounded():
    s = Shield(lambda *a: None, ShieldConfig(max_tracked_ips=100))
    for i in range(1000):
        s.check_rate(f"10.0.{i // 256}.{i % 256}", "*", 10, 60, 0)
    assert len(s.windows) <= 200
