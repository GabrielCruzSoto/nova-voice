"""TDD tests for T5: VoiceCallCoordinator (AC-1..AC-6, NFR-1..NFR-3, OP-4)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from nova.application.voice_coordinator import VoiceCallCoordinator, VoiceCoordinatorSettings
from nova.domain.errors import VoicePipelineError
from nova.domain.events import (
    CallStateChangedEvent,
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    VoiceMilestoneEvent,
    VoiceTranscriptionEvent,
)
from nova.domain.models import (
    AudioFrame,
    CallSessionState,
    SynthesizedAudio,
    TranscriptionResult,
    WakeWordDetection,
    WorkspaceContext,
    WorkspaceQueryMode,
)


def _state_of(coordinator: VoiceCallCoordinator) -> CallSessionState:
    return coordinator.session.state


def _frame(tag: int = 1, ts: float = 0.0) -> AudioFrame:
    return AudioFrame(
        pcm_s16le=bytes([tag % 256, 0]) * 512,
        sample_rate_hz=16000,
        channels=1,
        monotonic_ts=ts,
    )


class _ScriptedAudioInput:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[AudioFrame | BaseException | None] = asyncio.Queue()
        self.stopped = False

    def push_frame(self, frame: AudioFrame) -> None:
        self._queue.put_nowait(frame)

    def push_error(self, exc: BaseException) -> None:
        self._queue.put_nowait(exc)

    def close_stream(self) -> None:
        self._queue.put_nowait(None)

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item

    async def stop_capture(self) -> None:
        self.stopped = True
        self._queue.put_nowait(None)


class _ControlledAudioOutput:
    def __init__(self, *, hold_playback: bool = False) -> None:
        self.hold_playback = hold_playback
        self._playing = False
        self._stop_ev = asyncio.Event()
        self.started_ev = asyncio.Event()
        self.played: list[SynthesizedAudio] = []
        self.stop_calls = 0
        self.last_stop_perf: float | None = None

    @property
    def is_playing(self) -> bool:
        return self._playing

    async def play_pcm(self, audio: SynthesizedAudio) -> None:
        self._stop_ev.clear()
        self._playing = True
        self.played.append(audio)
        self.started_ev.set()
        try:
            if self.hold_playback:
                await self._stop_ev.wait()
            else:
                await asyncio.sleep(0.005)
        finally:
            self._playing = False

    async def stop_playback(self) -> None:
        self.stop_calls += 1
        self.last_stop_perf = time.perf_counter()
        self._playing = False
        self._stop_ev.set()


class _FakeWakeWord:
    def __init__(self) -> None:
        self.next_score = 0.05

    async def detect(self, frame: AudioFrame) -> WakeWordDetection:
        detected = self.next_score >= 0.60
        return WakeWordDetection(
            detected=detected,
            wake_word="oye_nova" if detected else None,
            score=self.next_score,
        )

    def reset(self) -> None:
        pass


class _FakeVad:
    def __init__(self) -> None:
        self.probs: list[float] = []
        self.default_prob = 0.05

    async def speech_probability(self, frame: AudioFrame) -> float:
        if self.probs:
            return self.probs.pop(0)
        return self.default_prob

    def reset(self) -> None:
        pass


class _FakeStt:
    def __init__(self, transcripts: list[str] | None = None) -> None:
        self.transcripts = list(transcripts or [])
        self.captured_buffers: list[bytes] = []
        self.fail_error: VoicePipelineError | None = None

    async def transcribe(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int = 16000,
    ) -> TranscriptionResult:
        if self.fail_error is not None:
            raise self.fail_error
        self.captured_buffers.append(pcm_s16le)
        text = self.transcripts.pop(0) if self.transcripts else "consulta por defecto"
        return TranscriptionResult(
            text=text,
            language="es",
            confidence=0.96,
            backend_device="cuda",
            duration_ms=42.0,
        )


class _FakeTts:
    def __init__(self) -> None:
        self.synthesized_texts: list[str] = []
        self.fail_error: VoicePipelineError | None = None

    async def synthesize(self, text: str) -> SynthesizedAudio:
        if self.fail_error is not None:
            raise self.fail_error
        self.synthesized_texts.append(text)
        return SynthesizedAudio(
            pcm_s16le=b"\x08\x00" * 400,
            sample_rate_hz=24000,
            channels=1,
        )


@dataclass
class _FakeOrchestrator:
    event_bus: OrchestratorEventBus
    hold_utterance: bool = False

    def __post_init__(self) -> None:
        self.utterances: list[tuple[str, str | None]] = []
        self.selected_workspaces: list[tuple[str, WorkspaceQueryMode]] = []
        self.approvals: list[tuple[str, bool, str]] = []
        self.barge_in_calls = 0
        self.last_barge_in_perf: float | None = None
        self._release_turn = asyncio.Event()

    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "voice_fuzzy",
        correlation_id: str | None = None,
    ) -> WorkspaceContext:
        self.selected_workspaces.append((query, mode))
        return WorkspaceContext(
            workspace_id="ws-1",
            name=query,
            canonical_path=f"/tmp/{query}",
        )

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
    ) -> None:
        self.utterances.append((utterance, correlation_id))
        if self.hold_utterance:
            await self._release_turn.wait()

    async def barge_in(self) -> None:
        self.barge_in_calls += 1
        self.last_barge_in_perf = time.perf_counter()
        self._release_turn.set()

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_voice_or_ui",
    ) -> CommandApprovalResolvedEvent:
        self.approvals.append((audit_id, approved, resolved_by))
        resolved = CommandApprovalResolvedEvent(
            audit_id=audit_id,
            correlation_id="corr-approval",
            approved=approved,
            verdict="approved_by_human" if approved else "rejected_by_human",
            resolved_by=resolved_by,
        )
        await self.event_bus.publish(resolved)
        return resolved


class _SecretHolder:
    @property
    def extra_secrets(self) -> tuple[str, ...]:
        return ("ephemVoiceSecret7788",)


async def test_ac1_ac2_wake_word_continuous_multi_turn_call_secret_redaction_and_idle_timeout() -> (
    None
):
    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)

    audio_in = _ScriptedAudioInput()
    audio_out = _ControlledAudioOutput()
    wake = _FakeWakeWord()
    vad = _FakeVad()
    stt = _FakeStt(
        transcripts=[
            "revisa el archivo usando ephemVoiceSecret7788",
            "ahora corre las pruebas unitarias",
        ]
    )
    tts = _FakeTts()
    orch = _FakeOrchestrator(event_bus=bus)

    now = 100.0

    def fake_clock() -> float:
        return now

    coordinator = VoiceCallCoordinator(
        audio_input=audio_in,
        audio_output=audio_out,
        wake_word=wake,
        vad=vad,
        stt=stt,
        tts=tts,
        orchestrator=orch,
        event_bus=bus,
        extra_secrets_provider=lambda: _SecretHolder().extra_secrets,
        settings=VoiceCoordinatorSettings(
            min_speech_frames=3,
            end_silence_frames=4,
            call_idle_timeout_seconds=180.0,
        ),
        clock=fake_clock,
    )
    await coordinator.start()

    # 1. Standby wake-word trigger
    wake.next_score = 0.85
    audio_in.push_frame(_frame(tag=1, ts=now))
    await asyncio.sleep(0.03)
    wake.next_score = 0.05
    assert coordinator.session.state == "listening"

    # 2. Turn 1 without repeating wake word (2 pre-roll + 3 speech + 4 silence)
    vad.probs = [0.1, 0.1, 0.8, 0.8, 0.8, 0.1, 0.1, 0.1, 0.1]
    for idx in range(9):
        now += 0.032
        audio_in.push_frame(_frame(tag=10 + idx, ts=now))
    await asyncio.sleep(0.05)

    # 3. Turn 2 directly inside same continuous call without wake word!
    vad.probs = [0.85, 0.85, 0.85, 0.05, 0.05, 0.05, 0.05]
    for idx in range(7):
        now += 0.032
        audio_in.push_frame(_frame(tag=30 + idx, ts=now))
    await asyncio.sleep(0.05)

    assert len(orch.utterances) == 2
    tx_events = [e for e in events if isinstance(e, VoiceTranscriptionEvent)]
    assert len(tx_events) == 2
    assert "ephemVoiceSecret7788" not in tx_events[0].sanitized_text
    assert "<REDACTED:secret>" in tx_events[0].sanitized_text

    # 4. Advance clock >= 180s in listening -> auto return to standby
    now += 181.0
    audio_in.push_frame(_frame(tag=99, ts=now))
    await asyncio.sleep(0.03)
    assert _state_of(coordinator) == "standby"

    state_events = [e for e in events if isinstance(e, CallStateChangedEvent)]
    assert any(e.state == "listening" and e.reason == "wake_word_detected" for e in state_events)
    assert any(e.state == "standby" and e.reason == "call_idle_timeout" for e in state_events)

    await coordinator.shutdown()
    assert _state_of(coordinator) == "closed"


async def test_ac2_nfr3_utterance_accumulator_caps_at_max_bytes_and_flushes() -> None:
    bus = OrchestratorEventBus()
    audio_in = _ScriptedAudioInput()
    vad = _FakeVad()
    vad.default_prob = 0.90  # non-stop speech without silence!
    stt = _FakeStt(transcripts=["segmento largo acotado"])
    orch = _FakeOrchestrator(event_bus=bus)

    coordinator = VoiceCallCoordinator(
        audio_input=audio_in,
        audio_output=_ControlledAudioOutput(),
        wake_word=_FakeWakeWord(),
        vad=vad,
        stt=stt,
        tts=_FakeTts(),
        orchestrator=orch,
        event_bus=bus,
        settings=VoiceCoordinatorSettings(
            pre_roll_frames=2,
            min_speech_frames=2,
            end_silence_frames=10,
            max_utterance_bytes=4096,  # 4 frames exact cap for fast deterministic test
        ),
    )
    await coordinator.start()
    await coordinator.start_call()

    for idx in range(6):
        audio_in.push_frame(_frame(tag=idx + 1, ts=float(idx)))
    await asyncio.sleep(0.05)

    assert len(stt.captured_buffers) >= 1
    assert len(stt.captured_buffers[0]) <= 4096
    await coordinator.shutdown()


async def test_ac3_spoken_gatekeeper_approval_ambiguous_hold_workspace_switch_and_hangup() -> None:
    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)

    audio_in = _ScriptedAudioInput()
    vad = _FakeVad()
    stt = _FakeStt(
        transcripts=[
            "qué hora es ahora",
            "sí aprobar comando",
            "no rechazar comando",
            "cambia al proyecto motor-pagos",
            "terminar llamada",
        ]
    )
    tts = _FakeTts()
    orch = _FakeOrchestrator(event_bus=bus)

    coordinator = VoiceCallCoordinator(
        audio_input=audio_in,
        audio_output=_ControlledAudioOutput(),
        wake_word=_FakeWakeWord(),
        vad=vad,
        stt=stt,
        tts=tts,
        orchestrator=orch,
        event_bus=bus,
        settings=VoiceCoordinatorSettings(min_speech_frames=2, end_silence_frames=2),
    )
    await coordinator.start()
    await coordinator.start_call()

    async def _feed_one_utterance() -> None:
        vad.probs = [0.9, 0.9, 0.05, 0.05]
        for _ in range(4):
            audio_in.push_frame(_frame())
        await asyncio.sleep(0.05)

    # Publish pending approval #1
    await bus.publish(
        CommandApprovalRequestedEvent(
            audit_id="aud-synt-01",
            correlation_id="corr-a1",
            source="tier1_pi",
            sanitized_command="sudo apt update",
            cwd="/tmp",
            risk_level="high",
            spoken_summary="Necesito aprobacion para actualizar paquetes.",
            technical_explanation="sudo apt update",
            expires_in_seconds=60.0,
        )
    )
    await asyncio.sleep(0.03)

    # 1. Ambiguous phrase must NOT approve and must NOT dispatch prompt to orchestrator!
    await _feed_one_utterance()
    assert orch.approvals == []
    assert orch.utterances == []
    interrupts = [
        e for e in events if isinstance(e, VoiceMilestoneEvent) and e.priority == "interrupt"
    ]
    assert len(interrupts) >= 1

    # 2. Explicit approval resolves aud-synt-01 with resolved_by="user_voice"
    await _feed_one_utterance()
    assert orch.approvals == [("aud-synt-01", True, "user_voice")]

    # 3. Publish pending approval #2 and reject by voice
    await bus.publish(
        CommandApprovalRequestedEvent(
            audit_id="aud-synt-02",
            correlation_id="corr-a2",
            source="tier2_agy",
            sanitized_command="docker rm -f db",
            cwd="/tmp",
            risk_level="critical",
            spoken_summary="Necesito aprobacion para detener contenedor.",
            technical_explanation="docker rm -f db",
            expires_in_seconds=60.0,
        )
    )
    await asyncio.sleep(0.03)
    await _feed_one_utterance()
    assert orch.approvals[-1] == ("aud-synt-02", False, "user_voice")

    # 4. Workspace fuzzy switch by voice
    await _feed_one_utterance()
    assert orch.selected_workspaces == [("motor-pagos", "voice_fuzzy")]

    # 5. Hang up call returning to standby
    await _feed_one_utterance()
    assert coordinator.session.state == "standby"

    await coordinator.shutdown()


async def test_ac4_tts_queue_preempts_normal_speech_on_interrupt_milestone() -> None:
    bus = OrchestratorEventBus()
    audio_in = _ScriptedAudioInput()
    audio_out = _ControlledAudioOutput(hold_playback=True)
    tts = _FakeTts()
    orch = _FakeOrchestrator(event_bus=bus)

    coordinator = VoiceCallCoordinator(
        audio_input=audio_in,
        audio_output=audio_out,
        wake_word=_FakeWakeWord(),
        vad=_FakeVad(),
        stt=_FakeStt(),
        tts=tts,
        orchestrator=orch,
        event_bus=bus,
        settings=VoiceCoordinatorSettings(max_tts_queue=16),
    )
    await coordinator.start()
    await coordinator.start_call()

    await bus.publish(
        VoiceMilestoneEvent(
            correlation_id="c-1",
            summary_text="Primer hito normal en reproduccion.",
            priority="normal",
        )
    )
    await bus.publish(
        VoiceMilestoneEvent(
            correlation_id="c-2",
            summary_text="Segundo hito normal que sera descartado.",
            priority="normal",
        )
    )
    await asyncio.wait_for(audio_out.started_ev.wait(), timeout=1.0)
    assert coordinator.session.state == "speaking"

    # Now let subsequent playbacks complete quickly and fire an interrupt alert!
    audio_out.hold_playback = False
    await bus.publish(
        VoiceMilestoneEvent(
            correlation_id="c-alert",
            summary_text="Alerta critica prioritaria de seguridad.",
            priority="interrupt",
        )
    )
    await asyncio.sleep(0.06)

    assert "Alerta critica prioritaria de seguridad." in tts.synthesized_texts
    assert "Segundo hito normal que sera descartado." not in tts.synthesized_texts
    assert _state_of(coordinator) == "listening"
    await coordinator.shutdown()


async def test_ac5_nfr1_barge_in_stops_playback_and_aborts_turn_in_under_100ms() -> None:
    bus = OrchestratorEventBus()
    audio_in = _ScriptedAudioInput()
    audio_out = _ControlledAudioOutput(hold_playback=True)
    vad = _FakeVad()
    stt = _FakeStt(transcripts=["mejor ejecuta solo linter"])
    tts = _FakeTts()
    orch = _FakeOrchestrator(event_bus=bus, hold_utterance=True)

    coordinator = VoiceCallCoordinator(
        audio_input=audio_in,
        audio_output=audio_out,
        wake_word=_FakeWakeWord(),
        vad=vad,
        stt=stt,
        tts=tts,
        orchestrator=orch,
        event_bus=bus,
        settings=VoiceCoordinatorSettings(
            pre_roll_frames=6,
            min_speech_frames=5,
            end_silence_frames=3,
            barge_in_threshold=0.60,
        ),
    )
    await coordinator.start()
    await coordinator.start_call()

    # Trigger active TTS playback ("speaking")
    await bus.publish(
        VoiceMilestoneEvent(
            correlation_id="c-speak",
            summary_text="Explicando avance largo del agente en curso.",
            priority="normal",
        )
    )
    await asyncio.wait_for(audio_out.started_ev.wait(), timeout=1.0)
    assert coordinator.session.state == "speaking"

    # Feed 4 speech frames (below 5-frame onset), then measure latency from 5th onset frame!
    vad.probs = [0.80, 0.80, 0.80, 0.80, 0.80, 0.05, 0.05, 0.05]
    for i in range(4):
        audio_in.push_frame(_frame(tag=50 + i))
    await asyncio.sleep(0.01)

    t_onset = time.perf_counter()
    audio_in.push_frame(_frame(tag=54))  # 5th speech frame confirms barge-in!
    while audio_out.stop_calls == 0 or orch.barge_in_calls == 0:
        await asyncio.sleep(0.002)
        if (time.perf_counter() - t_onset) > 0.5:
            break

    assert audio_out.last_stop_perf is not None
    assert orch.last_barge_in_perf is not None
    stop_latency_ms = (audio_out.last_stop_perf - t_onset) * 1000.0
    barge_latency_ms = (orch.last_barge_in_perf - t_onset) * 1000.0
    assert stop_latency_ms < 100.0, f"Playback cut took {stop_latency_ms:.2f} ms"
    assert barge_latency_ms < 100.0, f"Orchestrator barge_in took {barge_latency_ms:.2f} ms"
    assert _state_of(coordinator) == "listening"

    # Now finish trailing silence and verify the barge-in utterance preserved all 5 onset frames!
    orch.hold_utterance = False
    for i in range(3):
        audio_in.push_frame(_frame(tag=60 + i))
    await asyncio.sleep(0.05)

    assert len(stt.captured_buffers) == 1
    assert len(stt.captured_buffers[0]) >= 5 * 1024
    assert orch.utterances == [("mejor ejecuta solo linter", orch.utterances[0][1])]

    await coordinator.shutdown()


async def test_ac6_voice_failures_publish_sanitized_runner_failure_without_crashing() -> None:
    bus = OrchestratorEventBus()
    failures: list[RunnerFailureEvent] = []

    async def collect_fail(ev: OrchestratorEvent) -> None:
        if isinstance(ev, RunnerFailureEvent):
            failures.append(ev)

    bus.subscribe(collect_fail)

    audio_in = _ScriptedAudioInput()
    vad = _FakeVad()
    stt = _FakeStt()
    stt.fail_error = VoicePipelineError(
        code="VOICE_STT_FAILED",
        message="Decoder crashed with Bearer syntSttTok1234",
    )
    tts = _FakeTts()
    tts.fail_error = VoicePipelineError(
        code="VOICE_TTS_FAILED",
        message="ONNX synth failed for nvapi-syntTtsKey5678",
    )
    orch = _FakeOrchestrator(event_bus=bus)

    coordinator = VoiceCallCoordinator(
        audio_input=audio_in,
        audio_output=_ControlledAudioOutput(),
        wake_word=_FakeWakeWord(),
        vad=vad,
        stt=stt,
        tts=tts,
        orchestrator=orch,
        event_bus=bus,
        settings=VoiceCoordinatorSettings(min_speech_frames=2, end_silence_frames=2),
    )
    await coordinator.start()
    await coordinator.start_call()

    # Trigger STT failure
    vad.probs = [0.9, 0.9, 0.05, 0.05]
    for _ in range(4):
        audio_in.push_frame(_frame())
    # Trigger TTS failure
    await bus.publish(
        VoiceMilestoneEvent(
            correlation_id="c-tts-err",
            summary_text="Intentando hablar con fallo simulado.",
            priority="normal",
        )
    )
    # Trigger audio capture device failure
    await asyncio.sleep(0.05)
    audio_in.push_error(
        VoicePipelineError(
            code="VOICE_DEVICE_UNAVAILABLE",
            message="PipeWire socket lost",
        )
    )
    await asyncio.sleep(0.05)

    codes = {f.error_code for f in failures}
    assert {"VOICE_STT_FAILED", "VOICE_TTS_FAILED", "VOICE_DEVICE_UNAVAILABLE"} <= codes
    for f in failures:
        assert "syntSttTok1234" not in f.sanitized_message
        assert "syntTtsKey5678" not in f.sanitized_message

    await coordinator.shutdown()
    assert _state_of(coordinator) == "closed"


async def test_end_to_end_real_orchestrator_and_gatekeeper_spoken_approval_flow(
    tmp_path: object,
) -> None:
    from pathlib import Path

    from nova.adapters.workspace_resolver import LocalWorkspaceResolver
    from nova.application.orchestrator import NovaOrchestrator
    from nova.application.security_gatekeeper import GatekeeperSettings, SecurityGatekeeper
    from nova.domain.models import (
        CodingTaskRequest,
        CommandAuditInput,
        CommandAuditResult,
        CommandInterceptRequest,
        RunnerProgressEvent,
    )

    assert isinstance(tmp_path, Path)
    root = tmp_path / "Proyectos"
    repo = root / "voz-e2e"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)

    class _AuditorStub:
        async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
            return CommandAuditResult(
                risk_level="high",
                spoken_summary="Se requiere tu aprobacion hablada para este comando.",
                technical_explanation=request.rule_id,
                audited_by="deterministic_fallback",
            )

    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=_AuditorStub(),
        event_bus=bus,
        settings=GatekeeperSettings(approval_timeout_seconds=5.0),
    )

    command_completed = asyncio.Event()

    class _GatekeeperUsingTier1:
        async def switch_workspace(self, new_workspace_path: str) -> None:
            pass

        async def stream_prompt(self, message: str) -> AsyncIterator[str]:
            dec = await gatekeeper.evaluate_and_authorize(
                CommandInterceptRequest(
                    correlation_id="corr-e2e",
                    source="tier1_pi",
                    tool_name="bash",
                    command="sudo echo aprobado",
                    cwd=str(repo),
                )
            )
            assert dec.allowed is True
            command_completed.set()
            yield "Comando autorizado y ejecutado con exito."

        async def barge_in_abort(self) -> None:
            pass

    class _NoopTier2:
        async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
            yield RunnerProgressEvent(kind="completed", conversation_id="c", final_response="ok")

    real_orchestrator = NovaOrchestrator(
        workspace_resolver=resolver,
        tier1_client=_GatekeeperUsingTier1(),
        tier2_runner=_NoopTier2(),
        event_bus=bus,
        security_gatekeeper=gatekeeper,
    )
    await real_orchestrator.select_workspace(str(repo), mode="exact_path")

    audio_in = _ScriptedAudioInput()
    vad = _FakeVad()
    stt = _FakeStt(transcripts=["ejecuta comando con sudo", "sí aprobar comando"])
    tts = _FakeTts()

    coordinator = VoiceCallCoordinator(
        audio_input=audio_in,
        audio_output=_ControlledAudioOutput(),
        wake_word=_FakeWakeWord(),
        vad=vad,
        stt=stt,
        tts=tts,
        orchestrator=real_orchestrator,
        event_bus=bus,
        security_gatekeeper=gatekeeper,
        settings=VoiceCoordinatorSettings(min_speech_frames=2, end_silence_frames=2),
    )
    await coordinator.start()
    await coordinator.start_call()

    # 1. User speaks technical prompt triggering Gatekeeper hold
    vad.probs = [0.9, 0.9, 0.05, 0.05]
    for _ in range(4):
        audio_in.push_frame(_frame())
    await asyncio.sleep(0.05)

    # 2. User speaks "sí aprobar comando" -> resolves Gatekeeper and completes Tier 1 turn!
    vad.probs = [0.9, 0.9, 0.05, 0.05]
    for _ in range(4):
        audio_in.push_frame(_frame())

    await asyncio.wait_for(command_completed.wait(), timeout=1.0)
    await asyncio.sleep(0.04)
    assert "Comando autorizado y ejecutado con exito." in tts.synthesized_texts
    await coordinator.shutdown()
