"""Shared approved host execution, plus Claude's server-tool MCP pipe transport.

The pipe reader stays live while approval/execution waits in a separate task.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .shell import run_command
from .session_tools import TOOLS, validate_session_arguments
from .sandbox import SandboxFilesystem

SERVER = "agent_ui"
TOOL_NAME = "mcp__agent_ui__bypass_sandbox"
SANDBOX_GUIDANCE = (
    "You run inside an external Bubblewrap sandbox. Bash and its subprocesses inherit "
    "that sandbox; Bash's dangerouslyDisableSandbox flag cannot escape it. "
    f"The tool {TOOL_NAME} is a separate capability: it runs one command outside "
    "Bubblewrap in the server environment, after explicit user approval. "
    "When filesystem visibility, permissions, or container execution are blocked by "
    "the sandbox, use this tool rather than retrying Bash with dangerouslyDisableSandbox. "
    "If its schema is deferred, load it using ToolSearch first. "
    "Do not assume it duplicates Bash's sandbox flag without loading its schema. "
    "A missing file or permission error inside the sandbox does not establish that "
    "the file is absent or inaccessible in the server environment. When the user "
    "reports a file exists but you cannot see it, check your access boundary before "
    "drawing conclusions about their filesystem. If the tool is unavailable or "
    "approval is denied, report that limitation."
)
TOOL = {
    "name": "bypass_sandbox",
    "description": (
        "Run one shell command outside the agent's Bubblewrap sandbox with explicit user approval. "
        "This does not disable sandboxing for the session. "
        "Use when sandbox restrictions block execution, such as running Podman. "
        "Claude's dangerouslyDisableSandbox flag cannot bypass Bubblewrap. "
        "Runs in the session working directory, with the server's environment. "
        "Commands are non-interactive and subject to a server timeout."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["command", "reason"],
        "additionalProperties": False,
    },
}


def sandbox_guidance(filesystem: SandboxFilesystem) -> str:
    return filesystem.describe() + "\n\n" + SANDBOX_GUIDANCE


def pi_sandbox_guidance(filesystem: SandboxFilesystem) -> str:
    return filesystem.describe() + "\n\n" + (
        "You run inside an external Bubblewrap sandbox. Normal tools and subprocesses "
        "inherit this restricted view. When filesystem visibility, permissions, or "
        "container execution are blocked, use bypass_sandbox(command, reason) to run "
        "one command in the server environment after explicit user approval. "
        "A missing file in this view does not prove it is missing on the server. "
        "Check your access boundary before drawing conclusions about the user's "
        "filesystem. If approval is denied, report that limitation."
    )


def validate_host_arguments(args: Any) -> dict[str, str]:
    if (not isinstance(args, dict) or set(args) != {"command", "reason"}
            or any(not isinstance(v, str) or not v.strip() for v in args.values())):
        raise ValueError("Expected non-empty command and reason strings only")
    return dict(args)


async def execute_host_command(
    args: dict[str, str], cwd: str, approve: Callable[[dict[str, str]], Awaitable[Any]],
) -> dict[str, Any]:
    # Neither provider's native tool gate can authorize host execution.
    args = validate_host_arguments(args)
    decision = await approve(dict(args))
    if decision.behavior != "allow":
        return {"isError": True, "content": [{
            "type": "text", "text": decision.message or "Host execution denied by user",
        }]}
    output = await run_command(args["command"], cwd)
    return {
        "isError": output["exit_code"] != 0 or output["timed_out"],
        "content": [{"type": "text", "text": json.dumps(output)}],
    }


class HostTools:
    def __init__(self, process: Any, cwd: str,
                 approve: Callable[[dict[str, str]], Awaitable[Any]], *,
                 host_enabled: bool = True,
                 session_call: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None) -> None:
        self.process = process
        self.cwd = cwd
        self.approve = approve
        self.host_enabled = host_enabled
        self.session_call = session_call
        self.tool_names = ({"bypass_sandbox"} if host_enabled else set()) | (
            {tool["name"] for tool in TOOLS} if session_call else set()
        )
        self.events: asyncio.Queue[bytes | dict[str, Any]] = asyncio.Queue()
        self.calls: dict[str, asyncio.Task[None]] = {}
        self.execution_lock = asyncio.Lock()
        self.reader: asyncio.Task[None] | None = None
        self.init_id = "host_init_" + uuid.uuid4().hex
        self.initialized: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def write(self, payload: dict[str, Any]) -> None:
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        await self.process.stdin.drain()

    async def start(self) -> None:
        self.reader = asyncio.create_task(self._read())
        await self.write({
            "type": "control_request", "request_id": self.init_id,
            "request": {"subtype": "initialize", "hooks": None},
        })
        await asyncio.wait_for(self.initialized, 30)

    async def close(self) -> None:
        tasks = [*self.calls.values()]
        if self.reader is not None:
            tasks.append(self.reader)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.calls.clear()
        if not self.initialized.done():
            self.initialized.cancel()

    async def _read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    await self.events.put(line)
                    continue
                if not isinstance(event, dict):
                    await self.events.put(line)
                    continue
                response = event.get("response", {})
                if (event.get("type") == "control_response"
                        and response.get("request_id") == self.init_id):
                    if not self.initialized.done():
                        if response.get("subtype") == "success":
                            self.initialized.set_result(None)
                        else:
                            self.initialized.set_exception(RuntimeError(
                                f"Claude host-tool initialization failed: {response}"))
                    continue
                request = event.get("request", {})
                request_id = event.get("request_id")
                if event.get("type") == "control_cancel_request":
                    task = self.calls.get(request_id)
                    if task is not None:
                        task.cancel()
                    continue
                if (event.get("type") == "control_request"
                        and request.get("subtype") == "mcp_message"):
                    if not isinstance(request_id, str) or request_id in self.calls:
                        continue
                    task = asyncio.create_task(self._respond(request_id, request))
                    self.calls[request_id] = task
                    task.add_done_callback(lambda _, key=request_id: self.calls.pop(key, None))
                else:
                    await self.events.put(line)
        except Exception as exc:
            await self.events.put({"type": "error", "message": str(exc)})
        finally:
            if not self.initialized.done():
                self.initialized.set_exception(RuntimeError("Claude exited before host-tool initialization"))
            await self.events.put(b"")

    async def _respond(self, request_id: str, request: dict[str, Any]) -> None:
        try:
            if request.get("server_name") != SERVER:
                raise ValueError("Unknown SDK MCP server")
            message = request.get("message")
            if isinstance(message, dict) and message.get("method") == "tools/call":
                # One approval/execution at a time; discovery and cancellation stay live.
                async with self.execution_lock:
                    result = await self.dispatch(message)
            else:
                result = await self.dispatch(message)
            response = {
                "subtype": "success", "request_id": request_id,
                "response": {"mcp_response": result or {"jsonrpc": "2.0", "result": {}}},
            }
        except Exception as exc:
            response = {"subtype": "error", "request_id": request_id, "error": str(exc)}
        try:
            await self.write({"type": "control_response", "response": response})
        except Exception as exc:
            await self.events.put({"type": "error", "message": str(exc)})

    async def dispatch(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self.error(None, -32600, "Invalid JSON-RPC request")
        if "id" not in message:
            return None  # MCP notifications; outer control request is still acknowledged.
        mid = message["id"]
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER, "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": ([TOOL] if self.host_enabled else []) + (TOOLS if self.session_call else [])}
        elif method == "tools/call":
            params = message.get("params")
            if (not isinstance(params, dict) or not isinstance(params.get("name"), str)
                    or params["name"] not in self.tool_names):
                return self.error(mid, -32602, "Unknown tool")
            name = params["name"]
            try:
                args = (validate_host_arguments(params.get("arguments")) if name == "bypass_sandbox"
                        else validate_session_arguments(name, params.get("arguments")))
            except ValueError as exc:
                return self.error(mid, -32602, str(exc))
            if name == "bypass_sandbox":
                result = await execute_host_command(args, self.cwd, self.approve)
            else:
                assert self.session_call is not None
                result = await self.session_call(name, args)
        else:
            return self.error(mid, -32601, "Unknown method")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def error(mid: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
