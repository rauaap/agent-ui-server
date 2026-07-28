from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from typing import Any

import asyncio

from fastapi import HTTPException

import db as db_module
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


class ProjectTableTests(unittest.TestCase):
    def test_create_project_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")

            created = database.create_project("/projects/demo", "demo")
            self.assertEqual(created["path"], "/projects/demo")
            self.assertEqual(created["name"], "demo")
            self.assertEqual(created["session_count"], 0)
            self.assertIsNone(created["last_active_at"])

            # Creating it again neither duplicates nor errors.
            again = database.create_project("/projects/demo", "renamed")
            self.assertEqual(again["name"], "demo")
            self.assertEqual(len(database.list_projects()), 1)

            database.close()

    def test_projects_carry_session_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            database.create_project("/projects/shared", "shared")
            database.create_project("/projects/empty", "empty")
            for name in ("one", "two"):
                database.create_session(
                    name=name, working_dir="/projects/shared", agent="claude-code"
                )

            projects = {p["path"]: p for p in database.list_projects()}
            self.assertEqual(projects["/projects/shared"]["session_count"], 2)
            self.assertEqual(projects["/projects/empty"]["session_count"], 0)
            self.assertIsNone(projects["/projects/empty"]["last_active_at"])

            newest = max(
                session["last_active_at"]
                for session in database.list_sessions()
                if session["working_dir"] == "/projects/shared"
            )
            self.assertEqual(projects["/projects/shared"]["last_active_at"], newest)

            database.close()

    def test_sessions_elsewhere_do_not_leak_into_a_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            database.create_project("/projects/demo", "demo")
            database.create_session(
                name="other", working_dir="/projects/demo-2", agent="claude-code"
            )

            self.assertEqual(
                database.get_project("/projects/demo")["session_count"], 0
            )
            self.assertIsNone(database.get_project("/projects/missing"))
            database.close()

    def test_projects_sorted_by_recency_with_never_used_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            for path in ("/p/older", "/p/newer", "/p/b-idle", "/p/a-idle"):
                database.create_project(path, PurePosixPath(path).name)

            # Pin the clock so the two active projects differ by more than the
            # one-second resolution of the stored timestamps.
            stamps = iter(["2026-07-27T10:00:00Z", "2026-07-28T10:00:00Z"])
            original = db_module.utc_now
            db_module.utc_now = lambda: next(stamps)
            try:
                database.create_session(
                    name="a", working_dir="/p/older", agent="claude-code"
                )
                database.create_session(
                    name="b", working_dir="/p/newer", agent="claude-code"
                )
            finally:
                db_module.utc_now = original

            self.assertEqual(
                [p["path"] for p in database.list_projects()],
                ["/p/newer", "/p/older", "/p/a-idle", "/p/b-idle"],
            )
            database.close()

    def test_projects_survive_reopening_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            database = Database(path)
            database.create_project("/projects/demo", "demo")
            database.close()

            database = Database(path)
            projects = database.list_projects()
            self.assertEqual([p["path"] for p in projects], ["/projects/demo"])
            self.assertEqual(projects[0]["name"], "demo")
            database.close()


class CreateProjectTests(unittest.IsolatedAsyncioTestCase):
    async def _create(self, database: Database, path: str) -> dict[str, Any]:
        import main

        original = main.db
        main.db = database
        try:
            return await main.create_project(main.CreateProjectRequest(path=path))
        finally:
            main.db = original

    async def test_creates_directory_and_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            target = Path(tmpdir) / "group" / "fresh"

            project = await self._create(database, str(target))

            self.assertTrue(target.is_dir())
            self.assertEqual(project["path"], str(target))
            self.assertEqual(project["name"], "fresh")
            self.assertEqual(project["session_count"], 0)
            self.assertIsNone(project["last_active_at"])
            self.assertEqual([p["path"] for p in database.list_projects()],
                             [str(target)])
            database.close()

    async def test_recreating_reports_real_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            target = str(Path(tmpdir) / "used")
            await self._create(database, target)
            database.create_session(
                name="one", working_dir=target, agent="claude-code"
            )

            again = await self._create(database, target)
            self.assertEqual(again["session_count"], 1)
            self.assertEqual(len(database.list_projects()), 1)
            database.close()

    async def test_trailing_slash_and_traversal_are_normalised(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            messy = str(Path(tmpdir)) + "/one/../two//three/"

            project = await self._create(database, messy)

            # Same directory must not be able to enter the table twice under
            # two spellings.
            self.assertEqual(project["path"], str(Path(tmpdir) / "two" / "three"))
            self.assertEqual(project["name"], "three")
            database.close()

    async def test_relative_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            with self.assertRaises(HTTPException) as caught:
                await self._create(database, "relative/dir")
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(database.list_projects(), [])
            database.close()

    async def test_filesystem_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            with self.assertRaises(HTTPException) as caught:
                await self._create(database, "/")
            self.assertEqual(caught.exception.status_code, 400)
            database.close()

    async def test_explicit_name_is_kept_and_may_differ_from_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            target = str(Path(tmpdir) / "some-dir")

            import main

            original = main.db
            main.db = database
            try:
                project = await main.create_project(
                    main.CreateProjectRequest(path=target, name="My Project")
                )
            finally:
                main.db = original

            # The user broke the name/path link in the dialog, so the label
            # must not be re-derived from the directory.
            self.assertEqual(project["name"], "My Project")
            self.assertEqual(project["path"], target)
            database.close()

    async def test_adopting_an_existing_directory_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            target = Path(tmpdir) / "already-here"
            target.mkdir()
            (target / "code.py").write_text("x")

            project = await self._create(database, str(target))

            self.assertEqual(project["path"], str(target))
            self.assertTrue(project["exists"])
            # Adopting must not disturb what is already in the directory.
            self.assertEqual((target / "code.py").read_text(), "x")
            database.close()

    async def test_exists_flag_tracks_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            target = Path(tmpdir) / "vanishing"
            await self._create(database, str(target))

            import main

            original = main.db
            main.db = database
            try:
                self.assertTrue((await main.list_projects())[0]["exists"])
                target.rmdir()
                # The row survives; only the flag changes.
                listed = await main.list_projects()
                self.assertEqual(len(listed), 1)
                self.assertFalse(listed[0]["exists"])
            finally:
                main.db = original
            database.close()

    async def test_undeletable_path_is_a_400_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            # A file where the directory should go: mkdir fails, and the row
            # must not be written either.
            blocker = Path(tmpdir) / "blocker"
            blocker.write_text("x")

            with self.assertRaises(HTTPException) as caught:
                await self._create(database, str(blocker))
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(database.list_projects(), [])
            database.close()


class DeleteProjectTests(unittest.IsolatedAsyncioTestCase):
    async def _delete(self, database: Database, path: str) -> dict[str, Any]:
        import main

        original = main.db
        main.db = database
        try:
            return await main.delete_project(main.DeleteProjectRequest(path=path))
        finally:
            main.db = original

    async def test_deletes_row_and_sessions_but_never_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            target = Path(tmpdir) / "doomed"
            target.mkdir()
            (target / "work.txt").write_text("the agent's output")
            database.create_project(str(target), "doomed")

            for name in ("one", "two"):
                database.create_session(
                    name=name, working_dir=str(target), agent="claude-code"
                )
            keeper = database.create_session(
                name="elsewhere",
                working_dir=str(Path(tmpdir) / "other"),
                agent="claude-code",
            )

            result = await self._delete(database, str(target))

            self.assertEqual(result["sessions_deleted"], 2)
            self.assertEqual(database.list_projects(), [])
            # Sessions in other directories are untouched.
            self.assertEqual(
                [s["id"] for s in database.list_sessions()], [keeper["id"]]
            )
            # The whole point: files on disk survive.
            self.assertTrue(target.is_dir())
            self.assertEqual((target / "work.txt").read_text(), "the agent's output")
            database.close()

    async def test_deleting_scrollback_goes_with_the_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            target = str(Path(tmpdir) / "doomed")
            database.create_project(target, "doomed")
            session = database.create_session(
                name="one", working_dir=target, agent="claude-code"
            )
            database.append_scrollback(session["id"], "input", {"text": "hello"})

            await self._delete(database, target)

            self.assertEqual(database.recent_scrollback(session["id"]), [])
            database.close()

    async def test_missing_directory_can_still_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            # The case the client's pop-up exists for: directory removed
            # outside the app, project row left behind.
            gone = str(Path(tmpdir) / "gone")
            database.create_project(gone, "gone")

            result = await self._delete(database, gone)

            self.assertEqual(result["sessions_deleted"], 0)
            self.assertEqual(database.list_projects(), [])
            database.close()

    async def test_unknown_project_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            with self.assertRaises(HTTPException) as caught:
                await self._delete(database, "/projects/never-existed")
            self.assertEqual(caught.exception.status_code, 404)
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
