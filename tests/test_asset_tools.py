"""Shared asset link tools execute through both adapters without approval."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_ui_server.agent import ClaudeCodeAdapter, PiAdapter
from agent_ui_server.asset_tools import validate_asset_arguments
from agent_ui_server.session_tools import auto_approval_setting


class AssetToolTests(unittest.IsolatedAsyncioTestCase):
    def test_strict_absolute_arguments(self):
        for args in ({}, {"path": "relative"}, {"path": "~/notes"}, {"path": 42},
                     {"path": "/notes", "session_id": 7}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_asset_arguments(args)
        self.assertEqual(validate_asset_arguments({"path": "/missing/report"}),
                         {"path": "/missing/report"})
        self.assertIsNone(auto_approval_setting({
            "kind": "other", "name": "resolve_asset_link", "arguments": {"path": "/notes"},
        }, lambda _: None))

    async def test_both_transports_ungated_and_errors(self):
        fixture = str(Path(__file__).parent / "fixtures/session_tools.py")
        with tempfile.TemporaryDirectory() as tmp:
            for cls in (ClaudeCodeAdapter, PiAdapter):
                for mode in ("success", "error", "invalid"):
                    with self.subTest(adapter=cls.__name__, mode=mode), mock.patch.dict(
                        os.environ, {"CLAUDE_HOST_EXEC": "0", "PI_HOST_EXEC": "0"},
                    ):
                        operation = mock.AsyncMock(return_value={"url": "/shared-assets/notes/report"})
                        adapter = cls(session_operation=operation, executable=fixture)
                        if isinstance(adapter, PiAdapter):
                            adapter.web_extension_path = ""
                        if mode == "error":
                            operation.side_effect = ValueError("No registered root")
                        args = {"path": "relative" if mode == "invalid" else "/missing/report"}
                        async with asyncio.timeout(5):
                            events = [event async for event in adapter.start_turn(
                                {"id": 7, "working_dir": tmp, "sandbox": False},
                                json.dumps({"name": "resolve_asset_link", "arguments": args}),
                            )]
                        self.assertFalse(any(e["type"] == "approval_request" for e in events), events)
                        self.assertEqual(events[-1]["type"], "done", events)
                        action = next(e["action"] for e in events if e["type"] == "tool_use")
                        self.assertEqual(action, {"kind": "other", "name": "resolve_asset_link", "arguments": args})
                        response = json.loads(next(e["text"] for e in events if e["type"] == "output"))
                        if isinstance(adapter, PiAdapter):
                            result = json.loads(response["value"])
                        elif mode == "invalid":
                            self.assertEqual(response["error"]["code"], -32602)
                            operation.assert_not_awaited()
                            continue
                        else:
                            result = response["result"]
                        self.assertEqual(result["isError"], mode != "success")
                        if mode == "invalid":
                            operation.assert_not_awaited()
                        else:
                            operation.assert_awaited_once_with(7, "resolve_asset_link", args)
                            self.assertEqual(json.loads(result["content"][0]["text"]),
                                             "No registered root" if mode == "error" else
                                             {"url": "/shared-assets/notes/report"})
