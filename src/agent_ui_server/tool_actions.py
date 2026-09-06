"""Provider-specific projections into the canonical tool action schema."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from pydantic import ValidationError

from .actions import canonical_action, other_action


ToolTranslator = Callable[[dict[str, Any]], dict[str, Any]]


def _optional(
    source: dict[str, Any],
    native_name: str,
    canonical_name: str | None = None,
    *,
    omit_empty: bool = False,
) -> dict[str, Any]:
    """Return a canonical projection of one optional native field."""
    if native_name not in source or source[native_name] is None:
        return {}
    value = source[native_name]
    if omit_empty and value == "":
        return {}
    return {canonical_name or native_name: value}


def action_or_other(
    name: Any,
    arguments: Any,
    translators: Mapping[str, ToolTranslator],
) -> dict[str, Any]:
    """Translate a native call, falling back for unknown or malformed tools."""
    native_name = name if isinstance(name, str) and name else "tool"
    native_arguments = arguments if isinstance(arguments, dict) else {}
    translate = translators.get(native_name)
    if translate is not None:
        try:
            return translate(native_arguments)
        except ValidationError:
            pass
    return other_action(native_name, native_arguments)


def _claude_bash(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "command",
        command=arguments.get("command"),
        shell="bash",
        **_optional(arguments, "description", omit_empty=True),
        **_optional(arguments, "timeout", "timeout_ms"),
    )


def _claude_read(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "read",
        path=arguments.get("file_path"),
        **_optional(arguments, "offset"),
        **_optional(arguments, "limit"),
    )


def _claude_edit_entries(raw_edits: Any) -> Any:
    if not isinstance(raw_edits, list):
        return raw_edits

    edits = []
    for raw_edit in raw_edits:
        if not isinstance(raw_edit, dict):
            edits.append(raw_edit)
            continue
        edits.append(
            {
                "old_text": raw_edit.get("old_string"),
                "new_text": raw_edit.get("new_string"),
                **_optional(raw_edit, "replace_all"),
            }
        )
    return edits


def _claude_edit(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "edit",
        path=arguments.get("file_path"),
        edits=_claude_edit_entries([arguments]),
    )


def _claude_multi_edit(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "edit",
        path=arguments.get("file_path"),
        edits=_claude_edit_entries(arguments.get("edits")),
    )


def _claude_write(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "write",
        path=arguments.get("file_path"),
        content=arguments.get("content"),
    )


def _claude_glob(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "search",
        mode="files",
        query=arguments.get("pattern"),
        **_optional(arguments, "path"),
    )


def _claude_grep(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "search",
        mode="content",
        query=arguments.get("pattern"),
        **_optional(arguments, "path"),
        **_optional(arguments, "glob", omit_empty=True),
        **_optional(arguments, "head_limit", "limit"),
    )


def _claude_web_fetch(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "web",
        operation="fetch",
        url=arguments.get("url"),
        **_optional(arguments, "prompt", omit_empty=True),
    )


def _claude_web_search(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "web",
        operation="search",
        query=arguments.get("query"),
    )


def _claude_task(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "task",
        description=arguments.get("description"),
        **_optional(arguments, "prompt", omit_empty=True),
        **_optional(arguments, "subagent_type", "agent", omit_empty=True),
    )


CLAUDE_TOOL_TRANSLATORS: dict[str, ToolTranslator] = {
    "Bash": _claude_bash,
    "Read": _claude_read,
    "Edit": _claude_edit,
    "MultiEdit": _claude_multi_edit,
    "Write": _claude_write,
    "Glob": _claude_glob,
    "Grep": _claude_grep,
    "WebFetch": _claude_web_fetch,
    "WebSearch": _claude_web_search,
    "Task": _claude_task,
    "Agent": _claude_task,
}


def _pi_command(arguments: dict[str, Any], *, shell: str) -> dict[str, Any]:
    timeout: dict[str, Any] = {}
    if "timeout" in arguments and arguments["timeout"] is not None:
        native_timeout = arguments["timeout"]
        timeout["timeout_ms"] = (
            native_timeout * 1000
            if isinstance(native_timeout, (int, float))
            and not isinstance(native_timeout, bool)
            else native_timeout
        )
    return canonical_action(
        "command",
        command=arguments.get("command"),
        shell=shell,
        **timeout,
    )


def _pi_read(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "read",
        path=arguments.get("path"),
        **_optional(arguments, "offset"),
        **_optional(arguments, "limit"),
    )


def _pi_edit(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_edits = arguments.get("edits")
    if raw_edits is None and ("oldText" in arguments or "newText" in arguments):
        raw_edits = [
            {
                "oldText": arguments.get("oldText"),
                "newText": arguments.get("newText"),
            }
        ]

    edits: Any = raw_edits
    if isinstance(raw_edits, list):
        edits = [
            {
                "old_text": raw_edit.get("oldText"),
                "new_text": raw_edit.get("newText"),
            }
            if isinstance(raw_edit, dict)
            else raw_edit
            for raw_edit in raw_edits
        ]
    return canonical_action("edit", path=arguments.get("path"), edits=edits)


def _pi_write(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "write",
        path=arguments.get("path"),
        content=arguments.get("content"),
    )


def _pi_grep(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "search",
        mode="content",
        query=arguments.get("pattern"),
        **_optional(arguments, "path"),
        **_optional(arguments, "glob", omit_empty=True),
        **_optional(arguments, "limit"),
    )


def _pi_find(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "search",
        mode="files",
        query=arguments.get("pattern"),
        **_optional(arguments, "path"),
        **_optional(arguments, "limit"),
    )


def _pi_list(arguments: dict[str, Any]) -> dict[str, Any]:
    return canonical_action(
        "list",
        **_optional(arguments, "path"),
        **_optional(arguments, "limit"),
    )


PI_TOOL_TRANSLATORS: dict[str, ToolTranslator] = {
    "bash": partial(_pi_command, shell="bash"),
    "powershell": partial(_pi_command, shell="powershell"),
    "read": _pi_read,
    "edit": _pi_edit,
    "write": _pi_write,
    "grep": _pi_grep,
    "find": _pi_find,
    "ls": _pi_list,
}
