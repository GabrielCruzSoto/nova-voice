#!/usr/bin/env bash
# Determine idempotently whether to release, skip, or fail on master push (OP-5, AC-7).
set -euo pipefail

VERSION=""
SHA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="$2"
      shift 2
      ;;
    --sha)
      SHA="$2"
      shift 2
      ;;
    *)
      if [[ -z "$VERSION" ]]; then
        VERSION="$1"
        shift
      elif [[ -z "$SHA" ]]; then
        SHA="$1"
        shift
      else
        echo "release-decision: Unexpected argument '$1'." >&2
        exit 1
      fi
      ;;
  esac
done

if [[ -z "$VERSION" || -z "$SHA" ]]; then
  echo "Usage: release-decision.sh --version <X.Y.Z> --sha <commit-sha>" >&2
  exit 1
fi

# 1. Verify CHANGELOG.md contains required section for $VERSION
if [[ ! -f "CHANGELOG.md" ]]; then
  echo "release-decision error: CHANGELOG.md not found." >&2
  exit 1
fi

if ! grep -E "^##[[:space:]]+(\[)?${VERSION}(\])?([[:space:]]|$)" "CHANGELOG.md" >/dev/null 2>&1; then
  echo "release-decision error: CHANGELOG.md missing section for version '${VERSION}'." >&2
  exit 1
fi

# 2. Check git tag status
TAG_NAME="v${VERSION}"
TARGET_SHA="$(git rev-parse "${SHA}^{commit}")"

TAG_SHA="$(git rev-parse --verify -q "refs/tags/${TAG_NAME}^{commit}" 2>/dev/null || true)"

if [[ -n "$TAG_SHA" ]]; then
  if [[ "$TAG_SHA" == "$TARGET_SHA" ]]; then
    echo "decision=skip"
    echo "release-decision: Tag '${TAG_NAME}' already exists on target commit ${TARGET_SHA}. Skipping release."
    exit 0
  else
    echo "release-decision error: Tag conflict! Tag '${TAG_NAME}' already points to different commit (${TAG_SHA}), expected ${TARGET_SHA}." >&2
    exit 1
  fi
fi

# 3. Fresh release condition met
echo "decision=release"
echo "release-decision: Ready to publish release for ${TAG_NAME} on commit ${TARGET_SHA}."
exit 0
