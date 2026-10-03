"""Server-wide network settings and fail-closed namespace rules."""
from pathlib import Path
import tempfile
import subprocess
import unittest
import asyncio

from agent_ui_server.agent import PiAdapter, ClaudeCodeAdapter
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError

from agent_ui_server.db import Database
from agent_ui_server.sandbox import network_command, SANDBOX_DNS
from agent_ui_server.sandbox_network import validate_network_allowlist


class NetworkValidationTests(unittest.TestCase):
    def test_exact_destinations_and_deduplication(self):
        entries = [{"ip": "100.64.0.10", "port": 443}, {"ip": "100.64.0.10", "port": 22}]
        self.assertEqual(validate_network_allowlist(entries * 2), entries)
        for ip in ("localhost", "100.64.0.0/10", "::1", "127.0.0.1", "0.0.0.0",
                   "224.0.0.1", "255.255.255.255", "240.0.0.1", SANDBOX_DNS):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                validate_network_allowlist([{"ip": ip, "port": 443}])
        for port in (0, 65536, True, "443", 443.0):
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_network_allowlist([{"ip": "100.64.0.10", "port": port}])

    def test_firewall_precedes_route_and_exec(self):
        entries = [{"ip": "100.64.0.10", "port": 443}, {"ip": "100.64.0.10", "port": 22}]
        with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=lambda name, **kwargs: name), \
                mock.patch("agent_ui_server.sandbox.verify_network_namespace"), \
                mock.patch("agent_ui_server.sandbox.host_addresses", return_value=["100.64.0.10"]):
            command = network_command(["true"], entries)
        script = command[command.index("-c") + 1]
        self.assertIn("ip route add blackhole 100.64.0.10/32", script)
        self.assertIn("ip daddr 100.64.0.10 tcp dport 443 accept", script)
        self.assertIn("ip daddr 100.64.0.10 tcp dport 22 accept", script)
        self.assertIn("ip daddr { 100.64.0.10 } drop", script)
        self.assertLess(script.index("nft -f"), script.index("ip route replace"))
        self.assertLess(script.index("ip route replace"), script.index("exec /usr/bin/env"))
        self.assertTrue(script.startswith("set -e\n"))

    def test_firewall_failure_prevents_route_exception_and_agent_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, marker = root / "routes", root / "agent-started"
            ip, nft = root / "ip", root / "nft"
            ip.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n')
            nft.write_text("#!/bin/sh\nexit 1\n")
            ip.chmod(0o755)
            nft.chmod(0o755)
            with mock.patch("agent_ui_server.sandbox.shutil.which",
                            side_effect=lambda n, **kw: str(root / n)), \
                    mock.patch("agent_ui_server.sandbox.verify_network_namespace"), \
                    mock.patch("agent_ui_server.sandbox.host_addresses", return_value=[]):
                command = network_command(["/usr/bin/touch", str(marker)],
                                          [{"ip": "100.64.0.10", "port": 443}])
            shell = command.index("--") + 1
            result = subprocess.run(command[shell:], capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertNotIn("route replace", log.read_text())

    def test_nft_required_only_with_exceptions(self):
        with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=lambda n, **kwargs: None if n == "nft" else n), \
                mock.patch("agent_ui_server.sandbox.verify_network_namespace"):
            network_command(["true"])
            with self.assertRaises(FileNotFoundError):
                network_command(["true"], [{"ip": "100.64.0.10", "port": 443}])


class NetworkAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from agent_ui_server import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(Path(self.temp.name) / "settings.db")
        self.addCleanup(self.database.close)
        patch = mock.patch.object(main, "db", self.database)
        patch.start()
        self.addCleanup(patch.stop)

    async def test_adapter_forwarding_and_disabled_bypass(self):
        entries = [{"ip": "100.64.0.10", "port": 443}]
        for adapter, wrapper in ((PiAdapter(executable="pi"), "pi_sandbox_command"),
                                 (ClaudeCodeAdapter(executable="claude"), "claude_sandbox_command")):
            session = {"id": 123, "working_dir": self.temp.name,
                       "sandbox_network_allowlist": entries}
            with mock.patch(f"agent_ui_server.agent.{wrapper}", side_effect=ValueError("unsafe")) as wrap, \
                    mock.patch("agent_ui_server.agent.asyncio.create_subprocess_exec",
                               side_effect=FileNotFoundError("fixture")) as spawn:
                events = [event async for event in adapter.start_turn(session, "test")]
                self.assertEqual(wrap.call_args.kwargs["sandbox_network_allowlist"], entries)
                spawn.assert_not_called()
                self.assertIn("unsafe", events[0]["message"])
                wrap.reset_mock()
                session["sandbox"] = False
                events = [event async for event in adapter.start_turn(session, "test")]
                wrap.assert_not_called()
                spawn.assert_called_once()

    async def test_running_turn_keeps_snapshot_next_turn_uses_new_setting(self):
        first = [{"ip": "100.64.0.10", "port": 443}]
        second = [{"ip": "100.64.0.10", "port": 22}]
        self.database.set_sandbox_network_allowlist(first)
        project = self.database.create_project(self.temp.name, "fixture")
        session = self.database.create_session("fixture", project["id"], agent="pi")
        started, release = asyncio.Event(), asyncio.Event()
        snapshots = []

        class Adapter:
            async def start_turn(adapter, session, prompt):
                snapshots.append(session["sandbox_network_allowlist"])
                started.set()
                await release.wait()
                if False:
                    yield {}

        with mock.patch.dict(self.main.adapters, {"pi": Adapter()}):
            task = asyncio.create_task(self.main.run_turn(session["id"], "first"))
            await asyncio.wait_for(started.wait(), 2)
            try:
                self.database.set_sandbox_network_allowlist(second)
                self.assertEqual(snapshots, [first])
            finally:
                release.set()
                await task
            await self.main.run_turn(session["id"], "second")
        self.assertEqual(snapshots, [first, second])

    async def test_persistence_replace_clear_and_invalid_update(self):
        self.assertEqual(await self.main.get_sandbox_network(), {"sandbox_network_allowlist": []})
        entries = [{"ip": "100.64.0.10", "port": 443}]
        request = self.main.UpdateSandboxNetworkRequest(sandbox_network_allowlist=entries * 2)
        self.assertEqual(await self.main.update_sandbox_network(request), {"sandbox_network_allowlist": entries})
        reopened = Database(self.database.path)
        try:
            self.assertEqual(reopened.get_sandbox_network_allowlist(), entries)
        finally:
            reopened.close()
        bad = self.main.UpdateSandboxNetworkRequest(sandbox_network_allowlist=[{"ip": "localhost", "port": 443}])
        with self.assertRaises(HTTPException) as error:
            await self.main.update_sandbox_network(bad)
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(self.database.get_sandbox_network_allowlist(), entries)
        for values in ({}, {"sandbox_network_allowlist": [{"ip": "100.64.0.10", "port": "443"}]}):
            with self.assertRaises(ValidationError):
                self.main.UpdateSandboxNetworkRequest(**values)
        request = self.main.UpdateSandboxNetworkRequest(sandbox_network_allowlist=[])
        self.assertEqual(await self.main.update_sandbox_network(request), {"sandbox_network_allowlist": []})
