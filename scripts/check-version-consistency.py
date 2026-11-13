#!/usr/bin/env python3
"""Check version consistency across pyproject.toml, package.json, and package-lock.json (AC-5)."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


def main() -> int:
    repo_root = Path.cwd()
    pyproject_path = repo_root / "pyproject.toml"
    package_json_path = repo_root / "package.json"
    package_lock_path = repo_root / "package-lock.json"

    if not pyproject_path.is_file():
        sys.stderr.write(f"check-version-consistency: pyproject.toml not found in {repo_root}\n")
        return 1

    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)

    project_version = pyproject_data.get("project", {}).get("version")
    if not project_version:
        sys.stderr.write("check-version-consistency: [project] version missing in pyproject.toml\n")
        return 1

    # Check package.json
    if package_json_path.is_file():
        with open(package_json_path, encoding="utf-8") as f:
            package_data = json.load(f)
        pkg_version = package_data.get("version")
        if pkg_version != project_version:
            sys.stderr.write(
                f"check-version-consistency: package.json version '{pkg_version}' "
                f"diverges from pyproject.toml version '{project_version}'\n"
            )
            return 1

    # Check package-lock.json
    if package_lock_path.is_file():
        with open(package_lock_path, encoding="utf-8") as f:
            lock_data = json.load(f)
        lock_version = lock_data.get("version")
        if lock_version != project_version:
            sys.stderr.write(
                f"check-version-consistency: package-lock.json version '{lock_version}' "
                f"diverges from pyproject.toml version '{project_version}'\n"
            )
            return 1
        root_package_version = lock_data.get("packages", {}).get("", {}).get("version")
        if root_package_version != project_version:
            sys.stderr.write(
                f"check-version-consistency: package-lock.json packages[''] version "
                f"'{root_package_version}' diverges from "
                f"pyproject.toml version '{project_version}'\n"
            )
            return 1

    sys.stdout.write(
        f"check-version-consistency: All version declarations match {project_version}.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
