"""Two-layer SecurityGatekeeper coordinator and human approval lifecycle (NOVA-002 OP-4)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from nova.application.deterministic_policy import DeterministicCommandPolicy
from nova.domain.errors import (
    ApprovalNotFoundError,
    GatekeeperError,
    redact_sensitive_text,
)
from nova.domain.events import (
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    CommandBlockedEvent,
    OrchestratorEventBus,
    VoiceMilestoneEvent,
)
from nova.domain.models import (
    AuditRiskLevel,
    CommandAuditInput,
    CommandAuditResult,
    CommandExecutionDecision,
    CommandInterceptRequest,
)
from nova.domain.ports import CommandAuditorPort, WorkspaceResolverPort

logger = logging.getLogger("nova.security_gatekeeper")


class GatekeeperSettings(BaseModel):
    """Configuration boundary for SecurityGatekeeper."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    approval_timeout_seconds: float = Field(default=120.0, gt=0.0, le=300.0)
    max_pending_approvals: int = Field(default=16, ge=1, le=64)


@dataclass(slots=True)
class _PendingApproval:
    audit_id: str
    correlation_id: str
    future: asyncio.Future[tuple[bool, str]]


class SecurityGatekeeper:
    """Enforces Layer 1 deterministic policy + Layer 2 auditor + explicit human approval."""

    def __init__(
        self,
        workspace_resolver: WorkspaceResolverPort,
        auditor: CommandAuditorPort,
        event_bus: OrchestratorEventBus,
        policy: DeterministicCommandPolicy | None = None,
        settings: GatekeeperSettings | None = None,
    ) -> None:
        self._workspace_resolver = workspace_resolver
        self._auditor = auditor
        self._event_bus = event_bus
        self._policy = policy or DeterministicCommandPolicy()
        self._settings = settings or GatekeeperSettings()
        self._pending: dict[str, _PendingApproval] = {}
        self._extra_secrets: list[str] = []

    @property
    def event_bus(self) -> OrchestratorEventBus:
        """Expose event bus reference for orchestration wiring."""
        return self._event_bus

    @property
    def extra_secrets(self) -> tuple[str, ...]:
        """Expose registered ephemeral session secrets for cross-component redaction."""
        return tuple(self._extra_secrets)

    @property
    def pending_count(self) -> int:
        """Current count of pending human command approvals."""
        return len(self._pending)

    def register_ephemeral_secret(self, secret: str) -> None:
        """Register an ephemeral session token to scrub from all logs, events, and prompts."""
        if secret and secret not in self._extra_secrets:
            self._extra_secrets.append(secret)

    def _redact(self, text: str) -> str:
        return redact_sensitive_text(text, extra_secrets=tuple(self._extra_secrets))

    def _log(self, event_name: str, **fields: str | bool) -> None:
        payload: dict[str, str | bool] = {"event": event_name}
        for key, value in fields.items():
            payload[key] = self._redact(value) if isinstance(value, str) else value
        logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    async def evaluate_and_authorize(
        self,
        request: CommandInterceptRequest,
    ) -> CommandExecutionDecision:
        """Intercept a CLI command pre-execution and return allow/deny decision."""
        active_ws = self._workspace_resolver.active_context
        if active_ws is None:
            return CommandExecutionDecision(
                allowed=False,
                verdict="invalid_request",
                audit_id=None,
                reason="No active workspace selected in Nova.",
            )

        layer1 = self._policy.evaluate(
            command=request.command,
            cwd=request.cwd,
            workspace_canonical_path=active_ws.canonical_path,
        )
        sanitized_cmd = self._redact(request.command)

        if layer1.verdict == "allow":
            self._log(
                "command_auto_allowed",
                correlation_id=request.correlation_id,
                rule_id=layer1.rule_id,
                source=request.source,
            )
            return CommandExecutionDecision(
                allowed=True,
                verdict="auto_allowed",
                audit_id=None,
                reason=layer1.reason,
            )

        audit_id = f"aud-{uuid.uuid4()}"
        if layer1.verdict == "hard_deny":
            return await self._handle_hard_deny(
                audit_id=audit_id,
                request=request,
                sanitized_cmd=sanitized_cmd,
                rule_id=layer1.rule_id,
                reason=layer1.reason,
            )

        risk_hint: AuditRiskLevel = (
            "medium" if layer1.risk_hint == "low" else layer1.risk_hint
        )
        return await self._handle_escalated_command(
            audit_id=audit_id,
            request=request,
            sanitized_cmd=sanitized_cmd,
            workspace_path=active_ws.canonical_path,
            rule_id=layer1.rule_id,
            risk_hint=risk_hint,
        )

    async def _handle_hard_deny(
        self,
        *,
        audit_id: str,
        request: CommandInterceptRequest,
        sanitized_cmd: str,
        rule_id: str,
        reason: str,
    ) -> CommandExecutionDecision:
        self._log(
            "command_hard_blocked",
            audit_id=audit_id,
            correlation_id=request.correlation_id,
            rule_id=rule_id,
        )
        await self._event_bus.publish(
            CommandBlockedEvent(
                audit_id=audit_id,
                correlation_id=request.correlation_id,
                source=request.source,
                sanitized_command=sanitized_cmd,
                rule_id=rule_id,
                reason=self._redact(reason),
            )
        )
        await self._event_bus.publish(
            VoiceMilestoneEvent(
                correlation_id=request.correlation_id,
                summary_text=(
                    "Bloquee por seguridad un comando destructivo sobre el sistema."
                ),
                priority="interrupt",
            )
        )
        return CommandExecutionDecision(
            allowed=False,
            verdict="hard_blocked",
            audit_id=audit_id,
            reason=f"GATEKEEPER_COMMAND_DENIED ({rule_id}): {self._redact(reason)}",
        )

    async def _handle_escalated_command(
        self,
        *,
        audit_id: str,
        request: CommandInterceptRequest,
        sanitized_cmd: str,
        workspace_path: str,
        rule_id: str,
        risk_hint: AuditRiskLevel,
    ) -> CommandExecutionDecision:
        if len(self._pending) >= self._settings.max_pending_approvals:
            raise GatekeeperError(
                code="GATEKEEPER_QUEUE_FULL",
                message=(
                    f"Maximum concurrent pending command approvals "
                    f"({self._settings.max_pending_approvals}) reached."
                ),
            )

        loop = asyncio.get_running_loop()
        pending = _PendingApproval(
            audit_id=audit_id,
            correlation_id=request.correlation_id,
            future=loop.create_future(),
        )
        self._pending[audit_id] = pending

        try:
            audit_result = await self._safe_audit(
                CommandAuditInput(
                    correlation_id=request.correlation_id,
                    command=sanitized_cmd,
                    cwd=request.cwd,
                    workspace_path=workspace_path,
                    rule_id=rule_id,
                    risk_hint=risk_hint,
                )
            )
            spoken = self._redact(audit_result.spoken_summary)
            explanation = self._redact(audit_result.technical_explanation)

            await self._event_bus.publish(
                CommandApprovalRequestedEvent(
                    audit_id=audit_id,
                    correlation_id=request.correlation_id,
                    source=request.source,
                    sanitized_command=sanitized_cmd,
                    cwd=request.cwd,
                    risk_level=audit_result.risk_level,
                    spoken_summary=spoken,
                    technical_explanation=explanation,
                    expires_in_seconds=self._settings.approval_timeout_seconds,
                )
            )
            await self._event_bus.publish(
                VoiceMilestoneEvent(
                    correlation_id=request.correlation_id,
                    summary_text=spoken,
                    priority="interrupt",
                )
            )

            return await self._await_human_decision(pending)
        finally:
            self._pending.pop(audit_id, None)

    async def _safe_audit(self, audit_input: CommandAuditInput) -> CommandAuditResult:
        try:
            return await self._auditor.audit(audit_input)
        except Exception:
            return CommandAuditResult(
                risk_level=audit_input.risk_hint,
                spoken_summary=(
                    "Necesito tu aprobacion antes de ejecutar este comando sensible en consola."
                ),
                technical_explanation=(
                    f"Retenido por regla '{audit_input.rule_id}'. "
                    "El subagente auditor fallo de forma segura (fail-closed)."
                ),
                audited_by="deterministic_fallback",
            )

    async def _await_human_decision(
        self,
        pending: _PendingApproval,
    ) -> CommandExecutionDecision:
        try:
            approved, resolved_by = await asyncio.wait_for(
                pending.future,
                timeout=self._settings.approval_timeout_seconds,
            )
        except TimeoutError:
            self._log(
                "command_approval_timed_out",
                audit_id=pending.audit_id,
                correlation_id=pending.correlation_id,
            )
            await self._event_bus.publish(
                CommandApprovalResolvedEvent(
                    audit_id=pending.audit_id,
                    correlation_id=pending.correlation_id,
                    approved=False,
                    verdict="timed_out",
                    resolved_by="timeout",
                )
            )
            return CommandExecutionDecision(
                allowed=False,
                verdict="timed_out",
                audit_id=pending.audit_id,
                reason="GATEKEEPER_APPROVAL_TIMEOUT: Approval window expired (fail-closed).",
            )

        verdict = "approved_by_human" if approved else "rejected_by_human"
        reason = (
            f"Approved by {resolved_by}."
            if approved
            else f"GATEKEEPER_COMMAND_DENIED: Rejected by {resolved_by}."
        )
        return CommandExecutionDecision(
            allowed=approved,
            verdict=verdict,
            audit_id=pending.audit_id,
            reason=reason,
        )

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_voice_or_ui",
    ) -> CommandApprovalResolvedEvent:
        """Resolve a pending approval request explicitly."""
        pending = self._pending.get(audit_id)
        if pending is None or pending.future.done():
            raise ApprovalNotFoundError(audit_id)

        pending.future.set_result((approved, resolved_by))
        verdict = "approved_by_human" if approved else "rejected_by_human"
        self._log(
            "command_approval_resolved",
            audit_id=audit_id,
            correlation_id=pending.correlation_id,
            approved=approved,
            resolved_by=resolved_by,
        )
        event = CommandApprovalResolvedEvent(
            audit_id=audit_id,
            correlation_id=pending.correlation_id,
            approved=approved,
            verdict=verdict,
            resolved_by=resolved_by,
        )
        await self._event_bus.publish(event)
        return event
