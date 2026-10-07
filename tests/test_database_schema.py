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
                database.set_session_archived(session["id"], True)
                database.detach_session_from_worktree(session["id"])
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

    def test_missing_current_columns_fail_without_repair_and_close_connection(self):
        for table, column in (
            ("projects", "archived_at"),
            ("sessions", "agent_session_id"),
            ("sessions", "model"),
            ("sessions", "reasoning_level"),
            ("sessions", "archived_at"),
            ("sessions", "archived_with_project"),
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
