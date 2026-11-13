"""Tests for S1: Conventional Commits hook, idempotent installation, and chaining (AC-1, AC-2)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INSTALL_HOOKS_SH = _REPO_ROOT / "scripts" / "install-hooks.sh"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "develop"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    # Commit baseline file
    (repo_dir / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=repo_dir, check=True)
    return repo_dir


def test_install_hooks_idempotent_and_non_destructive(temp_git_repo: Path) -> None:
    hooks_dir = temp_git_repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Seed mock existing hooks mirroring repositories/nova/.git/hooks
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("#!/usr/bin/env bash\n# existing pre-commit\nexit 0\n")
    pre_commit.chmod(0o755)

    pre_push = hooks_dir / "pre-push"
    pre_push.write_text("#!/usr/bin/env bash\n# existing pre-push\nexit 0\n")
    pre_push.chmod(0o755)

    prot_branches = hooks_dir / "protected-branches.sh"
    prot_branches.write_text("#!/usr/bin/env bash\n# protected branches\n")
    prot_branches.chmod(0o644)

    hashes_before = {
        "pre-commit": _sha256(pre_commit),
        "pre-push": _sha256(pre_push),
        "protected-branches.sh": _sha256(prot_branches),
    }

    # First installation
    res1 = subprocess.run(
        ["bash", str(_INSTALL_HOOKS_SH), str(hooks_dir)],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0, f"Install failed: {res1.stderr}"
    commit_msg = hooks_dir / "commit-msg"
    assert commit_msg.is_file()
    assert os.access(commit_msg, os.X_OK)

    # Check existing hooks unmodified
    for name, orig_hash in hashes_before.items():
        assert _sha256(hooks_dir / name) == orig_hash

    commit_msg_hash1 = _sha256(commit_msg)

    # Second installation (idempotency check)
    res2 = subprocess.run(
        ["bash", str(_INSTALL_HOOKS_SH), str(hooks_dir)],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0
    assert _sha256(commit_msg) == commit_msg_hash1
    for name, orig_hash in hashes_before.items():
        assert _sha256(hooks_dir / name) == orig_hash


def test_install_hooks_refuses_to_overwrite_foreign_commit_msg(temp_git_repo: Path) -> None:
    hooks_dir = temp_git_repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    commit_msg = hooks_dir / "commit-msg"
    commit_msg.write_text("#!/usr/bin/env bash\n# foreign hook without marker\necho 'custom'\n")
    commit_msg.chmod(0o755)
    orig_content = commit_msg.read_text()

    res = subprocess.run(
        ["bash", str(_INSTALL_HOOKS_SH), str(hooks_dir)],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert "Refusing to overwrite" in res.stderr or "Refusing to overwrite" in res.stdout
    assert commit_msg.read_text() == orig_content


def test_commit_msg_hook_validation_scenarios(temp_git_repo: Path) -> None:
    hooks_dir = temp_git_repo / ".git" / "hooks"
    subprocess.run(["bash", str(_INSTALL_HOOKS_SH), str(hooks_dir)], cwd=temp_git_repo, check=True)

    # Switch to feature branch
    subprocess.run(["git", "checkout", "-b", "feature/NOVA-001"], cwd=temp_git_repo, check=True)

    import sys

    venv_bin = str(Path(sys.executable).parent)

    # Helper to attempt a commit
    def try_commit(msg: str) -> tuple[int, str]:
        (temp_git_repo / "dummy.txt").write_text(f"content {msg}\n")
        subprocess.run(["git", "add", "dummy.txt"], cwd=temp_git_repo, check=True)
        env = dict(os.environ)
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
        r = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            env=env,
        )
        return r.returncode, r.stdout + r.stderr

    # Valid conventional commit
    rc, out = try_commit("feat(ci): add deb build workflow")
    assert rc == 0, f"Valid commit rejected: {out}"

    # Valid bump commit
    rc, out = try_commit("bump: version 0.1.0 → 0.2.0")
    assert rc == 0, f"Valid bump commit rejected: {out}"

    # Fixup commit accepted
    rc, out = try_commit("fixup! feat(ci): add deb build workflow")
    assert rc == 0, f"Fixup commit rejected: {out}"

    # Squash commit accepted
    rc, out = try_commit("squash! feat(ci): add deb build workflow")
    assert rc == 0, f"Squash commit rejected: {out}"

    # Invalid commit messages
    invalid_cases = [
        "update stuff",
        "feat: ",
        "feature(ci): add x",
        "feat(test): " + "a" * 90,  # > 100 characters
    ]
    for inv in invalid_cases:
        rc, out = try_commit(inv)
        assert rc != 0, f"Invalid commit should fail: {inv}"
        low = out.lower()
        assert "validation: failed" in low or "commitizen" in low or "limit" in low


def test_commit_msg_fails_cleanly_when_cz_absent(temp_git_repo: Path) -> None:
    hooks_dir = temp_git_repo / ".git" / "hooks"
    subprocess.run(["bash", str(_INSTALL_HOOKS_SH), str(hooks_dir)], cwd=temp_git_repo, check=True)
    subprocess.run(["git", "checkout", "-b", "feature/NOVA-002"], cwd=temp_git_repo, check=True)

    (temp_git_repo / "file.txt").write_text("cz missing test\n")
    subprocess.run(["git", "add", "file.txt"], cwd=temp_git_repo, check=True)

    # Empty PATH and VIRTUAL_ENV
    env = {k: v for k, v in os.environ.items() if k not in ("PATH", "VIRTUAL_ENV")}
    env["PATH"] = "/usr/bin:/bin"  # standard PATH without .venv/bin

    r = subprocess.run(
        ["git", "commit", "-m", "feat: test missing cz"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        env=env,
    )
    # If system has cz in /usr/bin this might pass, but our host does not have cz in /usr/bin
    if not Path("/usr/bin/cz").exists():
        assert r.returncode == 1
        assert "commitizen" in r.stderr.lower() or "cz" in r.stderr.lower()
