import unittest
from unittest import mock

from agent_ui_server import agent_registry
from agent_ui_server.agent import PiAdapter
from agent_ui_server.host_tools import HostTools
from agent_ui_server.session_tools import agent_description, session_tool_schemas


class HarnessDescriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_schema_tracks_registry_and_default(self):
        host = HostTools(mock.Mock(), "/project", mock.AsyncMock(),
                         session_call=mock.AsyncMock())
        for ids in (("pi", "claude-code"), ("claude-code",), ("custom", "pi")):
            with self.subTest(ids=ids), mock.patch.object(
                agent_registry, "adapters", dict.fromkeys(ids)
            ):
                result = await host.dispatch({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                })
                tool = next(t for t in result["result"]["tools"] if t["name"] == "start_session")
                expected = f"Agent backend: {', '.join(ids)}. Omit to use {ids[0]} (default)."
                self.assertEqual(tool["inputSchema"]["properties"]["agent"]["description"], expected)
                self.assertEqual(agent_description(), expected)

    async def test_pi_receives_same_dynamic_description(self):
        adapter = PiAdapter()
        adapter.session_operation = mock.AsyncMock()
        with mock.patch.object(agent_registry, "adapters", {"custom": None}), mock.patch(
            "agent_ui_server.agent.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("test"),
        ) as spawn:
            _ = [event async for event in adapter.start_turn(
                {"id": 1, "working_dir": "/project", "sandbox": False}, "hello"
            )]
            argv = spawn.call_args.args
            description = argv[argv.index("--agent-ui-harness-description") + 1]
            tool = next(t for t in session_tool_schemas() if t["name"] == "start_session")
            self.assertEqual(description, tool["inputSchema"]["properties"]["agent"]["description"])
            self.assertEqual(description, "Agent backend: custom. Omit to use custom (default).")
