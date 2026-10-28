"""
Anti-DDoS / abuse protection for the API, as a pure ASGI middleware around the whole app.

Rate limiting itself is slowapi (copied into kabala_server/vendor/slowapi, see KABALA_CHANGES.md there).
This module adds what slowapi doesn't have:

  1. allowlist      KABALA_ALLOW_IPS skip all checks (e.g. a monitoring service)
  2. jail           fail2ban-style: an IP that keeps misbehaving gets 403 without touching the app or
                    the database; a repeat offender is banned twice as long each time
  3. body size      Content-Length (or a streamed body) over the limit gets 413
  4. concurrency    too many in-flight requests from one IP gets 429 (slow clients, floods)
  5. strikes        any 429 (slowapi or ours), and 401/400 on auth and webhook routes, count toward the jail

State is in memory and bounded (oldest IPs are evicted), so a flood of spoofed addresses can't exhaust
memory. With several worker processes each one keeps its own counters.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from kabala_server.vendor.slowapi.util import client_ip

log = logging.getLogger("kabala_server.shield")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Responses that count as a strike, besides 429 everywhere (guessing tokens, forging webhooks).
STRIKE_ROUTES: dict[str, tuple[int, ...]] = {
    "/api/session": (401,),
    "/api/email/verify": (401,),
    "/api/me": (401,),
    "/api/checkout": (401,),
    "/api/portal": (401,),
    "/api/stripe/webhook": (400,),
    "/admin/api/login/verify": (401,),
}


@dataclass
class ShieldConfig:
    enabled: bool = True
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


@dataclass
class _Jail:
    strikes: list[float] = field(default_factory=list)
    banned_until: float = 0.0
    bans: int = 0


class Shield:
    def __init__(self, app: ASGIApp, config: ShieldConfig | None = None, clock: Callable[[], float] = time.monotonic,
                 jails: OrderedDict | None = None) -> None:
        self.app = app
        self.cfg = config or ShieldConfig()
        self.clock = clock
        # passed in by the app so the admin panel can list and lift bans
        self.jails: OrderedDict[str, _Jail] = jails if jails is not None else OrderedDict()
        self.inflight: dict[str, int] = {}

    def client_ip(self, scope: Scope) -> str:
        return client_ip(scope, self.cfg.proxy_hops)

    def banned_for(self, ip: str, now: float) -> float:
        jail = self.jails.get(ip)
        return jail.banned_until - now if jail and jail.banned_until > now else 0.0

    def strike(self, ip: str, now: float, reason: str) -> None:
        jail = self.jails.get(ip)
        if jail is None:
            jail = self.jails[ip] = _Jail()
            while len(self.jails) > self.cfg.max_tracked_ips:
                self.jails.popitem(last=False)
        else:
            self.jails.move_to_end(ip)
        jail.strikes = [t for t in jail.strikes if now - t < self.cfg.strike_window]
        jail.strikes.append(now)
        if len(jail.strikes) >= self.cfg.max_strikes:
            jail.bans += 1
            duration = min(self.cfg.ban_seconds * 2 ** (jail.bans - 1), self.cfg.max_ban_seconds)
            jail.banned_until = now + duration
            jail.strikes.clear()
            log.warning("banned %s for %ds (%s, ban #%d)", ip, duration, reason, jail.bans)

    @staticmethod
    async def reject(send: Send, status: int, error: str, headers: dict[str, str] | None = None) -> None:
        body = json.dumps({"detail": error}).encode()
        raw = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        raw += [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
        await send({"type": "http.response.start", "status": status, "headers": raw})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.cfg.enabled:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        ip = self.client_ip(scope)
        if ip in self.cfg.allow_ips or path in self.cfg.exempt_paths:
            return await self.app(scope, receive, send)
        now = self.clock()

        ban = self.banned_for(ip, now)
        if ban:
            return await self.reject(send, 403, "temporarily blocked", {"Retry-After": str(math.ceil(ban))})

        # body size by header first (cheap), then while streaming (chunked bodies have no header)
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    too_big = int(value) > self.cfg.max_body_bytes
                except ValueError:
                    return await self.reject(send, 400, "bad content-length")
                if too_big:
                    return await self.reject(send, 413, "request body too large")

        if self.inflight.get(ip, 0) >= self.cfg.max_concurrent_per_ip:
            self.strike(ip, now, "too many concurrent requests")
            return await self.reject(send, 429, "too many concurrent requests", {"Retry-After": "1"})

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

        strike_on = (429, *STRIKE_ROUTES.get(path, ()))
        started = False

        async def watch_status(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                if message["status"] in strike_on:
                    self.strike(ip, self.clock(), f"{message['status']} on {path}")
            await send(message)

        self.inflight[ip] = self.inflight.get(ip, 0) + 1
        try:
            await self.app(scope, limited_receive, watch_status)
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


def active_bans(jails: OrderedDict, clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """For the admin panel: who is banned right now, for how much longer, and how many times so far."""
    now = clock()
    return sorted(({"ip": ip, "seconds_left": math.ceil(j.banned_until - now), "bans": j.bans}
                   for ip, j in list(jails.items()) if j.banned_until > now), key=lambda b: -b["seconds_left"])


def lift_ban(jails: OrderedDict, ip: str) -> bool:
    jail = jails.get(ip)
    if not jail or not jail.banned_until:
        return False
    jail.banned_until = 0.0
    jail.strikes.clear()
    return True
