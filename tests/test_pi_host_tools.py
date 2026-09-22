"""Pi host tool: server authorization, subprocess RPC, and extension startup."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_ui_server.actions import auto_approval_setting
from agent_ui_server.agent import PiAdapter
from agent_ui_server.sandbox import SandboxFilesystem, SandboxMount
from agent_ui_server.session_tools import TOOLS


def fake_sandbox(command, cwd, *, system_prompt=None, **kwargs):
    fs = SandboxFilesystem(Path(cwd), [SandboxMount("--bind", cwd, cwd)])
    return [*command, *(["--append-system-prompt", system_prompt(fs)] if system_prompt else [])]


class PiHostToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = {"id": 7, "working_dir": self.tmp.name, "sandbox": True}
        self.adapter = PiAdapter(
            executable=str(Path(__file__).parent / "fixtures/pi_host_exec.py"), web_extension_path="",
        )
        for patch in (
            mock.patch.dict(os.environ, {"PI_HOST_EXEC": "1"}),
            mock.patch("agent_ui_server.agent.pi_sandbox_command", side_effect=fake_sandbox),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def collect(self, calls=None, behavior="allow", callback=None):
        events = []
        prompt = json.dumps({"calls": calls if calls is not None else [
            {"command": "printf pi-host", "reason": "test"},
        ]})
        async with asyncio.timeout(10):
            async for event in self.adapter.start_turn(self.session, prompt):
                events.append(event)
                if callback:
                    await callback(event)
                elif event["type"] == "approval_request":
                    self.assertIsNone(auto_approval_setting(event["action"]))
                    self.assertEqual(event["action"]["arguments"]["cwd"], self.tmp.name)
                    with self.assertRaises(KeyError):
                        await self.adapter.send_approval({"id": 8}, event["request_id"], "allow")
                    await self.adapter.send_approval(self.session, event["request_id"], behavior,
                                                     message="not allowed" if behavior == "deny" else None)
        self.assertEqual(self.adapter.pending_approvals, {})
        self.assertEqual(self.adapter.host_calls, {})
        return events

    @staticmethod
    def results(events):
        return [json.loads(json.loads(e["text"])["value"])
                for e in events if e["type"] == "output"]

    async def test_approved_command_result_and_denial(self):
        for behavior in ("allow", "deny"):
            proof = Path(self.tmp.name) / "proof"
            proof.unlink(missing_ok=True)
            events = await self.collect([{
                "command": "printf pi-host > proof; pwd", "reason": "testing host access",
            }], behavior)
            self.assertEqual(sum(e["type"] == "approval_request" for e in events), 1)
            result = self.results(events)[0]
            self.assertEqual(result["isError"], behavior == "deny")
            self.assertEqual(proof.exists(), behavior == "allow")
            if behavior == "allow":
                self.assertEqual(proof.read_text(), "pi-host")
                output = json.loads(result["content"][0]["text"])
                self.assertEqual(output["stdout"].strip(), self.tmp.name)
            else:
                self.assertEqual(result["content"][0]["text"], "not allowed")
            self.assertEqual(events[-1]["type"], "done")

    async def test_invalid_arguments_and_disabled_sessions_cannot_execute(self):
        with mock.patch("agent_ui_server.host_tools.run_command", new_callable=mock.AsyncMock) as run:
            events = await self.collect([{"command": "echo nope", "reason": "test", "cwd": "/"}])
            self.assertFalse(any(e["type"] == "approval_request" for e in events))
            self.assertTrue(self.results(events)[0]["isError"])
            for sandbox, enabled in ((False, "1"), (True, "0")):
                self.session["sandbox"] = sandbox
                with mock.patch.dict(os.environ, {"PI_HOST_EXEC": enabled}):
                    events = await self.collect()
                self.assertFalse(any(e["type"] == "approval_request" for e in events))
                self.assertTrue(self.results(events)[0]["isError"])
            run.assert_not_awaited()

    async def test_nonzero_exit_and_parallel_requests(self):
        events = await self.collect([
            {"command": "printf first", "reason": "test"},
            {"command": "printf failure >&2; exit 3", "reason": "test"},
        ])
        self.assertEqual(sum(e["type"] == "approval_request" for e in events), 2)
        results = self.results(events)
        self.assertEqual([r["isError"] for r in results], [False, True])
        self.assertEqual(json.loads(results[1]["content"][0]["text"])["exit_code"], 3)

    async def test_extension_cancel_while_awaiting_approval(self):
        async def cancel(event):
            if event["type"] == "approval_request":
                process = self.adapter.processes[self.session["id"]]
                process.stdin.write(b'{"type":"test_cancel","call_id":"call-0"}\n')
                await process.stdin.drain()

        with mock.patch("agent_ui_server.host_tools.run_command", new_callable=mock.AsyncMock) as run:
            events = await self.collect(callback=cancel)
            response = json.loads(next(e["text"] for e in events if e["type"] == "output"))
            self.assertTrue(response["cancelled"])
            run.assert_not_awaited()

    async def test_stop_exit_and_tool_abort_cancel_running_host_command(self):
        for mode in ("stop", "exit", "tool_abort"):
            started, cancelled = asyncio.Event(), asyncio.Event()

            async def execute(*args):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

            with self.subTest(mode=mode), mock.patch("agent_ui_server.host_tools.run_command", side_effect=execute):
                task = asyncio.create_task(self.collect())
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    process = self.adapter.processes[self.session["id"]]
                    if mode == "stop":
                        await self.adapter.stop(self.session)
                    elif mode == "exit":
                        process.terminate()
                    else:
                        process.stdin.write(b'{"type":"test_cancel","call_id":"call-0"}\n')
                        await process.stdin.drain()
                    await task
                    self.assertTrue(cancelled.is_set())
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_startup_requires_host_capability_handshake(self):
        stub = Path(self.tmp.name) / "old-pi"
        stub.write_text('''#!/usr/bin/env python3
import json, time
print(json.dumps({"type":"extension_ui_request", "method":"notify", "id":"ready",
 "message":json.dumps({"agent-ui":1,"kind":"ready"})}), flush=True)
time.sleep(60)
''')
        stub.chmod(0o755)
        self.adapter.executable = str(stub)
        events = await self.collect()
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("did not register bypass_sandbox", events[0]["message"])

    async def test_launch_gates_tool_and_dynamic_prompt(self):
        for enabled in (False, True):
            for sandbox in (None, False, True):
                with mock.patch.dict(os.environ, {"PI_HOST_EXEC": "1" if enabled else "0"}), mock.patch(
                    "agent_ui_server.agent.asyncio.create_subprocess_exec", side_effect=FileNotFoundError("test")
                ) as spawn:
                    session = {"id": 1, "working_dir": "/project", "agent_session_id": "resume-id"}
                    if sandbox is not None:
                        session["sandbox"] = sandbox
                    _ = [e async for e in self.adapter.start_turn(session, "test")]
                    argv = spawn.call_args.args
                    expected = enabled and sandbox is not False
                    self.assertEqual("--agent-ui-host-exec" in argv, expected)
                    self.assertEqual("--append-system-prompt" in argv, expected)
                    if expected:
                        prompt = argv[argv.index("--append-system-prompt") + 1]
                        self.assertIn('Working directory: "/project"', prompt)
                        self.assertIn("bypass_sandbox(command, reason)", prompt)
                        self.assertNotIn("ToolSearch", prompt)
                        self.assertNotIn("dangerouslyDisableSandbox", prompt)

    @unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Pi/Node not accessible")
    async def test_typescript_tool_bridge_and_error_semantics(self):
        script = Path(__file__).parent / "fixtures/pi_host_extension_test.mjs"
        process = await asyncio.create_subprocess_exec(
            shutil.which("node"), str(script), str(Path(shutil.which("pi")).resolve()),
            PiAdapter.DEFAULT_EXTENSION, json.dumps(TOOLS),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            self.assertEqual(process.returncode, 0, (stdout + stderr).decode())
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    @unittest.skipUnless(shutil.which("pi"), "Pi not accessible")
    async def test_real_pi_extension_registration_without_model_calls(self):
        # Loads the real TypeScript extension and checks flag parsing + runtime
        # tool registration. No user prompt or inference request is sent.
        for enabled in (False, True):
            command = [shutil.which("pi"), "--mode", "rpc", "--no-session", "--no-extensions",
                       "-e", PiAdapter.DEFAULT_EXTENSION, "--agent-ui-session-tools"]
            if enabled:
                command.append("--agent-ui-host-exec")
            process = await asyncio.create_subprocess_exec(
                *command, cwd=self.tmp.name, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stderr = asyncio.create_task(process.stderr.read())
            try:
                async with asyncio.timeout(30):
                    while line := await process.stdout.readline():
                        event = json.loads(line)
                        self.assertNotEqual(event.get("type"), "extension_error", event)
                        envelope = PiAdapter._envelope(event.get("message"))
                        if envelope and envelope.get("kind") == "ready":
                            self.assertEqual(envelope.get("hostTool"), "bypass_sandbox" if enabled else None)
                            self.assertEqual(envelope.get("sessionTools"), [
                                "message_session", "start_session", "read_session",
                            ])
                            break
                    else:
                        self.fail((await stderr).decode())
            finally:
                if process.returncode is None:
                    process.terminate()
                await process.wait()
                await stderr
