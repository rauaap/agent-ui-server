"""Reject requests that a web page tricked a peer's browser into sending.

Being reachable over WireGuard is not proof that a request came from our own
clients: a browser on a peer device carries the peer's network access into
every page it opens. Two checks close the resulting holes:

- Host: a DNS-rebinding page reaches this server under the attacker's
  hostname, so any Host header not naming this server is refused. This covers
  REST and WebSocket alike.
- Origin: WebSockets are exempt from CORS, so any page could otherwise open
  /ws/sessions/{id} and drive the session. Browsers always send Origin on a
  WebSocket handshake and page scripts cannot change it, so a present Origin
  that is not this server's is refused. A missing Origin means a non-browser
  client (the Android app, curl), which this check is not meant to stop.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def allowed_hosts_from_env() -> frozenset[str]:
    """The hostnames this server is legitimately reached under.

    The bind address is always included. ALLOWED_HOSTS adds names that resolve
    to it (a WireGuard or MagicDNS hostname), comma-separated.
    """
    bind = os.environ.get("WIREGUARD_IP", "127.0.0.1").strip().lower()
    hosts = {bind}
    if bind in LOOPBACK_HOSTS:
        hosts |= LOOPBACK_HOSTS
    extra = os.environ.get("ALLOWED_HOSTS", "")
    hosts |= {host.strip().lower() for host in extra.split(",") if host.strip()}
    return frozenset(hosts)


def split_netloc(netloc: str) -> tuple[str | None, int | None]:
    """Hostname and port of a Host header value, or (None, None) if malformed."""
    try:
        parts = urlsplit(f"//{netloc}")
        return parts.hostname, parts.port
    except ValueError:
        return None, None


class NetworkGuardMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str], port: int) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.port = port

    def host_allowed(self, headers: Headers) -> bool:
        # The port is not compared: a rebinding page must connect to our real
        # port anyway, and only the hostname is under the attacker's control.
        hostname, _ = split_netloc(headers.get("host", ""))
        return hostname in self.allowed_hosts

    def origin_allowed(self, headers: Headers) -> bool:
        origin = headers.get("origin")
        if origin is None:
            return True
        # "null" (sandboxed iframes, file:// pages) fails here like any other
        # foreign origin, since it has no scheme or host.
        try:
            parts = urlsplit(origin)
            port = parts.port or (80 if parts.scheme == "http" else None)
        except ValueError:
            return False
        return (
            parts.scheme == "http"
            and parts.hostname in self.allowed_hosts
            and port == self.port
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if scope["type"] == "http":
            if not self.host_allowed(headers):
                response = PlainTextResponse("Invalid host header", status_code=400)
                await response(scope, receive, send)
                return
        elif not (self.host_allowed(headers) and self.origin_allowed(headers)):
            # Closing before accept makes the server refuse the handshake with
            # HTTP 403, so the endpoint never runs.
            await send({"type": "websocket.close", "code": 1008})
            return

        await self.app(scope, receive, send)
