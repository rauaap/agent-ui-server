"""One-shot startup model discovery, without harness SDK dependencies."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

from .agent import stop_process

DISCOVERY_TIMEOUT = 30


async def discover_models(agent: str, executable: str) -> dict[str, Any]:
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            models = await (_claude_models(executable) if agent == "claude-code" else _pi_models(executable))
        return {"models": models, "error": None}
    except TimeoutError:
        return {"models": [], "error": f"Model discovery timed out after {DISCOVERY_TIMEOUT} seconds"}
    except Exception as exc:
        return {"models": [], "error": f"Model discovery failed: {exc}"}


async def _pi_models(executable: str) -> list[dict[str, str]]:
    process = await asyncio.create_subprocess_exec(
        executable, "--no-extensions", "--list-models",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1"}, start_new_session=True,
    )
    try:
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace").strip() or f"Exit code {process.returncode}")
        lines = stdout.decode().splitlines()
        header = next((i for i, line in enumerate(lines) if line.split()[:2] == ["provider", "model"]), None)
        if header is None:
            if "No models available" in stdout.decode():
                return []
            raise RuntimeError("Unrecognized Pi model listing")
        models = []
        for line in lines[header + 1:]:
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) != 6:
                raise RuntimeError("Unrecognized Pi model row")
            provider, model = fields[:2]
            models.append({"id": f"{provider}/{model}", "name": model})
        # Clients preselect the first entry; reverse Pi's listing order.
        return models[::-1]
    finally:
        await stop_process(process)


async def _claude_models(executable: str) -> list[dict[str, str]]:
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
                    catalog.append({"id": model_id, "name": model["displayName"]})
                return catalog
            raise RuntimeError("Claude exited before model discovery")
        finally:
            await stop_process(process)
            await stderr_task


async def discover_catalog(adapters: dict[str, Any]) -> dict[str, Any]:
    entries = await asyncio.gather(*(discover_models(agent, adapter.executable) for agent, adapter in adapters.items()))
    return dict(zip(adapters, entries))
