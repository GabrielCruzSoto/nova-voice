"""Unit tests for SqliteHistoryStore (NOVA-015 S1 & S2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nova.adapters.sqlite_history_store import SqliteHistoryStore, resolve_default_history_db_path
from nova.domain.models import PersistedChatMessage, WorkspaceSessionPointer


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
