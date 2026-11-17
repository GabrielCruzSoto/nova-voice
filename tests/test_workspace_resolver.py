"""Deterministic tests for T1: domain events, event bus, and LocalWorkspaceResolver (AC-1, AC-5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nova.adapters.workspace_resolver import LocalWorkspaceResolver
from nova.domain.errors import WorkspaceResolutionError
from nova.domain.events import (
    ChatStreamEvent,
    OrchestratorEventBus,
    VoiceMilestoneEvent,
    WorkspaceChangedEvent,
    WorkspaceClearedEvent,
)
from nova.domain.models import InitializeWorkspaceRequest, ResolveWorkspaceRequest


async def test_ac1_resolve_exact_path_within_allowed_roots_and_emit_event(tmp_path: Path) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "mi-api-pagos"
    repo.mkdir(parents=True)

    bus = OrchestratorEventBus()
    received: list[WorkspaceChangedEvent] = []

    async def on_event(event: object) -> None:
        if isinstance(event, WorkspaceChangedEvent):
            received.append(event)

    bus.subscribe(on_event)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus)

    ctx = await resolver.resolve(
        ResolveWorkspaceRequest(
            correlation_id="corr-1",
            query=str(repo),
            mode="exact_path",
        )
    )

    assert ctx.name == "mi-api-pagos"
    assert ctx.canonical_path == str(repo.resolve())
    assert len(received) == 1
    assert received[0].workspace_name == "mi-api-pagos"

    # NFR-2 idempotency: repeat selection preserves active_since and skips duplicate event
    ctx_repeat = await resolver.resolve(
        ResolveWorkspaceRequest(
            correlation_id="corr-2",
            query=str(repo),
            mode="exact_path",
        )
    )
    assert ctx_repeat.canonical_path == ctx.canonical_path
    assert ctx_repeat.active_since == ctx.active_since
    assert len(received) == 1


async def test_ac1_resolve_voice_fuzzy_query_normalizes_accents_and_separators(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    repo = root / "mi-api-pagos"
    repo.mkdir(parents=True)
    (root / "otro-servicio").mkdir(parents=True)

    resolver = LocalWorkspaceResolver(allowed_roots=[root])
    ctx = await resolver.resolve(
        ResolveWorkspaceRequest(
            correlation_id="corr-fuzzy",
            query="Mí API Pagos",
            mode="voice_fuzzy",
        )
    )

    assert ctx.name == "mi-api-pagos"
    assert ctx.canonical_path == str(repo.resolve())


@pytest.mark.parametrize(
    ("query", "mode", "expected_code"),
    [
        ("   ", "voice_fuzzy", "WORKSPACE_INVALID_QUERY"),
        ("mi-api", "invalid_mode", "WORKSPACE_INVALID_QUERY"),
        ("inexistente-xyz", "voice_fuzzy", "WORKSPACE_NOT_FOUND"),
    ],
)
async def test_ac5_rejects_empty_or_unknown_queries(
    tmp_path: Path,
    query: str,
    mode: str,
    expected_code: str,
) -> None:
    root = tmp_path / "Proyectos"
    root.mkdir(parents=True)
    resolver = LocalWorkspaceResolver(allowed_roots=[root])

    with pytest.raises(WorkspaceResolutionError) as exc_info:
        await resolver.resolve(
            ResolveWorkspaceRequest(
                correlation_id="corr-neg",
                query=query,
                mode=mode,
            )
        )
    assert exc_info.value.code == expected_code


async def test_ac5_rejects_outside_allowed_roots_and_symlink_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "Proyectos"
    allowed.mkdir()
    outside = tmp_path / "SecretoExterno"
    outside.mkdir()
    escape_link = allowed / "link-trampa"
    escape_link.symlink_to(outside, target_is_directory=True)

    resolver = LocalWorkspaceResolver(allowed_roots=[allowed])

    for target in (outside, escape_link):
        with pytest.raises(WorkspaceResolutionError) as exc_info:
            await resolver.resolve(
                ResolveWorkspaceRequest(
                    correlation_id="corr-sec",
                    query=str(target),
                    mode="exact_path",
                )
            )
        assert exc_info.value.code == "WORKSPACE_OUTSIDE_ALLOWED_ROOTS"


async def test_ac5_detects_ambiguous_voice_match_with_candidates(tmp_path: Path) -> None:
    root = tmp_path / "Proyectos"
    (root / "nova-core").mkdir(parents=True)
    (root / "nova-mobile").mkdir(parents=True)

    resolver = LocalWorkspaceResolver(allowed_roots=[root])

    with pytest.raises(WorkspaceResolutionError) as exc_info:
        await resolver.resolve(
            ResolveWorkspaceRequest(
                correlation_id="corr-amb",
                query="nova",
                mode="voice_fuzzy",
            )
        )
    assert exc_info.value.code == "WORKSPACE_AMBIGUOUS_MATCH"
    assert sorted(exc_info.value.candidates) == ["nova-core", "nova-mobile"]


async def test_cache_uses_canonical_key_finite_ttl_with_jitter_and_revalidates_on_miss(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    (root / "alpha-service").mkdir(parents=True)

    clock_now = 1000.0
    resolver = LocalWorkspaceResolver(
        allowed_roots=[root],
        ttl_seconds=30.0,
        jitter_ratio=0.10,
        clock=lambda: clock_now,
    )

    await resolver.resolve(
        ResolveWorkspaceRequest(
            correlation_id="corr-c1",
            query="alpha service",
            mode="voice_fuzzy",
        )
    )
    keys = resolver.cache_keys()
    assert len(keys) == 1
    assert keys[0].startswith("nova:workspace:resolver:catalog:")
    ttl_remaining = resolver.cache_ttl_remaining(keys[0])
    assert 27.0 <= ttl_remaining <= 33.0

    # Newly created repo triggers cache invalidation and immediate rescan on miss
    (root / "beta-service").mkdir()
    ctx_beta = await resolver.resolve(
        ResolveWorkspaceRequest(
            correlation_id="corr-c2",
            query="beta service",
            mode="voice_fuzzy",
        )
    )
    assert ctx_beta.name == "beta-service"


def test_op4_voice_milestone_enforces_25_word_budget_and_pydantic_validation() -> None:
    valid = VoiceMilestoneEvent(
        correlation_id="corr-v1",
        summary_text="Iniciando el analisis de modulos y pruebas del proyecto seleccionado.",
        priority="normal",
    )
    assert len(valid.summary_text.split()) <= 25

    too_long = " ".join(f"palabra{i}" for i in range(26))
    with pytest.raises(ValidationError):
        VoiceMilestoneEvent(
            correlation_id="corr-v2",
            summary_text=too_long,
            priority="normal",
        )

    chat_ev = ChatStreamEvent(
        correlation_id="corr-v1",
        delta="Detailed streaming output...",
        source="tier1_pi",
        step_type="agent_response",
    )
    assert chat_ev.source == "tier1_pi"


async def test_ac9_list_available_workspaces_excludes_technical_artifact_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Proyectos"
    real_repo = root / "nova-platform"
    nested_group = root / "org-services"
    nested_repo = nested_group / "billing-api"
    real_repo.mkdir(parents=True)
    nested_repo.mkdir(parents=True)

    # Create technical/artifact folders both at root depth=1 and nested depth=2
    ignored_names = (
        "__pycache__",
        "node_modules",
        "venv",
        "env",
        "dist",
        "build",
        "target",
        "coverage",
        "htmlcov",
        ".venv",
    )
    for name in ignored_names:
        (root / name).mkdir(parents=True, exist_ok=True)
        (nested_group / name).mkdir(parents=True, exist_ok=True)
    (real_repo / ".git").mkdir(parents=True, exist_ok=True)

    resolver = LocalWorkspaceResolver(allowed_roots=[root])
    available = await resolver.list_available_workspaces()
    listed_names = [display_name for display_name, _ in available]

    assert "nova-platform" in listed_names
    assert "org-services" in listed_names
    assert "billing-api" in listed_names
    for ignored in ignored_names:
        assert ignored not in listed_names, f"Technical folder {ignored!r} must be excluded"

    # Explicit exact_path selection within allowed_roots remains permitted
    explicit_dist = root / "dist"
    ctx = await resolver.resolve(
        ResolveWorkspaceRequest(
            correlation_id="corr-exact-dist",
            query=str(explicit_dist),
            mode="exact_path",
        )
    )
    assert ctx.canonical_path == str(explicit_dist.resolve())


async def test_nova013_ac2_rejects_home_directory_across_all_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "operador_home"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    resolver = LocalWorkspaceResolver(allowed_roots=[tmp_path, fake_home], home_dir=fake_home)

    for query, mode in (
        ("~", "exact_path"),
        (str(fake_home), "exact_path"),
        ("~", "ui_picker"),
        (str(fake_home), "ui_picker"),
        ("operador home", "voice_fuzzy"),
    ):
        with pytest.raises(WorkspaceResolutionError) as exc_info:
            await resolver.resolve(
                ResolveWorkspaceRequest(
                    correlation_id="corr-home",
                    query=query,
                    mode=mode,
                )
            )
        assert exc_info.value.code == "WORKSPACE_HOME_FORBIDDEN"
    assert resolver.active_context is None


async def test_nova013_ac4_ac5_resolves_aigile_and_toml_workspaces_and_isolates_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Workspaces"
    alfa = root / "cliente-alfa-workspace"
    (alfa / "repositories" / "api-pagos").mkdir(parents=True)
    (alfa / "repositories" / "portal-web").mkdir(parents=True)
    (alfa / "aff.yaml").write_text(
        'schema_version: "1.0"\n'
        "project:\n"
        '  id: "cliente-alfa"\n'
        '  name: "Cliente Alfa"\n'
        '  namespace: "alfa"\n'
        "workspace:\n"
        '  topology: "worktrees"\n'
        'repositories: ["api-pagos", "portal-web"]\n',
        encoding="utf-8",
    )

    beta = root / "cliente-beta-workspace"
    (beta / "repositories" / "backend-core").mkdir(parents=True)
    (beta / "repositories" / "app-admin").mkdir(parents=True)
    (beta / "nova-workspace.toml").write_text(
        'schema_version = "1.0"\n\n'
        "[workspace]\n"
        'id = "cliente-beta"\n'
        'name = "Cliente Beta"\n'
        'topology = "multi-project"\n'
        'projects = ["repositories/backend-core", "repositories/app-admin"]\n',
        encoding="utf-8",
    )

    resolver = LocalWorkspaceResolver(allowed_roots=[root])
    entries = await resolver.list_workspace_entries()
    entry_names = [e.name for e in entries]
    assert entry_names == ["Cliente Alfa", "Cliente Beta"]
    assert all("repositories" not in e.name.lower() for e in entries)

    ctx_alfa = await resolver.resolve(
        ResolveWorkspaceRequest(
            correlation_id="corr-alfa",
            query="Cliente Alfa",
            mode="voice_fuzzy",
        )
    )
    assert ctx_alfa.name == "Cliente Alfa"
    assert ctx_alfa.topology == "multi-project"
    assert [p.name for p in ctx_alfa.projects] == ["api-pagos", "portal-web"]
    assert all(p.canonical_path.startswith(str(alfa.resolve())) for p in ctx_alfa.projects)

    # Cross-client symlink inside beta pointing to alfa must fail-closed
    cross_link = beta / "repositories" / "fuga-alfa"
    cross_link.symlink_to(alfa / "repositories" / "api-pagos", target_is_directory=True)
    (beta / "nova-workspace.toml").write_text(
        'schema_version = "1.0"\n\n'
        "[workspace]\n"
        'id = "cliente-beta"\n'
        'name = "Cliente Beta"\n'
        'topology = "multi-project"\n'
        'projects = ["repositories/backend-core", "repositories/fuga-alfa"]\n',
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceResolutionError) as exc_escape:
        await resolver.resolve(
            ResolveWorkspaceRequest(
                correlation_id="corr-escape",
                query=str(beta),
                mode="exact_path",
            )
        )
    assert exc_escape.value.code == "WORKSPACE_OUTSIDE_ALLOWED_ROOTS"


async def test_nova013_ac4_ac9_initialize_workspace_and_clear_active_workspace(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "home_op"
    fake_home.mkdir()
    root = tmp_path / "Proyectos"
    root.mkdir()

    bus = OrchestratorEventBus()
    cleared_events: list[WorkspaceClearedEvent] = []

    async def on_ev(ev: object) -> None:
        if isinstance(ev, WorkspaceClearedEvent):
            cleared_events.append(ev)

    bus.subscribe(on_ev)
    resolver = LocalWorkspaceResolver(allowed_roots=[root], event_bus=bus, home_dir=fake_home)

    created = await resolver.initialize_workspace(
        InitializeWorkspaceRequest(
            correlation_id="corr-init",
            parent_root=str(root),
            workspace_slug="cliente-gamma-workspace",
            display_name="Cliente Gamma",
            projects=("api-core", "web-portal"),
            activate=True,
        )
    )
    assert created.name == "Cliente Gamma"
    assert created.topology == "multi-project"
    assert [p.name for p in created.projects] == ["api-core", "web-portal"]
    assert (root / "cliente-gamma-workspace" / "nova-workspace.toml").is_file()
    assert resolver.active_context is not None

    with pytest.raises(WorkspaceResolutionError) as dup_exc:
        await resolver.initialize_workspace(
            InitializeWorkspaceRequest(
                correlation_id="corr-dup",
                parent_root=str(root),
                workspace_slug="cliente-gamma-workspace",
                display_name="Cliente Gamma",
                projects=("api-core",),
            )
        )
    assert dup_exc.value.code == "WORKSPACE_ALREADY_EXISTS"

    await resolver.clear_active_workspace("corr-clear-1")
    assert resolver.active_context is None
    assert len(cleared_events) == 1
    # Idempotent repeat clear does not emit duplicate event
    await resolver.clear_active_workspace("corr-clear-2")
    assert len(cleared_events) == 1


async def test_nova013_secret_keys_in_manifest_raise_invalid_manifest_and_keep_catalog(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "home_op"
    fake_home.mkdir()
    root = tmp_path / "Proyectos"
    root.mkdir()

    valid_ws = root / "cliente-sano"
    (valid_ws / "repositories" / "core").mkdir(parents=True)
    (valid_ws / "nova-workspace.toml").write_text(
        'schema_version = "1.0"\n\n'
        "[workspace]\n"
        'id = "cliente-sano"\n'
        'name = "Cliente Sano"\n'
        'projects = ["repositories/core"]\n',
        encoding="utf-8",
    )

    poisoned_toml = root / "cliente-secreto-toml"
    poisoned_toml.mkdir()
    (poisoned_toml / "nova-workspace.toml").write_text(
        'schema_version = "1.0"\n'
        'openai_api_key = "forbidden"\n\n'
        "[workspace]\n"
        'id = "bad"\n'
        'name = "Bad"\n',
        encoding="utf-8",
    )

    poisoned_aff = root / "cliente-secreto-aff"
    poisoned_aff.mkdir()
    (poisoned_aff / "aff.yaml").write_text(
        'schema_version: "1.0"\nauth_token: "forbidden"\nproject:\n  id: "bad-aff"\n',
        encoding="utf-8",
    )

    resolver = LocalWorkspaceResolver(allowed_roots=[root], home_dir=fake_home)

    for bad_dir in (poisoned_toml, poisoned_aff):
        with pytest.raises(WorkspaceResolutionError) as exc_info:
            await resolver.resolve(
                ResolveWorkspaceRequest(
                    correlation_id="corr-sec",
                    query=str(bad_dir),
                    mode="exact_path",
                )
            )
        assert exc_info.value.code == "WORKSPACE_INVALID_MANIFEST"

    entries = await resolver.list_workspace_entries()
    assert [e.name for e in entries] == ["Cliente Sano"]


