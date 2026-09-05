# Design: order WebSocket replay and live events

Status: **implemented.** This document describes a status desynchronization bug
in the session WebSocket and the server-side ordering and writer-queue design
used to fix it.

## Problem

A client can remain on `running` or `awaiting_approval` after the session has
actually returned to `idle`. The database and `GET /sessions` are correct; the
last status observed on that client's WebSocket is not.

The desktop client exposes this most often. An open Agent UI session tab treats
its WebSocket as authoritative and deliberately does not replace its status
from a REST refresh. Android is less likely to remain wrong because its detail
screen also performs a REST metadata refresh when opened and its list screens
poll `GET /sessions`, but Android's WebSocket replay logic is vulnerable to the
same server ordering problem.

This is not primarily a client reducer bug. Both clients correctly apply an
`idle` status and retire pending approval/question UI when that event arrives
last.

## Protocol and previous implementation

`GET /ws/sessions/{session_id}` sends up to 200 persisted scrollback events and
then a `status` event. Both clients use that status as the end-of-replay marker:

```text
persisted scrollback event
persisted scrollback event
...
status                         <- replay is complete
subsequent events              <- live
```

Before this fix, the endpoint did approximately this:

```python
session = db.get_session(session_id)
if session is None:
    await websocket.close(code=1008)
    return

await websocket.accept()
subscribers[session_id].add(websocket)

await replay_scrollback(websocket, session_id)
await websocket.send_json({"type": "status", "status": session["status"]})
```

There are two related defects.

### 1. The trailing status was read too early

The `session` row is fetched before `accept()` to validate the id, then reused
after an asynchronous replay. If it said `running` before replay and the turn
finishes during replay, the endpoint still sends that old `running` value at
the end.

A possible wire order is:

```text
historical output
live status: idle
more historical output
replay status: running         <- stale value sent last
```

The client correctly applies the last event and therefore remains incorrectly
`running`.

### 2. Replay and live broadcasts are not separated

The raw socket is inserted into `subscribers` before replay. `broadcast()` sends
directly to every socket, so a turn event can be written to the same socket
while `replay_scrollback()` is still writing historical events.

This violates the protocol boundary assumed by both clients. In particular, a
live status received during replay can be mistaken for the end-of-replay
marker. Merely re-reading the database status after replay narrows the stale
snapshot window but does not establish ordering:

```text
replay finishes
endpoint reads status: running
turn changes status and broadcasts idle
endpoint sends the value it read: running
```

The fix must make snapshot-to-live handoff ordered, not merely move the status
read closer to `send_json()`.

### 3. The current broadcast path has a second way to strand a client

The previous `broadcast()` awaited `send_json()` for subscribers one at a time. A slow socket
can therefore delay delivery to every socket later in the set, including the
final `idle`. There is no application-level send timeout.

If a send raised, the previous `broadcast()` silently removed that WebSocket from
`subscribers`, but it does not close it. The endpoint's receive loop and the
client can remain unaware that this otherwise-open connection will receive no
more broadcasts. A transport failure will normally cause the receive loop and
client to notice a disconnect too, but a send-side/concurrent-send error is not
required to do so. Such a connection stays visually "connected" while its last
status stays forever stale.

Several tasks could call `send_json()` on the same socket: replay, live
broadcasts, and direct error responses from the receive loop. Even where the
WebSocket implementation permits concurrent sends of complete frames, their
ordering is scheduler-dependent rather than the protocol order.

This is independent of the stale snapshot race, but the single-writer queue in
the proposed design fixes both. Writer failure must actively close/retire the
connection so the client reconnects; silently removing it from publication is
not sufficient.

## Other causes checked

- Both clients apply every `status` event they receive. Their reducers do not
  selectively ignore `idle`; `idle` also clears stranded approval/question UI.
- Every normal `run_turn()` exit, including exceptions and cancellation, enters
  its `finally` block, writes `idle` to the database, and broadcasts `idle`.
- A genuinely hung adapter can leave both the task and database status busy.
  That is a different failure: `GET /sessions` will also say `running`, and all
  clients should agree. OpenCode has had a known upstream subagent-permission
  deadlock of this kind, but it does not explain a desktop-only status when REST
  already says `idle`.
- A physical half-open connection can temporarily miss updates, although
  uvicorn's WebSocket ping/timeout normally detects it. The more concerning
  case here is the logical unsubscription described above, because transport
  pings may continue while application broadcasts have stopped.
- The desktop's ordinary refresh intentionally preserves status for an open
  Agent UI session tab, so it does not repair either server delivery failure.
  A full page reload creates new sockets and usually repairs it. This explains
  persistence; it is not the source of the wrong event order.

## Reproduction

Before the fix, the race was intermittent on a local network because replay was
usually fast. It could be made deterministic by temporarily adding a small
delay after every row in the old `replay_scrollback()`:

```python
await websocket.send_json({"type": row["type"], **payload})
await asyncio.sleep(0.05)
```

Then:

1. Use a session with enough scrollback to make replay last several seconds.
2. Close its Agent UI session tab in the desktop client so its socket closes.
3. Start a short turn from another client.
4. While the session is `running`, reopen its Agent UI session tab on desktop.
5. Let the turn finish while the new socket is replaying.
6. Observe that the database and `GET /sessions` say `idle` while the desktop
   tab can finish on `running`.

Remove the artificial delay after testing.

## Required ordering invariant

For each session, establishing a connection must define one snapshot boundary
for persisted scrollback and the WebSocket-snapshotted `status` and `archived`
state:

- Persisted events before the boundary are represented by scrollback replay;
  `status` and `archived` changes before it are represented by their snapshot
  frames.
- Events committed after the boundary are queued after the replay marker and
  initial archived frame.
- No live event is written between replay rows or initial snapshot frames.
- Only one task writes outbound frames to a given WebSocket.

This is not a universal snapshot of all session metadata. Events such as
`renamed`, `settings`, and `worktree_detached` have no WebSocket replay
representation and remain REST-authoritative. A client that misses one before
subscribing learns that state from its REST metadata refresh, not this handoff.

For the original failure, the required order is:

```text
all snapshot scrollback
snapshot status: running       <- replay marker
snapshot archived state
live status: idle              <- happened after the snapshot
```

The final client state is then necessarily `idle`.

## Proposed design

Use a short-lived per-session critical section to create a scheduler-atomic
snapshot and to pair each database mutation with publication. Give every
connected client an ordered outbound queue and a single writer task. Here,
"atomic" means that cooperating coroutines in this server process cannot
observe or publish an intermediate state; it does not promise crash-atomicity
across separate SQLite operations and queue insertion.

### Subscriber record

Replace `set[WebSocket]` with subscriber records resembling:

```python
@dataclass(eq=False)
class Subscriber:
    websocket: WebSocket
    outbound: asyncio.Queue[dict[str, Any]]
    writer: asyncio.Task[None] | None = None
    retired: bool = False
```

A subscriber does not need a separate replay flag if its queue is populated
with the complete initial sequence before it becomes visible to publishers.

### Per-session lock

Maintain one `asyncio.Lock` for each session id encountered by the process:

```python
stream_locks: dict[int, asyncio.Lock]
```

Keep these locks for the process lifetime. Their memory cost is negligible at
the expected session count, while removing an apparently unused lock is easy to
get wrong when another task already holds a reference or is waiting on it.

The lock protects only:

- database changes that affect the stream snapshot;
- creation of the replay/status snapshot;
- insertion of frames into subscriber queues;
- adding/removing a subscriber at the snapshot boundary.

Do **not** perform WebSocket network I/O or await an agent process while holding
this lock. Any synchronous precondition that guards a mutation, such as the
busy check before archiving, should run after the lock is acquired and in the
same non-yielding block as the mutation. The current check and mutation already
do not yield; do not introduce a race by checking first and then awaiting lock
acquisition.

### Establishing a connection

After `accept()`, acquire the session's stream lock. While holding it, and with
no `await` inside the critical section:

1. Revalidate/read the session row.
2. Read the recent scrollback rows.
3. Create the subscriber and its outbound queue.
4. Put the replay frames into that queue with `put_nowait()`.
5. Put the snapshot `status` frame into the queue. This is the existing replay
   marker.
6. Put the snapshot `archived` frame after it, preserving the current protocol.
7. Create and assign the outbound writer task.
8. Add the subscriber to the session's subscriber collection.

Conceptually:

```python
async with stream_lock(session_id):
    session = db.require_session(session_id)
    rows = db.recent_scrollback(session_id, SCROLLBACK_REPLAY_LIMIT)

    subscriber = Subscriber(websocket, new_outbound_queue())
    for row in rows:
        subscriber.outbound.put_nowait(frame_for(row))
    subscriber.outbound.put_nowait(
        {"type": "status", "status": session["status"]}
    )
    subscriber.outbound.put_nowait(
        {"type": "archived", "archived_at": session["archived_at"]}
    )
    subscriber.writer = asyncio.create_task(write_subscriber(subscriber))
    subscribers[session_id].add(subscriber)
```

`asyncio.create_task()` does not itself yield or perform network I/O; the writer
cannot run until the current coroutine later yields. Assigning it before the
subscriber becomes visible ensures retirement code never encounters a visible
subscriber with no writer. Events published after the subscriber is added
append behind the complete initial sequence.

The early `db.get_session()` may still be retained as a cheap pre-accept
validation, but it must not supply the replay status. The session must be
revalidated under the stream lock because it may have been deleted while
`accept()` yielded.

### Publishing events

A stream-visible state transition must perform its synchronous database change
and queue insertion in one critical section:

```python
async with stream_lock(session_id):
    db.update_status(session_id, "idle")
    enqueue_for_subscribers(
        session_id,
        {"type": "status", "status": "idle"},
    )
```

`enqueue_for_subscribers()` must not call `send_json()` and must not await. It
uses `put_nowait()` so every subscriber receives events in the same publication
order.

The same invariant applies to persisted transcript events:

```python
async with stream_lock(session_id):
    db.append_scrollback(session_id, event_type, payload)
    enqueue_for_subscribers(session_id, event)
```

Compound transitions must perform all related database operations and queue
insertions in one critical section, rather than calling a separately locked
helper for each operation. For example, preserve the current live approval
order while making the request and status indivisible at the snapshot boundary:

```python
async with stream_lock(session_id):
    db.append_scrollback(session_id, "approval_request", payload)
    db.update_status(session_id, "awaiting_approval")
    enqueue_for_subscribers(
        session_id,
        {"type": "status", "status": "awaiting_approval"},
    )
    enqueue_for_subscribers(
        session_id,
        {"type": "approval_request", **payload},
    )
```

Apply the same pattern to input plus `running`, approval response plus
`running`, and question response plus `running`.

This prevents an event from being included in a new connection's database
snapshot and then also queued as a post-snapshot event, or from being in
neither. Review every current `db.append_scrollback(...)` / `db.update_status(...)`
followed by `broadcast(...)` pair and move the pair behind a helper that enforces
this invariant. Do not hold the lock across adapter calls such as
`send_approval()`, process reads, or any other potentially slow await.

Events that change replay-adjacent metadata, particularly `archived`, must use
the same ordering rule. Events with no WebSocket snapshot representation can
still use the queue publication path so active subscribers see one total order,
but the handoff does not make those events replayable to a client that subscribes
afterward; their current state remains REST-authoritative.

### Deleting a session

Deletion does not need to send a final event. It does need to prevent a socket
that is concurrently finishing connection setup from being left open after the
subscriber collection and database row are removed.

Stop and await the agent and bash tasks outside the stream lock. Then acquire
the session lock and, without yielding, retire and remove all subscriber records
and delete the session row:

```python
async with stream_lock(session_id):
    doomed = retire_all_subscribers(session_id)
    db.delete_session(session_id)

await teardown_subscribers(doomed)
```

Writer cancellation may be requested during synchronous retirement, but writer
termination and WebSocket close are awaited outside the lock. If connection
setup wins the lock, deletion retires the newly registered subscriber. If
deletion wins, connection setup fails its under-lock revalidation and closes
the accepted socket. This race is unlikely in normal single-user operation, but
using the existing stream lock here is cheap and prevents an open, unregistered
socket from being stranded.

### One writer per socket

A dedicated outbound writer task is the only code that calls `send_json()` for
its socket. This includes connection-local validation/error responses currently
sent directly by `session_websocket()`; once the subscriber exists, those
responses must also be enqueued rather than bypassing the writer. Teardown may
send a WebSocket close frame only after the writer has stopped:

```python
WEBSOCKET_SEND_TIMEOUT_SECONDS = 30

async def write_subscriber(subscriber: Subscriber) -> None:
    while True:
        frame = await subscriber.outbound.get()
        async with asyncio.timeout(WEBSOCKET_SEND_TIMEOUT_SECONDS):
            await subscriber.websocket.send_json(frame)
```

This has three benefits:

1. Replay and live frames cannot be written concurrently.
2. A slow client does not make publication to every other client wait on its
   network send.
3. Queue order is wire order.

A frame that cannot be sent within 30 seconds means the connection is no longer
trustworthy and is handled exactly like any other send failure. Do not retry the
frame because delivery may have partially succeeded and a retry could duplicate
it. Retire the subscriber, actively close the socket with a bounded wait, and
ensure the receive side terminates so the client observes a disconnect and
reconnects with a fresh snapshot.

Run outbound writing and inbound receiving as separate tasks, and coordinate
them so either side finishing tears down the whole connection:

```python
done, pending = await asyncio.wait(
    {writer_task, receiver_task},
    return_when=asyncio.FIRST_COMPLETED,
)
```

Retire the subscriber, cancel and await the remaining task, and then close the
socket with a bounded wait. Thus a writer failure cannot leave a receive task
blocked forever in `receive_json()`, and a receive disconnect cannot leave its
writer behind. Connection teardown must be idempotent because both sides can
notice a dead socket independently. A task initiating its own teardown must not
cancel or await itself.

### Backpressure

Do not allow an indefinitely slow client to consume unlimited memory. The
implementation should choose a queue bound large enough for the initial replay
(`SCROLLBACK_REPLAY_LIMIT` plus the status/archive frames and a reasonable live
burst). If `put_nowait()` reaches the live-event capacity, retire that
subscriber; reconnect will give it a fresh bounded replay.

Retirement has a synchronous phase under the session stream lock: mark the
subscriber retired, remove that exact record from the subscriber collection,
and call `writer.cancel()` (which requests cancellation without awaiting it).
The publication helper can return the retired records to its caller. After
releasing the lock, await writer termination and close the WebSocket, with
bounded waits. Do not call or await `websocket.close()` while holding the stream
lock. This is not because the queue owns the lock; it is because network I/O
could otherwise block snapshots and publication for the entire session.

Initial replay population and live capacity may be represented separately if
that makes the bound clearer. Whichever representation is chosen, one stalled
client must not block agent execution or delivery to other clients.

## Why the critical section does not yield

`asyncio` scheduling is cooperative. Once an `async with lock` acquisition has
completed, ordinary synchronous statements continue until an `await` or another
yielding operation is reached. The SQLite methods used here are synchronous,
and `Queue.put_nowait()` is synchronous, so the snapshot/publication blocks
above do not yield.

Acquiring the lock itself may yield; that is intended. The important property
is that there is no `await` after acquisition and before the database operation,
snapshot, and queue insertion are complete.

This guarantee is system-wide only if publishers use the same lock and the
application runs as one process with one event loop. That is the deployment
model of this server; coordinating multiple workers or other processes writing
the same SQLite database is out of scope. Protecting the connection snapshot
while leaving `db.update_status()` and event queueing as separate operations
would still permit a half-published transition during normal execution.

The database methods may commit separately, and queue insertion is not part of
a SQLite transaction. A process crash between those steps can therefore leave
persisted state without its corresponding publication, or interrupt a compound
database transition. Crash recovery and crash-atomic database transactions are
out of scope for this ordering fix.

## Protocol compatibility

No client protocol change is required:

- Replay events keep their current JSON shapes.
- `status` remains the end-of-replay marker.
- `archived` remains immediately after the initial status.
- Live events retain their current JSON shapes.

The desktop and Android clients should not need changes. The server will simply
guarantee the ordering they already assume.

Live and replay order need not be identical within a compound transition. The
current live approval path sends `awaiting_approval` before its request, while a
reconnect replays the persisted request before the snapshot status marker. Both
orders are valid. Tests should verify that the request and status fall wholly on
the correct side of the snapshot boundary, not require replay order to match
live order.

## Tests

Focused async tests use a fake/blockable WebSocket or subscriber writer. The
coverage requirements are:

1. **Turn finishes during replay:** block replay delivery, publish `idle`, then
   release it; assert snapshot status precedes live `idle` and `idle` is last.
2. **Approval during replay:** assert `awaiting_approval` and its request occur
   after the complete initial sequence in current live publication order; do
   not require that order to match replay's request-then-status order.
3. **Transition before snapshot:** an event completed before the boundary is
   represented by the snapshot and is not also queued as a live duplicate.
4. **Transition after snapshot:** an event completed after the boundary appears
   exactly once after the marker.
5. **Concurrent live publications:** one socket sees the same order in which
   events were committed under the session lock.
6. **Slow subscriber isolation:** blocking one writer does not delay queueing or
   delivery to another subscriber.
7. **Disconnect cleanup:** writer failure and receive-loop disconnect cannot
   leave a subscriber or writer task behind.
8. **Send timeout:** inject a short timeout in the test and assert a blocked
   `send_json()` retires and closes only that subscriber without retrying the
   frame or delaying other clients. Production retains the 30-second value.
9. **Deletion during connect:** revalidation under the lock closes/refuses the
   accepted socket cleanly rather than constructing a snapshot for a vanished
   session.

Also retain an integration-level assertion that a reconnect stream consists of
all replay frames, exactly one initial status marker, initial archived state,
and only then events committed after the snapshot boundary.

## Scope

This proposal fixes the server source of stale session status and makes the
existing replay contract real. It does not add REST polling or status
reconciliation to either client. Those would be separate resilience features,
not substitutes for ordered server delivery.

### Correctness observations outside the intended use case

The following edge cases are useful to retain as correctness observations, but
do not justify complicating this replay-ordering fix for its single-user,
non-overlapping operation model:

- An agent blocked on an approval or question cannot produce more agent events,
  so its pending request normally remains within the 200-row replay limit. Bash
  commands are allowed while the agent is blocked, however, and roughly 100
  sequential commands could produce enough `bash_input` and
  `bash_output`/`error` rows to evict that request. A reconnect would then
  receive `awaiting_approval` without the request needed to resolve it. This is
  considered a degenerate usage pattern.
- An overlapping stop and new-turn request can race if separate clients or
  automation operate the same session concurrently. The old `stop_session()`
  may resume after the replacement turn starts and write `idle`; an adapter's
  cleanup of its old process entry may also remove a newly installed entry.
  Normal single-user operation waits for stop to complete before starting
  another turn, so this concurrency race does not require changes here.
- Concurrent project archive/unarchive operations would require a defined
  multi-session locking order for strict transaction-wide event ordering.
  Project mutation is synchronous, and normal operation does not issue
  overlapping project operations or start work while archiving. A connection
  therefore sees either the old archive snapshot followed by the live event or
  the new archive snapshot; a duplicate event with the same value is harmless.
  Project archive broadcasts should use the queue publication path, but this
  proposal does not require acquiring every affected session lock as one bulk
  transaction.
