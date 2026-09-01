# Client hand-off: first-class worktrees

Status: **server side implemented** (schema, `_migrate_v4`, endpoints, tests,
README). This is the client-side spec, for an implementing agent working in a
client repo — the Android app
([rauaap/agent-ui-server-android](https://github.com/rauaap/agent-ui-server-android))
or the desktop client served via `WEB_ROOT`. It is self-contained; you should
not need to read the server source to build against it.

Every path below is relative to the server root (`http://<wireguard-ip>:8000`).

## What changed

A git worktree used to be something a session created, owned, and destroyed. It
is now its **own resource**, created and removed through its own endpoints, that
sessions attach to. Several sessions can share one worktree, and a worktree
outlives the sessions that used it.

**If your client already implements the old flow, this is a breaking change.**
Three things to remove:

| Gone | Replacement |
|---|---|
| `worktree: { path, branch }` block in `POST /sessions` | `POST /worktrees` first, then `worktree_id` in `POST /sessions` |
| `owns_worktree` on session objects | `worktree_id` — `null` means the session runs in the project directory |
| `worktree_removed` / `worktree_error` in the `DELETE /sessions/{id}` response | Nothing. That response is now `{"status": "deleted"}`; removal moved to `DELETE /worktrees/{id}` |

`working_dir` is **unchanged** on every session object and still means "the cwd
the agent runs in." It is now computed server-side rather than stored, which is
invisible to you — keep rendering it exactly as before.

The `worktree_errors` array in the `DELETE /projects` response lost its
`"session"` key (worktrees no longer belong to one session) and kept `"path"`
and `"error"`.

## The resource

```jsonc
{
  "id": 1,
  "project_id": 1,
  "path": "/projects/app-fix-login",
  "branch": "fix-login",
  "created_at": "2026-08-31T09:14:02Z",
  "session_count": 2,
  "exists": true
}
```

- `id` and `project_id` are JSON **numbers**, not strings. Read
  [client_ids.md](client_ids.md) before comparing, storing or rendering one —
  the number/string distinction has sharp edges in a browser.
- `branch` is the branch the worktree was **created on**, and may be `null` for
  worktrees carried over by the server's migration. It is not live state: an
  agent working in the worktree can switch branches and nothing here updates.
  Label it accordingly ("created on `fix-login`"), or omit it when `null` rather
  than rendering "null".
- `session_count` may be `0`. That is an ordinary state — an unused worktree you
  can still attach to — not an error or a leak. Do not present it as one.
- `exists` is a `stat` of `path` at request time. `false` means the directory
  was removed outside the app; see [Worktrees whose directory is
  gone](#worktrees-whose-directory-is-gone).

## Endpoints

### `GET /worktrees`

Optional `?project_path=/projects/app` filter (URL-encode it). Returns an array
of the objects above, newest first. Unknown `project_path` → `404`.

### `POST /worktrees` → `201`

```jsonc
{ "project_path": "/projects/app",
  "path": "/projects/app-fix-login",
  "branch": "fix-login" }
```

Runs `git worktree add -b <branch> <path>`, always cutting a **new** branch off
the project's current HEAD. Attaching to an existing branch is not supported;
see [Out of scope](#out-of-scope-server-side).

Returns the created worktree. Errors:

| Status | `detail` | What to show |
|---|---|---|
| `404` | `Project not found` | Shouldn't happen from the UI; refresh the project list |
| `400` | `project is not a git repository` | Pre-empt with `is_git_repo` (below) — treat reaching this as a bug in your gating |
| `400` | `invalid branch name` | Inline on the branch field |
| `400` | `worktree path must differ from the project directory` | Inline on the path field |
| `400` | `worktree path already exists and is not an empty directory` | Inline on the path field. An **empty** directory is accepted — that is git's own rule |
| `400` | `Could not create worktree directory: [Errno 13] Permission denied: '…'` | Inline on the path field — the path is somewhere the server cannot write. Carries a real errno, so it is worth showing verbatim |
| `409` | `a worktree already exists at /projects/app-fix-login on branch fix-login` | You already have this worktree — offer to use it instead of creating it. See [Deriving the path](#deriving-the-path) |
| `400` | git's own stderr, e.g. `fatal: a branch named 'fix-login' already exists` | Inline on the branch field. Pass git's message through; it is more specific than anything you'd write |

Nothing is created on disk when any of these fire.

### Deriving the path

`path` must be **absolute** — a relative one is a `400`. Resolve it client-side;
the server has no notion of a path relative to the project, and no template
syntax. Everything needed to expand one is already in the project object from
`GET /projects` (`path`, `name`) plus the branch the user typed.

Paths are normalised **lexically** on arrival (`os.path.normpath`, no symlink
resolution), so naive joining is enough — you do not need to collapse `..`
yourself. `/projects/app` + `../app-fix-login` can be sent as
`/projects/app/../app-fix-login` and is stored as `/projects/app-fix-login`.

Do normalise for **display**, though: the form should preview the path the user
will actually get, and the value you send back on a later request should match
what `GET /worktrees` returns, or your own comparisons will miss.

A template that maps a branch name to a path produces the same path twice for
the same branch, so the `409` above is a routine outcome rather than an edge
case. Handle it by finding the existing worktree in your
`GET /worktrees?project_path=…` list (match on `path`, after normalising) and
offering it as the selection — attaching a session to it is almost always what
the user meant. The `409` body is a plain string and does not carry the id.

### What the form needs in hand

Strictly one thing: the project's **`path`**. It is the only input to the
template (`%P` is its dirname, `%N` its basename) and the only project field the
request carries — `POST /worktrees` is path-keyed, like every other project-scoped
endpoint. The project's `id` and `name` are not involved.

Everything else is about pre-empting failures rather than building the request:

| Also load | Why |
|---|---|
| `is_git_repo`, `exists` (from `GET /projects`) | Whether to offer the menu at all. A project whose directory is gone fails inside `git worktree add`, which is a confusing place to learn it |
| `GET /worktrees?project_path=…` | Flag a colliding path *as the user types the branch* rather than after a round trip — and it is what you need to recover from the `409` anyway (match on `path`, offer the existing worktree) |
| The template string from settings | Seeds the path field |
| A branch seed | The session name, when the form is opened from the new-session dialog |

Two things the client cannot determine and should not try to:

- **The project's current branch.** Worktrees are always cut from HEAD, but
  nothing exposes what HEAD is; `is_git_repo` is the only git-derived field on a
  project. Say "a new branch off the project's current HEAD", not "off `main`".
  Naming it would need a new endpoint.
- **Whether a branch name is valid.** That is `git check-ref-format`, run
  server-side, surfacing as a `400` on submit. Do not hand-roll a regex — using
  git's own rules rather than an approximation is deliberate, and a client-side
  check would reject names git accepts.

### The path template

A **settings** field holds a template string; the **create-worktree form** shows
it fully expanded, in an editable field. All of this is client-side — the server
has never heard of it.

Specifiers, expanded against the project the form was opened from. For a project
at `/projects/app` named "My App":

| Token | Expands to | Source | Example |
|---|---|---|---|
| `%P` | the project's **parent** directory | `dirname(project.path)` | `/projects` |
| `%N` | the project directory's **final component** | `basename(project.path)` | `app` |
| `%B` | the branch, **slug-safe** | the branch field, `/` → `-` | `feature-fix-login` |
| `%b` | the branch **verbatim** | the branch field | `feature/fix-login` |

`%P` and `%N` split at the parent so they compose: `%P/%N` reconstructs the
project directory, `%P/%N-%B` is the sibling default, and `%P/worktrees/%N-%B`
puts them all under one directory. A single "project directory" token would let
you write the first two but not the third.

`%N` is the path's last segment, **not** `project.name`. The display name merely
*defaults* to that segment and can be anything — "My App", with spaces — so it
has no business in a path.

`%B` and `%b` are separate for the same reason. Slashes are legal in branch
names, common in practice, and accepted by `git check-ref-format`, so `%P/%N-%b`
with branch `feature/fix-login` expands to `/projects/app-feature/fix-login` — a
directory nested two levels down rather than the sibling the user pictured. The
server creates exactly that, correctly: it is a legal absolute path and
`git worktree add` makes the parents. Since every use of a template is a path,
**`%B` is the slugged one** so the spelling people reach for is the safe one;
`%b` is there for deliberately nested layouts.

**`~` and `$HOME` are not supported, and the client must not expand them.** The
server does no tilde or variable expansion at all: a leading `~/…` or `$HOME/…`
is a `400 path must be absolute`, and an embedded one is taken literally as a
directory with that name. Expanding it client-side would resolve the *client's*
home — the phone's, or nothing at all in a browser — and send a path that is
missing or wrong on the server, failing silently instead of loudly. It is also
wrong in the Compose deployment specifically, where the server's `$HOME` is a
credentials volume and projects are bind-mounted at `/projects`.

`%P` covers the cases a home directory would: `%P/%N-%B` for a sibling,
`%P/worktrees/%N-%B` to group them, both anchored wherever the projects actually
are. A home-relative template would need a server-supplied token, since only the
server knows its own home; there is no such token today. Validate in the
settings field that a template starts with `/` or `%P`, so the user sees the
problem there rather than as a `400` on submit.

Join the expanded pieces with a real path join rather than string concatenation.
For a project directly under the root (`/app`), `%P` is `/` and `%P/%N-%B`
concatenated gives `//app-fix` — the server collapses that leading `//` for you,
but your own comparisons against `GET /worktrees` will not.

Settings shows a live example of the expansion. It has no project in hand, so
expand against a placeholder (or the most recently used project) and label it as
an example.

### The leash

In the create-worktree form the path field starts fully expanded and stays tied
to the branch field: editing the branch re-expands the path live. **Hand-editing
the path breaks the leash** — from then on the path is whatever the user typed,
and editing the branch no longer touches it. Same seed-then-break-the-link
pattern the server documents for a project's `name` vs its `path`.

Two things worth getting right:

- **These are independent leashes, not a chain.** If the branch is itself seeded
  from the session name, breaking the *branch* leash (hand-editing the branch)
  must not break the *path* leash — the path stays tied to the branch and keeps
  following it. Only a hand-edit of the path field itself breaks that one.
- **Offer a way back.** Once broken, there is no way to re-leash except
  reopening the form. A "reset to default" affordance next to the path field is
  cheap and saves a user who edited by accident.

### `DELETE /worktrees/{id}`

Removes the directory (`git worktree remove`, **never** `--force`) and the row.
`{"status": "deleted"}` on success.

| Status | `detail` | What to show |
|---|---|---|
| `404` | `Worktree not found` | Already gone; refresh |
| `409` | `2 session(s) are still using this worktree: fix login, review` | See [Deleting a worktree](#deleting-a-worktree) |
| `409` | `fatal: '…' contains modified or untracked files, use --force to delete it` | See [Deleting a worktree](#deleting-a-worktree) |

On any `409`, **nothing was removed and the row is still there** — re-render
from the unchanged state rather than optimistically dropping it.

### `POST /sessions` → `201`

```jsonc
{ "name": "fix login", "project_path": "/projects/app",
  "agent": "claude-code", "worktree_id": 1 }
```

`worktree_id` is optional; omit it (or send `null`) for a session that runs in
the project directory. Errors specific to it:

| Status | `detail` | Meaning |
|---|---|---|
| `404` | `Worktree not found` | Stale id — refresh the worktree list |
| `400` | `worktree belongs to a different project` | Your picker offered a worktree from another project |

## UX

### New-session dialog

Replace the old "Create a git worktree" toggle + two text inputs with a
**worktree picker**, since worktrees now exist independently:

- Default option: **"Project directory"** (`worktree_id` omitted) — the
  behaviour for a session with no worktree.
- Then each worktree from `GET /worktrees?project_path=…`, showing `path`,
  `branch` when non-null, and `session_count` when non-zero ("2 sessions here").
  Sharing a worktree is a supported choice, not a warning.
- A **"New worktree…"** action that opens the create form and, on success,
  selects the worktree it just created.

Show the picker only when the project's `is_git_repo` is `true` (from
`GET /projects`). That flag is a cheap `exists` on `<path>/.git` and only a
hint — `POST /worktrees` runs the real check — but it is what keeps the "not a
git repository" error rare rather than routine.

### Create-worktree form

Two inputs, both pre-filled and both editable — the same seed-then-break-the-link
pattern used for project name vs path:

- **path**, seeded by expanding the [path template](#the-path-template) — by
  default a sibling of the project directory, `%P/%N-%B`: project
  `/projects/app` + branch `fix-login` → `/projects/app-fix-login`. Editing it
  breaks the leash. Accept a relative path here and resolve it against the
  project directory before sending; see [Deriving the
  path](#deriving-the-path).
- **branch**, seeded from a slug of whatever the user is naming (the session
  name, if you open this from the new-session dialog).

In a Docker/Compose deployment the path **must** stay under the `/projects`
bind mount or the directory will not be visible inside the container. The
sibling default satisfies this; a user who edits the path can break it, so it is
worth a hint rather than a hard validation.

### Deleting a worktree

Two distinct `409`s, and neither is a failure the user should read as an error:

**Sessions still attached.** The server lists their names in `detail`. Offer to
show or delete those sessions, then retry. Do not offer to force it — there is
no force for this case.

**Dirty tree.** git counts **untracked** files as dirty, so any worktree an
agent did real work in will refuse. This is the *common* path, not an edge case.
Phrase it as information:

> "`app-fix-login` has uncommitted work, so it was left in place. Commit or
> discard the changes there, then try again."

Do not surface git's raw `use --force to delete it` suggestion as an action —
the server does not accept a force flag, deliberately. (It is on the roadmap as
an explicit "delete anyway"; until then, the honest answer is that the user
resolves it in the worktree.)

### Deleting a session

`DELETE /sessions/{id}` returns `{"status": "deleted"}` and touches nothing on
disk. If your UI previously reported worktree outcomes here, remove that
entirely.

Worth a line of copy where a user deletes the last session on a worktree, so the
worktree does not feel abandoned — something like "the worktree
`app-fix-login` is still there" with a link to it. Not a prompt to delete it:
finishing a session does not mean finishing with the branch.

### Deleting a project

`DELETE /projects` sweeps sessions and then worktrees, and reports:

```jsonc
{ "status": "deleted", "sessions_deleted": 3,
  "worktrees_removed": 1,
  "worktree_errors": [
    { "path": "/projects/app-fix-login",
      "error": "fatal: '…' contains modified or untracked files, …" }
  ] }
```

Always `200`. Rows are gone regardless; directories in `worktree_errors` are
still on disk. Report them as "left in place" alongside the existing note that
the project's own directory is untouched — same category of message.

### Worktrees whose directory is gone

`exists: false` means the row is still there but the directory was removed
outside the app. Two things follow:

- **Attaching a session to it still succeeds.** The server deliberately does not
  `mkdir` — that would hand the agent a plain directory dressed up as a
  worktree. The session will fail on its first turn instead. Warn, or disable
  the option in the picker.
- **`DELETE /worktrees/{id}` succeeds and tidies the row away.** git prunes its
  own admin files and exits 0. This is the recovery path — offer it as "clean
  up" rather than "delete", since there is nothing left to delete.

## Session objects

Only one field is new:

```jsonc
{ "id": 12, "name": "fix login", "project_id": 1,
  "worktree_id": 1,                              // null when in the project directory
  "working_dir": "/projects/app-fix-login",      // unchanged meaning
  "agent": "claude-code", "agent_session_id": null,
  "status": "idle", "created_at": "…", "last_active_at": "…",
  "auto_approve_write": false, "auto_approve_command": false }
```

Mark sessions with a non-null `worktree_id` in the list and detail views, and
show `working_dir`, so it is obvious the session is not running at the project
root. Grouping sessions by `worktree_id` within a project is now a meaningful
view; `project_id` remains the grouping that matches the project card's
`session_count`.

## Out of scope (server side)

Do not design UI that implies these work — none of them have an endpoint:

- Attaching a worktree to an **existing** branch or an arbitrary commit-ish.
- Forced removal (`--force`) of a dirty worktree.
- Moving a session to a different worktree after it is created. `worktree_id` is
  set at creation and there is no `PATCH` for it. (`PATCH /sessions/{id}` still
  handles `name` and the auto-approve toggles only.)
- Renaming or moving a worktree's `path`.
- Adopting a worktree that already exists on disk into the app.
- Any project endpoint keyed by `id` — the HTTP API stays path-keyed.
