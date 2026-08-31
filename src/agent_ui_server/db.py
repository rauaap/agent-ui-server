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

# Every sessions column stored as 0/1 that the API exposes as a bool. Kept
# apart from AUTO_APPROVE_CATEGORIES, which also drives `set_auto_approve` and
# so must stay a list of *toggles*.
BOOL_COLUMNS = tuple(
    f"auto_approve_{category}" for category in AUTO_APPROVE_CATEGORIES
) + ("owns_worktree",)

_SESSION_COLUMNS = """
    id, name, project_id, working_dir, owns_worktree, agent, agent_session_id,
    status, created_at, last_active_at,
    auto_approve_write, auto_approve_command
"""


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
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
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
        # move to id-linked projects needs a table rebuild, and so does the
        # move off uuid ids.
        self._migrate_v2()
        self._migrate_v3()

        with self._lock, self._conn:
            # After the rebuilds: an unmigrated database would trip over a
            # column it does not have yet, and a rebuilt table drops the
            # indexes that pointed at the table it replaced.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_project_id "
                "ON sessions(project_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scrollback_session_id_id "
                "ON scrollback(session_id, id)"
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
                f"""
                SELECT {_SESSION_COLUMNS}
                FROM sessions
                ORDER BY last_active_at DESC, created_at DESC
                """
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
                f"""
                SELECT {_SESSION_COLUMNS}
                FROM sessions
                WHERE project_id = ?
                ORDER BY last_active_at DESC, created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    # A project row plus the aggregates its card shows. The LEFT JOIN keeps
    # projects with no sessions, which report 0 / NULL. SQLite sorts NULL below
    # everything, so DESC already puts never-used projects last.
    _PROJECT_QUERY = """
        SELECT p.id, p.path, p.name,
               COUNT(s.id) AS session_count,
               MAX(s.last_active_at) AS last_active_at
        FROM projects p
        LEFT JOIN sessions s ON s.project_id = p.id
        {where}
        GROUP BY p.id, p.path, p.name
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
        """Forget a project. Its sessions are removed by the caller first."""
        with self._lock, self._conn:
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

    def create_session(
        self,
        name: str,
        project_id: int,
        working_dir: str,
        agent: str,
        owns_worktree: bool = False,
    ) -> dict[str, Any]:
        """Create a session belonging to a project.

        `working_dir` is the cwd the agent runs in and nothing else — usually
        the project's own path, but a worktree elsewhere when the server made
        one. `owns_worktree` records that we created that directory and are the
        ones responsible for removing it.
        """
        now = utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO sessions (
                    name, project_id, working_dir, owns_worktree, agent,
                    agent_session_id, status, created_at, last_active_at
                )
                VALUES (?, ?, ?, ?, ?, NULL, 'idle', ?, ?)
                """,
                (
                    name,
                    project_id,
                    working_dir,
                    1 if owns_worktree else 0,
                    agent,
                    now,
                    now,
                ),
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
                f"""
                SELECT {_SESSION_COLUMNS}
                FROM sessions
                WHERE id = ?
                """,
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
