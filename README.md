# DoorSpy Camera



WiFi door camera built on a Raspberry Pi Zero W. It does no video processing on the CPU:
one camera feeds the Pi's two hardware encoders. An ESP32 dashboard shows live MJPEG, and a
Pi 4 records the H.264 stream (and will run motion and face recognition).

```
                ┌──────────────── Pi Zero W "doorspy" ─────────────────┐
OV5647 ──► ISP ─┬─ main 1280×768 ─► HW H.264 ≤2.5 Mbit ─► MediaMTX ────┼──► Pi 4: Frigate / Home Assistant (planned)
                │                                         RTSP + WebRTC ┼──► VLC / browser
                └─ lores 800×480 ─► HW JPEG ~3 Mbit (on demand) ─► HTTP ┼──► ESP32-P4 dashboard
                                    + snapshot (software JPEG)          │
                └──────────────────────────────────────────────────────┘
```

## Features

- **Snapshot**: `/api/snapshot.jpeg`, 800×480, about 45 KB, served in 0.2–0.35 s. Meant for the dashboard's 5 s polling.
- **Live MJPEG**: `/api/stream.mjpeg`, 800×480 at about 15 fps and about 3 Mbit/s. The encoder runs only while at least one client is connected.
- **H.264 stream**: 1280×768 at 15 fps, up to 2.5 Mbit/s, with a keyframe every 2 s. Served over RTSP for recording and over WebRTC for viewing in a browser, without re-encoding.
- **Headless**: there's nothing to do on the Pi itself. It's set up over SSH, through WiFi or the USB gadget fallback.
- **PIR gate**: an SR602 motion sensor can switch Frigate's recording, motion detection and person detection (with face recognition) on only while someone is at the door. Each of the three is "always", "pir" or "off" (part of `pi-zero/doorspy_io.py`, see [configuration](docs/configuration.md#frigate-gate)).
- **Web UI**: `https://doorspy.local:8443/` (Basic auth, self-signed certificate) tunes the picture live with a preview, changes stream settings, drives the power LED and WS2812, sets the PIR / Frigate modes, shows status and logs, and restarts services. See [configuration](docs/configuration.md#web-ui).
- **Lights with a job**: the power LED can light up on the PIR (own brightness and hold), and the WS2812 can show status on top of its idle colour/animation: red while the PIR hold is active and recording is on, yellow for a person, green for a recognised face (colour, animation and speed per indicator, Test buttons in the UI).
- **Self-healing services**: systemd restarts the camera service whenever MediaMTX restarts or the RTSP output fails.
- **Reproducible**: `tools/deploy.sh` reinstalls everything from this repo.

## Endpoints

The address is `192.168.0.216`, given out by the router's DHCP. Reserve it in the router. `doorspy.local` works too, through mDNS.

Port 8080 needs HTTP Basic auth: the stream login **`guest` / `password`** (for stream clients) or the web UI's admin login. Change both on the web UI's System tab. Clients send `Authorization: Basic Z3Vlc3Q6cGFzc3dvcmQ=` (base64 of `guest:password`).

| URL | What | Notes |
|---|---|---|
| `http://doorspy.local:8080/api/snapshot.jpeg` | single JPEG | Login `guest`. Quality 80. If a stream is running, it returns the latest stream frame instead. |
| `http://doorspy.local:8080/api/stream.mjpeg` | `multipart/x-mixed-replace` | Login `guest`. Boundary `frame`. Each part has `Content-Length`. |
| `http://doorspy.local:8080/` | test page | The MJPEG stream shown in an `<img>`. |
| `https://doorspy.local:8443/` | **config web UI** | Basic auth, default `admin` / `password` (change it on the System tab). Self-signed certificate, accept it once. Snapshot and stream are also served here, behind the login. |
| `rtsp://doorspy.local:8554/cam` | H.264 over RTSP | UDP or TCP. Use this for Frigate and VLC. |
| `http://doorspy.local:8889/cam` | WebRTC player page | Opens in a browser with low delay. |

Login: `ssh pispy@doorspy.local` with a key. See [docs/setup.md](docs/setup.md#access).

## Measured performance

These numbers are from 2026-10-19, with the Pi on 2.4 GHz channel 3 at −39 dBm.

| | |
|---|---|
| WiFi TCP upload (Pi → LAN) | 13.1 Mbit/s |
| UDP at 5 Mbit/s | 0% loss, 1.9 ms jitter |
| H.264, 30 s capture | 442 frames (14.7 fps), largest gap 67 ms, about 1.2 Mbit/s with a static scene |
| MJPEG | 14.4 fps, 2.9 Mbit/s, about 25 KB per frame |
| CPU idle, H.264 only / with an MJPEG client | about 43% / about 24% |
| Throttling / temperature | `0x0` (none) / about 42 °C |

Peak use, with H.264, an MJPEG client and snapshots together, is about 6 Mbit/s of the roughly 13 Mbit/s available.

## ESP32 dashboard

- **ESP32-P4**: its hardware JPEG decoder shows the full-screen MJPEG at the full 15 fps without noticeable delay. This is the recommended board.
- **ESP32-S3**: decodes in software, managing about 10–12 fps at 800×480. Use it for snapshots only, or lower the MJPEG resolution.
- **Polling**: fetch `snapshot.jpeg` every 5 s, and open `stream.mjpeg` only while the full-screen view is visible. Closing the connection stops the encoder on the Pi.
- **Login**: send `Authorization: Basic <base64 of guest:password>` with every request (`esp_http_client_set_authtype(..., HTTP_AUTH_TYPE_BASIC)` + username/password in the config does this).

## Repository

```
pi-zero/              everything that runs on the camera
  camera.py           Picamera2 service: H.264 → MediaMTX, MJPEG + snapshot HTTP server (:8080)
  webui.py, www/      config web UI on :8443 (HTTPS, Basic auth), served by camera.py
  doorspy_io.py       one MQTT service: power LED + RGB lights, button events, PIR, Frigate gate (doorspy.env);
                      control socket /run/doorspy/io.sock for the web UI
  mediamtx.yml        MediaMTX config (RTSP + WebRTC only)
  wifi-watchdog.sh    reconnects WiFi if NetworkManager gave up on it
  systemd/            mediamtx.service, doorspy-cam.service, doorspy-io.service, doorspy-wifi-watchdog.timer
  install.sh          installs packages, MediaMTX, files and services on the Pi (idempotent)
stl/                  printed case (peephole mount)
  doorspy-housing-body.stl           housing, 36×93.5×20.3 mm
  doorspy-housing-backplate.stl      back plate / lid with snap latch, 4 screw holes
  doorspy-housing-led-cover.stl      covers the inset of the LED diffuser
  doorspy-housing-cam-cover.stl      camera cover with a smaller lens hole, adds some depth to the flat front
  peephole-hollow-bolt-M12-56mm.stl  through-door hollow bolt: ø20 flange, ø14.3 shaft, M12, ø5 cable bore
  peephole-hollow-bolt-M12-nut.stl   matching M12 nut, ø20×5 mm
tools/
  deploy.sh           pushes pi-zero/ to the Pi over ssh and runs install.sh
  windows/            USB-gadget helpers for a Windows PC (RNDIS driver, internet sharing)
pi4/
  frigate/            Frigate 0.18.2 on acidpi4: docker-compose.yml, config/config.yml, seed-faces.sh
docs/
  setup.md            from a blank SD card to a running camera, plus troubleshooting
  configuration.md    every setting and where it lives
  pi4.md              Frigate and Home Assistant on the Pi 4
CHANGELOG.md          what changed when, newest first
```

## Status / roadmap

- [x] Headless Pi Zero W over WiFi and USB gadget
- [x] Snapshot, on-demand MJPEG, H.264 over RTSP and WebRTC
- [x] PIR gate software (tested with a simulated sensor)
- [x] GPIO assignment: PIR GPIO17, button GPIO26 (active-high switch, tested), power LED GPIO13 (hardware PWM1, 10 kHz, MOSFET) + LED controller EN GPIO18 (on/off), WS2812 GPIO10 (SPI), see [configuration](docs/configuration.md#gpio-assignment)
- [x] SR602 tested (2026-10-20): `binary_sensor.doorspy_pir` holds motion for 60 s. Optionally switch person detection to "pir" on the web UI's Sensors tab
- [x] `doorspy-io` tested on the device (2026-10-20): lights, button `press`/`long_press`, PIR over one MQTT connection
- [x] Power LED turns on with the PIR
- [x] Web UI (camera, lights, sensors, Frigate modes, system) — 2026-10-20: API tested on the device over IPv6 and IPv4 (controls, reconfigure, lights, Frigate gate round-trip, password change); the page itself still needs a look in a browser
- [x] Software for the button, power LED and WS2812 (WS2812 data-line fault fixed in hardware)
- [ ] Pi 4: Frigate for motion-triggered recording and person detection, linked to Home Assistant (config ready, see [docs/pi4.md](docs/pi4.md))
- [x] Face recognition: Frigate built-in, small model, runs on the Pi 4 (about 175 ms per face)
- [x] Face recognition end to end: seeded with 6 snapshots, first walk-by recognised as "Sascha" at 90% (single attempts 87–92%)
- [ ] Train more photos per person (5–10, different lighting) and add other people
- [ ] ESP32-P4 dashboard firmware (this PlatformIO project)
