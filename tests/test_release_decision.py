"""Tests for S5: Release decision logic on master push (AC-7)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DECISION_SH = _REPO_ROOT / "scripts" / "release-decision.sh"


@pytest.fixture
def master_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "master_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    (repo / "pyproject.toml").write_text('[project]\nname = "nova-voice"\nversion = "1.0.0"\n')
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n- Release 1.0.0 notes\n")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "chore: release 1.0.0"], cwd=repo, check=True)
    return repo


def test_release_decision_fresh_release(master_repo: Path) -> None:
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=master_repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    res = subprocess.run(
        ["bash", str(_DECISION_SH), "--version", "1.0.0", "--sha", head_sha],
        cwd=master_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "decision=release" in res.stdout


def test_release_decision_idempotent_skip(master_repo: Path) -> None:
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=master_repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    # Create tag v1.0.0 on HEAD
    subprocess.run(["git", "tag", "v1.0.0", head_sha], cwd=master_repo, check=True)

    res = subprocess.run(
        ["bash", str(_DECISION_SH), "--version", "1.0.0", "--sha", head_sha],
        cwd=master_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "decision=skip" in res.stdout


def test_release_decision_tag_conflict_fails(master_repo: Path) -> None:
    first_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=master_repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "tag", "v1.0.0", first_sha], cwd=master_repo, check=True)

    # Make second commit
    (master_repo / "extra.txt").write_text("extra\n")
    subprocess.run(["git", "add", "extra.txt"], cwd=master_repo, check=True)
    subprocess.run(["git", "commit", "-m", "fix: extra fix"], cwd=master_repo, check=True)

    second_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=master_repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    # Attempt decision for v1.0.0 on second_sha (conflict: v1.0.0 exists on first_sha)
    res = subprocess.run(
        ["bash", str(_DECISION_SH), "--version", "1.0.0", "--sha", second_sha],
        cwd=master_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    low = (res.stdout + res.stderr).lower()
    assert "conflict" in low or "different" in low


def test_release_decision_missing_changelog_section_fails(master_repo: Path) -> None:
    # Overwrite CHANGELOG without 1.0.0
    (master_repo / "CHANGELOG.md").write_text("# Changelog\n\n## 0.9.0\n- Old notes\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=master_repo, check=True)
    subprocess.run(["git", "commit", "-m", "chore: bad changelog"], cwd=master_repo, check=True)

    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=master_repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    res = subprocess.run(
        ["bash", str(_DECISION_SH), "--version", "1.0.0", "--sha", head_sha],
        cwd=master_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "changelog" in (res.stdout + res.stderr).lower()
