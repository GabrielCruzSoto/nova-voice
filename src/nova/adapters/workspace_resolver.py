"""Bounded, realpath-confined workspace resolver with finite TTL+jitter cache (OP-1)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from nova.domain.errors import WorkspaceResolutionError
from nova.domain.events import OrchestratorEventBus, WorkspaceChangedEvent
from nova.domain.models import ResolveWorkspaceRequest, WorkspaceContext

MAX_SCAN_ENTRIES = 500
MAX_SCAN_DEPTH = 2


def _normalize_slug(text: str) -> str:
    """Normalize accents, punctuation, and whitespace into a comparable slug."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    tokens = re.findall(r"[a-z0-9]+", lowered)
    return "-".join(tokens)


@dataclass(slots=True)
class _CatalogCacheEntry:
    catalog: dict[str, list[Path]]
    expires_at: float


class LocalWorkspaceResolver:
    """Resolves exact paths and spoken project names strictly inside allowed_roots."""

    def __init__(
        self,
        allowed_roots: Sequence[Path],
        event_bus: OrchestratorEventBus | None = None,
        ttl_seconds: float = 30.0,
        jitter_ratio: float = 0.10,
        timeout_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
        self._allowed_roots_raw = tuple(allowed_roots)
        self._event_bus = event_bus
        self._ttl_seconds = ttl_seconds
        self._jitter_ratio = jitter_ratio
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._rng = rng
        self._cache: dict[str, _CatalogCacheEntry] = {}
        self._active_context: WorkspaceContext | None = None

    @property
    def active_context(self) -> WorkspaceContext | None:
        """Return currently selected workspace context, if any."""
        return self._active_context

    def cache_keys(self) -> list[str]:
        """Expose active cache keys for deterministic diagnostics and contract tests."""
        return list(self._cache.keys())

    def cache_ttl_remaining(self, key: str) -> float:
        """Return remaining TTL in seconds for a cached catalog key."""
        entry = self._cache.get(key)
        if entry is None:
            return 0.0
        return max(0.0, entry.expires_at - self._clock())

    async def resolve(self, request: ResolveWorkspaceRequest) -> WorkspaceContext:
        """Resolve and activate a project directory under OP-1 rules without blocking the loop."""
        if request.mode not in ("exact_path", "voice_fuzzy"):
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_QUERY",
                message=f"Unsupported workspace resolution mode: '{request.mode}'.",
            )
        raw_query = request.query.strip()
        if not raw_query:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_QUERY",
                message="Workspace query cannot be empty.",
            )

        resolved_path = await asyncio.wait_for(
            asyncio.to_thread(self._resolve_sync, raw_query, request.mode),
            timeout=self._timeout_seconds,
        )

        canonical_str = str(resolved_path)
        if (
            self._active_context is not None
            and self._active_context.canonical_path == canonical_str
        ):
            return self._active_context

        context = WorkspaceContext(
            workspace_id=f"ws-{_normalize_slug(resolved_path.name) or 'root'}",
            name=resolved_path.name,
            canonical_path=canonical_str,
        )
        self._active_context = context

        if self._event_bus is not None:
            await self._event_bus.publish(
                WorkspaceChangedEvent(
                    correlation_id=request.correlation_id,
                    workspace_name=context.name,
                    canonical_path=context.canonical_path,
                )
            )
        return context

    def _canonical_allowed_roots(self) -> list[Path]:
        roots: list[Path] = []
        for root in self._allowed_roots_raw:
            expanded = Path(os.path.realpath(os.path.expanduser(str(root))))
            if expanded.is_dir():
                roots.append(expanded)
        return roots

    def _is_within_allowed_roots(self, candidate: Path, allowed_roots: Sequence[Path]) -> bool:
        return any(candidate.is_relative_to(root) and candidate != root for root in allowed_roots)

    def _resolve_sync(self, raw_query: str, mode: str) -> Path:
        allowed_roots = self._canonical_allowed_roots()

        if mode == "exact_path":
            expanded = Path(os.path.realpath(os.path.expanduser(raw_query)))
            if not expanded.exists() or not expanded.is_dir():
                # Check if path outside roots existed or wouldn't be allowed anyway
                parent_real = Path(os.path.realpath(os.path.expanduser(raw_query)))
                if not any(parent_real.is_relative_to(root) for root in allowed_roots):
                    raise WorkspaceResolutionError(
                        code="WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                        message="Requested path is outside allowed project roots.",
                    )
                raise WorkspaceResolutionError(
                    code="WORKSPACE_NOT_FOUND",
                    message=f"Directory not found: {raw_query}",
                )
            if not self._is_within_allowed_roots(expanded, allowed_roots):
                raise WorkspaceResolutionError(
                    code="WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                    message="Requested path resolves outside allowed project roots.",
                )
            return expanded

        query_slug = _normalize_slug(raw_query)
        if not query_slug:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_QUERY",
                message="Voice workspace query contains no searchable tokens.",
            )

        cache_key = self._catalog_cache_key(allowed_roots)
        catalog = self._get_or_scan_catalog(cache_key, allowed_roots)
        matches = self._match_catalog(query_slug, catalog)

        if not matches:
            # Invalidate cache once on miss to pick up newly created repositories
            self._cache.pop(cache_key, None)
            catalog = self._get_or_scan_catalog(cache_key, allowed_roots)
            matches = self._match_catalog(query_slug, catalog)

        if not matches:
            raise WorkspaceResolutionError(
                code="WORKSPACE_NOT_FOUND",
                message=f"No project matched query '{raw_query}'.",
            )
        if len(matches) > 1:
            candidate_names = tuple(sorted({path.name for path in matches}))
            raise WorkspaceResolutionError(
                code="WORKSPACE_AMBIGUOUS_MATCH",
                message=f"Multiple projects matched '{raw_query}'.",
                candidates=candidate_names,
            )
        return matches[0]

    def _catalog_cache_key(self, allowed_roots: Sequence[Path]) -> str:
        joined = "|".join(sorted(str(r) for r in allowed_roots))
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
        return f"nova:workspace:resolver:catalog:{digest}"

    def _get_or_scan_catalog(
        self,
        cache_key: str,
        allowed_roots: Sequence[Path],
    ) -> dict[str, list[Path]]:
        now = self._clock()
        cached = self._cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.catalog

        catalog: dict[str, list[Path]] = {}
        scanned = 0
        for root in allowed_roots:
            scanned = self._scan_bounded(root, allowed_roots, catalog, depth=1, scanned=scanned)
            if scanned >= MAX_SCAN_ENTRIES:
                break

        jitter_factor = 1.0 + self._rng(-self._jitter_ratio, self._jitter_ratio)
        ttl = self._ttl_seconds * jitter_factor
        self._cache[cache_key] = _CatalogCacheEntry(catalog=catalog, expires_at=now + ttl)
        return catalog

    def _scan_bounded(
        self,
        directory: Path,
        allowed_roots: Sequence[Path],
        catalog: dict[str, list[Path]],
        depth: int,
        scanned: int,
    ) -> int:
        if depth > MAX_SCAN_DEPTH or scanned >= MAX_SCAN_ENTRIES:
            return scanned

        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return scanned

        for entry in entries:
            if scanned >= MAX_SCAN_ENTRIES:
                break
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue

            real_dir = Path(os.path.realpath(entry.path))
            if not self._is_within_allowed_roots(real_dir, allowed_roots):
                continue

            scanned += 1
            slug = _normalize_slug(real_dir.name)
            if slug:
                catalog.setdefault(slug, []).append(real_dir)

            if depth < MAX_SCAN_DEPTH and not (real_dir / ".git").exists():
                scanned = self._scan_bounded(
                    real_dir,
                    allowed_roots,
                    catalog,
                    depth=depth + 1,
                    scanned=scanned,
                )
        return scanned

    @staticmethod
    def _match_catalog(query_slug: str, catalog: dict[str, list[Path]]) -> list[Path]:
        if query_slug in catalog:
            return list(catalog[query_slug])

        query_tokens = set(query_slug.split("-"))
        scored: list[tuple[int, Path]] = []
        for slug, paths in catalog.items():
            slug_tokens = set(slug.split("-"))
            overlap = len(query_tokens & slug_tokens)
            if query_slug in slug or overlap == len(query_tokens):
                score = overlap * 10 - abs(len(slug_tokens) - len(query_tokens))
                for path in paths:
                    scored.append((score, path))

        if not scored:
            return []
        best_score = max(score for score, _ in scored)
        return [path for score, path in scored if score == best_score]
