"""TDD tests for NOVA-008 T2: local PipeWire/PulseAudio device discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from nova.adapters.audio_device_discovery import PipeWireDeviceDiscoveryAdapter


@dataclass
class _FakeResult:
    returncode: int
    stdout: str


@pytest.mark.asyncio
async def test_audio_discovery_parses_pactl_json_and_filters_monitors() -> None:
    sources_json = json.dumps(
        [
            {
                "name": "alsa_output.pci.analog-stereo.monitor",
                "description": "Monitor of Built-in Audio",
                "properties": {"device.description": "Monitor of Built-in Audio"},
            },
            {
                "name": "alsa_input.usb-mic.mono",
                "description": "USB Condenser Mic",
                "properties": {"node.description": "USB Condenser Mic"},
            },
        ]
    )
    sinks_json = json.dumps(
        [
            {
                "name": "alsa_output.pci.analog-stereo",
                "description": "Built-in Audio Analog Stereo",
            }
        ]
    )

    async def fake_runner(*argv: str) -> _FakeResult:
        cmd = list(argv[1:])
        if cmd == ["--format=json", "list", "sources"]:
            return _FakeResult(0, sources_json)
        if cmd == ["--format=json", "list", "sinks"]:
            return _FakeResult(0, sinks_json)
        if cmd == ["get-default-source"]:
            return _FakeResult(0, "alsa_input.usb-mic.mono\n")
        if cmd == ["get-default-sink"]:
            return _FakeResult(0, "alsa_output.pci.analog-stereo\n")
        return _FakeResult(1, "")

    adapter = PipeWireDeviceDiscoveryAdapter(
        which_resolver=lambda name: f"/usr/bin/{name}" if name == "pactl" else None,
        command_runner=fake_runner,
    )
    catalog = await adapter.list_devices()
    assert catalog.available is True
    assert catalog.backend == "pipewire"
    assert len(catalog.inputs) == 1
    assert catalog.inputs[0].id == "alsa_input.usb-mic.mono"
    assert catalog.inputs[0].description == "USB Condenser Mic"
    assert catalog.inputs[0].is_default is True
    assert len(catalog.outputs) == 1
    assert catalog.outputs[0].id == "alsa_output.pci.analog-stereo"
    assert catalog.outputs[0].is_default is True


@pytest.mark.asyncio
async def test_audio_discovery_degrades_gracefully_when_missing_or_timed_out() -> None:
    adapter_missing = PipeWireDeviceDiscoveryAdapter(which_resolver=lambda _name: None)
    catalog_missing = await adapter_missing.list_devices()
    assert catalog_missing.available is False
    assert catalog_missing.backend == "none"
    assert catalog_missing.inputs == ()
    assert catalog_missing.outputs == ()

    async def timeout_runner(*_argv: str) -> _FakeResult:
        raise TimeoutError("pactl timed out")

    adapter_timeout = PipeWireDeviceDiscoveryAdapter(
        which_resolver=lambda name: f"/usr/bin/{name}" if name == "pactl" else None,
        command_runner=timeout_runner,
    )
    catalog_timeout = await adapter_timeout.list_devices()
    assert catalog_timeout.available is False
    assert catalog_timeout.inputs == ()
    assert catalog_timeout.outputs == ()
