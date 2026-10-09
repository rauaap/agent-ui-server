"""One-shot startup model discovery, without harness SDK dependencies."""
from __future__ import annotations

import asyncio
import itertools
import json
import tempfile
from typing import Any

from .agent import stop_process

DISCOVERY_TIMEOUT = 30


async def discover_models(agent: str, executable: str) -> dict[str, Any]:
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            discover = {"claude-code": _claude_models, "pi": _pi_models}[agent]
            models = await discover(executable)
        return {"models": models, "error": None}
    except TimeoutError:
        return {"models": [], "error": f"Model discovery timed out after {DISCOVERY_TIMEOUT} seconds"}
    except Exception as exc:
        return {"models": [], "error": f"Model discovery failed: {exc}"}


async def _pi_models(executable: str) -> list[dict[str, Any]]:
    # Metadata only over RPC: no prompt, session file, extensions, or project settings.
    with tempfile.TemporaryDirectory(prefix="agent-ui-models-") as cwd:
        process = await asyncio.create_subprocess_exec(
            executable, "--mode", "rpc", "--no-extensions", "--no-session", cwd=cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        stderr_task = asyncio.create_task(process.stderr.read())
        request_ids = itertools.count()

        async def call(command: dict[str, Any]) -> Any:
            request_id = str(next(request_ids))
            process.stdin.write((json.dumps({**command, "id": request_id}) + "\n").encode())
            await process.stdin.drain()
            while line := await process.stdout.readline():
                event = json.loads(line)
                if event.get("type") != "response" or event.get("id") != request_id:
                    continue
                if not event["success"]:
                    raise RuntimeError(event["error"])
                return event.get("data")
            stderr = (await stderr_task).decode(errors="replace").strip()
            raise RuntimeError(stderr or "Pi exited before model discovery")

        try:
            models = (await call({"type": "get_available_models"}))["models"]
            # Match `pi --list-models` order, then reverse: clients preselect the first entry.
            models.sort(key=lambda model: (model["provider"], model["id"]), reverse=True)
            catalog = []
            for model in models:
                # Pi decides which of its levels a model offers; ask rather than
                # re-deriving that from `thinkingLevelMap`.
                await call({"type": "set_model", "provider": model["provider"], "modelId": model["id"]})
                levels = (await call({"type": "get_available_thinking_levels"}))["levels"]
                catalog.append({
                    "id": f"{model['provider']}/{model['id']}",
                    "name": model["id"],
                    "reasoning_levels": levels,
                    "input": model["input"],
                })
            return catalog
        finally:
            await stop_process(process)
            await stderr_task


async def _claude_models(executable: str) -> list[dict[str, Any]]:
    # No prompt, session persistence, tools, project settings, or MCP servers.
    with tempfile.TemporaryDirectory(prefix="agent-ui-models-") as cwd:
        process = await asyncio.create_subprocess_exec(
            executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--no-session-persistence", "--tools", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "user", cwd=cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            process.stdin.write((json.dumps({"type": "control_request", "request_id": "models",
                "request": {"subtype": "initialize", "hooks": None}}) + "\n").encode())
            await process.stdin.drain()
            while line := await process.stdout.readline():
                event = json.loads(line)
                response = event.get("response", {})
                if event.get("type") != "control_response" or response.get("request_id") != "models":
                    continue
                if response.get("subtype") != "success":
                    raise RuntimeError(response.get("error", "Initialization failed"))
                models = response["response"]["models"]
                catalog = []
                seen = set()
                for model in models:
                    if model["value"] == "default":
                        continue
                    # Aliases can move after CLI updates. Pin sessions to the
                    # concrete ID, preserving the CLI's recommendation order.
                    model_id = model["resolvedModel"]
                    if not isinstance(model_id, str) or not model_id:
                        raise ValueError("Claude returned an invalid resolved model ID")
                    if model_id in seen:
                        continue
                    seen.add(model_id)
                    catalog.append({
                        "id": model_id,
                        "name": model["displayName"],
                        # Absent for models without effort control, e.g. Haiku.
                        "reasoning_levels": model.get("supportedEffortLevels", []),
                        "input": ["text", "image"],
                    })
                return catalog
            raise RuntimeError("Claude exited before model discovery")
        finally:
            await stop_process(process)
            await stderr_task


async def discover_catalog(adapters: dict[str, Any]) -> dict[str, Any]:
    entries = await asyncio.gather(*(discover_models(agent, adapter.executable) for agent, adapter in adapters.items()))
    return dict(zip(adapters, entries))
