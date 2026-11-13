"""Tests for S3: Voice models canonical resolution algorithm (AC-12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nova.adapters.desktop_launcher import (
    build_arg_parser,
    config_from_namespace,
    resolve_model_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_parents_3_models_is_eradicated() -> None:
    launcher_file = _REPO_ROOT / "src" / "nova" / "adapters" / "desktop_launcher.py"
    content = launcher_file.read_text(encoding="utf-8")
    assert 'parents[3] / "models"' not in content
    assert "parents[3]" not in content


def test_models_resolution_step1_specific_param(tmp_path: Path) -> None:
    # Model exists at specific param path
    specific_file = tmp_path / "specific_silero.onnx"
    specific_file.write_bytes(b"silero vad content")

    resolved = resolve_model_path(
        "silero_vad.onnx",
        explicit_param=str(specific_file),
        models_dir=None,
        source_root=None,
    )
    assert resolved == specific_file.resolve()


def test_models_resolution_step2_models_dir(tmp_path: Path) -> None:
    models_dir = tmp_path / "custom_models"
    models_dir.mkdir()
    model_file = models_dir / "silero_vad.onnx"
    model_file.write_bytes(b"vad")

    resolved = resolve_model_path(
        "silero_vad.onnx",
        explicit_param=None,
        models_dir=str(models_dir),
        source_root=None,
    )
    assert resolved == model_file.resolve()


def test_models_resolution_step2_models_dir_invalid_raises_value_error(tmp_path: Path) -> None:
    non_existent = tmp_path / "does_not_exist"
    with pytest.raises(ValueError) as exc_info:
        resolve_model_path(
            "silero_vad.onnx",
            explicit_param=None,
            models_dir=str(non_existent),
            source_root=None,
        )
    assert str(non_existent) in str(exc_info.value)


def test_models_resolution_step3_nova_models_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "env_models"
    env_dir.mkdir()
    model_file = env_dir / "silero_vad.onnx"
    model_file.write_bytes(b"vad")

    monkeypatch.setenv("NOVA_MODELS_DIR", str(env_dir))

    resolved = resolve_model_path(
        "silero_vad.onnx",
        explicit_param=None,
        models_dir=None,
        source_root=None,
    )
    assert resolved == model_file.resolve()


def test_models_resolution_step3_invalid_env_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    non_existent = tmp_path / "non_existent_env_dir"
    monkeypatch.setenv("NOVA_MODELS_DIR", str(non_existent))

    with pytest.raises(ValueError) as exc_info:
        resolve_model_path(
            "silero_vad.onnx",
            explicit_param=None,
            models_dir=None,
            source_root=None,
        )
    assert str(non_existent) in str(exc_info.value)


def test_models_resolution_step4_user_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake_home"
    user_models = fake_home / ".local" / "share" / "nova" / "models"
    user_models.mkdir(parents=True)
    model_file = user_models / "silero_vad.onnx"
    model_file.write_bytes(b"user vad")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("NOVA_MODELS_DIR", raising=False)

    resolved = resolve_model_path(
        "silero_vad.onnx",
        explicit_param=None,
        models_dir=None,
        source_root=None,
    )
    assert resolved == model_file.resolve()


def test_models_resolution_step6_source_root_when_in_dev_checkout(tmp_path: Path) -> None:
    dev_root = tmp_path / "dev_repo"
    dev_root.mkdir()
    (dev_root / "pyproject.toml").write_text("[project]\nname='test'\n")
    models_dir = dev_root / "models"
    models_dir.mkdir()
    model_file = models_dir / "silero_vad.onnx"
    model_file.write_bytes(b"dev vad")

    resolved = resolve_model_path(
        "silero_vad.onnx",
        explicit_param=None,
        models_dir=None,
        source_root=dev_root,
    )
    assert resolved == model_file.resolve()


def test_models_resolution_clean_fallback_when_not_found(tmp_path: Path) -> None:
    resolved = resolve_model_path(
        "non_existent_model.onnx",
        explicit_param=None,
        models_dir=None,
        source_root=tmp_path,
    )
    assert resolved is None


def test_config_from_namespace_handles_invalid_models_dir(tmp_path: Path) -> None:
    parser = build_arg_parser()
    invalid_dir = str(tmp_path / "does_not_exist")
    ns = parser.parse_args(["--models-dir", invalid_dir])

    with pytest.raises(ValueError) as exc:
        config_from_namespace(ns)
    assert invalid_dir in str(exc.value)
