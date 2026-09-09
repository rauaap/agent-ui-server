import math
import unittest

from pydantic import ValidationError

from agent_ui_server.actions import (
    approval_request_event,
    auto_approval_setting,
    canonical_action,
    other_action,
    tool_use_event,
)
from agent_ui_server.agent import ClaudeCodeAdapter
from agent_ui_server.tool_actions import (
    CLAUDE_TOOL_TRANSLATORS,
    PI_TOOL_TRANSLATORS,
    action_or_other,
)


class CanonicalActionTests(unittest.TestCase):
    def test_all_action_kinds_serialize_without_none(self):
        actions = [
            canonical_action("command", command="printf 'x'", description=None),
            canonical_action("read", path="a", offset=1),
            canonical_action("edit", path="a", edits=[{"old_text": "x", "new_text": ""}]),
            canonical_action("write", path="a", content=""),
            canonical_action("search", mode="content", query="needle"),
            canonical_action("list"),
            canonical_action("web", operation="fetch", url="https://example.com"),
            canonical_action("task", description="delegate"),
            other_action("custom", {"x": 1}),
        ]
        self.assertEqual({action["kind"] for action in actions}, {
            "command", "read", "edit", "write", "search", "list", "web", "task", "other"
        })
        self.assertNotIn("description", actions[0])

    def test_strict_constraints_reject_coercion_and_extra_fields(self):
        invalid = [
            ("read", {"path": "a", "offset": True}),
            ("read", {"path": "a", "limit": 1.5}),
            ("command", {"command": "x", "timeout_ms": math.inf}),
            ("write", {"path": "", "content": "x"}),
            ("list", {"unknown": 1}),
            ("web", {"operation": "fetch", "url": "https://x", "query": "x"}),
        ]
        for kind, fields in invalid:
            with self.subTest(kind=kind, fields=fields), self.assertRaises(ValidationError):
                canonical_action(kind, **fields)

    def test_outer_events_are_canonical(self):
        action = canonical_action("command", command="ls")
        self.assertEqual(tool_use_event("c1", action), {
            "type": "tool_use", "call_id": "c1", "action": action
        })
        approval = approval_request_event("p1", "c1", action, [])
        self.assertEqual(approval["options"], [])
        self.assertNotIn("tool", approval)
        self.assertNotIn("input", approval)
        with self.assertRaises(ValueError):
            tool_use_event("", action)

    def test_auto_approval_is_derived_from_kind(self):
        self.assertEqual(auto_approval_setting({"kind": "command", "command": "x"}), "auto_approve_command")
        self.assertEqual(auto_approval_setting({"kind": "edit", "path": "a", "edits": [{"old_text": "x", "new_text": "y"}]}), "auto_approve_write")
        self.assertEqual(auto_approval_setting({"kind": "write", "path": "a", "content": ""}), "auto_approve_write")
        self.assertIsNone(auto_approval_setting({"kind": "read", "path": "a"}))


class ProviderNormalizationTests(unittest.TestCase):
    def test_claude_and_pi_share_command_and_edit_shapes(self):
        claude_command = action_or_other(
            "Bash", {"command": "ls"}, CLAUDE_TOOL_TRANSLATORS
        )
        pi_command = action_or_other(
            "bash", {"command": "ls"}, PI_TOOL_TRANSLATORS
        )
        self.assertEqual(claude_command, pi_command)

        claude_edit = action_or_other(
            "Edit",
            {"file_path": "a", "old_string": "x", "new_string": "y"},
            CLAUDE_TOOL_TRANSLATORS,
        )
        pi_edit = action_or_other(
            "edit",
            {"path": "a", "edits": [{"oldText": "x", "newText": "y"}]},
            PI_TOOL_TRANSLATORS,
        )
        self.assertEqual(claude_edit, pi_edit)

    def test_malformed_recognized_and_unknown_calls_become_other(self):
        self.assertEqual(
            action_or_other(
                "Read", {"file_path": ""}, CLAUDE_TOOL_TRANSLATORS
            ),
            {"kind": "other", "name": "Read", "arguments": {"file_path": ""}},
        )
        self.assertEqual(
            action_or_other("Deploy", ["bad"], PI_TOOL_TRANSLATORS),
            {"kind": "other", "name": "Deploy", "arguments": {}},
        )

    def test_fallback_does_not_hide_normalizer_bugs(self):
        def broken(_arguments):
            raise TypeError("programming error")

        with self.assertRaisesRegex(TypeError, "programming error"):
            action_or_other("tool", {}, {"tool": broken})

    def test_pi_timeout_conversion_and_legacy_edit(self):
        self.assertEqual(
            action_or_other(
                "bash",
                {"command": "x", "timeout": 2.5},
                PI_TOOL_TRANSLATORS,
            )["timeout_ms"],
            2500.0,
        )
        self.assertEqual(
            action_or_other(
                "edit",
                {"path": "a", "oldText": "x", "newText": "y"},
                PI_TOOL_TRANSLATORS,
            )["edits"],
            [{"old_text": "x", "new_text": "y"}],
        )

    def test_pi_web_tools_match_claude_web_shapes(self):
        """The vendored pi-web-search tools normalize like Claude's web tools.

        A client must not be able to tell which harness searched the web, so
        these assert the shared shape rather than each field in isolation.
        """
        self.assertEqual(
            action_or_other("web_search", {"query": "needle"}, PI_TOOL_TRANSLATORS),
            action_or_other("WebSearch", {"query": "needle"}, CLAUDE_TOOL_TRANSLATORS),
        )
        self.assertEqual(
            action_or_other(
                "url_context",
                {"query": "What is this?", "urls": ["https://example.com"]},
                PI_TOOL_TRANSLATORS,
            ),
            action_or_other(
                "WebFetch",
                {"url": "https://example.com", "prompt": "What is this?"},
                CLAUDE_TOOL_TRANSLATORS,
            ),
        )

    def test_pi_web_search_drops_supplementary_urls(self):
        """`urls` has no home in a canonical search, which forbids `url`."""
        self.assertEqual(
            action_or_other(
                "web_search",
                {"query": "needle", "urls": ["https://a.example", "https://b.example"]},
                PI_TOOL_TRANSLATORS,
            ),
            {"kind": "web", "operation": "search", "query": "needle"},
        )

    def test_pi_url_context_falls_back_rather_than_hiding_urls(self):
        """A multi-URL call renders every URL instead of a misleading first one."""
        arguments = {"query": "Summarize", "urls": ["https://a.example", "https://b.example"]}
        action = action_or_other("url_context", arguments, PI_TOOL_TRANSLATORS)

        self.assertEqual(action["kind"], "other")
        self.assertEqual(action["name"], "url_context")
        self.assertEqual(action["arguments"], arguments)

    def test_pi_web_tools_are_not_auto_approvable(self):
        """`web` has no auto-approval setting; the Pi gate allowlists instead."""
        self.assertIsNone(
            auto_approval_setting(
                action_or_other("web_search", {"query": "x"}, PI_TOOL_TRANSLATORS)
            )
        )

    def test_claude_permission_reuses_session_scoped_cached_action(self):
        adapter = ClaudeCodeAdapter(executable="claude")
        tool = adapter._tool_use_event({
            "id": "c1", "name": "Bash", "input": {"command": "exact command"}
        }, session_id=7)
        approval = adapter._approval_request_event({
            "request_id": "p1",
            "request": {
                "subtype": "can_use_tool",
                "tool_use_id": "c1",
                "tool_name": "Bash",
                "input": {"command": "different permission payload"},
            },
        }, session_id=7)
        other_session = adapter._approval_request_event({
            "request_id": "p2",
            "request": {
                "subtype": "can_use_tool",
                "tool_use_id": "c1",
                "tool_name": "Bash",
                "input": {"command": "different permission payload"},
            },
        }, session_id=8)

        self.assertEqual(approval["action"], tool["action"])
        self.assertEqual(approval["call_id"], tool["call_id"])
        self.assertNotEqual(other_session["action"], tool["action"])
