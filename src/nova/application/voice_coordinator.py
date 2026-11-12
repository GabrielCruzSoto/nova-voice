"""Asynchronous continuous CallSession coordinator and <100 ms barge-in engine (NOVA-003 OP-4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Literal, Protocol

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
    VoiceTranscriptionEvent,
)
from nova.domain.models import (
    CallSession,
    CallSessionState,
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


class VoiceCoordinatorSettings(BaseModel):
    """Bounded memory, threshold, and timeout configuration for VoiceCallCoordinator."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pre_roll_frames: int = Field(default=10, ge=1, le=50)
    speech_threshold: float = Field(default=0.50, gt=0.0, le=1.0)
    barge_in_threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    min_speech_frames: int = Field(default=5, ge=1, le=50)
    end_silence_frames: int = Field(default=19, ge=1, le=200)
    max_utterance_bytes: int = Field(default=960_000, ge=2048, le=3_840_000)
    max_tts_queue: int = Field(default=16, ge=1, le=64)
    call_idle_timeout_seconds: float = Field(default=180.0, gt=0.0, le=3600.0)
    allow_barge_in_during_tts: bool = True


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

        self._tts_queue: deque[VoiceMilestoneEvent] = deque()
        self._tts_wakeup = asyncio.Event()
        self._current_tts_priority: Literal["normal", "interrupt"] | None = None
        self._current_tts_play_task: asyncio.Task[None] | None = None

        self._segment_queue: asyncio.Queue[tuple[bytes, int] | None] = asyncio.Queue(maxsize=8)
        self._capture_task: asyncio.Task[None] | None = None
        self._segment_worker_task: asyncio.Task[None] | None = None
        self._tts_worker_task: asyncio.Task[None] | None = None
        self._active_turn_tasks: set[asyncio.Task[None]] = set()

        self._active_approval_id: str | None = None
        self._active_approval_deadline: float = 0.0
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

    def _has_active_pending_approval(self) -> bool:
        if self._active_approval_id is None:
            return False
        if self._clock() >= self._active_approval_deadline:
            self._active_approval_id = None
            return False
        return True

    async def start(self) -> None:
        """Subscribe to OrchestratorEventBus and launch capture, STT, and TTS workers."""
        if self._started:
            return
        self._started = True
        self._event_bus.subscribe(self._on_event)
        self._capture_task = asyncio.create_task(self._capture_loop())
        self._segment_worker_task = asyncio.create_task(self._segment_worker())
        self._tts_worker_task = asyncio.create_task(self._tts_worker())

    async def start_call(self, reason: str = "manual_call_start") -> None:
        """Explicitly open a continuous CallSession in 'listening' state."""
        if self._session.state == "closed":
            return
        self._call_open = True
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

    async def end_call(self, reason: str = "manual_call_end") -> None:
        """Return an open CallSession back to 'standby' hands-free wake-word mode."""
        if self._session.state in ("standby", "closed"):
            return
        self._call_open = False
        self._tts_queue.clear()
        await self._cancel_active_tts_playback()
        self._vad.reset()
        self._wake_word.reset()
        corr_id = self._session.active_correlation_id or f"corr-{uuid.uuid4()}"
        await self._transition_state("standby", reason=reason, correlation_id=corr_id)

    async def shutdown(self) -> None:
        """Unsubscribe from bus, stop audio streams, cancel workers, and close CallSession."""
        self._event_bus.unsubscribe(self._on_event)
        self._call_open = False
        self._tts_queue.clear()
        self._started = False

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
        now = self._clock()
        if prev == new_state:
            self._session = self._session.model_copy(
                update={
                    "last_activity_monotonic": now,
                    "active_correlation_id": correlation_id,
                }
            )
            return

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
            self._active_approval_id = event.audit_id
            self._active_approval_deadline = self._clock() + event.expires_in_seconds
            return

        if isinstance(event, CommandApprovalResolvedEvent):
            if self._active_approval_id == event.audit_id:
                self._active_approval_id = None
            return

        if isinstance(event, VoiceMilestoneEvent):
            if self._session.state == "closed":
                return
            if event.priority == "interrupt":
                kept = [ev for ev in self._tts_queue if ev.priority == "interrupt"]
                self._tts_queue = deque(kept[-max(0, self._settings.max_tts_queue - 1) :])
                self._tts_queue.append(event)
                if (
                    self._current_tts_play_task is not None
                    and not self._current_tts_play_task.done()
                    and self._current_tts_priority == "normal"
                ):
                    await self._cancel_active_tts_playback()
                self._tts_wakeup.set()
            else:
                if len(self._tts_queue) >= self._settings.max_tts_queue:
                    # Evict oldest normal item if present; otherwise drop incoming normal milestone
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
        play_task = self._current_tts_play_task
        if play_task is not None and not play_task.done():
            play_task.cancel()
        await self._audio_output.stop_playback()
        if play_task is not None and not play_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await play_task

    async def _capture_loop(self) -> None:
        pre_roll: deque[bytes] = deque(maxlen=self._settings.pre_roll_frames)
        utterance_buf = bytearray()
        in_speech = False
        speech_run = 0
        silence_run = 0
        barge_in_run = 0

        try:
            async for frame in self._audio_input.frames():
                state = self._session.state
                if state == "closed":
                    break

                if state == "standby":
                    pre_roll.append(frame.pcm_s16le)
                    det = await self._wake_word.detect(frame)
                    if det.detected:
                        pre_roll.clear()
                        utterance_buf.clear()
                        in_speech = False
                        speech_run = 0
                        silence_run = 0
                        barge_in_run = 0
                        self._vad.reset()
                        await self.start_call(reason="wake_word_detected")
                    continue

                # Active call states: speaking, thinking, executing -> monitor for <100 ms barge-in
                if state in ("speaking", "thinking", "executing"):
                    if state == "speaking" and not self._settings.allow_barge_in_during_tts:
                        continue
                    pre_roll.append(frame.pcm_s16le)
                    prob = await self._vad.speech_probability(frame)
                    if prob >= self._settings.barge_in_threshold:
                        barge_in_run += 1
                    else:
                        barge_in_run = 0

                    if barge_in_run >= self._settings.min_speech_frames:
                        barge_in_run = 0
                        await self._execute_barge_in()
                        in_speech = True
                        silence_run = 0
                        speech_run = 0
                        seeded = b"".join(pre_roll)
                        pre_roll.clear()
                        utterance_buf = bytearray(seeded[: self._settings.max_utterance_bytes])
                    continue

                # State == "listening"
                if not in_speech:
                    now = self._clock()
                    if (
                        not self._active_turn_tasks
                        and (now - self._session.last_activity_monotonic)
                        >= self._settings.call_idle_timeout_seconds
                    ):
                        pre_roll.clear()
                        speech_run = 0
                        await self.end_call(reason="call_idle_timeout")
                        continue

                    pre_roll.append(frame.pcm_s16le)
                    prob = await self._vad.speech_probability(frame)
                    if prob >= self._settings.speech_threshold:
                        speech_run += 1
                    else:
                        speech_run = 0

                    if speech_run >= self._settings.min_speech_frames:
                        in_speech = True
                        speech_run = 0
                        silence_run = 0
                        seeded = b"".join(pre_roll)
                        pre_roll.clear()
                        utterance_buf = bytearray(seeded[: self._settings.max_utterance_bytes])
                        self._session = self._session.model_copy(
                            update={"last_activity_monotonic": now}
                        )
                else:
                    prob = await self._vad.speech_probability(frame)
                    remaining = self._settings.max_utterance_bytes - len(utterance_buf)
                    if remaining > 0:
                        utterance_buf.extend(frame.pcm_s16le[:remaining])

                    if prob >= self._settings.speech_threshold:
                        silence_run = 0
                    else:
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
                        self._vad.reset()
                        self._enqueue_segment(segment, frame.sample_rate_hz)
        except asyncio.CancelledError:
            raise
        except VoicePipelineError as exc:
            await self._publish_voice_failure(exc.code, exc.sanitized_message)
        except Exception as exc:
            await self._publish_voice_failure("VOICE_AUDIO_STREAM_FAILED", str(exc))

    def _enqueue_segment(self, segment: bytes, sample_rate_hz: int) -> None:
        if len(segment) < 2:
            return
        if self._segment_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = self._segment_queue.get_nowait()
        self._segment_queue.put_nowait((segment, sample_rate_hz))

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
            segment_bytes, sample_rate_hz = item
            corr_id = f"corr-{uuid.uuid4()}"
            try:
                tx = await self._stt.transcribe(segment_bytes, sample_rate_hz=sample_rate_hz)
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

            raw_text = tx.text.strip()
            if not raw_text:
                continue

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

            has_pending = self._has_active_pending_approval()
            intent = classify_voice_control_intent(
                sanitized_text,
                has_pending_approval=has_pending,
            )

            if intent.kind == "ignore_empty":
                continue

            if intent.kind in ("approve_command", "reject_command"):
                audit_id = self._active_approval_id
                if audit_id is not None:
                    self._active_approval_id = None
                    try:
                        await self._orchestrator.resolve_command_approval(
                            audit_id,
                            approved=(intent.kind == "approve_command"),
                            resolved_by="user_voice",
                        )
                    except NovaDomainError as exc:
                        await self._publish_voice_failure(
                            exc.code,
                            exc.sanitized_message,
                            correlation_id=corr_id,
                        )
                continue

            if intent.kind == "hold_pending_approval":
                await self._event_bus.publish(
                    VoiceMilestoneEvent(
                        correlation_id=corr_id,
                        summary_text=_HOLD_REMINDER_TEXT,
                        priority="interrupt",
                    )
                )
                continue

            if intent.kind == "select_workspace" and intent.workspace_query:
                try:
                    await self._orchestrator.select_workspace(
                        intent.workspace_query,
                        mode="voice_fuzzy",
                        correlation_id=corr_id,
                    )
                except NovaDomainError as exc:
                    await self._publish_voice_failure(
                        exc.code,
                        exc.sanitized_message,
                        correlation_id=corr_id,
                    )
                continue

            if intent.kind == "end_call":
                await self.end_call(reason="voice_hangup_command")
                continue

            if intent.kind == "user_utterance" and intent.utterance_text:
                await self._transition_state(
                    "thinking",
                    reason="utterance_dispatched",
                    correlation_id=corr_id,
                )
                self._session = self._session.model_copy(
                    update={"turn_count": self._session.turn_count + 1}
                )
                turn_task = asyncio.create_task(
                    self._run_orchestrator_turn(intent.utterance_text, corr_id)
                )
                self._active_turn_tasks.add(turn_task)
                turn_task.add_done_callback(self._active_turn_tasks.discard)

    async def _run_orchestrator_turn(self, utterance: str, corr_id: str) -> None:
        try:
            if self._session.state == "thinking":
                await self._transition_state(
                    "executing",
                    reason="orchestrator_turn_executing",
                    correlation_id=corr_id,
                )
            await self._orchestrator.handle_user_utterance(utterance, correlation_id=corr_id)
        except asyncio.CancelledError:
            raise
        except NovaDomainError as exc:
            await self._publish_voice_failure(
                exc.code,
                exc.sanitized_message,
                correlation_id=corr_id,
            )
        finally:
            if (
                self._call_open
                and self._session.state in ("thinking", "executing")
                and not self._audio_output.is_playing
                and not self._tts_queue
            ):
                await self._transition_state(
                    "listening",
                    reason="turn_completed",
                    correlation_id=corr_id,
                )

    async def _tts_worker(self) -> None:
        while True:
            if not self._tts_queue:
                self._tts_wakeup.clear()
                if not self._tts_queue:
                    await self._tts_wakeup.wait()
            if not self._tts_queue:
                continue

            milestone = self._tts_queue.popleft()
            if self._session.state == "closed":
                break

            self._current_tts_priority = milestone.priority
            try:
                synth = await self._tts.synthesize(milestone.summary_text)
            except VoicePipelineError as exc:
                self._current_tts_priority = None
                await self._publish_voice_failure(
                    exc.code,
                    exc.sanitized_message,
                    correlation_id=milestone.correlation_id,
                )
                continue
            except Exception as exc:
                self._current_tts_priority = None
                await self._publish_voice_failure(
                    "VOICE_TTS_FAILED",
                    str(exc),
                    correlation_id=milestone.correlation_id,
                )
                continue

            await self._transition_state(
                "speaking",
                reason="tts_milestone_playback",
                correlation_id=milestone.correlation_id,
            )
            play_task = asyncio.create_task(self._audio_output.play_pcm(synth))
            self._current_tts_play_task = play_task
            try:
                await play_task
            except asyncio.CancelledError:
                if not self._started:
                    raise
                # Preempted by interrupt milestone or user barge-in; loop continues cleanly
            except VoicePipelineError as exc:
                await self._publish_voice_failure(
                    exc.code,
                    exc.sanitized_message,
                    correlation_id=milestone.correlation_id,
                )
            except Exception as exc:
                await self._publish_voice_failure(
                    "VOICE_AUDIO_STREAM_FAILED",
                    str(exc),
                    correlation_id=milestone.correlation_id,
                )
            finally:
                if self._current_tts_play_task is play_task:
                    self._current_tts_play_task = None
                self._current_tts_priority = None

            if not self._tts_queue and self._session.state == "speaking":
                target: CallSessionState = "listening" if self._call_open else "standby"
                await self._transition_state(
                    target,
                    reason="tts_playback_completed",
                    correlation_id=milestone.correlation_id,
                )
