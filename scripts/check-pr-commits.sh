#!/usr/bin/env bash
# Validates PR commits range and PR title against Conventional Commits using cz check.
set -euo pipefail

RANGE=""
PR_TITLE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --range)
      RANGE="$2"
      shift 2
      ;;
    --title)
      PR_TITLE="$2"
      shift 2
      ;;
    --base)
      BASE="$2"
      shift 2
      ;;
    --head)
      HEAD="$2"
      shift 2
      ;;
    *)
      if [[ -z "$RANGE" ]]; then
        RANGE="$1"
        shift
      elif [[ -z "$PR_TITLE" ]]; then
        PR_TITLE="$1"
        shift
      else
        echo "check-pr-commits: Unexpected argument '$1'." >&2
        exit 1
      fi
      ;;
  esac
done

if [[ -z "$RANGE" && -n "${BASE:-}" && -n "${HEAD:-}" ]]; then
  RANGE="${BASE}..${HEAD}"
fi

if [[ -z "$RANGE" ]]; then
  echo "Usage: check-pr-commits.sh --range <base>..<head> [--title <pr-title>]" >&2
  exit 1
fi

# Locate cz executable
cz_bin=""
root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if [[ -x "${root}/.venv/bin/cz" ]]; then
  cz_bin="${root}/.venv/bin/cz"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/cz" ]]; then
  cz_bin="${VIRTUAL_ENV}/bin/cz"
elif command -v cz >/dev/null 2>&1; then
  cz_bin="$(command -v cz)"
fi

if [[ -z "$cz_bin" ]]; then
  echo "check-pr-commits: 'cz' (commitizen) is required but not found in .venv/bin/cz or PATH." >&2
  exit 1
fi

# 1. Validate each non-merge commit in the specified range
commits="$(git log --no-merges --format="%H" "$RANGE" || true)"

if [[ -n "$commits" ]]; then
  while IFS= read -r sha; do
    [[ -z "$sha" ]] && continue
    msg="$(git log -1 --format="%B" "$sha")"
    first_line="$(git log -1 --format="%s" "$sha")"
    short_sha="$(git rev-parse --short "$sha")"

    # Skip fixup/squash commits
    if [[ "$first_line" =~ ^(fixup|squash)![[:space:]] ]]; then
      continue
    fi

    tmp_msg_file="$(mktemp)"
    printf "%s\n" "$msg" > "$tmp_msg_file"

    if ! "$cz_bin" check --commit-msg-file "$tmp_msg_file" --message-length-limit 100 >/dev/null 2>&1; then
      rm -f "$tmp_msg_file"
      echo "check-pr-commits: Invalid commit message in ${short_sha}: ${first_line}" >&2
      "$cz_bin" check --commit-msg-file <(printf "%s\n" "$msg") --message-length-limit 100 || true
      exit 1
    fi
    rm -f "$tmp_msg_file"
  done <<< "$commits"
fi

# 2. Validate PR title if provided
if [[ -n "$PR_TITLE" ]]; then
  tmp_title_file="$(mktemp)"
  printf "%s\n" "$PR_TITLE" > "$tmp_title_file"
  if ! "$cz_bin" check --commit-msg-file "$tmp_title_file" --message-length-limit 100 >/dev/null 2>&1; then
    rm -f "$tmp_title_file"
    echo "check-pr-commits: Invalid PR title: ${PR_TITLE}" >&2
    "$cz_bin" check --commit-msg-file <(printf "%s\n" "$PR_TITLE") --message-length-limit 100 || true
    exit 1
  fi
  rm -f "$tmp_title_file"
fi

echo "check-pr-commits: All commits and PR title conform to Conventional Commits."
exit 0
