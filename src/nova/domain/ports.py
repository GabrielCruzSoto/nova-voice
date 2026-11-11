"""Hexagonal ports for Nova domain orchestration (zero external CLI coupling)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from nova.domain.models import CodingTaskRequest, RunnerProgressEvent


class CodingRunnerPort(Protocol):
    """Abstract port for Tier 2 heavy coding runners (OP-3)."""

    def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        """Stream structured progress events for a heavy coding task."""
        ...
