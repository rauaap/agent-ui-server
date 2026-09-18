from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


VALID_STATUSES = {"idle", "running", "awaiting_approval"}

# Per-session auto-approve toggles, stored as 0/1 INTEGER columns. Read-only
# tools never reach the permission gate on either agent, so only the mutating
# categories are switchable; the keys here are the column suffixes.
AUTO_APPROVE_CATEGORIES = ("write", "command")

# Every sessions column stored as 0/1 that the API exposes as a bool. A
# separate name from AUTO_APPROVE_CATEGORIES, which also drives
# `set_auto_approve` and so must stay a list of approval toggles.
BOOL_COLUMNS = tuple(
    f"auto_approve_{category}" for category in AUTO_APPROVE_CATEGORIES
) + ("sandbox",)

# A session row with `working_dir` computed rather than stored. An attached
# session takes the worktree's path, a detached one keeps the path it was bound
# to, and an ordinary session takes the project's path. `detached_working_dir`
# is populated only while severing a worktree link, so live links do not keep a
# duplicate copy of the worktree path.
_SESSION_QUERY = """
    SELECT s.id, s.name, s.project_id, s.worktree_id,
           COALESCE(w.path, s.detached_working_dir, p.path) AS working_dir,
           s.agent, s.agent_session_id, s.status,
           s.created_at, s.last_active_at, s.archived_at,
           s.auto_approve_write, s.auto_approve_command, s.sandbox
    FROM sessions s
    JOIN projects p ON p.id = s.project_id
    LEFT JOIN worktrees w ON w.id = s.worktree_id
    {where}
    ORDER BY s.last_active_at DESC, s.created_at DESC
"""

# Columns added after the table rebuilds, by ALTER rather than in the CREATE, so
# an older database picks them up on open. `archived_at` is the archive flag on
# both tables — NULL means live, a timestamp means filed away, and keeping the
# time rather than a bool lets the archive view sort by when things went in.
#
# `archived_with_project` is bookkeeping the API never exposes: archiving a
# project cascades to its sessions, and this records which sessions went along
# for the ride so unarchiving the project can restore exactly those and leave
# the ones archived on their own account alone.
_POST_MIGRATION_COLUMNS = {
    "projects": {"archived_at": "TEXT"},
    "sessions": {
        "archived_at": "TEXT",
        "archived_with_project": "INTEGER NOT NULL DEFAULT 0",
        "sandbox": "INTEGER NOT NULL DEFAULT 1",
        # The immutable harness cwd after an archived session is detached from
        # its worktree. NULL while the project/worktree link still supplies it.
        "detached_working_dir": "TEXT",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    """Decode a sessions row, coercing the 0/1 columns to bool."""
    session = dict(row)
    for column in BOOL_COLUMNS:
        if column in session:
            session[column] = bool(session[column])
    return session


class Database:
    def __init__(self, path: str | Path = "sessions.db") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")
        self.init()

    def init(self) -> None:
        with self._lock, self._conn:
            # Projects have their own identity: `path` is still how the HTTP
            # API addresses one and is still unique, but sessions link to the
            # id, so a project's path can change without taking its sessions
            # with it.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    archived_at TEXT
                )
                """
            )
            # A worktree is its own entity, created and removed through its own
            # endpoints, so any number of sessions can attach to one and the
            # last session leaving does not take it with them. The row's
            # existence is the record that we created the directory and are the
            # ones responsible for removing it.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worktrees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL UNIQUE,
                    branch TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            # `worktree_id` links a session to a managed worktree. NULL usually
            # means the project directory; after explicit detachment the saved
            # `detached_working_dir` supplies its harness cwd instead. RESTRICT
            # is the backstop behind delete_worktree's attached-session check.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    worktree_id INTEGER REFERENCES worktrees(id) ON DELETE RESTRICT,
                    detached_working_dir TEXT,
                    agent TEXT NOT NULL,
                    agent_session_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    archived_at TEXT,
                    archived_with_project INTEGER NOT NULL DEFAULT 0,
                    auto_approve_write INTEGER NOT NULL DEFAULT 0,
                    auto_approve_command INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Migrate databases created before the column was generalised from
            # Claude Code's session id to any agent's resume id.
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(sessions)")
            }
            if "claude_session_id" in columns and "agent_session_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE sessions "
                    "RENAME COLUMN claude_session_id TO agent_session_id"
                )
            # Add the per-session auto-approve toggles to databases created
            # before the feature existed.
            for category in AUTO_APPROVE_CATEGORIES:
                column = f"auto_approve_{category}"
                if column not in columns:
                    self._conn.execute(
                        f"ALTER TABLE sessions "
                        f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scrollback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """
            )

        # The two ALTERs above are all SQLite can do in place; the rest of the
        # move to id-linked projects needs a table rebuild, and so do the moves
        # off uuid ids and off a stored working directory.
        self._migrate_v2()
        self._migrate_v3()
        self._migrate_v4()

        with self._lock, self._conn:
            # After the rebuilds: an unmigrated database would trip over a
            # column it does not have yet, and a rebuilt table drops the
            # indexes that pointed at the table it replaced.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_project_id "
                "ON sessions(project_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_worktree_id "
                "ON sessions(worktree_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_worktrees_project_id "
                "ON worktrees(project_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scrollback_session_id_id "
                "ON scrollback(session_id, id)"
            )
            # These features postdate every rebuild above, and each rebuild
            # copies an explicit column list into a fresh table — so additions
            # have to happen here or an older database would immediately lose
            # them while being upgraded on this same open.
            for table, columns in _POST_MIGRATION_COLUMNS.items():
                self._add_missing_columns(table, columns)

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        """ALTER in any of `columns` the table does not already have.

        Caller holds the lock and the transaction. Both column names and
        declarations are module constants, never user input.
        """
        existing = {
            row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        for column, declaration in columns.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                )

    def _migrate_v2(self) -> None:
        """Rebuild `projects` and `sessions` around a real foreign key.

        Before this, a session's project link was string equality between
        `sessions.working_dir` and `projects.path` — which stops working the
        moment a session runs somewhere else, like a worktree. The 12-step
        rebuild is unavoidable: SQLite cannot add a `REFERENCES` column with a
        non-NULL default, and `projects` needs a new primary key.

        A no-op on a fresh database (`init` already creates the new shape) and
        on one that has been through this before.
        """
        with self._lock:
            session_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(sessions)")
            }
            if "project_id" in session_columns:
                return

            # Detection keys off `sessions` rather than `projects`, because
            # `init` may just have created `projects` in the new shape on a
            # database old enough to predate it entirely.
            project_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(projects)")
            }

            self._rebuild_tables(lambda: self._rebuild_v2(project_columns))

    def _rebuild_tables(self, rebuild: Callable[[], None]) -> None:
        """Run a whole-table rebuild in one transaction, foreign keys off.

        Both PRAGMAs must be set outside a transaction: `foreign_keys` is
        silently ignored inside one, and autocommit makes the BEGIN ours. The
        closing `foreign_key_check` is what turns a botched copy into a
        rollback instead of a database full of dangling rows.
        """
        previous_isolation = self._conn.isolation_level
        self._conn.isolation_level = None
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.execute("BEGIN")
            try:
                rebuild()
                violations = self._conn.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if violations:
                    raise RuntimeError(
                        f"Migration left {len(violations)} foreign key "
                        f"violations; database untouched"
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.isolation_level = previous_isolation

    def _rebuild_v2(self, project_columns: set[str]) -> None:
        """The copy half of `_migrate_v2`, inside its transaction."""
        self._conn.execute(
            """
            CREATE TABLE projects_new (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE sessions_new (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                working_dir TEXT NOT NULL,
                owns_worktree INTEGER NOT NULL DEFAULT 0,
                agent TEXT NOT NULL,
                agent_session_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                auto_approve_write INTEGER NOT NULL DEFAULT 0,
                auto_approve_command INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        insert_project = (
            "INSERT INTO projects_new (id, path, name, created_at) "
            "VALUES (?, ?, ?, ?)"
        )
        if "id" in project_columns:
            # `init` created the table in the new shape on a database that
            # never had projects; there is nothing in it, but copy anyway.
            self._conn.execute(
                "INSERT INTO projects_new (id, path, name, created_at) "
                "SELECT id, path, name, created_at FROM projects"
            )
        else:
            for row in self._conn.execute(
                "SELECT path, name, created_at FROM projects"
            ).fetchall():
                self._conn.execute(
                    insert_project,
                    (str(uuid.uuid4()), row["path"], row["name"], row["created_at"]),
                )

        # Sessions resolve to a project by *normalised* path: `create_session`
        # never normalised `working_dir` while `create_project` did, so a
        # legacy `/p/demo/` has to find the existing `/p/demo` project rather
        # than mint a second one for the same directory.
        by_path = {
            os.path.normpath(row["path"]): row["id"]
            for row in self._conn.execute("SELECT id, path FROM projects_new")
        }

        for row in self._conn.execute(
            "SELECT DISTINCT working_dir FROM sessions"
        ).fetchall():
            path = os.path.normpath(row["working_dir"])
            if path in by_path:
                continue
            # An orphan: a session created at a path no project row matched.
            # It was invisible in the UI while still holding scrollback rows,
            # so adopt it into a project rather than dropping it.
            project_id = str(uuid.uuid4())
            self._conn.execute(
                insert_project,
                (project_id, path, PurePosixPath(path).name, utc_now()),
            )
            by_path[path] = project_id

        for row in self._conn.execute("SELECT * FROM sessions").fetchall():
            session = dict(row)
            self._conn.execute(
                """
                INSERT INTO sessions_new (
                    id, name, project_id, working_dir, owns_worktree, agent,
                    agent_session_id, status, created_at, last_active_at,
                    auto_approve_write, auto_approve_command
                )
                VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["id"],
                    session["name"],
                    by_path[os.path.normpath(session["working_dir"])],
                    session["working_dir"],
                    session["agent"],
                    session["agent_session_id"],
                    session["status"],
                    session["created_at"],
                    session["last_active_at"],
                    session["auto_approve_write"],
                    session["auto_approve_command"],
                ),
            )

        self._conn.execute("DROP TABLE projects")
        self._conn.execute("DROP TABLE sessions")
        # Without legacy_alter_table the rename tries to fix up references in
        # other tables and can rewrite scrollback's foreign key; with it,
        # scrollback's existing REFERENCES sessions(id) simply resolves to the
        # table that now has that name.
        self._conn.execute("PRAGMA legacy_alter_table = ON")
        self._conn.execute("ALTER TABLE projects_new RENAME TO projects")
        self._conn.execute("ALTER TABLE sessions_new RENAME TO sessions")
        self._conn.execute("PRAGMA legacy_alter_table = OFF")

    def _migrate_v3(self) -> None:
        """Renumber projects and sessions from uuid strings to integer ids.

        The uuids were never argued for: `sessions.id` was a uuid from the
        first version of this file and `projects.id` copied it. Nothing here
        needs one — the server has no auth, so unguessable ids protect
        nothing, and `scrollback` has always shown the alternative works.
        `INTEGER PRIMARY KEY` aliases the rowid, so the `project_id` and
        `session_id` joins stop comparing 36-byte strings and the tables lose
        a redundant index apiece.

        **Ids are renumbered, not preserved.** Anything holding an old uuid —
        a bookmarked URL, a client's stored session — stops resolving once
        this runs. Acceptable for a local single-user server, and there is no
        way to renumber without it.

        A no-op on a fresh database and on one that has been through this
        before; `_migrate_v2` runs first, so by here every database has the
        id-linked shape and differs only in the type of the ids.
        """
        with self._lock:
            declared = {
                row["name"]: row["type"]
                for row in self._conn.execute("PRAGMA table_info(sessions)")
            }
            if declared.get("id", "INTEGER").upper() != "TEXT":
                return
            self._rebuild_tables(self._rebuild_v3)

    def _rebuild_v3(self) -> None:
        """The copy half of `_migrate_v3`, inside its transaction.

        Unlike v2 this has to rebuild `scrollback` as well, since its
        `session_id` holds the ids being renumbered.
        """
        self._conn.execute(
            """
            CREATE TABLE projects_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE sessions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                working_dir TEXT NOT NULL,
                owns_worktree INTEGER NOT NULL DEFAULT 0,
                agent TEXT NOT NULL,
                agent_session_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                auto_approve_write INTEGER NOT NULL DEFAULT 0,
                auto_approve_command INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE scrollback_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )

        # Copied oldest-first so the new ids run in creation order rather than
        # in whatever order the uuids happened to sort.
        project_ids: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT id, path, name, created_at FROM projects ORDER BY created_at, rowid"
        ).fetchall():
            cursor = self._conn.execute(
                "INSERT INTO projects_new (path, name, created_at) VALUES (?, ?, ?)",
                (row["path"], row["name"], row["created_at"]),
            )
            project_ids[row["id"]] = cursor.lastrowid

        session_ids: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT * FROM sessions ORDER BY created_at, rowid"
        ).fetchall():
            cursor = self._conn.execute(
                """
                INSERT INTO sessions_new (
                    name, project_id, working_dir, owns_worktree, agent,
                    agent_session_id, status, created_at, last_active_at,
                    auto_approve_write, auto_approve_command
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["name"],
                    project_ids[row["project_id"]],
                    row["working_dir"],
                    row["owns_worktree"],
                    row["agent"],
                    row["agent_session_id"],
                    row["status"],
                    row["created_at"],
                    row["last_active_at"],
                    row["auto_approve_write"],
                    row["auto_approve_command"],
                ),
            )
            session_ids[row["id"]] = cursor.lastrowid

        for row in self._conn.execute(
            "SELECT session_id, ts, type, payload FROM scrollback ORDER BY id"
        ).fetchall():
            # Scrollback whose session is already gone: the foreign key was
            # declared from the start, but `PRAGMA foreign_keys` is per
            # connection, so a row written by something that never set it can
            # still be here. Dropping it beats failing the whole migration on
            # the `foreign_key_check` at the end.
            session_id = session_ids.get(row["session_id"])
            if session_id is None:
                continue
            self._conn.execute(
                "INSERT INTO scrollback_new (session_id, ts, type, payload) "
                "VALUES (?, ?, ?, ?)",
                (session_id, row["ts"], row["type"], row["payload"]),
            )

        self._conn.execute("DROP TABLE scrollback")
        self._conn.execute("DROP TABLE sessions")
        self._conn.execute("DROP TABLE projects")
        # As in v2: without this the renames try to fix up references from
        # other tables, and scrollback_new's own REFERENCES would be rewritten
        # to point at the table it is replacing.
        self._conn.execute("PRAGMA legacy_alter_table = ON")
        self._conn.execute("ALTER TABLE projects_new RENAME TO projects")
        self._conn.execute("ALTER TABLE sessions_new RENAME TO sessions")
        self._conn.execute("ALTER TABLE scrollback_new RENAME TO scrollback")
        self._conn.execute("PRAGMA legacy_alter_table = OFF")

    def _migrate_v4(self) -> None:
        """Move the worktree out of the session and into its own table.

        `sessions.working_dir` was a free-form directory string paired with an
        `owns_worktree` flag, which is what a worktree looked like before it had
        a row of its own. Both go: the cwd is derived (`_SESSION_QUERY`), and
        "we created this directory" is now recorded by a `worktrees` row
        existing at all.

        Session ids are preserved, so unlike `_migrate_v3` this leaves
        `scrollback` alone.

        A no-op on a fresh database and on one that has been through this
        before; `_migrate_v2` and `_migrate_v3` run first, so by here every
        database has integer ids and a real `project_id`.
        """
        with self._lock:
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(sessions)")
            }
            if "worktree_id" in columns:
                return
            self._rebuild_tables(self._rebuild_v4)

    def _rebuild_v4(self) -> None:
        """The copy half of `_migrate_v4`, inside its transaction."""
        self._conn.execute(
            """
            CREATE TABLE sessions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                worktree_id INTEGER REFERENCES worktrees(id) ON DELETE RESTRICT,
                agent TEXT NOT NULL,
                agent_session_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                auto_approve_write INTEGER NOT NULL DEFAULT 0,
                auto_approve_command INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # One worktree row per owned directory, not per session: nothing can
        # produce two sessions owning one path today, but the new schema has a
        # UNIQUE on it, so a database that somehow holds a pair must converge on
        # a single row rather than fail the migration.
        worktree_ids: dict[str, int] = {}
        for row in self._conn.execute("SELECT * FROM sessions ORDER BY id").fetchall():
            worktree_id: int | None = None
            # Anything not explicitly owned lands on NULL — meaning "runs in the
            # project directory". No code path writes an unowned session to a
            # directory other than its project's, and if one somehow did, the
            # cost of landing here is a corrected cwd, whereas minting a
            # worktree row for it would mark a directory we never created as
            # ours to delete.
            if row["owns_worktree"]:
                path = row["working_dir"]
                worktree_id = worktree_ids.get(path)
                if worktree_id is None:
                    cursor = self._conn.execute(
                        "INSERT INTO worktrees (project_id, path, branch, created_at) "
                        "VALUES (?, ?, NULL, ?)",
                        (row["project_id"], path, row["created_at"]),
                    )
                    worktree_id = cursor.lastrowid
                    worktree_ids[path] = worktree_id

            self._conn.execute(
                """
                INSERT INTO sessions_new (
                    id, name, project_id, worktree_id, agent, agent_session_id,
                    status, created_at, last_active_at,
                    auto_approve_write, auto_approve_command
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["name"],
                    row["project_id"],
                    worktree_id,
                    row["agent"],
                    row["agent_session_id"],
                    row["status"],
                    row["created_at"],
                    row["last_active_at"],
                    row["auto_approve_write"],
                    row["auto_approve_command"],
                ),
            )

        self._conn.execute("DROP TABLE sessions")
        # As in v2 and v3: without this the rename tries to fix up references
        # from other tables, and scrollback's FK would be rewritten to point at
        # the table being replaced.
        self._conn.execute("PRAGMA legacy_alter_table = ON")
        self._conn.execute("ALTER TABLE sessions_new RENAME TO sessions")
        self._conn.execute("PRAGMA legacy_alter_table = OFF")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset_active_sessions(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET status = 'idle' WHERE status != 'idle'"
            )

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                _SESSION_QUERY.format(where="")
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_sessions_for_project(self, project_id: int) -> list[dict[str, Any]]:
        """Every session belonging to a project, worktrees included.

        The link is the foreign key, not the working directory, so a session
        running in a worktree somewhere else still comes back here — which is
        what `delete_project`'s teardown sweep depends on.
        """
        with self._lock:
            rows = self._conn.execute(
                _SESSION_QUERY.format(where="WHERE s.project_id = ?"),
                (project_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_sessions_for_worktree(self, worktree_id: int) -> list[dict[str, Any]]:
        """Every session attached to a worktree.

        What `DELETE /worktrees/{id}` checks before removing anything: a
        worktree with sessions in it is refused rather than pulled out from
        under them.
        """
        with self._lock:
            rows = self._conn.execute(
                _SESSION_QUERY.format(where="WHERE s.worktree_id = ?"),
                (worktree_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_live_detached_sessions_for_worktree(
        self, worktree_id: int
    ) -> list[dict[str, Any]]:
        """Live detached sessions whose preserved cwd is this worktree's path."""
        with self._lock:
            rows = self._conn.execute(
                _SESSION_QUERY.format(
                    where=(
                        "WHERE s.worktree_id IS NULL "
                        "AND s.detached_working_dir = "
                        "(SELECT path FROM worktrees WHERE id = ?) "
                        "AND s.archived_at IS NULL"
                    )
                ),
                (worktree_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_sessions_archived_with_project(
        self, project_id: int
    ) -> list[dict[str, Any]]:
        """Sessions a project unarchive would restore."""
        with self._lock:
            rows = self._conn.execute(
                _SESSION_QUERY.format(
                    where=(
                        "WHERE s.project_id = ? "
                        "AND s.archived_with_project = 1"
                    )
                ),
                (project_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def detach_session_from_worktree(self, session_id: int) -> dict[str, Any]:
        """Sever an archived session's worktree link without changing its cwd.

        The path copied here is the harness identity that must survive deletion
        of the worktree row. A completed detach is idempotent so an HTTP client
        can safely retry after losing the response.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT s.archived_at, s.worktree_id, s.detached_working_dir,
                       w.path AS worktree_path
                FROM sessions s
                LEFT JOIN worktrees w ON w.id = s.worktree_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown session: {session_id}")
            if row["archived_at"] is None:
                raise ValueError("Session must be archived before detaching")
            if row["worktree_id"] is None:
                if row["detached_working_dir"] is not None:
                    return self.require_session(session_id)
                raise ValueError("Session is not attached to a worktree")
            if row["worktree_path"] is None:
                raise RuntimeError("Attached worktree is missing")

            self._conn.execute(
                """
                UPDATE sessions
                SET detached_working_dir = ?, worktree_id = NULL
                WHERE id = ?
                """,
                (row["worktree_path"], session_id),
            )
        return self.require_session(session_id)

    # A project row plus the aggregates its card shows. The LEFT JOIN keeps
    # projects with no sessions, which report 0 / NULL. SQLite sorts NULL below
    # everything, so DESC already puts never-used projects last.
    #
    # The FILTERs keep `session_count` and `last_active_at` about the sessions
    # the main list actually shows, so a project cannot claim five sessions
    # while displaying none. `archived_session_count` is what the archive view
    # shows instead; COUNT over the FK ignores the all-NULL row a project with
    # no sessions contributes, so both counts are 0 there rather than 1.
    _PROJECT_QUERY = """
        SELECT p.id, p.path, p.name, p.archived_at,
               COUNT(s.id) FILTER (WHERE s.archived_at IS NULL)
                   AS session_count,
               COUNT(s.id) FILTER (WHERE s.archived_at IS NOT NULL)
                   AS archived_session_count,
               MAX(s.last_active_at) FILTER (WHERE s.archived_at IS NULL)
                   AS last_active_at
        FROM projects p
        LEFT JOIN sessions s ON s.project_id = p.id
        {where}
        GROUP BY p.id, p.path, p.name, p.archived_at
        ORDER BY last_active_at DESC, p.path ASC
    """

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                self._PROJECT_QUERY.format(where="")
            ).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, path: str) -> dict[str, Any] | None:
        """Look a project up by path — how the HTTP API addresses one."""
        with self._lock:
            row = self._conn.execute(
                self._PROJECT_QUERY.format(where="WHERE p.path = ?"),
                (path,),
            ).fetchone()
        return dict(row) if row else None

    def get_project_by_id(self, project_id: int) -> dict[str, Any] | None:
        """Look a project up by id — how sessions refer to one."""
        with self._lock:
            row = self._conn.execute(
                self._PROJECT_QUERY.format(where="WHERE p.id = ?"),
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_project(self, path: str) -> bool:
        """Forget a project, its worktrees and any sessions still on it.

        The caller sweeps sessions first — teardown does much more than delete a
        row — so the explicit DELETE here is normally a no-op. It is not left to
        the cascade because `sessions.worktree_id` is RESTRICT: a cascade that
        happened to reach `worktrees` while a session still pointed at one would
        abort the whole delete. Doing sessions first makes the order ours rather
        than SQLite's.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM sessions WHERE project_id = "
                "(SELECT id FROM projects WHERE path = ?)",
                (path,),
            )
            cursor = self._conn.execute(
                "DELETE FROM projects WHERE path = ?",
                (path,),
            )
        return cursor.rowcount > 0

    def create_project(self, path: str, name: str) -> dict[str, Any]:
        """Register a project. Creating one that exists is a no-op, not an error.

        Returns the project either way, so a repeat create reports the real
        session aggregates rather than claiming the project is empty.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO projects (path, name, created_at) "
                "VALUES (?, ?, ?)",
                (path, name, utc_now()),
            )
        project = self.get_project(path)
        if project is None:
            raise KeyError(f"Unknown project: {path}")
        return project

    # A worktree row plus the session count its card shows, mirroring
    # `_PROJECT_QUERY`. The LEFT JOIN keeps worktrees nothing is attached to —
    # which, now that they outlive sessions, is an ordinary state rather than
    # the leak it used to be.
    _WORKTREE_QUERY = """
        SELECT w.id, w.project_id, w.path, w.branch, w.created_at,
               COUNT(s.id) AS session_count
        FROM worktrees w
        LEFT JOIN sessions s ON s.worktree_id = w.id
        {where}
        GROUP BY w.id, w.project_id, w.path, w.branch, w.created_at
        ORDER BY w.created_at DESC, w.id DESC
    """

    def list_worktrees(self, project_id: int | None = None) -> list[dict[str, Any]]:
        """Every worktree, or every worktree of one project."""
        where = "WHERE w.project_id = ?" if project_id is not None else ""
        params = (project_id,) if project_id is not None else ()
        with self._lock:
            rows = self._conn.execute(
                self._WORKTREE_QUERY.format(where=where), params
            ).fetchall()
        return [dict(row) for row in rows]

    def get_worktree(self, worktree_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                self._WORKTREE_QUERY.format(where="WHERE w.id = ?"),
                (worktree_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_worktree_by_path(self, path: str) -> dict[str, Any] | None:
        """Look a worktree up by directory — how `POST /worktrees` spots a repeat."""
        with self._lock:
            row = self._conn.execute(
                self._WORKTREE_QUERY.format(where="WHERE w.path = ?"),
                (path,),
            ).fetchone()
        return dict(row) if row else None

    def create_worktree(
        self, project_id: int, path: str, branch: str
    ) -> dict[str, Any]:
        """Record a worktree the server just created on disk.

        `path` is UNIQUE, so this raises `sqlite3.IntegrityError` if the
        directory is already registered — the caller creates the directory
        first, and must take it back if this fails.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO worktrees (project_id, path, branch, created_at) "
                "VALUES (?, ?, ?, ?)",
                (project_id, path, branch, utc_now()),
            )
            worktree_id = cursor.lastrowid
        worktree = self.get_worktree(worktree_id)
        if worktree is None:
            raise KeyError(f"Unknown worktree: {worktree_id}")
        return worktree

    def delete_worktree(self, worktree_id: int) -> bool:
        """Forget a worktree. The caller removes the directory first."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM worktrees WHERE id = ?",
                (worktree_id,),
            )
        return cursor.rowcount > 0

    def create_session(
        self,
        name: str,
        project_id: int,
        agent: str,
        worktree_id: int | None = None,
        sandbox: bool = True,
    ) -> dict[str, Any]:
        """Create a session belonging to a project.

        With a `worktree_id` the session runs in that worktree; without one it
        runs in the project's own directory. Nothing about the cwd is stored —
        the returned session's `working_dir` is derived from whichever link is
        set, so it stays correct if either path is ever changed.
        """
        now = utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO sessions (
                    name, project_id, worktree_id, agent,
                    agent_session_id, status, created_at, last_active_at, sandbox
                )
                VALUES (?, ?, ?, ?, NULL, 'idle', ?, ?, ?)
                """,
                (name, project_id, worktree_id, agent, now, now, int(sandbox)),
            )
            session_id = cursor.lastrowid
        return self.require_session(session_id)

    def delete_session(self, session_id: int) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )
        return cursor.rowcount > 0

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                _SESSION_QUERY.format(where="WHERE s.id = ?"),
                (session_id,),
            ).fetchone()
        return _row_to_session(row) if row else None

    def require_session(self, session_id: int) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    def update_status(self, session_id: int, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET status = ? WHERE id = ?",
                (status, session_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")

    def rename_session(self, session_id: int, name: str) -> dict[str, Any]:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET name = ? WHERE id = ?",
                (name, session_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")
        return self.require_session(session_id)

    def set_sandbox(self, session_id: int, sandbox: bool) -> dict[str, Any]:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET sandbox = ? WHERE id = ?",
                (int(sandbox), session_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")
        return self.require_session(session_id)

    def set_auto_approve(
        self,
        session_id: int,
        *,
        write: bool | None = None,
        command: bool | None = None,
    ) -> dict[str, Any]:
        """Update whichever auto-approve toggles are provided; leave the rest.

        Returns the refreshed session. A call with nothing to update is a no-op
        that still returns the current row.
        """
        updates = {"write": write, "command": command}
        assignments: list[str] = []
        params: list[Any] = []
        for category, value in updates.items():
            if value is None:
                continue
            assignments.append(f"auto_approve_{category} = ?")
            params.append(1 if value else 0)

        if not assignments:
            return self.require_session(session_id)

        params.append(session_id)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")
        return self.require_session(session_id)

    def set_session_archived(
        self, session_id: int, archived: bool
    ) -> dict[str, Any]:
        """Archive or unarchive one session on its own account.

        Either way `archived_with_project` is cleared: a session archived by
        hand is not the project's to restore, and one being unarchived has
        nothing left to restore. Archiving an already-archived session keeps
        the original timestamp, so re-filing does not reorder the archive.
        """
        with self._lock, self._conn:
            if archived:
                cursor = self._conn.execute(
                    """
                    UPDATE sessions
                    SET archived_at = COALESCE(archived_at, ?),
                        archived_with_project = 0
                    WHERE id = ?
                    """,
                    (utc_now(), session_id),
                )
            else:
                cursor = self._conn.execute(
                    """
                    UPDATE sessions
                    SET archived_at = NULL, archived_with_project = 0
                    WHERE id = ?
                    """,
                    (session_id,),
                )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")
        return self.require_session(session_id)

    def archive_project(self, project_id: int) -> int:
        """Archive a project and every live session under it.

        The cascade is the point: an archived project must not leave sessions
        showing in the main list. Sessions already archived keep their own
        timestamp and are not marked as the project's, so unarchiving the
        project later does not drag them back out. Returns how many sessions
        the cascade actually touched.
        """
        now = utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE projects SET archived_at = COALESCE(archived_at, ?) "
                "WHERE id = ?",
                (now, project_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown project: {project_id}")
            cascaded = self._conn.execute(
                """
                UPDATE sessions
                SET archived_at = ?, archived_with_project = 1
                WHERE project_id = ? AND archived_at IS NULL
                """,
                (now, project_id),
            )
        return cascaded.rowcount

    def unarchive_project(
        self, project_id: int, *, restore_sessions: bool = True
    ) -> int:
        """Bring a project back, optionally restoring what its archive took.

        With `restore_sessions` the sessions the cascade archived come back
        too — otherwise unarchiving a project would resurface it looking empty,
        with its whole history apparently gone. Sessions archived on their own
        account stay archived either way.

        `restore_sessions=False` is the path for a single session being
        unarchived: that session alone should reappear, but the project has to
        come with it or it would have nowhere to show. Every session still
        loses its `archived_with_project` mark, because this project archive is
        over and a later one must not claim sessions it never archived.

        Returns how many sessions were restored.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE projects SET archived_at = NULL WHERE id = ?",
                (project_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown project: {project_id}")
            # Both branches clear the mark; only one clears `archived_at`.
            assignment = (
                "archived_at = NULL, archived_with_project = 0"
                if restore_sessions
                else "archived_with_project = 0"
            )
            marked = self._conn.execute(
                f"UPDATE sessions SET {assignment} "
                f"WHERE project_id = ? AND archived_with_project = 1",
                (project_id,),
            ).rowcount
        return marked if restore_sessions else 0

    def touch_session(self, session_id: int) -> None:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET last_active_at = ? WHERE id = ?",
                (utc_now(), session_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")

    def set_agent_session_id(self, session_id: int, agent_session_id: str) -> None:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE sessions
                SET agent_session_id = ?, last_active_at = ?
                WHERE id = ?
                """,
                (agent_session_id, utc_now(), session_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")

    def append_scrollback(
        self,
        session_id: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ts = utc_now()
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO scrollback (session_id, ts, type, payload)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, ts, event_type, encoded),
            )
            row_id = cursor.lastrowid
        return {
            "id": row_id,
            "session_id": session_id,
            "ts": ts,
            "type": event_type,
            "payload": payload,
        }

    def recent_scrollback(self, session_id: int, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, session_id, ts, type, payload
                FROM (
                    SELECT id, session_id, ts, type, payload
                    FROM scrollback
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (session_id, limit),
            ).fetchall()

        decoded = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"])
            except json.JSONDecodeError:
                item["payload"] = {"text": item["payload"]}
            decoded.append(item)
        return decoded
