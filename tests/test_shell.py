from __future__ import annotations

import signal
import unittest
from unittest.mock import Mock, patch

from agent_ui_server import shell


class ShellSignalTests(unittest.TestCase):
    def test_permission_failure_surfaces(self):
        process = Mock(returncode=None, pid=123)
        with patch.object(shell.os, "getpgid", return_value=123), patch.object(
            shell.os, "killpg", side_effect=PermissionError("signal denied")
        ):
            with self.assertRaisesRegex(PermissionError, "signal denied"):
                shell._signal_group(process, signal.SIGTERM)

    def test_process_exit_race_is_harmless(self):
        process = Mock(returncode=None, pid=123)
        with patch.object(shell.os, "getpgid", side_effect=ProcessLookupError):
            shell._signal_group(process, signal.SIGTERM)

    def test_already_exited_process_is_not_signalled(self):
        process = Mock(returncode=0, pid=123)
        with patch.object(shell.os, "getpgid") as getpgid:
            shell._signal_group(process, signal.SIGTERM)
        getpgid.assert_not_called()
