"""Multi-tier CLI agent auto-detection, bidirectional wrappers, and factory registry (NOVA-017)."""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Literal

from nova.adapters.antigravity_adapter import (
    ALLOWED_TIER2_MODELS,
    AntigravityCLIAdapter,
    AntigravityRunnerConfig,
)
from nova.adapters.claude_adapter import ClaudeCodeAdapter, ClaudeCodeConfig
from nova.adapters.credential_store import ALLOWED_PROVIDER_ENV_VARS, SystemCredentialStore
from nova.adapters.opencode_adapter import OpenCodeAdapter, OpenCodeConfig
from nova.adapters.pi_rpc_client import PiRpcClient, PiRpcConfig
from nova.domain.models import (
    DEFAULT_PI_MODEL,
    ActiveTierSelection,
    AgentAuthMode,
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentEngineId,
    CodingTaskRequest,
    ModelsSettingsSection,
    ProviderCredentialStatus,
    RunnerProgressEvent,
)
from nova.domain.ports import CodingRunnerPort, Tier1ConversationPort

WhichResolver = Callable[[str], str | None]

TIER1_AUTO_PRIORITY: tuple[AgentEngineId, ...] = ("pi", "opencode", "claude", "agy")
TIER2_AUTO_PRIORITY: tuple[AgentEngineId, ...] = ("agy", "claude", "opencode", "pi")

ENGINE_DISPLAY_NAMES: dict[AgentEngineId, str] = {
    "claude": "Claude Code",
    "opencode": "OpenCode",
    "agy": "Antigravity",
    "pi": "Pi CLI",
}

ENGINE_DEFAULT_MODELS: dict[AgentEngineId, tuple[str, str]] = {
    "claude": ("haiku", "opus"),
    "opencode": ("anthropic/claude-haiku-4-5", "anthropic/claude-sonnet-4-5"),
    "agy": ("gemini-4-argon-high", "claude-opus-5-5-high"),
    "pi": (DEFAULT_PI_MODEL, DEFAULT_PI_MODEL),
}

_ENGINE_FALLBACK_PATHS: dict[AgentEngineId, tuple[Path, ...]] = {
    "claude": (
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".claude" / "local" / "claude",
    ),
    "opencode": (
        Path.home() / ".opencode" / "bin" / "opencode",
        Path.home() / ".local" / "bin" / "opencode",
    ),
    "agy": (Path.home() / ".local" / "bin" / "agy",),
    "pi": (
        Path.home() / ".local" / "bin" / "pi",
        Path.home() / ".pi" / "agent" / "bin" / "pi",
    ),
}

_ENGINE_NATIVE_AUTH_HINTS: dict[AgentEngineId, tuple[Path, ...]] = {
    "claude": (Path.home() / ".claude" / ".credentials.json", Path.home() / ".claude.json"),
    "opencode": (
        Path.home() / ".local" / "share" / "opencode" / "auth.json",
        Path.home() / ".config" / "opencode" / "opencode.json",
    ),
    "agy": (Path.home() / ".gemini" / "antigravity", Path.home() / ".config" / "antigravity"),
    "pi": (Path.home() / ".pi" / "agent" / "auth.json",),
}


def _resolve_binary_path(
    executable: str,
    *,
    which_resolver: WhichResolver,
    fallback_paths: tuple[Path, ...] = (),
) -> str | None:
    candidate = Path(executable).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.resolve())
    found = which_resolver(executable)
    if found:
        return found
    for fb in fallback_paths:
        expanded = fb.expanduser()
        if expanded.is_file() and os.access(expanded, os.X_OK):
            return str(expanded.resolve())
    return None


def detect_installed_engines(
    *,
    which_resolver: WhichResolver = shutil.which,
    executable_overrides: Mapping[AgentEngineId, str] | None = None,
    include_fallback_paths: bool = True,
) -> dict[AgentEngineId, str | None]:
    """Auto-detect installed CLI agent binaries for claude, opencode, agy, and pi."""
    result: dict[AgentEngineId, str | None] = {}
    overrides = executable_overrides or {}
    for engine in ("claude", "opencode", "agy", "pi"):
        eng: AgentEngineId = engine
        exe_name = overrides.get(eng, eng)
        fallbacks = _ENGINE_FALLBACK_PATHS[eng] if include_fallback_paths else ()
        result[eng] = _resolve_binary_path(
            exe_name,
            which_resolver=which_resolver,
            fallback_paths=fallbacks,
        )
    return result


def resolve_tier_engines(
    models: ModelsSettingsSection,
    installed: Mapping[AgentEngineId, str | None],
) -> tuple[AgentEngineId | None, AgentEngineId | None]:
    """Resolve effective (tier1_engine, tier2_engine) supporting Dual-Engine and Single-Engine."""
    available = {eng for eng, path in installed.items() if path}

    tier1: AgentEngineId | None
    if models.tier1_engine != "auto":
        tier1 = models.tier1_engine
    else:
        tier1 = next((eng for eng in TIER1_AUTO_PRIORITY if eng in available), None)

    tier2: AgentEngineId | None
    if models.single_engine_mode:
        tier2 = tier1
    elif models.tier2_engine != "auto":
        tier2 = models.tier2_engine
    else:
        tier2 = next((eng for eng in TIER2_AUTO_PRIORITY if eng in available), None)
        if tier2 is None:
            tier2 = tier1

    return tier1, tier2


class AntigravityTier1Adapter:
    """Thin Tier1ConversationPort wrapper over AntigravityCLIAdapter for Single-Engine mode."""

    def __init__(
        self,
        runner: AntigravityCLIAdapter,
        *,
        model: str = "gemini-4-argon-high",
        workspace_path: str | None = None,
    ) -> None:
        self._runner = runner
        self._model = model
        self._workspace_path = workspace_path or os.getcwd()
        self._active_session_id: str | None = None

    @property
    def active_session_id(self) -> str | None:
        return self._active_session_id

    async def switch_workspace(
        self,
        new_workspace_path: str,
        *,
        workspace_active: bool = True,
        allowed_project_paths: tuple[str, ...] = (),
        session_id: str | None = None,
    ) -> None:
        _ = (workspace_active, allowed_project_paths)
        self._workspace_path = new_workspace_path
        self._active_session_id = session_id

    async def barge_in_abort(self) -> None:
        return None

    async def stream_prompt(self, message: str) -> AsyncIterator[str]:
        req = CodingTaskRequest(
            correlation_id="corr-tier1-agy",
            workspace_path=self._workspace_path,
            prompt=message,
            model=self._model,
            conversation_id=self._active_session_id,
        )
        async for ev in self._runner.execute(req):
            if ev.conversation_id and ev.conversation_id != "unknown":
                self._active_session_id = ev.conversation_id
            if ev.kind == "delta" and ev.text_delta:
                yield ev.text_delta


class PiCodingRunnerAdapter:
    """Thin CodingRunnerPort wrapper over PiRpcClient for Single-Engine mode."""

    def __init__(self, pi_client: PiRpcClient) -> None:
        self._pi_client = pi_client

    async def execute(self, request: CodingTaskRequest) -> AsyncIterator[RunnerProgressEvent]:
        await self._pi_client.switch_workspace(
            request.workspace_path,
            workspace_active=True,
            session_id=request.conversation_id,
        )
        conv_id = request.conversation_id or "pi-tier2"
        yield RunnerProgressEvent(kind="init", conversation_id=conv_id, step_type="init")
        parts: list[str] = []
        async for delta in self._pi_client.stream_prompt(request.prompt):
            parts.append(delta)
            yield RunnerProgressEvent(
                kind="delta",
                conversation_id=conv_id,
                step_type="agent_response",
                text_delta=delta,
            )
        final_sid = (await self._pi_client.get_session_id()) or conv_id
        yield RunnerProgressEvent(
            kind="completed",
            conversation_id=final_sid,
            step_type="result",
            final_response="".join(parts),
        )


class AgentAdapterRegistry:
    """Central factory and catalog builder for Nova's multi-tier CLI agent ecosystem."""

    def __init__(
        self,
        *,
        which_resolver: WhichResolver = shutil.which,
        credential_store: SystemCredentialStore | None = None,
        executable_overrides: Mapping[AgentEngineId, str] | None = None,
        include_fallback_paths: bool = True,
    ) -> None:
        self._which = which_resolver
        self._credential_store = credential_store
        self._executable_overrides = dict(executable_overrides or {})
        self._include_fallback_paths = include_fallback_paths

    def detect_engines(self) -> dict[AgentEngineId, str | None]:
        return detect_installed_engines(
            which_resolver=self._which,
            executable_overrides=self._executable_overrides,
            include_fallback_paths=self._include_fallback_paths,
        )

    def _detect_auth_mode(
        self,
        engine: AgentEngineId,
        *,
        installed: bool,
        configured_env_vars: tuple[str, ...],
    ) -> AgentAuthMode:
        if configured_env_vars:
            return "api_token"
        if installed:
            for hint_path in _ENGINE_NATIVE_AUTH_HINTS[engine]:
                if hint_path.exists():
                    return "native_cli"
            return "native_cli"
        return "unconfigured"

    def build_catalog(self, models: ModelsSettingsSection) -> AgentCatalogSnapshot:
        """Build UI-safe catalog of all 4 CLI engines and active Tier 1 / Tier 2 routing."""
        installed_map = self.detect_engines()
        t1_engine, t2_engine = resolve_tier_engines(models, installed_map)

        configured_by_provider = (
            self._credential_store.list_configured_env_vars()
            if self._credential_store is not None
            else {}
        )
        all_configured_vars = tuple(
            sorted({var for vars_tuple in configured_by_provider.values() for var in vars_tuple})
        )

        entries: list[AgentCatalogEntry] = []
        for eng in ("claude", "opencode", "agy", "pi"):
            engine_id: AgentEngineId = eng
            bin_path = installed_map[engine_id]
            is_installed = bin_path is not None
            default_t1, default_t2 = ENGINE_DEFAULT_MODELS[engine_id]
            auth_mode = self._detect_auth_mode(
                engine_id,
                installed=is_installed,
                configured_env_vars=all_configured_vars,
            )
            entries.append(
                AgentCatalogEntry(
                    engine=engine_id,
                    display_name=ENGINE_DISPLAY_NAMES[engine_id],
                    installed=is_installed,
                    binary_path=bin_path,
                    auth_mode=auth_mode,
                    configured_env_vars=all_configured_vars,
                    supports_tier1=True,
                    supports_tier2=True,
                    default_tier1_model=default_t1,
                    default_tier2_model=default_t2,
                )
            )

        providers: tuple[ProviderCredentialStatus, ...]
        backend: Literal["libsecret", "local_file_0600", "none"]
        if self._credential_store is not None:
            providers = self._credential_store.provider_statuses()
            backend = self._credential_store.backend_name
        else:
            providers = tuple(
                ProviderCredentialStatus(
                    provider=prov,
                    env_var=env_var,
                    configured=bool(os.environ.get(env_var)),
                )
                for prov, env_vars in ALLOWED_PROVIDER_ENV_VARS.items()
                for env_var in env_vars
            )
            backend = "none"

        active_t1 = (
            ActiveTierSelection(
                engine=t1_engine,
                provider=models.tier1_provider,
                model=models.tier1_model,
            )
            if t1_engine is not None
            else None
        )
        active_t2 = (
            ActiveTierSelection(
                engine=t2_engine,
                provider=models.tier2_provider,
                model=models.tier2_model,
            )
            if t2_engine is not None
            else None
        )
        return AgentCatalogSnapshot(
            credential_backend=backend,
            active_tier1=active_t1,
            active_tier2=active_t2,
            single_engine_mode=models.single_engine_mode,
            engines=tuple(entries),
            providers=providers,
        )

    def build_tier1_adapter(
        self,
        engine: AgentEngineId,
        *,
        models: ModelsSettingsSection,
        workspace_path: str,
        workspace_active: bool = False,
        gatekeeper_ipc_env: dict[str, str] | None = None,
    ) -> Tier1ConversationPort:
        installed_map = self.detect_engines()
        exe = installed_map.get(engine) or self._executable_overrides.get(engine, engine)
        provider_env = (
            self._credential_store.build_provider_env()
            if self._credential_store is not None
            else {}
        )
        if engine == "claude":
            return ClaudeCodeAdapter(
                ClaudeCodeConfig(
                    executable=exe,
                    model=models.tier1_model or ENGINE_DEFAULT_MODELS["claude"][0],
                    workspace_path=workspace_path,
                    tier="tier1",
                    gatekeeper_ipc_env=gatekeeper_ipc_env,
                    provider_env=provider_env or None,
                )
            )
        if engine == "opencode":
            return OpenCodeAdapter(
                OpenCodeConfig(
                    executable=exe,
                    provider=models.tier1_provider,
                    model=models.tier1_model or ENGINE_DEFAULT_MODELS["opencode"][0],
                    workspace_path=workspace_path,
                    tier="tier1",
                    gatekeeper_ipc_env=gatekeeper_ipc_env,
                    provider_env=provider_env or None,
                )
            )
        if engine == "agy":
            model_name = models.tier1_model or ENGINE_DEFAULT_MODELS["agy"][0]
            agy = AntigravityCLIAdapter(
                AntigravityRunnerConfig(
                    executable=exe,
                    allowed_models=frozenset(set(ALLOWED_TIER2_MODELS) | {model_name}),
                    gatekeeper_ipc_env=gatekeeper_ipc_env,
                )
            )
            return AntigravityTier1Adapter(agy, model=model_name, workspace_path=workspace_path)

        # Default: Pi RPC
        merged_ipc = {**(gatekeeper_ipc_env or {}), **provider_env} or None
        return PiRpcClient(
            PiRpcConfig(
                executable=exe,
                provider=models.tier1_provider or models.pi_provider,
                model=models.tier1_model or models.pi_model,
                workspace_path=workspace_path,
                workspace_active=workspace_active,
                gatekeeper_ipc_env=merged_ipc,
            )
        )

    def build_tier2_adapter(
        self,
        engine: AgentEngineId,
        *,
        models: ModelsSettingsSection,
        workspace_path: str,
        gatekeeper_ipc_env: dict[str, str] | None = None,
    ) -> CodingRunnerPort:
        installed_map = self.detect_engines()
        exe = installed_map.get(engine) or self._executable_overrides.get(engine, engine)
        provider_env = (
            self._credential_store.build_provider_env()
            if self._credential_store is not None
            else {}
        )
        if engine == "claude":
            return ClaudeCodeAdapter(
                ClaudeCodeConfig(
                    executable=exe,
                    model=models.tier2_model or ENGINE_DEFAULT_MODELS["claude"][1],
                    workspace_path=workspace_path,
                    tier="tier2",
                    gatekeeper_ipc_env=gatekeeper_ipc_env,
                    provider_env=provider_env or None,
                )
            )
        if engine == "opencode":
            return OpenCodeAdapter(
                OpenCodeConfig(
                    executable=exe,
                    provider=models.tier2_provider,
                    model=models.tier2_model or ENGINE_DEFAULT_MODELS["opencode"][1],
                    workspace_path=workspace_path,
                    tier="tier2",
                    gatekeeper_ipc_env=gatekeeper_ipc_env,
                    provider_env=provider_env or None,
                )
            )
        if engine == "pi":
            merged_ipc = {**(gatekeeper_ipc_env or {}), **provider_env} or None
            pi_client = PiRpcClient(
                PiRpcConfig(
                    executable=exe,
                    provider=models.tier2_provider or models.pi_provider,
                    model=models.tier2_model or models.pi_model,
                    workspace_path=workspace_path,
                    workspace_active=True,
                    gatekeeper_ipc_env=merged_ipc,
                )
            )
            return PiCodingRunnerAdapter(pi_client)

        # Default: Antigravity CLI
        model_name = models.tier2_model or ENGINE_DEFAULT_MODELS["agy"][1]
        return AntigravityCLIAdapter(
            AntigravityRunnerConfig(
                executable=exe,
                allowed_models=frozenset(set(ALLOWED_TIER2_MODELS) | {model_name}),
                gatekeeper_ipc_env=gatekeeper_ipc_env,
            )
        )
