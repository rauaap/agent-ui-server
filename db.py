from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALID_STATUSES = {"idle", "running", "awaiting_approval"}

# Per-session auto-approve toggles, stored as 0/1 INTEGER columns. Read-only
# tools never reach the permission gate on either agent, so only the mutating
# categories are switchable; the keys here are the column suffixes.
AUTO_APPROVE_CATEGORIES = ("write", "command")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    """Decode a sessions row, coercing the auto-approve flags to bool."""
    session = dict(row)
    for category in AUTO_APPROVE_CATEGORIES:
        column = f"auto_approve_{category}"
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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    working_dir TEXT NOT NULL,
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
                    session_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scrollback_session_id_id "
                "ON scrollback(session_id, id)"
            )
            # Projects are explicit rows, keyed by their working directory —
            # sessions reference that path rather than an id, so there is no
            # foreign key and a session can still be created anywhere.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    path TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

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
                """
                SELECT id, name, working_dir, agent, agent_session_id, status,
                       created_at, last_active_at,
                       auto_approve_write, auto_approve_command
                FROM sessions
                ORDER BY last_active_at DESC, created_at DESC
                """
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    # A project row plus the aggregates its card shows. The LEFT JOIN keeps
    # projects with no sessions, which report 0 / NULL. SQLite sorts NULL below
    # everything, so DESC already puts never-used projects last.
    _PROJECT_QUERY = """
        SELECT p.path, p.name,
               COUNT(s.id) AS session_count,
               MAX(s.last_active_at) AS last_active_at
        FROM projects p
        LEFT JOIN sessions s ON s.working_dir = p.path
        {where}
        GROUP BY p.path, p.name
        ORDER BY last_active_at DESC, p.path ASC
    """

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                self._PROJECT_QUERY.format(where="")
            ).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                self._PROJECT_QUERY.format(where="WHERE p.path = ?"),
                (path,),
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

    def create_session(self, name: str, working_dir: str, agent: str) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    id, name, working_dir, agent, agent_session_id, status,
                    created_at, last_active_at
                )
                VALUES (?, ?, ?, ?, NULL, 'idle', ?, ?)
                """,
                (session_id, name, working_dir, agent, now, now),
            )
        return self.require_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )
        return cursor.rowcount > 0

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, name, working_dir, agent, agent_session_id, status,
                       created_at, last_active_at,
                       auto_approve_write, auto_approve_command
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return _row_to_session(row) if row else None

    def require_session(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    def update_status(self, session_id: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET status = ? WHERE id = ?",
                (status, session_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")

    def rename_session(self, session_id: str, name: str) -> dict[str, Any]:
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
        session_id: str,
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

    def touch_session(self, session_id: str) -> None:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET last_active_at = ? WHERE id = ?",
                (utc_now(), session_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown session: {session_id}")

    def set_agent_session_id(self, session_id: str, agent_session_id: str) -> None:
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
        session_id: str,
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

    def recent_scrollback(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
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
