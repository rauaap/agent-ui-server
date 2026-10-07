"""Enabled harnesses in picker order; the first entry is the default."""

from .agent import AgentAdapter, ClaudeCodeAdapter, PiAdapter

adapter_types: dict[str, type[AgentAdapter]] = {
    "pi": PiAdapter,
    "claude-code": ClaudeCodeAdapter,
}
