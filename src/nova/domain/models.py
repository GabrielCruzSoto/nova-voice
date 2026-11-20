"""Pydantic v2 boundary models for workspace selection, Tier 2 tasks, and Security Gatekeeper."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SPOKEN_WORDS = 25
DEFAULT_PI_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"


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
WorkspaceTopology = Literal["multi-project", "single-repo"]
AgentEngineId = Literal["pi", "agy", "claude", "opencode"]
AgentEngineChoice = Literal["auto", "pi", "agy", "claude", "opencode"]
AgentTier = Literal["tier1", "tier2"]
AgentAuthMode = Literal["native_cli", "api_token", "unconfigured"]
CommandSource = Literal[
    "tier1_pi",
    "tier2_agy",
    "tier1_claude",
    "tier2_claude",
    "tier1_opencode",
    "tier2_opencode",
    "tier1_agy",
    "tier2_pi",
]
ToolInterceptName = Literal["bash", "run_command", "Bash", "shell"]
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


class WorkspaceProject(BaseModel):
    """Validated isolated project declared inside a Workspace (NOVA-013)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)


class WorkspaceCatalogEntry(BaseModel):
    """Summary entry for a selectable Workspace inside allowed_roots (NOVA-013)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
    topology: WorkspaceTopology = "single-repo"
    projects: tuple[WorkspaceProject, ...] = ()


class InitializeWorkspaceRequest(BaseModel):
    """Boundary request to initialize a new isolated multi-project Workspace (NOVA-013 OP-2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    parent_root: str = Field(min_length=1)
    workspace_slug: str = Field(min_length=1, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)
    projects: tuple[str, ...] = ()
    activate: bool = True


class WorkspaceContext(BaseModel):
    """Validated active workspace context."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    canonical_path: str = Field(min_length=1)
    selected_via: WorkspaceQueryMode = "exact_path"
    active_since: str = Field(default_factory=utc_now_iso)
    topology: WorkspaceTopology = "single-repo"
    manifest_path: str | None = None
    projects: tuple[WorkspaceProject, ...] = ()


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

    first_run_completed: bool = False
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
    whisper_language: str | None = Field(default="es", min_length=2, max_length=10)
    whisper_model: str = Field(default="small", min_length=1, max_length=120)
    whisper_device: InferenceDevice = "cuda"
    tts_engine: TtsEngineChoice = "piper"
    kokoro_voice: str = Field(default="ef_dora", min_length=1, max_length=64)
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    sensitivity_preset: SensitivityPreset = "normal"
    wakeword_threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    speech_threshold: float = Field(default=0.50, gt=0.0, le=1.0)
    barge_in_threshold: float = Field(default=0.60, gt=0.0, le=1.0)

    @field_validator("whisper_language")
    @classmethod
    def _validate_whisper_language(cls, value: str | None) -> str | None:
        return _validate_safe_setting_token(value)

    @field_validator("whisper_model", "kokoro_voice")
    @classmethod
    def _validate_voice_tokens(cls, value: str) -> str:
        checked = _validate_safe_setting_token(value)
        if checked is None:
            raise ValueError("Voice setting value cannot be empty")
        return checked


class ModelsSettingsSection(BaseModel):
    """Tier 1 and Tier 2 agent/model identifiers persisted in config.toml (no API keys)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier1_engine: AgentEngineChoice = "auto"
    tier1_provider: str = Field(default="nvidia", max_length=64)
    tier1_model: str = Field(default=DEFAULT_PI_MODEL, min_length=1, max_length=160)
    tier2_engine: AgentEngineChoice = "auto"
    tier2_provider: str = Field(default="", max_length=64)
    tier2_model: str = Field(default="claude-opus-5-5-high", min_length=1, max_length=160)
    single_engine_mode: bool = False
    pi_provider: str = Field(default="nvidia", min_length=1, max_length=64)
    pi_model: str = Field(default=DEFAULT_PI_MODEL, min_length=1, max_length=160)

    @model_validator(mode="before")
    @classmethod
    def _migrate_v10_model_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        merged = dict(data)
        # Migrate legacy v1.0 keys -> v1.1 tier1_* when tier1_* were omitted
        if "tier1_provider" not in merged and "pi_provider" in merged:
            merged["tier1_provider"] = merged["pi_provider"]
        if "tier1_model" not in merged and "pi_model" in merged:
            merged["tier1_model"] = merged["pi_model"]
        # Keep legacy pi_* synchronized when updated via tier1_*
        if "pi_provider" not in merged and merged.get("tier1_provider"):
            merged["pi_provider"] = merged["tier1_provider"]
        if "pi_model" not in merged and merged.get("tier1_model"):
            merged["pi_model"] = merged["tier1_model"]
        # Normalize None to empty string for optional provider fields
        if merged.get("tier1_provider") is None:
            merged["tier1_provider"] = ""
        if merged.get("tier2_provider") is None:
            merged["tier2_provider"] = ""
        return merged

    @field_validator("pi_provider", "pi_model", "tier1_model", "tier2_model")
    @classmethod
    def _validate_model_tokens(cls, value: str) -> str:
        checked = _validate_safe_setting_token(value)
        if checked is None:
            raise ValueError("Model setting value cannot be empty")
        return checked

    @field_validator("tier1_provider", "tier2_provider")
    @classmethod
    def _validate_optional_provider_tokens(cls, value: str) -> str:
        if not value or not value.strip():
            return ""
        checked = _validate_safe_setting_token(value)
        if checked is None:
            return ""
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

    schema_version: Literal["1.0", "1.1"] = "1.1"
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


MicDiagnosticStatus = Literal["ok", "silent", "binary_missing", "device_error"]
AudioOutDiagnosticStatus = Literal["ok", "binary_missing", "tts_unavailable", "device_error"]
PiDiagnosticStatus = Literal["connected", "binary_missing", "auth_error", "timeout", "error"]
AntigravityDiagnosticStatus = Literal[
    "connected",
    "binary_missing",
    "plugin_missing",
    "auth_error",
    "timeout",
    "error",
]


class MicDiagnosticResult(BaseModel):
    """Live microphone readiness test result in RAM (NOVA-013 OP-4)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ok: bool
    status: MicDiagnosticStatus
    capture_target: str | None = None
    duration_ms: float = Field(ge=0.0)
    peak_level: float = Field(ge=0.0, le=1.0)
    rms_level: float = Field(ge=0.0, le=1.0)
    speech_detected: bool = False
    transcript_preview: str = ""
    message: str = Field(min_length=1)


class AudioOutputDiagnosticResult(BaseModel):
    """Live audio output readiness test result in RAM (NOVA-013 OP-5)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ok: bool
    status: AudioOutDiagnosticStatus
    engine: str = Field(min_length=1)
    latency_ms: float = Field(ge=0.0)
    message: str = Field(min_length=1)


class PiConnectionDiagnosticResult(BaseModel):
    """Live Nova <-> Pi RPC connectivity probe result (NOVA-013 OP-6)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ok: bool
    status: PiDiagnosticStatus
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    latency_ms: float = Field(ge=0.0)
    message: str = Field(min_length=1)


class AntigravityDiagnosticResult(BaseModel):
    """Live Nova <-> Antigravity CLI connectivity probe result (NOVA-013 OP-7)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ok: bool
    status: AntigravityDiagnosticStatus
    model: str = Field(min_length=1)
    plugin_verified: bool = False
    latency_ms: float = Field(ge=0.0)
    message: str = Field(min_length=1)


class PersistedChatMessage(BaseModel):
    """Value object representing a persisted chat message record in SQLite (NOVA-015)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    message_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    step_type: str = Field(min_length=1)
    content: str = ""
    created_at: float = Field(ge=0.0)
    workspace_path: str | None = None


class WorkspaceSessionPointer(BaseModel):
    """Active Tier 1 & Tier 2 continuity pointers associated with a workspace (NOVA-015)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    workspace_canonical_path: str = Field(min_length=1)
    pi_session_id: str | None = None
    agy_conversation_id: str | None = None
    updated_at: float = Field(ge=0.0)


AgentDiagnosticStatus = Literal[
    "connected",
    "binary_missing",
    "plugin_missing",
    "auth_error",
    "timeout",
    "error",
]


class AgentSessionPointer(BaseModel):
    """Per-engine, per-tier session continuity pointer in SQLite (NOVA-017)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    workspace_canonical_path: str = Field(min_length=1)
    tier: AgentTier
    agent_provider: AgentEngineId
    session_id: str = Field(min_length=1)
    updated_at: float = Field(ge=0.0)


class AgentDiagnosticResult(BaseModel):
    """Generic live connectivity probe result for any supported CLI agent (NOVA-017)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ok: bool
    engine: AgentEngineId
    status: AgentDiagnosticStatus
    provider: str = ""
    model: str = ""
    plugin_verified: bool = False
    latency_ms: float = Field(ge=0.0)
    message: str = Field(min_length=1)


class AgentCatalogEntry(BaseModel):
    """Auto-detected CLI agent metadata exposed to UI without raw secrets (NOVA-017)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    engine: AgentEngineId
    display_name: str = Field(min_length=1)
    installed: bool
    binary_path: str | None = None
    auth_mode: AgentAuthMode = "unconfigured"
    configured_env_vars: tuple[str, ...] = ()
    supports_tier1: bool = True
    supports_tier2: bool = True
    default_tier1_model: str = ""
    default_tier2_model: str = ""


class ProviderCredentialStatus(BaseModel):
    """Write-only status of an optional API token provider (never contains secret values)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    provider: str = Field(min_length=1)
    env_var: str = Field(min_length=1)
    configured: bool = False


class ActiveTierSelection(BaseModel):
    """Currently active engine, provider, and model for a tier."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    engine: AgentEngineId
    provider: str = ""
    model: str = ""


class AgentCatalogSnapshot(BaseModel):
    """Full catalog of detected CLI engines and active tier routing (NOVA-017)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    schema_version: Literal["1.0"] = "1.0"
    credential_backend: Literal["libsecret", "local_file_0600", "none"] = "none"
    active_tier1: ActiveTierSelection | None = None
    active_tier2: ActiveTierSelection | None = None
    single_engine_mode: bool = False
    engines: tuple[AgentCatalogEntry, ...] = ()
    providers: tuple[ProviderCredentialStatus, ...] = ()

