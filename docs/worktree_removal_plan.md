# Plan: allow worktree removal while preserving sessions

**Historical scope note:** this plan predates archive removal. Archive state and
explicit detachment have since been removed; the statements below that retain
them are superseded by [archive_removal_client_handoff.md](archive_removal_client_handoff.md).
The worktree-removal and saved-path behavior remains in place.

Status: server implemented; client implementation remains separate work.
See [worktree_removal_server_handoff.md](worktree_removal_server_handoff.md) for the server contract.

## Goal and scope

Allow the owner to delete a managed worktree without deleting its sessions or conversation history, regardless of whether those sessions are archived. Directory existence is separate from whether a managed worktree record exists.

Clients confirm deletion and explain that associated sessions may lose their working directory before sending the request. The server does not refuse deletion merely because sessions reference the worktree or its directory.

Archiving and the explicit session-detachment feature remain unchanged. Removing them is separate future work. This plan supersedes only the worktree-deletion restrictions and directory-existence reporting described in the existing archive/worktree documents.

## Session storage and worktree deletion

Use the existing `detached_working_dir` column to preserve a former worktree's absolute path.

When removing a worktree record:

1. Copy its path into `detached_working_dir` for each attached session.
2. Clear those sessions' `worktree_id`.
3. Delete the worktree record.

These database changes must be atomic. Do not leave dangling worktree IDs. The current foreign-key restriction no longer blocks deletion once the references are cleared; removing the constraint is not necessary for this design.

Preserve session identity, agent conversation identity, transcript, queued inputs, settings, and archive state. The effective working directory continues to be:

```sql
COALESCE(worktree.path, session.detached_working_dir, project.path)
```

Never silently move a former-worktree session into the project directory. Sessions already detached and using the same directory retain their saved paths.

Remove both worktree-deletion guards:

- Attached sessions block deletion.
- Live detached sessions whose preserved directory matches the worktree path block deletion.

Retain Git's existing safe-removal rules and resource-cleanup requirements; this plan does not authorize forced Git removal or removal of unrelated safety checks. Filesystem removal and database updates are not a single transaction: preserve the existing failure semantics when adapting this flow.

Before Git removal, list registrations with `git worktree list --porcelain -z`.
Listing failure blocks cleanup and changes no database metadata. A registered
path goes through normal safe Git removal. An unregistered path skips Git
removal and proceeds with database cleanup regardless of directory existence;
leave any directory at that path untouched. This permits cleanup after external
Git removal or after Git removal succeeded but database cleanup failed.

If a live session's preserved directory no longer exists, attempts to resume fail normally through the agent harness. Pi's missing-working-directory error already propagates to the user. Do not add special error-handling machinery.

Archived sessions remain archived, and their existing missing-directory unarchive checks remain in place. A recreated directory at the same path is checked as a directory, not as the original Git worktree.

## Directory-existence endpoint

Introduce a separate endpoint returning a map **S: absolute path -> boolean**. The route name is not yet chosen.

Build the set of unique paths from the union of:

- All project paths.
- All managed worktree paths.
- All session effective `working_dir` paths, including preserved former-worktree paths and archived sessions.

Check each unique path once with `os.path.isdir(path)`. This checks directory existence, not Git worktree validity. Include projects and worktrees with no sessions.

Remove `exists` from project and worktree responses. Clients derive those values from S:

```text
project existence  = S[project.path]
worktree existence = S[worktree.path]
```

The endpoint is a fresh filesystem snapshot, not stored state or a filesystem notification stream. Clients must refresh S alongside the catalogs used for rendering. Define request ordering/snapshot handling so a newly received path without a corresponding map entry is not silently interpreted as missing.

## Client rendering rules

For a session:

1. `session.working_dir == project.path`: retain the project-directory behavior; use the project's missing status.
2. `session.working_dir != project.path` and `session.worktree_id != null`: retain the attached-worktree behavior; use its worktree's existence status, now derived from S.
3. `session.working_dir != project.path` and `session.worktree_id == null`: use `S[session.working_dir]` directly. This is a former-worktree session, including sessions detached through the existing explicit action.

**FORMER WORKTREE / FWT** describes the lack of a managed worktree association. It does not mean the directory is missing.

**MISSING** means the effective directory does not exist. A former-worktree session can have either existence status; deletion may leave a directory behind, and directories may be recreated later.

Update clients after deletion so preserved session paths and cleared worktree associations are reflected without requiring a restart. The exact refresh/event mechanism remains to be chosen; do not change archive state as part of this update.

## Current refresh behavior to update

At the time of investigation:

- Desktop fetches projects, sessions, worktrees, and agents every three seconds while visible, and refreshes on startup, focus/visibility return, connection, and relevant actions.
- Android's project list polls projects every three seconds while active.
- Android's session list polls sessions every three seconds, but loads worktrees on loading/resuming the screen rather than on each poll.

Add S to the relevant refresh flows so missing-directory indicators remain current, including former-worktree sessions. Client deletion confirmations must describe affected sessions, including already-detached sessions using that directory.

## Implementation steps

1. Add the directory-existence endpoint and update affected clients/API consumers to derive existence from S instead of project/worktree response flags.
2. Allow worktree removal with attached or directory-dependent sessions, preserving attached sessions' paths and clearing their worktree associations atomically.
3. Update deletion confirmations, post-deletion refresh behavior, tests, and affected documentation.

Archive removal and removal of the explicit detachment endpoint/UI are not part of these steps.

## Verification

Cover at least:

- Deleting a worktree with attached sessions preserves history, archive state, and the original absolute working directory, while clearing the association.
- Live sessions and already-detached sessions using the directory no longer block removal solely because of that dependency.
- Existing Git safe-removal rules still apply.
- Unregistered stale records are cleaned up whether the directory is absent or
  present; any existing directory is untouched. Registration-listing errors
  block cleanup, and NUL-delimited paths are parsed without stripping/truncation.
- Failure during filesystem removal does not silently relocate or lose sessions.
- Shared working-directory paths are checked once per existence snapshot.
- Projects/worktrees with no sessions and former-worktree paths appear in S.
- Directory deletion and recreation change MISSING independently of FORMER WORKTREE.
- Missing-directory agent errors propagate normally for live sessions.
- Existing archive/unarchive and explicit-detachment behavior remains intact.
- Clients refresh session location metadata and directory-existence status after deletion.

## Settled implementation details

- Existence-map route: `GET /directory-existence`, returning the map directly.
- Clients fetch catalogs before existence and refresh again for unknown map keys;
  no new server event. The deleting client refetches after deletion; other clients
  use periodic polling.
- Reject deletion with `409` while any session using that directory has an active
  agent turn or shell command, including already-detached sessions. Serialize
  starts with the busy check and removal; idle dependencies do not block removal.
