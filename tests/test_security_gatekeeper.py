"""Integration tests for SecurityGatekeeper Layer 1 + Layer 2 + Human Approval (S2, S4, OP-4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.security_gatekeeper import GatekeeperSettings, SecurityGatekeeper
from nova.domain.errors import ApprovalNotFoundError, GatekeeperError
from nova.domain.events import (
    CommandApprovalRequestedEvent,
    CommandApprovalResolvedEvent,
    CommandBlockedEvent,
    OrchestratorEvent,
    OrchestratorEventBus,
    VoiceMilestoneEvent,
)
from nova.domain.models import (
    CommandAuditInput,
    CommandAuditResult,
    CommandInterceptRequest,
    ResolveWorkspaceRequest,
)


class _FakeAuditor:
    def __init__(self, raise_exc: bool = False) -> None:
        self.inputs: list[CommandAuditInput] = []
        self.raise_exc = raise_exc

    async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
        self.inputs.append(request)
        if self.raise_exc:
            raise RuntimeError("Unexpected auditor failure")
        return CommandAuditResult(
            risk_level="high",
            spoken_summary=(
                "Necesito tu aprobacion para ejecutar sudo apt update con permisos de superusuario."
            ),
            technical_explanation="Ejecuta apt update con privilegios root mediante sudo.",
            audited_by="pi_llm",
        )


async def _setup_gatekeeper(
    tmp_path: Path,
    timeout_seconds: float = 2.0,
    max_pending: int = 16,
    raise_auditor: bool = False,
) -> tuple[SecurityGatekeeper, _FakeAuditor, list[OrchestratorEvent], Path]:
    root = tmp_path / "Proyectos"
    ws = root / "core-api"
    ws.mkdir(parents=True)

    bus = OrchestratorEventBus()
    events: list[OrchestratorEvent] = []

    async def collect(ev: OrchestratorEvent) -> None:
        events.append(ev)

    bus.subscribe(collect)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)
    await resolver.resolve(
        ResolveWorkspaceRequest(correlation_id="c-init", query=str(ws), mode="exact_path")
    )
    events.clear()

    auditor = _FakeAuditor(raise_exc=raise_auditor)
    gatekeeper = SecurityGatekeeper(
        workspace_resolver=resolver,
        auditor=auditor,
        event_bus=bus,
        settings=GatekeeperSettings(
            approval_timeout_seconds=timeout_seconds,
            max_pending_approvals=max_pending,
        ),
    )
    return gatekeeper, auditor, events, ws.resolve()


@pytest.mark.asyncio
async def test_ac1_ac2_allow_and_hard_deny_bypass_auditor_appropriately(tmp_path: Path) -> None:
    gatekeeper, auditor, events, ws = await _setup_gatekeeper(tmp_path)

    allow_decision = await gatekeeper.evaluate_and_authorize(
        CommandInterceptRequest(
            correlation_id="c-allow",
            source="tier1_pi",
            tool_name="bash",
            command="pytest -q",
            cwd=str(ws),
        )
    )
    assert allow_decision.allowed is True
    assert allow_decision.verdict == "auto_allowed"
    assert len(auditor.inputs) == 0

    deny_decision = await gatekeeper.evaluate_and_authorize(
        CommandInterceptRequest(
            correlation_id="c-deny",
            source="tier2_agy",
            tool_name="run_command",
            command="rm -rf /",
            cwd=str(ws),
        )
    )
    assert deny_decision.allowed is False
    assert deny_decision.verdict == "hard_blocked"
    assert len(auditor.inputs) == 0
    assert any(isinstance(e, CommandBlockedEvent) for e in events)
    assert any(
        isinstance(e, VoiceMilestoneEvent) and e.priority == "interrupt" for e in events
    )


@pytest.mark.asyncio
async def test_ac3_dubious_command_pauses_emits_voice_and_executes_when_approved(
    tmp_path: Path,
) -> None:
    gatekeeper, auditor, events, ws = await _setup_gatekeeper(tmp_path)
    gatekeeper.register_ephemeral_secret("ephemeral_ipc_tok_9999")

    # Approve right inside the event subscriber to prove zero race condition on _pending
    async def auto_approver(ev: OrchestratorEvent) -> None:
        if isinstance(ev, CommandApprovalRequestedEvent):
            await gatekeeper.resolve_command_approval(ev.audit_id, approved=True)

    gatekeeper.event_bus.subscribe(auto_approver)

    decision = await gatekeeper.evaluate_and_authorize(
        CommandInterceptRequest(
            correlation_id="c-approve",
            source="tier1_pi",
            tool_name="bash",
            command="sudo apt update --header='Bearer secret_tok_abc' ephemeral_ipc_tok_9999",
            cwd=str(ws),
        )
    )

    assert decision.allowed is True
    assert decision.verdict == "approved_by_human"
    assert len(auditor.inputs) == 1

    req_events = [e for e in events if isinstance(e, CommandApprovalRequestedEvent)]
    res_events = [e for e in events if isinstance(e, CommandApprovalResolvedEvent)]
    voice_events = [e for e in events if isinstance(e, VoiceMilestoneEvent)]

    assert len(req_events) == 1
    assert "secret_tok_abc" not in req_events[0].sanitized_command
    assert "ephemeral_ipc_tok_9999" not in req_events[0].sanitized_command
    assert "<REDACTED:secret>" in req_events[0].sanitized_command
    assert len(res_events) == 1
    assert res_events[0].approved is True
    assert len(voice_events) == 1
    assert voice_events[0].priority == "interrupt"


@pytest.mark.asyncio
async def test_ac4_human_rejection_and_timeout_fail_closed(tmp_path: Path) -> None:
    gatekeeper, _, events, ws = await _setup_gatekeeper(tmp_path, timeout_seconds=0.15)

    # 1. Timeout fail-closed
    timeout_decision = await gatekeeper.evaluate_and_authorize(
        CommandInterceptRequest(
            correlation_id="c-timeout",
            source="tier2_agy",
            tool_name="run_command",
            command="git push origin master",
            cwd=str(ws),
        )
    )
    assert timeout_decision.allowed is False
    assert timeout_decision.verdict == "timed_out"

    # 2. Explicit human rejection
    async def reject_later() -> None:
        await asyncio.sleep(0.03)
        req_ev = next(
            e
            for e in reversed(events)
            if isinstance(e, CommandApprovalRequestedEvent) and e.correlation_id == "c-reject"
        )
        await gatekeeper.resolve_command_approval(req_ev.audit_id, approved=False)

    reject_task = asyncio.create_task(reject_later())
    reject_decision = await gatekeeper.evaluate_and_authorize(
        CommandInterceptRequest(
            correlation_id="c-reject",
            source="tier1_pi",
            tool_name="bash",
            command="sudo reboot",
            cwd=str(ws),
        )
    )
    await reject_task
    assert reject_decision.allowed is False
    assert reject_decision.verdict == "rejected_by_human"

    with pytest.raises(ApprovalNotFoundError):
        await gatekeeper.resolve_command_approval("aud-missing", approved=True)


@pytest.mark.asyncio
async def test_queue_cap_and_auditor_exception_stay_fail_closed(tmp_path: Path) -> None:
    gatekeeper, _, _, ws = await _setup_gatekeeper(
        tmp_path,
        timeout_seconds=0.25,
        max_pending=1,
        raise_auditor=True,
    )

    first_task = asyncio.create_task(
        gatekeeper.evaluate_and_authorize(
            CommandInterceptRequest(
                correlation_id="c-q1",
                source="tier1_pi",
                tool_name="bash",
                command="sudo whoami",
                cwd=str(ws),
            )
        )
    )
    await asyncio.sleep(0.03)

    with pytest.raises(GatekeeperError) as exc_info:
        await gatekeeper.evaluate_and_authorize(
            CommandInterceptRequest(
                correlation_id="c-q2",
                source="tier1_pi",
                tool_name="bash",
                command="sudo id",
                cwd=str(ws),
            )
        )
    assert exc_info.value.code == "GATEKEEPER_QUEUE_FULL"

    first_result = await first_task
    assert first_result.allowed is False
    assert first_result.verdict == "timed_out"
