# Client handoff: per-session sandbox setting

Status: **server implementation complete on `feature/pi-sandbox`**. This spec
covers the desktop and Android clients. It describes the implemented contract;
no server changes are required for the client work below.

All endpoint paths are relative to the server root.

**Updated: both Pi and Claude Code now implement sandboxing.** This supersedes
this document's earlier Pi-only guidance. Deploy clients using this contract
with the updated server; the earlier Pi-only server stored Claude's flag but
did not enforce it.

## Decisions already made

- Sessions have a persisted boolean `sandbox`, defaulting to **`true`**.
  Existing sessions are migrated to `true` as well. This is server state, not
  a device preference.
- Both **Pi** (`agent: "pi"`) and **Claude Code** (`agent: "claude-code"`)
  implement sandboxing. The same control and busy-turn rules apply to both.
- The setting applies to the next agent turn. Each turn starts a new process;
  changing this setting does not reset the conversation or its resume ID.
- A request supplying `sandbox` is rejected while an agent turn is active,
  including while awaiting approval or answers, and during turn cleanup.
  This restriction applies to all agents, including Claude Code.
- Sandboxing and tool auto-approval are independent. Changing one must not
  silently change the other.

## Server contract

### Session objects

`GET /sessions` returns an array of session objects with the added field:

```jsonc
{
  "id": 7,
  "name": "fix login",
  "agent": "pi",
  "status": "idle",
  "sandbox": true,
  "auto_approve_write": false,
  "auto_approve_command": false
  // Other existing session fields are unchanged.
}
```

Creation and update responses also include `sandbox`. Keep it in the shared
session model/store so list views, settings, and the active session agree.
Session IDs remain JSON numbers. There is no new capability field in
`GET /agents`; for this release, `pi` and `claude-code` support the setting.

### Create: `POST /sessions` → `201`

```json
{
  "name": "fix login",
  "project_path": "/projects/app",
  "agent": "pi",
  "sandbox": true
}
```

The optional `worktree_id` works as before. Omitting `sandbox` means `true`;
explicit `false` disables it. Send JSON booleans, not strings or `null`.
The response is the complete created session.

### Change: `PATCH /sessions/{id}` → `200`

```json
{ "sandbox": false }
```

The response is the complete updated session. The endpoint is a partial
update: omitted fields are unchanged. Omitting `sandbox` does **not** turn it
off or reset it. Explicit `null` currently behaves as omission on PATCH, not
as a reset; clients should omit the field instead.

Send only the fields the user changed. In particular, do not include a cached
`sandbox` value in rename or auto-approval PATCHes: **even supplying the current
boolean value is rejected during an active turn**.

Errors use the normal FastAPI body:

```json
{ "detail": "Cannot change sandbox while a turn is in progress" }
```

| Status | Meaning | Client action |
|---|---|---|
| `409` | A turn is active or its task has not finished cleanup | Retain the last confirmed value, show `detail`, and let the user retry after the turn ends |
| `404` | `Session not found` | Refresh/remove the stale session |
| `422` | Request validation failed | Handle using existing validation-error support; `detail` may be an array |

The busy check runs before applying any fields in a mixed PATCH. A busy
sandbox rejection therefore also leaves any supplied name/auto-approval fields
unchanged. Prefer a dedicated `{"sandbox": ...}` request anyway.

An idle archived session may change this setting. A direct user shell command
running by itself does not block the change: only agent turns are checked.

### Live updates: session WebSocket

On `/ws/sessions/{id}`, the existing `settings` frame gains `sandbox`:

```json
{
  "type": "settings",
  "auto_approve_write": false,
  "auto_approve_command": true,
  "sandbox": true
}
```

A PATCH supplying sandbox or either auto-approval toggle emits this complete
settings frame to every subscriber of that session. It includes all three
values even if only one changed. Apply it to the session identified by the
socket; there is no session ID inside the frame. It is a metadata update, not
something to render as a transcript message.

**There is no settings snapshot on WebSocket connect, and settings frames are
not persisted/replayed.** Initial/reconnect state must come from
`GET /sessions`; do not wait for a settings frame to initialize the control.
Refresh on reconnect and on the client's normal focus/view-refresh triggers.
There is no global feed for sessions without an active subscription.

Use both the PATCH response and live settings frames as authoritative input.
Receiving the same value through both paths is normal and must be idempotent.
Do not let a stale REST refresh overwrite newer settings frames received while
that refresh was in flight (for example, buffer/reapply those frames).
There is no WebSocket command for changing the setting; use HTTP PATCH.

## Client UI work

### New session

For Pi and Claude Code, offer a **Sandbox** switch, enabled by default.
Optional helper text:

> Restricts agent file access. Applies to agent turns, not direct shell commands.

If the user has not changed it, omitting the field on creation is fine. Always
preserve explicit `false`; do not use a truthiness fallback that turns it back
into `true`.

### Existing session settings

- For both supported agents, bind the switch to the server-confirmed
  `session.sandbox` value.
- Disable it while status is `running` or `awaiting_approval`, while session
  state is loading/disconnected, or while its save is pending.
- Explain the busy restriction: **“Sandbox can only be changed between turns.”**
- Prefer pessimistic saving: retain the confirmed value until success. If using
  optimistic updates, roll back on failure and reconcile from server state.
- Serialize saves for a session, and do not start a new turn from this client
  while a sandbox save is pending. Otherwise the turn may start before the
  intended setting is saved.
- Still handle `409` even when local status says `idle`: another client can
  start a turn, or the server can still be finishing its task.
- Do not automatically stop the turn or queue/retry the setting change later.
  Let the user choose when to retry.
- Leave auto-approval controls' existing mid-turn behavior unchanged. This new
  restriction is specifically for sandbox changes.

Show the control for both `pi` and `claude-code`. For any future/unknown agent,
do not infer support just because a stored boolean is present: hide the control
or show **“Sandbox support unknown for this agent.”** Preserve its raw server
value. A sandbox badge indicates a setting, not proof that a process is
currently running.

## What the sandbox does (for accurate UI/help copy)

When enabled for Pi or Claude Code:

- Writable storage is limited to the working directory, the agent's config and
  sessions (`~/.pi` for Pi; `~/.claude` or the server's `CLAUDE_CONFIG_DIR` for
  Claude), shared scratch, and linked-worktree Git metadata.
- Linked worktrees can use their shared Git metadata read/write (normally the
  main project's `.git`) for staging and commits. The main checkout's files
  are not mounted. Git metadata is shared between worktrees, not isolated.
- Scratch persists and is shared across agents for the server user. It uses
  host storage, which may itself be tmpfs.
- Home contents are hidden except for explicit mounts, including the read-only
  agent runtime. System files and server-supplied extensions are read-only.
- Host networking remains available, including localhost and Tailscale.
- Inherited environment variables are cleared. Authentication must be available
  through the agent's mounted configuration rather than environment-only
  secrets. Claude's explicit config-directory override is retained and its
  auto-updater is disabled.
- Direct user shell commands (`!` / `POST /sessions/{id}/bash`) are **not**
  sandboxed. Tool approval prompts still function independently of sandboxing.

Do not describe this as network isolation, per-agent private storage, or
sandboxing of every command in a session.

Missing Bubblewrap or other setup failures fail the turn rather than falling
back to unsandboxed execution. Turn-start HTTP acceptance (`202`) is not proof
that sandbox setup succeeded: launch failures arrive through the normal
`error` events, followed by the session returning to `idle`. Show those errors;
never automatically disable sandboxing to get the turn running.

Claude-specific setup note (server/operator concern, not a client migration):
sandboxed Claude redirects global state into its config directory so atomic
updates do not require a writable home. The default profile imports
`~/.claude.json` once into `~/.claude/.claude.json` if no profile state exists;
credentials and sessions already live in `~/.claude`. The imported global state
is independent afterward. Operators wanting identical global state for sandbox,
unsandboxed, and CLI runs should explicitly set the same absolute
`CLAUDE_CONFIG_DIR` for all of them. Explicit profiles are not imported. Clients
must not read, copy, or manage these files.

## Acceptance checklist

- [ ] Pi and Claude Code creation default to enabled; explicit disabled survives creation,
      reload, and reopening on another device.
- [ ] Session fetches, PATCH responses, and `settings` frames update the same
      shared model without losing existing auto-approval values.
- [ ] Toggle changes apply between turns without creating/resetting a session.
- [ ] Running and awaiting-approval/answer states disable the control.
- [ ] A stale-idle `409` leaves the confirmed setting unchanged and shows the
      server explanation; it does not retry automatically.
- [ ] Rename/auto-approval requests do not inadvertently include `sandbox`.
- [ ] Reconnect refreshes REST state even if no settings frame arrives.
- [ ] Both Pi and Claude Code expose the control and save the flag identically.
- [ ] Worktree sessions offer the same control for both agents; no client-side mount/path
      configuration is required.
- [ ] Launch errors are visible and do not silently disable sandboxing.

This spec targets the updated server. If the client also supports older server
versions, an absent `sandbox` field means **unknown/unsupported server feature**,
not evidence that sandboxing is enabled. Do not synthesize a safety claim from
the new server's default.
