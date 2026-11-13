"""Tests for S5: Gitflow branch policy validation script (AC-4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLICY_SH = _REPO_ROOT / "scripts" / "check-gitflow-policy.sh"


@pytest.mark.parametrize(
    ("head_ref", "base_ref"),
    [
        ("feature/NOVA-006", "develop"),
        ("feature/NOVA-010-release-1.2.0", "release/1.2.0"),
        ("release/1.2.0", "master"),
        ("release/1.2.0", "develop"),
        ("hotfix/1.2.1", "master"),
        ("hotfix/1.2.1", "develop"),
    ],
)
def test_gitflow_policy_accepts_valid_pairs(head_ref: str, base_ref: str) -> None:
    res = subprocess.run(
        ["bash", str(_POLICY_SH), "--head-ref", head_ref, "--base-ref", base_ref],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Valid pair ({head_ref} -> {base_ref}) rejected: {res.stderr}"


@pytest.mark.parametrize(
    ("head_ref", "base_ref"),
    [
        ("feature/x", "master"),
        ("feature/x", "hotfix/1.2.1"),
        ("develop", "master"),
        ("fix/x", "develop"),
        ("release/1.2", "master"),
        ("hotfix/abc", "master"),
        ("qa/1", "master"),
    ],
)
def test_gitflow_policy_rejects_invalid_pairs(head_ref: str, base_ref: str) -> None:
    res = subprocess.run(
        ["bash", str(_POLICY_SH), "--head-ref", head_ref, "--base-ref", base_ref],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0, f"Invalid pair ({head_ref} -> {base_ref}) unexpectedly accepted"
