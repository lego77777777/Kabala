from typing import Any

from starlette.requests import Request


def client_ip(scope: Any, proxy_hops: int = 0) -> str:
    """
    Kabala: the client's IP, trusting only the right-most `proxy_hops` entries of X-Forwarded-For
    (the ones our own proxies appended). Anything to their left was written by the client and is
    ignored, so a flood can't dodge limits by sending a new fake IP with every request.
    Replaces the original get_ipaddr, which returned the header as-is.
    """
    if proxy_hops > 0:
        for name, value in scope.get("headers") or ():
            if name == b"x-forwarded-for":
                hops = [h.strip() for h in value.decode("latin-1").split(",") if h.strip()]
                if len(hops) >= proxy_hops:
                    return hops[-proxy_hops]
                break
    client = scope.get("client")
    return client[0] if client and client[0] else "127.0.0.1"


def get_ipaddr(request: Request, proxy_hops: int = 1) -> str:
    """Kabala: kept for API compatibility, now safe (see client_ip)."""
    return client_ip(request.scope, proxy_hops)


def get_remote_address(request: Request) -> str:
    """
    Returns the ip address for the current request (or 127.0.0.1 if none found)
    """
    if not request.client or not request.client.host:
        return "127.0.0.1"

    return request.client.host
