# agent-ui-server

A lightweight, self-hosted backend for driving [Claude Code](https://github.com/anthropics/claude-code)
agent sessions on a VPS from your phone. It replaces the Termux + tmux workflow:
spawn a session, stream its output, and approve or deny tool requests from a
native **Android app** over WireGuard.

> **Scope.** This repo is the backend / control plane: a FastAPI app, a Claude
> Code adapter, and a SQLite store. It is implemented and tested. The client is
> a separate Android app —
> [rauaap/agent-ui-server-android](https://github.com/rauaap/agent-ui-server-android) — that
> talks to it over the REST + WebSocket API documented below; that API is also
> reachable from any WebSocket client (`curl`, `websocat`, etc.) for testing.

## Why

Controlling a coding agent from a phone usually means SSH'ing into a box and
fighting a terminal multiplexer through a touchscreen keyboard. agent-ui-server gives
each session stable metadata, streams output as plain JSON events, and surfaces
tool-approval requests as simple allow/deny messages the Android app renders as
buttons — no PTY, no terminal emulator, no copy-paste gymnastics.

### Goals

- Spawn, resume, and stop Claude Code sessions remotely
- Stream agent output in real time
- Persist session metadata and scrollback so sessions survive backend restarts
- Handle tool-approval requests interactively
- Stay simple enough to read and modify in one sitting

### Non-goals

- Multi-user support
- Application-level authentication (access control is delegated to WireGuard)
- A full terminal emulator (no xterm.js, no PTY)

## Architecture

```
Android app (WireGuard peer)
    |  WireGuard tunnel (plain HTTP, no TLS needed)
uvicorn (bound to the WireGuard interface IP)
    |  WebSocket (output streaming, input, approval prompts)
    |  HTTP REST (session CRUD + turn/stop)
FastAPI ......................... main.py
    |  AgentAdapter interface ... agent.py
ClaudeCodeAdapter
    -> claude -p --output-format stream-json --input-format stream-json
              --permission-prompt-tool stdio --permission-mode default --verbose
              [--resume <claude_session_id>]   # omitted on the first turn
    |  Bash mode (no agent) .... shell.py  ->  bash -lc '<command>'
    |  Worktrees .............. git.py    ->  git worktree add/remove (argv, no shell)
SQLite .......................... db.py  ->  sessions.db (metadata + scrollback)
```

Each turn spawns a fresh `claude` subprocess that runs one-shot: write the user
message to stdin, stream JSON events from stdout, exit. Session continuity is
provided by Claude Code's own on-disk session files, referenced via `--resume`
with the `session_id` captured from the previous turn's `result` event.

### Source layout

```
agent-ui-server/
├── src/agent_ui_server/
│   ├── main.py      # FastAPI app — REST routes, WebSocket endpoint, turn orchestration
│   ├── agent.py     # AgentAdapter base + ClaudeCodeAdapter (stream-json) + OpenCodeAdapter (ACP) + PiAdapter (RPC)
│   ├── opencode_permissions.json  # Default OpenCode permission config (gates tools to "ask")
│   ├── pi_extension.ts            # Bundled pi extension — approval gate + AskUserQuestion
│   ├── shell.py     # Bash mode — one-shot `bash -lc`, timeout + output caps
│   ├── git.py       # Worktrees — `git` via argv, never through a shell
│   └── db.py        # SQLite — session metadata + append-only scrollback
├── pyproject.toml   # Package metadata + dependencies (managed with uv)
├── Dockerfile       # Fedora + uv + Claude Code CLI + nested Podman
├── compose.yaml     # Host-network service, bind mounts, named auth volume
├── docs/            # Design notes + the WebSocket event schema reference
├── tests/           # Unit tests (db lifecycle + adapter stream-json parsing)
└── sessions.db      # SQLite file — gitignored, bind-mounted into the container
```

## Security

There is **no application-level auth**. The service binds exclusively to the
WireGuard interface IP and is unreachable from the public internet — only
WireGuard peers can reach it.

```
uvicorn --host <wireguard-ip> --port 8000   # NOT exposed to the internet
```

No reverse proxy, no TLS, no bearer tokens, no login form. The entire access
model is "you are on the WireGuard network or you are not." Do not bind this to
`0.0.0.0` or expose the port publicly.

[Bash mode](#bash-mode) sharpens this considerably: `POST /sessions/{id}/bash`
is unauthenticated arbitrary code execution as the server user, and unlike the
agent's own `Bash` tool it has **no approval gate at all** — that is the point
of the feature. Anyone who can reach the port has a shell. This is acceptable
only because the port is reachable from the WireGuard network and nowhere else;
if that ever stops being true, this endpoint is the first thing to remove.

## Running

The app binds to `WIREGUARD_IP` (default `127.0.0.1`) on `PORT` (default `8000`).

### With uv (no Docker)

Requires the Claude Code CLI (`claude`) and [`uv`](https://docs.astral.sh/uv/)
on the host. Log in to Claude Code once — credentials live in `~/.claude`:

```sh
claude login
uv sync
uv run agent-ui-server
```

`uv sync` installs the project itself (a `src/` layout package, built with
hatchling), so `agent-ui-server` is on the path inside the venv. It can equally
be installed anywhere else — `uv tool install .`, `uv pip install .`, `pipx
install .` — or run as `python -m agent_ui_server`.

Bind explicitly to the WireGuard interface:

```sh
WIREGUARD_IP=10.0.0.1 PORT=8000 uv run agent-ui-server
```

### With Docker / Podman Compose

```sh
docker compose up -d
```

After the **first** deploy, log in once inside the container — each agent you
intend to use needs its own login:

```sh
docker compose exec agent-ui-server claude login          # Claude Code
docker compose exec agent-ui-server opencode auth login   # OpenCode
docker compose exec agent-ui-server pi                     # pi (authenticate, then quit)
```

Credentials are persisted on the `claude-auth` named volume (mounted at
`HOME=/home/agent`), so later rebuilds (`docker compose up -d --build`) stay
logged in. To force a fresh login, remove the volume:

```sh
docker volume rm <project>_claude-auth
```

The container image is Fedora-based and ships `uv`, the Claude Code CLI, the
OpenCode CLI, the pi CLI, `git`, and a nested **Podman** stack (`privileged: true` + `/dev/fuse` +
`fuse-overlayfs`) so agents can run containers inside their working directory.
Host networking is used so the container sees the WireGuard interface directly.
Project directories are bind-mounted at `/projects`; create projects with
`path` values like `/projects/<name>`, and sessions with a matching
`project_path`. A worktree must live under the same bind mount to be visible
inside the container — a sibling like `/projects/<name>-<branch>` is the shape
the client suggests.

Both paths run the same entry point (the `agent-ui-server` console script), so
host/port behavior is identical whether you use uv or Compose.

### Configuration

| Variable       | Default       | Purpose                                          |
|----------------|---------------|--------------------------------------------------|
| `WIREGUARD_IP` | `127.0.0.1`   | Interface IP uvicorn binds to                    |
| `PORT`         | `8000`        | Listen port                                      |
| `SESSION_DB`   | `sessions.db` | SQLite database path                             |
| `CLAUDE_BIN`   | `claude`      | Path/name of the Claude Code executable          |
| `OPENCODE_BIN` | `opencode`    | Path/name of the OpenCode executable             |
| `OPENCODE_CONFIG` | bundled `opencode_permissions.json` | OpenCode config passed to the agent; sets which tools require approval |
| `PI_BIN`       | `pi`          | Path/name of the pi executable                   |
| `PI_EXTENSION` | bundled `pi_extension.ts` | pi extension supplying the approval gate and AskUserQuestion |
| `WEB_ROOT`     | unset         | Directory of static files to serve at `/`; unset serves no UI |
| `BASH_TIMEOUT_SECONDS` | `120` | Bash mode: how long a command may run before it is killed |
| `BASH_OUTPUT_LIMIT`    | `102400` | Bash mode: bytes kept per stream before output is truncated |
| `GIT_TIMEOUT_SECONDS`  | `30`  | Worktrees: how long a `git` invocation may run before it is killed |
| `GIT_OUTPUT_LIMIT`     | `4096` | Worktrees: bytes of git output kept for an error message |

### Serving a web client

Setting `WEB_ROOT` mounts a directory of static files at `/`, so a browser
client is served from the same origin as the API — no CORS, and nothing to
configure client-side because the page infers the API from its own URL. The
desktop client, [rauaap/agent-ui-desktop](https://github.com/rauaap/agent-ui-desktop),
is a zero-build static app meant to be pointed at exactly this:

```sh
WEB_ROOT=../agent-ui-desktop uv run agent-ui-server
```

The mount is registered after every route, so `/projects`, `/sessions` and
`/ws/sessions/{id}` still win over any file of the same name. Serving a UI does
not change the security model — the API was already reachable at that address,
and access is still "you are on the WireGuard network or you are not."

## API

### REST

| Method   | Path                    | Description                                                       |
|----------|-------------------------|------------------------------------------------------------------|
| `GET`    | `/projects`             | List projects (working directories) with session aggregates      |
| `POST`   | `/projects`             | Create a project: `mkdir -p` + row (`path`, `name`) → `201`      |
| `DELETE` | `/projects`             | Forget a project and its sessions (`path`); disk untouched       |
| `GET`    | `/worktrees`            | List worktrees, optionally `?project_path=`, with session counts |
| `POST`   | `/worktrees`            | Create a worktree (`project_path`, `path`, `branch`) → `201`     |
| `DELETE` | `/worktrees/{id}`       | Remove the worktree from disk and forget it                      |
| `GET`    | `/sessions`             | List all sessions with metadata                                  |
| `POST`   | `/sessions`             | Create a session (`name`, `project_path`, `agent`, `worktree_id`) → `201` |
| `PATCH`  | `/sessions/{id}`        | Update a session: rename (`name`) and/or set auto-approve toggles |
| `POST`   | `/sessions/{id}/turn`   | Send a prompt and spawn a turn → `202`                           |
| `POST`   | `/sessions/{id}/bash`   | Run a shell command (`command`), bypassing the agent → `202`     |
| `POST`   | `/sessions/{id}/stop`   | Stop the running process and any shell command, status → `idle`  |
| `DELETE` | `/sessions/{id}`        | Stop the process and delete the session and its scrollback       |

`POST /sessions` requires an absolute `project_path` naming a project that
already exists — a session belongs to a project by foreign key, so an
unregistered path is a `404`; create the project first. (`working_dir` is still
accepted as a deprecated alias for `project_path`, for clients written before
the rename.) The directory is created (`mkdir -p`) if missing. `agent` is one of
the registered adapters — `"claude-code"` (the default), `"opencode"`, or
`"pi"`. The
optional `worktree_id` attaches the session to one of the project's worktrees,
which it runs in instead; see [Worktrees](#worktrees). Starting a turn on a
session that is not `idle` returns `409`.

`PATCH /sessions/{id}` is a partial update; every field is optional and only the
supplied ones are applied. `name` (a non-empty 1–120 char label, trimmed)
renames the session and broadcasts a `renamed` event. `auto_approve_write` and
`auto_approve_command` are booleans that flip the per-session auto-approval
toggles (see [Auto-approval](#auto-approval)) and broadcast a `settings` event.
Both broadcasts reach all WebSocket subscribers so connected clients update live.

#### Projects

A **project** is a working directory, stored as a row in `projects` and keyed by
its path. The server never scans the filesystem to discover projects and has no
configured projects root: a project exists because it was created through
`POST /projects`, and nowhere else.

```jsonc
[
  { "id": 1, "path": "/projects/agent-ui", "name": "agent-ui",
    "exists": true, "is_git_repo": true,
    "session_count": 3, "last_active_at": "2026-07-28T09:14:02Z" },
  { "id": 2, "path": "/projects/scratch",  "name": "scratch",
    "exists": false, "is_git_repo": false,
    "session_count": 0, "last_active_at": null }
]
```

`session_count` and `last_active_at` are a `LEFT JOIN` onto `sessions` matched on
`sessions.project_id`, so a project with no sessions yet reports `0` / `null` —
and a session running in a worktree somewhere else still counts towards the
project it was cut from. Results are sorted by `last_active_at` descending —
SQLite sorts `NULL` below everything, so never-used projects land last — then by
`path` ascending.

`id` is the project's identity, and a JSON **number** — not a string. It exists
so a project's `path` can change later without taking its sessions with it, and
it is what sessions store; the HTTP API itself is still addressed by `path`
everywhere. Client code that compares, stores or renders an id should read
[docs/client_ids.md](docs/client_ids.md) first — the number/string distinction
has sharp edges in a browser.

`exists` is a `stat` of the stored path at request time, not a discovery scan.
Because the row is the record, a directory removed outside the app leaves the
project in place; the flag is how a client can say so and offer to forget it.

`is_git_repo` is one more `exists`, on `<path>/.git` — a hint so a client can
hide the worktree toggle for projects that cannot have one. It is only a hint:
`POST /worktrees` runs the real check. A `.git` *file* counts, so a project that
is itself a worktree reads as a repo; a project in a subdirectory of a repo
reads as `false`, which is deliberate.

#### `POST /projects`

Takes `{ "path": "/projects/foo", "name": "foo" }`, creates the directory
(`mkdir -p`), inserts the row, and returns the same shape as a list entry.

- `path` must be absolute, and is normalised lexically (`..` and duplicate
  slashes collapsed, trailing slash dropped) so one directory cannot enter the
  table twice under two spellings. `/` itself is a `400`.
- `name` is optional and defaults to the path's last segment. It is stored
  separately because the client lets you break the link between the two — a
  project may be called `api` while living in `/projects/backend-rewrite`.
- An **existing** directory is adopted as-is, but it has to be usable: a path
  that exists and is not a directory, or a directory the server cannot write to,
  is a `400` rather than a project that fails on its first turn.
- Creating a project that already exists is a no-op returning its real
  aggregates, not an error.

#### `DELETE /projects`

Takes `{ "path": "/projects/foo" }` and **never touches the filesystem** — the
directory and everything the agent wrote in it stay exactly where they are.

What it does remove is the row *and every session belonging to it*, along with
their scrollback: sessions are reachable only through their project, so leaving
them would strand history with no way to open or delete it. The project's
worktrees go the same way, under the never-forced policy described below:

```jsonc
{ "status": "deleted", "sessions_deleted": 3,
  "worktrees_removed": 1,
  "worktree_errors": [
    { "path": "/projects/app-fix-login",
      "error": "fatal: '…' contains modified or untracked files, …" }
  ] }
```

Sessions are torn down before any worktree is touched, so nothing is still
attached when git is asked to remove one. A worktree git refuses to remove stays
on disk and is reported — but its **row goes anyway**, with the project. That is
the one place a directory outlives its row, and it is deliberate: this endpoint
already leaves the project's own directory behind, so it is a "forget all of
this" operation rather than a delete.

Running sessions are stopped first. Unknown path → `404`.

The path travels in the body rather than the URL for the same reason there are
no nested `/projects/{path}/sessions` routes: a filesystem path does not belong
in a URL segment. `GET /sessions` already carries `project_id` and `working_dir`
on every row, so clients group locally.

Sessions **are** restricted to projects: `sessions.project_id` is `NOT NULL`
with a real foreign key, so `POST /sessions` at an unregistered path is a `404`
rather than a session nothing in the UI can reach.

#### Worktrees

A **worktree** is a git worktree of a project, with its own row and its own
endpoints, so two sessions on one project can work on separate branches without
fighting over a single checkout — and any number of sessions can share one
worktree when that is what you want.

```jsonc
[
  { "id": 1, "project_id": 1, "path": "/projects/app-fix-login",
    "branch": "fix-login", "created_at": "2026-08-31T09:14:02Z",
    "session_count": 2, "exists": true }
]
```

`POST /worktrees` takes `{ "project_path", "path", "branch" }` and runs
`git worktree add -b <branch> <path>`, always cutting a **new** branch off the
project's current HEAD. Attaching to an existing branch or commit-ish is not
offered. The row's existence is the record that the server created the
directory and is the one responsible for removing it; if the insert fails after
git succeeded, the worktree is removed again.

Failure modes, all `400` with the reason in `detail`: the project is not a git
repository, the branch name is not one git accepts (`git check-ref-format`), the
branch already exists, the target path is the project directory itself, the
target path exists and is not an empty directory, or the directory could not be
created. An empty directory *is* accepted — that is git's own rule. An
unregistered `project_path` is a `404`.

The directory is created with `Path.mkdir` **before** git runs, even though
`git worktree add` would create it itself. That command is not atomic: it writes
the new branch ref before creating the leading directories, so a filesystem
failure there leaves the branch behind with no worktree attached (verified, git
2.47.3), and the obvious retry then fails with `a branch named '…' already
exists` — an error about the wrong thing entirely. Creating it first moves the
failure ahead of the ref, so there is nothing to unwind, and `OSError` carries a
real errno, so the message is `Permission denied` rather than git's
`could not create leading directories of '…/.git'`.

A path that is **already a registered worktree** is a `409` naming the branch it
is on, checked before anything touches the filesystem. It has its own status and
message because `worktrees.path` is `UNIQUE` — so this is what stops the insert
failing after git has already done the work — and because both errors that would
otherwise fire describe the symptom rather than the cause: git blames a
non-empty directory, or, if the directory was deleted by hand, its own leftover
admin files. Clients that derive the path from a template hit this whenever two
worktrees would be named the same way.

Paths must be **absolute**; a relative one is a `400`. They are normalised
lexically (`os.path.normpath`, no symlink resolution), so a client is free to
build one by naive joining — `/projects/app/../app-fix` arrives as
`/projects/app-fix`.

`session_count` is a `LEFT JOIN` onto `sessions.worktree_id`. Zero is an
ordinary state: a worktree with nothing attached is one you can still attach to,
not a leak. `exists` is a `stat` of the path, the same hint `GET /projects`
carries — a worktree deleted by hand keeps its row, and this is how a client can
say so and offer to tidy up.

A session attaches at creation time by passing `worktree_id` to `POST /sessions`
(`404` if unknown, `400` if it belongs to a different project). Its
`working_dir` is then the worktree's path. **Nothing is created on disk** for an
attaching session — a `mkdir` would hand the agent a plain directory dressed up
as a worktree — so attaching to one whose directory is gone succeeds and shows
up as `exists: false`.

`DELETE /sessions/{id}` never touches a directory. A worktree outlives the
sessions that used it: others may still be attached, and even the last one
leaving does not mean the user is finished with the branch. Removing it is a
separate decision, and a separate request.

`DELETE /worktrees/{id}` removes the directory and the row, and answers `409`
with the reason in `detail` when it cannot:

- sessions are still attached — their names are listed, delete them first;
- git refuses because the tree has uncommitted or untracked work.

Removal is **never forced**. Expect the dirty case to be the *common* outcome
rather than an edge case: git counts untracked files as dirty, so any worktree
whose agent created a single new file will refuse. Clients should phrase it as a
notice, not an error. Unlike deleting a session, the row does **not** go
regardless — the row *is* the worktree, so keeping it while the directory
survives is what stops it becoming an orphan nothing can see. A worktree whose
directory was deleted by hand needs no special case: git prunes its admin files
and exits 0, so the request succeeds and tidies the row away.

### WebSocket

| Path                  | Description                                                  |
|-----------------------|-------------------------------------------------------------|
| `/ws/sessions/{id}`   | Bidirectional — replay scrollback, stream output, approvals |

On connect, the backend replays the last 200 scrollback rows, then sends the
current `status`. The same connection accepts input prompts and approval
responses, and receives every event broadcast for that session. (Prompts and
stops can also be issued over REST; everything is broadcast to all subscribers
either way.)

#### Message protocol

```jsonc
// Server -> Client
{ "type": "output", "text": "..." }                                   // agent text
{ "type": "tool_use", "tool": "Bash", "input": { "command": "..." } } // tool notification
{ "type": "approval_request", "request_id": "perm_1",                 // process blocked on stdin
  "tool": "Bash", "input": { "command": "rm -rf /tmp/test" },
  "category": "command",                                              // write | command | null
  "auto_approved": true,                                              // (optional) answered by a toggle
  "options": [ { "id": "allow", "name": "Allow", "kind": "allow_once" },
               { "id": "deny", "name": "Deny", "kind": "reject_once" } ] }
{ "type": "approval_response", "request_id": "perm_1",                // broadcast when an approval resolves
  "behavior": "allow" | "deny", "auto": true }                       // `auto` set on toggle auto-approvals
{ "type": "question", "request_id": "perm_2",                         // AskUserQuestion (Claude only) — blocks
  "questions": [ { "question": "...", "header": "...", "multiSelect": false,
                   "options": [ { "label": "...", "description": "..." } ] } ] }
{ "type": "question_response", "request_id": "perm_2",               // broadcast when a question is answered
  "answers": { "<question text>": "<label>" } }
{ "type": "input", "text": "..." }                                    // echo of a submitted prompt
{ "type": "bash_input", "command": "df -h" }                          // echo of a `!` command
{ "type": "bash_output", "command": "df -h",                          // that command, once it exits
  "stdout": "...", "stderr": "",
  "exit_code": 0,                                                     // null if it never started
  "duration_ms": 41, "timed_out": false, "truncated": false }
{ "type": "status", "status": "running" | "idle" | "awaiting_approval" }
{ "type": "renamed", "name": "..." }                                  // session label changed
{ "type": "settings", "auto_approve_write": false,                    // auto-approve toggles changed
  "auto_approve_command": true }
{ "type": "done" }                                                    // turn complete
{ "type": "error", "message": "..." }

// Client -> Server
{ "type": "input", "text": "..." }                                    // start a new turn
{ "type": "bash", "command": "df -h" }                                // run a shell command
{ "type": "approval_response", "request_id": "perm_1",                // answer an approval
  "behavior": "allow" | "deny",                                       // or pick a specific option:
  "option_id": "deny",                                                // (optional) one of options[].id
  "message": "..." }                                                  // (optional) denial reason
{ "type": "question_response", "request_id": "perm_2",               // answer an AskUserQuestion
  "answers": { "<question text>": "<label>" } }                       // label, or [labels] for multiSelect
```

`question` / `question_response` cover Claude Code's built-in **AskUserQuestion**
tool — the agent asking the user to *pick content*, distinct from a tool
allow/deny. A pending question reuses the `awaiting_approval` status; the client
tells them apart by event type.

Support is per agent. **Claude Code** has the tool built in. **pi** has no such
tool, so the bundled extension registers one — see [pi](#pi). **OpenCode** ships
a `question` tool but does not expose it over ACP, so the adapter has none.

### Bash mode

A message the user prefixes with `!` is not a prompt. The **client** strips the
`!` and sends `{"type": "bash", "command": "..."}` (or `POST
/sessions/{id}/bash`); the server spawns `bash -lc '<command>'` in the session's
`working_dir`, captures stdout and stderr, and writes the result to scrollback
as a `bash_output` event. The agent is never involved — no tokens, no context,
no approval prompt.

The server never inspects prompt text for a leading `!`. Keeping the split on
the client means a prompt that legitimately begins with `!` stays sendable, and
the wire says what it means.

- **Nothing persists between invocations.** Each command is a fresh shell:
  `cd`, `export` and shell functions are gone by the next one. Every command
  starts in the session's `working_dir`.
- **Independent of the agent.** Bash never takes the turn lock and never
  changes `status`, so a command can run while the session is `running` or
  parked in `awaiting_approval`, and neither side notices the other. A client
  should not gate the `!` path on session status.
- **One at a time per session.** A second command while one is in flight is
  rejected with `409` (REST) or an `error` event (WebSocket).
- **Bounded.** A command is killed after `BASH_TIMEOUT_SECONDS` (SIGTERM to the
  whole process group, SIGKILL 3s later, so backgrounded children die too) and
  the result comes back with `timed_out: true` plus whatever it printed first.
  Output past `BASH_OUTPUT_LIMIT` per stream is replaced mid-way by a
  `… N bytes omitted …` marker keeping the head and the tail, with
  `truncated: true`. The reader keeps draining past the cap, so a command like
  `yes` cannot wedge on a full pipe.
- **stdin is `/dev/null`,** so a command that decides to prompt gets EOF instead
  of hanging until the timeout.
- **Killable.** `POST /sessions/{id}/stop` kills an in-flight command as well as
  the agent process; the transcript gets an `error` event saying it was stopped.

`exit_code` is `null` when the command never started — most often a
`working_dir` that was deleted after the session was created, which is reported
in `stderr` rather than raised.

### Approval flow

1. The `claude` subprocess emits a `control_request` / `sdk_control_request`
   (subtype `permission` or `can_use_tool`) and blocks on stdin.
2. The backend sets status → `awaiting_approval`, persists the request to
   scrollback, and broadcasts an `approval_request` (with the available
   `options`) to all subscribers. The approval is held in an in-memory
   `asyncio.Future` keyed by `request_id`.
3. A client answers with `approval_response`, supplying either a `behavior`
   (`allow`/`deny`) or a specific `option_id`, plus an optional denial `message`.
4. The backend resolves the Future and writes a `control_response` to the
   subprocess stdin — `allow` echoes `updatedInput`, `deny` sends the client's
   `message` (falling back to a default) — then flips status back to `running`
   and the process continues.

A `deny` with a `message` is agent-specific. **Claude Code** delivers the reason
inline in the denial, so the agent reacts to it in the same turn. **OpenCode**'s
ACP cannot relay a reason (and 1.17.3 has no `session/cancel`), so a plain
"reject" would leave the agent speculating for the rest of the turn. Instead the
OpenCode adapter ends the turn at the denial and emits an internal `followup`
event; `run_turn` then auto-starts a new turn whose prompt restates the denied
tool + input + reason. `session/load` resumes the conversation, and because the
follow-up prompt is self-contained it does not depend on OpenCode having
persisted the interrupted call. **pi** delivers it inline like Claude Code: the
extension's `tool_call` hook returns `{block: true, reason}`, and pi hands the
reason to the model as the tool's (error) result.

If the session is stopped or the process exits while an approval is pending, the
Future is failed and the prompt is cleared.

### Auto-approval

Each session carries two toggles — `auto_approve_write` and
`auto_approve_command` — set via `PATCH /sessions/{id}`. Nothing changes on the
agent side: both adapters still run in "ask every time" mode and still emit an
`approval_request`. The toggles only change whether the *backend* waits for a
human or answers `allow` itself.

Every `approval_request` carries a `category` the adapter derives from the tool —
Claude Code by tool name (`Bash` → `command`; `Write`/`Edit`/`MultiEdit`/
`NotebookEdit` → `write`), OpenCode from ACP's `toolCall.kind` (`execute` →
`command`; `edit`/`delete`/`move` → `write`), pi by tool name (`bash`/
`powershell` → `command`; `edit`/`write` → `write`). When the matching session toggle is
on, `run_turn` marks the request `auto_approved`, broadcasts it without entering
`awaiting_approval`, and immediately answers `allow` on the user's behalf (the
resulting `approval_response` carries `auto: true`). The toggle is re-read from
the database on each approval, so flipping it mid-turn takes effect on the next
tool call.

There is no `read` toggle: read-only tools are auto-allowed by Claude Code's
`--permission-mode default`, by OpenCode's permission config, and — since pi
filters nothing itself — by the allowlist in the bundled pi extension, so they
never reach this gate. Tools with no category (e.g. `WebFetch`) always prompt.

## Data model

### `projects`

| Column       | Type      | Notes                                                        |
|--------------|-----------|--------------------------------------------------------------|
| `id`         | INTEGER PK | Autoincrement — the project's identity, and what sessions reference |
| `path`       | TEXT UQ   | Absolute working directory, normalised; how the HTTP API addresses a project |
| `name`       | TEXT      | Display label; defaults to the path's last segment but may differ |
| `created_at` | TEXT      | ISO 8601 (UTC, `Z`)                                          |

Sessions join to a project on `sessions.project_id = projects.id`, a real
foreign key (`ON DELETE CASCADE`). Identity is the id rather than the path
precisely so a project's `path` can change later without taking its sessions
with it.

### `worktrees`

| Column       | Type       | Notes                                                        |
|--------------|------------|--------------------------------------------------------------|
| `id`         | INTEGER PK | Autoincrement — what sessions reference                      |
| `project_id` | INTEGER FK | References `projects.id` (`ON DELETE CASCADE`), `NOT NULL`   |
| `path`       | TEXT UQ    | Absolute path of the worktree directory, normalised          |
| `branch`     | TEXT       | The branch it was cut on; `NULL` for rows created by migration |
| `created_at` | TEXT       | ISO 8601 (UTC, `Z`)                                          |

The row's existence *is* the ownership record: every worktree here was created
by `POST /worktrees`, so every one is ours to remove. There is no
`owned` flag, and adopting worktrees that already exist on disk would be what
adds one.

`branch` is a record of what was created, not live state — an agent working in
the worktree is free to switch branches, and nothing here tracks that. Ask git
if you need the current branch.

### `sessions`

| Column              | Type    | Notes                                                       |
|---------------------|---------|-------------------------------------------------------------|
| `id`                | INTEGER PK | Autoincrement — never reused, so a stale URL cannot hit a later session |
| `name`              | TEXT    | Human-readable label                                        |
| `project_id`        | INTEGER FK | References `projects.id` (`ON DELETE CASCADE`), `NOT NULL` |
| `worktree_id`       | INTEGER FK | References `worktrees.id` (`ON DELETE RESTRICT`); `NULL` means "runs in the project directory" |
| `agent`             | TEXT    | Which adapter to use, e.g. `claude-code`, `opencode`, or `pi` |
| `agent_session_id`  | TEXT    | The agent's own resume id; `NULL` until the first turn completes |
| `status`            | TEXT    | `idle` \| `running` \| `awaiting_approval`                  |
| `created_at`        | TEXT    | ISO 8601 (UTC, `Z`)                                         |
| `last_active_at`    | TEXT    | ISO 8601, bumped on each turn                               |
| `auto_approve_write`   | INTEGER | `0`/`1` — auto-approve write/edit tools (default `0`)    |
| `auto_approve_command` | INTEGER | `0`/`1` — auto-approve shell commands (default `0`)      |

**There is no `working_dir` column.** Every session response carries one, but it
is computed: `COALESCE(worktrees.path, projects.path)`. A session runs in its
worktree if it has one and in its project's directory otherwise — there is no
third possibility, so storing a copy would only be a second place for the same
path to live, and to drift once several sessions share one worktree.

### `scrollback` (append-only)

| Column       | Type    | Notes                                                                          |
|--------------|---------|--------------------------------------------------------------------------------|
| `id`         | INTEGER | Autoincrement PK                                                               |
| `session_id` | INTEGER FK | References `sessions.id` (`ON DELETE CASCADE`)                              |
| `ts`         | TEXT    | ISO 8601                                                                       |
| `type`       | TEXT    | `input` \| `output` \| `tool_use` \| `approval_request` \| `approval_response` \| `question` \| `question_response` \| `bash_input` \| `bash_output` \| `error` |
| `payload`    | TEXT    | JSON blob                                                                      |

SQLite runs in WAL mode with foreign keys on. On startup, any session left in a
non-`idle` state (from a crash or restart) is reset to `idle`, since no
subprocess survives a backend restart.

The `ON DELETE CASCADE` from sessions to projects is a backstop only:
`DELETE /projects` still sweeps its sessions explicitly through
`teardown_session`, which does much more than delete a row — it stops the agent,
cancels any bash command and closes subscribers.

`sessions.worktree_id` is `ON DELETE RESTRICT`, behind the `409` that
`DELETE /worktrees/{id}` answers when sessions are still attached. It is also
why `Database.delete_project` deletes sessions explicitly before the project row
rather than leaving it to the cascade: a cascade that happened to reach
`worktrees` while a session still pointed at one would abort the whole delete,
and the order of that is SQLite's business, not ours.

#### Migration

Databases are rebuilt on open, through as many steps as their age requires.

`_migrate_v2` gives projects an `id` and sessions a real `project_id` foreign
key. SQLite cannot add a `REFERENCES` column with a non-`NULL` default and
cannot re-key a table, so both tables go through the documented 12-step rebuild
inside one transaction. Existing rows resolve to a project by *normalised* path
— `create_session` never normalised it while `create_project` did, so a legacy
`/p/demo/` joins the existing `/p/demo` rather than minting a duplicate. A
session whose path matched no project at all (an orphan, invisible in the UI but
still holding scrollback) is adopted into a project created for it.

`_migrate_v3` renumbers projects and sessions from uuid strings to integer ids,
rebuilding `scrollback` along with them. **Ids are not preserved**, so anything
holding an old uuid stops resolving.

`_migrate_v4` moves the worktree out of the session and into `worktrees`. Each
session with `owns_worktree = 1` mints a worktree row at its `working_dir` (one
row per path, `branch` unknown and left `NULL`) and links to it; every other
session gets `worktree_id = NULL`, which resolves to the project directory it
was already running in. Both dropped columns were expressing something the new
schema says structurally — the cwd is derived from the links, and "we created
this directory" is a `worktrees` row existing. Session ids are preserved here,
so unlike v3 this leaves `scrollback` untouched.

### In-memory state

Live process handles, WebSocket subscribers, the running-turn tasks, the
in-flight bash-mode tasks, and pending approval Futures are held in memory and
intentionally **not** persisted — they are all empty after a restart. Bash tasks
are tracked in their own dict, separate from turns, precisely because the two
are allowed to run at the same time.

## Agent interface

Adapters implement a small interface so other CLIs can be added later:

```python
class AgentAdapter:
    async def start_turn(session, prompt) -> AsyncIterator[AgentEvent]
    async def send_approval(session, request_id, behavior,
                            *, option_id=None, message=None) -> str  # effective allow/deny
    async def stop(session) -> None
```

`AgentEvent` is a tagged union (`output`, `tool_use`, `approval_request`,
`done`, `error`). The WebSocket layer and the Android app only ever speak
`AgentEvent` — they never touch adapter internals. Two adapters ship today:

| Agent           | Per-turn process                          | Resume                  | Tool approval                                  |
|-----------------|-------------------------------------------|-------------------------|------------------------------------------------|
| **Claude Code** | `claude -p --output-format stream-json`   | `--resume <id>`         | `--permission-prompt-tool stdio` via stdin     |
| **OpenCode**    | `opencode acp` (JSON-RPC over stdio)      | `session/load <id>`     | `session/request_permission` callback over stdio |
| **pi**          | `pi --mode rpc` (JSONL over stdio)        | `--session <id>`        | bundled extension's `tool_call` hook, over pi's dialog protocol |
| **Codex**       | `codex exec --json`                       | `codex exec resume`     | not in `exec` — needs persistent `app-server`  |

All three shipping adapters are **one short-lived subprocess per turn** and
surface real multiple-choice approvals; they only differ in wire protocol.

**Claude Code** speaks Anthropic's `stream-json` over stdio: we write the user
message to stdin, stream JSON events from stdout, and answer
`control_request`/`sdk_control_request` permission prompts by writing a
`control_response` back to stdin.

**OpenCode** speaks **ACP** (Agent Client Protocol) — JSON-RPC 2.0 over stdio,
and bidirectional. Each turn the adapter spawns `opencode acp`, calls
`initialize` → `session/new` (first turn) or `session/load <id>` (resume) →
`session/prompt`, maps the streamed `session/update` notifications
(`agent_message_chunk` → `output`, `tool_call`/`tool_call_update` → `tool_use`)
to `AgentEvent`s, and answers the agent's `session/request_permission` callbacks
by forwarding the chosen `optionId`. The request's `options` are passed through
to the client so it can offer the agent's actual choices (e.g. allow always).
ACP has no field for a free-form denial reason, so a `deny` with a `message` is
handled by interrupting the turn and re-prompting with the reason (see the
approval flow above). The session id returned by `session/new` is stored as
`agent_session_id` and replayed via `session/load`.

OpenCode only emits `session/request_permission` for tools configured to `"ask"`
— out of the box most tools default to `"allow"` and would run without
prompting. The adapter therefore points the child at a permission config via the
`OPENCODE_CONFIG` env var; a bundled default (`opencode_permissions.json`) gates
`bash`, `edit`, `write`, and `webfetch`. Set `OPENCODE_CONFIG` yourself to
override which tools prompt.

**pi** speaks its **RPC mode** — newline-delimited JSON on stdio. Each turn the
adapter spawns `pi --mode rpc -e <extension> --no-extensions` (plus `--session
<id>` on resume), waits for the extension's handshake, sends `get_state` to
learn the session id, then `prompt`, and maps the event stream (`message_update`
→ `output`, `tool_execution_start` → `tool_use`, `agent_settled` → `done`).

Unlike the other two, pi ships **no permission system at all** — its own docs
say built-in tools "run shell commands with the permissions of the pi process"
and recommend containerization instead — and no tool for asking the user a
question. Both are supplied by the bundled `pi_extension.ts`, which the adapter
passes with `-e`:

- A `tool_call` hook gates every mutating tool and returns `{block: true,
  reason}` on denial. Read-only tools (`read`, `grep`, `find`, `ls`) are allowed
  without a prompt; anything unrecognized prompts.
- A registered `AskUserQuestion` tool takes a batch of 1-4 questions, matching
  Claude Code's shape.

The extension reaches the adapter over pi's extension dialog protocol: it calls
`ctx.ui.select` / `ctx.ui.input`, which pi serializes as `extension_ui_request`
lines answered with `extension_ui_response`. Neither call carries structured
data, so the extension JSON-encodes what the adapter needs into the dialog
`title`; tool *arguments* are not sent that way but recovered from the
`tool_execution_start` pi emits just before. A question batch becomes N
concurrent dialogs sharing one `toolCallId`, which the adapter reassembles into
a single `question` event.

Because the gate lives in an extension rather than in pi, a failure to load it
would leave the agent running unrestricted and silent. The extension therefore
announces itself on `session_start`, and the adapter **refuses to send the
prompt** until it does. `--no-extensions` is passed alongside `-e` so a
project's own `.pi/extensions` cannot join the session and mutate tool input
after the user has approved it.

One deployment note: pi's launcher is `#!/usr/bin/env node`, so it runs under
whatever `node` is first on `PATH`. Under too old a Node it fails deep inside
its own bundle with an unrelated-looking `SyntaxError`, so the adapter promotes
the Node shipped beside the `pi` binary, when there is one, to the front of the
child's `PATH`.

Codex remains future work — its approval handling needs a persistent
`app-server` speaking JSON-RPC, so it does not fit the one-shot shape.

## Development

```sh
uv sync                                        # install deps + the project (editable)
uv run python -m unittest discover -s tests    # run the test suite
uv build                                       # build a wheel + sdist into dist/
```

The tests import the installed package (`from agent_ui_server import ...`), so
`uv sync` has to have run at least once.

The tests cover the SQLite session/scrollback lifecycle, the schema migration
(built from a hand-written old-shaped database), the `ClaudeCodeAdapter`
stream-json parsing (assistant text/tool blocks, permission and `can_use_tool`
normalization, nested `session_id` extraction), bash mode end to end (timeout,
process-group kill, output truncation, turn independence), and worktrees end to
end (creation, every `400`, rollback on a failed insert, sessions sharing one,
and every way removal is refused or succeeds). No *agent* subprocess is spawned in
tests; the bash-mode tests spawn a real short-lived `/bin/bash`, and the
worktree tests `git init` a real repository in a temporary directory — neither
needs the network.

## Dependencies

`fastapi`, `uvicorn[standard]`, `python-dotenv` — plus `sqlite3` and `asyncio`
from the standard library. No ORM, no task queue, no message broker.

## Roadmap

- `AppServerAdapter` for Codex (persistent `app-server` over JSON-RPC 2.0)
- Voice input: Android app audio → backend → local `faster-whisper` (`tiny`
  model, no GPU) → transcription used as a prompt
- Session forking
- Per-session tool allowlists
- Worktrees: attach to an existing branch or commit-ish, an explicit "delete
  anyway" that passes `--force`, adopting worktrees already on disk, and moving
  a session between worktrees after it is created
- Moving a project's path (`projects.id` exists for it; no endpoint yet)
