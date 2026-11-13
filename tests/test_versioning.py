"""Tests for S2: Single source of truth versioning, bump, and CLI --version (AC-5, AC-6)."""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHECK_VERSION_PY = _REPO_ROOT / "scripts" / "check-version-consistency.py"
_BUMP_VERSION_SH = _REPO_ROOT / "scripts" / "bump-version.sh"


@pytest.fixture
def version_git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "v_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "develop"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    # Seed pyproject.toml
    pyproject = repo_dir / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "nova-voice"\nversion = "0.1.0"\n\n'
        "[tool.commitizen]\nname = \"cz_conventional_commits\"\nversion_provider = \"pep621\"\n"
        'tag_format = "v$version"\n'
        'version_files = [\n    "package.json:version",\n    "package-lock.json:version",\n'
        '    "package-lock.json:packages..version",\n]\n'
        'update_changelog_on_bump = true\nchangelog_file = "CHANGELOG.md"\n'
    )

    package_json = repo_dir / "package.json"
    package_json.write_text('{\n  "name": "nova-voice-ui",\n  "version": "0.1.0"\n}\n')

    package_lock = repo_dir / "package-lock.json"
    package_lock.write_text(
        '{\n  "name": "nova-voice-ui",\n  "version": "0.1.0",\n  "packages": {\n    "": {\n'
        '      "name": "nova-voice-ui",\n      "version": "0.1.0"\n    }\n  }\n}\n'
    )

    changelog = repo_dir / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## 0.1.0\n- Initial release\n")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: baseline version 0.1.0"], cwd=repo_dir, check=True
    )
    return repo_dir


def test_version_consistency_check_passes_when_synced(version_git_repo: Path) -> None:
    res = subprocess.run(
        [sys.executable, str(_CHECK_VERSION_PY)],
        cwd=version_git_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, (
        f"Expected 0, got {res.returncode}. Output:\n{res.stdout}\n{res.stderr}"
    )


def test_version_consistency_check_fails_when_package_json_diverges(
    version_git_repo: Path,
) -> None:
    package_json = version_git_repo / "package.json"
    package_json.write_text('{\n  "name": "nova-voice-ui",\n  "version": "0.2.0"\n}\n')

    res = subprocess.run(
        [sys.executable, str(_CHECK_VERSION_PY)],
        cwd=version_git_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "package.json" in combined


def test_version_consistency_check_fails_when_package_lock_diverges(
    version_git_repo: Path,
) -> None:
    package_lock = version_git_repo / "package-lock.json"
    package_lock.write_text(
        '{\n  "name": "nova-voice-ui",\n  "version": "0.1.0",\n  "packages": {\n    "": {\n'
        '      "name": "nova-voice-ui",\n      "version": "0.3.0"\n    }\n  }\n}\n'
    )

    res = subprocess.run(
        [sys.executable, str(_CHECK_VERSION_PY)],
        cwd=version_git_repo,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    combined = res.stdout + res.stderr
    assert "package-lock.json" in combined


def test_no_semantic_version_literals_in_src() -> None:
    src_dir = _REPO_ROOT / "src" / "nova"
    assert src_dir.is_dir()

    semver_pattern = re.compile(
        r'^\s*(__version__|version)\s*=\s*["\']\d+\.\d+\.\d+["\']', re.MULTILINE
    )
    for py_file in src_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        match = semver_pattern.search(content)
        assert match is None, f"Found semantic version literal in {py_file}: {match.group(0)}"


def test_desktop_launcher_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from nova.adapters.desktop_launcher import main

    # When version is found
    with patch("importlib.metadata.version", return_value="0.1.0"):
        rc = main(["--version"])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == "nova-voice 0.1.0\n"

    # When PackageNotFoundError is raised
    with patch("importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError):
        rc = main(["--version"])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == "nova-voice unknown\n"


def test_bump_version_valid_branch_creates_single_conventional_commit_without_tag(
    version_git_repo: Path,
) -> None:
    # Switch to feature branch meeting regex: feature/<ticket>-release-X.Y.Z
    subprocess.run(
        ["git", "checkout", "-b", "feature/NOVA-006-release-0.2.0"],
        cwd=version_git_repo,
        check=True,
    )

    tags_before = subprocess.run(
        ["git", "tag", "--list", "v*"],
        cwd=version_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    import sys
    env = dict(subprocess.os.environ)  # type: ignore[attr-defined]
    env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"

    res = subprocess.run(
        ["bash", str(_BUMP_VERSION_SH)],
        cwd=version_git_repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"Bump failed: {res.stdout}\n{res.stderr}"

    # Verify no local tag was created
    tags_after = subprocess.run(
        ["git", "tag", "--list", "v*"],
        cwd=version_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert tags_after == tags_before

    # Verify exactly one commit created
    commit_msg = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=version_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert "bump: version 0.1.0" in commit_msg
    assert "0.2.0" in commit_msg

    # Verify modified files are exactly:
    # pyproject.toml, package.json, package-lock.json, CHANGELOG.md
    diff_files = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        cwd=version_git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()
    assert sorted(diff_files) == sorted(
        ["CHANGELOG.md", "package-lock.json", "package.json", "pyproject.toml"]
    )

    # Verify CHANGELOG.md contains section 0.2.0
    changelog_content = (version_git_repo / "CHANGELOG.md").read_text()
    assert "0.2.0" in changelog_content


def test_bump_version_rejects_invalid_branch(version_git_repo: Path) -> None:
    subprocess.run(
        ["git", "checkout", "-b", "feature/invalid-branch-name"], cwd=version_git_repo, check=True
    )
    res = subprocess.run(
        ["bash", str(_BUMP_VERSION_SH)], cwd=version_git_repo, capture_output=True, text=True
    )
    assert res.returncode != 0
    assert "branch" in (res.stdout + res.stderr).lower()


def test_bump_version_rejects_lower_or_equal_version(version_git_repo: Path) -> None:
    subprocess.run(
        ["git", "checkout", "-b", "feature/NOVA-006-release-0.1.0"],
        cwd=version_git_repo,
        check=True,
    )
    res = subprocess.run(
        ["bash", str(_BUMP_VERSION_SH)], cwd=version_git_repo, capture_output=True, text=True
    )
    assert res.returncode != 0
    low = (res.stdout + res.stderr).lower()
    assert "greater" in low or "version" in low


def test_bump_version_rejects_dirty_working_tree(version_git_repo: Path) -> None:
    subprocess.run(
        ["git", "checkout", "-b", "feature/NOVA-006-release-0.2.0"],
        cwd=version_git_repo,
        check=True,
    )
    (version_git_repo / "dirty.txt").write_text("uncommitted\n")
    res = subprocess.run(
        ["bash", str(_BUMP_VERSION_SH)], cwd=version_git_repo, capture_output=True, text=True
    )
    assert res.returncode != 0
    low = (res.stdout + res.stderr).lower()
    assert "clean" in low or "dirty" in low
