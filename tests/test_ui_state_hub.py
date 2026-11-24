"""Deterministic TDD tests for NOVA-004 T2: UiStateHub bounded projection and SSE queues."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    VoiceMilestoneEvent,
    VoiceSpeechChunkEvent,
    VoiceTranscriptionEvent,
    WorkspaceChangedEvent,
)
from nova.domain.models import CallSession, WorkspaceContext


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

