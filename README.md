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
SQLite .......................... db.py  ->  sessions.db (metadata + scrollback)
```

Each turn spawns a fresh `claude` subprocess that runs one-shot: write the user
message to stdin, stream JSON events from stdout, exit. Session continuity is
provided by Claude Code's own on-disk session files, referenced via `--resume`
with the `session_id` captured from the previous turn's `result` event.

### Source layout

```
agent-ui-server/
├── main.py          # FastAPI app — REST routes, WebSocket endpoint, turn orchestration
├── agent.py         # AgentAdapter base + ClaudeCodeAdapter (stream-json) + OpenCodeAdapter (ACP)
├── opencode_permissions.json  # Default OpenCode permission config (gates tools to "ask")
├── db.py            # SQLite — session metadata + append-only scrollback
├── pyproject.toml   # Dependencies (managed with uv)
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

## Running

The app binds to `WIREGUARD_IP` (default `127.0.0.1`) on `PORT` (default `8000`).

### With uv (no Docker)

Requires the Claude Code CLI (`claude`) and [`uv`](https://docs.astral.sh/uv/)
on the host. Log in to Claude Code once — credentials live in `~/.claude`:

```sh
claude login
uv run main.py
```

Bind explicitly to the WireGuard interface:

```sh
WIREGUARD_IP=10.0.0.1 PORT=8000 uv run main.py
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
```

Credentials are persisted on the `claude-auth` named volume (mounted at
`HOME=/home/agent`), so later rebuilds (`docker compose up -d --build`) stay
logged in. To force a fresh login, remove the volume:

```sh
docker volume rm <project>_claude-auth
```

The container image is Fedora-based and ships `uv`, the Claude Code CLI, the
OpenCode CLI, `git`, and a nested **Podman** stack (`privileged: true` + `/dev/fuse` +
`fuse-overlayfs`) so agents can run containers inside their working directory.
Host networking is used so the container sees the WireGuard interface directly.
Project directories are bind-mounted at `/projects`; create sessions with
`working_dir` values like `/projects/<name>`.

Both paths run the same entry point (`main.py`), so host/port behavior is
identical whether you use uv or Compose.

### Configuration

| Variable       | Default       | Purpose                                          |
|----------------|---------------|--------------------------------------------------|
| `WIREGUARD_IP` | `127.0.0.1`   | Interface IP uvicorn binds to                    |
| `PORT`         | `8000`        | Listen port                                      |
| `SESSION_DB`   | `sessions.db` | SQLite database path                             |
| `CLAUDE_BIN`   | `claude`      | Path/name of the Claude Code executable          |
| `OPENCODE_BIN` | `opencode`    | Path/name of the OpenCode executable             |
| `OPENCODE_CONFIG` | bundled `opencode_permissions.json` | OpenCode config passed to the agent; sets which tools require approval |
| `WEB_ROOT`     | unset         | Directory of static files to serve at `/`; unset serves no UI |

### Serving a web client

Setting `WEB_ROOT` mounts a directory of static files at `/`, so a browser
client is served from the same origin as the API — no CORS, and nothing to
configure client-side because the page infers the API from its own URL. The
desktop client, [rauaap/agent-ui-desktop](https://github.com/rauaap/agent-ui-desktop),
is a zero-build static app meant to be pointed at exactly this:

```sh
WEB_ROOT=../agent-ui-desktop uv run main.py
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
| `GET`    | `/sessions`             | List all sessions with metadata                                  |
| `POST`   | `/sessions`             | Create a session (`name`, `working_dir`, `agent`) → `201`        |
| `PATCH`  | `/sessions/{id}`        | Update a session: rename (`name`) and/or set auto-approve toggles |
| `POST`   | `/sessions/{id}/turn`   | Send a prompt and spawn a turn → `202`                           |
| `POST`   | `/sessions/{id}/stop`   | Stop the running process, set status → `idle`, keep the session  |
| `DELETE` | `/sessions/{id}`        | Stop the process and delete the session and its scrollback       |

`POST /sessions` requires an absolute `working_dir`; the directory is created
(`mkdir -p`) if missing. `agent` is one of the registered adapters —
`"claude-code"` (the default) or `"opencode"`. Starting a turn on a session that
is not `idle` returns `409`.

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
  { "path": "/projects/agent-ui", "name": "agent-ui", "exists": true,
    "session_count": 3, "last_active_at": "2026-07-28T09:14:02Z" },
  { "path": "/projects/scratch",  "name": "scratch",  "exists": false,
    "session_count": 0, "last_active_at": null }
]
```

`session_count` and `last_active_at` are a `LEFT JOIN` onto `sessions` matched on
`working_dir`, so a project with no sessions yet reports `0` / `null`. Results
are sorted by `last_active_at` descending — SQLite sorts `NULL` below everything,
so never-used projects land last — then by `path` ascending.

`exists` is a `stat` of the stored path at request time, not a discovery scan.
Because the row is the record, a directory removed outside the app leaves the
project in place; the flag is how a client can say so and offer to forget it.

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

What it does remove is the row *and every session whose `working_dir` matches*,
along with their scrollback: sessions are reachable only through their project,
so leaving them would strand history with no way to open or delete it. The
response reports the count (`{"status": "deleted", "sessions_deleted": 2}`) so a
client can say up front what will be lost. Running sessions are stopped first,
exactly as `DELETE /sessions/{id}` does. Unknown path → `404`.

The path travels in the body rather than the URL for the same reason there are
no nested `/projects/{path}/sessions` routes: a filesystem path does not belong
in a URL segment. `GET /sessions` already carries `working_dir` on every row, so
clients group locally.

Sessions are **not** restricted to projects: `POST /sessions` still takes any
absolute `working_dir` and does its own `mkdir -p`. Nothing reconciles the two
afterwards, so a session whose `working_dir` has no project row simply does not
appear in a client that browses by project. Create the project first.

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
{ "type": "status", "status": "running" | "idle" | "awaiting_approval" }
{ "type": "renamed", "name": "..." }                                  // session label changed
{ "type": "settings", "auto_approve_write": false,                    // auto-approve toggles changed
  "auto_approve_command": true }
{ "type": "done" }                                                    // turn complete
{ "type": "error", "message": "..." }

// Client -> Server
{ "type": "input", "text": "..." }                                    // start a new turn
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
tells them apart by event type. OpenCode has no equivalent.

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
persisted the interrupted call.

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
`command`; `edit`/`delete`/`move` → `write`). When the matching session toggle is
on, `run_turn` marks the request `auto_approved`, broadcasts it without entering
`awaiting_approval`, and immediately answers `allow` on the user's behalf (the
resulting `approval_response` carries `auto: true`). The toggle is re-read from
the database on each approval, so flipping it mid-turn takes effect on the next
tool call.

There is no `read` toggle: read-only tools are auto-allowed by Claude Code's
`--permission-mode default` and by OpenCode's permission config, so they never
reach this gate. Tools with no category (e.g. `WebFetch`) always prompt.

## Data model

### `projects`

| Column       | Type    | Notes                                                        |
|--------------|---------|--------------------------------------------------------------|
| `path`       | TEXT PK | Absolute working directory, normalised — the project's identity |
| `name`       | TEXT    | Display label; defaults to the path's last segment but may differ |
| `created_at` | TEXT    | ISO 8601 (UTC, `Z`)                                          |

Sessions join to a project on `sessions.working_dir = projects.path`. There is
deliberately **no foreign key**: `POST /sessions` accepts any absolute directory,
and a session must never be blocked because its directory has no project row.

### `sessions`

| Column              | Type    | Notes                                                       |
|---------------------|---------|-------------------------------------------------------------|
| `id`                | TEXT PK | Internal UUID                                               |
| `name`              | TEXT    | Human-readable label                                        |
| `working_dir`       | TEXT    | Absolute path inside the container, e.g. `/projects/foo`    |
| `agent`             | TEXT    | Which adapter to use, e.g. `claude-code` or `opencode`      |
| `agent_session_id`  | TEXT    | The agent's own resume id; `NULL` until the first turn completes |
| `status`            | TEXT    | `idle` \| `running` \| `awaiting_approval`                  |
| `created_at`        | TEXT    | ISO 8601 (UTC, `Z`)                                         |
| `last_active_at`    | TEXT    | ISO 8601, bumped on each turn                               |
| `auto_approve_write`   | INTEGER | `0`/`1` — auto-approve write/edit tools (default `0`)    |
| `auto_approve_command` | INTEGER | `0`/`1` — auto-approve shell commands (default `0`)      |

### `scrollback` (append-only)

| Column       | Type    | Notes                                                                          |
|--------------|---------|--------------------------------------------------------------------------------|
| `id`         | INTEGER | Autoincrement PK                                                               |
| `session_id` | TEXT FK | References `sessions.id` (`ON DELETE CASCADE`)                                 |
| `ts`         | TEXT    | ISO 8601                                                                       |
| `type`       | TEXT    | `input` \| `output` \| `tool_use` \| `approval_request` \| `approval_response` \| `question` \| `question_response` \| `error` |
| `payload`    | TEXT    | JSON blob                                                                      |

SQLite runs in WAL mode with foreign keys on. On startup, any session left in a
non-`idle` state (from a crash or restart) is reset to `idle`, since no
subprocess survives a backend restart.

### In-memory state

Live process handles, WebSocket subscribers, the running-turn tasks, and pending
approval Futures are held in memory and intentionally **not** persisted —
they are all empty after a restart.

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
| **Codex**       | `codex exec --json`                       | `codex exec resume`     | not in `exec` — needs persistent `app-server`  |

Both shipping adapters are **one short-lived subprocess per turn** and surface
real multiple-choice approvals; they only differ in wire protocol.

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

Codex remains future work — its approval handling needs a persistent
`app-server` speaking JSON-RPC, so it does not fit the one-shot shape.

## Development

```sh
uv sync                                        # install dependencies
uv run python -m unittest discover -s tests    # run the test suite
```

The tests cover the SQLite session/scrollback lifecycle and the
`ClaudeCodeAdapter` stream-json parsing (assistant text/tool blocks, permission
and `can_use_tool` normalization, nested `session_id` extraction). No agent
subprocess is spawned in tests.

## Dependencies

`fastapi`, `uvicorn[standard]`, `python-dotenv` — plus `sqlite3` and `asyncio`
from the standard library. No ORM, no task queue, no message broker.

## Roadmap

- `AppServerAdapter` for Codex (persistent `app-server` over JSON-RPC 2.0)
- Voice input: Android app audio → backend → local `faster-whisper` (`tiny`
  model, no GPU) → transcription used as a prompt
- Session forking
- Per-session tool allowlists
