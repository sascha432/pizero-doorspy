"""doorspy web UI: HTTPS :8443 with Basic auth, served by camera.py next to the open :8080.

  /                      www/index.html (camera, lights, sensors, system tabs)
  /api/snapshot.jpeg     same as on :8080, so the page can embed the preview without mixed content
  /api/stream.mjpeg
  /api/config            GET  everything the page needs at load
  /api/camera/meta       GET  sensor metadata + MJPEG client count (cheap poll)
  /api/camera/controls   POST live libcamera controls      -> Camera.set_controls
  /api/camera/settings   POST stream settings / flips      -> Camera.reconfigure
  /api/camera/reset      POST back to DEFAULTS
  /api/io/state          GET  lights, PIR, Frigate gate, button, MQTT   (doorspy-io socket)
  /api/io/light/<key>    POST HA-style light command                    (doorspy-io socket)
  /api/io/env            POST doorspy.env keys, restarts doorspy-io     (doorspy-io socket)
  /api/io/indicate       POST {"name": "record|person|face"} test an RGB indicator (doorspy-io socket)
  /api/system/status     GET  temperature, throttling, WiFi, services, memory
  /api/system/service    POST {"unit", "action": "restart"} or {"action": "reboot"} (doorspy-io socket)
  /api/system/log        GET  ?unit=doorspy-cam&lines=100 (journalctl, group systemd-journal)
  /api/auth/password     POST {"current", "user", "new"}            the admin login (this UI)
  /api/auth/stream       POST {"current", "user", "new"}            the :8080 stream login (stream clients)

Files (written by install.sh when missing; /var/lib/doorspy belongs to the camera service user):
  /var/lib/doorspy/web.auth     user:salt:pbkdf2-sha256   default admin / password
  /var/lib/doorspy/stream.auth  same format               default guest / password
  /etc/doorspy/web.crt + web.key   self-signed, EC P-256 (cheap handshake on the Zero)
"""
import json
import logging
import os
import socket
import ssl
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import camera

HTTPS_PORT = 8443
CERT_FILE = os.environ.get("WEB_CERT", "/etc/doorspy/web.crt")
KEY_FILE = os.environ.get("WEB_KEY", "/etc/doorspy/web.key")
WWW_DIR = os.environ.get("WEB_WWW", os.path.join(os.path.dirname(os.path.abspath(__file__)), "www"))
IO_SOCKET = os.environ.get("SOCKET_PATH", "/run/doorspy/io.sock")
UNITS = ("doorspy-cam", "mediamtx", "doorspy-io")

log = logging.getLogger("doorspy.web")


# --- doorspy-io socket client ---------------------------------------------------------------

def io_call(req, timeout=5):
    """One JSON line to doorspy-io, one back. Errors come back as {"error": ...}."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(IO_SOCKET)
            s.sendall(json.dumps(req).encode() + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.split(b"\n", 1)[0] or b"{}")
    except (FileNotFoundError, ConnectionRefusedError):
        return {"error": "doorspy-io not running"}
    except (OSError, ValueError) as e:
        return {"error": f"doorspy-io: {e}"}


# --- system status ----------------------------------------------------------------------------

THROTTLE_BITS = {0: "under-voltage", 1: "ARM frequency capped", 2: "throttled", 3: "soft temperature limit",
                 16: "under-voltage occurred", 17: "frequency capping occurred", 18: "throttling occurred",
                 19: "soft temperature limit occurred"}


def _run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


_cache = {}


def cached(key, ttl, fn):
    """nmcli & co. take up to a second on the Zero: don't run them on every poll."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def wifi_status():
    return cached("wifi", 30, _wifi_status)


def _wifi_status():
    out = {"interface": "wlan0"}
    for line in _read("/proc/net/wireless").splitlines():
        if line.strip().startswith("wlan0:"):
            parts = line.split()
            try:
                out["quality"] = float(parts[2].rstrip("."))
                out["signal_dbm"] = float(parts[3].rstrip("."))
            except (IndexError, ValueError):
                pass
    for line in _run(["nmcli", "-t", "-f", "GENERAL.CONNECTION,GENERAL.STATE,IP4.ADDRESS", "dev", "show", "wlan0"]).splitlines():
        k, _, v = line.partition(":")
        if k == "GENERAL.CONNECTION":
            out["connection"] = v
        elif k == "GENERAL.STATE":
            out["state"] = v
        elif k.startswith("IP4.ADDRESS") and "ip" not in out:
            out["ip"] = v
    return out


def system_status(cam):
    st = dict(cached("status", 3, lambda: _system_status()))
    st.update(time=time.time(), mjpeg_clients=cam.mjpeg_clients, camera_uptime=time.time() - cam.started)
    return st


def _system_status():
    temp = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    throttled = _run(["vcgencmd", "get_throttled"]).partition("=")[2]
    try:
        flags = int(throttled, 16)
    except ValueError:
        flags = None
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) // 1024
    active = _run(["systemctl", "is-active", *[f"{u}.service" for u in UNITS]]).splitlines()
    return {
        "uptime": float(_read("/proc/uptime").split()[0] or 0),
        "temperature": int(temp) / 1000 if temp.isdigit() else None,
        "throttled": {"raw": throttled, "flags": [n for b, n in THROTTLE_BITS.items() if flags is not None and flags >> b & 1]},
        "load": os.getloadavg(),
        "memory_mb": mem,
        "wifi": wifi_status(),
        "services": dict(zip(UNITS, active + ["unknown"] * (len(UNITS) - len(active)))),
    }


def journal(unit, lines):
    if unit not in UNITS:
        raise ValueError("unknown unit")
    lines = max(10, min(500, int(lines)))
    return _run(["journalctl", "-u", f"{unit}.service", "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=10)


# --- HTTP ------------------------------------------------------------------------------------

class WebHandler(camera.Handler):
    auth: camera.AuthFile = None            # admin login (the only one accepted here)
    stream_auth: camera.AuthFile = None     # the :8080 login, changeable from here
    realm = "doorspy"
    server_version = "doorspy"
    # keep-alive: one TLS handshake per browser connection instead of one per request,
    # which on the Zero's ARMv6 is the difference between snappy and sluggish
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        # the listening socket is wrapped with do_handshake_on_connect=False so a slow client
        # can't stall accept(); finish the handshake here, in this connection's thread
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()

    def handle(self):
        try:
            super().handle()
        except (ssl.SSLError, ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    # --- helpers ---
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("expected application/json")
        n = int(self.headers.get("Content-Length", "0"))
        if n > 65536:
            raise ValueError("body too large")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def send_io(self, req):
        resp = io_call(req)
        self.send_json(resp, 502 if "error" in resp else 200)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")     # a reload must always fetch the current page
        self.end_headers()
        self.wfile.write(body)

    # --- routes ---
    def do_GET(self):
        if not self.authorised():
            return
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path == "/":
                self.send_file(os.path.join(WWW_DIR, "index.html"), "text/html; charset=utf-8")
            elif path in ("/api/snapshot.jpeg", "/api/stream.mjpeg"):
                self.close_connection = True        # the stream has no length; drop the connection after it
                self.send_snapshot() if path == "/api/snapshot.jpeg" else self.send_stream()
            elif path == "/api/config":
                self.send_json({"camera": self.cam.current(), "io": io_call({"cmd": "state"}),
                                "system": system_status(self.cam), "user": self.user,
                                "stream_user": self.stream_auth.user()})
            elif path == "/api/camera/meta":        # cheap poll for the Camera tab read-outs
                c = self.cam.current()
                self.send_json({"metadata": c["metadata"], "mjpeg_clients": c["mjpeg_clients"]})
            elif path == "/api/io/state":
                self.send_io({"cmd": "state"})
            elif path == "/api/system/status":
                self.send_json(system_status(self.cam))
            elif path == "/api/system/log":
                self.send_json({"unit": query.get("unit", ["doorspy-cam"])[0],
                                "log": journal(query.get("unit", ["doorspy-cam"])[0], query.get("lines", ["100"])[0])})
            else:
                self.send_error(404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            log.exception("GET %s failed", path)
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self.authorised():
            return
        path = urlsplit(self.path).path
        try:
            body = self.read_json()
            if path == "/api/camera/controls":
                self.send_json({"controls": self.cam.set_controls(body)})
            elif path == "/api/camera/settings":
                new = {**self.cam.settings, **body}
                new["controls"] = self.cam.settings["controls"]
                self.send_json({"settings": self.cam.reconfigure(new)})
            elif path == "/api/camera/reset":
                self.send_json({"settings": self.cam.reconfigure(camera.DEFAULTS)})
            elif path.startswith("/api/io/light/"):
                self.send_io({"cmd": "light", "key": path.rsplit("/", 1)[1], "set": body})
            elif path == "/api/io/env":
                self.send_io({"cmd": "env", "set": body})
            elif path == "/api/io/indicate":
                self.send_io({"cmd": "indicate", "name": body.get("name"), "hold": body.get("hold", 5)})
            elif path == "/api/system/service":
                self.send_io({"cmd": "service", "unit": body.get("unit"), "action": body.get("action")})
            elif path == "/api/auth/password":
                if not self.auth.check(self.user, str(body.get("current", ""))):
                    raise ValueError("current password is wrong")
                self.auth.set(str(body.get("user") or self.user), str(body.get("new", "")))
                self.send_json({"ok": True, "user": body.get("user") or self.user})
            elif path == "/api/auth/stream":        # the :8080 login; confirmed with the admin password
                if not self.auth.check(self.user, str(body.get("current", ""))):
                    raise ValueError("your (admin) password is wrong")
                self.stream_auth.set(str(body.get("user") or "guest"), str(body.get("new", "")))
                self.send_json({"ok": True, "user": body.get("user") or "guest"})
            else:
                self.send_error(404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            log.exception("POST %s failed", path)
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)


class HttpsServer(camera.Server):
    def handle_error(self, request, client_address):
        # a browser probing the port or rejecting the certificate is not worth a traceback
        log.debug("connection from %s failed", client_address[0], exc_info=True)


def start_https(cam, web_auth, stream_auth, port=HTTPS_PORT):
    """Second server next to :8080; skipped (with a log line) if the cert or auth file is missing,
    so the camera itself never fails because of the UI."""
    for path in (CERT_FILE, KEY_FILE, web_auth.path):
        if not os.path.exists(path):
            log.error("web UI disabled: %s missing (run install.sh)", path)
            return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    WebHandler.cam = cam
    WebHandler.auth = web_auth
    WebHandler.auths = (web_auth,)
    WebHandler.stream_auth = stream_auth
    srv = HttpsServer(port, WebHandler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=srv.serve_forever, daemon=True, name="https").start()
    log.info("web UI on https://:%d/ (%s)", port, WWW_DIR)
    return srv
