"""Global pytest fixtures guaranteeing host XDG_CONFIG_HOME isolation (NOVA-012 AC-5)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_xdg_config_home(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent any test from reading or mutating the operator's ~/.config/nova/config.toml."""
    xdg_dir = tmp_path_factory.mktemp("xdg_config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_dir))
