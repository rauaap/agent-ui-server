from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from agent_ui_server.agent import ClaudeCodeAdapter, PiAdapter
from agent_ui_server.db import Database
from agent_ui_server.sandbox import (
    claude_sandbox_command,
    git_metadata_directories,
    pi_sandbox_command,
    prepare_scratch,
)


def bubblewrap_unavailable(stderr):
    return any(message in stderr for message in (
        "Operation not permitted", "No permissions to create",
        "bwrap: setting up uid map: Read-only file system",
    ))


class SandboxSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from agent_ui_server import main

        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "sessions.db")
        self.addCleanup(self.database.close)
        patch = mock.patch.object(main, "db", self.database)
        patch.start()
        self.addCleanup(patch.stop)
        self.project = self.database.create_project(self.tmp.name, "project")

    async def create(self, **settings):
        return await self.main.create_session(self.main.CreateSessionRequest(
            name="session", project_path=self.tmp.name, agent="pi", **settings
        ))

    async def patch(self, session_id, **settings):
        return await self.main.update_session(
            session_id, self.main.UpdateSessionRequest(**settings)
        )

    async def test_default_override_persistence_and_settings_event(self):
        default = await self.create()
        disabled = await self.create(sandbox=False)
        self.assertIs(default["sandbox"], True)
        self.assertIs(disabled["sandbox"], False)
        with mock.patch.object(self.main, "enqueue_for_subscribers", return_value=[]) as send:
            updated = await self.patch(default["id"], sandbox=False)
        self.assertIs(updated["sandbox"], False)
        self.assertEqual(send.call_args.args[1], [{
            "type": "settings", "sandbox": False,
            "auto_approve_write": False, "auto_approve_command": False,
        }])
        self.assertIs((await self.patch(default["id"], name="renamed"))["sandbox"], False)
        reopened = Database(self.database.path)
        try:
            self.assertIs(reopened.require_session(default["id"])["sandbox"], False)
            self.assertTrue(all(type(s["sandbox"]) is bool for s in reopened.list_sessions()))
        finally:
            reopened.close()
        self.assertIs((await self.patch(default["id"], sandbox=True))["sandbox"], True)

    async def test_busy_rejects_before_other_fields_and_idle_allows(self):
        session = await self.create()
        for status in ("running", "awaiting_approval"):
            self.database.update_status(session["id"], status)
            with self.assertRaises(HTTPException) as caught:
                await self.patch(session["id"], sandbox=False, name="wrong", auto_approve_write=True)
            self.assertEqual(caught.exception.status_code, 409)
            current = self.database.require_session(session["id"])
            self.assertIs(current["sandbox"], True)
            self.assertIs(current["auto_approve_write"], False)
            self.assertEqual(current["name"], "session")
            # Other settings are still live-editable.
            await self.patch(session["id"], auto_approve_command=True)
        self.database.update_status(session["id"], "idle")
        self.assertIs((await self.patch(session["id"], sandbox=False))["sandbox"], False)

    async def test_idle_status_with_unfinished_turn_is_still_rejected(self):
        session = await self.create()
        task = asyncio.create_task(asyncio.sleep(60))
        self.main.running_tasks[session["id"]] = task
        try:
            with self.assertRaises(HTTPException) as caught:
                await self.patch(session["id"], sandbox=False)
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.main.running_tasks.pop(session["id"], None)

    async def test_setting_change_serializes_with_turn_start(self):
        session = await self.create()
        # Simulate begin_turn holding the lock before its status commit.
        async with self.main.turn_lock:
            request = asyncio.create_task(self.patch(session["id"], sandbox=False))
            await asyncio.sleep(0)
            self.assertFalse(request.done())
            self.database.update_status(session["id"], "running")
        with self.assertRaises(HTTPException) as caught:
            await request
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIs(self.database.require_session(session["id"])["sandbox"], True)

    async def test_claude_uses_the_same_persisted_setting(self):
        session = await self.main.create_session(self.main.CreateSessionRequest(
            name="claude", project_path=self.tmp.name, agent="claude-code"
        ))
        self.assertIs(session["sandbox"], True)
        self.assertIs((await self.patch(session["id"], sandbox=False))["sandbox"], False)

    async def test_existing_database_migrates_to_true(self):
        session = await self.create(sandbox=False)
        # Simulate a pre-feature database, preserving the rest of the schema.
        with sqlite3.connect(self.database.path) as conn:
            conn.execute("ALTER TABLE sessions DROP COLUMN sandbox")
        migrated = Database(self.database.path)
        try:
            self.assertIs(migrated.require_session(session["id"])["sandbox"], True)
        finally:
            migrated.close()


class SandboxCommandTests(unittest.TestCase):
    def setUp(self):
        # Keep synthetic home parents outside the intentionally writable /tmp.
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.cwd = self.home / "project"
        self.cwd.mkdir(parents=True)
        self.bin = self.home / "runtime" / "bin"
        self.bin.mkdir(parents=True)
        for name in ("pi", "node"):
            path = self.bin / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        self.gate = self.root / "gate.ts"
        self.gate.write_text("// gate")
        self.web = self.root / "web" / "index.ts"
        self.web.parent.mkdir()
        self.web.write_text("// web")
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.command = [str(self.bin / "pi"), "--mode", "rpc", "-e", str(self.gate),
                        "--no-extensions", "-e", str(self.web), "--session", "resume-id"]
        for patch in (
            mock.patch.dict(os.environ, {"HOME": str(self.home), "UNRELATED_SECRET": "secret"}),
            mock.patch("agent_ui_server.sandbox.prepare_scratch", return_value=self.scratch),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def build(self):
        return pi_sandbox_command(self.command, str(self.cwd))

    def test_mounts_environment_and_resume_argv(self):
        with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=lambda p: "/usr/bin/bwrap" if p == "bwrap" else p):
            command = self.build()
        writable = [command[i + 1:i + 3] for i, arg in enumerate(command) if arg == "--bind"]
        self.assertEqual(writable, [
            [str(self.scratch), "/tmp"], [str(self.home / ".pi")] * 2, [str(self.cwd)] * 2,
        ])
        self.assertNotIn("--tmpfs", command)
        self.assertNotIn("secret", command)
        env = {command[i + 1]: command[i + 2] for i, arg in enumerate(command) if arg == "--setenv"}
        self.assertEqual(set(env), {"HOME", "USER", "PATH", "TERM", "LANG", "TMPDIR", "XDG_CACHE_HOME"})
        self.assertEqual(env["PATH"], f"{self.bin}:/usr/bin:/bin")
        self.assertEqual(command[-2:], ["--session", "resume-id"])
        self.assertIn("--share-net", command)
        self.assertIn("--clearenv", command)
        inner = command[command.index("--") + 1:]
        self.assertEqual(inner[0], str(self.bin / "pi"))
        self.assertIn("--no-extensions", inner)
        self.assertTrue(all(inner[i + 1].startswith("/opt/agent-ui/") for i, arg in enumerate(inner) if arg == "-e"))

    def test_pi_host_prompt_uses_resolved_mount_plan(self):
        from agent_ui_server.host_tools import pi_sandbox_guidance

        extra = self.home / "additional-settings"
        extra.write_text("settings")
        with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=lambda p: "/usr/bin/bwrap" if p == "bwrap" else p):
            command = pi_sandbox_command(
                self.command, str(self.cwd), sandbox_paths=[{"path": str(extra)}],
                system_prompt=pi_sandbox_guidance,
            )
        prompt = command[command.index("--append-system-prompt") + 1]
        writable, readonly = prompt.split("Read-only mounts:", 1)
        for index, arg in enumerate(command):
            if arg in {"--bind", "--ro-bind", "--ro-bind-try"}:
                destination = command[index + 2]
                self.assertIn(json.dumps(destination), writable if arg == "--bind" else readonly)
        self.assertIn(json.dumps(str(self.cwd)), writable)
        self.assertIn(json.dumps(str(self.home / ".pi")), writable)
        self.assertIn(json.dumps(str(extra)), readonly)
        self.assertIn("/opt/agent-ui/extension-", readonly)
        self.assertIn("bypass_sandbox(command, reason)", prompt)
        self.assertNotIn("mcp__", prompt)
        self.assertNotIn("ToolSearch", prompt)

    def test_missing_bwrap_fails_closed(self):
        with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=lambda p: None if p == "bwrap" else p):
            with self.assertRaisesRegex(FileNotFoundError, "Bubblewrap"):
                self.build()

    def test_cannot_mount_home_as_project(self):
        self.cwd = self.home
        with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=lambda p: p):
            with self.assertRaisesRegex(ValueError, "home directory"):
                self.build()

    def make_worktree(self):
        repo = self.home / "main project"
        self.git("init", "-b", "main", str(repo))
        self.git("-C", str(repo), "config", "user.name", "Sandbox Test")
        self.git("-C", str(repo), "config", "user.email", "sandbox@example.com")
        (repo / "tracked.txt").write_text("initial\n")
        self.git("-C", str(repo), "add", "tracked.txt")
        self.git("-C", str(repo), "commit", "-m", "initial")
        self.git("-C", str(repo), "worktree", "add", "-b", "sandbox-test", str(self.cwd))
        (repo / "private-source").write_text("not available to the worktree")
        return repo

    def git(self, *args):
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    @unittest.skipUnless(shutil.which("git"), "Git not installed")
    def test_worktree_mounts_only_shared_git_metadata(self):
        repo = self.make_worktree()
        for relative in (False, True):
            with self.subTest(relative=relative):
                if relative:
                    git_dir = repo / ".git" / "worktrees" / self.cwd.name
                    (self.cwd / ".git").write_text(
                        f"gitdir: {os.path.relpath(git_dir, self.cwd)}\n"
                    )
                with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=lambda p: "/usr/bin/bwrap" if p == "bwrap" else p):
                    command = self.build()
                writable = [command[i + 1:i + 3] for i, arg in enumerate(command) if arg == "--bind"]
                self.assertEqual(writable, [
                    [str(self.scratch), "/tmp"], [str(self.home / ".pi")] * 2,
                    [str(self.cwd)] * 2, [str(repo / ".git")] * 2,
                ])

    @unittest.skipUnless(shutil.which("git"), "Git not installed")
    def test_ordinary_repository_needs_no_extra_mount(self):
        self.git("init", str(self.cwd))
        self.assertEqual(git_metadata_directories(self.cwd, self.home), [])

    def test_invalid_gitfile_and_missing_metadata_fail_closed(self):
        for contents in ("not a gitfile\n", "gitdir: \n", "gitdir: /missing/repo\n"):
            with self.subTest(contents=contents):
                (self.cwd / ".git").write_text(contents)
                with self.assertRaises(ValueError):
                    git_metadata_directories(self.cwd, self.home)

    def test_separate_worktree_and_common_metadata_are_both_mounted(self):
        git_dir = self.home / "worktree-metadata"
        common_dir = self.home / "common-metadata"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "commondir").write_text("../common-metadata\n")
        (common_dir / "objects").mkdir(parents=True)
        (common_dir / "refs").mkdir()
        (self.cwd / ".git").write_text("gitdir: ../worktree-metadata\n")
        self.assertEqual(
            git_metadata_directories(self.cwd, self.home), [common_dir, git_dir]
        )
        (git_dir / "commondir").write_text("")
        with self.assertRaisesRegex(ValueError, "Invalid Git metadata pointer"):
            git_metadata_directories(self.cwd, self.home)

    def test_oversized_git_pointer_is_rejected(self):
        (self.cwd / ".git").write_text("gitdir: " + "a" * 8192)
        with self.assertRaisesRegex(ValueError, "Invalid Git metadata pointer"):
            git_metadata_directories(self.cwd, self.home)

    def test_git_metadata_cannot_expose_home(self):
        (self.home / "HEAD").write_text("ref: refs/heads/main\n")
        (self.home / "objects").mkdir()
        (self.home / "refs").mkdir()
        (self.cwd / ".git").write_text(f"gitdir: {self.home}\n")
        with self.assertRaisesRegex(ValueError, "must not expose home"):
            git_metadata_directories(self.cwd, self.home)

    @unittest.skipUnless(shutil.which("git") and shutil.which("bwrap"), "Git/Bubblewrap not installed")
    def test_real_sandbox_worktree_can_commit_without_exposing_main_checkout(self):
        repo = self.make_worktree()
        # Check both normal Git pointers and a relative pointer through a
        # symlink. The sandbox must preserve the path used by the gitfile.
        alias = self.home / "repo-alias"
        alias.symlink_to(repo, target_is_directory=True)
        for metadata_root in (repo, alias):
            with self.subTest(metadata_root=metadata_root):
                git_dir = metadata_root / ".git" / "worktrees" / self.cwd.name
                pointer = str(git_dir) if metadata_root == repo else os.path.relpath(git_dir, self.cwd)
                (self.cwd / ".git").write_text(f"gitdir: {pointer}\n")
                command = self.build()
                probe = r'''
import json, subprocess, sys
from pathlib import Path
repo, alias = map(Path, sys.argv[1:])
def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()
assert git('status', '--porcelain') == ''
assert git('branch', '--show-current') == 'sandbox-test'
with Path('tracked.txt').open('a') as f:
    f.write('sandbox commit\n')
git('add', 'tracked.txt')
git('commit', '-m', 'from sandbox')
assert git('status', '--porcelain') == ''
for root in (repo, alias):
    assert not (root / 'private-source').exists()
    assert not (root / 'tracked.txt').exists()
    try:
        (root / 'new-file').write_text('must fail')
    except OSError:
        pass
    else:
        raise AssertionError('main checkout is writable')
print(json.dumps({'commit': git('rev-parse', 'HEAD')}))
'''
                command = command[:command.index("--") + 1] + [
                    "/usr/bin/python3", "-c", probe, str(repo), str(alias),
                ]
                result = subprocess.run(command, capture_output=True, text=True, env={}, timeout=10)
                if result.returncode and bubblewrap_unavailable(result.stderr):
                    self.skipTest(result.stderr.strip())
                self.assertEqual(result.returncode, 0, result.stderr)
                commit = json.loads(result.stdout)["commit"]
                self.assertEqual(self.git("-C", str(repo), "rev-parse", "sandbox-test"), commit)
                self.assertEqual(self.git("-C", str(repo), "log", "-1", "--format=%s", "sandbox-test"), "from sandbox")
                self.assertEqual((repo / "tracked.txt").read_text(), "initial\n")
                self.assertEqual(self.git("-C", str(repo), "status", "--porcelain"), "?? private-source")

    @unittest.skipUnless(shutil.which("bwrap"), "Bubblewrap not installed")
    def test_real_sandbox_filesystem_and_environment(self):
        (self.home / "hidden-secret").write_text("not visible")
        command = self.build()
        probe = r'''
import json, os
from pathlib import Path
home = Path.home()
def writable(path):
    try:
        (path / "probe").write_text("written")
        return True
    except OSError:
        return False
print(json.dumps({
    "hidden": not (home / "hidden-secret").exists(),
    "writes": [writable(p) for p in [Path('/tmp'), Path.cwd(), home / '.pi', home, Path('/'), home / 'runtime', Path('/dev/shm')]],
    "secret": os.environ.get('UNRELATED_SECRET'),
    "extensions": [p.read_text() for p in Path('/opt/agent-ui').glob('**/*.ts')],
}))
'''
        command = command[:command.index("--") + 1] + ["/usr/bin/python3", "-c", probe]
        result = subprocess.run(command, capture_output=True, text=True, env={}, timeout=10)
        if result.returncode and bubblewrap_unavailable(result.stderr):
            self.skipTest(result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["hidden"])
        self.assertEqual(data["writes"], [True, True, True, False, False, False, False])
        self.assertIsNone(data["secret"])
        self.assertCountEqual(data["extensions"], ["// gate", "// web"])
        self.assertEqual((self.scratch / "probe").read_text(), "written")


class ScratchTests(unittest.TestCase):
    def test_shared_private_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            with mock.patch("agent_ui_server.sandbox.Path", return_value=scratch):
                self.assertEqual(prepare_scratch(), scratch)
                (scratch / "retained").write_text("shared")
                scratch.chmod(0o777)
                self.assertEqual(prepare_scratch(), scratch)
                self.assertEqual(scratch.stat().st_mode & 0o777, 0o700)
                self.assertTrue((scratch / "retained").exists())
                (scratch / "retained").unlink()
                scratch.rmdir()
                scratch.symlink_to(Path(tmp), target_is_directory=True)
                with self.assertRaises(OSError):
                    prepare_scratch()

    def test_foreign_owned_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            with mock.patch("agent_ui_server.sandbox.Path", return_value=scratch), mock.patch(
                "agent_ui_server.sandbox.os.getuid", return_value=os.getuid() + 1
            ):
                with self.assertRaisesRegex(ValueError, "Unsafe scratch"):
                    prepare_scratch()


class SandboxAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_wraps_false_bypasses_and_next_turn_rechecks(self):
        adapter = PiAdapter(executable="pi")
        session = {"id": 123, "working_dir": "/project", "agent_session_id": "resume-id"}
        with mock.patch("agent_ui_server.agent.pi_sandbox_command", return_value=["bwrap", "wrapped"]) as wrap, mock.patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec", side_effect=FileNotFoundError("test spawn")
        ) as spawn:
            for enabled in (True, False, True):
                if not enabled:
                    session["sandbox"] = False
                elif "sandbox" in session:
                    session["sandbox"] = True
                wrap.reset_mock()
                events = [event async for event in adapter.start_turn(session, "hello")]
                self.assertEqual(events[0]["type"], "error")
                if enabled:
                    wrap.assert_called_once()
                    self.assertIn("--session", wrap.call_args.args[0])
                    self.assertEqual(spawn.call_args.args, ("bwrap", "wrapped"))
                    self.assertEqual(spawn.call_args.kwargs["env"], {})
                else:
                    wrap.assert_not_called()
                    self.assertEqual(spawn.call_args.args[0], "pi")

    async def test_setup_failure_does_not_spawn_unsandboxed(self):
        adapter = PiAdapter(executable="pi")
        with mock.patch("agent_ui_server.agent.pi_sandbox_command", side_effect=ValueError("unsafe")), mock.patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec"
        ) as spawn:
            events = [event async for event in adapter.start_turn({"id": 1, "working_dir": "/project"}, "go")]
        spawn.assert_not_called()
        self.assertIn("unsafe", events[0]["message"])
