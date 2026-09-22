"""Protocol-level prototype tests; these do not substitute for a real Claude CLI test."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_ui_server.actions import auto_approval_setting
from agent_ui_server.agent import ApprovalDecision, ClaudeCodeAdapter
from agent_ui_server.host_tools import HostTools, SANDBOX_GUIDANCE, TOOL_NAME
from agent_ui_server.sandbox import SandboxFilesystem, SandboxMount


def fake_sandbox(command, cwd, *, system_prompt=None, **kwargs):
    filesystem = SandboxFilesystem(Path(cwd), [SandboxMount("--bind", cwd, cwd)])
    return [*command, *(["--append-system-prompt", system_prompt(filesystem)] if system_prompt else [])]


class HostToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = ClaudeCodeAdapter()
        self.process = SimpleNamespace(
            stdin=SimpleNamespace(write=mock.Mock(), drain=mock.AsyncMock()),
            stdout=asyncio.StreamReader(),
        )
        self.host = HostTools(self.process, "/project",
                              lambda args, call_id: self.adapter._approve_host(1, args, call_id))
        self.adapter.host_tools[1] = self.host

    async def asyncTearDown(self):
        await self.host.close()

    def request(self, method="tools/call", **params):
        return {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}

    def call(self, **args):
        return self.request(name="bypass_sandbox", arguments=args or {
            "command": "echo hello", "reason": "testing",
        }, _meta={"claudecode/toolUseId": "toolu_host"})

    async def test_discovery_and_protocol_errors(self):
        result = await self.host.dispatch(self.request("initialize"))
        self.assertEqual(result["result"]["capabilities"], {"tools": {}})
        result = await self.host.dispatch(self.request("tools/list"))
        self.assertEqual(result["result"]["tools"][0]["name"], "bypass_sandbox")
        self.assertNotIn("_meta", result["result"]["tools"][0])
        self.assertEqual((await self.host.dispatch(self.request("ping")))["result"], {})
        self.assertIsNone(await self.host.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        for request, code in ((None, -32600), (self.request("nope"), -32601),
                              (self.call(command="echo", reason=""), -32602),
                              (self.call(command="echo", reason="ok", cwd="/"), -32602),
                              (self.call(command=2, reason="ok"), -32602),
                              (self.request(name="other", arguments={}), -32602)):
            self.assertEqual((await self.host.dispatch(request))["error"]["code"], code)
        self.assertEqual(self.adapter.pending_approvals, {})

    async def test_approval_required_and_result_returned(self):
        for behavior in ("allow", "deny"):
            with mock.patch("agent_ui_server.host_tools.run_command", new_callable=mock.AsyncMock) as run:
                run.return_value = {"stdout": "hello", "stderr": "", "exit_code": 0, "timed_out": False}
                task = asyncio.create_task(self.host.dispatch(self.call()))
                event = await asyncio.wait_for(self.host.events.get(), 1)
                self.assertEqual(event["type"], "approval_request")
                self.assertEqual(event["call_id"], "toolu_host")
                self.assertNotEqual(event["request_id"], "toolu_host")
                self.assertIsNone(auto_approval_setting(event["action"]))
                self.assertEqual(event["action"]["arguments"]["cwd"], "/project")
                run.assert_not_awaited()
                with self.assertRaises(KeyError):
                    await self.adapter.send_approval({"id": 2}, event["request_id"], "allow")
                await self.adapter.send_approval({"id": 1}, event["request_id"], behavior)
                response = await task
                if behavior == "allow":
                    run.assert_awaited_once_with("echo hello", "/project")
                    self.assertFalse(response["result"]["isError"])
                else:
                    run.assert_not_awaited()
                    self.assertTrue(response["result"]["isError"])
                self.assertEqual(self.adapter.pending_approvals, {})

    async def test_native_permission_does_not_double_prompt(self):
        event = {"type": "control_request", "request_id": "p1", "request": {
            "subtype": "can_use_tool", "tool_name": TOOL_NAME, "input": {},
        }}
        self.assertEqual([e async for e in self.adapter._events_from_json(1, self.process, event)], [])
        reply = json.loads(self.process.stdin.write.call_args.args[0])
        self.assertEqual(reply["response"]["response"]["behavior"], "allow")

    def feed(self, event):
        self.process.stdout.feed_data((json.dumps(event) + "\n").encode())

    async def start_host(self):
        task = asyncio.create_task(self.host.start())
        await asyncio.sleep(0)
        self.feed({"type": "control_response", "response": {
            "subtype": "success", "request_id": self.host.init_id, "response": {},
        }})
        await task

    async def test_reader_continues_during_approval_and_cancellation(self):
        await self.start_host()
        self.feed({"type": "control_request", "request_id": "call1", "request": {
            "subtype": "mcp_message", "server_name": "agent_ui", "message": self.call(),
        }})
        event = await asyncio.wait_for(self.host.events.get(), 1)
        self.assertEqual(event["type"], "approval_request")
        self.feed({"type": "system", "message": "still reading"})
        forwarded = await asyncio.wait_for(self.host.events.get(), 1)
        self.assertEqual(json.loads(forwarded)["type"], "system")
        call = self.host.calls["call1"]
        self.feed({"type": "control_cancel_request", "request_id": "call1"})
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertEqual(self.adapter.pending_approvals, {})

    async def test_close_cancels_execution(self):
        await self.start_host()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def execute(*args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        with mock.patch("agent_ui_server.host_tools.run_command", side_effect=execute):
            self.feed({"type": "control_request", "request_id": "call1", "request": {
                "subtype": "mcp_message", "server_name": "agent_ui", "message": self.call(),
            }})
            event = await asyncio.wait_for(self.host.events.get(), 1)
            await self.adapter.send_approval({"id": 1}, event["request_id"], "allow")
            await asyncio.wait_for(started.wait(), 1)
            await self.host.close()
            self.assertTrue(cancelled.is_set())

    async def test_initialization_failure(self):
        self.process.stdout.feed_eof()
        with self.assertRaisesRegex(RuntimeError, "before host-tool initialization"):
            await self.host.start()

    async def test_control_envelope(self):
        await self.host._respond("outer1", {
            "server_name": "agent_ui", "message": self.request("tools/list"),
        })
        response = json.loads(self.process.stdin.write.call_args.args[0])
        self.assertEqual(response["response"]["request_id"], "outer1")
        self.assertEqual(response["response"]["response"]["mcp_response"]["id"], 7)

    async def test_real_shell_execution(self):
        self.host.approve = mock.AsyncMock(return_value=ApprovalDecision("allow"))
        with tempfile.TemporaryDirectory() as tmp:
            self.host.cwd = tmp
            result = await self.host.dispatch(self.call(command="printf host > proof; pwd", reason="test"))
            self.assertEqual(Path(tmp, "proof").read_text(), "host")
            output = json.loads(result["result"]["content"][0]["text"])
            self.assertEqual(output["stdout"].strip(), tmp)

    async def test_end_to_end_subprocess_pipes(self):
        fixture = str(Path(__file__).parent / "fixtures" / "claude_host_exec.py")
        for behavior in ("allow", "deny"):
            adapter = ClaudeCodeAdapter(executable=fixture)
            # Exercise the sandboxed-session branch without requiring Bubblewrap
            # to launch the simulated Claude peer in this protocol test.
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"CLAUDE_HOST_EXEC": "1"}
            ), mock.patch(
                "agent_ui_server.agent.claude_sandbox_command",
                side_effect=fake_sandbox,
            ):
                session = {"id": 5, "working_dir": tmp, "sandbox": True}
                events = []
                async with asyncio.timeout(5):
                    async for event in adapter.start_turn(session, "test"):
                        events.append(event)
                        if event["type"] == "approval_request":
                            await adapter.send_approval(session, event["request_id"], behavior)
                self.assertEqual(sum(e["type"] == "approval_request" for e in events), 1)
                self.assertFalse(any(e["type"] == "error" for e in events), events)
                output = json.loads(next(e["text"] for e in events if e["type"] == "output"))
                self.assertEqual(output["isError"], behavior == "deny")
                if behavior == "allow":
                    self.assertEqual(json.loads(output["content"][0]["text"])["stdout"], "prototype")
                self.assertEqual(events[-1], {"type": "done", "session_id": "fake-host-session"})
                self.assertEqual(adapter.host_tools, {})
                self.assertEqual(adapter.pending_approvals, {})

    async def test_host_tool_and_guidance_gated_on_fresh_and_resumed_turns(self):
        for enabled in (False, True):
            for sandbox in (None, False, True):
                for resumed in (False, True):
                    with self.subTest(enabled=enabled, sandbox=sandbox, resumed=resumed), mock.patch.dict(
                        os.environ, {"CLAUDE_HOST_EXEC": "1" if enabled else "0"}
                    ), mock.patch(
                        "agent_ui_server.agent.claude_sandbox_command",
                        side_effect=fake_sandbox,
                    ), mock.patch(
                        "agent_ui_server.agent.asyncio.create_subprocess_exec",
                        side_effect=FileNotFoundError("test"),
                    ) as spawn:
                        session = {"id": 1, "working_dir": "/project"}
                        if sandbox is not None:
                            session["sandbox"] = sandbox
                        if resumed:
                            session["agent_session_id"] = "previous-session"
                        _ = [e async for e in self.adapter.start_turn(session, "hello")]
                        argv = spawn.call_args.args
                        expected = enabled and sandbox is not False
                        self.assertEqual("--append-system-prompt" in argv, expected)
                        if expected:
                            guidance = argv[argv.index("--append-system-prompt") + 1]
                            self.assertIn(SANDBOX_GUIDANCE, guidance)
                            self.assertIn('Working directory: "/project"', guidance)
                            for phrase in (TOOL_NAME, "Bubblewrap", "ToolSearch",
                                           "dangerouslyDisableSandbox", "user approval"):
                                self.assertIn(phrase, guidance)
                        self.assertEqual("--mcp-config" in argv, expected)
                        self.assertEqual("--resume" in argv, resumed)

    async def test_adapter_launch_configuration(self):
        with mock.patch.dict(os.environ, {"CLAUDE_HOST_EXEC": "1"}), mock.patch(
            "agent_ui_server.agent.claude_sandbox_command",
            side_effect=fake_sandbox,
        ), mock.patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec", side_effect=FileNotFoundError("test")
        ) as spawn:
            _ = [e async for e in self.adapter.start_turn(
                {"id": 1, "working_dir": "/project", "sandbox": True}, "hello")]
            argv = spawn.call_args.args
            config = json.loads(argv[argv.index("--mcp-config") + 1])
            self.assertEqual(config, {"mcpServers": {"agent_ui": {"type": "sdk", "name": "agent_ui"}}})
