"""Claude's blanket ask policy and adapter-side low-risk exemptions."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from agent_ui_server.agent import ClaudeCodeAdapter


class ClaudePermissionTests(unittest.IsolatedAsyncioTestCase):
    def request(self, tool, event_type="control_request"):
        return {
            "type": event_type,
            "request_id": "permission-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": tool,
                "tool_use_id": "call-1",
                "input": {"command": "ls", "file_path": "/project/example"},
            },
        }

    def process(self):
        return SimpleNamespace(stdin=SimpleNamespace(
            write=mock.Mock(), drain=mock.AsyncMock(),
        ))

    async def test_invocation_requires_all_tool_permissions(self):
        for sandbox in (False, True):
            with self.subTest(sandbox=sandbox), mock.patch(
                "agent_ui_server.agent.claude_sandbox_command",
                side_effect=lambda command, *args, **kwargs: command,
            ), mock.patch(
                "agent_ui_server.agent.asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("test"),
            ) as spawn:
                adapter = ClaudeCodeAdapter()
                _ = [event async for event in adapter.start_turn(
                    {"id": 1, "working_dir": "/project", "sandbox": sandbox}, "hello"
                )]
                argv = spawn.call_args.args
                settings = json.loads(argv[argv.index("--settings") + 1])
                self.assertEqual(settings["permissions"], {"ask": ["*"]})
                self.assertFalse(settings["sandbox"]["autoAllowBashIfSandboxed"])
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")

    async def test_reads_discovery_and_bookkeeping_are_allowed_without_ui_events(self):
        for event_type in ("control_request", "sdk_control_request"):
            for tool in (
                "Read", "Glob", "Grep", "WebSearch", "WebFetch", "LSP",
                "ToolSearch", "ListMcpResourcesTool", "ReadMcpResourceTool",
                "WaitForMcpServers", "ListAgents", "CronList",
                "TaskGet", "TaskList", "TaskOutput", "TaskCreate", "TaskUpdate",
                "TodoWrite", "EnterPlanMode", "ReportFindings",
            ):
                with self.subTest(tool=tool, event_type=event_type):
                    adapter = ClaudeCodeAdapter()
                    process = self.process()
                    request = self.request(tool, event_type)
                    events = [event async for event in adapter._events_from_json(1, process, request)]
                    self.assertEqual(events, [])
                    response = json.loads(process.stdin.write.call_args.args[0])["response"]
                    self.assertEqual(response["request_id"], "permission-1")
                    self.assertEqual(response["response"], {
                        "behavior": "allow", "updatedInput": request["request"]["input"],
                    })
                    process.stdin.drain.assert_awaited_once()
                    self.assertEqual(adapter.pending_approvals, {})
                    self.assertEqual(adapter.pending_sessions, {})

    async def test_commands_writes_and_unknown_tools_require_approval(self):
        for tool in (
            "Bash", "PowerShell", "Monitor", "Write", "Edit", "NotebookEdit",
            "Agent", "Task", "TaskStop", "Skill", "Workflow",
            "EnterWorktree", "ExitWorktree", "ExitPlanMode",
            "CronCreate", "CronDelete", "ScheduleWakeup", "RemoteTrigger",
            "SendMessage", "SendUserFile", "PushNotification", "Artifact",
            "ShareOnboardingGuide", "FutureTool", "mcp__server__read",
            "TaskFuture", "ReadFuture",
        ):
            with self.subTest(tool=tool):
                adapter = ClaudeCodeAdapter()
                process = self.process()
                stream = adapter._events_from_json(1, process, self.request(tool))
                event = await anext(stream)
                self.assertEqual(event["type"], "approval_request")
                process.stdin.write.assert_not_called()
                await adapter.send_approval({"id": 1}, "permission-1", "deny")
                self.assertEqual([event async for event in stream], [])
                response = json.loads(process.stdin.write.call_args.args[0])
                self.assertEqual(response["response"]["response"]["behavior"], "deny")
                self.assertEqual(adapter.pending_approvals, {})

    async def test_tool_discovery_does_not_approve_discovered_tool(self):
        adapter = ClaudeCodeAdapter()
        process = self.process()
        self.assertEqual([event async for event in adapter._events_from_json(
            1, process, self.request("ToolSearch")
        )], [])
        process.stdin.write.reset_mock()
        stream = adapter._events_from_json(1, process, self.request("mcp__server__write"))
        self.assertEqual((await anext(stream))["type"], "approval_request")
        process.stdin.write.assert_not_called()
        await adapter.send_approval({"id": 1}, "permission-1", "deny")
        self.assertEqual([event async for event in stream], [])

    async def test_read_response_failure_surfaces_error(self):
        adapter = ClaudeCodeAdapter()
        events = [event async for event in adapter._events_from_json(
            1, SimpleNamespace(stdin=None), self.request("Read")
        )]
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(adapter.pending_approvals, {})
