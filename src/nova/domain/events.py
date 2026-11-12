"""Domain events and asynchronous OrchestratorEventBus (NOVA-001..NOVA-003 OP-4)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.domain.errors import redact_sensitive_text
from nova.domain.models import (
    MAX_SPOKEN_WORDS,
    AuditRiskLevel,
    CallSessionState,
    CommandSource,
    DecisionVerdict,
    InferenceDevice,
    WorkspaceQueryMode,
    utc_now_iso,
    validate_spoken_word_budget,
)

MAX_VOICE_MILESTONE_WORDS = MAX_SPOKEN_WORDS
_bus_logger = logging.getLogger("nova.event_bus")


class WorkspaceChangedEvent(BaseModel):
    """Published whenever the active project directory changes."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    workspace_name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
    selected_via: WorkspaceQueryMode = "exact_path"
    timestamp: str = Field(default_factory=utc_now_iso)


class ChatStreamEvent(BaseModel):
    """Detailed progress fragment destined for the chat panel."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    delta: str
    source: Literal["tier1_pi", "tier2_runner"]
    step_type: str = "agent_response"
    timestamp: str = Field(default_factory=utc_now_iso)


class VoiceMilestoneEvent(BaseModel):
    """Concise <=25-word spoken milestone destined for the local TTS engine."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    summary_text: str = Field(min_length=1)
    priority: Literal["normal", "interrupt"] = "normal"
    timestamp: str = Field(default_factory=utc_now_iso)

    @field_validator("summary_text")
    @classmethod
    def _validate_word_budget(cls, value: str) -> str:
        return validate_spoken_word_budget(value)


class RunnerFailureEvent(BaseModel):
    """Sanitized failure event emitted when Tier 1, Tier 2, or Voice pipeline fails."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    error_code: str = Field(min_length=1)
    sanitized_message: str = Field(min_length=1)
    recoverable: bool = True
    timestamp: str = Field(default_factory=utc_now_iso)


class CommandBlockedEvent(BaseModel):
    """Published when Layer 1 hard-blocks a catastrophic host command."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    audit_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    source: CommandSource
    sanitized_command: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    timestamp: str = Field(default_factory=utc_now_iso)


class CommandApprovalRequestedEvent(BaseModel):
    """Published when Layer 2 holds a dubious command awaiting explicit human decision."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    audit_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    source: CommandSource
    sanitized_command: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    risk_level: AuditRiskLevel
    spoken_summary: str = Field(min_length=1)
    technical_explanation: str = Field(min_length=1)
    expires_in_seconds: float = Field(gt=0.0)
    timestamp: str = Field(default_factory=utc_now_iso)

    @field_validator("spoken_summary")
    @classmethod
    def _validate_spoken_summary(cls, value: str) -> str:
        return validate_spoken_word_budget(value)


class CommandApprovalResolvedEvent(BaseModel):
    """Published when a held command approval is approved, rejected, or times out."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    audit_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    approved: bool
    verdict: DecisionVerdict
    resolved_by: str = Field(min_length=1)
    timestamp: str = Field(default_factory=utc_now_iso)


class CallStateChangedEvent(BaseModel):
    """Published whenever CallSession transitions across standby/listening/speaking/etc."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    call_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    previous_state: CallSessionState
    state: CallSessionState
    reason: str = Field(min_length=1)
    timestamp: str = Field(default_factory=utc_now_iso)


class VoiceTranscriptionEvent(BaseModel):
    """Published when local SpeechToTextPort completes a sanitized Spanish transcription."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    call_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    sanitized_text: str = Field(min_length=1)
    backend_device: InferenceDevice
    duration_ms: float = Field(ge=0.0)
    timestamp: str = Field(default_factory=utc_now_iso)


OrchestratorEvent = (
    WorkspaceChangedEvent
    | ChatStreamEvent
    | VoiceMilestoneEvent
    | RunnerFailureEvent
    | CommandBlockedEvent
    | CommandApprovalRequestedEvent
    | CommandApprovalResolvedEvent
    | CallStateChangedEvent
    | VoiceTranscriptionEvent
)
EventSubscriber = Callable[[OrchestratorEvent], Awaitable[None]]


class OrchestratorEventBus:
    """In-memory asynchronous pub/sub bus with per-subscriber fault isolation."""

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []

    def subscribe(self, subscriber: EventSubscriber) -> None:
        """Register an async event subscriber."""
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        """Idempotently remove a previously registered async event subscriber."""
        self._subscribers = [sub for sub in self._subscribers if sub != subscriber]

    async def publish(self, event: OrchestratorEvent) -> None:
        """Deliver event sequentially to all subscribers, isolating individual Exceptions."""
        for subscriber in list(self._subscribers):
            try:
                await subscriber(event)
            except Exception as exc:
                safe_error = redact_sensitive_text(str(exc))
                _bus_logger.error(
                    json.dumps(
                        {
                            "error": safe_error,
                            "event": "event_bus_subscriber_error",
                            "event_type": type(event).__name__,
                            "subscriber": getattr(subscriber, "__qualname__", repr(subscriber)),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
