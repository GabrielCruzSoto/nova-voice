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


def test_nfr4_domain_layer_never_imports_adapters_or_application() -> None:
    domain_dir = Path(__file__).resolve().parents[1] / "src" / "nova" / "domain"
    for py_file in domain_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("nova.adapters"), (
                        f"{py_file.name} imports adapter {alias.name}"
                    )
                    assert not alias.name.startswith("nova.application"), (
                        f"{py_file.name} imports application {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("nova.adapters"), (
                    f"{py_file.name} imports from adapter {node.module}"
                )
                assert not node.module.startswith("nova.application"), (
                    f"{py_file.name} imports from application {node.module}"
                )
                assert node.module not in FORBIDDEN_ADAPTER_IMPORTS
