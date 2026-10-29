from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from agent_ui_server.db import Database


DEFAULT_SETTINGS = {
    "worktree_path_template": "%P/%N-%B",
    "default_working_directory": "/projects/",
    "default_agent": None,
}


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from agent_ui_server import main

        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(Path(self.temp.name) / "settings.db")
        self.addCleanup(self.database.close)
        patcher = mock.patch.object(main, "db", self.database)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_default_update_and_omitted_fields(self):
        self.assertEqual(await self.main.get_settings(), DEFAULT_SETTINGS)
        template = "%P/worktrees/%N-%b"
        result = await self.main.update_settings(self.main.UpdateSettingsRequest(
            worktree_path_template=f"  {template}  ",
        ))
        self.assertEqual(result, {**DEFAULT_SETTINGS, "worktree_path_template": template})
        self.assertEqual(await self.main.get_settings(), result)
        self.assertEqual(await self.main.update_settings(self.main.UpdateSettingsRequest()), result)
        self.assertEqual(self.database.list_worktrees(), [])
        self.assertEqual(self.database.list_projects(), [])

    async def test_update_directory_agent_and_clear_agent(self):
        expected = {
            **DEFAULT_SETTINGS,
            "default_working_directory": "/srv/projects/",
            "default_agent": "pi",
        }
        result = await self.main.update_settings(self.main.UpdateSettingsRequest(
            default_working_directory=" /srv/projects/ ", default_agent=" pi ",
        ))
        self.assertEqual(result, expected)
        result = await self.main.update_settings(self.main.UpdateSettingsRequest(
            worktree_path_template="%P/trees/%N-%B",
        ))
        self.assertEqual(result["default_agent"], "pi")
        self.assertEqual(result["default_working_directory"], "/srv/projects/")
        result = await self.main.update_settings(self.main.UpdateSettingsRequest(default_agent=None))
        self.assertIsNone(result["default_agent"])
        self.assertEqual(result["worktree_path_template"], "%P/trees/%N-%B")
        self.assertEqual(result["default_working_directory"], "/srv/projects/")

    async def test_persistence_and_shared_reads(self):
        expected = {
            "worktree_path_template": "relative/%N-%%-%B",
            "default_working_directory": "/srv/projects/",
            "default_agent": "claude-code",
        }
        await self.main.update_settings(self.main.UpdateSettingsRequest(**expected))
        other = Database(self.database.path)
        try:
            self.assertEqual(other.get_settings(), expected)
            other.update_settings({"worktree_path_template": "%P/trees/%N-%B"})
            self.assertEqual(await self.main.get_settings(), {
                **expected, "worktree_path_template": "%P/trees/%N-%B",
            })
        finally:
            other.close()

    def test_invalid_updates(self):
        for field in ("worktree_path_template", "default_working_directory"):
            for value in (None, "", " \t\n", 123, True, [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    self.main.UpdateSettingsRequest(**{field: value})
        for value in ("", " \t", "unknown", 123, True, [], {}):
            with self.subTest(agent=value), self.assertRaises(ValidationError):
                self.main.UpdateSettingsRequest(default_agent=value)
        with self.assertRaises(ValidationError):
            self.main.UpdateSettingsRequest(unknown="value")
        self.assertEqual(self.database.get_settings(), DEFAULT_SETTINGS)

    async def test_invalid_combined_update_leaves_all_fields_unchanged(self):
        with self.assertRaises(ValidationError):
            await self.main.update_settings(self.main.UpdateSettingsRequest(
                default_working_directory="/srv/projects", default_agent="unknown",
            ))
        self.assertEqual(await self.main.get_settings(), DEFAULT_SETTINGS)

    def test_singleton_constraint(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database._conn:
                self.database._conn.execute(
                    "INSERT INTO server_settings "
                    "(id, worktree_path_template, default_working_directory) VALUES (2, 'x', '/projects/')"
                )

    def test_routes_and_worktree_request_unchanged(self):
        routes = {(route.path, method) for route in self.main.app.routes
                  for method in getattr(route, "methods", ())}
        self.assertIn(("/settings", "GET"), routes)
        self.assertIn(("/settings", "PATCH"), routes)
        self.assertEqual(set(self.main.CreateWorktreeRequest.model_fields), {
            "project_path", "path", "branch",
        })


if __name__ == "__main__":
    unittest.main()
