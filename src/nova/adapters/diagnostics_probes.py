"""Concrete adapter wiring for `LiveDiagnosticsService` (PipeWire, Pi RPC, Antigravity CLI)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from nova.adapters.antigravity_adapter import (
    ALLOWED_TIER2_MODELS,
    AntigravityCLIAdapter,
    AntigravityRunnerConfig,
)
from nova.adapters.audio_pipewire import PipeWireAudioAdapter, PipeWireAudioSettings
from nova.adapters.claude_adapter import ClaudeCodeAdapter, ClaudeCodeConfig
from nova.adapters.opencode_adapter import OpenCodeAdapter, OpenCodeConfig
from nova.adapters.pi_rpc_client import PiRpcClient, PiRpcConfig
from nova.application.live_diagnostics import (
    AudioInputFactory,
    AudioOutputFactory,
    LiveDiagnosticsConfig,
    LiveDiagnosticsService,
    WhichResolver,
)
from nova.domain.models import CodingTaskRequest
from nova.domain.ports import (
    AudioInputPort,
    AudioOutputPort,
    SpeechToTextPort,
    TextToSpeechPort,
)


def default_audio_input_factory(capture_target: str | None) -> AudioInputPort:
    """Instantiate a RAM-only PipeWire/PulseAudio capture adapter for live diagnostics."""
    return PipeWireAudioAdapter(PipeWireAudioSettings(capture_target=capture_target))


def default_audio_output_factory(playback_target: str | None) -> AudioOutputPort:
    """Instantiate a RAM-only PipeWire/PulseAudio playback adapter for live diagnostics."""
    return PipeWireAudioAdapter(PipeWireAudioSettings(playback_target=playback_target))


async def default_pi_probe_runner(
    executable: str,
    extra_args: tuple[str, ...],
    provider: str,
    model: str,
    probe_dir: str,
) -> None:
    """Execute an isolated zero-tool Pi RPC ping inside `probe_dir`."""
    client = PiRpcClient(
        PiRpcConfig(
            executable=executable,
            extra_leading_args=extra_args,
            provider=provider,
            model=model,
            workspace_path=probe_dir,
            workspace_active=False,
            no_session=True,
            enable_gatekeeper_extension=False,
            ack_timeout_seconds=4.0,
            run_timeout_seconds=7.5,
        )
    )
    async with client:
        _ = [delta async for delta in client.stream_prompt("Responde unicamente OK")]


async def default_agy_probe_runner(
    executable: str,
    extra_args: tuple[str, ...],
    model: str,
    probe_dir: str,
    gatekeeper_ipc_env: dict[str, str] | None,
) -> None:
    """Verify Antigravity plugin and execute a bounded stream-json ping inside `probe_dir`."""
    allowed = frozenset(set(ALLOWED_TIER2_MODELS) | {model})
    adapter = AntigravityCLIAdapter(
        AntigravityRunnerConfig(
            executable=executable,
            extra_leading_args=extra_args,
            allowed_models=allowed,
            timeout_seconds=14.0,
            gatekeeper_ipc_env=gatekeeper_ipc_env
            or {
                "NOVA_GATEKEEPER_SOCKET": str(Path(probe_dir) / "nova-probe.sock"),
                "NOVA_GATEKEEPER_TOKEN": "probe-token",
            },
            require_gatekeeper_plugin=True,
        )
    )
    _ = [
        ev
        async for ev in adapter.execute(
            CodingTaskRequest(
                correlation_id="corr-diag-agy",
                workspace_path=probe_dir,
                prompt="Responde unicamente OK",
                model=model,
            )
        )
    ]


async def default_claude_probe_runner(
    executable: str,
    extra_args: tuple[str, ...],
    model: str,
    probe_dir: str,
    gatekeeper_ipc_env: dict[str, str] | None,
) -> None:
    """Execute a bounded Claude Code headless stream-json ping inside `probe_dir`."""
    adapter = ClaudeCodeAdapter(
        ClaudeCodeConfig(
            executable=executable,
            extra_leading_args=extra_args,
            model=model,
            workspace_path=probe_dir,
            timeout_seconds=14.0,
            gatekeeper_ipc_env=gatekeeper_ipc_env,
            enable_gatekeeper_hook=False,
        )
    )
    _ = [delta async for delta in adapter.stream_prompt("Responde unicamente OK")]


async def default_opencode_probe_runner(
    executable: str,
    extra_args: tuple[str, ...],
    provider: str,
    model: str,
    probe_dir: str,
    gatekeeper_ipc_env: dict[str, str] | None,
) -> None:
    """Execute a bounded OpenCode headless JSON ping inside `probe_dir`."""
    adapter = OpenCodeAdapter(
        OpenCodeConfig(
            executable=executable,
            extra_leading_args=extra_args,
            provider=provider,
            model=model,
            workspace_path=probe_dir,
            timeout_seconds=14.0,
            gatekeeper_ipc_env=gatekeeper_ipc_env,
            enable_gatekeeper_plugin=False,
        )
    )
    _ = [delta async for delta in adapter.stream_prompt("Responde unicamente OK")]


def build_live_diagnostics_service(
    *,
    config: LiveDiagnosticsConfig | None = None,
    which_resolver: WhichResolver = shutil.which,
    audio_input_factory: AudioInputFactory = default_audio_input_factory,
    audio_output_factory: AudioOutputFactory = default_audio_output_factory,
    stt_port: SpeechToTextPort | None = None,
    tts_port: TextToSpeechPort | None = None,
    extra_secrets_provider: Callable[[], tuple[str, ...]] | None = None,
) -> LiveDiagnosticsService:
    """Build `LiveDiagnosticsService` wired with concrete local adapters."""
    return LiveDiagnosticsService(
        config=config,
        which_resolver=which_resolver,
        audio_input_factory=audio_input_factory,
        audio_output_factory=audio_output_factory,
        pi_probe_runner=default_pi_probe_runner,
        agy_probe_runner=default_agy_probe_runner,
        claude_probe_runner=default_claude_probe_runner,
        opencode_probe_runner=default_opencode_probe_runner,
        stt_port=stt_port,
        tts_port=tts_port,
        extra_secrets_provider=extra_secrets_provider,
    )
