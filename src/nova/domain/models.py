"""Pydantic v2 boundary models for workspace selection, Tier 2 tasks, and Security Gatekeeper."""

from __future__ import annotations

import re
from dataclasses import dataclass
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


WorkspaceQueryMode = Literal["exact_path", "voice_fuzzy", "ui_picker"]
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
CallSessionState = Literal[
    "standby",
    "listening",
    "thinking",
    "executing",
    "speaking",
    "closed",
]
InferenceDevice = Literal["cuda", "cpu"]
MAX_SYNTHESIZED_PCM_BYTES = 11_520_000


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
    selected_via: WorkspaceQueryMode = "exact_path"
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


class CallSession(BaseModel):
    """Explicit state snapshot for a continuous hands-free Nova voice session (NOVA-003 OP-4)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    call_id: str = Field(min_length=1)
    state: CallSessionState = "standby"
    started_at: str = Field(default_factory=utc_now_iso)
    last_activity_monotonic: float = Field(ge=0.0)
    turn_count: int = Field(default=0, ge=0)
    active_correlation_id: str | None = None


class AudioFrame(BaseModel):
    """Bounded mono 16-bit PCM frame captured in RAM from microphone input (NOVA-003 OP-1)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pcm_s16le: bytes
    sample_rate_hz: int = Field(default=16000, gt=0)
    channels: Literal[1] = 1
    monotonic_ts: float = Field(ge=0.0)

    @field_validator("pcm_s16le")
    @classmethod
    def _validate_pcm_frame(cls, value: bytes) -> bytes:
        if len(value) < 2 or len(value) % 2 != 0:
            raise ValueError("VOICE_INVALID_PCM_FRAME: pcm_s16le must have even byte length >= 2")
        return value


class SynthesizedAudio(BaseModel):
    """Bounded mono 16-bit PCM utterance synthesized locally in RAM (NOVA-003 OP-1 & OP-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pcm_s16le: bytes
    sample_rate_hz: int = Field(default=24000, ge=8000, le=48000)
    channels: Literal[1] = 1

    @field_validator("pcm_s16le")
    @classmethod
    def _validate_synthesized_pcm(cls, value: bytes) -> bytes:
        if len(value) < 2 or len(value) > MAX_SYNTHESIZED_PCM_BYTES or len(value) % 2 != 0:
            raise ValueError(
                "VOICE_INVALID_PCM_FRAME: pcm_s16le must have even byte length "
                f"between 2 and {MAX_SYNTHESIZED_PCM_BYTES}"
            )
        return value


class WakeWordDetection(BaseModel):
    """Detection score produced by WakeWordPort for a single audio frame (NOVA-003 OP-2)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    detected: bool
    wake_word: str | None = None
    score: float = Field(ge=0.0, le=1.0)


class TranscriptionResult(BaseModel):
    """Local Spanish speech-to-text output produced by SpeechToTextPort (NOVA-003 OP-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str
    language: str = Field(default="es", min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    backend_device: InferenceDevice
    duration_ms: float = Field(ge=0.0)


SensitivityPreset = Literal["low", "normal", "high"]
PttMode = Literal["single_turn", "toggle_call"]
TtsEngineChoice = Literal["kokoro", "piper"]
ControlAction = Literal["ptt-toggle", "toggle-panel", "barge-in", "ping"]

SENSITIVITY_THRESHOLDS: dict[SensitivityPreset, tuple[float, float, float]] = {
    "low": (0.75, 0.65, 0.75),
    "normal": (0.60, 0.50, 0.60),
    "high": (0.45, 0.35, 0.45),
}

_FORBIDDEN_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:^sk-[A-Za-z0-9_\-]{6,}|^nvapi-[A-Za-z0-9_\-]{6,}|^gh[po]-[A-Za-z0-9_\-]{6,}|"
    r"^AIza[A-Za-z0-9_\-]{6,}|^Bearer\s+)"
)


def _validate_safe_setting_token(
    value: str | None,
    *,
    allow_empty: bool = False,
) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return "" if allow_empty else None
    if cleaned.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("Setting value must not start with '-' or contain control chars")
    if _FORBIDDEN_SECRET_VALUE_RE.search(cleaned):
        raise ValueError("Credentials, API keys, and tokens are forbidden in config.toml")
    return cleaned


class GeneralSettingsSection(BaseModel):
    """Non-sensitive general desktop preferences persisted in config.toml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    remember_workspace: bool = True
    last_active_workspace: str | None = None

    @field_validator("last_active_workspace")
    @classmethod
    def _validate_workspace(cls, value: str | None) -> str | None:
        return _validate_safe_setting_token(value)


class AudioSettingsSection(BaseModel):
    """Non-sensitive PipeWire/PulseAudio device preferences persisted in config.toml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_target: str | None = None
    playback_target: str | None = None

    @field_validator("capture_target", "playback_target")
    @classmethod
    def _validate_targets(cls, value: str | None) -> str | None:
        return _validate_safe_setting_token(value)


class VoiceSettingsSection(BaseModel):
    """Voice pipeline preferences persisted in config.toml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enable_voice: bool = True
    whisper_language: str = Field(default="es", min_length=2, max_length=10)
    whisper_model: str = Field(default="small", min_length=1, max_length=120)
    whisper_device: InferenceDevice = "cuda"
    tts_engine: TtsEngineChoice = "piper"
    kokoro_voice: str = Field(default="ef_dora", min_length=1, max_length=64)
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    sensitivity_preset: SensitivityPreset = "normal"
    wakeword_threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    speech_threshold: float = Field(default=0.50, gt=0.0, le=1.0)
    barge_in_threshold: float = Field(default=0.60, gt=0.0, le=1.0)

    @field_validator("whisper_language", "whisper_model", "kokoro_voice")
    @classmethod
    def _validate_voice_tokens(cls, value: str) -> str:
        checked = _validate_safe_setting_token(value)
        if checked is None:
            raise ValueError("Voice setting value cannot be empty")
        return checked


class ModelsSettingsSection(BaseModel):
    """Tier 1 and Tier 2 model identifiers persisted in config.toml (no API keys)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pi_provider: str = Field(default="nvidia", min_length=1, max_length=64)
    pi_model: str = Field(default="deepseek-ai/deepseek-v4.1-flash", min_length=1, max_length=160)
    tier2_model: str = Field(default="claude-opus-5-5-high", min_length=1, max_length=160)

    @field_validator("pi_provider", "pi_model", "tier2_model")
    @classmethod
    def _validate_model_tokens(cls, value: str) -> str:
        checked = _validate_safe_setting_token(value)
        if checked is None:
            raise ValueError("Model setting value cannot be empty")
        return checked


class ShortcutsSettingsSection(BaseModel):
    """Push-to-Talk and global shortcut preferences persisted in config.toml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ptt_mode: PttMode = "single_turn"
    global_shortcut: str = Field(default="Super+Shift+Space", min_length=1, max_length=64)

    @field_validator("global_shortcut")
    @classmethod
    def _validate_shortcut(cls, value: str) -> str:
        checked = _validate_safe_setting_token(value)
        if checked is None:
            raise ValueError("global_shortcut cannot be empty")
        return checked


class NovaPersistedSettings(BaseModel):
    """Validated non-sensitive configuration persisted in $XDG_CONFIG_HOME/nova/config.toml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    general: GeneralSettingsSection = Field(default_factory=GeneralSettingsSection)
    audio: AudioSettingsSection = Field(default_factory=AudioSettingsSection)
    voice: VoiceSettingsSection = Field(default_factory=VoiceSettingsSection)
    models: ModelsSettingsSection = Field(default_factory=ModelsSettingsSection)
    shortcuts: ShortcutsSettingsSection = Field(default_factory=ShortcutsSettingsSection)


class AudioDeviceInfo(BaseModel):
    """Local PipeWire/PulseAudio input or output device metadata (NOVA-008 OP-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    is_default: bool = False


class AudioDeviceCatalog(BaseModel):
    """Enumerated local input/output audio devices (NOVA-008 OP-3)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    available: bool = True
    backend: Literal["pipewire", "pulseaudio", "none"] = "none"
    inputs: tuple[AudioDeviceInfo, ...] = ()
    outputs: tuple[AudioDeviceInfo, ...] = ()


DEFAULT_PTT_COMMAND = "nova control ptt-toggle"


@dataclass(frozen=True, slots=True)
class ShortcutRegistrationResult:
    """Outcome of attempting to register Nova's global PTT shortcut on Linux."""

    status: Literal["registered", "manual_required"]
    desktop_environment: str
    shortcut: str
    command: str
    config_file: str | None = None
