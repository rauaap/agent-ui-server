# Design: session token usage tracking

> **Historical note:** OpenCode has since been removed from the shipped server.
> Its protocol research below is retained only as historical context in case the
> old adapter is inspected through version control.

Status: **implemented server-side** (db.py / agent.py / main.py, plus tests) on
the `usage` branch. The Android client is **not** done — nothing renders these
numbers yet. Live verification against both harnesses (see [Live
verification](#live-verification-do-this-before-declaring-done)) has **not** been
run.

This document remains the reference for *why* it works the way it does. It is
self-contained: it includes the protocol findings
(verified empirically against Claude Code **2.1.220** and OpenCode **1.18.9** on
2026-07-29) so you do not have to re-investigate. Every number quoted below was
captured from a real run, not inferred from documentation.

## Problem

The server streams agent turns to the Android app but reports nothing about what
those turns *cost*. The user wants per-session token usage.

Both harnesses already emit usage data on streams the adapters read today, and
both adapters throw it away:

- `ClaudeCodeAdapter` maps the `result` event to `{"type": "done"}`
  (`agent.py:387`), discarding `usage`, `modelUsage`, and `total_cost_usd`.
- `OpenCodeAdapter._classify_update` returns `None` for `usage_update`
  (`agent.py:1143`), and the `session/prompt` result's `usage` is dropped at
  `agent.py:982`.

**Goal:** accumulate per-session token usage and context-window occupancy, persist
it, and push it to the client.

## The core asymmetry (read this before designing anything)

The two harnesses report on **opposite bases**, and this drives the whole design:

|                      | Claude Code                          | OpenCode                                   |
|----------------------|--------------------------------------|--------------------------------------------|
| Natural reporting    | **per-turn delta**                   | **cumulative session total**               |
| Why                  | fresh subprocess per turn; knows only its own turn | maintains its own session store across turns |
| Per-turn totals      | complete, in the `result` event      | **not available** — see the ACP trap below |
| Cumulative totals    | not available — server must sum      | `opencode export <sid>`                    |
| Context occupancy    | derive from last assistant message   | `usage_update.used` / `.size` (direct)     |

Do not try to force both onto one basis inside the adapters. The adapter reports
what its harness actually knows, tags it with a `basis`, and the **server**
normalises. See [Wire contract](#wire-contract).

## Protocol findings — Claude Code (verified)

Invocation probed is the exact one in `ClaudeCodeAdapter.start_turn`:
`claude -p --output-format stream-json --input-format stream-json
--permission-prompt-tool stdio --permission-mode default --verbose`.

### The `result` event carries everything

Emitted once, last, per turn:

```jsonc
{ "type": "result", "subtype": "success",
  "total_cost_usd": 0.044054,
  "usage": { "input_tokens": 4, "output_tokens": 135,
             "cache_creation_input_tokens": 2846, "cache_read_input_tokens": 23214,
             "server_tool_use": { "web_search_requests": 0, "web_fetch_requests": 0 },
             "iterations": [ /* see pitfall 3 */ ] },
  "modelUsage": {
    "claude-opus-5": { "inputTokens": 4, "outputTokens": 135,
                       "cacheReadInputTokens": 23214, "cacheCreationInputTokens": 2846,
                       "costUSD": 0.043462, "contextWindow": 1000000,
                       "maxOutputTokens": 64000, "provider": "firstParty" },
    "claude-haiku-4-5-20251001": { "inputTokens": 527, "outputTokens": 13,
                                   "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                                   "costUSD": 0.000592, "contextWindow": 200000 } },
  "num_turns": 1, "duration_ms": 1543, "duration_api_ms": 1519 }
```

### `result.usage` is per-turn, not cumulative

Verified by running turn 2 with `--resume`: turn 1 cost `$0.059635`, turn 2 cost
`$0.008717`. The server must accumulate.

### Context occupancy is derivable and exact

Turn 1's last assistant message had `cache_read 7370 + cache_creation 5584 =
12954`. Turn 2's `cache_read_input_tokens` was **exactly 12954**. So:

```
context_used = last_assistant.usage.input_tokens
             + last_assistant.usage.cache_read_input_tokens
             + last_assistant.usage.cache_creation_input_tokens
```

The denominator is `result.modelUsage[<main model>].contextWindow`, where
`<main model>` is the `model` field of the `system`/`init` event (first line of
the stream, e.g. `"claude-opus-5"`). The adapter currently ignores `init`
entirely — you will need to capture that field.

> You **cannot** derive context from `result.usage`, because its `cache_read` is
> a *sum across the turn's round-trips* (23214 above), not the final context
> size. Use the last assistant message.

### Pitfalls (things that DON'T work — already tried)

1. **Do not aggregate top-level `usage` — it undercounts.** It reflects the main
   model only. Claude Code dispatches auxiliary work to Haiku, whose 527 in / 13
   out appear *only* in `modelUsage`. Note `total_cost_usd` (0.044054) *does*
   include it: `0.043462 + 0.000592`. **Aggregate `modelUsage` across all
   models.**

2. **Do not sum `assistant` events — they double-count.** One assistant message
   is emitted as *one event per content block*, each carrying the same
   `message.id` and the same full-message `usage`. Captured from a tool-call turn:

   ```
   assistant msg_011CdWATpNdNfTk1PNmTZV51 blocks=['text']      # usage: cc=2706 cr=10254
   assistant msg_011CdWATpNdNfTk1PNmTZV51 blocks=['tool_use']  # usage: cc=2706 cr=10254  <-- SAME
   assistant msg_011CdWAU7fBcdMkkf2Dv1Dqq blocks=['text']      # usage: cc=140  cr=12960
   ```

   Deduping by `message.id` reproduces `result.usage` exactly
   (`in 2+2=4`, `cc 2706+140=2846`, `cr 10254+12960=23214`). Summing naively does not.

3. **Ignore `usage.iterations`.** The turn above made two model calls; `iterations`
   listed one. It is not a reliable per-call ledger.

4. **`output_tokens` on streamed `assistant` events is a placeholder.** Every one
   reported `1` while the real turn total was `135`. Only `result` has true output
   counts. (The *input*-side numbers on assistant events are accurate, which is
   why the context formula above is safe.)

### Bonus: `rate_limit_event` (currently unhandled)

```jsonc
{ "type": "rate_limit_event",
  "rate_limit_info": { "status": "allowed", "rateLimitType": "five_hour",
                       "resetsAt": 1785332400, "overageStatus": "allowed",
                       "overageResetsAt": 1785542400, "isUsingOverage": false } }
```

Out of scope for v1 — see [Out of scope](#out-of-scope-v1).

## Protocol findings — OpenCode (verified)

### The ACP trap — DO NOT use the `session/prompt` result usage

`session/prompt` returns what looks like a per-turn total:

```jsonc
{ "result": { "stopReason": "end_turn",
              "usage": { "inputTokens": 412, "outputTokens": 8, "totalTokens": 9158,
                         "thoughtTokens": 34, "cachedReadTokens": 8704 } } }
```

It reports only the **final model call**. Captured from a turn with 3 tool calls,
93 thought chunks and 7 message chunks:

| metric      | ACP prompt result | actual session total |
|-------------|-------------------|----------------------|
| output      | **8**             | **281**              |
| cache read  | 8,704             | 24,832               |
| reasoning   | 34                | 93                   |

The four assistant messages had `output` 91, 91, 91, 8 — ACP reported the last.

**This is a trap because it is correct when a turn makes no tool calls.** On a
two-turn no-tool session the ACP figures summed to exactly the stored totals
(`input 7731+61=7792`, `output 2+2=4`, `thought 11+11=22`). It will pass casual
testing and undercount by ~35x in real use. Do not build on it, and do not use it
as a fallback.

### Cumulative totals: `opencode export <sessionID>`

Supported CLI subcommand, clean JSON on **stdout** (the `Exporting session: …`
line goes to stderr). Keyed by the id already stored as `agent_session_id`.

```jsonc
{ "info": { "id": "ses_0529eaeb8ffeNUAXBzdv7fcalA",
            "cost": 0,
            "tokens": { "input": 8924, "output": 281, "reasoning": 93,
                        "cache": { "read": 24832, "write": 0 } },
            "model": { "id": "big-pickle", "providerID": "opencode" } },
  "messages": [ /* per-message info.tokens, same shape */ ] }
```

Cross-checked three ways: `info.tokens` matches the `session` row in
`~/.local/share/opencode/opencode.db` and the aggregate from `opencode stats`.
**Cost: 0.74s** per invocation — acceptable once at turn end.

Trade-off to be aware of: `export` serialises the *entire transcript*, so on a
long session you parse megabytes to read four integers. If that becomes a
problem, the identical values are one row of `~/.local/share/opencode/opencode.db`
(`session.tokens_input / tokens_output / tokens_reasoning / tokens_cache_read /
tokens_cache_write / cost`, read-only via `file:…?mode=ro`). That is an internal
schema with a live migrations table — **use `export` for v1**; the DB is the
documented escape hatch, not the default.

### Context occupancy: the `usage_update` notification

Fires **once per turn**, at the end (confirmed across 4 turns, including one with
3 tool calls):

```jsonc
{ "method": "session/update",
  "params": { "sessionId": "ses_…",
              "update": { "sessionUpdate": "usage_update",
                          "used": 9116, "size": 200000,
                          "cost": { "amount": 0, "currency": "USD" } } } }
```

`used` is context occupancy (`= inputTokens + cachedReadTokens` of the final
call), `size` is the context window. Both directly usable — no model lookup
needed. This is the one genuinely useful thing on the ACP stream.

`cost.amount` was `0` throughout because the account is on OpenCode Zen free
models. **Unverified:** whether `cost` is per-turn or cumulative. Do not rely on
it — take cost from `export`'s `info.cost`, which is unambiguously cumulative.

## Wire contract

### Adapter → server (internal `AgentEvent`, never reaches the client)

```jsonc
{ "type": "usage",
  "basis": "turn" | "session",   // REQUIRED: "turn" => add, "session" => replace
  "tokens": { "input": 0, "output": 0, "cache_read": 0,
              "cache_write": 0, "reasoning": 0 },   // absent keys treated as 0
  "cost_usd": 0.044054,                              // optional
  "context": { "used": 13102, "window": 1000000 },   // optional; ALWAYS absolute
  "models": { "claude-opus-5": { … } } }             // optional, stored not displayed
```

### Server → client (WebSocket)

The server normalises; the client never sees `basis` and never accumulates.
**Always absolute session totals:**

```jsonc
{ "type": "usage",
  "tokens": { "input": 8924, "output": 281, "cache_read": 24832,
              "cache_write": 0, "reasoning": 93 },
  "cost_usd": 0.0,
  "context": { "used": 9116, "window": 200000 } }
```

`context` is omitted if never reported. Emitted (a) at the end of each turn, and
(b) once on WebSocket connect, after the scrollback replay and `status`.

Add both to `docs/tool_use_event_schema.md` and the README message-protocol block.

## Implementation

### `db.py`

Add to `sessions` (all via the existing `PRAGMA table_info` migration pattern in
`init()`, alongside the `auto_approve_*` loop):

```
usage_input, usage_output, usage_cache_read,
usage_cache_write, usage_reasoning   INTEGER NOT NULL DEFAULT 0
usage_cost_usd                       REAL    NOT NULL DEFAULT 0
context_used, context_window         INTEGER          NULL      -- NULL = never reported
```

**Remember to add every new column to both `SELECT` lists** —
`get_session()` *and* `list_sessions()`. They are separate literal queries;
missing one makes the session list silently lack usage.

New methods:

```python
def record_usage(self, session_id, *, basis, tokens, cost_usd=None, context=None) -> dict
def usage_snapshot(self, session_id) -> dict
```

`record_usage` does `+=` on the token/cost columns when `basis == "turn"` and `=`
when `basis == "session"`; `context` always replaces. Both return the snapshot in
the server→client shape above. Reject an unknown `basis` with `ValueError`
(mirrors `update_status`'s handling of a bad status).

### `agent.py` — `ClaudeCodeAdapter`

1. Capture `model` from the `system`/`init` event (currently unhandled) into
   per-turn state.
2. On each `assistant` event, record `message.usage` into per-turn state keyed by
   `message.id`, keeping the **last distinct id**. `_events_from_json` is a
   separate method from `start_turn`, so thread a small mutable per-turn state
   dict through it rather than adding instance state — a `dict[str, …]` keyed by
   session id would leak across concurrent turns.
3. On `result`, **before** yielding `done`, yield:
   - `basis: "turn"`
   - `tokens`: summed over **all** entries of `modelUsage`
     (`inputTokens`→`input`, `outputTokens`→`output`,
     `cacheReadInputTokens`→`cache_read`, `cacheCreationInputTokens`→`cache_write`;
     no reasoning field exists — omit)
   - `cost_usd`: `total_cost_usd`
   - `context`: `{used: <formula above from stored last assistant usage>,
     window: modelUsage[model].contextWindow}`, omitted if either is unavailable.

### `agent.py` — `OpenCodeAdapter`

1. In `reader()`, capture `usage_update` (currently dropped by `_classify_update`)
   into `used` / `size` locals. Prefer handling it in `_classify_update` with a new
   `("usage", None, payload)` tuple so it stays unit-testable like the others.
2. After the prompt result resolves and **before** `done`, run
   `opencode export <current_sid>` via `asyncio.create_subprocess_exec` (cwd =
   `session["working_dir"]`, env = `self._build_env()`, `self.executable` as
   argv[0] so `OPENCODE_BIN` is honoured). Parse stdout, take `info.tokens` and
   `info.cost`. Yield `basis: "session"` with `context` from step 1.
3. **On any export failure** (non-zero exit, bad JSON, timeout — use one) yield the
   event with `context` only and no `tokens`. Never fall back to the ACP
   prompt-result usage. Do not fail the turn: usage is best-effort telemetry.
4. A brand-new session that never produced a message is an untested export input —
   guard it.

### `main.py`

- In `run_turn`, handle `event_type == "usage"` **before** the generic persist
  block: call `db.record_usage(...)`, then persist *the returned snapshot* to
  scrollback as a `usage` row and broadcast that snapshot. Do not persist or
  broadcast the raw adapter event — the client must never see `basis`.
- Add `"usage"` to the persisted-event set for the scrollback history.
- In `session_websocket`, after `replay_scrollback` and the `status` send, send
  `db.usage_snapshot(session_id)`. This is deliberately sourced from the session
  row rather than the replay, so a session with more than
  `SCROLLBACK_REPLAY_LIMIT` (200) rows still reports correct totals.

### Android client

Java + plain Views (no Compose). `Api.java` parses events; `SessionActivity.java`
renders the transcript; `SessionSettingsActivity.java` holds per-session detail.
Suggested v1: a context gauge in the session header (`9.1K / 200K`) and token
totals in session settings. Treat a missing `context` as "unknown", not 0.

## Recorded decision: which metrics to surface

You asked for this to be decided rather than left open, so:

**v1 ships context occupancy + token totals as primary; cost is stored but
de-emphasised.**

Rationale — **cost is misleading in this deployment**, on both agents:

- OpenCode is on free models: `cost` is a genuine `0`.
- Claude Code runs on a subscription (`apiKeySource: "none"`), so
  `total_cost_usd` is a *notional* API-equivalent price, not money spent. The
  trivial `"reply with exactly: hi"` turn reported **$0.0596**.

Presenting either as spend invites misreading. Store both (they cost nothing to
record and matter if an API key is ever used); render cost secondary or behind a
toggle. Context occupancy is the metric that answers an actionable question —
*how close am I to compaction* — and it is clean on both agents.

**Override this if you disagree** — it changes only the Android layer, not the
schema or wire contract.

## Tests to add (`tests/test_core.py`)

Follow the existing style: stdlib `unittest`, pure-function parsing tests against
adapter internals, `IsolatedAsyncioTestCase` + a fake adapter for orchestration.

- `ClaudeCodeAdapterParsingTests`: `modelUsage` aggregation sums **both** models
  (use the two-model `result` above; assert output `135 + 13`, not `135`);
  assistant events sharing a `message.id` are deduped to one context reading;
  context formula on the captured turn-1/turn-2 pair yields `12954`.
- `OpenCodeAdapterParsingTests`: `_classify_update` maps `usage_update` to a usage
  tuple with `used`/`size`; an export-failure path yields context-only with no
  `tokens`.
- `DatabaseTests`: `record_usage(basis="turn")` accumulates across two calls;
  `basis="session"` replaces rather than adds; `context` always replaces; unknown
  `basis` raises; a fresh session snapshots as zeros with `context` absent.
- Orchestration: a fake adapter emitting a `usage` event results in one `usage`
  scrollback row and one broadcast, and the broadcast payload contains **no**
  `basis` key.

## Live verification (do this before declaring done)

Unit tests cannot catch a harness protocol change — both findings above came from
live runs. Before declaring done, run one real session per agent:

1. **Claude Code:** two turns, the second forcing a tool call. Assert the
   accumulated `output` equals the sum of the two `result` events' `modelUsage`
   output across all models, and that context occupancy after turn 1 equals turn
   2's `cache_read_input_tokens`.
2. **OpenCode:** one turn with **at least two tool calls** — this is the only way
   to catch a regression into the ACP trap. Assert the reported `output` matches
   `opencode export <sid> | jq .info.tokens.output`. If your number is single
   digits while export says hundreds, you are reading the ACP prompt result.

## Out of scope (v1)

- **`rate_limit_event`** (Claude only) — the five-hour window is arguably the
  truest budget signal on a subscription, and it is currently unhandled. Deferred
  because it is a different axis (account-wide, not per-session) and would need
  its own event and UI. Worth a v2.
- **Per-model breakdown in the UI** — capture `models` into the event, but do not
  render it.
- **Cost for OpenCode per-turn** (`usage_update.cost` semantics unverified).
- **Codex `AppServerAdapter`** — see the README roadmap; it will need its own
  findings section here.
- **Backfilling usage for sessions that predate this feature.** New columns
  default to 0; old sessions report 0 and that is correct-by-omission, not a bug.
