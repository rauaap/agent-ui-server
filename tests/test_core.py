from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from typing import Any

import asyncio

from fastapi import HTTPException

from agent_ui_server import db as db_module
from agent_ui_server import git, shell
from agent_ui_server.agent import (
    ApprovalDecision,
    ClaudeCodeAdapter,
    PiAdapter,
    _event_options,
    _normalize_questions,
    _option_behavior,
    _resolve_decision,
)
from agent_ui_server.db import Database


def make_session(
    database: Database,
    path: str,
    name: str = "demo",
    agent: str = "claude-code",
) -> dict[str, Any]:
    """Create a session running in `path`, registering its project first.

    Sessions belong to a project by foreign key now, so almost every test needs
    a project row even when the project is not what it is testing.
    """
    project = database.create_project(path, PurePosixPath(path).name)
    return database.create_session(
        name=name,
        project_id=project["id"],
        agent=agent,
    )


def init_repo(path: Path) -> None:
    """A git repository with one commit, which is what a worktree needs.

    `git worktree add -b` cannot branch from an unborn HEAD, so a freshly
    `git init`-ed directory is not enough.
    """
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "--quiet", "--initial-branch=main")
    run_git(path, "config", "user.email", "test@example.invalid")
    run_git(path, "config", "user.name", "Test")
    run_git(path, "commit", "--quiet", "--allow-empty", "-m", "root")


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class DatabaseTests(unittest.TestCase):
    def test_session_and_scrollback_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            session = make_session(database, "/projects/demo")

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
            session = make_session(database, "/projects/demo")

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
            session = make_session(database, "/projects/demo")

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
            shared = database.create_project("/projects/shared", "shared")
            database.create_project("/projects/empty", "empty")
            for name in ("one", "two"):
                database.create_session(
                    name=name,
                    project_id=shared["id"],
                    agent="claude-code",
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
            make_session(database, "/projects/demo-2", name="other")

            self.assertEqual(
                database.get_project("/projects/demo")["session_count"], 0
            )
            self.assertIsNone(database.get_project("/projects/missing"))
            database.close()

    def test_aggregates_follow_the_project_link_not_the_working_dir(self) -> None:
        # A worktree session runs outside the project directory entirely; the
        # foreign key is what keeps it on the project's card.
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            project = database.create_project("/projects/demo", "demo")
            worktree = database.create_worktree(
                project_id=project["id"],
                path="/projects/demo-feature",
                branch="feature",
            )
            session = database.create_session(
                name="feature",
                project_id=project["id"],
                agent="claude-code",
                worktree_id=worktree["id"],
            )

            self.assertEqual(session["working_dir"], "/projects/demo-feature")
            listed = database.get_project("/projects/demo")
            self.assertEqual(listed["session_count"], 1)
            self.assertEqual(listed["last_active_at"], session["last_active_at"])
            self.assertEqual(
                [s["id"] for s in database.list_sessions_for_project(project["id"])],
                [session["id"]],
            )
            database.close()

    def test_get_project_by_id_matches_the_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            project = database.create_project("/projects/demo", "demo")

            self.assertEqual(database.get_project_by_id(project["id"]), project)
            self.assertIsNone(database.get_project_by_id("missing"))
            database.close()

    def test_projects_sorted_by_recency_with_never_used_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            projects = {}
            for path in ("/p/older", "/p/newer", "/p/b-idle", "/p/a-idle"):
                projects[path] = database.create_project(
                    path, PurePosixPath(path).name
                )

            # Pin the clock so the two active projects differ by more than the
            # one-second resolution of the stored timestamps.
            stamps = iter(["2026-07-27T10:00:00Z", "2026-07-28T10:00:00Z"])
            original = db_module.utc_now
            db_module.utc_now = lambda: next(stamps)
            try:
                database.create_session(
                    name="a",
                    project_id=projects["/p/older"]["id"],
                    agent="claude-code",
                )
                database.create_session(
                    name="b",
                    project_id=projects["/p/newer"]["id"],
                    agent="claude-code",
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


class ArchiveTableTests(unittest.TestCase):
    """Archiving at the storage layer: the flag, the cascade, and the counts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "sessions.db")
        self.project = self.database.create_project("/p/demo", "demo")

    def tearDown(self) -> None:
        self.database.close()
        self._tmp.cleanup()

    def add_session(self, name: str) -> dict[str, Any]:
        return self.database.create_session(
            name=name,
            project_id=self.project["id"],
            agent="claude-code",
        )

    def archived_at(self, session_id: int) -> str | None:
        return self.database.require_session(session_id)["archived_at"]

    def test_new_rows_are_live(self) -> None:
        session = self.add_session("one")
        self.assertIsNone(session["archived_at"])
        self.assertIsNone(self.database.get_project("/p/demo")["archived_at"])

    def test_archiving_a_session_is_reversible(self) -> None:
        session = self.add_session("one")

        archived = self.database.set_session_archived(session["id"], True)
        self.assertIsNotNone(archived["archived_at"])

        live = self.database.set_session_archived(session["id"], False)
        self.assertIsNone(live["archived_at"])

    def test_rearchiving_keeps_the_original_timestamp(self) -> None:
        # Otherwise a no-op re-archive would jump the session to the top of an
        # archive sorted by when things went in.
        session = self.add_session("one")
        first = self.database.set_session_archived(session["id"], True)
        second = self.database.set_session_archived(session["id"], True)
        self.assertEqual(second["archived_at"], first["archived_at"])

    def test_detaching_preserves_the_worktree_path(self) -> None:
        worktree = self.database.create_worktree(
            self.project["id"], "/p/demo-fix", "fix"
        )
        session = self.database.create_session(
            name="worktree",
            project_id=self.project["id"],
            agent="claude-code",
            worktree_id=worktree["id"],
        )
        self.database.set_session_archived(session["id"], True)

        detached = self.database.detach_session_from_worktree(session["id"])

        self.assertIsNone(detached["worktree_id"])
        self.assertEqual(detached["working_dir"], "/p/demo-fix")
        self.assertEqual(
            self.database.list_sessions_for_worktree(worktree["id"]), []
        )
        self.assertTrue(self.database.delete_worktree(worktree["id"]))
        # The source row is gone, but the harness binding survives.
        self.assertEqual(
            self.database.require_session(session["id"])["working_dir"],
            "/p/demo-fix",
        )
        # Losing an HTTP response after the update is safe to retry.
        self.assertEqual(
            self.database.detach_session_from_worktree(session["id"])[
                "working_dir"
            ],
            "/p/demo-fix",
        )

    def test_only_archived_worktree_sessions_can_detach(self) -> None:
        worktree = self.database.create_worktree(
            self.project["id"], "/p/demo-fix", "fix"
        )
        attached = self.database.create_session(
            name="attached",
            project_id=self.project["id"],
            agent="claude-code",
            worktree_id=worktree["id"],
        )
        plain = self.add_session("plain")

        with self.assertRaises(ValueError):
            self.database.detach_session_from_worktree(attached["id"])
        self.database.set_session_archived(plain["id"], True)
        with self.assertRaises(ValueError):
            self.database.detach_session_from_worktree(plain["id"])

    def test_unknown_ids_raise(self) -> None:
        with self.assertRaises(KeyError):
            self.database.set_session_archived(9999, True)
        with self.assertRaises(KeyError):
            self.database.archive_project(9999)
        with self.assertRaises(KeyError):
            self.database.unarchive_project(9999)

    def test_archiving_a_project_cascades_to_its_sessions(self) -> None:
        one, two = self.add_session("one"), self.add_session("two")

        cascaded = self.database.archive_project(self.project["id"])

        self.assertEqual(cascaded, 2)
        project = self.database.get_project("/p/demo")
        self.assertIsNotNone(project["archived_at"])
        self.assertIsNotNone(self.archived_at(one["id"]))
        self.assertIsNotNone(self.archived_at(two["id"]))

    def test_unarchiving_a_project_restores_what_it_swept_up(self) -> None:
        one, two = self.add_session("one"), self.add_session("two")
        self.database.archive_project(self.project["id"])

        restored = self.database.unarchive_project(self.project["id"])

        self.assertEqual(restored, 2)
        self.assertIsNone(self.database.get_project("/p/demo")["archived_at"])
        self.assertIsNone(self.archived_at(one["id"]))
        self.assertIsNone(self.archived_at(two["id"]))

    def test_individually_archived_sessions_survive_the_round_trip(self) -> None:
        # The whole reason the cascade is tracked: a session the user filed away
        # on its own must not come back just because the project did.
        one, two = self.add_session("one"), self.add_session("two")
        filed = self.database.set_session_archived(one["id"], True)["archived_at"]

        self.assertEqual(self.database.archive_project(self.project["id"]), 1)
        self.assertEqual(self.archived_at(one["id"]), filed)

        self.assertEqual(self.database.unarchive_project(self.project["id"]), 1)
        self.assertEqual(self.archived_at(one["id"]), filed)
        self.assertIsNone(self.archived_at(two["id"]))

    def test_restore_can_be_declined_and_still_drops_the_mark(self) -> None:
        # The path a single session's unarchive takes: the project comes back so
        # that session has somewhere to show, but nothing else does — and a
        # later project archive must not claim to have archived the leftovers.
        one, two = self.add_session("one"), self.add_session("two")
        self.database.archive_project(self.project["id"])
        self.database.set_session_archived(one["id"], False)

        self.assertEqual(
            self.database.unarchive_project(
                self.project["id"], restore_sessions=False
            ),
            0,
        )
        self.assertIsNone(self.archived_at(one["id"]))
        self.assertIsNotNone(self.archived_at(two["id"]))

        # Re-archiving and unarchiving now leaves `two` where the user left it.
        self.assertEqual(self.database.archive_project(self.project["id"]), 1)
        self.assertEqual(self.database.unarchive_project(self.project["id"]), 1)
        self.assertIsNone(self.archived_at(one["id"]))
        self.assertIsNotNone(self.archived_at(two["id"]))

    def test_counts_split_live_from_archived(self) -> None:
        one = self.add_session("one")
        self.add_session("two")
        empty = self.database.create_project("/p/empty", "empty")

        self.database.set_session_archived(one["id"], True)

        project = self.database.get_project("/p/demo")
        self.assertEqual(project["session_count"], 1)
        self.assertEqual(project["archived_session_count"], 1)

        # A project with no sessions at all must report zero on both, not the
        # single all-NULL row the LEFT JOIN gives it.
        blank = self.database.get_project_by_id(empty["id"])
        self.assertEqual(blank["session_count"], 0)
        self.assertEqual(blank["archived_session_count"], 0)

    def test_last_active_at_ignores_archived_sessions(self) -> None:
        stamps = iter(["2026-07-27T10:00:00Z", "2026-07-28T10:00:00Z"])
        original = db_module.utc_now
        db_module.utc_now = lambda: next(stamps)
        try:
            self.add_session("old")
            recent = self.add_session("recent")
        finally:
            db_module.utc_now = original

        self.assertEqual(
            self.database.get_project("/p/demo")["last_active_at"],
            "2026-07-28T10:00:00Z",
        )

        self.database.set_session_archived(recent["id"], True)
        self.assertEqual(
            self.database.get_project("/p/demo")["last_active_at"],
            "2026-07-27T10:00:00Z",
        )

    def test_archived_sessions_still_list(self) -> None:
        # Storage hands back everything; deciding what to show is the client's.
        session = self.add_session("one")
        self.database.set_session_archived(session["id"], True)

        self.assertEqual([s["id"] for s in self.database.list_sessions()],
                         [session["id"]])
        self.assertEqual(
            [s["id"] for s in
             self.database.list_sessions_for_project(self.project["id"])],
            [session["id"]],
        )
        self.assertEqual(
            [p["path"] for p in self.database.list_projects()], ["/p/demo"]
        )

    def test_archive_state_survives_reopening(self) -> None:
        session = self.add_session("one")
        self.database.archive_project(self.project["id"])
        path = self.database.path
        self.database.close()

        self.database = Database(path)
        self.assertIsNotNone(self.database.get_project("/p/demo")["archived_at"])
        self.assertIsNotNone(self.archived_at(session["id"]))
        # And the cascade mark survived too, so the round trip still works.
        self.assertEqual(self.database.unarchive_project(self.project["id"]), 1)


class MigrationTests(unittest.TestCase):
    """Opening a pre-worktree database rebuilds it around a real foreign key."""

    LEGACY_SESSIONS = """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            working_dir TEXT NOT NULL,
            agent TEXT NOT NULL,
            agent_session_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            auto_approve_write INTEGER NOT NULL DEFAULT 0,
            auto_approve_command INTEGER NOT NULL DEFAULT 0
        )
    """
    LEGACY_PROJECTS = """
        CREATE TABLE projects (
            path TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """
    LEGACY_SCROLLBACK = """
        CREATE TABLE scrollback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """

    def build_legacy(
        self,
        path: Path,
        projects: list[tuple[str, str]],
        sessions: list[tuple[str, str, str]],
    ) -> None:
        """Write a database in the old shape: path-keyed projects, no FK."""
        conn = sqlite3.connect(path)
        with conn:
            for statement in (
                self.LEGACY_SESSIONS,
                self.LEGACY_PROJECTS,
                self.LEGACY_SCROLLBACK,
            ):
                conn.execute(statement)
            for project_path, name in projects:
                conn.execute(
                    "INSERT INTO projects (path, name, created_at) VALUES (?, ?, ?)",
                    (project_path, name, "2026-01-01T00:00:00Z"),
                )
            for session_id, name, working_dir in sessions:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        id, name, working_dir, agent, agent_session_id, status,
                        created_at, last_active_at,
                        auto_approve_write, auto_approve_command
                    )
                    VALUES (?, ?, ?, 'claude-code', 'resume-1', 'idle',
                            ?, ?, 1, 0)
                    """,
                    (
                        session_id,
                        name,
                        working_dir,
                        "2026-01-02T00:00:00Z",
                        "2026-01-03T00:00:00Z",
                    ),
                )
                conn.execute(
                    "INSERT INTO scrollback (session_id, ts, type, payload) "
                    "VALUES (?, ?, 'input', ?)",
                    (session_id, "2026-01-03T00:00:00Z", '{"text":"hello"}'),
                )
        conn.close()

    def sessions_by_name(self, database: Database) -> dict[str, dict]:
        """Migrated sessions keyed by name.

        The legacy ids in these fixtures are uuid-shaped strings, and the
        migration renumbers them, so a test cannot ask for `"s1"` afterwards.
        Name is the only field that survives a migration unchanged and is
        unique within each fixture.
        """
        return {session["name"]: session for session in database.list_sessions()}

    def test_sessions_keep_their_data_and_gain_a_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            self.build_legacy(
                path,
                projects=[("/p/demo", "demo")],
                sessions=[("s1", "one", "/p/demo"), ("s2", "two", "/p/demo")],
            )

            database = Database(path)

            project = database.get_project("/p/demo")
            self.assertTrue(project["id"])
            self.assertEqual(project["session_count"], 2)
            sessions = self.sessions_by_name(database)
            self.assertEqual(set(sessions), {"one", "two"})
            for session in sessions.values():
                self.assertEqual(session["project_id"], project["id"])
                self.assertEqual(session["working_dir"], "/p/demo")
                # Nothing here was created by us, so no worktree row is minted
                # and the cwd falls through to the project directory.
                self.assertIsNone(session["worktree_id"])
                # The rest of the row rides through untouched.
                self.assertEqual(session["agent_session_id"], "resume-1")
                self.assertIs(session["auto_approve_write"], True)
                self.assertEqual(session["created_at"], "2026-01-02T00:00:00Z")
            database.close()

    def test_orphan_session_is_adopted_into_a_new_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            # A session created at a path no project row matched: invisible in
            # the UI before, holding scrollback rows nothing could reach.
            self.build_legacy(
                path,
                projects=[],
                sessions=[("s1", "orphan", "/p/nowhere")],
            )

            database = Database(path)

            projects = database.list_projects()
            self.assertEqual([p["path"] for p in projects], ["/p/nowhere"])
            self.assertEqual(projects[0]["name"], "nowhere")
            self.assertEqual(projects[0]["session_count"], 1)
            self.assertEqual(
                self.sessions_by_name(database)["orphan"]["project_id"],
                projects[0]["id"],
            )
            database.close()

    def test_unnormalised_working_dir_joins_the_existing_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            # `create_session` never normalised working_dir while
            # `create_project` did, so both spellings mean one project.
            self.build_legacy(
                path,
                projects=[("/p/demo", "demo")],
                sessions=[("s1", "slashed", "/p/demo/")],
            )

            database = Database(path)

            self.assertEqual(len(database.list_projects()), 1)
            project = database.get_project("/p/demo")
            self.assertEqual(project["session_count"], 1)
            session = self.sessions_by_name(database)["slashed"]
            self.assertEqual(session["project_id"], project["id"])
            # The trailing slash does not survive: the cwd is no longer stored
            # on the session at all, it is read from the project it resolved
            # to. Same directory, one spelling.
            self.assertEqual(session["working_dir"], "/p/demo")
            database.close()

    def test_scrollback_survives_and_still_cascades(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            self.build_legacy(
                path,
                projects=[("/p/demo", "demo")],
                sessions=[("s1", "one", "/p/demo")],
            )

            database = Database(path)

            session_id = self.sessions_by_name(database)["one"]["id"]
            rows = database.recent_scrollback(session_id)
            self.assertEqual([row["payload"] for row in rows], [{"text": "hello"}])
            # The rename must not have left scrollback's foreign key pointing
            # at a table that no longer exists.
            database.delete_session(session_id)
            self.assertEqual(database.recent_scrollback(session_id), [])
            database.close()

    def test_reopening_a_migrated_database_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            self.build_legacy(
                path,
                projects=[("/p/demo", "demo")],
                sessions=[("s1", "one", "/p/demo")],
            )

            database = Database(path)
            first = database.get_project("/p/demo")
            database.close()

            database = Database(path)
            self.assertEqual(database.get_project("/p/demo"), first)
            self.assertEqual(len(database.list_projects()), 1)
            self.assertEqual(
                self.sessions_by_name(database)["one"]["project_id"], first["id"]
            )
            database.close()

    def test_database_predating_the_projects_table_migrates_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            conn = sqlite3.connect(path)
            with conn:
                # Old enough to have neither projects nor the renamed resume
                # column: it has to migrate through both steps.
                conn.execute(
                    """
                    CREATE TABLE sessions (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        working_dir TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        claude_session_id TEXT,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_active_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO sessions VALUES "
                    "('s1', 'one', '/p/ancient', 'claude-code', 'resume-1', "
                    "'idle', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                )
            conn.close()

            database = Database(path)

            session = self.sessions_by_name(database)["one"]
            self.assertEqual(session["agent_session_id"], "resume-1")
            self.assertIs(session["auto_approve_write"], False)
            self.assertIsNone(session["worktree_id"])
            self.assertEqual(
                [p["path"] for p in database.list_projects()], ["/p/ancient"]
            )
            self.assertEqual(
                session["project_id"], database.get_project("/p/ancient")["id"]
            )
            database.close()

    def test_uuid_ids_become_integers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            self.build_legacy(
                path,
                projects=[("/p/demo", "demo")],
                sessions=[("s1", "one", "/p/demo"), ("s2", "two", "/p/demo")],
            )

            database = Database(path)

            project = database.get_project("/p/demo")
            self.assertIsInstance(project["id"], int)
            sessions = self.sessions_by_name(database)
            for session in sessions.values():
                self.assertIsInstance(session["id"], int)
                self.assertEqual(session["project_id"], project["id"])
            # Renumbered in creation order, and each session keeps its own
            # scrollback rather than inheriting another's.
            self.assertLess(sessions["one"]["id"], sessions["two"]["id"])
            for name in ("one", "two"):
                rows = database.recent_scrollback(sessions[name]["id"])
                self.assertEqual(
                    [row["payload"] for row in rows], [{"text": "hello"}]
                )
            database.close()

    V2_SCHEMA = """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            working_dir TEXT NOT NULL,
            owns_worktree INTEGER NOT NULL DEFAULT 0,
            agent TEXT NOT NULL,
            agent_session_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            auto_approve_write INTEGER NOT NULL DEFAULT 0,
            auto_approve_command INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE scrollback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        INSERT INTO projects VALUES
            ('p-uuid', '/p/demo', 'demo', '2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES
            ('s-uuid', 'one', 'p-uuid', '/p/demo', 0, 'claude-code', 'resume-1',
             'idle', '2026-01-02T00:00:00Z', '2026-01-03T00:00:00Z', 1, 0);
        INSERT INTO scrollback (session_id, ts, type, payload) VALUES
            ('s-uuid', '2026-01-03T00:00:00Z', 'input', '{"text":"hello"}');
    """

    def test_scrollback_orphaned_before_the_migration_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            # Seeded in the *v2* shape — project_id already there, ids still
            # uuids — so `_migrate_v2` returns early and this exercises v3
            # alone. `PRAGMA foreign_keys` is per connection, so a writer that
            # never set it can leave scrollback pointing at a session that is
            # gone; renumbering must drop that row rather than fail outright.
            conn = sqlite3.connect(path)
            with conn:
                conn.executescript(self.V2_SCHEMA)
                conn.execute(
                    "INSERT INTO scrollback (session_id, ts, type, payload) "
                    "VALUES ('vanished', '2026-01-03T00:00:00Z', 'input', '{}')"
                )
            conn.close()

            database = Database(path)

            session = self.sessions_by_name(database)["one"]
            self.assertIsInstance(session["id"], int)
            self.assertEqual(
                [row["payload"] for row in database.recent_scrollback(session["id"])],
                [{"text": "hello"}],
            )
            database.close()

    V3_SCHEMA = """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            working_dir TEXT NOT NULL,
            owns_worktree INTEGER NOT NULL DEFAULT 0,
            agent TEXT NOT NULL,
            agent_session_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            auto_approve_write INTEGER NOT NULL DEFAULT 0,
            auto_approve_command INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE scrollback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        INSERT INTO projects VALUES
            (1, '/p/demo', 'demo', '2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES
            (10, 'plain', 1, '/p/demo', 0, 'claude-code', 'resume-1',
             'idle', '2026-01-02T00:00:00Z', '2026-01-03T00:00:00Z', 0, 0),
            (11, 'owned', 1, '/p/demo-fix', 1, 'claude-code', 'resume-2',
             'idle', '2026-01-02T00:00:00Z', '2026-01-04T00:00:00Z', 0, 0);
        INSERT INTO scrollback (session_id, ts, type, payload) VALUES
            (11, '2026-01-03T00:00:00Z', 'input', '{"text":"hello"}');
    """

    def test_owned_worktree_becomes_a_worktree_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            # Seeded in the v3 shape, so v2 and v3 both return early and this
            # exercises v4 alone.
            conn = sqlite3.connect(path)
            with conn:
                conn.executescript(self.V3_SCHEMA)
            conn.close()

            database = Database(path)

            worktrees = database.list_worktrees()
            self.assertEqual([w["path"] for w in worktrees], ["/p/demo-fix"])
            # Nothing recorded which branch it was cut on, so it stays unknown
            # rather than being guessed at.
            self.assertIsNone(worktrees[0]["branch"])
            self.assertEqual(worktrees[0]["session_count"], 1)

            sessions = self.sessions_by_name(database)
            self.assertIsNone(sessions["plain"]["worktree_id"])
            self.assertEqual(sessions["plain"]["working_dir"], "/p/demo")
            self.assertEqual(sessions["owned"]["worktree_id"], worktrees[0]["id"])
            self.assertEqual(sessions["owned"]["working_dir"], "/p/demo-fix")
            database.close()

    def test_session_ids_and_scrollback_survive_v4(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            conn = sqlite3.connect(path)
            with conn:
                conn.executescript(self.V3_SCHEMA)
            conn.close()

            database = Database(path)

            # Unlike v3, this migration does not renumber, so scrollback is left
            # alone entirely and the old ids still resolve.
            self.assertEqual(
                sorted(s["id"] for s in database.list_sessions()), [10, 11]
            )
            self.assertEqual(
                [row["payload"] for row in database.recent_scrollback(11)],
                [{"text": "hello"}],
            )
            database.close()

    def test_two_sessions_owning_one_path_converge_on_one_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            # Not reachable through the old API, but `worktrees.path` is UNIQUE,
            # so a database that somehow holds the pair must migrate rather than
            # fail on the second insert.
            conn = sqlite3.connect(path)
            with conn:
                conn.executescript(self.V3_SCHEMA)
                conn.execute(
                    "INSERT INTO sessions VALUES "
                    "(12, 'twin', 1, '/p/demo-fix', 1, 'claude-code', NULL, "
                    "'idle', '2026-01-02T00:00:00Z', '2026-01-05T00:00:00Z', 0, 0)"
                )
            conn.close()

            database = Database(path)

            worktrees = database.list_worktrees()
            self.assertEqual(len(worktrees), 1)
            self.assertEqual(worktrees[0]["session_count"], 2)
            sessions = self.sessions_by_name(database)
            self.assertEqual(
                sessions["owned"]["worktree_id"], sessions["twin"]["worktree_id"]
            )
            database.close()

    def test_deleting_a_project_takes_its_worktrees_whatever_the_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            project = database.create_project("/p/demo", "demo")
            worktree = database.create_worktree(project["id"], "/p/demo-fix", "fix")
            database.create_session(
                name="attached",
                project_id=project["id"],
                agent="claude-code",
                worktree_id=worktree["id"],
            )

            # `sessions.worktree_id` is RESTRICT, so a cascade that reached
            # `worktrees` before `sessions` would abort the whole delete.
            self.assertTrue(database.delete_project("/p/demo"))
            self.assertEqual(database.list_worktrees(), [])
            self.assertEqual(database.list_sessions(), [])
            database.close()

    def test_worktree_in_use_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            project = database.create_project("/p/demo", "demo")
            worktree = database.create_worktree(project["id"], "/p/demo-fix", "fix")
            database.create_session(
                name="attached",
                project_id=project["id"],
                agent="claude-code",
                worktree_id=worktree["id"],
            )

            # The endpoint answers 409 before reaching this; the constraint is
            # the backstop behind that check.
            with self.assertRaises(sqlite3.IntegrityError):
                database.delete_worktree(worktree["id"])
            database.close()

    def test_ids_are_not_reused_after_a_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            project = database.create_project("/p/demo", "demo")
            first = database.create_session(
                name="first",
                project_id=project["id"],
                agent="claude-code",
            )
            database.delete_session(first["id"])
            second = database.create_session(
                name="second",
                project_id=project["id"],
                agent="claude-code",
            )
            # AUTOINCREMENT, not a bare rowid: a client holding the deleted
            # session's URL must not land on its replacement.
            self.assertNotEqual(second["id"], first["id"])
    def test_legacy_database_gains_the_archive_columns(self) -> None:
        # Every rebuild recreates its tables in the pre-archive shape, so the
        # archive columns have to be added after them rather than before.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            self.build_legacy(
                path,
                projects=[("/p/demo", "demo")],
                sessions=[("s1", "one", "/p/demo")],
            )

            database = Database(path)

            migrated = self.sessions_by_name(database)["one"]
            self.assertIsNone(migrated["archived_at"])
            session_columns = {
                row["name"]
                for row in database._conn.execute("PRAGMA table_info(sessions)")
            }
            self.assertIn("detached_working_dir", session_columns)
            project = database.get_project("/p/demo")
            self.assertIsNone(project["archived_at"])
            self.assertEqual(project["session_count"], 1)
            self.assertEqual(project["archived_session_count"], 0)

            # And the migrated rows are fully usable, cascade mark included.
            self.assertEqual(database.archive_project(project["id"]), 1)
            self.assertEqual(database.unarchive_project(project["id"]), 1)
            self.assertIsNone(database.get_session(migrated["id"])["archived_at"])
            database.close()

    def test_pre_archive_database_is_upgraded_in_place(self) -> None:
        # Already through every rebuild, so nothing but the new columns is
        # missing: the ALTERs must run without triggering another rebuild.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.db"
            database = Database(path)
            project = database.create_project("/p/demo", "demo")
            session = database.create_session(
                name="one",
                project_id=project["id"],
                agent="claude-code",
            )
            database.close()

            conn = sqlite3.connect(path)
            with conn:
                for table, column in (
                    ("projects", "archived_at"),
                    ("sessions", "archived_at"),
                    ("sessions", "archived_with_project"),
                    ("sessions", "detached_working_dir"),
                ):
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            conn.close()

            database = Database(path)

            self.assertIsNone(database.get_session(session["id"])["archived_at"])
            self.assertEqual(
                database.get_project("/p/demo")["archived_session_count"], 0
            )
            self.assertEqual(database.archive_project(project["id"]), 1)
            database.close()


class GitModuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            init_repo(repo)
            plain = Path(tmpdir) / "plain"
            plain.mkdir()

            self.assertTrue(await git.is_git_repo(str(repo)))
            self.assertFalse(await git.is_git_repo(str(plain)))
            self.assertFalse(await git.is_git_repo(str(Path(tmpdir) / "gone")))

    async def test_check_branch_name_uses_gits_own_rules(self) -> None:
        self.assertTrue(await git.check_branch_name("fix-login"))
        self.assertTrue(await git.check_branch_name("feature/fix-login"))
        for bad in ("with space", "..", "-leading-dash", "trailing.lock", "a~b"):
            self.assertFalse(await git.check_branch_name(bad), bad)

    async def test_metacharacters_are_not_interpreted(self) -> None:
        # The whole reason this does not go through `shell.run_command`: a
        # branch name like this must be rejected as a name, not executed.
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            init_repo(repo)
            marker = Path(tmpdir) / "pwned"

            error = await git.add_worktree(
                str(repo),
                str(Path(tmpdir) / "wt"),
                f"x; touch {marker}",
            )

            self.assertIsNotNone(error)
            self.assertFalse(marker.exists())


class CreateProjectTests(unittest.IsolatedAsyncioTestCase):
    async def _create(self, database: Database, path: str) -> dict[str, Any]:
        from agent_ui_server import main

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
            make_session(database, target, name="one")

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

            from agent_ui_server import main

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

            from agent_ui_server import main

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

    async def test_is_git_repo_hint_marks_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            plain = await self._create(database, str(Path(tmpdir) / "plain"))
            self.assertFalse(plain["is_git_repo"])

            repo = Path(tmpdir) / "repo"
            init_repo(repo)
            adopted = await self._create(database, str(repo))
            self.assertTrue(adopted["is_git_repo"])

            # A project that is itself a worktree has `.git` as a file, and
            # still counts.
            worktree = Path(tmpdir) / "wt"
            run_git(repo, "worktree", "add", "-b", "side", str(worktree))
            nested = await self._create(database, str(worktree))
            self.assertTrue(nested["is_git_repo"])
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


class SessionEndpointTestCase(unittest.IsolatedAsyncioTestCase):
    """Drives main's session endpoints against a temporary database."""

    def setUp(self) -> None:
        from agent_ui_server import main

        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.database = Database(self.tmpdir / "sessions.db")
        self.main = main
        self._original_db = main.db
        main.db = self.database

    def tearDown(self) -> None:
        self.main.db = self._original_db
        self.database.close()
        self._tmp.cleanup()

    def make_project(self, name: str = "repo", repo: bool = True) -> dict[str, Any]:
        path = self.tmpdir / name
        if repo:
            init_repo(path)
        else:
            path.mkdir()
        return self.database.create_project(str(path), name)

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        return await self.main.create_session(
            self.main.CreateSessionRequest(**kwargs)
        )

    async def create_worktree(self, **kwargs: Any) -> dict[str, Any]:
        return await self.main.create_worktree(
            self.main.CreateWorktreeRequest(**kwargs)
        )

    async def worktree_for(
        self, project: dict[str, Any], name: str = "fix"
    ) -> tuple[dict[str, Any], Path]:
        """A worktree of `project` at `<tmpdir>/<name>`, on a branch of that name."""
        path = self.tmpdir / name
        worktree = await self.create_worktree(
            project_path=project["path"], path=str(path), branch=name
        )
        return worktree, path


class CreateSessionTests(SessionEndpointTestCase):
    async def test_without_worktree_runs_in_the_project_directory(self) -> None:
        project = self.make_project(repo=False)

        session = await self.create(name="plain", project_path=project["path"])

        self.assertEqual(session["working_dir"], project["path"])
        self.assertEqual(session["project_id"], project["id"])
        self.assertIsNone(session["worktree_id"])

    async def test_working_dir_is_accepted_as_a_deprecated_alias(self) -> None:
        project = self.make_project(repo=False)

        session = await self.create(name="legacy", working_dir=project["path"])

        self.assertEqual(session["working_dir"], project["path"])
        self.assertEqual(session["project_id"], project["id"])

    async def test_unregistered_project_is_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self.create(
                name="nope", project_path=str(self.tmpdir / "unregistered")
            )

        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.database.list_sessions(), [])
        self.assertFalse((self.tmpdir / "unregistered").exists())

    async def test_unknown_agent_is_400(self) -> None:
        project = self.make_project(repo=False)
        with self.assertRaises(HTTPException) as caught:
            await self.create(
                name="nope", project_path=project["path"], agent="gpt-whatever"
            )
        self.assertEqual(caught.exception.status_code, 400)

    async def test_session_attaches_to_an_existing_worktree(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project, "repo-fix-login")

        session = await self.create(
            name="fix login",
            project_path=project["path"],
            worktree_id=worktree["id"],
        )

        self.assertEqual(session["worktree_id"], worktree["id"])
        self.assertEqual(session["working_dir"], str(path))
        # And it still belongs to the project the worktree was cut from.
        listed = self.database.get_project(project["path"])
        self.assertEqual(listed["session_count"], 1)

    async def test_several_sessions_share_one_worktree(self) -> None:
        # The whole point of the rework: a worktree is not owned by whichever
        # session happened to create it.
        project = self.make_project()
        worktree, path = await self.worktree_for(project)

        first = await self.create(
            name="one", project_path=project["path"], worktree_id=worktree["id"]
        )
        second = await self.create(
            name="two", project_path=project["path"], worktree_id=worktree["id"]
        )

        self.assertEqual(first["working_dir"], str(path))
        self.assertEqual(second["working_dir"], str(path))
        self.assertEqual(
            self.database.get_worktree(worktree["id"])["session_count"], 2
        )

    async def test_unknown_worktree_is_404(self) -> None:
        project = self.make_project()

        with self.assertRaises(HTTPException) as caught:
            await self.create(
                name="fix", project_path=project["path"], worktree_id=999
            )

        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.database.list_sessions(), [])

    async def test_worktree_from_another_project_is_400(self) -> None:
        project = self.make_project("one")
        other = self.make_project("two")
        worktree, _ = await self.worktree_for(other, "two-fix")

        with self.assertRaises(HTTPException) as caught:
            await self.create(
                name="fix",
                project_path=project["path"],
                worktree_id=worktree["id"],
            )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.database.list_sessions(), [])

    async def test_attaching_to_a_deleted_directory_does_not_recreate_it(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        subprocess.run(["rm", "-rf", str(path)], check=True)

        session = await self.create(
            name="fix", project_path=project["path"], worktree_id=worktree["id"]
        )

        # A plain mkdir here would hand the agent a directory that looks like a
        # worktree and is not one. The row stays, flagged as gone, so the client
        # can offer to clean it up.
        self.assertEqual(session["working_dir"], str(path))
        self.assertFalse(path.exists())
        listed = await self.main.list_worktrees(project_path=project["path"])
        self.assertIs(listed[0]["exists"], False)


class NormalizeProjectPathTests(unittest.TestCase):
    def setUp(self) -> None:
        from agent_ui_server import main

        self.normalize = main.normalize_project_path

    def test_collapses_dots_and_duplicate_slashes(self) -> None:
        self.assertEqual(self.normalize("/projects//app/"), "/projects/app")
        self.assertEqual(self.normalize("  /projects/app/../app  "), "/projects/app")

    def test_leading_double_slash_is_collapsed(self) -> None:
        # `os.path.normpath` keeps exactly two leading slashes, so without the
        # explicit collapse this is a second spelling of the same directory and
        # the UNIQUE on projects.path / worktrees.path never sees the clash. A
        # client joining a path template onto a top-level project produces it.
        self.assertEqual(self.normalize("//app-fix"), "/app-fix")
        self.assertEqual(self.normalize("//projects/app"), "/projects/app")

    def test_relative_and_root_are_400(self) -> None:
        for path in ("app", "./app", "//", "/", "/projects/.."):
            with self.assertRaises(HTTPException) as caught:
                self.normalize(path)
            self.assertEqual(caught.exception.status_code, 400)


class CreateWorktreeTests(SessionEndpointTestCase):
    async def test_creates_a_directory_on_a_new_branch(self) -> None:
        project = self.make_project()
        path = self.tmpdir / "repo-fix-login"

        worktree = await self.create_worktree(
            project_path=project["path"],
            path=str(path),
            branch="fix-login",
        )

        self.assertEqual(worktree["path"], str(path))
        self.assertEqual(worktree["branch"], "fix-login")
        self.assertEqual(worktree["project_id"], project["id"])
        # Nothing is attached yet, which is an ordinary state now rather than
        # the leak it would have been before.
        self.assertEqual(worktree["session_count"], 0)
        self.assertIs(worktree["exists"], True)
        # A real worktree on a real new branch, not just a directory.
        self.assertTrue((path / ".git").is_file())
        self.assertEqual(
            run_git(path, "rev-parse", "--abbrev-ref", "HEAD"), "fix-login"
        )

    async def test_listing_is_scoped_to_a_project(self) -> None:
        one = self.make_project("one")
        two = self.make_project("two")
        await self.worktree_for(one, "one-a")
        await self.worktree_for(one, "one-b")
        await self.worktree_for(two, "two-a")

        scoped = await self.main.list_worktrees(project_path=one["path"])
        everything = await self.main.list_worktrees()

        self.assertEqual(
            sorted(w["path"] for w in scoped),
            [str(self.tmpdir / "one-a"), str(self.tmpdir / "one-b")],
        )
        self.assertEqual(len(everything), 3)

    async def test_empty_target_directory_is_taken_over(self) -> None:
        project = self.make_project()
        path = self.tmpdir / "prepared"
        path.mkdir()

        worktree = await self.create_worktree(
            project_path=project["path"], path=str(path), branch="fix"
        )

        self.assertEqual(worktree["path"], str(path))

    async def test_non_repo_project_is_400_and_creates_nothing(self) -> None:
        project = self.make_project(repo=False)
        path = self.tmpdir / "wt"

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"], path=str(path), branch="fix"
            )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "project is not a git repository")
        self.assertEqual(self.database.list_worktrees(), [])
        self.assertFalse(path.exists())

    async def test_unregistered_project_is_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=str(self.tmpdir / "unregistered"),
                path=str(self.tmpdir / "wt"),
                branch="fix",
            )

        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.database.list_worktrees(), [])

    async def test_non_empty_target_path_is_400(self) -> None:
        project = self.make_project()
        path = self.tmpdir / "occupied"
        path.mkdir()
        (path / "keep.txt").write_text("mine")

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"], path=str(path), branch="fix"
            )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.database.list_worktrees(), [])
        # Refusing must not disturb what is already there.
        self.assertEqual((path / "keep.txt").read_text(), "mine")

    async def test_invalid_branch_name_is_400(self) -> None:
        project = self.make_project()

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"],
                path=str(self.tmpdir / "wt"),
                branch="bad name",
            )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "invalid branch name")
        self.assertFalse((self.tmpdir / "wt").exists())

    async def test_existing_branch_is_400(self) -> None:
        project = self.make_project()
        run_git(Path(project["path"]), "branch", "taken")

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"],
                path=str(self.tmpdir / "wt"),
                branch="taken",
            )

        # v1 always cuts a new branch, so git's refusal is the answer; its own
        # message goes through so the client can show something specific.
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("taken", caught.exception.detail)
        self.assertEqual(self.database.list_worktrees(), [])

    async def test_worktree_at_the_project_path_is_400(self) -> None:
        project = self.make_project()

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"],
                path=project["path"] + "/",
                branch="fix",
            )

        self.assertEqual(caught.exception.status_code, 400)

    async def test_undiggable_path_is_400_and_leaves_no_branch(self) -> None:
        project = self.make_project()
        locked = self.tmpdir / "locked"
        locked.mkdir()
        locked.chmod(0o500)

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"],
                path=str(locked / "wt"),
                branch="fix",
            )

        # Restored before the assertions rather than in a cleanup, which would
        # run after tearDown has already removed the temporary directory.
        locked.chmod(0o700)
        self.assertEqual(caught.exception.status_code, 400)
        # `mkdir`'s errno, not git's `could not create leading directories of
        # '…/.git'`.
        self.assertIn("Could not create worktree directory", caught.exception.detail)
        self.assertIn("Permission denied", caught.exception.detail)
        # The point of creating the directory first: `git worktree add` writes
        # the branch ref before the leading directories, so letting it fail here
        # would leave `fix` behind and make the retry complain that the branch
        # already exists.
        branches = run_git(
            Path(project["path"]), "branch", "--list", "--format=%(refname:short)"
        )
        self.assertEqual(branches.split(), ["main"])
        self.assertEqual(self.database.list_worktrees(), [])

    async def test_path_that_is_already_a_worktree_is_409(self) -> None:
        project = self.make_project()
        _, path = await self.worktree_for(project)

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"], path=str(path), branch="other"
            )

        # The cause, not the symptom: without this check git's own refusal
        # blames a non-empty directory, which is not what went wrong.
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("already exists", caught.exception.detail)
        self.assertIn("fix", caught.exception.detail)
        self.assertEqual(len(self.database.list_worktrees()), 1)

    async def test_repeat_path_is_409_even_once_the_directory_is_gone(self) -> None:
        project = self.make_project()
        _, path = await self.worktree_for(project)
        subprocess.run(["rm", "-rf", str(path)], check=True)

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"], path=str(path), branch="other"
            )

        # `path` is UNIQUE, so reaching git here would do the work and then fail
        # the insert. git refuses this one too, but only with a message about
        # its own admin files.
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("already exists", caught.exception.detail)

    async def test_failed_insert_takes_the_worktree_back(self) -> None:
        project = self.make_project()
        path = self.tmpdir / "rolled-back"

        def explode(**kwargs: Any) -> dict[str, Any]:
            raise sqlite3.OperationalError("database is locked")

        self.database.create_worktree = explode
        with self.assertRaises(sqlite3.OperationalError):
            await self.create_worktree(
                project_path=project["path"], path=str(path), branch="fix"
            )

        # No row will ever claim the directory, so it must not survive.
        self.assertFalse(path.exists())
        self.assertEqual(
            run_git(Path(project["path"]), "worktree", "list", "--porcelain").count(
                "worktree "
            ),
            1,
        )


class DeleteWorktreeTests(SessionEndpointTestCase):
    async def test_clean_worktree_is_removed(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)

        result = await self.main.delete_worktree(worktree["id"])

        self.assertEqual(result["status"], "deleted")
        self.assertFalse(path.exists())
        self.assertIsNone(self.database.get_worktree(worktree["id"]))

    async def test_dirty_worktree_is_409_and_keeps_its_row(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        # An untracked file counts as dirty, so this is the *common* case for
        # any worktree an agent did work in — not an edge case.
        (path / "new_file.py").write_text("print('hi')")

        with self.assertRaises(HTTPException) as caught:
            await self.main.delete_worktree(worktree["id"])

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("untracked", caught.exception.detail)
        self.assertTrue(path.is_dir())
        self.assertEqual((path / "new_file.py").read_text(), "print('hi')")
        # The row is the worktree: dropping it while the directory survives is
        # exactly the orphan this rework exists to prevent.
        self.assertIsNotNone(self.database.get_worktree(worktree["id"]))

    async def test_attached_sessions_are_409_and_nothing_is_removed(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        await self.create(
            name="busy", project_path=project["path"], worktree_id=worktree["id"]
        )

        with self.assertRaises(HTTPException) as caught:
            await self.main.delete_worktree(worktree["id"])

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("busy", caught.exception.detail)
        self.assertTrue(path.is_dir())
        self.assertIsNotNone(self.database.get_worktree(worktree["id"]))

    async def test_removable_once_the_last_session_is_deleted(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        session = await self.create(
            name="done", project_path=project["path"], worktree_id=worktree["id"]
        )

        await self.main.delete_session(session["id"])
        result = await self.main.delete_worktree(worktree["id"])

        self.assertEqual(result["status"], "deleted")
        self.assertFalse(path.exists())

    async def test_directory_removed_by_hand_is_a_success(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        subprocess.run(["rm", "-rf", str(path)], check=True)

        result = await self.main.delete_worktree(worktree["id"])

        # git prunes its own admin files and exits 0, so this is how a row left
        # behind by a hand-deleted directory gets tidied up.
        self.assertEqual(result["status"], "deleted")
        self.assertIsNone(self.database.get_worktree(worktree["id"]))

    async def test_unknown_worktree_is_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self.main.delete_worktree(999)

        self.assertEqual(caught.exception.status_code, 404)


class TeardownWorktreeTests(SessionEndpointTestCase):
    async def test_deleting_a_session_leaves_its_worktree_alone(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        session = await self.create(
            name="fix", project_path=project["path"], worktree_id=worktree["id"]
        )

        result = await self.main.delete_session(session["id"])

        # The worktree outlives the session that used it, clean or not.
        self.assertEqual(result, {"status": "deleted"})
        self.assertTrue(path.is_dir())
        self.assertIsNotNone(self.database.get_worktree(worktree["id"]))
        self.assertIsNone(self.database.get_session(session["id"]))

    async def test_one_session_leaving_does_not_disturb_the_others(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        leaving = await self.create(
            name="one", project_path=project["path"], worktree_id=worktree["id"]
        )
        staying = await self.create(
            name="two", project_path=project["path"], worktree_id=worktree["id"]
        )

        await self.main.delete_session(leaving["id"])

        self.assertTrue(path.is_dir())
        self.assertEqual(
            self.database.get_session(staying["id"])["working_dir"], str(path)
        )
        self.assertEqual(
            self.database.get_worktree(worktree["id"])["session_count"], 1
        )

    async def test_delete_project_sweeps_worktrees_and_reports_counts(self) -> None:
        project = self.make_project()
        clean, clean_path = await self.worktree_for(project, "wt-clean")
        dirty, dirty_path = await self.worktree_for(project, "wt-dirty")
        sessions = [
            await self.create(
                name="clean",
                project_path=project["path"],
                worktree_id=clean["id"],
            ),
            await self.create(
                name="dirty",
                project_path=project["path"],
                worktree_id=dirty["id"],
            ),
            await self.create(name="plain", project_path=project["path"]),
        ]
        (dirty_path / "scratch.txt").write_text("x")

        result = await self.main.delete_project(
            self.main.DeleteProjectRequest(path=project["path"])
        )

        self.assertEqual(result["sessions_deleted"], 3)
        self.assertEqual(result["worktrees_removed"], 1)
        self.assertEqual(
            result["worktree_errors"],
            [
                {
                    "path": str(dirty_path),
                    "error": result["worktree_errors"][0]["error"],
                }
            ],
        )
        self.assertIn("untracked", result["worktree_errors"][0]["error"])
        self.assertFalse(clean_path.exists())
        self.assertTrue(dirty_path.is_dir())
        # The project directory itself is never touched.
        self.assertTrue(Path(project["path"]).is_dir())
        # Rows go regardless — including the dirty worktree's, by cascade.
        self.assertEqual(self.database.list_sessions(), [])
        self.assertEqual(self.database.list_worktrees(), [])
        for session in sessions:
            self.assertIsNone(self.database.get_session(session["id"]))


class ArchiveEndpointTests(SessionEndpointTestCase):
    """The HTTP contract: what archiving refuses, cascades, and announces."""

    def setUp(self) -> None:
        super().setUp()
        self.events: list[dict[str, Any]] = []
        self._original_enqueue = self.main.enqueue_for_subscribers

        def fake_enqueue(
            session_id: int, messages: list[dict[str, Any]]
        ) -> list[Any]:
            self.events.extend(
                {"session_id": session_id, **message} for message in messages
            )
            return []

        self.main.enqueue_for_subscribers = fake_enqueue

    def tearDown(self) -> None:
        self.main.enqueue_for_subscribers = self._original_enqueue
        self.main.bash_tasks.clear()
        super().tearDown()

    async def set_archived(self, session_id: int, archived: bool) -> dict[str, Any]:
        return await self.main.update_session(
            session_id, self.main.UpdateSessionRequest(archived=archived)
        )

    async def set_project_archived(
        self, path: str, archived: bool
    ) -> dict[str, Any]:
        return await self.main.update_project(
            self.main.UpdateProjectRequest(path=path, archived=archived)
        )

    async def test_archiving_a_session_flags_it_and_tells_its_clients(self) -> None:
        project = self.make_project(repo=False)
        session = await self.create(name="one", project_path=project["path"])

        archived = await self.set_archived(session["id"], True)

        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(
            self.events,
            [
                {
                    "session_id": session["id"],
                    "type": "archived",
                    "archived_at": archived["archived_at"],
                }
            ],
        )

    async def test_archived_worktree_session_can_detach_then_delete_worktree(
        self,
    ) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        session = await self.create(
            name="one", project_path=project["path"], worktree_id=worktree["id"]
        )
        await self.set_archived(session["id"], True)
        self.events.clear()

        detached = await self.main.detach_session_worktree(session["id"])

        self.assertIsNone(detached["worktree_id"])
        self.assertEqual(detached["working_dir"], str(path))
        self.assertEqual(
            self.events,
            [{
                "session_id": session["id"],
                "type": "worktree_detached",
                "worktree_id": None,
                "working_dir": str(path),
            }],
        )
        retried = await self.main.detach_session_worktree(session["id"])
        self.assertEqual(retried, detached)

        self.assertEqual(
            await self.main.delete_worktree(worktree["id"]),
            {"status": "deleted"},
        )
        self.assertFalse(path.exists())
        self.assertEqual(
            self.database.require_session(session["id"])["working_dir"],
            str(path),
        )

    async def test_live_or_project_directory_session_cannot_detach(self) -> None:
        project = self.make_project()
        worktree, _ = await self.worktree_for(project)
        attached = await self.create(
            name="attached",
            project_path=project["path"],
            worktree_id=worktree["id"],
        )
        plain = await self.create(name="plain", project_path=project["path"])

        with self.assertRaises(HTTPException) as caught:
            await self.main.detach_session_worktree(attached["id"])
        self.assertEqual(caught.exception.status_code, 409)

        await self.set_archived(plain["id"], True)
        with self.assertRaises(HTTPException) as caught:
            await self.main.detach_session_worktree(plain["id"])
        self.assertEqual(caught.exception.status_code, 409)

    async def test_live_detached_session_protects_its_worktree_directory(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        session = await self.create(
            name="one", project_path=project["path"], worktree_id=worktree["id"]
        )
        await self.set_archived(session["id"], True)
        await self.main.detach_session_worktree(session["id"])
        await self.set_archived(session["id"], False)

        with self.assertRaises(HTTPException) as caught:
            await self.main.delete_worktree(worktree["id"])

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("one", caught.exception.detail)
        self.assertTrue(path.is_dir())
        self.assertIsNotNone(self.database.get_worktree(worktree["id"]))

        # Archiving removes the active cwd dependency; detachment can now serve
        # its cleanup purpose without losing the path stored on the session.
        await self.set_archived(session["id"], True)
        self.assertEqual(
            await self.main.delete_worktree(worktree["id"]),
            {"status": "deleted"},
        )
        self.assertFalse(path.exists())

    async def test_detached_session_requires_its_directory_to_unarchive(self) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        session = await self.create(
            name="one", project_path=project["path"], worktree_id=worktree["id"]
        )
        await self.set_archived(session["id"], True)
        await self.main.detach_session_worktree(session["id"])
        await self.main.delete_worktree(worktree["id"])

        with self.assertRaises(HTTPException) as caught:
            await self.set_archived(session["id"], False)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn(str(path), caught.exception.detail)
        self.assertIsNotNone(
            self.database.require_session(session["id"])["archived_at"]
        )

        path.mkdir()
        restored = await self.set_archived(session["id"], False)
        self.assertIsNone(restored["archived_at"])
        self.assertEqual(restored["working_dir"], str(path))

    async def test_archiving_a_running_session_is_refused(self) -> None:
        project = self.make_project(repo=False)
        session = await self.create(name="one", project_path=project["path"])
        self.database.update_status(session["id"], "running")

        with self.assertRaises(HTTPException) as caught:
            await self.set_archived(session["id"], True)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNone(
            self.database.require_session(session["id"])["archived_at"]
        )

    async def test_archiving_a_session_running_a_command_is_refused(self) -> None:
        # Bash mode sits outside the turn state machine, so `status` alone would
        # call this session idle while a command is still running in it.
        project = self.make_project(repo=False)
        session = await self.create(name="one", project_path=project["path"])

        async def never() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(never())
        self.main.bash_tasks[session["id"]] = task
        try:
            with self.assertRaises(HTTPException) as caught:
                await self.set_archived(session["id"], True)
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unarchiving_a_session_brings_its_project_back_alone(self) -> None:
        project = self.make_project(repo=False)
        one = await self.create(name="one", project_path=project["path"])
        two = await self.create(name="two", project_path=project["path"])
        await self.set_project_archived(project["path"], True)

        await self.set_archived(one["id"], False)

        self.assertIsNone(self.database.get_project(project["path"])["archived_at"])
        self.assertIsNone(self.database.require_session(one["id"])["archived_at"])
        # Only the session the user asked for comes back.
        self.assertIsNotNone(
            self.database.require_session(two["id"])["archived_at"]
        )

    async def test_archiving_a_project_cascades_and_reports(self) -> None:
        project = self.make_project(repo=False)
        one = await self.create(name="one", project_path=project["path"])
        two = await self.create(name="two", project_path=project["path"])

        result = await self.set_project_archived(project["path"], True)

        self.assertEqual(result["sessions_affected"], 2)
        self.assertIsNotNone(result["archived_at"])
        self.assertEqual(result["session_count"], 0)
        self.assertEqual(result["archived_session_count"], 2)
        for session in (one, two):
            self.assertIsNotNone(
                self.database.require_session(session["id"])["archived_at"]
            )

    async def test_archiving_a_project_with_a_busy_session_writes_nothing(self) -> None:
        project = self.make_project(repo=False)
        one = await self.create(name="one", project_path=project["path"])
        busy = await self.create(name="busy", project_path=project["path"])
        self.database.update_status(busy["id"], "awaiting_approval")

        with self.assertRaises(HTTPException) as caught:
            await self.set_project_archived(project["path"], True)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("busy", caught.exception.detail)
        # Refused wholesale: the idle session must not be archived either.
        self.assertIsNone(self.database.get_project(project["path"])["archived_at"])
        self.assertIsNone(self.database.require_session(one["id"])["archived_at"])

    async def test_unarchiving_a_project_restores_the_round_trip(self) -> None:
        project = self.make_project(repo=False)
        one = await self.create(name="one", project_path=project["path"])
        two = await self.create(name="two", project_path=project["path"])
        filed = (await self.set_archived(two["id"], True))["archived_at"]
        await self.set_project_archived(project["path"], True)

        result = await self.set_project_archived(project["path"], False)

        self.assertEqual(result["sessions_affected"], 1)
        self.assertIsNone(result["archived_at"])
        self.assertIsNone(self.database.require_session(one["id"])["archived_at"])
        # The one archived by hand beforehand stays exactly where it was.
        self.assertEqual(
            self.database.require_session(two["id"])["archived_at"], filed
        )

    async def test_project_unarchive_refuses_missing_detached_directories(
        self,
    ) -> None:
        project = self.make_project()
        worktree, path = await self.worktree_for(project)
        session = await self.create(
            name="one", project_path=project["path"], worktree_id=worktree["id"]
        )
        await self.set_project_archived(project["path"], True)
        await self.main.detach_session_worktree(session["id"])
        await self.main.delete_worktree(worktree["id"])

        with self.assertRaises(HTTPException) as caught:
            await self.set_project_archived(project["path"], False)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn(str(path), caught.exception.detail)
        self.assertIsNotNone(
            self.database.get_project(project["path"])["archived_at"]
        )
        self.assertIsNotNone(
            self.database.require_session(session["id"])["archived_at"]
        )

    async def test_unknown_project_is_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await self.set_project_archived(str(self.tmpdir / "nope"), True)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_no_new_work_in_an_archived_project_or_session(self) -> None:
        project = self.make_project(repo=False)
        session = await self.create(name="one", project_path=project["path"])
        await self.set_project_archived(project["path"], True)

        for coroutine in (
            self.create(name="two", project_path=project["path"]),
            self.main.begin_turn(session["id"], "hello"),
            self.main.begin_bash(session["id"], "echo hi"),
        ):
            with self.assertRaises(HTTPException) as caught:
                await coroutine
            self.assertEqual(caught.exception.status_code, 409)

        self.assertEqual(len(self.database.list_sessions()), 1)
        self.assertEqual(self.database.recent_scrollback(session["id"]), [])

    async def test_no_new_worktree_in_an_archived_project(self) -> None:
        # Worktrees are created through their own endpoint, so the guard on
        # session creation does not cover them; without this one, an archived
        # project could still have a branch and a directory cut for it.
        project = self.make_project()
        await self.set_project_archived(project["path"], True)

        with self.assertRaises(HTTPException) as caught:
            await self.create_worktree(
                project_path=project["path"],
                path=str(self.tmpdir / "wt"),
                branch="fix",
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.database.list_worktrees(), [])
        self.assertFalse((self.tmpdir / "wt").exists())

    async def test_archived_sessions_can_still_be_read_renamed_and_deleted(self) -> None:
        # Archived is read-only, not sealed: the point is keeping old sessions
        # around, so their scrollback and their housekeeping must still work.
        project = self.make_project(repo=False)
        session = await self.create(name="one", project_path=project["path"])
        self.database.append_scrollback(session["id"], "input", {"text": "hello"})
        await self.set_archived(session["id"], True)

        renamed = await self.main.update_session(
            session["id"], self.main.UpdateSessionRequest(name="filed away")
        )
        self.assertEqual(renamed["name"], "filed away")
        self.assertIsNotNone(renamed["archived_at"])
        self.assertEqual(
            [row["payload"] for row in
             self.database.recent_scrollback(session["id"])],
            [{"text": "hello"}],
        )

        result = await self.main.delete_session(session["id"])
        self.assertEqual(result["status"], "deleted")
        self.assertIsNone(self.database.get_session(session["id"]))

    async def test_deleting_a_project_still_takes_archived_sessions(self) -> None:
        project = self.make_project(repo=False)
        await self.create(name="one", project_path=project["path"])
        await self.set_project_archived(project["path"], True)

        result = await self.main.delete_project(
            self.main.DeleteProjectRequest(path=project["path"])
        )

        self.assertEqual(result["sessions_deleted"], 1)
        self.assertEqual(self.database.list_sessions(), [])
        self.assertEqual(self.database.list_projects(), [])


class DeleteProjectTests(unittest.IsolatedAsyncioTestCase):
    async def _delete(self, database: Database, path: str) -> dict[str, Any]:
        from agent_ui_server import main

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
            doomed = database.create_project(str(target), "doomed")

            for name in ("one", "two"):
                database.create_session(
                    name=name,
                    project_id=doomed["id"],
                    agent="claude-code",
                )
            keeper = make_session(
                database, str(Path(tmpdir) / "other"), name="elsewhere"
            )

            result = await self._delete(database, str(target))

            self.assertEqual(result["sessions_deleted"], 2)
            self.assertEqual(result["worktrees_removed"], 0)
            self.assertEqual(result["worktree_errors"], [])
            self.assertEqual([p["path"] for p in database.list_projects()],
                             [str(Path(tmpdir) / "other")])
            # Sessions belonging to another project are untouched.
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
            session = make_session(database, target, name="one")
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


class ListAgentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_every_registered_adapter(self) -> None:
        from agent_ui_server import main

        agents = await main.list_agents()

        # The point of the endpoint: what it advertises is exactly what
        # POST /sessions accepts, so adding an adapter cannot leave the two
        # out of step.
        self.assertEqual(
            [agent["id"] for agent in agents], list(main.adapters.keys())
        )
        self.assertEqual([agent["id"] for agent in agents], ["claude-code", "pi"])
        self.assertEqual(
            [agent["name"] for agent in agents], ["Claude Code", "Pi"]
        )

    async def test_session_for_removed_adapter_is_inert_but_deletable(self) -> None:
        from agent_ui_server import main

        original_db = main.db
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            main.db = database
            try:
                session = make_session(database, tmpdir, agent="opencode")

                with self.assertRaises(HTTPException) as caught:
                    await main.begin_turn(session["id"], "hello")

                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(
                    database.require_session(session["id"])["status"], "idle"
                )
                self.assertEqual(database.recent_scrollback(session["id"], 10), [])

                await main.teardown_session(session)
                self.assertIsNone(database.get_session(session["id"]))
            finally:
                main.db = original_db
                database.close()

    async def test_marks_exactly_the_creation_default(self) -> None:
        from agent_ui_server import main

        agents = await main.list_agents()
        defaults = [agent["id"] for agent in agents if agent["default"]]

        self.assertEqual(
            defaults, [main.CreateSessionRequest.model_fields["agent"].default]
        )

    async def test_every_adapter_carries_a_label(self) -> None:
        from agent_ui_server import main

        agents = await main.list_agents()

        for agent in agents:
            with self.subTest(agent=agent["id"]):
                self.assertTrue(
                    type(main.adapters[agent["id"]]).LABEL,
                    "adapter is missing a LABEL, so the picker would show its id",
                )
                self.assertEqual(agent["name"], type(main.adapters[agent["id"]]).LABEL)


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
                            "id": "call-1",
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
            {
                "type": "tool_use",
                "call_id": "call-1",
                "action": {"kind": "command", "command": "pwd", "shell": "bash"},
            },
        )

    def test_permission_request_normalizes_to_approval_event(self) -> None:
        event = self.adapter._approval_request_event(
            {
                "type": "sdk_control_request",
                "request": {
                    "subtype": "permission",
                    "request_id": "perm_1",
                    "tool_name": "Bash",
                    "tool_use_id": "call-approval-1",
                    "input": {"command": "rm -rf /tmp/demo"},
                },
            }
        )

        self.assertEqual(
            event,
            {
                "type": "approval_request",
                "request_id": "perm_1",
                "call_id": "call-approval-1",
                "action": {
                    "kind": "command",
                    "command": "rm -rf /tmp/demo",
                    "shell": "bash",
                },
                "options": [
                    {"id": "allow", "name": "Allow", "kind": "allow_once"},
                    {"id": "deny", "name": "Deny", "kind": "reject_once"},
                ],
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
                    "tool_use_id": "call-approval-2",
                    "input": {"file_path": "/projects/demo/a.txt", "content": "hi"},
                },
            }
        )

        self.assertEqual(
            event,
            {
                "type": "approval_request",
                "request_id": "1",
                "call_id": "call-approval-2",
                "action": {
                    "kind": "write",
                    "path": "/projects/demo/a.txt",
                    "content": "hi",
                },
                "options": [
                    {"id": "allow", "name": "Allow", "kind": "allow_once"},
                    {"id": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        )

    def test_approval_request_normalizes_read_only_tool(self) -> None:
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
        self.assertEqual(event["action"], {"kind": "read", "path": "/projects/demo/a.txt"})
        self.assertNotIn("category", event)

    def test_result_session_id_is_extracted_from_nested_payload(self) -> None:
        session_id = self.adapter._extract_session_id(
            {"type": "result", "result": {"session_id": "abc123"}}
        )

        self.assertEqual(session_id, "abc123")

    def test_normalize_questions_extracts_fields_with_defaults(self) -> None:
        questions = _normalize_questions(
            [
                {
                    "question": "Which emoji do you want?",
                    "header": "Emoji",
                    "options": [
                        {"label": "Cat", "description": "The cat emoji"},
                        {"label": "Rocket"},
                    ],
                }
            ]
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
        self.assertEqual(_normalize_questions(None), [])

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
                            "id": "call-2",
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
                {
                    "type": "tool_use",
                    "call_id": "call-2",
                    "action": {"kind": "command", "command": "pwd", "shell": "bash"},
                },
            ],
        )


class PiAdapterParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = PiAdapter(executable="pi")

    def test_bundled_extension_ships_with_the_package(self) -> None:
        path = Path(PiAdapter.DEFAULT_EXTENSION)
        self.assertTrue(path.exists(), f"{path} is missing")
        source = path.read_text()
        # The two features the adapter's whole protocol depends on.
        self.assertIn('pi.on("tool_call"', source)
        self.assertIn("registerTool", source)

    def test_bundled_web_extension_ships_with_the_package(self) -> None:
        path = Path(PiAdapter.DEFAULT_WEB_EXTENSION)
        self.assertTrue(path.exists(), f"{path} is missing")
        source = path.read_text()
        # The tool names pi_extension.ts allowlists and tool_actions.py
        # translates. An upstream rename must fail here, not in production.
        self.assertIn('name: WEB_SEARCH_TOOL', source)
        self.assertIn('const WEB_SEARCH_TOOL = "web_search"', source)
        self.assertIn('const URL_CONTEXT_TOOL = "url_context"', source)

    def test_web_tools_bypass_the_approval_gate(self) -> None:
        # The gate is an allowlist, so an upstream rename would silently start
        # prompting for every search rather than failing loudly.
        source = Path(PiAdapter.DEFAULT_EXTENSION).read_text()
        self.assertIn('"web_search"', source)
        self.assertIn('"url_context"', source)

    def test_web_extension_is_on_by_default_and_can_be_disabled(self) -> None:
        import unittest.mock as mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PI_WEB_SEARCH", None)
            self.assertEqual(
                PiAdapter(executable="pi").web_extension_path,
                PiAdapter.DEFAULT_WEB_EXTENSION,
            )
        self.assertEqual(
            PiAdapter(executable="pi", web_extension_path="/tmp/other.ts")
            .web_extension_path,
            "/tmp/other.ts",
        )
        # Empty means "no web access", and must not fall back to the default.
        self.assertEqual(
            PiAdapter(executable="pi", web_extension_path="").web_extension_path, ""
        )
        with mock.patch.dict(os.environ, {"PI_WEB_SEARCH": ""}):
            self.assertEqual(PiAdapter(executable="pi").web_extension_path, "")
        with mock.patch.dict(os.environ, {"PI_WEB_SEARCH": "/tmp/env.ts"}):
            self.assertEqual(
                PiAdapter(executable="pi").web_extension_path, "/tmp/env.ts"
            )

    def test_envelope_accepts_our_marker(self) -> None:
        title = json.dumps(
            {"agent-ui": 1, "kind": "approval", "toolCallId": "c1", "toolName": "bash"}
        )
        self.assertEqual(
            self.adapter._envelope(title),
            {"agent-ui": 1, "kind": "approval", "toolCallId": "c1", "toolName": "bash"},
        )

    def test_envelope_rejects_foreign_dialogs(self) -> None:
        # A human-readable title from some other extension, a wrong protocol
        # version, and malformed JSON must all read as "not ours".
        self.assertIsNone(self.adapter._envelope("Allow dangerous command?"))
        self.assertIsNone(self.adapter._envelope(json.dumps({"agent-ui": 99})))
        self.assertIsNone(self.adapter._envelope('{"agent-ui":'))
        self.assertIsNone(self.adapter._envelope(None))

    def test_bundled_node_dir_prefers_a_node_beside_pi(self) -> None:
        # pi's launcher is `#!/usr/bin/env node`, so PATH decides which Node it
        # runs under; the adapter promotes the one shipped alongside it.
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp)
            (bindir / "pi").write_text("#!/bin/sh\n")
            (bindir / "pi").chmod(0o755)
            adapter = PiAdapter(executable=str(bindir / "pi"))
            self.assertIsNone(adapter._bundled_node_dir())

            (bindir / "node").write_text("#!/bin/sh\n")
            (bindir / "node").chmod(0o755)
            self.assertEqual(adapter._bundled_node_dir(), str(bindir))
            self.assertTrue(
                adapter._build_env()["PATH"].startswith(str(bindir) + os.pathsep)
            )


class PiAdapterTurnTests(unittest.IsolatedAsyncioTestCase):
    """Drive start_turn against a stub that speaks pi's RPC protocol."""

    STUB = '''#!/usr/bin/env python3
import json, sys

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

SCRIPT = json.loads(sys.argv[1])
if SCRIPT.get("ready", True):
    emit({"type": "extension_ui_request", "id": "d0", "method": "notify",
          "notifyType": "info",
          "message": json.dumps({"agent-ui": 1, "kind": "ready"})})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("type") == "get_state":
        emit({"id": msg.get("id"), "type": "response", "command": "get_state",
              "success": True, "data": {"sessionId": "sess-abc"}})
    elif msg.get("type") == "prompt":
        for step in SCRIPT["steps"]:
            emit(step)
    elif msg.get("type") == "extension_ui_response":
        for step in SCRIPT.get("on_response", {}).get(msg["id"], []):
            emit(step)
        SCRIPT.setdefault("answers", []).append(msg)
        if len(SCRIPT["answers"]) >= SCRIPT.get("expect_answers", 1):
            for step in SCRIPT.get("after_answers", []):
                emit(step)
'''

    def _adapter(self, script: dict) -> PiAdapter:
        stub = Path(self.tmp.name) / "pi"
        stub.write_text(self.STUB)
        stub.chmod(0o755)
        adapter = PiAdapter(executable=str(stub))
        adapter._script = json.dumps(script)  # type: ignore[attr-defined]

        original = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            # Replace pi's real flags with the stub's single argv slot.
            return await original(args[0], adapter._script, **kwargs)

        self.spawn = spawn
        return adapter

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = {"id": 7, "working_dir": self.tmp.name, "agent_session_id": None}

    async def _collect(self, adapter, answerer=None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        import unittest.mock

        with unittest.mock.patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec", self.spawn
        ):
            async for event in adapter.start_turn(self.session, "go"):
                events.append(event)
                if answerer:
                    task = answerer(adapter, event)
                    if task:
                        asyncio.create_task(task)
        return events

    async def test_launch_loads_both_extensions_with_discovery_off(self):
        """Both `-e` paths survive `--no-extensions`; discovery stays off.

        The gate and the web tools are loaded the same way for the same
        reason, so a change that drops either flag has to fail here.
        """
        adapter = self._adapter({"steps": [{"type": "agent_settled"}]})
        adapter.web_extension_path = "/tmp/web.ts"
        captured: list[tuple[str, ...]] = []

        original = asyncio.create_subprocess_exec

        async def capturing_spawn(*args, **kwargs):
            captured.append(args)
            return await original(args[0], adapter._script, **kwargs)

        self.spawn = capturing_spawn
        await self._collect(adapter)

        command = list(captured[0])
        self.assertIn("--no-extensions", command)
        self.assertEqual(
            [command[i + 1] for i, arg in enumerate(command) if arg == "-e"],
            [adapter.extension_path, "/tmp/web.ts"],
        )

    async def test_launch_omits_the_web_extension_when_disabled(self):
        adapter = self._adapter({"steps": [{"type": "agent_settled"}]})
        adapter.web_extension_path = ""
        captured: list[tuple[str, ...]] = []

        original = asyncio.create_subprocess_exec

        async def capturing_spawn(*args, **kwargs):
            captured.append(args)
            return await original(args[0], adapter._script, **kwargs)

        self.spawn = capturing_spawn
        await self._collect(adapter)

        command = list(captured[0])
        self.assertEqual(command.count("-e"), 1)
        self.assertIn("--no-extensions", command)

    async def test_approval_carries_input_captured_from_tool_execution_start(self):
        adapter = self._adapter(
            {
                "steps": [
                    {"type": "tool_execution_start", "toolCallId": "c1",
                     "toolName": "bash", "args": {"command": "echo hi"}},
                    {"type": "extension_ui_request", "id": "d1", "method": "select",
                     "options": ["Allow", "Deny"],
                     "title": json.dumps({"agent-ui": 1, "kind": "approval",
                                          "toolCallId": "c1", "toolName": "bash"})},
                ],
                "after_answers": [
                    {"type": "message_update",
                     "assistantMessageEvent": {"type": "text_end", "content": "done!"}},
                    {"type": "agent_settled"},
                ],
            }
        )

        def answerer(ad, event):
            if event["type"] == "approval_request":
                return ad.send_approval(self.session, event["request_id"], "allow")
            return None

        events = await self._collect(adapter, answerer)
        by_type = {e["type"]: e for e in events}

        self.assertEqual(
            by_type["tool_use"],
            {
                "type": "tool_use",
                "call_id": "c1",
                "action": {
                    "kind": "command", "command": "echo hi", "shell": "bash"
                },
            },
        )
        approval = by_type["approval_request"]
        # The dialog carries only an id; the arguments come from the
        # tool_execution_start that precedes it.
        self.assertEqual(approval["call_id"], "c1")
        self.assertEqual(approval["action"], by_type["tool_use"]["action"])
        self.assertEqual(by_type["output"]["text"], "done!")
        self.assertEqual(by_type["done"]["session_id"], "sess-abc")

    async def test_question_batch_becomes_one_event(self):
        def dialog(index, header, label):
            return {
                "type": "extension_ui_request", "id": f"q{index}", "method": "select",
                "options": [label, "Other"],
                "title": json.dumps({
                    "agent-ui": 1, "kind": "question", "toolCallId": "c9",
                    "index": index, "count": 2,
                    "question": {"question": f"Pick {header}?", "header": header,
                                 "multiSelect": False,
                                 "options": [{"label": label, "description": "d"},
                                             {"label": "Other", "description": "d"}]},
                }),
            }

        adapter = self._adapter(
            {
                "steps": [
                    # The question tool must not also render a tool bubble.
                    {"type": "tool_execution_start", "toolCallId": "c9",
                     "toolName": "AskUserQuestion", "args": {"questions": []}},
                    dialog(0, "Database", "Postgres"),
                    dialog(1, "Client", "Fetch"),
                ],
                "expect_answers": 2,
                "after_answers": [{"type": "agent_settled"}],
            }
        )

        def answerer(ad, event):
            if event["type"] == "question":
                answers = {q["question"]: q["options"][0]["label"]
                           for q in event["questions"]}
                return ad.send_answer(self.session, event["request_id"], answers)
            return None

        events = await self._collect(adapter, answerer)
        questions = [e for e in events if e["type"] == "question"]

        self.assertEqual(len(questions), 1, "batch should surface as one event")
        self.assertEqual(len(questions[0]["questions"]), 2)
        self.assertEqual(
            [q["header"] for q in questions[0]["questions"]], ["Database", "Client"]
        )
        self.assertFalse(
            [e for e in events if e["type"] == "tool_use"],
            "AskUserQuestion should not emit a tool_use bubble",
        )

    async def test_turn_refuses_to_run_without_the_extension_handshake(self):
        adapter = self._adapter({"ready": False, "steps": [{"type": "agent_settled"}]})
        adapter.HANDSHAKE_TIMEOUT = 1.0

        events = await self._collect(adapter)

        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("without the agent-ui extension", events[0]["message"])


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



class _FakeWebSocket:
    def __init__(self, *, send_open: bool = True, accept_open: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self.send_gate = asyncio.Event()
        self.accept_gate = asyncio.Event()
        if send_open:
            self.send_gate.set()
        if accept_open:
            self.accept_gate.set()
        self.closed = False
        self.close_code: int | None = None
        self._receive: asyncio.Future[dict[str, Any]] | None = None

    async def accept(self) -> None:
        await self.accept_gate.wait()

    async def send_json(self, message: dict[str, Any]) -> None:
        await self.send_gate.wait()
        self.sent.append(message)

    async def receive_json(self) -> dict[str, Any]:
        if self.closed:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        self._receive = asyncio.get_running_loop().create_future()
        return await self._receive

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code
        if self._receive is not None and not self._receive.done():
            from fastapi import WebSocketDisconnect

            self._receive.set_exception(WebSocketDisconnect())


class WebSocketOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from agent_ui_server import main

        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = main.db
        main.db = Database(Path(self.tmp.name) / "sessions.db")
        self.session = make_session(main.db, self.tmp.name)
        main.subscribers.clear()
        main.stream_locks.clear()

    async def asyncTearDown(self) -> None:
        pending = [
            subscriber
            for group in self.main.subscribers.values()
            for subscriber in group
        ]
        for subscriber in pending:
            subscriber.retired = True
        await self.main.teardown_subscribers(pending)
        self.main.subscribers.clear()
        self.main.stream_locks.clear()
        self.main.db.close()
        self.main.db = self.original_db
        self.tmp.cleanup()

    async def wait_for(self, predicate, timeout: float = 1.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0)

    async def test_snapshot_frames_precede_a_live_status(self) -> None:
        session_id = self.session["id"]
        self.main.db.append_scrollback(session_id, "output", {"text": "old"})
        self.main.db.update_status(session_id, "running")
        websocket = _FakeWebSocket(send_open=False)

        endpoint = asyncio.create_task(
            self.main.session_websocket(websocket, session_id)
        )
        await self.wait_for(lambda: bool(self.main.subscribers.get(session_id)))

        await self.main.commit_stream(
            session_id,
            lambda: self.main.db.update_status(session_id, "idle"),
            lambda _result: [{"type": "status", "status": "idle"}],
        )
        websocket.send_gate.set()
        await self.wait_for(lambda: len(websocket.sent) == 4)

        self.assertEqual(
            websocket.sent,
            [
                {"type": "output", "text": "old"},
                {"type": "status", "status": "running"},
                {"type": "archived", "archived_at": None},
                {"type": "status", "status": "idle"},
            ],
        )
        endpoint.cancel()
        await asyncio.gather(endpoint, return_exceptions=True)

    async def test_approval_transition_stays_after_initial_snapshot(self) -> None:
        session_id = self.session["id"]
        websocket = _FakeWebSocket(send_open=False)
        endpoint = asyncio.create_task(
            self.main.session_websocket(websocket, session_id)
        )
        await self.wait_for(lambda: bool(self.main.subscribers.get(session_id)))

        adapter = _AutoApproveAdapter("command")
        original_adapter = self.main.adapters["claude-code"]
        self.main.adapters["claude-code"] = adapter
        turn = asyncio.create_task(self.main.run_turn(session_id, "go"))
        try:
            await self.wait_for(
                lambda: self.main.db.require_session(session_id)["status"]
                == "awaiting_approval"
            )
            websocket.send_gate.set()
            await self.wait_for(lambda: len(websocket.sent) == 4)

            self.assertEqual(
                [event["type"] for event in websocket.sent],
                ["status", "archived", "status", "approval_request"],
            )
            self.assertEqual(
                websocket.sent[2]["status"], "awaiting_approval"
            )
            self.assertEqual(
                self.main.db.recent_scrollback(session_id)[-1]["type"],
                "approval_request",
            )
        finally:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            endpoint.cancel()
            await asyncio.gather(endpoint, return_exceptions=True)
            self.main.adapters["claude-code"] = original_adapter

    async def test_slow_writer_does_not_delay_another_subscriber(self) -> None:
        session_id = self.session["id"]
        slow_socket = _FakeWebSocket(send_open=False)
        fast_socket = _FakeWebSocket()
        slow = self.main.Subscriber(slow_socket, asyncio.Queue(maxsize=10))
        fast = self.main.Subscriber(fast_socket, asyncio.Queue(maxsize=10))
        slow.writer = asyncio.create_task(self.main.write_subscriber(slow))
        fast.writer = asyncio.create_task(self.main.write_subscriber(fast))
        self.main.subscribers[session_id].update({slow, fast})

        await self.main.broadcast(session_id, {"type": "output", "text": "now"})
        await self.wait_for(lambda: len(fast_socket.sent) == 1)

        self.assertEqual(fast_socket.sent[0]["text"], "now")
        self.assertEqual(slow_socket.sent, [])

    async def test_concurrent_publications_follow_commit_order(self) -> None:
        session_id = self.session["id"]
        subscriber = self.main.Subscriber(
            _FakeWebSocket(), asyncio.Queue(maxsize=10)
        )
        self.main.subscribers[session_id].add(subscriber)
        committed: list[int] = []

        async def publish(value: int) -> None:
            await self.main.commit_stream(
                session_id,
                lambda: committed.append(value),
                lambda _result: [{"type": "sequence", "value": value}],
            )

        await asyncio.gather(*(publish(value) for value in range(5)))
        delivered = [
            subscriber.outbound.get_nowait()["value"] for _ in range(5)
        ]

        self.assertEqual(delivered, committed)

    async def test_full_queue_retires_only_that_subscriber(self) -> None:
        session_id = self.session["id"]
        full_socket = _FakeWebSocket()
        healthy_socket = _FakeWebSocket()
        full = self.main.Subscriber(full_socket, asyncio.Queue(maxsize=1))
        healthy = self.main.Subscriber(healthy_socket, asyncio.Queue(maxsize=2))
        full.outbound.put_nowait({"type": "output", "text": "stuck"})
        self.main.subscribers[session_id].update({full, healthy})

        await self.main.broadcast(session_id, {"type": "status", "status": "idle"})

        self.assertTrue(full.retired)
        self.assertTrue(full_socket.closed)
        self.assertFalse(healthy.retired)
        self.assertEqual(
            healthy.outbound.get_nowait(), {"type": "status", "status": "idle"}
        )

    async def test_send_timeout_closes_connection_and_receive_task(self) -> None:
        session_id = self.session["id"]
        websocket = _FakeWebSocket(send_open=False)
        original_timeout = self.main.WEBSOCKET_SEND_TIMEOUT_SECONDS
        self.main.WEBSOCKET_SEND_TIMEOUT_SECONDS = 0.01
        try:
            endpoint = asyncio.create_task(
                self.main.session_websocket(websocket, session_id)
            )
            await asyncio.wait_for(endpoint, timeout=1)
        finally:
            self.main.WEBSOCKET_SEND_TIMEOUT_SECONDS = original_timeout

        self.assertTrue(websocket.closed)
        self.assertEqual(websocket.close_code, 1011)
        self.assertNotIn(session_id, self.main.subscribers)

    async def test_receive_disconnect_stops_writer_and_removes_subscriber(self) -> None:
        session_id = self.session["id"]
        websocket = _FakeWebSocket()
        endpoint = asyncio.create_task(
            self.main.session_websocket(websocket, session_id)
        )
        await self.wait_for(lambda: len(websocket.sent) == 2)
        subscriber = next(iter(self.main.subscribers[session_id]))
        assert subscriber.writer is not None

        await websocket.close()
        await asyncio.wait_for(endpoint, timeout=1)

        self.assertTrue(subscriber.writer.done())
        self.assertNotIn(session_id, self.main.subscribers)

    async def test_deletion_while_accepting_fails_revalidation(self) -> None:
        session_id = self.session["id"]
        websocket = _FakeWebSocket(accept_open=False)
        endpoint = asyncio.create_task(
            self.main.session_websocket(websocket, session_id)
        )
        await asyncio.sleep(0)

        await self.main.teardown_session(self.session)
        websocket.accept_gate.set()
        await asyncio.wait_for(endpoint, timeout=1)

        self.assertTrue(websocket.closed)
        self.assertEqual(websocket.close_code, 1008)
        self.assertIsNone(self.main.db.get_session(session_id))
        self.assertNotIn(session_id, self.main.subscribers)


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
        action = (
            {"kind": "command", "command": "ls"}
            if self.category == "command"
            else {"kind": "read", "path": "README.md"}
        )
        yield {
            "type": "approval_request",
            "request_id": "perm_1",
            "call_id": "call-1",
            "action": action,
            "options": [],
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


class _LateApprovalOnStopAdapter:
    """Expose a second approval only after stop clears the visible one."""

    LABEL = "Late approval"

    def __init__(self) -> None:
        self.first_released = asyncio.Event()
        self.late_emitted = asyncio.Event()
        self.cleaned_up = asyncio.Event()

    async def start_turn(self, session, prompt):
        try:
            yield {
                "type": "approval_request",
                "request_id": "perm_1",
                "call_id": "call-1",
                "action": {"kind": "read", "path": "/outside/one"},
                "options": [],
            }
            await self.first_released.wait()
            yield {
                "type": "approval_request",
                "request_id": "perm_2",
                "call_id": "call-2",
                "action": {"kind": "read", "path": "/outside/two"},
                "options": [],
            }
            self.late_emitted.set()
            await asyncio.Event().wait()
        finally:
            self.cleaned_up.set()

    async def stop(self, session) -> None:
        # Model the provider's buffered stdout becoming readable after the
        # currently visible approval is cleared. Waiting here makes the race
        # deterministic: stop_session returns from the adapter with run_turn
        # parked on the newly emitted approval.
        self.first_released.set()
        await self.late_emitted.wait()


class StopTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_turn_blocked_on_late_buffered_approval(self) -> None:
        from agent_ui_server import main

        original_db = main.db
        original_enqueue = main.enqueue_for_subscribers
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Database(Path(tmpdir) / "sessions.db")
            session = make_session(database, tmpdir, agent="late-approval")
            adapter = _LateApprovalOnStopAdapter()
            main.db = database
            main.adapters["late-approval"] = adapter
            main.enqueue_for_subscribers = lambda _session_id, _messages: []
            try:
                await main.begin_turn(session["id"], "go")
                turn_task = main.running_tasks[session["id"]]
                while (
                    database.require_session(session["id"])["status"]
                    != "awaiting_approval"
                ):
                    await asyncio.sleep(0)

                await asyncio.wait_for(main.stop_session(session["id"]), timeout=1)

                self.assertTrue(turn_task.done())
                self.assertTrue(adapter.cleaned_up.is_set())
                self.assertNotIn(session["id"], main.running_tasks)
                self.assertEqual(
                    database.require_session(session["id"])["status"], "idle"
                )
            finally:
                task = main.running_tasks.pop(session["id"], None)
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                main.adapters.pop("late-approval", None)
                main.enqueue_for_subscribers = original_enqueue
                main.db = original_db
                database.close()


class RunTurnAutoApproveTests(unittest.IsolatedAsyncioTestCase):
    async def _drive(self, *, auto_command: bool, category: str):
        from agent_ui_server import main

        with tempfile.TemporaryDirectory() as tmpdir:
            main.db = Database(Path(tmpdir) / "sessions.db")
            session = make_session(main.db, tmpdir, agent="fake")
            if auto_command:
                main.db.set_auto_approve(session["id"], command=True)

            adapter = _AutoApproveAdapter(category)
            main.adapters["fake"] = adapter

            events: list[dict] = []

            def fake_enqueue(session_id, messages):
                events.extend(messages)
                return []

            original = main.enqueue_for_subscribers
            main.enqueue_for_subscribers = fake_enqueue
            try:
                await main.run_turn(session["id"], "go")
            finally:
                main.enqueue_for_subscribers = original
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


class ShellCommandTests(unittest.IsolatedAsyncioTestCase):
    """Bash mode's one-shot runner. These spawn a real `/bin/bash`."""

    async def test_captures_stdout_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await shell.run_command("echo hello", cwd=tmpdir)

        self.assertEqual(result["stdout"], "hello\n")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["truncated"])

    async def test_captures_stderr_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await shell.run_command("echo boom >&2; exit 3", cwd=tmpdir)

        self.assertEqual(result["stderr"], "boom\n")
        self.assertEqual(result["exit_code"], 3)

    async def test_runs_in_the_given_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await shell.run_command("pwd", cwd=tmpdir)

        # macOS/temp paths can be symlinked, so compare resolved paths.
        self.assertEqual(
            Path(result["stdout"].strip()).resolve(), Path(tmpdir).resolve()
        )

    async def test_no_state_persists_between_invocations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            await shell.run_command("cd /tmp; export MARKER=set", cwd=tmpdir)
            result = await shell.run_command("pwd; echo \"[${MARKER-}]\"", cwd=tmpdir)

        self.assertIn("[]", result["stdout"])
        self.assertEqual(
            Path(result["stdout"].splitlines()[0]).resolve(), Path(tmpdir).resolve()
        )

    async def test_timeout_kills_and_keeps_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await shell.run_command(
                "echo started; sleep 30", cwd=tmpdir, timeout=0.5
            )

        self.assertTrue(result["timed_out"])
        self.assertEqual(result["stdout"], "started\n")
        self.assertNotEqual(result["exit_code"], 0)
        # Killed at the timeout, not after the full sleep.
        self.assertLess(result["duration_ms"], 5000)

    async def test_timeout_kills_backgrounded_children(self) -> None:
        # `sleep 30 &` outlives the shell unless the whole process group is
        # signalled — and it holds stdout open, so the drain would hang too.
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await shell.run_command(
                "sleep 30 & echo spawned; wait", cwd=tmpdir, timeout=0.5
            )

        self.assertTrue(result["timed_out"])
        self.assertLess(result["duration_ms"], 5000)

    async def test_stdin_is_closed_rather_than_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await shell.run_command("cat", cwd=tmpdir, timeout=5)

        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 0)

    async def test_output_over_the_limit_keeps_head_and_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await shell.run_command(
                "seq 1 20000", cwd=tmpdir, limit=200
            )

        self.assertTrue(result["truncated"])
        self.assertLess(len(result["stdout"]), 1000)
        self.assertTrue(result["stdout"].startswith("1\n2\n3\n"))
        self.assertTrue(result["stdout"].endswith("20000\n"))
        self.assertIn("bytes omitted", result["stdout"])
        self.assertEqual(result["exit_code"], 0)

    async def test_runaway_output_does_not_wedge_on_a_full_pipe(self) -> None:
        # `yes` never stops on its own: the drain has to keep reading past the
        # cap, and the timeout has to kill it.
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await shell.run_command("yes", cwd=tmpdir, timeout=0.5, limit=200)

        self.assertTrue(result["timed_out"])
        self.assertTrue(result["truncated"])
        self.assertLess(result["duration_ms"], 5000)

    async def test_missing_working_directory_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = str(Path(tmpdir) / "gone")
        result = await shell.run_command("echo hi", cwd=missing)

        self.assertIsNone(result["exit_code"])
        self.assertIn("Could not run the command", result["stderr"])
        self.assertEqual(result["stdout"], "")

    async def test_cancellation_kills_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task = asyncio.create_task(
                shell.run_command("sleep 30", cwd=tmpdir, timeout=30)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class BashModeTests(unittest.IsolatedAsyncioTestCase):
    """main.begin_bash / run_bash: the parts that must not touch turn state."""

    async def _session(self, tmpdir: str):
        from agent_ui_server import main

        main.db = Database(Path(tmpdir) / "sessions.db")
        session = make_session(main.db, tmpdir)
        return main, session

    async def _capture(self, main):
        events: list[dict] = []

        def fake_enqueue(session_id, messages):
            events.extend(messages)
            return []

        main.enqueue_for_subscribers = fake_enqueue
        return events

    async def test_echo_then_output_without_status_changes(self) -> None:
        from agent_ui_server import main

        original = main.enqueue_for_subscribers
        with tempfile.TemporaryDirectory() as tmpdir:
            main_mod, session = await self._session(tmpdir)
            events = await self._capture(main_mod)
            try:
                await main_mod.begin_bash(session["id"], "echo hi")
                await main_mod.bash_tasks[session["id"]]

                self.assertEqual(
                    [e["type"] for e in events], ["bash_input", "bash_output"]
                )
                self.assertEqual(events[1]["command"], "echo hi")
                self.assertEqual(events[1]["stdout"], "hi\n")
                self.assertEqual(events[1]["exit_code"], 0)

                # The turn state machine is untouched: no status event, and the
                # session is still idle with no turn recorded.
                self.assertNotIn("status", [e["type"] for e in events])
                refreshed = main_mod.db.require_session(session["id"])
                self.assertEqual(refreshed["status"], "idle")

                # Both events land in scrollback, so they replay on reconnect.
                rows = main_mod.db.recent_scrollback(session["id"])
                self.assertEqual(
                    [row["type"] for row in rows], ["bash_input", "bash_output"]
                )
            finally:
                main_mod.enqueue_for_subscribers = original
                main_mod.db.close()

    async def test_runs_while_the_agent_turn_is_running(self) -> None:
        from agent_ui_server import main

        original = main.enqueue_for_subscribers
        with tempfile.TemporaryDirectory() as tmpdir:
            main_mod, session = await self._session(tmpdir)
            events = await self._capture(main_mod)
            main_mod.db.update_status(session["id"], "running")
            try:
                await main_mod.begin_bash(session["id"], "echo hi")
                await main_mod.bash_tasks[session["id"]]

                self.assertEqual(events[-1]["type"], "bash_output")
                # Still running: bash neither waited for the turn nor ended it.
                self.assertEqual(
                    main_mod.db.require_session(session["id"])["status"], "running"
                )
            finally:
                main_mod.enqueue_for_subscribers = original
                main_mod.db.close()

    async def test_second_command_while_one_is_in_flight_is_rejected(self) -> None:
        from agent_ui_server import main

        original = main.enqueue_for_subscribers
        with tempfile.TemporaryDirectory() as tmpdir:
            main_mod, session = await self._session(tmpdir)
            await self._capture(main_mod)
            try:
                await main_mod.begin_bash(session["id"], "sleep 5")
                with self.assertRaises(HTTPException) as caught:
                    await main_mod.begin_bash(session["id"], "echo hi")
                self.assertEqual(caught.exception.status_code, 409)

                await main_mod.cancel_bash(session["id"])
            finally:
                main_mod.enqueue_for_subscribers = original
                main_mod.db.close()

    async def test_cancel_bash_reports_the_stop(self) -> None:
        from agent_ui_server import main

        original = main.enqueue_for_subscribers
        with tempfile.TemporaryDirectory() as tmpdir:
            main_mod, session = await self._session(tmpdir)
            events = await self._capture(main_mod)
            try:
                await main_mod.begin_bash(session["id"], "sleep 30")
                await asyncio.sleep(0.1)
                await main_mod.cancel_bash(session["id"])

                self.assertEqual(events[-1]["type"], "error")
                self.assertIn("Command stopped", events[-1]["message"])
                self.assertNotIn(session["id"], main_mod.bash_tasks)
            finally:
                main_mod.enqueue_for_subscribers = original
                main_mod.db.close()

    async def test_unknown_session_is_404(self) -> None:
        from agent_ui_server import main

        with tempfile.TemporaryDirectory() as tmpdir:
            main_mod, _ = await self._session(tmpdir)
            try:
                with self.assertRaises(HTTPException) as caught:
                    await main_mod.begin_bash("missing", "echo hi")
                self.assertEqual(caught.exception.status_code, 404)
            finally:
                main_mod.db.close()


if __name__ == "__main__":
    unittest.main()
