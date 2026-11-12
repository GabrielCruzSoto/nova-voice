"""Pydantic v2 boundary models for workspace selection, Tier 2 tasks, and Security Gatekeeper."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SPOKEN_WORDS = 25


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_spoken_word_budget(value: str) -> str:
    """Normalize whitespace and enforce the <=25-word spoken TTS budget."""
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError("spoken text cannot be empty")
    word_count = len(cleaned.split(" "))
    if word_count > MAX_SPOKEN_WORDS:
        raise ValueError(f"spoken text exceeds {MAX_SPOKEN_WORDS} words (got {word_count})")
    return cleaned


WorkspaceQueryMode = Literal["exact_path", "voice_fuzzy"]
CommandSource = Literal["tier1_pi", "tier2_agy"]
ToolInterceptName = Literal["bash", "run_command"]
RiskLevel = Literal["low", "medium", "high", "critical"]
AuditRiskLevel = Literal["medium", "high", "critical"]
Layer1Verdict = Literal["allow", "hard_deny", "escalate"]
DecisionVerdict = Literal[
    "auto_allowed",
    "hard_blocked",
    "approved_by_human",
    "rejected_by_human",
    "timed_out",
    "unauthorized",
    "invalid_request",
]


class ResolveWorkspaceRequest(BaseModel):
    """Boundary request for OP-1 WorkspaceResolver.resolve."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    query: str
    mode: str = "voice_fuzzy"


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


class Layer1Decision(BaseModel):
    """Pure deterministic Layer 1 command policy decision (NOVA-002 OP-2)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    verdict: Layer1Verdict
    rule_id: str = Field(min_length=1)
    risk_hint: RiskLevel
    reason: str = Field(min_length=1)


class CommandAuditInput(BaseModel):
    """Sanitized input sent to Layer 2 CommandAuditorPort (NOVA-002 OP-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    workspace_path: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    risk_hint: AuditRiskLevel = "high"


class CommandAuditResult(BaseModel):
    """Structured explanation produced by Layer 2 Auditor Subagent (NOVA-002 OP-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    risk_level: AuditRiskLevel
    spoken_summary: str = Field(min_length=1)
    technical_explanation: str = Field(min_length=1)
    audited_by: Literal["pi_llm", "deterministic_fallback"]

    @field_validator("spoken_summary")
    @classmethod
    def _validate_spoken_budget(cls, value: str) -> str:
        return validate_spoken_word_budget(value)


class CommandInterceptRequest(BaseModel):
    """Pre-execution command interception request entering SecurityGatekeeper."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    source: CommandSource
    tool_name: ToolInterceptName
    command: str
    cwd: str = Field(min_length=1)


class CommandExecutionDecision(BaseModel):
    """Final authorization decision returned to the CLI hook."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    allowed: bool
    verdict: DecisionVerdict
    audit_id: str | None = None
    reason: str = Field(min_length=1)
