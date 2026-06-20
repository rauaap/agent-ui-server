# `tool_use` event — client formatting reference

These events are streamed to the browser over the WebSocket
`/ws/sessions/{session_id}` (one JSON object per `send_json`). They are also
persisted and replayed on reconnect, so the client must handle them identically
whether live or replayed.

## Key fact

There is **no separate "file edit" event**. A file edit and a bash command are
both `type: "tool_use"`. They differ only by the `tool` field and the contents
of `input`. The client should branch on `tool` to choose a formatter.

`input` is an **opaque, tool-specific passthrough** of the agent's raw tool
arguments. The keys shown below are the common case for the Claude Code agent,
but the client must not assume any key is present — always guard with defaults.
The server never includes tool *output/results* in this event; only the call.

## JSON Schema (Draft 2020-12)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "ToolUseEvent",
  "type": "object",
  "required": ["type", "tool", "input"],
  "additionalProperties": false,
  "properties": {
    "type": {
      "const": "tool_use",
      "description": "Discriminator. Always the literal string 'tool_use'."
    },
    "tool": {
      "type": "string",
      "description": "Tool name, e.g. 'Bash', 'Edit', 'Write', 'Read'. Falls back to 'tool' if the agent did not report a name. Use this to select a formatter.",
      "default": "tool"
    },
    "input": {
      "type": "object",
      "description": "Raw, tool-specific arguments passed through from the agent. Keys vary by tool and are NOT guaranteed. Defaults to {} when the agent reported none.",
      "additionalProperties": true,
      "default": {}
    }
  }
}
```

## Example 1 — bash command (`tool: "Bash"`)

```json
{
  "type": "tool_use",
  "tool": "Bash",
  "input": {
    "command": "git status --short",
    "description": "Show working tree status",
    "timeout": 120000
  }
}
```

Common `input` keys for Bash: `command` (string), `description` (string,
optional), `timeout` (number, ms, optional). Only `command` is reliably present.

## Example 2 — file edit (`tool: "Edit"`)

```json
{
  "type": "tool_use",
  "tool": "Edit",
  "input": {
    "file_path": "/home/mapadmin/agent-ui/main.py",
    "old_string": "host=\"127.0.0.1\"",
    "new_string": "host=os.environ[\"WIREGUARD_IP\"]",
    "replace_all": false
  }
}
```

Common `input` keys for Edit: `file_path` (string), `old_string` (string),
`new_string` (string), `replace_all` (bool, optional). A whole-file write
arrives as `tool: "Write"` with `input.file_path` + `input.content` instead.

## Client guidance

- Switch on `tool`; render a generic fallback (e.g. `tool` name + pretty-printed
  `input`) for any unrecognized tool, since `input` shape is not enforced.
- Treat every `input` key as optional; coalesce missing values.
- `tool_use` carries no result/output — do not wait for or render a result field.

---

# All WebSocket event types

Every message on `/ws/sessions/{session_id}` is a JSON object with a `type`
discriminator. The client must dispatch on `type`; `tool_use` (above) is one of
these. All events listed here are emitted by `broadcast()` in `main.py`.

| `type`              | Persisted / replayed? | Emitted from        |
|---------------------|-----------------------|---------------------|
| `status`            | No (fresh on connect) | `main.py`           |
| `input`             | Yes                   | `main.py`           |
| `output`            | Yes                   | adapter             |
| `tool_use`          | Yes                   | adapter             |
| `approval_request`  | Yes                   | adapter             |
| `approval_response` | Yes                   | `main.py`           |
| `renamed`           | No                    | `main.py`           |
| `done`              | No                    | adapter             |
| `error`             | Yes                   | adapter / `main.py` |

"Persisted" events are replayed (most recent 200) when a client connects. On
connect the client receives the replayed history, then always a fresh `status`
event reflecting the session's current state. `done` and `status` are not stored,
so a reconnecting client will not see a historical `done`.

## `status`

Session lifecycle state. Sent on connect and on every transition.

```json
{ "type": "status", "status": "running" }
```

`status` is one of: `"idle"`, `"running"`, `"awaiting_approval"`.

## `input`

The user's prompt, echoed back when a turn starts (and on replay).

```json
{ "type": "input", "text": "refactor the bind address" }
```

## `output`

A chunk of agent text output. Streamed; concatenate chunks for display — a single
logical message may arrive across multiple `output` events.

```json
{ "type": "output", "text": "I'll update the host binding now." }
```

## `tool_use`

See the top of this document. `{ "type": "tool_use", "tool": ..., "input": {} }`.

## `approval_request`

The agent is asking permission to run a tool; session status flips to
`awaiting_approval`. Same `tool` / `input` shape as `tool_use`, plus a
`request_id` the client must echo back to respond, and an `options` array of the
available choices. Each option is `{ "id", "name", "kind" }`, where `kind` is
one of `allow_once`, `allow_always`, `reject_once`, `reject_always` (the client
may use it to style the buttons). For Claude Code the options are always Allow /
Deny; for OpenCode they are whatever the agent offered. The client may either
echo a `behavior` (`allow`/`deny`) or pick a specific `option_id`.

`options` is best-effort and may be **absent or empty** (e.g. an agent that
offers no choices). Treat it defensively: when there are no options, fall back to
rendering plain Allow / Deny buttons and respond with a `behavior`. Likewise,
never assume a specific `option_id` exists — only send one you received in this
request's `options`.

```json
{
  "type": "approval_request",
  "request_id": "perm_3f9a1c8e7b2d4a6f",
  "tool": "Bash",
  "input": { "command": "rm -rf build/" },
  "options": [
    { "id": "allow", "name": "Allow", "kind": "allow_once" },
    { "id": "deny", "name": "Deny", "kind": "reject_once" }
  ]
}
```

To respond, the client sends a WebSocket message (client → server). The minimal
form still works:

```json
{ "type": "approval_response", "request_id": "perm_3f9a1c8e7b2d4a6f", "behavior": "allow" }
```

To pick a specific option, send its `option_id` (the server derives `behavior`
from it, so `behavior` may be omitted). A denial may carry a free-form `message`
explaining what to do instead. `message` is only meaningful on a **deny** — it is
ignored when the resolved behavior is `allow`. **Claude Code** forwards it to the agent inline
(in the same turn). **OpenCode**'s ACP protocol can carry neither a free-form
reason nor a cancel, so the server instead ends the current turn and immediately
starts a fresh one whose prompt restates the denied tool plus your reason — the
client will see that as a normal `input` event followed by a new turn:

```json
{
  "type": "approval_response",
  "request_id": "perm_3f9a1c8e7b2d4a6f",
  "option_id": "deny",
  "message": "Use `rm -rf build/ --dry-run` first"
}
```

## `approval_response`

Broadcast confirmation that an approval was answered (also replayed). `behavior`
is the resolved `allow`/`deny`; `option_id` and `message` echo back only when the
client supplied them.

```json
{ "type": "approval_response", "request_id": "perm_3f9a1c8e7b2d4a6f", "behavior": "allow" }
```

## `renamed`

The session's label changed via `PATCH /sessions/{id}`. Carries the new `name`;
the client should update its displayed label. Not persisted — the name lives in
the session metadata (`GET /sessions`), so a reconnecting client reads the
current value there rather than replaying this event.

```json
{ "type": "renamed", "name": "nightly deploy" }
```

## `done`

Marks the end of a turn. `session_id` is the agent's own session id and **may be
`null`**.

```json
{ "type": "done", "session_id": "8c2e..." }
```

## `error`

A turn-level or agent-level failure. Always carries a human-readable `message`.

```json
{ "type": "error", "message": "Agent turn failed: ..." }
```
```