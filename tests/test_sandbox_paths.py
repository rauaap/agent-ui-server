from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from agent_ui_server.agent import ClaudeCodeAdapter, PiAdapter
from agent_ui_server.db import Database
from agent_ui_server.sandbox import claude_sandbox_command, pi_sandbox_command
from agent_ui_server.sandbox import sandbox_command
from agent_ui_server.sandbox_paths import destination, merge_paths, validate_paths
from test_sandbox import bubblewrap_unavailable, require_sandbox


class PathFixture:
    def setUp(self):
        # Use a host-backed project directory: /tmp is intentionally protected.
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config with spaces"
        self.config.mkdir()
        self.file = self.config / "settings"
        self.file.write_text("original")
        self.work = self.root / "project"
        self.work.mkdir()

    def entry(self, write=False):
        return {"path": str(self.config), "write": write}


class SandboxPathTests(PathFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # Argv tests use a fake launcher; the real test checks it can start.
        patch = mock.patch("agent_ui_server.sandbox.verify_network_namespace")
        patch.start()
        self.addCleanup(patch.stop)

    def test_expansion_and_relative_rejection(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root), "CONFIG_TEST": str(self.config)}):
            for value in ("~/config with spaces", "$HOME/config with spaces",
                          "${HOME}/config with spaces", "$CONFIG_TEST"):
                self.assertEqual(destination(value), self.config)
        for value in ("config", "./config", "../config"):
            with self.assertRaisesRegex(ValueError, "absolute"):
                destination(value)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(destination("/example/$NOT_DEFINED"), Path("/example/$NOT_DEFINED"))

    def test_merge_precedence_and_reset(self):
        with mock.patch.dict(os.environ, {"SANDBOX_TEST_ROOT": str(self.root)}):
            override = {"path": "$SANDBOX_TEST_ROOT/config with spaces"}
            merged = merge_paths([self.entry(True)], [override])
            self.assertEqual(merged, [override])
            self.assertFalse(validate_paths(merged)[0].write)
        self.assertEqual(merge_paths([self.entry(True)], []), [self.entry(True)])
        self.assertTrue(validate_paths(merge_paths([self.entry()], [self.entry(True)]))[0].write)
        other = {"path": str(self.work)}
        self.assertEqual(merge_paths([self.entry()], [other]), [self.entry(), other])

    def test_files_symlinks_missing_and_conflicts(self):
        link = self.root / "link"
        link.symlink_to(self.config)
        mount = validate_paths([{"path": str(link)}])[0]
        self.assertEqual(mount.source, self.config)
        self.assertEqual(mount.destination, link)
        self.assertEqual(validate_paths([{"path": str(self.file)}])[0].source, self.file)
        for entries in (
            [self.entry(), self.entry(True)],
            [self.entry(), {"path": str(self.file)}],
            [self.entry(), {"path": str(link)}],
            [{"path": str(self.root / "missing")}],
            [{"path": str(Path.home())}],
            [{"path": "/"}],
            [{"path": "/tmp"}],
            [{"path": "/usr/bin"}],
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                validate_paths(entries)
        with self.assertRaises(ValueError):
            merge_paths([self.entry()], [{"path": str(self.file)}])
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "regular file"):
            validate_paths([{"path": str(fifo)}])

    def build(self, paths, command=None):
        return sandbox_command(command or ["/bin/true"], str(self.work),
                               read_only=[], writable=[], environment={}, sandbox_paths=paths)

    def test_mount_modes_and_builtin_conflicts(self):
        with mock.patch("agent_ui_server.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
            for write in (False, True):
                args = self.build([self.entry(write)])
                index = args.index(str(self.config))
                self.assertEqual(args[index - 1], "--bind" if write else "--ro-bind")
                self.assertEqual(args[index + 1], str(self.config))
            with self.assertRaisesRegex(ValueError, "built-in"):
                self.build([{"path": str(self.work)}])
            # No broad parent mount exposing siblings is added for this symlink.
            link = self.root / "link"
            link.symlink_to(self.config)
            args = self.build([{"path": str(link)}])
            index = args.index(str(self.config))
            self.assertEqual(args[index + 1], str(link))
            self.assertNotIn(str(self.root), args)

    def test_both_profiles_include_extra_paths(self):
        home = self.root / "home"
        home.mkdir()
        runtime = home / "runtime"
        (runtime / "bin").mkdir(parents=True)
        pi = runtime / "bin/pi"
        pi.touch()
        (runtime / "bin/node").touch()
        claude = home / "claude"
        claude.write_bytes(b"\x7fELFfixture")
        with mock.patch.dict(os.environ, {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude")}), mock.patch(
            "agent_ui_server.sandbox.shutil.which", side_effect=lambda p: "/usr/bin/bwrap" if p == "bwrap" else p
        ):
            for builder, binary in ((pi_sandbox_command, pi), (claude_sandbox_command, claude)):
                args = builder([str(binary)], str(self.work), sandbox_paths=[self.entry(True)])
                index = args.index(str(self.config))
                self.assertEqual(args[index - 1:index + 2], ["--bind", str(self.config), str(self.config)])

    @unittest.skipUnless(shutil.which("bwrap"), "Bubblewrap not installed")
    def test_real_read_write_and_sibling_isolation(self):
        require_sandbox(self)
        sibling = self.root / "secret"
        sibling.write_text("hidden")
        link = self.root / "link"
        link.symlink_to(self.config)
        for write in (False, True):
            command = ["/bin/sh", "-c", 'test ! -e "$1" && cat "$2/settings" && echo changed > "$2/settings"',
                       "sh", str(sibling), str(link)]
            result = subprocess.run(self.build([{"path": str(link), "write": write}], command),
                                    capture_output=True, text=True, env={})
            if bubblewrap_unavailable(result.stderr):
                self.skipTest(result.stderr.strip())
            self.assertIn("original", result.stdout)
            self.assertEqual(result.returncode == 0, write, result.stderr)
            self.assertEqual(self.file.read_text(), "changed\n" if write else "original")


class SandboxPathsAPITests(PathFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        from agent_ui_server import main
        self.main = main
        self.database = Database(self.root / "sessions.db")
        self.addCleanup(self.database.close)
        patch = mock.patch.object(main, "db", self.database)
        patch.start()
        self.addCleanup(patch.stop)
        self.project = self.database.create_project(str(self.work), "project")

    async def settings(self, **values):
        return await self.main.update_sandbox_paths(self.main.UpdateSandboxPathsRequest(**values))

    async def project_settings(self, **values):
        return await self.main.update_project(self.main.UpdateProjectRequest(path=str(self.work), **values))

    async def test_defaults_roundtrip_and_restart(self):
        self.assertEqual(await self.main.get_sandbox_paths(), {"sandbox_paths": []})
        self.assertEqual(self.project["sandbox_paths"], [])
        self.assertEqual(await self.settings(sandbox_paths=[{"path": str(self.config)}]),
                         {"sandbox_paths": [self.entry()]})
        self.assertEqual(await self.settings(), {"sandbox_paths": [self.entry()]})
        project = await self.project_settings(sandbox_paths=[self.entry(True)])
        self.assertEqual(project["sandbox_paths"], [self.entry(True)])
        self.assertEqual(project["sessions_affected"], 0)
        self.assertEqual((await self.project_settings())["sandbox_paths"], [self.entry(True)])
        reopened = Database(self.database.path)
        try:
            self.assertEqual(reopened.get_sandbox_paths(), [self.entry()])
            self.assertEqual(reopened.get_project_by_id(self.project["id"])["sandbox_paths"], [self.entry(True)])
        finally:
            reopened.close()
        await self.project_settings(sandbox_paths=[])
        self.assertEqual(self.database.sandbox_paths_snapshot(self.project["id"]), ([self.entry()], []))
        self.assertEqual(await self.settings(sandbox_paths=[]), {"sandbox_paths": []})

    async def test_existing_database_migration(self):
        import sqlite3
        legacy_path = self.root / "legacy.db"
        legacy = Database(legacy_path)
        project = legacy.create_project(str(self.work), "legacy")
        legacy.close()
        with sqlite3.connect(legacy_path) as conn:
            conn.execute("DROP TABLE sandbox_paths")
        migrated = Database(legacy_path)
        try:
            self.assertEqual(migrated.get_sandbox_paths(), [])
            self.assertEqual(migrated.get_project_by_id(project["id"])["sandbox_paths"], [])
        finally:
            migrated.close()

    async def test_normalized_schema_constraints_and_cascade(self):
        import sqlite3
        await self.settings(sandbox_paths=[self.entry()])
        await self.project_settings(sandbox_paths=[self.entry(True)])
        with sqlite3.connect(self.database.path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertNotIn("server_settings", tables)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
            self.assertNotIn("sandbox_paths", columns)
            rows = conn.execute("SELECT project_id, path, writable FROM sandbox_paths ORDER BY id").fetchall()
            self.assertEqual(rows, [(None, str(self.config), 0), (self.project["id"], str(self.config), 1)])
            for project_id in (None, self.project["id"]):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO sandbox_paths(project_id, path) VALUES (?, ?)",
                                 (project_id, str(self.config)))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO sandbox_paths(path, writable) VALUES ('/invalid', 2)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.set_sandbox_paths([self.entry(), self.entry()])
        self.assertEqual(self.database.get_sandbox_paths(), [self.entry()])
        self.database.delete_project(str(self.work))
        self.assertEqual(self.database.get_sandbox_paths(self.project["id"]), [])
        self.assertEqual(self.database.get_sandbox_paths(), [self.entry()])
        routes = {route.path for route in self.main.app.routes}
        self.assertNotIn("/settings", routes)
        self.assertIn("/sandbox-paths", routes)

    async def test_adapter_forwarding_and_disabled_bypass(self):
        for adapter, wrapper in ((PiAdapter(executable="pi"), "pi_sandbox_command"),
                                 (ClaudeCodeAdapter(executable="claude"), "claude_sandbox_command")):
            session = {"id": 123, "working_dir": str(self.work), "sandbox_paths": [self.entry()],
                       "git_repository": str(self.work)}
            with mock.patch(f"agent_ui_server.agent.{wrapper}", side_effect=ValueError("unsafe")) as wrap, mock.patch(
                "agent_ui_server.agent.asyncio.create_subprocess_exec", side_effect=FileNotFoundError("fixture")
            ) as spawn:
                events = [event async for event in adapter.start_turn(session, "test")]
                self.assertEqual(wrap.call_args.kwargs["sandbox_paths"], [self.entry()])
                self.assertEqual(wrap.call_args.kwargs["git_repository"], str(self.work))
                spawn.assert_not_called()
                self.assertIn("unsafe", events[0]["message"])
                wrap.reset_mock()
                session["sandbox"] = False
                events = [event async for event in adapter.start_turn(session, "test")]
                wrap.assert_not_called()
                spawn.assert_called_once()

    async def test_validation_atomicity_and_create(self):
        await self.settings(sandbox_paths=[self.entry()])
        for paths in ([self.entry(), {"path": "relative"}], [self.entry(), self.entry()]):
            with self.assertRaises(HTTPException):
                await self.settings(sandbox_paths=paths)
            self.assertEqual((await self.main.get_sandbox_paths())["sandbox_paths"], [self.entry()])
        with self.assertRaises(HTTPException):
            await self.project_settings(archived=True, sandbox_paths=[{"path": "relative"}])
        self.assertIsNone(self.database.get_project(str(self.work))["archived_at"])
        created = await self.main.create_project(self.main.CreateProjectRequest(
            path=str(self.root / "new-project"), sandbox_paths=[self.entry(True)]))
        self.assertEqual(created["sandbox_paths"], [self.entry(True)])
        # Repeated creation remains a no-op.
        repeated = await self.main.create_project(self.main.CreateProjectRequest(path=created["path"]))
        self.assertEqual(repeated["sandbox_paths"], [self.entry(True)])

    async def test_running_turn_snapshot_and_project_isolation(self):
        await self.settings(sandbox_paths=[self.entry(True)])
        await self.project_settings(sandbox_paths=[self.entry()])
        session = self.database.create_session("test", self.project["id"], agent="pi")
        started, release = asyncio.Event(), asyncio.Event()
        snapshots, repositories = [], []

        class Adapter:
            async def start_turn(adapter, session, prompt):
                snapshots.append(session["sandbox_paths"])
                repositories.append(session["git_repository"])
                started.set()
                await release.wait()
                if False:
                    yield {}

        with mock.patch.dict(self.main.adapters, {"pi": Adapter()}):
            task = asyncio.create_task(self.main.run_turn(session["id"], "first"))
            await asyncio.wait_for(started.wait(), 2)
            try:
                await self.project_settings(sandbox_paths=[])
                self.assertEqual(snapshots, [[self.entry()]])
            finally:
                release.set()
                await task
            await self.main.run_turn(session["id"], "second")
        self.assertEqual(snapshots, [[self.entry()], [self.entry(True)]])
        # Taken from the project row, not from anything in the working tree.
        self.assertEqual(repositories, [str(self.work)] * 2)
        other = self.database.create_project(str(self.root / "other"), "other")
        await self.project_settings(sandbox_paths=[self.entry()])
        self.assertEqual(self.database.sandbox_paths_snapshot(other["id"]), ([self.entry(True)], []))

    async def test_missing_path_fails_turn_without_starting_adapter(self):
        await self.settings(sandbox_paths=[self.entry()])
        session = self.database.create_session("test", self.project["id"], agent="pi")
        self.file.unlink()
        self.config.rmdir()
        with mock.patch.object(self.main.adapters["pi"], "start_turn") as start:
            await self.main.run_turn(session["id"], "test")
        start.assert_not_called()
        events = self.database.recent_scrollback(session["id"])
        self.assertTrue(any("Invalid sandbox path" in str(event) for event in events))
