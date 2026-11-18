"""SQLite-backed history store for UI chat messages and workspace session pointers (NOVA-015)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path

from nova.domain.models import PersistedChatMessage, WorkspaceSessionPointer
from nova.domain.ports import HistoryStorePort

_SCHEMA_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    correlation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    step_type TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    workspace_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at ON chat_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_workspace ON chat_messages(workspace_path);

CREATE TABLE IF NOT EXISTS workspace_sessions (
    workspace_canonical_path TEXT PRIMARY KEY,
    pi_session_id TEXT,
    agy_conversation_id TEXT,
    updated_at REAL NOT NULL
);
"""


def resolve_default_history_db_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve `$XDG_DATA_HOME/nova/history.db` or `~/.local/share/nova/history.db`."""
    effective_env = env if env is not None else os.environ
    xdg_data = effective_env.get("XDG_DATA_HOME", "").strip()
    base_dir = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return base_dir / "nova" / "history.db"


class SqliteHistoryStore(HistoryStorePort):
    """Thread-safe SQLite adapter with WAL mode and 0600 file permissions."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        default_db = resolve_default_history_db_path()
        self._db_path = Path(db_path).expanduser() if db_path else default_db
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._initialized = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _ensure_initialized(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None and self._initialized:
                return self._conn

            # Enforce 0700 permissions on directory if creating
            parent_dir = self._db_path.parent
            if not parent_dir.exists():
                parent_dir.mkdir(parents=True, exist_ok=True)
                with contextlib_suppress(OSError):
                    parent_dir.chmod(0o700)

            is_new = not self._db_path.exists()
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=10.0,
            )
            conn.row_factory = sqlite3.Row

            if is_new:
                with contextlib_suppress(OSError):
                    self._db_path.chmod(0o600)

            conn.executescript(_SCHEMA_DDL)
            conn.commit()

            self._conn = conn
            self._initialized = True
            return conn

    async def append_message(self, message: PersistedChatMessage) -> None:
        """Persist a chat message record."""
        await asyncio.to_thread(self._append_message_sync, message)

    def _append_message_sync(self, message: PersistedChatMessage) -> None:
        conn = self._ensure_initialized()
        with self._lock:
            conn.execute(
                """
                INSERT INTO chat_messages (
                    message_id, correlation_id, source, step_type,
                    content, created_at, workspace_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    content = excluded.content,
                    step_type = excluded.step_type,
                    created_at = excluded.created_at
                """,
                (
                    message.message_id,
                    message.correlation_id,
                    message.source,
                    message.step_type,
                    message.content,
                    message.created_at,
                    message.workspace_path,
                ),
            )
            conn.commit()

    async def get_recent_messages(
        self,
        limit: int = 200,
        workspace_path: str | None = None,
    ) -> list[PersistedChatMessage]:
        """Fetch chronologically ordered recent messages up to `limit`."""
        return await asyncio.to_thread(self._get_recent_messages_sync, limit, workspace_path)

    def _get_recent_messages_sync(
        self,
        limit: int = 200,
        workspace_path: str | None = None,
    ) -> list[PersistedChatMessage]:
        conn = self._ensure_initialized()
        with self._lock:
            if workspace_path is not None:
                rows = conn.execute(
                    """
                    SELECT
                        message_id, correlation_id, source, step_type,
                        content, created_at, workspace_path
                    FROM chat_messages
                    WHERE workspace_path = ? OR workspace_path IS NULL
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (workspace_path, max(1, limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        message_id, correlation_id, source, step_type,
                        content, created_at, workspace_path
                    FROM chat_messages
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (max(1, limit),),
                ).fetchall()

            # Reverse rows so they return in chronological order
            ordered = list(reversed(rows))
            return [
                PersistedChatMessage(
                    message_id=row["message_id"],
                    correlation_id=row["correlation_id"],
                    source=row["source"],
                    step_type=row["step_type"],
                    content=row["content"],
                    created_at=row["created_at"],
                    workspace_path=row["workspace_path"],
                )
                for row in ordered
            ]

    async def save_session_pointer(self, pointer: WorkspaceSessionPointer) -> None:
        """Upsert active Tier 1 and Tier 2 session continuity identifiers for a workspace."""
        await asyncio.to_thread(self._save_session_pointer_sync, pointer)

    def _save_session_pointer_sync(self, pointer: WorkspaceSessionPointer) -> None:
        conn = self._ensure_initialized()
        with self._lock:
            conn.execute(
                """
                INSERT INTO workspace_sessions (
                    workspace_canonical_path, pi_session_id, agy_conversation_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_canonical_path) DO UPDATE SET
                    pi_session_id = COALESCE(
                        excluded.pi_session_id, workspace_sessions.pi_session_id
                    ),
                    agy_conversation_id = COALESCE(
                        excluded.agy_conversation_id, workspace_sessions.agy_conversation_id
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    pointer.workspace_canonical_path,
                    pointer.pi_session_id,
                    pointer.agy_conversation_id,
                    pointer.updated_at,
                ),
            )
            conn.commit()

    async def get_session_pointer(
        self,
        workspace_canonical_path: str,
    ) -> WorkspaceSessionPointer | None:
        """Retrieve stored session identifiers for a workspace, if any."""
        return await asyncio.to_thread(self._get_session_pointer_sync, workspace_canonical_path)

    def _get_session_pointer_sync(
        self,
        workspace_canonical_path: str,
    ) -> WorkspaceSessionPointer | None:
        conn = self._ensure_initialized()
        with self._lock:
            row = conn.execute(
                """
                SELECT workspace_canonical_path, pi_session_id, agy_conversation_id, updated_at
                FROM workspace_sessions
                WHERE workspace_canonical_path = ?
                """,
                (workspace_canonical_path,),
            ).fetchone()

            if row is None:
                return None

            return WorkspaceSessionPointer(
                workspace_canonical_path=row["workspace_canonical_path"],
                pi_session_id=row["pi_session_id"],
                agy_conversation_id=row["agy_conversation_id"],
                updated_at=row["updated_at"],
            )

    async def close(self) -> None:
        """Flush and release SQLite database connection."""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
                self._initialized = False


def contextlib_suppress(
    *exceptions: type[BaseException],
) -> contextlib.AbstractContextManager[None]:
    """Lightweight suppress context manager."""
    return contextlib.suppress(*exceptions)
