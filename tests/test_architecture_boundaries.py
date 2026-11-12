"""Hexagonal architecture boundary tests (NFR-4 & NFR-5): domain and application stay pure."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_ADAPTER_OR_RUNTIME_PREFIXES: tuple[str, ...] = (
    "nova.adapters",
    "faster_whisper",
    "ctranslate2",
    "openwakeword",
    "onnxruntime",
    "kokoro_onnx",
    "piper",
    "sounddevice",
    "pyaudio",
    "subprocess",
)


def _is_forbidden_import(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_ADAPTER_OR_RUNTIME_PREFIXES
    )


def test_nfr4_nfr5_domain_and_application_layers_never_import_adapters_or_voice_runtimes() -> None:
    assert _is_forbidden_import("faster_whisper")
    assert _is_forbidden_import("subprocess")
    assert _is_forbidden_import("nova.adapters.audio_pipewire")
    assert not _is_forbidden_import("nova.domain.models")

    src_nova = Path(__file__).resolve().parents[1] / "src" / "nova"
    for layer_name in ("domain", "application"):
        layer_dir = src_nova / layer_name
        if not layer_dir.exists():
            continue
        for py_file in layer_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not _is_forbidden_import(alias.name), (
                            f"{layer_name}/{py_file.name} imports forbidden module {alias.name}"
                        )
                        if layer_name == "domain":
                            assert not alias.name.startswith("nova.application"), (
                                f"domain/{py_file.name} imports application {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert not _is_forbidden_import(node.module), (
                        f"{layer_name}/{py_file.name} imports from forbidden module {node.module}"
                    )
                    if layer_name == "domain":
                        assert not node.module.startswith("nova.application"), (
                            f"domain/{py_file.name} imports from application {node.module}"
                        )
