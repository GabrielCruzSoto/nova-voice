#!/usr/bin/env bash
# Validate PR head_ref and base_ref against Gitflow branching policy (OP-5.3, AC-4).
set -euo pipefail

HEAD_REF=""
BASE_REF=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --head-ref|--head)
      HEAD_REF="$2"
      shift 2
      ;;
    --base-ref|--base)
      BASE_REF="$2"
      shift 2
      ;;
    *)
      if [[ -z "$HEAD_REF" ]]; then
        HEAD_REF="$1"
        shift
      elif [[ -z "$BASE_REF" ]]; then
        BASE_REF="$1"
        shift
      else
        echo "check-gitflow-policy: Unexpected argument '$1'." >&2
        exit 1
      fi
      ;;
  esac
done

if [[ -z "$HEAD_REF" || -z "$BASE_REF" ]]; then
  echo "Usage: check-gitflow-policy.sh --head-ref <head> --base-ref <base>" >&2
  exit 1
fi

# Clean potential refs/heads/ prefixes
HEAD_REF="${HEAD_REF#refs/heads/}"
BASE_REF="${BASE_REF#refs/heads/}"

# Semver regex for release/hotfix branches: 3 numeric components
SEMVER_RE='^[0-9]+\.[0-9]+\.[0-9]+$'

# Evaluate Gitflow Matrix:
# 1. feature/* -> develop
# 2. feature/* -> release/X.Y.Z (Option A release preparation / bump)
# 3. release/X.Y.Z -> master or develop
# 4. hotfix/X.Y.Z -> master or develop
accepted=false

if [[ "$HEAD_REF" =~ ^feature/ ]]; then
  if [[ "$BASE_REF" == "develop" ]]; then
    accepted=true
  elif [[ "$BASE_REF" =~ ^release/(.*)$ ]]; then
    rel_ver="${BASH_REMATCH[1]}"
    if [[ "$rel_ver" =~ $SEMVER_RE ]]; then
      accepted=true
    fi
  fi
elif [[ "$HEAD_REF" =~ ^release/(.*)$ ]]; then
  rel_ver="${BASH_REMATCH[1]}"
  if [[ "$rel_ver" =~ $SEMVER_RE ]]; then
    if [[ "$BASE_REF" == "master" || "$BASE_REF" == "develop" ]]; then
      accepted=true
    fi
  fi
elif [[ "$HEAD_REF" =~ ^hotfix/(.*)$ ]]; then
  hotfix_ver="${BASH_REMATCH[1]}"
  if [[ "$hotfix_ver" =~ $SEMVER_RE ]]; then
    if [[ "$BASE_REF" == "master" || "$BASE_REF" == "develop" ]]; then
      accepted=true
    fi
  fi
fi

if [[ "$accepted" == "true" ]]; then
  echo "check-gitflow-policy: Accepted PR pair '${HEAD_REF}' -> '${BASE_REF}'."
  exit 0
else
  echo "check-gitflow-policy error: Rejected PR pair '${HEAD_REF}' -> '${BASE_REF}' per Gitflow policy." >&2
  exit 1
fi
