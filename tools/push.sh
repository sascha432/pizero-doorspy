#!/bin/bash
# Quick update: copy only the doorspy scripts / web page to the Pi and restart their services.
# No apt, no MediaMTX, no boot config - for that use tools/deploy.sh.
# Usage (Git Bash):  tools/push.sh [user@host] [script ...]     default: all scripts
#   tools/push.sh pispy@192.168.0.216 doorspy_io.py
#   tools/push.sh pispy@doorspy.local www/index.html      (no restart needed)
set -euo pipefail

TARGET=${1:-pispy@192.168.0.216}
shift || true
SRC="$(cd "$(dirname "$0")/../pi-zero" && pwd)"
SUDO_PASS=${DOORSPY_SUDO_PASS:-password}
FILES=("$@")
[ ${#FILES[@]} -gt 0 ] || FILES=(camera.py webui.py www/index.html doorspy_io.py)

declare -A SERVICE=([camera.py]=doorspy-cam [webui.py]=doorspy-cam [doorspy_io.py]=doorspy-io)

for f in "${FILES[@]}"; do
    svc=${SERVICE[$f]:-}
    ssh "$TARGET" "cat > /tmp/$(basename "$f")" < "$SRC/$f"
    printf '%s\n' "$SUDO_PASS" | ssh "$TARGET" "sudo -S -p '' install -D -m 755 /tmp/$(basename "$f") /opt/doorspy/$f"
    if [ -n "$svc" ]; then
        printf '%s\n' "$SUDO_PASS" | ssh "$TARGET" "sudo -S -p '' systemctl restart $svc; systemctl is-active $svc" | sed "s/^/$f -> $svc: /"
    else
        echo "$f copied"
    fi
done
