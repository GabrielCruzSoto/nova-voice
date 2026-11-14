"""Application service for persistent settings, hot reload, audio catalog, and shortcuts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from nova.adapters.config_store import _reject_secret_keys
from nova.adapters.shortcut_registrar import (
    DEFAULT_PTT_COMMAND,
    LinuxShortcutRegistrar,
    ShortcutRegistrationResult,
)
from nova.domain.errors import DesktopUiError
from nova.domain.events import OrchestratorEventBus, SettingsUpdatedEvent
from nova.domain.models import (
    SENSITIVITY_THRESHOLDS,
    AudioDeviceCatalog,
    NovaPersistedSettings,
    SensitivityPreset,
)
from nova.domain.ports import AudioDeviceDiscoveryPort, ConfigStorePort

_RESTART_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "voice.enable_voice",
        "voice.whisper_model",
        "voice.whisper_device",
    }
)


def _merge_section(
    base: Mapping[str, Any],
    patch: Any,
    *,
    section_name: str,
) -> dict[str, Any]:
    if patch is None:
        return dict(base)
    if not isinstance(patch, Mapping):
        raise DesktopUiError(
            code="INVALID_SETTINGS",
            message=f"Settings section '{section_name}' must be an object.",
        )
    merged = dict(base)
    for key, val in patch.items():
        merged[str(key)] = None if val == "" else val
    return merged


class SettingsService:
    """Coordinates `ConfigStorePort`, audio discovery, shortcut registration, and hot reload."""

    def __init__(
        self,
        *,
        config_store: ConfigStorePort,
        initial_settings: NovaPersistedSettings | None = None,
        audio_discovery: AudioDeviceDiscoveryPort | None = None,
        shortcut_registrar: LinuxShortcutRegistrar | None = None,
        event_bus: OrchestratorEventBus | None = None,
        on_hot_apply: Callable[[NovaPersistedSettings], None] | None = None,
    ) -> None:
        self._store = config_store
        self._settings = initial_settings or config_store.load()
        self._audio_discovery = audio_discovery
        self._shortcut_registrar = shortcut_registrar or LinuxShortcutRegistrar()
        self._event_bus = event_bus
        self._on_hot_apply = on_hot_apply
        self._lock = asyncio.Lock()

    @property
    def current_settings(self) -> NovaPersistedSettings:
        return self._settings

    def get_settings_payload(self) -> dict[str, Any]:
        """Return serialized settings payload matching `GET /api/settings` contract."""
        data = self._settings.model_dump()
        shortcuts_dict = dict(data["shortcuts"])
        shortcuts_dict["control_command"] = DEFAULT_PTT_COMMAND
        shortcuts_dict["desktop_environment"] = self._shortcut_registrar.desktop_environment
        shortcuts_dict["auto_register_supported"] = self._shortcut_registrar.auto_register_supported
        data["shortcuts"] = shortcuts_dict
        return {
            "schema_version": self._settings.schema_version,
            "config_path": str(self._store.config_path),
            "settings": data,
        }

    async def list_audio_devices(self) -> AudioDeviceCatalog:
        """Enumerate local audio inputs/outputs, degrading cleanly when discovery is absent."""
        if self._audio_discovery is None:
            return AudioDeviceCatalog(available=False, backend="none")
        return await self._audio_discovery.list_devices()

    def register_shortcut(self, shortcut: str) -> ShortcutRegistrationResult:
        """Register global PTT shortcut on supported desktops or return manual command."""
        return self._shortcut_registrar.register_ptt_shortcut(shortcut)

    async def update_settings(
        self,
        raw_patch: Mapping[str, Any],
        *,
        correlation_id: str = "corr-settings",
    ) -> dict[str, Any]:
        """Validate, atomically persist `0600`, hot-apply dynamic fields, and publish SSE."""
        _reject_secret_keys(raw_patch)
        allowed_top = {"general", "audio", "voice", "models", "shortcuts"}
        unknown_top = set(raw_patch.keys()) - allowed_top
        if unknown_top:
            raise DesktopUiError(
                code="INVALID_SETTINGS",
                message=f"Unsupported settings section(s): {', '.join(sorted(unknown_top))}",
            )

        async with self._lock:
            prev = self._settings
            prev_dump = prev.model_dump()

            voice_patch = _merge_section(
                prev_dump["voice"],
                raw_patch.get("voice"),
                section_name="voice",
            )
            raw_voice = raw_patch.get("voice")
            if isinstance(raw_voice, Mapping) and "sensitivity_preset" in raw_voice:
                preset_val = str(voice_patch.get("sensitivity_preset", "normal"))
                if preset_val in SENSITIVITY_THRESHOLDS:
                    preset_key: SensitivityPreset = (
                        "low"
                        if preset_val == "low"
                        else "high"
                        if preset_val == "high"
                        else "normal"
                    )
                    ww_t, sp_t, bi_t = SENSITIVITY_THRESHOLDS[preset_key]
                    if "wakeword_threshold" not in raw_voice:
                        voice_patch["wakeword_threshold"] = ww_t
                    if "speech_threshold" not in raw_voice:
                        voice_patch["speech_threshold"] = sp_t
                    if "barge_in_threshold" not in raw_voice:
                        voice_patch["barge_in_threshold"] = bi_t

            merged_dump: dict[str, Any] = {
                "schema_version": "1.0",
                "general": _merge_section(
                    prev_dump["general"],
                    raw_patch.get("general"),
                    section_name="general",
                ),
                "audio": _merge_section(
                    prev_dump["audio"],
                    raw_patch.get("audio"),
                    section_name="audio",
                ),
                "voice": voice_patch,
                "models": _merge_section(
                    prev_dump["models"],
                    raw_patch.get("models"),
                    section_name="models",
                ),
                "shortcuts": _merge_section(
                    prev_dump["shortcuts"],
                    raw_patch.get("shortcuts"),
                    section_name="shortcuts",
                ),
            }

            try:
                updated = NovaPersistedSettings.model_validate(merged_dump)
            except ValidationError as exc:
                raise DesktopUiError(
                    code="INVALID_SETTINGS",
                    message=f"Invalid settings payload: {exc}",
                ) from exc

            await asyncio.to_thread(self._store.save, updated)
            self._settings = updated

            changed_fields: list[str] = []
            updated_dump = updated.model_dump()
            for sec in ("general", "audio", "voice", "models", "shortcuts"):
                for k, v in updated_dump[sec].items():
                    if prev_dump[sec].get(k) != v:
                        changed_fields.append(f"{sec}.{k}")

            restart_fields = tuple(f for f in changed_fields if f in _RESTART_ONLY_FIELDS)
            applied_hot = tuple(f for f in changed_fields if f not in _RESTART_ONLY_FIELDS)

            if self._on_hot_apply is not None:
                self._on_hot_apply(updated)

        if self._event_bus is not None:
            await self._event_bus.publish(
                SettingsUpdatedEvent(
                    correlation_id=correlation_id,
                    requires_restart=bool(restart_fields),
                    restart_fields=restart_fields,
                )
            )

        payload = self.get_settings_payload()
        return {
            "status": "saved",
            "config_path": payload["config_path"],
            "applied_hot": list(applied_hot),
            "requires_restart": bool(restart_fields),
            "restart_fields": list(restart_fields),
            "settings": payload["settings"],
        }

    async def record_active_workspace(self, canonical_path: str) -> None:
        """Persist `last_active_workspace` when `general.remember_workspace` is enabled."""
        async with self._lock:
            if not self._settings.general.remember_workspace:
                return
            if self._settings.general.last_active_workspace == canonical_path:
                return
            updated = self._settings.model_copy(
                update={
                    "general": self._settings.general.model_copy(
                        update={"last_active_workspace": canonical_path}
                    )
                }
            )
            await asyncio.to_thread(self._store.save, updated)
            self._settings = updated
