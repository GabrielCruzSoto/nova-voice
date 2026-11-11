"""Pydantic v2 boundary models for workspace selection and Tier 2 task requests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


WorkspaceQueryMode = Literal["exact_path", "voice_fuzzy"]


class ResolveWorkspaceRequest(BaseModel):
    """Boundary request for OP-1 WorkspaceResolver.resolve."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    query: str
    mode: WorkspaceQueryMode = "voice_fuzzy"


class WorkspaceContext(BaseModel):
    """Validated active workspace context."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
    active_since: str = Field(default_factory=utc_now_iso)


class CodingTaskRequest(BaseModel):
    """Domain request passed across CodingRunnerPort (OP-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    workspace_path: str = Field(min_length=1)
    prompt: str
    model: str = "claude-opus-5-5-high"
    conversation_id: str | None = None


RunnerEventKind = Literal["init", "delta", "milestone", "completed"]


class RunnerProgressEvent(BaseModel):
    """Domain progress item yielded by a CodingRunnerPort implementation."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: RunnerEventKind
    conversation_id: str
    step_type: str = "agent_response"
    text_delta: str = ""
    final_response: str | None = None
    duration_seconds: float | None = None


class CodingTaskResult(BaseModel):
    """Consolidated summary after a CodingRunnerPort turn completes."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    conversation_id: str
    response: str
    duration_seconds: float = 0.0
