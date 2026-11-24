"""Bounded, realpath-confined multi-project workspace resolver with finite TTL+jitter cache."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import re
import tempfile
import threading
import time
import tomllib
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nova.domain.errors import (
    DesktopUiError,
    WorkspaceResolutionError,
    reject_secret_settings_keys,
)
from nova.domain.events import (
    OrchestratorEventBus,
    WorkspaceChangedEvent,
    WorkspaceClearedEvent,
)
from nova.domain.models import (
    InitializeWorkspaceRequest,
    ResolveWorkspaceRequest,
    WorkspaceCatalogEntry,
    WorkspaceContext,
    WorkspaceProject,
    WorkspaceQueryMode,
    WorkspaceTopology,
)

MAX_SCAN_ENTRIES = 500
MAX_SCAN_DEPTH = 2
MAX_MANIFEST_BYTES = 65_536
MAX_PROJECTS_PER_WORKSPACE = 64
NOVA_WORKSPACE_MANIFEST = "nova-workspace.toml"
AFF_WORKSPACE_MANIFEST = "aff.yaml"

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")

IGNORED_WORKSPACE_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "env",
        "dist",
        "build",
        "target",
        "coverage",
        "htmlcov",
        "repositories",
        "worktrees",
    }
)


def _normalize_slug(text: str) -> str:
    """Normalize accents, punctuation, and whitespace into a comparable slug."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    tokens = re.findall(r"[a-z0-9]+", lowered)
    return "-".join(tokens)


def _has_workspace_manifest(directory: Path) -> bool:
    return (directory / NOVA_WORKSPACE_MANIFEST).is_file() or (
        directory / AFF_WORKSPACE_MANIFEST
    ).is_file()


def _strip_yaml_scalar(raw: str) -> str:
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
        return val[1:-1].strip()
    if "#" in val:
        val = val.split("#", 1)[0].strip()
    return val


def _parse_inline_yaml_list(raw: str) -> list[str]:
    inner = raw.strip()
    if not (inner.startswith("[") and inner.endswith("]")):
        return []
    body = inner[1:-1].strip()
    if not body:
        return []
    items: list[str] = []
    for part in body.split(","):
        cleaned = _strip_yaml_scalar(part)
        if cleaned:
            items.append(cleaned)
    return items


def _parse_aff_yaml_subset(raw_text: str) -> dict[str, Any]:
    """Parse canonical `aff.yaml` subset using stdlib only (no unsafe YAML loader)."""
    parsed: dict[str, Any] = {}
    current_section: str | None = None
    current_list_key: str | None = None

    for raw_line in raw_text.splitlines():
        stripped_comment = raw_line.lstrip()
        if not stripped_comment or stripped_comment.startswith("#"):
            continue
        indent = len(raw_line) - len(stripped_comment)
        line = stripped_comment.rstrip()

        if line.startswith("- ") and current_list_key is not None:
            item_val = _strip_yaml_scalar(line[2:])
            if item_val:
                target_list = parsed.setdefault(current_list_key, [])
                if isinstance(target_list, list):
                    target_list.append(item_val)
            continue

        if ":" not in line:
            continue

        key_part, raw_val = line.split(":", 1)
        key = key_part.strip()
        val_clean = raw_val.strip()

        if indent == 0:
            current_section = None
            current_list_key = None
            if not val_clean:
                current_section = key
                parsed.setdefault(key, {})
            elif val_clean.startswith("["):
                parsed[key] = _parse_inline_yaml_list(val_clean)
            else:
                parsed[key] = _strip_yaml_scalar(val_clean)
            if not val_clean and key == "repositories":
                parsed[key] = []
                current_section = None
                current_list_key = "repositories"
        elif indent > 0 and current_section is not None:
            sec_obj = parsed.setdefault(current_section, {})
            if isinstance(sec_obj, dict) and val_clean and not val_clean.startswith("["):
                sec_obj[key] = _strip_yaml_scalar(val_clean)

    return parsed


@dataclass(slots=True, frozen=True)
class _WorkspaceMetadata:
    workspace_id: str
    name: str
    topology: WorkspaceTopology
    manifest_path: str | None
    projects: tuple[WorkspaceProject, ...]


@dataclass(slots=True)
class _CatalogCacheEntry:
    catalog: dict[str, list[Path]]
    entries_by_path: dict[str, WorkspaceCatalogEntry]
    expires_at: float


class LocalWorkspaceResolver:
    """Resolves isolated multi-project workspaces strictly inside allowed_roots (never ~)."""

    def __init__(
        self,
        allowed_roots: Sequence[Path],
        event_bus: OrchestratorEventBus | None = None,
        ttl_seconds: float = 30.0,
        jitter_ratio: float = 0.10,
        timeout_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
        home_dir: Path | None = None,
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
        self._home_dir_override = home_dir
        self._lock = threading.Lock()
        self._cache: dict[str, _CatalogCacheEntry] = {}
        self._active_context: WorkspaceContext | None = None

    @property
    def allowed_roots(self) -> tuple[str, ...]:
        """Expose configured allowed root directories (read-only)."""
        return tuple(str(root) for root in self._allowed_roots_raw)

    @property
    def active_context(self) -> WorkspaceContext | None:
        """Return currently selected workspace context, if any."""
        with self._lock:
            return self._active_context

    def _resolved_home_dir(self) -> Path:
        if self._home_dir_override is not None:
            return Path(os.path.realpath(str(self._home_dir_override)))
        return Path(os.path.realpath(os.path.expanduser("~")))

    def _expand_query_path(self, raw_query: str) -> Path:
        home_real = self._resolved_home_dir()
        if raw_query == "~":
            return home_real
        if raw_query.startswith("~/"):
            return Path(os.path.realpath(str(home_real / raw_query[2:])))
        return Path(os.path.realpath(os.path.expanduser(raw_query)))

    def cache_keys(self) -> list[str]:
        """Expose active cache keys for deterministic diagnostics and contract tests."""
        with self._lock:
            return list(self._cache.keys())

    def cache_ttl_remaining(self, key: str) -> float:
        """Return remaining TTL in seconds for a cached catalog key."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return 0.0
            return max(0.0, entry.expires_at - self._clock())

    async def clear_active_workspace(self, correlation_id: str) -> bool:
        """Idempotently clear active workspace and return Nova to conversational ~ mode."""
        with self._lock:
            had_active = self._active_context is not None
            self._active_context = None
        if had_active and self._event_bus is not None:
            await self._event_bus.publish(
                WorkspaceClearedEvent(
                    correlation_id=correlation_id,
                    process_cwd=str(self._resolved_home_dir()),
                )
            )
        return had_active

    async def list_workspace_entries(self) -> list[WorkspaceCatalogEntry]:
        """Return sorted structured WorkspaceCatalogEntry records from allowed_roots."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._list_entries_sync),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_RESOLUTION_TIMEOUT",
                message=f"Workspace catalog listing timed out after {self._timeout_seconds}s.",
            ) from exc

    async def list_available_workspaces(self) -> list[tuple[str, str]]:
        """Return sorted (display_name, canonical_path) pairs from allowed_roots within timeout."""
        entries = await self.list_workspace_entries()
        return [(entry.name, entry.canonical_path) for entry in entries]

    def _list_entries_sync(self) -> list[WorkspaceCatalogEntry]:
        allowed_roots = self._canonical_allowed_roots()
        if not allowed_roots:
            return []
        cache_key = self._catalog_cache_key(allowed_roots)
        _, entries_map = self._get_or_scan_catalog(cache_key, allowed_roots)
        return sorted(
            entries_map.values(),
            key=lambda item: (item.name.lower(), item.canonical_path),
        )

    async def initialize_workspace(
        self,
        request: InitializeWorkspaceRequest,
    ) -> WorkspaceContext:
        """Atomically create a new multi-project Workspace inside allowed_roots (OP-2)."""
        try:
            ws_dir, meta = await asyncio.wait_for(
                asyncio.to_thread(self._initialize_workspace_sync, request),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_RESOLUTION_TIMEOUT",
                message=f"Workspace initialization timed out after {self._timeout_seconds}s.",
            ) from exc

        with self._lock:
            self._cache.clear()

        context = WorkspaceContext(
            workspace_id=meta.workspace_id,
            name=meta.name,
            canonical_path=str(ws_dir),
            selected_via="ui_picker",
            topology=meta.topology,
            manifest_path=meta.manifest_path,
            projects=meta.projects,
        )
        if request.activate:
            with self._lock:
                self._active_context = context
            if self._event_bus is not None:
                await self._event_bus.publish(
                    WorkspaceChangedEvent(
                        correlation_id=request.correlation_id,
                        workspace_name=context.name,
                        canonical_path=context.canonical_path,
                        selected_via="ui_picker",
                        topology=context.topology,
                        projects=context.projects,
                    )
                )
        return context

    def _initialize_workspace_sync(
        self,
        request: InitializeWorkspaceRequest,
    ) -> tuple[Path, _WorkspaceMetadata]:
        home_real = self._resolved_home_dir()
        parent_expanded = self._expand_query_path(request.parent_root.strip())
        if parent_expanded == home_real:
            raise WorkspaceResolutionError(
                code="WORKSPACE_HOME_FORBIDDEN",
                message="User home directory (~) cannot be used as a workspace or parent root.",
            )
        allowed_roots = self._canonical_allowed_roots()
        if not any(
            parent_expanded == root or parent_expanded.is_relative_to(root)
            for root in allowed_roots
        ):
            raise WorkspaceResolutionError(
                code="WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                message="Target parent_root is outside allowed workspace roots.",
            )

        slug = request.workspace_slug.strip()
        if not _SAFE_SLUG_RE.fullmatch(slug) or ".." in slug:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message="Invalid workspace_slug; use alphanumeric characters, '-', '_' or '.'.",
            )
        if len(request.projects) > MAX_PROJECTS_PER_WORKSPACE:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Workspace cannot exceed {MAX_PROJECTS_PER_WORKSPACE} projects.",
            )

        cleaned_projects: list[str] = []
        seen_proj: set[str] = set()
        for raw_proj in request.projects:
            p_slug = raw_proj.strip()
            if not _SAFE_SLUG_RE.fullmatch(p_slug) or ".." in p_slug:
                raise WorkspaceResolutionError(
                    code="WORKSPACE_INVALID_MANIFEST",
                    message=f"Invalid project name '{raw_proj}' in workspace initialization.",
                )
            if p_slug not in seen_proj:
                seen_proj.add(p_slug)
                cleaned_projects.append(p_slug)

        ws_candidate = parent_expanded / slug
        if ws_candidate.exists():
            ws_real = Path(os.path.realpath(str(ws_candidate)))
            if ws_real == home_real:
                raise WorkspaceResolutionError(
                    code="WORKSPACE_HOME_FORBIDDEN",
                    message="User home directory (~) cannot be used as a workspace.",
                )
            if _has_workspace_manifest(ws_real):
                raise WorkspaceResolutionError(
                    code="WORKSPACE_ALREADY_EXISTS",
                    message=f"Workspace manifest already exists at '{slug}'.",
                )

        ws_candidate.mkdir(parents=True, exist_ok=True)
        ws_real = Path(os.path.realpath(str(ws_candidate)))
        if ws_real == home_real or not self._is_within_allowed_roots(ws_real, allowed_roots):
            raise WorkspaceResolutionError(
                code="WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                message="Initialized workspace resolves outside allowed roots.",
            )

        repos_dir = ws_real / "repositories"
        repos_dir.mkdir(parents=True, exist_ok=True)
        rel_project_entries: list[str] = []
        for p_slug in cleaned_projects:
            (repos_dir / p_slug).mkdir(parents=True, exist_ok=True)
            rel_project_entries.append(f"repositories/{p_slug}")

        display_name = (request.display_name or "").strip() or slug
        ws_id = _normalize_slug(slug) or "workspace"
        manifest_lines = [
            'schema_version = "1.0"',
            "",
            "[workspace]",
            f"id = {json.dumps(ws_id, ensure_ascii=False)}",
            f"name = {json.dumps(display_name, ensure_ascii=False)}",
            'topology = "multi-project"',
            f"projects = {json.dumps(rel_project_entries, ensure_ascii=False)}",
            "",
        ]
        manifest_bytes = "\n".join(manifest_lines).encode("utf-8")
        manifest_target = ws_real / NOVA_WORKSPACE_MANIFEST

        tmp_path: Path | None = None
        try:
            fd, raw_tmp = tempfile.mkstemp(
                prefix=".nova-ws.",
                suffix=".tmp",
                dir=str(ws_real),
            )
            tmp_path = Path(raw_tmp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, manifest_target)
        except OSError as exc:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Failed to write workspace manifest: {exc}",
            ) from exc

        meta = self._load_workspace_metadata(ws_real)
        return ws_real, meta

    async def resolve(self, request: ResolveWorkspaceRequest) -> WorkspaceContext:
        """Resolve and activate a workspace directory under OP-1 & NOVA-013 rules."""
        if request.mode == "exact_path":
            checked_mode: WorkspaceQueryMode = "exact_path"
        elif request.mode == "voice_fuzzy":
            checked_mode = "voice_fuzzy"
        elif request.mode == "ui_picker":
            checked_mode = "ui_picker"
        else:
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

        try:
            resolved_path, meta = await asyncio.wait_for(
                asyncio.to_thread(self._resolve_with_metadata_sync, raw_query, checked_mode),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_RESOLUTION_TIMEOUT",
                message=f"Workspace resolution timed out after {self._timeout_seconds}s.",
            ) from exc

        canonical_str = str(resolved_path)
        with self._lock:
            if (
                self._active_context is not None
                and self._active_context.canonical_path == canonical_str
                and self._active_context.projects == meta.projects
            ):
                return self._active_context

            context = WorkspaceContext(
                workspace_id=meta.workspace_id,
                name=meta.name,
                canonical_path=canonical_str,
                selected_via=checked_mode,
                topology=meta.topology,
                manifest_path=meta.manifest_path,
                projects=meta.projects,
            )
            self._active_context = context

        if self._event_bus is not None:
            await self._event_bus.publish(
                WorkspaceChangedEvent(
                    correlation_id=request.correlation_id,
                    workspace_name=context.name,
                    canonical_path=context.canonical_path,
                    selected_via=checked_mode,
                    topology=context.topology,
                    projects=context.projects,
                )
            )
        return context

    def _canonical_allowed_roots(self) -> list[Path]:
        home_real = self._resolved_home_dir()
        roots: list[Path] = []
        for root in self._allowed_roots_raw:
            expanded = Path(os.path.realpath(os.path.expanduser(str(root))))
            if expanded == home_real:
                continue
            if expanded.is_dir() and expanded not in roots:
                roots.append(expanded)
        return roots

    def _is_within_allowed_roots(self, candidate: Path, allowed_roots: Sequence[Path]) -> bool:
        if candidate == self._resolved_home_dir():
            return False
        return any(candidate.is_relative_to(root) and candidate != root for root in allowed_roots)

    def _resolve_with_metadata_sync(
        self,
        raw_query: str,
        mode: str,
    ) -> tuple[Path, _WorkspaceMetadata]:
        resolved_path = self._resolve_sync(raw_query, mode)
        return resolved_path, self._load_workspace_metadata(resolved_path)

    def _resolve_sync(self, raw_query: str, mode: str) -> Path:
        home_real = self._resolved_home_dir()
        if raw_query in {"~", "~/", "$HOME", "${HOME}"}:
            raise WorkspaceResolutionError(
                code="WORKSPACE_HOME_FORBIDDEN",
                message="User home directory (~) cannot be used as a workspace.",
            )

        allowed_roots = self._canonical_allowed_roots()
        is_path_like = raw_query.startswith(("/", "~", ".")) or (os.path.sep in raw_query)

        if mode == "exact_path" or (mode == "ui_picker" and is_path_like):
            expanded = self._expand_query_path(raw_query)
            if expanded == home_real:
                raise WorkspaceResolutionError(
                    code="WORKSPACE_HOME_FORBIDDEN",
                    message="User home directory (~) cannot be used as a workspace.",
                )
            if not expanded.exists() or not expanded.is_dir():
                if not any(expanded.is_relative_to(root) for root in allowed_roots):
                    raise WorkspaceResolutionError(
                        code="WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                        message="Requested path is outside allowed workspace roots.",
                    )
                raise WorkspaceResolutionError(
                    code="WORKSPACE_NOT_FOUND",
                    message=f"Directory not found: {raw_query}",
                )
            if not self._is_within_allowed_roots(expanded, allowed_roots):
                raise WorkspaceResolutionError(
                    code="WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                    message="Requested path resolves outside allowed workspace roots.",
                )
            return expanded

        query_slug = _normalize_slug(raw_query)
        if not query_slug:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_QUERY",
                message="Voice workspace query contains no searchable tokens.",
            )

        cache_key = self._catalog_cache_key(allowed_roots)
        catalog, _ = self._get_or_scan_catalog(cache_key, allowed_roots)
        matches = self._match_catalog(query_slug, catalog)

        if not matches:
            with self._lock:
                self._cache.pop(cache_key, None)
            catalog, _ = self._get_or_scan_catalog(cache_key, allowed_roots)
            matches = self._match_catalog(query_slug, catalog)

        if not matches:
            home_slug = _normalize_slug(home_real.name)
            if query_slug in {"home", home_slug} and home_slug:
                raise WorkspaceResolutionError(
                    code="WORKSPACE_HOME_FORBIDDEN",
                    message="User home directory (~) cannot be used as a workspace.",
                )
            raise WorkspaceResolutionError(
                code="WORKSPACE_NOT_FOUND",
                message=f"No workspace matched query '{raw_query}'.",
            )
        unique_matches = list(dict.fromkeys(matches))
        if len(unique_matches) > 1:
            candidate_names = tuple(sorted({path.name for path in unique_matches}))
            raise WorkspaceResolutionError(
                code="WORKSPACE_AMBIGUOUS_MATCH",
                message=f"Multiple workspaces matched '{raw_query}'.",
                candidates=candidate_names,
            )
        return unique_matches[0]

    def _load_workspace_metadata(self, ws_dir: Path) -> _WorkspaceMetadata:
        toml_manifest = ws_dir / NOVA_WORKSPACE_MANIFEST
        if toml_manifest.is_file():
            return self._load_nova_toml_manifest(ws_dir, toml_manifest)

        aff_manifest = ws_dir / AFF_WORKSPACE_MANIFEST
        if aff_manifest.is_file():
            return self._load_aff_yaml_manifest(ws_dir, aff_manifest)

        slug = _normalize_slug(ws_dir.name) or "root"
        return _WorkspaceMetadata(
            workspace_id=f"ws-{slug}",
            name=ws_dir.name,
            topology="single-repo",
            manifest_path=None,
            projects=(
                WorkspaceProject(
                    project_id=slug,
                    name=ws_dir.name,
                    relative_path=".",
                    canonical_path=str(ws_dir),
                ),
            ),
        )

    def _load_nova_toml_manifest(
        self,
        ws_dir: Path,
        manifest_file: Path,
    ) -> _WorkspaceMetadata:
        try:
            raw_bytes = manifest_file.read_bytes()
        except OSError as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Cannot read {NOVA_WORKSPACE_MANIFEST}: {exc}",
            ) from exc
        if len(raw_bytes) > MAX_MANIFEST_BYTES:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"{NOVA_WORKSPACE_MANIFEST} exceeds {MAX_MANIFEST_BYTES} bytes.",
            )
        try:
            parsed = tomllib.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Invalid UTF-8 TOML in {NOVA_WORKSPACE_MANIFEST}: {exc}",
            ) from exc

        try:
            reject_secret_settings_keys(parsed)
        except DesktopUiError as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=exc.sanitized_message,
            ) from exc
        ws_sec = parsed.get("workspace")
        if not isinstance(ws_sec, Mapping):
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Missing [workspace] table in {NOVA_WORKSPACE_MANIFEST}.",
            )
        raw_id = str(ws_sec.get("id", "")).strip() or (_normalize_slug(ws_dir.name) or "root")
        raw_name = str(ws_sec.get("name", "")).strip() or ws_dir.name
        raw_topo = str(ws_sec.get("topology", "multi-project")).strip()
        topology: WorkspaceTopology = (
            "single-repo" if raw_topo == "single-repo" else "multi-project"
        )
        raw_projects = ws_sec.get("projects", [])
        if not isinstance(raw_projects, list) or len(raw_projects) > MAX_PROJECTS_PER_WORKSPACE:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Field 'projects' in {NOVA_WORKSPACE_MANIFEST} must be a bounded list.",
            )
        projects = self._resolve_declared_projects(ws_dir, [str(x) for x in raw_projects])
        return _WorkspaceMetadata(
            workspace_id=f"ws-{_normalize_slug(raw_id) or 'root'}",
            name=raw_name,
            topology=topology,
            manifest_path=str(manifest_file),
            projects=projects,
        )

    def _load_aff_yaml_manifest(
        self,
        ws_dir: Path,
        manifest_file: Path,
    ) -> _WorkspaceMetadata:
        try:
            raw_bytes = manifest_file.read_bytes()
        except OSError as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Cannot read {AFF_WORKSPACE_MANIFEST}: {exc}",
            ) from exc
        if len(raw_bytes) > MAX_MANIFEST_BYTES:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"{AFF_WORKSPACE_MANIFEST} exceeds {MAX_MANIFEST_BYTES} bytes.",
            )
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Invalid UTF-8 in {AFF_WORKSPACE_MANIFEST}: {exc}",
            ) from exc

        parsed = _parse_aff_yaml_subset(raw_text)
        try:
            reject_secret_settings_keys(parsed)
        except DesktopUiError as exc:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=exc.sanitized_message,
            ) from exc
        proj_sec = parsed.get("project")
        ws_sec = parsed.get("workspace")
        raw_id = (
            str(proj_sec.get("id", "")).strip()
            if isinstance(proj_sec, Mapping)
            else ""
        ) or (_normalize_slug(ws_dir.name) or "root")
        raw_name = (
            str(proj_sec.get("name", "")).strip()
            if isinstance(proj_sec, Mapping)
            else ""
        ) or ws_dir.name
        aff_topo = (
            str(ws_sec.get("topology", "worktrees")).strip()
            if isinstance(ws_sec, Mapping)
            else "worktrees"
        )
        raw_repos = parsed.get("repositories", [])
        if not isinstance(raw_repos, list) or len(raw_repos) > MAX_PROJECTS_PER_WORKSPACE:
            raise WorkspaceResolutionError(
                code="WORKSPACE_INVALID_MANIFEST",
                message=f"Field 'repositories' in {AFF_WORKSPACE_MANIFEST} is invalid.",
            )

        rel_candidates: list[str] = []
        for repo_item in raw_repos:
            item_str = str(repo_item).strip()
            if not item_str:
                continue
            in_repos_dir = ws_dir / "repositories" / item_str
            if os.path.lexists(in_repos_dir):
                rel_candidates.append(f"repositories/{item_str}")
            else:
                rel_candidates.append(item_str)

        if not rel_candidates and aff_topo == "single-repo":
            slug = _normalize_slug(raw_id) or "root"
            return _WorkspaceMetadata(
                workspace_id=f"ws-{slug}",
                name=raw_name,
                topology="single-repo",
                manifest_path=str(manifest_file),
                projects=(
                    WorkspaceProject(
                        project_id=slug,
                        name=ws_dir.name,
                        relative_path=".",
                        canonical_path=str(ws_dir),
                    ),
                ),
            )

        projects = self._resolve_declared_projects(ws_dir, rel_candidates)
        return _WorkspaceMetadata(
            workspace_id=f"ws-{_normalize_slug(raw_id) or 'root'}",
            name=raw_name,
            topology="multi-project",
            manifest_path=str(manifest_file),
            projects=projects,
        )

    def _resolve_declared_projects(
        self,
        ws_dir: Path,
        rel_paths: Sequence[str],
    ) -> tuple[WorkspaceProject, ...]:
        resolved_projects: list[WorkspaceProject] = []
        seen_canonical: set[str] = set()

        for raw_rel in rel_paths:
            cleaned_rel = raw_rel.strip().strip("/")
            if (
                not cleaned_rel
                or cleaned_rel.startswith(("/", "~"))
                or ".." in cleaned_rel.split("/")
            ):
                raise WorkspaceResolutionError(
                    code="WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                    message=f"Declared project path '{raw_rel}' escapes workspace boundary.",
                )
            candidate = ws_dir / cleaned_rel
            if not os.path.lexists(candidate):
                raise WorkspaceResolutionError(
                    code="WORKSPACE_INVALID_MANIFEST",
                    message=f"Declared project directory '{cleaned_rel}' does not exist.",
                )
            real_proj = Path(os.path.realpath(str(candidate)))
            if (
                real_proj == ws_dir
                or real_proj == self._resolved_home_dir()
                or not real_proj.is_relative_to(ws_dir)
            ):
                raise WorkspaceResolutionError(
                    code="WORKSPACE_OUTSIDE_ALLOWED_ROOTS",
                    message=f"Declared project '{cleaned_rel}' resolves outside workspace root.",
                )
            if not real_proj.is_dir():
                raise WorkspaceResolutionError(
                    code="WORKSPACE_INVALID_MANIFEST",
                    message=f"Declared project '{cleaned_rel}' is not a directory.",
                )
            canonical_str = str(real_proj)
            if canonical_str in seen_canonical:
                continue
            seen_canonical.add(canonical_str)
            proj_name = real_proj.name
            resolved_projects.append(
                WorkspaceProject(
                    project_id=_normalize_slug(proj_name) or "project",
                    name=proj_name,
                    relative_path=cleaned_rel,
                    canonical_path=canonical_str,
                )
            )
        return tuple(resolved_projects)

    def _catalog_cache_key(self, allowed_roots: Sequence[Path]) -> str:
        joined = "|".join(sorted(str(r) for r in allowed_roots))
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
        return f"nova:workspace:resolver:catalog:{digest}"

    def _get_or_scan_catalog(
        self,
        cache_key: str,
        allowed_roots: Sequence[Path],
    ) -> tuple[dict[str, list[Path]], dict[str, WorkspaceCatalogEntry]]:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at > now:
                return (
                    {slug: list(paths) for slug, paths in cached.catalog.items()},
                    dict(cached.entries_by_path),
                )

        catalog: dict[str, list[Path]] = {}
        entries_by_path: dict[str, WorkspaceCatalogEntry] = {}
        scanned = 0
        for root in allowed_roots:
            scanned = self._scan_bounded(
                root,
                allowed_roots,
                catalog,
                entries_by_path,
                depth=1,
                scanned=scanned,
            )
            if scanned >= MAX_SCAN_ENTRIES:
                break

        with self._lock:
            jitter_factor = 1.0 + self._rng(-self._jitter_ratio, self._jitter_ratio)
            ttl = self._ttl_seconds * jitter_factor
            self._cache[cache_key] = _CatalogCacheEntry(
                catalog=catalog,
                entries_by_path=entries_by_path,
                expires_at=now + ttl,
            )
            return (
                {slug: list(paths) for slug, paths in catalog.items()},
                dict(entries_by_path),
            )

    def _scan_bounded(
        self,
        directory: Path,
        allowed_roots: Sequence[Path],
        catalog: dict[str, list[Path]],
        entries_by_path: dict[str, WorkspaceCatalogEntry],
        depth: int,
        scanned: int,
    ) -> int:
        if depth > MAX_SCAN_DEPTH or scanned >= MAX_SCAN_ENTRIES:
            return scanned

        home_real = self._resolved_home_dir()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return scanned

        for entry in entries:
            if scanned >= MAX_SCAN_ENTRIES:
                break
            if entry.name.startswith(".") or entry.name.lower() in IGNORED_WORKSPACE_DIRS:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue

            real_dir = Path(os.path.realpath(entry.path))
            if real_dir == home_real or not self._is_within_allowed_roots(real_dir, allowed_roots):
                continue

            scanned += 1
            has_manifest = _has_workspace_manifest(real_dir)
            try:
                meta = self._load_workspace_metadata(real_dir)
            except WorkspaceResolutionError:
                continue

            canonical_str = str(real_dir)
            entries_by_path[canonical_str] = WorkspaceCatalogEntry(
                workspace_id=meta.workspace_id,
                name=meta.name,
                canonical_path=canonical_str,
                topology=meta.topology,
                projects=meta.projects,
            )

            for slug in {_normalize_slug(real_dir.name), _normalize_slug(meta.name)}:
                if slug:
                    bucket = catalog.setdefault(slug, [])
                    if real_dir not in bucket:
                        bucket.append(real_dir)

            if depth < MAX_SCAN_DEPTH and not has_manifest and not (real_dir / ".git").exists():
                scanned = self._scan_bounded(
                    real_dir,
                    allowed_roots,
                    catalog,
                    entries_by_path,
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
