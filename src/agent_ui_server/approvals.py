"""Shared model-facing approval messages."""


def denial_message(reason: str | None) -> str:
    """Keep the refusal explicit even when the user supplies an explanation."""
    message = "Tool execution was denied by the user."
    if reason and reason.strip():
        message += f" Reason: {reason.strip()}"
    return message
