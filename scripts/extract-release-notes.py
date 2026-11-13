#!/usr/bin/env python3
"""Extract release notes for a specific version from CHANGELOG.md."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def extract_release_notes(changelog_path: Path, version: str) -> str:
    if not changelog_path.is_file():
        raise FileNotFoundError(f"Changelog not found: {changelog_path}")

    content = changelog_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    # Match: ## 0.1.0, ## [0.1.0], ## [0.1.0] - 2026-11-12, etc.
    header_pattern = re.compile(rf"^##\s+\[?{re.escape(version)}\]?(?:\s+.*)?$")
    next_header_pattern = re.compile(r"^##\s+")

    recording = False
    notes: list[str] = []

    for line in lines:
        if header_pattern.match(line):
            recording = True
            continue
        elif recording and next_header_pattern.match(line):
            break
        if recording:
            notes.append(line)

    if not recording:
        raise ValueError(f"Version '{version}' not found in {changelog_path}")

    return "\n".join(notes).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract release notes from CHANGELOG.md")
    parser.add_argument("--version", required=True, help="Target semantic version (X.Y.Z)")
    parser.add_argument(
        "--changelog",
        default="CHANGELOG.md",
        help="Path to CHANGELOG.md (default: CHANGELOG.md)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path (default: stdout)",
    )

    args = parser.parse_args(argv)

    try:
        notes = extract_release_notes(Path(args.changelog), args.version)
    except Exception as exc:
        print(f"extract-release-notes error: {exc}", file=sys.stderr)
        return 1

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(notes, encoding="utf-8")
    else:
        sys.stdout.write(notes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
