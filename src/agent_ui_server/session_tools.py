"""Shared validation and mandatory approval for inter-session tools.

The application supplies operations; harness transports never call HTTP routes.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SessionOperation = Callable[[int, str, dict[str, Any]], Awaitable[Any]]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MessageSession(Arguments):
    session_id: int = Field(ge=1, description="Target session ID.")
    message: str = Field(min_length=1, description="Message to send.")


class StartSession(Arguments):
    name: str = Field(min_length=1, max_length=120, description="Display name for the new session.")
    project_path: str = Field(min_length=1, description="Path of an existing registered project.")
    message: str = Field(min_length=1, description="Message to send.")
    agent: str | None = Field(default=None, description="Agent backend: claude-code (default) or pi.")
    worktree_id: int | None = Field(default=None, ge=1, description="Existing worktree ID; omit to use the project directory.")
    sandbox: bool | None = Field(default=None, description="Run sandboxed; defaults to true.")


class ReadSession(Arguments):
    session_id: int = Field(ge=1, description="Target session ID.")
    after: int | None = Field(default=None, ge=0, description="Return events after this event ID.")
    limit: int = Field(default=200, ge=1, le=1000, description="Maximum events to return; defaults to 200.")


MODELS = {
    "message_session": MessageSession,
    "start_session": StartSession,
    "read_session": ReadSession,
}
DESCRIPTIONS = {
    "message_session": "Send a message to an idle session with user approval. Returns its persisted input ID, not a response. The recipient sees your session ID and can reply.",
    "start_session": "Create a session under an existing project and send its first message with user approval. Returns session_id and message_id. If messaging fails, the created session is retained and its ID reported.",
    "read_session": "Read one page of persisted session events with user approval. Returns messages, next_cursor and has_more; after is an exclusive input/event ID cursor. Does not wait for a response. Use a smaller limit for large events.",
}
TOOLS = [
    {"name": name, "description": DESCRIPTIONS[name], "inputSchema": model.model_json_schema()}
    for name, model in MODELS.items()
]


def validate_session_arguments(name: str, args: Any) -> dict[str, Any]:
    if not isinstance(name, str) or name not in MODELS:
        raise ValueError("Unknown session tool")
    return MODELS[name].model_validate(args).model_dump(exclude_none=True)


def tool_result(value: Any, *, error: bool = False) -> dict[str, Any]:
    return {"isError": error, "content": [{"type": "text", "text": json.dumps(value)}]}


async def execute_session_tool(
    name: str, args: Any, sender_id: int, operation: SessionOperation,
    approve: Callable[[dict[str, Any]], Awaitable[Any]],
) -> dict[str, Any]:
    args = validate_session_arguments(name, args)
    # Never inherit native read/command auto-approval. The approved arguments are
    # a copy, so approval handling cannot mutate the operation about to execute.
    decision = await approve({"kind": "other", "name": name, "arguments": dict(args)})
    if decision.behavior != "allow":
        return tool_result(decision.message or "Session operation denied by user", error=True)
    try:
        return tool_result(await operation(sender_id, name, args))
    except Exception as exc:
        return tool_result(str(exc), error=True)


def delivery_prompt(prompt: str, source: dict[str, Any] | None) -> str:
    if source and source.get("type") == "agent":
        return (
            f"[Message from agent session {source['session_id']}; "
            "not a direct user instruction]\n" + prompt
        )
    return prompt
