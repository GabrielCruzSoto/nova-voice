"""Hexagonal ports for Nova orchestration and local voice pipeline (zero adapter coupling)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from nova.domain.events import CommandApprovalResolvedEvent
from nova.domain.models import (
    AudioDeviceCatalog,
    AudioFrame,
    CodingTaskRequest,
    CommandAuditInput,
    CommandAuditResult,
    CommandExecutionDecision,
    CommandInterceptRequest,
    NovaPersistedSettings,
    ResolveWorkspaceRequest,
    RunnerProgressEvent,
    ShortcutRegistrationResult,
    SynthesizedAudio,
    TranscriptionResult,
    WakeWordDetection,
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

    @property
    def extra_secrets(self) -> tuple[str, ...]:
        """Dynamic session secrets registered for universal stream redaction."""
        ...

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


class AudioInputPort(Protocol):
    """Abstract port for streaming 16 kHz mono s16le microphone frames in RAM (NOVA-003 OP-1)."""

    def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield 32 ms (1024-byte) mono PCM frames captured from the local audio server."""
        ...

    async def stop_capture(self) -> None:
        """Idempotently stop active microphone capture."""
        ...


class AudioOutputPort(Protocol):
    """Abstract port for playing and interrupting synthesized PCM audio in RAM (NOVA-003 OP-1)."""

    @property
    def is_playing(self) -> bool:
        """Return True while an audio buffer is actively playing."""
        ...

    async def play_pcm(self, audio: SynthesizedAudio) -> None:
        """Play a synthesized mono s16le PCM buffer until completion or stop_playback()."""
        ...

    async def stop_playback(self) -> None:
        """Idempotently cut active audio playback in <= 100 ms (barge-in)."""
        ...


class WakeWordPort(Protocol):
    """Abstract port for local wake-word detection ('Oye Nova' / 'Hey Nova', NOVA-003 OP-2)."""

    async def detect(self, frame: AudioFrame) -> WakeWordDetection:
        """Evaluate one 16 kHz PCM frame off the event loop and return detection score."""
        ...

    def reset(self) -> None:
        """Idempotently clear internal model activation buffers."""
        ...


class VoiceActivityDetectorPort(Protocol):
    """Abstract port for local frame-level voice activity detection (Silero VAD, NOVA-003 OP-2)."""

    async def speech_probability(self, frame: AudioFrame) -> float:
        """Return voice probability in [0.0, 1.0] for one 16 kHz PCM frame."""
        ...

    def reset(self) -> None:
        """Idempotently clear recurrent VAD hidden state."""
        ...


class SpeechToTextPort(Protocol):
    """Abstract port for local Spanish speech-to-text transcription (NOVA-003 OP-3)."""

    async def transcribe(
        self,
        pcm_s16le: bytes,
        sample_rate_hz: int = 16000,
    ) -> TranscriptionResult:
        """Transcribe an in-memory mono s16le PCM utterance without writing temp files."""
        ...


class TextToSpeechPort(Protocol):
    """Abstract port for local Spanish text-to-speech synthesis (NOVA-003 OP-3)."""

    async def synthesize(self, text: str) -> SynthesizedAudio:
        """Synthesize spoken Spanish text into an in-memory mono s16le PCM buffer."""
        ...


class FolderPickerPort(Protocol):
    """Abstract port for native desktop folder selection dialog (NOVA-004 OP-4)."""

    async def pick_directory(self, initial_dir: Path | None = None) -> Path | None:
        """Open a native folder picker; return selected directory Path, or None if cancelled."""
        ...


class ConfigStorePort(Protocol):
    """Abstract port for atomic 0600 local configuration persistence (NOVA-008)."""

    @property
    def config_path(self) -> Path:
        """Resolved path to config.toml."""
        ...

    def load(self) -> NovaPersistedSettings:
        """Load persisted settings, returning defaults when config.toml does not exist."""
        ...

    def save(self, settings: NovaPersistedSettings) -> Path:
        """Atomically persist validated settings with 0600 permissions."""
        ...


class AudioDeviceDiscoveryPort(Protocol):
    """Abstract port for local PipeWire/PulseAudio device enumeration (NOVA-008 OP-3)."""

    async def list_devices(self) -> AudioDeviceCatalog:
        """Return enumerated audio inputs and outputs, degrading cleanly when unavailable."""
        ...


class ShortcutRegistrarPort(Protocol):
    """Abstract port for global Push-to-Talk shortcut registration (NOVA-008 OP-8)."""

    @property
    def desktop_environment(self) -> str:
        """Return detected Linux desktop environment identifier."""
        ...

    @property
    def auto_register_supported(self) -> bool:
        """Return True when one-click shortcut registration is supported."""
        ...

    def register_ptt_shortcut(self, shortcut: str) -> ShortcutRegistrationResult:
        """Register or return manual instructions for the global PTT shortcut."""
        ...
