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
- Supports per-session auto-approval for commands, file writes, and inter-agent communication
- Creates and manages Git worktrees independently of sessions
- Runs explicit one-shot shell commands without involving the agent
- Synchronizes a session's working-directory file tree for path completion
- Optionally serves a static web client from the same origin as the API

### Non-goals

- Multi-user accounts, or authentication beyond one shared token
- A terminal emulator or persistent interactive shell
- A hardened multi-tenant or network security boundary

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
├── pi_web_search/    # vendored pi-web-search; Pi's web_search and url_context
├── usage.py          # subscription usage percentages from Claude and Codex
├── db.py             # SQLite schema, migrations, transcript storage
├── file_tree.py      # snapshots, patches, ignore rules, inotify lifecycle
├── git.py            # bounded Git worktree operations
├── network_guard.py  # shared-token auth, Host and WebSocket Origin checks
└── shell.py          # bounded one-shot bash execution

tests/
├── test_core.py      # API, database, adapters, WebSocket ordering, shell, Git
├── test_actions.py   # canonical schema and provider normalization
├── test_file_tree.py # scans, watches, limits, patches, endpoint lifecycle
└── fixtures/         # captured native tool events from supported agents
```

## Security model

The intended deployment binds uvicorn to a WireGuard interface and permits
only trusted peers to reach it. On top of that, every client must present a
shared token. The server generates it on first start in
`~/.config/agent-ui-server/token` (mode `0600`; `AUTH_TOKEN_FILE` overrides the
path) and prints where it is, never the token itself:

```sh
WIREGUARD_IP=10.0.0.1 PORT=8000 uv run agent-ui-server
cat ~/.config/agent-ui-server/token   # enter this in each client
```

The token is a file rather than an environment variable because the server's
environment is inherited by `!` commands, unsandboxed agents, and git, any of
which could print it into a transcript. Sandboxed agents can't read the file,
since no sandbox mount includes it. Don't add `~/.config` itself as a sandbox
path. Unsandboxed agents and `!` commands run as the server user and can read
it like any other file.

The server refuses to start if the file is a symlink, belongs to another user,
is readable by other users, or holds fewer than 32 characters.

Clients send the token as `Authorization: Bearer <token>`. Browsers can't set
headers on a WebSocket, so WebSockets also accept `?token=<token>`, which the
server strips before the endpoint or access log sees it. Only the static web client
under `WEB_ROOT` loads without the token. Enter the token once in each
client's settings; see [docs/auth_client_handoff.md](docs/auth_client_handoff.md).
To rotate it, delete the file, restart, and enter the new token in each
client.

A browser on a peer device carries the peer's network access into every page
it opens, so the server also refuses requests a web page could forge:

- Any request whose `Host` is not the bind address or a name in
  `ALLOWED_HOSTS` gets a 400. This blocks DNS rebinding.
- A WebSocket handshake whose `Origin` is present and is not
  `http://<allowed host>:<PORT>` is refused with 403. WebSockets are exempt
  from CORS, so without this any page could drive a session. Handshakes with
  no `Origin` come from non-browser clients such as the Android app and are
  accepted.

If you open the server by a hostname rather than its IP, add that name to
`ALLOWED_HOSTS` or browsers will get 400/403.

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

On first start the server generates the client token; see
[Security model](#security-model).

You only need to install the agent CLI(s) you intend to use. For example, a
Claude Code-only deployment does not need Pi installed. Note that `/agents`
lists the adapters built into the server; it does not check whether each CLI is
installed or authenticated. A turn fails when its selected CLI is unavailable.

The package also exposes `python -m agent_ui_server` and can be installed with
`uv tool install .`, `pip`, or `pipx`.

### With Docker or Podman Compose

```sh
docker compose up -d --build
docker compose exec agent-ui-server cat /home/agent/.config/agent-ui-server/token
```

The token file lives in the persistent `claude-auth` home volume, so it
survives rebuilds.

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
| `AUTH_TOKEN_FILE` | `~/.config/agent-ui-server/token` | Client token file, generated on first start |
| `ALLOWED_HOSTS` | unset | Extra hostnames, comma-separated, accepted in `Host`/`Origin` besides `WIREGUARD_IP` |
| `SESSION_DB` | `sessions.db` | SQLite database path |
| `CLAUDE_BIN` | `claude` | Claude Code executable |
| `CLAUDE_CONFIG_DIR` | `~/.claude` in sandbox | Claude configuration, credentials, and session directory |
| `PI_BIN` | `pi` | Pi executable |
| `PI_EXTENSION` | bundled `pi_extension.ts` | Pi approval/question extension |
| `PI_WEB_SEARCH` | bundled `pi_web_search/index.ts` | Pi web extension; empty disables web access |
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
| `GET` | `/usage` | Five-hour and weekly consumption for each subscription |
| `GET` | `/sandbox-paths` | Read server-wide sandbox path defaults |
| `PATCH` | `/sandbox-paths` | Replace server-wide sandbox path defaults |
| `GET` | `/projects` | List projects and live/archive session aggregates |
| `POST` | `/projects` | Register/create a project directory |
| `PATCH` | `/projects` | Update project sandbox paths or archive/unarchive with session cascade |
| `DELETE` | `/projects` | Forget a project, sessions, and managed worktrees |
| `GET` | `/worktrees` | List worktrees; optionally filter by `project_path` |
| `POST` | `/worktrees` | Create a worktree on a new branch from project HEAD |
| `DELETE` | `/worktrees/{id}` | Remove a clean, unused worktree |
| `GET` | `/sessions` | List sessions and current metadata |
| `POST` | `/sessions` | Create a session for a registered project |
| `PATCH` | `/sessions/{id}` | Rename, archive, or change auto-approval/sandbox settings |
| `POST` | `/sessions/{id}/detach-worktree` | Detach an archived session while preserving its cwd |
| `POST` | `/sessions/{id}/turn` | Start an agent turn |
| `POST` | `/sessions/{id}/bash` | Start a direct one-shot shell command |
| `GET` | `/sessions/{id}/scrollback` | Read persisted transcript events with cursor pagination |
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

Create a project before creating a session. Every request needs the token
(see [Security model](#security-model)):

```sh
AUTH_TOKEN="$(cat ~/.config/agent-ui-server/token)"
```

```sh
curl -X POST http://127.0.0.1:8000/projects \
  -H "authorization: Bearer $AUTH_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"path":"/absolute/path/to/project","name":"my project"}'

curl -X POST http://127.0.0.1:8000/sessions \
  -H "authorization: Bearer $AUTH_TOKEN" \
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

### Persisted scrollback and input cursors

`POST /sessions/{id}/turn` (body `{"prompt":"..."}`) and
`POST /sessions/{id}/bash` (body `{"command":"..."}`) return HTTP **202**:

```json
{"status": "running", "message_id": 123}
```

`message_id` is the persisted input event's ID. Use it as `after` to read
subsequent persisted events, even if output started before the first read:

```sh
curl 'http://127.0.0.1:8000/sessions/7/scrollback?after=123&limit=200' \
  -H "authorization: Bearer $AUTH_TOKEN"
```

```json
{
  "messages": [
    {
      "id": 124,
      "session_id": 7,
      "ts": "2026-01-01T12:00:00+00:00",
      "type": "output",
      "payload": {"text": "Hello"}
    }
  ],
  "next_cursor": 124,
  "has_more": false
}
```

- `after`: optional nonnegative ID, exclusive; omit to read from the beginning.
- `limit`: defaults to 200; must be within 1–1000. Invalid parameters return 422.
- Messages belong only to the requested session, ordered by ID ascending, with
  decoded JSON payloads. At most `limit` messages are returned.
- Advance `after` to `next_cursor` for the next page. Empty pages retain the
  supplied `after`, or return `null` when it was omitted.
- `has_more` indicates whether additional persisted events existed at query
  time; `false` does not mean the running turn or command has finished.

Reads return immediately without waiting for new events or requiring a live
WebSocket connection. Archived sessions remain readable; nonexistent sessions
return 404.

### Subscription usage

`GET /usage` reports how much of each plan's five-hour and weekly quota has
been consumed. A subscription is not a harness — the Codex plan is read from
Pi's credential file here, but the same plan can back any client — so the two
keys name plans rather than adapters:

```json
{
  "claude_code": {
    "five_hour": {"used_percent": 23.0, "reset_at": 1789933800},
    "weekly": {"used_percent": 12.0, "reset_at": 1790218800},
    "error": null
  },
  "codex": {
    "five_hour": {"used_percent": 6.0, "reset_at": 1789936104},
    "weekly": {"used_percent": 51.0, "reset_at": 1790415517},
    "error": null
  }
}
```

`used_percent` is the share of the window already spent, and `reset_at` is Unix
seconds — Anthropic reports an ISO-8601 instant and OpenAI a Unix timestamp, so
the former is converted. Codex's five-hour window is rolling: until the first
request of a window its reset is simply five hours out, and it firms up once
usage starts.

Both keys are always present. A plan that is unauthenticated or unreachable
reports null windows and a reason in `error` instead of failing the request, so
authenticating only one of the two still yields a useful response:

```json
{"codex": {"five_hour": null, "weekly": null, "error": "not authenticated"}}
```

Access tokens are read from `~/.claude/.credentials.json` and
`~/.pi/agent/auth.json` and used as they are. The server never refreshes them:
Claude Code rewrites its own credential file when it refreshes, and a second
writer would race it. An expired token surfaces as `"error": "HTTP 401"`.

### Additional sandbox paths

`GET /sandbox-paths` returns `{"sandbox_paths": []}` by default. Configure host files
or directories available to all sandboxed agents with `PATCH /sandbox-paths`:

```json
{
  "sandbox_paths": [
    {"path": "~/.config/my-tool"},
    {"path": "$HOME/.local/share/my-tool", "write": true}
  ]
}
```

Read access is implicit; `write` defaults to `false`. Files/directories must exist.
Paths expand using the server user's home/environment (`~`, `$VAR`, `${VAR}`),
without shell execution, and must be absolute after expansion. Undefined variables
retain Python's literal expansion behavior. Symlink targets are mounted at the
expanded path the program expects. Configuration files may contain credentials;
read-only access still exposes them. Writable access modifies real host data.

Projects expose their own `sandbox_paths` list in project responses. Set it on
`POST /projects` or update it with `PATCH /projects`:

```json
{
  "path": "/path/to/project",
  "sandbox_paths": [{"path": "~/.config/my-tool", "write": true}]
}
```

Project entries merge with server defaults by expanded, normalized destination
path. The project `write` value wins for matching paths, including when omitted
(default `false`). Other server paths remain inherited. Each supplied list replaces
that scope's entire list; omission leaves it unchanged. `[]` clears server defaults
or resets a project to inheritance. There is no per-session list and no mechanism
to remove an inherited path, only to override its write permission.

Paths persist as individual rows in the `sandbox_paths` table (`project_id IS NULL`
for server defaults), not JSON columns. Changes affect future sandboxed turns, including worktree
sessions. Running turns retain their existing mounts. Updates are allowed during
turns. Paths conflicting with protected/system mounts or exposing the entire home
are rejected, as are duplicate/nested paths in one list. Cross-scope conflicts
and conflicts with session-specific built-in mounts fail turn preparation; no
requested mount is silently skipped. Sandbox-disabled turns and direct user shell
commands are unaffected. Validation errors on updates return `400`; malformed
request shapes return FastAPI's usual `422`.

See [the design](docs/sandbox_paths_design.md) for mount rules and lifecycle details.

### Inter-agent communication

Claude and pi sessions expose three server-approved tools:

- `message_session(session_id, message)` submits an input to an idle session and
  returns its persisted input ID, without waiting for a response.
- `start_session(name, project_path, message, agent?, worktree_id?)`
  creates a sandboxed session under an existing project, sends its first message, and returns
  `session_id` and `message_id`. If messaging fails, the session is retained and its
  ID is reported in the error.
- `read_session(session_id, after?, limit=200)` returns one unchanged scrollback
  page (`messages`, `next_cursor`, `has_more`). The cursor is exclusive; limit is
  1–1000. Reads do not wait for completion.

Claude names these `mcp__agent_ui__message_session`, etc., on the existing SDK MCP
server. Pi registers them in the bundled extension. Every call, including reads,
requires approval; ordinary read/write/command auto-approval does not apply. The
sending session's `auto_approve_inter_agent_communication` toggle (default off,
set with `PATCH /sessions/{id}`) auto-approves them, except that a message or read
targeting an unsandboxed or missing session always asks.
They are available independently of sandbox-bypass flags and session sandboxing.
Busy targets reject messages rather than queueing them.

Input payloads now include `source`: `{"type":"user"}` for user submissions, or
`{"type":"agent","session_id":42}` for a message from session 42. The server sets
this identity, including for a newly started session's first message. Both live
WebSocket input events and persisted scrollback payloads carry the field. Missing
`source` on legacy records means user-originated.

Stored `text` is unchanged. At harness delivery the server prefixes agent messages
with sender context, allowing the recipient to reply using that session ID. No
parent/child roles are imposed. Client rendering and links to sender sessions are
UI concerns. See [the design](docs/inter_agent_communication_design.md).

### Experimental sandbox-bypass tools

Enable either or both tools in the server environment:

| Setting | Tool in sandboxed sessions |
| --- | --- |
| `CLAUDE_HOST_EXEC=1` | Claude: `mcp__agent_ui__bypass_sandbox(command, reason)` |
| `PI_HOST_EXEC=1` | Pi: `bypass_sandbox(command, reason)` |

Both settings default to disabled. Non-sandboxed sessions receive neither tool
nor its system-prompt guidance.

Pi registers its tool in the bundled extension and forwards requests/results over
the existing extension UI RPC bridge. There is no MCP layer. Registration is
checked during the startup handshake, and the server independently enforces
approval, even if an extension attempts to skip its normal tool gate.
Pi gets the same generated mount description with Pi-specific usage guidance.

Claude uses SDK MCP control messages over the existing stdin/stdout pipes:
no MCP package, HTTP endpoint, or helper process. Its tool uses normal deferred
discovery; no `alwaysLoad` override is set. For sandboxed turns with this feature enabled, the server appends a
system prompt listing the actual resolved working directory and writable and
read-only mounts. The prompt and Bubblewrap arguments are generated from the
same mount plan, including runtime/config paths, worktree Git metadata, merged
server/project extra paths, and the sandbox `/tmp` backing directory. Synthetic
and optional mounts are labeled; mount permissions do not override ordinary
filesystem permissions.

Claude's prompt also explains why `dangerouslyDisableSandbox` cannot escape
Bubblewrap and when to discover and use `mcp__agent_ui__bypass_sandbox` through
ToolSearch. The mount plan is rebuilt for every turn, including resumed sessions.

The agent remains sandboxed. Each host-tool invocation asks the user to
**Execute outside sandbox**, showing the exact command, reason, and session
working directory. Ordinary command auto-approval does not bypass this gate;
only a per-invocation approval permits execution. Denial is returned to the agent
as a tool error. Both adapters permit dispatch through the ordinary tool gate to
this server-owned approval gate, avoiding duplicate prompts.

Approved commands use the same server-side shell runner as user `!` commands:
`bash -lc`, server environment, session working directory, no interactive stdin,
and the existing `BASH_TIMEOUT_SECONDS` / `BASH_OUTPUT_LIMIT` limits. Output and
exit status are returned when execution finishes (not streamed). Cancellation
or turn termination cancels active host commands. Containerized deployments
execute inside the server container, not outside that container. Sandbox `/tmp`
and the server's `/tmp` are different; use project paths for shared files.

This grants the approved command and everything it invokes the server account's
access, including its environment. It does not change the trusted-user deployment
model.

Smoke test: enable the corresponding flag, start a sandboxed turn, and ask it to use
`bypass_sandbox` for `printf 'host execution works\\n'`. Check both denial and approval,
then test Podman in your deployment. The automated tests use a simulated Claude
peer; they do not establish compatibility with a real Claude version. Pi tests
also load the real extension and exercise its callback without making model calls.

### Session sandbox

Client integration: [sandbox client handoff](docs/sandbox_client_handoff.md).

Sessions expose a boolean `sandbox`, defaulting to `true` for both new and
existing sessions. Set it on `POST /sessions` or change it between turns:

```sh
curl -X PATCH http://127.0.0.1:8000/sessions/1 -H "authorization: Bearer $AUTH_TOKEN" \
  -H 'content-type: application/json' -d '{"sandbox":false}'
```

A PATCH containing `sandbox` returns `409` while a turn is running, awaiting
approval/answers, or still shutting down. Successful changes emit a `settings`
WebSocket event and apply to the next turn. **Both Pi and Claude Code implement
sandboxing.** Direct user shell commands (`!` or `POST /sessions/{id}/bash`) are
not sandboxed.

Sandboxed turns require Linux, `bwrap` (Bubblewrap), and permission to create its
namespaces. Install Bubblewrap with your OS package manager (the container image
includes it). `PI_BIN` should point to the installer's `<runtime>/bin/pi` beside
`bin/node`, or a system installation under `/usr`. `CLAUDE_BIN` supports native
Claude binaries and npm Node launchers; only their runtime resources are exposed
read-only. Claude auto-updates are disabled inside the sandbox. A setup failure
fails the turn; it never silently runs unsandboxed.

The sandbox uses the reference policy:

- Writable host bind mounts: the working directory, the agent's configuration
  directory (`~/.pi` for Pi, `~/.claude` or `CLAUDE_CONFIG_DIR` for Claude), and shared
  `/tmp/agent-sandbox-<uid>` mounted as `/tmp`. Scratch persists across turns
  and agents. Its storage follows the host filesystem (including host tmpfs).
- Linked worktrees additionally mount their Git metadata read/write at its
  original path (normally the main repository's `.git`). This allows index
  updates, commits, and shared objects/refs without exposing the main checkout's
  source files. Shared Git metadata is not isolated between worktrees.
- The home directory is hidden except for these explicit mounts and the agent's
  read-only runtime. Home itself cannot be the sandboxed working directory.
- System programs/libraries, HTTPS/DNS configuration, and server-supplied Pi
  extensions are read-only. Synthetic root/parents, `/dev`, and `/proc` are
  read-only; standard devices remain usable.
- Host networking is retained, including localhost and Tailscale. This is not
  network isolation or isolation between agents sharing scratch/config.
- Inherited environment variables are cleared. The common environment restores
  only `HOME`, `USER`, `PATH`, `TERM`, `LANG`, `TMPDIR`, and `XDG_CACHE_HOME`.
  Claude additionally gets `CLAUDE_CONFIG_DIR` and `DISABLE_AUTOUPDATER=1`.
  Authenticate using Pi's `~/.pi/agent/auth.json` or Claude's configuration
  directory; environment-only credentials, SSH agents, and home dotfiles are
  not carried into the sandbox.

Claude normally stores global state in `~/.claude.json`, whose atomic updates
would require a writable home directory. Sandboxed turns instead set
`CLAUDE_CONFIG_DIR`, placing global state inside the writable config directory.
For the default profile, the server imports `~/.claude.json` once into
`~/.claude/.claude.json` if neither that file nor legacy `.config.json` exists.
The import is private (0600) and never overwrites existing profile state.
Credentials and sessions stay in the existing `~/.claude` directory. The imported
global-state file is independent afterward: unsandboxed/default CLI runs still
use `~/.claude.json`. To use the same profile in both modes and for login, set
an absolute `CLAUDE_CONFIG_DIR` explicitly for the server and your CLI. Explicit
profiles are not seeded from the default home file. For example:

```sh
CLAUDE_CONFIG_DIR="$HOME/.claude" claude auth login
```

Pi's config-directory environment overrides are still cleared in sandbox mode.
Do not put secrets in API-key environment variables and expect them to cross
the boundary; neither adapter forwards them.

The bundled gate is mounted as a file; the web extension's directory is mounted
read-only for its sibling imports. Custom extension dependencies must be within
these mounts or the runtime. Paths outside the mounts remain unavailable; Git
worktree metadata is discovered from the working directory's `.git` file and
its `commondir` pointer, not by mounting the entire parent project.

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

A `web` action carries one target. Pi's `url_context` accepts up to twenty URLs
under a single question, and rendering such a call as a fetch of its first URL
would misrepresent it, so only the single-URL form becomes a `web` action;
multi-URL calls fall back to `other`, which shows every URL. Extra `urls` passed
alongside a `web_search` query are dropped from the action, since a canonical
search carries a query and forbids a URL.

Auto-approval is derived from `action.kind`: `command` uses
`auto_approve_command`; `edit` and `write` use `auto_approve_write`. Inter-agent
tools (`other` actions named `message_session`, `start_session`, `read_session`)
use `auto_approve_inter_agent_communication`, as described above. There is no
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
{ "type": "settings", "auto_approve_write": true, "auto_approve_command": false,
  "auto_approve_inter_agent_communication": false, "sandbox": true }
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

Claude Code runs with `--permission-mode default`,
`--permission-prompt-tool stdio`, and inline `--settings` setting
`permissions.ask` to `["*"]` and `sandbox.autoAllowBashIfSandboxed` to `false`.
The adapter's explicit `AUTO_APPROVE_TOOLS` allowlist skips UI prompts for:

- Reads/web/code intelligence: `Read`, `Glob`, `Grep`, `WebSearch`, `WebFetch`, `LSP`.
- Discovery: `ToolSearch`, `ListMcpResourcesTool`, `ReadMcpResourceTool`,
  `WaitForMcpServers`, `ListAgents`, `CronList`.
- Task bookkeeping: `TaskGet`, `TaskList`, `TaskOutput`, `TaskCreate`,
  `TaskUpdate`, `TodoWrite` (task metadata, not project-file writes).
- Planning/reporting: `EnterPlanMode`, `ReportFindings`.

`ToolSearch` loads tool definitions; invoking a discovered tool is still gated
independently. MCP resource reads are allowed, but arbitrary `mcp__...` tool
calls are not. `AskUserQuestion` uses the question flow. All other tools,
including unknown tools, request approval, subject to the server's per-session
write/command auto-approve toggles. This includes commands, file writes,
agent/skill/workflow execution, stopping tasks, scheduling, messaging/uploads,
worktree changes, and `ExitPlanMode` (plan approval). Inherited deny rules still
apply upstream. Tool classifications follow the
[Claude tools reference](https://code.claude.com/docs/en/tools-reference), not
its default permission column: some normally unprompted tools launch work or
have external side effects.

Pi has no native permission system, so
`pi_extension.ts` gates mutating tools and supplies `AskUserQuestion`. The
adapter waits for the extension's ready handshake before sending a prompt;
failure to load the gate fails closed. `--no-extensions` prevents project-local
Pi extensions from modifying tool input after approval.

Pi also ships no web access, so the vendored
[pi-web-search](https://github.com/ttttmr/pi-web-search) extension is loaded as
a second explicit `-e` and gives Pi `web_search` and `url_context`. Both
normalize into canonical `web` actions, so a client renders a Pi search exactly
as it renders Claude Code's `WebSearch`. Because `--no-extensions` drops
anything Pi discovered from its own settings, an extension must be passed by
path to survive; vendoring it under `src/agent_ui_server/pi_web_search/` keeps
that guarantee and ships it in the wheel. Set `PI_WEB_SEARCH` to another path to
substitute an extension, or to the empty string to leave Pi without web access.

The web tools search through whichever provider backs the session's current
model, using the credentials Pi already holds — a subscription login is enough,
and no separate API key is required. They are read-only locally and so bypass
the approval gate. Claude Code's adapter likewise auto-approves `WebSearch`
and `WebFetch`. Removing web tool names from the corresponding adapter/extension
auto-approval allowlist restores the prompt. Each call is still network egress and
spends a full inference request on the provider. `url_context` is Gemini-only
and hides itself on other models.

Expect a search to take noticeably longer than Claude Code's. Claude Code's
`WebSearch` returns ranked results for the agent to read, while pi-web-search
runs a nested inference call that searches, reads, and writes a cited prose
answer before the tool returns at all. Pointing that nested call at a fast model
is the main lever: pi-web-search reads `provider` and `model` from
`web-search.json` in Pi's agent directory — `~/.pi/agent/` unless
`PI_CODING_AGENT_DIR` moves it, or `PI_WEB_SEARCH_CONFIG` overrides the file
path outright — and uses that model for `web_search` instead of the
conversation's:

```json
{ "provider": "openai-codex", "model": "gpt-5.6-luna" }
```

`provider` and `model` must match a Pi model registry entry, and that provider
must be authenticated in Pi; a miss silently leaves web search unavailable
rather than falling back. Those two keys are the whole schema — there is no
thinking or effort setting, and the extension deliberately sends no reasoning
effort so each provider applies its own default. The setting does not affect
`url_context`, which always uses the conversation model. See [`src/agent_ui_server/pi_web_search/VENDOR.md`](src/agent_ui_server/pi_web_search/VENDOR.md)
for provenance and update steps.

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
