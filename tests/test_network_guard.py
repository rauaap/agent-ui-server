from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_ui_server.network_guard import (
    NetworkGuardMiddleware,
    allowed_hosts_from_env,
    load_or_create_token,
    token_path,
)

ALLOWED = frozenset({"10.0.0.1", "myserver"})
TOKEN = "t" * 44
AUTH = {"authorization": f"Bearer {TOKEN}"}


def scope(kind: str, headers: dict[str, str], path: str | None = None, query: str = "") -> dict:
    return {
        "type": kind,
        "path": path or ("/ws/sessions/1" if kind == "websocket" else "/sessions/1/bash"),
        "root_path": "",
        "method": "GET",
        "query_string": query.encode(),
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }


async def run(
    kind: str,
    headers: dict[str, str],
    app=None,
    *,
    token: str | None = TOKEN,
    public: bool = False,
    **scope_args,
) -> tuple[dict | None, list[dict]]:
    """Send one request through app; return the scope the inner app got, if any."""
    reached: dict | None = None

    async def inner(scope, receive, send) -> None:
        nonlocal reached
        reached = scope

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    if app is None:
        app = NetworkGuardMiddleware(
            inner, allowed_hosts=ALLOWED, port=8000, token=lambda: token,
            is_public=lambda scope: public,
        )
    await app(scope(kind, headers, **scope_args), receive, send)
    return reached, sent


REFUSED = [{"type": "websocket.close", "code": 1008}]


async def _noop() -> None:
    pass


class WebSocketOriginTests(unittest.IsolatedAsyncioTestCase):
    async def assert_websocket(self, headers: dict[str, str], allowed: bool) -> None:
        reached, sent = await run("websocket", {**AUTH, **headers})
        self.assertEqual(reached is not None, allowed)
        if not allowed:
            self.assertEqual(sent, REFUSED)

    async def test_foreign_origin_is_refused(self) -> None:
        await self.assert_websocket(
            {"host": "10.0.0.1:8000", "origin": "http://evil.example"}, allowed=False
        )

    async def test_null_origin_is_refused(self) -> None:
        await self.assert_websocket(
            {"host": "10.0.0.1:8000", "origin": "null"}, allowed=False
        )

    async def test_missing_origin_is_allowed(self) -> None:
        await self.assert_websocket({"host": "10.0.0.1:8000"}, allowed=True)

    async def test_own_origin_is_allowed(self) -> None:
        await self.assert_websocket(
            {"host": "10.0.0.1:8000", "origin": "http://10.0.0.1:8000"}, allowed=True
        )
        await self.assert_websocket(
            {"host": "myserver:8000", "origin": "http://MyServer:8000"}, allowed=True
        )

    async def test_own_host_on_another_port_or_scheme_is_refused(self) -> None:
        await self.assert_websocket(
            {"host": "10.0.0.1:8000", "origin": "http://10.0.0.1:3000"}, allowed=False
        )
        await self.assert_websocket(
            {"host": "10.0.0.1:8000", "origin": "https://10.0.0.1:8000"}, allowed=False
        )
        await self.assert_websocket(
            {"host": "10.0.0.1:8000", "origin": "http://10.0.0.1"}, allowed=False
        )

    async def test_malformed_origin_is_refused(self) -> None:
        await self.assert_websocket(
            {"host": "10.0.0.1:8000", "origin": "http://10.0.0.1:notaport"},
            allowed=False,
        )

    async def test_rebound_host_is_refused_even_with_matching_origin(self) -> None:
        await self.assert_websocket(
            {"host": "attacker.example:8000", "origin": "http://attacker.example:8000"},
            allowed=False,
        )


class WebSocketTokenTests(unittest.IsolatedAsyncioTestCase):
    HOST = {"host": "10.0.0.1:8000"}

    async def test_missing_or_wrong_token_is_refused(self) -> None:
        for headers, query in (
            ({}, ""),
            ({"authorization": "Bearer wrong"}, ""),
            ({}, "token=wrong"),
            ({"authorization": f"Basic {TOKEN}"}, ""),
        ):
            with self.subTest(headers=headers, query=query):
                reached, sent = await run("websocket", {**self.HOST, **headers}, query=query)
                self.assertIsNone(reached)
                self.assertEqual(sent, REFUSED)

    async def test_header_token_is_accepted(self) -> None:
        reached, _ = await run("websocket", {**self.HOST, **AUTH})
        self.assertIsNotNone(reached)

    async def test_query_token_is_accepted_and_removed(self) -> None:
        reached, _ = await run(
            "websocket", self.HOST, query=f"since=5&token={TOKEN}&x="
        )
        self.assertIsNotNone(reached)
        # Neither the endpoint nor the access log may see it.
        self.assertEqual(reached["query_string"], b"since=5&x=")

    async def test_query_token_is_removed_even_when_refused(self) -> None:
        # uvicorn logs a refused handshake with the scope's query string.
        for headers in ({"origin": "http://evil.example"}, {"host": "attacker.example"}):
            with self.subTest(headers=headers):
                handshake = scope("websocket", {**self.HOST, **headers}, query=f"token={TOKEN}")
                sent: list[dict] = []

                async def send(message: dict) -> None:
                    sent.append(message)

                guard = NetworkGuardMiddleware(
                    None, allowed_hosts=ALLOWED, port=8000, token=lambda: TOKEN
                )
                await guard(handshake, None, send)
                self.assertEqual(sent, REFUSED)
                self.assertEqual(handshake["query_string"], b"")

    async def test_websocket_is_never_public(self) -> None:
        reached, sent = await run("websocket", self.HOST, public=True)
        self.assertIsNone(reached)
        self.assertEqual(sent, REFUSED)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_host_gets_400(self) -> None:
        reached, sent = await run("http", {**AUTH, "host": "attacker.example:8000"})
        self.assertIsNone(reached)
        self.assertEqual(sent[0]["type"], "http.response.start")
        self.assertEqual(sent[0]["status"], 400)

    async def test_missing_host_gets_400(self) -> None:
        reached, sent = await run("http", AUTH)
        self.assertIsNone(reached)
        self.assertEqual(sent[0]["status"], 400)

    async def test_own_host_is_allowed(self) -> None:
        reached, _ = await run("http", {**AUTH, "host": "10.0.0.1:8000"})
        self.assertIsNotNone(reached)
        reached, _ = await run("http", {**AUTH, "host": "myserver"})
        self.assertIsNotNone(reached)

    async def test_http_origin_is_not_checked(self) -> None:
        # Browsers already enforce CORS on REST; only Host and token matter here.
        reached, _ = await run(
            "http", {**AUTH, "host": "10.0.0.1:8000", "origin": "http://evil.example"}
        )
        self.assertIsNotNone(reached)

    async def test_missing_or_wrong_token_gets_401(self) -> None:
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": TOKEN}):
            with self.subTest(headers=headers):
                reached, sent = await run("http", {"host": "10.0.0.1:8000", **headers})
                self.assertIsNone(reached)
                self.assertEqual(sent[0]["status"], 401)
                self.assertIn((b"www-authenticate", b"Bearer"), sent[0]["headers"])

    async def test_query_token_does_not_authenticate_rest(self) -> None:
        reached, sent = await run("http", {"host": "10.0.0.1:8000"}, query=f"token={TOKEN}")
        self.assertIsNone(reached)
        self.assertEqual(sent[0]["status"], 401)

    async def test_query_token_is_stripped_from_every_request(self) -> None:
        # uvicorn logs the query string of refused and accepted requests alike.
        for headers, public in (
            ({"host": "10.0.0.1:8000"}, False),  # refused with 401
            ({"host": "attacker.example"}, False),  # refused with 400
            ({**AUTH, "host": "10.0.0.1:8000"}, False),  # accepted
            ({"host": "10.0.0.1:8000"}, True),  # static file
        ):
            with self.subTest(headers=headers, public=public):
                request = scope("http", headers, query=f"a=1&token={TOKEN}")
                guard = NetworkGuardMiddleware(
                    lambda scope, receive, send: _noop(), allowed_hosts=ALLOWED,
                    port=8000, token=lambda: TOKEN, is_public=lambda scope: public,
                )

                async def send(message: dict) -> None:
                    pass

                await guard(request, None, send)
                self.assertEqual(request["query_string"], b"a=1")

    async def test_public_request_needs_no_token_but_still_a_valid_host(self) -> None:
        reached, _ = await run("http", {"host": "10.0.0.1:8000"}, public=True)
        self.assertIsNotNone(reached)
        reached, sent = await run("http", {"host": "attacker.example"}, public=True)
        self.assertIsNone(reached)
        self.assertEqual(sent[0]["status"], 400)

    async def test_no_configured_token_refuses_everything(self) -> None:
        reached, sent = await run(
            "http", {"host": "10.0.0.1:8000", "authorization": "Bearer "}, token=None
        )
        self.assertIsNone(reached)
        self.assertEqual(sent[0]["status"], 401)


class ConfigurationTests(unittest.TestCase):
    def test_bind_address_and_extra_hosts(self) -> None:
        env = {"WIREGUARD_IP": "10.0.0.1", "ALLOWED_HOSTS": " MyServer , ,other.ts.net"}
        with mock.patch.dict("os.environ", env):
            self.assertEqual(
                allowed_hosts_from_env(),
                {"10.0.0.1", "myserver", "other.ts.net"},
            )

    def test_loopback_bind_allows_every_loopback_name(self) -> None:
        with mock.patch.dict("os.environ", {"WIREGUARD_IP": "127.0.0.1"}, clear=True):
            self.assertEqual(
                allowed_hosts_from_env(), {"127.0.0.1", "localhost", "::1"}
            )

    def test_token_path_default_and_override(self) -> None:
        with mock.patch.dict("os.environ", {"HOME": "/home/u"}, clear=True):
            self.assertEqual(token_path(), Path("/home/u/.config/agent-ui-server/token"))
        with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": "/cfg"}, clear=True):
            self.assertEqual(token_path(), Path("/cfg/agent-ui-server/token"))
        with mock.patch.dict("os.environ", {"AUTH_TOKEN_FILE": "/srv/token"}, clear=True):
            self.assertEqual(token_path(), Path("/srv/token"))


class TokenFileTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "config" / "agent-ui-server" / "token"

    def write(self, contents: str, mode: int = 0o600) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(contents)
        self.path.chmod(mode)

    def test_first_start_generates_a_private_token(self) -> None:
        token, created = load_or_create_token(self.path)
        self.assertTrue(created)
        self.assertGreaterEqual(len(token), 32)
        # URL-safe, so it needs no encoding in a WebSocket query string.
        self.assertRegex(token, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(self.path.read_text(), token + "\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        # Later starts reuse it.
        self.assertEqual(load_or_create_token(self.path), (token, False))

    def test_hand_written_token_is_trimmed(self) -> None:
        self.write(f"  {TOKEN}\n\n")
        self.assertEqual(load_or_create_token(self.path), (TOKEN, False))

    def test_short_or_empty_token_is_refused(self) -> None:
        self.write("hunter2\n")
        with self.assertRaisesRegex(ValueError, "at least 32"):
            load_or_create_token(self.path)
        self.path.write_text("")
        with self.assertRaisesRegex(ValueError, "at least 32"):
            load_or_create_token(self.path)

    def test_file_readable_by_others_is_refused(self) -> None:
        for mode in (0o640, 0o604):
            with self.subTest(mode=oct(mode)):
                if self.path.exists():
                    self.path.chmod(mode)
                else:
                    self.write(TOKEN, mode)
                with self.assertRaisesRegex(ValueError, "chmod 600"):
                    load_or_create_token(self.path)

    def test_symlink_is_refused(self) -> None:
        target = self.root / "elsewhere"
        target.write_text(TOKEN)
        target.chmod(0o600)
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            load_or_create_token(self.path)

    def test_directory_is_refused(self) -> None:
        self.path.mkdir(parents=True)
        os.chmod(self.path, 0o700)
        with self.assertRaisesRegex(ValueError, "regular file"):
            load_or_create_token(self.path)


class AppWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_loads_the_token_file(self) -> None:
        from agent_ui_server import main

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(main, "auth_token", None):
            path = Path(tmp) / "token"
            with mock.patch.dict("os.environ", {"AUTH_TOKEN_FILE": str(path)}), \
                    mock.patch("builtins.print") as printed:
                main.load_auth_token()
            self.assertEqual(main.auth_token, path.read_text().strip())
            # The message names the file but never contains the token.
            message = printed.call_args.args[0]
            self.assertIn(str(path), message)
            self.assertNotIn(main.auth_token, message)
            # An authenticated request now gets through the guard.
            _, sent = await run(
                "http",
                {"host": "127.0.0.1:8000", "authorization": f"Bearer {main.auth_token}"},
                app=main.app, path="/agents",
            )
            self.assertEqual(sent[0]["status"], 200)
            _, sent = await run("http", {"host": "127.0.0.1:8000"}, app=main.app, path="/agents")
            self.assertEqual(sent[0]["status"], 401)

    async def test_session_websocket_refuses_foreign_origin(self) -> None:
        from agent_ui_server import main

        _, sent = await run(
            "websocket",
            {"host": "127.0.0.1:8000", "origin": "http://evil.example"},
            app=main.app,
        )
        self.assertEqual(sent, REFUSED)

    async def test_only_the_web_root_mount_is_public(self) -> None:
        from starlette.routing import Mount

        from agent_ui_server import main

        def public(path: str) -> bool:
            return main.is_web_root_request(scope("http", {}, path=path))

        # Without WEB_ROOT nothing is public, not even unknown paths.
        for path in ("/", "/index.html", "/agents", "/docs"):
            with self.subTest(web_root=False, path=path):
                self.assertFalse(public(path))

        # Another mount, registered before the web root as main.py would.
        other = Mount("/admin", app=_noop, name="admin")
        web = Mount("/", app=_noop, name=main.WEB_ROOT_MOUNT)
        routes = [*main.app.router.routes, other, web]
        with mock.patch.object(main.app.router, "routes", routes):
            for path, expected in (
                ("/", True),
                ("/js/app.js", True),
                ("/agents", False),
                ("/docs", False),
                ("/admin/", False),
                ("/admin/secrets", False),
                # Starlette gives a method mismatch (only PATCH/DELETE exist
                # here) to a later full match, so the web root serves it: a
                # static 404 that never reaches the API.
                ("/sessions/1", True),
            ):
                with self.subTest(web_root=True, path=path):
                    self.assertEqual(public(path), expected)
