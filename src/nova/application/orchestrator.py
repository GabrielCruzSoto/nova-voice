"""Two-tier complexity router and dual Chat+Voice event orchestrator (S4)."""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nova.domain.errors import NovaDomainError, WorkspaceResolutionError
from nova.domain.events import (
    ChatStreamEvent,
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
    Tier1ConversationPort,
    WorkspaceResolverPort,
)

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

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_PATH_TOKEN_RE = re.compile(r"(?:~|/)[\w./-]+")


class OrchestratorSettings(BaseModel):
    """Application settings for NovaOrchestrator."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    default_tier2_model: str = Field(default="claude-opus-5-5-high", min_length=1)


def classify_complexity(utterance: str) -> Literal["low", "high"]:
    """Classify a user instruction into Tier 1 ('low') or Tier 2 ('high')."""
    normalized = utterance.lower()
    if "[delegate_tier2]" in normalized:
        return "high"

    hits = sum(1 for kw in _HIGH_COMPLEXITY_KEYWORDS if kw in normalized)
    if hits >= 2 or (hits >= 1 and len(normalized.split()) >= 14):
        return "high"
    return "low"


def sanitize_for_voice(raw_text: str, fallback: str, max_words: int = 20) -> str:
    """Strip code blocks and filesystem paths, returning a <=25-word spoken summary."""
    without_code = _CODE_BLOCK_RE.sub(" ", raw_text)
    without_paths = _PATH_TOKEN_RE.sub("archivo del proyecto", without_code)
    cleaned = re.sub(r"[`*_#>]+", " ", without_paths)
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
    ) -> None:
        self._workspace_resolver = workspace_resolver
        self._tier1_client = tier1_client
        self._tier2_runner = tier2_runner
        self._event_bus = event_bus
        self._settings = settings or OrchestratorSettings()
        self._tier2_conversations: dict[str, str] = {}

    async def select_workspace(
        self,
        query: str,
        mode: WorkspaceQueryMode = "voice_fuzzy",
        correlation_id: str | None = None,
    ) -> WorkspaceContext:
        """Resolve, activate, and announce a target project directory."""
        corr_id = correlation_id or f"corr-{uuid.uuid4().hex[:12]}"
        context = await self._workspace_resolver.resolve(
            ResolveWorkspaceRequest(
                correlation_id=corr_id,
                query=query,
                mode=mode,
            )
        )
        await self._tier1_client.switch_workspace(context.canonical_path)
        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=corr_id,
                summary_text=sanitize_for_voice(
                    f"Proyecto activo cambiado a {context.name}.",
                    fallback="Proyecto activo actualizado.",
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
        corr_id = correlation_id or f"corr-{uuid.uuid4().hex[:12]}"
        active_ws = self._workspace_resolver.active_context
        if active_ws is None:
            raise WorkspaceResolutionError(
                code="WORKSPACE_NOT_FOUND",
                message="No active workspace selected before handling utterance.",
            )

        tier = classify_complexity(utterance)
        try:
            if tier == "low":
                await self._run_tier1(utterance, corr_id)
            else:
                await self._run_tier2(utterance, active_ws.canonical_path, corr_id)
        except NovaDomainError as exc:
            await self._event_bus.publish(
                RunnerFailureEvent(
                    correlation_id=corr_id,
                    error_code=exc.code,
                    sanitized_message=exc.sanitized_message,
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
        await self._tier1_client.barge_in_abort()

    async def _run_tier1(self, utterance: str, correlation_id: str) -> None:
        collected: list[str] = []
        async for delta in self._tier1_client.stream_prompt(utterance):
            collected.append(delta)
            await self._event_bus.publish(
                ChatStreamEvent(
                    correlation_id=correlation_id,
                    delta=delta,
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
        cleaned_prompt = utterance.replace("[DELEGATE_TIER2]", "").strip() or utterance.strip()
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

        emitted_working_milestone = False
        final_summary = "Ejecucion profunda completada en Antigravity."

        async for progress in self._tier2_runner.execute(
            CodingTaskRequest(
                correlation_id=correlation_id,
                workspace_path=workspace_path,
                prompt=cleaned_prompt,
                model=self._settings.default_tier2_model,
                conversation_id=prior_conv,
            )
        ):
            if progress.conversation_id and progress.conversation_id != "unknown":
                self._tier2_conversations[workspace_path] = progress.conversation_id

            if progress.kind == "delta" and progress.text_delta:
                await self._event_bus.publish(
                    ChatStreamEvent(
                        correlation_id=correlation_id,
                        delta=progress.text_delta,
                        source="tier2_runner",
                        step_type=progress.step_type,
                    )
                )
                if not emitted_working_milestone:
                    emitted_working_milestone = True
                    await self._event_bus.publish(
                        VoiceMilestoneEvent(
                            correlation_id=correlation_id,
                            summary_text=sanitize_for_voice(
                                progress.text_delta,
                                fallback="Avanzando en los cambios del repositorio.",
                            ),
                            priority="normal",
                        )
                    )
            elif progress.kind == "completed":
                if progress.final_response:
                    await self._event_bus.publish(
                        ChatStreamEvent(
                            correlation_id=correlation_id,
                            delta=progress.final_response,
                            source="tier2_runner",
                            step_type="result",
                        )
                    )
                    final_summary = sanitize_for_voice(
                        progress.final_response,
                        fallback=final_summary,
                    )

        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=correlation_id,
                summary_text=final_summary,
                priority="normal",
            )
        )
