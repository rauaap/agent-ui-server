"""Required native protocol fields and malformed streams must not be papered over."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_ui_server.agent import ClaudeCodeAdapter, PiAdapter, _normalize_questions, _signal_group
from agent_ui_server.host_tools import ServerTools
from agent_ui_server.main import is_empty_or_missing
from agent_ui_server.session_tools import MODELS, delivery_prompt


class AdapterConfigurationTests(unittest.TestCase):
    def test_empty_executable_override_is_not_replaced(self):
        for cls, env_name in ((ClaudeCodeAdapter, "CLAUDE_BIN"), (PiAdapter, "PI_BIN")):
            with self.subTest(adapter=cls.__name__), mock.patch.dict("os.environ", {env_name: "/configured"}):
                self.assertEqual(cls(executable="", session_operation=mock.AsyncMock()).executable, "")
                self.assertEqual(cls(session_operation=mock.AsyncMock()).executable, "/configured")

    def test_empty_required_extension_is_not_replaced(self):
        with mock.patch.dict("os.environ", {"PI_EXTENSION": "/configured.ts"}):
            self.assertEqual(PiAdapter(extension_path="", session_operation=mock.AsyncMock()).extension_path, "")
            self.assertEqual(PiAdapter(session_operation=mock.AsyncMock()).extension_path, "/configured.ts")
        with mock.patch.dict("os.environ", {"PI_EXTENSION": ""}):
            self.assertEqual(PiAdapter(session_operation=mock.AsyncMock()).extension_path, "")


class PendingStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_approval_options_are_not_replaced_with_defaults(self):
        for cls in (ClaudeCodeAdapter, PiAdapter):
            with self.subTest(adapter=cls.__name__):
                adapter = cls(session_operation=mock.AsyncMock())
                future = asyncio.get_running_loop().create_future()
                self.addCleanup(future.cancel)
                adapter.pending_approvals["permission-1"] = future
                adapter.pending_sessions["permission-1"] = 1
                with self.assertRaises(KeyError):
                    await adapter.send_approval({"id": 1}, "permission-1", "allow")
                self.assertFalse(future.done())

    async def test_missing_question_specs_are_not_replaced_with_empty_questions(self):
        for cls in (ClaudeCodeAdapter, PiAdapter):
            with self.subTest(adapter=cls.__name__):
                adapter = cls(session_operation=mock.AsyncMock())
                future = asyncio.get_running_loop().create_future()
                self.addCleanup(future.cancel)
                adapter.pending_questions["question-1"] = future
                adapter.pending_sessions["question-1"] = 1
                with self.assertRaises(KeyError):
                    await adapter.send_answer({"id": 1}, "question-1", {})
                self.assertFalse(future.done())


class ClaudeProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = ClaudeCodeAdapter(session_operation=mock.AsyncMock())

    def test_permission_uses_native_ids_and_preserves_empty_input(self):
        event = {"type": "control_request", "request_id": "permission-1", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {},
            "tool_use_id": "call-1",
        }}
        self.assertEqual(self.adapter._permission_parts(event), ("permission-1", "Bash", {}, "call-1"))
        for key in ("tool_name", "input", "tool_use_id"):
            with self.subTest(key=key), self.assertRaises(KeyError):
                self.adapter._permission_parts({**event, "request": {
                    k: v for k, v in event["request"].items() if k != key
                }})
        with self.assertRaises(KeyError):
            self.adapter._permission_parts({"request": event["request"]})

    def test_tool_blocks_require_native_fields(self):
        block = {"id": "call-1", "name": "Bash", "input": {"command": "echo hi"}}
        for key in block:
            with self.subTest(key=key), self.assertRaises(KeyError):
                self.adapter._tool_use_event({k: v for k, v in block.items() if k != key})
        self.assertEqual(self.adapter.tool_actions, {})
        with self.assertRaises(KeyError):
            self.adapter._assistant_events({"type": "assistant", "text": "legacy text"})

    async def test_legacy_sdk_permission_event_is_not_supported(self):
        event = {"type": "sdk_control_request", "request": {
            "subtype": "permission", "request_id": "old", "tool_name": "Bash",
            "input": {"command": "echo old"}, "tool_use_id": "old-call",
        }}
        self.assertEqual([e async for e in self.adapter._events_from_json(1, None, event)], [])
        self.assertEqual(self.adapter.pending_approvals, {})

    def tools(self):
        process = SimpleNamespace(
            stdin=SimpleNamespace(write=mock.Mock(), drain=mock.AsyncMock()),
            stdout=asyncio.StreamReader(),
        )
        tools = ServerTools(process, "/project", mock.AsyncMock(),
                            bypass_sandbox_enabled=False,
                            session_call=mock.AsyncMock(), asset_call=mock.AsyncMock())
        self.addAsyncCleanup(tools.close)
        return process, tools

    async def test_malformed_json_before_initialization_raises_original_error(self):
        process, tools = self.tools()
        process.stdout.feed_data(b"not JSON\n")
        process.stdout.feed_eof()
        with self.assertRaises(json.JSONDecodeError):
            await tools.start()

    async def test_malformed_json_after_initialization_is_error_not_output(self):
        process, tools = self.tools()
        process.stdout.feed_data((json.dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": tools.init_id, "response": {},
        }}) + "\nnot JSON\n").encode())
        process.stdout.feed_eof()
        await tools.start()
        event = await tools.events.get()
        self.assertEqual(event["type"], "error")
        self.assertIn("Expecting value", event["message"])
        self.assertEqual(await tools.events.get(), b"")


class PiProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_json_is_reported_before_and_after_handshake(self):
        ready = {"type": "extension_ui_request", "id": "ready", "method": "notify",
                 "message": json.dumps({"agent-ui": 1, "kind": "ready",
                                        "sessionTools": list(MODELS), "assetTool": "resolve_asset_link"})}
        for before_handshake in (False, True):
            with self.subTest(before_handshake=before_handshake), tempfile.TemporaryDirectory() as tmp:
                stub = Path(tmp) / "pi"
                stub.write_text("#!/usr/bin/env python3\nimport json, sys\n" + (
                    "print('not JSON', flush=True)\n" if before_handshake else
                    f"print({json.dumps(ready)!r}, flush=True)\n"
                    "for line in sys.stdin:\n"
                    "    if json.loads(line)['type'] == 'prompt':\n"
                    "        print('not JSON', flush=True)\n"
                    "        break\n"
                ))
                stub.chmod(0o755)
                adapter = PiAdapter(executable=str(stub), web_extension_path="",
                                    session_operation=mock.AsyncMock())
                async with asyncio.timeout(5):
                    events = [e async for e in adapter.start_turn(
                        {"id": 1, "working_dir": tmp, "sandbox": False}, "hello")]
                self.assertEqual([e["type"] for e in events], ["error"])
                self.assertIn("Expecting value", events[0]["message"])
                self.assertEqual(adapter.processes, {})


    async def test_missing_native_event_fields_surface_errors(self):
        ready = {"type": "extension_ui_request", "id": "ready", "method": "notify",
                 "message": json.dumps({"agent-ui": 1, "kind": "ready",
                                        "sessionTools": list(MODELS), "assetTool": "resolve_asset_link"})}
        cases = [
            ({"type": "tool_execution_start", "toolName": "bash", "args": {}}, "toolCallId"),
            ({"type": "tool_execution_start", "toolCallId": "call-1", "args": {}}, "toolName"),
            ({"type": "message_update", "assistantMessageEvent": {"type": "text_end"}}, "content"),
            ({"type": "extension_ui_request", "method": "select", "id": "approval-1",
              "title": json.dumps({"agent-ui": 1, "kind": "approval", "toolName": "bash"})}, "toolCallId"),
            ({"type": "extension_ui_request", "method": "select", "id": "approval-1",
              "title": json.dumps({"agent-ui": 1, "kind": "approval", "toolName": "bash",
                                   "toolCallId": "missing-call"})}, "missing-call"),
        ]
        for event, missing_key in cases:
            with self.subTest(missing_key=missing_key), tempfile.TemporaryDirectory() as tmp:
                stub = Path(tmp) / "pi"
                stub.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                                f"print({json.dumps(ready)!r}, flush=True)\n"
                                "for line in sys.stdin:\n"
                                "    if json.loads(line)['type'] == 'prompt':\n"
                                f"        print({json.dumps(event)!r}, flush=True)\n"
                                "        break\n")
                stub.chmod(0o755)
                adapter = PiAdapter(executable=str(stub), web_extension_path="",
                                    session_operation=mock.AsyncMock())
                async with asyncio.timeout(5):
                    events = [e async for e in adapter.start_turn(
                        {"id": 1, "working_dir": tmp, "sandbox": False}, "hello")]
                self.assertEqual(events, [{"type": "error", "message": repr(missing_key)}])
                self.assertEqual(adapter.pending_approvals, {})


class ProcessSignalTests(unittest.TestCase):
    def test_permission_error_is_not_suppressed(self):
        with mock.patch("agent_ui_server.agent.os.killpg", side_effect=PermissionError("denied")), self.assertRaises(PermissionError):
            _signal_group(SimpleNamespace(pid=1), 15)

    def test_exited_process_is_a_normal_cleanup_race(self):
        with mock.patch("agent_ui_server.agent.os.killpg", side_effect=ProcessLookupError):
            _signal_group(SimpleNamespace(pid=1), 15)


class StderrTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_required_stderr_pipe_is_not_empty_output(self):
        for cls in (ClaudeCodeAdapter, PiAdapter):
            with self.subTest(adapter=cls.__name__), self.assertRaises(AttributeError):
                await cls(session_operation=mock.AsyncMock())._collect_stderr(None)

    async def test_stderr_drain_does_not_swallow_cancellation(self):
        async def pending():
            await asyncio.Future()

        task = asyncio.create_task(pending())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await PiAdapter._drain(task)


class QuestionProjectionTests(unittest.TestCase):
    def test_malformed_questions_and_options_are_not_silently_dropped(self):
        for questions, error in (
            (None, TypeError), ({}, TypeError), ([], ValueError), ([None], TypeError),
            ([{"header": "Missing", "options": []}], KeyError),
            ([{"question": "Choose?", "header": "Choice", "options": [None]}], TypeError),
            ([{"question": "Choose?", "header": "Choice", "options": [{}]}], KeyError),
        ):
            with self.subTest(questions=questions), self.assertRaises(error):
                _normalize_questions(questions)


class MessageProvenanceTests(unittest.TestCase):
    def test_missing_or_unknown_source_is_not_labelled_as_user(self):
        for source, error in ((None, TypeError), ({}, KeyError), ({"type": "unknown"}, ValueError)):
            with self.subTest(source=source), self.assertRaises(error):
                delivery_prompt("hello", source)


class DirectoryPreconditionTests(unittest.TestCase):
    def test_directory_read_failure_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            Path, "iterdir", side_effect=PermissionError("denied"),
        ), self.assertRaises(PermissionError):
            is_empty_or_missing(tmp)
