import asyncio
from collections import defaultdict
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from agent_ui_server import main
from agent_ui_server.actions import auto_approval_setting
from agent_ui_server.agent import ApprovalDecision, ClaudeCodeAdapter, PiAdapter
from agent_ui_server.db import Database
from agent_ui_server.session_tools import (
    MODELS, delivery_prompt, execute_session_tool, validate_session_arguments,
)


CALLS = {
    "message_session": {"session_id": 8, "message": "hello"},
    "start_session": {"name": "Review", "project_path": "/project", "message": "hello"},
    "read_session": {"session_id": 8, "limit": 2},
}


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_arguments_never_approve(self):
        approve, operation = mock.AsyncMock(), mock.AsyncMock()
        for name, args in [
            ("nope", {}), ([], {}),
            ("read_session", {"session_id": True}),
            ("read_session", {"session_id": 1, "limit": 1001}),
            ("read_session", {"session_id": 1, "after": -1}),
            ("message_session", {"session_id": "1", "message": "hi"}),
            ("message_session", {"session_id": 1, "message": ""}),
            ("start_session", {**CALLS["start_session"], "source": {"type": "user"}}),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                await execute_session_tool(name, args, 7, operation, approve)
        approve.assert_not_awaited()
        operation.assert_not_awaited()

    async def test_errors_become_tool_results(self):
        result = await execute_session_tool(
            "read_session", {"session_id": 8}, 7,
            mock.AsyncMock(side_effect=RuntimeError("missing")),
            mock.AsyncMock(return_value=ApprovalDecision("allow")),
        )
        self.assertTrue(result["isError"])
        self.assertIn("missing", result["content"][0]["text"])


class SessionOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.project = self.db.create_project(self.tmp.name, "test")
        self.sender = self.db.create_session("sender", self.project["id"], "claude-code")
        self.target = self.db.create_session("target", self.project["id"], "claude-code")
        self.adapter = mock.Mock()
        self.prompts = []

        async def turn(session, prompt):
            self.prompts.append(prompt)
            yield {"type": "done"}
        self.adapter.start_turn = turn
        self.events = []
        for patch in (
            mock.patch.object(main, "db", self.db),
            mock.patch.object(main, "adapters", {"claude-code": self.adapter}),
            mock.patch.object(main, "running_tasks", {}),
            mock.patch.object(main, "stream_locks", defaultdict(asyncio.Lock)),
            mock.patch.object(main, "turn_lock", asyncio.Lock()),
            mock.patch.object(main, "enqueue_for_subscribers", side_effect=lambda sid, events: self.events.extend(events) or []),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def finish(self):
        await asyncio.gather(*list(main.running_tasks.values()))

    async def test_message_provenance_live_history_and_delivery(self):
        sid = self.target["id"]
        for sender in (None, self.sender["id"]):
            mid = await main.begin_turn(sid, "original", sender_session_id=sender)
            await self.finish()
            source = {"type": "user"} if sender is None else {"type": "agent", "session_id": sender}
            row = next(r for r in self.db.recent_scrollback(sid) if r["id"] == mid)
            self.assertEqual(row["payload"], {"text": "original", "source": source})
            self.assertIn({"type": "input", **row["payload"]}, self.events)
            self.assertEqual(self.prompts[-1], delivery_prompt("original", source))
            page = await main.session_tool_operation(self.sender["id"], "read_session", {"session_id": sid})
            self.assertIn(row, page["messages"])
            self.assertEqual(page, await main.get_scrollback(sid, after=None, limit=200))
        legacy = self.db.append_scrollback(sid, "input", {"text": "old"})
        self.assertEqual(delivery_prompt("old", legacy["payload"].get("source")), "old")

    async def test_start_and_followup_use_same_agent_source(self):
        result = await main.session_tool_operation(self.sender["id"], "start_session", {
            "name": "new", "project_path": self.tmp.name, "message": "first",
        })
        await self.finish()
        followup = await main.session_tool_operation(self.sender["id"], "message_session", {
            "session_id": result["session_id"], "message": "second",
        })
        await self.finish()
        inputs = [r for r in self.db.recent_scrollback(result["session_id"]) if r["type"] == "input"]
        self.assertEqual([r["id"] for r in inputs], [result["message_id"], followup])
        for row in inputs:
            self.assertEqual(row["payload"]["source"], {"type": "agent", "session_id": self.sender["id"]})

    async def test_partial_creation_failure_retains_session(self):
        with mock.patch.object(main, "begin_turn", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, r"Session \d+ was created.*failed"):
                await main.session_tool_operation(self.sender["id"], "start_session", {
                    "name": "retained", "project_path": self.tmp.name, "message": "first",
                })
        self.assertTrue(any(s["name"] == "retained" for s in self.db.list_sessions()))

    async def test_missing_busy_and_archived_target(self):
        for sid in (999, self.target["id"]):
            if sid != 999:
                self.db.update_status(sid, "running")
            with self.assertRaises(HTTPException):
                await main.session_tool_operation(self.sender["id"], "message_session", {
                    "session_id": sid, "message": "hello",
                })
        self.db.update_status(self.target["id"], "idle")
        self.db.set_session_archived(self.target["id"], True)
        with self.assertRaises(HTTPException):
            await main.begin_turn(self.target["id"], "hello", sender_session_id=self.sender["id"])
        self.assertEqual(self.prompts, [])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_independent_of_sandbox_and_host_flags(self):
        for cls, sandbox_fn, flag in (
            (ClaudeCodeAdapter, "claude_sandbox_command", "--mcp-config"),
            (PiAdapter, "pi_sandbox_command", "--agent-ui-session-tools"),
        ):
            adapter = cls()
            adapter.session_operation = mock.AsyncMock()
            for sandbox in (False, True):
                for host in ("0", "1"):
                    with self.subTest(adapter=cls.__name__, sandbox=sandbox, host=host), mock.patch.dict(
                        os.environ, {"CLAUDE_HOST_EXEC": host, "PI_HOST_EXEC": host},
                    ), mock.patch(
                        "agent_ui_server.agent." + sandbox_fn,
                        side_effect=lambda command, *a, **kw: command,
                    ), mock.patch(
                        "agent_ui_server.agent.asyncio.create_subprocess_exec",
                        side_effect=FileNotFoundError("test"),
                    ) as spawn:
                        _ = [e async for e in adapter.start_turn(
                            {"id": 7, "sandbox": sandbox, "working_dir": "/project"}, "hi",
                        )]
                        self.assertIn(flag, spawn.call_args.args)

    async def test_stop_cancels_approved_execution(self):
        fixture = str(Path(__file__).parent / "fixtures/session_tools.py")
        for cls in (ClaudeCodeAdapter, PiAdapter):
            with self.subTest(adapter=cls.__name__), tempfile.TemporaryDirectory() as tmp:
                adapter = cls(executable=fixture)
                if isinstance(adapter, PiAdapter):
                    adapter.web_extension_path = ""
                started, cancelled = asyncio.Event(), asyncio.Event()

                async def operation(*args):
                    started.set()
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()

                adapter.session_operation = operation
                session = {"id": 7, "working_dir": tmp, "sandbox": False}

                async def consume():
                    async for event in adapter.start_turn(session, json.dumps({
                        "name": "read_session", "arguments": {"session_id": 8},
                    })):
                        if event["type"] == "approval_request":
                            await adapter.send_approval(session, event["request_id"], "allow")

                task = asyncio.create_task(consume())
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await adapter.stop(session)
                    await asyncio.wait_for(task, 5)
                    self.assertTrue(cancelled.is_set())
                    self.assertEqual(adapter.pending_approvals, {})
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_pi_requires_session_capability_handshake(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "old-pi"
            stub.write_text('''#!/usr/bin/env python3
import json, time
print(json.dumps({"type":"extension_ui_request", "method":"notify", "id":"ready",
 "message":json.dumps({"agent-ui":1,"kind":"ready"})}), flush=True)
time.sleep(60)
''')
            stub.chmod(0o755)
            adapter = PiAdapter(executable=str(stub), web_extension_path="")
            adapter.session_operation = mock.AsyncMock()
            async with asyncio.timeout(5):
                events = [e async for e in adapter.start_turn(
                    {"id": 7, "working_dir": tmp, "sandbox": False}, "hi",
                )]
            self.assertIn("did not register session tools", events[0]["message"])
            adapter.session_operation.assert_not_awaited()

    async def test_both_transports_approve_deny_cancel_and_validate(self):
        fixture = str(Path(__file__).parent / "fixtures/session_tools.py")
        with tempfile.TemporaryDirectory() as tmp:
            for cls in (ClaudeCodeAdapter, PiAdapter):
                for name in MODELS:
                    for behavior in ("allow", "deny", "cancel", "invalid"):
                        with self.subTest(adapter=cls.__name__, name=name, behavior=behavior):
                            adapter = cls(executable=fixture)
                            if isinstance(adapter, PiAdapter):
                                adapter.web_extension_path = ""
                            operation = mock.AsyncMock(return_value={"test": "result"})
                            adapter.session_operation = operation
                            session = {"id": 7, "working_dir": tmp, "sandbox": False}
                            args = dict(CALLS[name])
                            if behavior == "invalid":
                                args["sender_id"] = 999
                            events = []
                            with mock.patch.dict(os.environ, {"CLAUDE_HOST_EXEC": "0", "PI_HOST_EXEC": "0"}):
                                async with asyncio.timeout(5):
                                    async for event in adapter.start_turn(session, json.dumps({"name": name, "arguments": args})):
                                        events.append(event)
                                        if event["type"] == "approval_request":
                                            operation.assert_not_awaited()
                                            self.assertEqual(event["action"]["name"], name)
                                            self.assertIsNone(auto_approval_setting(event["action"]))
                                            with self.assertRaises(KeyError):
                                                await adapter.send_approval({"id": 9}, event["request_id"], "allow")
                                            if behavior == "cancel":
                                                process = adapter.processes[7]
                                                process.stdin.write(b'{"type":"test_cancel"}\n')
                                                await process.stdin.drain()
                                            else:
                                                await adapter.send_approval(session, event["request_id"], behavior)
                            self.assertFalse(any(e["type"] == "error" for e in events), events)
                            self.assertEqual(sum(e["type"] == "approval_request" for e in events), int(behavior != "invalid"))
                            if behavior == "allow":
                                operation.assert_awaited_once_with(7, name, validate_session_arguments(name, args))
                                self.assertIn("result", str(events))
                            else:
                                operation.assert_not_awaited()
                            self.assertEqual(adapter.pending_approvals, {})
                            self.assertEqual(adapter.pending_sessions, {})
