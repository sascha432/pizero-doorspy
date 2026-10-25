# Configuration

Most settings are changed in the **web UI** at `https://doorspy.local:8443/` (see [Web UI](#web-ui)); they're stored on the Pi and survive a deploy. The defaults and everything else live in the files in `pi-zero/`: edit them, then run `tools/deploy.sh`. Don't edit those on the Pi, because the next deploy overwrites them.

## Web UI

Served by `camera.py` (module `pi-zero/webui.py`, page `pi-zero/www/index.html`) on **port 8443, HTTPS with Basic auth**. The plain `:8080` endpoints keep working but need the stream login (`stream.auth`, below). The stream and snapshot are also served on `:8443` behind the login, so the page can show the live preview.

| File on the Pi | Owner | What |
|---|---|---|
| `/var/lib/doorspy/web.auth` | `pispy`, mode 600 | `user:salt:pbkdf2-sha256`. Created by `install.sh` as **`admin` / `password`** (the same default as the ssh login); change it on the System tab. Delete the file and redeploy to reset it. |
| `/var/lib/doorspy/stream.auth` | `pispy`, mode 600 | Same format: the login for **port 8080** (MJPEG, snapshot, test page) and **RTSP / WebRTC** (MediaMTX), meant for stream clients. Created as **`guest` / `password`**; change it on the System tab (needs your admin password). The admin login is accepted on 8080, RTSP and WebRTC as well; the guest login is not accepted on 8443. |
| `/etc/doorspy/web.crt`, `web.key` | `pispy` | Self-signed EC P-256 certificate for `doorspy.local` and the current IP, 10 years. Delete `web.key` and redeploy for a new one. The browser warns once; accept it. |
| `/var/lib/doorspy/lights.json` | root | Last state of the power LED and RGB (on/off, brightness, colour, effect), written by `doorspy-io` on every command and restored when it starts — every Save on the Lights/Sensors tabs restarts it. |
| `/var/lib/doorspy/camera.json` | `pispy` | Stream settings and image controls set in the UI. Overrides `DEFAULTS` in `camera.py`. Empty = defaults. (`/var/lib/doorspy` is the service's `StateDirectory`, so it can replace files there atomically; `/etc/doorspy` stays root's.) |
| `/etc/doorspy/doorspy.env` | root | The I/O settings (below). The UI rewrites it through `doorspy-io`, keeping comments. |
| `/run/doorspy/io.sock` | root:`pispy`, 660 | Control socket of `doorspy-io` for the UI: lights, state, env edits, service restarts (one JSON line per request). |

Tabs and what they change:

| Tab | Applies | Needs |
|---|---|---|
| **Camera**: brightness, contrast, saturation, sharpness, EV, white balance, noise reduction, auto/manual exposure | live, `camera.json` | — |
| **Camera → Stream**: resolution, fps, bitrates, snapshot quality, flips | reconfigures the pipeline, about 3 s; RTSP and MJPEG clients stay connected and resume | — |
| **Lights**: power LED, WS2812 | at once; the state is also published to MQTT, so Home Assistant stays in sync | `doorspy-io` |
| **Sensors**: PIR / button state, Frigate modes, `doorspy.env` keys | rewrites `doorspy.env` and restarts `doorspy-io` | `doorspy-io` |
| **System**: temperature, throttling, WiFi, memory, services; restart / reboot; log tail; password | restarts go through `doorspy-io` (root); the log tail needs the `systemd-journal` group, which the unit sets | `doorspy-io` for restarts |

With `doorspy-io` down, the Camera tab still works and the others say so. The API behind the page is listed at the top of `webui.py`; `curl -k -u admin:password https://doorspy.local:8443/api/config` dumps everything.

## Camera service: `pi-zero/camera.py`

On the Pi this is installed as `/opt/doorspy/camera.py` and runs as `doorspy-cam.service`. The values below are `DEFAULTS`; `/var/lib/doorspy/camera.json` (written by the web UI) overrides them.

| Setting | Default | Meaning |
|---|---|---|
| `main_size` | `[1280, 768]` | Size of the H.264 stream. It's 5:3, like `lores_size`. See [aspect ratio](#aspect-ratio). |
| `lores_size` | `[800, 480]` | Size of the snapshot and MJPEG. It must be no larger than `main_size` in either dimension. |
| `fps` | `15` | Frame rate of the camera, which every output shares. The sensor manages 46 fps at full field of view, but more fps means more WiFi and CPU. |
| `h264_bitrate` | `2_500_000` | Upper limit for H.264. It's variable bitrate, so a static scene uses about 1.2 Mbit/s. |
| `mjpeg_bitrate` | `6_000_000` | **The target, not the actual rate.** The VC4 MJPEG encoder lands at about half of it (see below). |
| `snapshot_quality` | `80` | JPEG quality (1–100) for snapshots taken while no stream is running. |
| `hflip`, `vflip` | `false` | Mirror / turn the picture by 180° (both on). See [orientation](#orientation). |
| `controls` | `{}` | libcamera controls applied live: `Brightness` −1..1, `Contrast`, `Saturation` 0..4, `Sharpness` 0..8, `ExposureValue` −8..8, `AwbMode` 0–6, `NoiseReductionMode` 0–3, `AeEnable`, and with AE off `ExposureTime` (µs) and `AnalogueGain`. The full list is `CONTROLS` in `camera.py`. |
| `RTSP_URL` | `rtsp://127.0.0.1:8554/cam` | Where the H.264 is published (the local MediaMTX). Constant. |
| `HTTP_PORT` | `8080` | Port for `/api/snapshot.jpeg`, `/api/stream.mjpeg` and `/`, Basic auth with `stream.auth` or `web.auth`. Constant; the UI is on `8443` (`webui.py`). |

Also set in `Camera._configure`:

- `SENSOR_SIZE = (1296, 972)`: the sensor's 2×2 binned mode, which uses the full field of view. The 1920×1080 mode is a cropped centre area.
- `buffer_count=4`: kept low to save memory on the 512 MB board.

### MJPEG bitrate vs quality

Picamera2's `MJPEGEncoder` uses the H.264 block's MJPEG mode (`/dev/video11`), not the separate JPEG encoder (`/dev/video31`). So it can only be given a **target bitrate**, not a quality level, and on mostly static scenes it falls well short of that target:

| `MJPEG_BITRATE` | Actual | Per frame | Picture |
|---|---|---|---|
| 3 M | 1.5 Mbit/s | about 11 KB | blocky, blotchy colour |
| 6 M (default) | 2.9 Mbit/s | about 25 KB | good |
| 8 M | 4.0 Mbit/s | about 34 KB | very good |
| 15 M | 7.2 Mbit/s | about 60 KB | no visible gain |

Because the stream is only encoded while full screen is open, 3 Mbit/s is affordable. For a 2 Mbit/s limit, set it to about 4 M.

### Snapshot path

- **With no stream running:** the snapshot grabs one 800×480 YUV frame and encodes it in software with `simplejpeg` (libjpeg-turbo). That costs nothing between requests.
- **With a stream running:** it returns the newest hardware-encoded MJPEG frame, so it never competes with the stream.

### Aspect ratio

On the Pi Zero's image processor, the main and lores outputs are both scaled from the **same crop** of the sensor. If they had different shapes, one of them would come out stretched. That's why the H.264 is 1280×768 instead of 1280×720. To get real 720p, set `MAIN_SIZE = (1280, 720)` and accept that the 800×480 picture gets squashed by about 7% horizontally.

### Orientation

libcamera can only flip the picture (in steps of 180°). To turn it upside down, tick both flips on the Camera tab (`hflip` + `vflip` in `camera.json`).

The image processor **can't rotate by 90°**. Doing it in software would use up the Zero's CPU. If you need a portrait view, mount the camera the right way round, or rotate it on the display side; the ESP32 can do that in its display driver or LVGL.

## MediaMTX: `pi-zero/mediamtx.yml`

On the Pi this is installed as `/etc/doorspy/mediamtx.yml`, and the program lives in `/opt/mediamtx`. The version is pinned by `MTX_VERSION` in `install.sh` (currently `v1.21.2`, built for ARMv6).

| Setting | Value | Why |
|---|---|---|
| `rtsp`, `rtspTransports` | on, `[udp, tcp]` | Port 8554, plus 8000/8001 for UDP RTP/RTCP. |
| `webrtc` | on | Port 8889 for the page and signalling, 8189/UDP for media. It passes the H.264 through without re-encoding. |
| `rtmp`, `hls`, `srt`, `moq` | off | Not needed. HLS would also cost CPU and memory on the Zero. |
| `authMethod`, `authHTTPAddress` | `http`, `http://127.0.0.1:8081/mediamtx/auth` | MediaMTX asks `camera.py` about every client: publishing only from localhost, watching only with the stream or admin login (the files above), so a password changed on the System tab applies to RTSP and WebRTC at once. The check runs in a child process of `camera.py`, listening on localhost only, because PyAV blocks the camera process while it connects the very RTSP output MediaMTX asks about. |
| `paths.cam` | `source: publisher` | The stream `camera.py` publishes to. |

Each RTSP or WebRTC viewer adds another copy of the stream, about 1–2.5 Mbit/s each, to the WiFi link. Once the Pi 4 is set up, have it pull the stream **once** and let it serve the other viewers.

## Services: `pi-zero/systemd/`

| Unit | Runs | Behaviour |
|---|---|---|
| `mediamtx.service` | `/opt/mediamtx/mediamtx /etc/doorspy/mediamtx.yml` | `Restart=always` |
| `doorspy-cam.service` | `python3 -u /opt/doorspy/camera.py` | `Requires=` and `PartOf=mediamtx`, so restarting MediaMTX restarts the camera. `After=doorspy-io` so the control socket exists, but not required. `Restart=always`. If the RTSP output fails, the process exits on purpose so it restarts cleanly. |
| `doorspy-io.service` | `python3 -u /opt/doorspy/doorspy_io.py` as root | `EnvironmentFile=/etc/doorspy/doorspy.env`, `RuntimeDirectory=doorspy` for the control socket, `SOCKET_USER` = the camera's user. `Restart=always`. |
| `doorspy-wifi-watchdog.timer` | `/opt/doorspy/wifi-watchdog.sh`, 3 min after boot and then every 2 min, as root | If `wlan0` is neither connected nor connecting, runs `nmcli device connect wlan0`. A WPA handshake that times out at boot makes NetworkManager mark `FoxSay2` as "no secrets" and never retry it; this clears that. Tested: recovered from a forced disconnect in 7 s. |

Both run as the user who ran `install.sh` (`pispy`), with the `video` and `render` groups for access to the camera and encoders.

## Network and system (outside this repo)

| What | Where | Value |
|---|---|---|
| WiFi connections | NetworkManager (`nmcli con`) | `FoxSay2` only. See [setup](setup.md#4-wifi). |
| WiFi power saving | `/etc/NetworkManager/conf.d/wifi-powersave-off.conf` (written by `install.sh`), plus each connection's `wifi.powersave 2` | off. On the Zero W it causes lag and dropouts. |
| USB gadget | `bootfs/config.txt` `dtoverlay=dwc2`, `cmdline.txt` `modules-load=dwc2,g_ether` | `usb0` uses DHCP through the keyfile `/etc/NetworkManager/system-connections/usb0.nmconnection` (route metric 700, so WiFi stays the default route), written by `install.sh`. |
| WiFi country | `cmdline.txt` `cfg80211.ieee80211_regdom=CA` | Channels 1–11 only. |
| Hostname / mDNS | cloud-init `user-data` (first boot only), `avahi-daemon` | `doorspy.local` |
| cloud-init | `/etc/cloud/cloud-init.disabled` (written by `install.sh`) | off after the first boot. It ran on every boot, cost about 27 s and applied nothing. |
| netplan | `/etc/netplan/` (emptied by `install.sh`) | unused. With cloud-init's netplan-backed `netplan-eth0`, NetworkManager ran `netplan generate` + `daemon-reload` three times per start, about 37 s, before bringing up `wlan0`. |
| GPIO boot settings | `/boot/firmware/config.txt`, block `# doorspy begin … end` (written by `install.sh`, which reports when a reboot is needed) | `dtparam=audio=off`, `dtparam=spi=on`, `core_freq_min=400`, `dtoverlay=pwm,pin=13,func=4` (PWM1 on GPIO13), `gpio=18=op,dl` (EN output, off at boot), `enable_uart=1` (serial console on GPIO14/15, 115200 baud) |

## GPIO assignment

The case holds 1 button, 1 power LED (dimmable), 1 WS2812 and the SR602 PIR. Everything connects to the **J8** header, the 40-pin GPIO header (2 × 20, unpopulated on the Zero W). The board also has a 2-pin **RUN** header (RUN, GND), which isn't used here. Header pin = physical pin number on J8.

| Function | GPIO (BCM) | Header pin (J8) | Hardware | Connection |
|---|---|---|---|---|
| PIR SR602 | GPIO17 | 11 | input, internal pull-down | `+` → pin 1 (3.3 V), `OUT` → pin 11, `−` → pin 9 (GND) |
| Button | GPIO26 | 37 | input, **active high**; the switch drives the pin itself (3.3 V pressed / 0 V released, push-pull); internal pull-down only as a fallback | switch OUT → pin 37; switch GND → a GND pin (e.g. 39); power the switch from **3.3 V**, not 5 V |
| Power LED (brightness) | GPIO13 | 33 | **hardware PWM1**, **10 kHz** (`/sys/class/pwm/pwmchip0/pwm1`, Alt0) | pin 33 → **MOSFET gate, active high** (high = LED on); GND → pin 34 |
| LED controller EN | GPIO18 | 12 | **plain output, active high**, no PWM | pin 12 → **EN** of the LED controller; GND → pin 14 |
| WS2812 | GPIO10 | 19 | **SPI0 MOSI** (`/dev/spidev0.0`) | DIN ← **330 Ω** ← pin 19; VDD and GND see below |

```
 J8 header (physical pin numbers in brackets, pin 1 = square pad)
          3V3  (1) (2)  5V ── 1N4148 ──► WS2812 VDD (≈4.3 V), 100 nF to GND
          GPIO3 (5) (6)  GND
          GND  (9) (10)
   PIR GPIO17 (11) (12) GPIO18 ── EN  LED controller (on/off, active high)
               (13) (14) GND ── GND LED controller
          ...
 WS2812 GPIO10 (19) (20) GND ── WS2812 GND
          ...
 LED MOSFET ── GPIO13 (33) (34) GND
          ...
 switch OUT ── GPIO26 (37) (38)
 switch GND ── GND    (39) (40)
```

Why these pins:

- **GPIO0/1** are the HAT ID EEPROM, and the camera's I²C runs on GPIO28/29 through the `i2c0mux`, so those stay free. **GPIO14/15** (UART) stay free for a serial console. With SPI on, **GPIO7–11** belong to SPI0, but only MOSI (GPIO10) is wired.
- **Button on GPIO26:** the switch has an **active push-pull output** (3.3 V = pressed, 0 V = released), not a contact to GND. So it's read as active high: `gpiozero.Button(26, pull_up=False)`. The internal pull-down doesn't fight the switch; it only keeps the pin low if the switch is unplugged. A `Button(26)` with the default pull-up reads it **inverted**, as found in testing. The pin tolerates only 3.3 V, so never power the switch from 5 V.
- **Power LED: two pins with separate jobs.**
  - **GPIO18 = EN** of the LED controller, a plain active-high output. It's switched **once on** before the LED is used and **once off** afterwards. **Never PWM it**: an earlier version drove EN with PWM, and that input didn't work above 120 Hz.
  - **GPIO13 = brightness**, through a MOSFET, active high: duty cycle = brightness. It's **hardware PWM1 at 10 kHz** (period 100000 ns), with no CPU load and no visible flicker, even on camera.
  - **Order:** brightness to 0 → EN on → dim → brightness to 0 → EN off.
  - **At boot:** `gpio=18=op,dl` holds EN low (off) from the firmware stage onwards. GPIO13 has its default pull-down, so the MOSFET is off too, and nothing flashes.
- **WS2812 on SPI instead of PWM/DMA:** it needs no root and no DMA channel, and doesn't clash with the power LED's PWM. Each data bit is sent as 3 SPI bits at about 2.4 MHz. The SPI clock is divided from the **core clock**, which normally drops from 400 to 250 MHz at idle, and that would wreck the WS2812 timing. Hence `core_freq_min=400`.
- **Audio off:** the Zero has no analog audio and the camera has no microphone. Turning it off also keeps the PWM block free.

**WS2812 levels:** with VDD at 5 V, the WS2812 wants at least 0.7 × VDD = 3.5 V for a "high" on DIN, more than the Pi's 3.3 V. With one LED, the simplest fix is to supply VDD from 5 V through a **1N4148 diode** (giving about 4.3 V, so the threshold drops to about 3.0 V), plus 100 nF close to the LED. Alternatively, use a level shifter (74AHCT125) and supply it from the full 5 V.

**Currents:** according to Raspberry Pi, the 3.3 V supply was designed for **about 3 mA per GPIO pin**. The pads can be configured up to 16 mA, but that isn't meant as a continuous load on every pin. The power LED itself runs from the LED controller; GPIO18 only drives its EN input and GPIO13 a MOSFET gate, and neither takes noticeable current. The WS2812 runs from 5 V (up to about 60 mA at full white), not from a GPIO.

### Testing the power LED (EN once on, 10 kHz ramp 0→100% in 10 s)

Test script: [`pi-zero/tests/led-ramp.py`](../pi-zero/tests/led-ramp.py). After a deploy it's on the Pi at `/tmp/doorspy/tests/led-ramp.py`; run it with `sudo python3 … [seconds] [gamma]`. Ctrl+C turns brightness to 0 and EN off at any point.

Tested 2026-10-20: EN on GPIO18 and **10 kHz PWM on GPIO13 work**. A **linear** duty cycle doesn't look linear, though: the eye sees brightness roughly logarithmically, so the ramp seems to jump at the start and barely change at the top. The script therefore maps brightness through a **gamma curve**, duty = (brightness/100)^2.2 (`gamma` 1 = linear, for comparison):

| Brightness | 5 % | 10 % | 20 % | 50 % | 70 % | 100 % |
|---|---|---|---|---|---|---|
| Duty (γ 2.2) | 0.14 % | 0.63 % | 2.9 % | 21.8 % | 45.6 % | 100 % |

**γ = 2.2 confirmed** (2026-10-20): it looks much more even than linear, so it's the default for the LED service. Below about 5 % brightness the high time is only a few hundred ns. Whether the MOSFET and LED controller still follow cleanly there has to be checked by eye.

The same steps by hand, linear:

```bash
P=/sys/class/pwm/pwmchip0
sudo pinctrl set 13 a0                     # GPIO13 -> PWM1 (only needed without the new boot config)
sudo sh -c "[ -d $P/pwm1 ] || { echo 1 > $P/export; sleep 0.5; }
  echo 0 > $P/pwm1/enable; echo 0 > $P/pwm1/duty_cycle; echo 100000 > $P/pwm1/period; echo 1 > $P/pwm1/enable"
sudo pinctrl set 18 op dh                  # EN on - once
sudo sh -c "for i in \$(seq 0 1000); do echo \$((i*100)) > $P/pwm1/duty_cycle; sleep 0.01; done"
sudo sh -c "echo 0 > $P/pwm1/duty_cycle"; sudo pinctrl set 18 op dl   # LED 0, EN off - once
```

Set `duty_cycle` to 0 first, then the period: the kernel rejects a period shorter than the current duty cycle.

### Testing the button and power LED together

The LED is on at 100% while the switch is pressed. Pressed = 3.3 V on GPIO26, so `pull_up=False`:

```python
# sudo python3 ...  (/sys/class/pwm belongs to root)
import os, subprocess, time; from signal import pause; from gpiozero import Button
PWM = "/sys/class/pwm/pwmchip0"; CH = f"{PWM}/pwm1"; PERIOD = 100_000   # 10 kHz on GPIO13
def w(p, v): open(p, "w").write(str(v))
if not os.path.isdir(CH): w(f"{PWM}/export", 1); time.sleep(0.5)
w(f"{CH}/enable", 0); w(f"{CH}/duty_cycle", 0); w(f"{CH}/period", PERIOD); w(f"{CH}/enable", 1)
subprocess.run(["pinctrl", "set", "13", "a0"]); subprocess.run(["pinctrl", "set", "18", "op", "dh"])  # EN on, once
b = Button(26, pull_up=False, bounce_time=0.03)
b.when_pressed = lambda: w(f"{CH}/duty_cycle", PERIOD)
b.when_released = lambda: w(f"{CH}/duty_cycle", 0)
pause()
```

Tested 2026-10-20 with the earlier wiring (LED through EN on GPIO18): pressed → LED on, released → LED off. `pinctrl get 26` shows `lo` when released and `hi` when pressed. **Not yet re-tested with the GPIO13 MOSFET.**

### Sources

| Claim | Source |
|---|---|
| Header name **J8** (2 × 20), RUN header, pin numbers ↔ GPIO | read on the device: gpiozero `board_info` for revision `9000c1` = "Zero W, PCB 1.1" (2026-10-20) |
| GPIO10 = `SPI0_MOSI`, GPIO17 without a conflicting function | `pinctrl funcs 10,17,18` on the device; after the reboot `pinctrl get` shows GPIO10 = SPI0_MOSI |
| PWM1 on GPIO13 = `pin=13,func=4` (Alt0); analog audio uses both PWM channels | `/boot/firmware/overlays/README`, section `pwm` (on the device): *"PWM1: 13,4(Alt0) 19,2(Alt5) …"* |
| `gpio=18=op,dl` sets a pin as an output, driven low, at firmware boot | Raspberry Pi `config.txt` documentation, `gpio` option. Not yet tested on this device. |
| 3.3 V supply about 3 mA per pin; pads up to 16 mA | [raspberrypi.com documentation, "GPIO pads control"](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html) |
| Camera I²C on `i2c0mux` / `i2c@1` (not GPIO0/1) | device tree path of the sensor in `rpicam-hello --list-cameras`: `/base/soc/i2c0mux/i2c@1/ov5647@36` |
| WS2812 data "high" ≥ 0.7 × VDD | WS2812B datasheet (Worldsemi), VIH spec. Not re-checked here; check the datasheet for your exact LED. |
| SPI clock depends on the core clock; WS2812 over SPI needs a fixed core clock | common practice from the rpi_ws281x project (SPI mode); on this Zero: `vcgencmd get_config core_freq_min` = 0 before, and `measure_clock core` stays at 400 MHz after `core_freq_min=400` |

Tested 2026-10-20 after a reboot (with the earlier PWM-on-GPIO18 config): `pinctrl` shows GPIO10 = SPI0_MOSI; `/dev/spidev0.0` and `pwmchip0` exist; the core clock stays at 400 MHz. The services run as `pispy`, who is in the `spi` and `gpio` groups. The LEDs and the button themselves aren't wired yet, and there's no software for them yet.

## MQTT I/O service: `pi-zero/doorspy_io.py`

Everything on the Zero's GPIOs that talks MQTT runs in **one** process with **one** MQTT connection (client `doorspy-io`) and one availability topic. On the Pi this is `/opt/doorspy/doorspy_io.py` under `doorspy-io.service`, running as root (`/sys/class/pwm` is root-only), with its settings in `/etc/doorspy/doorspy.env`. The installer doesn't overwrite that file, so local edits survive a deploy; the web UI edits it in place (Sensors tab) and restarts the service. It replaces the earlier separate `pir.py` and `lights.py`.

The web UI reaches it over `/run/doorspy/io.sock` (commands `state`, `light`, `env`, `service`), which is also the privileged path for restarting services and rebooting.

Home Assistant discovers all entities by itself (MQTT discovery), under the device **doorspy**:

| Entity | Topics | Hardware |
|---|---|---|
| `light` **Power LED** (on/off, brightness, transition) | `doorspy/light/power/set`, `…/state` (JSON) | GPIO13 PWM1 10 kHz brightness, γ 2.2; GPIO18 EN once on / once off |
| `light` **RGB** (on/off, brightness, colour, effects `rainbow`, `pulse`, `flash`) | `doorspy/light/rgb/set`, `…/state` (JSON) | WS2812 on SPI0 MOSI (GPIO10). Also the Frigate status indicator, see `RGB_INDICATOR`. |
| `event` **Button** (`press`, `long_press`) | `doorspy/button` → `{"event_type": "press"}` | GPIO26, active high |
| `binary_sensor` **PIR** (motion, always on) | `doorspy/pir` `ON`/`OFF`, held `PIR_HOLD` s | SR602 on GPIO17 |
| `sensor` **CPU temperature** (°C) | `doorspy/temperature`, retained, every `TEMP_INTERVAL` s (expires after 3 intervals) | `/sys/class/thermal/thermal_zone0` |
| availability of all of the above | `doorspy/availability` `online`/`offline` (last will) | |

Light commands use HA's JSON schema, e.g. `{"state":"ON","brightness":128,"transition":2}` or `{"state":"ON","color":{"r":255,"g":0,"b":0}}`. A plain `ON`/`OFF` works too.

| Setting (`doorspy.env`) | Default | Meaning |
|---|---|---|
| `MQTT_HOST` / `MQTT_PORT` | `192.168.0.4` / `1883` | Mosquitto on acidpi4 (anonymous) |
| `DEFAULT_TRANSITION` | `0.5` | Fade time in s when HA sends no `transition` |
| `RAINBOW_PERIOD`, `PULSE_PERIOD`, `FLASH_PERIOD` | `6`, `2`, `1` | Seconds per cycle of the RGB effects `rainbow`, `pulse`, `flash`. Home Assistant only picks the effect; the speed is set here / on the Lights tab. |
| `POWER_ON_MOTION`, `POWER_MOTION_BRIGHTNESS`, `POWER_MOTION_HOLD` | `0`, `100`, `30` | Power LED on a PIR trigger: fade to this % and back to the HA state after the hold (s), which is independent of `PIR_HOLD`. A higher HA brightness wins. |
| `RGB_INDICATOR`, `INDICATOR_HOLD` | `0`, `5` | Status indicator on the WS2812, on top of the idle light (shown at the light's brightness even while it's off). **Recording** = while the PIR hold (`PIR_HOLD`) is active and recording is switched on — decided on the Zero, no MQTT round trip. **Person** = while Frigate tracks a person (`frigate/events` new/update/end, `frigate/doorspy/person` count), **face** = while a tracked person has a recognised face (`frigate/tracked_object_update`, or a `sub_label` on the event). Frigate reports a tracked object once, not continuously, so these follow the object rather than timing out; both stay `INDICATOR_HOLD` s after Frigate ends it, and are cleared when the camera or detection is gated off. Face overrides person overrides recording. Each has a Test button on the Lights tab. |
| `RGB_<RECORD\|PERSON\|FACE>_COLOR`, `_MODE`, `_PERIOD` | `ff0000`/`ffff00`/`00ff00`, `solid`, `1` | Colour (hex), animation (`solid`, `pulse`, `flash`) and seconds per cycle of each indicator. |
| `BUTTON_GPIO` / `BUTTON_HOLD` | `26` / `2` | Button pin; `long_press` after this many seconds |
| `PIR_HOLD` | `60` | Motion stays `ON` this many seconds after the last trigger; retriggers extend it without new messages. `0` = raw sensor signal |
| `PIR_GPIO` | `17` | BCM number; GPIO17 is header pin 11. Not editable in the UI (wiring). |
| `GATE_CAMERA`, `GATE_RECORD`, `GATE_MOTION`, `GATE_DETECT` | `always` | Frigate camera enable / recording / motion detection / person detection (+ faces): `always`, `pir` or `off` (see below). **`GATE_CAMERA=pir` is the one that saves CPU on the Pi 4.** |
| `HOLD_SECONDS` | `120` | In `pir` mode: how long the function stays on after the last PIR trigger |
| `FRIGATE_CAMERA` | `doorspy` | Frigate camera name in the MQTT topics |
| `TEMP_INTERVAL` | `60` | Seconds between CPU temperature publishes |
| `PIR_GATE` | — | **Legacy.** `1` with no `GATE_*` keys is read as `GATE_DETECT=pir`; saving from the UI writes the new keys and drops it. |

Quick update of the script alone, with no apt and no boot config: `tools/push.sh pispy@192.168.0.216 doorspy_io.py`.

**Status:** tested on the device on 2026-10-20: the lights work (WS2812 data-line fault fixed in hardware), the button delivers `press` and `long_press`, the PIR holds motion for 60 s, and the power LED turns on with the PIR. The detect part of the gate logic is the version tested on 2026-10-19; the per-function modes and the control socket were added on 2026-10-20 and are untested.

### Frigate gate

Frigate on the Pi 4 has four per-camera switches over MQTT: **enabled** (the camera itself), **recordings**, **motion** detection and object (**person**) **detection**. Face recognition only runs on detected `person`s, so it follows the detect switch. `doorspy-io` drives each of them in one of three modes, set on the Sensors tab:

| Mode | Means |
|---|---|
| `always` | On all the time. Recording then keeps footage per Frigate's own `config.yml` (motion segments; `record.continuous` for 24/7). This is the default and today's behaviour. |
| `pir` | Switched on when the **SR602** sees motion, off again `HOLD_SECONDS` after the last trigger. |
| `off` | Switched off (and kept off, Frigate remembers its switches). |

```
SR602 OUT ─► GPIO17 ─► doorspy_io.py ─► MQTT 192.168.0.4 ─► frigate/doorspy/enabled/set     ON / OFF
                                            │                 frigate/doorspy/recordings/set  ON / OFF
                                            │                 frigate/doorspy/motion/set      ON / OFF
                                            │                 frigate/doorspy/detect/set      ON / OFF
                                            └─► doorspy/pir ON/OFF  (Home Assistant: binary_sensor)
```

**Which switch saves the CPU:** only `enabled`. With the camera enabled, Frigate keeps its ffmpeg capture (decode at the detect fps) and, with `record` configured, the segment-writing ffmpeg running no matter how `recordings`, `motion` and `detect` are set — those only decide what is done with the frames. Disabling the camera stops both ffmpegs, and go2rtc then drops its RTSP pull from the Zero, so the WiFi is free as well. The price is a few seconds after a PIR trigger until Frigate has the stream up again; `HOLD_SECONDS` keeps it running between triggers. The usual setup is therefore **`GATE_CAMERA=pir`** with the other three at `always`.

Rules built into `FrigateGate`:

- **Nothing more on than the camera**: a disabled camera records and detects nothing, so `GATE_RECORD`, `GATE_MOTION` and `GATE_DETECT` can't be "more on" than `GATE_CAMERA` (`always` > `pir` > `off`). Permanent recording therefore needs `GATE_CAMERA=always` too.
- **Detect needs motion**: Frigate refuses to switch motion off while detect is on. So `GATE_DETECT` can't be "more on" than `GATE_MOTION` (`always` > `pir` > `off`); the UI greys the invalid choices out and the `env` command rejects them. Switching on goes camera → motion → detect → recordings, switching off detect → motion → recordings → camera.
- Only **changes** are published, except on every MQTT (re)connect, where the wanted state of all three is sent again: Frigate keeps the switches across its own restarts, so the gate has to start from a known state.
- The `…/state` topics are subscribed, and the UI shows what Frigate reports next to each mode. "no reply from Frigate" means nothing was received since `doorspy-io` started (Frigate down, or the MQTT broker).

### Wiring the SR602 (3.3–15 V, output 3.3 V high)

See [GPIO assignment](#gpio-assignment): `+` → pin 1, `OUT` → pin 11 (GPIO17), `−` → pin 9. The pin order on the SR602 board varies by seller, so check the markings. Its output switches actively between 3.3 V and 0 V, so no level shifter or resistor is needed. GPIO17 additionally has the internal pull-down, which keeps it low if the sensor is unplugged. Each detection holds the output high for about 2.5 s, and the gate stretches that to `HOLD_SECONDS`.

### Enabling it

The PIR **sensor** is always published (`binary_sensor.doorspy_pir`). The SR602 drops its output after about 2.5 s and retriggers constantly, so the service holds `ON` for `PIR_HOLD` (60 s) after the last trigger. Home Assistant sees one `ON` and one `OFF` per visit, not a stream of short pulses.

All three Frigate functions are **`always` by default**: without a working sensor, `pir` would keep them off for good. Once the sensor is tested, switch person detection (and, if wanted, motion detection and recording) to **PIR** on the Sensors tab, or set `GATE_DETECT=pir` in `/etc/doorspy/doorspy.env` and run `sudo systemctl restart doorspy-io`. `journalctl -u doorspy-io -f` should then log `pir: motion` and `frigate: detect ON` when you wave, and `frigate: detect OFF` `HOLD_SECONDS` later.

Setting a function back to `always` switches it on at once; the UI's badges (or `mosquitto_sub -h 192.168.0.4 -v -t 'frigate/doorspy/+/state'`) confirm it.

Tested 2026-10-19 with the then separate `pir.py --simulate` (15 s hold, detect only): ON at motion, OFF after 15 s, confirmed through `frigate/doorspy/detect/state`.
