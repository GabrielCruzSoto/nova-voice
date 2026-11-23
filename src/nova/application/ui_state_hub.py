"""Bounded O(1) reactive UI state projection and SSE fanout hub (NOVA-004 OP-1 & OP-2)."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nova.application.whiteboard import redact_whiteboard_deck
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
    VoiceSpeechChunkEvent,
    VoiceTranscriptionEvent,
    WhiteboardDeckCreatedEvent,
    WhiteboardSlideChangedEvent,
    WorkspaceChangedEvent,
    WorkspaceClearedEvent,
)
from nova.domain.models import (
    DEFAULT_WHITEBOARD_TTL_SECONDS,
    MAX_WHITEBOARD_DECKS,
    AuditRiskLevel,
    CallSession,
    CallSessionState,
    CommandSource,
    PersistedChatMessage,
    WhiteboardDeck,
    WhiteboardDeckSummary,
    WhiteboardSlideChangeReason,
    WorkspaceCatalogEntry,
    WorkspaceContext,
    WorkspaceProject,
    WorkspaceQueryMode,
    WorkspaceTopology,
    utc_now_iso,
)
from nova.domain.ports import HistoryStorePort

MAX_SINGLE_MESSAGE_CHARS = 65_536

UiEventType = Literal[
    "workspace_changed",
    "workspace_cleared",
    "chat_stream",
    "voice_milestone",
    "voice_speech_chunk",
    "runner_failure",
    "command_blocked",
    "command_approval_requested",
    "command_approval_resolved",
    "call_state_changed",
    "voice_transcription",
    "settings_updated",
    "panel_toggle_requested",
    "health_updated",
    "whiteboard_deck_created",
    "whiteboard_slide_changed",
]
UiMessageRole = Literal["user", "assistant", "system", "security"]
UiMessageModality = Literal[
    "text",
    "voice",
    "tier1_pi",
    "tier1_agent",
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


class UiProjectView(BaseModel):
    """Wire DTO for a project declared inside a multi-project Workspace (NOVA-013)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)


def _projects_to_views(
    projects: tuple[WorkspaceProject, ...],
    redact_fn: Callable[[str], str],
) -> list[UiProjectView]:
    return [
        UiProjectView(
            project_id=redact_fn(p.project_id),
            name=redact_fn(p.name),
            relative_path=redact_fn(p.relative_path),
            canonical_path=redact_fn(p.canonical_path),
        )
        for p in projects
    ]


class UiWorkspaceView(BaseModel):
    """Wire DTO for active workspace inside UiStateSnapshot (OP-1 & OP-4)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    display_name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
    selected_via: WorkspaceQueryMode = "exact_path"
    topology: WorkspaceTopology = "single-repo"
    projects: list[UiProjectView] = Field(default_factory=list)


class UiWorkspaceOption(BaseModel):
    """Wire DTO for selectable workspace inside allowed_roots catalog (OP-1)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    display_name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
    topology: WorkspaceTopology = "single-repo"
    project_count: int = 0
    projects: list[UiProjectView] = Field(default_factory=list)


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
    onboarding_required: bool = False
    process_cwd: str = ""
    allowed_roots: list[str] = Field(default_factory=list)
    call_session: UiCallSessionView
    active_workspace: UiWorkspaceView | None = None
    available_workspaces: list[UiWorkspaceOption]
    pending_approvals: list[UiApprovalItem]
    messages: list[UiChatMessage]
    active_whiteboard: WhiteboardDeck | None = None
    whiteboard_history: list[WhiteboardDeckSummary] = Field(default_factory=list)


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


@dataclass(slots=True)
class _WhiteboardCacheEntry:
    cache_key: str
    deck: WhiteboardDeck
    expires_at_monotonic: float


class UiHubSettings(BaseModel):
    """Memory and concurrency limits for UiStateHub (NFR-3 & contract.md §5)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    max_messages: int = Field(default=200, ge=1, le=1000)
    max_pending_approvals: int = Field(default=64, ge=1, le=256)
    max_sse_queue: int = Field(default=128, ge=2, le=512)
    max_sse_subscribers: int = Field(default=8, ge=1, le=64)
    max_whiteboard_decks: int = Field(default=MAX_WHITEBOARD_DECKS, ge=1, le=32)
    whiteboard_deck_ttl_seconds: float = Field(
        default=DEFAULT_WHITEBOARD_TTL_SECONDS, ge=60.0, le=7200.0
    )
    whiteboard_ttl_jitter_ratio: float = Field(default=0.05, ge=0.0, le=0.25)


class UiStateHub:
    """Projects OrchestratorEventBus events into bounded O(1) RAM state and SSE client queues."""

    def __init__(
        self,
        *,
        event_bus: OrchestratorEventBus,
        settings: UiHubSettings | None = None,
        extra_secrets_provider: Callable[[], tuple[str, ...]] | None = None,
        available_workspaces_provider: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None,
        workspace_entries_provider: (
            Callable[[], Awaitable[list[WorkspaceCatalogEntry]]] | None
        ) = None,
        onboarding_required_provider: Callable[[], bool] | None = None,
        process_cwd_provider: Callable[[], str] | None = None,
        allowed_roots_provider: Callable[[], tuple[str, ...]] | None = None,
        call_session_provider: Callable[[], CallSession] | None = None,
        initial_workspace: WorkspaceContext | None = None,
        initial_call_session: CallSession | None = None,
        history_store: HistoryStorePort | None = None,
        voice_enabled: bool = False,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._event_bus = event_bus
        self._settings = settings or UiHubSettings()
        self._history_store = history_store
        self._extra_secrets_provider = extra_secrets_provider
        self._available_workspaces_provider = available_workspaces_provider
        self._workspace_entries_provider = workspace_entries_provider
        self._onboarding_required_provider = onboarding_required_provider
        self._process_cwd_provider = process_cwd_provider
        self._allowed_roots_provider = allowed_roots_provider
        self._call_session_provider = call_session_provider
        self._voice_enabled = voice_enabled
        self._clock = clock
        self._rng = rng

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
                topology=initial_workspace.topology,
                projects=_projects_to_views(initial_workspace.projects, lambda s: s),
            )
            if initial_workspace is not None
            else None
        )

        self._messages: deque[_MutableChatRecord] = deque()
        self._message_index: dict[str, _MutableChatRecord] = {}
        self._active_streams: dict[tuple[str, str, str], str] = {}
        self._pending_approvals: OrderedDict[str, tuple[UiApprovalItem, float]] = OrderedDict()
        self._whiteboards: OrderedDict[str, _WhiteboardCacheEntry] = OrderedDict()
        self._active_whiteboard_id: str | None = None
        self._subscribers: dict[str, SseSubscription] = {}
        self._seq = 0
        self._started = False

    @property
    def indexed_message_count(self) -> int:
        """Diagnostic counter verifying O(1) index stays strictly bounded."""
        return len(self._message_index)

    @property
    def whiteboard_cache_keys(self) -> tuple[str, ...]:
        """Return canonical active whiteboard RAM cache keys (newest last)."""
        self._prune_whiteboards()
        return tuple(self._whiteboards.keys())

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

    async def hydrate_history(self) -> None:
        """Preload bounded historical chat messages from persistent SQLite store (NOVA-015)."""
        if self._history_store is None:
            return
        ws_path = self._active_workspace.canonical_path if self._active_workspace else None
        persisted = await self._history_store.get_recent_messages(
            limit=self._settings.max_messages,
            workspace_path=ws_path,
        )
        for p in persisted:
            if p.message_id not in self._message_index:
                role: UiMessageRole = "user" if p.source in ("user", "voice") else "assistant"
                if p.source == "error":
                    role = "system"
                modality: UiMessageModality = "text"
                if p.source == "voice":
                    modality = "voice"
                elif p.source in ("tier1_pi", "tier1_agent"):
                    modality = "tier1_agent" if p.source == "tier1_agent" else "tier1_pi"
                elif p.source == "tier2_runner":
                    modality = "tier2_runner"
                elif p.source == "error":
                    modality = "error"
                ts = datetime.fromtimestamp(p.created_at, UTC).isoformat()
                record = _MutableChatRecord(
                    message_id=p.message_id,
                    correlation_id=p.correlation_id,
                    role=role,
                    modality=modality,
                    step_type=p.step_type,
                    content=p.content,
                    timestamp=ts,
                    streaming=False,
                )
                self._append_record(record)

    def _persist_record_bg(self, record: _MutableChatRecord) -> None:
        if self._history_store is None:
            return
        ws_path = self._active_workspace.canonical_path if self._active_workspace else None
        source_val = (
            record.modality
            if record.role != "user"
            else ("voice" if record.modality == "voice" else "user")
        )
        p_msg = PersistedChatMessage(
            message_id=record.message_id,
            correlation_id=record.correlation_id,
            source=source_val,
            step_type=record.step_type,
            content=record.content,
            created_at=time.time(),
            workspace_path=ws_path,
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._history_store.append_message(p_msg))
        except RuntimeError:
            pass


    def close(self) -> None:
        """Unsubscribe from OrchestratorEventBus and clear SSE subscribers and RAM whiteboards."""
        if self._started:
            self._started = False
            self._event_bus.unsubscribe(self._on_event)
        self._subscribers.clear()
        self._whiteboards.clear()
        self._active_whiteboard_id = None

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

    def _extra_secrets(self) -> tuple[str, ...]:
        return self._extra_secrets_provider() if self._extra_secrets_provider is not None else ()

    def _redact(self, text: str) -> str:
        return redact_sensitive_text(text, extra_secrets=self._extra_secrets())

    @staticmethod
    def _whiteboard_cache_key(deck_id: str) -> str:
        return f"nova:ui:whiteboard:deck:{deck_id}"

    def _prune_whiteboards(self) -> None:
        now = self._clock()
        expired_keys = [
            key
            for key, entry in self._whiteboards.items()
            if entry.expires_at_monotonic <= now
        ]
        for key in expired_keys:
            self._whiteboards.pop(key, None)
        if self._active_whiteboard_id is not None:
            active_key = self._whiteboard_cache_key(self._active_whiteboard_id)
            if active_key not in self._whiteboards:
                if self._whiteboards:
                    last_entry = next(reversed(self._whiteboards.values()))
                    self._active_whiteboard_id = last_entry.deck.deck_id
                else:
                    self._active_whiteboard_id = None

    def _whiteboard_summaries(self) -> list[WhiteboardDeckSummary]:
        return [
            WhiteboardDeckSummary(
                deck_id=entry.deck.deck_id,
                topic=entry.deck.topic,
                slide_count=len(entry.deck.slides),
                active_slide_index=entry.deck.active_slide_index,
                created_at=entry.deck.created_at,
                source=entry.deck.source,
            )
            for entry in reversed(self._whiteboards.values())
        ]

    def get_whiteboard_state(self) -> tuple[WhiteboardDeck | None, list[WhiteboardDeckSummary]]:
        """Return the active RAM whiteboard deck and bounded newest-first session history."""
        self._prune_whiteboards()
        active: WhiteboardDeck | None = None
        if self._active_whiteboard_id is not None:
            entry = self._whiteboards.get(self._whiteboard_cache_key(self._active_whiteboard_id))
            if entry is not None:
                active = entry.deck
        return active, self._whiteboard_summaries()

    def _store_whiteboard_deck(self, deck: WhiteboardDeck) -> WhiteboardDeck:
        self._prune_whiteboards()
        redacted_deck = redact_whiteboard_deck(deck, extra_secrets=self._extra_secrets())
        cache_key = self._whiteboard_cache_key(redacted_deck.deck_id)
        if cache_key in self._whiteboards:
            self._whiteboards.pop(cache_key, None)
        while len(self._whiteboards) >= self._settings.max_whiteboard_decks:
            self._whiteboards.popitem(last=False)
        jitter = 1.0 + self._rng(
            -self._settings.whiteboard_ttl_jitter_ratio,
            self._settings.whiteboard_ttl_jitter_ratio,
        )
        ttl = self._settings.whiteboard_deck_ttl_seconds * jitter
        self._whiteboards[cache_key] = _WhiteboardCacheEntry(
            cache_key=cache_key,
            deck=redacted_deck,
            expires_at_monotonic=self._clock() + ttl,
        )
        self._active_whiteboard_id = redacted_deck.deck_id
        return redacted_deck

    def navigate_whiteboard(
        self,
        slide_index: int,
        *,
        deck_id: str | None = None,
        correlation_id: str,
        reason: WhiteboardSlideChangeReason = "manual_ui",
    ) -> WhiteboardDeck:
        """Validate slide bounds, update active slide in RAM, and fanout SSE sync (OP-4)."""
        self._prune_whiteboards()
        target_id = deck_id.strip() if isinstance(deck_id, str) and deck_id.strip() else (
            self._active_whiteboard_id
        )
        if not target_id:
            raise DesktopUiError(
                code="WHITEBOARD_DECK_NOT_FOUND",
                message="No active whiteboard deck found in this session.",
            )
        cache_key = self._whiteboard_cache_key(target_id)
        entry = self._whiteboards.get(cache_key)
        if entry is None:
            raise DesktopUiError(
                code="WHITEBOARD_DECK_NOT_FOUND",
                message=f"Whiteboard deck '{target_id}' not found or expired.",
            )
        if slide_index < 0 or slide_index >= len(entry.deck.slides):
            raise DesktopUiError(
                code="WHITEBOARD_INVALID_SLIDE_INDEX",
                message=(
                    f"slide_index {slide_index} is out of range for deck '{target_id}' "
                    f"({len(entry.deck.slides)} slides)."
                ),
            )
        updated_deck = entry.deck.model_copy(update={"active_slide_index": slide_index})
        entry.deck = updated_deck
        self._whiteboards.move_to_end(cache_key)
        self._active_whiteboard_id = target_id
        self._seq += 1
        self._emit_envelope(
            seq=self._seq,
            event_type="whiteboard_slide_changed",
            correlation_id=correlation_id,
            timestamp=utc_now_iso(),
            payload={
                "deck_id": target_id,
                "slide_index": slide_index,
                "reason": reason,
            },
        )
        return updated_deck

    def select_whiteboard_deck(
        self,
        deck_id: str,
        *,
        correlation_id: str,
    ) -> tuple[WhiteboardDeck, list[WhiteboardDeckSummary]]:
        """Switch active whiteboard deck from bounded session RAM history and fanout SSE (OP-5)."""
        self._prune_whiteboards()
        target_id = deck_id.strip()
        cache_key = self._whiteboard_cache_key(target_id)
        entry = self._whiteboards.get(cache_key)
        if entry is None:
            raise DesktopUiError(
                code="WHITEBOARD_DECK_NOT_FOUND",
                message=f"Whiteboard deck '{target_id}' not found or expired.",
            )
        self._whiteboards.move_to_end(cache_key)
        self._active_whiteboard_id = target_id
        summaries = self._whiteboard_summaries()
        self._seq += 1
        self._emit_envelope(
            seq=self._seq,
            event_type="whiteboard_deck_created",
            correlation_id=correlation_id,
            timestamp=utc_now_iso(),
            payload={
                "auto_open": False,
                "deck": entry.deck.model_dump(),
                "history": [s.model_dump() for s in summaries],
            },
        )
        return entry.deck, summaries

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
                rec = self._message_index[msg_id]
                rec.streaming = False
                self._persist_record_bg(rec)

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
        self._persist_record_bg(record)
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

    def clear_health_alerts(self) -> bool:
        """Remove stale COMPONENTS_MISSING timeline error records once health recovers."""
        before = len(self._messages)
        if before == 0:
            return False
        kept = [rec for rec in self._messages if rec.step_type != "COMPONENTS_MISSING"]
        if len(kept) == before:
            return False
        self._messages.clear()
        self._messages.extend(kept)
        return True

    def update_runtime_health(
        self,
        *,
        all_ok: bool,
        voice_enabled: bool,
        call_session_provider: Callable[[], CallSession] | None = None,
        alert_message: str | None = None,
    ) -> None:
        """Apply hot-reloaded voice state and health status, broadcasting to UI subscribers."""
        self._voice_enabled = voice_enabled
        if call_session_provider is not None:
            self._call_session_provider = call_session_provider
        if all_ok:
            self.clear_health_alerts()
        call_view = self.sync_call_session()
        self._seq += 1
        self._emit_envelope(
            seq=self._seq,
            event_type="health_updated",
            correlation_id="corr-health-recheck",
            timestamp=datetime.now(UTC).isoformat(),
            payload={
                "all_ok": all_ok,
                "voice_enabled": self._voice_enabled,
                "call_session": call_view.model_dump(),
                "alert_message": self._redact(alert_message) if alert_message else None,
            },
        )

    async def build_snapshot(self, *, csrf_token: str) -> UiStateSnapshot:
        """Construct an immutable UiStateSnapshot for GET /api/state or SSE snapshot."""
        self._prune_approvals()
        call_view = self.sync_call_session()
        options: list[UiWorkspaceOption] = []
        if self._workspace_entries_provider is not None:
            try:
                entries = await self._workspace_entries_provider()
                options = [
                    UiWorkspaceOption(
                        display_name=self._redact(entry.name),
                        canonical_path=self._redact(entry.canonical_path),
                        topology=entry.topology,
                        project_count=len(entry.projects),
                        projects=_projects_to_views(entry.projects, self._redact),
                    )
                    for entry in entries
                ]
            except Exception:
                options = []
        elif self._available_workspaces_provider is not None:
            try:
                pairs = await self._available_workspaces_provider()
                options = [
                    UiWorkspaceOption(display_name=name, canonical_path=path)
                    for name, path in pairs
                ]
            except Exception:
                options = []

        onboarding_req = (
            bool(self._onboarding_required_provider())
            if self._onboarding_required_provider is not None
            else False
        )
        proc_cwd = (
            self._redact(self._process_cwd_provider())
            if self._process_cwd_provider is not None
            else ""
        )
        roots_list = (
            [self._redact(r) for r in self._allowed_roots_provider()]
            if self._allowed_roots_provider is not None
            else []
        )

        active_wb, wb_history = self.get_whiteboard_state()

        return UiStateSnapshot(
            csrf_token=csrf_token,
            voice_enabled=self._voice_enabled,
            onboarding_required=onboarding_req,
            process_cwd=proc_cwd,
            allowed_roots=roots_list,
            call_session=call_view,
            active_workspace=self._active_workspace,
            available_workspaces=options,
            pending_approvals=[item for item, _ in self._pending_approvals.values()],
            messages=[rec.to_dto() for rec in self._messages],
            active_whiteboard=active_wb,
            whiteboard_history=wb_history,
        )

    async def _on_event(self, event: OrchestratorEvent) -> None:
        self._seq += 1
        seq = self._seq

        if isinstance(event, WorkspaceChangedEvent):
            self._active_workspace = UiWorkspaceView(
                display_name=self._redact(event.workspace_name),
                canonical_path=self._redact(event.canonical_path),
                selected_via=event.selected_via,
                topology=event.topology,
                projects=_projects_to_views(event.projects, self._redact),
            )
            self._emit_envelope(
                seq=seq,
                event_type="workspace_changed",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload=self._active_workspace.model_dump(),
            )
            return

        if isinstance(event, WorkspaceClearedEvent):
            self._active_workspace = None
            self._emit_envelope(
                seq=seq,
                event_type="workspace_cleared",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "active_workspace": None,
                    "process_cwd": self._redact(event.process_cwd),
                },
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

        if isinstance(event, VoiceSpeechChunkEvent):
            if event.is_final:
                self._close_streams_for_correlation(event.correlation_id)
            safe_text = self._redact(event.text)
            payload: dict[str, Any] = {
                "text": safe_text,
                "chunk_index": event.chunk_index,
                "is_final": event.is_final,
                "priority": event.priority,
            }
            if event.deck_id is not None:
                payload["deck_id"] = event.deck_id
            if event.slide_index is not None:
                payload["slide_index"] = event.slide_index
            self._emit_envelope(
                seq=seq,
                event_type="voice_speech_chunk",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload=payload,
            )
            return

        if isinstance(event, WhiteboardDeckCreatedEvent):
            stored_deck = self._store_whiteboard_deck(event.deck)
            self._emit_envelope(
                seq=seq,
                event_type="whiteboard_deck_created",
                correlation_id=event.correlation_id,
                timestamp=event.timestamp,
                payload={
                    "auto_open": event.auto_open,
                    "deck": stored_deck.model_dump(),
                    "history": [s.model_dump() for s in self._whiteboard_summaries()],
                },
            )
            return

        if isinstance(event, WhiteboardSlideChangedEvent):
            self._prune_whiteboards()
            cache_key = self._whiteboard_cache_key(event.deck_id)
            entry = self._whiteboards.get(cache_key)
            if entry is not None and 0 <= event.slide_index < len(entry.deck.slides):
                entry.deck = entry.deck.model_copy(
                    update={"active_slide_index": event.slide_index}
                )
                self._whiteboards.move_to_end(cache_key)
                self._active_whiteboard_id = event.deck_id
                self._emit_envelope(
                    seq=seq,
                    event_type="whiteboard_slide_changed",
                    correlation_id=event.correlation_id,
                    timestamp=event.timestamp,
                    payload={
                        "deck_id": event.deck_id,
                        "slide_index": event.slide_index,
                        "reason": event.reason,
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
            self._persist_record_bg(rec)
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
            self._persist_record_bg(rec)
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
