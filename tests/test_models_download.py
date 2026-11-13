"""Tests for S3: Manifest-driven, SHA256-verified, atomic voice models downloader (AC-13)."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from nova.adapters.models_downloader import download_models, load_manifest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _MockModelHandler(BaseHTTPRequestHandler):
    payloads: dict[str, bytes] = {}

    def do_GET(self) -> None:
        path = self.path.lstrip("/")
        if path in self.payloads:
            content = self.payloads[path]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def mock_http_server() -> Iterator[tuple[str, dict[str, bytes]]]:
    payloads: dict[str, bytes] = {}
    _MockModelHandler.payloads = payloads

    server = HTTPServer(("127.0.0.1", 0), _MockModelHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, payloads
    finally:
        server.shutdown()
        server.server_close()


def test_faster_whisper_not_in_manifest() -> None:
    manifest = load_manifest()
    models = manifest.get("models", [])
    filenames = [m["filename"] for m in models]
    for fn in filenames:
        assert "whisper" not in fn.lower(), f"faster-whisper model '{fn}' must not be in manifest"


def test_download_models_atomic_and_idempotent(
    tmp_path: Path, mock_http_server: tuple[str, dict[str, bytes]]
) -> None:
    base_url, payloads = mock_http_server
    vad_bytes = b"mock vad model content 12345678"
    vad_hash = _sha256(vad_bytes)
    payloads["test_vad.onnx"] = vad_bytes

    synthetic_manifest = {
        "schema_version": "1.0.0",
        "models": [
            {
                "filename": "test_vad.onnx",
                "type": "vad",
                "source_type": "https",
                "url": f"{base_url}/test_vad.onnx",
                "sha256": vad_hash,
                "size_bytes": len(vad_bytes),
                "description": "Mock VAD model",
            }
        ],
    }

    dest_dir = tmp_path / "models_dest"

    # First download
    rc1 = download_models(dest_dir, manifest_data=synthetic_manifest)
    assert rc1 == 0
    downloaded_file = dest_dir / "test_vad.onnx"
    assert downloaded_file.is_file()
    assert _sha256(downloaded_file.read_bytes()) == vad_hash

    # Ensure no leftover .part files
    assert list(dest_dir.glob("*.part")) == []

    # Second download (idempotency check)
    rc2 = download_models(dest_dir, manifest_data=synthetic_manifest)
    assert rc2 == 0
    assert _sha256(downloaded_file.read_bytes()) == vad_hash


def test_download_models_hash_mismatch_fails_and_cleans_up(
    tmp_path: Path, mock_http_server: tuple[str, dict[str, bytes]]
) -> None:
    base_url, payloads = mock_http_server
    actual_bytes = b"corrupted payload"
    expected_hash = _sha256(b"correct expected payload")
    payloads["corrupt_vad.onnx"] = actual_bytes

    synthetic_manifest = {
        "schema_version": "1.0.0",
        "models": [
            {
                "filename": "corrupt_vad.onnx",
                "type": "vad",
                "source_type": "https",
                "url": f"{base_url}/corrupt_vad.onnx",
                "sha256": expected_hash,
                "size_bytes": len(actual_bytes),
                "description": "Mock VAD model with bad hash",
            }
        ],
    }

    dest_dir = tmp_path / "models_dest"
    rc = download_models(dest_dir, manifest_data=synthetic_manifest)
    assert rc == 1
    # File should not exist in destination and no .part leftover
    assert not (dest_dir / "corrupt_vad.onnx").exists()
    assert list(dest_dir.glob("*.part")) == []


def test_local_import_handling(tmp_path: Path) -> None:
    ww_bytes = b"mock wakeword content 987654"
    ww_hash = _sha256(ww_bytes)

    synthetic_manifest = {
        "schema_version": "1.0.0",
        "models": [
            {
                "filename": "oye_nova.onnx",
                "type": "wakeword",
                "source_type": "local-import",
                "sha256": ww_hash,
                "size_bytes": len(ww_bytes),
                "description": "Mock local wake-word",
            }
        ],
    }

    dest_dir = tmp_path / "models_dest"

    # Without --import, exits 0 and reports pending
    rc1 = download_models(dest_dir, manifest_data=synthetic_manifest)
    assert rc1 == 0
    assert not (dest_dir / "oye_nova.onnx").exists()

    # With --import directory
    import_dir = tmp_path / "import_source"
    import_dir.mkdir()
    (import_dir / "oye_nova.onnx").write_bytes(ww_bytes)

    rc2 = download_models(dest_dir, import_dir=import_dir, manifest_data=synthetic_manifest)
    assert rc2 == 0
    imported_file = dest_dir / "oye_nova.onnx"
    assert imported_file.is_file()
    assert _sha256(imported_file.read_bytes()) == ww_hash
