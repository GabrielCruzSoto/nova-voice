"""TDD tests for NOVA-017 T3: AgentAdapterRegistry (auto-detection, Dual-Engine, Single-Engine)."""

from __future__ import annotations

from pathlib import Path

from nova.adapters.agent_registry import (
    AgentAdapterRegistry,
    detect_installed_engines,
    resolve_tier_engines,
)
from nova.adapters.credential_store import SystemCredentialStore
from nova.domain.models import AgentEngineId, ModelsSettingsSection


def test_detect_and_resolve_single_engine_when_only_claude_installed() -> None:
    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/claude" if name == "claude" else None

    detected = detect_installed_engines(which_resolver=fake_which, include_fallback_paths=False)
    assert detected["claude"] == "/usr/local/bin/claude"
    assert detected["pi"] is None
    assert detected["opencode"] is None
    assert detected["agy"] is None

    t1, t2 = resolve_tier_engines(ModelsSettingsSection(), detected)
    assert t1 == "claude"
    assert t2 == "claude"


def test_resolve_dual_engine_and_explicit_single_engine_mode() -> None:
    detected: dict[AgentEngineId, str | None] = {
        "pi": "/usr/bin/pi",
        "opencode": "/usr/bin/opencode",
        "claude": "/usr/bin/claude",
        "agy": "/usr/bin/agy",
    }

    dual_cfg = ModelsSettingsSection(
        tier1_engine="opencode",
        tier1_model="anthropic/claude-haiku-4-5",
        tier2_engine="claude",
        tier2_model="opus",
        single_engine_mode=False,
    )
    t1, t2 = resolve_tier_engines(dual_cfg, detected)
    assert t1 == "opencode"
    assert t2 == "claude"

    single_cfg = dual_cfg.model_copy(update={"single_engine_mode": True})
    st1, st2 = resolve_tier_engines(single_cfg, detected)
    assert st1 == "opencode"
    assert st2 == "opencode"


def test_registry_builds_catalog_without_leaking_secrets(tmp_path: Path) -> None:
    cred_store = SystemCredentialStore(
        fallback_path=tmp_path / "credentials.json",
        prefer_libsecret=False,
    )
    secret_val = "sk-ant-never-leak-to-catalog-12345"
    cred_store.set_secret("anthropic", "ANTHROPIC_API_KEY", secret_val)

    registry = AgentAdapterRegistry(
        which_resolver=lambda name: f"/usr/bin/{name}" if name in ("opencode", "claude") else None,
        credential_store=cred_store,
        include_fallback_paths=False,
    )
    catalog = registry.build_catalog(ModelsSettingsSection())
    dumped = catalog.model_dump_json()
    assert secret_val not in dumped
    assert catalog.active_tier1 is not None and catalog.active_tier1.engine == "opencode"
    assert catalog.active_tier2 is not None and catalog.active_tier2.engine == "claude"
    assert len(catalog.engines) == 4
