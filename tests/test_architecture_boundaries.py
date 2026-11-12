"""Hexagonal architecture boundary tests (NFR-4): domain and application stay adapter-agnostic."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_ADAPTER_IMPORTS = {
    "nova.adapters",
    "nova.adapters.antigravity_adapter",
    "nova.adapters.pi_rpc_client",
    "nova.adapters.workspace_resolver",
}


def test_nfr4_domain_and_application_layers_never_import_adapters() -> None:
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
                        assert not alias.name.startswith("nova.adapters"), (
                            f"{layer_name}/{py_file.name} imports adapter {alias.name}"
                        )
                        if layer_name == "domain":
                            assert not alias.name.startswith("nova.application"), (
                                f"domain/{py_file.name} imports application {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("nova.adapters"), (
                        f"{layer_name}/{py_file.name} imports from adapter {node.module}"
                    )
                    if layer_name == "domain":
                        assert not node.module.startswith("nova.application"), (
                            f"domain/{py_file.name} imports from application {node.module}"
                        )
                    assert node.module not in FORBIDDEN_ADAPTER_IMPORTS
