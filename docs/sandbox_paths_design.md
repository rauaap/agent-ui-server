# Configurable sandbox paths

Status: server implementation complete; client UI integration remains separate.

## Goal

Let users expose additional host files and directories to sandboxed agents, so
programs can find their existing configuration and other required resources.
The server cannot predict which programs or configuration paths a user needs.
Do not expose all of `~/.config` automatically.

## Agreed decisions

- **Server-wide defaults plus project-level paths** apply to sandboxed sessions,
  for both Pi and Claude. There are no per-session settings.
- Merge by path: inherit server entries, add project entries, and use the project's
  `write` value when the same path appears at both levels.
- Project settings apply to all of that project's sessions and worktrees.
- Each entry contains `path` and an optional boolean `write`.
- Read access is implicit. `write` defaults to `false`; `true` grants read/write.
- Files and directories are supported. A directory exposes its contents.
- Mount each resource at its expanded absolute path, not a client-selected
  destination, so programs find it where expected.
- The client sends the path string. Expansion happens on the server using the
  server user's home and environment.
- Support Python's standard `~`/`~user`, `$VAR`, and `${VAR}` expansion.
- Use standard undefined-variable behavior: leave undefined variables unchanged.
  Do not add custom undefined-variable validation; this is a trusted-user setting.
- Reject paths that remain relative after expansion. Do not implicitly use home,
  the session directory, or the server working directory as their base.

Example:

```json
{
  "sandbox_paths": [
    {"path": "~/.config/my-tool"},
    {"path": "$HOME/.local/share/my-tool", "write": true}
  ]
}
```

## Inheritance and precedence

Build the effective list from the server defaults and the session's project list.
Project entries override matching server entries; unrelated server entries remain.
An empty project list means inherit all server defaults, not disable them.
Removing a project override restores the corresponding server default.

Match paths after expansion and absolute-path normalization, so `~/config` and
`$HOME/config` identify the same destination. Preserve symlink destination
spellings: different destinations pointing at the same source are aliases, not
matching override keys, and remain subject to conflict validation.

A project can change an inherited path from read-only to writable or from writable
to read-only. Omitting `write` in a project entry means `false`, not inherit the
server's flag. This schema does not support removing an inherited path entirely.
Nested paths are not matching keys; project precedence does not bypass the
nested-mount conflict rules below.

For example, if the server grants writable access to `~/.config/tool` and the
project lists that path with `write: false`, that project's sessions receive
read-only access. Other projects retain the server default.

## API and persistence

Dedicated server-wide sandbox path endpoints (no generic `/settings` endpoint):

- `GET /sandbox-paths`: return `{"sandbox_paths": [...]}` for server defaults.
- `PATCH /sandbox-paths`: replace the whole list when `sandbox_paths` is supplied;
  omission leaves it unchanged, and `[]` clears it.

Extend project read/create/update APIs with a project `sandbox_paths` list.
On project updates, omission leaves the list unchanged; a supplied list replaces
that project's entries, and `[]` resets the project to server defaults.

Persist one row per path in a normalized `sandbox_paths` table. A NULL `project_id`
means server-wide; otherwise the row references its project with `ON DELETE CASCADE`.
There is no `server_settings` table or `projects.sandbox_paths` column.

```sql
CREATE TABLE sandbox_paths (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL CHECK (length(path) > 0),
    writable INTEGER NOT NULL DEFAULT 0 CHECK (writable IN (0, 1))
);
CREATE UNIQUE INDEX sandbox_paths_server_path
    ON sandbox_paths(path) WHERE project_id IS NULL;
CREATE UNIQUE INDEX sandbox_paths_project_path
    ON sandbox_paths(project_id, path) WHERE project_id IS NOT NULL;
```

The API's `write` flag maps to `writable` in SQLite. Each list replacement deletes
and inserts rows within one transaction. Lists are returned in insertion order.
New databases and databases predating this feature start with empty lists. There
is no automatic conversion from the earlier JSON implementation: that instance
requires the standalone, untracked conversion script before restarting the server.
Store the original path strings, not just their resolved values, so the UI can
round-trip the user's input and expansions follow the server environment.
Responses can normalize omitted `write` values to `false`.

Validate the complete replacement within its scope before saving; never partially
apply a list. Effective-list conflicts across scopes and session-specific built-in
mount conflicts are checked when preparing a turn. Updates return `400` with the
offending entry and reason for invalid paths; malformed request shapes use `422`.
PATCH omission (or `null`, following existing PATCH conventions) leaves a list
unchanged. Repeated project creation remains a no-op, including its path settings.

## Expansion and path handling

For each entry:

1. Expand environment variables with `os.path.expandvars(value)`.
2. Construct a `Path` and call `.expanduser()`.
3. Check `.is_absolute()` **before** calling `.resolve()`; otherwise resolving
   would silently turn a relative path into an absolute path using the server cwd.
4. Resolve with `strict=True` to check existence and identify the host source.
5. Require a regular file or directory.

If a path is a symlink, mount the resolved host file or directory at the symlink's
path inside the sandbox (after expanding `~` and environment variables). For
example, if `~/.config/tool` points to `/data/tool-config`, mount the host directory
`/data/tool-config` at `/home/apoleon/.config/tool` inside the sandbox. There it is
a mount point, not a symlink, so programs can use their usual config path.

Expansion is not shell execution: no command substitution, globbing, or shell
expressions. Missing variables stay literal, as Python specifies. A resulting
relative or nonexistent path fails the ordinary path checks, not a separate
undefined-variable check.

Examples:

| Input | Result |
| --- | --- |
| `/opt/tool/config` | Accepted if present and otherwise valid |
| `~/.config/tool` | Expanded under the server user's home |
| `$HOME/.config/tool` | Expanded using the server environment |
| `${HOME}/.config/tool` | Same |
| `.config/tool` | Rejected: path must be absolute |
| `./config` | Rejected: path must be absolute |

Validate on settings updates and again when preparing each turn, because files,
symlinks, and the server environment can change. A launch-time failure fails the
turn with a useful error; never skip a requested mount or run unsandboxed.

## Turn lifecycle

Behavior for server and project settings differs from the per-session
sandbox toggle: allow updates while turns are running, but apply them only to future
turns. Existing Bubblewrap processes cannot have their mounts changed this way.

Snapshot server and project lists consistently and merge them once when preparing
a turn. Both adapters use the same effective-list logic and shared mount-policy
implementation. Server changes affect future turns across projects; project
changes affect future turns only for that project. Changing settings does not
restart agents or revoke access from running turns; UI copy must say so.

The list has no effect on sessions with sandboxing disabled. Direct user shell
commands remain outside the sandbox, as they are today.

## Mount integration and conflicts

Extend the shared policy in `src/agent_ui_server/sandbox.py`, rather than
implementing separate path behavior in the Pi and Claude profiles.

Additional paths are additive; they do not alter built-in access modes. Existing
writable project, Git metadata, agent config, and scratch mounts remain intact.
Existing read-only runtime and system mounts remain read-only.

Conflict rules:

- Preserve the existing restriction against mounting home itself or an ancestor
  exposing all of home, checking both source and destination.
- Reject additional mounts overlapping built-in mount trees in either direction,
  instead of relying on Bubblewrap argument order to determine access. Report
  these conflicts during turn preparation when session-specific mounts are known.
- Reject duplicate entries within each scope. Matching destinations across scopes
  are merged with project precedence before mount-conflict validation.
- Reject nested additional paths and conflicting source aliases in the effective
  list, considering both expanded destinations and resolved sources. Ask the user
  to select one enclosing mount or disjoint mounts with the desired permissions.
- Protect sandbox infrastructure such as `/tmp`, `/dev`, `/proc`, and
  `/opt/agent-ui` from replacement or shadowing.
- Structural parent directories created to reach an allowed destination must not
  expose the corresponding host parent directory's other contents.

These rules deliberately avoid ambiguous permissions and accidental replacement
of sandbox internals, without changing the trusted-user deployment model.

## UI guidance

Provide sandbox-paths editors in server settings and project settings:

- Path text field, preserved as entered.
- “Allow writes” checkbox, unchecked by default.
- Add/remove entries and save the list atomically.
- Explain that paths are on the server and expansion uses the server environment.
- Show inherited server entries in the project editor, distinguish project
  additions/overrides, and allow resetting an override to the server default.
- Explain that server changes apply to new turns across all sandboxed sessions;
  project changes apply only to that project's sessions and worktrees.
- Explain that clearing a project list restores inheritance, not an empty sandbox
  allowlist.
- Explain that a read-only config mount can still expose credentials, and writable
  mounts allow agents to change or delete host data.

Some programs require writable configuration or separate data/cache paths; users
can add these explicitly. Exposing a path does not forward arbitrary environment
variables into the sandbox or change program-specific configuration discovery.

## Test coverage targets

- Persistence of both scopes, empty defaults, restart behavior, and whole-list
  replacement within each scope.
- Inheritance, project additions, matching-path overrides in both permission
  directions, and resetting overrides to server defaults.
- Equivalent expanded paths match; nested paths and conflicting aliases fail.
- Project changes affect its sessions/worktrees but not other projects.
- Omitted `write` is false, including overrides; true selects a writable bind.
- Both adapters receive the same effective server-plus-project list.
- File and directory mounts, including paths containing spaces.
- `~`, `~user`, `$HOME`, and `${HOME}` expansion using server-side values.
- Undefined variables follow standard Python behavior without special validation.
- Relative paths are rejected before resolution.
- Symlinked sources appear at their expected expanded destination.
- Missing paths, invalid types, conflicts, and symlink changes fail clearly.
- Real Bubblewrap checks: read-only mounts cannot be modified; writable mounts
  update the host; unrelated siblings remain hidden.
- An update during a running turn affects the next turn, not the running process.
- Setup failures never fall back to unsandboxed execution.

## Non-goals

- Per-session mount lists or overrides.
- Arbitrary destination mappings.
- Automatic discovery of config dependencies.
- Shell evaluation of path strings.
- Environment forwarding or changes to network policy.
- Sandboxing direct user shell commands.
