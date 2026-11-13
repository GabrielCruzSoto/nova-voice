#!/usr/bin/env bash
# Branch-validated bump command producing a single conventional commit without local tag (AC-6).
set -euo pipefail

# 1. Verify working tree is clean
dirty="$(git status --porcelain)"
if [[ -n "$dirty" ]]; then
  echo "bump-version: Working tree is dirty. Stash or commit changes before bumping version." >&2
  exit 1
fi

# 2. Extract and validate branch name
CURRENT_BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
if [[ -z "$CURRENT_BRANCH" ]]; then
  echo "bump-version: Detached HEAD state. Must be on a release or hotfix branch." >&2
  exit 1
fi

TARGET_VERSION=""
if [[ "$CURRENT_BRANCH" =~ ^feature/[A-Za-z0-9._-]+-release-([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
  TARGET_VERSION="${BASH_REMATCH[1]}"
elif [[ "$CURRENT_BRANCH" =~ ^hotfix/([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
  TARGET_VERSION="${BASH_REMATCH[1]}"
else
  echo "bump-version: Branch '$CURRENT_BRANCH' is invalid. Must match '^feature/<ticket>-release-X.Y.Z' or '^hotfix/X.Y.Z'." >&2
  exit 1
fi

if [[ ! -f "pyproject.toml" ]]; then
  echo "bump-version: pyproject.toml not found in current directory." >&2
  exit 1
fi

# 3. Read current version from pyproject.toml
CURRENT_VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"

# 4. Verify target version is strictly greater than current version
is_greater="$(python3 -c "
import sys
try:
    cur = tuple(int(x) for x in sys.argv[1].split('.'))
    tgt = tuple(int(x) for x in sys.argv[2].split('.'))
    print('yes' if tgt > cur else 'no')
except Exception:
    print('no')
" "$CURRENT_VERSION" "$TARGET_VERSION")"

if [[ "$is_greater" != "yes" ]]; then
  echo "bump-version: Target version '$TARGET_VERSION' is not greater than current version '$CURRENT_VERSION'." >&2
  exit 1
fi

# 5. Record tags before
TAGS_BEFORE="$(git tag -l 'v*')"

# 6. Locate cz executable
CZ_BIN=""
root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
common_dir="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"

if [[ -x "${root}/.venv/bin/cz" ]]; then
  CZ_BIN="${root}/.venv/bin/cz"
elif [[ -n "${common_dir}" && -x "$(dirname "${common_dir}")/.venv/bin/cz" ]]; then
  CZ_BIN="$(dirname "${common_dir}")/.venv/bin/cz"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/cz" ]]; then
  CZ_BIN="${VIRTUAL_ENV}/bin/cz"
elif command -v cz >/dev/null 2>&1; then
  CZ_BIN="$(command -v cz)"
fi

if [[ -z "$CZ_BIN" ]]; then
  echo "bump-version: 'cz' (commitizen) is required but not found in .venv/bin/cz or PATH." >&2
  exit 1
fi

# 7. Bump files and changelog without creating git tag or commit
"$CZ_BIN" bump --yes --files-only --changelog "$TARGET_VERSION"

# 8. Stage exactly the updated files and create a conventional commit
git add pyproject.toml package.json package-lock.json CHANGELOG.md
git commit -m "bump: version ${CURRENT_VERSION} → ${TARGET_VERSION}"

# 9. Verify no local tag was created (or clean up if cz created one)
TAGS_AFTER="$(git tag -l 'v*')"
if [[ "$TAGS_AFTER" != "$TAGS_BEFORE" ]]; then
  while IFS= read -r tag; do
    if ! grep -Fxq "$tag" <<< "$TAGS_BEFORE"; then
      git tag -d "$tag" >/dev/null 2>&1 || true
    fi
  done <<< "$TAGS_AFTER"
fi

echo "bump-version: Successfully bumped version from ${CURRENT_VERSION} to ${TARGET_VERSION}."
exit 0
