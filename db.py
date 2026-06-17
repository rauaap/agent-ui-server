from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALID_STATUSES = {"idle", "running", "awaiting_approval"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
                    last_active_at TEXT NOT NULL
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
                       created_at, last_active_at
                FROM sessions
                ORDER BY last_active_at DESC, created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

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
                       created_at, last_active_at
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

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
