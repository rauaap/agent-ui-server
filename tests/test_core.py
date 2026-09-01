from __future__ import annotations

import json
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
    OpenCodeAdapter,
    _event_options,
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
        from agent_ui_server import main

        with tempfile.TemporaryDirectory() as tmpdir:
            main.db = Database(Path(tmpdir) / "sessions.db")
            session = make_session(main.db, tmpdir, agent="fake")
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

        async def fake_broadcast(session_id, message):
            events.append(message)

        main.broadcast = fake_broadcast
        return events

    async def test_echo_then_output_without_status_changes(self) -> None:
        from agent_ui_server import main

        original = main.broadcast
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
                main_mod.broadcast = original
                main_mod.db.close()

    async def test_runs_while_the_agent_turn_is_running(self) -> None:
        from agent_ui_server import main

        original = main.broadcast
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
                main_mod.broadcast = original
                main_mod.db.close()

    async def test_second_command_while_one_is_in_flight_is_rejected(self) -> None:
        from agent_ui_server import main

        original = main.broadcast
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
                main_mod.broadcast = original
                main_mod.db.close()

    async def test_cancel_bash_reports_the_stop(self) -> None:
        from agent_ui_server import main

        original = main.broadcast
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
                main_mod.broadcast = original
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
