#!/usr/bin/env bash
# Build reproducible Debian package for nova-voice (OP-4, AC-9, NFR-1, NFR-3).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MODE="pr"
OUTPUT_DIR="${REPO_ROOT}/dist"
SOURCE_DATE_EPOCH_PARAM=""
PIP_BIN_PARAM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --source-date-epoch)
      SOURCE_DATE_EPOCH_PARAM="$2"
      shift 2
      ;;
    --pip-bin)
      PIP_BIN_PARAM="$2"
      shift 2
      ;;
    *)
      echo "build.sh: Unknown argument '$1'." >&2
      exit 1
      ;;
  esac
done

if [[ "$MODE" != "pr" && "$MODE" != "release" ]]; then
  echo "build.sh: Mode must be 'pr' or 'release', got '$MODE'." >&2
  exit 1
fi

# 1. Determine Source Date Epoch
if [[ -n "$SOURCE_DATE_EPOCH_PARAM" ]]; then
  SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH_PARAM"
elif [[ -n "${SOURCE_DATE_EPOCH:-}" ]]; then
  SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}"
else
  SOURCE_DATE_EPOCH="$(git -C "$REPO_ROOT" log -1 --format=%ct HEAD 2>/dev/null || date +%s)"
fi
export SOURCE_DATE_EPOCH

# 2. Derive Package Version
cd "$REPO_ROOT"
PROJECT_VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"

if [[ "$MODE" == "pr" ]]; then
  COMMIT_SHA7="$(git -C "$REPO_ROOT" rev-parse --short=7 HEAD 2>/dev/null || echo "0000000")"
  DEB_VERSION="${PROJECT_VERSION}+git${COMMIT_SHA7}"
else
  DEB_VERSION="${PROJECT_VERSION}"
fi

echo "Building nova-voice Debian package version ${DEB_VERSION} (mode: ${MODE}, epoch: ${SOURCE_DATE_EPOCH})..."

# Find workspace root containing repositories/nova if in worktree
WORKSPACE_ROOT=""
check_dir="$REPO_ROOT"
while [[ "$check_dir" != "/" ]]; do
  if [[ -d "${check_dir}/repositories/nova" ]]; then
    WORKSPACE_ROOT="$check_dir"
    break
  fi
  check_dir="$(dirname "$check_dir")"
done

# 3. Build UI Assets
if command -v npm >/dev/null 2>&1; then
  if [[ -d "${REPO_ROOT}/node_modules/.bin" ]]; then
    export PATH="${REPO_ROOT}/node_modules/.bin:${PATH}"
  elif [[ -n "$WORKSPACE_ROOT" && -d "${WORKSPACE_ROOT}/repositories/nova/node_modules/.bin" ]]; then
    export PATH="${WORKSPACE_ROOT}/repositories/nova/node_modules/.bin:${PATH}"
  fi
  echo "Compiling UI TypeScript assets..."
  npm run build:ui
fi

if [[ ! -f "${REPO_ROOT}/src/nova/ui/static/app.js" ]]; then
  echo "build.sh error: UI asset '${REPO_ROOT}/src/nova/ui/static/app.js' was not generated." >&2
  exit 1
fi

# 4. Locate Pip
PIP_BIN=""
if [[ -n "$PIP_BIN_PARAM" && -x "$PIP_BIN_PARAM" ]]; then
  PIP_BIN="$PIP_BIN_PARAM"
elif [[ -n "${PIP_BOOTSTRAP_BIN:-}" && -x "$PIP_BOOTSTRAP_BIN" ]]; then
  PIP_BIN="$PIP_BOOTSTRAP_BIN"
elif [[ -x "${REPO_ROOT}/.venv/bin/pip" ]]; then
  PIP_BIN="${REPO_ROOT}/.venv/bin/pip"
elif [[ -n "$WORKSPACE_ROOT" && -x "${WORKSPACE_ROOT}/repositories/nova/.venv/bin/pip" ]]; then
  PIP_BIN="${WORKSPACE_ROOT}/repositories/nova/.venv/bin/pip"
elif command -v pip >/dev/null 2>&1; then
  PIP_BIN="$(command -v pip)"
elif command -v pip3 >/dev/null 2>&1; then
  PIP_BIN="$(command -v pip3)"
fi

if [[ -z "$PIP_BIN" ]]; then
  echo "build.sh: pip binary not found. Set --pip-bin or PIP_BOOTSTRAP_BIN." >&2
  exit 1
fi

# 5. Prepare Staging Tree
STAGING_DIR="$(mktemp -d -t nova-deb-staging-XXXXXX)"
trap 'rm -rf "${STAGING_DIR}"' EXIT

mkdir -p "${STAGING_DIR}/DEBIAN"
mkdir -p "${STAGING_DIR}/opt/nova"
mkdir -p "${STAGING_DIR}/usr/bin"
mkdir -p "${STAGING_DIR}/usr/share/applications"
mkdir -p "${STAGING_DIR}/usr/share/doc/nova-voice"

VENV_TARGET="${STAGING_DIR}/opt/nova/venv"

SYS_PYTHON="python3"
if [[ -x "/usr/bin/python3" ]]; then
  SYS_PYTHON="/usr/bin/python3"
fi

echo "Creating isolated virtual environment at ${VENV_TARGET} using ${SYS_PYTHON}..."
if ! "$SYS_PYTHON" -m venv --system-site-packages "$VENV_TARGET" 2>/dev/null; then
  "$SYS_PYTHON" -m venv --without-pip --system-site-packages "$VENV_TARGET"
fi

echo "Installing runtime dependencies and nova-voice..."
"$PIP_BIN" --python "${VENV_TARGET}/bin/python" install --prefer-binary -r "${SCRIPT_DIR}/constraints.txt" >/dev/null
"$PIP_BIN" --python "${VENV_TARGET}/bin/python" install --no-deps "openwakeword==0.6.0" >/dev/null
"$PIP_BIN" --python "${VENV_TARGET}/bin/python" install --no-deps "$REPO_ROOT" >/dev/null

# 6. Hygiene: remove pyc, pycache, models, and leaks
echo "Cleaning byte-code, models, and build host references..."
find "${STAGING_DIR}" -type f -name "*.pyc" -delete
find "${STAGING_DIR}" -type d -name "__pycache__" -delete
find "${STAGING_DIR}" -type f -name "direct_url.json" -delete

# Ensure /opt/nova/models is absent and standalone voice models are excluded
rm -rf "${STAGING_DIR}/opt/nova/models" "${STAGING_DIR}/models"
find "${STAGING_DIR}" -maxdepth 4 -name "silero_vad.onnx" -delete 2>/dev/null || true
find "${STAGING_DIR}" -name "oye_nova.onnx" -delete 2>/dev/null || true
find "${STAGING_DIR}" -name "es_ES-davefx-medium*" -delete 2>/dev/null || true

# Sanitize pyvenv.cfg
cat > "${VENV_TARGET}/pyvenv.cfg" << 'EOF'
home = /usr/bin
include-system-site-packages = true
version = 3.12.3
executable = /usr/bin/python3.12
command = /usr/bin/python3 -m venv --system-site-packages /opt/nova/venv
EOF

# Normalize interpreter symlinks inside venv so there is no host toolcache leakage
ln -snf /usr/bin/python3.12 "${VENV_TARGET}/bin/python3"
ln -snf python3 "${VENV_TARGET}/bin/python"
ln -snf python3 "${VENV_TARGET}/bin/python3.12"

# Sanitize shebangs in /opt/nova/venv/bin
for binfile in "${VENV_TARGET}/bin"/*; do
  if [[ -f "$binfile" ]] && head -n 1 "$binfile" | grep -q "${STAGING_DIR}"; then
    sed -i "1s|^#!.*python.*|#!/opt/nova/venv/bin/python3|" "$binfile"
  fi
done

# 7. Install Wrappers
cat > "${STAGING_DIR}/usr/bin/nova-desktop" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec /opt/nova/venv/bin/nova-desktop "$@"
EOF
chmod 0755 "${STAGING_DIR}/usr/bin/nova-desktop"

cat > "${STAGING_DIR}/usr/bin/nova" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec /opt/nova/venv/bin/nova "$@"
EOF
chmod 0755 "${STAGING_DIR}/usr/bin/nova"

# 8. Install Desktop Entry
cp "${SCRIPT_DIR}/nova-desktop.desktop" "${STAGING_DIR}/usr/share/applications/nova-desktop.desktop"
chmod 0644 "${STAGING_DIR}/usr/share/applications/nova-desktop.desktop"

# 9. Generate Debian Changelog and Copyright
RFC_DATE="$(date -u -d "@${SOURCE_DATE_EPOCH}" -R 2>/dev/null || date -u -R)"
TMP_CHANGELOG="$(mktemp)"
cat > "$TMP_CHANGELOG" << EOF
nova-voice (${DEB_VERSION}) noble; urgency=medium

  * Release ${DEB_VERSION}.

 -- Gabriel Cruz Soto <dev@nova.local>  ${RFC_DATE}
EOF

gzip -9n < "$TMP_CHANGELOG" > "${STAGING_DIR}/usr/share/doc/nova-voice/changelog.Debian.gz"
rm -f "$TMP_CHANGELOG"
chmod 0644 "${STAGING_DIR}/usr/share/doc/nova-voice/changelog.Debian.gz"

cp "${SCRIPT_DIR}/copyright" "${STAGING_DIR}/usr/share/doc/nova-voice/copyright"
chmod 0644 "${STAGING_DIR}/usr/share/doc/nova-voice/copyright"

# 10. Install Maintainer Scripts
cp "${SCRIPT_DIR}/postinst" "${STAGING_DIR}/DEBIAN/postinst"
cp "${SCRIPT_DIR}/prerm" "${STAGING_DIR}/DEBIAN/prerm"
cp "${SCRIPT_DIR}/postrm" "${STAGING_DIR}/DEBIAN/postrm"
chmod 0755 "${STAGING_DIR}/DEBIAN/postinst" "${STAGING_DIR}/DEBIAN/prerm" "${STAGING_DIR}/DEBIAN/postrm"

# 11. Calculate Installed Size and Render Control
INSTALLED_SIZE_KB="$(du -sk --apparent-size --exclude=DEBIAN "${STAGING_DIR}" 2>/dev/null | cut -f1 || du -sk "${STAGING_DIR}" | cut -f1)"

sed -e "s/__DEB_VERSION__/${DEB_VERSION}/g" \
    -e "s/__INSTALLED_SIZE_KB__/${INSTALLED_SIZE_KB}/g" \
    "${SCRIPT_DIR}/control.template" > "${STAGING_DIR}/DEBIAN/control"
chmod 0644 "${STAGING_DIR}/DEBIAN/control"

# 12. Set Deterministic Timestamps & Permissions
find "${STAGING_DIR}" -type d -exec chmod 0755 {} +
find "${STAGING_DIR}" -type f -exec touch -d "@${SOURCE_DATE_EPOCH}" -h {} +
find "${STAGING_DIR}" -type d -exec touch -d "@${SOURCE_DATE_EPOCH}" {} +

# 13. Build Debian Archive
mkdir -p "$OUTPUT_DIR"
DEB_FILENAME="nova-voice_${DEB_VERSION}_amd64.deb"
DEB_PATH="${OUTPUT_DIR}/${DEB_FILENAME}"

echo "Building final .deb archive with dpkg-deb..."
dpkg-deb --root-owner-group -Zxz --build "${STAGING_DIR}" "${DEB_PATH}"

# 14. Validate Size Budget (NFR-1: <= 300 MiB)
ACTUAL_SIZE="$(stat -c %s "${DEB_PATH}")"
MAX_SIZE=$((300 * 1024 * 1024))
if [[ "$ACTUAL_SIZE" -gt "$MAX_SIZE" ]]; then
  echo "build.sh error: Built .deb size (${ACTUAL_SIZE} bytes) exceeds 300 MiB limit!" >&2
  exit 1
fi

# 15. Generate SHA256 Checksum
(cd "$OUTPUT_DIR" && sha256sum "$DEB_FILENAME" > "${DEB_FILENAME}.sha256")

echo "Successfully built ${DEB_PATH} ($(numfmt --to=iec-i --suffix=B "${ACTUAL_SIZE}"))."
exit 0
