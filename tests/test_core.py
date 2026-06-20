from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import asyncio

from agent import (
    ApprovalDecision,
    ClaudeCodeAdapter,
    OpenCodeAdapter,
    _event_options,
    _option_behavior,
    _resolve_decision,
)
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

    def test_auto_approve_defaults_off_and_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            session = database.create_session(
                name="demo",
                working_dir="/projects/demo",
                agent="claude-code",
            )

            # New sessions start with both toggles off, exposed as bools.
            self.assertIs(session["auto_approve_write"], False)
            self.assertIs(session["auto_approve_command"], False)

            updated = database.set_auto_approve(session["id"], command=True)
            self.assertIs(updated["auto_approve_command"], True)
            self.assertIs(updated["auto_approve_write"], False)

            # A partial update leaves the untouched toggle alone.
            updated = database.set_auto_approve(session["id"], write=True)
            self.assertIs(updated["auto_approve_write"], True)
            self.assertIs(updated["auto_approve_command"], True)

            # An empty update is a no-op that still returns the row.
            same = database.set_auto_approve(session["id"])
            self.assertIs(same["auto_approve_command"], True)

            self.assertIs(
                database.get_session(session["id"])["auto_approve_write"], True
            )
            with self.assertRaises(KeyError):
                database.set_auto_approve("missing", write=True)
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
                "options": [
                    {"id": "allow", "name": "Allow", "kind": "allow_once"},
                    {"id": "deny", "name": "Deny", "kind": "reject_once"},
                ],
                "category": "command",
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
                "options": [
                    {"id": "allow", "name": "Allow", "kind": "allow_once"},
                    {"id": "deny", "name": "Deny", "kind": "reject_once"},
                ],
                "category": "write",
            },
        )

    def test_approval_request_category_none_for_read_only_tool(self) -> None:
        # Read-only tools never reach the gate, but if one did it carries no
        # auto-approve category (it is unmapped).
        event = self.adapter._approval_request_event(
            {
                "type": "control_request",
                "request_id": "1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Read",
                    "input": {"file_path": "/projects/demo/a.txt"},
                },
            }
        )
        self.assertIsNone(event["category"])

    def test_result_session_id_is_extracted_from_nested_payload(self) -> None:
        session_id = self.adapter._extract_session_id(
            {"type": "result", "result": {"session_id": "abc123"}}
        )

        self.assertEqual(session_id, "abc123")

    def test_normalize_questions_extracts_fields_with_defaults(self) -> None:
        questions = self.adapter._normalize_questions(
            {
                "questions": [
                    {
                        "question": "Which emoji do you want?",
                        "header": "Emoji",
                        "options": [
                            {"label": "Cat", "description": "The cat emoji"},
                            {"label": "Rocket"},
                        ],
                    }
                ]
            }
        )

        self.assertEqual(
            questions,
            [
                {
                    "question": "Which emoji do you want?",
                    "header": "Emoji",
                    "multiSelect": False,
                    "options": [
                        {"label": "Cat", "description": "The cat emoji"},
                        {"label": "Rocket", "description": ""},
                    ],
                }
            ],
        )

    def test_normalize_questions_defaults_to_empty(self) -> None:
        self.assertEqual(self.adapter._normalize_questions({}), [])

    def test_assistant_events_omit_askuserquestion_tool_use(self) -> None:
        events = self.adapter._assistant_events(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "thinking"},
                        {
                            "type": "tool_use",
                            "name": "AskUserQuestion",
                            "input": {"questions": []},
                        },
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pwd"},
                        },
                    ]
                },
            }
        )

        self.assertEqual(
            events,
            [
                {"type": "output", "text": "thinking"},
                {"type": "tool_use", "tool": "Bash", "input": {"command": "pwd"}},
            ],
        )


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

    def test_kind_categories_map_mutating_acp_kinds(self) -> None:
        self.assertEqual(self.adapter.KIND_CATEGORIES.get("execute"), "command")
        self.assertEqual(self.adapter.KIND_CATEGORIES.get("edit"), "write")
        self.assertEqual(self.adapter.KIND_CATEGORIES.get("delete"), "write")
        self.assertEqual(self.adapter.KIND_CATEGORIES.get("move"), "write")
        # Read-oriented kinds are not auto-approvable.
        self.assertIsNone(self.adapter.KIND_CATEGORIES.get("read"))
        self.assertIsNone(self.adapter.KIND_CATEGORIES.get("fetch"))

    def test_denial_followup_prompt_restates_tool_and_reason(self) -> None:
        prompt = self.adapter._denial_followup_prompt(
            "bash", {"command": "rm -rf build/"}, "Use a dry run first."
        )
        self.assertEqual(
            prompt,
            'I denied your request to run the bash tool with input '
            '{"command": "rm -rf build/"}. Use a dry run first.',
        )

    def test_denial_followup_prompt_omits_empty_input(self) -> None:
        prompt = self.adapter._denial_followup_prompt("bash", {}, "No shell please.")
        self.assertEqual(
            prompt, "I denied your request to run the bash tool. No shell please."
        )


class ApprovalDecisionHelperTests(unittest.TestCase):
    OPTIONS = [
        {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "always", "name": "Allow always", "kind": "allow_always"},
        {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
    ]

    def test_option_behavior_maps_kind_to_behavior(self) -> None:
        self.assertEqual(_option_behavior(self.OPTIONS, "always"), "allow")
        self.assertEqual(_option_behavior(self.OPTIONS, "reject"), "deny")
        self.assertIsNone(_option_behavior(self.OPTIONS, "missing"))

    def test_event_options_projects_wire_shape(self) -> None:
        self.assertEqual(
            _event_options(self.OPTIONS),
            [
                {"id": "once", "name": "Allow once", "kind": "allow_once"},
                {"id": "always", "name": "Allow always", "kind": "allow_always"},
                {"id": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        )

    def test_resolve_decision_prefers_option_id_over_behavior(self) -> None:
        decision = _resolve_decision(self.OPTIONS, "deny", "always", None)
        self.assertEqual(
            decision, ApprovalDecision(behavior="allow", option_id="always")
        )

    def test_resolve_decision_keeps_deny_message(self) -> None:
        decision = _resolve_decision([], "deny", None, "use sudo instead")
        self.assertEqual(decision.behavior, "deny")
        self.assertEqual(decision.message, "use sudo instead")

    def test_resolve_decision_rejects_unknown_option(self) -> None:
        with self.assertRaises(KeyError):
            _resolve_decision(self.OPTIONS, "", "nope", None)

    def test_resolve_decision_rejects_bad_behavior(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_decision([], "maybe", None, None)


class SendApprovalTests(unittest.IsolatedAsyncioTestCase):
    def _arm(self, adapter, request_id: str, options: list[dict]) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        adapter.pending_approvals[request_id] = future
        adapter.pending_sessions[request_id] = "s1"
        adapter.pending_options[request_id] = options
        return future

    async def test_claude_deny_with_message(self) -> None:
        adapter = ClaudeCodeAdapter(executable="claude")
        future = self._arm(adapter, "perm_1", adapter.OPTIONS)

        effective = await adapter.send_approval(
            {"id": "s1"}, "perm_1", "deny", message="too risky"
        )

        self.assertEqual(effective, "deny")
        self.assertEqual(future.result(), ApprovalDecision("deny", message="too risky"))

    async def test_opencode_selects_explicit_option(self) -> None:
        adapter = OpenCodeAdapter(executable="opencode")
        options = [
            {"optionId": "always", "kind": "allow_always"},
            {"optionId": "reject", "kind": "reject_once"},
        ]
        future = self._arm(adapter, "perm_2", options)

        effective = await adapter.send_approval(
            {"id": "s1"}, "perm_2", "", option_id="always"
        )

        self.assertEqual(effective, "allow")
        self.assertEqual(future.result().option_id, "always")


class _AutoApproveAdapter:
    """Minimal adapter: emit one approval_request, then finish on approval.

    Mirrors the real adapters' contract closely enough to drive main.run_turn:
    register a pending future before yielding the request, block on it, and
    complete once send_approval resolves it.
    """

    def __init__(self, category: str) -> None:
        self.category = category
        self.future: asyncio.Future[ApprovalDecision] | None = None
        self.effective: str | None = None

    async def start_turn(self, session, prompt):
        self.future = asyncio.get_running_loop().create_future()
        yield {
            "type": "approval_request",
            "request_id": "perm_1",
            "tool": "Bash",
            "input": {"command": "ls"},
            "options": [],
            "category": self.category,
        }
        await self.future
        yield {"type": "done", "session_id": "agent-1"}

    async def send_approval(self, session, request_id, behavior, *, option_id=None, message=None):
        self.effective = behavior
        if self.future and not self.future.done():
            self.future.set_result(ApprovalDecision(behavior=behavior))
        return behavior

    async def stop(self, session) -> None:
        pass


class RunTurnAutoApproveTests(unittest.IsolatedAsyncioTestCase):
    async def _drive(self, *, auto_command: bool, category: str):
        import main

        with tempfile.TemporaryDirectory() as tmpdir:
            main.db = Database(Path(tmpdir) / "sessions.db")
            session = main.db.create_session(
                name="demo", working_dir=tmpdir, agent="fake"
            )
            if auto_command:
                main.db.set_auto_approve(session["id"], command=True)

            adapter = _AutoApproveAdapter(category)
            main.adapters["fake"] = adapter

            events: list[dict] = []

            async def fake_broadcast(session_id, message):
                events.append(message)

            original = main.broadcast
            main.broadcast = fake_broadcast
            try:
                await main.run_turn(session["id"], "go")
            finally:
                main.broadcast = original
                main.adapters.pop("fake", None)
                status = main.db.require_session(session["id"])["status"]
                main.db.close()
            return events, adapter, status

    async def test_matching_category_is_auto_approved(self) -> None:
        events, adapter, status = await self._drive(
            auto_command=True, category="command"
        )

        # The request goes out marked auto, immediately followed by an
        # allow response carrying the auto flag — and the agent was answered.
        request = next(e for e in events if e["type"] == "approval_request")
        self.assertTrue(request["auto_approved"])
        response = next(e for e in events if e["type"] == "approval_response")
        self.assertEqual(response["behavior"], "allow")
        self.assertTrue(response["auto"])
        self.assertEqual(adapter.effective, "allow")

        # Auto-approval never parks the session in awaiting_approval.
        self.assertNotIn(
            "awaiting_approval",
            [e.get("status") for e in events if e["type"] == "status"],
        )
        self.assertEqual(status, "idle")

    async def test_unmatched_category_waits_for_user(self) -> None:
        # Toggle off: the request must block on the user (awaiting_approval)
        # and never auto-resolve, so the turn can't complete on its own.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._drive(auto_command=False, category="command"), timeout=0.2
            )


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        pass


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()


class SendAnswerTests(unittest.IsolatedAsyncioTestCase):
    QUESTIONS = [
        {
            "question": "Which emoji do you want?",
            "header": "Emoji",
            "multiSelect": False,
            "options": [
                {"label": "Cat", "description": "The cat emoji"},
                {"label": "Rocket", "description": "The rocket emoji"},
            ],
        }
    ]

    def _arm(self, adapter, request_id: str, questions: list[dict]) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        adapter.pending_questions[request_id] = future
        adapter.pending_sessions[request_id] = "s1"
        adapter.pending_question_specs[request_id] = questions
        return future

    async def test_send_answer_resolves_future(self) -> None:
        adapter = ClaudeCodeAdapter(executable="claude")
        future = self._arm(adapter, "perm_1", self.QUESTIONS)

        result = await adapter.send_answer(
            {"id": "s1"}, "perm_1", {"Which emoji do you want?": "Rocket"}
        )

        self.assertEqual(result, {"Which emoji do you want?": "Rocket"})
        self.assertEqual(future.result(), {"Which emoji do you want?": "Rocket"})

    async def test_send_answer_multiselect_accepts_label_list(self) -> None:
        adapter = ClaudeCodeAdapter(executable="claude")
        questions = [
            {
                "question": "Pick languages",
                "header": "Langs",
                "multiSelect": True,
                "options": [
                    {"label": "Python", "description": ""},
                    {"label": "Go", "description": ""},
                ],
            }
        ]
        self._arm(adapter, "perm_1", questions)

        result = await adapter.send_answer(
            {"id": "s1"}, "perm_1", {"Pick languages": ["Python", "Go"]}
        )

        self.assertEqual(result, {"Pick languages": ["Python", "Go"]})

    async def test_send_answer_unknown_request_id_raises(self) -> None:
        adapter = ClaudeCodeAdapter(executable="claude")
        with self.assertRaises(KeyError):
            await adapter.send_answer({"id": "s1"}, "missing", {})

    async def test_send_answer_unknown_question_raises(self) -> None:
        adapter = ClaudeCodeAdapter(executable="claude")
        self._arm(adapter, "perm_1", self.QUESTIONS)
        with self.assertRaises(ValueError):
            await adapter.send_answer({"id": "s1"}, "perm_1", {"Nope?": "Rocket"})

    async def test_send_answer_unknown_option_raises(self) -> None:
        adapter = ClaudeCodeAdapter(executable="claude")
        self._arm(adapter, "perm_1", self.QUESTIONS)
        with self.assertRaises(ValueError):
            await adapter.send_answer(
                {"id": "s1"}, "perm_1", {"Which emoji do you want?": "Taco"}
            )

    async def test_opencode_send_answer_not_supported(self) -> None:
        adapter = OpenCodeAdapter(executable="opencode")
        with self.assertRaises(NotImplementedError):
            await adapter.send_answer({"id": "s1"}, "perm_1", {})

    async def test_write_question_response_allows_with_answers(self) -> None:
        adapter = ClaudeCodeAdapter(executable="claude")
        process = _FakeProcess()
        tool_input = {"questions": self.QUESTIONS}

        await adapter._write_question_response(
            process, "perm_1", tool_input, {"Which emoji do you want?": "Rocket"}
        )

        sent = json.loads(process.stdin.data.decode("utf-8"))
        response = sent["response"]["response"]
        self.assertEqual(response["behavior"], "allow")
        self.assertEqual(
            response["updatedInput"]["answers"],
            {"Which emoji do you want?": "Rocket"},
        )
        # The original questions ride along unchanged in updatedInput.
        self.assertEqual(response["updatedInput"]["questions"], self.QUESTIONS)

    async def test_unknown_request_id_raises(self) -> None:
        adapter = ClaudeCodeAdapter(executable="claude")
        with self.assertRaises(KeyError):
            await adapter.send_approval({"id": "s1"}, "missing", "allow")


if __name__ == "__main__":
    unittest.main()
