"""Tests for voice models provisioning service and runtime hot-reload (NOVA-016)."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.models_downloader import download_models
from nova.application.models_provisioning import VoiceModelsProvisioningService


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _PayloadHandler(BaseHTTPRequestHandler):
    payloads: dict[str, bytes] = {}

    def do_GET(self) -> None:
        key = self.path.lstrip("/")
        if key in self.payloads:
            body = self.payloads[key]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def mock_models_server() -> Iterator[tuple[str, dict[str, bytes]]]:
    payloads: dict[str, bytes] = {}
    _PayloadHandler.payloads = payloads
    server = HTTPServer(("127.0.0.1", 0), _PayloadHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", payloads
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_provisioning_service_downloads_and_invokes_reload_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_models_server: tuple[str, dict[str, bytes]],
) -> None:
    base_url, payloads = mock_models_server
    sample_bytes = b"synthetic-silero-vad-bytes"
    sample_hash = _sha256(sample_bytes)
    payloads["silero_vad.onnx"] = sample_bytes

    manifest = {
        "schema_version": "1.0.0",
        "models": [
            {
                "filename": "silero_vad.onnx",
                "type": "vad",
                "source_type": "https",
                "url": f"{base_url}/silero_vad.onnx",
                "sha256": sample_hash,
                "size_bytes": len(sample_bytes),
                "description": "Synthetic VAD",
            }
        ],
    }

    dest = tmp_path / "user_models"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("NOVA_MODELS_DIR", raising=False)

    reloaded = asyncio.Event()

    async def on_provisioned() -> None:
        reloaded.set()

    svc = VoiceModelsProvisioningService(
        downloader=download_models,
        dest_dir=dest,
        manifest_data=manifest,
        source_root=tmp_path,
        on_provisioned=on_provisioned,
    )
    initial = svc.get_status()
    assert initial.all_installed is False
    assert initial.status == "idle"
    assert len(initial.models) == 1
    assert initial.models[0].installed is False

    started = await svc.start_provisioning()
    assert started.status in ("running", "completed")

    await asyncio.wait_for(reloaded.wait(), timeout=5.0)
    await svc.wait_until_idle()

    final = svc.get_status()
    assert final.status == "completed"
    assert final.all_installed is True
    assert final.percent == 100
    assert (dest / "silero_vad.onnx").is_file()
    await svc.close()


@pytest.mark.asyncio
async def test_runtime_recheck_health_hot_reloads_voice_and_clears_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nova.adapters.desktop_launcher import NovaDesktopConfig, NovaDesktopRuntime
    from nova.domain.errors import VoicePipelineError
    from nova.domain.models import CallSession

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    monkeypatch.setenv("NOVA_MODELS_DIR", str(models_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    def fake_which(cmd: str) -> str | None:
        if cmd in ("pi", "pw-record", "pw-play"):
            return f"/usr/bin/{cmd}"
        return None

    class _FakeVoiceRuntime:
        def __init__(self) -> None:
            self.session = CallSession(
                call_id="call-hot",
                state="standby",
                last_activity_monotonic=0.0,
            )
            self.started = False

        async def start(self) -> None:
            self.started = True

        async def shutdown(self) -> None:
            self.started = False

        async def start_call(self, reason: str = "ui_call_start") -> None:
            pass

        async def end_call(self, reason: str = "ui_call_end") -> None:
            pass

        async def barge_in(self) -> None:
            pass

    attempts = 0

    def flaky_voice_factory(**_kwargs: Any) -> _FakeVoiceRuntime:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise VoicePipelineError("MODELS_MISSING", "models missing on first boot")
        return _FakeVoiceRuntime()

    config = NovaDesktopConfig(
        port=0,
        enable_voice=True,
        open_browser=False,
        config_path=str(tmp_path / "config.toml"),
        ipc_runtime_dir=str(tmp_path / "ipc"),
        allowed_roots=(str(tmp_path),),
    )

    runtime = NovaDesktopRuntime(
        config=config,
        voice_factory=flaky_voice_factory,
        which_resolver=fake_which,
    )
    try:
        await runtime.start()
        assert runtime.voice_enabled is False
        assert runtime.health_report is not None
        assert runtime.health_report.all_ok is False

        # Provision all required models into models_dir
        for fn in (
            "oye_nova.onnx",
            "melspectrogram.onnx",
            "embedding_model.onnx",
            "silero_vad.onnx",
            "es_ES-davefx-medium.onnx",
        ):
            (models_dir / fn).write_bytes(b"ok-model")

        report, voice_ok = await runtime.recheck_health_and_reload_voice()
        assert report.all_ok is True
        assert voice_ok is True
        assert runtime.voice_enabled is True
        assert attempts == 2

        assert runtime._state_hub is not None
        snap = await runtime._state_hub.build_snapshot(csrf_token="tok")
        assert snap.voice_enabled is True
        assert all(m.step_type != "COMPONENTS_MISSING" for m in snap.messages)
    finally:
        await runtime.close()
