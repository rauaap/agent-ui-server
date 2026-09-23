"""Denial explanations must not obscure that the user refused execution."""
import json
import unittest
from unittest import mock

from agent_ui_server.agent import ApprovalDecision
from agent_ui_server.approvals import denial_message
from agent_ui_server.host_tools import execute_host_command
from agent_ui_server.session_tools import execute_session_tool


CASES = [
    (None, "Tool execution was denied by the user."),
    ("", "Tool execution was denied by the user."),
    (" \n\t", "Tool execution was denied by the user."),
    ("  use another approach \n", "Tool execution was denied by the user. Reason: use another approach"),
]


class DenialTests(unittest.IsolatedAsyncioTestCase):
    def test_format(self):
        for reason, expected in CASES:
            with self.subTest(reason=reason):
                self.assertEqual(denial_message(reason), expected)

    async def test_session_denials(self):
        calls = {
            "message_session": {"session_id": 8, "message": "hello"},
            "start_session": {"name": "Review", "project_path": "/project", "message": "hello"},
            "read_session": {"session_id": 8},
        }
        for name, args in calls.items():
            for reason, expected in CASES:
                with self.subTest(name=name, reason=reason):
                    operation = mock.AsyncMock()
                    approve = mock.AsyncMock(return_value=ApprovalDecision("deny", message=reason))
                    result = await execute_session_tool(name, args, 7, operation, approve)
                    self.assertTrue(result["isError"])
                    self.assertEqual(json.loads(result["content"][0]["text"]), expected)
                    operation.assert_not_awaited()

    async def test_host_denials(self):
        for reason, expected in CASES:
            with self.subTest(reason=reason):
                approve = mock.AsyncMock(return_value=ApprovalDecision("deny", message=reason))
                with mock.patch("agent_ui_server.host_tools.run_command", new_callable=mock.AsyncMock) as run:
                    result = await execute_host_command(
                        {"command": "echo hello", "reason": "test"}, "/project", approve,
                    )
                self.assertTrue(result["isError"])
                self.assertEqual(result["content"][0]["text"], expected)
                run.assert_not_awaited()
