#!/usr/bin/env bash
# Install git hooks idempotently into repository hooks directory.
set -euo pipefail

HOOKS_DIR="${1:-}"
if [[ -z "$HOOKS_DIR" ]]; then
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "install-hooks: Not inside a git repository." >&2
    exit 1
  fi
  HOOKS_DIR="$(git rev-parse --git-path hooks 2>/dev/null || echo "$(git rev-parse --git-common-dir)/hooks")"
fi

mkdir -p "$HOOKS_DIR"

COMMIT_MSG_TARGET="${HOOKS_DIR}/commit-msg"
MARKER="# Managed by nova-voice install-hooks.sh"

if [[ -f "$COMMIT_MSG_TARGET" ]]; then
  if ! grep -Fq "$MARKER" "$COMMIT_MSG_TARGET"; then
    echo "install-hooks: Refusing to overwrite existing unmanaged commit-msg hook at '$COMMIT_MSG_TARGET'." >&2
    exit 1
  fi
fi

TMP_HOOK="${COMMIT_MSG_TARGET}.tmp.$$"
cat > "$TMP_HOOK" << 'EOF'
#!/usr/bin/env bash
# Managed by nova-voice install-hooks.sh
set -euo pipefail

commit_msg_file="$1"
first_line="$(head -n 1 "$commit_msg_file")"

# Skip merge commits and fixup!/squash! commits
if [[ "$first_line" =~ ^Merge[[:space:]] || "$first_line" =~ ^(fixup|squash)![[:space:]] ]]; then
  exit 0
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
  echo "commit-msg: 'cz' (commitizen) is required but not found in .venv/bin/cz or PATH." >&2
  echo "Please install commitizen: pip install commitizen (or .venv/bin/pip install -e '.[dev]')" >&2
  exit 1
fi

exec "$cz_bin" check --commit-msg-file "$commit_msg_file" --message-length-limit 100
EOF

chmod 0755 "$TMP_HOOK"

if [[ -f "$COMMIT_MSG_TARGET" ]] && cmp -s "$TMP_HOOK" "$COMMIT_MSG_TARGET"; then
  rm -f "$TMP_HOOK"
  echo "install-hooks: Hooks already up-to-date at '$HOOKS_DIR'."
  exit 0
fi

mv "$TMP_HOOK" "$COMMIT_MSG_TARGET"
echo "install-hooks: Installed commit-msg hook into '$HOOKS_DIR'."
