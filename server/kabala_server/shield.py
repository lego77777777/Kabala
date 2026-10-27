"""
Anti-DDoS / abuse protection for the API, as a pure ASGI middleware (no extra dependencies).

Adapted for Kabala from the designs of well-known open-source tools:
  - slowapi / limits (MIT): per-route limits, the "sliding window counter" algorithm,
    X-RateLimit-* and Retry-After headers.
  - fail2ban: a "jail" that bans an IP for a while after repeated abuse (429s, bad tokens,
    bad webhook signatures); a repeat offender is banned for longer each time.
  - @fastify/rate-limit (already used by ../billing): the same limits on both servers.

Layers, in order, for every request:
  1. allowlist          KABALA_ALLOW_IPS skip all checks (e.g. a monitoring service)
  2. jail               a banned IP gets 403 without touching the app or the database
  3. body size          Content-Length (or a streamed body) over the limit gets 413
  4. concurrency        too many in-flight requests from one IP gets 429 (slow clients, floods)
  5. rate limit         a global per-IP limit plus a stricter limit per sensitive route
  6. strikes            responses 401/400 on auth and webhook routes count toward the jail

All state is in memory, bounded (oldest IPs are evicted), so a flood of spoofed addresses
can't exhaust memory. With several worker processes each one keeps its own counters.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("kabala_server.shield")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass(frozen=True)
class Rule:
    """`limit` requests per `window` seconds for requests matching `method` + `path` (exact)."""

    method: str
    path: str
    limit: int
    window: float


# Same numbers as the billing server (../billing/src/app.ts).
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("POST", "/api/session", 20, 60),
    Rule("POST", "/api/checkout", 10, 60),
    Rule("POST", "/api/portal", 10, 60),
    Rule("POST", "/api/stripe/webhook", 600, 60),
)

# Responses on these routes that count as a strike (guessing tokens, forging webhooks).
STRIKE_ROUTES: dict[str, tuple[int, ...]] = {
    "/api/session": (401,),
    "/api/me": (401,),
    "/api/checkout": (401,),
    "/api/portal": (401,),
    "/api/stripe/webhook": (400,),
}


@dataclass
class ShieldConfig:
    enabled: bool = True
    global_limit: int = 120  # requests per IP per window, all routes together
    global_window: float = 60
    rules: tuple[Rule, ...] = DEFAULT_RULES
    max_body_bytes: int = 256 * 1024
    max_concurrent_per_ip: int = 10
    # jail (fail2ban: maxretry / findtime / bantime)
    max_strikes: int = 10
    strike_window: float = 600
    ban_seconds: float = 900
    max_ban_seconds: float = 24 * 3600
    # how many reverse proxies (Render, Fly, Cloudflare…) append to X-Forwarded-For; 0 = trust nobody
    proxy_hops: int = 0
    allow_ips: frozenset[str] = frozenset()
    exempt_paths: frozenset[str] = frozenset({"/health"})
    max_tracked_ips: int = 100_000


class SlidingWindow:
    """Sliding window counter (as in `limits`): the previous window's count is weighted by how much of it
    still overlaps the last `window` seconds. O(1) memory per key, smooth at window edges."""

    __slots__ = ("start", "current", "previous")

    def __init__(self, now: float) -> None:
        self.start = now
        self.current = 0
        self.previous = 0

    def hit(self, now: float, limit: int, window: float) -> tuple[bool, int, float]:
        """Returns (allowed, remaining, seconds until a request would be allowed again)."""
        elapsed = now - self.start
        if elapsed >= window:
            periods = int(elapsed // window)
            self.previous = self.current if periods == 1 else 0
            self.current = 0
            self.start += periods * window
            elapsed = now - self.start
        weight = 1 - elapsed / window
        used = self.previous * weight + self.current
        if used + 1 > limit:
            # time until the weighted previous count has decayed enough for one more request
            retry = window - elapsed if self.previous == 0 else max(0.0, (used + 1 - limit) / self.previous * window)
            return False, 0, max(retry, 1.0)
        self.current += 1
        return True, max(0, int(limit - used - 1)), window - elapsed


class _LRU(OrderedDict):
    """Dict that drops its oldest entries past `cap`."""

    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap

    def touch(self, key: Any, factory: Callable[[], Any]) -> Any:
        value = self.get(key)
        if value is None:
            value = self[key] = factory()
            while len(self) > self.cap:
                self.popitem(last=False)
        else:
            self.move_to_end(key)
        return value


@dataclass
class _Jail:
    strikes: list[float] = field(default_factory=list)
    banned_until: float = 0.0
    bans: int = 0


class Shield:
    def __init__(self, app: ASGIApp, config: ShieldConfig | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.cfg = config or ShieldConfig()
        self.clock = clock
        self.rules = {(r.method, r.path): r for r in self.cfg.rules}
        cap = self.cfg.max_tracked_ips
        self.windows: _LRU = _LRU(cap * 2)  # (ip, rule key) → SlidingWindow
        self.jails: _LRU = _LRU(cap)
        self.inflight: dict[str, int] = {}

    # ---- helpers -------------------------------------------------------------------------

    def client_ip(self, scope: Scope) -> str:
        if self.cfg.proxy_hops > 0:
            for name, value in scope.get("headers") or ():
                if name == b"x-forwarded-for":
                    hops = [h.strip() for h in value.decode("latin-1").split(",") if h.strip()]
                    # the right-most entries were added by our own proxies; anything left of them is spoofable
                    if len(hops) >= self.cfg.proxy_hops:
                        return hops[-self.cfg.proxy_hops]
                    break
        client = scope.get("client")
        return client[0] if client else "unknown"

    def banned_for(self, ip: str, now: float) -> float:
        jail = self.jails.get(ip)
        return jail.banned_until - now if jail and jail.banned_until > now else 0.0

    def strike(self, ip: str, now: float, reason: str) -> None:
        jail = self.jails.touch(ip, _Jail)
        jail.strikes = [t for t in jail.strikes if now - t < self.cfg.strike_window]
        jail.strikes.append(now)
        if len(jail.strikes) >= self.cfg.max_strikes:
            jail.bans += 1
            duration = min(self.cfg.ban_seconds * 2 ** (jail.bans - 1), self.cfg.max_ban_seconds)
            jail.banned_until = now + duration
            jail.strikes.clear()
            log.warning("banned %s for %ds (%s, ban #%d)", ip, duration, reason, jail.bans)

    def check_rate(self, ip: str, key: Any, limit: int, window: float, now: float) -> tuple[bool, int, float]:
        return self.windows.touch((ip, key), lambda: SlidingWindow(now)).hit(now, limit, window)

    @staticmethod
    async def reject(send: Send, status: int, error: str, headers: dict[str, str] | None = None) -> None:
        body = json.dumps({"detail": error}).encode()
        raw = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        raw += [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
        await send({"type": "http.response.start", "status": status, "headers": raw})
        await send({"type": "http.response.body", "body": body})

    # ---- ASGI ----------------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.cfg.enabled:
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "GET")
        ip = self.client_ip(scope)
        if ip in self.cfg.allow_ips or path in self.cfg.exempt_paths:
            return await self.app(scope, receive, send)
        now = self.clock()

        # 2. jail
        ban = self.banned_for(ip, now)
        if ban:
            return await self.reject(send, 403, "temporarily blocked", {"Retry-After": str(math.ceil(ban))})

        # 3. body size, by header first (cheap) and then while streaming (chunked bodies have no header)
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    too_big = int(value) > self.cfg.max_body_bytes
                except ValueError:
                    return await self.reject(send, 400, "bad content-length")
                if too_big:
                    return await self.reject(send, 413, "request body too large")

        # 4. concurrency
        if self.inflight.get(ip, 0) >= self.cfg.max_concurrent_per_ip:
            self.strike(ip, now, "too many concurrent requests")
            return await self.reject(send, 429, "too many concurrent requests", {"Retry-After": "1"})

        # 5. rate limits: the route's own rule, then the global one
        rule = self.rules.get((method, path))
        checks = [((method, path), rule.limit, rule.window)] if rule else []
        checks.append(("*", self.cfg.global_limit, self.cfg.global_window))
        headers: dict[str, str] = {}
        for key, limit, window in checks:
            ok, remaining, reset = self.check_rate(ip, key, limit, window, now)
            if not headers:  # report the strictest (first) limit, like slowapi
                headers = {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining),
                           "X-RateLimit-Reset": str(math.ceil(reset))}
            if not ok:
                self.strike(ip, now, f"rate limit {method} {path}")
                headers["Retry-After"] = str(math.ceil(reset))
                return await self.reject(send, 429, "rate limit exceeded", headers)

        received = 0
        too_large = False

        async def limited_receive() -> dict:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.cfg.max_body_bytes:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        strike_on = STRIKE_ROUTES.get(path, ())
        started = False

        async def send_with_headers(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                if message["status"] in strike_on:
                    self.strike(ip, self.clock(), f"{message['status']} on {path}")
                message = {**message, "headers": [*message.get("headers", []),
                                                   *((k.encode(), v.encode()) for k, v in headers.items())]}
            await send(message)

        self.inflight[ip] = self.inflight.get(ip, 0) + 1
        try:
            await self.app(scope, limited_receive, send_with_headers)
        except Exception:
            # the app may raise on the disconnect we fed it for an oversized body
            if not (too_large and not started):
                raise
        finally:
            left = self.inflight[ip] - 1
            if left:
                self.inflight[ip] = left
            else:
                del self.inflight[ip]
        if too_large and not started:
            await self.reject(send, 413, "request body too large")
