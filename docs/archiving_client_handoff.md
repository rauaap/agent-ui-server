# Handoff: archiving sessions and projects (client side)

Status: **server side implemented** (schema, migration, endpoints, WebSocket
event, tests, README). This document is a hand-off spec for the agents building
the client half, in `agent-ui-desktop` (separate repo). It is self-contained:
everything you need about the server contract is reproduced here, so you do not
need to read the server source.

This is a greenfield client feature: build it against the endpoints below. There
is nothing to migrate and no prior client state to carry over.

## Problem

Archiving files a session or a project away without deleting it, so old work can
be kept without cluttering the list. Archived things live in a separate menu.

An earlier throwaway prototype kept the archived ids in `localStorage`, which
meant archiving had to be redone on every device — annoying enough to be the
reason this is a server feature. It never shipped, so it is only useful here as
the rationale for the design: the database records what is archived, so the
archive is the same everywhere, and a second device finds out live over the
WebSocket.

## Decisions already made

Recorded so they are not re-litigated during implementation. All of these are
implemented and covered by tests on the server.

- **`archived_at` is a nullable timestamp, not a boolean.** `null` means live, an
  ISO 8601 string (UTC, `Z`) means archived. The archive view wants to sort by
  when things went in, which a boolean cannot support. Re-archiving something
  already archived keeps the original timestamp, so a redundant call does not
  reorder the archive.

- **The list endpoints do not filter.** `GET /projects` and `GET /sessions`
  return everything, archived or not, with `archived_at` attached. Splitting the
  two lists is the client's job. Filtering server-side would silently change what
  an older client sees; this way the field is purely additive.

- **Archiving a project cascades to its sessions.** An archived project must not
  leave sessions showing in the main list. There is no "you must archive its
  sessions first" precondition — one call does the lot.

- **Unarchiving a project is a round trip.** It restores exactly the sessions
  that its own cascade archived. A session archived by hand *before* the project
  was archived keeps its own timestamp and stays archived through the whole
  cycle. The server tracks which is which internally; the client does not need to
  model it and cannot see it.

- **Unarchiving a session takes its project with it**, but nothing else — a live
  session under an archived project would have nowhere to show. The other
  sessions the project's archive swept up stay archived.

- **Archived is read-only, not sealed.** Scrollback still replays, the session
  can still be renamed, have its auto-approve toggles flipped, and be deleted.
  Only starting *new* work is blocked.

- **A busy session cannot be archived**, and one busy session refuses a whole
  project archive without writing anything.

## Invariants you can rely on

These hold at every point the client can observe, and are worth leaning on
rather than defending against:

1. **An archived project has no live sessions.** The cascade archives them,
   creating one is blocked, and unarchiving one unarchives the project. So a
   project with `archived_at != null` always reports `session_count: 0` and
   `last_active_at: null`.
2. **A live project may still have archived sessions** — `archived_session_count`
   is how many, and it can be non-zero on a perfectly live project.
3. **`session_count` and `last_active_at` count live sessions only.** They will
   never claim five sessions while the main list shows none.

Consequence of 1 that will bite if you miss it: **`GET /projects` sorts by
`last_active_at` descending**, and archived projects have `last_active_at: null`,
which SQLite sorts below everything. So archived projects always arrive at the
*end* of the list. Do not use the server's order for the archive view — sort it
yourself by `archived_at` descending.

## The server contract

### `GET /projects`

Returns every project, archived included.

```jsonc
[
  { "id": 1, "path": "/projects/agent-ui", "name": "agent-ui",
    "exists": true, "is_git_repo": true,
    "archived_at": null,                      // live
    "session_count": 3,                       // live sessions only
    "archived_session_count": 1,              // archived sessions under a live project
    "last_active_at": "2026-07-28T09:14:02Z" },
  { "id": 2, "path": "/projects/old-thing", "name": "old-thing",
    "exists": true, "is_git_repo": true,
    "archived_at": "2026-08-26T11:02:00Z",    // archived
    "session_count": 0,                       // always 0 when archived (invariant 1)
    "archived_session_count": 4,
    "last_active_at": null }                  // always null when archived
]
```

### `PATCH /projects`

Archive or unarchive a project. Addressed by `path` in the body, matching
`DELETE /projects` — a filesystem path does not belong in a URL segment.

```jsonc
// Request
{ "path": "/projects/old-thing", "archived": true }

// 200 — the project in the same shape as GET /projects, plus:
{ "…": "…", "sessions_affected": 4 }
```

`sessions_affected` is how many sessions the cascade archived (when
`archived: true`) or restored (when `archived: false`). It can legitimately be
`0` — archiving a project with no live sessions, or unarchiving one whose
sessions were all archived by hand beforehand.

Errors:

| Code  | `detail`                                                        | When |
|-------|-----------------------------------------------------------------|------|
| `404` | `Project not found`                                             | No project at that path |
| `409` | `Cannot archive a project with busy sessions: fix login, deploy` | One or more sessions not `idle`, or running a shell command. **Nothing was written.** The names in the message are the busy sessions. |
| `400` | `path must be absolute` / `path must name a directory, not /`   | Malformed path — should not happen if you send back a path from `GET /projects` |

### `GET /sessions`

Unchanged except for the new field. Every session, archived included:

```jsonc
{ "id": 7, "name": "fix login", "project_id": 1, "worktree_id": null,
  "working_dir": "/projects/agent-ui",   // derived: the worktree's path, or the project's
  "agent": "claude-code", "agent_session_id": "resume-1",
  "status": "idle", "created_at": "…", "last_active_at": "…",
  "archived_at": null,                                  // ← new
  "auto_approve_write": false, "auto_approve_command": true }
```

### `PATCH /sessions/{id}`

The existing partial-update route, with one more optional field. Every field is
optional; only the supplied ones are applied, so `{"archived": true}` on its own
is a valid body and will not disturb the name or the toggles.

```jsonc
{ "archived": true }   // → 200, the updated session
```

Errors:

| Code  | `detail`                                    | When |
|-------|---------------------------------------------|------|
| `404` | `Session not found`                         | Unknown id |
| `409` | `Cannot archive a session while it is busy` | `status` is not `idle`, **or** a shell command is running (which `status` does not show — see below) |

### Blocked while archived

| Call                        | Code  | `detail` |
|-----------------------------|-------|----------|
| `POST /sessions` into an archived project | `409` | `Cannot create a session in an archived project` |
| `POST /worktrees` into an archived project | `409` | `Cannot create a worktree in an archived project` |
| `POST /sessions/{id}/turn`  | `409` | `Session is archived` |
| `POST /sessions/{id}/bash`  | `409` | `Session is archived` |

Still allowed on an archived session: `GET`, the WebSocket (including scrollback
replay), `PATCH` (rename, toggles, unarchive), `POST /stop`, `DELETE`. And
`DELETE /projects` still sweeps archived sessions along with the rest — archiving
is not a shield against deletion, and should not be presented as one.

### Worktrees are not archivable

Worktrees have no `archived_at` and no archive of their own. Archiving is about
what clutters the session and project lists, and a worktree appears in neither —
it is reached through its project. So `GET /worktrees` keeps returning an
archived project's worktrees, and `DELETE /worktrees/{id}` keeps working on
them, in the same spirit as being able to delete an archived session.

The one rule that does apply: you cannot *create* a worktree in an archived
project, exactly as you cannot create a session in one. Hide the control rather
than let it 409.

Note this means archiving a project does **not** clean up its worktrees, and
unarchiving does not restore anything about them. If you archive a project with
worktrees on disk, they stay on disk. That is deliberate — worktrees hold real
uncommitted work and are removed only through their own endpoint — but it is
worth being honest about in any confirmation copy, since "archive" can otherwise
read as "put away everything to do with this".

### WebSocket

A new server → client event, broadcast to every subscriber of that session:

```jsonc
{ "type": "archived", "archived_at": "2026-08-26T11:02:00Z" }   // null = brought back
```

It fires on `PATCH /sessions/{id}` with `archived`, and on `PATCH /projects` for
each affected session that has a subscriber. **It is also sent on connect**,
right after the `status` event, so a client that was offline when another device
archived something still finds out. Treat it exactly like `status`: authoritative,
idempotent, and safe to receive when nothing changed.

If you send `{"type": "input"}` or `{"type": "bash"}` over the WebSocket for an
archived session, the refusal comes back as a normal error frame with no status
code:

```jsonc
{ "type": "error", "message": "Session is archived" }
```

That is the same shape as every other WebSocket error, so it needs no special
handling — but it is a reason to disable the composer rather than let the user
type into it and get a generic error back.

## Client work

### Navigation

A separate **Archive** view, reachable from the main list. It holds archived
projects and archived sessions. Sort both by `archived_at` descending — most
recently filed first — and **not** by the order `GET /projects` returns, which
puts archived projects last (see [Invariants](#invariants-you-can-rely-on)).

The main list shows only rows with `archived_at === null`. Everything the archive
view needs is already in the two list responses; no extra requests.

### Project cards

- Live project with `archived_session_count > 0`: show it, quietly — something
  like "3 archived" alongside the live `session_count`, linking into the archive
  filtered to that project. Do not fold archived sessions into `session_count`;
  the server deliberately keeps them apart.
- Archived project: `session_count` is `0` and `last_active_at` is `null` by
  invariant. Do not render "no sessions" or "never used" for these — it is
  misleading. Use `archived_session_count` and `archived_at` instead.
- An archived project must not offer **New session** (the server returns `409`).

### Archiving actions

- **Archive project** is a single call. It cascades, so the confirmation should
  say so with the real number: the client already knows
  `session_count + archived_session_count`. Something like "Archive *old-thing*
  and its 4 sessions?" — the cascade is not a surprise to be discovered
  afterwards.
- **Unarchive project** is also a single call, and restores only what the cascade
  took. `sessions_affected` in the response is the honest number to report; do
  not assume it equals `archived_session_count`, because sessions archived by
  hand beforehand stay archived.
- **Unarchive session** silently unarchives its project too, if that project was
  archived. Refetch `GET /projects` (or patch the project in your store) after
  it, or a project will be missing from the main list until the next poll. This
  is the one place a session-level action has a project-level effect.

### Archived session view

Opening an archived session must work — keeping old sessions readable is the
entire point of the feature. Connect the WebSocket, replay the scrollback,
render it as usual.

What changes: **disable the prompt composer and bash mode**, and say why, with
unarchiving as the offered action ("This session is archived. Unarchive to
continue working in it."). Rename, the auto-approve toggles, and delete stay
available.

### The busy check

Archiving is refused while a session is busy. Two things to note:

- **`status` is not the whole story.** A session running a shell command through
  bash mode stays `idle` — bash deliberately sits outside the turn state machine.
  So a session can look idle in your store and still return `409`. You cannot
  fully predict this client-side; handle the `409` rather than only guarding on
  `status`.
- **A project archive is all-or-nothing.** A `409` there means nothing was
  written, including for the idle sessions. Surface the names from `detail` and
  let the user stop those sessions and retry; do not partially update your store.

Disabling the archive action on a non-`idle` session is still worth doing — it
catches the common case — but the error path has to exist regardless.

### Live updates across devices

The `archived` WebSocket event only reaches clients subscribed to *that session*.
There is no project-level or global feed, so a device sitting on the project list
with no session open will not hear about an archive performed elsewhere. Refetch
`GET /projects` and `GET /sessions` on the events you already refetch on —
window focus, view mount, whatever the client does today. This is the same
staleness the list already has; archiving does not make it worse and does not
warrant new polling.

## Out of scope

- Server-side filtering or pagination of the two list endpoints. If the archive
  ever grows enough to matter, that is a separate change with its own
  compatibility story.
- Bulk archive/unarchive of arbitrary session sets. The only bulk operation is
  the project cascade.
- Auto-archiving on any schedule or inactivity rule. Archiving is always an
  explicit user action.
- Any notion of archiving as protection from deletion — `DELETE /projects` still
  takes archived sessions with it, by design.
- Exporting or compacting an archived session's scrollback.
