# Archive and worktree design decisions

## Core model

- Worktrees are not archivable.
- Archiving applies only to projects and sessions.
- Archiving a session does not automatically detach it from its worktree.
- Detachment is a separate cleanup operation available only for archived sessions.
- A live session can release its worktree only by being deleted, or by first being archived and then explicitly detached.

## Detach UX

Offer **Detach from worktree** only in an archived session's settings.

Do not:

- add detachment to the archive confirmation;
- automatically detach after archiving;
- remove the worktree from disk as part of detachment.

Detaching a session:

- does not alter or delete anything on disk;
- sets the public `worktree_id` to `null`;
- preserves the former worktree's absolute path in `working_dir`;
- removes that session from the worktree's `session_count`;
- allows the worktree to be deleted separately once every attached session has either been detached or deleted.

Detaching may be retried while the session remains archived. The server remembers whether a session was previously detached:

- A session that was never attached to a worktree returns `409`.
- A session that was attached and has already been detached returns `200` with the same state.
- A detached session that has subsequently been unarchived returns `409`, because detachment is available only to archived sessions.

## Worktree deletion UX

Do not provide a guided cleanup workflow when worktree deletion is blocked. Submit the normal:

```http
DELETE /worktrees/{id}
```

When attached sessions cause a `409`:

- show the session names supplied by the server;
- explain that live sessions must be deleted, or archived and subsequently detached through Session settings;
- explain that archived sessions can be detached directly through Session settings;
- do not automatically archive, detach, or delete any session;
- do not offer forced worktree removal.

Detaching one session releases only that session's reference. Worktree deletion remains blocked until no sessions are attached.

A live detached session can still depend on the registered worktree's directory even though it no longer contributes to `session_count`. If worktree deletion returns `409` naming live detached sessions:

- explain that their preserved `working_dir` is still in active use;
- ask the user to archive those sessions before retrying;
- do not archive them automatically.

Before deleting a worktree that was formerly used by detached archived sessions, warn that deletion may remove the directory those sessions require for future unarchiving. `GET /worktrees` has no detached-session count, so inspect `GET /sessions` and match sessions where `worktree_id === null`, `working_dir === worktree.path`, and `archived_at !== null`. Detachment itself leaves the directory untouched; the later worktree deletion is the operation that may remove it. The server will not allow that deletion while any detached session using the path is live.

## Detached-session presentation

Display a detached session as using a **Former worktree**.

Continue showing its preserved absolute `working_dir`. Do not present it as running in the project directory merely because `worktree_id` is now `null`.

The visible client distinction is:

| State | Condition | Display |
|---|---|---|
| Attached worktree | `worktree_id != null` | Current worktree |
| Project directory | `worktree_id == null` and `working_dir == project.path` | Project directory |
| Detached worktree | `worktree_id == null` and `working_dir != project.path` | Former worktree |

This state can survive unarchiving. If the preserved directory exists as a directory, the session resumes there without being reattached to a managed worktree resource. The agent harness requires the absolute working directory; it does not require that directory to remain a Git worktree.

## Unarchive behavior

A session can be unarchived only if its effective `working_dir` is an existing directory, as represented server-side by:

```python
Path(session["working_dir"]).is_dir()
```

For a detached session whose preserved directory is missing or is not a directory:

- keep the session archived;
- explain that a directory must be recreated at the same absolute path before retrying.

Project unarchive performs the same preflight for every session its archive cascade would restore. If any required directory is unavailable, project unarchive is all-or-nothing:

- the project remains archived;
- all affected sessions remain archived;
- the client must not apply partial local updates.

## Live-state handling

Handle `worktree_detached` WebSocket events by updating the session's:

- `worktree_id` to `null`;
- `working_dir` to the supplied preserved path;
- displayed location to **Former worktree**.

Detachment also decreases the former worktree's `session_count`. Update that count using the session's previous `worktree_id` when available, or refetch the worktree list. The event itself does not include the former worktree ID.

Unlike `archived`, `worktree_detached` is not sent when a WebSocket connects. REST session refreshes therefore remain authoritative for worktree and location metadata, including sessions that were detached while a client was disconnected. Event handling should also tolerate duplicate `worktree_detached` events caused by an idempotent retry.

## Server operations remain separate

The client may compose these calls as a workflow, but the server keeps them independent:

1. Archive the session.
2. Optionally detach it from its worktree.
3. Optionally delete the worktree once nothing remains attached.

A failure at any later step leaves the earlier state valid and visible. No step implicitly performs the next one.
