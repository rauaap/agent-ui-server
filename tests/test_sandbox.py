from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from agent_ui_server.agent import ClaudeCodeAdapter, PiAdapter, stop_process
from agent_ui_server.db import Database
from agent_ui_server.sandbox import (
    BLOCKED_NETWORKS,
    SANDBOX_DNS,
    SANDBOX_RESOLV_CONF,
    claude_sandbox_command,
    git_metadata_directories,
    host_addresses,
    pi_sandbox_command,
    prepare_scratch,
    verify_network_namespace,
)


def bubblewrap_unavailable(stderr):
    return any(message in stderr for message in (
        "Operation not permitted", "No permissions to create",
        "bwrap: setting up uid map: Read-only file system",
        # pasta, inside an outer sandbox without a TUN device or real /proc.
        "Failed to open() /dev/net/tun", "Couldn't configure user mappings",
    ))


def require_sandbox(test):
    """Skip unless a real pasta + Bubblewrap sandbox can start here.

    Checked up front: a pasta that cannot set up would otherwise hang a
    test that captures its output.
    """
    tools = {name: shutil.which(name) for name in ("bwrap", "pasta", "ip")}
    if not all(tools.values()):
        test.skipTest("Bubblewrap, pasta or iproute2 not installed")
    try:
        verify_network_namespace(tools["pasta"])
    except OSError as exc:
        test.skipTest(str(exc))


def inner_start(command):
    """Index of the sandboxed command: after Bubblewrap's own `--`."""
    bwrap = next(i for i, arg in enumerate(command) if Path(arg).name == "bwrap")
    return command.index("--", bwrap) + 1


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
            "auto_approve_inter_agent_communication": False,
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
        self.repository = None
        self.command = [str(self.bin / "pi"), "--mode", "rpc", "-e", str(self.gate),
                        "--no-extensions", "-e", str(self.web), "--session", "resume-id"]
        for patch in (
            mock.patch.dict(os.environ, {"HOME": str(self.home), "UNRELATED_SECRET": "secret"}),
            mock.patch("agent_ui_server.sandbox.prepare_scratch", return_value=self.scratch),
            mock.patch("agent_ui_server.sandbox.verify_network_namespace"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def build(self):
        return pi_sandbox_command(self.command, str(self.cwd), git_repository=self.repository)

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
        inner = command[inner_start(command):]
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

    def test_network_namespace_blocks_host_and_private_ranges(self):
        which = lambda p: {"bwrap": "/usr/bin/bwrap", "ip": "/usr/sbin/ip"}.get(p, p)
        with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=which), mock.patch(
            "agent_ui_server.sandbox.host_addresses", return_value=["192.0.2.10", "203.0.113.5"],
        ):
            command = self.build()
        # pasta launches Bubblewrap through the fixed route script.
        self.assertEqual(command[0], "pasta")
        launcher = command[:command.index("--")]
        for flag in ("--config-net", "--foreground", "--ipv4-only", "--no-map-gw"):
            self.assertIn(flag, launcher)
        for flag in ("--tcp-ports", "--udp-ports", "--tcp-ns", "--udp-ns"):
            self.assertEqual(launcher[launcher.index(flag) + 1], "none")
        self.assertEqual(launcher[launcher.index("--dns-forward") + 1], SANDBOX_DNS)
        shell = command.index("--") + 1
        self.assertEqual(command[shell:shell + 2], ["/bin/sh", "-c"])
        script = command[shell + 2].splitlines()
        self.assertEqual(script[:3], [
            "set -e", "/usr/sbin/ip route flush table main",
            "/usr/sbin/ip route add default dev agent0",
        ])
        for network in (*BLOCKED_NETWORKS, "192.0.2.10/32", "203.0.113.5/32"):
            self.assertIn(f"/usr/sbin/ip route add blackhole {network}", script)
        self.assertEqual(script[-1], 'exec /usr/bin/env --default-signal=PIPE "$@"')
        self.assertEqual(command[shell + 3:shell + 5], ["sh", "/usr/bin/bwrap"])
        # Bubblewrap shares pasta's namespace and maps back to the real ids.
        sandbox = command[shell + 4:inner_start(command)]
        self.assertIn("--share-net", sandbox)
        self.assertEqual(sandbox[sandbox.index("--uid") + 1], str(os.getuid()))
        self.assertEqual(sandbox[sandbox.index("--gid") + 1], str(os.getgid()))
        mounts = [command[i:i + 3] for i, arg in enumerate(command) if arg == "--ro-bind"]
        self.assertIn(["--ro-bind", str(SANDBOX_RESOLV_CONF), "/etc/resolv.conf"], mounts)

    def test_resolv_conf_names_the_forwarded_resolver(self):
        self.assertEqual(SANDBOX_RESOLV_CONF.read_text(), f"nameserver {SANDBOX_DNS}\n")

    def test_missing_network_tools_fail_closed(self):
        for missing in ("pasta", "ip"):
            with self.subTest(missing=missing), mock.patch(
                "agent_ui_server.sandbox.shutil.which",
                side_effect=lambda p: None if p == missing else p,
            ):
                with self.assertRaisesRegex(FileNotFoundError, "network isolation"):
                    self.build()

    def fake_pasta(self, body):
        pasta = self.root / f"pasta-{len(list(self.root.glob('pasta-*')))}"
        pasta.write_text(f"#!/bin/sh\n{body}\n")
        pasta.chmod(0o755)
        return str(pasta)

    def test_broken_pasta_fails_closed_without_hanging_or_leaking(self):
        # Like real pasta: exit, but leave a helper holding the probe's
        # output. It must neither block the probe nor outlive it.
        helper = self.root / "helper.pid"
        pasta = self.fake_pasta(
            f"sleep 30 &\necho $! > '{helper}'\n"
            "echo 'Failed to open() /dev/net/tun: No such file' >&2\nexit 1"
        )
        started = time.monotonic()
        with self.assertRaisesRegex(OSError, "cannot create.*/dev/net/tun"):
            verify_network_namespace(pasta)
        self.assertLess(time.monotonic() - started, 4)
        pid = int(helper.read_text())
        for _ in range(50):
            if not StopProcessTests.alive(pid):
                break
            time.sleep(0.05)
        else:
            StopProcessTests.kill_quietly(pid)
            self.fail("preflight helper outlived the probe")

    def test_working_pasta_is_probed_once(self):
        pasta = self.fake_pasta("exit 0")
        verify_network_namespace(pasta)
        Path(pasta).unlink()
        verify_network_namespace(pasta)

    def test_host_addresses_are_every_local_non_loopback_address(self):
        fib = """Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        |-- 127.0.0.1
           /32 host LOCAL
        |-- 127.255.255.255
           /32 link BROADCAST
     |-- 192.168.1.243
        /32 host LOCAL
Local:
  +-- 0.0.0.0/0 3 0 5
     |-- 100.123.118.79
        /32 host LOCAL
     |-- 192.168.1.243
        /32 host LOCAL
"""
        with mock.patch.object(Path, "read_text", return_value=fib):
            self.assertEqual(host_addresses(), ["100.123.118.79", "192.168.1.243"])

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
        self.repository = str(repo)
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
        self.assertEqual(git_metadata_directories(self.cwd, self.home, self.cwd), [])

    def fake_repository(self, root):
        """A minimal .git directory Git would accept as a common dir."""
        (root / ".git" / "objects").mkdir(parents=True)
        (root / ".git" / "refs").mkdir()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        return root

    def link_worktree(self, repo, name="wt"):
        git_dir = repo / ".git" / "worktrees" / name
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "commondir").write_text("../..\n")
        (self.cwd / ".git").write_text(f"gitdir: {git_dir}\n")
        return git_dir

    def test_project_worktree_mounts_common_metadata(self):
        repo = self.fake_repository(self.root / "repo")
        self.link_worktree(repo)
        self.assertEqual(
            git_metadata_directories(self.cwd, self.home, repo), [repo / ".git"]
        )

    def test_invalid_gitfile_and_missing_metadata_fail_closed(self):
        repo = self.fake_repository(self.root / "repo")
        for contents in ("not a gitfile\n", "gitdir: \n", "gitdir: /missing/repo\n"):
            with self.subTest(contents=contents):
                (self.cwd / ".git").write_text(contents)
                with self.assertRaises(ValueError):
                    git_metadata_directories(self.cwd, self.home, repo)

    def test_gitfile_cannot_redirect_to_another_repository(self):
        repo = self.fake_repository(self.root / "repo")
        other = self.fake_repository(self.root / "other")
        # The agent swaps the gitfile for one naming a fake gitdir it created
        # in its own tree, whose commondir names another repository's .git.
        fake = self.cwd / "fake-gitdir"
        fake.mkdir()
        (fake / "HEAD").write_text("ref: refs/heads/main\n")
        (fake / "commondir").write_text(f"{other / '.git'}\n")
        (self.cwd / ".git").write_text(f"gitdir: {fake}\n")
        with self.assertRaisesRegex(ValueError, "not the project's repository"):
            git_metadata_directories(self.cwd, self.home, repo)
        # Pointing straight at another repository's worktree fails the same way.
        (self.cwd / ".git").unlink()
        self.link_worktree(other)
        with self.assertRaisesRegex(ValueError, "not the project's repository"):
            git_metadata_directories(self.cwd, self.home, repo)

    def test_gitdir_must_be_a_worktree_of_the_project(self):
        repo = self.fake_repository(self.root / "repo")
        # The project's own .git is a valid gitdir, but not a linked worktree's.
        (self.cwd / ".git").write_text(f"gitdir: {repo / '.git'}\n")
        with self.assertRaisesRegex(ValueError, "not the project's repository"):
            git_metadata_directories(self.cwd, self.home, repo)

    def test_no_trusted_repository_mounts_nothing(self):
        other = self.fake_repository(self.root / "other")
        self.link_worktree(other)
        self.assertEqual(git_metadata_directories(self.cwd, self.home, None), [])
        # A project that is itself a linked worktree has no .git directory.
        linked = self.root / "linked"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: elsewhere\n")
        self.assertEqual(git_metadata_directories(self.cwd, self.home, linked), [])

    def test_oversized_git_pointer_is_rejected(self):
        repo = self.fake_repository(self.root / "repo")
        (self.cwd / ".git").write_text("gitdir: " + "a" * 8192)
        with self.assertRaisesRegex(ValueError, "Invalid Git metadata pointer"):
            git_metadata_directories(self.cwd, self.home, repo)

    def test_git_metadata_cannot_expose_home(self):
        repo = self.fake_repository(self.root / "repo")
        (self.home / "HEAD").write_text("ref: refs/heads/main\n")
        (self.home / "objects").mkdir()
        (self.home / "refs").mkdir()
        (self.cwd / ".git").write_text(f"gitdir: {self.home}\n")
        with self.assertRaises(ValueError):
            git_metadata_directories(self.cwd, self.home, repo)

    @unittest.skipUnless(shutil.which("git") and shutil.which("bwrap"), "Git/Bubblewrap not installed")
    def test_real_sandbox_worktree_can_commit_without_exposing_main_checkout(self):
        require_sandbox(self)
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
                command = command[:inner_start(command)] + [
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
        require_sandbox(self)
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
        command = command[:inner_start(command)] + ["/usr/bin/python3", "-c", probe]
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

    def run_real(self, script):
        require_sandbox(self)
        command = self.build()
        return command[:inner_start(command)] + ["/usr/bin/python3", "-c", script]

    def test_real_sandbox_cannot_reach_host_services(self):
        # One listener on every host address; the sandbox must reach none of
        # them, and must not be able to lift the blackhole routes.
        listener = socket.create_server(("0.0.0.0", 0))
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        targets = ["127.0.0.1", *host_addresses()]
        probe = r"""
import json, socket, subprocess, sys
port, targets = int(sys.argv[1]), sys.argv[2:]
def reachable(host):
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False
route = subprocess.run(["ip", "route", "del", "blackhole", "100.64.0.0/10"], capture_output=True)
print(json.dumps({"reached": [t for t in targets if reachable(t)], "route": route.returncode}))
"""
        command = self.run_real(probe) + [str(port), *targets]
        result = subprocess.run(command, capture_output=True, text=True, env={}, timeout=30)
        if result.returncode and bubblewrap_unavailable(result.stderr):
            self.skipTest(result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["reached"], [])
        self.assertNotEqual(data["route"], 0)

    def test_real_stopping_the_launcher_kills_the_sandbox(self):
        # Stop it the way the adapters do: pasta alone exits on SIGTERM and
        # leaves Bubblewrap running, so stop_process signals the group.
        probe = r"""
import time
from pathlib import Path
Path("/tmp/started").touch()
time.sleep(2)
Path("/tmp/survived").touch()
"""
        command = self.run_real(probe)

        async def start_and_stop():
            process = await asyncio.create_subprocess_exec(
                *command, env={}, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
            try:
                async with asyncio.timeout(15):
                    while not (self.scratch / "started").exists():
                        if process.returncode is not None:
                            self.fail((await process.stderr.read()).decode())
                        await asyncio.sleep(0.05)
            finally:
                # pasta exits at once; its sandbox must not linger either.
                started = time.monotonic()
                await stop_process(process)
                self.assertLess(time.monotonic() - started, 1.5)

        asyncio.run(start_and_stop())
        time.sleep(3)
        self.assertFalse((self.scratch / "survived").exists())

    def test_real_sandbox_restores_default_sigpipe(self):
        # pasta ignores SIGPIPE; the agent must not inherit that. Read the
        # mask with grep, not Python, which ignores SIGPIPE itself.
        require_sandbox(self)
        command = self.build()
        command = command[:inner_start(command)] + ["/bin/grep", "^SigIgn:", "/proc/self/status"]
        result = subprocess.run(command, capture_output=True, text=True, env={}, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        ignored = int(result.stdout.split()[1], 16)
        self.assertFalse(ignored >> (signal.SIGPIPE - 1) & 1)

class StopProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_reaches_children_the_leader_leaves_behind(self):
        # Like pasta: the spawned leader exits on SIGTERM, but the child it
        # started keeps running unless the whole group is signalled. The
        # child reports its own pid, so it is running before the stop.
        child_script = "import os, time; print(os.getpid(), flush=True); time.sleep(30)"
        process = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", f'trap "exit 0" TERM; /usr/bin/python3 -c "{child_script}" & wait',
            stdout=asyncio.subprocess.PIPE, start_new_session=True,
        )
        child = int(await process.stdout.readline())
        self.addCleanup(self.kill_quietly, child)
        await stop_process(process)
        for _ in range(50):
            if not self.alive(child):
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("child survived stop_process")

    @staticmethod
    def alive(pid):
        # A killed child can linger briefly as a zombie until it is reaped.
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False

    @staticmethod
    def kill_quietly(pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

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
