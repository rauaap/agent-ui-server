from __future__ import annotations

import abc
import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
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
        if session.get("claude_session_id"):
            command.extend(["--resume", session["claude_session_id"]])

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
                    "session_id": session.get("claude_session_id"),
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
