from __future__ import annotations

import abc
import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


AgentEvent = dict[str, Any]


class AgentAdapter(abc.ABC):
    @abc.abstractmethod
    async def start_turn(
        self,
        session: dict[str, Any],
        prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError

    @abc.abstractmethod
    async def send_approval(
        self,
        session: dict[str, Any],
        request_id: str,
        behavior: str,
    ) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def stop(self, session: dict[str, Any]) -> None:
        raise NotImplementedError


class ClaudeCodeAdapter(AgentAdapter):
    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or os.environ.get("CLAUDE_BIN", "claude")
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.pending_approvals: dict[str, asyncio.Future[str]] = {}
        self.pending_sessions: dict[str, str] = {}

    async def start_turn(
        self,
        session: dict[str, Any],
        prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        session_id = session["id"]
        existing = self.processes.get(session_id)
        if existing and existing.returncode is None:
            raise RuntimeError("Session already has a running process")

        command = [
            self.executable,
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--permission-prompt-tool",
            "stdio",
            "--permission-mode",
            "default",
            "--verbose",
        ]
        if session.get("agent_session_id"):
            command.extend(["--resume", session["agent_session_id"]])

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=session["working_dir"],
                env=self._build_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            yield {"type": "error", "message": f"Unable to start Claude Code: {exc}"}
            return
        except NotADirectoryError as exc:
            yield {"type": "error", "message": f"Invalid working directory: {exc}"}
            return

        self.processes[session_id] = process
        stderr_task = asyncio.create_task(self._collect_stderr(process.stderr))
        result_seen = False

        try:
            await self._write_user_message(process, prompt)
            assert process.stdout is not None
            while True:
                raw_line = await process.stdout.readline()
                if not raw_line:
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    yield {"type": "output", "text": line}
                    continue

                async for agent_event in self._events_from_json(
                    session_id,
                    process,
                    event,
                ):
                    if agent_event["type"] == "done":
                        result_seen = True
                        self._close_stdin(process)
                    yield agent_event

            returncode = await process.wait()
            stderr = await stderr_task
            if returncode != 0:
                detail = stderr.strip() or f"Claude Code exited with {returncode}"
                yield {"type": "error", "message": detail}
            elif not result_seen:
                yield {
                    "type": "done",
                    "session_id": session.get("agent_session_id"),
                }
        finally:
            self.processes.pop(session_id, None)
            await self._clear_session_approvals(session_id, "Session ended")
            if not stderr_task.done():
                stderr_task.cancel()

    async def send_approval(
        self,
        session: dict[str, Any],
        request_id: str,
        behavior: str,
    ) -> None:
        if behavior not in {"allow", "deny"}:
            raise ValueError("Approval behavior must be 'allow' or 'deny'")

        future = self.pending_approvals.get(request_id)
        if future is None or self.pending_sessions.get(request_id) != session["id"]:
            raise KeyError(f"Unknown approval request: {request_id}")

        if not future.done():
            future.set_result(behavior)

    async def stop(self, session: dict[str, Any]) -> None:
        session_id = session["id"]
        await self._clear_session_approvals(session_id, "Session stopped")

        process = self.processes.get(session_id)
        if process is None:
            return

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        self.processes.pop(session_id, None)

    async def _events_from_json(
        self,
        session_id: str,
        process: asyncio.subprocess.Process,
        event: dict[str, Any],
    ) -> AsyncIterator[AgentEvent]:
        event_type = event.get("type")

        if event_type == "assistant":
            for output_event in self._assistant_events(event):
                yield output_event
            return

        if event_type == "tool_use":
            yield self._tool_use_event(event)
            return

        if event_type in {"control_request", "sdk_control_request"}:
            request = self._approval_request_event(event)
            if request is None:
                return

            request_id = request["request_id"]
            tool_input = request.get("input") or {}
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            self.pending_approvals[request_id] = future
            self.pending_sessions[request_id] = session_id

            yield request

            try:
                behavior = await future
                await self._write_approval_response(
                    process, request_id, behavior, tool_input
                )
            except Exception as exc:
                yield {"type": "error", "message": str(exc)}
            finally:
                self.pending_approvals.pop(request_id, None)
                self.pending_sessions.pop(request_id, None)
            return

        if event_type == "result":
            yield {
                "type": "done",
                "session_id": self._extract_session_id(event),
            }
            return

        if event_type == "error":
            yield {"type": "error", "message": self._error_message(event)}

    def _assistant_events(self, event: dict[str, Any]) -> list[AgentEvent]:
        message = event.get("message")
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = event.get("content")

        if isinstance(content, str):
            return [{"type": "output", "text": content}] if content else []

        events: list[AgentEvent] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and block.get("text"):
                    events.append({"type": "output", "text": block["text"]})
                elif block_type in {"tool_use", "server_tool_use"}:
                    events.append(self._tool_use_event(block))

        if not events and event.get("text"):
            events.append({"type": "output", "text": event["text"]})

        return events

    def _tool_use_event(self, event: dict[str, Any]) -> AgentEvent:
        nested = event.get("tool_use") if isinstance(event.get("tool_use"), dict) else {}
        source = nested or event
        return {
            "type": "tool_use",
            "tool": (
                source.get("tool")
                or source.get("tool_name")
                or source.get("name")
                or "tool"
            ),
            "input": source.get("input") or source.get("arguments") or {},
        }

    def _approval_request_event(self, event: dict[str, Any]) -> AgentEvent | None:
        request = event.get("request")
        if not isinstance(request, dict):
            request = event.get("control_request")
        if not isinstance(request, dict):
            request = event

        subtype = event.get("subtype") or request.get("subtype")
        if subtype and subtype not in {"can_use_tool", "permission"}:
            return None

        request_id = (
            event.get("request_id")
            or request.get("request_id")
            or request.get("id")
            or f"perm_{uuid.uuid4().hex}"
        )
        tool = (
            event.get("tool")
            or event.get("tool_name")
            or request.get("tool")
            or request.get("tool_name")
            or request.get("name")
            or "tool"
        )
        tool_input = (
            event.get("input")
            or event.get("tool_input")
            or request.get("input")
            or request.get("tool_input")
            or {}
        )

        return {
            "type": "approval_request",
            "request_id": request_id,
            "tool": tool,
            "input": tool_input,
        }

    def _extract_session_id(self, event: dict[str, Any]) -> str | None:
        if event.get("session_id"):
            return event["session_id"]
        for key in ("result", "metadata", "message"):
            nested = event.get(key)
            if isinstance(nested, dict) and nested.get("session_id"):
                return nested["session_id"]
        return None

    def _error_message(self, event: dict[str, Any]) -> str:
        for key in ("message", "error"):
            value = event.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and value.get("message"):
                return str(value["message"])
        return json.dumps(event, ensure_ascii=False)

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("SHELL", "/bin/bash")
        return env

    async def _write_user_message(
        self,
        process: asyncio.subprocess.Process,
        prompt: str,
    ) -> None:
        if process.stdin is None:
            raise RuntimeError("Claude Code stdin is unavailable")

        payload = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await process.stdin.drain()

    def _close_stdin(self, process: asyncio.subprocess.Process) -> None:
        if process.stdin is None or process.stdin.is_closing():
            return
        process.stdin.close()

    async def _write_approval_response(
        self,
        process: asyncio.subprocess.Process,
        request_id: str,
        behavior: str,
        tool_input: dict[str, Any],
    ) -> None:
        if process.stdin is None:
            raise RuntimeError("Claude Code stdin is unavailable")

        if behavior == "allow":
            decision: dict[str, Any] = {
                "behavior": "allow",
                "updatedInput": tool_input or {},
            }
        else:
            decision = {"behavior": "deny", "message": "Denied by user"}

        payload = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": decision,
            },
        }
        process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _clear_session_approvals(self, session_id: str, reason: str) -> None:
        request_ids = [
            request_id
            for request_id, pending_session_id in self.pending_sessions.items()
            if pending_session_id == session_id
        ]
        for request_id in request_ids:
            future = self.pending_approvals.get(request_id)
            if future and not future.done():
                future.set_exception(RuntimeError(reason))
            self.pending_approvals.pop(request_id, None)
            self.pending_sessions.pop(request_id, None)

    async def _collect_stderr(
        self,
        stream: asyncio.StreamReader | None,
    ) -> str:
        if stream is None:
            return ""
        chunks: list[str] = []
        while True:
            raw_line = await stream.readline()
            if not raw_line:
                break
            chunks.append(raw_line.decode("utf-8", errors="replace"))
        return "".join(chunks)


class OpenCodeAdapter(AgentAdapter):
    """Adapter for OpenCode over ACP (Agent Client Protocol).

    ACP is JSON-RPC 2.0 over stdio, and bidirectional: we call the agent
    (`initialize`, `session/new` or `session/load`, `session/prompt`) and the
    agent calls back to us (`session/request_permission`, `fs/*`). Like
    ClaudeCodeAdapter, each turn is one short-lived subprocess: spawn
    `opencode acp`, run a single `session/prompt`, stream `session/update`
    notifications, answer permission callbacks, and exit when the prompt
    completes. Continuity comes from `session/load` with the session id captured
    on the first turn (stored as `agent_session_id`, passed back via `--resume`'s
    moral equivalent).

    OpenCode only emits `session/request_permission` for tools configured to
    "ask"; out of the box most tools default to "allow". The adapter therefore
    points the child at a permission config via `OPENCODE_CONFIG` (a bundled
    default that gates the mutating/external tools), unless the operator has
    already set `OPENCODE_CONFIG` themselves.
    """

    PROTOCOL_VERSION = 1
    # Tools that should prompt for approval; everything else stays "allow".
    DEFAULT_CONFIG = str(Path(__file__).resolve().parent / "opencode_permissions.json")

    def __init__(
        self,
        executable: str | None = None,
        config_path: str | None = None,
    ) -> None:
        self.executable = executable or os.environ.get("OPENCODE_BIN", "opencode")
        self.config_path = (
            config_path or os.environ.get("OPENCODE_CONFIG") or self.DEFAULT_CONFIG
        )
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.pending_approvals: dict[str, asyncio.Future[str]] = {}
        self.pending_sessions: dict[str, str] = {}

    async def start_turn(
        self,
        session: dict[str, Any],
        prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        session_id = session["id"]
        existing = self.processes.get(session_id)
        if existing and existing.returncode is None:
            raise RuntimeError("Session already has a running process")

        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "acp",
                cwd=session["working_dir"],
                env=self._build_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            yield {"type": "error", "message": f"Unable to start OpenCode: {exc}"}
            return
        except NotADirectoryError as exc:
            yield {"type": "error", "message": f"Invalid working directory: {exc}"}
            return

        self.processes[session_id] = process
        stderr_task = asyncio.create_task(self._collect_stderr(process.stderr))

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        pending_rpc: dict[int, asyncio.Future[Any]] = {}
        next_id = 1
        prompt_id: int | None = None
        current_sid: str | None = session.get("agent_session_id")
        text_buf: list[str] = []
        emitted_tools: set[str] = set()
        tool_titles: dict[str, str] = {}
        # On resume, `session/load` re-streams the whole prior conversation as
        # session/update notifications before it responds. Stay silent until the
        # live turn (set True right before session/prompt) so we don't re-emit
        # history on every turn.
        capturing = False

        assert process.stdin is not None
        assert process.stdout is not None

        async def write_msg(obj: dict[str, Any]) -> None:
            process.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            await process.stdin.drain()

        def alloc_id() -> int:
            nonlocal next_id
            mid = next_id
            next_id += 1
            return mid

        async def send_request(method: str, params: dict[str, Any]) -> Any:
            mid = alloc_id()
            future: asyncio.Future[Any] = loop.create_future()
            pending_rpc[mid] = future
            await write_msg(
                {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
            )
            return await future

        async def respond(
            mid: Any,
            result: dict[str, Any] | None = None,
            error: dict[str, Any] | None = None,
        ) -> None:
            message: dict[str, Any] = {"jsonrpc": "2.0", "id": mid}
            if error is not None:
                message["error"] = error
            else:
                message["result"] = result if result is not None else {}
            await write_msg(message)

        async def flush_text() -> None:
            if not text_buf:
                return
            text = "".join(text_buf)
            text_buf.clear()
            if text.strip():
                await queue.put({"type": "output", "text": text})

        async def handle_agent_request(message: dict[str, Any]) -> None:
            method = message.get("method")
            mid = message.get("id")
            params = message.get("params") or {}

            if method == "session/request_permission":
                await flush_text()
                tool_call = params.get("toolCall") or {}
                options = params.get("options") or []
                request_id = f"perm_{uuid.uuid4().hex}"
                future: asyncio.Future[str] = loop.create_future()
                self.pending_approvals[request_id] = future
                self.pending_sessions[request_id] = session_id
                await queue.put(
                    {
                        "type": "approval_request",
                        "request_id": request_id,
                        "tool": tool_call.get("title") or "tool",
                        "input": tool_call.get("rawInput") or {},
                    }
                )
                try:
                    behavior = await future
                    option_id = self._select_option(options, behavior)
                    await respond(
                        mid,
                        {"outcome": {"outcome": "selected", "optionId": option_id}},
                    )
                except Exception:
                    await respond(mid, {"outcome": {"outcome": "cancelled"}})
                finally:
                    self.pending_approvals.pop(request_id, None)
                    self.pending_sessions.pop(request_id, None)
                return

            if method == "fs/read_text_file":
                try:
                    content = Path(params["path"]).read_text(encoding="utf-8")
                    await respond(mid, {"content": content})
                except Exception as exc:
                    await respond(mid, error={"code": -32603, "message": str(exc)})
                return

            if method == "fs/write_text_file":
                try:
                    path = Path(params["path"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(params.get("content", ""), encoding="utf-8")
                    await respond(mid, {})
                except Exception as exc:
                    await respond(mid, error={"code": -32603, "message": str(exc)})
                return

            await respond(
                mid, error={"code": -32601, "message": f"Unsupported method: {method}"}
            )

        async def reader() -> None:
            try:
                while True:
                    raw_line = await process.stdout.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        await queue.put({"type": "output", "text": line})
                        continue

                    if "method" in message and "id" in message:
                        await handle_agent_request(message)
                    elif "method" in message:
                        if not capturing:
                            # session/load history replay — ignore until live.
                            continue
                        update = (message.get("params") or {}).get("update") or {}
                        # OpenCode's tool `title` mutates across pending ->
                        # completed updates (the tool name, then the call's
                        # description); keep the first-seen title as the label.
                        if update.get("sessionUpdate") in {
                            "tool_call",
                            "tool_call_update",
                        }:
                            tcid = update.get("toolCallId") or ""
                            title = update.get("title")
                            if title and tcid and tcid not in tool_titles:
                                tool_titles[tcid] = title
                        classified = self._classify_update(update)
                        if classified is None:
                            continue
                        kind, key, payload = classified
                        if kind == "text":
                            text_buf.append(payload)
                        elif kind == "tool" and key not in emitted_tools:
                            emitted_tools.add(key)
                            if key in tool_titles:
                                payload = {**payload, "tool": tool_titles[key]}
                            await flush_text()
                            await queue.put(payload)
                    elif "id" in message:
                        mid = message["id"]
                        if mid == prompt_id:
                            await flush_text()
                            if "error" in message:
                                await queue.put(
                                    {
                                        "type": "error",
                                        "message": f"OpenCode prompt failed: "
                                        f"{message['error']}",
                                    }
                                )
                            else:
                                await queue.put(
                                    {"type": "done", "session_id": current_sid}
                                )
                            break
                        future = pending_rpc.pop(mid, None)
                        if future and not future.done():
                            if "error" in message:
                                future.set_exception(
                                    RuntimeError(str(message["error"]))
                                )
                            else:
                                future.set_result(message.get("result"))
            finally:
                for future in pending_rpc.values():
                    if not future.done():
                        future.set_exception(RuntimeError("OpenCode connection closed"))
                await queue.put(None)

        reader_task = asyncio.create_task(reader())
        done_emitted = False

        try:
            await send_request(
                "initialize",
                {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": False,
                    },
                },
            )

            if session.get("agent_session_id"):
                await send_request(
                    "session/load",
                    {
                        "sessionId": session["agent_session_id"],
                        "cwd": session["working_dir"],
                        "mcpServers": [],
                    },
                )
                current_sid = session["agent_session_id"]
            else:
                result = await send_request(
                    "session/new",
                    {"cwd": session["working_dir"], "mcpServers": []},
                )
                current_sid = (result or {}).get("sessionId")

            # History replay (if any) is done now that session/load returned;
            # everything from here on is the live turn.
            capturing = True
            prompt_id = alloc_id()
            await write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": prompt_id,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": current_sid,
                        "prompt": [{"type": "text", "text": prompt}],
                    },
                }
            )

            produced = False
            while True:
                event = await queue.get()
                if event is None:
                    break
                event_type = event.get("type")
                # Either terminal outcome counts: don't pile a fallback error
                # on top of an error we already surfaced.
                if event_type in {"done", "error"}:
                    done_emitted = True
                if event_type in {"output", "tool_use", "approval_request"}:
                    produced = True
                # OpenCode swallows model/auth failures in ACP mode: it returns
                # a clean end_turn with no content and no error. A turn that ends
                # having produced nothing almost always means the model call
                # failed (commonly an expired auth token); surface a hint rather
                # than a silent empty response.
                if event_type == "done" and not produced:
                    yield {
                        "type": "output",
                        "text": "[agent-ui] OpenCode produced no output. This "
                        "usually means the model call failed silently — run "
                        "`opencode run \"hi\"` to see the real error (often an "
                        "expired auth token; fix with `opencode auth login`).",
                    }
                yield event

            returncode = await self._terminate(process)
            stderr = await stderr_task
            if not done_emitted:
                detail = stderr.strip() or f"OpenCode exited with {returncode}"
                yield {"type": "error", "message": detail}
        except Exception as exc:
            yield {"type": "error", "message": f"OpenCode ACP error: {exc}"}
        finally:
            self.processes.pop(session_id, None)
            await self._clear_session_approvals(session_id, "Session ended")
            await self._terminate(process)
            if not reader_task.done():
                reader_task.cancel()
            if not stderr_task.done():
                stderr_task.cancel()

    async def send_approval(
        self,
        session: dict[str, Any],
        request_id: str,
        behavior: str,
    ) -> None:
        if behavior not in {"allow", "deny"}:
            raise ValueError("Approval behavior must be 'allow' or 'deny'")

        future = self.pending_approvals.get(request_id)
        if future is None or self.pending_sessions.get(request_id) != session["id"]:
            raise KeyError(f"Unknown approval request: {request_id}")

        if not future.done():
            future.set_result(behavior)

    async def stop(self, session: dict[str, Any]) -> None:
        session_id = session["id"]
        await self._clear_session_approvals(session_id, "Session stopped")
        process = self.processes.get(session_id)
        if process is not None:
            await self._terminate(process)
        self.processes.pop(session_id, None)

    def _classify_update(
        self,
        update: dict[str, Any],
    ) -> tuple[str, str | None, Any] | None:
        kind = update.get("sessionUpdate")

        if kind == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text", "") if isinstance(content, dict) else ""
            return ("text", None, text)

        if kind in {"tool_call", "tool_call_update"}:
            raw_input = update.get("rawInput") or {}
            if not raw_input:
                return None
            tool_call_id = update.get("toolCallId") or ""
            return (
                "tool",
                tool_call_id,
                {
                    "type": "tool_use",
                    "tool": update.get("title") or "tool",
                    "input": raw_input,
                },
            )

        # agent_thought_chunk (reasoning), usage_update, plan, and command
        # listings carry nothing the UI needs.
        return None

    def _select_option(self, options: list[dict[str, Any]], behavior: str) -> str:
        preferred = "allow_once" if behavior == "allow" else "reject_once"
        for option in options:
            if option.get("kind") == preferred:
                return option["optionId"]

        token = "allow" if behavior == "allow" else "reject"
        for option in options:
            haystack = f"{option.get('kind', '')}{option.get('optionId', '')}".lower()
            if token in haystack:
                return option["optionId"]

        return options[0]["optionId"] if options else "reject"

    async def _terminate(self, process: asyncio.subprocess.Process) -> int | None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                return process.returncode
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        return process.returncode

    async def _clear_session_approvals(self, session_id: str, reason: str) -> None:
        request_ids = [
            request_id
            for request_id, pending_session_id in self.pending_sessions.items()
            if pending_session_id == session_id
        ]
        for request_id in request_ids:
            future = self.pending_approvals.get(request_id)
            if future and not future.done():
                future.set_exception(RuntimeError(reason))
            self.pending_approvals.pop(request_id, None)
            self.pending_sessions.pop(request_id, None)

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("SHELL", "/bin/bash")
        if self.config_path:
            env["OPENCODE_CONFIG"] = self.config_path
        return env

    async def _collect_stderr(
        self,
        stream: asyncio.StreamReader | None,
    ) -> str:
        if stream is None:
            return ""
        chunks: list[str] = []
        while True:
            raw_line = await stream.readline()
            if not raw_line:
                break
            chunks.append(raw_line.decode("utf-8", errors="replace"))
        return "".join(chunks)
