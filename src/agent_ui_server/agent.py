from __future__ import annotations

import abc
import asyncio
import json
import os
import shutil
import signal
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .actions import approval_request_event, tool_use_event
from .sandbox import claude_sandbox_command, pi_sandbox_command
from .host_tools import (
    HostTools, TOOL_NAME, execute_host_command, pi_sandbox_guidance, sandbox_guidance,
    validate_host_arguments,
)
from .tool_actions import (
    CLAUDE_TOOL_TRANSLATORS,
    PI_TOOL_TRANSLATORS,
    action_or_other,
)


AgentEvent = dict[str, Any]


@dataclass
class ApprovalDecision:
    """A resolved answer to an approval_request.

    `behavior` is always the canonical "allow"/"deny" derived from whichever of
    `option_id` / `behavior` the client supplied. `option_id` names a specific
    choice from the request's `options` list (multiple choice); `message` is an
    optional free-form denial reason.
    """

    behavior: str
    option_id: str | None = None
    message: str | None = None


def _option_behavior(options: list[dict[str, Any]], option_id: str) -> str | None:
    """Map an option id to "allow"/"deny" via its kind, or None if not found.

    Options are stored internally in ACP shape (`optionId`, `name`, `kind`)
    regardless of agent, so `kind` is one of allow_once/allow_always/
    reject_once/reject_always.
    """
    for option in options:
        if option.get("optionId") == option_id:
            kind = (option.get("kind") or "").lower()
            return "allow" if kind.startswith("allow") else "deny"
    return None


def _event_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project internal options into the wire shape sent to clients."""
    projected: list[dict[str, Any]] = []
    for option in options:
        option_id = option.get("optionId")
        if not option_id:
            continue
        projected.append(
            {
                "id": option_id,
                "name": option.get("name") or option_id,
                "kind": option.get("kind"),
            }
        )
    return projected


def _resolve_decision(
    options: list[dict[str, Any]],
    behavior: str,
    option_id: str | None,
    message: str | None,
) -> ApprovalDecision:
    """Validate a client response and normalize it to an ApprovalDecision."""
    if option_id:
        derived = _option_behavior(options, option_id)
        if derived is None:
            raise KeyError(f"Unknown approval option: {option_id}")
        behavior = derived
    if behavior not in {"allow", "deny"}:
        raise ValueError("Approval behavior must be 'allow' or 'deny'")
    return ApprovalDecision(behavior=behavior, option_id=option_id, message=message)


def _normalize_questions(raw_questions: Any) -> list[dict[str, Any]]:
    """Project an agent's question spec into the wire `questions` shape.

    Every field is guarded with a default — the spec originates in model
    output, so it is an untrusted passthrough however it reached us.

    Shared by every adapter that supports questions: the wire shape is the
    client's contract, not any one agent's.
    """
    if not isinstance(raw_questions, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        options: list[dict[str, Any]] = []
        raw_options = raw.get("options")
        if isinstance(raw_options, list):
            for opt in raw_options:
                if not isinstance(opt, dict):
                    continue
                options.append(
                    {
                        "label": str(opt.get("label", "")),
                        "description": str(opt.get("description", "")),
                    }
                )
        normalized.append(
            {
                "question": str(raw.get("question", "")),
                "header": str(raw.get("header", "")),
                "multiSelect": bool(raw.get("multiSelect", False)),
                "options": options,
            }
        )
    return normalized


def _validate_answers(
    questions: list[dict[str, Any]],
    answers: Any,
) -> dict[str, Any]:
    """Check answers against the stored spec, keyed by question text.

    Each key must name a known question and each value a known option label
    (a list of labels for multiSelect). Raises ValueError on any mismatch so
    the request stays pending and the client can retry.
    """
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object keyed by question text")

    by_text = {question["question"]: question for question in questions}
    validated: dict[str, Any] = {}
    for question_text, value in answers.items():
        question = by_text.get(question_text)
        if question is None:
            raise ValueError(f"Unknown question: {question_text!r}")
        labels = {opt["label"] for opt in question["options"]}
        if question["multiSelect"]:
            if not isinstance(value, list):
                raise ValueError(
                    f"multiSelect question {question_text!r} expects a list of labels"
                )
            for label in value:
                if label not in labels:
                    raise ValueError(
                        f"Unknown option {label!r} for question {question_text!r}"
                    )
            validated[question_text] = list(value)
        else:
            if not isinstance(value, str) or value not in labels:
                raise ValueError(
                    f"Unknown option {value!r} for question {question_text!r}"
                )
            validated[question_text] = value
    return validated


# asyncio's StreamReader defaults to a 64 KiB line buffer. Agent stdout is
# newline-delimited JSON whose single lines (large tool results, file reads,
# long assistant messages) routinely exceed that, which makes readline() raise
# "Separator is found, but chunk is longer than limit". Give it ample room.
STREAM_LIMIT = 64 * 1024 * 1024  # 64 MiB


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    # Agents start their own session, so the group id is the spawned pid, and
    # it stays valid for the survivors after that leader has exited.
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def stop_process(process: asyncio.subprocess.Process) -> int | None:
    """SIGTERM the agent's process group, then SIGKILL whatever remains.

    A sandboxed agent is spawned as pasta, which exits on SIGTERM without
    taking Bubblewrap along: Bubblewrap is init of pasta's PID namespace, so
    only SIGKILL reaches it from here. Once the leader has exited, the group
    is killed at once, and Bubblewrap's --die-with-parent ends the sandbox.
    """
    if process.returncode is not None:
        return process.returncode
    _signal_group(process, signal.SIGTERM)
    # Poll the exit status rather than awaiting wait(): that also waits for
    # the pipes to close, which the still-running sandbox holds open.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3
    while process.returncode is None and loop.time() < deadline:
        await asyncio.sleep(0.05)
    _signal_group(process, signal.SIGKILL)
    await process.wait()
    return process.returncode


class AgentAdapter(abc.ABC):
    # Human-readable name for an agent picker, surfaced by `GET /agents`. It
    # lives on the adapter so the id, the label and the implementation cannot
    # drift apart; the endpoint falls back to the id if a subclass omits it.
    LABEL = ""

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
        *,
        option_id: str | None = None,
        message: str | None = None,
    ) -> str:
        """Answer a pending approval; returns the effective "allow"/"deny"."""
        raise NotImplementedError

    async def send_answer(
        self,
        session: dict[str, Any],
        request_id: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        """Answer a pending AskUserQuestion; returns the validated answers.

        Concrete (not abstract): adapters without an equivalent tool inherit
        this clean failure, which the server surfaces as an `error` event.
        """
        raise NotImplementedError("This agent does not support interactive questions")

    @abc.abstractmethod
    async def stop(self, session: dict[str, Any]) -> None:
        raise NotImplementedError


class ClaudeCodeAdapter(AgentAdapter):
    LABEL = "Claude Code"

    # Claude Code's stdio permission protocol does not advertise a list of
    # choices: its decision is allow (with updatedInput) or deny (with a
    # message). We surface the two it supports as multiple-choice options so the
    # wire contract supports multiple-choice approvals; "allow always" is
    # intentionally omitted since we do not persist permission rules.
    OPTIONS = [
        {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
        {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
    ]

    # The built-in tool that asks the *user* a multiple-choice question. It
    # arrives as a can_use_tool control_request like any other tool, but is
    # surfaced as a `question` event (not an approval) and answered by merging
    # the user's pick into updatedInput.answers.
    QUESTION_TOOL = "AskUserQuestion"

    # With ask:["*"], Claude forwards tool permissions here. Exempt known
    # reads, discovery, and session bookkeeping, not arbitrary tool prefixes.
    # ToolSearch only loads definitions: discovered tool calls are gated anew.
    # TaskCreate/TaskUpdate/TodoWrite mutate Claude's task metadata, not project
    # files. Agent launches, task stopping, scheduling, messaging, worktree
    # changes, and ExitPlanMode still require approval.
    # Reference: https://code.claude.com/docs/en/tools-reference
    AUTO_APPROVE_TOOLS = frozenset({
        "Read", "Glob", "Grep", "WebSearch", "WebFetch", "LSP",
        "ToolSearch", "ListMcpResourcesTool", "ReadMcpResourceTool",
        "WaitForMcpServers", "ListAgents", "CronList",
        "TaskGet", "TaskList", "TaskOutput",
        "TaskCreate", "TaskUpdate", "TodoWrite",
        "EnterPlanMode", "ReportFindings",
    })

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or os.environ.get("CLAUDE_BIN", "claude")
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self.pending_approvals: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self.pending_sessions: dict[str, int] = {}
        self.pending_options: dict[str, list[dict[str, Any]]] = {}
        # Pending AskUserQuestion calls: the future resolves to the validated
        # answers, and the spec is kept alongside so send_answer can validate.
        self.pending_questions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.pending_question_specs: dict[str, list[dict[str, Any]]] = {}
        self.tool_actions: dict[tuple[int, str], dict[str, Any]] = {}
        self.host_tools: dict[int, HostTools] = {}

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
            "--settings",
            json.dumps({
                "permissions": {"ask": ["*"]},
                "sandbox": {"autoAllowBashIfSandboxed": False},
            }),
            "--verbose",
        ]
        host_enabled = (
            os.environ.get("CLAUDE_HOST_EXEC") == "1"
            and session.get("sandbox", True)
        )
        if host_enabled:
            command.extend(["--mcp-config", json.dumps({
                "mcpServers": {"agent_ui": {"type": "sdk", "name": "agent_ui"}}
            })])
        if session.get("agent_session_id"):
            command.extend(["--resume", session["agent_session_id"]])

        try:
            if session.get("sandbox", True):
                command = claude_sandbox_command(
                    command, session["working_dir"],
                    **({"system_prompt": sandbox_guidance} if host_enabled else {}),
                    **({"sandbox_paths": session["sandbox_paths"]} if session.get("sandbox_paths") else {}),
                    **({"git_repository": session["git_repository"]} if session.get("git_repository") else {}),
                )
                env = {}
            else:
                env = self._build_env()
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=session["working_dir"],
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
                # Its own group, which stop_process() signals as a whole.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            yield {"type": "error", "message": f"Unable to start Claude Code: {exc}"}
            return
        except NotADirectoryError as exc:
            yield {"type": "error", "message": f"Invalid working directory: {exc}"}
            return
        except (OSError, ValueError) as exc:
            yield {"type": "error", "message": f"Unable to start Claude Code: {exc}"}
            return

        self.processes[session_id] = process
        stderr_task = asyncio.create_task(self._collect_stderr(process.stderr))
        result_seen = False
        error_seen = False
        host = None
        if host_enabled:
            host = HostTools(process, session["working_dir"],
                             lambda args: self._approve_host(session_id, args))
            self.host_tools[session_id] = host

        try:
            if host is not None:
                await host.start()
            await self._write_user_message(process, prompt)
            assert process.stdout is not None
            while True:
                raw_line = await host.events.get() if host is not None else await process.stdout.readline()
                if isinstance(raw_line, dict):
                    yield raw_line
                    continue
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
                    if agent_event["type"] == "error":
                        error_seen = True
                    if agent_event["type"] == "done":
                        result_seen = True
                        self._close_stdin(process)
                    yield agent_event

            returncode = await process.wait()
            stderr = await stderr_task
            if returncode != 0 and not error_seen:
                detail = stderr.strip() or f"Claude Code exited with {returncode}"
                yield {"type": "error", "message": detail}
            elif not result_seen and not error_seen:
                yield {
                    "type": "error",
                    "message": "Claude Code exited without a result event.",
                }
        finally:
            if host is not None:
                await host.close()
                self.host_tools.pop(session_id, None)
                await stop_process(process)
            self.processes.pop(session_id, None)
            await self._clear_session_approvals(session_id, "Session ended")
            self._clear_tool_actions(session_id)
            if not stderr_task.done():
                stderr_task.cancel()

    async def send_approval(
        self,
        session: dict[str, Any],
        request_id: str,
        behavior: str,
        *,
        option_id: str | None = None,
        message: str | None = None,
    ) -> str:
        future = self.pending_approvals.get(request_id)
        if future is None or self.pending_sessions.get(request_id) != session["id"]:
            raise KeyError(f"Unknown approval request: {request_id}")

        options = self.pending_options.get(request_id, self.OPTIONS)
        decision = _resolve_decision(options, behavior, option_id, message)
        if not future.done():
            future.set_result(decision)
        return decision.behavior

    async def send_answer(
        self,
        session: dict[str, Any],
        request_id: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        future = self.pending_questions.get(request_id)
        if future is None or self.pending_sessions.get(request_id) != session["id"]:
            raise KeyError(f"Unknown question request: {request_id}")

        questions = self.pending_question_specs.get(request_id, [])
        validated = _validate_answers(questions, answers)
        if not future.done():
            future.set_result(validated)
        return validated

    async def _approve_host(self, session_id: int, args: dict[str, str]) -> ApprovalDecision:
        host = self.host_tools[session_id]
        request_id = "host_" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending_approvals[request_id] = future
        self.pending_sessions[request_id] = session_id
        self.pending_options[request_id] = self.OPTIONS
        # 'other' deliberately never inherits normal command auto-approval.
        action = {"kind": "other", "name": "Execute outside sandbox",
                  "arguments": {**args, "cwd": host.cwd}}
        try:
            await host.events.put(approval_request_event(
                request_id, request_id, action, _event_options(self.OPTIONS)))
            return await future
        finally:
            self.pending_approvals.pop(request_id, None)
            self.pending_sessions.pop(request_id, None)
            self.pending_options.pop(request_id, None)

    async def stop(self, session: dict[str, Any]) -> None:
        session_id = session["id"]
        host = self.host_tools.get(session_id)
        if host is not None:
            await host.close()
        await self._clear_session_approvals(session_id, "Session stopped")
        self._clear_tool_actions(session_id)

        process = self.processes.get(session_id)
        if process is None:
            return

        await stop_process(process)
        self.processes.pop(session_id, None)

    async def _events_from_json(
        self,
        session_id: int,
        process: asyncio.subprocess.Process,
        event: dict[str, Any],
    ) -> AsyncIterator[AgentEvent]:
        event_type = event.get("type")

        if event_type == "assistant":
            for output_event in self._assistant_events(event, session_id):
                yield output_event
            return

        if event_type == "tool_use":
            yield self._tool_use_event(event, session_id)
            return

        if event_type in {"control_request", "sdk_control_request"}:
            native = self._permission_parts(event)
            if native is None:
                return
            request_id, tool, tool_input, _call_id = native

            # Allow transport dispatch only; the host executor asks independently.
            if tool in self.AUTO_APPROVE_TOOLS or (
                tool == TOOL_NAME and session_id in self.host_tools
            ):
                try:
                    await self._write_approval_response(
                        process, request_id, ApprovalDecision(behavior="allow"), tool_input
                    )
                except Exception as exc:
                    yield {"type": "error", "message": str(exc)}
                return

            # AskUserQuestion is a question for the user, not a run/deny gate:
            # surface it as a `question` event and answer it by writing the
            # user's pick into updatedInput.answers.
            if tool == self.QUESTION_TOOL:
                questions = _normalize_questions(
                    tool_input.get("questions") if isinstance(tool_input, dict) else None
                )
                loop = asyncio.get_running_loop()
                answer_future: asyncio.Future[dict[str, Any]] = loop.create_future()
                self.pending_questions[request_id] = answer_future
                self.pending_sessions[request_id] = session_id
                self.pending_question_specs[request_id] = questions

                yield {
                    "type": "question",
                    "request_id": request_id,
                    "questions": questions,
                }

                try:
                    answers = await answer_future
                    await self._write_question_response(
                        process, request_id, tool_input, answers
                    )
                except Exception as exc:
                    yield {"type": "error", "message": str(exc)}
                finally:
                    self.pending_questions.pop(request_id, None)
                    self.pending_sessions.pop(request_id, None)
                    self.pending_question_specs.pop(request_id, None)
                return

            request = self._approval_request_event(event, session_id)
            if request is None:
                return

            loop = asyncio.get_running_loop()
            future: asyncio.Future[ApprovalDecision] = loop.create_future()
            self.pending_approvals[request_id] = future
            self.pending_sessions[request_id] = session_id
            self.pending_options[request_id] = self.OPTIONS

            yield request

            try:
                decision = await future
                await self._write_approval_response(
                    process, request_id, decision, tool_input
                )
            except Exception as exc:
                yield {"type": "error", "message": str(exc)}
            finally:
                self.pending_approvals.pop(request_id, None)
                self.pending_sessions.pop(request_id, None)
                self.pending_options.pop(request_id, None)
            return

        if event_type == "result":
            subtype = str(event.get("subtype") or "")
            if event.get("is_error") or subtype.startswith("error"):
                errors = event.get("errors")
                detail = "\n".join(str(item) for item in errors if item) if isinstance(errors, list) else ""
                detail = detail or event.get("result") or event.get("error") or subtype or "Unknown error"
                yield {"type": "error", "message": f"Claude Code failed: {detail}"}
            # done also carries the resume id and closes stdin on failed results.
            yield {
                "type": "done",
                "session_id": self._extract_session_id(event),
            }
            return

        if event_type == "error":
            yield {"type": "error", "message": self._error_message(event)}

    def _assistant_events(
        self, event: dict[str, Any], session_id: int = 0
    ) -> list[AgentEvent]:
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
                    # AskUserQuestion is surfaced as a `question` event via its
                    # control_request, so don't also emit a tool_use bubble.
                    if block.get("name") == self.QUESTION_TOOL:
                        continue
                    events.append(self._tool_use_event(block, session_id))

        if not events and event.get("text"):
            events.append({"type": "output", "text": event["text"]})

        return events

    def _tool_use_event(
        self, event: dict[str, Any], session_id: int = 0
    ) -> AgentEvent:
        nested = event.get("tool_use") if isinstance(event.get("tool_use"), dict) else {}
        source = nested or event
        name = source.get("tool") or source.get("tool_name") or source.get("name") or "tool"
        arguments = source.get("input") if "input" in source else source.get("arguments", {})
        call_id = source.get("id") or source.get("tool_use_id") or f"tool_{uuid.uuid4().hex}"
        call_id = str(call_id)
        action = action_or_other(name, arguments, CLAUDE_TOOL_TRANSLATORS)
        self.tool_actions[(session_id, call_id)] = action
        return tool_use_event(call_id, action)

    def _permission_parts(
        self, event: dict[str, Any]
    ) -> tuple[str, Any, Any, str] | None:
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

        call_id = request.get("tool_use_id") or event.get("tool_use_id") or f"tool_{uuid.uuid4().hex}"
        return str(request_id), tool, tool_input, str(call_id)

    def _approval_request_event(
        self, event: dict[str, Any], session_id: int = 0
    ) -> AgentEvent | None:
        native = self._permission_parts(event)
        if native is None:
            return None
        request_id, tool, tool_input, call_id = native
        action = self.tool_actions.get((session_id, call_id))
        if action is None:
            action = action_or_other(
                tool, tool_input, CLAUDE_TOOL_TRANSLATORS
            )
        return approval_request_event(
            request_id, call_id, action, _event_options(self.OPTIONS)
        )

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
        decision: ApprovalDecision,
        tool_input: dict[str, Any],
    ) -> None:
        if process.stdin is None:
            raise RuntimeError("Claude Code stdin is unavailable")

        if decision.behavior == "allow":
            response: dict[str, Any] = {
                "behavior": "allow",
                "updatedInput": tool_input or {},
            }
        else:
            response = {
                "behavior": "deny",
                "message": decision.message or "Denied by user",
            }

        payload = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        }
        process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _write_question_response(
        self,
        process: asyncio.subprocess.Process,
        request_id: str,
        tool_input: dict[str, Any],
        answers: dict[str, Any],
    ) -> None:
        """Answer an AskUserQuestion control_request.

        The tool reads the answer back out of its own input, so we allow the
        call with the answers merged into updatedInput.answers (keyed by the
        exact question text). Keying by header, or leaving updatedInput
        unchanged, is recorded as "did not answer".
        """
        if process.stdin is None:
            raise RuntimeError("Claude Code stdin is unavailable")

        updated_input = {**(tool_input or {}), "answers": answers}
        payload = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "allow",
                    "updatedInput": updated_input,
                },
            },
        }
        process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await process.stdin.drain()

    def _clear_tool_actions(self, session_id: int) -> None:
        for key in [key for key in self.tool_actions if key[0] == session_id]:
            self.tool_actions.pop(key, None)

    async def _clear_session_approvals(self, session_id: int, reason: str) -> None:
        request_ids = [
            request_id
            for request_id, pending_session_id in self.pending_sessions.items()
            if pending_session_id == session_id
        ]
        for request_id in request_ids:
            future = self.pending_approvals.get(request_id)
            if future and not future.done():
                future.set_exception(RuntimeError(reason))
            # A pending question blocks the turn the same way an approval does;
            # unblock it too so a stop / process exit doesn't hang.
            answer_future = self.pending_questions.get(request_id)
            if answer_future and not answer_future.done():
                answer_future.set_exception(RuntimeError(reason))
            self.pending_approvals.pop(request_id, None)
            self.pending_questions.pop(request_id, None)
            self.pending_question_specs.pop(request_id, None)
            self.pending_sessions.pop(request_id, None)
            self.pending_options.pop(request_id, None)

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


class PiAdapter(AgentAdapter):
    """Adapter for Pi over its RPC mode (`pi --mode rpc`).

    RPC mode is newline-delimited JSON on stdio: we write commands to stdin
    (`prompt`, `get_state`) and read a stream of events and responses from
    stdout. Like Claude Code, each turn is one short-lived process —
    spawn, prompt, stream until `agent_settled`, exit — with continuity coming
    from Pi's own session store via `--session <id>`.

    Pi ships no permission system and no way to ask the user a question, so
    both are supplied by the bundled `pi_extension.ts`, passed with `-e`. That
    extension reaches us through Pi's extension dialog protocol: it calls
    `ctx.ui.select` / `ctx.ui.input`, which Pi serializes as
    `extension_ui_request` lines that we answer with `extension_ui_response`.
    Neither call has a field for structured data, so the extension JSON-encodes
    what we need into `title`; `_envelope` unpacks it.

    Pi also ships no web access. The vendored `pi_web_search/` extension adds
    `web_search` and `url_context`, and is passed as a second `-e` for the same
    reason as the first: `--no-extensions` drops installed extensions, so an
    explicit path is the only way to load one without also re-admitting the
    project's own.
    """

    LABEL = "Pi"

    # Envelope contract with pi_extension.ts. Bump both in step.
    MARKER = "agent-ui"
    PROTOCOL_VERSION = 1

    DEFAULT_EXTENSION = str(Path(__file__).resolve().parent / "pi_extension.ts")

    DEFAULT_WEB_EXTENSION = str(
        Path(__file__).resolve().parent / "pi_web_search" / "index.ts"
    )

    QUESTION_TOOL = "AskUserQuestion"

    # How long to wait for the extension's `ready` notification. Generous
    # because the first load compiles the TypeScript; a miss is fatal rather
    # than slow, so erring long costs nothing.
    HANDSHAKE_TIMEOUT = 60.0

    # Pi's dialogs carry no allow/deny vocabulary of their own — the extension
    # offers these two labels and we answer with one of them verbatim.
    DIALOG_ALLOW = "Allow"
    DIALOG_DENY = "Deny"

    OPTIONS = [
        {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
        {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
    ]

    # Dialog methods that block the extension until answered. The fire-and-
    # forget ones (notify, setStatus, setWidget, ...) must NOT be answered.
    BLOCKING_METHODS = {"select", "confirm", "input", "editor"}

    def __init__(
        self,
        executable: str | None = None,
        extension_path: str | None = None,
        web_extension_path: str | None = None,
    ) -> None:
        self.executable = executable or os.environ.get("PI_BIN", "pi")
        self.extension_path = (
            extension_path
            or os.environ.get("PI_EXTENSION")
            or self.DEFAULT_EXTENSION
        )
        # Unlike the gate, the web extension is optional: an empty override
        # (argument or `PI_WEB_SEARCH=`) drops it and leaves Pi without web
        # access. `or` would read that empty string as "unset" and restore the
        # default, so resolve the precedence explicitly.
        if web_extension_path is None:
            web_extension_path = os.environ.get(
                "PI_WEB_SEARCH", self.DEFAULT_WEB_EXTENSION
            )
        self.web_extension_path = web_extension_path
        self.host_calls: dict[int, dict[str, asyncio.Task[None]]] = {}
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self.pending_approvals: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self.pending_sessions: dict[str, int] = {}
        self.pending_options: dict[str, list[dict[str, Any]]] = {}
        self.pending_questions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.pending_question_specs: dict[str, list[dict[str, Any]]] = {}

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
            "--mode",
            "rpc",
            "-e",
            self.extension_path,
            # Discovery off, explicit -e on (verified: --no-extensions drops
            # discovered extensions but honours -e). A project's own
            # .pi/extensions must not join the session: tool_call handlers can
            # mutate tool input, and one running after ours could change the
            # arguments the user just approved.
            "--no-extensions",
        ]
        host_enabled = os.environ.get("PI_HOST_EXEC") == "1" and session.get("sandbox", True)
        if host_enabled:
            command.append("--agent-ui-host-exec")
        if self.web_extension_path:
            command.extend(["-e", self.web_extension_path])
        if session.get("agent_session_id"):
            command.extend(["--session", session["agent_session_id"]])

        try:
            if session.get("sandbox", True):
                command = pi_sandbox_command(
                    command, session["working_dir"],
                    **({"system_prompt": pi_sandbox_guidance} if host_enabled else {}),
                    **({"sandbox_paths": session["sandbox_paths"]} if session.get("sandbox_paths") else {}),
                    **({"git_repository": session["git_repository"]} if session.get("git_repository") else {}),
                )
                # Clear bwrap's own environment too, not just its child's.
                env = {}
            else:
                env = self._build_env()
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=session["working_dir"],
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
                # Its own group, which stop_process() signals as a whole.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            yield {"type": "error", "message": f"Unable to start Pi: {exc}"}
            return
        except NotADirectoryError as exc:
            yield {"type": "error", "message": f"Invalid working directory: {exc}"}
            return
        except (OSError, ValueError) as exc:
            yield {"type": "error", "message": f"Unable to start Pi: {exc}"}
            return

        self.processes[session_id] = process
        stderr_task = asyncio.create_task(self._collect_stderr(process.stderr))

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        ready: asyncio.Future[None] = loop.create_future()
        resolvers: set[asyncio.Task[None]] = set()
        host_calls: dict[str, asyncio.Task[None]] = {}
        self.host_calls[session_id] = host_calls
        host_lock = asyncio.Lock()

        # Per-turn correlation state.
        #  tool_args   toolCallId -> arguments, captured from
        #              tool_execution_start, which Pi emits before the approval
        #              dialog. That ordering is why the dialog envelope only
        #              needs to carry an id.
        #  deny_reasons toolCallId -> the reason the client sent with a denial,
        #              held until the extension's follow-up `input` dialog asks
        #              for it.
        #  batches     toolCallId -> the question dialogs seen so far, held
        #              until all `count` have arrived so the whole set reaches
        #              the client as one `question` event.
        tool_args: dict[str, dict[str, Any]] = {}
        tool_actions: dict[str, dict[str, Any]] = {}
        deny_reasons: dict[str, str] = {}
        batches: dict[str, dict[str, Any]] = {}
        text_buf: list[str] = []
        current_sid: str | None = session.get("agent_session_id")
        pending_error: str | None = None

        assert process.stdin is not None
        assert process.stdout is not None

        async def write_msg(obj: dict[str, Any]) -> None:
            if process.stdin is None or process.stdin.is_closing():
                return
            process.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            await process.stdin.drain()

        async def answer_dialog(dialog_id: Any, value: str | None) -> None:
            """Resolve one extension dialog; `None` cancels it.

            A cancelled dialog resolves to `undefined` inside the extension,
            which every call site there treats as a refusal — so failing to
            answer is always the safe direction.
            """
            if not dialog_id:
                return
            payload: dict[str, Any] = {
                "type": "extension_ui_response",
                "id": dialog_id,
            }
            if value is None:
                payload["cancelled"] = True
            else:
                payload["value"] = value
            try:
                await write_msg(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def spawn(coro: Any) -> None:
            task = asyncio.create_task(coro)
            resolvers.add(task)
            task.add_done_callback(resolvers.discard)

        async def flush_text() -> None:
            if not text_buf:
                return
            text = "".join(text_buf)
            text_buf.clear()
            if text.strip():
                await queue.put({"type": "output", "text": text})

        async def resolve_approval(
            request_id: str,
            dialog_id: Any,
            tool_call_id: str,
            future: asyncio.Future[ApprovalDecision],
        ) -> None:
            try:
                decision = await future
                if decision.behavior == "deny" and decision.message:
                    # Stashed for the follow-up `input` dialog the extension
                    # raises immediately after a denial.
                    deny_reasons[tool_call_id] = decision.message
                await answer_dialog(
                    dialog_id,
                    self.DIALOG_ALLOW
                    if decision.behavior == "allow"
                    else self.DIALOG_DENY,
                )
            except Exception:
                await answer_dialog(dialog_id, None)
            finally:
                self.pending_approvals.pop(request_id, None)
                self.pending_sessions.pop(request_id, None)
                self.pending_options.pop(request_id, None)

        async def approve_host(args: dict[str, str], tool_call_id: str) -> ApprovalDecision:
            request_id = "host_" + uuid.uuid4().hex
            future = loop.create_future()
            self.pending_approvals[request_id] = future
            self.pending_sessions[request_id] = session_id
            self.pending_options[request_id] = self.OPTIONS
            try:
                await flush_text()
                await queue.put(approval_request_event(
                    request_id, tool_call_id,
                    {"kind": "other", "name": "Execute outside sandbox", "arguments": {
                        **args, "cwd": session["working_dir"],
                    }}, _event_options(self.OPTIONS),
                ))
                return await future
            finally:
                self.pending_approvals.pop(request_id, None)
                self.pending_sessions.pop(request_id, None)
                self.pending_options.pop(request_id, None)

        async def resolve_host(dialog_id: Any, tool_call_id: str, args: dict[str, str]) -> None:
            try:
                async with host_lock:
                    result = await execute_host_command(
                        args, session["working_dir"], lambda values: approve_host(values, tool_call_id),
                    )
                await answer_dialog(dialog_id, json.dumps(result))
            except asyncio.CancelledError:
                await answer_dialog(dialog_id, None)
                raise
            except Exception as exc:
                await answer_dialog(dialog_id, json.dumps({
                    "isError": True, "content": [{"type": "text", "text": str(exc)}],
                }))

        async def resolve_question(
            request_id: str,
            dialogs: list[tuple[Any, dict[str, Any]]],
            questions: list[dict[str, Any]],
            future: asyncio.Future[dict[str, Any]],
        ) -> None:
            try:
                answers = await future
                for (dialog_id, _), question in zip(dialogs, questions):
                    value = answers.get(question["question"])
                    # An unanswered question is cancelled rather than guessed;
                    # the tool reports the gap to the model itself.
                    await answer_dialog(
                        dialog_id, value if isinstance(value, str) and value else None
                    )
            except Exception:
                for dialog_id, _ in dialogs:
                    await answer_dialog(dialog_id, None)
            finally:
                self.pending_questions.pop(request_id, None)
                self.pending_question_specs.pop(request_id, None)
                self.pending_sessions.pop(request_id, None)

        async def collect_question(
            envelope: dict[str, Any],
            dialog_id: Any,
        ) -> None:
            tool_call_id = str(envelope.get("toolCallId") or "")
            index = envelope.get("index")
            count = envelope.get("count")
            question = envelope.get("question")
            if (
                not tool_call_id
                or not isinstance(index, int)
                or not isinstance(count, int)
                or count < 1
                or not isinstance(question, dict)
            ):
                await answer_dialog(dialog_id, None)
                return

            batch = batches.setdefault(tool_call_id, {"count": count, "dialogs": {}})
            batch["dialogs"][index] = (dialog_id, question)
            if len(batch["dialogs"]) < batch["count"]:
                return

            batches.pop(tool_call_id, None)
            ordered = [batch["dialogs"][key] for key in sorted(batch["dialogs"])]
            questions = _normalize_questions([spec for _, spec in ordered])
            if len(questions) != len(ordered):
                for held_id, _ in ordered:
                    await answer_dialog(held_id, None)
                return

            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self.pending_questions[tool_call_id] = future
            self.pending_question_specs[tool_call_id] = questions
            self.pending_sessions[tool_call_id] = session_id

            await flush_text()
            await queue.put(
                {
                    "type": "question",
                    "request_id": tool_call_id,
                    "questions": questions,
                }
            )
            spawn(resolve_question(tool_call_id, ordered, questions, future))

        async def handle_dialog(message: dict[str, Any]) -> None:
            method = message.get("method")
            dialog_id = message.get("id")
            # notify carries its payload in `message`; the blocking dialogs
            # carry theirs in `title`.
            carrier = message.get("message") if method == "notify" else message.get("title")
            envelope = self._envelope(carrier)

            if envelope is None:
                # Not ours. Nothing should reach here with --no-extensions, but
                # a blocking dialog left unanswered would wedge the turn, so
                # refuse it rather than ignore it.
                if method in self.BLOCKING_METHODS:
                    await answer_dialog(dialog_id, None)
                return

            kind = envelope.get("kind")

            if kind == "ready":
                if not ready.done():
                    if host_enabled and envelope.get("hostTool") != "bypass_sandbox":
                        ready.set_exception(RuntimeError("Pi extension did not register bypass_sandbox"))
                    else:
                        ready.set_result(None)
                return

            if kind == "host_cancel":
                call_id = envelope.get("toolCallId")
                if isinstance(call_id, str) and call_id in host_calls:
                    host_calls[call_id].cancel()
                return

            if kind == "host_exec":
                try:
                    if (not host_enabled or method != "input"
                            or self.processes.get(session_id) is not process):
                        raise ValueError("Host execution is not enabled for this session")
                    call_id = envelope.get("toolCallId")
                    if not isinstance(call_id, str) or not call_id or call_id in host_calls:
                        raise ValueError("Invalid or duplicate host tool call id")
                    args = validate_host_arguments(envelope.get("arguments"))
                except ValueError as exc:
                    await answer_dialog(dialog_id, json.dumps({
                        "isError": True, "content": [{"type": "text", "text": str(exc)}],
                    }))
                    return
                task = asyncio.create_task(resolve_host(dialog_id, call_id, args))
                host_calls[call_id] = task
                task.add_done_callback(lambda _, key=call_id: host_calls.pop(key, None))
                return

            if kind == "approval":
                tool_call_id = str(
                    envelope.get("toolCallId") or f"tool_{uuid.uuid4().hex}"
                )
                tool = str(envelope.get("toolName") or "tool")
                request_id = f"perm_{uuid.uuid4().hex}"
                future: asyncio.Future[ApprovalDecision] = loop.create_future()
                self.pending_approvals[request_id] = future
                self.pending_sessions[request_id] = session_id
                self.pending_options[request_id] = self.OPTIONS

                await flush_text()
                action = tool_actions.get(tool_call_id)
                if action is None:
                    action = action_or_other(
                        tool,
                        tool_args.get(tool_call_id, {}),
                        PI_TOOL_TRANSLATORS,
                    )
                await queue.put(
                    approval_request_event(
                        request_id,
                        tool_call_id,
                        action,
                        _event_options(self.OPTIONS),
                    )
                )
                # Resolved off the reader so a batch of parallel tool calls
                # surfaces every approval at once instead of one at a time.
                spawn(resolve_approval(request_id, dialog_id, tool_call_id, future))
                return

            if kind == "deny_reason":
                # Protocol-only round trip: the client sent its reason together
                # with the denial, so this is answered from what we already
                # hold and the user is never prompted twice.
                tool_call_id = str(envelope.get("toolCallId") or "")
                await answer_dialog(dialog_id, deny_reasons.pop(tool_call_id, ""))
                return

            if kind == "question":
                await collect_question(envelope, dialog_id)

        async def handle_message(message: dict[str, Any]) -> None:
            nonlocal current_sid, pending_error
            kind = message.get("type")

            if kind == "message_end":
                completed = message.get("message")
                if isinstance(completed, dict) and completed.get("role") == "assistant":
                    # A later successful assistant message supersedes an error
                    # recovered through retry or context compaction.
                    reason = completed.get("stopReason")
                    if reason in {"error", "aborted"}:
                        pending_error = str(completed.get("errorMessage") or f"Pi assistant {reason}")
                    else:
                        pending_error = None
                return

            if kind == "auto_retry_end":
                if message.get("success"):
                    pending_error = None
                else:
                    pending_error = str(message.get("finalError") or pending_error or "Pi retries exhausted")
                return

            if kind == "compaction_end" and message.get("errorMessage"):
                pending_error = str(message["errorMessage"])
                return

            if kind == "message_update":
                event = message.get("assistantMessageEvent") or {}
                event_type = event.get("type")
                if event_type == "text_delta":
                    text_buf.append(str(event.get("delta") or ""))
                elif event_type == "text_end":
                    # text_end carries the whole block; the accumulated deltas
                    # are the fallback if it ever arrives without one.
                    content = event.get("content")
                    text = content if isinstance(content, str) else "".join(text_buf)
                    text_buf.clear()
                    if text.strip():
                        await queue.put({"type": "output", "text": text})
                # thinking_* and toolcall_* deltas carry nothing the UI needs.
                return

            if kind == "tool_execution_start":
                tool = str(message.get("toolName") or "tool")
                args = message.get("args")
                args = args if isinstance(args, dict) else {}
                tool_call_id = str(
                    message.get("toolCallId") or f"tool_{uuid.uuid4().hex}"
                )
                tool_args[tool_call_id] = args
                # The question tool is surfaced as a `question` event, so don't
                # also emit a tool_use bubble for it.
                if tool == self.QUESTION_TOOL:
                    return
                action = action_or_other(tool, args, PI_TOOL_TRANSLATORS)
                tool_actions[tool_call_id] = action
                await flush_text()
                await queue.put(tool_use_event(tool_call_id, action))
                return

            if kind == "extension_ui_request":
                await handle_dialog(message)
                return

            if kind == "agent_settled":
                # Terminal: agent_end can still be followed by a retry, settled
                # cannot.
                await flush_text()
                if pending_error:
                    await queue.put({"type": "error", "message": pending_error})
                await queue.put({"type": "done", "session_id": current_sid})
                await queue.put(None)
                return

            if kind == "response":
                if message.get("command") == "get_state" and message.get("success"):
                    data = message.get("data")
                    if isinstance(data, dict) and data.get("sessionId"):
                        current_sid = str(data["sessionId"])
                    return
                if not message.get("success"):
                    await flush_text()
                    await queue.put(
                        {
                            "type": "error",
                            "message": f"Pi {message.get('command')} failed: "
                            f"{message.get('error')}",
                        }
                    )
                return

            if kind == "extension_error":
                # The gate lives in that extension, so a failure there is not a
                # detail the user can be left to discover.
                await queue.put(
                    {
                        "type": "error",
                        "message": f"Pi extension error: {message.get('error')}",
                    }
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
                    if isinstance(message, dict):
                        await handle_message(message)
            finally:
                # Fail the handshake rather than let it wait out the timeout
                # when the process dies early.
                if not ready.done():
                    ready.set_exception(
                        RuntimeError("Pi exited before the extension loaded")
                    )
                await queue.put(None)

        reader_task = asyncio.create_task(reader())
        done_emitted = False

        try:
            try:
                await asyncio.wait_for(ready, timeout=self.HANDSHAKE_TIMEOUT)
            except (asyncio.TimeoutError, RuntimeError) as exc:
                # Terminate first: stderr only reaches EOF once the child is
                # gone, and its contents are the whole diagnosis here (a
                # TypeScript compile error, a bad -e path).
                await self._terminate(process)
                stderr = await self._drain(stderr_task)
                detail = stderr.strip() or str(exc)
                yield {
                    "type": "error",
                    "message": "Pi started without the agent-ui extension, so no "
                    f"tool would be gated. Refusing to run the turn. {detail}",
                }
                return

            # Asked before the prompt so the id is in hand even if the turn
            # fails: on a new session this is the only place we learn it.
            await write_msg({"id": "state", "type": "get_state"})
            await write_msg({"id": "prompt", "type": "prompt", "message": prompt})

            while True:
                event = await queue.get()
                if event is None:
                    break
                if event.get("type") in {"done", "error"}:
                    done_emitted = True
                yield event

            returncode = await self._terminate(process)
            stderr = await self._drain(stderr_task)
            if not done_emitted:
                detail = stderr.strip() or f"Pi exited with {returncode}"
                yield {"type": "error", "message": detail}
        except Exception as exc:
            yield {"type": "error", "message": f"Pi RPC error: {exc}"}
        finally:
            # Stop reading before cancelling host calls, so no new work can arrive.
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
            await self._cancel_host_calls(session_id)
            self.host_calls.pop(session_id, None)
            self.processes.pop(session_id, None)
            await self._clear_session_approvals(session_id, "Session ended")
            await self._terminate(process)
            tasks = list(resolvers)
            for task in tasks:
                task.cancel()
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(*tasks, stderr_task, return_exceptions=True)

    async def send_approval(
        self,
        session: dict[str, Any],
        request_id: str,
        behavior: str,
        *,
        option_id: str | None = None,
        message: str | None = None,
    ) -> str:
        future = self.pending_approvals.get(request_id)
        if future is None or self.pending_sessions.get(request_id) != session["id"]:
            raise KeyError(f"Unknown approval request: {request_id}")

        options = self.pending_options.get(request_id, self.OPTIONS)
        decision = _resolve_decision(options, behavior, option_id, message)
        if not future.done():
            future.set_result(decision)
        return decision.behavior

    async def send_answer(
        self,
        session: dict[str, Any],
        request_id: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        future = self.pending_questions.get(request_id)
        if future is None or self.pending_sessions.get(request_id) != session["id"]:
            raise KeyError(f"Unknown question request: {request_id}")

        questions = self.pending_question_specs.get(request_id, [])
        validated = _validate_answers(questions, answers)
        if not future.done():
            future.set_result(validated)
        return validated

    async def _cancel_host_calls(self, session_id: int) -> None:
        tasks = list(self.host_calls.get(session_id, {}).values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self, session: dict[str, Any]) -> None:
        session_id = session["id"]
        # Revoke dispatch before cancelling so buffered requests cannot start work.
        process = self.processes.pop(session_id, None)
        await self._cancel_host_calls(session_id)
        await self._clear_session_approvals(session_id, "Session stopped")
        if process is not None:
            await self._terminate(process)

    @classmethod
    def _envelope(cls, carrier: Any) -> dict[str, Any] | None:
        """Unpack the JSON envelope pi_extension.ts hides in a dialog's title.

        Returns None for anything that is not one of ours, including a plain
        human-readable title from some other extension.
        """
        if not isinstance(carrier, str) or not carrier.startswith("{"):
            return None
        try:
            parsed = json.loads(carrier)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get(cls.MARKER) != cls.PROTOCOL_VERSION:
            return None
        return parsed

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("SHELL", "/bin/bash")
        node_dir = self._bundled_node_dir()
        if node_dir:
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        return env

    def _bundled_node_dir(self) -> str | None:
        """Directory of the Node that ships beside Pi, if there is one.

        Pi's launcher is `#!/usr/bin/env node`, so it runs under whatever Node
        is first on PATH. Installed via its own installer it sits next to a
        pinned Node, and running it under an older system Node fails deep
        inside Pi's bundle with an unrelated-looking SyntaxError. Putting the
        neighbouring Node first turns that into a non-issue.
        """
        resolved = shutil.which(self.executable)
        if not resolved:
            return None
        # Not resolve(): the launcher is typically a symlink into node_modules,
        # and it is the bin directory we want, not the link's target.
        directory = Path(resolved).parent
        return str(directory) if (directory / "node").exists() else None

    async def _terminate(self, process: asyncio.subprocess.Process) -> int | None:
        return await stop_process(process)

    async def _clear_session_approvals(self, session_id: int, reason: str) -> None:
        request_ids = [
            request_id
            for request_id, pending_session_id in self.pending_sessions.items()
            if pending_session_id == session_id
        ]
        for request_id in request_ids:
            future = self.pending_approvals.get(request_id)
            if future and not future.done():
                future.set_exception(RuntimeError(reason))
            answer_future = self.pending_questions.get(request_id)
            if answer_future and not answer_future.done():
                answer_future.set_exception(RuntimeError(reason))
            self.pending_approvals.pop(request_id, None)
            self.pending_questions.pop(request_id, None)
            self.pending_question_specs.pop(request_id, None)
            self.pending_sessions.pop(request_id, None)
            self.pending_options.pop(request_id, None)

    @staticmethod
    async def _drain(task: asyncio.Task[str]) -> str:
        """Collect stderr without hanging if the stream is still open."""
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return ""

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
