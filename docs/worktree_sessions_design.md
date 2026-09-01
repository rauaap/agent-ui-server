# Design: per-session git worktrees — **superseded**

Status: **superseded** by first-class worktrees. Do not implement from this
document. The server-side model it describes shipped and was then reversed; the
current one is documented in the README ([Worktrees](../README.md#worktrees) and
[Data model](../README.md#data-model)), and the client work is specified in
[worktree_client_handoff.md](worktree_client_handoff.md).

The full text is in git history (`git show 28a0803:docs/worktree_sessions_design.md`).

## What it got wrong, and why that is worth remembering

The document argued that creating a worktree had to be part of `POST /sessions`
rather than a request of its own, because **ownership has to be atomic**: with
two requests, a client that died in between would leave a worktree on disk that
no session row claimed, so `owns_worktree` would never be set and nothing would
ever clean it up.

That reasoning was sound given its own premise — that a worktree exists only as
a `working_dir` string on a session row. It stops being sound the moment a
worktree has a row of its own, because then the "orphan" it feared is just a
worktree with no sessions attached: visible in `GET /worktrees`, removable
through `DELETE /worktrees/{id}`, and a perfectly ordinary thing to have. The
coupling was compensating for the missing entity.

Two smaller things fell out of the same premise:

- **`owns_worktree`** recorded "the server created this directory" because a
  path string cannot say who made it. It was needed when `POST /sessions` took
  an arbitrary `working_dir` and a user could point a session at a worktree they
  had made by hand. The same commit that added the flag also replaced
  `working_dir` with `project_path` and made an unregistered path a `404` —
  which made an unowned session unconstructible, leaving the flag with no live
  case to serve. Provenance is now recorded structurally: a `worktrees` row
  exists, therefore we created it.
- **`working_dir` as a stored column** was justified as avoiding a join "to buy
  a move-project feature that does not exist yet." Once several sessions can
  share one worktree, the join buys something real — one source of truth for a
  path that would otherwise be copied across every attached session. It is now
  derived as `COALESCE(worktrees.path, projects.path)`.

The lesson worth carrying: both mistakes were a schema expressing state that
nothing could construct, and each was defended on its own terms without
re-deriving whether the case it protected still existed.
