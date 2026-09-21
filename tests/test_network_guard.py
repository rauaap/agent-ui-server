from __future__ import annotations

import unittest
from unittest import mock

from agent_ui_server.network_guard import NetworkGuardMiddleware, allowed_hosts_from_env

ALLOWED = frozenset({"10.0.0.1", "myserver"})


def scope(kind: str, headers: dict[str, str]) -> dict:
    return {
        "type": kind,
        "path": "/ws/sessions/1" if kind == "websocket" else "/sessions/1/bash",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }


async def run(kind: str, headers: dict[str, str], app=None) -> tuple[bool, list[dict]]:
    """Send one request through app; report whether the inner app ran."""
    reached = False

    async def inner(scope, receive, send) -> None:
        nonlocal reached
        reached = True

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    if app is None:
        app = NetworkGuardMiddleware(inner, allowed_hosts=ALLOWED, port=8000)
    await app(scope(kind, headers), receive, send)
    return reached, sent


class WebSocketOriginTests(unittest.IsolatedAsyncioTestCase):
    async def assert_websocket(self, headers: dict[str, str], allowed: bool) -> None:
        reached, sent = await run("websocket", headers)
        self.assertEqual(reached, allowed)
        if not allowed:
            self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])

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


class HttpHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_host_gets_400(self) -> None:
        reached, sent = await run("http", {"host": "attacker.example:8000"})
        self.assertFalse(reached)
        self.assertEqual(sent[0]["type"], "http.response.start")
        self.assertEqual(sent[0]["status"], 400)

    async def test_missing_host_gets_400(self) -> None:
        reached, sent = await run("http", {})
        self.assertFalse(reached)
        self.assertEqual(sent[0]["status"], 400)

    async def test_own_host_is_allowed(self) -> None:
        reached, _ = await run("http", {"host": "10.0.0.1:8000"})
        self.assertTrue(reached)
        reached, _ = await run("http", {"host": "myserver"})
        self.assertTrue(reached)

    async def test_http_origin_is_not_checked(self) -> None:
        # Browsers already enforce CORS on REST; only Host matters here.
        reached, _ = await run(
            "http", {"host": "10.0.0.1:8000", "origin": "http://evil.example"}
        )
        self.assertTrue(reached)


class AllowedHostsTests(unittest.TestCase):
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


class AppWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_websocket_refuses_foreign_origin(self) -> None:
        from agent_ui_server import main

        _, sent = await run(
            "websocket",
            {"host": "127.0.0.1:8000", "origin": "http://evil.example"},
            app=main.app,
        )
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
