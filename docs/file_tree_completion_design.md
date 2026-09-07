# Design: synchronized file trees and Bash path completion

Status: **approved for implementation.** This document is the complete handoff
for both the server implementation and the UI implementations. It does not
assume knowledge of the design discussion that produced it.

Implementation order:

1. Implement the server-side file-tree index, protocol, and watcher.
2. Implement local path matching and Bash-mode completion in each UI.
3. Evaluate the behavior before adding file references to agent context.

Agent-context attachment is explicitly out of scope. The `@` convention is
reserved for that future feature and is not used by Bash completion.

## Goals

A client needs a current, local cache of the paths beneath a session's working
directory. It can then offer interactive completion without a server round trip
for every keystroke.

The first consumer is the existing Bash input mode. For example, entering a
partial token such as `uti` and invoking completion can find both
`src/utils/clean.py` and `scripts/utils.py`. Accepting a result inserts an
ordinary shell-escaped relative path. No `@` is sent to Bash and no special
syntax is interpreted by the server.

The design must:

- include files and intermediate directories, including empty directories;
- update connected clients when paths are created, removed, or renamed;
- avoid indexing known irrelevant dependency, environment, and cache trees;
- allow working-directory-specific ignore configuration;
- avoid consuming inotify watches inside ignored directories;
- isolate file-tree traffic and failures from the existing session event stream;
- bound server memory, inotify use, patch size, and client snapshot size; and
- recover deterministically through a fresh connection and snapshot.

## Non-goals

This feature does not:

- attach files to agent context;
- parse or alter agent prompts;
- use `@` in Bash mode;
- restrict what an agent, shell command, or explicit path can access;
- treat `.gitignore` as an Agent UI ignore source;
- watch ordinary file contents;
- persist file trees in SQLite;
- retain unused indexes as a cache;
- support operating systems other than Linux; or
- attempt to represent every possible non-UTF-8 Linux filename.

The ignore mechanism is a discoverability and performance filter, **not a
sandbox or security boundary**. An ignored file remains accessible to agents,
Bash, and any future explicit context operation.

# Shared concepts

## Tree root and identity

A file tree is rooted at the `working_dir` already returned for a session. Wire
paths are relative to this root. The server registry is keyed by `os.path.normpath()` of the lexical
`working_dir` string from the session record, not by `realpath()`, inode, or
device. Two sessions whose normalized strings are equal share one index and one
watcher. Lexically different aliases do not. As elsewhere in the server, exactly
two leading slashes are collapsed to one after normalization.

Using the lexical path preserves the same working-directory identity used by
Bash and the agent harness and avoids new symlink-canonicalization behavior.

A tree exists only while at least one file-tree socket is connected or waiting
for that tree's initialization. When the final subscriber disconnects, the
server immediately stops the watcher and discards the index. There is no TTL.
A later subscriber performs a new scan.

The filesystem is authoritative. Trees are never persisted in the database.

## Path representation

Each path is represented once as a JSON string:

```text
README.md
src/
src/agent_ui_server/
src/agent_ui_server/main.py
```

Rules:

- paths are relative to the session working directory;
- paths never start with `/`;
- `/` is the path-component separator;
- paths contain no `.` or `..` components;
- directories end in `/`;
- regular files and symlinks do not end in `/`;
- the root itself is omitted;
- paths are unique;
- snapshots and each patch array are sorted lexically by the original,
  case-sensitive path string; and
- path matching is case-insensitive in the client, but storage and updates are
  case-sensitive because the server runs on Linux.

The index includes:

- regular files;
- directories;
- empty directories; and
- symlinks, including broken symlinks.

Symlinks beneath the root are never traversed. A symlink to a directory is still
serialized as a non-directory path without a trailing `/`; its target and
descendants are not part of the tree through that link. The working-directory
root itself may be a symlink to a directory and is traversed; this preserves the
working-directory behavior used by Bash and the agent harness. When the root is
a symlink, also watch the lexical symlink inode without following it; replacing
that symlink invalidates the tree as `working_directory_unavailable`.

Child filesystem mount points and duplicate directory inodes are included as
directory entries but their subtrees are pruned and unwatched. This avoids
recursive bind-mount walks and inotify watch-descriptor aliasing. The root is
traversed even when it is a mount point.

Sockets, FIFOs, block devices, character devices, and other special entries are
omitted.

Python can represent undecodable Linux byte names with surrogate characters,
but those names cannot reliably round-trip through JSON and all supported UI
string models. Skip any path component that cannot round-trip as UTF-8, and
skip its subtree when it names a directory. Log the omission at warning level;
it is not a tree-level error.

# Wire protocol

## Endpoint and lifecycle

Add a dedicated WebSocket endpoint:

```text
/ws/sessions/{session_id}/files
```

This is separate from `/ws/sessions/{session_id}`. File snapshots must never be
put in the existing transcript/status subscriber queue. The separation prevents
large snapshots and patch bursts from delaying status, approvals, agent output,
or Bash results, and it gives file synchronization an independent failure and
reconnect domain.

The socket itself is the subscription:

- opening it subscribes the client;
- closing or losing it unsubscribes the client; and
- there are no subscribe or unsubscribe application messages.

The protocol is server-to-client only. The server must nevertheless run a task
waiting on `websocket.receive()` so it promptly observes disconnects when there
are no outbound filesystem events. Coordinate the receiver and the sole writer
with the same `FIRST_COMPLETED` teardown pattern used by the existing session
socket. Only one task may call `send_json()` for a file socket.

Before accepting the socket, verify that the session exists and that its
`working_dir` is currently a directory. Reject an invalid session or missing
root with WebSocket policy close code `1008`; no application error frame is
required because a client should not request a file socket for a session whose
working directory is already known to be unavailable. Revalidate during
initialization because the root may disappear after the first check.

Limit and watcher errors discovered after acceptance must produce a
`file_tree_error` frame before closing whenever the socket is still writable.
After an application error frame, close with code `1011`. Error delivery is best
effort if the transport has already failed. A tree-level error supersedes stale
queued snapshots or patches: discard those queued frames and prioritize the
`file_tree_error` frame before closing.

File-tree frames are ephemeral. They are never written to scrollback and are
not replayed on the ordinary session socket.

## Snapshot

The first application frame on a successful connection is an authoritative
snapshot:

```json
{
  "type": "file_tree_snapshot",
  "generation": "5b56cf98-17cb-4f09-81c7-f245a6497e13",
  "revision": 0,
  "paths": [
    "README.md",
    "src/",
    "src/agent_ui_server/",
    "src/agent_ui_server/main.py"
  ]
}
```

`generation` is an opaque UUID generated by the server. `revision` is a
non-negative integer scoped to that generation. A newly built generation starts
at revision zero.

A snapshot completely replaces the client's previous file-tree state. The
server can send another snapshot later on the same socket when it rebuilds a
tree. Such a rebuild uses a new generation and revision zero.

A new subscriber to an already active tree receives that tree's current
generation, current revision, and complete current path set. Registering the
subscriber and enqueueing its snapshot must be atomic with respect to tree
patch publication: its snapshot is queued first, and every later patch is queued
after it.

Snapshots are sent as one WebSocket JSON frame, not chunked. The limits below
bound that frame.

## Patch

Structural changes are sent as patches:

```json
{
  "type": "file_tree_patch",
  "generation": "5b56cf98-17cb-4f09-81c7-f245a6497e13",
  "base_revision": 0,
  "revision": 1,
  "added": [
    "src/utils/",
    "src/utils/format.py"
  ],
  "removed": [
    "src/old_format.py"
  ]
}
```

Patch rules:

- `revision` is exactly `base_revision + 1`;
- `base_revision` equals the tree revision before applying the patch;
- additions and removals describe the net change for the batch;
- `added` and `removed` are disjoint and individually sorted;
- adding an already present path or removing an absent path is eliminated while
  computing the net change;
- a rename is represented as removal of the old paths and addition of the new
  paths;
- changing an entry between file/symlink and directory removes one wire string
  and adds the other because the directory form has a trailing `/`; and
- ordinary file-content modifications produce no patch.

The server commits the set mutation, revision increment, and patch enqueueing
under one per-tree lock. No network I/O or filesystem scan occurs while holding
that lock.

The WebSocket transport is ordered, so the server does not provide patch replay
or accept a resynchronization message. A client that sees an unknown generation
or a `base_revision` different from its current revision closes and reconnects
for a fresh snapshot.

## Error

An accepted socket reports a tree-level failure as:

```json
{
  "type": "file_tree_error",
  "code": "path_limit_exceeded",
  "message": "The working directory contains more than 200000 indexed paths."
}
```

Stable error codes are:

- `path_limit_exceeded` — per-tree path count or path-byte limit;
- `global_path_limit_exceeded` — activating or growing this tree would exceed
  the process-wide path count or path-byte budget;
- `watch_limit_exceeded` — the application watch budget was exceeded or inotify
  returned `ENOSPC`;
- `watcher_overflow` — the inotify queue emitted `IN_Q_OVERFLOW`;
- `working_directory_unavailable` — the root disappeared or became unusable
  after the socket was accepted;
- `invalid_ignore_file` — `.agent-ui-ignore` could not be decoded or compiled;
- `scan_failed` — the root scan failed for a reason that cannot be treated as a
  skipped subtree; and
- `watcher_failed` — another unrecoverable inotify failure.

An unreadable `.agent-ui-ignore` is `invalid_ignore_file`; an inaccessible root
is `working_directory_unavailable`; inotify instance/watch resource exhaustion
(including `ENOSPC`, `EMFILE`, and `ENFILE`) is `watch_limit_exceeded`; and an
unexpected non-root subtree I/O failure is `scan_failed`.

Clients must display `message` rather than manufacturing text from `code`.
Unknown future codes are handled like `watcher_failed`.

A tree-level error invalidates the index. Send the error to all of that tree's
subscribers when possible, close their sockets, close the inotify instance,
release global resource accounting, and remove the tree from the registry. The
server does not retry in the background. A later socket connection is a new
subscription and a new initialization attempt.

If one individual subscriber is slow or its transport fails, retire only that
subscriber. Do not invalidate a healthy shared tree or disconnect its other
subscribers.

# Ignore policy

## Built-in rules

Do not read or apply `.gitignore`. Version-control exclusions are not the same
as paths useful to users and agents; ignored source, generated inputs, local
configuration, and sensitive files may all be legitimate targets.

Apply the following built-in basename exclusions at any depth. A matching
directory is pruned with its whole subtree, and a matching non-directory entry
is omitted:

```text
.git
.hg
.svn
node_modules
.venv
venv
bower_components
__pycache__
.pytest_cache
.mypy_cache
.ruff_cache
.hypothesis
.tox
.nox
.gradle
.terraform
.direnv
.turbo
.parcel-cache
.next
.nuxt
.svelte-kit
```

Also omit these junk-file patterns:

```text
*.pyc
*.pyo
*.swp
*.swo
*~
.DS_Store
```

Do **not** add generic build-output exclusions such as `build`, `dist`, `out`,
`target`, or `coverage`. Do not blanket-ignore hidden files. In particular,
files such as `.env`, `.github/...`, `.vscode/...`, `.idea/...`, fixtures,
vendored sources, and generated sources remain discoverable unless the user
configures otherwise.

Own this rule list in Agent UI code. Do not inherit `watchfiles.DefaultFilter`
or another library's evolving defaults.

## `.agent-ui-ignore`

If present, read exactly one ignore file at the working-directory root:

```text
<working_dir>/.agent-ui-ignore
```

Do not search parents and do not load nested files. The file uses `.gitignore`
syntax, including:

- blank lines and `#` comments;
- escaped leading `#` and `!`;
- `*`, `?`, character classes, and `**`;
- root anchoring with a leading `/`;
- directory patterns with a trailing `/`; and
- negation with a leading `!`.

Use the `pathspec` package's Git-ignore implementation rather than creating a
partial wildcard language. Add `pathspec` as a direct project dependency.

Conceptually, built-in rules are the first lines in one rule set and the
contents of `.agent-ui-ignore` follow them. User rules therefore override
built-ins using ordinary Git-ignore negation:

```gitignore
# Restore an otherwise excluded environment.
!.venv/

# Exclude project-specific generated data.
scratch/
artifacts/**/*.json
```

Standard Git-ignore parent-directory behavior applies: a descendant cannot be
re-included while its parent directory remains excluded. Restoring selected
content under a default-excluded directory requires restoring/traversing the
necessary parent first. This property permits the scanner to prune an excluded
directory.

`.agent-ui-ignore` itself is always included in the index and monitored even if
a broad custom rule would otherwise match it.

Read the file as UTF-8. An undecodable file or pattern compilation failure is
`invalid_ignore_file`; do not silently run with a different policy than the
user requested. The ignore file must be a regular file rather than a symlink;
a symlink is also `invalid_ignore_file`, because watching only included
directories cannot observe content changes to an arbitrary symlink target.

Creation, deletion, replacement, or content modification of
`.agent-ui-ignore` rebuilds the complete tree. The rebuilt tree receives a new
generation, and every subscriber receives a replacement snapshot. If rebuilding
fails, invalidate the tree and report an error instead of retaining an index
built under stale rules.

# Resource limits and batching

Use separately configurable module constants with these initial values:

```text
MAX_FILE_TREE_WATCHES_GLOBAL       = 20,000 directories
MAX_FILE_TREE_PATHS_PER_TREE       = 200,000 entries
MAX_FILE_TREE_PATHS_GLOBAL         = 500,000 entries
MAX_FILE_TREE_PATH_BYTES_PER_TREE  = 16 MiB
MAX_FILE_TREE_PATH_BYTES_GLOBAL    = 64 MiB
MAX_FILE_TREE_PATCH_PATHS          = 5,000 added + removed
MAX_FILE_TREE_PATCH_PATH_BYTES     = 512 KiB
FILE_TREE_PATCH_DEBOUNCE           = 100 ms
FILE_TREE_PATCH_MAX_LATENCY        = 500 ms
FILE_TREE_SUBSCRIBER_QUEUE_FRAMES  = 32
```

Path-byte accounting is the sum of each wire path's UTF-8 encoded length. It is
used in addition to entry count so a tree containing unusually long names is
still bounded. Global counts include each active shared tree once, regardless
of how many sessions or sockets subscribe to it.

The limits are application budgets, not assumptions about the host. Modern
Linux dynamically sizes `fs.inotify.max_user_watches`, it is shared by all
processes under a user, and another process may exhaust it first. Always treat
`ENOSPC` from inotify as `watch_limit_exceeded` even below the application
budget.

Reserve and release global path, byte, and watch accounting for committed trees
under one registry lock. Concurrent commits or growth events must not both pass
a stale global-limit check. Candidate scans do not make provisional global
reservations; their resources are validated when committed. Do not hold the
registry lock during scanning, JSON transmission, or any other blocking/awaiting
operation. A completed candidate tree is committed against the current global
totals; if it no longer fits, reject the new tree rather than evicting an
existing one.

While scanning, stop as soon as a per-tree count or byte limit is exceeded; do
not finish constructing an already invalid tree. Initial limit failure reports
an error to the connecting socket and refuses the index. If a live tree's growth
crosses a per-tree or global limit, invalidate the growing tree rather than
invalidating unrelated indexes.

Coalesce structural inotify events for 100 ms, but publish within 500 ms under a
continuous event stream. Compute one net patch from the batch.

If either patch threshold is exceeded, do not send that patch. Rebuild the tree
from the filesystem, assign a new generation, validate all limits, and send a
replacement snapshot. Rebuilding is also the conservative response when an
event batch cannot be reconciled unambiguously. An inotify overflow is different:
it invalidates the tree and waits for a new subscription rather than rebuilding
automatically.

Use the existing 30-second WebSocket send timeout and 5-second close timeout for
file sockets. The file socket has its own 32-frame outbound queue. A full queue
retires and closes only that subscriber. Reconnecting provides a current
snapshot.

# Server implementation handoff

## Dependencies

Add direct dependencies for:

- `inotify_simple`, for thin access to Linux inotify; and
- `pathspec`, for `.gitignore`-compatible matching.

Do not use `watchfiles` for recursive watching. Its filter is applied after its
recursive native watcher is created, so ignored directories still consume
inotify watches. This design requires ignored subtrees not to be watched.

## Suggested types

The exact module split is up to the implementation, but keep file-tree logic out
of the already large request-handling paths where practical. A structure like
this is expected:

```python
@dataclass
class FileTree:
    root: str
    generation: str
    revision: int
    entries: dict[str, EntryKind]
    path_bytes: int
    inotify: INotify
    wd_to_directory: dict[int, str]
    directory_to_wd: dict[str, int]
    subscribers: set[FileTreeSubscriber]
    lock: asyncio.Lock
    event_task: asyncio.Task[None] | None

@dataclass(eq=False)
class FileTreeSubscriber:
    session_id: int
    websocket: WebSocket
    outbound: asyncio.Queue[dict[str, Any]]
    writer: asyncio.Task[None] | None
    retired: bool = False
```

Internally retain entry kinds even though the compact wire representation uses
a trailing slash only for real directories. This is necessary to distinguish a
regular file, directory, and symlink while processing changes.

Maintain:

```python
file_trees: dict[str, FileTree]
file_tree_initializations: dict[str, asyncio.Task[FileTree]]
file_tree_registry_lock: asyncio.Lock
```

The registry also owns global resource counters.

## Concurrent initialization

Only one scan and watcher may initialize a lexical root at a time:

1. Under the registry lock, return the active tree if present.
2. Otherwise find or create one initialization task for that root.
3. Release the lock before awaiting the task.
4. Await it through `asyncio.shield()` so one disconnect does not cancel work
   needed by another waiter.
5. Under the registry lock, atomically validate/reserve global resources and
   publish the completed tree if it is still needed.

Track pending subscribers as users of the initialization. If all pending sockets
disconnect, allow an already-running worker-thread scan to finish safely, then
close and discard its result instead of installing an unused tree. Python cannot
reliably cancel a filesystem walk already executing in a worker thread.

Initialization failure is delivered to every pending subscriber for that root.
Always remove the shared initialization task from the registry in a `finally`
path so a later connection can retry.

## Scanner and inotify setup

All potentially large directory enumeration runs via `asyncio.to_thread()`.
Do not walk a repository synchronously on the FastAPI event loop or while
holding the registry, session-stream, or tree lock.

Use one nonblocking `INotify` instance per active tree. Separate instances
isolate queue overflow: losing events for one root does not invalidate every
other active root. The expected active tree count is small; still handle the
host's `max_user_instances` exhaustion as `watch_limit_exceeded`.

During the initial worker-thread scan:

1. Create the inotify instance.
2. Add a watch to the root before loading the ignore file or enumerating the
   directory, so a concurrent ignore-file replacement is retained in the kernel
   queue.
3. Load and compile the built-in and `.agent-ui-ignore` rules.
4. For each included directory, add its watch before enumerating its children.
5. Add the directory to the index only if enumeration is permitted. If opening
   it raises `PermissionError`, remove its watch and omit the directory and its
   subtree.
6. Use `os.scandir()` and `lstat`/non-following type checks. Never follow a
   symlink.
7. Enforce per-tree resource limits incrementally.
8. Return the complete entry set and watch-descriptor mappings to the event
   loop.

Events accumulate in the kernel queue while scanning. Queue the subscriber's
snapshot before beginning normal event draining. Later reconcile accumulated
events against the built set. This order is safe:

- an event already reflected by the scan becomes a no-op;
- an event after the relevant directory was scanned becomes a patch after the
  snapshot; and
- overflow is explicitly reported by `IN_Q_OVERFLOW` and invalidates the tree.

## Inotify integration

Register the nonblocking inotify descriptor with the running asyncio loop using
`loop.add_reader()`. The reader callback must only drain available events into
an in-memory batch and schedule processing; it must not scan directories or
send network frames.

Watch included directories, not individual files. Use masks covering:

```text
IN_CREATE
IN_DELETE
IN_MOVED_FROM
IN_MOVED_TO
IN_ATTRIB
IN_CLOSE_WRITE
IN_DELETE_SELF
IN_MOVE_SELF
```

Also process `IN_IGNORED`, `IN_ISDIR`, and `IN_Q_OVERFLOW` flags returned with
events. `IN_MODIFY` is unnecessary. `IN_CLOSE_WRITE` is ignored except when the
child is the root `.agent-ui-ignore` file.

Directory watches report changes to immediate children. Maintain both watch
maps so an event's watch descriptor and name can be converted to a root-relative
path.

Event behavior:

- **regular file/symlink create or move in:** lstat and add the path if included;
- **regular file/symlink delete or move out:** remove the path if present;
- **directory create or move in:** if included, run a worker-thread subtree scan
  that adds a watch before enumerating each directory, then add the resulting
  paths;
- **directory delete or move out:** remove the directory, all paths beneath its
  prefix, and every corresponding watch;
- **rename within the tree:** processing move-out removals before move-in
  additions is sufficient for the first implementation; remove old watches and
  scan/install at the new location. The wire result is remove plus add;
- **attribute change:** reconcile accessibility and type. A newly accessible
  directory can be scanned; a directory that became inaccessible is removed
  and unwatched. Ordinary file metadata changes have no wire effect;
- **expected `IN_IGNORED` after removing a watch:** clean stale map state without
  treating it as failure;
- **root `IN_DELETE_SELF` or `IN_MOVE_SELF`:** invalidate with
  `working_directory_unavailable`; and
- **`IN_Q_OVERFLOW`:** invalidate with `watcher_overflow`.

When an event requires a subtree scan, pause processing/draining for that tree
while the scan runs. The kernel continues queueing events. Install the scan's
watch-descriptor mappings before reading events that may refer to those new
descriptors.

Treat inotify events as hints and reconcile against actual current filesystem
state. Creation followed rapidly by deletion, atomic file replacement, duplicate
events, and failed `lstat` calls should naturally collapse to the correct net
set rather than becoming errors.

## Tree rebuild

A rebuild is required for an ignore-file change, an oversized candidate patch,
or another explicitly recognized ambiguous batch.

Mark the tree as rebuilding so no ordinary patch is published from the old
state. Close the old inotify descriptor and release its watches before building
the replacement; this avoids temporarily doubling the process watch budget.
Build a complete new watched tree with the same scan procedure. Events occurring
during the rebuild are captured as watches are installed and reconcile after
the replacement snapshot.

On success, atomically swap resource accounting and tree state, use a new UUID
and revision zero, and enqueue a replacement snapshot to all subscribers. On
failure, invalidate the tree and send an error.

## Publication and teardown

Give each tree a lock analogous to the session stream lock. It protects:

- tree entry and revision mutation;
- snapshot-to-live subscriber registration;
- enqueueing file frames;
- generation replacement; and
- subscriber retirement from that tree.

It does not protect filesystem scanning or network I/O. Establish a consistent
lock order if registry accounting and tree state must both change; never acquire
them in opposite order. Prefer preparing data outside locks and making the
registry/tree commit a short non-yielding operation.

Use one writer task per socket. Queue frames with `put_nowait()`. A full queue
retires that subscriber, and teardown/cancellation/network close happen outside
the tree lock. A writer timeout or receive-side disconnect similarly removes
only that subscriber.

When the last established and pending subscriber is gone:

1. remove the tree from the registry and release its global counters;
2. unregister its inotify fd from the event loop;
3. cancel/await its event task;
4. close the inotify instance; and
5. discard all entries and maps.

Make teardown idempotent because the reader, writer, session deletion, and tree
failure can race.

Deleting a session must close that session's file-tree subscribers. If other
sessions subscribe to the same lexical working directory, their sockets and the
shared index remain active. Application shutdown closes every file subscriber
and inotify instance.

## Server tests

Constants must be injectable or patchable so tests can exercise limits with
small trees. Add focused unit and async tests for at least:

1. snapshot path formatting, sorting, empty directories, and omission of root;
2. regular files, directories, symlinks, broken symlinks, and special files;
3. symlinked directories are listed but never traversed;
4. built-in exclusions at root and nested depths;
5. `.gitignore` is not consulted;
6. `.agent-ui-ignore` anchoring, wildcards, comments, escaping, directory rules,
   and negation of a built-in;
7. `.agent-ui-ignore` itself remains visible;
8. changing, atomically replacing, creating, and deleting the ignore file emits
   a new-generation snapshot;
9. create/delete and file/directory type replacement patches;
10. file and directory renames, including complete descendant removal/addition;
11. ordinary content modification emits nothing;
12. a file created while initial scanning is blocked appears either in the
    snapshot or exactly once in a later patch;
13. `IN_Q_OVERFLOW`, root deletion, and watcher failure error and invalidate;
14. permission-denied subtrees are skipped without rejecting the root;
15. undecodable names are skipped;
16. per-tree and global count, byte, and watch limits;
17. a growing tree that exceeds a global limit does not evict another tree;
18. oversized patches rebuild and emit a replacement snapshot;
19. concurrent first subscribers perform one scan and receive ordered snapshots;
20. sessions sharing a working directory share one index and watcher;
21. the last disconnect immediately destroys the index;
22. one slow file subscriber does not affect another file subscriber or the
    ordinary session WebSocket;
23. queue overflow and writer failure retire only the affected subscriber;
24. session deletion closes only its file subscribers; and
25. application shutdown releases descriptors and tasks.

Use real temporary directories and inotify for Linux integration tests where
practical. Use seams/fakes for deterministic overflow, permission, blocked scan,
queue, and send-order tests.

# UI implementation handoff

Each UI implements the same protocol and matching behavior using its native
state-management and WebSocket facilities. The server does not perform
completion queries.

## Socket and cache lifecycle

When file completion is wanted for a session with an available working
directory, open:

```text
/ws/sessions/{session_id}/files
```

The natural first integration point is entering the existing Bash input mode.
A UI may keep the socket open until leaving that mode or until leaving the
session screen, but it must not open file sockets globally for sessions the user
is not interacting with. Closing the socket releases the server index when no
other client needs it.

Maintain these cache fields:

```text
generation: string
revision: integer
paths: set or map of wire path strings
sorted/search representation: client-specific derived state
state: connecting | ready | unavailable
```

On `file_tree_snapshot`:

1. validate every required field;
2. replace all previous paths;
3. set the generation and revision from the frame;
4. rebuild any lowercase/search index locally; and
5. mark completion ready.

On `file_tree_patch`:

1. require the same generation;
2. require `base_revision == current revision`;
3. remove every `removed` path;
4. add every `added` path;
5. set the current revision to `revision`; and
6. incrementally update or rebuild derived search state.

If generation/revision validation fails, do not guess or apply part of the
patch. Close the socket, clear or mark the cache unavailable, and reconnect to
obtain a snapshot.

On `file_tree_error`, show the supplied human-readable `message` in the Bash
completion UI, mark completion unavailable, and let the socket close. In
particular, do not enter an immediate reconnect loop for deterministic limit or
ignore-file errors. A later explicit entry into completion, manual retry, or
ordinary backoff-based reconnect may create a new subscription.

On a transport disconnect, stop applying completions from the cache because it
is no longer being kept current. Clear it or mark it stale until a new snapshot
arrives. Existing command text is unaffected.

Clients send no frames on this socket.

## Matching semantics

Matching is entirely local and case-insensitive. Let `query` be the decoded
current Bash token and `path` one cached wire path. A path matches when the query
is a prefix beginning at any path-component boundary:

```text
lower(path).startsWith(lower(query))
    OR
lower(path).contains("/" + lower(query))
```

Equivalently, the query must match at the beginning of the path or immediately
after `/`, and the candidate may contain any suffix after the query.

Examples:

```text
query: util

match: src/utils/clean.py
match: scripts/utils.py
match: util.py
no:    src/futile.py
no:    src/futile/experiment.py
```

```text
query: scripts/display

match: scripts/display/
match: scripts/display/icon.svg
match: scripts/display_setup.py
match: tools/scripts/display.py
no:    my_scripts/display.py
no:    scripts/my_display.py
```

Because every directory and descendant is separately present, a query matching
a directory component naturally matches both that directory and all descendant
paths. Multiple query components are literal: the `/` in the query must align
with a `/` in the candidate.

Use locale-independent lowercase/case-folding available on the platform. Do not
lowercase the inserted path; preserve the server's original spelling.

An empty query may match all paths, but the UI should render only a bounded
number of results at once (50 is the recommended initial display limit). This
is a display limit, not a cache limit.

Recommended deterministic ranking is:

1. exact path match, ignoring a directory's trailing slash;
2. match at the beginning of the full path;
3. earlier matching component boundary;
4. shorter full path;
5. locale-independent case-insensitive lexical order; and
6. original path as the final tie-breaker.

The inclusion rule is normative; ranking can be tuned after observing real use.

## Bash-mode triggering and insertion

Bash is already a distinct client input mode. Do not require or insert `@`.
Pressing Tab while Bash mode is active invokes path completion for the shell
token at the cursor. Touch-only clients must expose an equivalent completion
affordance because their software keyboard may not provide Tab.

At minimum, recognize the current token using shell whitespace and unquoted
shell operators (`|`, `&`, `;`, `(`, `)`, `<`, `>`) as boundaries. Handle
incomplete single quotes, double quotes, and backslash escapes sufficiently to
turn the token prefix into a path query. Platform shell-word utilities may be
used if they support incomplete input. Completion must never execute or send a
partial command merely to resolve quoting.

When the user accepts a result:

1. replace the complete current token with the result's full relative wire path;
2. remove the wire trailing slash only for classification purposes, not from
   inserted directories;
3. POSIX-shell-escape the inserted path as one argument; and
4. preserve the cursor after the inserted path.

Paths containing only shell-safe characters can be inserted unchanged. Paths
containing whitespace, quotes, operators, glob characters, substitutions, or
other shell syntax must be quoted/escaped. A standard safe single-quote strategy
is acceptable, with an embedded `'` represented by closing the quote, inserting
an escaped quote, and reopening it. Never interpolate an unescaped server path
into the command.

For a directory, preserve the trailing `/` so the user can continue refining a
path beneath it. If the UI immediately reopens completion after directory
selection, that is a presentation choice.

The existing Bash submission contract remains unchanged: the client sends an
ordinary command through the existing `{"type":"bash","command":"..."}` frame
or REST endpoint. There is no `@` for the client or server to strip.

## UI performance

Keep the authoritative received path strings and a derived lowercase array/map
in memory. Begin with a linear local scan; the server limits cap the input, and
prematurely introducing a graph or trie complicates patching. Debounce rendering
if necessary, but do not round-trip completion queries to the server.

If profiling later shows linear scanning is inadequate, build a client-local
index from the same flat protocol. That optimization must not change wire
format or matching semantics.

Do not render every match. Rank locally and retain/render only the leading
candidate window. Ensure filtering occurs off the main/UI thread if a platform's
largest permitted snapshot causes visible input latency.

## UI tests

Each UI should cover:

1. authoritative snapshot replacement;
2. ordered patch application;
3. generation and base-revision mismatch reconnect behavior;
4. error and disconnect disable stale completion;
5. case-insensitive component-boundary matching;
6. rejection of mid-component matches such as `futile` for `util`;
7. multi-component queries;
8. directory and descendant matching;
9. deterministic ranking and result display limit;
10. Bash mode invokes completion without `@`;
11. accepting a file inserts its complete relative path;
12. accepting a directory preserves `/`;
13. spaces, single quotes, glob characters, and shell operators are safely
    escaped;
14. completion around incomplete shell quoting does not corrupt surrounding
    command text;
15. touch completion affordance where relevant; and
16. no completion query is sent over the network.

# Future agent-context work

A later feature may reuse the same file-tree endpoint, client cache, and matcher
for `@path` references in ordinary agent input. In that mode `@` is expected to
mean a structured context reference and would remain semantically distinct from
Bash insertion. That work requires a Pi harness extension and a context wire
contract and must be designed separately. Nothing in this implementation should
strip `@` globally or assign it a Bash meaning.
