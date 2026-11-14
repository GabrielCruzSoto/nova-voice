"""Local PipeWire / PulseAudio device discovery via pactl JSON (NOVA-008 OP-3)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from nova.domain.models import AudioDeviceCatalog, AudioDeviceInfo

MAX_DISCOVERED_DEVICES = 32
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 2.0

WhichResolver = Callable[[str], str | None]


class CommandOutputProtocol(Protocol):
    """Minimal protocol for a finished local CLI command result."""

    @property
    def returncode(self) -> int: ...

    @property
    def stdout(self) -> str: ...


@dataclass(frozen=True, slots=True)
class _SubprocessOutput:
    returncode: int
    stdout: str


CommandRunner = Callable[..., Awaitable[CommandOutputProtocol]]


async def _default_command_runner(*argv: str) -> CommandOutputProtocol:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        raw_out, _ = await proc.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    return _SubprocessOutput(
        returncode=int(proc.returncode or 0),
        stdout=raw_out.decode("utf-8", errors="replace"),
    )


def _extract_device_description(entry: dict[str, Any], fallback_id: str) -> str:
    props = entry.get("properties")
    if isinstance(props, dict):
        for key in ("node.description", "device.description", "alsa.card_name"):
            raw = props.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    desc = entry.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    return fallback_id


def _parse_pactl_devices(
    raw_json: str,
    *,
    default_id: str | None,
    exclude_monitors: bool,
) -> tuple[AudioDeviceInfo, ...]:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()

    devices: list[AudioDeviceInfo] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str):
            continue
        dev_id = raw_name.strip()
        if not dev_id or dev_id in seen:
            continue
        if exclude_monitors and dev_id.endswith(".monitor"):
            continue
        seen.add(dev_id)
        devices.append(
            AudioDeviceInfo(
                id=dev_id,
                description=_extract_device_description(item, dev_id),
                is_default=(default_id is not None and dev_id == default_id),
            )
        )
        if len(devices) >= MAX_DISCOVERED_DEVICES:
            break
    return tuple(devices)


class PipeWireDeviceDiscoveryAdapter:
    """Enumerates local audio input/output devices using `pactl --format=json` (`shell=False`)."""

    def __init__(
        self,
        *,
        which_resolver: WhichResolver = shutil.which,
        command_runner: CommandRunner = _default_command_runner,
        timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    ) -> None:
        self._which = which_resolver
        self._runner = command_runner
        self._timeout = timeout_seconds

    async def _run_pactl(self, pactl_bin: str, *args: str) -> CommandOutputProtocol | None:
        try:
            res = await asyncio.wait_for(
                self._runner(pactl_bin, *args),
                timeout=self._timeout,
            )
        except (OSError, TimeoutError, ValueError):
            return None
        return res if res.returncode == 0 else None

    async def list_devices(self) -> AudioDeviceCatalog:
        """Return local inputs (excluding `.monitor`) and outputs, degrading cleanly on error."""
        pactl_bin = self._which("pactl")
        if not pactl_bin:
            return AudioDeviceCatalog(available=False, backend="none")

        sources_res, sinks_res, def_src_res, def_sink_res = await asyncio.gather(
            self._run_pactl(pactl_bin, "--format=json", "list", "sources"),
            self._run_pactl(pactl_bin, "--format=json", "list", "sinks"),
            self._run_pactl(pactl_bin, "get-default-source"),
            self._run_pactl(pactl_bin, "get-default-sink"),
        )
        if sources_res is None and sinks_res is None:
            return AudioDeviceCatalog(available=False, backend="none")

        default_src = def_src_res.stdout.strip() if def_src_res is not None else None
        default_sink = def_sink_res.stdout.strip() if def_sink_res is not None else None

        inputs = (
            _parse_pactl_devices(
                sources_res.stdout,
                default_id=default_src or None,
                exclude_monitors=True,
            )
            if sources_res is not None
            else ()
        )
        outputs = (
            _parse_pactl_devices(
                sinks_res.stdout,
                default_id=default_sink or None,
                exclude_monitors=False,
            )
            if sinks_res is not None
            else ()
        )
        return AudioDeviceCatalog(
            available=True,
            backend="pipewire",
            inputs=inputs,
            outputs=outputs,
        )
