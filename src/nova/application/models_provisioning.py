"""Application service for voice model status inspection and async provisioning (NOVA-016)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nova.application.health_diagnostics import resolve_model_path


class ModelDownloaderPort(Protocol):
    """Callable port satisfied by nova.adapters.models_downloader.download_models."""

    def __call__(
        self,
        dest: Path | str,
        *,
        manifest_data: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> int: ...


class ModelItemStatus(BaseModel):
    """Availability status of a single voice model from the manifest."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    filename: str
    type: str
    description: str
    source_type: str
    size_bytes: int
    installed: bool
    path: str | None = None


class ModelsProvisionState(BaseModel):
    """Snapshot of voice models availability and active provisioning progress."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: Literal["idle", "running", "completed", "error"] = "idle"
    all_installed: bool = False
    total_models: int = 0
    completed_models: int = 0
    current_model: str | None = None
    bytes_downloaded: int = 0
    total_bytes: int = 0
    percent: int = 0
    error: str | None = None
    models: list[ModelItemStatus] = Field(default_factory=list)


def default_user_models_dir() -> Path:
    """Resolve the writable per-user models directory."""
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data).expanduser() / "nova" / "models"
    return Path.home() / ".local" / "share" / "nova" / "models"


def _load_default_manifest() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "config" / "models_manifest.json"
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Invalid models manifest format")
    return data


class VoiceModelsProvisioningService:
    """Coordinates status checks and background atomic provisioning of voice models."""

    def __init__(
        self,
        *,
        downloader: ModelDownloaderPort,
        dest_dir: Path | str | None = None,
        models_dir: str | None = None,
        manifest_data: dict[str, Any] | None = None,
        source_root: Path | None = None,
        on_provisioned: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._downloader = downloader
        self._dest_dir = Path(dest_dir).expanduser() if dest_dir is not None else None
        self._models_dir = models_dir
        self._manifest_data = manifest_data
        self._source_root = source_root
        self._on_provisioned = on_provisioned

        self._lock = threading.Lock()
        self._status: Literal["idle", "running", "completed", "error"] = "idle"
        self._completed_models = 0
        self._current_model: str | None = None
        self._bytes_downloaded = 0
        self._total_bytes = 0
        self._error: str | None = None
        self._task: asyncio.Task[None] | None = None

    def _effective_dest(self) -> Path:
        if self._dest_dir is not None:
            return self._dest_dir
        if self._models_dir:
            return Path(self._models_dir).expanduser()
        return default_user_models_dir()

    def _manifest(self) -> dict[str, Any]:
        if self._manifest_data is not None:
            return self._manifest_data
        return _load_default_manifest()

    def _inspect_models(self) -> list[ModelItemStatus]:
        manifest = self._manifest()
        entries: list[dict[str, Any]] = manifest.get("models", [])
        dest = self._effective_dest()
        items: list[ModelItemStatus] = []
        for entry in entries:
            filename = str(entry["filename"])
            resolved = resolve_model_path(
                filename,
                models_dir=self._models_dir,
                source_root=self._source_root,
            )
            if resolved is None:
                direct_candidate = dest / filename
                if direct_candidate.is_file():
                    resolved = direct_candidate
            items.append(
                ModelItemStatus(
                    filename=filename,
                    type=str(entry.get("type", "model")),
                    description=str(entry.get("description", "")),
                    source_type=str(entry.get("source_type", "https")),
                    size_bytes=int(entry.get("size_bytes", 0)),
                    installed=resolved is not None,
                    path=str(resolved) if resolved is not None else None,
                )
            )
        return items

    def get_status(self) -> ModelsProvisionState:
        """Return current availability of all manifest models and live provisioning progress."""
        items = self._inspect_models()
        total_models = len(items)
        installed_count = sum(1 for item in items if item.installed)
        all_installed = total_models > 0 and installed_count == total_models

        with self._lock:
            status = self._status
            completed = self._completed_models if status == "running" else installed_count
            current_model = self._current_model
            bytes_downloaded = self._bytes_downloaded
            total_bytes = self._total_bytes or sum(item.size_bytes for item in items)
            error = self._error

        if status == "completed" or (status == "idle" and all_installed):
            percent = 100
        elif total_models == 0:
            percent = 100
        elif total_bytes > 0 and status == "running":
            percent = min(99, int((bytes_downloaded * 100) / total_bytes))
        else:
            percent = int((completed * 100) / total_models)

        return ModelsProvisionState(
            status=status,
            all_installed=all_installed,
            total_models=total_models,
            completed_models=completed,
            current_model=current_model,
            bytes_downloaded=bytes_downloaded,
            total_bytes=total_bytes,
            percent=percent,
            error=error,
            models=items,
        )

    async def start_provisioning(self) -> ModelsProvisionState:
        """Start background model provisioning idempotently."""
        if self._task is not None and not self._task.done():
            return self.get_status()

        items = self._inspect_models()
        total_bytes = sum(item.size_bytes for item in items)
        with self._lock:
            self._status = "running"
            self._completed_models = 0
            self._current_model = items[0].filename if items else None
            self._bytes_downloaded = 0
            self._total_bytes = total_bytes
            self._error = None

        self._task = asyncio.create_task(self._run_provisioning())
        return self.get_status()

    async def _run_provisioning(self) -> None:
        dest = self._effective_dest()
        manifest = self._manifest()
        completed_bytes_acc = 0

        def _on_progress(event: dict[str, Any]) -> None:
            nonlocal completed_bytes_acc
            ev_status = str(event.get("status", ""))
            filename = str(event.get("filename", ""))
            ev_dl = int(event.get("bytes_downloaded", 0))
            ev_total = int(event.get("total_bytes", 0))
            ev_err = event.get("error")

            with self._lock:
                self._current_model = filename or self._current_model
                if ev_status in ("completed", "skipped"):
                    self._completed_models += 1
                    completed_bytes_acc += ev_total
                    self._bytes_downloaded = min(self._total_bytes, completed_bytes_acc)
                elif ev_status in ("downloading", "importing"):
                    self._bytes_downloaded = min(self._total_bytes, completed_bytes_acc + ev_dl)
                elif ev_status == "error" and ev_err:
                    self._error = str(ev_err)

        try:
            rc = await asyncio.to_thread(
                self._downloader,
                dest,
                manifest_data=manifest,
                progress_callback=_on_progress,
            )
            if rc != 0:
                with self._lock:
                    self._status = "error"
                    if not self._error:
                        self._error = "Error al descargar o verificar los modelos de voz."
                return

            with self._lock:
                self._status = "completed"
                self._current_model = None
                self._bytes_downloaded = self._total_bytes
                self._error = None

            if self._on_provisioned is not None:
                await self._on_provisioned()
        except asyncio.CancelledError:
            with self._lock:
                self._status = "idle"
                self._current_model = None
            raise
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._error = str(exc)

    async def wait_until_idle(self) -> None:
        """Await active provisioning task completion (primarily for deterministic tests)."""
        if self._task is not None and not self._task.done():
            await self._task

    async def close(self) -> None:
        """Cancel any active provisioning task cleanly during runtime shutdown."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
