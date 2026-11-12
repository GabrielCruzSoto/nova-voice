"""Two-tier complexity router and dual Chat+Voice event orchestrator (S4)."""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import (
    GatekeeperError,
    NovaDomainError,
    WorkspaceResolutionError,
    redact_sensitive_text,
)
from nova.domain.events import (
    ChatStreamEvent,
    CommandApprovalResolvedEvent,
    OrchestratorEventBus,
    RunnerFailureEvent,
    VoiceMilestoneEvent,
)
from nova.domain.models import (
    CodingTaskRequest,
    ResolveWorkspaceRequest,
    WorkspaceContext,
    WorkspaceQueryMode,
)
from nova.domain.ports import (
    CodingRunnerPort,
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
_PATH_TOKEN_RE = re.compile(r"(?:~|/)[\w./-]+")


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

    default_tier2_model: str = Field(default="claude-opus-5-5-high", min_length=1)


def classify_complexity(utterance: str) -> Literal["low", "high"]:
    """Classify a user instruction into Tier 1 ('low') or Tier 2 ('high')."""
    normalized = utterance.lower()
    if _DELEGATE_MARKER_RE.search(normalized) is not None:
        return "high"

    hits = sum(1 for kw in _HIGH_COMPLEXITY_KEYWORDS if kw in normalized)
    if hits >= 2 or (hits >= 1 and len(normalized.split()) >= 14):
        return "high"
    return "low"


def sanitize_for_voice(
    raw_text: str,
    fallback: str,
    max_words: int = 20,
    extra_secrets: tuple[str, ...] = (),
) -> str:
    """Strip code blocks and filesystem paths, returning a <=25-word spoken summary."""
    without_code = _CODE_BLOCK_RE.sub(" ", raw_text)
    without_paths = _PATH_TOKEN_RE.sub("archivo del proyecto", without_code)
    redacted = redact_sensitive_text(without_paths, extra_secrets=extra_secrets)
    cleaned = re.sub(r"[`*_#>]+", " ", redacted)
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
    ) -> None:
        self._workspace_resolver = workspace_resolver
        self._tier1_client = tier1_client
        self._tier2_runner = tier2_runner
        self._event_bus = event_bus
        self._settings = settings or OrchestratorSettings()
        self._security_gatekeeper = security_gatekeeper
        self._tier2_conversations: dict[str, str] = {}

    def _extra_secrets(self) -> tuple[str, ...]:
        if self._security_gatekeeper is None:
            return ()
        return self._security_gatekeeper.extra_secrets

    def _redact(self, text: str) -> str:
        return redact_sensitive_text(text, extra_secrets=self._extra_secrets())

    def _log(self, event_name: str, **fields: str | bool) -> None:
        _log_structured(event_name, extra_secrets=self._extra_secrets(), **fields)

    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "voice_fuzzy",
        correlation_id: str | None = None,
    ) -> WorkspaceContext:
        """Resolve, activate, and announce a target project directory."""
        corr_id = correlation_id or f"corr-{uuid.uuid4()}"
        context = await self._workspace_resolver.resolve(
            ResolveWorkspaceRequest(
                correlation_id=corr_id,
                query=query,
                mode=mode,
            )
        )
        await self._tier1_client.switch_workspace(context.canonical_path)
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

    async def handle_user_utterance(
        self,
        utterance: str,
        correlation_id: str | None = None,
    ) -> None:
        """Route a user utterance by complexity and emit synchronized Chat + Voice events."""
        corr_id = correlation_id or f"corr-{uuid.uuid4()}"
        active_ws = self._workspace_resolver.active_context
        if active_ws is None:
            raise WorkspaceResolutionError(
                code="WORKSPACE_NOT_FOUND",
                message="No active workspace selected before handling utterance.",
            )

        tier = classify_complexity(utterance)
        self._log(
            "utterance_routed",
            correlation_id=corr_id,
            tier=tier,
            workspace_name=active_ws.name,
        )
        try:
            if tier == "low":
                await self._run_tier1(utterance, corr_id)
            else:
                await self._run_tier2(utterance, active_ws.canonical_path, corr_id)
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
                        "Ocurrio un problema al ejecutar la tarea. Revisa el detalle en el chat."
                    ),
                    priority="interrupt",
                )
            )

    async def barge_in(self) -> None:
        """Interrupt active speech/agent turn immediately on user voice activity."""
        self._log("barge_in_triggered")
        await self._tier1_client.barge_in_abort()

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

    async def _run_tier1(self, utterance: str, correlation_id: str) -> None:
        collected: list[str] = []
        async with _closing_async_iter(self._tier1_client.stream_prompt(utterance)) as stream:
            async for delta in stream:
                redacted_delta = self._redact(delta)
                collected.append(redacted_delta)
                await self._event_bus.publish(
                    ChatStreamEvent(
                        correlation_id=correlation_id,
                        delta=redacted_delta,
                        source="tier1_pi",
                        step_type="agent_response",
                    )
                )

        joined = "".join(collected)
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

    async def _run_tier2(
        self,
        utterance: str,
        workspace_path: str,
        correlation_id: str,
    ) -> None:
        cleaned_prompt = _DELEGATE_MARKER_RE.sub("", utterance).strip() or utterance.strip()
        prior_conv = self._tier2_conversations.get(workspace_path)

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
                    if progress.step_type != last_milestone_stage:
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
                    await self._event_bus.publish(
                        VoiceMilestoneEvent(
                            correlation_id=correlation_id,
                            summary_text=sanitize_for_voice(
                                f"Entrando en etapa {progress.step_type}.",
                                fallback="Avanzando en el proyecto.",
                            ),
                            priority="normal",
                        )
                    )
                elif progress.kind == "completed":
                    if progress.final_response:
                        redacted_final = self._redact(progress.final_response)
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

        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=correlation_id,
                summary_text=final_summary,
                priority="normal",
            )
        )
