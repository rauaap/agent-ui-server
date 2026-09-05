"""Canonical, provider-neutral tool actions and transcript event constructors."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, TypeAdapter, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _NonEmptyModel(_StrictModel):
    @staticmethod
    def _non_empty(value: str) -> str:
        if not value:
            raise ValueError("must be a non-empty string")
        return value


class CommandAction(_NonEmptyModel):
    kind: Literal["command"]
    command: StrictStr
    description: StrictStr | None = None
    timeout_ms: StrictFloat | StrictInt | None = None
    shell: StrictStr | None = None

    _command_non_empty = field_validator("command")(_NonEmptyModel._non_empty)

    @field_validator("description", "shell")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("optional strings must be non-empty when present")
        return value

    @field_validator("timeout_ms")
    @classmethod
    def timeout(cls, value: float | int | None) -> float | int | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("timeout_ms must be finite and non-negative")
        return value


class ReadAction(_NonEmptyModel):
    kind: Literal["read"]
    path: StrictStr
    offset: StrictInt | None = None
    limit: StrictInt | None = None

    _path_non_empty = field_validator("path")(_NonEmptyModel._non_empty)


class EditEntry(_StrictModel):
    old_text: StrictStr
    new_text: StrictStr
    replace_all: StrictBool | None = None

    @field_validator("old_text")
    @classmethod
    def old_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("old_text must be non-empty")
        return value


class EditAction(_NonEmptyModel):
    kind: Literal["edit"]
    path: StrictStr
    edits: list[EditEntry] = Field(min_length=1)

    _path_non_empty = field_validator("path")(_NonEmptyModel._non_empty)


class WriteAction(_NonEmptyModel):
    kind: Literal["write"]
    path: StrictStr
    content: StrictStr

    _path_non_empty = field_validator("path")(_NonEmptyModel._non_empty)


class SearchAction(_NonEmptyModel):
    kind: Literal["search"]
    mode: Literal["content", "files"]
    query: StrictStr
    path: StrictStr | None = None
    glob: StrictStr | None = None
    limit: StrictInt | None = None

    _query_non_empty = field_validator("query")(_NonEmptyModel._non_empty)

    @field_validator("path", "glob")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("optional strings must be non-empty when present")
        return value


class ListAction(_StrictModel):
    kind: Literal["list"]
    path: StrictStr | None = None
    limit: StrictInt | None = None

    @field_validator("path")
    @classmethod
    def optional_path(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("path must be non-empty when present")
        return value


class WebAction(_NonEmptyModel):
    kind: Literal["web"]
    operation: Literal["search", "fetch"]
    query: StrictStr | None = None
    url: StrictStr | None = None
    prompt: StrictStr | None = None

    @field_validator("query", "url")
    @classmethod
    def target_non_empty(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("web targets must be non-empty")
        return value

    @field_validator("prompt")
    @classmethod
    def prompt_non_empty(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("prompt must be non-empty when present")
        return value

    @model_validator(mode="after")
    def operation_fields(self) -> "WebAction":
        if self.operation == "search" and (self.query is None or self.url is not None):
            raise ValueError("web search requires query and forbids url")
        if self.operation == "fetch" and (self.url is None or self.query is not None):
            raise ValueError("web fetch requires url and forbids query")
        return self


class TaskAction(_NonEmptyModel):
    kind: Literal["task"]
    description: StrictStr
    prompt: StrictStr | None = None
    agent: StrictStr | None = None

    _description_non_empty = field_validator("description")(_NonEmptyModel._non_empty)

    @field_validator("prompt", "agent")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("optional strings must be non-empty when present")
        return value


class OtherAction(_NonEmptyModel):
    kind: Literal["other"]
    name: StrictStr
    arguments: dict[str, Any]

    _name_non_empty = field_validator("name")(_NonEmptyModel._non_empty)


CanonicalAction = Annotated[
    CommandAction | ReadAction | EditAction | WriteAction | SearchAction
    | ListAction | WebAction | TaskAction | OtherAction,
    Field(discriminator="kind"),
]
_ACTION_ADAPTER = TypeAdapter(CanonicalAction)


class ApprovalOption(_NonEmptyModel):
    id: StrictStr
    name: StrictStr
    kind: Literal["allow_once", "allow_always", "reject_once", "reject_always"]

    _id_non_empty = field_validator("id")(_NonEmptyModel._non_empty)
    _name_non_empty = field_validator("name")(_NonEmptyModel._non_empty)


def canonical_action(kind: str, **fields: Any) -> dict[str, Any]:
    """Validate and serialize one recognized action, omitting optional None fields."""
    model = _ACTION_ADAPTER.validate_python({"kind": kind, **fields}, strict=True)
    return model.model_dump(exclude_none=True)


def validate_action(action: Any) -> dict[str, Any]:
    model = _ACTION_ADAPTER.validate_python(action, strict=True)
    return model.model_dump(exclude_none=True)


def other_action(name: Any, arguments: Any) -> dict[str, Any]:
    safe_name = name if isinstance(name, str) and name else "tool"
    safe_arguments = arguments if isinstance(arguments, dict) else {}
    return canonical_action("other", name=safe_name, arguments=safe_arguments)


def tool_use_event(call_id: str, action: Any) -> dict[str, Any]:
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("call_id must be a non-empty string")
    return {"type": "tool_use", "call_id": call_id, "action": validate_action(action)}


def approval_request_event(
    request_id: str,
    call_id: str,
    action: Any,
    options: Any,
) -> dict[str, Any]:
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id must be a non-empty string")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("call_id must be a non-empty string")
    if not isinstance(options, list):
        raise ValueError("options must be an array")
    validated_options = [ApprovalOption.model_validate(option, strict=True) for option in options]
    return {
        "type": "approval_request",
        "request_id": request_id,
        "call_id": call_id,
        "action": validate_action(action),
        "options": [option.model_dump() for option in validated_options],
    }


def auto_approval_setting(action: Any) -> str | None:
    kind = validate_action(action)["kind"]
    if kind == "command":
        return "auto_approve_command"
    if kind in {"edit", "write"}:
        return "auto_approve_write"
    return None
