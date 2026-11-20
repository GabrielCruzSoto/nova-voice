"""Secure per-user API credential store (`libsecret` via secret-tool + 0600 local fallback)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from nova.domain.errors import DesktopUiError
from nova.domain.models import ProviderCredentialStatus

ALLOWED_PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "nvidia": ("NVIDIA_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}

_SECRET_TOOL_SERVICE = "nova-voice"
_MAX_CREDENTIAL_FILE_BYTES = 65_536

CommandRunner = Callable[..., tuple[int, str]]


def resolve_default_credentials_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve `$XDG_DATA_HOME/nova` or `~/.local/share/nova` (separate from config.toml)."""
    effective_env = env if env is not None else os.environ
    xdg_data = effective_env.get("XDG_DATA_HOME", "").strip()
    base_dir = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return base_dir / "nova"


def _default_command_runner(
    args: list[str],
    *,
    stdin_text: str | None = None,
) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            args,
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        return completed.returncode, completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _atomic_write_json_0600(target_path: Path, payload: Mapping[str, object]) -> None:
    parent = target_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    tmp_path: Path | None = None
    try:
        fd, raw_tmp = tempfile.mkstemp(prefix=".cred.", suffix=".tmp", dir=str(parent))
        tmp_path = Path(raw_tmp)
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_path)
        os.chmod(target_path, 0o600)
    except OSError as exc:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise DesktopUiError(
            code="CREDENTIAL_PERSIST_FAILED",
            message=f"Failed to persist secure credentials atomically: {exc}",
        ) from exc


def _read_json_mapping(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    if len(raw) > _MAX_CREDENTIAL_FILE_BYTES:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for prov, entries in parsed.items():
        if isinstance(prov, str) and isinstance(entries, dict):
            clean_entries = {
                str(k): str(v) for k, v in entries.items() if isinstance(k, str) and v
            }
            if clean_entries:
                result[prov] = clean_entries
    return result


def _read_index_mapping(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, list[str]] = {}
    for prov, keys in parsed.items():
        if isinstance(prov, str) and isinstance(keys, list):
            clean = sorted({str(k) for k in keys if isinstance(k, str) and k.strip()})
            if clean:
                result[prov] = clean
    return result


class SystemCredentialStore:
    """Stores optional per-user API tokens in libsecret (`secret-tool`) or 0600 local store."""

    def __init__(
        self,
        *,
        fallback_path: Path | None = None,
        index_path: Path | None = None,
        prefer_libsecret: bool = True,
        secret_tool_bin: str | None = None,
        command_runner: CommandRunner = _default_command_runner,
        on_secret_loaded: Callable[[str], None] | None = None,
    ) -> None:
        base_dir = resolve_default_credentials_dir()
        self._fallback_path = (fallback_path or (base_dir / "credentials.json")).expanduser()
        self._index_path = (index_path or (base_dir / "credentials_index.json")).expanduser()
        self._runner = command_runner
        self._on_secret_loaded = on_secret_loaded
        resolved_tool = secret_tool_bin if secret_tool_bin is not None else shutil.which(
            "secret-tool"
        )
        self._secret_tool_bin: str | None = resolved_tool if prefer_libsecret else None
        self._backend: Literal["libsecret", "local_file_0600"] = (
            "libsecret" if self._secret_tool_bin else "local_file_0600"
        )

    @property
    def backend_name(self) -> Literal["libsecret", "local_file_0600"]:
        return self._backend

    def set_secret_callback(self, callback: Callable[[str], None]) -> None:
        """Attach a redaction callback and register all currently stored secrets immediately."""
        self._on_secret_loaded = callback
        for val in self.get_all_secret_values():
            callback(val)

    def _notify_secret(self, value: str) -> None:
        if value and self._on_secret_loaded is not None:
            self._on_secret_loaded(value)

    def _validate_target(self, provider: str, env_var: str) -> tuple[str, str]:
        norm_provider = provider.strip().lower()
        norm_env = env_var.strip()
        allowed = ALLOWED_PROVIDER_ENV_VARS.get(norm_provider)
        if allowed is None or norm_env not in allowed:
            raise DesktopUiError(
                code="INVALID_CREDENTIAL_REQUEST",
                message=(
                    f"Unsupported provider/env_var combination: '{provider}' / '{env_var}'."
                ),
            )
        return norm_provider, norm_env

    def set_secret(self, provider: str, env_var: str, value: str) -> None:
        norm_provider, norm_env = self._validate_target(provider, env_var)
        cleaned_val = value.strip()
        if not cleaned_val or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned_val):
            raise DesktopUiError(
                code="INVALID_CREDENTIAL_REQUEST",
                message="Credential value cannot be empty or contain control characters.",
            )

        self._notify_secret(cleaned_val)

        if self._backend == "libsecret" and self._secret_tool_bin:
            rc, _ = self._runner(
                [
                    self._secret_tool_bin,
                    "store",
                    f"--label=Nova API Credential ({norm_provider})",
                    "service",
                    _SECRET_TOOL_SERVICE,
                    "provider",
                    norm_provider,
                    "env_var",
                    norm_env,
                ],
                stdin_text=cleaned_val,
            )
            if rc == 0:
                idx = _read_index_mapping(self._index_path)
                existing = set(idx.get(norm_provider, []))
                existing.add(norm_env)
                idx[norm_provider] = sorted(existing)
                _atomic_write_json_0600(self._index_path, idx)
                return
            # Fall back to 0600 local store if secret-tool/D-Bus keyring is locked or unavailable
            self._backend = "local_file_0600"

        data = _read_json_mapping(self._fallback_path)
        prov_map = dict(data.get(norm_provider, {}))
        prov_map[norm_env] = cleaned_val
        data[norm_provider] = prov_map
        _atomic_write_json_0600(self._fallback_path, data)

    def get_secret(self, provider: str, env_var: str) -> str | None:
        norm_provider, norm_env = self._validate_target(provider, env_var)
        if self._backend == "libsecret" and self._secret_tool_bin:
            rc, stdout_val = self._runner(
                [
                    self._secret_tool_bin,
                    "lookup",
                    "service",
                    _SECRET_TOOL_SERVICE,
                    "provider",
                    norm_provider,
                    "env_var",
                    norm_env,
                ],
            )
            if rc == 0 and stdout_val:
                self._notify_secret(stdout_val)
                return stdout_val

        data = _read_json_mapping(self._fallback_path)
        val = data.get(norm_provider, {}).get(norm_env)
        if val:
            self._notify_secret(val)
            return val
        return None

    def delete_secret(self, provider: str, env_var: str) -> bool:
        norm_provider, norm_env = self._validate_target(provider, env_var)
        removed = False

        if self._backend == "libsecret" and self._secret_tool_bin:
            existing = self.get_secret(norm_provider, norm_env)
            if existing is not None:
                rc, _ = self._runner(
                    [
                        self._secret_tool_bin,
                        "clear",
                        "service",
                        _SECRET_TOOL_SERVICE,
                        "provider",
                        norm_provider,
                        "env_var",
                        norm_env,
                    ],
                )
                if rc == 0:
                    removed = True
            idx = _read_index_mapping(self._index_path)
            if norm_provider in idx and norm_env in idx[norm_provider]:
                idx[norm_provider] = [k for k in idx[norm_provider] if k != norm_env]
                if not idx[norm_provider]:
                    idx.pop(norm_provider, None)
                _atomic_write_json_0600(self._index_path, idx)

        data = _read_json_mapping(self._fallback_path)
        if norm_provider in data and norm_env in data[norm_provider]:
            data[norm_provider].pop(norm_env, None)
            if not data[norm_provider]:
                data.pop(norm_provider, None)
            _atomic_write_json_0600(self._fallback_path, data)
            removed = True

        return removed

    def list_configured_env_vars(
        self,
        provider: str | None = None,
    ) -> dict[str, tuple[str, ...]]:
        combined: dict[str, set[str]] = {}
        if self._backend == "libsecret":
            for prov, keys in _read_index_mapping(self._index_path).items():
                combined.setdefault(prov, set()).update(keys)
        for prov, entries in _read_json_mapping(self._fallback_path).items():
            combined.setdefault(prov, set()).update(entries.keys())

        if provider is not None:
            norm = provider.strip().lower()
            keys_set = combined.get(norm, set())
            return {norm: tuple(sorted(keys_set))} if keys_set else {}
        return {prov: tuple(sorted(keys_set)) for prov, keys_set in sorted(combined.items())}

    def get_all_secret_values(self) -> tuple[str, ...]:
        secrets: list[str] = []
        seen: set[str] = set()
        for prov, env_vars in self.list_configured_env_vars().items():
            for env_var in env_vars:
                if prov in ALLOWED_PROVIDER_ENV_VARS and env_var in ALLOWED_PROVIDER_ENV_VARS[prov]:
                    val = self.get_secret(prov, env_var)
                    if val and val not in seen:
                        seen.add(val)
                        secrets.append(val)
        return tuple(secrets)

    def build_provider_env(self) -> dict[str, str]:
        """Build an env dict of all configured provider API keys for child CLI processes."""
        env_overrides: dict[str, str] = {}
        for prov, env_vars in self.list_configured_env_vars().items():
            for env_var in env_vars:
                if prov in ALLOWED_PROVIDER_ENV_VARS and env_var in ALLOWED_PROVIDER_ENV_VARS[prov]:
                    val = self.get_secret(prov, env_var)
                    if val:
                        env_overrides[env_var] = val
        return env_overrides

    def provider_statuses(self) -> tuple[ProviderCredentialStatus, ...]:
        """Return write-only configuration status for all supported providers."""
        configured = self.list_configured_env_vars()
        statuses: list[ProviderCredentialStatus] = []
        for prov, env_vars in ALLOWED_PROVIDER_ENV_VARS.items():
            configured_set = set(configured.get(prov, ()))
            for env_var in env_vars:
                statuses.append(
                    ProviderCredentialStatus(
                        provider=prov,
                        env_var=env_var,
                        configured=(env_var in configured_set or bool(os.environ.get(env_var))),
                    )
                )
        return tuple(statuses)
