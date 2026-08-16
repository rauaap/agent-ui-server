# Design: per-session git worktrees (and the projects rework they need)

Status: **server side implemented** (schema, migration, `git.py`, endpoints,
tests, README). The client work in the last section is still to do. This
document is a hand-off spec for an implementing agent; it is self-contained.

Two open questions in it were answered before implementation:

- The one-request flow below was **confirmed** — `POST /sessions` with an
  optional `worktree` block.
- Step 3 of `POST /sessions` went the *other* way from what this document
  suggests: an unknown `project_path` is a **404**, not an adopted project.
  `sessions.project_id` is a real foreign key now, so a session at an
  unregistered path would be exactly the invisible-but-present row the rework
  exists to remove. Clients create the project first.

## Problem

Creating a session should be able to create a **git worktree** for that session,
so two sessions on the same project can work on separate branches without
fighting over one checkout. In the client: a toggle in the new-session dialog
that, when on, enables a path input (and a branch input). If the toggle is on,
the worktree is created first; if that fails, no session is created.

The blocker is the schema. `sessions.working_dir` is doing two unrelated jobs:

1. **The cwd the agent runs in** — `agent.py:196`, `agent.py:765`,
   `agent.py:1019`, `agent.py:1027`, and bash mode at `main.py:473`.
2. **The link to `projects`** — there is no foreign key; the join is string
   equality `s.working_dir = p.path` (`db.py:144`), and `delete_project` repeats
   the match in Python (`main.py:163`).

So pointing `working_dir` at a worktree would silently detach the session from
its project: gone from the project card's `session_count` / `last_active_at`,
and skipped by `delete_project`'s teardown sweep, stranding its scrollback rows
with nothing in the UI able to reach them.

**Therefore this change is two things:** give projects a real identity and make
the session→project link explicit, then add worktrees on top of the fixed model.

## Decisions already made

Recorded so they are not re-litigated during implementation.

- **Projects get a uuid `id`; `path` stays `UNIQUE` but stops being identity.**
  This is what makes a project's path editable later (`UPDATE projects SET path`
  instead of delete-and-recreate, which would take the sessions with it).
- **`sessions.project_id` is `NOT NULL` with a real FK.** Today a session can be
  created at a path no project row matches; such a session is invisible in the
  UI (`delete_project`'s docstring: sessions "are reachable only through their
  project") while still holding rows. That is a bug, not a feature.
- **`working_dir` is `NOT NULL` and always populated**, seeded from
  `project.path` at create time. Not nullable-meaning-inherit: that would make
  every read of a session's cwd depend on a join, to buy a move-project feature
  that does not exist yet. When it lands, moving a project is
  `UPDATE sessions SET working_dir = ? WHERE project_id = ? AND owns_worktree = 0`.
- **`owns_worktree` is an explicit column, not inferred** from a NULL cwd and
  not a `directories` table. What teardown needs to know is *provenance* — "the
  server created this and owns its cleanup" — which is a fact about the
  session↔directory relationship, not a property of the directory. A worktree
  the user made by hand is a worktree, but is not ours to delete.
- **Removal never forces.** `git worktree remove` without `--force`, so a dirty
  tree refuses and is reported. Deleting a session must not be able to destroy
  uncommitted work.

  Be aware of how often this will fire: git counts **untracked** files as dirty
  (*"contains modified or untracked files, use --force to delete it"*, verified
  2.47.3), and an agent that created so much as one new file leaves the tree
  untracked-dirty. So refusing to remove is the *common* case, not the edge
  case, and the client message has to read as normal information rather than an
  error. If that becomes tiresome, the follow-up is an explicit "delete anyway"
  affordance that passes `--force` — deliberately not v1, because the default
  must never be the destructive one.
- **Project deletion uses the same policy**, via the existing teardown sweep.
- **Branch comes from the client**, pre-filled with a slug of the session name
  and editable — the same seed-then-break-the-link pattern `CreateProjectRequest`
  documents for project name vs path (`main.py:35-39`).
- **A non-repo project is a 400**, and the client hides the toggle for projects
  it can see are not repos, so the error is rare rather than routine.

## Deviation from the described flow — please confirm

The feature was described as *two* requests: create the worktree, and if that
succeeds create the session. This spec instead makes it **one** request —
`POST /sessions` with an optional `worktree` block — because ownership has to be
atomic. With two requests, a client that dies (or a user who closes the dialog)
between them leaves a worktree on disk that no session row claims, so
`owns_worktree` is never set and nothing will ever clean it up. One request lets
the server roll the worktree back if the insert fails.

The client-side UX is identical either way: same toggle, same inputs, same
"worktree failed → no session created" outcome, one round trip instead of two.
If a standalone `POST /worktrees` is wanted for other reasons, it can be added
later; it is not needed for this feature.

## Schema

```sql
CREATE TABLE projects (
    id         TEXT PRIMARY KEY,      -- uuid4, like sessions.id
    path       TEXT NOT NULL UNIQUE,  -- still how the HTTP API addresses a project
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE sessions (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    working_dir          TEXT NOT NULL,              -- the cwd, nothing else
    owns_worktree        INTEGER NOT NULL DEFAULT 0, -- we created it, we clean it up
    agent                TEXT NOT NULL,
    agent_session_id     TEXT,
    status               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    last_active_at       TEXT NOT NULL,
    auto_approve_write   INTEGER NOT NULL DEFAULT 0,
    auto_approve_command INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_sessions_project_id ON sessions(project_id);
```

`scrollback` is unchanged.

The FK is declared `ON DELETE CASCADE` as a backstop only — `delete_project`
must keep sweeping sessions through `teardown_session` explicitly, because
teardown does much more than delete a row (stops the agent, cancels the bash
task, closes subscribers, and now removes worktrees).

### Migration

The existing inline `ALTER TABLE ADD COLUMN` pattern (`db.py:66-83`) cannot do
this: SQLite only allows `ADD COLUMN ... REFERENCES` when the default is NULL,
and `projects` needs a new primary key. Both tables need the documented 12-step
rebuild. Put it in a private `_migrate_v2` called from `init()`.

Detection: run only if `id` is not among `PRAGMA table_info(projects)`. On a
fresh database the `CREATE TABLE IF NOT EXISTS` statements already produce the
new shape, so the migration is a no-op — keep it that way, and keep the existing
`claude_session_id` rename and auto-approve `ADD COLUMN`s ahead of it so an
ancient database migrates through both.

Sequence, with the SQLite gotchas that matter:

1. `PRAGMA foreign_keys = OFF` — **must be outside a transaction**, it is
   silently a no-op inside one. `Database.__init__` turns it on at connect, so
   turn it off, migrate, turn it back on.
2. `BEGIN`.
3. Create `projects_new` / `sessions_new` with the schema above.
4. Copy `projects` → `projects_new`, minting a `uuid4()` per row.
5. **Adopt orphans.** For each distinct `sessions.working_dir` with no matching
   project, insert a project (`name` = basename, `created_at` = `utc_now()`).
   Match on the **normalised** path (`os.path.normpath`) so a legacy
   `/p/demo/` adopts into the existing `/p/demo` project instead of creating a
   duplicate — `create_session` never normalised `working_dir` (`main.py:185`),
   only `create_project` did (`main.py:610`).
6. Copy `sessions` → `sessions_new`, resolving `project_id` from that same
   normalised match, keeping `working_dir` verbatim, `owns_worktree = 0` (we
   did not create any of these).
7. `DROP TABLE projects; DROP TABLE sessions;`
8. `PRAGMA legacy_alter_table = ON` around the two
   `ALTER TABLE ..._new RENAME TO ...` statements, then back off. Without it the
   rename tries to fix up references in other tables and can rewrite
   `scrollback`'s FK; with it, `scrollback`'s existing
   `REFERENCES sessions(id)` simply resolves to the new table.
9. Create the index.
10. `PRAGMA foreign_key_check` — abort the migration if it returns any row.
11. `COMMIT`, then `PRAGMA foreign_keys = ON`.

`owns_worktree = 0` for every migrated row is deliberate: pre-existing sessions
that happen to sit in a worktree were not created by us, so we must not start
deleting their directories.

## `git.py` (new module)

Do **not** route these through `shell.run_command` — it runs `bash -lc` with an
interpolated string, so a path or branch name containing shell metacharacters
would be an injection. Use `asyncio.create_subprocess_exec` with an argv list,
mirroring `shell.py`'s convention of returning a result rather than raising.

```python
async def is_git_repo(path: str) -> bool
async def check_branch_name(name: str) -> bool
async def add_worktree(repo: str, path: str, branch: str) -> str | None
async def remove_worktree(repo: str, path: str) -> str | None
```

- `is_git_repo` — `git -C <repo> rev-parse --is-inside-work-tree`, true on exit 0
  with `true` on stdout.
- `check_branch_name` — `git check-ref-format --branch <name>`, so we reject
  `..`, spaces, leading `-` and friends with git's own rules rather than a
  hand-rolled regex.
- `add_worktree` — `git -C <repo> worktree add -b <branch> <path>`. Returns
  `None` on success, else git's stderr (trimmed, capped) for the 400 body.
- `remove_worktree` — `git -C <repo> worktree remove <path>`, **no `--force`**.
  Returns `None` on success, else stderr. A worktree whose directory was already
  deleted by hand needs no special case: git exits 0 and cleans up its own admin
  files (verified, 2.47.3).

Give every call a modest timeout (`git.GIT_TIMEOUT_SECONDS`, default 30) so a
hung git cannot wedge a request, and cap captured output the way `shell.py`
does.

## `db.py`

- `_row_to_session` coerces `owns_worktree` to bool as well. Rather than
  extending `AUTO_APPROVE_CATEGORIES` (which also drives `set_auto_approve`),
  add a separate `BOOL_COLUMNS` tuple and iterate that.
- `create_project` returns the row including `id`.
- `get_project(path)` stays (path lookup is the HTTP surface); add
  `get_project_by_id(project_id)`.
- `_PROJECT_QUERY` joins `s.project_id = p.id` and selects `p.id` alongside
  `p.path`, `p.name`.
- `create_session(name, project_id, working_dir, agent, owns_worktree=False)`.
- `list_sessions` / `get_session` select `project_id`, `working_dir`,
  `owns_worktree`.
- Add `list_sessions_for_project(project_id)` and use it in `delete_project`,
  replacing the Python-side `working_dir` filter at `main.py:163`.
- `delete_project(path)` keeps its signature; resolve to the id internally.

## `main.py`

### `CreateSessionRequest`

```python
class WorktreeSpec(BaseModel):
    path: str = Field(min_length=1)
    branch: str = Field(min_length=1, max_length=200)

class CreateSessionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    project_path: str = Field(min_length=1)   # was: working_dir
    agent: str = "claude-code"
    worktree: WorktreeSpec | None = None
```

Accept `working_dir` as a deprecated alias for `project_path` (a
`model_validator(mode="before")` that fills one from the other) so the currently
deployed client keeps working; the session dict in every response still carries
`working_dir`, so read paths are unaffected.

### `POST /sessions`

1. Unknown `agent` → 400 (unchanged). Trim and validate `name`.
2. `project_path = normalize_project_path(payload.project_path)`.
3. Resolve the project. If no row exists, **adopt it** — create the project the
   way `POST /projects` would. This keeps the current contract that a session
   can be created at any absolute path while still satisfying `NOT NULL`;
   the alternative is a 404, which would break API clients that create sessions
   without registering a project first. *(Flagged: easy to flip if you'd rather
   it 404.)*
4. **No `worktree`:** `working_dir = project.path`, `mkdir -p` exactly as today
   (`main.py:187-193`), `owns_worktree = 0`. Behaviour is unchanged from today.
5. **With `worktree`:**
   a. `worktree_path = normalize_project_path(spec.path)` — absolute, normalised,
      not `/`.
   b. 400 if `worktree_path == project.path`.
   c. 400 if `worktree_path` exists and is not an **empty** directory. This
      matches git's own rule — `worktree add` happily takes over an existing
      empty directory and only refuses a non-empty one (verified, 2.47.3) — and
      failing here gives a better message than parsing git's.
   d. 400 `"project is not a git repository"` if `not await
      git.is_git_repo(project.path)`.
   e. 400 `"invalid branch name"` if `not await git.check_branch_name(branch)`.
   f. `error = await git.add_worktree(project.path, worktree_path, branch)`;
      on error → 400 with git's stderr in `detail`. An already-existing branch
      lands here, which is the intended v1 behaviour (see Out of scope).
   g. On success create the session with `working_dir = worktree_path` and
      `owns_worktree = 1`. **If the insert raises, remove the worktree before
      propagating** — otherwise the rollback leaks the directory.

No `mkdir` in the worktree case: `git worktree add` creates the directory
itself.

### `teardown_session`

After `db.delete_session(session_id)` (the row goes regardless), if
`session["owns_worktree"]`:

- look up the project via `db.get_project_by_id(session["project_id"])` — do
  this **at the top of the function**, since `delete_project` deletes the
  project row after its sweep;
- `error = await git.remove_worktree(project["path"], session["working_dir"])`.

Change the return type to `str | None` (the error) so callers can report it. A
missing project row → skip removal, no error.

### Responses

- `DELETE /sessions/{id}` →
  `{"status": "deleted", "worktree_removed": bool, "worktree_error": str | None}`.
  Still 200 when removal fails: the session *is* deleted, and the dirty tree is
  information, not a failure of the request.
- `DELETE /projects` → add `"worktrees_removed": int` and
  `"worktree_errors": [{"session": name, "path": str, "error": str}]` to the
  existing `{"status", "sessions_deleted"}`.

### `with_existence`

Add `"is_git_repo": os.path.exists(os.path.join(path, ".git"))` — one cheap
`exists` next to the existing `isdir`, no subprocess, and `.git` as a *file*
(the project itself being a worktree) counts. This is only a hint for the client
to hide the toggle; step 5d remains the authoritative check. A repo whose root
is above the project directory reads as false here — acceptable, since creating
a worktree from a subdirectory of a repo is not something we want to encourage.

## Client (agent-ui-desktop, separate repo)

- New-session dialog gets a **"Create a git worktree"** toggle, shown only when
  the project's `is_git_repo` is true.
- Toggle on enables two inputs:
  - **path**, pre-filled with a sibling of the project directory derived from
    the session name — e.g. project `/home/me/app`, session "fix login" →
    `/home/me/app-fix-login`. Editable; editing breaks the link to the name.
  - **branch**, pre-filled with the same slug (`fix-login`). Editable.
- Submit posts once to `/sessions` with the `worktree` block. A 400 keeps the
  dialog open and shows `detail` — the session was not created.
- Session list/detail marks worktree sessions and shows `working_dir`, so it is
  obvious the session is not running at the project root.
- Deleting a worktree session surfaces `worktree_error` when present ("the
  worktree has uncommitted changes and was left in place"). Phrase it as a
  notice, not a failure — see the untracked-files note above; this is the
  expected outcome for most sessions that did any work.

## Tests (`tests/test_core.py`)

Migration:

- Build an old-shaped database by hand (path-PK `projects`, `working_dir`-linked
  `sessions`, plus `scrollback` rows), open `Database`, and assert: projects have
  uuid ids, every session resolves to the right `project_id`, `working_dir` is
  unchanged, `owns_worktree` is false, scrollback survives and still cascades.
- An orphan session (no project row) is adopted into a newly created project.
- A session whose `working_dir` is `/p/demo/` joins the existing `/p/demo`
  project rather than creating a second one.
- Opening an already-migrated database twice is a no-op.

Project/session join:

- `session_count` and `last_active_at` aggregate through `project_id`, including
  for a session whose `working_dir` is a worktree elsewhere.

Worktree creation (real `git init` in a `tmpdir` — no network needed):

- Toggle off → unchanged behaviour, `owns_worktree` false, cwd is the project.
- Toggle on → directory exists, is a worktree, branch exists, `owns_worktree`
  true, `working_dir` is the worktree, project aggregates still count it.
- Non-repo project → 400, **no session row and no directory created**.
- Existing target path → 400. Invalid branch name → 400. Existing branch → 400.
- Insert failure after a successful `add` (monkeypatch `db.create_session` to
  raise) removes the worktree.

Teardown:

- Delete a session with a clean worktree → directory gone, `worktree_removed`
  true.
- Delete a session with an uncommitted change → row gone, directory **still
  there**, `worktree_error` populated.
- Delete a session whose worktree directory was removed by hand → reported as
  success (the prune path).
- `owns_worktree = 0` session pointed at a worktree → directory untouched.
- `DELETE /projects` removes each session's worktree and reports the counts.

## Out of scope (v1)

- Attaching a worktree to an **existing** branch (`git worktree add` without
  `-b`) or to an arbitrary commit-ish. v1 always creates a new branch off the
  current HEAD.
- A standalone `POST /worktrees` endpoint.
- Moving a project's path (the reason `projects.id` exists, but no endpoint yet).
- Forced removal, or any UI for resolving a dirty worktree.
- Listing/adopting worktrees that already exist on disk.
- id-keyed project endpoints — the HTTP API stays path-keyed, with `id` internal.
