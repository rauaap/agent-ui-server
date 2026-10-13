from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_ui_server.db import Database


class DatabaseSchemaTests(unittest.TestCase):
    def test_current_database_reopen_preserves_schema_data_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.db"
            database = Database(path)
            try:
                project = database.create_project("/project", "project")
                worktree = database.create_worktree(project["id"], "/worktree", "branch")
                session = database.create_session(
                    "session", project["id"], "pi", model="provider/model",
                    reasoning_level="high", worktree_id=worktree["id"],
                )
                database.append_scrollback(session["id"], "output", {"text": "héllo"})
                database.enqueue_input(session["id"], "queued", {"type": "user"})
                database.set_sandbox(session["id"], False)
                database.set_auto_approve(session["id"], inter_agent_communication=True)
                database.delete_worktree(worktree["id"])
                database.create_shared_asset_root("assets", "/assets", project["id"])
                database.set_sandbox_network_allowlist([{"ip": "1.2.3.4", "port": 80}])
                database.set_sandbox_paths([{"path": "/extra", "writable": True}])
                before = list(database._conn.iterdump())
                expected = database.require_session(session["id"])
            finally:
                database.close()

            database = Database(path)
            try:
                self.assertEqual(list(database._conn.iterdump()), before)
                self.assertEqual(database.require_session(session["id"]), expected)
                self.assertEqual(database._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                database.close()

    def test_schema_has_no_archive_columns(self):
        database = Database(":memory:")
        try:
            for table in ("projects", "sessions"):
                columns = {row["name"] for row in database._conn.execute(f"PRAGMA table_info({table})")}
                self.assertNotIn("archived_at", columns)
                self.assertNotIn("archived_with_project", columns)
            self.assertIn("detached_working_dir", columns)
        finally:
            database.close()

    def test_manual_archive_column_removal_preserves_records_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.db"
            database = Database(path)
            project = database.create_project("/project", "project")
            worktree = database.create_worktree(project["id"], "/former-worktree", "branch")
            session = database.create_session("retained", project["id"], "pi", worktree_id=worktree["id"])
            database.set_agent_session_id(session["id"], "conversation")
            database.append_scrollback(session["id"], "output", {"text": "history"})
            database.enqueue_input(session["id"], "pending", {"type": "user"})
            database.delete_worktree(worktree["id"])
            expected = database.require_session(session["id"])
            transcript = database.recent_scrollback(session["id"])
            queued = database.pending_inputs(session["id"])
            database.close()

            with sqlite3.connect(path) as connection:
                # Recreate the previous schema's archive state, then execute
                # the owner's manual cleanup. Opening Database never migrates it.
                connection.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT")
                connection.execute("ALTER TABLE sessions ADD COLUMN archived_at TEXT")
                connection.execute("ALTER TABLE sessions ADD COLUMN archived_with_project INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE projects SET archived_at = '2026-01-01T00:00:00Z'")
                connection.execute("UPDATE sessions SET archived_at = '2026-01-01T00:00:00Z', archived_with_project = 1")
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("ALTER TABLE projects DROP COLUMN archived_at")
                connection.execute("ALTER TABLE sessions DROP COLUMN archived_at")
                connection.execute("ALTER TABLE sessions DROP COLUMN archived_with_project")

            database = Database(path)
            try:
                self.assertEqual(database.list_sessions(), [expected])
                self.assertEqual(database.require_session(session["id"])["working_dir"], "/former-worktree")
                self.assertEqual(database.recent_scrollback(session["id"]), transcript)
                self.assertEqual(database.pending_inputs(session["id"]), queued)
                result = database.get_project_by_id(project["id"])
                self.assertEqual(result["session_count"], 1)
                self.assertEqual(result["last_active_at"], expected["last_active_at"])
                self.assertNotIn("archived_at", result)
                self.assertNotIn("archived_session_count", result)
                self.assertEqual(database._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(database._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                database.close()

    def test_missing_current_columns_fail_without_repair_and_close_connection(self):
        for table, column in (
            ("projects", "created_at"),
            ("sessions", "agent_session_id"),
            ("sessions", "model"),
            ("sessions", "reasoning_level"),
            ("sessions", "sandbox"),
            ("sessions", "auto_approve_inter_agent_communication"),
            ("sessions", "detached_working_dir"),
        ):
            with self.subTest(table=table, column=column), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "sessions.db"
                database = Database(path)
                database.close()
                connection = sqlite3.connect(path)
                try:
                    connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                    connection.commit()
                    before = list(connection.iterdump())
                finally:
                    connection.close()

                database = Database.__new__(Database)
                with self.assertRaises(sqlite3.OperationalError):
                    database.__init__(path)
                with self.assertRaises(sqlite3.ProgrammingError):
                    database._conn.execute("SELECT 1")

                connection = sqlite3.connect(path)
                try:
                    self.assertEqual(list(connection.iterdump()), before)
                finally:
                    connection.close()

    def test_ancient_session_schema_is_not_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT, "
                    "working_dir TEXT, claude_session_id TEXT)"
                )
                connection.execute("INSERT INTO sessions VALUES ('uuid', 'old', '/old', 'resume')")
                connection.commit()
                before = list(connection.iterdump())
            finally:
                connection.close()
            with self.assertRaises(sqlite3.OperationalError):
                Database(path)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(list(connection.iterdump()), before)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
