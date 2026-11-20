"""Asynchronous continuous CallSession coordinator and <100 ms barge-in engine (NOVA-003 OP-4)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import math
import re
import struct
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nova.application.voice_intent import classify_voice_control_intent
from nova.domain.errors import NovaDomainError, VoicePipelineError, redact_sensitive_text
from nova.domain.events import (
    CallStateChangedEvent,
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    VoiceMilestoneEvent,
    VoiceSpeechChunkEvent,
    VoiceTranscriptionEvent,
)
from nova.domain.models import (
    CallSession,
    CallSessionState,
    PttMode,
    SynthesizedAudio,
    TranscriptionResult,
    WorkspaceContext,
    WorkspaceQueryMode,
)
from nova.domain.ports import (
    AudioInputPort,
    AudioOutputPort,
    SecurityGatekeeperPort,
    SpeechToTextPort,
    TextToSpeechPort,
    VoiceActivityDetectorPort,
    WakeWordPort,
)

logger = logging.getLogger("nova.voice_coordinator")

_HOLD_REMINDER_TEXT = (
    "Hay un comando pendiente de aprobacion. Responde aprobar comando o rechazar comando."
)
_MAX_TRACKED_APPROVALS = 64
_STANDBY_SPOTTER_MAX_CHUNK_BYTES = 64_000
_WAKE_PHRASE_RE = re.compile(
    r"\b(?:(?:oye|hola|hey|ok|okay)\s*[,.:;¡!¿?-]*\s+(?:nova|no\s+va|noba|nava)|nova)\b",
    re.IGNORECASE,
)


def _extract_post_wake_utterance(raw_text: str) -> tuple[bool, str]:
    """Return (wake_detected, post_wake_remainder) for a standby STT transcript."""
    text = raw_text.strip()
    if not text:
        return False, ""
    match = _WAKE_PHRASE_RE.search(text)
    if match is None:
        return False, ""
    remainder = text[match.end() :].lstrip(" ,.:;¡!¿?-").strip()
    if any(ch.isalnum() for ch in remainder):
        return True, remainder
    return True, ""


_SpokenQueueItem = VoiceMilestoneEvent | VoiceSpeechChunkEvent


def _spoken_item_text(item: _SpokenQueueItem) -> str:
    if isinstance(item, VoiceSpeechChunkEvent):
        return item.text
    return item.summary_text


@dataclass(slots=True, frozen=True)
class _PendingVoiceApproval:
    audit_id: str
    requested_at: float
    deadline: float


class OrchestratorVoiceTargetPort(Protocol):
    """Application boundary consumed by VoiceCallCoordinator (satisfied by NovaOrchestrator)."""

    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "voice_fuzzy",
        correlation_id: str | None = None,
    ) -> WorkspaceContext: ...

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
    ) -> None: ...

    async def barge_in(self) -> None: ...

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_voice_or_ui",
    ) -> CommandApprovalResolvedEvent: ...


def generate_earcon_chime(sample_rate_hz: int = 16000) -> SynthesizedAudio:
    """Generate a low-latency ascending two-tone earcon chime (587 Hz -> 880 Hz, 80 ms)."""
    tone1_freq = 587.33  # D5
    tone2_freq = 880.0  # A5
    tone1_duration = 0.035  # 35 ms
    tone2_duration = 0.045  # 45 ms
    fade_duration = 0.005  # 5 ms linear envelope to prevent clicks
    volume = 0.22

    total_samples_1 = int(sample_rate_hz * tone1_duration)
    total_samples_2 = int(sample_rate_hz * tone2_duration)
    fade_samples = max(1, int(sample_rate_hz * fade_duration))

    pcm_bytes = bytearray()

    # Tone 1
    for i in range(total_samples_1):
        envelope = 1.0
        if i < fade_samples:
            envelope = i / fade_samples
        elif i > total_samples_1 - fade_samples:
            envelope = (total_samples_1 - i) / fade_samples
        angle = 2.0 * math.pi * tone1_freq * (i / sample_rate_hz)
        val = int(32767.0 * volume * envelope * math.sin(angle))
        pcm_bytes.extend(struct.pack("<h", max(-32768, min(32767, val))))

    # Tone 2
    for i in range(total_samples_2):
        envelope = 1.0
        if i < fade_samples:
            envelope = i / fade_samples
        elif i > total_samples_2 - fade_samples:
            envelope = (total_samples_2 - i) / fade_samples
        angle = 2.0 * math.pi * tone2_freq * (i / sample_rate_hz)
        val = int(32767.0 * volume * envelope * math.sin(angle))
        pcm_bytes.extend(struct.pack("<h", max(-32768, min(32767, val))))

    return SynthesizedAudio(
        pcm_s16le=bytes(pcm_bytes),
        sample_rate_hz=sample_rate_hz,
        channels=1,
    )


def _frame_ac_rms(pcm_s16le: bytes) -> float:
    """Compute DC-removed normalized RMS energy in [0.0, 1.0] for mono s16le PCM."""
    sample_count = len(pcm_s16le) // 2
    if sample_count == 0:
        return 0.0
    ints = struct.unpack(f"<{sample_count}h", pcm_s16le[: sample_count * 2])
    mean = sum(ints) / sample_count
    variance = sum((val - mean) ** 2 for val in ints) / sample_count
    return math.sqrt(variance) / 32768.0


def _update_noise_floor(current: float, sample_rms: float) -> float:
    """Track ambient room noise floor via bounded EMA."""
    clamped = min(0.08, max(0.0, sample_rms))
    if current <= 0.0:
        return clamped
    return min(0.08, 0.92 * current + 0.08 * clamped)


def _update_echo_floor(current: float, sample_rms: float) -> float:
    """Track loudspeaker echo RMS with fast attack and slow decay during TTS."""
    clamped = max(0.0, sample_rms)
    if current <= 0.0:
        return clamped
    if clamped > current:
        return 0.70 * current + 0.30 * clamped
    return 0.95 * current + 0.05 * clamped


class VoiceCoordinatorSettings(BaseModel):
    """Bounded memory, threshold, and timeout configuration for VoiceCallCoordinator."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pre_roll_frames: int = Field(default=10, ge=1, le=50)
    speech_threshold: float = Field(default=0.50, gt=0.0, le=1.0)
    barge_in_threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    min_speech_frames: int = Field(default=5, ge=1, le=50)
    end_silence_frames: int = Field(default=19, ge=1, le=200)
    max_utterance_bytes: int = Field(default=960_000, ge=2048, le=3_840_000)
    max_tts_queue: int = Field(default=64, ge=1, le=128)
    call_idle_timeout_seconds: float = Field(default=180.0, gt=0.0, le=3600.0)
    allow_barge_in_during_tts: bool = True
    half_duplex_turn_mode: bool = False
    barge_in_grace_seconds: float = Field(default=0.35, ge=0.0, le=2.0)
    barge_in_min_frames: int | None = Field(default=None, ge=1, le=50)
    barge_in_echo_multiplier: float = Field(default=1.8, ge=1.0, le=10.0)
    min_speech_rms: float = Field(default=0.0, ge=0.0, le=0.5)
    noise_floor_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    speech_resume_frames: int = Field(default=2, ge=1, le=10)
    capture_retry_delay_seconds: float = Field(default=0.25, ge=0.01, le=5.0)
    enable_wake_earcon: bool = True
    ptt_mode: PttMode = "single_turn"


class VoiceCallCoordinator:
    """Coordinates hands-free Wake Word, continuous VAD turns, local STT/TTS, and barge-in."""

    def __init__(
        self,
        *,
        audio_input: AudioInputPort,
        audio_output: AudioOutputPort,
        wake_word: WakeWordPort,
        vad: VoiceActivityDetectorPort,
        stt: SpeechToTextPort,
        tts: TextToSpeechPort,
        orchestrator: OrchestratorVoiceTargetPort,
        event_bus: OrchestratorEventBus,
        security_gatekeeper: SecurityGatekeeperPort | None = None,
        extra_secrets_provider: Callable[[], tuple[str, ...]] | None = None,
        settings: VoiceCoordinatorSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audio_input = audio_input
        self._audio_output = audio_output
        self._wake_word = wake_word
        self._vad = vad
        self._stt = stt
        self._tts = tts
        self._orchestrator = orchestrator
        self._event_bus = event_bus
        self._security_gatekeeper = security_gatekeeper
        self._extra_secrets_provider = extra_secrets_provider
        self._settings = settings or VoiceCoordinatorSettings()
        self._clock = clock

        self._session = CallSession(
            call_id=f"call-{uuid.uuid4()}",
            state="standby",
            last_activity_monotonic=self._clock(),
            turn_count=0,
        )
        self._call_open = False
        self._call_epoch = 0
        self._capture_reset_epoch = 0
        self._single_turn_active = False
        self._ptt_hold_active = False
        self._flush_ptt_hold = False
        self._speaking_started_at = 0.0
        self._earcon_audio: SynthesizedAudio | None = generate_earcon_chime(sample_rate_hz=16000)

        self._tts_queue: deque[_SpokenQueueItem] = deque()
        self._tts_wakeup = asyncio.Event()
        self._tts_epoch = 0
        self._current_tts_priority: Literal["normal", "interrupt"] | None = None
        self._current_tts_item_task: asyncio.Task[None] | None = None

        self._segment_queue: asyncio.Queue[tuple[bytes, int, float, int] | None] = asyncio.Queue(
            maxsize=8
        )
        self._current_stt_task: asyncio.Task[TranscriptionResult] | None = None
        self._standby_spotter_task: asyncio.Task[None] | None = None
        self._pending_standby_followup: tuple[bytes, int, float] | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._segment_worker_task: asyncio.Task[None] | None = None
        self._tts_worker_task: asyncio.Task[None] | None = None
        self._active_turn_tasks: set[asyncio.Task[None]] = set()

        self._pending_approvals: deque[_PendingVoiceApproval] = deque(
            maxlen=_MAX_TRACKED_APPROVALS
        )
        self._started = False

    @property
    def session(self) -> CallSession:
        """Return current immutable CallSession snapshot."""
        return self._session

    def _extra_secrets(self) -> tuple[str, ...]:
        secrets: list[str] = []
        if self._security_gatekeeper is not None:
            secrets.extend(self._security_gatekeeper.extra_secrets)
        if self._extra_secrets_provider is not None:
            secrets.extend(self._extra_secrets_provider())
        return tuple(secrets)

    def _redact(self, text: str) -> str:
        return redact_sensitive_text(text, extra_secrets=self._extra_secrets())

    def _log(self, event_name: str, **fields: str | int | float | bool) -> None:
        payload: dict[str, str | int | float | bool] = {"event": event_name}
        for key, val in fields.items():
            payload[key] = self._redact(val) if isinstance(val, str) else val
        logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _prune_expired_approvals(self) -> None:
        now = self._clock()
        while self._pending_approvals and self._pending_approvals[0].deadline <= now:
            self._pending_approvals.popleft()
        if self._pending_approvals:
            self._pending_approvals = deque(
                (item for item in self._pending_approvals if item.deadline > now),
                maxlen=_MAX_TRACKED_APPROVALS,
            )

    def _has_active_pending_approval(self) -> bool:
        self._prune_expired_approvals()
        return bool(self._pending_approvals)

    def _post_speech_target_state(self) -> CallSessionState:
        """Determine state to restore after TTS finishes or is cancelled."""
        if not self._call_open:
            return "standby"
        if self._active_turn_tasks and not self._has_active_pending_approval():
            return "executing"
        return "listening"

    async def start(self) -> None:
        """Subscribe to OrchestratorEventBus and launch capture, STT, and TTS workers."""
        if self._started:
            return
        self._started = True
        self._event_bus.subscribe(self._on_event)
        self._capture_task = asyncio.create_task(self._capture_loop())
        self._segment_worker_task = asyncio.create_task(self._segment_worker())
        self._tts_worker_task = asyncio.create_task(self._tts_worker())

    def _purge_segment_queue(self) -> None:
        while not self._segment_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._segment_queue.get_nowait()

    async def _cancel_standby_spotter(self) -> None:
        spotter_task = self._standby_spotter_task
        self._standby_spotter_task = None
        if (
            spotter_task is not None
            and not spotter_task.done()
            and spotter_task is not asyncio.current_task()
        ):
            spotter_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await spotter_task

    async def start_call(
        self,
        reason: str = "manual_call_start",
        *,
        preserve_standby_followup: bool = False,
    ) -> None:
        """Explicitly open a continuous CallSession in 'listening' state."""
        if self._session.state == "closed":
            return
        followup = self._pending_standby_followup if preserve_standby_followup else None
        self._pending_standby_followup = None
        await self._cancel_standby_spotter()
        self._call_open = True
        self._call_epoch += 1
        self._capture_reset_epoch += 1
        self._purge_segment_queue()
        self._vad.reset()
        corr_id = f"corr-{uuid.uuid4()}"
        self._session = self._session.model_copy(
            update={
                "call_id": f"call-{uuid.uuid4()}",
                "last_activity_monotonic": self._clock(),
                "turn_count": 0,
                "active_correlation_id": corr_id,
            }
        )
        await self._transition_state("listening", reason=reason, correlation_id=corr_id)
        if self._settings.enable_wake_earcon and reason in (
            "wake_word_detected",
            "wake_word_transcribed",
            "ptt_single_turn_start",
            "ptt_toggle_start",
            "ptt_hold_start",
        ):
            asyncio.create_task(self._play_earcon_safe())
        if followup is not None:
            seg_bytes, seg_rate, seg_started = followup
            self._enqueue_segment(seg_bytes, seg_rate, seg_started)

    async def _play_earcon_safe(self) -> None:
        if not self._settings.enable_wake_earcon or self._earcon_audio is None:
            return
        try:
            await self._audio_output.play_pcm(self._earcon_audio)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Failed to play wake earcon: %s", exc)

    async def end_call(self, reason: str = "manual_call_end") -> None:
        """Return an open CallSession back to 'standby' hands-free wake-word mode."""
        if self._session.state in ("standby", "closed"):
            return
        self._call_open = False
        self._call_epoch += 1
        self._capture_reset_epoch += 1
        self._pending_standby_followup = None
        await self._cancel_standby_spotter()
        self._single_turn_active = False
        self._ptt_hold_active = False
        self._flush_ptt_hold = False
        self._speaking_started_at = 0.0
        self._earcon_audio = generate_earcon_chime(sample_rate_hz=16000)
        self._purge_segment_queue()

        stt_task = self._current_stt_task
        if stt_task is not None and not stt_task.done():
            stt_task.cancel()

        if self._active_turn_tasks and not self._has_active_pending_approval():
            for turn_t in list(self._active_turn_tasks):
                turn_t.cancel()
            await self._orchestrator.barge_in()

        self._tts_queue.clear()
        await self._cancel_active_tts_playback()
        self._vad.reset()
        self._wake_word.reset()
        corr_id = self._session.active_correlation_id or f"corr-{uuid.uuid4()}"
        await self._transition_state("standby", reason=reason, correlation_id=corr_id)

    async def barge_in(self) -> None:
        """Public idempotent manual/UI barge-in cutting TTS (<100 ms) and active turn."""
        if self._session.state == "closed":
            return
        await self._execute_barge_in()

    async def trigger_ptt_toggle(self) -> CallSessionState:
        """Handle global/UI Push-to-Talk toggle according to `settings.ptt_mode`."""
        state = self._session.state
        if state == "closed":
            return state
        if state == "speaking":
            await self.barge_in()
            return self._session.state
        if state == "standby":
            self._ptt_hold_active = False
            self._flush_ptt_hold = False
            self._single_turn_active = self._settings.ptt_mode == "single_turn"
            reason = (
                "ptt_single_turn_start"
                if self._single_turn_active
                else "ptt_toggle_start"
            )
            await self.start_call(reason=reason)
            return self._session.state
        await self.end_call(reason="ptt_toggle_stop")
        return self._session.state

    async def start_ptt_hold(self) -> CallSessionState:
        """Begin Hold-to-Talk capture directly from panel button/key press."""
        state = self._session.state
        if state == "closed":
            return state
        if state == "speaking":
            await self.barge_in()
        self._ptt_hold_active = True
        self._flush_ptt_hold = False
        self._single_turn_active = True
        if self._session.state == "standby":
            await self.start_call(reason="ptt_hold_start")
        return self._session.state

    async def stop_ptt_hold(self) -> CallSessionState:
        """Release Hold-to-Talk capture and flush buffered audio immediately."""
        if self._session.state == "closed":
            return self._session.state
        if self._ptt_hold_active:
            self._ptt_hold_active = False
            self._flush_ptt_hold = True
        return self._session.state

    def update_settings(
        self,
        *,
        ptt_mode: PttMode | None = None,
        speech_threshold: float | None = None,
        barge_in_threshold: float | None = None,
        barge_in_grace_seconds: float | None = None,
        enable_wake_earcon: bool | None = None,
        half_duplex_turn_mode: bool | None = None,
    ) -> None:
        """Update dynamic voice coordinator thresholds and PTT mode in RAM."""
        updates: dict[str, object] = {}
        if ptt_mode is not None:
            updates["ptt_mode"] = ptt_mode
        if speech_threshold is not None:
            updates["speech_threshold"] = speech_threshold
        if barge_in_threshold is not None:
            updates["barge_in_threshold"] = barge_in_threshold
        if barge_in_grace_seconds is not None:
            updates["barge_in_grace_seconds"] = barge_in_grace_seconds
        if enable_wake_earcon is not None:
            updates["enable_wake_earcon"] = enable_wake_earcon
        if half_duplex_turn_mode is not None:
            updates["half_duplex_turn_mode"] = half_duplex_turn_mode
        if updates:
            self._settings = self._settings.model_copy(update=updates)

    async def shutdown(self) -> None:
        """Unsubscribe from bus, stop audio streams, cancel workers, and close CallSession."""
        self._event_bus.unsubscribe(self._on_event)
        self._call_open = False
        self._call_epoch += 1
        self._capture_reset_epoch += 1
        self._single_turn_active = False
        self._ptt_hold_active = False
        self._flush_ptt_hold = False
        self._speaking_started_at = 0.0
        self._earcon_audio = generate_earcon_chime(sample_rate_hz=16000)
        self._purge_segment_queue()
        self._tts_queue.clear()
        self._pending_standby_followup = None
        self._started = False

        await self._cancel_standby_spotter()
        stt_task = self._current_stt_task
        if stt_task is not None and not stt_task.done():
            stt_task.cancel()

        await self._audio_input.stop_capture()
        await self._cancel_active_tts_playback()

        for turn_t in list(self._active_turn_tasks):
            turn_t.cancel()
        if self._active_turn_tasks:
            await asyncio.gather(*self._active_turn_tasks, return_exceptions=True)
        self._active_turn_tasks.clear()

        for worker in (self._capture_task, self._segment_worker_task, self._tts_worker_task):
            if worker is not None and not worker.done():
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

        if self._session.state != "closed":
            corr_id = self._session.active_correlation_id or f"corr-{uuid.uuid4()}"
            await self._transition_state(
                "closed",
                reason="coordinator_shutdown",
                correlation_id=corr_id,
            )

    async def _transition_state(
        self,
        new_state: CallSessionState,
        *,
        reason: str,
        correlation_id: str,
    ) -> None:
        prev = self._session.state
        if prev == "closed":
            return
        if not self._call_open and new_state not in ("standby", "closed"):
            return
        now = self._clock()
        if prev == new_state:
            self._session = self._session.model_copy(
                update={
                    "last_activity_monotonic": now,
                    "active_correlation_id": correlation_id,
                }
            )
            return

        if new_state == "speaking":
            self._speaking_started_at = now
        elif prev == "speaking" and reason == "tts_playback_completed":
            self._capture_reset_epoch += 1
            self._vad.reset()

        self._session = self._session.model_copy(
            update={
                "state": new_state,
                "last_activity_monotonic": now,
                "active_correlation_id": correlation_id,
            }
        )
        await self._event_bus.publish(
            CallStateChangedEvent(
                call_id=self._session.call_id,
                correlation_id=correlation_id,
                previous_state=prev,
                state=new_state,
                reason=reason,
            )
        )

    async def _publish_voice_failure(
        self,
        code: str,
        message: str,
        correlation_id: str | None = None,
    ) -> None:
        corr_id = correlation_id or self._session.active_correlation_id or f"corr-{uuid.uuid4()}"
        redacted = self._redact(message)
        self._log("voice_pipeline_failure", code=code, correlation_id=corr_id, message=redacted)
        await self._event_bus.publish(
            RunnerFailureEvent(
                correlation_id=corr_id,
                error_code=code,
                sanitized_message=redacted,
                recoverable=True,
            )
        )

    async def _on_event(self, event: OrchestratorEvent) -> None:
        if isinstance(event, CommandApprovalRequestedEvent):
            self._prune_expired_approvals()
            now = self._clock()
            if not any(a.audit_id == event.audit_id for a in self._pending_approvals):
                self._pending_approvals.append(
                    _PendingVoiceApproval(
                        audit_id=event.audit_id,
                        requested_at=now,
                        deadline=now + event.expires_in_seconds,
                    )
                )
            return

        if isinstance(event, CommandApprovalResolvedEvent):
            self._pending_approvals = deque(
                (a for a in self._pending_approvals if a.audit_id != event.audit_id),
                maxlen=_MAX_TRACKED_APPROVALS,
            )
            return

        if isinstance(event, (VoiceMilestoneEvent, VoiceSpeechChunkEvent)):
            if self._session.state == "closed" or not self._call_open:
                return
            if event.priority == "interrupt":
                kept = [ev for ev in self._tts_queue if ev.priority == "interrupt"]
                self._tts_queue = deque(kept[-max(0, self._settings.max_tts_queue - 1) :])
                self._tts_queue.append(event)
                if (
                    self._current_tts_item_task is not None
                    and not self._current_tts_item_task.done()
                    and self._current_tts_priority == "normal"
                ):
                    await self._cancel_active_tts_playback()
                self._tts_wakeup.set()
            else:
                if len(self._tts_queue) >= self._settings.max_tts_queue:
                    dropped = False
                    for idx, queued in enumerate(self._tts_queue):
                        if queued.priority == "normal":
                            del self._tts_queue[idx]
                            dropped = True
                            break
                    if not dropped:
                        return
                self._tts_queue.append(event)
                self._tts_wakeup.set()

    async def _cancel_active_tts_playback(self) -> None:
        self._tts_epoch += 1
        item_task = self._current_tts_item_task
        if item_task is not None and not item_task.done():
            item_task.cancel()
        await self._audio_output.stop_playback()
        if item_task is not None and not item_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await item_task

    def _should_keep_capturing(self) -> bool:
        return self._started and self._session.state != "closed"

    async def _capture_loop(self) -> None:
        pre_roll: deque[bytes] = deque(maxlen=self._settings.pre_roll_frames)
        utterance_buf = bytearray()
        in_speech = False
        speech_run = 0
        silence_run = 0
        speech_resume_run = 0
        barge_in_run = 0
        standby_speech_frames = 0
        noise_floor_rms = 0.0
        speaking_echo_rms = 0.0
        onset_started_at = 0.0
        barge_in_started_at = 0.0
        speech_started_at = 0.0
        seen_reset_epoch = self._capture_reset_epoch

        while self._should_keep_capturing():
            try:
                async for frame in self._audio_input.frames():
                    if not self._should_keep_capturing():
                        return

                    if seen_reset_epoch != self._capture_reset_epoch:
                        seen_reset_epoch = self._capture_reset_epoch
                        pre_roll.clear()
                        utterance_buf.clear()
                        in_speech = False
                        speech_run = 0
                        silence_run = 0
                        speech_resume_run = 0
                        barge_in_run = 0
                        standby_speech_frames = 0
                        speaking_echo_rms = 0.0

                    state = self._session.state
                    if state == "closed":
                        return

                    frame_rms = _frame_ac_rms(frame.pcm_s16le)
                    speech_rms_floor = max(
                        self._settings.min_speech_rms,
                        noise_floor_rms * self._settings.noise_floor_multiplier,
                    )

                    try:
                        if state == "standby":
                            speaking_echo_rms = 0.0
                            pre_roll.append(frame.pcm_s16le)
                            det = await self._wake_word.detect(frame)
                            if det.detected:
                                await self.start_call(reason="wake_word_detected")
                                seen_reset_epoch = self._capture_reset_epoch
                                pre_roll.clear()
                                utterance_buf.clear()
                                in_speech = False
                                speech_run = 0
                                silence_run = 0
                                speech_resume_run = 0
                                barge_in_run = 0
                                standby_speech_frames = 0
                                continue

                            standby_end_silence = min(8, self._settings.end_silence_frames)
                            prob = await self._vad.speech_probability(frame)
                            if (
                                prob >= self._settings.speech_threshold
                                and frame_rms >= speech_rms_floor
                            ):
                                if standby_speech_frames == 0:
                                    speech_started_at = self._clock()
                                    seeded = b"".join(pre_roll)
                                    utterance_buf = bytearray(
                                        seeded[: self._settings.max_utterance_bytes]
                                    )
                                standby_speech_frames += 1
                                remaining = self._settings.max_utterance_bytes - len(utterance_buf)
                                if remaining > 0:
                                    utterance_buf.extend(frame.pcm_s16le[:remaining])
                                silence_run = 0
                                if len(utterance_buf) >= _STANDBY_SPOTTER_MAX_CHUNK_BYTES:
                                    segment = bytes(utterance_buf)
                                    utterance_buf.clear()
                                    standby_speech_frames = 0
                                    self._launch_or_queue_standby_spotter(
                                        segment,
                                        frame.sample_rate_hz,
                                        speech_started_at,
                                    )
                            else:
                                if (
                                    prob < self._settings.speech_threshold
                                    and not utterance_buf
                                ):
                                    noise_floor_rms = _update_noise_floor(
                                        noise_floor_rms, frame_rms
                                    )
                                if utterance_buf:
                                    silence_run += 1
                                    if silence_run >= standby_end_silence:
                                        min_frames = self._settings.min_speech_frames
                                        if standby_speech_frames >= min_frames:
                                            segment = bytes(utterance_buf)
                                            self._launch_or_queue_standby_spotter(
                                                segment,
                                                frame.sample_rate_hz,
                                                speech_started_at,
                                            )
                                        utterance_buf.clear()
                                        standby_speech_frames = 0
                                        silence_run = 0
                            continue

                        # Active call states: monitor for <100 ms barge-in (gated in half-duplex)
                        if state in ("speaking", "thinking", "executing"):
                            if state != "speaking":
                                speaking_echo_rms = 0.0
                            if self._settings.half_duplex_turn_mode or (
                                state == "speaking" and not self._settings.allow_barge_in_during_tts
                            ):
                                if (
                                    in_speech
                                    or utterance_buf
                                    or speech_run
                                    or silence_run
                                    or speech_resume_run
                                    or barge_in_run
                                    or pre_roll
                                ):
                                    in_speech = False
                                    utterance_buf.clear()
                                    speech_run = 0
                                    silence_run = 0
                                    speech_resume_run = 0
                                    barge_in_run = 0
                                    pre_roll.clear()
                                    self._vad.reset()
                                continue

                            in_grace = (
                                self._clock() - self._speaking_started_at
                                < self._settings.barge_in_grace_seconds
                            )
                            if state == "speaking" and in_grace:
                                if not in_speech:
                                    speaking_echo_rms = _update_echo_floor(
                                        speaking_echo_rms, frame_rms
                                    )
                                    barge_in_run = 0
                                    pre_roll.clear()
                                    continue

                            if in_speech:
                                await self._execute_barge_in()
                                seen_reset_epoch = self._capture_reset_epoch
                                prob = await self._vad.speech_probability(frame)
                                remaining = self._settings.max_utterance_bytes - len(utterance_buf)
                                if remaining > 0:
                                    utterance_buf.extend(frame.pcm_s16le[:remaining])
                                if (
                                    prob >= self._settings.speech_threshold
                                    and frame_rms >= speech_rms_floor
                                ):
                                    speech_resume_run += 1
                                    if speech_resume_run >= self._settings.speech_resume_frames:
                                        silence_run = 0
                                    else:
                                        silence_run += 1
                                else:
                                    speech_resume_run = 0
                                    silence_run += 1
                                if (
                                    silence_run >= self._settings.end_silence_frames
                                    or len(utterance_buf) >= self._settings.max_utterance_bytes
                                ):
                                    segment = bytes(utterance_buf)
                                    utterance_buf.clear()
                                    in_speech = False
                                    silence_run = 0
                                    speech_run = 0
                                    speech_resume_run = 0
                                    self._vad.reset()
                                    self._enqueue_segment(
                                        segment,
                                        frame.sample_rate_hz,
                                        speech_started_at,
                                    )
                                continue

                            pre_roll.append(frame.pcm_s16le)
                            prob = await self._vad.speech_probability(frame)
                            barge_in_rms_floor = speech_rms_floor
                            if state == "speaking":
                                barge_in_rms_floor = max(
                                    speech_rms_floor,
                                    speaking_echo_rms * self._settings.barge_in_echo_multiplier,
                                )
                            if (
                                prob >= self._settings.barge_in_threshold
                                and frame_rms >= barge_in_rms_floor
                            ):
                                if barge_in_run == 0:
                                    barge_in_started_at = self._clock()
                                barge_in_run += 1
                            else:
                                barge_in_run = 0
                                if state == "speaking":
                                    speaking_echo_rms = _update_echo_floor(
                                        speaking_echo_rms, frame_rms
                                    )

                            required_barge_in_frames = (
                                self._settings.barge_in_min_frames
                                or self._settings.min_speech_frames
                            )
                            if barge_in_run >= required_barge_in_frames:
                                barge_in_run = 0
                                speaking_echo_rms = 0.0
                                await self._execute_barge_in()
                                seen_reset_epoch = self._capture_reset_epoch
                                in_speech = True
                                speech_started_at = barge_in_started_at
                                silence_run = 0
                                speech_run = 0
                                speech_resume_run = 0
                                seeded = b"".join(pre_roll)
                                pre_roll.clear()
                                utterance_buf = bytearray(
                                    seeded[: self._settings.max_utterance_bytes]
                                )
                            continue

                        # State == "listening"
                        speaking_echo_rms = 0.0
                        if not in_speech:
                            if self._ptt_hold_active:
                                in_speech = True
                                speech_started_at = self._clock()
                                speech_run = 0
                                silence_run = 0
                                speech_resume_run = 0
                                seeded = b"".join(pre_roll)
                                pre_roll.clear()
                                utterance_buf = bytearray(
                                    seeded[: self._settings.max_utterance_bytes]
                                )
                                remaining = self._settings.max_utterance_bytes - len(utterance_buf)
                                if remaining > 0:
                                    utterance_buf.extend(frame.pcm_s16le[:remaining])
                                continue
                            now = self._clock()
                            if (
                                not self._active_turn_tasks
                                and (now - self._session.last_activity_monotonic)
                                >= self._settings.call_idle_timeout_seconds
                            ):
                                pre_roll.clear()
                                speech_run = 0
                                await self.end_call(reason="call_idle_timeout")
                                seen_reset_epoch = self._capture_reset_epoch
                                continue

                            pre_roll.append(frame.pcm_s16le)
                            prob = await self._vad.speech_probability(frame)
                            if (
                                prob >= self._settings.speech_threshold
                                and frame_rms >= speech_rms_floor
                            ):
                                if speech_run == 0:
                                    onset_started_at = now
                                speech_run += 1
                            else:
                                speech_run = 0
                                if prob < self._settings.speech_threshold:
                                    noise_floor_rms = _update_noise_floor(
                                        noise_floor_rms, frame_rms
                                    )

                            if speech_run >= self._settings.min_speech_frames:
                                in_speech = True
                                speech_started_at = onset_started_at
                                speech_run = 0
                                silence_run = 0
                                speech_resume_run = 0
                                seeded = b"".join(pre_roll)
                                pre_roll.clear()
                                utterance_buf = bytearray(
                                    seeded[: self._settings.max_utterance_bytes]
                                )
                                self._session = self._session.model_copy(
                                    update={"last_activity_monotonic": now}
                                )
                        else:
                            prob = await self._vad.speech_probability(frame)
                            remaining = self._settings.max_utterance_bytes - len(utterance_buf)
                            if remaining > 0:
                                utterance_buf.extend(frame.pcm_s16le[:remaining])

                            if (
                                prob >= self._settings.speech_threshold
                                and frame_rms >= speech_rms_floor
                            ):
                                speech_resume_run += 1
                                if speech_resume_run >= self._settings.speech_resume_frames:
                                    silence_run = 0
                                else:
                                    silence_run += 1
                            else:
                                speech_resume_run = 0
                                silence_run += 1

                            if (
                                (
                                    not self._ptt_hold_active
                                    and (
                                        self._flush_ptt_hold
                                        or silence_run >= self._settings.end_silence_frames
                                    )
                                )
                                or len(utterance_buf) >= self._settings.max_utterance_bytes
                            ):
                                self._flush_ptt_hold = False
                                segment = bytes(utterance_buf)
                                utterance_buf.clear()
                                in_speech = False
                                silence_run = 0
                                speech_run = 0
                                speech_resume_run = 0
                                self._vad.reset()
                                self._enqueue_segment(
                                    segment,
                                    frame.sample_rate_hz,
                                    speech_started_at,
                                )
                    except asyncio.CancelledError:
                        raise
                    except VoicePipelineError as exc:
                        pre_roll.clear()
                        utterance_buf.clear()
                        in_speech = False
                        speech_run = 0
                        silence_run = 0
                        barge_in_run = 0
                        standby_speech_frames = 0
                        await self._publish_voice_failure(exc.code, exc.sanitized_message)
                    except Exception as exc:
                        pre_roll.clear()
                        utterance_buf.clear()
                        in_speech = False
                        speech_run = 0
                        silence_run = 0
                        barge_in_run = 0
                        standby_speech_frames = 0
                        await self._publish_voice_failure("VOICE_CAPTURE_FRAME_FAILED", str(exc))

                if not self._should_keep_capturing():
                    return
                await self._publish_voice_failure(
                    "VOICE_AUDIO_STREAM_EOF",
                    "Audio input stream ended unexpectedly; retrying capture.",
                )
            except asyncio.CancelledError:
                raise
            except VoicePipelineError as exc:
                if not self._should_keep_capturing():
                    return
                await self._publish_voice_failure(exc.code, exc.sanitized_message)
            except Exception as exc:
                if not self._should_keep_capturing():
                    return
                await self._publish_voice_failure("VOICE_AUDIO_STREAM_FAILED", str(exc))

            pre_roll.clear()
            utterance_buf.clear()
            in_speech = False
            speech_run = 0
            silence_run = 0
            barge_in_run = 0
            standby_speech_frames = 0
            if not self._should_keep_capturing():
                return
            await asyncio.sleep(self._settings.capture_retry_delay_seconds)

    def _launch_or_queue_standby_spotter(
        self,
        pcm_bytes: bytes,
        sample_rate_hz: int,
        speech_started_at: float,
    ) -> None:
        if self._standby_spotter_task is not None and not self._standby_spotter_task.done():
            self._pending_standby_followup = (pcm_bytes, sample_rate_hz, speech_started_at)
            return
        self._standby_spotter_task = asyncio.create_task(
            self._run_standby_spotter(
                pcm_bytes,
                sample_rate_hz,
                self._capture_reset_epoch,
                speech_started_at,
            )
        )

    async def _run_standby_spotter(
        self,
        pcm_bytes: bytes,
        sample_rate_hz: int,
        expected_reset_epoch: int,
        speech_started_at: float,
    ) -> None:
        this_task = asyncio.current_task()
        current_pcm = pcm_bytes
        current_rate = sample_rate_hz
        current_started = speech_started_at
        try:
            while (
                self._started
                and not self._call_open
                and self._session.state == "standby"
                and expected_reset_epoch == self._capture_reset_epoch
            ):
                try:
                    tx = await self._stt.transcribe(current_pcm, sample_rate_hz=current_rate)
                except asyncio.CancelledError:
                    raise
                except VoicePipelineError as exc:
                    await self._publish_voice_failure(exc.code, exc.sanitized_message)
                    return
                except Exception as exc:
                    await self._publish_voice_failure("VOICE_STT_FAILED", str(exc))
                    return

                if (
                    not self._started
                    or self._call_open
                    or self._session.state != "standby"
                    or expected_reset_epoch != self._capture_reset_epoch
                ):
                    return

                detected, remainder = _extract_post_wake_utterance(tx.text)
                self._log(
                    "standby_spotter_evaluated",
                    detected=detected,
                    transcript=tx.text.strip(),
                )
                if detected:
                    await self.start_call(
                        reason="wake_word_transcribed",
                        preserve_standby_followup=(not remainder),
                    )
                    if remainder and self._call_open:
                        corr_id = self._session.active_correlation_id or f"corr-{uuid.uuid4()}"
                        await self._dispatch_transcribed_segment(
                            tx=tx.model_copy(update={"text": remainder}),
                            corr_id=corr_id,
                            speech_started_at=current_started,
                            segment_epoch=self._call_epoch,
                        )
                    return

                next_item = self._pending_standby_followup
                self._pending_standby_followup = None
                if next_item is None:
                    return
                current_pcm, current_rate, current_started = next_item
        finally:
            if self._standby_spotter_task is this_task:
                self._standby_spotter_task = None

    def _enqueue_segment(
        self,
        segment: bytes,
        sample_rate_hz: int,
        speech_started_at: float,
    ) -> None:
        if not self._call_open or len(segment) < 2:
            return
        if self._segment_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._segment_queue.get_nowait()
        self._segment_queue.put_nowait(
            (segment, sample_rate_hz, speech_started_at, self._call_epoch)
        )

    async def _execute_barge_in(self) -> None:
        """Stop TTS playback (<100 ms), clear queued speech, and abort active turn unless held."""
        corr_id = f"corr-{uuid.uuid4()}"
        self._tts_queue.clear()
        await self._cancel_active_tts_playback()

        if not self._has_active_pending_approval():
            for turn_t in list(self._active_turn_tasks):
                turn_t.cancel()
            await self._orchestrator.barge_in()

        if self._call_open:
            await self._transition_state(
                "listening",
                reason="user_barge_in",
                correlation_id=corr_id,
            )

    async def _segment_worker(self) -> None:
        while True:
            item = await self._segment_queue.get()
            if item is None:
                break
            segment_bytes, sample_rate_hz, speech_started_at, segment_epoch = item
            if not self._call_open or segment_epoch != self._call_epoch:
                continue

            corr_id = f"corr-{uuid.uuid4()}"
            stt_task = asyncio.create_task(
                self._stt.transcribe(segment_bytes, sample_rate_hz=sample_rate_hz)
            )
            self._current_stt_task = stt_task
            try:
                tx = await stt_task
            except asyncio.CancelledError:
                if not self._started:
                    raise
                continue
            except VoicePipelineError as exc:
                await self._publish_voice_failure(
                    exc.code,
                    exc.sanitized_message,
                    correlation_id=corr_id,
                )
                continue
            except Exception as exc:
                await self._publish_voice_failure(
                    "VOICE_STT_FAILED",
                    str(exc),
                    correlation_id=corr_id,
                )
                continue
            finally:
                if self._current_stt_task is stt_task:
                    self._current_stt_task = None

            if not self._call_open or segment_epoch != self._call_epoch:
                continue

            await self._dispatch_transcribed_segment(
                tx=tx,
                corr_id=corr_id,
                speech_started_at=speech_started_at,
                segment_epoch=segment_epoch,
            )

    async def _dispatch_transcribed_segment(
        self,
        *,
        tx: TranscriptionResult,
        corr_id: str,
        speech_started_at: float,
        segment_epoch: int,
    ) -> None:
        raw_text = tx.text.strip()
        if not raw_text:
            return

        sanitized_text = self._redact(raw_text)
        await self._event_bus.publish(
            VoiceTranscriptionEvent(
                call_id=self._session.call_id,
                correlation_id=corr_id,
                sanitized_text=sanitized_text,
                backend_device=tx.backend_device,
                duration_ms=tx.duration_ms,
            )
        )

        self._prune_expired_approvals()
        head_approval = self._pending_approvals[0] if self._pending_approvals else None

        # F2 Anti-TOCTOU: Speech started BEFORE the earliest pending approval was requested
        # must NEVER authorize or reject that approval, nor dispatch a new prompt while held.
        if head_approval is not None and speech_started_at < head_approval.requested_at:
            await self._event_bus.publish(
                VoiceMilestoneEvent(
                    correlation_id=corr_id,
                    summary_text=_HOLD_REMINDER_TEXT,
                    priority="interrupt",
                )
            )
            return

        has_pending = head_approval is not None
        intent = classify_voice_control_intent(
            sanitized_text,
            has_pending_approval=has_pending,
        )

        if intent.kind == "ignore_empty":
            return

        if intent.kind in ("approve_command", "reject_command"):
            if head_approval is not None:
                if (
                    self._pending_approvals
                    and self._pending_approvals[0].audit_id == head_approval.audit_id
                ):
                    self._pending_approvals.popleft()
                if (
                    self._active_turn_tasks
                    and not self._has_active_pending_approval()
                    and self._call_open
                ):
                    await self._transition_state(
                        "executing",
                        reason="approval_resolved_resuming_turn",
                        correlation_id=corr_id,
                    )
                try:
                    await self._orchestrator.resolve_command_approval(
                        head_approval.audit_id,
                        approved=(intent.kind == "approve_command"),
                        resolved_by="user_voice",
                    )
                except asyncio.CancelledError:
                    raise
                except NovaDomainError as exc:
                    await self._publish_voice_failure(
                        exc.code,
                        exc.sanitized_message,
                        correlation_id=corr_id,
                    )
                except Exception as exc:
                    await self._publish_voice_failure(
                        "VOICE_APPROVAL_FAILED",
                        str(exc),
                        correlation_id=corr_id,
                    )
            return

        if intent.kind == "hold_pending_approval":
            await self._event_bus.publish(
                VoiceMilestoneEvent(
                    correlation_id=corr_id,
                    summary_text=_HOLD_REMINDER_TEXT,
                    priority="interrupt",
                )
            )
            return

        if intent.kind == "select_workspace" and intent.workspace_query:
            try:
                await self._orchestrator.select_workspace(
                    intent.workspace_query,
                    mode="voice_fuzzy",
                    correlation_id=corr_id,
                )
            except asyncio.CancelledError:
                raise
            except NovaDomainError as exc:
                await self._publish_voice_failure(
                    exc.code,
                    exc.sanitized_message,
                    correlation_id=corr_id,
                )
            except Exception as exc:
                await self._publish_voice_failure(
                    "WORKSPACE_SELECTION_FAILED",
                    str(exc),
                    correlation_id=corr_id,
                )
            return

        if intent.kind == "end_call":
            await self.end_call(reason="voice_hangup_command")
            return

        if (
            intent.kind == "user_utterance"
            and intent.utterance_text
            and self._call_open
            and segment_epoch == self._call_epoch
        ):
            await self._transition_state(
                "thinking",
                reason="utterance_dispatched",
                correlation_id=corr_id,
            )
            self._session = self._session.model_copy(
                update={"turn_count": self._session.turn_count + 1}
            )
            turn_task = asyncio.create_task(
                self._run_orchestrator_turn(intent.utterance_text, corr_id, segment_epoch)
            )
            self._active_turn_tasks.add(turn_task)
            turn_task.add_done_callback(self._active_turn_tasks.discard)

    async def _invoke_orchestrator_utterance(self, utterance: str, corr_id: str) -> None:
        sig = inspect.signature(self._orchestrator.handle_user_utterance)
        kwargs: dict[str, Any] = {"correlation_id": corr_id}
        if "origin" in sig.parameters:
            kwargs["origin"] = "voice"
        await self._orchestrator.handle_user_utterance(utterance, **kwargs)

    async def _run_orchestrator_turn(
        self,
        utterance: str,
        corr_id: str,
        expected_call_epoch: int,
    ) -> None:
        this_task = asyncio.current_task()
        try:
            if not self._call_open or expected_call_epoch != self._call_epoch:
                return
            if self._session.state == "thinking":
                await self._transition_state(
                    "executing",
                    reason="orchestrator_turn_executing",
                    correlation_id=corr_id,
                )
            await self._invoke_orchestrator_utterance(utterance, corr_id)
        except asyncio.CancelledError:
            raise
        except NovaDomainError as exc:
            await self._publish_voice_failure(
                exc.code,
                exc.sanitized_message,
                correlation_id=corr_id,
            )
        except Exception as exc:
            await self._publish_voice_failure(
                "ORCHESTRATOR_UNEXPECTED_FAILURE",
                str(exc),
                correlation_id=corr_id,
            )
        finally:
            if this_task is not None:
                self._active_turn_tasks.discard(this_task)
            tts_idle = (
                not self._audio_output.is_playing
                and not self._tts_queue
                and (
                    self._current_tts_item_task is None
                    or self._current_tts_item_task.done()
                )
            )
            if (
                self._call_open
                and self._session.state in ("thinking", "executing")
                and not self._active_turn_tasks
                and tts_idle
                and (
                    expected_call_epoch == self._call_epoch
                    or self._settings.half_duplex_turn_mode
                )
            ):
                if (
                    (self._single_turn_active or self._settings.half_duplex_turn_mode)
                    and not self._has_active_pending_approval()
                    and (self._session.turn_count > 0 or self._settings.half_duplex_turn_mode)
                ):
                    reason = (
                        "half_duplex_turn_completed"
                        if self._settings.half_duplex_turn_mode
                        else "ptt_single_turn_completed"
                    )
                    await self.end_call(reason=reason)
                else:
                    await self._transition_state(
                        "listening",
                        reason="turn_completed",
                        correlation_id=corr_id,
                    )

    async def _synthesize_and_play_item(
        self,
        milestone: _SpokenQueueItem,
        expected_epoch: int,
    ) -> None:
        synth = await self._tts.synthesize(_spoken_item_text(milestone))
        if (
            expected_epoch != self._tts_epoch
            or not self._call_open
            or self._session.state == "closed"
        ):
            return
        await self._transition_state(
            "speaking",
            reason="tts_milestone_playback",
            correlation_id=milestone.correlation_id,
        )
        await self._audio_output.play_pcm(synth)

    async def _tts_worker(self) -> None:
        while True:
            if not self._tts_queue:
                self._tts_wakeup.clear()
                if not self._tts_queue:
                    await self._tts_wakeup.wait()
            if not self._tts_queue:
                continue

            milestone = self._tts_queue.popleft()
            if self._session.state == "closed" or not self._call_open:
                continue

            item_epoch = self._tts_epoch
            self._current_tts_priority = milestone.priority
            item_task = asyncio.create_task(
                self._synthesize_and_play_item(milestone, item_epoch)
            )
            self._current_tts_item_task = item_task
            preempted = False
            try:
                await item_task
            except asyncio.CancelledError:
                if not self._started:
                    raise
                preempted = True
            except VoicePipelineError as exc:
                await self._publish_voice_failure(
                    exc.code,
                    exc.sanitized_message,
                    correlation_id=milestone.correlation_id,
                )
            except Exception as exc:
                await self._publish_voice_failure(
                    "VOICE_TTS_FAILED",
                    str(exc),
                    correlation_id=milestone.correlation_id,
                )
            finally:
                if self._current_tts_item_task is item_task:
                    self._current_tts_item_task = None
                self._current_tts_priority = None

            if (
                not preempted
                and item_epoch == self._tts_epoch
                and not self._tts_queue
                and self._call_open
            ):
                if self._session.state == "speaking":
                    if self._settings.half_duplex_turn_mode and self._has_active_pending_approval():
                        await self.end_call(reason="half_duplex_awaiting_approval")
                    elif (
                        self._settings.half_duplex_turn_mode
                        and not self._active_turn_tasks
                        and not self._has_active_pending_approval()
                    ):
                        await self.end_call(reason="half_duplex_turn_completed")
                    elif (
                        self._single_turn_active
                        and self._session.turn_count > 0
                        and not self._active_turn_tasks
                        and not self._has_active_pending_approval()
                    ):
                        await self.end_call(reason="ptt_single_turn_completed")
                    else:
                        target = self._post_speech_target_state()
                        await self._transition_state(
                            target,
                            reason="tts_playback_completed",
                            correlation_id=milestone.correlation_id,
                        )
                elif (
                    self._session.state in ("thinking", "executing")
                    and not self._active_turn_tasks
                ):
                    if (
                        (self._single_turn_active or self._settings.half_duplex_turn_mode)
                        and not self._has_active_pending_approval()
                        and (self._session.turn_count > 0 or self._settings.half_duplex_turn_mode)
                    ):
                        reason = (
                            "half_duplex_turn_completed"
                            if self._settings.half_duplex_turn_mode
                            else "ptt_single_turn_completed"
                        )
                        await self.end_call(reason=reason)
                    else:
                        await self._transition_state(
                            "listening",
                            reason="turn_completed",
                            correlation_id=milestone.correlation_id,
                        )
