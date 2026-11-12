"""Deterministic TDD tests for NOVA-004 T1: FU-1..FU-3 closures and GtkFolderPickerAdapter."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.folder_picker_gtk import GtkFolderPickerAdapter, GtkFolderPickerSettings
from nova.adapters.tts_local_onnx import LocalOnnxTtsAdapter, LocalOnnxTtsSettings
from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.voice_coordinator import VoiceCallCoordinator, VoiceCoordinatorSettings
from nova.domain.errors import DesktopUiError, WorkspaceResolutionError
from nova.domain.events import (
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
)
from nova.domain.models import (
    AudioFrame,
    ResolveWorkspaceRequest,
    SynthesizedAudio,
    TranscriptionResult,
    WakeWordDetection,
    WorkspaceContext,
)
from nova.domain.ports import FolderPickerPort


class _FrameQueueAudioInput:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[AudioFrame | None] = asyncio.Queue()

    def push(self, frame: AudioFrame) -> None:
        self._queue.put_nowait(frame)

    def finish(self) -> None:
        self._queue.put_nowait(None)

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    async def stop_capture(self) -> None:
        self.finish()


class _GateAudioOutput:
    def __init__(self) -> None:
        self._playing = False
        self.release_playback = asyncio.Event()

    @property
    def is_playing(self) -> bool:
        return self._playing

    async def play_pcm(self, audio: SynthesizedAudio) -> None:
        _ = audio
        self._playing = True
        try:
            await self.release_playback.wait()
        finally:
            self._playing = False

    async def stop_playback(self) -> None:
        self._playing = False
        self.release_playback.set()


class _StubWakeWord:
    async def detect(self, frame: AudioFrame) -> WakeWordDetection:
        _ = frame
        return WakeWordDetection(detected=False, score=0.0)

    def reset(self) -> None:
        return None


class _ProgrammableVad:
    def __init__(self) -> None:
        self.reset_calls = 0

    async def speech_probability(self, frame: AudioFrame) -> float:
        return 0.95 if frame.pcm_s16le[0] != 0 else 0.01

    def reset(self) -> None:
        self.reset_calls += 1


class _ProgrammableStt:
    def __init__(self, next_text: str = "ejecuta pruebas") -> None:
        self.next_text = next_text
        self.transcribed_Lengths: list[int] = []

    async def transcribe(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int = 16000,
    ) -> TranscriptionResult:
        _ = sample_rate_hz
        self.transcribed_Lengths.append(len(pcm_s16le))
        return TranscriptionResult(
            text=self.next_text,
            language="es",
            confidence=0.95,
            backend_device="cpu",
            duration_ms=20.0,
        )


class _FastTts:
    async def synthesize(self, text: str) -> SynthesizedAudio:
        _ = text
        return SynthesizedAudio(pcm_s16le=b"\x01\x00" * 160, sample_rate_hz=24000)


class _ExplodingOrchestrator:
    def __init__(self, canary: str) -> None:
        self._canary = canary

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
    ) -> None:
        _ = (utterance, correlation_id)
        raise RuntimeError(f"boom utterance {self._canary}")

    async def select_workspace(
        self,
        query: str,
        mode: str = "voice_fuzzy",
        correlation_id: str | None = None,
    ) -> WorkspaceContext:
        _ = (query, mode, correlation_id)
        raise RuntimeError(f"boom workspace {self._canary}")

    async def barge_in(self) -> None:
        return None

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_voice",
    ) -> CommandApprovalResolvedEvent:
        _ = (audit_id, approved, resolved_by)
        raise RuntimeError(f"boom approval {self._canary}")


async def test_fu1_workspace_resolver_wraps_timeout_into_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Proyectos"
    root.mkdir()
    resolver = LocalWorkspaceResolver(allowed_roots=[root], timeout_seconds=0.02)

    def _slow_resolve_sync(raw_query: str, mode: str) -> Path:
        _ = (raw_query, mode)
        time.sleep(0.25)
        return root

    monkeypatch.setattr(resolver, "_resolve_sync", _slow_resolve_sync)

    with pytest.raises(WorkspaceResolutionError) as exc_info:
        await resolver.resolve(
            ResolveWorkspaceRequest(
                correlation_id="corr-timeout",
                query="demo",
                mode="voice_fuzzy",
            )
        )
    assert exc_info.value.code == "WORKSPACE_RESOLUTION_TIMEOUT"


async def test_fu1_voice_coordinator_catches_unexpected_exceptions_and_redacts_secrets() -> None:
    canary = "-".join(("SYNTH", "CANARY", "9999"))
    bus = OrchestratorEventBus()
    failures: list[RunnerFailureEvent] = []

    async def _on_ev(ev: OrchestratorEvent) -> None:
        if isinstance(ev, RunnerFailureEvent):
            failures.append(ev)

    bus.subscribe(_on_ev)
    audio_in = _FrameQueueAudioInput()
    audio_out = _GateAudioOutput()
    audio_out.release_playback.set()
    vad = _ProgrammableVad()
    stt = _ProgrammableStt()
    orch = _ExplodingOrchestrator(canary=canary)

    coord = VoiceCallCoordinator(
        orchestrator=orch,
        event_bus=bus,
        audio_input=audio_in,
        audio_output=audio_out,
        wake_word=_StubWakeWord(),
        vad=vad,
        stt=stt,
        tts=_FastTts(),
        settings=VoiceCoordinatorSettings(min_speech_frames=2, end_silence_frames=2),
        extra_secrets_provider=lambda: (canary,),
    )

    def _push_utterance(text: str, ts_start: float) -> None:
        stt.next_text = text
        for idx in range(2):
            audio_in.push(
                AudioFrame(pcm_s16le=b"\x10\x00" * 512, monotonic_ts=ts_start + idx * 0.03)
            )
        for idx in range(2):
            audio_in.push(
                AudioFrame(pcm_s16le=b"\x00\x00" * 512, monotonic_ts=ts_start + 0.1 + idx * 0.03)
            )

    await coord.start()
    try:
        await coord.start_call(reason="manual")

        # 1) Unexpected RuntimeError in select_workspace keeps _segment_worker alive
        _push_utterance("cambia al proyecto demo", 10.0)
        await asyncio.sleep(0.08)

        # 2) Unexpected RuntimeError in handle_user_utterance emits RunnerFailureEvent
        _push_utterance("analiza el modulo principal", 20.0)
        await asyncio.sleep(0.08)
        assert coord.session.state == "listening"

        # 3) Unexpected RuntimeError in resolve_command_approval keeps _segment_worker alive
        await bus.publish(
            CommandApprovalRequestedEvent(
                audit_id="aud-fu1",
                correlation_id="corr-fu1",
                source="tier1_pi",
                sanitized_command="npm install",
                cwd="/tmp/demo",
                risk_level="medium",
                spoken_summary="Confirma instalacion de dependencias.",
                technical_explanation="Modifica node_modules",
                expires_in_seconds=60.0,
            )
        )
        _push_utterance("aprobar comando", 1e12)
        await asyncio.sleep(0.08)

        assert len(failures) >= 3
        for failure in failures:
            assert canary not in failure.sanitized_message
            assert "<REDACTED:secret>" in failure.sanitized_message
    finally:
        await coord.shutdown()


async def test_fu2_vad_accumulators_reset_during_speaking_when_barge_in_disabled() -> None:
    now = 100.0
    bus = OrchestratorEventBus()
    audio_in = _FrameQueueAudioInput()
    audio_out = _GateAudioOutput()
    vad = _ProgrammableVad()
    stt = _ProgrammableStt(next_text="hola")

    class _NoopOrch:
        async def handle_user_utterance(
            self, utterance: str, correlation_id: str | None = None
        ) -> None:
            _ = (utterance, correlation_id)

        async def select_workspace(
            self, query: str, mode: str = "voice_fuzzy", correlation_id: str | None = None
        ) -> WorkspaceContext:
            raise NotImplementedError

        async def barge_in(self) -> None:
            return None

        async def resolve_command_approval(
            self, audit_id: str, *, approved: bool, resolved_by: str = "user_voice"
        ) -> CommandApprovalResolvedEvent:
            raise NotImplementedError

    coord = VoiceCallCoordinator(
        orchestrator=_NoopOrch(),
        event_bus=bus,
        audio_input=audio_in,
        audio_output=audio_out,
        wake_word=_StubWakeWord(),
        vad=vad,
        stt=stt,
        tts=_FastTts(),
        settings=VoiceCoordinatorSettings(
            allow_barge_in_during_tts=False,
            min_speech_frames=2,
            end_silence_frames=2,
            pre_roll_frames=3,
        ),
        clock=lambda: now,
    )

    await coord.start()
    try:
        await coord.start_call(reason="manual")
        # User starts speaking in "listening" (2 frames -> in_speech=True, utterance_buf populated)
        audio_in.push(AudioFrame(pcm_s16le=b"\x05\x00" * 512, monotonic_ts=100.1))
        audio_in.push(AudioFrame(pcm_s16le=b"\x05\x00" * 512, monotonic_ts=100.2))
        await asyncio.sleep(0.05)

        # Milestone triggers TTS -> enters "speaking" while allow_barge_in_during_tts=False
        from nova.domain.events import VoiceMilestoneEvent

        await bus.publish(
            VoiceMilestoneEvent(
                correlation_id="corr-tts",
                summary_text="Reproduciendo respuesta hablada.",
                priority="normal",
            )
        )
        await asyncio.sleep(0.05)
        assert coord.session.state == "speaking"

        # Frame arrives during "speaking" -> FU-2 must clear partial utterance_buf & in_speech
        audio_in.push(AudioFrame(pcm_s16le=b"\x05\x00" * 512, monotonic_ts=100.3))
        await asyncio.sleep(0.05)

        # Finish TTS playback -> returns to "listening"
        audio_out.release_playback.set()
        await asyncio.sleep(0.05)
        state_after_tts: str = coord.session.state
        assert state_after_tts == "listening"

        # Silence frames after TTS must NOT flush stale pre-TTS partial utterance to STT!
        audio_in.push(AudioFrame(pcm_s16le=b"\x00\x00" * 512, monotonic_ts=100.4))
        audio_in.push(AudioFrame(pcm_s16le=b"\x00\x00" * 512, monotonic_ts=100.5))
        await asyncio.sleep(0.05)
        assert stt.transcribed_Lengths == []
    finally:
        audio_out.release_playback.set()
        await coord.shutdown()


async def test_fu3_kokoro_tts_serializes_concurrent_inference_with_threading_lock() -> None:
    active_calls = 0
    max_concurrent = 0
    guard = threading.Lock()

    class _ConcurrencyProbeKokoro:
        def create(
            self,
            text: str,
            *,
            voice: str,
            speed: float,
            lang: str,
        ) -> tuple[Sequence[float], int]:
            nonlocal active_calls, max_concurrent
            _ = (text, voice, speed, lang)
            with guard:
                active_calls += 1
                if active_calls > max_concurrent:
                    max_concurrent = active_calls
            time.sleep(0.04)
            with guard:
                active_calls -= 1
            return ([0.1, -0.1, 0.2, -0.2], 24000)

    adapter = LocalOnnxTtsAdapter(
        settings=LocalOnnxTtsSettings(engine="kokoro"),
        kokoro_factory=_ConcurrencyProbeKokoro,
    )
    results = await asyncio.gather(
        adapter.synthesize("Primer aviso hablado."),
        adapter.synthesize("Segundo aviso hablado."),
        adapter.synthesize("Tercer aviso hablado."),
    )
    assert len(results) == 3
    assert max_concurrent == 1


async def test_ui_picker_mode_and_list_available_workspaces(tmp_path: Path) -> None:
    root = tmp_path / "Proyectos"
    alpha = root / "alpha-app"
    beta = root / "beta-api"
    outside = tmp_path / "outside-secret"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    outside.mkdir(parents=True)

    resolver = LocalWorkspaceResolver(allowed_roots=[root])
    catalog = await resolver.list_available_workspaces()
    assert [name for name, _ in catalog] == ["alpha-app", "beta-api"]

    ctx = await resolver.resolve(
        ResolveWorkspaceRequest(
            correlation_id="corr-ui-1",
            query=str(beta),
            mode="ui_picker",
        )
    )
    assert ctx.name == "beta-api"
    assert ctx.selected_via == "ui_picker"

    with pytest.raises(WorkspaceResolutionError) as exc_info:
        await resolver.resolve(
            ResolveWorkspaceRequest(
                correlation_id="corr-ui-out",
                query=str(outside),
                mode="ui_picker",
            )
        )
    assert exc_info.value.code == "WORKSPACE_OUTSIDE_ALLOWED_ROOTS"


async def test_gtk_folder_picker_adapter_conforms_to_port_and_handles_headless_and_selection(
    tmp_path: Path,
) -> None:
    # Headless fail-fast (<2 s) when DISPLAY / WAYLAND_DISPLAY are absent
    headless_picker: FolderPickerPort = GtkFolderPickerAdapter(
        settings=GtkFolderPickerSettings(python_binary="/usr/bin/python3"),
        environ={},
    )
    with pytest.raises(DesktopUiError) as exc_info:
        await headless_picker.pick_directory(initial_dir=tmp_path)
    assert exc_info.value.code == "UI_NATIVE_PICKER_UNAVAILABLE"

    # Simulated GTK3 subprocess returning selected path with shell=False argv safety
    captured_argv: list[str] = []

    class _FakeProc:
        def __init__(self, stdout_payload: dict[str, Any], returncode: int = 0) -> None:
            self._stdout = json.dumps(stdout_payload).encode("utf-8")
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return (self._stdout, b"")

        def kill(self) -> None:
            return None

        async def wait(self) -> int:
            return self.returncode

    chosen_dir = tmp_path / "elegido"
    chosen_dir.mkdir()

    async def _spawner(*argv: str, env: dict[str, str]) -> _FakeProc:
        _ = env
        captured_argv.extend(argv)
        return _FakeProc({"status": "selected", "path": str(chosen_dir)})

    picker = GtkFolderPickerAdapter(
        settings=GtkFolderPickerSettings(python_binary="/usr/bin/python3"),
        environ={"DISPLAY": ":0"},
        spawner=_spawner,
    )
    picked = await picker.pick_directory(initial_dir=tmp_path)
    assert picked == chosen_dir
    assert captured_argv[0] == "/usr/bin/python3"
    assert "-s" in captured_argv and "-E" in captured_argv
    assert captured_argv[-1] == str(tmp_path)

    # Cancelled dialog returns None cleanly
    async def _cancel_spawner(*argv: str, env: dict[str, str]) -> _FakeProc:
        _ = (argv, env)
        return _FakeProc({"status": "cancelled"})

    cancel_picker = GtkFolderPickerAdapter(
        settings=GtkFolderPickerSettings(python_binary="/usr/bin/python3"),
        environ={"WAYLAND_DISPLAY": "wayland-0"},
        spawner=_cancel_spawner,
    )
    assert await cancel_picker.pick_directory() is None
