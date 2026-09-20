"""Subscription usage for the plans backing this server's agents.

Two subscriptions, two unrelated payloads, one shape on the wire. A
subscription is not a harness: the Codex plan happens to be reached through
Pi's credential file here, but the same plan can back any number of clients,
so nothing below is named for the harness that stored the token.

Tokens are read off disk and sent as-is. Refreshing them is the user's job —
Claude Code rewrites its own credential file on the 401-refresh path, so a
second refresher here would race it for the same bytes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CODEX_CREDENTIALS = Path.home() / ".pi" / "agent" / "auth.json"

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

REQUEST_TIMEOUT_SECONDS = 5.0

# Codex reports windows positionally, so they are matched on duration instead
# of on the `primary`/`secondary` names that carry no guarantee of order.
FIVE_HOUR_SECONDS = 18_000
WEEK_SECONDS = 604_800


def _window(used_percent: Any, reset_at: int | None) -> dict[str, Any] | None:
    if used_percent is None:
        return None
    return {"used_percent": float(used_percent), "reset_at": reset_at}


def _unavailable(error: str) -> dict[str, Any]:
    """A subscription that could not be read, in the shape of one that could.

    Both keys are always present so a client can render one subscription
    while the other is unauthenticated or unreachable.
    """
    return {"five_hour": None, "weekly": None, "error": error}


def _get(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def _unix(timestamp: Any) -> int | None:
    """Anthropic's ISO-8601 reset time as the Unix seconds Codex already uses."""
    if not isinstance(timestamp, str):
        return None
    try:
        return int(datetime.fromisoformat(timestamp).timestamp())
    except ValueError:
        return None


def _claude_window(bucket: Any) -> dict[str, Any] | None:
    if not isinstance(bucket, dict):
        return None
    return _window(bucket.get("utilization"), _unix(bucket.get("resets_at")))


def claude_code_usage() -> dict[str, Any]:
    credentials = json.loads(CLAUDE_CREDENTIALS.read_text())
    token = credentials["claudeAiOauth"]["accessToken"]
    payload = _get(CLAUDE_USAGE_URL, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    # The response also carries a dozen null buckets for limit types this
    # account has none of; only the two named windows are of interest.
    return {
        "five_hour": _claude_window(payload.get("five_hour")),
        "weekly": _claude_window(payload.get("seven_day")),
        "error": None,
    }


def _account_id(token: str) -> str:
    """The ChatGPT account id the usage endpoint requires, from the JWT itself."""
    segments = token.split(".")
    if len(segments) < 2:
        raise ValueError("Codex credential is not a JWT")
    padded = segments[1] + "=" * (-len(segments[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    account = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    if not account:
        raise ValueError("Codex credential carries no chatgpt_account_id")
    return account


def codex_usage() -> dict[str, Any]:
    credentials = json.loads(CODEX_CREDENTIALS.read_text())
    token = credentials["openai-codex"]["access"]
    payload = _get(CODEX_USAGE_URL, {
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-Id": _account_id(token),
        "Accept": "application/json",
    })

    five_hour = weekly = None
    rate_limit = payload.get("rate_limit")
    for window in (rate_limit or {}).values():
        if not isinstance(window, dict):
            continue
        span = window.get("limit_window_seconds")
        if span == FIVE_HOUR_SECONDS:
            five_hour = _window(window.get("used_percent"), window.get("reset_at"))
        elif span == WEEK_SECONDS:
            weekly = _window(window.get("used_percent"), window.get("reset_at"))
    return {"five_hour": five_hour, "weekly": weekly, "error": None}


def _read(fetch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one subscription's fetch, turning every failure into a reportable one.

    A subscription the user has never authenticated is the ordinary case, not
    an error worth failing the whole response over.
    """
    try:
        return fetch()
    except FileNotFoundError:
        return _unavailable("not authenticated")
    except urllib.error.HTTPError as exc:
        return _unavailable(f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _unavailable(f"unreachable: {exc}")
    except (KeyError, ValueError, TypeError) as exc:
        return _unavailable(f"unreadable response: {exc}")


async def collect_usage() -> dict[str, Any]:
    """Both subscriptions, fetched concurrently off the event loop."""
    claude_code, codex = await asyncio.gather(
        asyncio.to_thread(_read, claude_code_usage),
        asyncio.to_thread(_read, codex_usage),
    )
    return {"claude_code": claude_code, "codex": codex}
