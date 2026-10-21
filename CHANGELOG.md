# Changelog

Newest first. Dates are when the change landed on the camera. "Deploy" means `tools/deploy.sh`; most entries only needed `tools/push.sh`.

## 2026-10-20 — web UI, Frigate gating, lights with a job

### Added
- **Web UI** on `https://doorspy.local:8443/` (HTTPS, Basic auth, default `admin` / `password`): `pi-zero/webui.py` + `pi-zero/www/index.html`, served by `camera.py` next to the unchanged `:8080`.
  - *Camera*: live preview, brightness / contrast / saturation / sharpness / EV, white balance, noise reduction, auto or manual exposure; stream settings (resolution preset, fps, H.264 and MJPEG bitrate, snapshot quality, flips) applied by reconfiguring the pipeline in about 1 s while clients stay connected. Settings persist in `/var/lib/doorspy/camera.json`.
  - *Lights*: power LED and WS2812 (colour, brightness, mode solid / rainbow / pulsing / flashing with a per-mode speed), power LED on PIR trigger (own brightness and hold), WS2812 status indicator (see below), Test buttons.
  - *Sensors*: PIR state, last button event, MQTT state, Frigate modes, `doorspy.env` editor (keeps comments, restarts `doorspy-io`).
  - *System*: temperature, throttling flags, WiFi, memory, services; restart / reboot; `journalctl` tail; change the admin and stream logins.
- **Local control socket** `/run/doorspy/io.sock` in `doorspy_io.py` for the UI (state, lights, env edits, service actions, indicator test) — no MQTT round trips for anything local, no sudo rules.
- **Frigate gate per function**: `GATE_CAMERA`, `GATE_RECORD`, `GATE_MOTION`, `GATE_DETECT`, each `always` / `pir` / `off`, replacing `PIR_GATE`. `GATE_CAMERA` drives Frigate's *enabled* switch, the only one that stops its ffmpeg and the RTSP pull from the Zero — `camera = pir` is what keeps the Pi 4 idle while nobody is at the door. Rules enforced: nothing more "on" than the camera, detect not more than motion. Wanted state is republished on every MQTT connect.
- **WS2812 status indicator** (`RGB_INDICATOR`): red while the PIR hold is active and recording is on (decided locally), yellow while Frigate tracks a person, green once that person has a recognised face; face > person > recording; person/face linger `INDICATOR_HOLD` s after Frigate ends the object. Colour, animation and speed per indicator.
- **Power LED on motion** (`POWER_ON_MOTION`, `POWER_MOTION_BRIGHTNESS`, `POWER_MOTION_HOLD`), independent of `PIR_HOLD`; a higher Home Assistant brightness wins.
- **CPU temperature** on MQTT: `doorspy/temperature` every `TEMP_INTERVAL` s, Home Assistant sensor by discovery.
- **Stream login** for port 8080 (`/var/lib/doorspy/stream.auth`, default `guest` / `password`): MJPEG, snapshot and the test page now need Basic auth; the admin login works there too.
- Light state (`/var/lib/doorspy/lights.json`) survives `doorspy-io` restarts, which every Save causes.
- `tools/push.sh` knows `webui.py` and `www/index.html`.

### Changed
- Both HTTP servers are **dual-stack**: `doorspy.local` resolves to the IPv6 address first on most clients and a plain `HTTPServer` only listened on IPv4, so by name `:8080` was unreachable while MediaMTX worked.
- The web server speaks HTTP/1.1 (keep-alive): one TLS handshake per browser connection instead of one per request; system status is cached (WiFi 30 s, rest 3 s); the Camera tab polls a cheap metadata endpoint.
- Files the services write moved from `/etc/doorspy` (root) to `/var/lib/doorspy` (the camera service's `StateDirectory`), so they can be replaced atomically.
- `doorspy-cam.service` gets the `systemd-journal` group (log tail) and starts after `doorspy-io`; `doorspy-io.service` gets `RuntimeDirectory=doorspy` and `SOCKET_USER`.
- Picking a colour no longer ends the rainbow; the mode select is the only thing that changes the mode.
- `install.sh` seeds the two logins and a self-signed EC P-256 certificate when missing; its health check accepts `401` from `:8080`.

### Fixed
- UI painting stale state over a click (toggles flipping back), and Save un-greying before the restarted `doorspy-io` was back: polls now yield to user actions, and Save waits until the state reports the saved values.
- Person indicator "never" showing: Frigate reports a tracked object once, not continuously, so a per-message timeout was wrong; indicators now follow the tracked object.
- A credential stayed valid in the cache after its password file was replaced from outside.
- Recording "stopped" when `record = always` with `camera = pir`: a disabled camera records nothing; now rejected by the rules above.

### Notes
- Face recognition is **not** suitable as the only factor for unlocking a door: no liveness check, and the anonymous MQTT broker lets anyone on the LAN publish a fake recognition (this was demonstrated during testing).
- Pi 4 cost of the gate: with `camera = pir`, about 13 s from PIR trigger to the first detection.

## 2026-10-20 (earlier) — I/O service merged

- `pir.py` and `lights.py` merged into `doorspy_io.py`: one MQTT connection, one availability topic; power LED (GPIO13 PWM1 10 kHz + EN on GPIO18, gamma 2.2), WS2812 on SPI0, button on GPIO26 as an HA event, PIR on GPIO17 as a binary sensor held `PIR_HOLD` s.
- Boot config block in `config.txt` managed by `install.sh` (`dtparam=spi=on`, `core_freq_min=400`, PWM overlay, `gpio=18=op,dl`, `enable_uart=1`).
- Power LED test script (`tests/led-ramp.py`), γ = 2.2 confirmed by eye; button on GPIO26 tested active-high.

## 2026-10-19 — Frigate and face recognition

- Frigate 0.18.2 on the Pi 4 (`pi4/frigate/`): go2rtc pulls the Zero's stream once; person detection on the CPU (about 104 ms per check); recording of motion only.
- Face recognition with the small model works on the Pi 4 (about 175 ms per face); seeded from event snapshots with `seed-faces.sh`, first walk-by recognised at 90 %.
- PIR gate for detection tested with a simulated sensor.
- Measurements: WiFi 13.1 Mbit/s upload, H.264 14.7 fps, MJPEG 14.4 fps at 2.9 Mbit/s, CPU 43 % idle.

## Before — camera and setup

- Pi Zero W with OV5647: one Picamera2 pipeline feeding the hardware H.264 encoder (1280×768, 15 fps, to MediaMTX for RTSP and WebRTC) and the hardware MJPEG encoder on demand (800×480) plus a software snapshot.
- Headless setup over WiFi or the USB gadget, WiFi watchdog timer, fast boot (cloud-init off, netplan removed), `tools/deploy.sh` for reproducible installs.
- Printed peephole case (`stl/`).
