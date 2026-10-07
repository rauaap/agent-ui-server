"""Terminal failures must not be translated into silent success."""
import asyncio
import json
import sys
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

from agent_ui_server.agent import ClaudeCodeAdapter


class ClaudeFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_results_emit_error_then_done(self):
        adapter = ClaudeCodeAdapter(session_operation=mock.AsyncMock())
        for fields, expected in [
            ({"is_error": True, "errors": ["quota exceeded", "try later"]}, "quota exceeded\ntry later"),
            ({"subtype": "error_max_turns"}, "error_max_turns"),
            ({"is_error": True, "result": "authentication failed"}, "authentication failed"),
            ({"is_error": True}, "Unknown error"),
        ]:
            with self.subTest(fields=fields):
                events = [e async for e in adapter._events_from_json(
                    1, None, {"type": "result", "session_id": "resume", **fields}
                )]
                self.assertEqual(events, [
                    {"type": "error", "message": f"Claude Code failed: {expected}"},
                    {"type": "done"},
                ])

    async def test_init_reports_session_id(self):
        events = [e async for e in ClaudeCodeAdapter(session_operation=mock.AsyncMock())._events_from_json(
            1, None, {"type": "system", "subtype": "init", "session_id": "resume"}
        )]
        self.assertEqual(events, [{"type": "session", "session_id": "resume"}])

    async def test_success_result_is_not_an_error(self):
        events = [e async for e in ClaudeCodeAdapter(session_operation=mock.AsyncMock())._events_from_json(
            1, None, {"type": "result", "subtype": "success", "is_error": False}
        )]
        self.assertEqual([e["type"] for e in events], ["done"])

    async def test_failed_result_and_nonzero_exit_report_once(self):
        original = asyncio.create_subprocess_exec
        result = json.dumps({"type": "result", "is_error": True,
                             "errors": ["quota exceeded"], "session_id": "resume"})

        async def spawn(*args, **kwargs):
            script = '''import sys, json
init = json.loads(sys.stdin.readline())
print(json.dumps({"type": "control_response", "response": {
    "subtype": "success", "request_id": init["request_id"], "response": {}}}), flush=True)
sys.stdin.readline()
''' + f"print({result!r}, flush=True); sys.exit(1)"
            return await original(sys.executable, "-c", script, **kwargs)

        with tempfile.TemporaryDirectory() as cwd, patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec", spawn
        ):
            events = [e async for e in ClaudeCodeAdapter(session_operation=mock.AsyncMock(), executable=sys.executable).start_turn(
                {"id": 1, "working_dir": cwd, "sandbox": False, "agent_session_id": None}, "hi"
            )]
        self.assertEqual([e["type"] for e in events], ["error", "done"])
        self.assertIn("quota exceeded", events[0]["message"])

    async def test_clean_exit_without_result_is_an_error(self):
        original = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            script = '''import sys, json
init = json.loads(sys.stdin.readline())
print(json.dumps({"type": "control_response", "response": {
    "subtype": "success", "request_id": init["request_id"], "response": {}}}), flush=True)
sys.stdin.readline()
'''
            return await original(sys.executable, "-c", script, **kwargs)

        with tempfile.TemporaryDirectory() as cwd, patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec", spawn
        ):
            events = [e async for e in ClaudeCodeAdapter(session_operation=mock.AsyncMock(), executable=sys.executable).start_turn(
                {"id": 1, "working_dir": cwd, "sandbox": False, "agent_session_id": None}, "hi"
            )]
        self.assertTrue(any(e["type"] == "error" and "without a result" in e["message"] for e in events))
        self.assertFalse(any(e["type"] == "done" for e in events))
