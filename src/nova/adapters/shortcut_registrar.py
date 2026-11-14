"""Desktop global shortcut registration adapter (COSMIC RON + manual fallback, NOVA-008)."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from nova.domain.errors import DesktopUiError
from nova.domain.models import DEFAULT_PTT_COMMAND, ShortcutRegistrationResult

DEFAULT_GLOBAL_SHORTCUT = "Super+Shift+Space"

_ALLOWED_MODIFIERS: dict[str, str] = {
    "super": "Super",
    "shift": "Shift",
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
}
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
_COSMIC_KEY_ALIASES: dict[str, str] = {
    "space": "space",
    "return": "Return",
    "enter": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "BackSpace",
}
_NOVA_SPAWN_RE = re.compile(r'Spawn\("nova\s+control\s+[^"]*"\)')


def parse_shortcut_combo(shortcut: str) -> tuple[tuple[str, ...], str]:
    """Validate `<Mod>+<Key>` against a strict allowlist to prevent RON/shell injection."""
    parts = [part.strip() for part in shortcut.split("+") if part.strip()]
    if len(parts) < 2 or len(parts) > 4:
        raise DesktopUiError(
            code="INVALID_SHORTCUT",
            message=(
                "Shortcut must include at least one modifier and one key "
                "(e.g. Super+Shift+Space)."
            ),
        )

    modifiers: list[str] = []
    for raw_mod in parts[:-1]:
        canonical = _ALLOWED_MODIFIERS.get(raw_mod.lower())
        if canonical is None:
            raise DesktopUiError(
                code="INVALID_SHORTCUT",
                message=f"Unsupported shortcut modifier '{raw_mod}'.",
            )
        if canonical not in modifiers:
            modifiers.append(canonical)

    raw_key = parts[-1]
    if not _SAFE_KEY_RE.fullmatch(raw_key):
        raise DesktopUiError(
            code="INVALID_SHORTCUT",
            message="Shortcut key must be an alphanumeric or named key (e.g. Space, F8, V).",
        )
    key_name = _COSMIC_KEY_ALIASES.get(
        raw_key.lower(),
        raw_key.lower() if len(raw_key) == 1 else raw_key,
    )
    return tuple(modifiers), key_name


def _detect_desktop_environment(env: Mapping[str, str]) -> str:
    raw = env.get("XDG_CURRENT_DESKTOP", "").strip()
    if not raw:
        return "unknown"
    return raw.split(":", 1)[0].strip() or "unknown"


def _cosmic_shortcuts_path(env: Mapping[str, str]) -> Path:
    xdg_config = env.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return base / "cosmic" / "com.system76.CosmicSettings.Shortcuts" / "v1" / "custom"


def _merge_cosmic_ron(existing_text: str, *, modifiers: tuple[str, ...], key: str) -> str:
    mods_str = ", ".join(modifiers)
    new_entry = f'    (modifiers: [{mods_str}], key: "{key}"): Spawn("{DEFAULT_PTT_COMMAND}"),'
    kept_lines: list[str] = []
    for line in existing_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped in ("{", "}"):
            continue
        if _NOVA_SPAWN_RE.search(stripped):
            continue
        kept_lines.append(line if line.startswith("    ") else f"    {stripped}")

    kept_lines.append(new_entry)
    return "{\n" + "\n".join(kept_lines) + "\n}\n"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        fd, raw_tmp = tempfile.mkstemp(prefix=".shortcut.", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(raw_tmp)
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise DesktopUiError(
            code="SHORTCUT_REGISTER_FAILED",
            message=f"Failed to persist COSMIC shortcut file: {exc}",
        ) from exc


class LinuxShortcutRegistrar:
    """Registers `nova control ptt-toggle` in COSMIC shortcuts or returns manual instructions."""

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self._env = env if env is not None else os.environ

    @property
    def desktop_environment(self) -> str:
        return _detect_desktop_environment(self._env)

    @property
    def auto_register_supported(self) -> bool:
        return self.desktop_environment.upper() == "COSMIC"

    def register_ptt_shortcut(
        self,
        shortcut: str = DEFAULT_GLOBAL_SHORTCUT,
    ) -> ShortcutRegistrationResult:
        """Validate `shortcut` and register in COSMIC when active, else return manual status."""
        modifiers, key = parse_shortcut_combo(shortcut)
        de = self.desktop_environment
        if de.upper() != "COSMIC":
            return ShortcutRegistrationResult(
                status="manual_required",
                desktop_environment=de,
                shortcut=shortcut,
                command=DEFAULT_PTT_COMMAND,
                config_file=None,
            )

        target = _cosmic_shortcuts_path(self._env)
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        merged = _merge_cosmic_ron(existing, modifiers=modifiers, key=key)
        _atomic_write_text(target, merged)
        return ShortcutRegistrationResult(
            status="registered",
            desktop_environment=de,
            shortcut=shortcut,
            command=DEFAULT_PTT_COMMAND,
            config_file=str(target),
        )
