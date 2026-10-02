# Message queue contract and client handoff

Ordinary user and agent messages share one persistent, server-owned queue. Sending
while `running` or `awaiting_approval` is accepted immediately, not rejected and
not steered into the current harness run. The server formats every message with
its sender label (including single user messages) and joins each pending batch
into one prompt, for one new harness turn. No native harness batching is used.

## Sending

The existing WebSocket request remains `{"type":"input","text":"..."}`.
`POST /sessions/{id}/turn` still accepts `{"prompt":"..."}` and returns HTTP 202,
now with `{"status":"accepted","message_id":123}`. Acceptance is not delivery
or completion. `message_session` still returns the persisted input ID immediately.

Clients must remove their busy-session rejection for ordinary messages. Keep
archived, disconnected, and sandbox-setting-save guards. Bash commands are
unchanged and do not use this queue.

## Accepted input (persisted and broadcast immediately)

```json
{
  "type": "input",
  "message_id": 123,
  "text": "Check Android too.",
  "source": {"type": "user"},
  "delivery": "queued"
}
```

For agent inputs, `source` is `{"type":"agent","session_id":7}`. The server
sets source identity; clients cannot supply it. Text is stored unchanged.

For REST scrollback, this is the usual row (`id`, `session_id`, `ts`, `type`,
`payload`); `message_id` is the row's `id`, not duplicated in its payload. Live
and replayed WebSocket input frames both include `message_id`.

Classify an input as queued by `delivery === "queued"`, **not** by the presence
of `message_id`. Every WebSocket input frame has an integer `message_id`, including
replayed historical inputs accepted before queueing existed. Those persisted
historical inputs have no `delivery` field and belong directly in the transcript;
this is historical-data rendering, not support for older servers.

**Do not append queued inputs to the conversation transcript or reset the active
assistant bubble.** Add them to a separately visible pending list keyed by
`message_id`, preserving acceptance order and sender attribution. User-originated
input echoes still update/deduplicate composer history; agent inputs do not.
Do not add history again when shipping. No optimistic transcript append on Send.

## Shipped batch (persisted at the delivery boundary)

```json
{
  "type": "inputs_shipped",
  "messages": [
    {
      "message_id": 123,
      "text": "Check Android too.",
      "source": {"type":"user"},
      "delivery": "shipped"
    },
    {
      "message_id": 124,
      "text": "The API changes are ready.",
      "source": {"type":"agent","session_id":7},
      "delivery": "shipped"
    }
  ]
}
```

Remove these IDs from the pending list and append **one transcript row per
message**, in array order, using existing user/agent attribution and sender links.
Reset the assistant bubble here, before subsequent output. Shipping does not
update composer history. The event has full message contents: replay must work
even when original acceptance events have fallen outside the replay window.
Deduplicate by `message_id` if your client retains state across reconnects.

Scrollback is append-only: the original `input` is the acceptance event;
`inputs_shipped` records the transition for the same message IDs, not new message
identities. Its REST wrapper has its own event ID, used only for event pagination.
Rendering the conversation from replay must place messages at the shipment
boundary, not at the original acceptance position.

Shipped means handed off for a harness turn; it does not mean the model accepted,
read, processed, or answered the messages. Do not automatically resend shipped
messages after crashes or errors.

## Reconnect and REST reads

WebSocket initialization, atomically ordered with live publications:

1. Up to 200 persisted scrollback events, oldest first.
2. `{"type":"input_queue","messages":[...]}` — authoritative replacement of
   the entire current pending list. Items have the same shape as accepted inputs
   without `type`: `message_id`, `text`, `source`, `delivery:"queued"`.
3. Current `status`.
4. Current `archived` state.

The queue snapshot includes **all** pending inputs, even when their acceptance
records are outside the replay window. Replace, don't merge, the pending list
when processing this snapshot. It is not a transcript event or history echo.

`GET /sessions/{id}/scrollback` (and `read_session`) retains `messages`,
`next_cursor`, and `has_more`, and also returns `queued_messages`, an authoritative
current pending snapshot independent of the page cursor/limit.

## Turn lifecycle

- Idle submission: accept input, ship all pending inputs, publish `running`, start
  one harness turn.
- Busy submission: accept input only; no status change and no interruption.
- Successful completion: ship all currently pending inputs into one next turn.
  There is **no intermediate idle status** when a next batch exists.
- Anything accepted after a batch is claimed stays pending for a later turn.
- Questions and approvals must still be answered through their existing response
  events. Sending an ordinary message neither answers them nor resumes the agent.
- Stop cancels active work and retains pending inputs. A failure also retains the
  queue. Restart resets stale active status to idle without automatically running
  pending inputs. In each case, a new submission to the idle session resumes by
  shipping all retained inputs plus the new submission. There is no separate
  resume/cancel/edit-queue endpoint in this change.
- Archived sessions reject new submissions. Existing pending messages stay
  persisted while archived. Deletion removes their queue together with scrollback.

## Tests expected in clients

Busy send succeeds; queue arrival does not split a streaming answer; shipment
appends separate attributed rows; history is recorded once; multiple messages are
ordered; old acceptances outside replay still ship correctly; authoritative queue
snapshot clears stale pending entries; reconnect does not duplicate messages;
Stop/failure leave pending inputs visible; archived/disconnected guards and Bash
behavior remain unchanged.

The synchronous Python API package already returns raw response JSON and its
`message_session` extracts only `message_id`, so it needs no functional changes.
Documentation/tests that assume turn response status `running`, reject busy
recipients, or enumerate exactly three scrollback response keys should be updated.
