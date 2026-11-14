"""Atomic 0600 TOML configuration persistence in $XDG_CONFIG_HOME/nova/config.toml (NOVA-008)."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nova.domain.errors import DesktopUiError, reject_secret_settings_keys
from nova.domain.models import NovaPersistedSettings

MAX_CONFIG_FILE_BYTES = 65_536
_reject_secret_keys = reject_secret_settings_keys


def resolve_config_file_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve `$XDG_CONFIG_HOME/nova/config.toml` or `~/.config/nova/config.toml`."""
    effective_env = env if env is not None else os.environ
    xdg_config = effective_env.get("XDG_CONFIG_HOME", "").strip()
    base_dir = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return base_dir / "nova" / "config.toml"


def _normalize_optional_empty_strings(raw: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = dict(raw)
    general = normalized.get("general")
    if isinstance(general, Mapping):
        g_copy = dict(general)
        if g_copy.get("last_active_workspace") == "":
            g_copy["last_active_workspace"] = None
        normalized["general"] = g_copy
    audio = normalized.get("audio")
    if isinstance(audio, Mapping):
        a_copy = dict(audio)
        for key in ("capture_target", "playback_target"):
            if a_copy.get(key) == "":
                a_copy[key] = None
        normalized["audio"] = a_copy
    return normalized


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return '""'
    return json.dumps(str(value), ensure_ascii=False)


def serialize_settings_toml(settings: NovaPersistedSettings) -> str:
    """Serialize validated `NovaPersistedSettings` to clean human-readable TOML."""
    lines: list[str] = [
        f"schema_version = {_toml_value(settings.schema_version)}",
        "",
        "[general]",
        f"remember_workspace = {_toml_value(settings.general.remember_workspace)}",
        f"last_active_workspace = {_toml_value(settings.general.last_active_workspace)}",
        "",
        "[audio]",
        f"capture_target = {_toml_value(settings.audio.capture_target)}",
        f"playback_target = {_toml_value(settings.audio.playback_target)}",
        "",
        "[voice]",
        f"enable_voice = {_toml_value(settings.voice.enable_voice)}",
        f"whisper_language = {_toml_value(settings.voice.whisper_language)}",
        f"whisper_model = {_toml_value(settings.voice.whisper_model)}",
        f"whisper_device = {_toml_value(settings.voice.whisper_device)}",
        f"tts_engine = {_toml_value(settings.voice.tts_engine)}",
        f"kokoro_voice = {_toml_value(settings.voice.kokoro_voice)}",
        f"tts_speed = {_toml_value(settings.voice.tts_speed)}",
        f"sensitivity_preset = {_toml_value(settings.voice.sensitivity_preset)}",
        f"wakeword_threshold = {_toml_value(settings.voice.wakeword_threshold)}",
        f"speech_threshold = {_toml_value(settings.voice.speech_threshold)}",
        f"barge_in_threshold = {_toml_value(settings.voice.barge_in_threshold)}",
        "",
        "[models]",
        f"pi_provider = {_toml_value(settings.models.pi_provider)}",
        f"pi_model = {_toml_value(settings.models.pi_model)}",
        f"tier2_model = {_toml_value(settings.models.tier2_model)}",
        "",
        "[shortcuts]",
        f"ptt_mode = {_toml_value(settings.shortcuts.ptt_mode)}",
        f"global_shortcut = {_toml_value(settings.shortcuts.global_shortcut)}",
        "",
    ]
    return "\n".join(lines)


class TomlConfigStore:
    """Reads and atomically writes `NovaPersistedSettings` with 0700/0600 permissions."""

    def __init__(self, config_path: Path | None = None) -> None:
        self._path = (config_path or resolve_config_file_path()).expanduser()

    @property
    def config_path(self) -> Path:
        return self._path

    def load(self) -> NovaPersistedSettings:
        """Load `config.toml` if present, enforcing size, secret rejection, and schema."""
        if not self._path.exists():
            return NovaPersistedSettings()

        try:
            raw_bytes = self._path.read_bytes()
        except OSError as exc:
            raise DesktopUiError(
                code="CONFIG_READ_FAILED",
                message=f"Failed to read configuration file: {exc}",
            ) from exc

        if len(raw_bytes) > MAX_CONFIG_FILE_BYTES:
            raise DesktopUiError(
                code="CONFIG_TOO_LARGE",
                message=f"Configuration file exceeds {MAX_CONFIG_FILE_BYTES} bytes.",
            )

        try:
            parsed = tomllib.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise DesktopUiError(
                code="CONFIG_INVALID_TOML",
                message=f"Configuration file is not valid UTF-8 TOML: {exc}",
            ) from exc

        _reject_secret_keys(parsed)
        normalized = _normalize_optional_empty_strings(parsed)
        try:
            return NovaPersistedSettings.model_validate(normalized)
        except ValidationError as exc:
            raise DesktopUiError(
                code="INVALID_SETTINGS",
                message=f"Invalid configuration in config.toml: {exc}",
            ) from exc

    def save(self, settings: NovaPersistedSettings) -> Path:
        """Atomically persist `settings` with `0700` dir and `0600` file permissions."""
        validated = NovaPersistedSettings.model_validate(settings)
        payload = serialize_settings_toml(validated).encode("utf-8")
        if len(payload) > MAX_CONFIG_FILE_BYTES:
            raise DesktopUiError(
                code="CONFIG_TOO_LARGE",
                message=f"Serialized configuration exceeds {MAX_CONFIG_FILE_BYTES} bytes.",
            )

        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        except OSError as exc:
            raise DesktopUiError(
                code="CONFIG_PERSIST_FAILED",
                message=f"Failed to prepare configuration directory: {exc}",
            ) from exc

        tmp_path: Path | None = None
        try:
            fd, raw_tmp = tempfile.mkstemp(
                prefix=".config.",
                suffix=".tmp",
                dir=str(parent),
            )
            tmp_path = Path(raw_tmp)
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
            os.chmod(self._path, 0o600)
        except OSError as exc:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            raise DesktopUiError(
                code="CONFIG_PERSIST_FAILED",
                message=f"Failed to persist configuration atomically: {exc}",
            ) from exc

        return self._path
