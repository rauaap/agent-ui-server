from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


VALID_STATUSES = {"idle", "running", "awaiting_approval"}

# Per-session auto-approve toggles, stored as 0/1 INTEGER columns. Read-only
# tools never reach the permission gate on either agent, so only the mutating
# categories are switchable; the keys here are the column suffixes.
# `inter_agent_communication` covers the session tools, reads included.
AUTO_APPROVE_CATEGORIES = ("write", "command", "inter_agent_communication")

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
           s.agent, s.model, s.reasoning_level, s.agent_session_id, s.status,
           s.created_at, s.last_active_at, s.archived_at,
           s.auto_approve_write, s.auto_approve_command,
           s.auto_approve_inter_agent_communication, s.sandbox
    FROM sessions s
    JOIN projects p ON p.id = s.project_id
    LEFT JOIN worktrees w ON w.id = s.worktree_id
    {where}
    ORDER BY s.last_active_at DESC, s.created_at DESC
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    """Decode a sessions row, coercing the 0/1 columns to bool."""
    session = dict(row)
    for column in BOOL_COLUMNS:
        session[column] = bool(session[column])
    return session


class Database:
    def __init__(self, path: str | Path = "sessions.db") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.execute("PRAGMA foreign_keys = ON")
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA busy_timeout = 5000")
            self.init()
        except BaseException:
            self._conn.close()
            raise

    def init(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
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
                    auto_approve_command INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    reasoning_level TEXT,
                    sandbox INTEGER NOT NULL DEFAULT 1,
                    auto_approve_inter_agent_communication INTEGER NOT NULL DEFAULT 0
                )
                """
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

            self._conn.execute(_SESSION_QUERY.format(where="") + " LIMIT 0")
            self._conn.execute("SELECT archived_with_project FROM sessions LIMIT 0")
            self._conn.execute(
                "SELECT id, path, name, created_at, archived_at FROM projects LIMIT 0"
            )
            self._conn.execute(
                "SELECT id, project_id, path, branch, created_at FROM worktrees LIMIT 0"
            )
            self._conn.execute(
                "SELECT id, session_id, ts, type, payload FROM scrollback LIMIT 0"
            )
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
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS shared_asset_roots (
                    asset_root TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS sandbox_network_allowlist (
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
                    PRIMARY KEY (ip, port)
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS sandbox_paths (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL CHECK (length(path) > 0),
                    writable INTEGER NOT NULL DEFAULT 0 CHECK (writable IN (0, 1))
                )
            """)
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS sandbox_paths_server_path "
                "ON sandbox_paths(path) WHERE project_id IS NULL"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS sandbox_paths_project_path "
                "ON sandbox_paths(project_id, path) WHERE project_id IS NOT NULL"
            )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_inputs (
                    message_id INTEGER PRIMARY KEY REFERENCES scrollback(id) ON DELETE CASCADE
                )
            """)
            self._conn.execute(
                "SELECT asset_root, path, project_id FROM shared_asset_roots LIMIT 0"
            )
            self._conn.execute("SELECT ip, port FROM sandbox_network_allowlist LIMIT 0")
            self._conn.execute(
                "SELECT id, project_id, path, writable FROM sandbox_paths LIMIT 0"
            )
            self._conn.execute("SELECT message_id FROM pending_inputs LIMIT 0")

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

    def get_sandbox_network_allowlist(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(
                "SELECT ip, port FROM sandbox_network_allowlist ORDER BY rowid"
            )]

    def set_sandbox_network_allowlist(self, entries: list[dict[str, Any]]) -> None:
        from .sandbox_network import validate_network_allowlist

        entries = validate_network_allowlist(entries)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sandbox_network_allowlist")
            self._conn.executemany(
                "INSERT INTO sandbox_network_allowlist (ip, port) VALUES (?, ?)",
                [(entry["ip"], entry["port"]) for entry in entries],
            )

    def get_sandbox_paths(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, writable FROM sandbox_paths WHERE project_id IS ? ORDER BY id",
                (project_id,),
            ).fetchall()
        return [{"path": row["path"], "write": bool(row["writable"])} for row in rows]

    def _insert_sandbox_paths(self, paths: list[dict[str, Any]], project_id: int | None) -> None:
        """Caller holds the lock and transaction."""
        self._conn.executemany(
            "INSERT INTO sandbox_paths (project_id, path, writable) VALUES (?, ?, ?)",
            [(project_id, entry["path"], int(entry.get("write", False))) for entry in paths],
        )

    def set_sandbox_paths(
        self, paths: list[dict[str, Any]], project_id: int | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sandbox_paths WHERE project_id IS ?", (project_id,))
            self._insert_sandbox_paths(paths, project_id)

    def sandbox_paths_snapshot(
        self, project_id: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read both scopes in one SQLite snapshot, including concurrent writers."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT project_id, path, writable FROM sandbox_paths "
                "WHERE project_id IS NULL OR project_id = ? ORDER BY id", (project_id,),
            ).fetchall()
        defaults, project = [], []
        for row in rows:
            target = defaults if row["project_id"] is None else project
            target.append({"path": row["path"], "write": bool(row["writable"])})
        return defaults, project

    def _project_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["sandbox_paths"] = self.get_sandbox_paths(row["id"])
        return result

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                self._PROJECT_QUERY.format(where="")
            ).fetchall()
        return [self._project_dict(row) for row in rows]

    def get_project(self, path: str) -> dict[str, Any] | None:
        """Look a project up by path — how the HTTP API addresses one."""
        with self._lock:
            row = self._conn.execute(
                self._PROJECT_QUERY.format(where="WHERE p.path = ?"),
                (path,),
            ).fetchone()
        return self._project_dict(row) if row else None

    def list_shared_asset_roots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(
                "SELECT * FROM shared_asset_roots ORDER BY asset_root"
            )]

    def get_shared_asset_root(self, asset_root: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shared_asset_roots WHERE asset_root = ?", (asset_root,)
            ).fetchone()
            return dict(row) if row else None

    def create_shared_asset_root(self, asset_root: str, path: str, project_id: int | None) -> dict[str, Any]:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO shared_asset_roots VALUES (?, ?, ?)",
                (asset_root, path, project_id),
            )
        return {"asset_root": asset_root, "path": path, "project_id": project_id}

    def update_shared_asset_root(self, old_name: str, asset_root: str, path: str, project_id: int | None) -> dict[str, Any]:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE shared_asset_roots SET asset_root = ?, path = ?, project_id = ? WHERE asset_root = ?",
                (asset_root, path, project_id, old_name),
            )
        return {"asset_root": asset_root, "path": path, "project_id": project_id}

    def delete_shared_asset_root(self, asset_root: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM shared_asset_roots WHERE asset_root = ?", (asset_root,)
            ).rowcount > 0

    def get_project_by_id(self, project_id: int) -> dict[str, Any] | None:
        """Look a project up by id — how sessions refer to one."""
        with self._lock:
            row = self._conn.execute(
                self._PROJECT_QUERY.format(where="WHERE p.id = ?"),
                (project_id,),
            ).fetchone()
        return self._project_dict(row) if row else None

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

    def create_project(
        self, path: str, name: str, sandbox_paths: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Register a project. Creating one that exists is a no-op, not an error.

        Returns the project either way, so a repeat create reports the real
        session aggregates rather than claiming the project is empty.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO projects (path, name, created_at) VALUES (?, ?, ?)",
                (path, name, utc_now()),
            )
            if cursor.rowcount:
                self._insert_sandbox_paths(sandbox_paths or [], cursor.lastrowid)
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
        model: str | None = None,
        reasoning_level: str | None = None,
    ) -> dict[str, Any]:
        """Create a session belonging to a project.

        With a `worktree_id` the session runs in that worktree; without one it
        runs in the project's own directory. Nothing about the cwd is stored —
        the returned session's `working_dir` is derived from whichever link is
        set, so it stays correct if either path is ever changed.
        """
        # Set creation defaults explicitly; existing sessions retain their
        # approval preferences.
        now = utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO sessions (
                    name, project_id, worktree_id, agent, model, reasoning_level,
                    agent_session_id, status, created_at, last_active_at, sandbox,
                    auto_approve_write, auto_approve_command,
                    auto_approve_inter_agent_communication
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, 'idle', ?, ?, ?, 1, 1, 1)
                """,
                (name, project_id, worktree_id, agent, model, reasoning_level,
                 now, now, int(sandbox)),
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

    def set_reasoning_level(self, session_id: int, level: str | None) -> dict[str, Any]:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET reasoning_level = ? WHERE id = ?",
                (level, session_id),
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
        inter_agent_communication: bool | None = None,
    ) -> dict[str, Any]:
        """Update whichever auto-approve toggles are provided; leave the rest.

        Returns the refreshed session. A call with nothing to update is a no-op
        that still returns the current row.
        """
        updates = {
            "write": write,
            "command": command,
            "inter_agent_communication": inter_agent_communication,
        }
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
                "UPDATE sessions SET agent_session_id = ? WHERE id = ?",
                (agent_session_id, session_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")

    def append_scrollback(
        self,
        session_id: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock, self._conn:
            return self._append_scrollback(session_id, event_type, payload)

    def _append_scrollback(
        self, session_id: int, event_type: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Caller holds the database lock and transaction."""
        ts = utc_now()
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        cursor = self._conn.execute(
            """
            INSERT INTO scrollback (session_id, ts, type, payload)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, ts, event_type, encoded),
        )
        return {
            "id": cursor.lastrowid,
            "session_id": session_id,
            "ts": ts,
            "type": event_type,
            "payload": payload,
        }

    def enqueue_input(
        self, session_id: int, text: str, source: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist acceptance and pending membership in the same transaction."""
        with self._lock, self._conn:
            row = self._append_scrollback(
                session_id, "input", {"text": text, "source": source, "delivery": "queued"},
            )
            self._conn.execute(
                "INSERT INTO pending_inputs (message_id) VALUES (?)", (row["id"],),
            )
            self._conn.execute(
                "UPDATE sessions SET last_active_at = ? WHERE id = ?",
                (row["ts"], session_id),
            )
            return row

    def pending_inputs(self, session_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.id, s.payload FROM scrollback s
                JOIN pending_inputs p ON p.message_id = s.id
                WHERE s.session_id = ? ORDER BY s.id
                """,
                (session_id,),
            ).fetchall()
            return [
                {"message_id": row["id"], **json.loads(row["payload"])}
                for row in rows
            ]

    def ship_inputs(self, session_id: int) -> dict[str, Any] | None:
        """Atomically claim the whole inbox and record its delivery boundary.

        Shipped means handed off for one harness turn, not processed or answered.
        Shipped batches are never replayed automatically after a process crash.
        """
        with self._lock, self._conn:
            messages = self.pending_inputs(session_id)
            if not messages:
                return None
            for message in messages:
                message["delivery"] = "shipped"
            row = self._append_scrollback(session_id, "inputs_shipped", {"messages": messages})
            self._conn.executemany(
                "DELETE FROM pending_inputs WHERE message_id = ?",
                [(message["message_id"],) for message in messages],
            )
            self._conn.execute(
                "UPDATE sessions SET status = 'running', last_active_at = ? WHERE id = ?",
                (row["ts"], session_id),
            )
            return row

    def scrollback_after(
        self, session_id: int, after: int | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Read persisted events in ascending ID order, excluding the cursor."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, session_id, ts, type, payload
                FROM scrollback
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, after if after is not None else 0, limit),
            ).fetchall()

        decoded = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            decoded.append(item)
        return decoded

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
            item["payload"] = json.loads(item["payload"])
            decoded.append(item)
        return decoded
