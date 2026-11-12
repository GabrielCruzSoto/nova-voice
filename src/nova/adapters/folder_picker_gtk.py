"""Isolated Linux GTK3 folder picker adapter via system Python subprocess (NOVA-004 OP-4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nova.domain.errors import DesktopUiError

_GTK_PICKER_SCRIPT = """\
import json
import os
import sys

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
except Exception as exc:
    sys.stderr.write(f"GTK3 import failed: {exc}\\n")
    sys.exit(2)

ok, _ = Gtk.init_check(sys.argv[:1])
if not ok:
    sys.stderr.write("Gtk.init_check failed (no graphical display).\\n")
    sys.exit(2)

dialog = Gtk.FileChooserDialog(
    title="Nova — Seleccionar carpeta de proyecto",
    parent=None,
    action=Gtk.FileChooserAction.SELECT_FOLDER,
)
dialog.add_buttons(
    Gtk.STOCK_CANCEL,
    Gtk.ResponseType.CANCEL,
    Gtk.STOCK_OPEN,
    Gtk.ResponseType.OK,
)
if len(sys.argv) > 1 and sys.argv[1] and os.path.isdir(sys.argv[1]):
    dialog.set_current_folder(sys.argv[1])

response = dialog.run()
chosen = dialog.get_filename() if response == Gtk.ResponseType.OK else None
dialog.destroy()
while Gtk.events_pending():
    Gtk.main_iteration()

if chosen:
    sys.stdout.write(json.dumps({"status": "selected", "path": chosen}) + "\\n")
else:
    sys.stdout.write(json.dumps({"status": "cancelled"}) + "\\n")
sys.stdout.flush()
"""

_SAFE_DISPLAY_ENV_KEYS: tuple[str, ...] = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
)


class PickerProcessProtocol(Protocol):
    """Minimal async process protocol for isolated GTK3 folder dialog execution."""

    @property
    def returncode(self) -> int | None: ...

    async def communicate(self) -> tuple[bytes, bytes]: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class _AsyncPickerProc:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return await self._proc.communicate()

    def kill(self) -> None:
        self._proc.kill()

    async def wait(self) -> int:
        return await self._proc.wait()


PickerSpawner = Callable[..., Awaitable[PickerProcessProtocol]]


async def _default_picker_spawner(*argv: str, env: dict[str, str]) -> PickerProcessProtocol:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    return _AsyncPickerProc(proc)


class GtkFolderPickerSettings(BaseModel):
    """Configuration boundary for GtkFolderPickerAdapter (OP-4)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    python_binary: str = Field(default="/usr/bin/python3", min_length=1)
    timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0)

    @field_validator("python_binary")
    @classmethod
    def _validate_python_binary(cls, value: str) -> str:
        cleaned = value.strip()
        has_ctrl = any(ord(c) < 32 or ord(c) == 127 for c in cleaned)
        if not cleaned or cleaned.startswith("-") or has_ctrl:
            raise ValueError("python_binary must be a valid executable path or name")
        return cleaned


class GtkFolderPickerAdapter:
    """Invokes system Python3 + GTK3 FileChooserDialog safely (shell=False, headless fallback)."""

    def __init__(
        self,
        settings: GtkFolderPickerSettings | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        which_resolver: Callable[[str], str | None] = shutil.which,
        spawner: PickerSpawner = _default_picker_spawner,
    ) -> None:
        self._settings = settings or GtkFolderPickerSettings()
        self._environ = environ if environ is not None else os.environ
        self._which = which_resolver
        self._spawner = spawner

    def _build_safe_env(self) -> dict[str, str]:
        display = self._environ.get("DISPLAY", "").strip()
        wayland = self._environ.get("WAYLAND_DISPLAY", "").strip()
        if not display and not wayland:
            raise DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message=(
                    "No graphical display (DISPLAY or WAYLAND_DISPLAY) available for native picker."
                ),
            )
        safe_env: dict[str, str] = {}
        for key in _SAFE_DISPLAY_ENV_KEYS:
            val = self._environ.get(key)
            if val:
                safe_env[key] = val
        safe_env.setdefault("PATH", "/usr/bin:/bin")
        return safe_env

    def _resolve_python_binary_sync(self) -> str:
        raw_bin = self._settings.python_binary
        if os.path.isabs(raw_bin):
            if os.path.isfile(raw_bin) and os.access(raw_bin, os.X_OK):
                return raw_bin
            raise DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message=f"System Python binary not executable: {raw_bin}",
            )
        found = self._which(raw_bin)
        if not found:
            raise DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message=f"System Python binary not found: {raw_bin}",
            )
        return found

    async def pick_directory(self, initial_dir: Path | None = None) -> Path | None:
        """Open native GTK3 folder chooser; return selected Path or None if cancelled."""
        safe_env = self._build_safe_env()
        python_bin = await asyncio.to_thread(self._resolve_python_binary_sync)

        argv: list[str] = [python_bin, "-s", "-E", "-c", _GTK_PICKER_SCRIPT]
        if initial_dir is not None:
            resolved_init = await asyncio.to_thread(
                lambda: Path(os.path.realpath(str(initial_dir)))
            )
            init_str = str(resolved_init)
            if not any(ord(ch) < 32 or ord(ch) == 127 for ch in init_str):
                argv.append(init_str)

        try:
            proc = await asyncio.wait_for(self._spawner(*argv, env=safe_env), timeout=2.0)
        except (TimeoutError, OSError) as exc:
            raise DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message=f"Failed to spawn native GTK3 folder picker: {exc}",
            ) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._settings.timeout_seconds,
            )
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(TimeoutError, OSError):
                await asyncio.wait_for(proc.wait(), timeout=0.05)
            raise DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message="Native GTK3 folder picker timed out.",
            ) from exc
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(TimeoutError, OSError):
                await asyncio.wait_for(proc.wait(), timeout=0.05)
            raise

        if (proc.returncode or 0) != 0:
            err_detail = stderr_bytes[:512].decode("utf-8", errors="replace").strip()
            raise DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message=(
                    f"Native GTK3 folder picker unavailable (exit={proc.returncode}): {err_detail}"
                ),
            )

        try:
            payload = json.loads(stdout_bytes[:8192].decode("utf-8").strip())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message="Invalid JSON output from native GTK3 folder picker.",
            ) from exc

        if not isinstance(payload, dict):
            raise DesktopUiError(
                code="UI_NATIVE_PICKER_UNAVAILABLE",
                message="Malformed response object from native GTK3 folder picker.",
            )

        status = payload.get("status")
        if status == "cancelled":
            return None
        raw_path = payload.get("path")
        if status == "selected" and isinstance(raw_path, str) and raw_path.strip():
            return await asyncio.to_thread(lambda: Path(os.path.realpath(raw_path.strip())))

        raise DesktopUiError(
            code="UI_NATIVE_PICKER_UNAVAILABLE",
            message="Unexpected status returned by native GTK3 folder picker.",
        )
