"""Hexagonal ports for Nova domain orchestration (zero external CLI coupling)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from nova.domain.models import (
    CodingTaskRequest,
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
