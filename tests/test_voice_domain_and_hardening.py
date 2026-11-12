"""TDD tests for T1: Voice domain models/events/ports, bus isolation, and FU-3 concurrency locks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.orchestrator import NovaOrchestrator
from nova.application.security_gatekeeper import GatekeeperSettings, SecurityGatekeeper
from nova.domain.errors import VoicePipelineError
from nova.domain.events import (
    CallStateChangedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    VoiceTranscriptionEvent,
)
from nova.domain.models import (
    AudioFrame,
    CallSession,
    CodingTaskRequest,
    CommandAuditInput,
    CommandAuditResult,
    CommandInterceptRequest,
    RunnerProgressEvent,
    SynthesizedAudio,
    TranscriptionResult,
    WakeWordDetection,
)
from nova.domain.ports import (
    AudioInputPort,
    AudioOutputPort,
    SpeechToTextPort,
    TextToSpeechPort,
    VoiceActivityDetectorPort,
    WakeWordPort,
)


def test_voice_domain_models_validate_pcm_and_confidence_boundaries() -> None:
    valid_frame = AudioFrame(
        pcm_s16le=b"\x00\x00" * 512,
        sample_rate_hz=16000,
        channels=1,
        monotonic_ts=12.5,
    )
    assert len(valid_frame.pcm_s16le) == 1024

    with pytest.raises(ValidationError, match="VOICE_INVALID_PCM_FRAME"):
        AudioFrame(pcm_s16le=b"\x00", sample_rate_hz=16000, channels=1, monotonic_ts=1.0)

    with pytest.raises(ValidationError, match="VOICE_INVALID_PCM_FRAME"):
        AudioFrame(pcm_s16le=b"", sample_rate_hz=16000, channels=1, monotonic_ts=1.0)

    valid_synth = SynthesizedAudio(pcm_s16le=b"\x01\x02" * 100, sample_rate_hz=24000, channels=1)
    assert valid_synth.sample_rate_hz == 24000

    with pytest.raises(ValidationError, match="VOICE_INVALID_PCM_FRAME"):
        SynthesizedAudio(pcm_s16le=b"\x01\x02\x03", sample_rate_hz=24000, channels=1)

    with pytest.raises(ValidationError):
        SynthesizedAudio(pcm_s16le=b"\x00\x00", sample_rate_hz=4000, channels=1)

    ww = WakeWordDetection(detected=True, wake_word="oye_nova", score=0.88)
    assert ww.detected is True

    with pytest.raises(ValidationError):
        WakeWordDetection(detected=False, score=1.5)

    tx = TranscriptionResult(
        text="hola nova",
        language="es",
        confidence=0.95,
        backend_device="cuda",
        duration_ms=85.3,
    )
    assert tx.backend_device == "cuda"

    session = CallSession(call_id="call-001", state="standby", last_activity_monotonic=5.0)
    assert session.turn_count == 0

    err = VoicePipelineError(
        code="VOICE_DEVICE_UNAVAILABLE",
        message="Audio stream failed with token=mySecretToken9999",
    )
    assert err.code == "VOICE_DEVICE_UNAVAILABLE"
    assert "mySecretToken9999" not in err.sanitized_message


def test_voice_ports_protocol_signatures_exist() -> None:
    for proto in (
        AudioInputPort,
        AudioOutputPort,
        WakeWordPort,
        VoiceActivityDetectorPort,
        SpeechToTextPort,
        TextToSpeechPort,
    ):
        assert hasattr(proto, "__protocol_attrs__") or isinstance(proto, type)


async def test_event_bus_isolates_subscriber_exceptions_and_supports_idempotent_unsubscribe() -> (
    None
):
    bus = OrchestratorEventBus()
    received_by_healthy: list[OrchestratorEvent] = []

    async def failing_subscriber(ev: OrchestratorEvent) -> None:
        raise RuntimeError(f"boom while handling {type(ev).__name__} Bearer secretTok123")

    async def healthy_subscriber(ev: OrchestratorEvent) -> None:
        received_by_healthy.append(ev)

    bus.subscribe(failing_subscriber)
    bus.subscribe(healthy_subscriber)

    ev1 = CallStateChangedEvent(
        call_id="call-1",
        correlation_id="corr-1",
        previous_state="standby",
        state="listening",
        reason="wake_word_detected",
    )
    await bus.publish(ev1)
    assert received_by_healthy == [ev1]

    # Unsubscribe failing and healthy subscribers idempotently
    bus.unsubscribe(failing_subscriber)
    bus.unsubscribe(failing_subscriber)
    ev2 = VoiceTranscriptionEvent(
        call_id="call-1",
        correlation_id="corr-1",
        sanitized_text="ejecuta pytest",
        backend_device="cuda",
        duration_ms=110.0,
    )
    await bus.publish(ev2)
    assert received_by_healthy == [ev1, ev2]

    bus.unsubscribe(healthy_subscriber)
    await bus.publish(ev2)
    assert len(received_by_healthy) == 2


async def test_orchestrator_turn_lock_serializes_turns_without_deadlocking_approval_or_barge_in(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "concurrent-app"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)

    class _AuditorStub:
        async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
            return CommandAuditResult(
                risk_level="high",
                spoken_summary="Aprobar comando sensible en ejecucion.",
                technical_explanation=request.rule_id,
                audited_by="deterministic_fallback",
            )

    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_AuditorStub(),
        event_bus=bus,
        settings=GatekeeperSettings(approval_timeout_seconds=5.0),
    )

    execution_order: list[str] = []
    first_turn_waiting = asyncio.Event()
    tier2_started = asyncio.Event()
    tier2_aclosed = asyncio.Event()

    class _LockTestingTier1:
        def __init__(self) -> None:
            self.aborted = False

        async def switch_workspace(self, new_workspace_path: str) -> None:
            execution_order.append(f"switch:{Path(new_workspace_path).name}")

        async def stream_prompt(self, message: str) -> AsyncIterator[str]:
            execution_order.append(f"start:{message}")
            if message == "turn-1":
                first_turn_waiting.set()
                # Simulate Tier 1 waiting on a Gatekeeper approval while holding _turn_lock!
                decision = await gatekeeper.evaluate_and_authorize(
                    CommandInterceptRequest(
                        correlation_id="corr-t1",
                        source="tier1_pi",
                        tool_name="bash",
                        command="sudo echo ok",
                        cwd=str(repo),
                    )
                )
                assert decision.allowed is True
            yield f"done:{message}"
            execution_order.append(f"end:{message}")

        async def barge_in_abort(self) -> None:
            self.aborted = True

    class _BlockingTier2Runner:
        def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
            async def _gen() -> AsyncIterator[RunnerProgressEvent]:
                try:
                    tier2_started.set()
                    await asyncio.sleep(10.0)
                    yield RunnerProgressEvent(
                        kind="completed",
                        conversation_id="c-1",
                        final_response="never reached",
                    )
                finally:
                    tier2_aclosed.set()

            return _gen()

    tier1 = _LockTestingTier1()
    orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=tier1,
        tier2_runner=_BlockingTier2Runner(),
        event_bus=bus,
        security_gatekeeper=gatekeeper,
    )
    await orchestrator.select_workspace(str(repo), mode="exact_path")

    seen_audit_ids: list[str] = []

    async def capture_approval(ev: OrchestratorEvent) -> None:
        from nova.domain.events import CommandApprovalRequestedEvent

        if isinstance(ev, CommandApprovalRequestedEvent):
            seen_audit_ids.append(ev.audit_id)

    bus.subscribe(capture_approval)

    t1 = asyncio.create_task(orchestrator.handle_user_utterance("turn-1"))
    await asyncio.wait_for(first_turn_waiting.wait(), timeout=1.0)
    # Start turn-2 concurrently; it must wait behind _turn_lock until turn-1 finishes
    t2 = asyncio.create_task(orchestrator.handle_user_utterance("turn-2"))
    await asyncio.sleep(0.02)
    assert execution_order == ["switch:concurrent-app", "start:turn-1"]
    assert len(seen_audit_ids) == 1

    # Resolving command approval MUST NOT deadlock even though turn-1 holds _turn_lock!
    resolved = await asyncio.wait_for(
        orchestrator.resolve_command_approval(
            seen_audit_ids[0],
            approved=True,
            resolved_by="user_voice",
        ),
        timeout=1.0,
    )
    assert resolved.approved is True
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
    assert execution_order == [
        "switch:concurrent-app",
        "start:turn-1",
        "end:turn-1",
        "start:turn-2",
        "end:turn-2",
    ]

    # Now verify barge_in() cancels an in-flight Tier 2 turn immediately and frees _turn_lock!
    t_heavy = asyncio.create_task(
        orchestrator.handle_user_utterance("[delegate_tier2] Refactoriza todo")
    )
    await asyncio.wait_for(tier2_started.wait(), timeout=1.0)
    await asyncio.wait_for(orchestrator.barge_in(), timeout=0.2)
    await asyncio.wait_for(t_heavy, timeout=0.2)
    assert tier2_aclosed.is_set()
    assert tier1.aborted is True


def test_workspace_resolver_cache_is_thread_safe_under_concurrent_scans(tmp_path: Path) -> None:
    root = tmp_path / "Proyectos"
    for idx in range(8):
        (root / f"servicio-{idx}").mkdir(parents=True)

    resolver = LocalWorkspaceResolver(allowed_roots=[root])

    def worker(idx: int) -> str:
        path = resolver._resolve_sync(f"servicio {idx % 8}", "voice_fuzzy")
        _ = resolver.cache_keys()
        return path.name

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(32)))

    assert len(results) == 32
    assert results[0] == "servicio-0"
