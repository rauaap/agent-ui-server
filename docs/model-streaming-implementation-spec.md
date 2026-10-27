# Model output streaming — implementation specification

Status: server implemented; client work separately authorized. This specifies both server and clients. **The first implementation phase is server only. Do not change desktop, Android, or the API repository in that phase. Stop after server implementation/tests and report the contract for review.**

Owner-approved correction after Phase A: installed Pi 1.1.0 JSON/RPC explicitly strips SDK `partial` snapshots from `message_update`. The server must accumulate Pi RPC deltas and parse provisional arguments server-side, not expect the SDK snapshot shape. This corrects the original Pi snapshot assumption; the public WebSocket protocol, replay, persistence, approval, and client contracts below are unchanged.

Related diagrams: `docs/model-streaming-flow.md` and `docs/model-streaming-flow.html`. This specification is authoritative where the diagrams are illustrative.

## 1. Goals and agreed architecture

Expose ordinary assistant text and tool-call argument generation live, for both Pi and Claude Code. A long `message_session` argument must be visible as a growing, read-only message card in the sender's session, without delivering partial input to the recipient.

The architecture agreed with the owner:

- Normalized server state is the **accumulated current draft**, not a log of deltas.
- Claude supplies deltas: accumulate them into server state and forward the current delta.
- Pi RPC supplies native deltas, not SDK partial-message snapshots: accumulate text/raw argument fragments, parse provisional arguments server-side, and forward the current delta.
- Server state additionally tracks identity, ordering and lifecycle; final authoritative content replaces the draft without changing its identity.
- Live partial updates are not database events. Complete blocks/calls become normal persisted transcript events. Orderly interruption preserves unfinished drafts explicitly as interrupted.
- Existing per-session `stream_locks`, per-subscriber outbound FIFO queues, and single writer tasks provide ordering. Use them; do not add another delivery transport, replay subsystem, or parallel socket writer.
- Reconnect queues persisted history, a frozen snapshot of current drafts, existing pending-input metadata, and the existing status replay marker, then receives subsequent live updates.
- Tool drafts have **no actionable controls**. Complete arguments alone do not authorize execution or make an approval pending. Only explicit server approval/question requests expose controls.

## 2. Scope and non-goals

Included: text blocks, tool argument JSON and structured previews, finalization, approvals/questions correlation, inter-agent message composition, orderly interruption, retries, reconnect, desktop and Android rendering contracts, tests and documentation.

Not included: thinking/reasoning display, speculative tool execution, partial prompt delivery, database writes per token, draft checkpointing, surviving server crashes with unfinished drafts, a new SSE/NDJSON endpoint, binary/image streaming, or general streaming of tool execution output/results. Existing tool execution/result behavior remains unchanged. Do not invent successful execution/delivery states from completed arguments.

Only render the session's own assistant output. Claude child/subagent events with `parent_tool_use_id` must not be mixed into top-level drafts; preserve current top-level behavior and explicitly filter/route those events. Adding a nested subagent transcript is outside this feature.

No backwards-compatibility mode, protocol negotiation, old-client fallbacks, or dependency failure fallbacks. Server/client rollout is staged for review, not a requirement to support old clients. Existing completed historical transcript events remain renderable under their existing semantics; no data migration is needed solely to add optional streaming metadata.

## 3. Existing implementation touchpoints

Server:

- `src/agent_ui_server/agent.py`: `ClaudeCodeAdapter.start_turn`, `_events_from_json`, `_assistant_events`; Pi RPC `handle_message`, `text_buf`, `flush_text`, tool execution dispatch and approval/question correlation.
- `src/agent_ui_server/main.py`: `run_turn`, `commit_stream`, `session_websocket`, `Subscriber`, `enqueue_for_subscribers`, `write_subscriber`, session stop/deletion/shutdown paths.
- `src/agent_ui_server/actions.py`, `tool_actions.py`: complete, strict canonical tool actions. Do not weaken final validators for previews.
- `src/agent_ui_server/session_tools.py`: complete validated messaging arguments, mandatory approval, atomic recipient input submission.
- `src/agent_ui_server/db.py`: append-only JSON scrollback, current pagination and recipient input queues.
- Tests: `tests/test_adapter_protocol.py`, `test_agent_failures.py`, `test_core.py`, `test_scrollback.py`, `test_actions.py`, `test_session_tools.py`, and approval/question tests.

Desktop (later phase): `js/store.js`, `js/socket.js`, `js/transcript-history.js`, `js/render/transcript.js`, `js/render/toolformat.js`, `js/render/markdown.js`, `js/tools.js`, `js/inter-agent.js`, `js/render/scroll-follow.js`, associated tests.

Android (later phase): Java sources under `app/src/main/java/com/agentui/app/`, especially `SessionActivity.java` socket dispatch/rendering, replay buffering, transcript reconstruction, scrollback acceptance and tool-card helpers. Inspect actual classes before editing; this is a Java client, not Kotlin.

## 4. Identity, ordering and draft state

Use an opaque `stream_id` unique for the lifetime of the server's transcript (UUID per draft is sufficient). It identifies **one assistant content block**, not a whole user turn. Use native message/block/call identities within each adapter to map native events to that ID. Do not identify blocks by text or reuse block index alone across messages/retries.

Each turn gets an opaque `turn_id`. Each draft has a monotonically assigned `ordinal` within the turn, determining source order. Preserve source content order even if multiple tool calls execute concurrently. Every new attempt/message after a retry has distinct identities unless the harness explicitly updates the same source message.

Normalized draft:

```
stream_id: string
turn_id: string
ordinal: integer >= 0
revision: integer >= 0
kind: "text" | "tool"
text: string                         # text drafts only
call_id: string                      # tool drafts; native call correlation ID
name: string                         # native tool name, including MCP prefix where applicable
arguments_json: string              # accumulated raw JSON representation for tools
arguments_preview: object           # provisional parsed snapshot; not canonical/validated
```

Tool start requires a native tool name and call ID. If a provider exposes them after an initial empty start, retain native bookkeeping until available; do not fabricate a tool identity or emit an actionable event early. Empty text blocks need not become persisted empty output rows.

Active draft registry is per session, with explicit source ordering. Native parsing/bookkeeping can live in adapters; **the server-owned replayable draft registry must be mutated in the same `stream_lock` critical section as publication**, not independently in an adapter callback. Adapter events should carry the native delta plus the accumulated content/preview information needed for that mutation. Do not enqueue a mutable reference to the registry.

Pi RPC `message_update` omits both the top-level cumulative `message` and nested `assistantMessageEvent.partial`; do not read either. Use `contentIndex` within the current native message to update only the relevant registered block. `toolcall_start` supplies native `id` and `toolName`; `text_start` supplies the index only. Accumulate text and raw `toolcall_delta` fragments exactly, and derive the provisional structured preview with the same server-side partial parser as Claude. Preserve any initial content supplied by the actual `message_start.message`, but do not serialize its parsed tool arguments as a raw JSON prefix. Preserve block IDs and do not duplicate finalized sibling blocks.

## 5. Public WebSocket protocol

No new socket endpoint. Existing completed frames and replay cursor semantics remain. Below is the exact new contract; implementation may use different internal event classes but must document and test these wire frames.

### 5.1 Start: transient

```json
{"type":"stream_start","draft":{"stream_id":"s1","turn_id":"t1","ordinal":0,"revision":0,"kind":"text","text":""}}
```

A tool draft has `kind:"tool"`, `call_id`, `name`, `arguments_json`, and `arguments_preview` instead of `text`. Start may include already available initial content. It has no database `event_id`.

### 5.2 Update: transient

Text:

```json
{"type":"stream_delta","stream_id":"s1","revision":1,"delta":"Hello"}
```

Tool:

```json
{"type":"stream_delta","stream_id":"s2","revision":1,"delta":"{\"session_id\":42,\"message\":\"Hello","arguments_preview":{"session_id":42,"message":"Hello"}}
```

`delta` is the native raw text/argument JSON fragment, not a JSON patch and not an execution result. Clients append it to their corresponding local text/raw-JSON buffer. Tool updates additionally carry the current provisional structured object for display. Send this object as a replacement, not a recursively merged patch; incomplete fields can disappear or change type. Clients must not parse partial JSON themselves.

Revisions increase once for every published content update to that draft. Frames may be batched into contiguous updates only if revision semantics remain explicit and consistent; initial implementation should favor correctness and avoid timer/coalescing complexity. Clients may batch actual DOM/View redraws independently. Do not send both a complete growing text snapshot and its delta on every ordinary update.

### 5.3 Reconnect snapshot: transient

```json
{"type":"stream_snapshot","drafts":[{"stream_id":"s1","turn_id":"t1","ordinal":0,"revision":7,"kind":"text","text":"Hello so far"}]}
```

Exactly one frame per initial subscription, including `drafts:[]` when empty. Tool drafts use the same full shape as section 4. This replaces the client's entire active-draft collection; it is not appended as transcript history. Snapshots are frozen/deep-copied before enqueueing.

The frame has no persisted cursor. Client expects the next update for each restored draft to have revision `snapshot.revision + 1`. If the native provider's final content differs from its partial representation, the final authoritative frame replaces it; do not try to repair final content with synthesized textual patches.

### 5.4 Completion: persisted existing event types

Text blocks become:

```json
{"type":"output","text":"Hello world","stream_id":"s1","turn_id":"t1","ordinal":0,"event_id":124}
```

Tool calls become the existing canonical `tool_use` event, plus `stream_id`, `turn_id`, `ordinal`. Keep existing `call_id` and canonical `action` fields. Final tool arguments must be complete, and canonical translation must behave as it does today (including established unknown-tool handling). Never run strict canonical validation against a partial preview.

Both final events **replace the associated provisional card/block**. They also work as standalone history entries when there is no draft, e.g. history replay. A final event means generation finished, not that a tool executed successfully. Atomically commit final event, remove the active draft, and enqueue the final frame under the session lock. Do not remove a draft before successful persistence.

Each native content block produces at most one final transcript event, even if completed assistant messages, content-block end, and tool execution start all describe the same tool call. Deduplication must live at the adapter's native identity mapping, not by comparing content.

Intentional uniformity for question tools: their argument generation can use the same read-only tool preview and completed `tool_use` lifecycle. The later `question` event supplies the actual controls. Update the current special-case suppression of question-tool tool-use events as needed to achieve a single correlated card in the eventual clients; do not create two independently visible question/tool cards. Correlate `question` with `call_id`, adding this field to persisted question frames where currently absent. Existing approval events already carry `call_id`. Document this intentional protocol change and update relevant server tests. No question controls may be constructed from streamed arguments alone.

### 5.5 Interrupted draft: persisted new transcript event

```json
{"type":"stream_interrupted","draft":{"stream_id":"s2","turn_id":"t1","ordinal":1,"revision":5,"kind":"tool","call_id":"c1","name":"message_session","arguments_json":"...","arguments_preview":{"message":"unfinished"}},"reason":"cancelled","event_id":125}
```

`reason` is one of `cancelled`, `error`, `retry`, `incomplete`. This preserves the visible accumulated draft as a non-executable interrupted entry. Do not represent an incomplete tool as a completed canonical action or a pending approval. A text draft retains its text. Retire the draft and publish this frame in the same critical section as persistence.

If a started draft has no substantive content, it may be removed without a durable history row using transient `stream_remove` with `stream_id`; test this behavior. This frame is also how clients clear an empty placeholder. Do not use removal to conceal substantive output on error/retry.

## 6. Adapter behavior

### Claude Code

- Add `--include-partial-messages` alongside existing print/verbose/input/output stream-json options.
- Parse `stream_event.event` by native message ID and content-block index. Native `message_start` starts a new source message; `content_block_start` identifies text/tool blocks; text deltas and `input_json_delta.partial_json` update them.
- Tool-use start supplies name/call ID, frequently with `{}` input. Do not prepend a literal `{}` to subsequent JSON fragments; empty initial input is metadata, not a serialized prefix. Handle meaningful initial input correctly if present.
- Accumulate text/raw JSON deltas; maintain provisional parsed arguments server-side.
- Completed assistant content is authoritative. In an observed real probe the completed `assistant` tool message arrived **before** its matching `content_block_stop`; do not assume the reverse or finalize twice.
- Existing `assistant` handling must not emit duplicates of streamed final blocks. Preserve final non-streamed content types according to their existing semantics.
- Ignore reasoning and avoid accidentally persisting its contents as text.
- Maintain existing permission/MCP transport dispatch, token accounting and tool translation. Tool generation completion is distinct from permission requests and tool results.
- EOF/result/stop/error paths must terminate unfinished drafts deterministically. Failed attempts are not merged into later retries.

### Pi

- Handle `message_update.assistantMessageEvent` text start/delta/end and toolcall start/delta/end, not just `text_delta` buffering until `text_end`.
- Normalize the actual JSON/RPC wire shape, not the in-process SDK shape. On text/tool start create the corresponding block; tool start fields are `id` and `toolName`. On delta accumulate the native fragment and publish it; parse tool previews server-side. There is no `partial` snapshot on RPC updates. Preserve any initial content already present in `message_start.message`.
- Treat `text_end.content` and `toolcall_end.toolCall` as authoritative completed content. Final message artifacts are another authoritative source, not permission to duplicate blocks.
- The current `tool_execution_start` path must still populate execution/approval correlation maps, but must not emit a second `tool_use` for an already finalized call. Completion should be visible even before actual execution starts.
- Replace current `text_buf`/`flush_text` semantics carefully: a tool execution event must not flush an already persisted text block again. No substantive unfinished text may be silently dropped on error.
- Preserve existing extension-mediated server tools, question dispatch, concurrent tool execution, `agent_settled` terminal semantics, automatic retries and compaction.
- Handle `message_end` with error/aborted reason, retry transitions and terminal settlement. Close unfinished drafts from a failed attempt as interrupted before the next attempt. Completed blocks from an earlier attempt remain ordinary history.

## 7. Incremental argument parsing and previews

Keep raw fragments exactly as received. JSON strings can span arbitrary chunk boundaries, including escaped quotes, backslashes, Unicode escapes/surrogates and newlines. JSON encoding of live frames, persisted payloads, and HTTP scrollback must escape incomplete surrogate code units so a provisional string cannot fail UTF-8 encoding. Do not drop or repair native fragments. Never parse by regex-extracting `message` or repeatedly adding closing quotes/braces with ad hoc heuristics.

Use a small, explicit incremental/partial JSON parser or a verified existing dependency/helper. Assess the installed Pi helper if suitable, but Python server code must not spawn Node per chunk. A small Python dependency is acceptable if it is maintained, deterministic, and tested against this contract; document it in the server dependency configuration. Ordinary parse incompleteness is expected, not an operational failure or a reason to log tracebacks per token. Malformed final data must surface through established errors/unknown-tool handling, not be silently repaired.

Both Claude and Pi RPC require server-side provisional argument parsing; Pi's provider-parsed snapshots exist only in the in-process SDK, not on this transport. Tool previews can have missing/partial fields and must never enter canonical action validators, approval auto-policy, or execution. A field may arrive in any order; do not promise a destination header before its session ID exists. Final native arguments replace all provisional data and pass through existing complete-call validation.

## 8. Persistence, lifecycle and replay

No schema migration is required: store final-event streaming metadata and `stream_interrupted` JSON in existing scrollback. Existing final event IDs remain the only history cursors; transient frames never affect pagination, deduplication IDs or `read_session` results.

`read_session` and HTTP scrollback continue returning durable events only, including interrupted entries. They do not expose live drafts. No database row per delta and no periodic checkpoint in this feature.

For connection initialization, while holding the existing per-session lock:

1. Read/enqueue the latest `SCROLLBACK_REPLAY_LIMIT` persisted rows (currently 200).
2. Enqueue one frozen `stream_snapshot`, sorted by turn/source order.
3. Enqueue existing `input_queue` and `status` frames. Preserve `status` as the end-of-replay marker used by clients.
4. Register subscriber and start its single writer as today.

Live state mutation plus delta enqueueing must acquire the same lock. Completion persistence plus draft retirement plus final enqueueing must also acquire it. No awaits for subprocess/network I/O inside this critical section. The existing synchronous DB operations follow current project practice.

The snapshot is separate from the history page limit: this is 200 durable events **plus** current drafts, not a special 199+1 rule. Increase initial outbound capacity for the added snapshot frame; keep live queue capacity/bounded slow-subscriber retirement behavior. Do not add unbounded backlogs. A slow subscriber is disconnected according to current behavior and restores the latest draft on reconnect.

Stop/cancellation, exceptions, process EOF and terminal `done` finalize active drafts before idle/status and before starting another queued turn. Handle `asyncio.CancelledError` explicitly: it is not caught by `except Exception`. Do not permit automatic shipping of queued work after cancellation/failure. Session deletion clears draft state without resurrecting DB rows after deletion; normal shutdown should finalize drafts while the DB is available where the existing lifecycle permits it. A hard crash loses unfinished drafts by design.

Empty/whitespace output follows current transcript policy, but an already visible substantive draft must never vanish merely because finalization/error paths bypass normal text-end.

## 9. Approvals, questions and inter-agent messaging

- Drafts are always display-only. No allow/deny/always/answer/run buttons or optimistic execution states.
- After completed validated `tool_use`, show a non-actionable completed tool card until an actual approval/question request arrives. That request associates via `call_id`; controls come from its validated options/questions.
- Auto-approval remains server policy and existing event sequencing; never infer it client-side from settings.
- A tool may be denied, fail before execution, or execute concurrently with other tools. Draft finalization does not mark it successful.
- `message_session`/`start_session` stream their `message` argument only to sender subscribers. Recipient session receives no draft or token updates.
- Only existing complete argument validation + approval + operation may persist and deliver/queue the recipient input. Sender identity labeling, busy-recipient queueing and persisted input IDs remain unchanged.
- Long message preview uses the decoded provisional `message` string. Header resolves target from provisional `session_id` when present; missing target says composing, not invalid. Unknown/malformed target is judged at final validation/current existing policy.
- Do not add delivery receipts/statuses without an actual server operation outcome and a separately documented result event. This feature does not make tool results part of client transcript automatically.

## 10. Desktop implementation requirements — later phase

- Add active drafts keyed by `stream_id`, separate from durable `history.events`/`history.ids`. Never store every delta in the reducer's replay log.
- Dispatch transient frames before generic durable-history recording. Maintain revision and raw text/JSON per draft, replacing preview objects on tool deltas. Final events reconcile by `stream_id`, and approvals/questions by `call_id`.
- Reconnect's snapshot replaces draft collection. Existing offscreen replay behavior must build final history + drafts before the status replay marker becomes visible; clear stale drafts absent from the snapshot.
- History prepend/rebuild must preserve current active drafts and current metadata, without replaying obsolete deltas or duplicating finalized rows. Existing strict `exactEventKeys`/canonical validators must admit specified final metadata and question correlation fields without accepting arbitrary malformed actions.
- Preserve source order. Draft IDs and completed DB IDs are different identity domains, not interchangeable.
- Text updates reuse current assistant bubble/block rendering. It is acceptable to show plain text while streaming and render Markdown at finalization; if incremental Markdown is used, sanitize exactly as completed output and handle incomplete fences/links safely.
- Tool previews use a dedicated provisional model, not `isCanonicalAction`. Reuse final card visual language. Known tools can show path, command, emerging write/edit content; unknown tools show title and expandable raw arguments.
- For messaging tools, normalize the MCP prefix as current helpers do, resolve target names, and show growing decoded `message` plain text. Do not duplicate the entire long message in multiple always-visible regions.
- Correlate question-tool completed card with question request into a single visible card. Never create controls from arguments; never display independent duplicate question/tool cards.
- Interrupted output stays visible and is clearly labeled, with no action controls. Complete callbacks must not leave stale draft DOM nodes.
- Batch render work approximately once per animation frame or short 30–100 ms interval; reducer/state application remains ordered. Avoid rebuilding all transcript DOM on each token. Follow tail only while already following; preserve scroll position when reading older content and when prepending history.
- On revision gap or update for unknown draft, treat it as a protocol/state desynchronization: close/reconnect to obtain authoritative history + snapshot; do not append unknown deltas or silently guess. Ignore exact duplicate/older draft revisions. Final snapshots are authoritative even if no matching draft is present.

## 11. Android implementation requirements — later phase

Apply the same protocol, identity and UI rules in the native client:

- Keep draft models and draft View references separate from durable scrollback/replay entries. Transient frames must bypass durable history acceptance and must not become hundreds of stored events.
- Handle `stream_snapshot` inside current offscreen reconnect replay. Keep `status` as commit marker so the swap includes drafts. Old draft Views must not leak into the new replay tree.
- Apply deltas on the ordered event path; marshal/coalesce actual View updates onto the UI thread without violating finalization order. Cancel pending redraw callbacks on finalization/activity destruction.
- Preserve draft state during in-app transcript reconstruction and history prepend, but restore from server snapshot on reconnect/activity resubscription rather than replaying locally stored token events.
- Tool preview is plain text/selectable where appropriate and read-only. Final approval/question controls are created only by explicit request frames and correlated with completed tool cards.
- Decode/render messaging preview from `arguments_preview.message`. Native tool name prefix normalization and target links retain current semantics.
- Avoid rebuilding large text/tool Views per token; preserve selection, tail-follow and reading position to the extent current native UI permits. Bound retained state via finalized history policy, not by truncating the authoritative server draft silently.
- Update strict event classification/validation, interrupted row rendering, call correlation, duplicate handling and replay filtering, including test helpers and any shared cached SessionState models.
- Same desynchronization/reconnect and stale revision behavior as desktop.

## 12. Testing and acceptance criteria

### Server unit/protocol fixtures

Test both adapters using deterministic captured or synthetic native event fixtures:

1. Several text deltas produce correct current draft and one final output row, no token rows.
2. Pi delta-only RPC events generated by the installed `toJsonEvent` serializer: no SDK snapshots, native tool identity, initial nonempty `message_start` content, interleaved sibling blocks, exact raw argument accumulation and server-parsed previews; final authoritative correction wins.
3. Claude raw JSON accumulation, escaped strings/Unicode split across chunks, interleaved block indices, multiple assistant messages in one turn.
4. Claude completed `assistant` arriving before block-stop and the reverse both finalize exactly once.
5. Pi `toolcall_end` followed by `tool_execution_start` produces one tool_use while preserving approval correlation.
6. Tool argument field order variation, incomplete parser input, malformed final input, unknown tools, tools with empty arguments, and large `message`/write contents.
7. Question previews never create requests; explicit question/approval remains the only controls contract. Question `call_id` is complete and matches finalized tool_use.
8. No reasoning leakage or child/subagent output mixed into the top-level transcript.
9. Text/tool interruptions on cancellation, error, EOF and retry preserve substantive content with correct reason, clear registry, and never execute incomplete calls. Empty drafts clear safely.
10. Completed blocks survive a later failure without being persisted twice or incorrectly marked interrupted.

### Server integration/ordering

11. Reconnect during generation sees durable history, exact frozen snapshot, queue/status marker, then next delta. Snapshot must not mutate after enqueue.
12. Force completion during reconnect setup: client gets either draft snapshot followed by final event or already persisted final event, never a gap or two final copies.
13. Multiple subscribers receive equivalent ordered data. One slow subscriber overflows/retire behavior without blocking others; reconnect restores latest state.
14. Initial replay capacity accommodates the extra snapshot; no drafts is an explicit empty snapshot.
15. Transient events never change DB row count/cursor, and HTTP scrollback/read_session do not return drafts. Final/interrupted events paginate normally.
16. Stop, deletion, shutdown and queued-turn handoff leave no active draft state/callbacks behind and do not write to deleted sessions.
17. Long messaging tool: no recipient input exists during streaming or before approval; deny creates none; allow submits one complete input with sender identity; busy target queues exactly once.
18. Preserve all existing adapter/approval/image/usage/message queue tests, updating intentional event-shape changes rather than weakening assertions.

### Client tests — later phase

19. Reducer/model tests for start/delta/snapshot/final/interrupted/remove, revisions, duplicate finals, draft-to-final replacement, multiple blocks, question/tool coalescing.
20. Reconnect and history prepend retain current draft exactly once and do not retain token events in durable replay storage.
21. Messaging preview grows without controls, final request enables only specified controls, and cancellation/denial leaves no actionable stale cards.
22. Long output performance/scroll tests, background/foreground native activity lifecycle, incomplete Markdown/JSON, text selection, reconnect while finalization is pending.

### Manual verification

After deterministic tests pass, use real Pi and Claude sessions to stream text, a long write/edit, a long inter-agent message, a denied message, and stop mid-tool. Use disposable files/recipient sessions with owner approval where required. Verify reconnect mid-generation in both eventual clients. Do not message real agents or execute side-effecting tools merely to obtain fixtures without approval.

## 13. Implementation phases and handoff

**Phase A — session 553: server only**

1. Inspect current tree and project instructions; report existing dirty/untracked files and do not overwrite unrelated work.
2. Implement draft/state/protocol types and partial parsing; adapter normalization; finalization/deduplication; lock/queue replay integration; interruption cleanup; server docs and deterministic tests.
3. Update the server README and a precise client handoff document with wire examples, question correlation change, final metadata and replay semantics. Keep this spec/diagrams intact unless correcting a discovered inconsistency explicitly documented in the report.
4. Run targeted tests and the full server suite using the repository's documented environment. Report actual commands/results and any access/environment limitation.
5. Stop for owner review. **Do not implement clients, change the API repo, deploy/restart services, or create a Git commit unless separately asked.** If the contract needs a material departure from this spec, ask the owner before implementing that departure.
6. Report files changed, protocol examples, tests, unresolved issues and client follow-up. Send the requesting session/owner a concise completion summary.

**Phase B — after owner review only:** implement desktop and Android against the approved contract; update affected shared API documentation/types if the API repository actually owns relevant definitions. No compatibility layers for older clients.

**Phase C — after client implementation/review:** end-to-end verification and deployment as separately authorized.
