"""TDD tests for NOVA-008 T1: persistent TOML config store, 0600 atomic writes, and precedence."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from nova.adapters.config_store import TomlConfigStore
from nova.adapters.desktop_launcher import build_arg_parser, config_from_namespace
from nova.domain.errors import DesktopUiError
from nova.domain.models import NovaPersistedSettings


def test_config_store_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg_path = tmp_path / "nova" / "config.toml"
    store = TomlConfigStore(config_path=cfg_path)
    loaded = store.load()
    assert isinstance(loaded, NovaPersistedSettings)
    assert loaded.general.remember_workspace is True
    assert loaded.shortcuts.ptt_mode == "single_turn"
    assert loaded.voice.tts_speed == 1.0
    assert not cfg_path.exists()


def test_config_store_saves_atomic_0600_and_roundtrips(tmp_path: Path) -> None:
    cfg_path = tmp_path / "xdg" / "nova" / "config.toml"
    store = TomlConfigStore(config_path=cfg_path)

    original = NovaPersistedSettings()
    updated = original.model_copy(
        update={
            "general": original.general.model_copy(
                update={
                    "remember_workspace": True,
                    "last_active_workspace": str(tmp_path / "proj"),
                }
            ),
            "audio": original.audio.model_copy(
                update={"capture_target": "alsa_input.usb-mic", "playback_target": None}
            ),
            "voice": original.voice.model_copy(
                update={
                    "whisper_language": "en",
                    "tts_speed": 1.35,
                    "sensitivity_preset": "high",
                }
            ),
            "models": original.models.model_copy(
                update={"pi_model": "openai/gpt-4o-mini", "tier2_model": "gemini-pro"}
            ),
            "shortcuts": original.shortcuts.model_copy(
                update={"ptt_mode": "toggle_call", "global_shortcut": "Super+Alt+Space"}
            ),
        }
    )

    store.save(updated)
    assert cfg_path.is_file()
    dir_mode = stat.S_IMODE(cfg_path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(cfg_path.stat().st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600

    reloaded = store.load()
    assert reloaded.general.last_active_workspace == str(tmp_path / "proj")
    assert reloaded.audio.capture_target == "alsa_input.usb-mic"
    assert reloaded.voice.whisper_language == "en"
    assert reloaded.voice.tts_speed == pytest.approx(1.35)
    assert reloaded.voice.sensitivity_preset == "high"
    assert reloaded.models.pi_model == "openai/gpt-4o-mini"
    assert reloaded.models.tier2_model == "gemini-pro"
    assert reloaded.shortcuts.ptt_mode == "toggle_call"
    assert reloaded.shortcuts.global_shortcut == "Super+Alt+Space"


def test_config_store_rejects_secrets_and_oversized_files(tmp_path: Path) -> None:
    cfg_path = tmp_path / "nova" / "config.toml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        'schema_version = "1.0"\n[models]\napi_key = "sk-secret-123456"\n',
        encoding="utf-8",
    )
    store = TomlConfigStore(config_path=cfg_path)
    with pytest.raises(DesktopUiError) as exc_info:
        store.load()
    assert exc_info.value.code == "CONFIG_SECRET_FORBIDDEN"

    cfg_path.write_text("# " + ("a" * 70_000), encoding="utf-8")
    with pytest.raises(DesktopUiError) as size_exc:
        store.load()
    assert size_exc.value.code == "CONFIG_TOO_LARGE"


def test_config_store_rejects_secret_like_values_on_save(tmp_path: Path) -> None:
    cfg_path = tmp_path / "nova" / "config.toml"
    with pytest.raises(ValueError):
        NovaPersistedSettings(
            models={
                "pi_provider": "nvidia",
                "pi_model": "sk-live-abcdef123456",
                "tier2_model": "claude-opus-5-5-high",
            }
        )
    assert not cfg_path.exists()


def test_launcher_precedence_cli_over_env_over_toml_over_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "nova" / "config.toml"
    store = TomlConfigStore(config_path=cfg_path)
    persisted = NovaPersistedSettings()
    persisted = persisted.model_copy(
        update={
            "models": persisted.models.model_copy(
                update={
                    "pi_model": "from-toml-pi",
                    "tier2_model": "from-toml-tier2",
                }
            ),
            "voice": persisted.voice.model_copy(
                update={
                    "whisper_model": "medium",
                    "tts_speed": 1.25,
                }
            ),
        }
    )
    store.save(persisted)

    monkeypatch.setenv("NOVA_WHISPER_MODEL", "large-v3")
    monkeypatch.delenv("NOVA_PI_MODEL", raising=False)

    parser = build_arg_parser()
    ns = parser.parse_args(["--no-browser", "--tier2-model", "from-cli-tier2"])
    cfg = config_from_namespace(ns, persisted_settings=store.load(), env=os.environ)

    # CLI > ENV > config.toml > defaults
    assert cfg.tier2_model == "from-cli-tier2"
    assert cfg.whisper_model == "large-v3"
    assert cfg.pi_model == "from-toml-pi"
    assert cfg.tts_speed == pytest.approx(1.25)


def test_faster_whisper_accepts_none_auto_detect_language() -> None:
    from nova.adapters.stt_faster_whisper import FasterWhisperSettings, FasterWhisperSttAdapter

    settings = FasterWhisperSettings(language=None, preferred_device="cpu")
    assert settings.language is None
    adapter = FasterWhisperSttAdapter(settings)
    adapter.update_settings(language="en")
    adapter.update_settings(language=None)


def test_nova013_ac6_first_run_completed_defaults_false_and_roundtrips(tmp_path: Path) -> None:
    cfg_path = tmp_path / "nova" / "config.toml"
    store = TomlConfigStore(config_path=cfg_path)

    default_loaded = store.load()
    assert default_loaded.general.first_run_completed is False

    # Legacy config.toml without first_run_completed loads cleanly as False
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        'schema_version = "1.0"\n[general]\nremember_workspace = true\n',
        encoding="utf-8",
    )
    legacy_loaded = store.load()
    assert legacy_loaded.general.first_run_completed is False

    completed = legacy_loaded.model_copy(
        update={
            "general": legacy_loaded.general.model_copy(
                update={"first_run_completed": True}
            )
        }
    )
    store.save(completed)
    reloaded = store.load()
    assert reloaded.general.first_run_completed is True


def test_nova017_ac3_config_v10_migrates_to_multi_tier_v11_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "nova" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        'schema_version = "1.0"\n'
        "[models]\n"
        'pi_provider = "openai"\n'
        'pi_model = "gpt-4o-mini"\n'
        'tier2_model = "claude-opus-5-5-high"\n',
        encoding="utf-8",
    )
    store = TomlConfigStore(config_path=cfg_path)
    loaded = store.load()
    assert loaded.models.tier1_engine == "auto"
    assert loaded.models.tier1_provider == "openai"
    assert loaded.models.tier1_model == "gpt-4o-mini"
    assert loaded.models.tier2_engine == "auto"
    assert loaded.models.tier2_model == "claude-opus-5-5-high"
    assert loaded.models.single_engine_mode is False

    updated = loaded.model_copy(
        update={
            "models": loaded.models.model_copy(
                update={
                    "tier1_engine": "opencode",
                    "tier1_provider": "anthropic",
                    "tier1_model": "anthropic/claude-haiku-4-5",
                    "tier2_engine": "claude",
                    "tier2_provider": "anthropic",
                    "tier2_model": "opus",
                    "single_engine_mode": False,
                }
            )
        }
    )
    store.save(updated)
    reloaded = store.load()
    assert reloaded.models.tier1_engine == "opencode"
    assert reloaded.models.tier1_model == "anthropic/claude-haiku-4-5"
    assert reloaded.models.tier2_engine == "claude"
    assert reloaded.models.tier2_model == "opus"
    assert reloaded.models.single_engine_mode is False


def test_nova018_f1_config_store_roundtrips_half_duplex_and_confirmation_flags(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "nova" / "config.toml"
    store = TomlConfigStore(config_path=cfg_path)
    base = store.load()
    assert base.voice.half_duplex_turn_mode is True
    assert base.voice.require_voice_confirmation is True

    updated = base.model_copy(
        update={
            "voice": base.voice.model_copy(
                update={
                    "half_duplex_turn_mode": False,
                    "require_voice_confirmation": False,
                }
            )
        }
    )
    store.save(updated)

    raw_text = cfg_path.read_text(encoding="utf-8")
    assert "half_duplex_turn_mode = false" in raw_text
    assert "require_voice_confirmation = false" in raw_text

    reloaded = store.load()
    assert reloaded.voice.half_duplex_turn_mode is False
    assert reloaded.voice.require_voice_confirmation is False
