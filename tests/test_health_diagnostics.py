"""Tests for pre-flight health diagnostics and component validator (NOVA-010)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nova.adapters.desktop_launcher import NovaDesktopConfig
from nova.application.health_diagnostics import (
    ComponentCheckResult,
    SystemHealthReport,
    check_system_components,
)


def test_health_report_models_instantiation() -> None:
    comp = ComponentCheckResult(
        name="test_tool",
        category="cli",
        installed=True,
        path="/usr/bin/test_tool",
        required=True,
        details="Found",
        remedy="",
    )
    report = SystemHealthReport(
        all_ok=True,
        missing_critical_count=0,
        missing_optional_count=0,
        components=[comp],
        alert_message=None,
    )
    assert report.all_ok is True
    assert len(report.components) == 1
    assert report.components[0].name == "test_tool"


def test_all_components_present_yields_all_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pi_bin = fake_bin / "pi"
    pi_bin.write_text("#!/bin/sh\nexit 0\n")
    pi_bin.chmod(0o755)

    agy_bin = fake_bin / "agy"
    agy_bin.write_text("#!/bin/sh\nexit 0\n")
    agy_bin.chmod(0o755)

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    for m in (
        "oye_nova.onnx",
        "melspectrogram.onnx",
        "embedding_model.onnx",
        "silero_vad.onnx",
        "es_ES-davefx-medium.onnx",
    ):
        (models_dir / m).write_bytes(b"model data")

    def fake_which(cmd: str) -> str | None:
        if cmd in ("pi", str(pi_bin)):
            return str(pi_bin)
        if cmd in ("agy", str(agy_bin)):
            return str(agy_bin)
        if cmd in ("pw-record", "pw-play"):
            return f"/usr/bin/{cmd}"
        return None

    config = NovaDesktopConfig(
        pi_executable=str(pi_bin),
        agy_executable=str(agy_bin),
        enable_voice=True,
    )
    monkeypatch.setenv("NOVA_MODELS_DIR", str(models_dir))

    report = check_system_components(config, which_resolver=fake_which)
    assert report.all_ok is True
    assert report.missing_critical_count == 0
    assert report.alert_message is None
    assert all(c.installed for c in report.components)


def test_missing_pi_and_audio_tools_generates_alert_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("NOVA_MODELS_DIR", raising=False)

    def empty_which(_cmd: str) -> str | None:
        return None

    config = NovaDesktopConfig(
        pi_executable="non_existent_pi",
        agy_executable="non_existent_agy",
        enable_voice=False,
    )

    report = check_system_components(config, which_resolver=empty_which)
    assert report.all_ok is False
    assert report.missing_critical_count >= 2
    assert report.alert_message is not None
    assert "agent_runtime" in report.alert_message

    remedies = [c.remedy for c in report.components if not c.installed]
    assert any("Pi CLI" in r and "Claude Code" in r for r in remedies)
    assert any("pipewire" in r for r in remedies)


def test_nova017_ac1_claude_or_opencode_only_passes_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)

    def claude_only_which(cmd: str) -> str | None:
        if cmd in ("claude", "pw-record", "pw-play"):
            return f"/usr/bin/{cmd}"
        return None

    config = NovaDesktopConfig(enable_voice=False)
    report = check_system_components(config, which_resolver=claude_only_which)
    assert report.all_ok is True
    assert report.missing_critical_count == 0
    assert any(c.name == "claude" and c.installed for c in report.components)
    assert any(c.name == "agent_runtime" and c.installed for c in report.components)


def test_missing_onnx_model_reported_with_download_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_models = tmp_path / "empty_models"
    empty_models.mkdir()
    monkeypatch.setenv("NOVA_MODELS_DIR", str(empty_models))
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    def audio_only_which(cmd: str) -> str | None:
        if cmd in ("pi", "pw-record", "pw-play"):
            return f"/usr/bin/{cmd}"
        return None

    config = NovaDesktopConfig(enable_voice=True)
    report = check_system_components(
        config,
        which_resolver=audio_only_which,
        source_root=tmp_path,
    )
    assert report.all_ok is False
    missing_models = [c for c in report.components if c.category == "model" and not c.installed]
    assert len(missing_models) >= 4
    assert any("download-models" in m.remedy for m in missing_models)


@pytest.mark.parametrize(
    ("pkg_mgr_bin", "expected_pm", "expected_audio_cmd"),
    [
        ("apt-get", "apt", "sudo apt"),
        ("dnf", "dnf", "sudo dnf"),
        ("pacman", "pacman", "sudo pacman"),
        ("zypper", "zypper", "sudo zypper"),
    ],
)
def test_os_package_manager_remediation_for_missing_audio_and_pi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pkg_mgr_bin: str,
    expected_pm: str,
    expected_audio_cmd: str,
) -> None:
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("NOVA_MODELS_DIR", raising=False)

    def pm_which(cmd: str) -> str | None:
        if cmd == pkg_mgr_bin:
            return f"/usr/bin/{pkg_mgr_bin}"
        return None

    config = NovaDesktopConfig(
        pi_executable="non_existent_pi",
        agy_executable="non_existent_agy",
        enable_voice=False,
    )
    report = check_system_components(config, which_resolver=pm_which)
    assert report.host_environment is not None
    assert report.host_environment.package_manager == expected_pm

    audio_comp = next(c for c in report.components if c.name == "audio_capture")
    assert audio_comp.installed is False
    assert expected_audio_cmd in audio_comp.remedy

    agent_comp = next(c for c in report.components if c.name == "agent_runtime")
    assert agent_comp.installed is False
    assert "npm install -g" in agent_comp.remedy
    assert expected_audio_cmd in agent_comp.remedy
