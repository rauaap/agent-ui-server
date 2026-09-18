from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_ui_server.agent import ClaudeCodeAdapter
from agent_ui_server.sandbox import claude_sandbox_command
from test_sandbox import bubblewrap_unavailable


class ClaudeSandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.cwd = self.home / "project"
        self.cwd.mkdir(parents=True)
        self.binary = self.home / ".local/share/claude/versions/test"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b"\x7fELFfixture")
        self.binary.chmod(0o755)
        self.launcher = self.home / ".local/bin/claude"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to(self.binary)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home), "UNRELATED_SECRET": "hidden"})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.scratch = Path(self.tmp.name) / "scratch"
        self.scratch.mkdir()
        patch = mock.patch("agent_ui_server.sandbox.prepare_scratch", return_value=self.scratch)
        patch.start()
        self.addCleanup(patch.stop)

    def build(self):
        return claude_sandbox_command(
            [str(self.launcher), "-p", "--permission-prompt-tool", "stdio", "--resume", "resume-id"],
            str(self.cwd),
        )

    @staticmethod
    def env(command):
        return {command[i + 1]: command[i + 2] for i, arg in enumerate(command) if arg == "--setenv"}

    @staticmethod
    def mounts(command, kind):
        return [command[i + 1:i + 3] for i, arg in enumerate(command) if arg == kind]

    def test_native_runtime_and_config_are_narrow_mounts(self):
        with mock.patch("agent_ui_server.sandbox.shutil.which", side_effect=lambda p: "/usr/bin/bwrap" if p == "bwrap" else p):
            command = self.build()
        self.assertEqual(self.mounts(command, "--bind"), [
            [str(self.scratch), "/tmp"], [str(self.home / ".claude")] * 2,
            [str(self.cwd)] * 2,
        ])
        self.assertIn([str(self.binary)] * 2, self.mounts(command, "--ro-bind"))
        self.assertNotIn(str(self.home / ".local"), command)
        self.assertEqual(command[command.index("--") + 1:], [str(self.binary), "-p", "--permission-prompt-tool", "stdio", "--resume", "resume-id"])
        env = self.env(command)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.home / ".claude"))
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        self.assertNotIn("UNRELATED_SECRET", env)
        self.assertIn("--clearenv", command)
        self.assertIn("--die-with-parent", command)

    def test_default_global_config_import_is_private_and_not_repeated(self):
        original = self.home / ".claude.json"
        original.write_text('{"testState": 1}')
        self.build()
        imported = self.home / ".claude/.claude.json"
        self.assertEqual(imported.read_text(), original.read_text())
        self.assertEqual(imported.stat().st_mode & 0o777, 0o600)
        imported.write_text('{"testState": 2}')
        self.build()
        self.assertEqual(json.loads(imported.read_text())["testState"], 2)
        self.assertEqual(json.loads(original.read_text())["testState"], 1)

    def test_legacy_config_and_explicit_profiles_are_not_overwritten(self):
        (self.home / ".claude.json").write_text('{"fromHome": true}')
        config = self.home / ".claude"
        config.mkdir()
        (config / ".config.json").write_text("{}")
        self.build()
        self.assertFalse((config / ".claude.json").exists())
        custom = self.home / "claude-profile"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom)}):
            command = self.build()
        self.assertEqual(self.env(command)["CLAUDE_CONFIG_DIR"], str(custom))
        self.assertFalse((custom / ".claude.json").exists())
        self.assertIn([str(custom)] * 2, self.mounts(command, "--bind"))

    def test_config_cannot_expose_home_or_use_relative_path(self):
        for path in (str(self.home), "relative-profile"):
            with self.subTest(path=path), mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": path}):
                with self.assertRaises(ValueError):
                    self.build()

    def test_npm_launcher_exposes_package_and_node_not_whole_prefix(self):
        package = self.home / "npm/lib/node_modules/@anthropic-ai/claude-code"
        package.mkdir(parents=True)
        (package / "package.json").write_text("{}")
        cli = package / "cli.js"
        cli.write_text("#!/usr/bin/env node\n")
        cli.chmod(0o755)
        self.launcher.unlink()
        self.launcher.symlink_to(cli)
        node = self.launcher.parent / "node"
        node.write_text("node fixture")
        command = self.build()
        mounts = self.mounts(command, "--ro-bind")
        self.assertIn([str(package)] * 2, mounts)
        self.assertIn([str(node), "/opt/agent-ui/node/bin/node"], mounts)
        self.assertNotIn(str(self.home / "npm"), command)
        self.assertTrue(self.env(command)["PATH"].startswith("/opt/agent-ui/node/bin:"))

    @unittest.skipUnless(shutil.which("bwrap"), "Bubblewrap not installed")
    def test_real_config_atomic_writes_credentials_and_home_isolation(self):
        config = self.home / ".claude"
        config.mkdir()
        (config / ".credentials.json").write_text('{"fixture": true}')
        (self.home / "secret").write_text("hidden")
        command = self.build()
        probe = '''
import json, os
from pathlib import Path
config = Path(os.environ['CLAUDE_CONFIG_DIR'])
assert json.loads((config / '.credentials.json').read_text())['fixture']
assert not (Path.home() / 'secret').exists()
assert 'UNRELATED_SECRET' not in os.environ
pending = config / 'pending.json'
pending.write_text('{"updated":true}')
pending.replace(config / '.claude.json')
(config / 'projects').mkdir(exist_ok=True)
(config / 'projects' / 'resume.jsonl').write_text('session fixture')
'''
        command = command[:command.index("--") + 1] + ["/usr/bin/python3", "-c", probe]
        result = subprocess.run(command, env={}, capture_output=True, text=True, timeout=10)
        if result.returncode and bubblewrap_unavailable(result.stderr):
            self.skipTest(result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads((config / ".claude.json").read_text())["updated"])
        self.assertEqual((config / "projects/resume.jsonl").read_text(), "session fixture")


class ClaudeSandboxAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_on_bypass_and_next_turn_rechecks(self):
        adapter = ClaudeCodeAdapter(executable="claude")
        session = {"id": 456, "working_dir": "/project", "agent_session_id": "resume-id"}
        with mock.patch("agent_ui_server.agent.claude_sandbox_command", return_value=["bwrap", "wrapped"]) as wrap, mock.patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec", side_effect=FileNotFoundError("test spawn")
        ) as spawn:
            for enabled in (True, False, True):
                if not enabled or "sandbox" in session:
                    session["sandbox"] = enabled
                wrap.reset_mock()
                events = [event async for event in adapter.start_turn(session, "hello")]
                self.assertEqual(events[0]["type"], "error")
                if enabled:
                    wrap.assert_called_once()
                    argv = wrap.call_args.args[0]
                    self.assertEqual(argv[-2:], ["--resume", "resume-id"])
                    self.assertIn("--permission-prompt-tool", argv)
                    self.assertEqual(spawn.call_args.args, ("bwrap", "wrapped"))
                    self.assertEqual(spawn.call_args.kwargs["env"], {})
                else:
                    wrap.assert_not_called()
                    self.assertEqual(spawn.call_args.args[0], "claude")

    async def test_setup_failure_never_falls_back(self):
        with mock.patch("agent_ui_server.agent.claude_sandbox_command", side_effect=ValueError("unsafe")), mock.patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec"
        ) as spawn:
            events = [event async for event in ClaudeCodeAdapter().start_turn({"id": 1, "working_dir": "/project"}, "go")]
        spawn.assert_not_called()
        self.assertIn("unsafe", events[0]["message"])

    async def test_wrapped_process_stdio_approval_resume_and_stop(self):
        # A protocol fixture replaces the sandbox argv here; actual mount and
        # config checks are separate integration tests requiring namespaces.
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "claude"
            stub.write_text('''#!/usr/bin/python3
import json, sys
prompt = json.loads(sys.stdin.readline())
assert prompt['type'] == 'user'
assert '--resume' in sys.argv and 'resume-id' in sys.argv
print(json.dumps({'type':'control_request','request_id':'approval','request':{
    'subtype':'can_use_tool','tool_name':'Bash','input':{'command':'echo ok'}}}), flush=True)
answer = json.loads(sys.stdin.readline())
assert answer['response']['response']['behavior'] == 'allow'
if prompt['message']['content'] == 'wait':
    import time
    print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'waiting'}]}}), flush=True)
    time.sleep(60)
else:
    print(json.dumps({'type':'result','session_id':'resumed-id'}), flush=True)
''')
            stub.chmod(0o755)
            adapter = ClaudeCodeAdapter(executable=str(stub))
            session = {"id": 789, "working_dir": tmp, "agent_session_id": "resume-id", "sandbox": True}
            with mock.patch("agent_ui_server.agent.claude_sandbox_command", side_effect=lambda command, cwd: command) as wrap:
                async with asyncio.timeout(10):
                    for prompt in ("finish", "wait"):
                        events = []
                        async for event in adapter.start_turn(session, prompt):
                            events.append(event)
                            if event['type'] == 'approval_request':
                                await adapter.send_approval(session, event['request_id'], 'allow')
                            if event.get('text') == 'waiting':
                                await adapter.stop(session)
                        self.assertNotIn(session['id'], adapter.processes)
                        self.assertFalse(adapter.pending_approvals)
                        if prompt == "finish":
                            self.assertIn({"type": "done", "session_id": "resumed-id"}, events)
                    self.assertEqual(wrap.call_count, 2)
