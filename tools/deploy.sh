#!/bin/bash
# Push pi-zero/ to the camera and run its installer.
# Usage (Git Bash / Linux / macOS):  tools/deploy.sh [user@host]
# sudo on the Pi uses the setup's default password; override with DOORSPY_SUDO_PASS=...
# The image has no SFTP server, so files go over plain ssh + tar instead of scp.
set -euo pipefail

TARGET=${1:-pispy@doorspy.local}
SUDO_PASS=${DOORSPY_SUDO_PASS:-password}
SRC="$(cd "$(dirname "$0")/../pi-zero" && pwd)"

tar -C "$SRC" -cf - . | ssh "$TARGET" "rm -rf /tmp/doorspy && mkdir -p /tmp/doorspy && tar -C /tmp/doorspy -xf -"
printf '%s\n' "$SUDO_PASS" | ssh "$TARGET" "sudo -S -p '' bash /tmp/doorspy/install.sh"
