"""Hexagonal ports for Nova domain orchestration (zero external CLI coupling)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from nova.domain.events import CommandApprovalResolvedEvent
from nova.domain.models import (
    CodingTaskRequest,
    CommandAuditInput,
    CommandAuditResult,
    CommandExecutionDecision,
    CommandInterceptRequest,
    ResolveWorkspaceRequest,
    RunnerProgressEvent,
    WorkspaceContext,
)


class WorkspaceResolverPort(Protocol):
    """Abstract port for workspace lookup and confinement (OP-1)."""

    @property
    def active_context(self) -> WorkspaceContext | None:
        """Return currently selected workspace context, if any."""
        ...

    async def resolve(self, request: ResolveWorkspaceRequest) -> WorkspaceContext:
        """Resolve and activate a workspace directory."""
        ...


class Tier1ConversationPort(Protocol):
    """Abstract port for Tier 1 persistent conversational agent (OP-2)."""

    async def switch_workspace(self, new_workspace_path: str) -> None:
        """Switch active directory of the Tier 1 agent."""
        ...

    def stream_prompt(self, message: str) -> AsyncIterator[str]:
        """Stream text deltas for a conversational or low-complexity turn."""
        ...

    async def barge_in_abort(self) -> None:
        """Clear pending queue and abort active Tier 1 turn immediately."""
        ...


class CodingRunnerPort(Protocol):
    """Abstract port for Tier 2 heavy coding runners (OP-3)."""

    def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        """Stream structured progress events for a heavy coding task."""
        ...


class CommandAuditorPort(Protocol):
    """Abstract port for Layer 2 isolated command security auditor (NOVA-002 OP-3)."""

    async def audit(self, request: CommandAuditInput) -> CommandAuditResult:
        """Analyze a dubious shell command and return a structured explanation."""
        ...


class SecurityGatekeeperPort(Protocol):
    """Abstract port for pre-execution command authorization and human approval."""

    async def evaluate_and_authorize(
        self,
        request: CommandInterceptRequest,
    ) -> CommandExecutionDecision:
        """Run Layer 1 + Layer 2 and return final allow/deny decision."""
        ...

    async def resolve_command_approval(
        self,
        audit_id: str,
        *,
        approved: bool,
        resolved_by: str = "user_voice_or_ui",
    ) -> CommandApprovalResolvedEvent:
        """Resolve a held command approval request explicitly."""
        ...
