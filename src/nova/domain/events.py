"""Domain events and asynchronous OrchestratorEventBus (OP-4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.domain.models import utc_now_iso

MAX_VOICE_MILESTONE_WORDS = 25


class WorkspaceChangedEvent(BaseModel):
    """Published whenever the active project directory changes."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    workspace_name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
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
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("summary_text cannot be empty")
        word_count = len(cleaned.split(" "))
        if word_count > MAX_VOICE_MILESTONE_WORDS:
            raise ValueError(
                f"summary_text exceeds {MAX_VOICE_MILESTONE_WORDS} words (got {word_count})"
            )
        return cleaned


class RunnerFailureEvent(BaseModel):
    """Sanitized failure event emitted when Tier 1 or Tier 2 fails."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    error_code: str = Field(min_length=1)
    sanitized_message: str = Field(min_length=1)
    recoverable: bool = True
    timestamp: str = Field(default_factory=utc_now_iso)


OrchestratorEvent = (
    WorkspaceChangedEvent | ChatStreamEvent | VoiceMilestoneEvent | RunnerFailureEvent
)
EventSubscriber = Callable[[OrchestratorEvent], Awaitable[None]]


class OrchestratorEventBus:
    """In-memory asynchronous pub/sub bus for Nova domain events."""

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []

    def subscribe(self, subscriber: EventSubscriber) -> None:
        """Register an async event subscriber."""
        self._subscribers.append(subscriber)

    async def publish(self, event: OrchestratorEvent) -> None:
        """Deliver event sequentially to all registered async subscribers."""
        for subscriber in list(self._subscribers):
            await subscriber(event)
