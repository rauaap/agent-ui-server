"""Read-only shared asset tool validation and execution for both transports."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .session_tools import SessionOperation, tool_result

NAME = "resolve_asset_link"
DESCRIPTION = (
    "Get a server-relative URL for a file or directory under a registered shared asset root."
)


class ResolveAssetLink(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(description="Absolute filesystem path to link.")

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("path must be absolute")
        return value


TOOL = {
    "name": NAME, "description": DESCRIPTION,
    "inputSchema": ResolveAssetLink.model_json_schema(),
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


def validate_asset_arguments(args: Any) -> dict[str, Any]:
    return ResolveAssetLink.model_validate(args).model_dump()


async def execute_asset_tool(
    args: Any, sender_id: int, operation: SessionOperation,
) -> dict[str, Any]:
    args = validate_asset_arguments(args)
    try:
        return tool_result(await operation(sender_id, NAME, args))
    except Exception as exc:
        return tool_result(str(exc), error=True)
