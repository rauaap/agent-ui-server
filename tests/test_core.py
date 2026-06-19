from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent import ClaudeCodeAdapter, OpenCodeAdapter
from db import Database


class DatabaseTests(unittest.TestCase):
    def test_session_and_scrollback_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            session = database.create_session(
                name="demo",
                working_dir="/projects/demo",
                agent="claude-code",
            )

            self.assertEqual(session["status"], "idle")
            self.assertEqual(session["name"], "demo")

            database.update_status(session["id"], "running")
            database.append_scrollback(session["id"], "input", {"text": "hello"})
            database.append_scrollback(
                session["id"],
                "output",
                {"text": "world"},
            )

            rows = database.recent_scrollback(session["id"])
            self.assertEqual([row["type"] for row in rows], ["input", "output"])
            self.assertEqual(rows[1]["payload"], {"text": "world"})

            database.reset_active_sessions()
            self.assertEqual(database.require_session(session["id"])["status"], "idle")
            database.close()

    def test_rename_session_updates_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            session = database.create_session(
                name="demo",
                working_dir="/projects/demo",
                agent="claude-code",
            )

            renamed = database.rename_session(session["id"], "renamed")
            self.assertEqual(renamed["name"], "renamed")
            self.assertEqual(
                database.require_session(session["id"])["name"], "renamed"
            )

            with self.assertRaises(KeyError):
                database.rename_session("missing", "nope")
            database.close()


class ClaudeCodeAdapterParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ClaudeCodeAdapter(executable="claude")

    def test_assistant_text_blocks_become_output_events(self) -> None:
        events = self.adapter._assistant_events(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pwd"},
                        },
                    ]
                },
            }
        )

        self.assertEqual(events[0], {"type": "output", "text": "hello"})
        self.assertEqual(
            events[1],
            {"type": "tool_use", "tool": "Bash", "input": {"command": "pwd"}},
        )

    def test_permission_request_normalizes_to_approval_event(self) -> None:
        event = self.adapter._approval_request_event(
            {
                "type": "sdk_control_request",
                "request": {
                    "subtype": "permission",
                    "request_id": "perm_1",
                    "tool_name": "Bash",
                    "input": {"command": "rm -rf /tmp/demo"},
                },
            }
        )

        self.assertEqual(
            event,
            {
                "type": "approval_request",
                "request_id": "perm_1",
                "tool": "Bash",
                "input": {"command": "rm -rf /tmp/demo"},
            },
        )

    def test_can_use_tool_control_request_normalizes_to_approval_event(self) -> None:
        event = self.adapter._approval_request_event(
            {
                "type": "control_request",
                "request_id": "1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Write",
                    "input": {"file_path": "/projects/demo/a.txt", "content": "hi"},
                },
            }
        )

        self.assertEqual(
            event,
            {
                "type": "approval_request",
                "request_id": "1",
                "tool": "Write",
                "input": {"file_path": "/projects/demo/a.txt", "content": "hi"},
            },
        )

    def test_result_session_id_is_extracted_from_nested_payload(self) -> None:
        session_id = self.adapter._extract_session_id(
            {"type": "result", "result": {"session_id": "abc123"}}
        )

        self.assertEqual(session_id, "abc123")


class OpenCodeAdapterParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenCodeAdapter(executable="opencode")

    def test_message_chunk_classified_as_text(self) -> None:
        self.assertEqual(
            self.adapter._classify_update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hi"},
                }
            ),
            ("text", None, "hi"),
        )

    def test_tool_call_update_with_input_becomes_tool_use(self) -> None:
        kind, key, payload = self.adapter._classify_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_1",
                "title": "bash",
                "rawInput": {"command": "echo hi"},
            }
        )
        self.assertEqual(kind, "tool")
        self.assertEqual(key, "call_1")
        self.assertEqual(
            payload,
            {"type": "tool_use", "tool": "bash", "input": {"command": "echo hi"}},
        )

    def test_tool_call_without_input_is_skipped(self) -> None:
        self.assertIsNone(
            self.adapter._classify_update(
                {"sessionUpdate": "tool_call", "toolCallId": "call_1", "rawInput": {}}
            )
        )

    def test_thought_and_usage_updates_are_ignored(self) -> None:
        self.assertIsNone(
            self.adapter._classify_update(
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "thinking"},
                }
            )
        )
        self.assertIsNone(
            self.adapter._classify_update({"sessionUpdate": "usage_update", "used": 5})
        )

    def test_select_option_prefers_allow_once_and_reject_once(self) -> None:
        options = [
            {"optionId": "once", "kind": "allow_once"},
            {"optionId": "always", "kind": "allow_always"},
            {"optionId": "reject", "kind": "reject_once"},
        ]
        self.assertEqual(self.adapter._select_option(options, "allow"), "once")
        self.assertEqual(self.adapter._select_option(options, "deny"), "reject")


if __name__ == "__main__":
    unittest.main()
