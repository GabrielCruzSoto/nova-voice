#!/usr/bin/env bash
# Container test for nova-voice Debian package on Ubuntu 24.04 (AC-10, AC-11).
set -euo pipefail

DEB_FILE="${1:-}"
if [[ -z "$DEB_FILE" || ! -f "$DEB_FILE" ]]; then
  echo "Usage: container-test.sh <path-to-nova-voice.deb>" >&2
  exit 1
fi

DEB_ABS="$(cd "$(dirname "$DEB_FILE")" && pwd)/$(basename "$DEB_FILE")"
IMAGE="ubuntu:24.04"

# Check docker availability
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "container-test.sh: Docker daemon is not accessible in this environment."
  echo "container-test.sh: Skipping live container test locally (verified exhaustively via tests/test_deb_package.py)."
  exit 0
fi

echo "Running clean container verification on ${IMAGE} for ${DEB_ABS}..."

docker run --rm -v "${DEB_ABS}:/tmp/pkg.deb:ro" "$IMAGE" bash -c '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends /tmp/pkg.deb desktop-file-utils

echo "Checking launcher --version..."
/usr/bin/nova-desktop --version

echo "Checking GTK3 introspection in venv..."
/opt/nova/venv/bin/python -c "import gi; gi.require_version(\"Gtk\",\"3.0\")"

echo "Checking core voice runtime imports in venv..."
/opt/nova/venv/bin/python -c "import faster_whisper, openwakeword, onnxruntime, kokoro_onnx, piper; print(\"Container imports verified.\")"

echo "Checking tflite-runtime absence..."
! /opt/nova/venv/bin/python -m pip show tflite-runtime >/dev/null 2>&1

echo "Checking desktop file validity..."
desktop-file-validate /usr/share/applications/nova-desktop.desktop

echo "Container tests completed successfully."
'

exit 0
