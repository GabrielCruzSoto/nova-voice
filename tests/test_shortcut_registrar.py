"""TDD tests for NOVA-008 T2: COSMIC shortcut registration and manual fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from nova.adapters.shortcut_registrar import LinuxShortcutRegistrar
from nova.domain.errors import DesktopUiError


def test_cosmic_shortcut_registrar_preserves_existing_and_writes_atomically(
    tmp_path: Path,
) -> None:
    custom_file = tmp_path / "cosmic" / "com.system76.CosmicSettings.Shortcuts" / "v1" / "custom"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text(
        '{\n    (modifiers: [Super], key: "Return"): Spawn("cosmic-term"),\n}\n',
        encoding="utf-8",
    )

    registrar = LinuxShortcutRegistrar(
        env={"XDG_CURRENT_DESKTOP": "COSMIC", "XDG_CONFIG_HOME": str(tmp_path)}
    )
    result = registrar.register_ptt_shortcut("Super+Shift+Space")
    assert result.status == "registered"
    assert result.desktop_environment == "COSMIC"
    assert result.command == "nova control ptt-toggle"
    content = custom_file.read_text(encoding="utf-8")
    assert 'Spawn("cosmic-term")' in content
    assert 'modifiers: [Super, Shift], key: "space"' in content
    assert 'Spawn("nova control ptt-toggle")' in content


def test_shortcut_registrar_returns_manual_required_on_non_cosmic_desktop(
    tmp_path: Path,
) -> None:
    registrar = LinuxShortcutRegistrar(
        env={"XDG_CURRENT_DESKTOP": "GNOME", "XDG_CONFIG_HOME": str(tmp_path)}
    )
    result = registrar.register_ptt_shortcut("Super+Shift+Space")
    assert result.status == "manual_required"
    assert result.desktop_environment == "GNOME"
    assert result.command == "nova control ptt-toggle"


def test_shortcut_registrar_rejects_invalid_or_injection_shortcut(tmp_path: Path) -> None:
    registrar = LinuxShortcutRegistrar(
        env={"XDG_CURRENT_DESKTOP": "COSMIC", "XDG_CONFIG_HOME": str(tmp_path)}
    )
    with pytest.raises(DesktopUiError) as exc_info:
        registrar.register_ptt_shortcut('Super+Space"): Terminate, //')
    assert exc_info.value.code == "INVALID_SHORTCUT"
