"""Enabled harnesses in picker order; the first entry is the default."""

from .agent import AgentAdapter, ClaudeCodeAdapter, PiAdapter

adapters: dict[str, AgentAdapter] = {
    "pi": PiAdapter(),
    "claude-code": ClaudeCodeAdapter(),
}
