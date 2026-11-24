"""Two-tier complexity router and dual Chat+Voice event orchestrator (S4)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import re
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nova.application.action_directive import ActionDirectiveStreamFilter
from nova.application.task_manager import TaskManager
from nova.application.voice_intent import classify_confirmation_response
from nova.domain.errors import (
    GatekeeperError,
    NovaDomainError,
    TaskManagerError,
    redact_sensitive_text,
)
from nova.domain.events import (
    ChatStreamEvent,
    CommandApprovalResolvedEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    VoiceMilestoneEvent,
    VoiceSpeechChunkEvent,
)
from nova.domain.models import (
    MAX_SPEECH_CHUNK_CHARS,
    TERMINAL_TASK_STATUSES,
    AgentEngineId,
    AgentSessionPointer,
    AgentTaskRecord,
    CodingTaskRequest,
    PendingVoiceTask,
    ResolveWorkspaceRequest,
    Tier1ActionDirective,
    WorkspaceContext,
    WorkspaceQueryMode,
    WorkspaceSessionPointer,
)
from nova.domain.ports import (
    CodingRunnerPort,
    HistoryStorePort,
    SecurityGatekeeperPort,
    Tier1ConversationPort,
    WorkspaceResolverPort,
)

logger = logging.getLogger("nova.orchestrator")

_HIGH_COMPLEXITY_KEYWORDS: tuple[str, ...] = (
    "arquitectura",
    "architecture",
    "refactoriza",
    "refactor",
    "multi-archivo",
    "microservicio",
    "migracion",
    "migraciones",
    "contrato",
    "contratos",
    "integracion completa",
    "auditoria profunda",
)

_DELEGATE_MARKER_RE = re.compile(r"\[delegate_tier2\]", re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_PSEUDO_XML_BLOCK_RE = re.compile(
    r"<(cd|search|bash|shell|command|tool|thinking|think)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_XML_OPEN_TAG_RE = re.compile(
    r"<(cd|search|bash|shell|command|tool|thinking|think)\b[^>]*>",
    re.IGNORECASE,
)
_STRAY_TAG_RE = re.compile(r"</?(?!REDACTED:)[a-zA-Z_][\w-]*\b[^>]*>")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_PATH_TOKEN_RE = re.compile(r"(?:~|/)[\w./-]+")
_MD_LINE_PREFIX_RE = re.compile(r"(?m)^\s*(?:#{1,6}\s+|[-*•]\s+|\d+\.\s+|>\s*)")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?:[.!?…]+(?:\s+|$)|\n\s*\n+)")


@contextlib.asynccontextmanager
async def _closing_async_iter[T](stream: AsyncIterator[T]) -> AsyncIterator[AsyncIterator[T]]:
    """Deterministically await stream.aclose() when available on exit."""
    try:
        yield stream
    finally:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            res = aclose()
            if inspect.isawaitable(res):
                await res


def _log_structured(
    event_name: str,
    *,
    extra_secrets: tuple[str, ...] = (),
    **fields: str | bool,
) -> None:
    """Emit structured JSON log record with sensitive values masked (NFR-3)."""
    safe_payload: dict[str, str | bool] = {"event": event_name}
    for key, val in fields.items():
        safe_payload[key] = (
            redact_sensitive_text(val, extra_secrets=extra_secrets)
            if isinstance(val, str)
            else val
        )
    logger.info(json.dumps(safe_payload, ensure_ascii=False, sort_keys=True))


class OrchestratorSettings(BaseModel):
    """Application settings for NovaOrchestrator."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    tier1_engine: AgentEngineId = "pi"
    tier2_engine: AgentEngineId = "agy"
    default_tier2_model: str = Field(default="claude-opus-5-5-high", min_length=1)
    require_voice_confirmation: bool = False
    voice_confirmation_ttl_seconds: float = Field(default=300.0, ge=30.0, le=900.0)
    max_voice_confirmation_revisions: int = Field(default=8, ge=1, le=20)


def classify_complexity(utterance: str) -> Literal["low", "high"]:
    """Classify a user instruction into Tier 1 ('low') or Tier 2 ('high')."""
    normalized = utterance.lower()
    if _DELEGATE_MARKER_RE.search(normalized) is not None:
        return "high"

    hits = sum(1 for kw in _HIGH_COMPLEXITY_KEYWORDS if kw in normalized)
    if hits >= 2 or (hits >= 1 and len(normalized.split()) >= 14):
        return "high"
    return "low"


def sanitize_spoken_chunk(
    raw_text: str,
    *,
    extra_secrets: tuple[str, ...] = (),
) -> str:
    """Clean Markdown, code blocks, XML pseudo-tags, paths, and secrets for natural TTS."""
    redacted_first = redact_sensitive_text(raw_text, extra_secrets=extra_secrets)
    without_code = _CODE_BLOCK_RE.sub(" ", redacted_first)
    without_xml = _PSEUDO_XML_BLOCK_RE.sub(" ", without_code)
    without_tags = _STRAY_TAG_RE.sub(" ", without_xml)
    without_urls = _URL_RE.sub("enlace", without_tags)
    without_paths = _PATH_TOKEN_RE.sub("archivo del proyecto", without_urls)
    redacted = redact_sensitive_text(without_paths, extra_secrets=extra_secrets)
    without_prefixes = _MD_LINE_PREFIX_RE.sub(" ", redacted)
    cleaned = re.sub(r"[`*_~|]+", " ", without_prefixes)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    normalized = " ".join(cleaned.split())
    if not any(ch.isalnum() for ch in normalized):
        return ""
    return normalized


def _split_long_spoken_text(text: str, *, max_chars: int) -> list[str]:
    """Split an over-long sanitized sentence cleanly at clause or word boundaries."""
    limit = min(max_chars, MAX_SPEECH_CHUNK_CHARS)
    remaining = text.strip()
    if not remaining:
        return []
    parts: list[str] = []
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for sep in (". ", "? ", "! ", "; ", ", ", ": "):
            pos = window.rfind(sep)
            if pos >= limit // 3:
                cut = max(cut, pos + len(sep) - 1)
        if cut < 0:
            space_pos = window.rfind(" ")
            cut = space_pos if space_pos > 0 else limit
        head = remaining[:cut].strip()
        if head:
            parts.append(head)
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


class VoiceSentenceStreamSplitter:
    """Incremental sentence splitter for streaming spoken TTS chunks (NOVA-018 OP-1)."""

    def __init__(
        self,
        *,
        extra_secrets: tuple[str, ...] = (),
        min_chunk_chars: int = 24,
        target_max_chars: int = 240,
    ) -> None:
        self._extra_secrets = extra_secrets
        self._min_chunk_chars = max(1, min_chunk_chars)
        self._target_max_chars = min(max(60, target_max_chars), MAX_SPEECH_CHUNK_CHARS)
        self._raw_buf = ""
        self._in_code_fence = False
        self._xml_block_tag: str | None = None

    def _consume_suppressed_blocks(self) -> None:
        while True:
            if self._in_code_fence:
                end_fence = self._raw_buf.find("```")
                if end_fence < 0:
                    # Keep up to 2 trailing backticks in case closing fence splits across deltas
                    self._raw_buf = self._raw_buf[-2:] if self._raw_buf.endswith("`") else ""
                    return
                self._raw_buf = self._raw_buf[end_fence + 3 :]
                self._in_code_fence = False
                continue

            if self._xml_block_tag is not None:
                close_re = re.compile(rf"</{re.escape(self._xml_block_tag)}\s*>", re.IGNORECASE)
                match = close_re.search(self._raw_buf)
                if match is None:
                    self._raw_buf = self._raw_buf[-24:]
                    return
                self._raw_buf = self._raw_buf[match.end() :]
                self._xml_block_tag = None
                continue
            return

    def _extract_safe_prefix(self) -> tuple[str, bool]:
        """Return (speakable_prefix, blocked_by_open_block) consuming suppressed spans."""
        self._consume_suppressed_blocks()
        if self._in_code_fence or self._xml_block_tag is not None:
            return "", True

        fence_idx = self._raw_buf.find("```")
        xml_match = _XML_OPEN_TAG_RE.search(self._raw_buf)
        xml_idx = xml_match.start() if xml_match is not None else -1

        if fence_idx >= 0 and (xml_idx < 0 or fence_idx <= xml_idx):
            prefix = self._raw_buf[:fence_idx]
            self._raw_buf = self._raw_buf[fence_idx + 3 :]
            self._in_code_fence = True
            self._consume_suppressed_blocks()
            return prefix, True

        if xml_match is not None:
            prefix = self._raw_buf[:xml_idx]
            self._xml_block_tag = xml_match.group(1)
            self._raw_buf = self._raw_buf[xml_match.end() :]
            self._consume_suppressed_blocks()
            return prefix, True

        return self._raw_buf, False

    def feed(self, delta: str) -> list[str]:
        """Feed a streamed LLM delta and emit any complete spoken sentences immediately."""
        if not delta:
            return []
        self._raw_buf += delta
        emitted: list[str] = []

        while True:
            self._consume_suppressed_blocks()
            if self._in_code_fence or self._xml_block_tag is not None:
                break

            fence_idx = self._raw_buf.find("```")
            xml_match = _XML_OPEN_TAG_RE.search(self._raw_buf)
            xml_idx = xml_match.start() if xml_match is not None else -1
            if fence_idx >= 0 or xml_idx >= 0:
                prefix, _ = self._extract_safe_prefix()
                cleaned_prefix = sanitize_spoken_chunk(prefix, extra_secrets=self._extra_secrets)
                if cleaned_prefix:
                    emitted.extend(
                        _split_long_spoken_text(
                            cleaned_prefix,
                            max_chars=self._target_max_chars,
                        )
                    )
                continue

            # Emit completed sentence only once subsequent text has started so final chunk
            # naturally flushes with is_final=True when the stream ends. Scan across multiple
            # boundaries so a short prefix ("Claro. ", "1. ") never stalls streaming (F3).
            emitted_sentence = False
            for match in _SENTENCE_BOUNDARY_RE.finditer(self._raw_buf):
                if not self._raw_buf[match.end() :].strip():
                    break
                candidate = self._raw_buf[: match.end()]
                cleaned = sanitize_spoken_chunk(candidate, extra_secrets=self._extra_secrets)
                if len(cleaned) >= self._min_chunk_chars or cleaned.endswith(("?", "!")):
                    self._raw_buf = self._raw_buf[match.end() :]
                    emitted.extend(
                        _split_long_spoken_text(cleaned, max_chars=self._target_max_chars)
                    )
                    emitted_sentence = True
                    break
            if emitted_sentence:
                continue

            if len(self._raw_buf) >= self._target_max_chars * 2:
                # Preserve a short raw tail so split ``` or <tag delimiters are not corrupted
                tail_guard = 12
                head_raw = self._raw_buf[:-tail_guard]
                tail_raw = self._raw_buf[-tail_guard:]
                cleaned_head = sanitize_spoken_chunk(
                    head_raw,
                    extra_secrets=self._extra_secrets,
                )
                split_parts = _split_long_spoken_text(
                    cleaned_head,
                    max_chars=self._target_max_chars,
                )
                if len(split_parts) > 1:
                    emitted.extend(split_parts[:-1])
                    self._raw_buf = split_parts[-1] + " " + tail_raw
                    continue
                if len(split_parts) == 1:
                    emitted.append(split_parts[0])
                    self._raw_buf = tail_raw
                    continue
            break

        return emitted

    def flush(self) -> list[str]:
        """Drain remaining speakable text at end of stream."""
        self._consume_suppressed_blocks()
        if self._in_code_fence or self._xml_block_tag is not None:
            self._raw_buf = ""
            self._in_code_fence = False
            self._xml_block_tag = None
            return []
        leftover = self._raw_buf
        self._raw_buf = ""
        cleaned = sanitize_spoken_chunk(leftover, extra_secrets=self._extra_secrets)
        if not cleaned:
            return []
        return _split_long_spoken_text(cleaned, max_chars=self._target_max_chars)

    @classmethod
    def split_text(
        cls,
        text: str,
        *,
        extra_secrets: tuple[str, ...] = (),
        target_max_chars: int = 240,
    ) -> list[str]:
        """Split a complete response string into bounded sanitized spoken chunks."""
        splitter = cls(extra_secrets=extra_secrets, target_max_chars=target_max_chars)
        chunks = splitter.feed(text)
        chunks.extend(splitter.flush())
        return chunks


def sanitize_for_voice(
    raw_text: str,
    fallback: str,
    max_words: int = 20,
    extra_secrets: tuple[str, ...] = (),
) -> str:
    """Strip code blocks and filesystem paths, returning a <=25-word spoken summary."""
    cleaned = sanitize_spoken_chunk(raw_text, extra_secrets=extra_secrets)
    words = [w for w in cleaned.split() if w]
    if not words:
        words = fallback.split()
    capped = words[: min(max_words, 24)]
    return " ".join(capped)


class NovaOrchestrator:
    """Coordinates workspace context, Tier 1 Pi RPC, Tier 2 CodingRunnerPort, and dual events."""

    def __init__(
        self,
        workspace_resolver: WorkspaceResolverPort,
        tier1_client: Tier1ConversationPort,
        tier2_runner: CodingRunnerPort,
        event_bus: OrchestratorEventBus,
        settings: OrchestratorSettings | None = None,
        security_gatekeeper: SecurityGatekeeperPort | None = None,
        history_store: HistoryStorePort | None = None,
        task_manager: TaskManager | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._workspace_resolver = workspace_resolver
        self._tier1_client = tier1_client
        self._tier2_runner = tier2_runner
        self._event_bus = event_bus
        self._settings = settings or OrchestratorSettings()
        self._security_gatekeeper = security_gatekeeper
        self._history_store = history_store
        self._task_manager = task_manager
        self._clock = clock
        self._tier2_conversations: dict[str, str] = {}
        self._pending_voice_task: PendingVoiceTask | None = None
        self._turn_lock = asyncio.Lock()
        self._active_turn_task: asyncio.Task[None] | None = None
        self._barge_in_cancelled: weakref.WeakSet[asyncio.Task[None]] = weakref.WeakSet()

    @property
    def task_manager(self) -> TaskManager | None:
        """Return attached asynchronous TaskManager, if configured (NOVA-020)."""
        return self._task_manager

    def _prune_expired_voice_task(self) -> PendingVoiceTask | None:
        task = self._pending_voice_task
        if task is None:
            return None
        if self._clock() >= task.expires_at_monotonic:
            self._pending_voice_task = None
            self._log(
                "voice_task_expired",
                task_id=task.task_id,
                correlation_id=task.correlation_id,
            )
            return None
        return task

    @property
    def pending_voice_task(self) -> PendingVoiceTask | None:
        """Return currently active non-expired PendingVoiceTask, if any."""
        return self._prune_expired_voice_task()

    def update_settings(self, *, require_voice_confirmation: bool | None = None) -> None:
        """Hot-update voice confirmation behavior in RAM (NOVA-018)."""
        if require_voice_confirmation is not None:
            self._settings = self._settings.model_copy(
                update={"require_voice_confirmation": require_voice_confirmation}
            )
            if not require_voice_confirmation:
                self._pending_voice_task = None

    def update_models(self, *, tier2_model: str | None = None) -> None:
        """Hot-update default Tier 2 model for future delegations."""
        if tier2_model is not None and tier2_model.strip():
            cleaned = tier2_model.strip()
            self._settings = self._settings.model_copy(
                update={"default_tier2_model": cleaned}
            )
            if self._task_manager is not None:
                self._task_manager.update_defaults(default_model=cleaned)

    def update_engines(
        self,
        *,
        tier1_client: Tier1ConversationPort | None = None,
        tier2_runner: CodingRunnerPort | None = None,
        tier1_engine: AgentEngineId | None = None,
        tier2_engine: AgentEngineId | None = None,
        tier2_model: str | None = None,
    ) -> None:
        """Hot-swap Tier 1 / Tier 2 adapters and engine metadata (NOVA-017)."""
        if tier1_client is not None:
            self._tier1_client = tier1_client
        if tier2_runner is not None:
            self._tier2_runner = tier2_runner
        updates: dict[str, object] = {}
        if tier1_engine is not None:
            updates["tier1_engine"] = tier1_engine
        if tier2_engine is not None:
            updates["tier2_engine"] = tier2_engine
        if tier2_model is not None and tier2_model.strip():
            updates["default_tier2_model"] = tier2_model.strip()
        if updates:
            self._settings = self._settings.model_copy(update=updates)
        if self._task_manager is not None:
            self._task_manager.update_defaults(
                default_provider=tier2_engine,
                default_model=tier2_model,
            )

    def _extra_secrets(self) -> tuple[str, ...]:
        if self._security_gatekeeper is None:
            return ()
        return self._security_gatekeeper.extra_secrets

    def _redact(self, text: str) -> str:
        return redact_sensitive_text(text, extra_secrets=self._extra_secrets())

    def _log(self, event_name: str, **fields: str | bool) -> None:
        _log_structured(event_name, extra_secrets=self._extra_secrets(), **fields)

    async def _switch_tier1_context(
        self,
        target_path: str,
        *,
        workspace_active: bool,
        allowed_project_paths: tuple[str, ...],
        session_id: str | None = None,
    ) -> None:
        sig = inspect.signature(self._tier1_client.switch_workspace)
        kwargs: dict[str, Any] = {}
        if "workspace_active" in sig.parameters:
            kwargs["workspace_active"] = workspace_active
        if "allowed_project_paths" in sig.parameters:
            kwargs["allowed_project_paths"] = allowed_project_paths
        if "session_id" in sig.parameters:
            kwargs["session_id"] = session_id
        await self._tier1_client.switch_workspace(target_path, **kwargs)

    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "voice_fuzzy",
        correlation_id: str | None = None,
    ) -> WorkspaceContext:
        """Resolve, activate, and announce an isolated workspace directory."""
        corr_id = correlation_id or f"corr-{uuid.uuid4()}"
        async with self._turn_lock:
            context = await self._workspace_resolver.resolve(
                ResolveWorkspaceRequest(
                    correlation_id=corr_id,
                    query=query,
                    mode=mode,
                )
            )

            # Query session pointers from history store for continuity (NOVA-015 / NOVA-017)
            tier1_session_id: str | None = None
            if self._history_store is not None:
                get_agent_fn = getattr(self._history_store, "get_agent_session", None)
                if callable(get_agent_fn):
                    t1_ptr = await get_agent_fn(
                        context.canonical_path, "tier1", self._settings.tier1_engine
                    )
                    if t1_ptr is not None and getattr(t1_ptr, "session_id", None):
                        tier1_session_id = str(t1_ptr.session_id)
                    t2_ptr = await get_agent_fn(
                        context.canonical_path, "tier2", self._settings.tier2_engine
                    )
                    if t2_ptr is not None and getattr(t2_ptr, "session_id", None):
                        self._tier2_conversations[context.canonical_path] = str(t2_ptr.session_id)

                if (
                    tier1_session_id is None
                    or context.canonical_path not in self._tier2_conversations
                ):
                    pointer = await self._history_store.get_session_pointer(context.canonical_path)
                    if pointer is not None:
                        if (
                            pointer.agy_conversation_id
                            and context.canonical_path not in self._tier2_conversations
                            and self._settings.tier2_engine == "agy"
                        ):
                            self._tier2_conversations[context.canonical_path] = (
                                pointer.agy_conversation_id
                            )
                        if (
                            pointer.pi_session_id
                            and tier1_session_id is None
                            and self._settings.tier1_engine == "pi"
                        ):
                            tier1_session_id = pointer.pi_session_id

            project_paths = tuple(p.canonical_path for p in context.projects)
            await self._switch_tier1_context(
                context.canonical_path,
                workspace_active=True,
                allowed_project_paths=project_paths,
                session_id=tier1_session_id,
            )
            self._pending_voice_task = None
            self._log(
                "workspace_selected",
                correlation_id=corr_id,
                workspace_id=context.workspace_id,
                workspace_name=context.name,
            )
            await self._event_bus.publish(
                VoiceMilestoneEvent(
                    correlation_id=corr_id,
                    summary_text=sanitize_for_voice(
                        f"Proyecto activo cambiado a {context.name}.",
                        fallback="Proyecto activo actualizado.",
                        extra_secrets=self._extra_secrets(),
                    ),
                    priority="normal",
                )
            )
            return context

    async def clear_workspace(self, correlation_id: str | None = None) -> None:
        """Clear active workspace and return Tier 1 to conversational ~ mode with zero tools."""
        corr_id = correlation_id or f"corr-{uuid.uuid4()}"
        async with self._turn_lock:
            clear_fn = getattr(self._workspace_resolver, "clear_active_workspace", None)
            if callable(clear_fn):
                res = clear_fn(corr_id)
                if inspect.isawaitable(res):
                    await res
            home_cwd = str(Path.home().resolve())
            await self._switch_tier1_context(
                home_cwd,
                workspace_active=False,
                allowed_project_paths=(),
            )
            self._pending_voice_task = None
            self._log("workspace_cleared", correlation_id=corr_id)
            await self._event_bus.publish(
                VoiceMilestoneEvent(
                    correlation_id=corr_id,
                    summary_text="Modo conversacional activo sin workspace.",
                    priority="normal",
                )
            )

    async def _publish_spoken_text_chunks(
        self,
        text: str,
        *,
        correlation_id: str,
        fallback: str,
        priority: Literal["normal", "interrupt"] = "normal",
    ) -> None:
        chunks = VoiceSentenceStreamSplitter.split_text(
            text,
            extra_secrets=self._extra_secrets(),
        )
        if not chunks:
            cleaned_fb = sanitize_spoken_chunk(fallback, extra_secrets=self._extra_secrets())
            safe_fb = cleaned_fb or "Operación completada."
            chunks = _split_long_spoken_text(safe_fb, max_chars=MAX_SPEECH_CHUNK_CHARS)
            if not chunks:
                chunks = ["Operación completada."]
        last_idx = len(chunks) - 1
        for idx, chunk in enumerate(chunks):
            await self._event_bus.publish(
                VoiceSpeechChunkEvent(
                    correlation_id=correlation_id,
                    text=chunk,
                    chunk_index=idx,
                    is_final=(idx == last_idx),
                    priority=priority,
                )
            )

    def _build_confirmation_message(
        self,
        understood_utterance: str,
        active_ws: WorkspaceContext | None,
        *,
        is_revision: bool = False,
    ) -> str:
        prefix = "Actualicé la instrucción. " if is_revision else ""
        if active_ws is not None:
            return (
                f"{prefix}Entendí que en el proyecto {active_ws.name} quieres que haga lo "
                f"siguiente: {understood_utterance}. "
                "¿Es correcto? Di Oye Nova, sí para ejecutarlo, o dime qué corregir."
            )
        return (
            f"{prefix}Entendí que quieres lo siguiente: {understood_utterance}. "
            "¿Es correcto? Di Oye Nova, sí para continuar, o dime qué corregir."
        )

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
        *,
        origin: Literal["chat", "voice"] = "chat",
    ) -> None:
        """Route a user utterance by origin/complexity and emit synchronized Chat + Voice events."""
        corr_id = correlation_id or f"corr-{uuid.uuid4()}"
        async with self._turn_lock:
            active_ws = self._workspace_resolver.active_context
            effective_utterance = utterance
            speak_full_response = origin == "voice"

            if origin == "voice" and self._settings.require_voice_confirmation:
                pending = self._prune_expired_voice_task()
                decision = classify_confirmation_response(utterance)
                if decision.action == "ignore_empty":
                    return

                if pending is None:
                    if decision.action in ("confirm", "cancel"):
                        msg = (
                            "No hay ninguna tarea pendiente para confirmar. "
                            "Dime qué necesitas que haga."
                            if decision.action == "confirm"
                            else "No hay ninguna tarea pendiente para cancelar."
                        )
                        await self._event_bus.publish(
                            ChatStreamEvent(
                                correlation_id=corr_id,
                                delta=msg,
                                source="tier1_pi",
                                step_type="voice_confirmation",
                            )
                        )
                        await self._publish_spoken_text_chunks(
                            msg,
                            correlation_id=corr_id,
                            fallback=msg,
                        )
                        return

                    redacted_draft = self._redact(decision.cleaned_text).strip()
                    if not redacted_draft:
                        return
                    needs_ws_block = (
                        active_ws is None
                        and (
                            _DELEGATE_MARKER_RE.search(redacted_draft) is not None
                            if self._task_manager is not None
                            else classify_complexity(redacted_draft) == "high"
                        )
                    )
                    if needs_ws_block:
                        self._log(
                            "utterance_blocked_no_workspace",
                            correlation_id=corr_id,
                            tier="high",
                        )
                        warn_msg = (
                            "Para realizar tareas de código o modificar archivos, "
                            "por favor selecciona un proyecto en pantalla primero."
                        )
                        await self._event_bus.publish(
                            ChatStreamEvent(
                                correlation_id=corr_id,
                                delta=warn_msg,
                                source="tier1_pi",
                                step_type="agent_response",
                            )
                        )
                        await self._publish_spoken_text_chunks(
                            warn_msg,
                            correlation_id=corr_id,
                            fallback=warn_msg,
                        )
                        return

                    now = self._clock()
                    summary = (
                        f"En {active_ws.name}: {redacted_draft}"
                        if active_ws is not None
                        else redacted_draft
                    )
                    task = PendingVoiceTask(
                        task_id=f"vtask-{uuid.uuid4()}",
                        correlation_id=corr_id,
                        utterance=redacted_draft,
                        understood_summary=summary,
                        workspace_canonical_path=(
                            active_ws.canonical_path if active_ws is not None else None
                        ),
                        workspace_name=active_ws.name if active_ws is not None else None,
                        revision=1,
                        created_at_monotonic=now,
                        expires_at_monotonic=now + self._settings.voice_confirmation_ttl_seconds,
                    )
                    self._pending_voice_task = task
                    self._log(
                        "voice_task_confirmation_requested",
                        task_id=task.task_id,
                        correlation_id=corr_id,
                        workspace_name=active_ws.name if active_ws is not None else "none",
                    )
                    confirm_msg = self._build_confirmation_message(
                        redacted_draft,
                        active_ws,
                        is_revision=False,
                    )
                    await self._event_bus.publish(
                        ChatStreamEvent(
                            correlation_id=corr_id,
                            delta=confirm_msg,
                            source="tier1_pi",
                            step_type="voice_confirmation",
                        )
                    )
                    await self._publish_spoken_text_chunks(
                        confirm_msg,
                        correlation_id=corr_id,
                        fallback=confirm_msg,
                    )
                    return

                # Active pending task exists
                if decision.action == "cancel":
                    self._pending_voice_task = None
                    self._log(
                        "voice_task_cancelled",
                        task_id=pending.task_id,
                        correlation_id=corr_id,
                    )
                    cancel_msg = "Entendido, cancelé la tarea pendiente."
                    await self._event_bus.publish(
                        ChatStreamEvent(
                            correlation_id=corr_id,
                            delta=cancel_msg,
                            source="tier1_pi",
                            step_type="voice_confirmation",
                        )
                    )
                    await self._publish_spoken_text_chunks(
                        cancel_msg,
                        correlation_id=corr_id,
                        fallback=cancel_msg,
                    )
                    return

                if decision.action == "correct":
                    redacted_corr = self._redact(decision.cleaned_text).strip()
                    if not redacted_corr:
                        return
                    if pending.revision >= self._settings.max_voice_confirmation_revisions:
                        self._pending_voice_task = None
                        self._log(
                            "voice_task_revision_limit_reached",
                            task_id=pending.task_id,
                            correlation_id=corr_id,
                        )
                        limit_msg = (
                            "Alcanzaste el máximo de correcciones para esta tarea. "
                            "Cancelé el borrador; dime de nuevo la instrucción completa."
                        )
                        await self._event_bus.publish(
                            ChatStreamEvent(
                                correlation_id=corr_id,
                                delta=limit_msg,
                                source="tier1_pi",
                                step_type="voice_confirmation",
                            )
                        )
                        await self._publish_spoken_text_chunks(
                            limit_msg,
                            correlation_id=corr_id,
                            fallback=limit_msg,
                        )
                        return
                    now = self._clock()
                    next_rev = pending.revision + 1
                    updated_utterance = f"{pending.utterance}. Corrección: {redacted_corr}"
                    updated_summary = (
                        f"En {active_ws.name}: {updated_utterance}"
                        if active_ws is not None
                        else updated_utterance
                    )
                    updated_task = pending.model_copy(
                        update={
                            "correlation_id": corr_id,
                            "utterance": updated_utterance,
                            "understood_summary": updated_summary,
                            "revision": next_rev,
                            "expires_at_monotonic": (
                                now + self._settings.voice_confirmation_ttl_seconds
                            ),
                        }
                    )
                    self._pending_voice_task = updated_task
                    self._log(
                        "voice_task_corrected",
                        task_id=updated_task.task_id,
                        correlation_id=corr_id,
                    )
                    confirm_msg = self._build_confirmation_message(
                        updated_utterance,
                        active_ws,
                        is_revision=True,
                    )
                    await self._event_bus.publish(
                        ChatStreamEvent(
                            correlation_id=corr_id,
                            delta=confirm_msg,
                            source="tier1_pi",
                            step_type="voice_confirmation",
                        )
                    )
                    await self._publish_spoken_text_chunks(
                        confirm_msg,
                        correlation_id=corr_id,
                        fallback=confirm_msg,
                    )
                    return

                # Explicit confirmation -> atomically consume pending task and execute
                self._pending_voice_task = None
                effective_utterance = pending.utterance
                self._log(
                    "voice_task_confirmed",
                    task_id=pending.task_id,
                    correlation_id=corr_id,
                )

            if active_ws is None:
                block_no_ws = (
                    _DELEGATE_MARKER_RE.search(effective_utterance) is not None
                    if self._task_manager is not None
                    else classify_complexity(effective_utterance) == "high"
                )
                if block_no_ws:
                    self._log(
                        "utterance_blocked_no_workspace",
                        correlation_id=corr_id,
                        tier="high",
                    )
                    await self._event_bus.publish(
                        VoiceMilestoneEvent(
                            correlation_id=corr_id,
                            summary_text=(
                                "Para modificar archivos o ejecutar código, "
                                "por favor seleccioná un proyecto en pantalla."
                            ),
                            priority="normal",
                        )
                    )
                    await self._event_bus.publish(
                        ChatStreamEvent(
                            correlation_id=corr_id,
                            delta=(
                                "Para realizar tareas de código o modificar archivos, "
                                "seleccioná un proyecto primero."
                            ),
                            source="tier1_pi",
                            step_type="agent_response",
                        )
                    )
                    return

                turn_task = asyncio.create_task(
                    self._execute_conversational_utterance(
                        effective_utterance,
                        corr_id,
                        speak_full_response=speak_full_response,
                    )
                )
            else:
                turn_task = asyncio.create_task(
                    self._execute_utterance(
                        effective_utterance,
                        active_ws,
                        corr_id,
                        speak_full_response=speak_full_response,
                    )
                )
            self._active_turn_task = turn_task
            try:
                await turn_task
            except asyncio.CancelledError:
                if turn_task in self._barge_in_cancelled:
                    self._log("utterance_aborted_by_barge_in", correlation_id=corr_id)
                    return
                turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await turn_task
                raise
            finally:
                if self._active_turn_task is turn_task:
                    self._active_turn_task = None

    async def _execute_conversational_utterance(
        self,
        utterance: str,
        corr_id: str,
        *,
        speak_full_response: bool = False,
    ) -> None:
        self._log(
            "utterance_routed",
            correlation_id=corr_id,
            tier="low",
            workspace_name="none",
        )
        try:
            await self._run_tier1(
                utterance,
                corr_id,
                speak_full_response=speak_full_response,
            )
        except NovaDomainError as exc:
            redacted_err = self._redact(exc.sanitized_message)
            self._log(
                "runner_failure",
                correlation_id=corr_id,
                error_code=exc.code,
                message=redacted_err,
            )
            await self._event_bus.publish(
                RunnerFailureEvent(
                    correlation_id=corr_id,
                    error_code=exc.code,
                    sanitized_message=redacted_err,
                    recoverable=True,
                )
            )
            await self._event_bus.publish(
                VoiceMilestoneEvent(
                    correlation_id=corr_id,
                    summary_text=(
                        "Ocurrio un problema al responder tu consulta."
                    ),
                    priority="interrupt",
                )
            )

    async def _execute_utterance(
        self,
        utterance: str,
        active_ws: WorkspaceContext,
        corr_id: str,
        *,
        speak_full_response: bool = False,
    ) -> None:
        if self._task_manager is not None:
            if _DELEGATE_MARKER_RE.search(utterance) is not None:
                cleaned_prompt = _DELEGATE_MARKER_RE.sub("", utterance).strip() or utterance.strip()
                self._log(
                    "utterance_routed",
                    correlation_id=corr_id,
                    tier="high_async",
                    workspace_name=active_ws.name,
                )
                try:
                    task = await self._task_manager.dispatch_task(
                        workspace_canonical_path=active_ws.canonical_path,
                        workspace_name=active_ws.name,
                        objective=cleaned_prompt,
                        provider=self._settings.tier2_engine,
                        model=self._settings.default_tier2_model,
                        correlation_id=corr_id,
                    )
                    ack = (
                        f"Listo, mandé la tarea {task.task_id} a {task.provider} "
                        f"en {task.workspace_name}. Te aviso cuando termine."
                    )
                    await self._emit_orchestrator_reply(
                        ack,
                        correlation_id=corr_id,
                        speak_full_response=speak_full_response,
                    )
                except NovaDomainError as exc:
                    await self._publish_domain_failure(exc, corr_id)
                return

            self._log(
                "utterance_routed",
                correlation_id=corr_id,
                tier="tier1_structured",
                workspace_name=active_ws.name,
            )
            try:
                await self._run_tier1(
                    utterance,
                    corr_id,
                    speak_full_response=speak_full_response,
                )
            except NovaDomainError as exc:
                await self._publish_domain_failure(exc, corr_id)
            return

        tier = classify_complexity(utterance)
        self._log(
            "utterance_routed",
            correlation_id=corr_id,
            tier=tier,
            workspace_name=active_ws.name,
        )
        try:
            if tier == "low":
                await self._run_tier1(
                    utterance,
                    corr_id,
                    speak_full_response=speak_full_response,
                )
            else:
                await self._run_tier2(
                    utterance,
                    active_ws.canonical_path,
                    corr_id,
                    speak_full_response=speak_full_response,
                )
        except NovaDomainError as exc:
            await self._publish_domain_failure(exc, corr_id)

    async def _publish_domain_failure(self, exc: NovaDomainError, corr_id: str) -> None:
        redacted_err = self._redact(exc.sanitized_message)
        self._log(
            "runner_failure",
            correlation_id=corr_id,
            error_code=exc.code,
            message=redacted_err,
        )
        await self._event_bus.publish(
            RunnerFailureEvent(
                correlation_id=corr_id,
                error_code=exc.code,
                sanitized_message=redacted_err,
                recoverable=True,
            )
        )
        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=corr_id,
                summary_text=(
                    "Ocurrio un problema al ejecutar la tarea. Revisa el detalle en el chat."
                ),
                priority="interrupt",
            )
        )

    async def _emit_orchestrator_reply(
        self,
        message: str,
        *,
        correlation_id: str,
        speak_full_response: bool,
        start_chunk_index: int = 0,
    ) -> int:
        redacted = self._redact(message)
        await self._event_bus.publish(
            ChatStreamEvent(
                correlation_id=correlation_id,
                delta=redacted,
                source="tier1_pi",
                step_type="agent_response",
            )
        )
        if speak_full_response:
            chunks = VoiceSentenceStreamSplitter.split_text(
                redacted,
                extra_secrets=self._extra_secrets(),
            )
            if not chunks:
                chunks = ["Operación completada."]
            last_idx = len(chunks) - 1
            chunk_idx = start_chunk_index
            for idx, chunk in enumerate(chunks):
                await self._event_bus.publish(
                    VoiceSpeechChunkEvent(
                        correlation_id=correlation_id,
                        text=chunk,
                        chunk_index=chunk_idx,
                        is_final=(idx == last_idx),
                        priority="normal",
                    )
                )
                chunk_idx += 1
            return chunk_idx
        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=correlation_id,
                summary_text=sanitize_for_voice(
                    redacted,
                    fallback="Listo.",
                    extra_secrets=self._extra_secrets(),
                ),
                priority="normal",
            )
        )
        return start_chunk_index

    async def barge_in(self) -> None:
        """Interrupt active speech/agent turn immediately on user voice activity."""
        self._log("barge_in_triggered")
        self._pending_voice_task = None
        turn_task = self._active_turn_task
        if turn_task is not None and not turn_task.done():
            self._barge_in_cancelled.add(turn_task)
            turn_task.cancel()
        await self._tier1_client.barge_in_abort()
        if turn_task is not None and not turn_task.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(turn_task), timeout=0.05)

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_voice_or_ui",
    ) -> CommandApprovalResolvedEvent:
        """Delegate human command approval/rejection to the configured SecurityGatekeeper."""
        if self._security_gatekeeper is None:
            raise GatekeeperError(
                code="GATEKEEPER_NOT_CONFIGURED",
                message="SecurityGatekeeper is not attached to NovaOrchestrator.",
            )
        return await self._security_gatekeeper.resolve_command_approval(
            audit_id,
            approved=approved,
            resolved_by=resolved_by,
        )

    async def _run_tier1(
        self,
        utterance: str,
        correlation_id: str,
        *,
        speak_full_response: bool = False,
    ) -> None:
        collected: list[str] = []
        directive_filter = (
            ActionDirectiveStreamFilter(extra_secrets=self._extra_secrets())
            if self._task_manager is not None
            else None
        )
        splitter = (
            VoiceSentenceStreamSplitter(extra_secrets=self._extra_secrets())
            if speak_full_response
            else None
        )
        chunk_index = 0

        async def _emit_visible(text: str) -> None:
            nonlocal chunk_index
            if not text:
                return
            collected.append(text)
            await self._event_bus.publish(
                ChatStreamEvent(
                    correlation_id=correlation_id,
                    delta=text,
                    source="tier1_pi",
                    step_type="agent_response",
                )
            )
            if splitter is not None:
                for ready_chunk in splitter.feed(text):
                    await self._event_bus.publish(
                        VoiceSpeechChunkEvent(
                            correlation_id=correlation_id,
                            text=ready_chunk,
                            chunk_index=chunk_index,
                            is_final=False,
                            priority="normal",
                        )
                    )
                    chunk_index += 1

        async with _closing_async_iter(self._tier1_client.stream_prompt(utterance)) as stream:
            async for delta in stream:
                redacted_delta = self._redact(delta)
                visible = (
                    directive_filter.feed(redacted_delta)
                    if directive_filter is not None
                    else redacted_delta
                )
                await _emit_visible(visible)

        if directive_filter is not None:
            await _emit_visible(directive_filter.flush())

        joined = "".join(collected)
        active_ws = self._workspace_resolver.active_context
        if self._history_store is not None and active_ws is not None:
            sess_id: str | None = None
            get_sess_fn = getattr(self._tier1_client, "get_session_id", None)
            if callable(get_sess_fn):
                res = get_sess_fn()
                raw_sid = await res if inspect.isawaitable(res) else res
                if isinstance(raw_sid, str) and raw_sid.strip():
                    sess_id = raw_sid.strip()
            else:
                attr_sid = getattr(self._tier1_client, "active_session_id", None)
                if isinstance(attr_sid, str) and attr_sid.strip():
                    sess_id = attr_sid.strip()

            if sess_id:
                now_ts = time.time()
                save_agent_fn = getattr(self._history_store, "save_agent_session", None)
                if callable(save_agent_fn):
                    await save_agent_fn(
                        AgentSessionPointer(
                            workspace_canonical_path=active_ws.canonical_path,
                            tier="tier1",
                            agent_provider=self._settings.tier1_engine,
                            session_id=sess_id,
                            updated_at=now_ts,
                        )
                    )
                if self._settings.tier1_engine == "pi":
                    await self._history_store.save_session_pointer(
                        WorkspaceSessionPointer(
                            workspace_canonical_path=active_ws.canonical_path,
                            pi_session_id=sess_id,
                            updated_at=now_ts,
                        )
                    )

        directives = directive_filter.directives if directive_filter is not None else ()
        if directives and self._task_manager is not None:
            if splitter is not None:
                for chunk in splitter.flush():
                    await self._event_bus.publish(
                        VoiceSpeechChunkEvent(
                            correlation_id=correlation_id,
                            text=chunk,
                            chunk_index=chunk_index,
                            is_final=False,
                            priority="normal",
                        )
                    )
                    chunk_index += 1
            summaries = await self._execute_tier1_directives(
                directives,
                active_ws=active_ws,
                correlation_id=correlation_id,
            )
            combined_reply = " ".join(s for s in summaries if s).strip()
            if combined_reply:
                await self._emit_orchestrator_reply(
                    combined_reply,
                    correlation_id=correlation_id,
                    speak_full_response=speak_full_response,
                    start_chunk_index=chunk_index,
                )
            elif splitter is not None:
                await self._event_bus.publish(
                    VoiceSpeechChunkEvent(
                        correlation_id=correlation_id,
                        text="Listo.",
                        chunk_index=chunk_index,
                        is_final=True,
                        priority="normal",
                    )
                )
            return

        if splitter is not None:
            tail = splitter.flush()
            if not tail:
                tail = [
                    "Listo, completé la consulta en el proyecto."
                    if chunk_index == 0
                    else "Tienes el detalle en el chat."
                ]
            for idx, chunk in enumerate(tail):
                await self._event_bus.publish(
                    VoiceSpeechChunkEvent(
                        correlation_id=correlation_id,
                        text=chunk,
                        chunk_index=chunk_index,
                        is_final=(idx == len(tail) - 1),
                        priority="normal",
                    )
                )
                chunk_index += 1
            return

        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=correlation_id,
                summary_text=sanitize_for_voice(
                    joined,
                    fallback="Listo, complete la consulta en el proyecto.",
                    extra_secrets=self._extra_secrets(),
                ),
                priority="normal",
            )
        )

    @staticmethod
    def _is_home_workspace_hint(hint: str) -> bool:
        cleaned = hint.strip()
        if cleaned in ("~", "~/", "$HOME", "${HOME}"):
            return True
        try:
            home_dir = Path.home().resolve()
            expanded = Path(cleaned).expanduser()
            if expanded.is_absolute() and expanded.resolve() == home_dir:
                return True
        except OSError:
            return False
        return False

    async def _resolve_directive_workspace(
        self,
        workspace_hint: str | None,
        active_ws: WorkspaceContext | None,
    ) -> tuple[str, str] | None:
        hint = (workspace_hint or "").strip()
        if hint:
            if await asyncio.to_thread(self._is_home_workspace_hint, hint):
                raise TaskManagerError(
                    code="WORKSPACE_HOME_FORBIDDEN",
                    message="Home directory cannot be used as a task workspace.",
                )
            hint_lower = hint.lower()
            list_entries = getattr(self._workspace_resolver, "list_workspace_entries", None)
            if callable(list_entries):
                entries = await list_entries()
                for entry in entries:
                    if hint_lower in (entry.name.lower(), entry.canonical_path.lower()):
                        return entry.canonical_path, entry.name
            list_avail = getattr(self._workspace_resolver, "list_available_workspaces", None)
            if callable(list_avail):
                pairs = await list_avail()
                for name, canonical in pairs:
                    if hint_lower in (name.lower(), canonical.lower()):
                        return canonical, name
            if active_ws is not None and hint_lower in (
                active_ws.name.lower(),
                active_ws.canonical_path.lower(),
            ):
                return active_ws.canonical_path, active_ws.name
            raise TaskManagerError(
                code="WORKSPACE_NOT_FOUND",
                message=f"Workspace '{self._redact(hint)}' was not found in allowed workspaces.",
            )
        if active_ws is not None:
            return active_ws.canonical_path, active_ws.name
        return None

    async def _execute_tier1_directives(
        self,
        directives: tuple[Tier1ActionDirective, ...],
        *,
        active_ws: WorkspaceContext | None,
        correlation_id: str,
    ) -> list[str]:
        assert self._task_manager is not None
        summaries: list[str] = []
        for directive in directives:
            try:
                if directive.action == "dispatch_task":
                    target_ws = await self._resolve_directive_workspace(
                        directive.workspace_hint,
                        active_ws,
                    )
                    if target_ws is None:
                        self._log(
                            "utterance_blocked_no_workspace",
                            correlation_id=correlation_id,
                            tier="dispatch_task",
                        )
                        summaries.append(
                            "Para despachar tareas de código, primero selecciona "
                            "un proyecto en pantalla."
                        )
                        continue
                    ws_path, ws_name = target_ws
                    target_provider = directive.provider_hint or self._settings.tier2_engine
                    target_model = (
                        directive.model_hint
                        if directive.model_hint
                        else (
                            self._settings.default_tier2_model
                            if target_provider == self._settings.tier2_engine
                            else None
                        )
                    )
                    task = await self._task_manager.dispatch_task(
                        workspace_canonical_path=ws_path,
                        workspace_name=ws_name,
                        objective=directive.objective or "",
                        provider=target_provider,
                        model=target_model,
                        correlation_id=correlation_id,
                    )
                    summaries.append(
                        f"Tarea {task.task_id} enviada a {task.provider} en {task.workspace_name}. "
                        "Te aviso cuando termine."
                    )
                elif directive.action == "list_tasks":
                    tasks = await self._task_manager.list_tasks(limit=10)
                    if directive.status_filter == "active":
                        filtered = [t for t in tasks if t.status not in TERMINAL_TASK_STATUSES]
                    elif directive.status_filter == "all":
                        filtered = tasks
                    else:
                        filtered = [t for t in tasks if t.status == directive.status_filter]
                    summaries.append(self._format_task_list_summary(filtered))
                elif directive.action == "get_task_status":
                    if directive.task_id:
                        rec = await self._task_manager.get_task(directive.task_id)
                        summaries.append(self._format_single_task_status(rec))
                    else:
                        recent = await self._task_manager.list_tasks(limit=5)
                        active = [t for t in recent if t.status not in TERMINAL_TASK_STATUSES]
                        chosen = active[0] if active else (recent[0] if recent else None)
                        if chosen is None:
                            summaries.append("No hay tareas registradas todavía.")
                        else:
                            summaries.append(self._format_single_task_status(chosen))
                elif directive.action == "cancel_task":
                    target_id = directive.task_id
                    if not target_id:
                        recent = await self._task_manager.list_tasks(limit=10)
                        active = [t for t in recent if t.status not in TERMINAL_TASK_STATUSES]
                        if not active:
                            summaries.append("No hay ninguna tarea activa para cancelar.")
                            continue
                        if len(active) > 1:
                            ids = ", ".join(t.task_id for t in active[:4])
                            summaries.append(
                                f"Hay varias tareas activas ({ids}). Indícame cuál cancelar."
                            )
                            continue
                        target_id = active[0].task_id
                    cancelled = await self._task_manager.cancel_task(target_id)
                    summaries.append(
                        f"Tarea {cancelled.task_id} en estado {cancelled.status}."
                    )
            except TaskManagerError as exc:
                summaries.append(f"No pude completar la acción: {exc.sanitized_message}")
        return summaries

    @staticmethod
    def _format_task_list_summary(tasks: list[AgentTaskRecord]) -> str:
        if not tasks:
            return "No hay tareas activas en este momento."
        parts = [
            f"{t.task_id} ({t.provider} en {t.workspace_name}: {t.status})"
            for t in tasks[:5]
        ]
        return f"Tareas actuales ({len(tasks)}): " + "; ".join(parts) + "."

    @staticmethod
    def _format_single_task_status(task: AgentTaskRecord) -> str:
        detail = f" Último avance: {task.last_log}" if task.last_log else ""
        return (
            f"La tarea {task.task_id} ({task.provider} en {task.workspace_name}) "
            f"está {task.status}.{detail}"
        )

    async def _run_tier2(
        self,
        utterance: str,
        workspace_path: str,
        correlation_id: str,
        *,
        speak_full_response: bool = False,
    ) -> None:
        cleaned_prompt = _DELEGATE_MARKER_RE.sub("", utterance).strip() or utterance.strip()
        prior_conv = self._tier2_conversations.get(workspace_path)
        chunk_index = 0

        if speak_full_response:
            await self._event_bus.publish(
                VoiceSpeechChunkEvent(
                    correlation_id=correlation_id,
                    text="Tarea compleja detectada. Delegando ejecución profunda.",
                    chunk_index=chunk_index,
                    is_final=False,
                    priority="normal",
                )
            )
            chunk_index += 1
        else:
            await self._event_bus.publish(
                VoiceMilestoneEvent(
                    correlation_id=correlation_id,
                    summary_text=(
                        "Tarea compleja detectada. Delegando ejecucion profunda a Antigravity."
                    ),
                    priority="normal",
                )
            )

        last_milestone_stage: str | None = None
        final_summary = "Ejecucion profunda completada en Antigravity."
        final_response_raw = ""
        splitter = (
            VoiceSentenceStreamSplitter(extra_secrets=self._extra_secrets())
            if speak_full_response
            else None
        )
        emitted_body_chunks = 0

        request = CodingTaskRequest(
            correlation_id=correlation_id,
            workspace_path=workspace_path,
            prompt=cleaned_prompt,
            model=self._settings.default_tier2_model,
            conversation_id=prior_conv,
        )
        async with _closing_async_iter(self._tier2_runner.execute(request)) as stream:
            async for progress in stream:
                if progress.conversation_id and progress.conversation_id != "unknown":
                    self._tier2_conversations[workspace_path] = progress.conversation_id
                    if self._history_store is not None:
                        now_ts = time.time()
                        save_agent_fn = getattr(self._history_store, "save_agent_session", None)
                        if callable(save_agent_fn):
                            await save_agent_fn(
                                AgentSessionPointer(
                                    workspace_canonical_path=workspace_path,
                                    tier="tier2",
                                    agent_provider=self._settings.tier2_engine,
                                    session_id=progress.conversation_id,
                                    updated_at=now_ts,
                                )
                            )
                        if self._settings.tier2_engine == "agy":
                            await self._history_store.save_session_pointer(
                                WorkspaceSessionPointer(
                                    workspace_canonical_path=workspace_path,
                                    agy_conversation_id=progress.conversation_id,
                                    updated_at=now_ts,
                                )
                            )

                if progress.kind == "delta" and progress.text_delta:
                    redacted_delta = self._redact(progress.text_delta)
                    await self._event_bus.publish(
                        ChatStreamEvent(
                            correlation_id=correlation_id,
                            delta=redacted_delta,
                            source="tier2_runner",
                            step_type=progress.step_type,
                        )
                    )
                    if splitter is not None:
                        for ready_chunk in splitter.feed(redacted_delta):
                            await self._event_bus.publish(
                                VoiceSpeechChunkEvent(
                                    correlation_id=correlation_id,
                                    text=ready_chunk,
                                    chunk_index=chunk_index,
                                    is_final=False,
                                    priority="normal",
                                )
                            )
                            chunk_index += 1
                            emitted_body_chunks += 1
                    elif progress.step_type != last_milestone_stage:
                        last_milestone_stage = progress.step_type
                        await self._event_bus.publish(
                            VoiceMilestoneEvent(
                                correlation_id=correlation_id,
                                summary_text=sanitize_for_voice(
                                    redacted_delta,
                                    fallback=f"Avanzando en etapa {progress.step_type}.",
                                    extra_secrets=self._extra_secrets(),
                                ),
                                priority="normal",
                            )
                        )
                elif (
                    progress.kind == "milestone"
                    and progress.step_type != last_milestone_stage
                ):
                    last_milestone_stage = progress.step_type
                    if not speak_full_response:
                        await self._event_bus.publish(
                            VoiceMilestoneEvent(
                                correlation_id=correlation_id,
                                summary_text=sanitize_for_voice(
                                    f"Entrando en etapa {progress.step_type}.",
                                    fallback="Avanzando en el proyecto.",
                                    extra_secrets=self._extra_secrets(),
                                ),
                                priority="normal",
                            )
                        )
                elif progress.kind == "completed":
                    if progress.final_response:
                        redacted_final = self._redact(progress.final_response)
                        final_response_raw = redacted_final
                        await self._event_bus.publish(
                            ChatStreamEvent(
                                correlation_id=correlation_id,
                                delta=redacted_final,
                                source="tier2_runner",
                                step_type="result",
                            )
                        )
                        final_summary = sanitize_for_voice(
                            redacted_final,
                            fallback=final_summary,
                            extra_secrets=self._extra_secrets(),
                        )

        if splitter is not None:
            tail = splitter.flush()
            if not tail and emitted_body_chunks == 0:
                tail = VoiceSentenceStreamSplitter.split_text(
                    final_response_raw or final_summary,
                    extra_secrets=self._extra_secrets(),
                )
            if not tail:
                tail = [final_summary]
            for idx, chunk in enumerate(tail):
                await self._event_bus.publish(
                    VoiceSpeechChunkEvent(
                        correlation_id=correlation_id,
                        text=chunk,
                        chunk_index=chunk_index,
                        is_final=(idx == len(tail) - 1),
                        priority="normal",
                    )
                )
                chunk_index += 1
            return

        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=correlation_id,
                summary_text=final_summary,
                priority="normal",
            )
        )
