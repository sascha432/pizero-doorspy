#!/usr/bin/env python3
"""doorspy camera service.

One Picamera2 instance feeds both hardware encoders:
  main  1280x768 -> H.264 -> MediaMTX (rtsp://<pi>:8554/cam, WebRTC http://<pi>:8889/cam)
  lores  800x480 -> JPEG  -> HTTP  /api/snapshot.jpeg  and  /api/stream.mjpeg (on demand)

Settings: the DEFAULTS below, overridden by /var/lib/doorspy/camera.json, which the web UI
(webui.py, HTTPS :8443) writes. Image controls apply live; stream settings and flips
reconfigure the pipeline (about 3 s, stream clients stay connected).

:8080 needs HTTP Basic auth: the stream login (/var/lib/doorspy/stream.auth, default
guest / password, for stream clients) or the web UI's admin login. Both files are
user:salt:pbkdf2-sha256, written by install.sh when missing and changed on the System tab.
"""
import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import socket
import threading
import time
from http import server
from socketserver import ThreadingMixIn

import libcamera
import simplejpeg
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, MJPEGEncoder
from picamera2.outputs import FileOutput, PyavOutput

DEFAULTS = {
    "main_size": [1280, 768],      # 5:3 like lores, so both share the crop without stretching
    "lores_size": [800, 480],
    "fps": 15,
    "h264_bitrate": 2_500_000,
    "mjpeg_bitrate": 6_000_000,    # VC4 rate control lands at ~half: ~3 Mbit/s, ~25 KB/frame
                                   # only spent while a stream client is connected (+ MJPEG_LINGER)
    "snapshot_quality": 80,
    "hflip": False,
    "vflip": False,
    "controls": {},                # libcamera controls, see CONTROLS
}
SENSOR_SIZE = (1296, 972)          # 2x2 binned full field of view
RTSP_URL = "rtsp://127.0.0.1:8554/cam"
HTTP_PORT = 8080
MJPEG_LINGER = 300                 # s the MJPEG encoder keeps running after the last client left
SETTINGS_FILE = os.environ.get("CAMERA_SETTINGS", "/var/lib/doorspy/camera.json")
STREAM_AUTH_FILE = os.environ.get("STREAM_AUTH", "/var/lib/doorspy/stream.auth")   # guest login for :8080
WEB_AUTH_FILE = os.environ.get("WEB_AUTH", "/var/lib/doorspy/web.auth")            # admin login (web UI)
PBKDF2_ROUNDS = 50_000             # ~0.2 s on the Zero; verified once per credential, then cached

# Live controls the web UI may set: name -> (type, min, max). Enums as libcamera ints:
#   AwbMode 0 Auto 1 Incandescent 2 Tungsten 3 Fluorescent 4 Indoor 5 Daylight 6 Cloudy
#   NoiseReductionMode 0 Off 1 Fast 2 HighQuality 3 Minimal
# ExposureTime/AnalogueGain only matter with AeEnable off; a frame at 15 fps is 66 ms, so
# longer exposures lower the frame rate.
CONTROLS = {
    "Brightness": (float, -1.0, 1.0),
    "Contrast": (float, 0.0, 4.0),
    "Saturation": (float, 0.0, 4.0),
    "Sharpness": (float, 0.0, 8.0),
    "ExposureValue": (float, -8.0, 8.0),
    "AwbMode": (int, 0, 6),
    "AeEnable": (bool, 0, 1),
    "ExposureTime": (int, 100, 500_000),
    "AnalogueGain": (float, 1.0, 16.0),
    "NoiseReductionMode": (int, 0, 3),
}
STREAM_LIMITS = {
    "fps": (int, 5, 30),
    "h264_bitrate": (int, 500_000, 10_000_000),
    "mjpeg_bitrate": (int, 1_000_000, 20_000_000),
    "snapshot_quality": (int, 10, 100),
}

log = logging.getLogger("doorspy")


def _num(spec, name, v):
    typ, lo, hi = spec
    if typ is bool:
        return bool(v)
    try:
        x = typ(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: not a number") from None
    if not lo <= x <= hi:
        raise ValueError(f"{name}: must be {lo}..{hi}")
    return x


def validate_controls(ctl):
    out = {}
    for k, v in ctl.items():
        if k not in CONTROLS:
            raise ValueError(f"{k}: unknown control")
        out[k] = _num(CONTROLS[k], k, v)
    return out


def _size(name, v):
    try:
        w, h = int(v[0]), int(v[1])
    except (TypeError, ValueError, IndexError):
        raise ValueError(f"{name}: expected [width, height]") from None
    if w % 2 or h % 2 or not 64 <= w <= 1296 or not 64 <= h <= 972:
        raise ValueError(f"{name}: even numbers up to 1296x972")
    return [w, h]


def validate_settings(s):
    """Return a complete, checked settings dict (unknown keys dropped)."""
    out = dict(DEFAULTS)
    out["controls"] = validate_controls(s.get("controls") or {})
    for k, spec in STREAM_LIMITS.items():
        if k in s:
            out[k] = _num(spec, k, s[k])
    for k in ("main_size", "lores_size"):
        if k in s:
            out[k] = _size(k, s[k])
    for k in ("hflip", "vflip"):
        if k in s:
            out[k] = bool(s[k])
    if out["lores_size"][0] > out["main_size"][0] or out["lores_size"][1] > out["main_size"][1]:
        raise ValueError("lores_size must not be larger than main_size")
    return out


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            raw = f.read().strip()
        data = json.loads(raw) if raw else {}
        s = validate_settings(data)
        log.info("settings from %s", SETTINGS_FILE)
        return s
    except FileNotFoundError:
        return validate_settings({})
    except (ValueError, OSError) as e:
        log.error("%s unusable (%s), using defaults", SETTINGS_FILE, e)
        return validate_settings({})


def save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as e:
        log.error("can't save %s: %s", SETTINGS_FILE, e)
        raise


class AuthFile:
    """user:salt_hex:pbkdf2_sha256_hex. Verified credentials are cached by their sha256 so the
    slow PBKDF2 runs once per process and credential, not on every request."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.ok = set()
        self.stamp = None           # file mtime the cache belongs to

    def _read(self):
        with open(self.path) as f:
            user, salt, digest = f.read().strip().split(":")
        return user, bytes.fromhex(salt), bytes.fromhex(digest)

    def user(self):
        try:
            return self._read()[0]
        except (OSError, ValueError):
            return None

    @staticmethod
    def _hash(pw, salt):
        return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)

    def check(self, user, pw):
        key = hashlib.sha256(f"{user}\0{pw}".encode()).digest()
        try:
            stamp = os.stat(self.path).st_mtime_ns
        except OSError:
            stamp = None
        with self.lock:
            if stamp != self.stamp:     # file replaced (UI, install.sh, by hand): forget old logins
                self.ok.clear()
                self.stamp = stamp
            if key in self.ok:
                return True
        try:
            fuser, salt, digest = self._read()
        except (OSError, ValueError) as e:
            log.error("auth: %s unusable (%s)", self.path, e)
            return False
        if hmac.compare_digest(fuser.encode(), user.encode()) and hmac.compare_digest(self._hash(pw, salt), digest):
            with self.lock:
                self.ok.add(key)
            return True
        return False

    def set(self, user, pw):
        if not user or any(c in user for c in ":\n") or len(pw) < 6:
            raise ValueError("user must not contain ':', password at least 6 characters")
        salt = secrets.token_bytes(16)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{user}:{salt.hex()}:{self._hash(pw, salt).hex()}\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        with self.lock:
            self.ok.clear()
        log.info("auth: password set for %s in %s", user, self.path)


def basic_user(headers, auths):
    """The user name from an Authorization: Basic header if any of the AuthFiles accepts it."""
    header = headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        user, _, pw = base64.b64decode(header[6:], validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return user if any(a.check(user, pw) for a in auths) else None


class FrameBuffer(io.BufferedIOBase):
    """Latest MJPEG frame, with a condition so stream clients wake on each new frame."""

    def __init__(self):
        self.frame = None
        self.cond = threading.Condition()

    def write(self, buf):
        with self.cond:
            self.frame = bytes(buf)
            self.cond.notify_all()
        return len(buf)


class Camera:
    def __init__(self, settings):
        self.picam2 = Picamera2()
        self.settings = settings
        self.started = time.time()
        self.mjpeg = None
        self.mjpeg_buf = FrameBuffer()
        self.mjpeg_clients = 0
        self.mjpeg_stop_timer = None
        self.mjpeg_lock = threading.Lock()     # also serialises reconfigure
        self.h264 = None
        self._configure()

    # --- pipeline -------------------------------------------------------------------------
    def _configure(self):
        s = self.settings
        frame_us = int(1_000_000 / s["fps"])
        controls = {"FrameDurationLimits": (frame_us, frame_us), **self._live_controls(s["controls"])}
        config = self.picam2.create_video_configuration(
            main={"size": tuple(s["main_size"]), "format": "YUV420"},
            lores={"size": tuple(s["lores_size"]), "format": "YUV420"},
            sensor={"output_size": SENSOR_SIZE},
            transform=libcamera.Transform(hflip=int(s["hflip"]), vflip=int(s["vflip"])),
            controls=controls,
            buffer_count=4,
        )
        self.picam2.configure(config)
        lores = self.picam2.stream_configuration("lores")
        self.lores_stride = lores["stride"]
        self.lores_w, self.lores_h = lores["size"]

    @staticmethod
    def _live_controls(ctl):
        """What actually goes to libcamera: with AE on, manual exposure values are kept in the
        settings for the UI but not sent."""
        ctl = dict(ctl)
        if ctl.get("AeEnable", True):
            ctl.pop("ExposureTime", None)
            ctl.pop("AnalogueGain", None)
        return ctl

    def start(self):
        self.picam2.start()
        self._start_h264()
        s = self.settings
        log.info("camera started: main %s H.264 -> %s, lores %s, %d fps", s["main_size"], RTSP_URL,
                 s["lores_size"], s["fps"])

    def _start_h264(self):
        s = self.settings
        self.h264 = H264Encoder(bitrate=s["h264_bitrate"], repeat=True, iperiod=s["fps"] * 2)
        out = PyavOutput(RTSP_URL, format="rtsp")
        # Losing MediaMTX is fatal: exit and let systemd restart us cleanly.
        out.error_callback = lambda e: (log.error("RTSP output failed: %s", e), os._exit(1))
        self.picam2.start_encoder(self.h264, out, name="main")

    def _start_mjpeg(self):
        self.mjpeg = MJPEGEncoder(bitrate=self.settings["mjpeg_bitrate"])
        self.mjpeg_buf.frame = None
        self.picam2.start_encoder(self.mjpeg, FileOutput(self.mjpeg_buf), name="lores")
        log.info("mjpeg encoder started")

    def mjpeg_join(self):
        with self.mjpeg_lock:
            self.mjpeg_clients += 1
            if self.mjpeg_stop_timer:
                self.mjpeg_stop_timer.cancel()
                self.mjpeg_stop_timer = None
            if not self.mjpeg:
                self._start_mjpeg()

    def mjpeg_leave(self):
        # The encoder lingers: pollers that grab one frame a minute would otherwise start and
        # stop it each time, and after a few hundred cycles the VC4 codec driver oopses and
        # leaks CMA memory - no frames on any stream until a reboot.
        with self.mjpeg_lock:
            self.mjpeg_clients -= 1
            if self.mjpeg_clients == 0 and self.mjpeg:
                self.mjpeg_stop_timer = threading.Timer(MJPEG_LINGER, self._linger_done)
                self.mjpeg_stop_timer.daemon = True
                self.mjpeg_stop_timer.start()

    def _linger_done(self):
        with self.mjpeg_lock:
            self.mjpeg_stop_timer = None
            if self.mjpeg_clients == 0 and self.mjpeg:
                self.picam2.stop_encoder(self.mjpeg)
                self.mjpeg = None
                log.info("mjpeg encoder stopped")

    def snapshot(self):
        # While the MJPEG encoder runs, reuse its latest hardware-encoded frame.
        if self.mjpeg and self.mjpeg_buf.frame:
            return self.mjpeg_buf.frame
        with self.mjpeg_lock:          # not during a reconfigure
            flat = self.picam2.capture_array("lores").reshape(-1)
            s, w, h = self.lores_stride, self.lores_w, self.lores_h
        y = flat[: s * h].reshape(h, s)[:, :w]
        u = flat[s * h : s * h * 5 // 4].reshape(h // 2, s // 2)[:, : w // 2]
        v = flat[s * h * 5 // 4 : s * h * 3 // 2].reshape(h // 2, s // 2)[:, : w // 2]
        return simplejpeg.encode_jpeg_yuv_planes(
            y.copy(), u.copy(), v.copy(), quality=self.settings["snapshot_quality"]
        )

    # --- web UI ---------------------------------------------------------------------------
    def set_controls(self, ctl):
        """Apply image controls live and persist them."""
        ctl = validate_controls(ctl)
        merged = {**self.settings["controls"], **ctl}
        live = self._live_controls(merged)
        if "AeEnable" in ctl or "ExposureTime" in ctl or "AnalogueGain" in ctl:
            send = {k: v for k, v in live.items() if k in ("AeEnable", "ExposureTime", "AnalogueGain")}
            send.update({k: v for k, v in live.items() if k in ctl})
        else:
            send = {k: v for k, v in live.items() if k in ctl}
        self.picam2.set_controls(send)
        self.settings["controls"] = merged
        save_settings(self.settings)
        log.info("controls: %s", send)
        return merged

    def reconfigure(self, new):
        """Stop everything, rebuild the pipeline with new stream settings, start again.
        MJPEG clients keep their connection and wait for the first new frame."""
        new = validate_settings(new)
        with self.mjpeg_lock:
            t0 = time.time()
            if self.mjpeg:
                self.picam2.stop_encoder(self.mjpeg)
                self.mjpeg = None
            self.picam2.stop_encoder(self.h264)
            self.picam2.stop()
            old = self.settings
            self.settings = new
            try:
                self._configure()
            except Exception:
                log.exception("reconfigure failed, back to the previous settings")
                self.settings = old
                self._configure()
                self.picam2.start()
                self._start_h264()
                if self.mjpeg_clients:
                    self._start_mjpeg()
                raise
            self.picam2.start()
            self._start_h264()
            if self.mjpeg_clients:
                self._start_mjpeg()
            save_settings(self.settings)
            log.info("reconfigured in %.1fs: %s", time.time() - t0,
                     {k: v for k, v in new.items() if k != "controls"})
        return self.settings

    def current(self):
        """Settings plus what the sensor is actually doing, for the UI read-outs."""
        meta = {}
        try:
            m = self.picam2.capture_metadata()
            for k in ("ExposureTime", "AnalogueGain", "DigitalGain", "ColourTemperature", "Lux",
                      "FrameDuration"):
                if k in m:
                    meta[k] = m[k]
        except Exception as e:
            meta["error"] = str(e)
        return {"settings": self.settings, "defaults": DEFAULTS, "metadata": meta,
                "mjpeg_clients": self.mjpeg_clients, "uptime": time.time() - self.started}


class Handler(server.BaseHTTPRequestHandler):
    cam: Camera = None
    auths = ()                  # AuthFiles accepted here (stream login + admin login)
    realm = "doorspy camera"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def address_string(self):
        return self.client_address[0].removeprefix("::ffff:")     # IPv4 clients on the v6 socket

    def authorised(self):
        self.user = basic_user(self.headers, self.auths)
        if self.user:
            return True
        if self.headers.get("Authorization"):
            log.warning("auth: failed login from %s", self.address_string())
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{self.realm}"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "13")
        self.end_headers()
        self.wfile.write(b"login needed\n")
        return False

    def do_GET(self):
        if not self.authorised():
            return
        path = self.path.split("?")[0]
        if path == "/api/snapshot.jpeg":
            self.send_snapshot()
        elif path == "/api/stream.mjpeg":
            self.send_stream()
        elif path == "/":
            self.send_index()
        else:
            self.send_error(404)

    def send_index(self):
        body = (
            "<!doctype html><title>doorspy</title><body style='margin:0;background:#000'>"
            "<img src='/api/stream.mjpeg' style='width:100%'>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_snapshot(self):
        jpeg = self.cam.snapshot()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpeg)

    def send_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        buf = self.cam.mjpeg_buf
        self.cam.mjpeg_join()
        log.info("stream client %s connected", self.address_string())
        try:
            last = None
            while True:
                with buf.cond:
                    # 15 s: a reconfigure takes a few seconds and sends no frames meanwhile
                    if not buf.cond.wait_for(lambda: buf.frame is not last and buf.frame, timeout=15):
                        raise TimeoutError("no frames from encoder")
                    frame = last = buf.frame
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError) as e:
            log.info("stream client %s gone (%s)", self.address_string(), type(e).__name__)
        finally:
            self.cam.mjpeg_leave()


class Server(ThreadingMixIn, server.HTTPServer):
    """Dual-stack: doorspy.local resolves to the IPv6 address first on most clients, and a plain
    HTTPServer only listens on IPv4. Falls back to IPv4 if the kernel has no IPv6."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, handler):
        try:
            self.address_family = socket.AF_INET6
            super().__init__(("::", port), handler)
        except OSError:
            self.address_family = socket.AF_INET
            super().__init__(("", port), handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cam = Camera(load_settings())
    cam.start()
    stream_auth, web_auth = AuthFile(STREAM_AUTH_FILE), AuthFile(WEB_AUTH_FILE)
    for a in (stream_auth, web_auth):
        if not os.path.exists(a.path):
            log.error("%s missing: nobody can log in with it (run install.sh)", a.path)
    Handler.cam = cam
    Handler.auths = (stream_auth, web_auth)
    srv = Server(HTTP_PORT, Handler)
    log.info("http on :%d  /api/snapshot.jpeg  /api/stream.mjpeg  (login: %s or the web UI's admin)",
             HTTP_PORT, stream_auth.user() or "stream.auth missing")
    try:
        import sys
        sys.modules.setdefault("camera", sys.modules[__name__])   # webui does "import camera"
        import webui
        webui.start_https(cam, web_auth, stream_auth)
    except Exception:
        log.exception("web UI not started")
    srv.serve_forever()


if __name__ == "__main__":
    main()
