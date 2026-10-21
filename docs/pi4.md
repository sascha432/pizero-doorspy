# Pi 4: Frigate and Home Assistant

The Pi 4 is `acidpi4` (192.168.0.4): Debian 13 on aarch64, 4 cores, 7.6 GB of RAM, booting from a USB SSD, with a 1.8 TB encrypted RAID1 at `/media/1`. It already runs Docker (`homeassistant`, `nodered`, `zigbee2mqtt`) and Mosquitto on the host, port 1883.

Frigate pulls the Zero's H.264 stream **once**. It records only when there's motion and detects people. It then serves live view, recordings and events to Home Assistant and to any number of viewers, so none of that goes over the Zero's WiFi.

## Files: `pi4/frigate/` → `/opt/frigate`

| File | What |
|---|---|
| `docker-compose.yml` | Frigate 0.18.2 container. Ports, volumes, and the MQTT connection to the host's Mosquitto. |
| `config/config.yml` | The Frigate config: camera, recording, detection, face recognition. |

## Install

1. **Unlock the RAID** if it isn't already, and create the recordings folder **on it**:
   ```bash
   mountpoint /media/1 && mkdir -p /media/1/frigate
   ```
   If `/media/1/frigate` doesn't exist, Frigate refuses to start (`create_host_path: false`). That's on purpose: otherwise a locked RAID would mean it records onto the SSD.

2. **MQTT:** the host's Mosquitto has `allow_anonymous true` (listeners 1883, 1887, 9001), so Frigate connects without a login through `host.docker.internal:1883`.

3. **Copy the files over and start Frigate.** From this repo on the PC (Git Bash):
   ```bash
   tar -C pi4/frigate -cf - . | ssh root@acidpi4 "mkdir -p /opt/frigate && tar -C /opt/frigate -xf -"
   ```
   Then on the Pi 4:
   ```bash
   cd /opt/frigate && docker compose up -d && docker logs -f frigate
   ```
   On the first start, the log shows the **admin password** for the web interface.

4. **Web interface:** `https://acidpi4:8971`. The certificate is self-signed, so accept it. Log in as `admin` with the password from the log.

## Home Assistant

1. Check how the Home Assistant container is networked:
   ```bash
   docker inspect homeassistant -f '{{.HostConfig.NetworkMode}}'
   ```
   On acidpi4 it's `host`, so Home Assistant reaches Frigate's API at `http://127.0.0.1:5000`. That port isn't exposed to the LAN.
2. If it isn't set up already, add the **MQTT** integration (broker `127.0.0.1`). zigbee2mqtt already uses it.
3. **Frigate integration**: it isn't built into Home Assistant, and this setup doesn't use HACS. Install it by hand into `/home/sascha/hass/custom_components/frigate`, run `docker restart homeassistant`, then go to Settings → Devices & services → Add integration → **Frigate**, with the URL `http://127.0.0.1:5000` and no login:
   ```bash
   V=5.15.6; HA_CFG=$(docker inspect homeassistant -f '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}')
   cd /tmp && curl -fsSL -o fhi.tgz https://github.com/blakeblackshear/frigate-hass-integration/archive/refs/tags/v$V.tar.gz && tar xzf fhi.tgz     && rm -rf "$HA_CFG/custom_components/frigate" && mkdir -p "$HA_CFG/custom_components"     && cp -r frigate-hass-integration-$V/custom_components/frigate "$HA_CFG/custom_components/"     && rm -rf fhi.tgz frigate-hass-integration-$V && docker restart homeassistant
   ```
   It doesn't update itself. To update, run the same commands with a new `V`.
4. That gives you `camera.doorspy`, person sensors, event and recording browsing, and notification blueprints.

## Ports on acidpi4

| Port | What |
|---|---|
| 8971/tcp | Frigate web interface and API, with login |
| 5000/tcp | API without login, **localhost only**, for Home Assistant |
| 8554/tcp | RTSP restream `rtsp://acidpi4:8554/doorspy`, for VLC and other viewers |
| 8555/tcp+udp | WebRTC live view |

## Settings in `config.yml`

| Setting | Value | Notes |
|---|---|---|
| `go2rtc.streams.doorspy` | `rtsp://192.168.0.216:8554/cam` | The Zero's MediaMTX. It's the only place the Zero's address appears. |
| `detect` | `enabled: true`, 1280×768 at 5 fps | **Must be switched on explicitly, because 0.18 defaults to off.** Full stream size, so faces stay big enough to recognise. Lower it, e.g. to 640×384, if the CPU is overloaded. |
| `detectors.cpu.num_threads` | 3 | Leaves one core for Home Assistant and the rest. |
| `objects.track` | `person` | Add more objects (e.g. `car`, `dog`) as needed. |
| `record` | motion 3 days; alerts and detections 30 days | Only footage with motion is kept. For round-the-clock recording, add `continuous: {days: N}`. |
| recordings / motion / detect switches | driven by the Zero | The per-camera switches that Frigate shows in its UI and Home Assistant are set over MQTT by `doorspy-io` on the Zero: each "always", "pir" (on with the door's PIR) or "off", chosen in the Zero's web UI. See [configuration](configuration.md#frigate-gate). Flipping them by hand in Frigate or HA gets overridden at the next change or MQTT reconnect. |
| `snapshots.retain.default` | 30 days | |
| `face_recognition.enabled` | `false` | See below. |

## Face recognition

**It works on the Pi 4**, despite the docs' AVX note. The `small` model uses YuNet (OpenCV) to find the face and FaceNet as TFLite to recognise it, and both run on ARM. Tested 2026-10-19: about **175 ms per face**, with no crashes.

The setting in `config.yml` is `face_recognition: {enabled: true, model_size: small}`. Faces are only searched for inside a detected `person`, and must be at least `min_area: 750` pixels (about 27×27).

### Getting started: seed the library first

As long as **no face** is trained yet, Frigate saves **nothing** under Recent Recognitions, even though it detects faces. In `frigate/data_processing/real_time/face.py`, `classify()` returns `None` when the library is empty, and `process_frame()` exits before `write_face_attempt()`. So each new person needs a few starting photos:

- **From existing events (recommended):** `pi4/frigate/seed-faces.sh NAME ['GLOB']` on acidpi4, in `/opt/frigate`. It crops the face out of every clean event snapshot (`clips/doorspy-*-clean.webp`) with YuNet, the same detector Frigate uses, saves the crops under `clips/faces/NAME/`, and restarts Frigate. Use `GLOB` to limit it to events that show only that person, then check the crops in the Face Library.
- **Or by hand:** Frigate → Face Library → **Add face** → name → upload a photo.

Then:

1. Have the person walk past the camera. Recent Recognitions fills up, with at most 12 attempts per person event (`MAX_FACE_ATTEMPTS`). Check with `ls /media/1/frigate/clips/faces/train/`; the file names end in `-NAME-SCORE.webp`.
2. Assign **5–10** good photos to the name: front-facing, sharp, with different lighting. From 5 photos on, Frigate filters out bad matches.

Tested 2026-10-19: 6 seed photos from the event snapshots, then 12 attempts saved on the first walk-by, and the event recognised as **Sascha at 90%** (single attempts 87–92%, one turned-away face "unknown"). YuNet on the Pi 4 finds faces in the 1280×768 snapshots with scores of 0.77–0.89.

Recognised names become the person event's `sub_label` (once `recognition_threshold` reaches 0.9), and show up in Home Assistant through the Frigate integration.

**Orientation:** the face detector needs roughly upright faces, so mount the camera the right way round.

## Measured (2026-10-19, idle scene)

- Stream: 5.0 fps at the detect rate, connection "excellent", 0 reconnects or stalls per hour.
- CPU: the ffmpeg decode takes about 23% of one core; Frigate as a whole about 16% of the system.
- Shared memory: Frigate asks for at least 148 MB, so `shm_size` is 256 MB.
- Person detection on the CPU: **about 104 ms** per check, with the system at about 49% CPU during detection.
- Face recognition: about 175 ms per face.
- RAID `/media/1`: 182 GB free out of 1.9 TB (as of 2026-10-19).

## Load on the Pi 4 (expected)

- **Decoding:** the 1280×768 at 15 fps stream is decoded in software, at about 30–50% of one core. Hardware decoding on the Pi 4 needs the Raspberry Pi kernel, and the Pi 4 runs a standard Debian one.
- **Detection:** 5 fps on the CPU, while there's motion.
- **Storage:** about 0.5–1 GB per day with motion-only recording at about 1.2 Mbit/s. That depends heavily on how much happens in front of the door.
