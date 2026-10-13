# Server handoff: archive removal

Server archive and explicit session-detachment behavior has been removed. Update desktop, Android, and the Python API client together; no legacy API support is provided.

## Removed contract

- Project and session PATCH requests no longer have `archived`.
- Project and session responses no longer have `archived_at`.
- Project responses no longer have `archived_session_count`.
- Project PATCH responses no longer have `sessions_affected`.
- `POST /sessions/{id}/detach-worktree` no longer exists.
- Session WebSockets no longer send `archived` or `worktree_detached`, including on connection. Initialization is persisted transcript replay, then `input_queue`, then `status`; live publications follow that boundary.

Remove archive screens, actions, badges, sorting, list/search filtering, composer/input/image guards, and explicit detachment actions. All retained projects and sessions belong in ordinary lists. Project `session_count` counts all sessions; `last_active_at` considers all session activity. Session input, Bash, and inter-agent messaging have no archive guard. Existing busy/approval/settings rules remain unchanged.

## Worktree behavior remains

[Worktree removal and directory existence](worktree_removal_server_handoff.md) remains authoritative. Deleting a worktree automatically preserves each attached session's absolute working directory and clears its `worktree_id`, retaining its identity, transcript, queue, and settings. The internal `detached_working_dir` column remains for this purpose and for previously saved paths; it is not archive state.

Keep MISSING/former-worktree presentation, existence-map refreshes, and deletion confirmations. Sessions with missing directories remain readable and configurable. Attempts to run there fail through the ordinary operation/harness error path; the server does not recreate their former worktree directory, relocate them, or introduce a replacement read-only state.

## Deployment database

With the server stopped and a backup made, the owner manually removes `projects.archived_at`, `sessions.archived_at`, and `sessions.archived_with_project` during rollout. There is no startup migration or migration script. No unarchiving step is needed: the updated code treats all retained records normally. Preserve all other columns and records, especially saved working-directory paths, conversation IDs, transcript events, and queued inputs.
