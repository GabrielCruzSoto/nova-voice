"""System health diagnostics and pre-flight dependency validator (NOVA-010)."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


@runtime_checkable
class DiagnosticsConfig(Protocol):
    """Protocol for desktop launcher configuration needed by diagnostics."""

    pi_executable: str
    agy_executable: str
    enable_voice: bool
    wakeword_models: list[str]
    silero_vad_model_path: str | None
    piper_model_path: str | None


WhichResolver = Callable[[str], str | None]


PackageManagerName = Literal["apt", "dnf", "pacman", "zypper", "brew", "winget", "unknown"]


class HostEnvironmentInfo(BaseModel):
    """Detected OS platform and package manager used to tailor install remedies."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    platform: str
    distro_id: str | None = None
    package_manager: PackageManagerName = "unknown"


class ComponentCheckResult(BaseModel):
    """Status of an individual system executable or local AI model."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    category: Literal["cli", "audio", "model"]
    installed: bool
    path: str | None = None
    required: bool = True
    details: str = ""
    remedy: str = ""


class SystemHealthReport(BaseModel):
    """Aggregate health status of all required and optional Nova components."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    all_ok: bool
    missing_critical_count: int = 0
    missing_optional_count: int = 0
    components: list[ComponentCheckResult] = Field(default_factory=list)
    alert_message: str | None = None
    host_environment: HostEnvironmentInfo | None = None


def _parse_os_release(os_release_path: Path) -> tuple[str | None, list[str]]:
    if not os_release_path.is_file():
        return None, []
    distro_id: str | None = None
    id_like: list[str] = []
    try:
        for line in os_release_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw_val = stripped.split("=", 1)
            val = raw_val.strip().strip('"').strip("'").lower()
            if key == "ID" and val:
                distro_id = val
            elif key == "ID_LIKE" and val:
                id_like = [tok for tok in val.split() if tok]
    except OSError:
        return None, []
    return distro_id, id_like


def detect_host_environment(
    *,
    which_resolver: WhichResolver = shutil.which,
    os_release_path: Path | None = None,
    platform_name: str | None = None,
) -> HostEnvironmentInfo:
    """Detect host OS family and preferred package manager without spawning subprocesses."""
    plat = (platform_name or sys.platform).lower()
    if plat.startswith("darwin"):
        pm: PackageManagerName = "brew" if which_resolver("brew") else "unknown"
        return HostEnvironmentInfo(platform="darwin", distro_id="macos", package_manager=pm)
    if plat.startswith("win"):
        pm = "winget" if which_resolver("winget") else "unknown"
        return HostEnvironmentInfo(platform="windows", distro_id="windows", package_manager=pm)

    release_file = os_release_path if os_release_path is not None else Path("/etc/os-release")
    distro_id, id_like = _parse_os_release(release_file)

    pm_checks: tuple[tuple[str, PackageManagerName], ...] = (
        ("apt-get", "apt"),
        ("apt", "apt"),
        ("dnf", "dnf"),
        ("pacman", "pacman"),
        ("zypper", "zypper"),
    )
    for binary, candidate_pm in pm_checks:
        if which_resolver(binary):
            return HostEnvironmentInfo(
                platform="linux",
                distro_id=distro_id,
                package_manager=candidate_pm,
            )

    tokens = {tok for tok in ([distro_id, *id_like] if distro_id else id_like) if tok}
    if tokens & {"ubuntu", "debian", "linuxmint", "pop"}:
        fallback_pm: PackageManagerName = "apt"
    elif tokens & {"fedora", "rhel", "centos", "rocky", "almalinux"}:
        fallback_pm = "dnf"
    elif tokens & {"arch", "manjaro", "endeavouros"}:
        fallback_pm = "pacman"
    elif tokens & {"opensuse", "suse", "sles", "opensuse-tumbleweed", "opensuse-leap"}:
        fallback_pm = "zypper"
    else:
        fallback_pm = "unknown"

    return HostEnvironmentInfo(
        platform="linux",
        distro_id=distro_id,
        package_manager=fallback_pm,
    )


def _node_install_prefix(pm: PackageManagerName) -> str | None:
    if pm == "apt":
        return "sudo apt update && sudo apt install -y nodejs npm"
    if pm == "dnf":
        return "sudo dnf install -y nodejs npm"
    if pm == "pacman":
        return "sudo pacman -S --needed nodejs npm"
    if pm == "zypper":
        return "sudo zypper install -y nodejs npm"
    if pm == "brew":
        return "brew install node"
    if pm == "winget":
        return "winget install OpenJS.NodeJS.LTS"
    return None


def build_component_remedy(
    component_name: str,
    env: HostEnvironmentInfo,
    *,
    which_resolver: WhichResolver = shutil.which,
) -> str:
    """Return an actionable, OS/package-manager-aware installation remedy for a missing item."""
    pm = env.package_manager
    if component_name in ("audio_capture", "audio_playback"):
        if pm == "apt":
            return "Ejecutá: sudo apt update && sudo apt install -y pipewire-bin pulseaudio-utils"
        if pm == "dnf":
            return "Ejecutá: sudo dnf install -y pipewire-utils pulseaudio-utils"
        if pm == "pacman":
            return "Ejecutá: sudo pacman -S --needed pipewire pipewire-pulse"
        if pm == "zypper":
            return "Ejecutá: sudo zypper install -y pipewire-tools pulseaudio-utils"
        return "Instalá los paquetes pipewire o pulseaudio-utils con el gestor de tu sistema."

    if component_name == "pi":
        has_npm = bool(which_resolver("npm"))
        if has_npm:
            return (
                "Instalá Pi CLI con: npm install -g @mariozechner/pi-coding-agent "
                "(o creá un enlace en ~/.local/bin/pi)"
            )
        node_cmd = _node_install_prefix(pm)
        if node_cmd:
            return (
                f"Instalá Node.js/npm y Pi CLI con: {node_cmd} && "
                "npm install -g @mariozechner/pi-coding-agent"
            )
        return (
            "Instalá Pi CLI con 'npm install -g @mariozechner/pi-coding-agent' "
            "o creá un enlace en ~/.local/bin/pi"
        )

    if component_name == "agy":
        return (
            "Instalá Antigravity CLI en ~/.local/bin/agy "
            "para tareas de programación profunda (opcional)"
        )

    if component_name.startswith("model_"):
        return (
            "Usá el botón 'Instalar modelos de voz' en el asistente inicial "
            "o ejecutá 'nova-desktop --download-models'"
        )

    return "Instalá o configurá este componente en tu sistema y volvé a verificar."


def _resolve_cli_binary(
    name: str,
    which_resolver: WhichResolver,
    fallback_paths: tuple[Path, ...] = (),
) -> tuple[bool, str | None]:
    target_path = Path(name).expanduser()
    if target_path.is_file() and os.access(target_path, os.X_OK):
        return True, str(target_path.resolve())
    resolved = which_resolver(name)
    if resolved:
        return True, resolved
    for fb in fallback_paths:
        expanded = fb.expanduser().resolve()
        if expanded.is_file() and os.access(expanded, os.X_OK):
            return True, str(expanded)
    return False, None


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

    # Step 7: Bundled package asset fallback (e.g., oye_nova.onnx shipped inside nova package)
    bundled_candidate = Path(__file__).resolve().parent.parent / "assets" / "models" / filename
    if bundled_candidate.is_file():
        return bundled_candidate

    return None


def check_system_components(
    config: DiagnosticsConfig | Any | None = None,
    *,
    which_resolver: WhichResolver = shutil.which,
    source_root: Path | None = None,
    os_release_path: Path | None = None,
) -> SystemHealthReport:
    """Audit system dependencies and voice models, producing a structured health report."""
    checks: list[ComponentCheckResult] = []
    host_env = detect_host_environment(
        which_resolver=which_resolver,
        os_release_path=os_release_path,
    )

    # 1. Multi-engine CLI agent detection (NOVA-017: claude, opencode, agy, pi)
    pi_name = config.pi_executable if config is not None else "pi"
    agy_name = config.agy_executable if config is not None else "agy"
    claude_name = getattr(config, "claude_executable", "claude") if config is not None else "claude"
    opencode_name = (
        getattr(config, "opencode_executable", "opencode") if config is not None else "opencode"
    )

    cli_specs: tuple[tuple[str, str, str, tuple[Path, ...]], ...] = (
        (
            "claude",
            "Claude Code",
            claude_name,
            (
                Path.home() / ".local" / "bin" / "claude",
                Path.home() / ".claude" / "local" / "claude",
            ),
        ),
        (
            "opencode",
            "OpenCode",
            opencode_name,
            (
                Path.home() / ".opencode" / "bin" / "opencode",
                Path.home() / ".local" / "bin" / "opencode",
            ),
        ),
        (
            "agy",
            "Antigravity",
            agy_name,
            (Path.home() / ".local" / "bin" / "agy",),
        ),
        (
            "pi",
            "Pi CLI",
            pi_name,
            (
                Path.home() / ".local" / "bin" / "pi",
                Path.home() / ".pi" / "agent" / "bin" / "pi",
            ),
        ),
    )
    detected_engines: list[tuple[str, str]] = []
    for engine_id, label, exe_name, fallbacks in cli_specs:
        ok, resolved_path = _resolve_cli_binary(
            exe_name,
            which_resolver,
            fallback_paths=fallbacks,
        )
        if ok and resolved_path is not None:
            detected_engines.append((engine_id, resolved_path))
            checks.append(
                ComponentCheckResult(
                    name=engine_id,
                    category="cli",
                    installed=True,
                    path=resolved_path,
                    required=False,
                    details=f"{label} encontrado en {resolved_path}",
                    remedy="",
                )
            )

    if detected_engines:
        summary_engines = ", ".join(name for name, _ in detected_engines)
        checks.append(
            ComponentCheckResult(
                name="agent_runtime",
                category="cli",
                installed=True,
                path=detected_engines[0][1],
                required=True,
                details=f"Motores CLI compatibles detectados: {summary_engines}",
                remedy="",
            )
        )
    else:
        os_cli_remedy = build_component_remedy("pi", host_env, which_resolver=which_resolver)
        checks.append(
            ComponentCheckResult(
                name="agent_runtime",
                category="cli",
                installed=False,
                path=None,
                required=True,
                details="No se encontró ningún agente CLI compatible (claude, opencode, agy o pi)",
                remedy=(
                    "Instalá al menos un agente CLI compatible: Claude Code (claude), "
                    "OpenCode (opencode), Antigravity (agy) o Pi CLI (pi). "
                    f"{os_cli_remedy}"
                ),
            )
        )

    # 3. Audio tools (PipeWire / PulseAudio)
    pw_rec_ok = bool(which_resolver("pw-record") or which_resolver("parecord"))
    rec_path = which_resolver("pw-record") or which_resolver("parecord")
    checks.append(
        ComponentCheckResult(
            name="audio_capture",
            category="audio",
            installed=pw_rec_ok,
            path=rec_path,
            required=True,
            details=(
                f"Herramienta de captura encontrada: {rec_path}"
                if pw_rec_ok
                else "No se encontró 'pw-record' ni 'parecord'"
            ),
            remedy=(
                ""
                if pw_rec_ok
                else build_component_remedy(
                    "audio_capture", host_env, which_resolver=which_resolver
                )
            ),
        )
    )

    pw_play_ok = bool(which_resolver("pw-play") or which_resolver("paplay"))
    play_path = which_resolver("pw-play") or which_resolver("paplay")
    checks.append(
        ComponentCheckResult(
            name="audio_playback",
            category="audio",
            installed=pw_play_ok,
            path=play_path,
            required=True,
            details=(
                f"Herramienta de reproducción encontrada: {play_path}"
                if pw_play_ok
                else "No se encontró 'pw-play' ni 'paplay'"
            ),
            remedy=(
                ""
                if pw_play_ok
                else build_component_remedy(
                    "audio_playback", host_env, which_resolver=which_resolver
                )
            ),
        )
    )

    # 4. Local AI voice models
    voice_enabled = config.enable_voice if config is not None else True
    if voice_enabled:
        models_dir = getattr(config, "models_dir", None) if config is not None else None

        # Wake Word model
        ww_explicit = (
            config.wakeword_models[0]
            if (config is not None and config.wakeword_models)
            else None
        )
        ww_path = resolve_model_path(
            "oye_nova.onnx",
            explicit_param=ww_explicit,
            models_dir=models_dir,
            source_root=source_root,
        )
        checks.append(
            ComponentCheckResult(
                name="model_wakeword",
                category="model",
                installed=ww_path is not None,
                path=str(ww_path) if ww_path else None,
                required=True,
                details=(
                    f"Modelo Wake Word localizado en {ww_path}"
                    if ww_path
                    else "Modelo 'oye_nova.onnx' no encontrado"
                ),
                remedy=(
                    ""
                    if ww_path
                    else "Ejecutá 'nova-desktop --download-models' para descargar modelos"
                ),
            )
        )

        # OpenWakeWord feature models
        mel_path = resolve_model_path(
            "melspectrogram.onnx",
            models_dir=models_dir,
            source_root=source_root,
        )
        checks.append(
            ComponentCheckResult(
                name="model_melspectrogram",
                category="model",
                installed=mel_path is not None,
                path=str(mel_path) if mel_path else None,
                required=True,
                details=(
                    f"Extractor melspectrogram localizado en {mel_path}"
                    if mel_path
                    else "Modelo 'melspectrogram.onnx' no encontrado"
                ),
                remedy="" if mel_path else "Ejecutá 'nova-desktop --download-models'",
            )
        )

        emb_path = resolve_model_path(
            "embedding_model.onnx",
            models_dir=models_dir,
            source_root=source_root,
        )
        checks.append(
            ComponentCheckResult(
                name="model_embedding",
                category="model",
                installed=emb_path is not None,
                path=str(emb_path) if emb_path else None,
                required=True,
                details=(
                    f"Extractor embedding localizado en {emb_path}"
                    if emb_path
                    else "Modelo 'embedding_model.onnx' no encontrado"
                ),
                remedy="" if emb_path else "Ejecutá 'nova-desktop --download-models'",
            )
        )

        # Silero VAD model
        vad_explicit = config.silero_vad_model_path if config is not None else None
        vad_path = resolve_model_path(
            "silero_vad.onnx",
            explicit_param=vad_explicit,
            models_dir=models_dir,
            source_root=source_root,
        )
        checks.append(
            ComponentCheckResult(
                name="model_silero_vad",
                category="model",
                installed=vad_path is not None,
                path=str(vad_path) if vad_path else None,
                required=True,
                details=(
                    f"Modelo Silero VAD localizado en {vad_path}"
                    if vad_path
                    else "Modelo 'silero_vad.onnx' no encontrado"
                ),
                remedy="" if vad_path else "Ejecutá 'nova-desktop --download-models'",
            )
        )

        # TTS voice model
        tts_explicit = (
            config.piper_model_path
            if (config is not None and getattr(config, "piper_model_path", None))
            else None
        )
        tts_path = resolve_model_path(
            "es_ES-davefx-medium.onnx",
            explicit_param=tts_explicit,
            models_dir=models_dir,
            source_root=source_root,
        )
        checks.append(
            ComponentCheckResult(
                name="model_tts",
                category="model",
                installed=tts_path is not None,
                path=str(tts_path) if tts_path else None,
                required=True,
                details=(
                    f"Modelo de síntesis TTS localizado en {tts_path}"
                    if tts_path
                    else "Modelo TTS 'es_ES-davefx-medium.onnx' no encontrado"
                ),
                remedy="" if tts_path else "Ejecutá 'nova-desktop --download-models'",
            )
        )

    missing_critical = [c for c in checks if c.required and not c.installed]
    missing_optional = [c for c in checks if not c.required and not c.installed]

    alert_message: str | None = None
    if missing_critical:
        names_str = ", ".join(c.name for c in missing_critical)
        alert_message = (
            f"Faltan componentes necesarios: {names_str}. "
            "Por favor instalalos o configuralos para el funcionamiento completo."
        )
    elif missing_optional:
        names_str = ", ".join(c.name for c in missing_optional)
        alert_message = f"Componentes opcionales no encontrados: {names_str}."

    all_ok = len(missing_critical) == 0

    return SystemHealthReport(
        all_ok=all_ok,
        missing_critical_count=len(missing_critical),
        missing_optional_count=len(missing_optional),
        components=checks,
        alert_message=alert_message,
        host_environment=host_env,
    )
