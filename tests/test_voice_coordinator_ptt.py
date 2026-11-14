"""TDD tests for NOVA-008 T3: Push-to-Talk single_turn, toggle_call, Hold-to-Talk, and barge-in."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from nova.application.voice_coordinator import VoiceCallCoordinator, VoiceCoordinatorSettings
from nova.domain.events import (
    CommandApprovalResolvedEvent,
    OrchestratorEventBus,
    VoiceMilestoneEvent,
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


class _FakeAudioStream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[AudioFrame] = asyncio.Queue()
        self.playing = False
        self.stop_playback_calls = 0

    @property
    def is_playing(self) -> bool:
        return self.playing

    async def push_silence(self, count: int = 1) -> None:
        for idx in range(count):
            await self.queue.put(
                AudioFrame(
                    pcm_s16le=b"\x00\x00" * 512,
                    sample_rate_hz=16000,
                    monotonic_ts=idx * 0.032,
                )
            )

    async def push_speech(self, count: int = 6) -> None:
        for idx in range(count):
            await self.queue.put(
                AudioFrame(
                    pcm_s16le=b"\x10\x00" * 512,
                    sample_rate_hz=16000,
                    monotonic_ts=idx * 0.032,
                )
            )

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            yield await self.queue.get()

    async def stop_capture(self) -> None:
        return

    async def play_pcm(self, audio: SynthesizedAudio) -> None:
        del audio
        self.playing = True
        try:
            await asyncio.sleep(0.05)
        finally:
            self.playing = False

    async def stop_playback(self) -> None:
        self.stop_playback_calls += 1
        self.playing = False


class _FakeWakeWord:
    async def detect(self, frame: AudioFrame) -> WakeWordDetection:
        del frame
        return WakeWordDetection(detected=False, score=0.0)

    def reset(self) -> None:
        return


class _FakeVad:
    async def speech_probability(self, frame: AudioFrame) -> float:
        return 0.9 if frame.pcm_s16le[:2] == b"\x10\x00" else 0.0

    def reset(self) -> None:
        return


class _FakeStt:
    async def transcribe(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int = 16000,
    ) -> TranscriptionResult:
        del pcm_s16le, sample_rate_hz
        return TranscriptionResult(
            text="Explica este archivo",
            language="es",
            confidence=0.95,
            backend_device="cpu",
            duration_ms=25.0,
        )


class _FakeTts:
    async def synthesize(self, text: str) -> SynthesizedAudio:
        del text
        return SynthesizedAudio(pcm_s16le=b"\x01\x00" * 160, sample_rate_hz=24000)


class _FakeOrchestrator:
    def __init__(self, bus: OrchestratorEventBus) -> None:
        self._bus = bus
        self.utterances: list[str] = []
        self.barge_in_calls = 0

    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "voice_fuzzy",
        correlation_id: str | None = None,
    ) -> WorkspaceContext:
        del query, mode, correlation_id
        raise NotImplementedError

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
    ) -> None:
        self.utterances.append(utterance)
        await self._bus.publish(
            VoiceMilestoneEvent(
                correlation_id=correlation_id or "corr-ptt",
                summary_text="Listo, archivo explicado.",
                priority="normal",
            )
        )

    async def barge_in(self) -> None:
        self.barge_in_calls += 1

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_voice_or_ui",
    ) -> CommandApprovalResolvedEvent:
        del audit_id, approved, resolved_by
        raise NotImplementedError


@pytest.mark.asyncio
async def test_ptt_single_turn_listens_without_wakeword_and_returns_to_standby() -> None:
    bus = OrchestratorEventBus()
    audio = _FakeAudioStream()
    orch = _FakeOrchestrator(bus)
    coord = VoiceCallCoordinator(
        audio_input=audio,
        audio_output=audio,
        wake_word=_FakeWakeWord(),
        vad=_FakeVad(),
        stt=_FakeStt(),
        tts=_FakeTts(),
        orchestrator=orch,
        event_bus=bus,
        settings=VoiceCoordinatorSettings(
            ptt_mode="single_turn",
            min_speech_frames=2,
            end_silence_frames=2,
        ),
    )
    await coord.start()
    try:
        assert str(coord.session.state) == "standby"
        await coord.trigger_ptt_toggle()
        assert str(coord.session.state) == "listening"

        await audio.push_speech(3)
        await audio.push_silence(3)

        for _ in range(40):
            if orch.utterances and str(coord.session.state) == "standby":
                break
            await asyncio.sleep(0.02)

        assert orch.utterances == ["Explica este archivo"]
        assert str(coord.session.state) == "standby"
    finally:
        await coord.shutdown()


@pytest.mark.asyncio
async def test_ptt_toggle_during_speaking_triggers_barge_in() -> None:
    bus = OrchestratorEventBus()
    audio = _FakeAudioStream()
    orch = _FakeOrchestrator(bus)
    coord = VoiceCallCoordinator(
        audio_input=audio,
        audio_output=audio,
        wake_word=_FakeWakeWord(),
        vad=_FakeVad(),
        stt=_FakeStt(),
        tts=_FakeTts(),
        orchestrator=orch,
        event_bus=bus,
        settings=VoiceCoordinatorSettings(ptt_mode="toggle_call"),
    )
    await coord.start()
    try:
        await coord.trigger_ptt_toggle()
        assert str(coord.session.state) == "listening"
        await coord._transition_state("speaking", reason="test", correlation_id="corr-1")
        state_before: CallSessionState = coord.session.state
        assert state_before == "speaking"

        await coord.trigger_ptt_toggle()
        assert audio.stop_playback_calls >= 1
        assert orch.barge_in_calls == 1
        assert str(coord.session.state) == "listening"

        await coord.trigger_ptt_toggle()
        assert str(coord.session.state) == "standby"
    finally:
        await coord.shutdown()


@pytest.mark.asyncio
async def test_ptt_hold_captures_until_released() -> None:
    bus = OrchestratorEventBus()
    audio = _FakeAudioStream()
    orch = _FakeOrchestrator(bus)
    coord = VoiceCallCoordinator(
        audio_input=audio,
        audio_output=audio,
        wake_word=_FakeWakeWord(),
        vad=_FakeVad(),
        stt=_FakeStt(),
        tts=_FakeTts(),
        orchestrator=orch,
        event_bus=bus,
        settings=VoiceCoordinatorSettings(min_speech_frames=2, end_silence_frames=2),
    )
    await coord.start()
    try:
        await coord.start_ptt_hold()
        assert str(coord.session.state) == "listening"
        await audio.push_speech(2)
        await audio.push_silence(4)
        await asyncio.sleep(0.05)
        # While held, brief silence must not prematurely dispatch the turn
        assert orch.utterances == []

        await coord.stop_ptt_hold()
        await audio.push_silence(1)
        for _ in range(40):
            if orch.utterances and str(coord.session.state) == "standby":
                break
            await asyncio.sleep(0.02)

        assert orch.utterances == ["Explica este archivo"]
        assert str(coord.session.state) == "standby"
    finally:
        await coord.shutdown()
