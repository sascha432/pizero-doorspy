#!/usr/bin/env python3
"""doorspy I/O: everything on the Zero's GPIOs that talks MQTT, over ONE connection.

  Power LED  GPIO13 (J8-33) hardware PWM1 10 kHz -> MOSFET, active high: brightness
             GPIO18 (J8-12) LED controller EN, active high: switched once on when the light turns
             on and once off when it is off again - never pulsed
             POWER_ON_MOTION=1: fades up to POWER_MOTION_BRIGHTNESS % on a PIR trigger and back
             to its HA state POWER_MOTION_HOLD s after the last trigger (independent of PIR_HOLD)
  RGB        WS2812 on SPI0 MOSI (GPIO10, J8-19): brightness, colour, effect rainbow / pulse /
             flash (speed: RAINBOW_PERIOD / PULSE_PERIOD / FLASH_PERIOD s per cycle, web UI only)
             RGB_INDICATOR=1: status on top of the idle light: red while the PIR hold is active
             and recording is on (local, no MQTT round trip); yellow while Frigate tracks a
             person, green once that person has a recognised face, both kept INDICATOR_HOLD s
             after Frigate ends the object (face > person > record)
  Button     GPIO26 (J8-37), switch drives 3.3 V when pressed (active high, push-pull):
             HA event entity with event types "press" and "long_press" (held BUTTON_HOLD s)
  PIR        SR602 on GPIO17 (J8-11): always published as a motion binary_sensor, held ON for
             PIR_HOLD s after the last trigger (the SR602 itself drops after ~2.5 s and retriggers).
  CPU temp   /sys/class/thermal -> doorspy/temperature every TEMP_INTERVAL s (HA sensor)
  Frigate    the camera itself (GATE_CAMERA: Frigate's enabled switch - off stops its ffmpeg
             capture/decode and the RTSP pull from the Zero, i.e. no CPU while idle), recording,
             motion detection and person detection (face recognition runs inside it) are each
             "always" on, "pir" (on with motion, off HOLD_SECONDS after the last trigger) or "off":
             GATE_CAMERA / GATE_RECORD / GATE_MOTION / GATE_DETECT. Frigate keeps these switches
             across its restarts, so the wanted state is republished on every MQTT connect.

Lights are Home Assistant MQTT lights (JSON schema), the button an event, the PIR a binary_sensor; all auto-discovered
under one device "doorspy" with one availability topic. Lights fade (HA "transition") with
gamma 2.2. Runs as root (sysfs PWM). Settings: /etc/doorspy/doorspy.env.

The web UI (camera.py, user pispy) talks to this process over /run/doorspy/io.sock: one JSON
line in, one JSON line out (lights, state, doorspy.env edits, service restarts).
"""
import colorsys
import json
import logging
import math
import os
import pwd
import signal
import socket
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt
import spidev
from gpiozero import OutputDevice

ENV_DEFAULTS = {
    "MQTT_HOST": "192.168.0.4", "MQTT_PORT": "1883",
    "DEFAULT_TRANSITION": "0.5", "RAINBOW_PERIOD": "6", "PULSE_PERIOD": "2", "FLASH_PERIOD": "1",
    "BUTTON_HOLD": "2", "PIR_HOLD": "60", "HOLD_SECONDS": "120", "FRIGATE_CAMERA": "doorspy",
    "GATE_CAMERA": "always", "GATE_RECORD": "always", "GATE_MOTION": "always", "GATE_DETECT": "always",
    "POWER_ON_MOTION": "0", "POWER_MOTION_BRIGHTNESS": "100", "POWER_MOTION_HOLD": "30",
    "RGB_INDICATOR": "0", "INDICATOR_HOLD": "5", "TEMP_INTERVAL": "60",
    "RGB_RECORD_COLOR": "ff0000", "RGB_RECORD_MODE": "solid", "RGB_RECORD_PERIOD": "1",
    "RGB_PERSON_COLOR": "ffff00", "RGB_PERSON_MODE": "solid", "RGB_PERSON_PERIOD": "1",
    "RGB_FACE_COLOR": "00ff00", "RGB_FACE_MODE": "solid", "RGB_FACE_PERIOD": "1",
}


def env(key):
    return os.environ.get(key, ENV_DEFAULTS[key])


MQTT_HOST = env("MQTT_HOST")
MQTT_PORT = int(env("MQTT_PORT"))
DEFAULT_TRANSITION = float(env("DEFAULT_TRANSITION"))
RAINBOW_PERIOD = float(env("RAINBOW_PERIOD"))
PULSE_PERIOD = float(env("PULSE_PERIOD"))
FLASH_PERIOD = float(env("FLASH_PERIOD"))
PIR_HOLD = float(env("PIR_HOLD"))
PIR_GPIO = int(os.environ.get("PIR_GPIO", "17"))
HOLD_SECONDS = float(env("HOLD_SECONDS"))
CAMERA = env("FRIGATE_CAMERA")
BUTTON_GPIO = int(os.environ.get("BUTTON_GPIO", "26"))
BUTTON_HOLD = float(env("BUTTON_HOLD"))
POWER_ON_MOTION = env("POWER_ON_MOTION") == "1"
POWER_MOTION_BRIGHTNESS = int(env("POWER_MOTION_BRIGHTNESS"))
POWER_MOTION_HOLD = float(env("POWER_MOTION_HOLD"))
RGB_INDICATOR = env("RGB_INDICATOR") == "1"
INDICATOR_HOLD = float(env("INDICATOR_HOLD"))
TEMP_INTERVAL = float(env("TEMP_INTERVAL"))
SOCKET_PATH = os.environ.get("SOCKET_PATH", "/run/doorspy/io.sock")
LIGHTS_FILE = os.environ.get("LIGHTS_FILE", "/var/lib/doorspy/lights.json")   # light state across restarts
SOCKET_USER = os.environ.get("SOCKET_USER", "pispy")
ENV_FILE = "/etc/doorspy/doorspy.env"
GAMMA = 2.2
FPS = 50

MODES = ("always", "pir", "off")
GATE_FUNCS = ("camera", "record", "motion", "detect")
GATE_TOPICS = {"camera": "enabled", "record": "recordings", "motion": "motion", "detect": "detect"}
INDICATORS = ("face", "person", "record")          # priority order
EFFECTS = ("rainbow", "pulse", "flash")             # RGB effects (HA effect_list); none = solid
INDICATOR_MODES = ("solid", "pulse", "flash")       # per-indicator animation


def effect_level(effect, period, level, now):
    """Modulate a brightness level with an animation: pulse breathes 5..100 %, flash is on for
    half the period."""
    if effect == "pulse":
        return level * (0.05 + 0.95 * (0.5 - 0.5 * math.cos(2 * math.pi * now / period)))
    if effect == "flash":
        return level if (now % period) < period / 2 else 0.0
    return level


def hex_rgb(v):
    v = str(v).strip().lstrip("#")
    if len(v) != 6:
        raise ValueError("colour must be 6 hex digits")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def gate_modes(environ):
    """GATE_* from the environment; a doorspy.env from before the per-function gate only has
    PIR_GATE (1 = detect followed the PIR, 0 = Frigate untouched, i.e. always on)."""
    modes = {}
    for f in GATE_FUNCS:
        v = environ.get(f"GATE_{f.upper()}")
        modes[f] = v if v in MODES else None
    if all(m is None for m in modes.values()) and environ.get("PIR_GATE") == "1":
        modes["detect"] = "pir"
    return {f: m or "always" for f, m in modes.items()}


GATE_MODES = gate_modes(os.environ)
RGB_STYLE = {n: (hex_rgb(env(f"RGB_{n.upper()}_COLOR")), env(f"RGB_{n.upper()}_MODE"),
                 float(env(f"RGB_{n.upper()}_PERIOD"))) for n in INDICATORS}

AVAILABILITY = "doorspy/availability"
DEVICE = {"identifiers": ["doorspy"], "name": "doorspy", "model": "Pi Zero W door camera"}

log = logging.getLogger("doorspy-io")


def write(path, val):
    with open(path, "w") as f:
        f.write(str(val))


class Light:
    """Brightness level 0..1 with fades; subclasses turn a level into hardware output."""

    def __init__(self, key, name):
        self.key, self.name = key, name
        self.topic = f"doorspy/light/{key}"
        self.on = False
        self.bri = 255                  # HA brightness 1..255, kept while off
        self.level = 0.0                # current output level 0..1 (perceptual)
        self.fade = None                # (from, to, t0, duration)
        self.cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    # --- Home Assistant ---
    def discovery(self):
        return {"schema": "json", "name": self.name, "unique_id": f"doorspy_{self.key}",
                "command_topic": f"{self.topic}/set", "state_topic": f"{self.topic}/state",
                "availability_topic": AVAILABILITY, "brightness": True, "device": DEVICE,
                **self.capabilities()}

    def capabilities(self):
        return {"supported_color_modes": ["brightness"]}

    def state(self):
        return {"state": "ON" if self.on else "OFF", "brightness": self.bri, "color_mode": "brightness"}

    def target(self):
        """Output level the light should settle at (subclasses may raise it temporarily)."""
        return self.bri / 255 if self.on else 0.0

    def command(self, cmd):
        with self.cv:
            if "brightness" in cmd:
                self.bri = max(1, min(255, int(cmd["brightness"])))
            self.apply_extra(cmd)
            if "state" in cmd:
                self.on = cmd["state"] == "ON"
            dur = max(0.0, float(cmd.get("transition", DEFAULT_TRANSITION)))
            self.fade_to(self.target(), dur)
            return self.state()

    def fade_to(self, target, dur):
        """Start a fade; call with self.cv held."""
        self.fade = (self.level, target, time.monotonic(), dur)
        self.cv.notify()

    def apply_extra(self, cmd):
        pass

    def animated(self):
        return False

    # --- output loop ---
    def _run(self):
        while True:
            with self.cv:
                while self.fade is None and not self.animated():
                    self.cv.wait()
                if self.fade:
                    a, b, t0, d = self.fade
                    x = 1.0 if d <= 0 else min(1.0, (time.monotonic() - t0) / d)
                    self.level = a + (b - a) * x
                    if x >= 1.0:
                        self.fade = None
                level, settled = self.level, self.fade is None
            try:
                self.render(level, settled)
            except Exception:
                log.exception("%s: render failed", self.key)
            time.sleep(1 / FPS)

    def render(self, level, settled):
        raise NotImplementedError

    def off_now(self):
        raise NotImplementedError


class PowerLed(Light):
    PWM = "/sys/class/pwm/pwmchip0"
    CH = f"{PWM}/pwm1"          # GPIO13 = PWM channel 1
    PERIOD = 100_000            # 10 kHz

    def __init__(self):
        if not os.path.isdir(self.CH):
            write(f"{self.PWM}/export", 1)
            time.sleep(0.5)
        write(f"{self.CH}/enable", 0)
        write(f"{self.CH}/duty_cycle", 0)
        write(f"{self.CH}/period", self.PERIOD)
        write(f"{self.CH}/enable", 1)
        # GPIO13 -> PWM1 (also done by the boot overlay; harmless to repeat)
        subprocess.run(["pinctrl", "set", "13", "a0"], check=True)
        self.en = OutputDevice(18, active_high=True, initial_value=False)
        self.boost = False          # motion boost active (POWER_ON_MOTION)
        self.boost_timer = None
        super().__init__("power", "Power LED")

    def target(self):
        t = super().target()
        return max(t, POWER_MOTION_BRIGHTNESS / 100) if self.boost else t

    def motion(self):
        """PIR trigger: up to POWER_MOTION_BRIGHTNESS, back POWER_MOTION_HOLD s after the last one."""
        if not POWER_ON_MOTION:
            return
        with self.cv:
            if self.boost_timer:
                self.boost_timer.cancel()
            self.boost_timer = threading.Timer(POWER_MOTION_HOLD, self._boost_off)
            self.boost_timer.daemon = True
            self.boost_timer.start()
            if not self.boost:
                self.boost = True
                self.fade_to(self.target(), DEFAULT_TRANSITION)
                log.info("power: motion, %d%% for %.0fs", POWER_MOTION_BRIGHTNESS, POWER_MOTION_HOLD)

    def _boost_off(self):
        with self.cv:
            self.boost = False
            self.boost_timer = None
            self.fade_to(self.target(), DEFAULT_TRANSITION)
            log.info("power: motion hold over, back to %s", "ON" if self.on else "OFF")

    def render(self, level, settled):
        if level > 0 and not self.en.value:
            write(f"{self.CH}/duty_cycle", 0)
            self.en.on()                                   # EN on - once per on-cycle
            log.info("power: EN on")
        write(f"{self.CH}/duty_cycle", int(self.PERIOD * level ** GAMMA))
        if settled and level == 0 and self.en.value:
            self.en.off()                                  # EN off - once, after the fade out
            log.info("power: EN off")

    def off_now(self):
        write(f"{self.CH}/duty_cycle", 0)
        self.en.off()


class Ws2812(Light):
    SPI_HZ = 2_400_000
    RESET = [0] * 100           # ~330 us low

    def __init__(self, count=1):
        self.count = count
        self.rgb = (255, 255, 255)
        self.effect = None
        self.flags = {}             # indicator name -> expiry (monotonic) or None = until cleared
        self.dirty = False          # render the idle state once after the last indicator went
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = self.SPI_HZ
        self.spi.mode = 0
        self.lut = []
        for v in range(256):
            bits = 0
            for i in range(7, -1, -1):
                bits = (bits << 3) | (0b110 if v >> i & 1 else 0b100)
            self.lut.append([(bits >> 16) & 0xFF, (bits >> 8) & 0xFF, bits & 0xFF])
        super().__init__("rgb", "RGB")

    def capabilities(self):
        return {"supported_color_modes": ["rgb"], "effect": True, "effect_list": list(EFFECTS)}

    def state(self):
        s = {"state": "ON" if self.on else "OFF", "brightness": self.bri, "color_mode": "rgb",
             "color": dict(zip("rgb", self.rgb))}
        s["effect"] = self.effect or "none"
        return s

    def apply_extra(self, cmd):
        if "color" in cmd:
            c = cmd["color"]
            self.rgb = (int(c.get("r", 0)), int(c.get("g", 0)), int(c.get("b", 0)))
        if "effect" in cmd:
            self.effect = cmd["effect"] if cmd["effect"] in EFFECTS else None

    # --- Frigate status indicator ---
    def indicate(self, name, on=True, hold=None):
        """on without hold: latched until switched off. on with hold: for that many seconds
        (test button). off with hold: keep showing that long, then clear. off: clear now."""
        if name not in INDICATORS:
            raise ValueError(f"no indicator {name!r}")
        with self.cv:
            now = time.monotonic()
            if on:
                if name not in self.flags:
                    log.info("rgb: %s indicator on%s", name, f" for {hold:g}s" if hold else "")
                self.flags[name] = None if hold is None else now + hold
            elif name in self.flags:
                if hold and self.flags[name] is None:
                    self.flags[name] = now + hold          # off-delay, not extended by repeats
                    log.info("rgb: %s indicator off in %gs", name, hold)
                elif not hold:
                    del self.flags[name]
                    log.info("rgb: %s indicator off", name)
            self.dirty = True
            self.cv.notify()

    def indicator(self):
        """Highest-priority active indicator, dropping expired ones. Call with self.cv held."""
        now = time.monotonic()
        for n, t in list(self.flags.items()):
            if t is not None and t < now:
                del self.flags[n]
                self.dirty = True
                log.info("rgb: %s indicator expired", n)
        for n in INDICATORS:
            if n in self.flags:
                return n
        return None

    def animated(self):
        if self.indicator():
            return True
        if self.dirty:                         # one more render of the idle state
            self.dirty = False
            return True
        return self.effect in EFFECTS and (self.on or self.level > 0)

    def show(self, rgb):
        g, r, b = rgb[1], rgb[0], rgb[2]       # WS2812 byte order is GRB
        px = self.lut[g] + self.lut[r] + self.lut[b]
        self.spi.xfer2(self.RESET + px * self.count + self.RESET)

    def render(self, level, settled):
        with self.cv:
            ind = self.indicator()
        now = time.monotonic()
        base = self.rgb
        if ind:                                # indicator at the light's brightness, even if off
            base, mode, period = RGB_STYLE[ind]
            level = effect_level(mode, period, self.bri / 255, now)
        elif self.effect == "rainbow":
            r, g, b = colorsys.hsv_to_rgb((now / RAINBOW_PERIOD) % 1.0, 1.0, 1.0)
            base = (r * 255, g * 255, b * 255)
        elif self.effect == "pulse":
            level = effect_level("pulse", PULSE_PERIOD, level, now)
        elif self.effect == "flash":
            level = effect_level("flash", FLASH_PERIOD, level, now)
        self.show(tuple(round(255 * ((c / 255) * level) ** GAMMA) for c in base))

    def off_now(self):
        self.show((0, 0, 0))


class CpuTemp:
    """Zero's SoC temperature as a Home Assistant sensor, published every TEMP_INTERVAL s."""

    TOPIC = "doorspy/temperature"
    PATH = "/sys/class/thermal/thermal_zone0/temp"

    def __init__(self, client):
        self.client = client
        self.last = None
        threading.Thread(target=self._run, daemon=True).start()

    def discovery(self):
        return {"name": "CPU temperature", "unique_id": "doorspy_temperature", "device_class": "temperature",
                "state_class": "measurement", "unit_of_measurement": "°C", "suggested_display_precision": 1,
                "state_topic": self.TOPIC, "expire_after": int(TEMP_INTERVAL * 3),
                "availability_topic": AVAILABILITY, "device": DEVICE}

    def read(self):
        with open(self.PATH) as f:
            return int(f.read()) / 1000

    def publish(self):
        try:
            self.last = self.read()
        except (OSError, ValueError) as e:
            log.warning("temperature: %s", e)
            return
        self.client.publish(self.TOPIC, f"{self.last:.1f}", retain=True)

    def _run(self):
        while True:
            time.sleep(TEMP_INTERVAL)
            self.publish()


class PressButton:
    """GPIO26 switch -> HA event entity: "press" on press, "long_press" after BUTTON_HOLD s."""

    TOPIC = "doorspy/button"

    def __init__(self, client):
        from gpiozero import Button
        self.client = client
        self.last = None                # (event, unix time) for the web UI
        # the switch drives the line itself: 3.3 V = pressed, so active high; the pull-down
        # only keeps the pin defined if the switch is unplugged
        self.btn = Button(BUTTON_GPIO, pull_up=False, bounce_time=0.03, hold_time=BUTTON_HOLD)
        self.btn.when_pressed = lambda: self.send("press")
        self.btn.when_held = lambda: self.send("long_press")
        log.info("button: GPIO%d, long press after %.1fs", BUTTON_GPIO, BUTTON_HOLD)

    def discovery(self):
        return {"name": "Button", "unique_id": "doorspy_button", "device_class": "button",
                "state_topic": self.TOPIC, "event_types": ["press", "long_press"],
                "availability_topic": AVAILABILITY, "device": DEVICE}

    def send(self, event):
        self.last = (event, time.time())
        self.client.publish(self.TOPIC, json.dumps({"event_type": event}))
        log.info("button: %s", event)

    def state(self):
        return {"event": self.last[0], "at": self.last[1]} if self.last else None


class FrigateGate:
    """Frigate's per-camera switches (recordings, motion, detect) driven by GATE_* and the PIR,
    plus Frigate's events for the RGB status indicator.

    Frigate refuses to switch motion off while detect is on, so ON goes camera -> motion ->
    detect -> recordings and OFF goes detect -> motion -> recordings -> camera. Only changes
    are published. "camera" is Frigate's enabled switch: off stops its ffmpeg for this camera."""

    def __init__(self, client, modes, rgb=None):
        self.client = client
        self.modes = modes
        self.rgb = rgb                  # Ws2812 to indicate on, or None
        self.pir_active = False
        self.sent = {}                  # last ON/OFF sent per function
        self.actual = {}                # last .../state seen from Frigate
        self.pir_on = False             # the PIR's held state (PIR_HOLD), set by Pir
        self.tracked = {}               # Frigate event id -> face recognised (person objects)

    def topic(self, f, leaf):
        return f"frigate/{CAMERA}/{GATE_TOPICS[f]}/{leaf}"

    def uses_pir(self):
        return "pir" in self.modes.values()

    def wanted(self, f):
        m = self.modes[f]
        return m == "always" or (m == "pir" and self.pir_active)

    def apply(self, force=False):
        on = [f for f in ("camera", "motion", "detect", "record") if self.wanted(f)]
        off = [f for f in ("detect", "motion", "record", "camera") if not self.wanted(f)]
        for f, val in [(f, True) for f in on] + [(f, False) for f in off]:
            if force or self.sent.get(f) != val:
                self.sent[f] = val
                self.client.publish(self.topic(f, "set"), "ON" if val else "OFF")
                log.info("frigate: %s %s", GATE_TOPICS[f], "ON" if val else "OFF")
        self.record_indicator()
        if self.tracked and not (self.wanted("camera") and self.wanted("detect")):
            self.tracked.clear()        # Frigate stops tracking without end events
            self.person_indicators()

    def subscribe(self, client):
        for f in GATE_FUNCS:
            client.subscribe(self.topic(f, "state"))
        if self.rgb:
            for t in (f"frigate/{CAMERA}/person", "frigate/tracked_object_update", "frigate/events"):
                client.subscribe(t)

    def on_message(self, topic, payload):
        for f in GATE_FUNCS:
            if topic == self.topic(f, "state"):
                self.actual[f] = payload
                return
        if not self.rgb:
            return
        if topic == f"frigate/{CAMERA}/person":
            # count of tracked persons; 0 is the reliable "nobody" even if an end event got lost
            if payload == "0" and self.tracked:
                self.tracked.clear()
                self.person_indicators()
            elif payload.isdigit() and int(payload) > 0 and not self.tracked:
                self.tracked["count"] = False            # someone, before the event arrives
                self.person_indicators()
        elif topic == "frigate/tracked_object_update":
            d = json.loads(payload)
            if d.get("type") == "face" and d.get("camera", CAMERA) == CAMERA and d.get("name"):
                log.info("frigate: face %s (%.0f%%)", d["name"], float(d.get("score", 0)) * 100)
                self.tracked.pop("count", None)
                self.tracked[d.get("id", "face")] = True
                self.person_indicators()
        elif topic == "frigate/events":
            d = json.loads(payload)
            a = d.get("after") or {}
            if a.get("camera") != CAMERA or a.get("label") != "person":
                return
            self.tracked.pop("count", None)
            if d.get("type") == "end" or a.get("end_time"):
                self.tracked.pop(a.get("id"), None)
            else:
                self.tracked[a.get("id")] = bool(a.get("sub_label")) or self.tracked.get(a.get("id"), False)
            self.person_indicators()

    def pir(self, on):
        self.pir_on = on
        self.record_indicator()

    def record_indicator(self):
        """Red while the PIR hold is active and recording is on - decided here, not from
        Frigate's MQTT motion topic, so it follows the sensor without any lag."""
        if self.rgb:
            self.rgb.indicate("record", self.pir_on and self.wanted("record") and self.wanted("camera"))

    def person_indicators(self):
        """Yellow while Frigate tracks a person, green while one of them has a recognised face;
        both linger INDICATOR_HOLD s after the last person is gone."""
        for name, on in (("person", bool(self.tracked)), ("face", any(self.tracked.values()))):
            self.rgb.indicate(name, on, hold=None if on else INDICATOR_HOLD)   # latched on, lingers off

    def state(self):
        return {f: {"mode": self.modes[f], "wanted": self.wanted(f), "actual": self.actual.get(f)}
                for f in GATE_FUNCS}


class Pir:
    """SR602 motion sensor -> binary_sensor held PIR_HOLD s; also drives the Frigate gate and
    the power LED's motion boost."""

    STATE = "doorspy/pir"

    def __init__(self, client, gate, power=None):
        from gpiozero import DigitalInputDevice
        self.client = client
        self.gate = gate
        self.power = power
        self.lock = threading.Lock()
        self.timer = None
        self.on = False
        self.hold_until = 0.0
        self.off_timer = None
        # SR602 drives the line actively (3.3 V / 0 V); the pull-down only covers an unplugged sensor.
        self.pir = DigitalInputDevice(PIR_GPIO, pull_up=False)

    def start(self):
        self.pir.when_activated = self.motion
        if PIR_HOLD <= 0:
            self.pir.when_deactivated = lambda: self.set_state(False)
        if self.pir.is_active:
            self.motion()
        log.info("pir: watching GPIO%d, hold %.0fs; Frigate %s, hold %.0fs; power LED on motion %s",
                 PIR_GPIO, PIR_HOLD, " ".join(f"{f}={m}" for f, m in self.gate.modes.items()),
                 HOLD_SECONDS, f"{POWER_MOTION_BRIGHTNESS}% for {POWER_MOTION_HOLD:.0f}s" if POWER_ON_MOTION else "off")

    def connected(self):
        """(Re)publish state after every MQTT connect - publishes before it are lost."""
        self.client.publish(self.STATE, "ON" if self.on else "OFF", retain=True)
        # Frigate persists its switches across restarts: always start from the wanted state.
        self.gate.apply(force=True)

    def discovery(self):
        return {"name": "PIR", "unique_id": "doorspy_pir", "device_class": "motion",
                "state_topic": self.STATE, "availability_topic": AVAILABILITY, "device": DEVICE}

    def state(self):
        return {"on": self.on, "hold_until": self.hold_until if self.on else None,
                "frigate_active": self.gate.pir_active}

    def set_state(self, on):
        with self.lock:
            if on == self.on:
                return
            self.on = on
        self.client.publish(self.STATE, "ON" if on else "OFF", retain=True)
        log.info("pir: %s", "motion" if on else "clear")
        self.gate.pir(on)

    def motion(self):
        self.set_state(True)
        if self.power:
            self.power.motion()
        if PIR_HOLD > 0:            # retriggers only extend the hold, no new message
            with self.lock:
                self.hold_until = time.time() + PIR_HOLD
                if self.off_timer:
                    self.off_timer.cancel()
                self.off_timer = threading.Timer(PIR_HOLD, self.set_state, args=(False,))
                self.off_timer.daemon = True
                self.off_timer.start()
        if not self.gate.uses_pir():
            return
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(HOLD_SECONDS, self.expire)
            self.timer.daemon = True
            self.timer.start()
            if not self.gate.pir_active:
                self.gate.pir_active = True
                self.gate.apply()

    def expire(self):
        with self.lock:
            self.timer = None
            self.gate.pir_active = False
            log.info("pir: quiet for %.0fs", HOLD_SECONDS)
            self.gate.apply()


# --- doorspy.env editing (from the web UI) ---------------------------------------------------

def _float(lo):
    def conv(v):
        x = float(v)
        if x < lo:
            raise ValueError(f"must be >= {lo}")
        return f"{x:g}"
    return conv


def _int(lo, hi):
    def conv(v):
        x = int(v)
        if not lo <= x <= hi:
            raise ValueError(f"must be {lo}..{hi}")
        return str(x)
    return conv


def _word(v):
    v = str(v).strip()
    if not v or any(c.isspace() for c in v) or any(c in v for c in "\"'#=$"):
        raise ValueError("invalid value")
    return v


def _mode(v):
    if v not in MODES:
        raise ValueError("must be always, pir or off")
    return v


def _bool(v):
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return "1"
    if s in ("0", "false", "off", "no", ""):
        return "0"
    raise ValueError("must be 0 or 1")


def _color(v):
    r, g, b = hex_rgb(v)
    return f"{r:02x}{g:02x}{b:02x}"


def _imode(v):
    if v not in INDICATOR_MODES:
        raise ValueError("must be solid, pulse or flash")
    return v


ENV_KEYS = {
    "MQTT_HOST": _word, "MQTT_PORT": _int(1, 65535),
    "DEFAULT_TRANSITION": _float(0), "RAINBOW_PERIOD": _float(0.1), "PULSE_PERIOD": _float(0.1),
    "FLASH_PERIOD": _float(0.1),
    "BUTTON_HOLD": _float(0.1), "PIR_HOLD": _float(0), "HOLD_SECONDS": _float(1),
    "FRIGATE_CAMERA": _word,
    "GATE_CAMERA": _mode, "GATE_RECORD": _mode, "GATE_MOTION": _mode, "GATE_DETECT": _mode,
    "POWER_ON_MOTION": _bool, "POWER_MOTION_BRIGHTNESS": _int(1, 100), "POWER_MOTION_HOLD": _float(1),
    "RGB_INDICATOR": _bool, "INDICATOR_HOLD": _float(1), "TEMP_INTERVAL": _float(10),
    **{f"RGB_{n.upper()}_COLOR": _color for n in INDICATORS},
    **{f"RGB_{n.upper()}_MODE": _imode for n in INDICATORS},
    **{f"RGB_{n.upper()}_PERIOD": _float(0.1) for n in INDICATORS},
}


def current_env():
    """The editable keys as the running process sees them (GATE_* after the legacy mapping)."""
    out = {k: env(k) for k in ENV_KEYS}
    for f, m in GATE_MODES.items():
        out[f"GATE_{f.upper()}"] = m
    return out


def validate_env(changes):
    new = {}
    for k, v in changes.items():
        if k not in ENV_KEYS:
            raise ValueError(f"{k}: not editable")
        try:
            new[k] = ENV_KEYS[k](v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{k}: {e}") from None
    merged = {**current_env(), **new}
    cam, motion, detect = merged["GATE_CAMERA"], merged["GATE_MOTION"], merged["GATE_DETECT"]
    # always > pir > off. A disabled camera records/detects nothing, so no function may be
    # "more on" than the camera; and Frigate's detect needs motion.
    for f in ("record", "motion", "detect"):
        if MODES.index(merged[f"GATE_{f.upper()}"]) < MODES.index(cam):
            raise ValueError(f"{f} can't be on more than the camera (camera={cam}): "
                             "set the camera to always first")
    if MODES.index(detect) < MODES.index(motion):
        raise ValueError("person detection can't be on more than motion detection "
                         f"(motion={motion}, detect={detect})")
    return new


def rewrite_env(changes):
    """Update KEY=value lines in doorspy.env in place, keep comments and order, append new
    keys, drop the legacy PIR_GATE line. Atomic replace."""
    try:
        with open(ENV_FILE) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, seen = [], set()
    for line in lines:
        key = None
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
        if key == "PIR_GATE":
            continue
        if key in changes:
            out.append(f"{key}={changes[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in changes.items():
        if k not in seen:
            out.append(f"{k}={v}")
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, ENV_FILE)


UNITS = ("doorspy-cam", "mediamtx", "doorspy-io")


def service_action(unit, action):
    if action == "reboot":
        log.warning("reboot requested from the web UI")
        threading.Timer(0.5, subprocess.run, args=(["systemctl", "reboot"],)).start()
        return
    if action != "restart" or unit not in UNITS:
        raise ValueError("unknown service action")
    log.info("restart %s requested from the web UI", unit)
    # reply first, then act (restarting ourselves or the camera drops the connection)
    threading.Timer(0.5, subprocess.run, args=(["systemctl", "restart", f"{unit}.service"],)).start()


# --- local control socket -------------------------------------------------------------------

class IoSocket(threading.Thread):
    """One JSON line per connection: {"cmd": ...} -> {"ok": true, ...} or {"error": "..."}."""

    def __init__(self, handler):
        super().__init__(daemon=True)
        self.handler = handler
        os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(SOCKET_PATH)
        try:
            os.chown(SOCKET_PATH, pwd.getpwnam(SOCKET_USER).pw_uid, -1)
        except KeyError:
            log.warning("socket: user %s not found, socket stays root-only", SOCKET_USER)
        os.chmod(SOCKET_PATH, 0o660)
        self.sock.listen(4)
        log.info("socket: %s (user %s)", SOCKET_PATH, SOCKET_USER)

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        data = b""
        with conn:
            conn.settimeout(5)
            try:
                while b"\n" not in data and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data.split(b"\n", 1)[0] or b"{}")
                resp = {"ok": True, **(self.handler(req) or {})}
            except (ValueError, KeyError) as e:
                resp = {"error": str(e)}
            except Exception as e:
                log.exception("socket: %r failed", data[:200])
                resp = {"error": f"{type(e).__name__}: {e}"}
            try:
                conn.sendall(json.dumps(resp).encode() + b"\n")
            except OSError:
                pass

    def close(self):
        self.sock.close()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass


def save_lights(lights):
    """Every Save in the web UI restarts this service: keep the lights' state so they come back
    as they were instead of off."""
    try:
        tmp = LIGHTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: l.state() for k, l in lights.items()}, f)
        os.replace(tmp, LIGHTS_FILE)
    except OSError as e:
        log.warning("lights: can't save state to %s: %s", LIGHTS_FILE, e)


def restore_lights(lights):
    try:
        with open(LIGHTS_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    for key, st in saved.items():
        if key in lights and isinstance(st, dict):
            lights[key].command({**st, "transition": 0})
            log.info("%s: restored %s", key, st)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    lights = {}
    for cls in (PowerLed, Ws2812):
        try:
            light = cls()
            lights[light.key] = light
        except Exception:
            log.exception("%s unavailable", cls.__name__)
    restore_lights(lights)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="doorspy-io")
    client.will_set(AVAILABILITY, "offline", retain=True)
    mqtt_up = {"connected": False}
    rgb = lights.get("rgb") if RGB_INDICATOR else None
    gate = FrigateGate(client, GATE_MODES, rgb)
    if rgb:
        log.info("rgb: Frigate indicator on, %s, hold %.0fs",
                 " ".join(f"{n}=#{r:02x}{g:02x}{b:02x}/{m}/{p:g}s" for n, ((r, g, b), m, p) in RGB_STYLE.items()),
                 INDICATOR_HOLD)

    def publish_state(light):
        client.publish(f"{light.topic}/state", json.dumps(light.state()), retain=True)

    def on_connect(c, userdata, flags, reason, props):
        log.info("mqtt connected to %s:%d (%s)", MQTT_HOST, MQTT_PORT, reason)
        mqtt_up["connected"] = True
        for light in lights.values():
            c.publish(f"homeassistant/light/doorspy_{light.key}/config",
                      json.dumps(light.discovery()), retain=True)
            c.subscribe(f"{light.topic}/set")
            publish_state(light)
        if button:
            c.publish("homeassistant/event/doorspy_button/config", json.dumps(button.discovery()), retain=True)
        c.publish("homeassistant/sensor/doorspy_temperature/config", json.dumps(temp.discovery()), retain=True)
        temp.publish()
        gate.subscribe(c)
        if pir:
            c.publish("homeassistant/binary_sensor/doorspy_pir/config", json.dumps(pir.discovery()), retain=True)
            pir.connected()
        else:
            gate.apply(force=True)
        c.publish(AVAILABILITY, "online", retain=True)

    def on_disconnect(c, userdata, flags, reason, props):
        mqtt_up["connected"] = False
        log.warning("mqtt disconnected (%s)", reason)

    def on_message(c, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip()
        if msg.topic.startswith("frigate/"):
            try:
                gate.on_message(msg.topic, payload)
            except (ValueError, TypeError) as e:
                log.warning("frigate: bad message on %s: %s", msg.topic, e)
            return
        key = msg.topic.split("/")[2]
        light = lights.get(key)
        if not light:
            return
        try:
            cmd = json.loads(payload)
        except ValueError:
            cmd = {"state": payload.upper()}                     # plain ON/OFF also works
        log.info("%s <- %s", key, cmd)
        light.command(cmd)
        publish_state(light)
        save_lights(lights)

    temp = CpuTemp(client)
    button = None
    try:
        button = PressButton(client)
    except Exception:
        log.exception("button unavailable")
    pir = None
    try:
        pir = Pir(client, gate, lights.get("power"))
    except Exception:
        log.exception("PIR unavailable")
    if not lights and not pir and not button:
        sys.exit(1)

    def state():
        power = lights.get("power")
        return {"lights": {k: l.state() for k, l in lights.items()},
                "pir": pir.state() if pir else None,
                "frigate": gate.state(),
                "button": button.state() if button else None,
                "mqtt": mqtt_up["connected"],
                "temperature": temp.last,
                "indicators": {"power_motion": bool(power and power.boost),
                               "rgb": rgb.indicator() if rgb else None,
                               "tracked": {k: v for k, v in gate.tracked.items()}},
                "env": current_env()}

    def handle(req):
        cmd = req.get("cmd")
        if cmd == "state":
            return state()
        if cmd == "light":
            light = lights.get(req.get("key"))
            if not light:
                raise ValueError(f"no light {req.get('key')!r}")
            log.info("%s <- %s (web)", light.key, req.get("set"))
            light.command(dict(req.get("set") or {}))
            publish_state(light)
            save_lights(lights)
            return {"state": light.state()}
        if cmd == "env":
            changes = validate_env(dict(req.get("set") or {}))
            rewrite_env(changes)
            log.info("doorspy.env updated (%s), restarting", ", ".join(changes))
            service_action("doorspy-io", "restart")
            return {"changed": changes, "restarting": True}
        if cmd == "service":
            service_action(req.get("unit"), req.get("action"))
            return {}
        if cmd == "indicate":                  # test button in the web UI
            if not rgb:
                raise ValueError("RGB indicator is disabled (RGB_INDICATOR=0)")
            rgb.indicate(str(req.get("name")), bool(req.get("on", True)), float(req.get("hold", INDICATOR_HOLD)))
            return {"indicator": rgb.indicator()}
        raise ValueError(f"unknown cmd {cmd!r}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    if pir:
        pir.start()
    ctl = None
    try:
        ctl = IoSocket(handle)
        ctl.start()
    except OSError:
        log.exception("control socket unavailable, web UI can't reach doorspy-io")

    def stop(*_):
        for light in lights.values():
            try:
                light.off_now()
            except Exception:
                pass
        client.publish(AVAILABILITY, "offline", retain=True)
        time.sleep(0.3)
        client.loop_stop()
        if ctl:
            ctl.close()
        log.info("stopped: lights off")
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.pause()


if __name__ == "__main__":
    main()
