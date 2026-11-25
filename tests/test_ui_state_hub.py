"""Deterministic TDD tests for NOVA-004 T2: UiStateHub bounded projection and SSE queues."""

from __future__ import annotations

from pathlib import Path

import pytest

from nova.adapters.sqlite_history_store import SqliteHistoryStore
from nova.application.ui_state_hub import UiHubSettings, UiStateHub
from nova.domain.errors import DesktopUiError
from nova.domain.events import (
    CallStateChangedEvent,
    ChatStreamEvent,
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    CommandBlockedEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    TaskCreatedEvent,
    TaskStateChangedEvent,
    VoiceMilestoneEvent,
    VoiceSpeechChunkEvent,
    VoiceTranscriptionEvent,
    WhiteboardDeckCreatedEvent,
    WhiteboardSlideChangedEvent,
    WorkspaceChangedEvent,
)
from nova.domain.models import (
    AgentTaskRecord,
    CallSession,
    WhiteboardDeck,
    WhiteboardNode,
    WhiteboardSlide,
    WorkspaceContext,
)


async def test_ui_state_hub_assembles_streams_in_o1_and_caps_200_messages() -> None:
    bus = OrchestratorEventBus()
    hub = UiStateHub(
        event_bus=bus,
        settings=UiHubSettings(max_messages=5, max_sse_queue=8, max_pending_approvals=4),
    )
    hub.start()
    try:
        await bus.publish(
            ChatStreamEvent(
                correlation_id="corr-1",
                delta="Hola ",
                source="tier1_pi",
                step_type="agent_response",
            )
        )
        await bus.publish(
            ChatStreamEvent(
                correlation_id="corr-1",
                delta="mundo",
                source="tier1_pi",
                step_type="agent_response",
            )
        )
        snap = await hub.build_snapshot(csrf_token="csrf-1")
        assert len(snap.messages) == 1
        assert snap.messages[0].content == "Hola mundo"
        assert snap.messages[0].streaming is True

        await bus.publish(
            VoiceMilestoneEvent(
                correlation_id="corr-1",
                summary_text="Saludo completado en el chat.",
                priority="normal",
            )
        )
        snap2 = await hub.build_snapshot(csrf_token="csrf-1")
        assert snap2.messages[0].streaming is False

        # Push 10 more distinct turns -> deque and internal index stay strictly <= 5
        for idx in range(10):
            await hub.record_user_utterance(f"turno-{idx}", correlation_id=f"corr-u-{idx}")

        snap3 = await hub.build_snapshot(csrf_token="csrf-1")
        assert len(snap3.messages) == 5
        assert [m.content for m in snap3.messages] == [f"turno-{i}" for i in range(5, 10)]
        assert hub.indexed_message_count <= 5
    finally:
        hub.close()


async def test_ui_state_hub_manages_approvals_call_state_workspace_and_redacts_secrets() -> None:
    canary = "-".join(("DYN", "TOK", "8844"))
    now = 1000.0
    bus = OrchestratorEventBus()
    hub = UiStateHub(
        event_bus=bus,
        extra_secrets_provider=lambda: (canary,),
        available_workspaces_provider=self_workspaces,
        initial_workspace=WorkspaceContext(
            workspace_id="ws-init",
            name="init-proj",
            canonical_path="/home/dev/Proyectos/init-proj",
            selected_via="exact_path",
        ),
        initial_call_session=CallSession(
            call_id="call-1",
            state="standby",
            last_activity_monotonic=now,
        ),
        voice_enabled=True,
        clock=lambda: now,
    )
    hub.start()
    try:
        await bus.publish(
            CallStateChangedEvent(
                call_id="call-1",
                correlation_id="corr-call",
                previous_state="standby",
                state="listening",
                reason="wake_word_detected",
            )
        )
        await bus.publish(
            WorkspaceChangedEvent(
                correlation_id="corr-ws",
                workspace_name="nova-workspace",
                canonical_path="/home/dev/Proyectos/nova-workspace",
                selected_via="ui_picker",
            )
        )
        await bus.publish(
            VoiceTranscriptionEvent(
                call_id="call-1",
                correlation_id="corr-tx",
                sanitized_text=f"mi clave es {canary}",
                backend_device="cuda",
                duration_ms=45.0,
            )
        )
        await bus.publish(
            CommandBlockedEvent(
                audit_id="aud-blk",
                correlation_id="corr-blk",
                source="tier1_pi",
                sanitized_command=f"rm -rf / -- {canary}",
                rule_id="HARD_DENY_RM_ROOT",
                reason="Root deletion blocked",
            )
        )
        await bus.publish(
            RunnerFailureEvent(
                correlation_id="corr-err",
                error_code="PI_FAILURE",
                sanitized_message=f"fallo con {canary}",
                recoverable=True,
            )
        )
        await bus.publish(
            CommandApprovalRequestedEvent(
                audit_id="aud-1",
                correlation_id="corr-ap",
                source="tier2_agy",
                sanitized_command=f"curl https://example.org/{canary}",
                cwd="/home/dev/Proyectos/nova-workspace",
                risk_level="high",
                spoken_summary="Confirma descarga externa.",
                technical_explanation=f"Usa token {canary}",
                expires_in_seconds=30.0,
            )
        )

        snap = await hub.build_snapshot(csrf_token="csrf-mem")
        assert snap.call_session.state == "listening"
        assert snap.active_workspace is not None
        assert snap.active_workspace.display_name == "nova-workspace"
        assert snap.active_workspace.selected_via == "ui_picker"
        assert len(snap.available_workspaces) == 2
        assert len(snap.pending_approvals) == 1
        assert canary not in snap.pending_approvals[0].sanitized_command
        assert canary not in snap.pending_approvals[0].technical_explanation
        for msg in snap.messages:
            assert canary not in msg.content

        # Resolving approval removes it in O(1)
        await bus.publish(
            CommandApprovalResolvedEvent(
                audit_id="aud-1",
                correlation_id="corr-ap",
                approved=True,
                verdict="approved_by_human",
                resolved_by="user_ui",
            )
        )
        snap_after = await hub.build_snapshot(csrf_token="csrf-mem")
        assert snap_after.pending_approvals == []
    finally:
        hub.close()


async def self_workspaces() -> list[tuple[str, str]]:
    return [
        ("init-proj", "/home/dev/Proyectos/init-proj"),
        ("nova-workspace", "/home/dev/Proyectos/nova-workspace"),
    ]


async def test_ui_state_hub_sse_backpressure_injects_resync_and_enforces_client_cap() -> None:
    bus = OrchestratorEventBus()
    hub = UiStateHub(
        event_bus=bus,
        settings=UiHubSettings(max_sse_queue=3, max_sse_subscribers=2),
    )
    hub.start()
    try:
        sub1 = hub.subscribe_sse()
        sub2 = hub.subscribe_sse()
        with pytest.raises(DesktopUiError) as exc_info:
            hub.subscribe_sse()
        assert exc_info.value.code == "UI_TOO_MANY_STREAMS"

        # Publish 5 events into a maxsize=3 subscriber queue without draining
        for idx in range(5):
            await bus.publish(
                ChatStreamEvent(
                    correlation_id=f"corr-{idx}",
                    delta=f"chunk-{idx}",
                    source="tier1_pi",
                    step_type="agent_response",
                )
            )

        drained = []
        while not sub1.queue.empty():
            drained.append(sub1.queue.get_nowait())
        assert len(drained) <= 3
        assert any(item.kind == "resync_snapshot" for item in drained)

        hub.unsubscribe_sse(sub1)
        hub.unsubscribe_sse(sub2)
        assert hub.subscriber_count == 0
    finally:
        hub.close()


async def test_ui_state_hub_hydrates_and_persists_messages_in_sqlite(tmp_path: Path) -> None:
    import asyncio

    from nova.adapters.sqlite_history_store import SqliteHistoryStore
    from nova.domain.models import PersistedChatMessage

    db_path = tmp_path / "hub_history.db"
    store = SqliteHistoryStore(db_path)

    # Prepopulate SQLite with 2 historical messages
    await store.append_message(
        PersistedChatMessage(
            message_id="hist-1",
            correlation_id="corr-h1",
            source="user",
            step_type="user_prompt",
            content="Pregunta previa",
            created_at=100.0,
            workspace_path="/ws/test",
        )
    )
    await store.append_message(
        PersistedChatMessage(
            message_id="hist-2",
            correlation_id="corr-h1",
            source="tier1_pi",
            step_type="agent_response",
            content="Respuesta previa",
            created_at=101.0,
            workspace_path="/ws/test",
        )
    )

    bus = OrchestratorEventBus()
    hub = UiStateHub(
        event_bus=bus,
        history_store=store,
    )
    hub.start()
    try:
        # Hydrate history
        await hub.hydrate_history()

        snap = await hub.build_snapshot(csrf_token="csrf-t")
        assert len(snap.messages) == 2
        assert snap.messages[0].content == "Pregunta previa"
        assert snap.messages[1].content == "Respuesta previa"

        # Record a new user message and verify it is persisted
        await hub.record_user_utterance("Nueva pregunta en vivo", correlation_id="corr-live")
        # Yield to event loop to allow background persist task to complete
        await asyncio.sleep(0.05)

        stored = await store.get_recent_messages()
        assert len(stored) == 3
        assert stored[2].content == "Nueva pregunta en vivo"
    finally:
        hub.close()
        await store.close()


async def test_nova018_ui_state_hub_broadcasts_voice_speech_chunk_and_closes_on_final() -> None:
    canary = "-".join(("SPEECH", "SEC", "9911"))
    bus = OrchestratorEventBus()
    hub = UiStateHub(event_bus=bus, extra_secrets_provider=lambda: (canary,))
    hub.start()
    sub = hub.subscribe_sse()
    try:
        await bus.publish(
            ChatStreamEvent(
                correlation_id="corr-sp",
                delta="Explicación larga en curso.",
                source="tier1_pi",
                step_type="agent_response",
            )
        )
        _ = await sub.queue.get()  # chat_stream

        await bus.publish(
            VoiceSpeechChunkEvent(
                correlation_id="corr-sp",
                text=f"Primera oración con secreto {canary}.",
                chunk_index=0,
                is_final=False,
            )
        )
        item0 = await sub.queue.get()
        assert item0.envelope is not None
        assert item0.envelope.event_type == "voice_speech_chunk"
        assert canary not in item0.envelope.payload["text"]
        assert item0.envelope.payload["is_final"] is False

        snap_mid = await hub.build_snapshot(csrf_token="csrf")
        assert snap_mid.messages[0].streaming is True

        await bus.publish(
            VoiceSpeechChunkEvent(
                correlation_id="corr-sp",
                text="Segunda oración final.",
                chunk_index=1,
                is_final=True,
            )
        )
        item1 = await sub.queue.get()
        assert item1.envelope is not None
        assert item1.envelope.event_type == "voice_speech_chunk"
        assert item1.envelope.payload["chunk_index"] == 1
        assert item1.envelope.payload["is_final"] is True

        snap_end = await hub.build_snapshot(csrf_token="csrf")
        assert snap_end.messages[0].streaming is False
    finally:
        hub.unsubscribe_sse(sub)
        hub.close()


def _sample_deck(deck_id: str, topic: str, *, secret: str = "") -> WhiteboardDeck:
    slides = tuple(
        WhiteboardSlide(
            slide_index=idx,
            title=f"Lámina {idx} {secret}".strip(),
            visual_kind="flowchart" if idx == 0 else "mindmap",
            nodes=(WhiteboardNode(id="n1", label=f"Nodo {idx}"),),
            narration=f"Explicación de lámina {idx} {secret}".strip(),
        )
        for idx in range(2)
    )
    return WhiteboardDeck(
        deck_id=deck_id,
        correlation_id=f"corr-{deck_id}",
        topic=topic,
        slides=slides,
    )


async def test_nova019_ac8_ac9_ac10_whiteboard_ram_cache_lru_ttl_sse_and_redaction() -> None:
    canary = "-".join(("WB", "CSRF", "7742"))
    now = [1000.0]
    bus = OrchestratorEventBus()
    hub = UiStateHub(
        event_bus=bus,
        settings=UiHubSettings(
            max_whiteboard_decks=10,
            whiteboard_deck_ttl_seconds=1800.0,
            whiteboard_ttl_jitter_ratio=0.0,
        ),
        extra_secrets_provider=lambda: (canary,),
        clock=lambda: now[0],
    )
    hub.start()
    sub = hub.subscribe_sse()
    try:
        # N/N-1 empty snapshot defaults
        empty_snap = await hub.build_snapshot(csrf_token="csrf-1")
        assert empty_snap.active_whiteboard is None
        assert empty_snap.whiteboard_history == []

        # Insert 12 decks -> LRU eviction caps at 10 under canonical cache keys
        for i in range(12):
            deck = _sample_deck(f"deck-{i:02d}", f"Tema {i}", secret=canary if i == 11 else "")
            await bus.publish(
                WhiteboardDeckCreatedEvent(
                    correlation_id=f"corr-{i}",
                    deck=deck,
                    auto_open=True,
                )
            )
            env_item = await sub.queue.get()
            assert env_item.envelope is not None
            assert env_item.envelope.event_type == "whiteboard_deck_created"

        active, history = hub.get_whiteboard_state()
        assert active is not None
        assert active.deck_id == "deck-11"
        assert len(history) == 10
        assert all(h.deck_id not in ("deck-00", "deck-01") for h in history)
        assert hub.whiteboard_cache_keys[0].startswith("nova:ui:whiteboard:deck:")
        # Canary secret redacted everywhere
        assert canary not in active.model_dump_json()
        assert "<REDACTED:" in active.model_dump_json()

        # Slide sync event updates active_slide_index and fans out SSE
        await bus.publish(
            WhiteboardSlideChangedEvent(
                correlation_id="corr-sync",
                deck_id="deck-11",
                slide_index=1,
                reason="narration_sync",
            )
        )
        slide_env = await sub.queue.get()
        assert slide_env.envelope is not None
        assert slide_env.envelope.event_type == "whiteboard_slide_changed"
        assert slide_env.envelope.payload["slide_index"] == 1

        # Manual navigation validates range & is idempotent
        nav_ok = hub.navigate_whiteboard(0, deck_id="deck-11", correlation_id="corr-nav")
        assert nav_ok.active_slide_index == 0
        _ = await sub.queue.get()

        with pytest.raises(DesktopUiError) as oob_exc:
            hub.navigate_whiteboard(5, deck_id="deck-11", correlation_id="corr-oob")
        assert oob_exc.value.code == "WHITEBOARD_INVALID_SLIDE_INDEX"

        # Select an earlier surviving deck
        selected, _ = hub.select_whiteboard_deck("deck-05", correlation_id="corr-sel")
        assert selected.deck_id == "deck-05"
        _ = await sub.queue.get()

        # TTL expiration purges decks and returns WHITEBOARD_DECK_NOT_FOUND
        now[0] += 1801.0
        expired_active, expired_hist = hub.get_whiteboard_state()
        assert expired_active is None
        assert expired_hist == []
        with pytest.raises(DesktopUiError) as nf_exc:
            hub.select_whiteboard_deck("deck-05", correlation_id="corr-exp")
        assert nf_exc.value.code == "WHITEBOARD_DECK_NOT_FOUND"
    finally:
        hub.unsubscribe_sse(sub)
        hub.close()


@pytest.mark.asyncio
async def test_nova020_f6_f19_ui_state_hub_terminal_ttl_eviction_and_task_only_hydration(
    tmp_path: Path,
) -> None:
    store = SqliteHistoryStore(tmp_path / "tasks_only.db")
    await store.upsert_task(
        AgentTaskRecord(
            task_id="tsk-hyd-1",
            correlation_id="corr-hyd",
            workspace_canonical_path="/ws/demo",
            workspace_name="demo",
            provider="claude",
            model="opus",
            objective="Hydrated completed task",
            status="completed",
            result_summary="Finished.",
            created_at=100.0,
            updated_at=101.0,
            completed_at=101.0,
        )
    )

    now = [1000.0]
    bus = OrchestratorEventBus()
    # F19: history_store=None with task_store populated still hydrates tasks!
    hub = UiStateHub(
        event_bus=bus,
        history_store=None,
        task_store=store,
        settings=UiHubSettings(max_tasks=10, terminal_task_ttl_seconds=60.0),
        clock=lambda: now[0],
    )
    hub.start()
    try:
        await hub.hydrate_history()
        snap0 = await hub.build_snapshot(csrf_token="csrf")
        assert [t.id for t in snap0.tasks] == ["tsk-hyd-1"]

        # Add an active task and 12 completed tasks -> bounded to max_tasks=10 while active stays
        active_rec = AgentTaskRecord(
            task_id="tsk-active",
            correlation_id="corr-a",
            workspace_canonical_path="/ws/demo",
            workspace_name="demo",
            provider="opencode",
            model="sonnet",
            objective="Active work",
            status="running",
            created_at=1001.0,
            updated_at=1001.0,
        )
        await bus.publish(TaskCreatedEvent(task=active_rec, correlation_id="corr-a"))

        for idx in range(12):
            done_rec = AgentTaskRecord(
                task_id=f"tsk-done-{idx}",
                correlation_id=f"corr-d-{idx}",
                workspace_canonical_path="/ws/demo",
                workspace_name="demo",
                provider="claude",
                model="opus",
                objective=f"Completed {idx}",
                status="completed",
                created_at=1002.0 + idx,
                updated_at=1002.0 + idx,
                completed_at=1002.0 + idx,
            )
            await bus.publish(
                TaskStateChangedEvent(
                    task_id=done_rec.task_id,
                    correlation_id=done_rec.correlation_id,
                    previous_status="running",
                    status="completed",
                    task=done_rec,
                    reason="done",
                )
            )

        snap_cap = await hub.build_snapshot(csrf_token="csrf")
        assert len(snap_cap.tasks) == 10
        assert any(t.id == "tsk-active" for t in snap_cap.tasks)

        # F6: Advance clock past 60s * 1.05 max jitter -> terminal tasks evicted, active retained
        now[0] = 1070.0
        snap_expired = await hub.build_snapshot(csrf_token="csrf")
        assert [t.id for t in snap_expired.tasks] == ["tsk-active"]
    finally:
        hub.close()
        await store.close()

