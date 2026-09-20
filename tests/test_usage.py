from __future__ import annotations

import base64
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agent_ui_server import usage


def jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


CODEX_TOKEN = jwt({
    "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"},
})


def credential_file(directory: str, name: str, body: dict) -> Path:
    path = Path(directory) / name
    path.write_text(json.dumps(body))
    return path


class ClaudeCodeUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = credential_file(self.directory.name, "creds.json", {
            "claudeAiOauth": {"accessToken": "sk-ant-test"},
        })
        patcher = mock.patch.object(usage, "CLAUDE_CREDENTIALS", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_normalizes_percentages_and_reset_times(self):
        response = {
            "five_hour": {"utilization": 9.0, "resets_at": "2026-09-20T19:50:00.130769+00:00"},
            "seven_day": {"utilization": 11.0, "resets_at": "2026-09-24T03:00:00+00:00"},
        }
        with mock.patch.object(usage, "_get", return_value=response) as get:
            result = usage.claude_code_usage()

        self.assertEqual(result["five_hour"], {"used_percent": 9.0, "reset_at": 1789933800})
        self.assertEqual(result["weekly"], {"used_percent": 11.0, "reset_at": 1790218800})
        self.assertIsNone(result["error"])
        # The token is a bearer credential, never an x-api-key.
        _, headers = get.call_args.args
        self.assertEqual(headers["Authorization"], "Bearer sk-ant-test")

    def test_ignores_the_null_buckets_beside_the_named_windows(self):
        response = {
            "five_hour": {"utilization": 3.0, "resets_at": "2026-09-20T19:50:00+00:00"},
            "seven_day": {"utilization": 4.0, "resets_at": "2026-09-24T03:00:00+00:00"},
            "seven_day_opus": None,
            "nimbus_quill": {"utilization": 99.0, "resets_at": None},
        }
        with mock.patch.object(usage, "_get", return_value=response):
            result = usage.claude_code_usage()

        self.assertEqual(set(result), {"five_hour", "weekly", "error"})
        self.assertEqual(result["five_hour"]["used_percent"], 3.0)

    def test_window_absent_from_the_response_is_null(self):
        with mock.patch.object(usage, "_get", return_value={"seven_day": None}):
            result = usage.claude_code_usage()

        self.assertIsNone(result["five_hour"])
        self.assertIsNone(result["weekly"])


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = credential_file(self.directory.name, "auth.json", {
            "openai-codex": {"access": CODEX_TOKEN},
        })
        patcher = mock.patch.object(usage, "CODEX_CREDENTIALS", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_matches_windows_by_duration_not_position(self):
        # The weekly window arrives as `primary_window` here: keying off the
        # name rather than the duration would swap the two readings.
        response = {"rate_limit": {
            "primary_window": {
                "used_percent": 50, "limit_window_seconds": 604800, "reset_at": 1790415517,
            },
            "secondary_window": {
                "used_percent": 0, "limit_window_seconds": 18000, "reset_at": 1789935228,
            },
        }}
        with mock.patch.object(usage, "_get", return_value=response):
            result = usage.codex_usage()

        self.assertEqual(result["five_hour"], {"used_percent": 0.0, "reset_at": 1789935228})
        self.assertEqual(result["weekly"], {"used_percent": 50.0, "reset_at": 1790415517})

    def test_sends_the_account_id_drawn_from_the_token(self):
        with mock.patch.object(usage, "_get", return_value={"rate_limit": {}}) as get:
            usage.codex_usage()

        _, headers = get.call_args.args
        self.assertEqual(headers["ChatGPT-Account-Id"], "acct-1")
        self.assertEqual(headers["Authorization"], f"Bearer {CODEX_TOKEN}")

    def test_integer_percentages_are_reported_as_floats(self):
        response = {"rate_limit": {"w": {
            "used_percent": 50, "limit_window_seconds": 604800, "reset_at": 1,
        }}}
        with mock.patch.object(usage, "_get", return_value=response):
            result = usage.codex_usage()

        self.assertIsInstance(result["weekly"]["used_percent"], float)

    def test_token_without_an_account_id_is_reported_not_raised(self):
        path = credential_file(self.directory.name, "auth.json", {
            "openai-codex": {"access": jwt({"sub": "user"})},
        })
        with mock.patch.object(usage, "CODEX_CREDENTIALS", path):
            result = usage._read(usage.codex_usage)

        self.assertIn("chatgpt_account_id", result["error"])


class SubscriptionFailureTests(unittest.TestCase):
    def test_missing_credentials_read_as_unauthenticated(self):
        with mock.patch.object(usage, "CLAUDE_CREDENTIALS", Path("/nonexistent/creds.json")):
            result = usage._read(usage.claude_code_usage)

        self.assertEqual(result, {"five_hour": None, "weekly": None,
                                  "error": "not authenticated"})

    def test_http_status_is_surfaced(self):
        error = urllib.error.HTTPError(usage.CLAUDE_USAGE_URL, 401, "Unauthorized", {}, None)
        with mock.patch.object(usage, "claude_code_usage", side_effect=error):
            result = usage._read(usage.claude_code_usage)

        self.assertEqual(result["error"], "HTTP 401")


class CollectUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_unauthenticated_subscription_does_not_hide_the_other(self):
        healthy = {"five_hour": {"used_percent": 1.0, "reset_at": 2},
                   "weekly": {"used_percent": 3.0, "reset_at": 4}, "error": None}
        with mock.patch.object(usage, "claude_code_usage", return_value=healthy), \
             mock.patch.object(usage, "CODEX_CREDENTIALS", Path("/nonexistent/auth.json")):
            result = await usage.collect_usage()

        self.assertEqual(result["claude_code"], healthy)
        self.assertEqual(result["codex"]["error"], "not authenticated")
        self.assertIsNone(result["codex"]["five_hour"])

    async def test_both_subscriptions_are_always_present(self):
        with mock.patch.object(usage, "CLAUDE_CREDENTIALS", Path("/nonexistent/a.json")), \
             mock.patch.object(usage, "CODEX_CREDENTIALS", Path("/nonexistent/b.json")):
            result = await usage.collect_usage()

        self.assertEqual(set(result), {"claude_code", "codex"})


if __name__ == "__main__":
    unittest.main()
