# Inter-agent communication design

Status: implemented. Claude protocol coverage uses a simulated CLI peer; real
Claude compatibility still requires a deployment smoke test.

## Goal

Expose three server-executed tools to agents, matching the high-level helpers in
`../agent-ui-api`:

- `message_session(session_id, message)`
- `start_session(name, project_path, message, *, agent=None, worktree_id=None)`
- `read_session(session_id, *, after=None, limit=200)`

Communication is between sessions, not between fixed parent/child roles. An agent
can start another session, send a message to an existing session, read its history,
and reply to the session that contacted it.

## Tool semantics

### `message_session`

Submit an input to the target session and return its persisted input ID. This does
not wait for the agent's response.

Reuse `begin_turn()` and its existing checks and lifecycle: validate the target,
reject archived or busy sessions, persist the input, broadcast it, and start the
turn. Do not add a separate queue or silently retry busy targets.

### `start_session`

Create a session under an existing project, then submit its first message using
the same agent-originated input path as `message_session`. Return:

```json
{"session_id": 43, "message_id": 123}
```

Preserve the API helper's semantics: optional creation arguments use the existing
server defaults. If creation succeeds but the first message fails, retain the
session and report its ID with the failure. Do not automatically retry or delete
it.

Agent-started sessions are always sandboxed; the tool has no `sandbox` argument.
Unsandboxed sessions can only be created through the HTTP API.

The session itself does not need an agent-created/user-created distinction. Its
first input does, just like every subsequent agent-sent input.

### `read_session`

Return one page of persisted events, with the same shape and cursor semantics as
the scrollback endpoint:

```json
{
  "messages": [],
  "next_cursor": 123,
  "has_more": false
}
```

Reuse `Database.scrollback_after()` and share the pagination response assembly
with the endpoint. Events are ordered by ascending ID and exclude the `after`
cursor. Preserve existing bounds: `after >= 0` when supplied and
`1 <= limit <= 1000`, defaulting to 200.

This tool does not wait for completion or aggregate agent output. Input IDs allow
the caller to read events following a submitted message; they are not separate
response or turn IDs.

## Input provenance

Add a structured `source` to input payloads. Keep `text` unchanged.

User-originated input:

```json
{
  "text": "Please review the changes.",
  "source": {"type": "user"}
}
```

Agent-originated input:

```json
{
  "text": "Please review the changes.",
  "source": {"type": "agent", "session_id": 42}
}
```

Here `source.session_id` identifies the **sending** session, not the receiving
session. It enables replies with `message_session` and lets clients link a message
to its sender. No parent/child relationship is implied.

The server derives agent provenance from the session making the tool call.
`source` and the sender ID are not model-supplied tool arguments. Ordinary user
submission paths set user provenance; they must not accept a client override
claiming agent provenance.

Existing input records without `source` are interpreted as user-originated. No
historical backfill is required.

### Scrollback

Persist `source` alongside `text` in the input record's `payload`:

```json
{
  "id": 123,
  "session_id": 43,
  "ts": "...",
  "type": "input",
  "payload": {
    "text": "Please review the changes.",
    "source": {"type": "agent", "session_id": 42}
  }
}
```

The outer `session_id` is the receiving session. Both the scrollback endpoint and
`read_session` expose the stored provenance.

### Live events

Include the same provenance in the live WebSocket input event:

```json
{
  "type": "input",
  "text": "Please review the changes.",
  "source": {"type": "agent", "session_id": 42}
}
```

History and live events must agree about the source of an input. UI rendering,
clickable sender links, and navigation are client concerns, not part of this
server implementation.

## Delivering sender context to the agent

Currently adapters receive prompt text, not the input record's structured
metadata. Persisting `source` alone therefore does not inform the receiving agent
who sent a message.

Carry provenance through the turn path to the adapter. When passing an
agent-originated input to the Claude or pi harness, add server-generated sender
context, for example:

```text
[Message from agent session 42]
Please review the changes.
```

The exact wrapper is an implementation detail. Its purpose is to identify the
sender and provide the session ID needed to reply. It does not give the message
higher instruction priority or imply user approval of its contents.

Do not persist the injected wrapper as the input's text. Scrollback retains the
original message and structured provenance. User messages do not need an
agent-sender wrapper.

## Transport, approval, and execution

- **Claude:** extend the existing `agent_ui` SDK MCP server used for sandbox
  bypass rather than creating another MCP server.
- **Pi:** reuse the existing extension's server request/response transport and
  cancellation plumbing where practical.
- **Shared backend:** centralize schemas, argument validation, approval policy,
  and session operations rather than duplicating business logic per harness.

Each of the three tools requires explicit server-side approval before executing,
including reads. Existing read/command auto-approval must not authorize these
operations. The approval request must identify the requested operation and its
arguments. Denial or cancellation must not execute the operation.

These capabilities are independent of sandbox bypass: their availability must
not depend on `CLAUDE_HOST_EXEC`, `PI_HOST_EXEC`, or whether the calling session
is sandboxed. The agent requests an operation through its harness transport; the
server executes it internally. No HTTP loopback, API token exposure, or sandbox
network access is needed.

## Code reuse

The two recent commits provide most of the underlying primitives:

- `0711964`: `begin_turn()` returns the persisted input ID and already owns turn
  admission, input persistence/broadcast, and task startup.
- `14f6256`: `Database.scrollback_after()` provides cursor-based event querying.

Extract the small pagination wrapper and session creation logic currently in
route handlers into shared operations. Both REST handlers and tool execution
should call these operations; tool execution should not invoke HTTP endpoints.

Extend the existing input persistence and broadcast path to accept trusted source
metadata, and carry that metadata to harness delivery. Avoid a second,
agent-specific implementation of turn startup.

## Verification checklist

- User input is marked as user-originated; legacy inputs remain readable.
- Agent input stores the calling session's ID, never a model-provided sender.
- `start_session`'s first input has the same provenance as later agent messages.
- Live events, scrollback, and `read_session` agree on provenance.
- Both harnesses receive sender context while stored text remains unchanged.
- All three operations require approval, even with ordinary auto-approval on.
- Denied/cancelled requests do not execute; pending requests are cleaned up.
- Both harness integrations work independently of sandbox bypass settings.
- Existing missing/archived/busy target behavior is preserved.
- Creation followed by failed messaging reports the retained session's ID.
- Pagination and input-ID return semantics match the API helpers.
