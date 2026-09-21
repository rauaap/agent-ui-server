"""Authenticate clients and reject requests forged by web pages.

Being reachable over WireGuard is not proof that a request came from our own
clients: a browser on a peer device carries the peer's network access into
every page it opens, and anything else on the network can connect too. Three
checks cover that:

- Token: every API request and WebSocket must present the shared token, kept
  in a file the server generates on first start (see `token_path`), as
  `Authorization: Bearer <token>`. Browsers cannot set headers on a
  WebSocket, so a handshake may pass it as `?token=` instead; it is removed
  from the query string before anything downstream (including the access log)
  sees it. Only the desktop client's static files are served without it, so
  the client can load and ask for the token.

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

import errno
import hmac
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# A generated token is 43 characters; anything this short or shorter is a
# password someone typed into the file, not a generated token.
MIN_TOKEN_LENGTH = 32
TOKEN_QUERY_PARAMETER = "token"


def token_path() -> Path:
    """Where the token lives: AUTH_TOKEN_FILE, else the user's config dir.

    A file rather than an environment variable, because the server's
    environment is inherited by `!` commands, unsandboxed agents and git, any
    of which could print it into a transcript. It must stay outside every
    sandbox mount, which ~/.config/agent-ui-server is.
    """
    override = os.environ.get("AUTH_TOKEN_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "agent-ui-server" / "token"


def check_auth_token(token: str) -> str:
    """The token, or a ValueError saying why the server must not start."""
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"The auth token must be at least {MIN_TOKEN_LENGTH} characters. "
            "Delete the token file to have a new one generated."
        )
    return token


def load_or_create_token(path: Path) -> tuple[str, bool]:
    """Read the token file, generating it first if it does not exist.

    Returns the token and whether it was just created. Refuses, like ssh does
    with keys, a file that is a symlink, belongs to someone else or is
    readable by other users.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            # Another server created it first; use theirs.
            return load_or_create_token(path)
        with os.fdopen(fd, "w") as stream:
            stream.write(token + "\n")
        return token, True
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"Auth token file {path} must not be a symlink") from None
        raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise ValueError(f"Auth token file {path} must be a regular file you own")
    if info.st_mode & 0o077:
        os.close(fd)
        raise ValueError(
            f"Auth token file {path} is accessible to other users; run chmod 600 {path}"
        )
    with os.fdopen(fd) as stream:
        token = stream.read(4096).strip()
    return check_auth_token(token), False


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


def take_query_token(scope: Scope) -> str | None:
    """Remove the token from a WebSocket's query string and return it.

    The scope is edited in place so neither the endpoint nor uvicorn's access
    log, which prints the path with its query string, ever sees the token.
    """
    query = scope.get("query_string", b"").decode("latin-1")
    pairs = parse_qsl(query, keep_blank_values=True)
    tokens = [value for name, value in pairs if name == TOKEN_QUERY_PARAMETER]
    if not tokens:
        return None
    rest = [(name, value) for name, value in pairs if name != TOKEN_QUERY_PARAMETER]
    scope["query_string"] = urlencode(rest).encode("latin-1")
    return tokens[-1]


class NetworkGuardMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: frozenset[str],
        port: int,
        token: Callable[[], str | None],
        is_public: Callable[[Scope], bool] = lambda scope: False,
    ) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.port = port
        # Read per request: the token is loaded at startup, after the
        # middleware is configured. None fails closed.
        self.token = token
        self.is_public = is_public

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

    def authorized(self, scope: Scope, headers: Headers) -> bool:
        presented = None
        scheme, _, value = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            presented = value.strip()
        if scope["type"] == "websocket":
            # Always strip it, even when a header was also sent.
            query_token = take_query_token(scope)
            presented = presented or query_token
        token = self.token()
        if token is None or presented is None:
            return False
        return hmac.compare_digest(presented.encode(), token.encode())

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
            if not self.is_public(scope) and not self.authorized(scope, headers):
                response = PlainTextResponse(
                    "Missing or invalid token",
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        # Authorize first: it strips a query token, which must happen even
        # when another check refuses the handshake and it is logged.
        elif not (
            self.authorized(scope, headers)
            and self.host_allowed(headers)
            and self.origin_allowed(headers)
        ):
            # Closing before accept makes the server refuse the handshake with
            # HTTP 403, so the endpoint never runs.
            await send({"type": "websocket.close", "code": 1008})
            return

        await self.app(scope, receive, send)
