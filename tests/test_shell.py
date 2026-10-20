from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from agent_ui_server import shell


class ShellSignalTests(unittest.TestCase):
    def test_permission_failure_surfaces(self):
        process = Mock(returncode=None, pid=123)
        with patch.object(shell.os, "killpg", side_effect=PermissionError("signal denied")):
            with self.assertRaisesRegex(PermissionError, "signal denied"):
                shell._signal_group(process, signal.SIGTERM)

    def test_process_exit_race_is_harmless(self):
        process = Mock(returncode=None, pid=123)
        with patch.object(shell.os, "killpg", side_effect=ProcessLookupError):
            shell._signal_group(process, signal.SIGTERM)

    def test_already_exited_shell_group_is_still_signalled(self):
        process = Mock(returncode=0, pid=123)
        with patch.object(shell.os, "killpg") as killpg:
            shell._signal_group(process, signal.SIGTERM)
        killpg.assert_called_once_with(123, signal.SIGTERM)


class ShellTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_kill_group_escalates_after_shell_exit(self):
        process = Mock(returncode=0, pid=123)
        with (
            patch.object(shell.os, "killpg") as killpg,
            patch.object(shell.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            await shell._kill_group(process)
        self.assertEqual(killpg.call_args_list, [
            call(123, signal.SIGTERM), call(123, signal.SIGKILL),
        ])
        sleep.assert_awaited_once_with(shell.KILL_GRACE_SECONDS)

    async def run_inherited_pipe_case(self, *, escaped: bool, parent_exits: bool):
        with tempfile.TemporaryDirectory() as cwd:
            pid_file = Path(cwd) / "child.pid"
            child = (
                "import os, signal, sys, time; "
                "from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('child stdout', flush=True); "
                "print('child stderr', file=sys.stderr, flush=True); "
                f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            parent = (
                "import subprocess, time; from pathlib import Path; "
                f"subprocess.Popen([{sys.executable!r}, '-c', {child!r}], "
                f"start_new_session={escaped!r})\n"
                f"while not Path({str(pid_file)!r}).exists(): time.sleep(0.01)\n"
            )
            if not parent_exits:
                parent += "time.sleep(60)\n"
            command = shlex.join([sys.executable, "-c", parent])
            try:
                with patch.object(shell, "KILL_GRACE_SECONDS", 0.1):
                    result = await asyncio.wait_for(
                        shell.run_command(command, cwd, timeout=0.5), 3,
                    )
                self.assertTrue(result["timed_out"])
                self.assertIn("child stdout", result["stdout"])
                self.assertIn("child stderr", result["stderr"])
                if parent_exits:
                    self.assertEqual(result["exit_code"], 0)
                else:
                    self.assertEqual(result["exit_code"], -signal.SIGTERM)
            finally:
                # Escaped children intentionally survive group signalling.
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    async def test_exited_shell_with_child_ignoring_sigterm(self):
        await self.run_inherited_pipe_case(escaped=False, parent_exits=True)

    async def test_exited_shell_with_escaped_child(self):
        await self.run_inherited_pipe_case(escaped=True, parent_exits=True)

    async def test_live_shell_with_escaped_child(self):
        await self.run_inherited_pipe_case(escaped=True, parent_exits=False)
