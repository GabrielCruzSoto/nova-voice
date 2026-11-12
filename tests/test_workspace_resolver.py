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
)
from nova.domain.models import ResolveWorkspaceRequest


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
