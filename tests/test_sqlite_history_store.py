"""Unit tests for SqliteHistoryStore (NOVA-015 S1 & S2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nova.adapters.sqlite_history_store import SqliteHistoryStore, resolve_default_history_db_path
from nova.domain.models import AgentSessionPointer, PersistedChatMessage, WorkspaceSessionPointer


@pytest.mark.asyncio
async def test_sqlite_history_store_roundtrip_messages(tmp_path: Path) -> None:
    db_file = tmp_path / "test_history.db"
    store = SqliteHistoryStore(db_file)

    # Initial read is empty
    empty = await store.get_recent_messages()
    assert empty == []

    # Append 3 messages
    msg1 = PersistedChatMessage(
        message_id="msg-1",
        correlation_id="corr-1",
        source="user",
        step_type="user_prompt",
        content="Hola Nova",
        created_at=1000.0,
        workspace_path="/path/ws1",
    )
    msg2 = PersistedChatMessage(
        message_id="msg-2",
        correlation_id="corr-1",
        source="tier1_pi",
        step_type="agent_response",
        content="Hola! En que puedo ayudarte?",
        created_at=1001.0,
        workspace_path="/path/ws1",
    )
    msg3 = PersistedChatMessage(
        message_id="msg-3",
        correlation_id="corr-2",
        source="user",
        step_type="user_prompt",
        content="Como estas?",
        created_at=1002.0,
        workspace_path="/path/ws2",
    )

    await store.append_message(msg1)
    await store.append_message(msg2)
    await store.append_message(msg3)

    # Read all recent messages (chronological order)
    all_msgs = await store.get_recent_messages()
    assert len(all_msgs) == 3
    assert [m.message_id for m in all_msgs] == ["msg-1", "msg-2", "msg-3"]

    # Filter by workspace (should include matching workspace and global/None)
    ws1_msgs = await store.get_recent_messages(workspace_path="/path/ws1")
    assert len(ws1_msgs) == 2
    assert [m.message_id for m in ws1_msgs] == ["msg-1", "msg-2"]

    # Limit check
    limited = await store.get_recent_messages(limit=1)
    assert len(limited) == 1
    assert limited[0].message_id == "msg-3"

    await store.close()


@pytest.mark.asyncio
async def test_sqlite_history_store_upsert_message(tmp_path: Path) -> None:
    db_file = tmp_path / "test_history.db"
    store = SqliteHistoryStore(db_file)

    msg = PersistedChatMessage(
        message_id="stream-1",
        correlation_id="corr-1",
        source="tier2_runner",
        step_type="delta",
        content="Iniciando...",
        created_at=2000.0,
        workspace_path="/ws",
    )
    await store.append_message(msg)

    # Update the same message_id with final content
    updated = PersistedChatMessage(
        message_id="stream-1",
        correlation_id="corr-1",
        source="tier2_runner",
        step_type="result",
        content="Iniciando... Completado exitosamente!",
        created_at=2005.0,
        workspace_path="/ws",
    )
    await store.append_message(updated)

    res = await store.get_recent_messages()
    assert len(res) == 1
    assert res[0].step_type == "result"
    assert res[0].content == "Iniciando... Completado exitosamente!"

    await store.close()


@pytest.mark.asyncio
async def test_sqlite_history_store_session_pointers(tmp_path: Path) -> None:
    db_file = tmp_path / "test_history.db"
    store = SqliteHistoryStore(db_file)

    ws_path = "/home/user/project-a"
    none_pointer = await store.get_session_pointer(ws_path)
    assert none_pointer is None

    # Save Pi session
    p1 = WorkspaceSessionPointer(
        workspace_canonical_path=ws_path,
        pi_session_id="pi-sess-123",
        agy_conversation_id=None,
        updated_at=3000.0,
    )
    await store.save_session_pointer(p1)

    loaded1 = await store.get_session_pointer(ws_path)
    assert loaded1 is not None
    assert loaded1.pi_session_id == "pi-sess-123"
    assert loaded1.agy_conversation_id is None

    # Update with Agy conversation (COALESCE preserves existing pi_session_id)
    p2 = WorkspaceSessionPointer(
        workspace_canonical_path=ws_path,
        pi_session_id=None,
        agy_conversation_id="conv-xyz-789",
        updated_at=3010.0,
    )
    await store.save_session_pointer(p2)

    loaded2 = await store.get_session_pointer(ws_path)
    assert loaded2 is not None
    assert loaded2.pi_session_id == "pi-sess-123"
    assert loaded2.agy_conversation_id == "conv-xyz-789"
    assert loaded2.updated_at == 3010.0

    await store.close()


def test_resolve_default_history_db_path() -> None:
    env = {"XDG_DATA_HOME": "/custom/data"}
    resolved = resolve_default_history_db_path(env)
    assert resolved == Path("/custom/data/nova/history.db")


@pytest.mark.asyncio
async def test_nova017_ac9_agent_sessions_roundtrip_and_legacy_migration(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy_history.db"
    # Pre-seed legacy NOVA-015 workspace_sessions table
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE workspace_sessions (
            workspace_canonical_path TEXT PRIMARY KEY,
            pi_session_id TEXT,
            agy_conversation_id TEXT,
            updated_at REAL NOT NULL
        );
        INSERT INTO workspace_sessions (
            workspace_canonical_path, pi_session_id, agy_conversation_id, updated_at
        ) VALUES ('/home/user/legacy-ws', 'pi-legacy-1', 'agy-legacy-2', 1234.5);
        """
    )
    conn.commit()
    conn.close()

    store = SqliteHistoryStore(db_file)
    migrated_pi = await store.get_agent_session("/home/user/legacy-ws", "tier1", "pi")
    migrated_agy = await store.get_agent_session("/home/user/legacy-ws", "tier2", "agy")
    assert migrated_pi is not None
    assert migrated_pi.session_id == "pi-legacy-1"
    assert migrated_agy is not None
    assert migrated_agy.session_id == "agy-legacy-2"

    # Save multi-engine pointers for claude & opencode on same workspace
    await store.save_agent_session(
        AgentSessionPointer(
            workspace_canonical_path="/home/user/legacy-ws",
            tier="tier1",
            agent_provider="opencode",
            session_id="ses_oc_001",
            updated_at=2000.0,
        )
    )
    await store.save_agent_session(
        AgentSessionPointer(
            workspace_canonical_path="/home/user/legacy-ws",
            tier="tier2",
            agent_provider="claude",
            session_id="claude-sess-999",
            updated_at=2010.0,
        )
    )

    oc_ptr = await store.get_agent_session("/home/user/legacy-ws", "tier1", "opencode")
    claude_ptr = await store.get_agent_session("/home/user/legacy-ws", "tier2", "claude")
    assert oc_ptr is not None and oc_ptr.session_id == "ses_oc_001"
    assert claude_ptr is not None and claude_ptr.session_id == "claude-sess-999"
    # Unrelated tier/engine remains None
    assert await store.get_agent_session("/home/user/legacy-ws", "tier1", "claude") is None

    await store.close()


@pytest.mark.asyncio
async def test_nova020_ac9_tasks_table_indexes_roundtrip_and_reconciliation(tmp_path: Path) -> None:
    from nova.domain.errors import TaskNotFoundError, TaskQueueFullError, TaskStateError
    from nova.domain.events import TaskCreatedEvent, TaskProgressEvent, TaskStateChangedEvent
    from nova.domain.models import AgentTaskRecord, Tier1ActionDirective

    # Validate domain errors and directive schema
    assert TaskNotFoundError("tsk-missing").code == "TASK_NOT_FOUND"
    assert TaskQueueFullError(32).code == "TASK_QUEUE_FULL"
    assert TaskStateError("completed", "running").code == "TASK_STATE_CONFLICT"

    directive = Tier1ActionDirective(
        action="dispatch_task",
        objective="Refactor HTTP client",
        provider_hint="claude",
    )
    assert directive.action == "dispatch_task"
    assert directive.provider_hint == "claude"

    db_file = tmp_path / "tasks_history.db"
    # Pre-seed an existing chat_messages row to verify additive non-destructive migration
    store = SqliteHistoryStore(db_file)
    await store.append_message(
        PersistedChatMessage(
            message_id="msg-keep",
            correlation_id="corr-keep",
            source="user",
            step_type="user_prompt",
            content="Keep me",
            created_at=100.0,
            workspace_path="/ws/alpha",
        )
    )

    t1 = AgentTaskRecord(
        task_id="tsk-0001",
        correlation_id="corr-t1",
        workspace_canonical_path="/ws/alpha",
        workspace_name="alpha",
        provider="claude",
        model="opus",
        objective="Fix parser edge case",
        status="queued",
        created_at=1000.0,
        updated_at=1000.0,
    )
    t2 = AgentTaskRecord(
        task_id="tsk-0002",
        correlation_id="corr-t2",
        workspace_canonical_path="/ws/beta",
        workspace_name="beta",
        provider="opencode",
        model="sonnet",
        objective="Run integration suite",
        status="running",
        started_at=1005.0,
        created_at=1005.0,
        updated_at=1006.0,
    )
    t3 = AgentTaskRecord(
        task_id="tsk-0003",
        correlation_id="corr-t3",
        workspace_canonical_path="/ws/alpha",
        workspace_name="alpha",
        provider="agy",
        model="claude-opus-5-5-high",
        objective="Completed analysis",
        status="completed",
        result_summary="All checks green.",
        created_at=990.0,
        started_at=991.0,
        updated_at=995.0,
        completed_at=995.0,
    )
    t4 = AgentTaskRecord(
        task_id="tsk-0004",
        correlation_id="corr-t4",
        workspace_canonical_path="/ws/alpha",
        workspace_name="alpha",
        provider="pi",
        model="nvidia/nemotron",
        objective="Waiting command approval",
        status="waiting_approval",
        pending_audit_id="aud-123",
        created_at=1010.0,
        started_at=1011.0,
        updated_at=1012.0,
    )

    # Verify events instantiate cleanly
    ev_created = TaskCreatedEvent(correlation_id="corr-t1", task=t1)
    assert ev_created.task.task_id == "tsk-0001"
    ev_changed = TaskStateChangedEvent(
        task_id="tsk-0002",
        correlation_id="corr-t2",
        previous_status="queued",
        status="running",
        task=t2,
        reason="worker_started",
    )
    assert ev_changed.status == "running"
    ev_prog = TaskProgressEvent(
        task_id="tsk-0002",
        correlation_id="corr-t2",
        step_type="tool_call",
        last_log="Running pytest",
        provider="opencode",
        workspace_canonical_path="/ws/beta",
    )
    assert ev_prog.last_log == "Running pytest"

    for task in (t1, t2, t3, t4):
        await store.upsert_task(task)

    loaded = await store.get_task("tsk-0002")
    assert loaded is not None
    assert loaded.provider == "opencode"
    assert loaded.status == "running"
    assert await store.get_task("tsk-does-not-exist") is None

    all_tasks = await store.list_tasks(limit=10)
    assert [t.task_id for t in all_tasks] == ["tsk-0004", "tsk-0002", "tsk-0001", "tsk-0003"]

    alpha_tasks = await store.list_tasks(workspace_path="/ws/alpha", limit=2)
    assert [t.task_id for t in alpha_tasks] == ["tsk-0004", "tsk-0001"]

    running_only = await store.list_tasks(status="running", limit=10)
    assert [t.task_id for t in running_only] == ["tsk-0002"]

    # Verify indexes exist and query plan uses index
    conn = sqlite3.connect(str(db_file))
    idx_rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'"
    ).fetchall()
    idx_names = {r[0] for r in idx_rows}
    assert "idx_tasks_status_updated" in idx_names
    assert "idx_tasks_workspace_created" in idx_names
    assert "idx_tasks_created_at" in idx_names

    plan_ws = conn.execute(
        "EXPLAIN QUERY PLAN SELECT task_id FROM tasks "
        "WHERE workspace_canonical_path = ? ORDER BY created_at DESC LIMIT 10",
        ("/ws/alpha",),
    ).fetchall()
    assert any("idx_tasks_workspace_created" in str(row) for row in plan_ws)

    plan_status = conn.execute(
        "EXPLAIN QUERY PLAN SELECT task_id FROM tasks "
        "WHERE status = ? ORDER BY updated_at DESC LIMIT 10",
        ("running",),
    ).fetchall()
    assert any("idx_tasks_status_updated" in str(row) for row in plan_status)
    conn.close()

    # Reconcile interrupted tasks on startup (queued, running, waiting_approval -> failed)
    reconciled = await store.reconcile_interrupted_tasks(updated_at=2000.0)
    assert reconciled == 3
    # Idempotent on second call
    assert await store.reconcile_interrupted_tasks(updated_at=2001.0) == 0

    for interrupted_id in ("tsk-0001", "tsk-0002", "tsk-0004"):
        rec = await store.get_task(interrupted_id)
        assert rec is not None
        assert rec.status == "failed"
        assert rec.error_code == "DAEMON_RESTARTED"
        assert rec.pending_audit_id is None
        assert rec.completed_at == 2000.0

    # Completed task must remain untouched
    kept = await store.get_task("tsk-0003")
    assert kept is not None and kept.status == "completed"
    # Existing chat message must remain untouched
    msgs = await store.get_recent_messages()
    assert [m.message_id for m in msgs] == ["msg-keep"]

    await store.close()
