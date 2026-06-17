# agent-ui

A lightweight, self-hosted backend for driving [Claude Code](https://github.com/anthropics/claude-code)
agent sessions on a VPS from your phone. It replaces the Termux + tmux workflow:
spawn a session, stream its output, and approve or deny tool requests from a
native **Android app** over WireGuard.

> **Scope.** This repo is the backend / control plane: a FastAPI app, a Claude
> Code adapter, and a SQLite store. It is implemented and tested. The client is
> a separate Android app —
> [rauaap/agent-ui-android](https://github.com/rauaap/agent-ui-android) — that
> talks to it over the REST + WebSocket API documented below; that API is also
> reachable from any WebSocket client (`curl`, `websocat`, etc.) for testing.

## Why

Controlling a coding agent from a phone usually means SSH'ing into a box and
fighting a terminal multiplexer through a touchscreen keyboard. agent-ui gives
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
agent-ui/
├── main.py          # FastAPI app — REST routes, WebSocket endpoint, turn orchestration
├── agent.py         # AgentAdapter base + ClaudeCodeAdapter (stream-json) + OpenCodeAdapter (ACP)
├── opencode_permissions.json  # Default OpenCode permission config (gates tools to "ask")
├── db.py            # SQLite — session metadata + append-only scrollback
├── pyproject.toml   # Dependencies (managed with uv)
├── Dockerfile       # Fedora + uv + Claude Code CLI + nested Podman
├── compose.yaml     # Host-network service, bind mounts, named auth volume
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
docker compose exec agent-ui claude login          # Claude Code
docker compose exec agent-ui opencode auth login   # OpenCode
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

## API

### REST

| Method   | Path                    | Description                                                       |
|----------|-------------------------|------------------------------------------------------------------|
| `GET`    | `/sessions`             | List all sessions with metadata                                  |
| `POST`   | `/sessions`             | Create a session (`name`, `working_dir`, `agent`) → `201`        |
| `POST`   | `/sessions/{id}/turn`   | Send a prompt and spawn a turn → `202`                           |
| `POST`   | `/sessions/{id}/stop`   | Stop the running process, set status → `idle`, keep the session  |
| `DELETE` | `/sessions/{id}`        | Stop the process and delete the session and its scrollback       |

`POST /sessions` requires an absolute `working_dir`; the directory is created
(`mkdir -p`) if missing. `agent` is one of the registered adapters —
`"claude-code"` (the default) or `"opencode"`. Starting a turn on a session that
is not `idle` returns `409`.

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
  "tool": "Bash", "input": { "command": "rm -rf /tmp/test" } }
{ "type": "input", "text": "..." }                                    // echo of a submitted prompt
{ "type": "status", "status": "running" | "idle" | "awaiting_approval" }
{ "type": "done" }                                                    // turn complete
{ "type": "error", "message": "..." }

// Client -> Server
{ "type": "input", "text": "..." }                                    // start a new turn
{ "type": "approval_response", "request_id": "perm_1",                // answer an approval
  "behavior": "allow" | "deny" }
```

### Approval flow

1. The `claude` subprocess emits a `control_request` / `sdk_control_request`
   (subtype `permission` or `can_use_tool`) and blocks on stdin.
2. The backend sets status → `awaiting_approval`, persists the request to
   scrollback, and broadcasts an `approval_request` to all subscribers. The
   approval is held in an in-memory `asyncio.Future` keyed by `request_id`.
3. A client answers with `approval_response` (`allow` or `deny`).
4. The backend resolves the Future and writes a `control_response` to the
   subprocess stdin — `allow` echoes `updatedInput`, `deny` sends a denial
   message — then flips status back to `running` and the process continues.

If the session is stopped or the process exits while an approval is pending, the
Future is failed and the prompt is cleared.

## Data model

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

### `scrollback` (append-only)

| Column       | Type    | Notes                                                                          |
|--------------|---------|--------------------------------------------------------------------------------|
| `id`         | INTEGER | Autoincrement PK                                                               |
| `session_id` | TEXT FK | References `sessions.id` (`ON DELETE CASCADE`)                                 |
| `ts`         | TEXT    | ISO 8601                                                                       |
| `type`       | TEXT    | `input` \| `output` \| `tool_use` \| `approval_request` \| `approval_response` \| `error` |
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
    async def send_approval(session, request_id, behavior) -> None
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
real allow/deny approvals; they only differ in wire protocol.

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
as `approval_request` → `allow`/`deny`. The session id returned by
`session/new` is stored as `agent_session_id` and replayed via `session/load`.

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
