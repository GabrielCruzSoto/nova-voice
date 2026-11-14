"""Bounded O(1) reactive UI state projection and SSE fanout hub (NOVA-004 OP-1 & OP-2)."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import DesktopUiError, redact_sensitive_text
from nova.domain.events import (
    CallStateChangedEvent,
    ChatStreamEvent,
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    CommandBlockedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    SettingsUpdatedEvent,
    UiPanelToggleRequestedEvent,
    VoiceMilestoneEvent,
    VoiceTranscriptionEvent,
    WorkspaceChangedEvent,
)
from nova.domain.models import (
    AuditRiskLevel,
    CallSession,
    CallSessionState,
    CommandSource,
    WorkspaceContext,
    WorkspaceQueryMode,
    utc_now_iso,
)

MAX_SINGLE_MESSAGE_CHARS = 65_536

UiEventType = Literal[
    "workspace_changed",
    "chat_stream",
    "voice_milestone",
    "runner_failure",
    "command_blocked",
    "command_approval_requested",
    "command_approval_resolved",
    "call_state_changed",
    "voice_transcription",
    "settings_updated",
    "panel_toggle_requested",
]
UiMessageRole = Literal["user", "assistant", "system", "security"]
UiMessageModality = Literal[
    "text",
    "voice",
    "tier1_pi",
    "tier2_runner",
    "milestone",
    "error",
    "blocked",
]


class UiCallSessionView(BaseModel):
    """Wire DTO for CallSession inside UiStateSnapshot (OP-1)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    call_id: str = Field(min_length=1)
    state: CallSessionState
    turn_count: int = Field(ge=0)
    active_correlation_id: str | None = None


class UiWorkspaceView(BaseModel):
    """Wire DTO for active workspace inside UiStateSnapshot (OP-1 & OP-4)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    display_name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
    selected_via: WorkspaceQueryMode = "exact_path"


class UiWorkspaceOption(BaseModel):
    """Wire DTO for selectable workspace inside allowed_roots catalog (OP-1)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    display_name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)


class UiApprovalItem(BaseModel):
    """Wire DTO for a pending SecurityGatekeeper human approval card (OP-1 & OP-5)."""

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
    timestamp: str = Field(min_length=1)


class UiChatMessage(BaseModel):
    """Wire DTO for a bounded chat/voice timeline item (OP-1 & AC-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    message_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    role: UiMessageRole
    modality: UiMessageModality
    step_type: str = Field(min_length=1)
    content: str
    timestamp: str = Field(min_length=1)
    streaming: bool = False


class UiStateSnapshot(BaseModel):
    """Complete bootstrap and resync payload for GET /api/state and SSE snapshot (OP-1)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.3.0"] = "1.3.0"
    csrf_token: str
    voice_enabled: bool
    call_session: UiCallSessionView
    active_workspace: UiWorkspaceView | None = None
    available_workspaces: list[UiWorkspaceOption]
    pending_approvals: list[UiApprovalItem]
    messages: list[UiChatMessage]


class UiEventEnvelope(BaseModel):
    """Typed SSE event frame emitted on GET /api/events (OP-2)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    seq: int = Field(ge=1)
    event_type: UiEventType
    correlation_id: str = Field(min_length=1)
    timestamp: str = Field(min_length=1)
    payload: dict[str, Any]


@dataclass(slots=True)
class _MutableChatRecord:
    message_id: str
    correlation_id: str
    role: UiMessageRole
    modality: UiMessageModality
    step_type: str
    content: str
    timestamp: str
    streaming: bool
    stream_key: tuple[str, str, str] | None = None

    def to_dto(self) -> UiChatMessage:
        return UiChatMessage(
            message_id=self.message_id,
            correlation_id=self.correlation_id,
            role=self.role,
            modality=self.modality,
            step_type=self.step_type,
            content=self.content,
            timestamp=self.timestamp,
            streaming=self.streaming,
        )


@dataclass(slots=True, frozen=True)
class SseOutboundItem:
    """Internal queue item dispatched to each connected SSE client."""

    kind: Literal["nova_event", "resync_snapshot"]
    envelope: UiEventEnvelope | None = None


@dataclass(slots=True, eq=False)
class SseSubscription:
    """Per-client bounded SSE queue handle."""

    subscription_id: str
    queue: asyncio.Queue[SseOutboundItem]


class UiHubSettings(BaseModel):
    """Memory and concurrency limits for UiStateHub (NFR-3 & contract.md §5)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    max_messages: int = Field(default=200, ge=1, le=1000)
    max_pending_approvals: int = Field(default=64, ge=1, le=256)
    max_sse_queue: int = Field(default=128, ge=2, le=512)
    max_sse_subscribers: int = Field(default=8, ge=1, le=64)


class UiStateHub:
    """Projects OrchestratorEventBus events into bounded O(1) RAM state and SSE client queues."""

    def __init__(
        self,
        *,
        event_bus: OrchestratorEventBus,
        settings: UiHubSettings | None = None,
        extra_secrets_provider: Callable[[], tuple[str, ...]] | None = None,
        available_workspaces_provider: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None,
        call_session_provider: Callable[[], CallSession] | None = None,
        initial_workspace: WorkspaceContext | None = None,
        initial_call_session: CallSession | None = None,
        voice_enabled: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._event_bus = event_bus
        self._settings = settings or UiHubSettings()
        self._extra_secrets_provider = extra_secrets_provider
        self._available_workspaces_provider = available_workspaces_provider
        self._call_session_provider = call_session_provider
        self._voice_enabled = voice_enabled
        self._clock = clock

        default_call = initial_call_session or CallSession(
            call_id=f"call-{uuid.uuid4()}",
            state="standby",
            last_activity_monotonic=self._clock(),
            turn_count=0,
        )
        self._call_view = UiCallSessionView(
            call_id=default_call.call_id,
            state=default_call.state,
            turn_count=default_call.turn_count,
            active_correlation_id=default_call.active_correlation_id,
        )
        self._active_workspace: UiWorkspaceView | None = (
            UiWorkspaceView(
                display_name=initial_workspace.name,
                canonical_path=initial_workspace.canonical_path,
                selected_via=initial_workspace.selected_via,
            )
            if initial_workspace is not None
            else None
        )

        self._messages: deque[_MutableChatRecord] = deque()
        self._message_index: dict[str, _MutableChatRecord] = {}
        self._active_streams: dict[tuple[str, str, str], str] = {}
        self._pending_approvals: OrderedDict[str, tuple[UiApprovalItem, float]] = OrderedDict()
        self._subscribers: dict[str, SseSubscription] = {}
        self._seq = 0
        self._started = False

    @property
    def indexed_message_count(self) -> int:
        """Diagnostic counter verifying O(1) index stays strictly bounded."""
        return len(self._message_index)

    @property
    def subscriber_count(self) -> int:
        """Active SSE subscriber count."""
        return len(self._subscribers)

    @property
    def voice_enabled(self) -> bool:
        return self._voice_enabled

    def start(self) -> None:
        """Idempotently subscribe to OrchestratorEventBus."""
        if not self._started:
            self._started = True
            self._event_bus.subscribe(self._on_event)

    def close(self) -> None:
        """Unsubscribe from OrchestratorEventBus and clear SSE subscribers."""
        if self._started:
            self._started = False
            self._event_bus.unsubscribe(self._on_event)
        self._subscribers.clear()

    def subscribe_sse(self) -> SseSubscription:
        """Register a new bounded SSE client queue (max 8 concurrent clients)."""
        if len(self._subscribers) >= self._settings.max_sse_subscribers:
            raise DesktopUiError(
                code="UI_TOO_MANY_STREAMS",
                message="Maximum concurrent local SSE streams reached.",
            )
        sub_id = f"sse-{uuid.uuid4()}"
        sub = SseSubscription(
            subscription_id=sub_id,
            queue=asyncio.Queue(maxsize=self._settings.max_sse_queue),
        )
        self._subscribers[sub_id] = sub
        return sub

    def unsubscribe_sse(self, subscription: SseSubscription) -> None:
        """Idempotently remove an SSE client subscription."""
        self._subscribers.pop(subscription.subscription_id, None)

    def _redact(self, text: str) -> str:
        secrets = self._extra_secrets_provider() if self._extra_secrets_provider is not None else ()
        return redact_sensitive_text(text, extra_secrets=secrets)

    def _prune_approvals(self) -> None:
        now = self._clock()
        expired_ids = [
            audit_id
            for audit_id, (_, deadline) in self._pending_approvals.items()
            if deadline <= now
        ]
        for audit_id in expired_ids:
            self._pending_approvals.pop(audit_id, None)

    def _append_record(self, record: _MutableChatRecord) -> None:
        if len(self._messages) >= self._settings.max_messages:
            evicted = self._messages.popleft()
            self._message_index.pop(evicted.message_id, None)
            if evicted.stream_key is not None:
                mapped = self._active_streams.get(evicted.stream_key)
                if mapped == evicted.message_id:
                    self._active_streams.pop(evicted.stream_key, None)
        self._messages.append(record)
        self._message_index[record.message_id] = record
        if record.stream_key is not None:
            self._active_streams[record.stream_key] = record.message_id

    def _close_streams_for_correlation(self, correlation_id: str) -> None:
        stale_keys = [k for k in self._active_streams if k[0] == correlation_id]
        for key in stale_keys:
            msg_id = self._active_streams.pop(key, None)
            if msg_id is not None and msg_id in self._message_index:
                self._message_index[msg_id].streaming = False

    def _fanout(self, item: SseOutboundItem) -> None:
        for sub in list(self._subscribers.values()):
            if sub.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    _ = sub.queue.get_nowait()
                sub.queue.put_nowait(SseOutboundItem(kind="resync_snapshot"))
            else:
                sub.queue.put_nowait(item)

    async def record_user_utterance(self, utterance: str, *, correlation_id: str) -> UiChatMessage:
        """Record a typed chat prompt into bounded history and notify SSE subscribers."""
        sanitized = self._redact(utterance.strip())[:MAX_SINGLE_MESSAGE_CHARS]
        record = _MutableChatRecord(
            message_id=f"msg-{uuid.uuid4()}",
            correlation_id=correlation_id,
            role="user",
            modality="text",
            step_type="user_prompt",
            content=sanitized,
            timestamp=utc_now_iso(),
            streaming=False,
        )
        self._append_record(record)
        self._fanout(SseOutboundItem(kind="resync_snapshot"))
        return record.to_dto()

    def sync_call_session(self) -> UiCallSessionView:
        """Refresh CallSession view from live VoiceCallCoordinator when bound."""
        if self._call_session_provider is not None:
            live = self._call_session_provider()
            self._call_view = UiCallSessionView(
                call_id=live.call_id,
                state=live.state,
                turn_count=live.turn_count,
                active_correlation_id=live.active_correlation_id,
            )
        return self._call_view

    async def build_snapshot(self, *, csrf_token: str) -> UiStateSnapshot:
        """Construct an immutable UiStateSnapshot for GET /api/state or SSE snapshot."""
        self._prune_approvals()
        call_view = self.sync_call_session()
        options: list[UiWorkspaceOption] = []
        if self._available_workspaces_provider is not None:
            try:
                pairs = await self._available_workspaces_provider()
                options = [
                    UiWorkspaceOption(display_name=name, canonical_path=path)
                    for name, path in pairs
                ]
            except Exception:
                options = []

        return UiStateSnapshot(
            csrf_token=csrf_token,
            voice_enabled=self._voice_enabled,
            call_session=call_view,
            active_workspace=self._active_workspace,
            available_workspaces=options,
            pending_approvals=[item for item, _ in self._pending_approvals.values()],
            messages=[rec.to_dto() for rec in self._messages],
        )

    async def _on_event(self, event: OrchestratorEvent) -> None:
        self._seq += 1
        seq = self._seq

        if isinstance(event, WorkspaceChangedEvent):
            self._active_workspace = UiWorkspaceView(
                display_name=self._redact(event.workspace_name),
                canonical_path=self._redact(event.canonical_path),
                selected_via=event.selected_via,
            )
            self._emit_envelope(
                seq=seq,
                event_type="workspace_changed",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload=self._active_workspace.model_dump(),
            )
            return

        if isinstance(event, ChatStreamEvent):
            safe_delta = self._redact(event.delta)
            s_key = (event.correlation_id, event.source, event.step_type)
            existing_id = self._active_streams.get(s_key)
            existing = self._message_index.get(existing_id) if existing_id is not None else None
            if existing is not None:
                rem = MAX_SINGLE_MESSAGE_CHARS - len(existing.content)
                if rem > 0:
                    existing.content += safe_delta[:rem]
                msg_id = existing.message_id
            else:
                msg_id = f"msg-{uuid.uuid4()}"
                rec = _MutableChatRecord(
                    message_id=msg_id,
                    correlation_id=event.correlation_id,
                    role="assistant",
                    modality=event.source,
                    step_type=event.step_type,
                    content=safe_delta[:MAX_SINGLE_MESSAGE_CHARS],
                    timestamp=event.timestamp,
                    streaming=True,
                    stream_key=s_key,
                )
                self._append_record(rec)
            self._emit_envelope(
                seq=seq,
                event_type="chat_stream",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "message_id": msg_id,
                    "delta": safe_delta,
                    "source": event.source,
                    "step_type": event.step_type,
                },
            )
            return

        if isinstance(event, VoiceMilestoneEvent):
            self._close_streams_for_correlation(event.correlation_id)
            safe_summary = self._redact(event.summary_text)
            self._emit_envelope(
                seq=seq,
                event_type="voice_milestone",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "summary_text": safe_summary,
                    "priority": event.priority,
                },
            )
            return

        if isinstance(event, VoiceTranscriptionEvent):
            safe_tx = self._redact(event.sanitized_text)
            rec = _MutableChatRecord(
                message_id=f"msg-{uuid.uuid4()}",
                correlation_id=event.correlation_id,
                role="user",
                modality="voice",
                step_type="voice_transcription",
                content=safe_tx[:MAX_SINGLE_MESSAGE_CHARS],
                timestamp=event.timestamp,
                streaming=False,
            )
            self._append_record(rec)
            self._emit_envelope(
                seq=seq,
                event_type="voice_transcription",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload=rec.to_dto().model_dump(),
            )
            return

        if isinstance(event, SettingsUpdatedEvent):
            self._emit_envelope(
                seq=seq,
                event_type="settings_updated",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "requires_restart": event.requires_restart,
                    "restart_fields": list(event.restart_fields),
                },
            )
            return

        if isinstance(event, UiPanelToggleRequestedEvent):
            self._emit_envelope(
                seq=seq,
                event_type="panel_toggle_requested",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={"source": event.source},
            )
            return

        if isinstance(event, RunnerFailureEvent):
            self._close_streams_for_correlation(event.correlation_id)
            safe_err = self._redact(event.sanitized_message)
            rec = _MutableChatRecord(
                message_id=f"msg-{uuid.uuid4()}",
                correlation_id=event.correlation_id,
                role="system",
                modality="error",
                step_type=event.error_code,
                content=safe_err[:MAX_SINGLE_MESSAGE_CHARS],
                timestamp=event.timestamp,
                streaming=False,
            )
            self._append_record(rec)
            self._emit_envelope(
                seq=seq,
                event_type="runner_failure",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "message": rec.to_dto().model_dump(),
                    "error_code": event.error_code,
                    "sanitized_message": safe_err,
                    "recoverable": event.recoverable,
                },
            )
            return

        if isinstance(event, CommandBlockedEvent):
            safe_cmd = self._redact(event.sanitized_command)
            safe_reason = self._redact(event.reason)
            rec = _MutableChatRecord(
                message_id=f"msg-{uuid.uuid4()}",
                correlation_id=event.correlation_id,
                role="security",
                modality="blocked",
                step_type=event.rule_id,
                content=f"[{event.rule_id}] {safe_cmd} — {safe_reason}"[:MAX_SINGLE_MESSAGE_CHARS],
                timestamp=event.timestamp,
                streaming=False,
            )
            self._append_record(rec)
            self._emit_envelope(
                seq=seq,
                event_type="command_blocked",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "audit_id": event.audit_id,
                    "rule_id": event.rule_id,
                    "sanitized_command": safe_cmd,
                    "reason": safe_reason,
                    "message": rec.to_dto().model_dump(),
                },
            )
            return

        if isinstance(event, CommandApprovalRequestedEvent):
            self._prune_approvals()
            if len(self._pending_approvals) >= self._settings.max_pending_approvals:
                self._pending_approvals.popitem(last=False)
            item = UiApprovalItem(
                audit_id=event.audit_id,
                correlation_id=event.correlation_id,
                source=event.source,
                sanitized_command=self._redact(event.sanitized_command),
                cwd=self._redact(event.cwd),
                risk_level=event.risk_level,
                spoken_summary=self._redact(event.spoken_summary),
                technical_explanation=self._redact(event.technical_explanation),
                expires_in_seconds=event.expires_in_seconds,
                timestamp=event.timestamp,
            )
            self._pending_approvals[event.audit_id] = (
                item,
                self._clock() + event.expires_in_seconds,
            )
            self._emit_envelope(
                seq=seq,
                event_type="command_approval_requested",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload=item.model_dump(),
            )
            return

        if isinstance(event, CommandApprovalResolvedEvent):
            self._pending_approvals.pop(event.audit_id, None)
            self._emit_envelope(
                seq=seq,
                event_type="command_approval_resolved",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "audit_id": event.audit_id,
                    "approved": event.approved,
                    "verdict": event.verdict,
                    "resolved_by": event.resolved_by,
                },
            )
            return

        if isinstance(event, CallStateChangedEvent):
            live_turns = self._call_view.turn_count
            if self._call_session_provider is not None:
                live_turns = self._call_session_provider().turn_count
            self._call_view = UiCallSessionView(
                call_id=event.call_id,
                state=event.state,
                turn_count=live_turns,
                active_correlation_id=event.correlation_id,
            )
            self._emit_envelope(
                seq=seq,
                event_type="call_state_changed",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "call_id": event.call_id,
                    "previous_state": event.previous_state,
                    "state": event.state,
                    "reason": event.reason,
                    "turn_count": live_turns,
                },
            )

    def _emit_envelope(
        self,
        *,
        seq: int,
        event_type: UiEventType,
        correlation_id: str,
        timestamp: str,
        payload: dict[str, Any],
    ) -> None:
        envelope = UiEventEnvelope(
            seq=seq,
            event_type=event_type,
            correlation_id=correlation_id,
            timestamp=timestamp,
            payload=payload,
        )
        self._fanout(SseOutboundItem(kind="nova_event", envelope=envelope))
