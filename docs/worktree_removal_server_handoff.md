# Server handoff: worktree removal and directory existence

Server implemented; client changes remain separate work. This contract supersedes worktree-deletion restrictions and `exists` response fields in the older worktree handoff. Archive and explicit session-detachment behavior has since been removed; see [archive_removal_client_handoff.md](archive_removal_client_handoff.md).

## `GET /directory-existence`

Returns a JSON object mapping absolute paths directly to booleans, for example:

```json
{"/projects/app": true, "/projects/app-fix": false}
```

The map includes all projects, all managed worktrees (even with no sessions), and all effective session working directories (including former-worktree sessions). Unique paths are collected in one database query and each checked once with `os.path.isdir`. This is a fresh filesystem snapshot, not stored state, Git validity, or a filesystem event stream. Filesystem changes during the checks are possible.

Project and worktree responses no longer contain `exists`, including create/update responses. Projects still contain `is_git_repo`. Session response shapes are unchanged.

Clients should fetch catalogs first, then the existence map, and publish the combined refresh together. If any catalog path is absent from the map because concurrent catalog mutations occurred, refresh again; an absent key is unknown, not false. Fetch this map alongside the existing periodic catalog refreshes.

## `DELETE /worktrees/{id}`

- `404`: unknown worktree.
- `409`: a session using this directory is busy (active agent turn or shell command), including already-detached sessions. No deletion or session metadata changes occur.
- `409`: Git registration listing fails, or Git refuses safe removal (for example due to modified/untracked files). No database changes occur. Removal is never forced.
- Before removal, the server checks `git worktree list --porcelain -z`. If the path is registered, it runs normal `git worktree remove`. If it is not registered, it skips Git removal and only cleans up the server record, leaving any existing directory untouched. Listing failure is not treated as absence.
- Success: `{"status": "deleted"}`. Attached sessions atomically receive the worktree's absolute path in `detached_working_dir`, have `worktree_id` cleared, and retain their effective `working_dir`. The worktree record is deleted in the same database transaction.

Agent and shell starts are serialized with the busy check and removal so they cannot start during the filesystem operation. Idle sessions do not block removal. Sessions with previously saved paths are not modified. Session identity, agent conversation identity, transcript, queued inputs, and settings are preserved.

Registration listing and, when needed, Git removal precede the database transaction. A database failure after successful Git removal rolls back metadata changes but cannot restore the directory; the worktree record and session associations remain. Retrying then cleans up the stale record because the path is no longer registered. Missing-directory errors propagate through the normal operation/harness path. Transcript access remains available, and there is no archive or restoration preflight.

There is no new WebSocket event. After deletion the initiating client should refetch session/worktree catalogs and then existence; other clients discover updated metadata through polling. Android's refresh must include worktrees as well as sessions and existence.

Confirmations should include both attached sessions and detached sessions whose `working_dir` equals the worktree path. A session with no managed worktree association is FORMER WORKTREE regardless of whether its preserved path is MISSING. Use the existence map to distinguish these independently.
