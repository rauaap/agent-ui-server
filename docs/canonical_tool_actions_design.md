# Design: canonical tool actions across agent adapters

Status: **reviewed / ready for implementation.** No implementation has been
started. This is the agreed contract for changing the server, desktop client,
and Android client.

## Problem

The server currently gives every adapter a common outer event shape but leaves
the useful part provider-specific:

```json
{
  "type": "tool_use",
  "tool": "Edit",
  "input": {
    "file_path": "main.py",
    "old_string": "before",
    "new_string": "after"
  }
}
```

`tool` and `input` are raw harness values. Claude Code happens to match the
renderers because those renderers were written first against Claude Code's tool
names and argument spelling. The equivalent Pi call is `edit` with `path`,
`oldText`, and `newText`, so both clients fall back to JSON.

Teaching every client every harness dialect defeats the adapter boundary. It
also means adding one harness requires coordinated rendering changes in the
server, desktop client, and Android client.

**Goal:** adapters translate harness events into one canonical action schema.
Clients render that schema and do not know which harness produced it. Adding a
harness whose tools map to existing action kinds requires adapter changes only.

OpenCode has been removed from the shipped server and is deliberately out of
scope. Its former implementation remains available in version control. This
contract covers Claude Code and Pi.

## Decision summary

1. Keep `tool_use` and `approval_request` as separate transcript event types.
2. Put a canonical, discriminated `action` object in both events.
3. Give every tool invocation a required `call_id`.
4. Repeat the action in an approval request. `call_id` establishes identity but
   is not a lookup dependency.
5. Normalize in adapters, never in clients.
6. Do not expose raw provider arguments or names for recognized actions.
   Unknown or malformed tools use an explicit `other` fallback.
7. Require `options` on approvals but allow an empty array; keep
   `auto_approved` optional and true-only.
8. Derive auto-approval from `action.kind` on the server and send no category
   field to clients.
9. Make a clean wire cutover with no duplicated legacy fields. Old rows receive
   only a raw-JSON client fallback.
10. Keep tool results out of v1. This design describes calls, not their output.

## Event model

`type` describes what happened in the transcript. `action.kind` describes what
the invoked tool does.

A normal tool call:

```json
{
  "type": "tool_use",
  "call_id": "toolu_01ABC",
  "action": {
    "kind": "command",
    "command": "git status --short",
    "description": "Show the working tree status"
  }
}
```

A call that then blocks for approval remains two events:

```json
{
  "type": "tool_use",
  "call_id": "toolu_01ABC",
  "action": {
    "kind": "command",
    "command": "rm -rf build/"
  }
}
```

```json
{
  "type": "approval_request",
  "request_id": "perm_456",
  "call_id": "toolu_01ABC",
  "action": {
    "kind": "command",
    "command": "rm -rf build/"
  },
  "options": [
    { "id": "allow", "name": "Allow", "kind": "allow_once" },
    { "id": "deny", "name": "Deny", "kind": "reject_once" }
  ]
}
```

The two events are intentionally retained:

- `tool_use` records that the agent attempted a call.
- `approval_request` records that execution is blocked on a user decision.
- The client may replace the first card with the approval card when their
  `call_id` values match.
- The approval repeats `action`, so it remains renderable if the preceding event
  was dropped, lies outside the 200-row replay window, or was consumed by a
  client that does not retain transcript history.

The approval contains the same action data, not a nested copy of the complete
`tool_use` event. In particular, it does not repeat `type` inside `action`.

### Outer event contracts

A canonical `tool_use` contains exactly the required fields `type`, `call_id`,
and `action`. New events never contain the legacy `tool` or `input` fields.

A canonical `approval_request` has these fields:

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `type` | string | yes | Always `approval_request` |
| `request_id` | string | yes | Non-empty approval identity echoed in the response |
| `call_id` | string | yes | Non-empty tool invocation identity |
| `action` | object | yes | Valid canonical action, repeated from `tool_use` |
| `options` | array | yes | Available named approval choices; may be empty |
| `auto_approved` | boolean | no | Present only with value `true` |

Every option contains non-empty string `id` and `name` fields plus `kind`, which
is one of `allow_once`, `allow_always`, `reject_once`, or `reject_always`. When
`options` is empty, clients show generic Allow and Deny controls and send a
`behavior` instead of an `option_id`.

There is no approval category field on the wire. `auto_approved` is never sent
as `false` or `null`. An auto-approved request is still followed by the existing
persisted `approval_response`, with `auto: true`, so transcript state and replay
show the decision.

Approval responses are otherwise unchanged. A client sends `request_id` and
either a received `option_id` or `behavior: "allow" | "deny"`; a denial may
also include `message`. The server broadcasts and persists the resolved
`approval_response`. `call_id` is not part of the response because
`request_id` identifies the pending interaction.

## `call_id`

`call_id` identifies one invocation of one tool.

Contract:

- It is a required, non-empty string on `tool_use` and `approval_request`.
- The `tool_use` and `approval_request` for the same invocation have the same
  `call_id`.
- It is stable in persisted scrollback and replay.
- It only needs to be unique within an agent-ui session. Clients must treat it
  as an opaque string.
- It is distinct from `request_id`. `call_id` identifies the tool invocation;
  `request_id` identifies the interactive approval and is echoed in the user's
  response.
- Use the harness's ID when one exists: Claude Code's tool-use ID and Pi's
  `toolCallId`.
- If a future harness supplies no ID, its adapter generates one when it first
  observes the call and retains enough per-turn correlation state to reuse it
  for a later approval event.

Adapters should cache the normalized action by the harness call ID and reuse it
for the approval rather than normalize two provider payloads independently.
This guarantees that both emitted events contain the exact same action even if
the harness presents its permission payload differently.

If an approval unexpectedly has no cached action, the adapter still emits the
approval. It independently normalizes whatever native tool name and arguments
are available in the permission event or other per-turn state. If those data do
not form a valid recognized action, it emits `other` with the native name and an
arguments object, which may be empty when no arguments are recoverable. A cache
miss must never suppress an approval or produce an invalid action.

`call_id` replaces the clients' current heuristic of comparing a tool name and
human-readable summary. Two identical commands in succession are no longer
ambiguous.

## Canonical action union

Every action is a JSON object with a required `kind`. Fields documented as
required are always present with the stated type. Optional fields are omitted
rather than emitted as `null`. Canonical action objects and edit entries are
strict: fields not documented for that kind are rejected. The sole opaque area
is `other.arguments`, whose object may contain arbitrary JSON values.

Clients still need a defensive generic fallback for malformed future events,
but normal rendering must dispatch only on `action.kind`, never on an agent ID,
provider tool name, or provider argument spelling. Recognized actions do not
carry the original provider tool name; that detail ends at the adapter boundary.

### `command`

Run a shell command.

```json
{
  "kind": "command",
  "command": "pytest -q",
  "description": "Run the test suite",
  "timeout_ms": 120000,
  "shell": "bash"
}
```

| Field         | Type   | Required | Meaning |
|---------------|--------|----------|---------|
| `kind`        | string | yes      | Always `command` |
| `command`     | string | yes      | Exact command text |
| `description` | string | no       | Human-readable description supplied by the harness/model |
| `timeout_ms`  | number | no       | Requested timeout in milliseconds |
| `shell`       | string | no       | Shell family when known, e.g. `bash` or `powershell` |

The command must not be trimmed or otherwise rewritten; copy actions need the
exact text.

### `read`

Read a file or a range of a file.

```json
{
  "kind": "read",
  "path": "src/main.py",
  "offset": 120,
  "limit": 80
}
```

| Field    | Type    | Required | Meaning |
|----------|---------|----------|---------|
| `kind`   | string  | yes      | Always `read` |
| `path`   | string  | yes      | Path as presented by the harness |
| `offset` | integer | no       | First requested line, using the harness's value |
| `limit`  | integer | no       | Maximum requested lines |

The adapter does not make paths absolute or resolve them against the working
directory. Displaying exactly what the agent requested is preferable to
silently changing it.

### `edit`

Replace one or more text ranges in one file.

```json
{
  "kind": "edit",
  "path": "src/main.py",
  "edits": [
    {
      "old_text": "host = \"127.0.0.1\"",
      "new_text": "host = settings.host",
      "replace_all": false
    }
  ]
}
```

| Field                 | Type    | Required | Meaning |
|-----------------------|---------|----------|---------|
| `kind`                | string  | yes      | Always `edit` |
| `path`                | string  | yes      | Edited file |
| `edits`               | array   | yes      | Ordered replacement operations; at least one |
| `edits[].old_text`    | string  | yes      | Text to remove/replace |
| `edits[].new_text`    | string  | yes      | Replacement text |
| `edits[].replace_all` | boolean | no       | Whether all matches are replaced |

Using an array makes Claude Code `Edit`, Claude Code `MultiEdit`, and Pi `edit`
share one shape. A single edit is represented by a one-element array. Clients
can render each element as a diff, separated visually when there is more than
one.

### `write`

Create or overwrite a complete file.

```json
{
  "kind": "write",
  "path": "src/generated.py",
  "content": "print(\"hello\")\n"
}
```

| Field     | Type   | Required | Meaning |
|-----------|--------|----------|---------|
| `kind`    | string | yes      | Always `write` |
| `path`    | string | yes      | Written file |
| `content` | string | yes      | Complete new content |

`write` remains distinct from `edit`: clients commonly render it as an
all-additions diff, while `edit` has old and new sides.

### `search`

Search file names or file contents.

```json
{
  "kind": "search",
  "mode": "content",
  "query": "class AgentAdapter",
  "path": "src",
  "glob": "*.py"
}
```

| Field   | Type   | Required | Meaning |
|---------|--------|----------|---------|
| `kind`  | string | yes      | Always `search` |
| `mode`  | string | yes      | `content` or `files` |
| `query` | string | yes      | Text or file pattern being searched for |
| `path`  | string | no       | Search root |
| `glob`  | string | no       | Canonical include filter when supplied |
| `limit` | integer| no       | Requested result limit |

Provider-only output formatting controls are intentionally not exposed. This is
a display protocol, not a lossless executable representation of the original
call.

### `list`

List a directory.

```json
{
  "kind": "list",
  "path": "src",
  "limit": 200
}
```

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `kind` | string | yes | Always `list` |
| `path` | string | no | Directory as presented by the harness |
| `limit` | integer | no | Requested entry limit |

An omitted path means the harness's current working directory; adapters leave it
omitted rather than inventing `.`. Keeping this separate from `search` avoids
inventing a fake query for Pi's `ls` tool.

### `web`

Search the web or fetch a URL.

```json
{
  "kind": "web",
  "operation": "fetch",
  "url": "https://example.com/docs",
  "prompt": "Extract the authentication requirements"
}
```

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `kind` | string | yes | Always `web` |
| `operation` | string | yes | `search` or `fetch` |
| `query` | string | for `search` | Search query; forbidden for `fetch` |
| `url` | string | for `fetch` | URL to fetch; forbidden for `search` |
| `prompt` | string | no | Requested extraction or processing guidance |

Provider-specific domain filters and result formatting controls are omitted.

### `task`

Delegate work to a subagent or named task facility.

```json
{
  "kind": "task",
  "description": "Inspect the authentication flow",
  "prompt": "Find where refresh tokens are persisted"
}
```

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `kind` | string | yes | Always `task` |
| `description` | string | yes | Concise description of the delegated work |
| `prompt` | string | no | Full delegated instructions |
| `agent` | string | no | Requested agent/subagent type |

This action is for rendering delegation consistently, not for exposing every
harness-specific subagent control such as background execution.

### `other`

Fallback for an unknown tool or one whose semantics do not fit a canonical kind.

```json
{
  "kind": "other",
  "name": "CustomDeployTool",
  "arguments": {
    "environment": "staging"
  }
}
```

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `kind` | string | yes | Always `other` |
| `name` | string | yes | Non-empty provider-reported tool name |
| `arguments` | object | yes | Opaque native arguments |

This is the only canonical action that intentionally carries opaque provider
data. Clients render it generically as the name plus pretty-printed arguments.
If the harness omits a usable name or object arguments, use `"tool"` and `{}`
respectively.

A new harness's differently named command or edit is **not** `other`; its adapter
must map it to `command` or `edit`. `other` is for genuinely new semantics. If a
new semantic operation becomes common and deserves specialized rendering, add a
new canonical kind deliberately rather than teaching clients a provider name.

## Initial adapter mappings

The mappings are backed by sanitized events captured from real turns with
Claude Code 2.1.261 and Pi 0.85.1. The fixtures live in
`tests/fixtures/claude_code_2_1_261_tool_events.json` and
`tests/fixtures/pi_0_85_1_tool_events.json`. Claude's `tool_use_id` permission
correlation was also independently recorded in
`docs/askuserquestion_design.md`. Pi's published schemas can be inspected in the
installed `@earendil-works/pi-coding-agent` package. Tests should consume these
fixtures rather than duplicating hand-written provider payloads.

The mapping itself is a design choice: for example, classifying both Claude
`Glob` and Pi `find` as `search`/`files` follows their shared semantics. The
native names and field spellings are protocol facts; their canonical kind is
ours.

### Claude Code

| Claude tool | Canonical projection |
|---|---|
| `Bash` | `command`; copy `command` exactly, copy non-empty optional `description`, copy millisecond `timeout` to `timeout_ms`, set `shell` to `bash` |
| `Read` | `read`; `file_path` → `path`, copy optional `offset` and `limit` |
| `Edit` | `edit`; `file_path` → `path`, create one edit from `old_string`/`new_string`, copy optional `replace_all` |
| `MultiEdit` | `edit`; `file_path` → `path`, map ordered `edits[].old_string`/`new_string` and optional `replace_all`; historical compatibility |
| `Write` | `write`; `file_path` → `path`, copy `content` exactly |
| `Glob` | `search` with mode `files`; `pattern` → `query`, copy optional `path` |
| `Grep` | `search` with mode `content`; `pattern` → `query`, copy optional `path` and `glob`, `head_limit` → `limit` |
| `WebFetch` | `web` with operation `fetch`; copy `url` and optional `prompt` |
| `WebSearch` | `web` with operation `search`; copy `query` |
| `Task` | `task`; same fields as `Agent`; historical/provider alias |
| `Agent` | `task`; copy `description` and optional `prompt`, `subagent_type` → `agent` |
| `NotebookEdit` | `other`; notebook-specific normalization and UI are out of scope |
| unrecognized tool | `other` |

Claude `Grep` controls such as `output_mode`, context formatting, case flags,
and offsets are not canonicalized. `WebSearch` domain controls and `Agent`
controls such as `run_in_background` are also omitted. If omission would leave a
recognized action without one of its required canonical fields, validation
turns the complete native call into `other`.

Claude's `AskUserQuestion` remains a `question` event and must not also emit a
`tool_use`, matching current behavior.

Claude Code 2.1.261 did not advertise `MultiEdit` in its system-init tool list,
so its mapping is retained for old sessions rather than treated as a current
built-in. The same version advertised `Task` but emitted the native name `Agent`
for a forced delegation call; the adapter accepts both names.

The assistant tool block contains the call ID. The following permission request
contains `request.tool_use_id`; the adapter uses that value to retrieve the
cached action and emits it as the approval's `call_id` and `action`.

### Pi

| Pi tool | Canonical projection |
|---|---|
| `bash` | `command`; copy `command` exactly, multiply optional timeout seconds by 1000 for `timeout_ms`, set `shell` to `bash` |
| `powershell` | Same as `bash`, with `shell` set to `powershell` |
| `read` | `read`; copy `path`, optional `offset`, and optional `limit` |
| `edit` | `edit`; copy `path`, map ordered `edits[].oldText`/`newText` |
| `write` | `write`; copy `path` and `content` exactly |
| `grep` | `search` with mode `content`; `pattern` → `query`, copy optional `path`, `glob`, and `limit` |
| `find` | `search` with mode `files`; `pattern` → `query`, copy optional `path` and `limit` |
| `ls` | `list`; copy optional `path` and `limit`; `{}` is a valid current-directory call |
| unrecognized tool | `other` |

Pi `grep.ignoreCase`, `literal`, and `context` are deliberately omitted because
they control execution/output details that have no specialized client UI.

Pi 0.85.1's current `edit` schema already accepts multiple replacements in
`edits`; older Pi sessions may contain its legacy top-level `oldText` / `newText`
shape, which Pi's own `prepareArguments` upgrades before execution. The adapter
should accept both defensively because `tool_execution_start.args` is the
prepared payload in current Pi but old resumed sessions must not produce an
unrenderable card if that behavior changes.

Pi already supplies `toolCallId` on `tool_execution_start`, and the bundled
permission extension carries that same ID in its approval envelope. The adapter
can therefore cache one normalized action under that ID and use it for both
events without changing the extension protocol.

Pi's extension-provided `AskUserQuestion` remains a `question` event and must not
also emit a `tool_use`.

A captured Pi turn with two sibling Bash calls confirmed that approval preflight
is sequential in 0.85.1: Pi emitted the first `tool_execution_start` and waited
for its approval response before emitting the second start and approval. The
fixture preserves that sequence. The adapter still correlates exclusively by
`toolCallId` and must not rely on approvals being adjacent or commands being
different.

## Approval policy

Approval policy remains separate from action rendering:

- `command` checks the existing `auto_approve_command` setting.
- `edit` and `write` check the existing `auto_approve_write` setting.
- Other action kinds are never auto-approved in v1.

This mapping lives in one shared server helper based on canonical `action.kind`,
rather than being independently maintained in each adapter's
provider-tool-name table. Adapters still decide how to gate native tools, but
the server decides which existing user toggle can automatically answer a
normalized approval. The derived setting name is internal and is not sent to
clients. Existing `options`, `auto_approved`, approval responses, and denial
messages are otherwise unchanged.

## Validation and adapter boundary

The canonical projection should be implemented in shared server constructors or
validation helpers, not as ad hoc dictionaries spread through each adapter.
Adapters are responsible for extracting native fields and choosing a canonical
kind; the shared layer is responsible for enforcing the wire shape.

Validation does not coerce field types. Numeric or boolean values are not turned
into strings, numeric strings are not parsed, and fractional numbers are not
rounded. Booleans do not count as numbers even in Python, where `bool` is an
`int` subclass.

Value constraints:

- Empty `write.content` and `edit.edits[].new_text` are valid. They represent an
  empty file and deletion respectively.
- Required commands, paths, queries, URLs, task descriptions, `other.name`, and
  `edit.edits[].old_text` must be non-empty strings. An optional path, such as
  `list.path`, may be omitted but must be non-empty when present.
- Empty optional display strings such as `description`, `prompt`, `shell`,
  `glob`, and `agent` are omitted.
- An absent or `null` native optional field is treated as omitted. If a native
  optional field is present with any other invalid type or value, the
  recognized call is malformed and becomes `other`; it is not coerced or
  silently replaced.
- `offset` and `limit` values are finite integers.
- `timeout_ms` is a finite, non-negative number.

For a recognized native tool that violates these rules or has malformed or
missing required arguments, the adapter emits `other` with the original name
and object arguments rather than an invalid recognized action. This preserves
something useful for the user without weakening the canonical contracts for
normal actions.

Raw arguments needed to answer a harness permission request remain private
adapter state. For example, Claude Code still needs the untouched native input
for `updatedInput`. The canonical action is a UI projection and must not be sent
back to the harness in place of its native arguments.

## Implementation plan

### Shared server layer

Add `src/agent_ui_server/actions.py`. It owns the canonical action models or
validators, constructors that serialize with optional `None` fields omitted,
`other` fallback construction, outer event validation, and the internal
auto-approval-setting lookup. Pydantic v2 strict discriminated models with
`extra="forbid"` are a suitable implementation because Pydantic is already a
server dependency; custom validators are still needed for finite numbers,
non-empty fields, and Python's boolean/integer distinction.

Adapter-specific field extraction remains in `agent.py`: provider names and
spellings must not leak into the shared schema or `main.py`. Each adapter builds
a canonical candidate through shared constructors. If recognized normalization
or validation fails, it calls the shared `other` constructor with the untouched
native name and object arguments. Non-object native arguments become `{}`.

The internal auto-approval helper returns the session setting name, not a wire
field:

```python
command       -> "auto_approve_command"
edit, write   -> "auto_approve_write"
everything else -> None
```

Delete the adapters' provider-name `TOOL_CATEGORIES` tables after this helper is
in use. The Pi extension's separate read-only-tool allowlist remains necessary;
it controls which native calls are gated, not how approvals are displayed.

### Claude Code adapter

For every assistant `tool_use` or `server_tool_use` block other than
`AskUserQuestion`:

1. Read the native name, object input, and block `id`.
2. Use the native ID as `call_id`; generate a UUID only if it is absent or
   empty.
3. Normalize and validate the action once.
4. Cache it under `(agent_ui_session_id, call_id)` and emit canonical
   `tool_use`.

The cache is session-scoped because the wire contract guarantees uniqueness
only within one agent-ui session. Clear its entries when the turn/process ends
or the session is stopped. Keep native input separately wherever Claude needs
it for `updatedInput`.

For a `can_use_tool` permission request, `request_id` remains the control
request's ID and `call_id` is `request.tool_use_id`. Reuse the cached action. On
a cache miss, normalize `request.tool_name` and `request.input`; if that is not
valid, use `other`. If `tool_use_id` itself is absent, generate a non-empty
`call_id` for the approval. Continue suppressing `AskUserQuestion` tool rows and
route those requests through the existing `question` flow unchanged.

### Pi adapter

Keep two per-turn maps keyed by `toolCallId`: untouched native arguments for the
extension response path, and normalized actions for wire events. On
`tool_execution_start`, normalize `toolName` and prepared `args`, cache the
action, and emit canonical `tool_use` with `call_id = toolCallId`. Continue
suppressing the extension-provided `AskUserQuestion` tool row.

The approval envelope already contains `toolCallId` and `toolName`. Keep the
currently generated `perm_<uuid>` as `request_id`, set `call_id` to the envelope
ID, and reuse the cached action. On a miss, normalize the retained native args;
if they are unavailable or invalid, use `other`. The TypeScript extension
protocol does not need to change. Clear both maps at turn cleanup. Do not assume
multiple approvals are simultaneous or adjacent.

### Server orchestration

In `main.py`, persist and broadcast the canonical adapter event unchanged. Do
not add `tool`, `input`, or a category. For an `approval_request`, call the
shared setting lookup on `event["action"]`; when the returned session toggle is
enabled, add `auto_approved: true`, emit the request, and immediately use the
existing approval path to produce the persisted response. Empty `options` does
not affect server resolution by `behavior`.

No database migration is required: scrollback payloads are arbitrary JSON and
new canonical payloads replay unchanged. Existing rows remain legacy JSON.

### Desktop and Android clients

The sibling repositories are `../agent-ui-desktop` and `../agent-ui-android`.
Update each client's transcript model so canonical tool and approval rows retain
`call_id` and `action`. Dispatch summaries and bodies exclusively on
`action.kind`. Remove the provider-name formatting tables and the old
name/summary approval matching. A canonical approval may replace only the
immediately eligible tool row whose `call_id` is exactly equal; render the
approval from its own repeated action.

For `options: []`, show generic Allow and Deny buttons and send `behavior`. For
`auto_approved: true`, render the request as already resolved while waiting for
or replaying its normal `approval_response`. Remove the dim category label.

If an event has no `action`, do not inspect any legacy fields: show an
older-server-version notice and pretty-print the complete event JSON. A present
but malformed/unknown canonical action should likewise fail safely to a generic
raw-JSON view, but without claiming it is necessarily an old event. Legacy
approval JSON is display-only and has no response controls.

### Implementation order

1. Add the shared schema/constructors and fixture-driven server tests.
2. Convert both adapters and server auto-approval orchestration.
3. Convert desktop and Android reducers/renderers and their tests.
4. Replace the legacy contract in `docs/tool_use_event_schema.md` and update any
   conflicting README examples; do not leave two documents claiming different
   current wire formats.
5. Run all three test suites, then deploy all three components together.

## Client behavior

Both clients reduce `tool_use` into a tool row containing `call_id` and `action`.
They render and summarize by `action.kind`:

- `command`: terminal block, exact command copy action.
- `edit`: one or more old/new diffs.
- `write`: all-additions diff.
- `read`, `search`, `list`, `web`, `task`: concise canonical summary; a generic
  detail view is acceptable initially.
- `other`: provider name plus pretty-printed `arguments`.

When an `approval_request` arrives, a client may remove/replace the immediately
preceding tool row with matching `call_id`. It must not compare summaries or
paths. The approval is rendered from its own `action`, not by looking up the
removed row.

No rendering branch may inspect the session's agent ID.

## Compatibility and rollout

New events make a clean cutover: the server emits canonical `call_id` and
`action` only, never duplicated `tool` or `input` compatibility fields.

Existing scrollback may still contain legacy events with no `action`. Clients
show a notice such as **“Legacy event from an older server version”** and
pretty-print the complete raw event JSON. They do not interpret `tool`, `input`,
provider tool names, or any other legacy fields. A legacy approval does not
replace a preceding tool row because it has no reliable `call_id`.

This fallback is deliberately isolated from normal rendering and is not a
second supported protocol. It exists only to keep old transcript data visible;
all new behavior and tests use canonical actions.

The server and both clients are deployed together. The generic fallback keeps
old transcript data visible, but it is not a functional approval UI for an old
server; no turns should be run while only one side of the cutover is deployed.
All canonical events use exact `call_id` matching.

## Testing

### Shared schema/normalization tests

Provider normalization tests load the captured files in `tests/fixtures/`.

- Every canonical kind accepts its documented fields and rejects wrong types,
  unknown fields, non-finite numbers, and the prohibited empty values.
- Empty write content and edit replacement text remain valid.
- Optional fields are omitted rather than serialized as `null`.
- Booleans are rejected for numeric fields; fractional `offset`/`limit` values
  are rejected without rounding.
- Malformed recognized calls become `other` with object arguments.
- `tool_use` requires only `type`, non-empty `call_id`, and valid `action`.
- `approval_request` requires non-empty `request_id`/`call_id`, valid `action`,
  and an options array; it rejects a wire category.
- An empty options array remains valid.
- Auto-approval setting selection derives from `action.kind`.

### Claude Code adapter

- Fixture calls for `Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep`, `Agent`,
  `WebSearch`, and `WebFetch` map to their documented canonical actions.
- A synthetic historical `MultiEdit` fixture maps every edit in order.
- Both native `Task` and `Agent` names map to `task`.
- Provider-only Grep, web, and Agent controls are omitted.
- A tool block's native ID becomes `call_id`.
- A permission request with `request.tool_use_id` reuses the cached action and
  call ID.
- Cache-miss approval normalization and `other` fallback still emit an approval.
- Two identical consecutive commands retain distinct IDs.
- Cache entries are isolated between agent-ui sessions and cleared after turns.
- `AskUserQuestion` remains suppressed from tool events.

### Pi adapter

- Every built-in call in the Pi fixture maps to its documented canonical action.
- Lowercase names and camelCase edit fields produce the same actions as Claude.
- Modern multi-edit and legacy top-level `oldText`/`newText` both normalize.
- `ls` with `{}` becomes `list` with no path or limit.
- `toolCallId` is preserved.
- The approval event reuses the cached action from `tool_execution_start`.
- Cache-miss approval normalization and `other` fallback still emit an approval.
- Bash and PowerShell timeout seconds are converted to milliseconds and differ
  only through canonical `shell` otherwise.
- The captured sequential sibling-approval ordering preserves each distinct ID.
- `AskUserQuestion` remains suppressed from tool events.

### Server orchestration and persistence

- Canonical events persist and replay unchanged and contain no `tool`, `input`,
  or category compatibility fields.
- Auto-approval selects its server-side setting from `action.kind`.
- Auto-approved requests carry `auto_approved: true` and are followed by the
  normal persisted response.
- `tool_use` and `approval_request` for one call carry equal actions and equal
  call IDs but distinct event types/request IDs.

### Desktop and Android

Run the same fixture set through both renderers:

- Claude and Pi edits produce the same diff UI.
- Claude and Pi commands produce the same terminal UI.
- Multi-edit renders every replacement.
- Matching `call_id` replaces a tool card with its approval card.
- Identical consecutive calls with different IDs are not confused.
- `read`, `search`, `list`, `web`, `task`, and `other` produce provider-neutral
  summaries or generic canonical details.
- Empty approval options produce functional generic Allow/Deny controls.
- Auto-approved requests never flash active approval controls and show no
  category label.
- `other` renders its name and pretty-printed arguments.
- A historical event with no `action` shows an older-version notice and its
  complete raw JSON; no legacy fields are interpreted and no heuristic approval
  matching is attempted.

## Out of scope for v1

- Restoring or migrating the removed OpenCode adapter.
- Tool execution result/output events.
- Correlating results with `call_id`.
- A canonical notebook-cell edit action.
- A lossless representation of every provider option.
- Changing `question`, `question_response`, or standalone `bash_input` /
  `bash_output` events.

A future `tool_result` event can reuse `call_id`, but this design does not reserve
its detailed shape.
