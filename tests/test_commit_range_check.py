"""Tests for S1: PR commit range and title check script (AC-3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHECK_PR_COMMITS_SH = _REPO_ROOT / "scripts" / "check-pr-commits.sh"


@pytest.fixture
def pr_git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "pr_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "develop"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    (repo_dir / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: initial base commit"], cwd=repo_dir, check=True)
    return repo_dir


def test_pr_commit_range_valid_commits_and_valid_title(pr_git_repo: Path) -> None:
    subprocess.run(["git", "checkout", "-b", "feature/NOVA-003"], cwd=pr_git_repo, check=True)
    (pr_git_repo / "f1.txt").write_text("f1\n")
    subprocess.run(["git", "add", "f1.txt"], cwd=pr_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat(voice): add wakeword detector"], cwd=pr_git_repo, check=True
    )

    (pr_git_repo / "f2.txt").write_text("f2\n")
    subprocess.run(["git", "add", "f2.txt"], cwd=pr_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fix(ui): handle empty chat timeline"], cwd=pr_git_repo, check=True
    )

    # Valid range and valid title
    res = subprocess.run(
        [
            "bash",
            str(_CHECK_PR_COMMITS_SH),
            "--range",
            "develop..HEAD",
            "--title",
            "feat(voice): add wakeword and fix chat timeline",
        ],
        cwd=pr_git_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (
        f"Expected 0, got {res.returncode}. Output:\n{res.stdout}\n{res.stderr}"
    )


def test_pr_commit_range_invalid_commit_fails_with_sha(pr_git_repo: Path) -> None:
    subprocess.run(["git", "checkout", "-b", "feature/NOVA-004"], cwd=pr_git_repo, check=True)
    (pr_git_repo / "f1.txt").write_text("f1\n")
    subprocess.run(["git", "add", "f1.txt"], cwd=pr_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat(api): valid commit message"], cwd=pr_git_repo, check=True
    )

    (pr_git_repo / "f2.txt").write_text("f2\n")
    subprocess.run(["git", "add", "f2.txt"], cwd=pr_git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "bad commit message without conventional type"],
        cwd=pr_git_repo,
        check=True,
    )

    rev = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=pr_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    res = subprocess.run(
        [
            "bash",
            str(_CHECK_PR_COMMITS_SH),
            "--range",
            "develop..HEAD",
            "--title",
            "feat(api): valid title",
        ],
        cwd=pr_git_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert rev in combined, f"Short SHA {rev} not found in output: {combined}"
    assert "bad commit message" in combined


def test_pr_commit_range_invalid_title_fails(pr_git_repo: Path) -> None:
    subprocess.run(["git", "checkout", "-b", "feature/NOVA-005"], cwd=pr_git_repo, check=True)
    (pr_git_repo / "f1.txt").write_text("f1\n")
    subprocess.run(["git", "add", "f1.txt"], cwd=pr_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "feat(core): valid commit"], cwd=pr_git_repo, check=True)

    res = subprocess.run(
        [
            "bash",
            str(_CHECK_PR_COMMITS_SH),
            "--range",
            "develop..HEAD",
            "--title",
            "WIP: this is not a conventional title",
        ],
        cwd=pr_git_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "Invalid PR title" in combined or "title" in combined.lower()
