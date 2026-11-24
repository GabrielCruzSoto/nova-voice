"""SQLite-backed history store for UI chat messages and workspace session pointers (NOVA-015)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path

from nova.domain.models import (
    AgentEngineId,
    AgentSessionPointer,
    AgentTaskRecord,
    AgentTier,
    PersistedChatMessage,
    TaskStatus,
    WorkspaceSessionPointer,
)
from nova.domain.ports import HistoryStorePort, TaskStorePort

MAX_TASK_QUERY_LIMIT = 200

_TASK_COLUMNS = (
    "task_id, correlation_id, workspace_canonical_path, workspace_name, "
    "provider, model, tier, objective, status, result_summary, last_log, "
    "error_code, error_message, pending_audit_id, conversation_id, "
    "created_at, started_at, updated_at, completed_at"
)

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

CREATE TABLE IF NOT EXISTS workspace_agent_sessions (
    workspace_canonical_path TEXT NOT NULL,
    tier TEXT NOT NULL,
    agent_provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (workspace_canonical_path, tier, agent_provider)
);

CREATE INDEX IF NOT EXISTS idx_workspace_agent_sessions_ws
    ON workspace_agent_sessions(workspace_canonical_path);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    workspace_canonical_path TEXT NOT NULL,
    workspace_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'tier2',
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    result_summary TEXT,
    last_log TEXT,
    error_code TEXT,
    error_message TEXT,
    pending_audit_id TEXT,
    conversation_id TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    updated_at REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_updated
    ON tasks(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_tasks_workspace_created
    ON tasks(workspace_canonical_path, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tasks_created_at
    ON tasks(created_at DESC);
"""

_MIGRATE_LEGACY_SESSIONS_SQL = """
INSERT OR IGNORE INTO workspace_agent_sessions (
    workspace_canonical_path, tier, agent_provider, session_id, updated_at
)
SELECT workspace_canonical_path, 'tier1', 'pi', pi_session_id, updated_at
FROM workspace_sessions
WHERE pi_session_id IS NOT NULL AND TRIM(pi_session_id) != '';

INSERT OR IGNORE INTO workspace_agent_sessions (
    workspace_canonical_path, tier, agent_provider, session_id, updated_at
)
SELECT workspace_canonical_path, 'tier2', 'agy', agy_conversation_id, updated_at
FROM workspace_sessions
WHERE agy_conversation_id IS NOT NULL AND TRIM(agy_conversation_id) != '';
"""


def resolve_default_history_db_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve `$XDG_DATA_HOME/nova/history.db` or `~/.local/share/nova/history.db`."""
    effective_env = env if env is not None else os.environ
    xdg_data = effective_env.get("XDG_DATA_HOME", "").strip()
    base_dir = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return base_dir / "nova" / "history.db"


class SqliteHistoryStore(HistoryStorePort, TaskStorePort):
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
            conn.executescript(_MIGRATE_LEGACY_SESSIONS_SQL)
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

    async def save_agent_session(self, pointer: AgentSessionPointer) -> None:
        """Upsert an engine- and tier-specific session pointer for a workspace (NOVA-017)."""
        await asyncio.to_thread(self._save_agent_session_sync, pointer)

    def _save_agent_session_sync(self, pointer: AgentSessionPointer) -> None:
        conn = self._ensure_initialized()
        with self._lock:
            conn.execute(
                """
                INSERT INTO workspace_agent_sessions (
                    workspace_canonical_path, tier, agent_provider, session_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_canonical_path, tier, agent_provider) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (
                    pointer.workspace_canonical_path,
                    pointer.tier,
                    pointer.agent_provider,
                    pointer.session_id,
                    pointer.updated_at,
                ),
            )
            conn.commit()

    async def get_agent_session(
        self,
        workspace_canonical_path: str,
        tier: AgentTier,
        agent_provider: AgentEngineId,
    ) -> AgentSessionPointer | None:
        """Retrieve stored session pointer by (workspace, tier, agent_provider) (NOVA-017)."""
        return await asyncio.to_thread(
            self._get_agent_session_sync,
            workspace_canonical_path,
            tier,
            agent_provider,
        )

    def _get_agent_session_sync(
        self,
        workspace_canonical_path: str,
        tier: AgentTier,
        agent_provider: AgentEngineId,
    ) -> AgentSessionPointer | None:
        conn = self._ensure_initialized()
        with self._lock:
            row = conn.execute(
                """
                SELECT workspace_canonical_path, tier, agent_provider, session_id, updated_at
                FROM workspace_agent_sessions
                WHERE workspace_canonical_path = ? AND tier = ? AND agent_provider = ?
                """,
                (workspace_canonical_path, tier, agent_provider),
            ).fetchone()
            if row is None:
                return None
            return AgentSessionPointer(
                workspace_canonical_path=row["workspace_canonical_path"],
                tier=row["tier"],
                agent_provider=row["agent_provider"],
                session_id=row["session_id"],
                updated_at=row["updated_at"],
            )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> AgentTaskRecord:
        return AgentTaskRecord(
            task_id=row["task_id"],
            correlation_id=row["correlation_id"],
            workspace_canonical_path=row["workspace_canonical_path"],
            workspace_name=row["workspace_name"],
            provider=row["provider"],
            model=row["model"],
            tier=row["tier"],
            objective=row["objective"],
            status=row["status"],
            result_summary=row["result_summary"],
            last_log=row["last_log"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            pending_audit_id=row["pending_audit_id"],
            conversation_id=row["conversation_id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    async def upsert_task(self, task: AgentTaskRecord) -> None:
        """Persist or update a managed asynchronous task record (NOVA-020)."""
        await asyncio.to_thread(self._upsert_task_sync, task)

    def _upsert_task_sync(self, task: AgentTaskRecord) -> None:
        conn = self._ensure_initialized()
        with self._lock:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, correlation_id, workspace_canonical_path, workspace_name,
                    provider, model, tier, objective, status, result_summary, last_log,
                    error_code, error_message, pending_audit_id, conversation_id,
                    created_at, started_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    correlation_id = excluded.correlation_id,
                    workspace_canonical_path = excluded.workspace_canonical_path,
                    workspace_name = excluded.workspace_name,
                    provider = excluded.provider,
                    model = excluded.model,
                    tier = excluded.tier,
                    objective = excluded.objective,
                    status = excluded.status,
                    result_summary = excluded.result_summary,
                    last_log = excluded.last_log,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    pending_audit_id = excluded.pending_audit_id,
                    conversation_id = excluded.conversation_id,
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at
                """,
                (
                    task.task_id,
                    task.correlation_id,
                    task.workspace_canonical_path,
                    task.workspace_name,
                    task.provider,
                    task.model,
                    task.tier,
                    task.objective,
                    task.status,
                    task.result_summary,
                    task.last_log,
                    task.error_code,
                    task.error_message,
                    task.pending_audit_id,
                    task.conversation_id,
                    task.created_at,
                    task.started_at,
                    task.updated_at,
                    task.completed_at,
                ),
            )
            conn.commit()

    async def get_task(self, task_id: str) -> AgentTaskRecord | None:
        """Fetch a single persisted task record by task_id (NOVA-020)."""
        return await asyncio.to_thread(self._get_task_sync, task_id)

    def _get_task_sync(self, task_id: str) -> AgentTaskRecord | None:
        conn = self._ensure_initialized()
        with self._lock:
            row = conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_task(row)

    async def list_tasks(
        self,
        *,
        limit: int = 50,
        status: TaskStatus | None = None,
        workspace_path: str | None = None,
    ) -> list[AgentTaskRecord]:
        """Return bounded recent tasks ordered newest-first using explicit indexes (NOVA-020)."""
        return await asyncio.to_thread(
            self._list_tasks_sync,
            limit,
            status,
            workspace_path,
        )

    def _list_tasks_sync(
        self,
        limit: int,
        status: TaskStatus | None,
        workspace_path: str | None,
    ) -> list[AgentTaskRecord]:
        bounded_limit = min(max(1, limit), MAX_TASK_QUERY_LIMIT)
        conn = self._ensure_initialized()
        with self._lock:
            if status is not None and workspace_path is not None:
                rows = conn.execute(
                    f"""
                    SELECT {_TASK_COLUMNS}
                    FROM tasks
                    WHERE workspace_canonical_path = ? AND status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (workspace_path, status, bounded_limit),
                ).fetchall()
            elif workspace_path is not None:
                rows = conn.execute(
                    f"""
                    SELECT {_TASK_COLUMNS}
                    FROM tasks
                    WHERE workspace_canonical_path = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (workspace_path, bounded_limit),
                ).fetchall()
            elif status is not None:
                rows = conn.execute(
                    f"""
                    SELECT {_TASK_COLUMNS}
                    FROM tasks
                    WHERE status = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (status, bounded_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT {_TASK_COLUMNS}
                    FROM tasks
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
            return [self._row_to_task(row) for row in rows]

    async def reconcile_interrupted_tasks(
        self,
        *,
        updated_at: float,
        reason: str = "DAEMON_RESTARTED",
    ) -> int:
        """Mark leftover non-terminal tasks as failed on daemon startup (NOVA-020 AC-9)."""
        return await asyncio.to_thread(self._reconcile_interrupted_tasks_sync, updated_at, reason)

    def _reconcile_interrupted_tasks_sync(self, updated_at: float, reason: str) -> int:
        conn = self._ensure_initialized()
        with self._lock:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    error_code = ?,
                    error_message = ?,
                    last_log = ?,
                    pending_audit_id = NULL,
                    updated_at = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE status IN ('queued', 'running', 'waiting_approval')
                """,
                (
                    reason,
                    "Task interrupted by daemon restart.",
                    "Interrumpida por reinicio del daemon.",
                    updated_at,
                    updated_at,
                ),
            )
            conn.commit()
            return int(cursor.rowcount)

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
