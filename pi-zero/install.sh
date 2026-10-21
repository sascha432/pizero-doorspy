#!/bin/bash
# Install or update the doorspy camera on a Raspberry Pi running Raspberry Pi OS (trixie).
# Run from the pi-zero/ directory on the Pi:  sudo ./install.sh
# Safe to re-run: packages and MediaMTX are only fetched when missing or outdated.
set -euo pipefail

MTX_VERSION=v1.21.2
RUN_USER=${SUDO_USER:-pispy}
cd "$(dirname "$0")"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo "== packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    python3-picamera2 python3-av python3-simplejpeg python3-numpy \
    python3-gpiozero python3-lgpio python3-paho-mqtt python3-spidev openssl

echo "== mediamtx $MTX_VERSION"
case "$(uname -m)" in
    armv6l)  MTX_ARCH=armv6 ;;
    armv7l)  MTX_ARCH=armv7 ;;
    aarch64) MTX_ARCH=arm64 ;;
    *) echo "unsupported arch $(uname -m)"; exit 1 ;;
esac
if [ "$(/opt/mediamtx/mediamtx --version 2>/dev/null)" != "$MTX_VERSION" ]; then
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/mtx.tgz" \
        "https://github.com/bluenviron/mediamtx/releases/download/$MTX_VERSION/mediamtx_${MTX_VERSION}_linux_${MTX_ARCH}.tar.gz"
    install -d /opt/mediamtx
    tar -xzf "$tmp/mtx.tgz" -C /opt/mediamtx
    rm -rf "$tmp"
fi

echo "== files (services run as $RUN_USER)"
install -d /opt/doorspy /etc/doorspy
install -m 755 camera.py /opt/doorspy/camera.py
install -m 644 webui.py /opt/doorspy/webui.py
install -d /opt/doorspy/www
install -m 644 www/index.html /opt/doorspy/www/index.html
install -m 755 doorspy_io.py /opt/doorspy/doorspy_io.py
install -m 755 wifi-watchdog.sh /opt/doorspy/wifi-watchdog.sh
[ -f /etc/doorspy/doorspy.env ] || install -m 644 doorspy.env /etc/doorspy/doorspy.env   # keep local edits
# PIR_GATE=1 (old single gate) -> GATE_DETECT=pir; doorspy_io.py maps it until the UI saves the file

echo "== web UI (https://$(hostname).local:8443/)"
# /var/lib/doorspy: what the UI writes (camera.json, web.auth); owned by the camera service's
# user so it can replace files atomically. /etc/doorspy stays root's (env, mediamtx, cert).
STATE=/var/lib/doorspy
install -d -o "$RUN_USER" "$STATE"
for f in camera.json web.auth stream.auth; do   # moved here from /etc/doorspy on 2026-10-20
    [ -f /etc/doorspy/$f ] && [ ! -s "$STATE/$f" ] && mv -f /etc/doorspy/$f "$STATE/$f"; rm -f /etc/doorspy/$f
done
[ -f "$STATE/camera.json" ] || : > "$STATE/camera.json"
# web.auth: user:salt:pbkdf2-sha256 (50000 rounds, same as webui.py). Default admin / password, like the ssh login.
if [ ! -s "$STATE/web.auth" ]; then
    python3 - > "$STATE/web.auth" <<'EOF'
import hashlib, secrets
salt = secrets.token_bytes(16)
print(f"admin:{salt.hex()}:{hashlib.pbkdf2_hmac('sha256', b'password', salt, 50000).hex()}")
EOF
    echo "web login created: admin / password (the setup default) - change it on the System tab"
fi
# stream.auth: the :8080 login for stream clients (MJPEG + snapshot). Default guest / password.
if [ ! -s "$STATE/stream.auth" ]; then
    python3 - > "$STATE/stream.auth" <<'EOF'
import hashlib, secrets
salt = secrets.token_bytes(16)
print(f"guest:{salt.hex()}:{hashlib.pbkdf2_hmac('sha256', b'password', salt, 50000).hex()}")
EOF
    echo "stream login created: guest / password (port 8080) - change it on the System tab"
fi
chown "$RUN_USER" "$STATE/camera.json" "$STATE/web.auth" "$STATE/stream.auth"; chmod 600 "$STATE/web.auth" "$STATE/stream.auth"
# self-signed EC P-256 certificate, 10 years; delete web.key to get a new one
if [ ! -f /etc/doorspy/web.key ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
        -keyout /etc/doorspy/web.key -out /etc/doorspy/web.crt -subj "/CN=$(hostname).local" \
        -addext "subjectAltName=DNS:$(hostname).local,DNS:$(hostname),IP:$(hostname -I | awk '{print $1}')" 2>/dev/null
fi
chown "$RUN_USER" /etc/doorspy/web.key /etc/doorspy/web.crt; chmod 600 /etc/doorspy/web.key
# pir.py / lights.py were merged into doorspy_io.py (one MQTT connection)
for old in doorspy-pir doorspy-lights; do
    systemctl disable --now $old.service 2>/dev/null || true
    rm -f /etc/systemd/system/$old.service
done
rm -f /opt/doorspy/pir.py /opt/doorspy/lights.py
install -m 644 mediamtx.yml /etc/doorspy/mediamtx.yml
for unit in systemd/*.service systemd/*.timer; do
    sed "s/^User=.*/User=$RUN_USER/; s/^Environment=SOCKET_USER=.*/Environment=SOCKET_USER=$RUN_USER/" \
        "$unit" > "/etc/systemd/system/$(basename "$unit")"
done

echo "== wifi power saving off"
cat > /etc/NetworkManager/conf.d/wifi-powersave-off.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF

echo "== faster boot"
# cloud-init only matters on the first boot, but it runs on every boot and costs about 27 s.
touch /etc/cloud/cloud-init.disabled
# The USB gadget connection from cloud-init (netplan-eth0) is backed by netplan: on every start
# NetworkManager rewrites its YAML and runs netplan generate + daemon-reload three times, about
# 37 s on the Zero, before it touches wlan0. Replace it with a plain keyfile.
cat > /etc/NetworkManager/system-connections/usb0.nmconnection <<'EOF'
[connection]
id=usb0
uuid=3f0c1a52-6d8e-4c1b-9a0e-7d2b5e1f0a01
type=ethernet
interface-name=usb0

[ipv4]
method=auto
route-metric=700
may-fail=true

[ipv6]
method=auto
route-metric=700
EOF
chmod 600 /etc/NetworkManager/system-connections/usb0.nmconnection
if ls /etc/netplan/*.yaml >/dev/null 2>&1; then
    rm -f /etc/netplan/*.yaml
    netplan generate 2>/dev/null || true
fi
nmcli connection reload

echo "== boot config (GPIO: SPI for WS2812, hardware PWM for the power LED)"
BOOTCFG=/boot/firmware/config.txt
BLOCK=$(cat <<'EOF'
# doorspy begin (managed by install.sh)
[all]
dtparam=audio=off
# WS2812 on SPI0 MOSI (GPIO10). The SPI clock divides the core clock, so pin the core
# clock at its normal 400 MHz instead of letting it drop to 250 MHz at idle.
dtparam=spi=on
core_freq_min=400
# Power LED: hardware PWM1 on GPIO13 (Alt0) -> MOSFET, active high, 10 kHz
dtoverlay=pwm,pin=13,func=4
# LED controller EN on GPIO18: plain output, active high, held low (off) from boot on
gpio=18=op,dl
# Serial console on GPIO14/15 (pins 8/10), 115200 baud, for rescue when the network is down
enable_uart=1
# doorspy end
EOF
)
if [ "$(sed -n '/^# doorspy begin/,/^# doorspy end/p' "$BOOTCFG")" != "$BLOCK" ]; then
    sed -i '/^# doorspy begin/,/^# doorspy end/d; s/^dtparam=audio=on$/#dtparam=audio=on  (doorspy: off, see block below)/' "$BOOTCFG"
    printf '\n%s\n' "$BLOCK" >> "$BOOTCFG"
    echo "boot config changed: reboot required"
    touch /run/doorspy-reboot-required
fi

echo "== services"
systemctl daemon-reload
systemctl enable mediamtx.service doorspy-cam.service doorspy-io.service
systemctl restart doorspy-io.service
systemctl enable --now doorspy-wifi-watchdog.timer
systemctl restart mediamtx.service   # PartOf= restarts doorspy-cam with it
for _ in $(seq 30); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/snapshot.jpeg)" = 401 ] && break   # up, wants the login
    sleep 2
done
systemctl is-active mediamtx.service doorspy-cam.service doorspy-io.service
# Frigate functions in "pir" mode are off while it's quiet; keep them "always" until the sensor is wired
grep -Eq '^(GATE_[A-Z]+=pir|PIR_GATE=1)' /etc/doorspy/doorspy.env && echo "doorspy-io: Frigate PIR gate active (see the web UI, Sensors tab)"
# 401 = up and asking for the login (the guest password may have been changed since)
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/snapshot.jpeg)
[ "$code" = 401 ] || [ "$code" = 200 ] || { echo "camera not answering: journalctl -u doorspy-cam"; exit 1; }
rc=0; curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/ 2>/dev/null | grep -q 401 || rc=1
[ $rc -eq 0 ] || echo "web UI not answering on :8443: journalctl -u doorspy-cam"
echo "done: https://$(hostname).local:8443/  http://$(hostname).local:8080/  rtsp://$(hostname).local:8554/cam"
