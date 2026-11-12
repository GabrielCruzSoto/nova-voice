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
from nova.adapters.audio_pipewire import PipeWireAudioAdapter, PipeWireAudioSettings
from nova.adapters.desktop_ui_server import DesktopUiServer, DesktopUiServerSettings
from nova.adapters.folder_picker_gtk import GtkFolderPickerAdapter
from nova.adapters.gatekeeper_ipc import GatekeeperIPCConfig, UnixSocketGatekeeperServer
from nova.adapters.pi_auditor_adapter import PiAuditorAdapter, PiAuditorConfig
from nova.adapters.pi_rpc_client import PiRpcClient, PiRpcConfig
from nova.adapters.stt_faster_whisper import FasterWhisperSettings, FasterWhisperSttAdapter
from nova.adapters.tts_local_onnx import LocalOnnxTtsAdapter, LocalOnnxTtsSettings
from nova.adapters.vad_silero import SileroVadAdapter, SileroVadSettings
from nova.adapters.wakeword_openwakeword import OpenWakeWordAdapter, OpenWakeWordSettings
from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.application.orchestrator import NovaOrchestrator, OrchestratorSettings
from nova.application.security_gatekeeper import SecurityGatekeeper
from nova.application.ui_state_hub import UiStateHub
from nova.application.voice_coordinator import VoiceCallCoordinator
from nova.domain.errors import (
    DesktopUiError,
    NovaDomainError,
    VoicePipelineError,
    redact_sensitive_text,
)
from nova.domain.events import OrchestratorEventBus
from nova.domain.models import (
    CallSession,
    InferenceDevice,
    ResolveWorkspaceRequest,
    WorkspaceContext,
)
from nova.domain.ports import FolderPickerPort

logger = logging.getLogger("nova.desktop_launcher")

BrowserPreference = Literal["auto", "xdg-open", "firefox"]
TtsEngineChoice = Literal["kokoro", "piper"]

_LOOPBACK_BROWSER_URL_RE = re.compile(r"^http://127\.0\.0\.1:[1-9][0-9]{0,4}/?$")


class BrowserProcessProtocol(Protocol):
    """Minimal protocol for detached browser launcher subprocess handles."""

    async def wait(self) -> int: ...


BrowserSpawner = Callable[..., Awaitable[BrowserProcessProtocol]]
WhichResolver = Callable[[str], str | None]


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
    whisper_device: InferenceDevice = "cuda"
    tts_engine: TtsEngineChoice = "kokoro"
    kokoro_model_path: str | None = None
    kokoro_voices_path: str | None = None
    piper_binary: str = Field(default="piper", min_length=1)
    piper_model_path: str | None = None

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
        "kokoro_model_path",
        "kokoro_voices_path",
        "piper_binary",
        "piper_model_path",
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
    audio = PipeWireAudioAdapter(PipeWireAudioSettings())
    wake_word = OpenWakeWordAdapter(
        OpenWakeWordSettings(model_paths=config.wakeword_models),
    )
    vad = SileroVadAdapter(
        SileroVadSettings(model_path=config.silero_vad_model_path),
    )
    stt = FasterWhisperSttAdapter(
        FasterWhisperSettings(
            model_size_or_path=config.whisper_model,
            preferred_device=config.whisper_device,
            local_files_only=True,
        ),
    )
    tts = LocalOnnxTtsAdapter(
        LocalOnnxTtsSettings(
            engine=config.tts_engine,
            kokoro_model_path=config.kokoro_model_path,
            kokoro_voices_path=config.kokoro_voices_path,
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
    )


async def open_local_browser_window(
    url: str,
    *,
    preference: BrowserPreference = "auto",
    env: Mapping[str, str] | None = None,
    which_resolver: WhichResolver = shutil.which,
    spawner: BrowserSpawner = _default_browser_spawner,
    reap_tasks: set[asyncio.Task[int]] | None = None,
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
    if preference == "firefox":
        ff_bin = which_resolver("firefox")
        if ff_bin:
            argv = (ff_bin, "--new-window", cleaned_url)
    elif preference == "xdg-open":
        xdg_bin = which_resolver("xdg-open")
        if xdg_bin:
            argv = (xdg_bin, cleaned_url)
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

    wait_task = asyncio.create_task(proc.wait())
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
    ) -> None:
        self._config = config or NovaDesktopConfig()
        self._folder_picker = folder_picker or GtkFolderPickerAdapter()
        self._voice_factory: VoiceRuntimeFactory = voice_factory or build_default_voice_coordinator
        self._browser_spawner = browser_spawner
        self._which_resolver = which_resolver

        self._event_bus = OrchestratorEventBus()
        self._ipc_server: UnixSocketGatekeeperServer | None = None
        self._pi_client: PiRpcClient | None = None
        self._voice_coordinator: VoiceRuntimePort | None = None
        self._state_hub: UiStateHub | None = None
        self._ui_server: DesktopUiServer | None = None
        self._gatekeeper: SecurityGatekeeper | None = None
        self._reap_tasks: set[asyncio.Task[int]] = set()
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

            ui_server = DesktopUiServer(
                orchestrator=orchestrator,
                state_hub=state_hub,
                event_bus=self._event_bus,
                voice_coordinator=voice_coord,
                folder_picker=self._folder_picker,
                settings=DesktopUiServerSettings(host="127.0.0.1", port=self._config.port),
            )
            await ui_server.start()
            self._ui_server = ui_server
            self._started = True

            if self._config.open_browser:
                await open_local_browser_window(
                    ui_server.base_url,
                    preference=self._config.browser_preference,
                    which_resolver=self._which_resolver,
                    spawner=self._browser_spawner,
                    reap_tasks=self._reap_tasks,
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
        """Deterministically close UI server, SSE hub, voice stack, Pi RPC, and IPC socket."""
        for task in list(self._reap_tasks):
            task.cancel()
        if self._reap_tasks:
            await asyncio.gather(*self._reap_tasks, return_exceptions=True)
        self._reap_tasks.clear()

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


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI parser for `nova-desktop` / `python -m nova`."""
    parser = argparse.ArgumentParser(
        prog="nova-desktop",
        description="Nova 100% local voice-assisted agentic desktop UI for Linux (127.0.0.1).",
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
        help="Optional initial project directory inside allowed roots.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically launch a desktop browser window on startup.",
    )
    parser.add_argument(
        "--browser",
        choices=("auto", "xdg-open", "firefox"),
        default="auto",
        help="Browser launcher preference when opening the local UI.",
    )
    parser.add_argument(
        "--no-voice",
        action="store_true",
        help="Disable the local voice pipeline and start in text/UI mode only.",
    )
    parser.add_argument(
        "--ipc-runtime-dir",
        default=None,
        help="Custom 0700 directory for the Gatekeeper AF_UNIX socket.",
    )
    parser.add_argument(
        "--pi-executable",
        default="pi",
        help="Executable name or path for Tier 1 Pi CLI.",
    )
    parser.add_argument(
        "--pi-provider",
        default="nvidia",
        help="Provider passed to Tier 1 Pi CLI.",
    )
    parser.add_argument(
        "--pi-model",
        default="deepseek-ai/deepseek-v4.1-flash",
        help="Model identifier passed to Tier 1 Pi CLI.",
    )
    parser.add_argument(
        "--agy-executable",
        default="agy",
        help="Executable name or path for Tier 2 Antigravity CLI.",
    )
    parser.add_argument(
        "--tier2-model",
        default="claude-opus-5-5-high",
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
        help="Local faster-whisper model size or local directory path.",
    )
    parser.add_argument(
        "--whisper-device",
        choices=("cuda", "cpu"),
        default="cuda",
        help="Preferred faster-whisper device (falls back to CPU automatically).",
    )
    parser.add_argument(
        "--tts-engine",
        choices=("kokoro", "piper"),
        default="kokoro",
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
    return parser


def config_from_namespace(ns: argparse.Namespace) -> NovaDesktopConfig:
    """Convert parsed CLI namespace into a validated `NovaDesktopConfig`."""
    roots = tuple(ns.allowed_roots) if ns.allowed_roots else _default_allowed_roots()
    wake_models = tuple(ns.wakeword_models) if ns.wakeword_models else ()
    return NovaDesktopConfig(
        port=int(ns.port),
        allowed_roots=roots,
        initial_workspace=ns.workspace,
        open_browser=not bool(ns.no_browser),
        browser_preference=ns.browser,
        enable_voice=not bool(ns.no_voice),
        ipc_runtime_dir=ns.ipc_runtime_dir,
        pi_executable=ns.pi_executable,
        pi_provider=ns.pi_provider,
        pi_model=ns.pi_model,
        agy_executable=ns.agy_executable,
        tier2_model=ns.tier2_model,
        wakeword_models=wake_models,
        silero_vad_model_path=ns.silero_vad_model,
        whisper_model=ns.whisper_model,
        whisper_device=ns.whisper_device,
        tts_engine=ns.tts_engine,
        kokoro_model_path=ns.kokoro_model,
        kokoro_voices_path=ns.kokoro_voices,
        piper_binary=ns.piper_binary,
        piper_model_path=ns.piper_model,
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for `nova-desktop` and `python -m nova`."""
    parser = build_arg_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)
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
