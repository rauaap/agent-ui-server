# Transcript history pagination

The WebSocket still replays the newest 200 persisted events. Every persisted
WebSocket frame, both replayed and live, now includes `event_id` (the database
scrollback row ID). Non-persisted frames such as status, input_queue, archived,
and done have no event_id. Input frames also retain message_id for input delivery.

When the user scrolls to the top, fetch 200 older events:

`GET /sessions/{session_id}/scrollback?before=<oldest_event_id>&limit=200`

- before is exclusive. IDs are globally allocated and may have gaps; never
  subtract 200 from an ID.
- messages contains the nearest older events in ascending chronological order.
  Each row has id, session_id, ts, type, payload.
- next_cursor is the oldest returned ID. Use it as before for the next page.
  An empty page keeps the supplied cursor.
- has_more indicates whether more older events exist; stop requesting once false.
- queued_messages remains the current input queue snapshot, not historical data.
- after still supports forward pagination; after and before together return 422.
- limit defaults to 200 and supports 1 through 1000; before must be nonnegative.

Client work:
1. Retain event_id on transcript events (REST row id is the same identifier),
   including events that do not produce their own visible transcript item.
2. On reaching the top, request the page using the oldest loaded event ID, then
   prepend the events in chronological order through the existing rendering path.
   REST rows can be normalized to {type, ...payload, event_id: id}, with
   message_id: id additionally for input events.
3. Preserve the visible scroll anchor when prepending; do not jump to the bottom.
4. Allow only one history request at a time per session, deduplicate by event ID,
   and ignore stale responses after changing sessions. Reset paging state on a
   fresh replay/session change and use has_more to detect exhaustion. An empty
   initial replay has no historical events to fetch.

Pages contain 200 persisted events, not necessarily 200 visible chat bubbles:
output fragments, tool events, input delivery, and approval/question responses
are all persisted transcript events.
