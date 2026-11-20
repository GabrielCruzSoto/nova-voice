"""TDD tests for NOVA-017 T1: secure per-user CredentialStorePort and extra_secrets registration."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from nova.adapters.credential_store import SystemCredentialStore
from nova.domain.errors import DesktopUiError


def test_credential_store_fallback_0600_roundtrip_and_secret_callback(tmp_path: Path) -> None:
    cred_path = tmp_path / "nova-data" / "credentials.json"
    registered: list[str] = []
    store = SystemCredentialStore(
        fallback_path=cred_path,
        prefer_libsecret=False,
        on_secret_loaded=registered.append,
    )

    assert store.backend_name == "local_file_0600"
    assert store.get_secret("anthropic", "ANTHROPIC_API_KEY") is None

    synthetic_token = "sk-ant-synthetic-test-value-0001"
    store.set_secret("anthropic", "ANTHROPIC_API_KEY", synthetic_token)

    assert cred_path.is_file()
    assert stat.S_IMODE(cred_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(cred_path.stat().st_mode) == 0o600
    assert synthetic_token in registered

    assert store.get_secret("anthropic", "ANTHROPIC_API_KEY") == synthetic_token
    listed = store.list_configured_env_vars()
    assert listed == {"anthropic": ("ANTHROPIC_API_KEY",)}
    assert store.get_all_secret_values() == (synthetic_token,)

    deleted = store.delete_secret("anthropic", "ANTHROPIC_API_KEY")
    assert deleted is True
    assert store.get_secret("anthropic", "ANTHROPIC_API_KEY") is None
    assert store.delete_secret("anthropic", "ANTHROPIC_API_KEY") is False


def test_credential_store_rejects_unknown_provider_or_env_var(tmp_path: Path) -> None:
    store = SystemCredentialStore(
        fallback_path=tmp_path / "credentials.json",
        prefer_libsecret=False,
    )
    with pytest.raises(DesktopUiError) as exc_prov:
        store.set_secret("unknown_vendor", "UNKNOWN_KEY", "val-12345678")
    assert exc_prov.value.code == "INVALID_CREDENTIAL_REQUEST"

    with pytest.raises(DesktopUiError) as exc_env:
        store.set_secret("anthropic", "OPENAI_API_KEY", "val-12345678")
    assert exc_env.value.code == "INVALID_CREDENTIAL_REQUEST"

    with pytest.raises(DesktopUiError) as exc_empty:
        store.set_secret("anthropic", "ANTHROPIC_API_KEY", "   ")
    assert exc_empty.value.code == "INVALID_CREDENTIAL_REQUEST"


def test_credential_store_uses_libsecret_when_available(tmp_path: Path) -> None:
    memory_keyring: dict[tuple[str, str], str] = {}

    def fake_runner(
        args: list[str],
        *,
        stdin_text: str | None = None,
    ) -> tuple[int, str]:
        cmd = args[1]
        if cmd == "store":
            prov = args[args.index("provider") + 1]
            env_var = args[args.index("env_var") + 1]
            assert stdin_text is not None
            memory_keyring[(prov, env_var)] = stdin_text
            return 0, ""
        if cmd == "lookup":
            prov = args[args.index("provider") + 1]
            env_var = args[args.index("env_var") + 1]
            val = memory_keyring.get((prov, env_var))
            return (0, val) if val is not None else (1, "")
        if cmd == "clear":
            prov = args[args.index("provider") + 1]
            env_var = args[args.index("env_var") + 1]
            memory_keyring.pop((prov, env_var), None)
            return 0, ""
        return 1, ""

    index_path = tmp_path / "credentials_index.json"
    fallback_path = tmp_path / "credentials.json"
    store = SystemCredentialStore(
        fallback_path=fallback_path,
        index_path=index_path,
        prefer_libsecret=True,
        secret_tool_bin="/usr/bin/secret-tool",
        command_runner=fake_runner,
    )

    assert store.backend_name == "libsecret"
    store.set_secret("openai", "OPENAI_API_KEY", "sk-synth-openai-9999")
    # Secret value must NOT be stored in the local index file
    assert "sk-synth-openai-9999" not in index_path.read_text(encoding="utf-8")
    assert not fallback_path.exists()
    assert store.get_secret("openai", "OPENAI_API_KEY") == "sk-synth-openai-9999"
    assert store.list_configured_env_vars() == {"openai": ("OPENAI_API_KEY",)}
    assert store.get_all_secret_values() == ("sk-synth-openai-9999",)
    assert store.delete_secret("openai", "OPENAI_API_KEY") is True
    assert store.get_secret("openai", "OPENAI_API_KEY") is None
