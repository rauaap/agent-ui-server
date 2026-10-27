"""Native block normalization and server-owned accumulated draft state.

Adapter bookkeeping is deliberately separate from DraftRegistry: only the latter
is replayable, and its caller must hold the session stream lock.
"""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any, Callable

from partial_json_parser import loads, JSONDecodeError


def argument_preview(raw: str) -> dict[str, Any]:
    """Parse provisional JSON, never canonicalize or execute it."""
    try:
        value = loads(raw)
    except (JSONDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class NativeBlocks:
    def __init__(self, tool_event: Callable[[dict[str, Any]], dict[str, Any]]):
        self.tool_event = tool_event
        self.blocks: dict[Any, dict[str, Any]] = {}
        self.finished: set[Any] = set()
        self.calls: set[str] = set()

    def start(self, key: Any, block: dict[str, Any], *, pi: bool = False) -> list[dict[str, Any]]:
        if key in self.blocks or key in self.finished:
            return []
        kind = block.get("type")
        if kind == "text":
            draft = {"kind": "text", "text": block.get("text", "")}
        elif kind in {"tool_use", "server_tool_use", "toolCall"}:
            call_id, name = block.get("id"), block.get("name")
            if not call_id or not name or call_id in self.calls:
                return []
            args = block.get("arguments" if pi else "input", {})
            draft = {"kind": "tool", "call_id": call_id, "name": name,
                     # Pi's parsed initial message content is not a raw prefix.
                     "arguments_json": json.dumps(args, ensure_ascii=True) if args and not pi else "",
                     "arguments_preview": deepcopy(args) if isinstance(args, dict) else {}}
        else:
            return []
        draft["stream_id"] = uuid.uuid4().hex
        self.blocks[key] = draft
        return [{"type": "_stream_start", "draft": deepcopy(draft)}]

    def update(self, key: Any, delta: str) -> list[dict[str, Any]]:
        if key not in self.blocks or key in self.finished:
            return []
        draft = self.blocks[key]
        event = {"type": "_stream_delta", "stream_id": draft["stream_id"], "delta": delta}
        if draft["kind"] == "text":
            draft["text"] += delta
            event["text"] = draft["text"]
        else:
            draft["arguments_json"] += delta
            draft["arguments_preview"] = argument_preview(draft["arguments_json"])
            event.update(arguments_json=draft["arguments_json"], arguments_preview=deepcopy(draft["arguments_preview"]))
        return [event]

    def interrupt(self, reason: str) -> list[dict[str, Any]]:
        events = []
        for key, draft in self.blocks.items():
            if key not in self.finished:
                self.finished.add(key)
                events.append({"type": "_stream_interrupt", "reason": reason, "stream_id": draft["stream_id"]})
        return events

    def finish(self, key: Any, block: dict[str, Any], *, pi: bool = False) -> list[dict[str, Any]]:
        if key in self.finished:
            return []
        # Only starts may omit tool identity. Final artifacts must be complete.
        if block.get("type") in {"tool_use", "server_tool_use", "toolCall"}:
            if not block["id"] or not block["name"]:
                raise ValueError("Final tool call requires id and name")
            block["arguments" if pi else "input"]
        # A final artifact can be the first observation of a block.
        starts = self.start(key, block, pi=pi)
        draft = self.blocks.get(key)
        if draft is None:
            return []
        self.finished.add(key)
        if draft["kind"] == "text":
            text = block["text"]
            if not text.strip():
                # Keep substantive partial output even if an empty final artifact arrives.
                return starts + [{"type": "_stream_interrupt", "reason": "incomplete", "stream_id": draft["stream_id"]}]
            final = {"type": "output", "text": text}
        else:
            call_id = block["id"]
            if call_id in self.calls:
                return []
            final = self.tool_event({"id": call_id, "name": block["name"],
                                     "input": block["arguments" if pi else "input"]})
            self.calls.add(call_id)
        final["stream_id"] = draft["stream_id"]
        return starts + [final]


class ClaudeBlocks(NativeBlocks):
    def __init__(self, tool_event: Callable):
        super().__init__(tool_event)
        self.message_id: str | None = None
        self.pending: dict[Any, str] = {}
        self.artifacts: set[str] = set()

    def partial(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event["type"]
        if kind == "message_start":
            message_id = event["message"]["id"]
            events = self.interrupt("incomplete") if self.message_id != message_id else []
            self.message_id = message_id
            return events
        if kind not in {"content_block_start", "content_block_delta"}:
            # Block stop alone is not authoritative; assistant artifacts are.
            return []
        if self.message_id is None:
            raise ValueError("Claude block without message_start")
        key = (self.message_id, event["index"])
        if kind == "content_block_start":
            block = event["content_block"]
            events = self.start(key, block)
            if block.get("type") in {"tool_use", "server_tool_use"} and key not in self.blocks:
                self.pending.setdefault(key, "")
            elif events and key in self.pending:
                raw = self.pending.pop(key)
                self.blocks[key].update(arguments_json=raw, arguments_preview=argument_preview(raw))
                events[0]["draft"] = deepcopy(self.blocks[key])
            return events
        delta = event["delta"]
        if delta["type"] == "text_delta":
            return self.update(key, delta["text"])
        if delta["type"] == "input_json_delta":
            if key in self.pending:
                self.pending[key] += delta["partial_json"]
                return []
            return self.update(key, delta["partial_json"])
        return []

    def assistant(self, message: dict[str, Any], artifact_id: str | None = None) -> list[dict[str, Any]]:
        if artifact_id is not None:
            if artifact_id in self.artifacts:
                return []
            self.artifacts.add(artifact_id)
        content = message["content"]
        message_id = message.get("id") or uuid.uuid4().hex
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        events = []
        source = [(key, draft) for key, draft in self.blocks.items() if key[0] == message_id]
        text_keys = [key for key, draft in source if draft["kind"] == "text"]
        text_index = 0
        for index, block in enumerate(content):
            if isinstance(block, dict):
                key = (message_id, index)
                if block.get("type") in {"tool_use", "server_tool_use"}:
                    # Claude can emit a single completed block, rather than the
                    # whole message content array. Its array index then is NOT
                    # the native content-block index; call ID is authoritative.
                    pending_keys = [native_key for native_key in self.pending if native_key[0] == message_id]
                    fallback = pending_keys[0] if len(pending_keys) == 1 else (message_id, ("call", block["id"]))
                    key = next((native_key for native_key, draft in source if draft.get("call_id") == block["id"]), fallback)
                elif block.get("type") == "text" and text_index < len(text_keys):
                    key = text_keys[-1] if len(content) == 1 else text_keys[text_index]
                    text_index += 1
                if key in self.pending:
                    starts = self.start(key, block)
                    if starts:
                        raw = self.pending.pop(key)
                        self.blocks[key].update(arguments_json=raw, arguments_preview=argument_preview(raw))
                        starts[0]["draft"] = deepcopy(self.blocks[key])
                        events.extend(starts)
                events.extend(self.finish(key, block))
        return events


class PiBlocks(NativeBlocks):
    def __init__(self, tool_event: Callable):
        super().__init__(tool_event)
        self.message_id = uuid.uuid4().hex
        self.pending: dict[Any, str] = {}

    def new_message(self, message: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.message_id = uuid.uuid4().hex
        self.pending.clear()
        events = []
        if message is not None:
            # message_start still carries a message on the actual RPC wire.
            # Preserve any initial content without assuming SDK update snapshots.
            for index, block in enumerate(message["content"]):
                events.extend(self.start((self.message_id, index), block, pi=True))
        return events

    def update_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize Pi's delta-only JSON/RPC events, not SDK event snapshots."""
        kind = event["type"]
        if kind not in {"text_start", "text_delta", "text_end", "toolcall_start", "toolcall_delta", "toolcall_end"}:
            return []
        # Read authoritative final fields first so malformed artifacts fail directly.
        final = ({"type": "text", "text": event["content"]} if kind == "text_end"
                 else event["toolCall"] if kind == "toolcall_end" else None)
        key = (self.message_id, event["contentIndex"])
        if final is not None:
            starts = []
            if key in self.pending:
                starts = self.start(key, final, pi=True)
                if starts:
                    raw = self.pending.pop(key)
                    self.blocks[key].update(arguments_json=raw, arguments_preview=argument_preview(raw))
                    starts[0]["draft"] = deepcopy(self.blocks[key])
            return starts + self.finish(key, final, pi=True)
        if kind == "text_start":
            return self.start(key, {"type": "text", "text": ""}, pi=True)
        if kind == "toolcall_start":
            events = self.start(key, {"type": "toolCall", "id": event["id"], "name": event["toolName"], "arguments": {}}, pi=True)
            if not events and key not in self.blocks and key not in self.finished:
                self.pending.setdefault(key, "")
            return events
        if key in self.finished:
            return []
        if key in self.pending:
            self.pending[key] += event["delta"]
            return []
        # A delta without a native start is a malformed stream, not permission to
        # fabricate a block or drop generated output silently.
        if key not in self.blocks:
            raise KeyError(key)
        return self.update(key, event["delta"])

    def message_end(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        reason = message.get("stopReason")
        if reason in {"error", "aborted"}:
            events = self.interrupt("cancelled" if reason == "aborted" else "error")
        else:
            events = []
            for index, block in enumerate(message.get("content", [])):
                events.extend(self.finish((self.message_id, index), block, pi=True))
            events.extend(self.interrupt("incomplete"))
        return events

    def execution(self, call_id: str, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        if call_id in self.calls:
            return []
        # Correlate with a generated block even if execution preceded toolcall_end.
        key = next((key for key, draft in self.blocks.items() if draft.get("call_id") == call_id), (self.message_id, call_id))
        return self.finish(key, {"type": "toolCall", "id": call_id, "name": name, "arguments": args}, pi=True)


class DraftRegistry:
    """Synchronous state mutation, used exclusively under stream_locks."""
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.next_ordinal = 0
        self.drafts: dict[str, dict[str, Any]] = {}
        self.metadata: dict[str, dict[str, Any]] = {}

    def snapshot(self) -> dict[str, Any]:
        return {"type": "stream_snapshot", "drafts": deepcopy(list(self.drafts.values()))}

    def apply(self, event: dict[str, Any]) -> dict[str, Any]:
        if event["type"] == "_stream_start":
            draft = deepcopy(event["draft"])
            stream_id = draft["stream_id"]
            meta = {"stream_id": stream_id, "turn_id": self.turn_id, "ordinal": self.next_ordinal}
            self.next_ordinal += 1
            self.metadata[stream_id] = meta
            draft.update(meta, revision=0)
            self.drafts[stream_id] = draft
            return {"type": "stream_start", "draft": deepcopy(draft)}
        draft = self.drafts[event["stream_id"]]
        draft["revision"] += 1
        frame = {"type": "stream_delta", "stream_id": event["stream_id"], "revision": draft["revision"], "delta": event["delta"]}
        if draft["kind"] == "text":
            draft["text"] = event["text"]
        else:
            draft["arguments_json"] = event["arguments_json"]
            draft["arguments_preview"] = deepcopy(event["arguments_preview"])
            frame["arguments_preview"] = deepcopy(event["arguments_preview"])
        return frame

    def interrupt(self, reason: str, persist: Callable, stream_id: str | None = None) -> list[dict[str, Any]]:
        frames = []
        for key, draft in list(self.drafts.items()):
            if stream_id is not None and key != stream_id:
                continue
            substantive = draft["text"].strip() if draft["kind"] == "text" else (draft["arguments_json"].strip() or draft["arguments_preview"])
            if substantive:
                frames.append(persist({"type": "stream_interrupted", "draft": deepcopy(draft), "reason": reason}))
            else:
                frames.append({"type": "stream_remove", "stream_id": key})
            del self.drafts[key]
        return frames
