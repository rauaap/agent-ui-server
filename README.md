# agent-ui-server

A self-hosted backend for running coding-agent sessions remotely from a browser
or phone.

Agent UI gives projects, worktrees, agent sessions, transcripts, interactive
approvals, and shell commands a stable HTTP/WebSocket API. It is not tied to one
agent harness: adapters translate each harness's native protocol into a shared,
provider-neutral event and action schema. The server currently ships adapters
for:

- [Claude Code](https://github.com/anthropics/claude-code)
- [Pi](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)

A client renders the same `command`, `edit`, `read`, `search`, and other actions
regardless of which agent produced them. Supporting another harness is primarily
an adapter concern rather than a coordinated client rewrite.

This repository contains the backend only. The current clients are:

- [agent-ui-desktop](https://github.com/rauaap/agent-ui-desktop) — static browser UI
- [agent-ui-android](https://github.com/rauaap/agent-ui-android) — native Android app

The API can also be exercised directly with tools such as `curl` and
`websocat`.

## What it does

- Registers project directories and reports their session activity
- Creates, resumes, stops, archives, and deletes agent sessions
- Selects an agent per session through the adapter registry
- Streams output and persists the latest transcript history in SQLite
- Normalizes provider-specific tool calls into a canonical action schema
- Presents tool approvals and multiple-choice agent questions to clients
- Supports per-session auto-approval for commands and file writes
- Creates and manages Git worktrees independently of sessions
- Runs explicit one-shot shell commands without involving the agent
- Synchronizes a session's working-directory file tree for path completion
- Optionally serves a static web client from the same origin as the API

### Non-goals

- Multi-user accounts
- Application-level authentication
- A terminal emulator or persistent interactive shell
- A sandbox or filesystem security boundary

## Architecture

```text
Desktop / Android / another API client
          │
          ├── REST: projects, worktrees, sessions, turns, shell commands
          ├── WebSocket: transcript, status, approvals, questions
          └── WebSocket: revisioned working-directory file tree
          │
FastAPI application ................................ main.py
          │
          ├── session orchestration + canonical wire events
          ├── AgentAdapter registry ................ agent.py
          │     ├── ClaudeCodeAdapter (stream-json over stdio)
          │     └── PiAdapter (RPC JSONL over stdio)
          ├── provider → canonical tool actions .... tool_actions.py / actions.py
          ├── one-shot shell commands .............. shell.py
          ├── Git worktrees ........................ git.py
          ├── inotify file-tree synchronization .... file_tree.py
          └── SQLite metadata + transcript ......... db.py
```

An Agent UI session belongs to a project and selects an adapter by id. Each
shipping adapter starts one short-lived subprocess per turn. Continuity comes
from the harness's own persisted session id, stored by Agent UI as
`agent_session_id` and supplied on the next turn.

The adapter boundary has two layers:

1. **Lifecycle adaptation:** start a turn, stream text, pause for approvals or
   questions, resume, stop, and capture the harness session id.
2. **Schema adaptation:** convert native tool names and argument spellings into
   canonical actions. For example, Claude Code's `Edit` and Pi's `edit` both
   become an `action` with `kind: "edit"` and the same fields.

Provider-specific protocols stop at this boundary. REST clients, WebSocket
clients, persistence, and auto-approval logic operate on the common schema.

## Source layout

```text
src/agent_ui_server/
├── main.py           # FastAPI routes, WebSockets, orchestration
├── agent.py          # AgentAdapter, Claude Code adapter, Pi adapter
├── actions.py        # validated canonical action/event models
├── tool_actions.py   # provider-specific action projections
├── pi_extension.ts   # Pi approval gate and AskUserQuestion tool
├── db.py             # SQLite schema, migrations, transcript storage
├── file_tree.py      # snapshots, patches, ignore rules, inotify lifecycle
├── git.py            # bounded Git worktree operations
└── shell.py          # bounded one-shot bash execution

tests/
├── test_core.py      # API, database, adapters, WebSocket ordering, shell, Git
├── test_actions.py   # canonical schema and provider normalization
├── test_file_tree.py # scans, watches, limits, patches, endpoint lifecycle
└── fixtures/         # captured native tool events from supported agents
```

## Security model

There is **no application-level authentication**. The intended deployment binds
uvicorn to a WireGuard interface and permits only trusted peers to reach it:

```sh
WIREGUARD_IP=10.0.0.1 PORT=8000 uv run agent-ui-server
```

Do not expose this service directly to the internet. In particular,
`POST /sessions/{id}/bash` is intentional arbitrary command execution as the
server user. It has no agent approval gate. Agents also have the filesystem and
process privileges of their subprocess unless the deployment provides stronger
isolation.

Gitignore rules and `.agent-ui-ignore` only control what the file-tree endpoint
indexes. They do not prevent an agent or shell command from reading a path.

The Compose configuration uses host networking so the container can bind the
host's WireGuard address. It also runs privileged with `/dev/fuse` to support
nested Podman for agents that need containers. Treat that as a powerful,
trusted-user deployment, not a hardened multi-tenant sandbox.

## Running

The server binds to `WIREGUARD_IP` (default `127.0.0.1`) and `PORT` (default
`8000`). Python 3.11 or newer is required.

### With uv

Install [`uv`](https://docs.astral.sh/uv/) and the CLI for every agent you want
to use. The shipping CLIs can both be installed from npm (Pi requires Node.js
22.19 or newer). Authenticate each CLI once, then install and start the server:

```sh
npm install -g @anthropic-ai/claude-code @earendil-works/pi-coding-agent
claude login       # when using Claude Code
pi                 # when using Pi; authenticate, then quit

uv sync
WIREGUARD_IP=127.0.0.1 PORT=8000 uv run agent-ui-server
```

You only need to install the agent CLI(s) you intend to use. For example, a
Claude Code-only deployment does not need Pi installed. Note that `/agents`
lists the adapters built into the server; it does not check whether each CLI is
installed or authenticated. A turn fails when its selected CLI is unavailable.

The package also exposes `python -m agent_ui_server` and can be installed with
`uv tool install .`, `pip`, or `pipx`.

### With Docker or Podman Compose

```sh
docker compose up -d --build
```

Authenticate the bundled agent CLIs after the first deployment:

```sh
docker compose exec agent-ui-server claude login
docker compose exec agent-ui-server pi
```

The named `claude-auth` volume is the container user's complete home directory,
so it persists credentials for both harnesses despite its historical name. The
image contains Fedora, Python, uv, Node.js, Claude Code, Pi, Git, and nested
Podman support.

The provided Compose file bind-mounts:

- `./sessions.db` at `/app/sessions.db`
- `/home/wawa/projects` at `/projects`
- `../agent-ui-desktop` at `/web` and serves it through `WEB_ROOT`

Adjust those host paths for your machine. Project and worktree paths submitted
to the API must use their paths *inside* the container, such as
`/projects/my-project`.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `WIREGUARD_IP` | `127.0.0.1` | Address uvicorn binds to |
| `PORT` | `8000` | HTTP/WebSocket port |
| `SESSION_DB` | `sessions.db` | SQLite database path |
| `CLAUDE_BIN` | `claude` | Claude Code executable |
| `PI_BIN` | `pi` | Pi executable |
| `PI_EXTENSION` | bundled `pi_extension.ts` | Pi approval/question extension |
| `WEB_ROOT` | unset | Static files mounted at `/` |
| `BASH_TIMEOUT_SECONDS` | `120` | Timeout for a direct shell command |
| `BASH_OUTPUT_LIMIT` | `102400` | Bytes retained per stdout/stderr stream |
| `GIT_TIMEOUT_SECONDS` | `30` | Timeout for a Git operation |
| `GIT_OUTPUT_LIMIT` | `4096` | Git error-output limit |

When `WEB_ROOT` is set, static files are mounted after the API routes, so API
and WebSocket paths continue to take precedence. A same-origin client can infer
its API URL without CORS configuration.

## API overview

FastAPI also exposes generated OpenAPI documentation at `/docs`.

### REST

| Method | Path | Description |
|---|---|---|
| `GET` | `/agents` | List registered agent adapters for a picker |
| `GET` | `/projects` | List projects and live/archive session aggregates |
| `POST` | `/projects` | Register/create a project directory |
| `PATCH` | `/projects` | Archive or unarchive a project and cascade sessions |
| `DELETE` | `/projects` | Forget a project, sessions, and managed worktrees |
| `GET` | `/worktrees` | List worktrees; optionally filter by `project_path` |
| `POST` | `/worktrees` | Create a worktree on a new branch from project HEAD |
| `DELETE` | `/worktrees/{id}` | Remove a clean, unused worktree |
| `GET` | `/sessions` | List sessions and current metadata |
| `POST` | `/sessions` | Create a session for a registered project |
| `PATCH` | `/sessions/{id}` | Rename, archive, or change auto-approval settings |
| `POST` | `/sessions/{id}/detach-worktree` | Detach an archived session while preserving its cwd |
| `POST` | `/sessions/{id}/turn` | Start an agent turn |
| `POST` | `/sessions/{id}/bash` | Start a direct one-shot shell command |
| `POST` | `/sessions/{id}/stop` | Stop the agent turn and shell command |
| `DELETE` | `/sessions/{id}` | Delete a session and its transcript |

### Agents and sessions

`GET /agents` currently returns:

```json
[
  { "id": "claude-code", "name": "Claude Code", "default": true },
  { "id": "pi", "name": "Pi", "default": false }
]
```

Create a project before creating a session:

```sh
curl -X POST http://127.0.0.1:8000/projects \
  -H 'content-type: application/json' \
  -d '{"path":"/absolute/path/to/project","name":"my project"}'

curl -X POST http://127.0.0.1:8000/sessions \
  -H 'content-type: application/json' \
  -d '{"name":"refactor","project_path":"/absolute/path/to/project","agent":"pi"}'
```

A session's effective `working_dir` is derived from its attached worktree,
preserved detached-worktree path, or project path, in that order. It is not a
stored session column. `working_dir` remains accepted as a deprecated request
alias for `project_path`.

Only one agent turn may run per session. A direct shell command has a separate
slot and may run while the agent is running or awaiting approval. Starting new
work in an archived session or project is rejected.

### Projects and worktrees

Projects are explicit database records; the server never scans the filesystem
to discover them. Paths must be absolute and are normalized lexically. Deleting
a project removes its Agent UI records and transcripts but **does not delete the
project directory**.

Worktrees are first-class resources rather than session-owned temporary
directories. `POST /worktrees` runs `git worktree add -b`, and any number of
sessions can attach to the resulting row. Deleting a session never removes its
worktree. Worktree removal is never forced: attached sessions, live detached
sessions, or dirty/untracked files cause `409` and leave both directory and row
intact.

See [`docs/worktree_sessions_design.md`](docs/worktree_sessions_design.md) and
[`docs/archive_worktree_design_decisions.md`](docs/archive_worktree_design_decisions.md)
for the detailed lifecycle decisions.

### Archiving

Projects and sessions use nullable `archived_at` timestamps. List endpoints
return both live and archived rows; clients split the views.

Archiving a project archives its live sessions. Unarchiving restores only the
sessions archived by that cascade. Busy sessions prevent archiving, and missing
working directories prevent restoration. Archived sessions retain transcript
access and can still be renamed, reconfigured, detached, or deleted, but cannot
start turns or shell commands.

## Session WebSocket

Connect to:

```text
/ws/sessions/{session_id}
```

The socket is bidirectional. On connect, the server atomically establishes a
snapshot boundary, queues up to the latest 200 persisted transcript rows, then
sends current `status` and `archived` snapshots. Live events follow the snapshot
without interleaving. Every connection has one bounded writer queue, so a slow
or failed client cannot block healthy subscribers.

Clients can send:

```jsonc
{ "type": "input", "text": "Implement the parser" }
{ "type": "bash", "command": "git status --short" }
{ "type": "approval_response", "request_id": "perm_1", "behavior": "allow" }
{ "type": "approval_response", "request_id": "perm_1", "option_id": "deny",
  "message": "Do not remove generated fixtures" }
{ "type": "question_response", "request_id": "question_1",
  "answers": { "Which format?": "JSON" } }
```

Prompts and shell commands can alternatively be started over REST. All
subscribers receive the resulting events.

### Canonical tool actions

Native tool payloads are never the client rendering contract. New `tool_use`
events contain exactly a call identity and a canonical action:

```json
{
  "type": "tool_use",
  "call_id": "toolu_01ABC",
  "action": {
    "kind": "command",
    "command": "git status --short",
    "description": "Show repository status",
    "shell": "bash"
  }
}
```

If that invocation requires permission, the approval repeats the same action so
it can render independently and uses the same `call_id`:

```json
{
  "type": "approval_request",
  "request_id": "perm_456",
  "call_id": "toolu_01ABC",
  "action": { "kind": "command", "command": "rm -rf build/", "shell": "bash" },
  "options": [
    { "id": "allow", "name": "Allow", "kind": "allow_once" },
    { "id": "deny", "name": "Deny", "kind": "reject_once" }
  ]
}
```

`request_id` identifies the interaction; `call_id` identifies the tool
invocation. Approval options may be empty, in which case clients should send a
generic `behavior: "allow"` or `"deny"`.

Supported action kinds are:

| Kind | Main fields | Meaning |
|---|---|---|
| `command` | `command`, optional `description`, `timeout_ms`, `shell` | Shell/process command |
| `read` | `path`, optional `offset`, `limit` | Read a file |
| `edit` | `path`, `edits[]` | One or more exact-text edits |
| `write` | `path`, `content` | Replace/write a file |
| `search` | `mode`, `query`, optional `path`, `glob`, `limit` | Content or path search |
| `list` | optional `path`, `limit` | Directory listing |
| `web` | `operation` plus `query` or `url`, optional `prompt` | Web search or fetch |
| `task` | `description`, optional `prompt`, `agent` | Delegate work |
| `other` | `name`, `arguments` | Unknown or malformed native tool fallback |

Recognized actions do not expose the provider's original tool name or argument
spelling. Unknown tools preserve both under `other`. Tool results are not part
of this schema.

Auto-approval is derived from `action.kind`: `command` uses
`auto_approve_command`; `edit` and `write` use `auto_approve_write`. There is no
provider-name table and no `category` field on the wire. An auto-approved
request carries `"auto_approved": true` and is followed by an
`approval_response` with `"auto": true`.

### Other server events

```jsonc
{ "type": "output", "text": "Streaming agent text" }
{ "type": "status", "status": "running" } // idle | running | awaiting_approval
{ "type": "approval_response", "request_id": "perm_1", "behavior": "allow" }
{ "type": "question", "request_id": "question_1", "questions": [/* ... */] }
{ "type": "question_response", "request_id": "question_1", "answers": {/* ... */} }
{ "type": "input", "text": "Echoed prompt" }
{ "type": "bash_input", "command": "pytest -q" }
{ "type": "bash_output", "command": "pytest -q", "stdout": "...", "stderr": "",
  "exit_code": 0, "duration_ms": 821, "timed_out": false, "truncated": false }
{ "type": "renamed", "name": "new label" }
{ "type": "settings", "auto_approve_write": true, "auto_approve_command": false }
{ "type": "archived", "archived_at": "2026-08-26T11:02:00Z" }
{ "type": "worktree_detached", "worktree_id": null, "working_dir": "/projects/app-fix" }
{ "type": "done", "session_id": "harness-session-id" }
{ "type": "error", "message": "Human-readable failure" }
```

Transcript events are persisted; lifecycle snapshots such as `status`,
`archived`, and `done` are not historical transcript records. SQLite is the
source of truth for current metadata.

A `question` is different from approval: the agent is asking the user to choose
content, not asking permission to execute a tool. Claude Code provides
`AskUserQuestion`; the bundled Pi extension supplies an equivalent tool.

## File-tree WebSocket

Connect to:

```text
/ws/sessions/{session_id}/files
```

This server-to-client socket indexes the session's effective working directory.
It first sends an authoritative `file_tree_snapshot`, then revisioned
`file_tree_patch` frames. The server shares one inotify-backed tree between
subscribers watching the same root and enforces watch, path-count, path-byte,
patch-size, and queue limits.

Built-in rules omit common VCS metadata, dependency directories, caches, and
editor junk. A root `.agent-ui-ignore` file adds gitignore-style rules and can
use negation to override built-ins. Mount-point subtrees and symlinked
directories are not traversed. Failures are sent as `file_tree_error` when
possible before the socket closes.

The complete protocol and ignore semantics are in
[`docs/file_tree_completion_design.md`](docs/file_tree_completion_design.md).
The implementation is Linux-specific because it uses inotify.

## Direct shell mode

A client conventionally treats input beginning with `!` as a direct shell
command, strips the prefix, and sends a `bash` WebSocket message. The server
does not inspect ordinary prompts for `!`.

Each command is a fresh `bash -lc` process in the session's working directory:

- no `cd`, environment variable, or shell function persists to the next command;
- stdin is closed, so interactive programs receive EOF rather than hanging;
- timeout terminates the entire process group and escalates to `SIGKILL`;
- stdout and stderr are drained and truncated to bounded head/tail output;
- one shell command may run per session, independently of its agent turn;
- the stop endpoint cancels both the shell command and agent process.

This path deliberately bypasses the agent, context window, tokens, and approval
system.

## Agent adapters

Adapters implement the small lifecycle interface in `agent.py`:

```python
class AgentAdapter:
    async def start_turn(session, prompt): ...       # async AgentEvent stream
    async def send_approval(session, request_id, behavior,
                            *, option_id=None, message=None): ...
    async def send_answer(session, request_id, answers): ...
    async def stop(session): ...
```

| Agent | Native protocol | Resume | Interactive gate |
|---|---|---|---|
| Claude Code | `stream-json` over stdio | `--resume <id>` | stdio permission requests |
| Pi | RPC JSONL over stdio | `--session <id>` | bundled extension dialogs |

Claude Code runs with `--permission-mode default` and
`--permission-prompt-tool stdio`. Pi has no native permission system, so
`pi_extension.ts` gates mutating tools and supplies `AskUserQuestion`. The
adapter waits for the extension's ready handshake before sending a prompt;
failure to load the gate fails closed. `--no-extensions` prevents project-local
Pi extensions from modifying tool input after approval.

To add another harness:

1. Implement `AgentAdapter` and give it a user-facing `LABEL`.
2. Translate native calls into the canonical models in `actions.py`; keep native
   names and spellings in adapter-specific code.
3. Preserve call identity between `tool_use` and `approval_request`.
4. Register the adapter in `main.py`.
5. Add captured native fixtures and normalization/lifecycle tests.

A client should never branch on the session's agent id to render a recognized
action.

## Persistence

SQLite runs in WAL mode with foreign keys enabled. It stores:

- `projects` — explicit project identity, path, label, archive timestamp
- `worktrees` — project-owned Git worktree paths and branch records
- `sessions` — adapter id, harness resume id, state, settings, timestamps, links
- `scrollback` — append-only persisted transcript events

Integer project/session ids are monotonic and are not reused. Startup resets any
persisted `running` or `awaiting_approval` session to `idle`, because subprocess
handles, pending interactions, tasks, and subscribers are intentionally
in-memory and cannot survive a restart.

Older database shapes are migrated on open. Back up `sessions.db` before
upgrading if its history matters.

## Development

```sh
uv sync
uv run python -m unittest discover -s tests
uv build
```

Tests use temporary SQLite databases, real short-lived Bash processes, real Git
repositories/worktrees, simulated WebSockets, captured Claude Code/Pi events,
and inotify-backed temporary trees. They do not start a real coding-agent
subprocess or require network access.

Runtime dependencies are FastAPI, uvicorn, Pydantic, python-dotenv,
`inotify-simple`, and `pathspec`, plus Python's `sqlite3` and `asyncio` modules.
