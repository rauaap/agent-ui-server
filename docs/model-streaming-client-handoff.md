# Model streaming — server Phase A handoff

Server implemented, including the owner-approved Pi RPC correction; awaiting
owner review. **No desktop, Android, API, deployment, or commit work is included
in this server worktree.** Clients were separately authorized in their own sessions. The authoritative design remains
[model-streaming-implementation-spec.md](model-streaming-implementation-spec.md).
Existing historical events need no migration. No old-client compatibility mode.

## Transport and identity

Use the existing `/ws/sessions/{session_id}` socket. Each generated assistant
content block gets a unique opaque `stream_id`; each invocation of the adapter
gets one `turn_id`. `ordinal` is zero-based source order within that turn, not
completion/execution order. Retries/new messages get new block identities.
`call_id` is the native invocation ID and correlates approval/question controls.
A draft revision begins at zero and increments once per published update.

All state mutation, persistence/retirement, and outbound enqueueing occur under
the existing session `stream_lock`. The single subscriber writer is unchanged.
Snapshots/preview objects are copied before enqueueing; there are no mutable
registry references in outbound frames. Live queues remain bounded; slow
subscribers disconnect and recover through normal reconnect.

## Exact frames

### Text

```json
{"type":"stream_start","draft":{"stream_id":"s1","turn_id":"t1","ordinal":0,"revision":0,"kind":"text","text":""}}
{"type":"stream_delta","stream_id":"s1","revision":1,"delta":"Hello"}
{"type":"stream_delta","stream_id":"s1","revision":2,"delta":" world"}
{"type":"output","text":"Hello world","stream_id":"s1","turn_id":"t1","ordinal":0,"event_id":124}
```

### Tools and messaging

```json
{"type":"stream_start","draft":{"stream_id":"s2","turn_id":"t1","ordinal":1,"revision":0,"kind":"tool","call_id":"c1","name":"mcp__agent_ui__message_session","arguments_json":"","arguments_preview":{}}}
{"type":"stream_delta","stream_id":"s2","revision":1,"delta":"{\"session_id\":42,\"message\":\"Hello","arguments_preview":{"session_id":42,"message":"Hello"}}
{"type":"stream_delta","stream_id":"s2","revision":2,"delta":" world\"}","arguments_preview":{"session_id":42,"message":"Hello world"}}
{"type":"tool_use","call_id":"c1","action":{"kind":"other","name":"mcp__agent_ui__message_session","arguments":{"session_id":42,"message":"Hello world"}},"stream_id":"s2","turn_id":"t1","ordinal":1,"event_id":125}
```

`name` is the native tool name (MCP prefix included). Final `action` uses the
existing canonical translator, including existing `other` handling for unknown
or malformed calls. These examples do not authorize or report delivery.

Starts can already contain content. Pi's actual RPC `message_start.message`
can supply initial content, but `message_update` carries native deltas only:
Pi 1.1.0 strips both the cumulative `message` and nested SDK `partial` snapshot.
`text_start` has only `contentIndex`; `toolcall_start` adds `id` and `toolName`.
The server accumulates both providers' deltas and parses provisional arguments
with `partial-json-parser`. Pi `arguments_json` contains only received raw
`toolcall_delta` fragments, never a serialization of initial parsed arguments
prepended to later fragments. Claude's empty initial `{}` input similarly is
not prepended to native JSON deltas; meaningful initial Claude input is retained.

The `delta` is the native fragment, not a patch or tool result. Append it to the
local text/raw JSON buffer. JSON encoding escapes Unicode code units so an
incomplete surrogate pair cannot break WebSocket UTF-8, SQLite persistence, or
HTTP scrollback. This preserves decoded fragments and preview values; clients
continue using ordinary JSON decoding. **Replace**, do not recursively merge,
`arguments_preview` on each tool update. Do not parse partial JSON client-side.
Missing preview fields are normal. For messaging, display decoded
`arguments_preview.message` as plain text; resolve a target only when
`session_id` is present. Do not show the same long message twice.

Pi native deltas accumulate into the corresponding server draft, just like
Claude deltas. Reconnect snapshots and final artifacts are authoritative.
Final `text_end`/`toolcall_end` and Claude completed assistant artifacts replace
partial content, not append to it. This is an owner-approved correction to the
original spec's SDK-snapshot assumption, **not a change to the public client
WebSocket contract**. Block-stop/execution-start/final artifacts
are deduplicated by native identities, never by content equality.

### Reconnect

Under one session lock the server queues:

1. Up to 200 latest durable transcript events, ascending event ID.
2. Exactly one frozen `stream_snapshot`, including `drafts:[]` when empty.
3. Existing `input_queue`, then `status` (the end-of-replay marker).
4. Subsequent live frames through the same FIFO writer.

```json
{"type":"stream_snapshot","drafts":[{"stream_id":"s2","turn_id":"t1","ordinal":1,"revision":2,"kind":"tool","call_id":"c1","name":"mcp__agent_ui__message_session","arguments_json":"{\"session_id\":42,\"message\":\"Hello world","arguments_preview":{"session_id":42,"message":"Hello world"}}]}
```

Replace the client's entire active-draft collection with this snapshot. It is
separate from the durable replay page (200 history rows **plus** drafts). The
next update is revision `snapshot.revision + 1`. Reconnect racing completion
gets either snapshot + final or already persisted final + empty snapshot.
Nothing can fall between the replay boundary and subscription.

### Interruption/removal

```json
{"type":"stream_interrupted","draft":{"stream_id":"s2","turn_id":"t1","ordinal":1,"revision":2,"kind":"tool","call_id":"c1","name":"mcp__agent_ui__message_session","arguments_json":"{\"session_id\":42,\"message\":\"Hello world","arguments_preview":{"session_id":42,"message":"Hello world"}},"reason":"cancelled","event_id":126}
{"type":"stream_remove","stream_id":"s3"}
```

`stream_interrupted` is durable and retires the matching active draft after
successful persistence. Reasons: `cancelled`, `error`, `retry`, `incomplete`.
Render it as interrupted and non-executable. Empty/whitespace placeholders can
instead retire with transient `stream_remove`; substantive content is retained.
Completed blocks remain ordinary history if later generation fails.

Partial frames have **no `event_id`** and never advance scrollback cursors.
HTTP scrollback and `read_session` return only durable events, including
interrupted entries, never active drafts. There are no token DB writes, periodic
checkpoints or crash recovery of unfinished drafts.

## Intentional question protocol change

Question-tool argument generation follows the same read-only tool draft and
completed canonical `tool_use` lifecycle as every tool. The later request now
contains the native call ID:

```json
{"type":"question","request_id":"question_1","call_id":"c_question","questions":[{"question":"Proceed?","header":"Confirm","multiSelect":false,"options":[{"label":"Yes","description":"Proceed"},{"label":"No","description":"Stop"}]}],"event_id":127}
```

Correlate by `call_id` into **one** visible card. `request_id` still identifies
the response interaction. Existing question answers and approval options are
unchanged. Approvals already have `call_id`. Tool completion alone creates no
buttons, pending permission, successful execution, delivery receipt or tool
result. Only explicit validated `question`/`approval_request` frames create
controls; auto-approval remains server policy.

Recipient sessions get **no** partial messaging frames or provisional inputs.
Only existing complete validation + explicit approval/policy + session operation
can persist/deliver one complete sender-labelled recipient input. Denial creates
none; busy-recipient queueing is unchanged.

## Client implementation checklist (later phase)

- Keep drafts keyed by `stream_id` separate from durable event IDs/history.
  Dispatch transient frames before history recording; never retain a delta log.
- Apply state changes in order. Ignore duplicate/older revisions; a revision gap
  or unknown draft update means desynchronization: reconnect for history/snapshot.
- Reconcile final `output`/`tool_use` and interrupted rows by `stream_id`, even
  without a local draft. Final events also render standalone during history replay.
- Admit `stream_id`, `turn_id`, `ordinal` in strict final-event validators and
  `call_id` on questions without relaxing canonical action validation.
- Preserve source ordering, active drafts and current metadata during history
  prepends/rebuilds; do not replay stale deltas. Snapshot replaces stale drafts.
- Include the snapshot in offscreen replay before committing at the status marker.
- Tool previews are a separate provisional model, not canonical actions. Preserve
  prefix normalization/target links. No actions from previews/interrupted calls.
- Render plain streaming text or use the existing sanitized Markdown path; never
  introduce an unsanitized incremental renderer. Coalesce redraws without dropping
  state/revision updates; avoid rebuilding the transcript every token.
- Preserve tail-follow only when following; preserve reading position/selection.
  Retire draft Views/DOM nodes and pending redraw callbacks on finalization.
- Desktop touchpoints: store/socket/history/transcript/toolformat/inter-agent and
  scroll-follow. Android is Java: socket dispatch, replay, SessionState, transcript
  construction, tool cards and activity/View lifecycle. Inspect actual code first.

## Server implementation and verification

- `streaming.py`: native block identity/deduplication, delta-only RPC/native
  normalization, partial parser helper, server-owned registry and interruptions.
- `agent.py`: Claude partial flag/events and top-level filtering; Pi block events,
  authoritative artifacts, retry/error boundaries, execution correlation;
  question call IDs. Execution/approval validation and delivery paths unchanged.
- `main.py`: turn identities, locked publication/finalization/interruption,
  snapshot replay, bounded queue allowance, cancellation/deletion/shutdown cleanup.
- `partial-json-parser` is locked in `uv.lock`; Python only, no per-chunk Node
  process. Expected incomplete/malformed preview input is non-operational and
  yields a provisional object; final arguments use the existing strict complete
  translation/validation paths, never repaired previews.
- `tests/test_streaming.py` plus updated native fixtures/core assertions cover
  fragmented escaped/Unicode JSON, field order/large content, nonempty Pi message
  starts, interleaved sibling accumulation, final corrections, both completion orders,
  delayed tool identity, duplicate finals/execution, retries, cancellation/error/
  EOF, empty removals, completed-block survival, DB persistence failure retention,
  frozen reconnect boundaries/200+draft replay, slow subscribers, deletion/shutdown,
  queued-work safety, and approved/denied long messaging through both transports.
- `tests/fixtures/pi_1_1_0_rpc_streaming.json` is generated from deterministic SDK
  events with the installed Pi 1.1.0 `toJsonEvent` serializer, not hand-written SDK
  snapshots or a model run. A full subprocess-adapter/server/WebSocket regression
  replays it. `generate_pi_rpc_streaming.mjs` regenerates it from an explicit path
  to Pi's `dist/modes/json-event.js`, with no model, credentials, or tool execution.

Run with the repository's `uv` environment. Deployment environment variables
must not leak into tests; for example:

```sh
env -u WEB_ROOT -u ALLOWED_HOSTS -u WIREGUARD_IP -u SESSION_DB -u PORT \
  uv run python -m unittest discover -s tests -p test_streaming.py
env -u WEB_ROOT -u ALLOWED_HOSTS -u WIREGUARD_IP -u SESSION_DB -u PORT \
  uv run python -m unittest discover -s tests
```

Verification after the Pi RPC correction: focused suite **22 passed**; full
server suite **486 tests, 3 skipped**, successful. `git diff --check` passed. The full suite also emitted
non-failing subprocess/resource-cleanup warnings; no streaming test failed.
The sandbox cannot see the project's Python 3.11 interpreter, so the documented
`uv` commands were run in the server environment with explicit approval and the
above deployment variables removed.

Real model/manual client checks remain for separately approved later phases.
No live sessions, recipients, side-effecting model probes or deployed services
were used to obtain fixtures for this change.
