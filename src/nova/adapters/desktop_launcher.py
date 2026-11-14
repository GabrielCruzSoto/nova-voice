"""Unified local daemon and hybrid UI launcher (`nova-desktop` / `python -m nova`, NOVA-004 S4)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.adapters.antigravity_adapter import AntigravityCLIAdapter, AntigravityRunnerConfig
from nova.adapters.audio_device_discovery import PipeWireDeviceDiscoveryAdapter
from nova.adapters.audio_pipewire import PipeWireAudioAdapter, PipeWireAudioSettings
from nova.adapters.config_store import TomlConfigStore, resolve_config_file_path
from nova.adapters.control_ipc import UnixControlIpcServer
from nova.adapters.desktop_ui_server import DesktopUiServer, DesktopUiServerSettings
from nova.adapters.folder_picker_gtk import GtkFolderPickerAdapter
from nova.adapters.gatekeeper_ipc import GatekeeperIPCConfig, UnixSocketGatekeeperServer
from nova.adapters.pi_auditor_adapter import PiAuditorAdapter, PiAuditorConfig
from nova.adapters.pi_rpc_client import PiRpcClient, PiRpcConfig
from nova.adapters.shortcut_registrar import LinuxShortcutRegistrar
from nova.adapters.stt_faster_whisper import FasterWhisperSettings, FasterWhisperSttAdapter
from nova.adapters.tts_local_onnx import LocalOnnxTtsAdapter, LocalOnnxTtsSettings
from nova.adapters.vad_silero import SileroVadAdapter, SileroVadSettings
from nova.adapters.wakeword_openwakeword import OpenWakeWordAdapter, OpenWakeWordSettings
from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.orchestrator import NovaOrchestrator, OrchestratorSettings
from nova.application.security_gatekeeper import SecurityGatekeeper
from nova.application.settings_service import SettingsService
from nova.application.ui_state_hub import UiStateHub
from nova.application.voice_coordinator import VoiceCallCoordinator, VoiceCoordinatorSettings
from nova.domain.errors import (
    DesktopUiError,
    NovaDomainError,
    VoicePipelineError,
    redact_sensitive_text,
)
from nova.domain.events import OrchestratorEventBus, UiPanelToggleRequestedEvent
from nova.domain.models import (
    CallSession,
    InferenceDevice,
    NovaPersistedSettings,
    PttMode,
    ResolveWorkspaceRequest,
    SensitivityPreset,
    TtsEngineChoice,
    WorkspaceContext,
)
from nova.domain.ports import FolderPickerPort

logger = logging.getLogger("nova.desktop_launcher")

BrowserPreference = Literal["auto", "xdg-open", "firefox", "webview", "window"]

_LOOPBACK_BROWSER_URL_RE = re.compile(r"^http://127\.0\.0\.1:[1-9][0-9]{0,4}/?$")


class BrowserProcessProtocol(Protocol):
    """Minimal protocol for detached browser launcher subprocess handles."""

    async def wait(self) -> int: ...


BrowserSpawner = Callable[..., Awaitable[BrowserProcessProtocol]]
WhichResolver = Callable[[str], str | None]
NativeShellProbe = Callable[[], bool]


def _default_native_shell_probe() -> bool:
    """Return True when the Python environment provides a native desktop webview/Qt shell."""
    return (
        importlib.util.find_spec("PyQt6.QtWebEngineWidgets") is not None
        or importlib.util.find_spec("webview") is not None
    )


async def _terminate_browser_process(
    proc: BrowserProcessProtocol,
    *,
    grace_seconds: float = 2.5,
) -> None:
    """Signal and reap a spawned UI subprocess (including its session group when detached)."""
    if getattr(proc, "returncode", None) is not None:
        return

    pid = getattr(proc, "pid", None)
    signaled_group = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGTERM)
            signaled_group = True
        except (ProcessLookupError, PermissionError, OSError):
            signaled_group = False

    if not signaled_group:
        terminate_fn = getattr(proc, "terminate", None)
        if callable(terminate_fn):
            with contextlib.suppress(ProcessLookupError, OSError):
                terminate_fn()

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass

    if isinstance(pid, int) and pid > 0:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
    kill_fn = getattr(proc, "kill", None)
    if callable(kill_fn):
        with contextlib.suppress(ProcessLookupError, OSError):
            kill_fn()

    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=0.5)


class VoiceRuntimePort(Protocol):
    """Lifecycle and call-control interface required from the optional voice stack."""

    @property
    def session(self) -> CallSession: ...

    async def start(self) -> None: ...

    async def start_call(self, reason: str = "ui_call_start") -> None: ...

    async def end_call(self, reason: str = "ui_call_end") -> None: ...

    async def barge_in(self) -> None: ...

    async def shutdown(self) -> None: ...


VoiceRuntimeFactory = Callable[..., VoiceRuntimePort | Awaitable[VoiceRuntimePort]]


async def _default_browser_spawner(*argv: str) -> BrowserProcessProtocol:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


def _default_allowed_roots() -> tuple[str, ...]:
    candidates: list[str] = []
    proyectos = Path.home() / "Proyectos"
    if proyectos.is_dir():
        candidates.append(str(proyectos.resolve()))
    cwd = str(Path.cwd().resolve())
    if cwd not in candidates:
        candidates.append(cwd)
    return tuple(candidates)


def _validate_safe_cli_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("CLI option value must not start with '-' or contain control chars")
    return cleaned


class NovaDesktopConfig(BaseModel):
    """Validated runtime configuration for the unified `nova-desktop` local daemon."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    port: int = Field(default=0, ge=0, le=65535)
    allowed_roots: tuple[str, ...] = Field(default_factory=_default_allowed_roots, min_length=1)
    initial_workspace: str | None = None
    open_browser: bool = True
    browser_preference: BrowserPreference = "auto"
    enable_voice: bool = True
    ipc_runtime_dir: str | None = None

    pi_executable: str = Field(default="pi", min_length=1)
    pi_provider: str = Field(default="nvidia", min_length=1)
    pi_model: str = Field(default="deepseek-ai/deepseek-v4.1-flash", min_length=1)
    agy_executable: str = Field(default="agy", min_length=1)
    tier2_model: str = Field(default="claude-opus-5-5-high", min_length=1)

    wakeword_models: tuple[str, ...] = ()
    silero_vad_model_path: str | None = None
    whisper_model: str = Field(default="small", min_length=1)
    whisper_language: str | None = Field(default="es", min_length=2, max_length=10)
    whisper_device: InferenceDevice = "cuda"
    tts_engine: TtsEngineChoice = "kokoro"
    kokoro_model_path: str | None = None
    kokoro_voices_path: str | None = None
    kokoro_voice: str = Field(default="ef_dora", min_length=1)
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    piper_binary: str = Field(default="piper", min_length=1)
    piper_model_path: str | None = None
    capture_target: str | None = None
    playback_target: str | None = None
    sensitivity_preset: SensitivityPreset = "normal"
    wakeword_threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    speech_threshold: float = Field(default=0.50, gt=0.0, le=1.0)
    barge_in_threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    ptt_mode: PttMode = "single_turn"
    global_shortcut: str = Field(default="Super+Shift+Space", min_length=1)
    remember_workspace: bool = True
    config_path: str | None = None

    @field_validator(
        "initial_workspace",
        "ipc_runtime_dir",
        "pi_executable",
        "pi_provider",
        "pi_model",
        "agy_executable",
        "tier2_model",
        "silero_vad_model_path",
        "whisper_model",
        "whisper_language",
        "kokoro_model_path",
        "kokoro_voices_path",
        "kokoro_voice",
        "piper_binary",
        "piper_model_path",
        "capture_target",
        "playback_target",
        "global_shortcut",
        "config_path",
    )
    @classmethod
    def _check_tokens(cls, value: str | None) -> str | None:
        return _validate_safe_cli_value(value)


def _probe_voice_prerequisites(
    config: NovaDesktopConfig,
    *,
    which_resolver: WhichResolver = shutil.which,
) -> None:
    """Verify local audio binaries, optional `[voice]` modules, and required local model paths."""
    has_capture = bool(which_resolver("pw-record") or which_resolver("parecord"))
    has_playback = bool(which_resolver("pw-play") or which_resolver("paplay"))
    if not (has_capture and has_playback):
        raise VoicePipelineError(
            code="VOICE_AUDIO_BINARY_MISSING",
            message="Neither PipeWire (pw-record/pw-play) nor PulseAudio utilities were found.",
        )

    required_modules = ("onnxruntime", "openwakeword", "faster_whisper")
    for mod_name in required_modules:
        if importlib.util.find_spec(mod_name) is None:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Optional voice dependency '{mod_name}' is not installed.",
            )

    if not config.silero_vad_model_path or not Path(config.silero_vad_model_path).is_file():
        raise VoicePipelineError(
            code="VOICE_MODEL_UNAVAILABLE",
            message="Local Silero VAD ONNX model file is not configured or missing.",
        )

    for ww_model in config.wakeword_models:
        if not Path(ww_model).is_file():
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message=f"Wake-word ONNX model file not found: {ww_model}",
            )

    if config.tts_engine == "kokoro":
        if importlib.util.find_spec("kokoro_onnx") is None:
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message="Optional voice dependency 'kokoro_onnx' is not installed.",
            )
        if (
            not config.kokoro_model_path
            or not Path(config.kokoro_model_path).is_file()
            or not config.kokoro_voices_path
            or not Path(config.kokoro_voices_path).is_file()
        ):
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message="Local Kokoro ONNX model/voices files are not configured or missing.",
            )
    else:
        if which_resolver(config.piper_binary) is None:
            raise VoicePipelineError(
                code="VOICE_TTS_UNAVAILABLE",
                message=f"Piper TTS binary '{config.piper_binary}' was not found on PATH.",
            )
        if not config.piper_model_path or not Path(config.piper_model_path).is_file():
            raise VoicePipelineError(
                code="VOICE_MODEL_UNAVAILABLE",
                message="Local Piper ONNX model file is not configured or missing.",
            )


def build_default_voice_coordinator(
    *,
    config: NovaDesktopConfig,
    orchestrator: NovaOrchestrator,
    event_bus: OrchestratorEventBus,
    security_gatekeeper: SecurityGatekeeper,
    extra_secrets_provider: Callable[[], tuple[str, ...]],
) -> VoiceCallCoordinator:
    """Instantiate the local GPU/CPU voice stack after verifying prerequisites."""
    _probe_voice_prerequisites(config)
    audio = PipeWireAudioAdapter(
        PipeWireAudioSettings(
            capture_target=config.capture_target,
            playback_target=config.playback_target,
        )
    )
    wake_word = OpenWakeWordAdapter(
        OpenWakeWordSettings(
            model_paths=config.wakeword_models,
            threshold=config.wakeword_threshold,
        ),
    )
    vad = SileroVadAdapter(
        SileroVadSettings(model_path=config.silero_vad_model_path),
    )
    stt = FasterWhisperSttAdapter(
        FasterWhisperSettings(
            model_size_or_path=config.whisper_model,
            language=config.whisper_language,
            preferred_device=config.whisper_device,
            local_files_only=True,
        ),
    )
    tts = LocalOnnxTtsAdapter(
        LocalOnnxTtsSettings(
            engine=config.tts_engine,
            kokoro_model_path=config.kokoro_model_path,
            kokoro_voices_path=config.kokoro_voices_path,
            kokoro_voice=config.kokoro_voice,
            speed=config.tts_speed,
            piper_binary=config.piper_binary,
            piper_model_path=config.piper_model_path,
        ),
    )
    return VoiceCallCoordinator(
        audio_input=audio,
        audio_output=audio,
        wake_word=wake_word,
        vad=vad,
        stt=stt,
        tts=tts,
        orchestrator=orchestrator,
        event_bus=event_bus,
        security_gatekeeper=security_gatekeeper,
        extra_secrets_provider=extra_secrets_provider,
        settings=VoiceCoordinatorSettings(
            speech_threshold=config.speech_threshold,
            barge_in_threshold=config.barge_in_threshold,
            ptt_mode=config.ptt_mode,
        ),
    )


async def open_local_browser_window(
    url: str,
    *,
    preference: BrowserPreference = "auto",
    env: Mapping[str, str] | None = None,
    which_resolver: WhichResolver = shutil.which,
    spawner: BrowserSpawner = _default_browser_spawner,
    reap_tasks: set[asyncio.Task[int]] | None = None,
    native_shell_available: NativeShellProbe = _default_native_shell_probe,
    spawned_processes: set[BrowserProcessProtocol] | None = None,
) -> bool:
    """Open `url` strictly on loopback using `shell=False` when a graphical display exists."""
    cleaned_url = url.strip()
    if not _LOOPBACK_BROWSER_URL_RE.fullmatch(cleaned_url):
        raise DesktopUiError(
            code="UI_INVALID_BROWSER_URL",
            message="Browser launcher refuses non-loopback or malformed URL.",
        )

    effective_env = env if env is not None else os.environ
    has_display = bool(
        effective_env.get("DISPLAY", "").strip()
        or effective_env.get("WAYLAND_DISPLAY", "").strip()
    )
    if not has_display:
        return False

    argv: tuple[str, ...] | None = None
    if preference in ("webview", "window"):
        argv = (sys.executable, "-m", "nova.adapters.desktop_window", cleaned_url)
    elif preference == "firefox":
        ff_bin = which_resolver("firefox")
        if ff_bin:
            argv = (ff_bin, "--new-window", cleaned_url)
    elif preference == "xdg-open":
        xdg_bin = which_resolver("xdg-open")
        if xdg_bin:
            argv = (xdg_bin, cleaned_url)
    else:
        if native_shell_available():
            argv = (sys.executable, "-m", "nova.adapters.desktop_window", cleaned_url)
        else:
            xdg_bin = which_resolver("xdg-open")
            if xdg_bin:
                argv = (xdg_bin, cleaned_url)
            else:
                ff_bin = which_resolver("firefox")
                if ff_bin:
                    argv = (ff_bin, "--new-window", cleaned_url)

    if argv is None:
        return False

    try:
        proc = await spawner(*argv)
    except OSError:
        return False

    if spawned_processes is not None:
        spawned_processes.add(proc)

    wait_task = asyncio.create_task(proc.wait())
    if spawned_processes is not None:
        wait_task.add_done_callback(lambda _t: spawned_processes.discard(proc))
    if reap_tasks is not None:
        reap_tasks.add(wait_task)
        wait_task.add_done_callback(reap_tasks.discard)
    return True


def _resolve_initial_pi_cwd(
    allowed_roots: tuple[str, ...],
    active_workspace: WorkspaceContext | None,
) -> str:
    if active_workspace is not None:
        return active_workspace.canonical_path
    return str(Path(allowed_roots[0]).resolve())


class NovaDesktopRuntime:
    """Assembles orchestrator, Gatekeeper IPC, optional voice pipeline, and loopback UI bridge."""

    def __init__(
        self,
        config: NovaDesktopConfig | None = None,
        *,
        folder_picker: FolderPickerPort | None = None,
        voice_factory: VoiceRuntimeFactory | None = None,
        browser_spawner: BrowserSpawner = _default_browser_spawner,
        which_resolver: WhichResolver = shutil.which,
        native_shell_available: NativeShellProbe = _default_native_shell_probe,
    ) -> None:
        self._config = config or NovaDesktopConfig()
        self._folder_picker = folder_picker or GtkFolderPickerAdapter()
        self._voice_factory: VoiceRuntimeFactory = voice_factory or build_default_voice_coordinator
        self._browser_spawner = browser_spawner
        self._which_resolver = which_resolver
        self._native_shell_available = native_shell_available

        self._event_bus = OrchestratorEventBus()
        cfg_path = (
            Path(self._config.config_path)
            if self._config.config_path
            else resolve_config_file_path()
        )
        self._config_store = TomlConfigStore(config_path=cfg_path)
        self._ipc_server: UnixSocketGatekeeperServer | None = None
        self._control_ipc: UnixControlIpcServer | None = None
        self._pi_client: PiRpcClient | None = None
        self._orchestrator: NovaOrchestrator | None = None
        self._voice_coordinator: VoiceRuntimePort | None = None
        self._settings_service: SettingsService | None = None
        self._state_hub: UiStateHub | None = None
        self._ui_server: DesktopUiServer | None = None
        self._gatekeeper: SecurityGatekeeper | None = None
        self._reap_tasks: set[asyncio.Task[int]] = set()
        self._spawned_processes: set[BrowserProcessProtocol] = set()
        self._voice_enabled = False
        self._started = False

    @property
    def port(self) -> int:
        return self._ui_server.port if self._ui_server is not None else 0

    @property
    def base_url(self) -> str:
        if self._ui_server is None:
            return "http://127.0.0.1:0"
        return self._ui_server.base_url

    @property
    def voice_enabled(self) -> bool:
        return self._voice_enabled

    @property
    def gatekeeper_socket_path(self) -> str:
        return self._ipc_server.socket_path if self._ipc_server is not None else ""

    @property
    def event_bus(self) -> OrchestratorEventBus:
        return self._event_bus

    def _combined_extra_secrets(self) -> tuple[str, ...]:
        secrets_out: list[str] = []
        if self._gatekeeper is not None:
            secrets_out.extend(self._gatekeeper.extra_secrets)
        if self._ui_server is not None:
            secrets_out.extend(self._ui_server.extra_secrets)
        return tuple(secrets_out)

    def _log(self, event_name: str, **fields: str | int | bool) -> None:
        safe: dict[str, str | int | bool] = {"event": event_name}
        extra = self._combined_extra_secrets()
        for key, value in fields.items():
            safe[key] = redact_sensitive_text(value, extra_secrets=extra) if isinstance(
                value, str
            ) else value
        logger.info(json.dumps(safe, ensure_ascii=False, sort_keys=True))

    def _apply_hot_settings(self, updated: NovaPersistedSettings) -> None:
        """Apply hot-reloadable configuration changes to live runtime components."""
        if self._pi_client is not None:
            self._pi_client.update_model(
                provider=updated.models.pi_provider,
                model=updated.models.pi_model,
            )
        if self._orchestrator is not None:
            self._orchestrator.update_models(tier2_model=updated.models.tier2_model)

        voice_coord = self._voice_coordinator
        if voice_coord is None:
            return
        update_coord = getattr(voice_coord, "update_settings", None)
        if callable(update_coord):
            update_coord(
                ptt_mode=updated.shortcuts.ptt_mode,
                speech_threshold=updated.voice.speech_threshold,
                barge_in_threshold=updated.voice.barge_in_threshold,
            )
        audio_in = getattr(voice_coord, "_audio_input", None)
        if audio_in is not None and callable(getattr(audio_in, "update_settings", None)):
            audio_in.update_settings(
                capture_target=updated.audio.capture_target,
                playback_target=updated.audio.playback_target,
            )
        audio_out = getattr(voice_coord, "_audio_output", None)
        if (
            audio_out is not None
            and audio_out is not audio_in
            and callable(getattr(audio_out, "update_settings", None))
        ):
            audio_out.update_settings(
                capture_target=updated.audio.capture_target,
                playback_target=updated.audio.playback_target,
            )
        wake_word = getattr(voice_coord, "_wake_word", None)
        if wake_word is not None and callable(getattr(wake_word, "update_settings", None)):
            wake_word.update_settings(threshold=updated.voice.wakeword_threshold)
        stt = getattr(voice_coord, "_stt", None)
        if stt is not None and callable(getattr(stt, "update_settings", None)):
            stt.update_settings(language=updated.voice.whisper_language)
        tts = getattr(voice_coord, "_tts", None)
        if tts is not None and callable(getattr(tts, "update_settings", None)):
            tts.update_settings(
                engine=updated.voice.tts_engine,
                speed=updated.voice.tts_speed,
                kokoro_voice=updated.voice.kokoro_voice,
            )

    async def _handle_control_action(self, action: str) -> dict[str, object]:
        """Handle local `nova control <action>` commands received via UnixControlIpcServer."""
        if action == "ptt-toggle":
            if self._voice_coordinator is None:
                return {"ok": True, "action": action, "voice_enabled": False}
            trigger = getattr(self._voice_coordinator, "trigger_ptt_toggle", None)
            if callable(trigger):
                await trigger()
            else:
                await self._voice_coordinator.start_call(reason="global_ptt_toggle")
            return {"ok": True, "action": action}
        if action == "toggle-panel":
            await self._event_bus.publish(
                UiPanelToggleRequestedEvent(correlation_id="corr-ipc-panel")
            )
            return {"ok": True, "action": action}
        if action == "barge-in":
            if self._voice_coordinator is not None:
                await self._voice_coordinator.barge_in()
            elif self._orchestrator is not None:
                await self._orchestrator.barge_in()
            return {"ok": True, "action": action}
        return {"ok": False, "action": action}

    async def __aenter__(self) -> NovaDesktopRuntime:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Start Gatekeeper IPC, orchestrator, optional voice coordinator, and 127.0.0.1 UI."""
        if self._started:
            return

        try:
            workspace_resolver = LocalWorkspaceResolver(
                allowed_roots=[Path(r) for r in self._config.allowed_roots],
                event_bus=self._event_bus,
            )
            initial_ws: WorkspaceContext | None = None
            if self._config.initial_workspace is not None:
                initial_ws = await workspace_resolver.resolve(
                    ResolveWorkspaceRequest(
                        correlation_id="corr-startup-ws",
                        query=self._config.initial_workspace,
                        mode="exact_path",
                    )
                )

            auditor = PiAuditorAdapter(
                PiAuditorConfig(
                    executable=self._config.pi_executable,
                    provider=self._config.pi_provider,
                    model=self._config.pi_model,
                )
            )
            gatekeeper = SecurityGatekeeper(
                workspace_resolver=workspace_resolver,
                auditor=auditor,
                event_bus=self._event_bus,
            )
            self._gatekeeper = gatekeeper

            ipc_server = UnixSocketGatekeeperServer(
                gatekeeper,
                GatekeeperIPCConfig(runtime_base_dir=self._config.ipc_runtime_dir),
            )
            await ipc_server.start()
            self._ipc_server = ipc_server
            ipc_env = ipc_server.child_env()

            initial_pi_cwd = await asyncio.to_thread(
                _resolve_initial_pi_cwd,
                self._config.allowed_roots,
                initial_ws,
            )
            pi_client = PiRpcClient(
                PiRpcConfig(
                    executable=self._config.pi_executable,
                    provider=self._config.pi_provider,
                    model=self._config.pi_model,
                    workspace_path=initial_pi_cwd,
                    gatekeeper_ipc_env=ipc_env,
                )
            )
            self._pi_client = pi_client

            tier2_runner = AntigravityCLIAdapter(
                AntigravityRunnerConfig(
                    executable=self._config.agy_executable,
                    gatekeeper_ipc_env=ipc_env,
                )
            )
            orchestrator = NovaOrchestrator(
                workspace_resolver=workspace_resolver,
                tier1_client=pi_client,
                tier2_runner=tier2_runner,
                event_bus=self._event_bus,
                settings=OrchestratorSettings(default_tier2_model=self._config.tier2_model),
                security_gatekeeper=gatekeeper,
            )
            self._orchestrator = orchestrator

            voice_coord: VoiceRuntimePort | None = None
            if self._config.enable_voice:
                try:
                    maybe_voice = self._voice_factory(
                        config=self._config,
                        orchestrator=orchestrator,
                        event_bus=self._event_bus,
                        security_gatekeeper=gatekeeper,
                        extra_secrets_provider=lambda: (
                            self._ui_server.extra_secrets if self._ui_server is not None else ()
                        ),
                    )
                    resolved_voice: VoiceRuntimePort = (
                        await maybe_voice if inspect.isawaitable(maybe_voice) else maybe_voice
                    )
                    await resolved_voice.start()
                    voice_coord = resolved_voice
                    self._voice_enabled = True
                except (VoicePipelineError, ImportError, OSError, ValueError) as exc:
                    self._voice_enabled = False
                    self._log(
                        "voice_pipeline_degraded_to_text_only",
                        reason=str(exc),
                    )
            else:
                self._voice_enabled = False

            self._voice_coordinator = voice_coord

            state_hub = UiStateHub(
                event_bus=self._event_bus,
                extra_secrets_provider=self._combined_extra_secrets,
                available_workspaces_provider=workspace_resolver.list_available_workspaces,
                call_session_provider=(
                    (lambda: voice_coord.session) if voice_coord is not None else None
                ),
                initial_workspace=initial_ws,
                initial_call_session=(
                    voice_coord.session if voice_coord is not None else None
                ),
                voice_enabled=self._voice_enabled,
            )
            self._state_hub = state_hub

            initial_persisted = await asyncio.to_thread(self._config_store.load)
            settings_service = SettingsService(
                config_store=self._config_store,
                initial_settings=initial_persisted,
                audio_discovery=PipeWireDeviceDiscoveryAdapter(),
                shortcut_registrar=LinuxShortcutRegistrar(),
                event_bus=self._event_bus,
                on_hot_apply=self._apply_hot_settings,
            )
            self._settings_service = settings_service

            ui_server = DesktopUiServer(
                orchestrator=orchestrator,
                state_hub=state_hub,
                event_bus=self._event_bus,
                voice_coordinator=voice_coord,
                folder_picker=self._folder_picker,
                settings_service=settings_service,
                settings=DesktopUiServerSettings(host="127.0.0.1", port=self._config.port),
            )
            await ui_server.start()
            self._ui_server = ui_server

            control_ipc = UnixControlIpcServer(
                handler=self._handle_control_action,
                runtime_base_dir=self._config.ipc_runtime_dir,
            )
            await control_ipc.start()
            self._control_ipc = control_ipc
            self._started = True

            if self._config.open_browser:
                await open_local_browser_window(
                    ui_server.base_url,
                    preference=self._config.browser_preference,
                    which_resolver=self._which_resolver,
                    spawner=self._browser_spawner,
                    reap_tasks=self._reap_tasks,
                    native_shell_available=self._native_shell_available,
                    spawned_processes=self._spawned_processes,
                )

            self._log(
                "desktop_runtime_started",
                url=ui_server.base_url,
                voice_enabled=self._voice_enabled,
            )
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Close spawned UI processes, UI server, SSE hub, voice stack, Pi RPC, and IPC."""
        active_procs = list(self._spawned_processes)
        self._spawned_processes.clear()
        if active_procs:
            await asyncio.gather(
                *(_terminate_browser_process(proc) for proc in active_procs),
                return_exceptions=True,
            )

        for task in list(self._reap_tasks):
            task.cancel()
        if self._reap_tasks:
            await asyncio.gather(*self._reap_tasks, return_exceptions=True)
        self._reap_tasks.clear()

        if self._control_ipc is not None:
            with contextlib.suppress(Exception):
                await self._control_ipc.close()
            self._control_ipc = None

        if self._ui_server is not None:
            with contextlib.suppress(Exception):
                await self._ui_server.close()
            self._ui_server = None

        if self._state_hub is not None:
            with contextlib.suppress(Exception):
                self._state_hub.close()
            self._state_hub = None

        if self._voice_coordinator is not None:
            with contextlib.suppress(Exception):
                await self._voice_coordinator.shutdown()
            self._voice_coordinator = None

        if self._pi_client is not None:
            with contextlib.suppress(Exception):
                await self._pi_client.close()
            self._pi_client = None

        if self._ipc_server is not None:
            with contextlib.suppress(Exception):
                await self._ipc_server.close()
            self._ipc_server = None

        self._started = False


class _TrackStoreAction(argparse.Action):
    """Store CLI argument value and record `dest` as explicitly supplied on CLI."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        setattr(namespace, self.dest, values)
        explicit: set[str] = getattr(namespace, "_explicit_cli_fields", set())
        explicit.add(self.dest)
        namespace._explicit_cli_fields = explicit


class _TrackStoreFalseAction(argparse.Action):
    """Store False when flag is passed and record `dest` as explicitly supplied on CLI."""

    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str,
        default: bool = True,
        help: str | None = None,
    ) -> None:
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            nargs=0,
            const=False,
            default=default,
            help=help,
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del parser, values, option_string
        setattr(namespace, self.dest, False)
        explicit: set[str] = getattr(namespace, "_explicit_cli_fields", set())
        explicit.add(self.dest)
        namespace._explicit_cli_fields = explicit


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI parser for `nova-desktop` / `python -m nova`."""
    parser = argparse.ArgumentParser(
        prog="nova-desktop",
        description="Nova 100% local voice-assisted agentic desktop UI for Linux (127.0.0.1).",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        default=False,
        help="Show application version and exit.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Loopback TCP port on 127.0.0.1 (0 selects an ephemeral free port).",
    )
    parser.add_argument(
        "--allowed-root",
        dest="allowed_roots",
        action="append",
        default=None,
        help="Allowed workspace root directory (may be specified multiple times).",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        action=_TrackStoreAction,
        help="Optional initial project directory inside allowed roots.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically launch a desktop browser window on startup.",
    )
    parser.add_argument(
        "--browser",
        choices=("auto", "xdg-open", "firefox", "webview", "window"),
        default="auto",
        help="Browser or native desktop window launcher preference when opening the local UI.",
    )
    parser.add_argument(
        "--no-voice",
        action="store_true",
        help="Disable the local voice pipeline and start in text/UI mode only.",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        action=_TrackStoreAction,
        help="Optional path to config.toml (defaults to $XDG_CONFIG_HOME/nova/config.toml).",
    )
    parser.add_argument(
        "--ipc-runtime-dir",
        default=None,
        help="Custom 0700 directory for the Gatekeeper AF_UNIX socket.",
    )
    parser.add_argument(
        "--pi-executable",
        default="pi",
        action=_TrackStoreAction,
        help="Executable name or path for Tier 1 Pi CLI.",
    )
    parser.add_argument(
        "--pi-provider",
        default="nvidia",
        action=_TrackStoreAction,
        help="Provider passed to Tier 1 Pi CLI.",
    )
    parser.add_argument(
        "--pi-model",
        default="deepseek-ai/deepseek-v4.1-flash",
        action=_TrackStoreAction,
        help="Model identifier passed to Tier 1 Pi CLI.",
    )
    parser.add_argument(
        "--agy-executable",
        default="agy",
        action=_TrackStoreAction,
        help="Executable name or path for Tier 2 Antigravity CLI.",
    )
    parser.add_argument(
        "--tier2-model",
        default="claude-opus-5-5-high",
        action=_TrackStoreAction,
        help="Default model identifier passed to Tier 2 runner.",
    )
    parser.add_argument(
        "--wakeword-model",
        dest="wakeword_models",
        action="append",
        default=None,
        help="Local ONNX wake-word model path (repeatable).",
    )
    parser.add_argument(
        "--silero-vad-model",
        default=os.environ.get("NOVA_SILERO_VAD_MODEL"),
        help="Local Silero VAD ONNX model file path.",
    )
    parser.add_argument(
        "--whisper-model",
        default=os.environ.get("NOVA_WHISPER_MODEL", "small"),
        action=_TrackStoreAction,
        help="Local faster-whisper model size or local directory path.",
    )
    parser.add_argument(
        "--whisper-device",
        choices=("cuda", "cpu"),
        default="cuda",
        action=_TrackStoreAction,
        help="Preferred faster-whisper device (falls back to CPU automatically).",
    )
    parser.add_argument(
        "--tts-engine",
        choices=("kokoro", "piper"),
        default="kokoro",
        action=_TrackStoreAction,
        help="Local Spanish ONNX TTS engine.",
    )
    parser.add_argument(
        "--kokoro-model",
        default=os.environ.get("NOVA_KOKORO_MODEL"),
        help="Local Kokoro ONNX model file path.",
    )
    parser.add_argument(
        "--kokoro-voices",
        default=os.environ.get("NOVA_KOKORO_VOICES"),
        help="Local Kokoro voices file path.",
    )
    parser.add_argument(
        "--piper-binary",
        default="piper",
        help="Piper executable name or path.",
    )
    parser.add_argument(
        "--piper-model",
        default=os.environ.get("NOVA_PIPER_MODEL"),
        help="Local Piper ONNX voice model file path.",
    )
    parser.add_argument(
        "--models-dir",
        default=os.environ.get("NOVA_MODELS_DIR"),
        help="Directory containing voice models for priority resolution.",
    )
    parser.add_argument(
        "--download-models",
        action="store_true",
        default=False,
        help="Download and verify required voice models then exit.",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Destination directory for --download-models.",
    )
    parser.add_argument(
        "--import",
        dest="import_dir",
        default=None,
        help="Directory containing local voice models to import.",
    )
    parser.add_argument(
        "--import-wakeword",
        default=None,
        help="Local wake-word model path to import.",
    )
    return parser


def resolve_model_path(
    filename: str,
    explicit_param: str | None = None,
    models_dir: str | None = None,
    source_root: Path | None = None,
) -> Path | None:
    """Resolve a voice model path evaluating canonical 6-step precedence (OP-2, AC-12)."""
    # Step 1: Explicit parameter for the model
    if explicit_param:
        explicit_path = Path(explicit_param).expanduser().resolve()
        if explicit_path.is_file():
            return explicit_path

    # Step 2: --models-dir CLI flag
    if models_dir is not None:
        md_path = Path(models_dir).expanduser().resolve()
        if not md_path.is_dir():
            raise ValueError(f"Models directory not found: {models_dir}")
        candidate = md_path / filename
        if candidate.is_file():
            return candidate

    # Step 3: NOVA_MODELS_DIR environment variable
    env_models_dir = os.environ.get("NOVA_MODELS_DIR")
    if env_models_dir:
        env_path = Path(env_models_dir).expanduser().resolve()
        if not env_path.is_dir():
            raise ValueError(f"NOVA_MODELS_DIR directory not found: {env_models_dir}")
        candidate = env_path / filename
        if candidate.is_file():
            return candidate

    # Step 4: User data directory (~/.local/share/nova/models or $XDG_DATA_HOME/nova/models)
    xdg_data = os.environ.get("XDG_DATA_HOME")
    user_models_dir = (
        Path(xdg_data) / "nova" / "models"
        if xdg_data
        else Path.home() / ".local" / "share" / "nova" / "models"
    )
    user_candidate = user_models_dir / filename
    if user_candidate.is_file():
        return user_candidate

    # Step 5: Global system installation (/opt/nova/models)
    sys_candidate = Path("/opt/nova/models") / filename
    if sys_candidate.is_file():
        return sys_candidate

    # Step 6: Source development checkout (<source_root>/models)
    running_in_packaged_venv = sys.prefix.startswith("/opt/nova") or str(
        Path(__file__).resolve()
    ).startswith("/opt/nova")
    if not running_in_packaged_venv:
        dev_root = source_root
        if dev_root is None:
            for parent in Path(__file__).resolve().parents:
                if (parent / "pyproject.toml").is_file() or (parent / ".git").is_dir():
                    dev_root = parent
                    break
        if dev_root is not None and (
            (dev_root / "pyproject.toml").is_file() or (dev_root / ".git").is_dir()
        ):
            src_candidate = dev_root / "models" / filename
            if src_candidate.is_file():
                return src_candidate

    return None


def _pick_setting_str(
    *,
    field_name: str,
    explicit_fields: set[str],
    cli_value: str | None,
    env_var: str | None,
    toml_value: str | None,
    default_value: str,
    env: Mapping[str, str],
) -> str:
    if field_name in explicit_fields and cli_value is not None:
        return cli_value
    if env_var is not None:
        env_val = env.get(env_var, "").strip()
        if env_val:
            return env_val
    if toml_value is not None:
        return toml_value
    return default_value


def config_from_namespace(
    ns: argparse.Namespace,
    *,
    persisted_settings: NovaPersistedSettings | None = None,
    env: Mapping[str, str] | None = None,
) -> NovaDesktopConfig:
    """Build `NovaDesktopConfig` applying precedence CLI > ENV > config.toml > defaults."""
    effective_env = env if env is not None else os.environ
    explicit_fields: set[str] = set(getattr(ns, "_explicit_cli_fields", set()))

    raw_cfg_path = getattr(ns, "config_path", None)
    resolved_cfg_path = (
        Path(raw_cfg_path).expanduser()
        if isinstance(raw_cfg_path, str) and raw_cfg_path.strip()
        else resolve_config_file_path(effective_env)
    )
    persisted = (
        persisted_settings
        if persisted_settings is not None
        else TomlConfigStore(resolved_cfg_path).load()
    )

    roots = tuple(ns.allowed_roots) if ns.allowed_roots else _default_allowed_roots()
    models_dir = getattr(ns, "models_dir", None)

    wake_models = tuple(ns.wakeword_models) if ns.wakeword_models else ()
    if not wake_models:
        resolved_ww = resolve_model_path(
            "oye_nova.onnx",
            explicit_param=effective_env.get("NOVA_WAKEWORD_MODEL"),
            models_dir=models_dir,
        )
        if resolved_ww is not None:
            wake_models = (str(resolved_ww),)

    vad_model = ns.silero_vad_model
    resolved_vad = resolve_model_path(
        "silero_vad.onnx",
        explicit_param=vad_model,
        models_dir=models_dir,
    )
    if resolved_vad is not None:
        vad_model = str(resolved_vad)

    tts_engine_raw = _pick_setting_str(
        field_name="tts_engine",
        explicit_fields=explicit_fields,
        cli_value=ns.tts_engine,
        env_var="NOVA_TTS_ENGINE",
        toml_value=persisted.voice.tts_engine,
        default_value="kokoro",
        env=effective_env,
    )
    tts_engine: TtsEngineChoice = "piper" if tts_engine_raw == "piper" else "kokoro"

    piper_model = ns.piper_model
    resolved_piper = resolve_model_path(
        "es_ES-davefx-medium.onnx",
        explicit_param=piper_model,
        models_dir=models_dir,
    )
    if resolved_piper is not None:
        piper_model = str(resolved_piper)
        if tts_engine == "kokoro" and not ns.kokoro_model:
            tts_engine = "piper"

    piper_bin = ns.piper_binary
    if piper_bin == "piper" and shutil.which("piper") is None:
        venv_piper = Path(sys.executable).parent / "piper"
        if venv_piper.is_file() and os.access(venv_piper, os.X_OK):
            piper_bin = str(venv_piper)

    whisper_model = _pick_setting_str(
        field_name="whisper_model",
        explicit_fields=explicit_fields,
        cli_value=ns.whisper_model,
        env_var="NOVA_WHISPER_MODEL",
        toml_value=persisted.voice.whisper_model,
        default_value="small",
        env=effective_env,
    )
    whisper_dev_raw = _pick_setting_str(
        field_name="whisper_device",
        explicit_fields=explicit_fields,
        cli_value=ns.whisper_device,
        env_var="NOVA_WHISPER_DEVICE",
        toml_value=persisted.voice.whisper_device,
        default_value="cuda",
        env=effective_env,
    )
    whisper_dev: InferenceDevice = "cpu" if whisper_dev_raw == "cpu" else "cuda"
    if whisper_dev == "cuda" and not shutil.which("nvidia-smi"):
        whisper_dev = "cpu"

    pi_provider = _pick_setting_str(
        field_name="pi_provider",
        explicit_fields=explicit_fields,
        cli_value=ns.pi_provider,
        env_var="NOVA_PI_PROVIDER",
        toml_value=persisted.models.pi_provider,
        default_value="nvidia",
        env=effective_env,
    )
    pi_model = _pick_setting_str(
        field_name="pi_model",
        explicit_fields=explicit_fields,
        cli_value=ns.pi_model,
        env_var="NOVA_PI_MODEL",
        toml_value=persisted.models.pi_model,
        default_value="deepseek-ai/deepseek-v4.1-flash",
        env=effective_env,
    )
    tier2_model = _pick_setting_str(
        field_name="tier2_model",
        explicit_fields=explicit_fields,
        cli_value=ns.tier2_model,
        env_var="NOVA_TIER2_MODEL",
        toml_value=persisted.models.tier2_model,
        default_value="claude-opus-5-5-high",
        env=effective_env,
    )

    initial_ws = ns.workspace
    if (
        initial_ws is None
        and persisted.general.remember_workspace
        and persisted.general.last_active_workspace
    ):
        ws_candidate = Path(persisted.general.last_active_workspace)
        if ws_candidate.is_dir():
            resolved_candidate = ws_candidate.resolve()
            if any(resolved_candidate.is_relative_to(Path(root).resolve()) for root in roots):
                initial_ws = str(resolved_candidate)

    enable_voice = False if ns.no_voice else bool(persisted.voice.enable_voice)

    return NovaDesktopConfig(
        port=int(ns.port),
        allowed_roots=roots,
        initial_workspace=initial_ws,
        open_browser=not bool(ns.no_browser),
        browser_preference=ns.browser,
        enable_voice=enable_voice,
        ipc_runtime_dir=ns.ipc_runtime_dir,
        pi_executable=ns.pi_executable,
        pi_provider=pi_provider,
        pi_model=pi_model,
        agy_executable=ns.agy_executable,
        tier2_model=tier2_model,
        wakeword_models=wake_models,
        silero_vad_model_path=vad_model,
        whisper_model=whisper_model,
        whisper_language=persisted.voice.whisper_language,
        whisper_device=whisper_dev,
        tts_engine=tts_engine,
        kokoro_model_path=ns.kokoro_model,
        kokoro_voices_path=ns.kokoro_voices,
        kokoro_voice=persisted.voice.kokoro_voice,
        tts_speed=persisted.voice.tts_speed,
        piper_binary=piper_bin,
        piper_model_path=piper_model,
        capture_target=persisted.audio.capture_target,
        playback_target=persisted.audio.playback_target,
        sensitivity_preset=persisted.voice.sensitivity_preset,
        wakeword_threshold=persisted.voice.wakeword_threshold,
        speech_threshold=persisted.voice.speech_threshold,
        barge_in_threshold=persisted.voice.barge_in_threshold,
        ptt_mode=persisted.shortcuts.ptt_mode,
        global_shortcut=persisted.shortcuts.global_shortcut,
        remember_workspace=persisted.general.remember_workspace,
        config_path=str(resolved_cfg_path),
    )


async def run_desktop_daemon(config: NovaDesktopConfig) -> int:
    """Run `NovaDesktopRuntime` until `SIGINT` or `SIGTERM` triggers orderly teardown."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)
            installed_signals.append(sig)

    try:
        async with NovaDesktopRuntime(config) as runtime:
            sys.stdout.write(
                f"Nova Desktop UI ready at {runtime.base_url} "
                f"(voice_enabled={str(runtime.voice_enabled).lower()})\n"
            )
            sys.stdout.flush()
            await stop_event.wait()
        return 0
    finally:
        for sig in installed_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)


def _run_control_cli(args: Sequence[str]) -> int:
    from nova.adapters.control_ipc import VALID_CONTROL_ACTIONS, send_control_command

    if not args:
        allowed = "|".join(sorted(VALID_CONTROL_ACTIONS))
        sys.stderr.write(f"Usage: nova control <{allowed}>\n")
        sys.stderr.flush()
        return 2
    action = args[0]
    try:
        resp = send_control_command(action)
    except ValueError as exc:
        sys.stderr.write(f"nova control error: {exc}\n")
        sys.stderr.flush()
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"nova control unavailable: {exc}\n")
        sys.stderr.flush()
        return 1

    if not resp.get("ok"):
        sys.stderr.write(f"nova control rejected: {resp.get('message', 'unknown error')}\n")
        sys.stderr.flush()
        return 1

    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for `nova-desktop` and `python -m nova`."""
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if raw_argv and raw_argv[0] == "control":
        return _run_control_cli(raw_argv[1:])

    parser = build_arg_parser()
    ns = parser.parse_args(raw_argv)
    if ns.version:
        try:
            import importlib.metadata

            ver = importlib.metadata.version("nova-voice")
            sys.stdout.write(f"nova-voice {ver}\n")
        except Exception:
            sys.stdout.write("nova-voice unknown\n")
        sys.stdout.flush()
        return 0
    if getattr(ns, "download_models", False):
        from nova.adapters.models_downloader import download_models

        dest = ns.dest or (Path.home() / ".local" / "share" / "nova" / "models")
        return download_models(
            dest=dest,
            import_dir=getattr(ns, "import_dir", None),
            import_wakeword=getattr(ns, "import_wakeword", None),
        )
    try:
        config = config_from_namespace(ns)
        return asyncio.run(run_desktop_daemon(config))
    except KeyboardInterrupt:
        return 0
    except NovaDomainError as exc:
        safe_msg = redact_sensitive_text(str(exc))
        sys.stderr.write(f"nova-desktop error [{exc.code}]: {safe_msg}\n")
        sys.stderr.flush()
        return 1
    except ValueError as exc:
        safe_msg = redact_sensitive_text(str(exc))
        sys.stderr.write(f"nova-desktop configuration error: {safe_msg}\n")
        sys.stderr.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
