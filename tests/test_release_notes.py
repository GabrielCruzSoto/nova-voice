"""Tests for scripts/extract-release-notes.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXTRACT_SCRIPT = _REPO_ROOT / "scripts" / "extract-release-notes.py"


def test_extract_release_notes_success(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n"
        "## [0.2.0] - 2026-11-13\n\n"
        "### Features\n"
        "- Add voice model downloader\n\n"
        "## [0.1.0] - 2026-11-12\n\n"
        "### Initial\n"
        "- First release\n\n"
        "## [0.0.1]\n"
        "- Prototype\n",
        encoding="utf-8",
    )

    out_file = tmp_path / "notes_0.2.0.md"
    res = subprocess.run(
        [
            sys.executable,
            str(_EXTRACT_SCRIPT),
            "--version",
            "0.2.0",
            "--changelog",
            str(changelog),
            "--output",
            str(out_file),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    content = out_file.read_text(encoding="utf-8")
    assert "### Features" in content
    assert "Add voice model downloader" in content
    assert "0.1.0" not in content
    assert "First release" not in content


def test_extract_release_notes_stdout(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## 1.0.0\n\n"
        "- Major feature release\n\n"
        "## 0.9.0\n"
        "- Old release\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        [
            sys.executable,
            str(_EXTRACT_SCRIPT),
            "--version",
            "1.0.0",
            "--changelog",
            str(changelog),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "- Major feature release" in res.stdout
    assert "Old release" not in res.stdout


def test_extract_release_notes_missing_version(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## 0.1.0\n\n- Fix bug\n", encoding="utf-8")

    res = subprocess.run(
        [
            sys.executable,
            str(_EXTRACT_SCRIPT),
            "--version",
            "9.9.9",
            "--changelog",
            str(changelog),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "not found" in res.stderr.lower()


def test_extract_release_notes_missing_file(tmp_path: Path) -> None:
    non_existent = tmp_path / "NO_FILE.md"
    res = subprocess.run(
        [
            sys.executable,
            str(_EXTRACT_SCRIPT),
            "--version",
            "1.0.0",
            "--changelog",
            str(non_existent),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "not found" in res.stderr.lower()
