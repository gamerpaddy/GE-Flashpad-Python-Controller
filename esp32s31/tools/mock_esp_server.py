#!/usr/bin/env python
"""Mock of the ESP32-S31 FlashPad controller HTTP API (docs/API.md) - Python standard library only.

Serves www/ (or www_gz/ with --gz, like the firmware) and implements every endpoint with a synthetic detector:
  * frame  = 2048x2048 u16: dark offset + FPN + gradient + phantom + noise (mean ~3000), with the real panel's defects
             (dead rows 48 / 1093, dead column 783, ~0 ADU);  dark = ~1789 ADU + fixed pattern + noise
  * capture state machine: arming -> wait_window -> exposing -> readout -> receiving (blocks 0..2048) -> verifying
             [-> saving_dark] -> done -> idle; abort, X-ray master switch (auto clears), relay timing, test pulse
  * config GET/POST, /api/log, /api/validate_fast, /api/reinit, /api/dark/from_frame
  * mock-only helpers (NOT part of the contract): POST /mock/upload?name=<file> stores the body under --upload-dir
    (used by the browser tests to hand saved files to Pillow/numpy), GET /mock/uploads lists them.

  python tools/mock_esp_server.py [--port 8080] [--speed 1] [--rate MB/s] [--empty] [--gz]
"""
import argparse
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
import zlib
from array import array
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

W = H = 2048
NBLOCKS = 2048
TICKS_PER_MS = 26.3e3
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".md": "text/markdown; charset=utf-8"}


# ---------------------------------------------------------------------------------------------- synthetic frames
class Scene:
    """Builds the noise-free signal / dark once (pure Python, ~3 s), then adds fresh noise per capture (~1 s)."""

    def __init__(self, seed=1):
        self.rnd = random.Random(seed)
        self.pool_x = self._pool(30.0)
        self.pool_d = self._pool(21.0)
        self.clean_sig = None
        self.clean_dark = None
        self.ready = threading.Event()

    def _pool(self, sigma, n=1 << 20):
        g = self.rnd.gauss
        return array("h", [int(round(g(0, sigma))) for _ in range(n)])

    def build(self):
        rnd = self.rnd
        col = [1100 + int(500 * (1 - x / W)) for x in range(W)]               # heel-effect-like gradient
        shapes = [  # (kind, cx, cy, rx, ry, min_factor)  factor = 1 - (1-min)*thickness(ellipsoid)
            ("e", 1024, 1000, 720, 880, 0.55),     # soft tissue
            ("e", 880, 1080, 130, 650, 0.12),      # bone (cylinder-ish)
            ("e", 1300, 800, 90, 90, 0.04),        # dense blob
            ("r", 1480, 560, 150, 90, 0.02),       # metal block
        ]
        sig = array("H")
        for y in range(H):
            base = 150 * y // H
            row = [c + base for c in col]
            for kind, cx, cy, rx, ry, fmin in shapes:
                dy = (y - cy) / ry
                if abs(dy) >= 1:
                    continue
                half = rx * math.sqrt(1 - dy * dy)
                xa, xb = max(0, int(cx - half)), min(W - 1, int(cx + half))
                for x in range(xa, xb + 1):
                    if kind == "e":
                        u = (x - cx) / rx
                        th = math.sqrt(max(0.0, 1 - u * u - dy * dy))
                        row[x] = int(row[x] * (1 - (1 - fmin) * th))
                    else:
                        row[x] = int(row[x] * fmin)
            # step wedge (bottom left)
            if 1750 <= y < 1950:
                for i, f in enumerate((0.9, 0.7, 0.5, 0.35, 0.2)):
                    for x in range(200 + i * 160, 200 + (i + 1) * 160):
                        row[x] = int(row[x] * f)
            sig.extend(row)
        coff = [int(rnd.gauss(0, 5)) + (6 if x % 64 == 0 else 0) for x in range(W)]       # fixed-pattern noise
        roff = [int(rnd.gauss(0, 3)) for _ in range(H)]
        dk = array("H")
        for y in range(H):
            r = roff[y]
            dk.extend([1789 + c + r + ((x * 7 + y * 3) & 7) for x, c in enumerate(coff)])
        # the offset is part of the raw X-ray frame too (raw = dark + signal)
        for y in range(H):
            o = y * W
            sig[o:o + W] = array("H", [a + b for a, b in zip(sig[o:o + W], dk[o:o + W])])
        self.clean_sig, self.clean_dark = sig, dk
        self.ready.set()

    def frame(self, kind):
        clean = self.clean_sig if kind == "xray" else self.clean_dark
        pool = self.pool_x if kind == "xray" else self.pool_d
        rnd = self.rnd
        out = array("H")
        for y in range(H):
            o = rnd.randrange(0, len(pool) - W)
            out.extend(array("H", [a + b if a + b > 0 else 0 for a, b in zip(clean[y * W:(y + 1) * W], pool[o:o + W])]))
        if kind == "xray":                      # dead lines: ~0 ADU
            for r in (48, 1093):
                out[r * W:(r + 1) * W] = array("H", [rnd.randint(0, 3) for _ in range(W)])
            for y in range(H):
                out[y * W + 783] = rnd.randint(0, 3)
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()


def frame_stats(b):
    a = array("H")
    a.frombytes(b)
    if sys.byteorder == "big":
        a.byteswap()
    sub = a[::7]
    n = len(sub)
    mean = sum(sub) / n
    var = sum((v - mean) ** 2 for v in sub) / n
    return {"min": min(a), "max": max(a), "mean": round(mean, 1), "std": round(math.sqrt(var), 1), "crc32": "%08x" % (zlib.crc32(b) & 0xFFFFFFFF), "bytes": len(b)}


# ---------------------------------------------------------------------------------------------- device model
class Mock:
    def __init__(self, args):
        self.args = args
        self.t0 = time.time()
        self.lock = threading.RLock()
        self.scene = Scene(args.seed)
        self.log_lines = deque(maxlen=200)
        self.cfg = {
            "detector_ip": "192.168.1.30", "link_mode": "adv100fd", "setup_mode": "session", "fire_trigger": "window_open", "early_close": False,
            "relay_gpio": 2, "relay_active_high": True, "relay_max_pulse_ms": 3000, "xray_idle_disable_min": 10,
            "ap_ssid": "FlashPad-ESP", "ap_pass": "flashpad1", "sta_ssid": "", "sta_pass": "",
            "defaults": {"window_ms": 2000, "scrubs": 0, "exposure_ms": 250, "fire_delay_ms": 20}}
        self.det_state = "ready"
        self.capture = {"state": "idle", "step": "", "type": "xray", "blocks": 0, "blocks_total": NBLOCKS, "started": 0.0, "error": "", "fast_fallback": False}
        self.abort = threading.Event()
        self.frame = None
        self.frame_meta = None
        self.frame_seq = 0
        self.frame_t = 0.0
        self.dark = None
        self.dark_meta = None
        self.dark_saving_until = 0.0
        self.dark_t = 0.0
        self.master = False
        self.master_t = 0.0
        self.relay_until = 0.0
        self.fires = 0
        self.last_fire_ms = 0
        self.validate = None
        self.log("mock ESP32-S31 FlashPad controller started (fw 0.1.0-mock)")

    # ---- helpers
    def log(self, msg):
        with self.lock:
            self.log_lines.append("[%8.3f] %s" % (time.time() - self.t0, msg))

    def sleep(self, secs):
        """Sleep secs/speed in 20 ms slices; False if aborted."""
        end = time.time() + secs / self.args.speed
        while time.time() < end:
            if self.abort.is_set():
                return False
            time.sleep(min(0.02, max(0, end - time.time())))
        return not self.abort.is_set()

    def set_cap(self, **kw):
        with self.lock:
            self.capture.update(kw)

    # ---- startup preload
    def preload(self):
        self.scene.build()
        self.log("synthetic scene built")
        if self.args.empty:
            return
        d = self.scene.frame("dark")
        with self.lock:
            self.dark = d
            self.dark_meta = dict(frame_stats(d), window_ticks=int(2000 * TICKS_PER_MS), window_ms=2000, scrubs=0)
            self.dark_t = time.time()
        x = self.scene.frame("xray")
        self._commit_frame(x, "xray", 2000, 0, 250)
        self.log("preloaded dark + X-ray frame")

    def _commit_frame(self, data, typ, window_ms, scrubs, exposure_ms):
        st = frame_stats(data)
        with self.lock:
            self.frame_seq += 1
            self.frame = data
            self.frame_t = time.time()
            self.frame_meta = dict(st, type=typ, image_id=self.frame_seq & 0xFFFF, window_ticks=int(round(window_ms * TICKS_PER_MS)), window_ms=window_ms,
                                   scrubs=scrubs, exposure_ms=exposure_ms)

    # ---- X-ray
    def relay_on(self):
        return time.time() < self.relay_until

    def fire(self, ms, why):
        ms = min(ms, self.cfg["relay_max_pulse_ms"])
        with self.lock:
            self.relay_until = time.time() + ms / 1000.0
            self.fires += 1
            self.last_fire_ms = ms
        self.log("RELAY ON %d ms (%s)" % (ms, why))

    def check_master_timeout(self):
        with self.lock:
            if self.master and time.time() - self.master_t > self.cfg["xray_idle_disable_min"] * 60:
                self.master = False
                self.log("X-ray master auto-disabled (idle timeout)")

    # ---- capture state machine (own thread)
    def start_capture(self, p):
        threading.Thread(target=self._capture, args=(p,), daemon=True).start()

    def _capture(self, p):
        typ, win, scr, exp, dly = p["type"], p["window_ms"], p["scrubs"], p.get("exposure_ms", 0), p.get("fire_delay_ms", 0)
        self.abort.clear()
        gen = {}
        threading.Thread(target=lambda: gen.update(d=self.scene.frame(typ)), daemon=True).start()
        self.set_cap(state="arming", step="arming: script download + EXECUTE", type=typ, blocks=0, error="", started=time.time())
        self.det_state = "busy"
        self.log("capture %s window=%s ms scrubs=%s exposure=%s delay=%s" % (typ, win, scr, exp, dly))
        ok = self.sleep(1.2 if self.cfg["setup_mode"] == "full" else 0.5)
        if ok:
            self.set_cap(state="wait_window", step="waiting for window open (0x40000 [1])")
            ok = self.sleep(0.25)
        if ok:
            fired = typ == "xray" and self.master
            self.set_cap(state="exposing", step=("firing relay in %d ms" % dly) if fired else ("armed - external firing" if typ == "xray" else "dark window"))
            t_open = time.time()
            if fired:
                ok = self.sleep(dly / 1000.0)
                if ok:
                    self.fire(exp, "window_open")
                    self.set_cap(step="relay ON (%d ms)" % exp)
            rest = win / 1000.0 - (time.time() - t_open) * self.args.speed
            if ok and rest > 0:
                ok = self.sleep(rest)
        if ok:
            self.set_cap(state="readout", step="detector readout (0x50000 -> 0x30000)")
            ok = self.sleep(0.7)
        if ok:
            self.set_cap(state="receiving", step="receiving image blocks")
            n = 64
            for i in range(1, n + 1):
                if not self.sleep(1.0 / n):
                    ok = False
                    break
                self.set_cap(blocks=i * NBLOCKS // n)
        if ok:
            self.set_cap(state="verifying", step="bitmap complete, CRC")
            ok = self.sleep(0.2)
        if ok:
            while "d" not in gen and not self.abort.is_set():
                time.sleep(0.05)
            ok = "d" in gen
        if not ok:
            self.relay_until = 0
            self.set_cap(state="aborted", step="aborted")
            self.log("capture aborted")
            self.det_state = "ready"
            time.sleep(1.5)
            self.set_cap(state="idle", step="")
            return
        self._commit_frame(gen["d"], typ, win, scr, exp)
        self.log("frame #%d %s stored: mean %.1f std %.1f crc %s" % (self.frame_seq, typ, self.frame_meta["mean"], self.frame_meta["std"], self.frame_meta["crc32"]))
        if typ == "dark":
            self.set_cap(state="saving_dark", step="writing dark partition (flash)")
            self._store_dark(gen["d"], self.frame_meta)
            self.sleep(2.0)
        self.set_cap(state="done", step="done", blocks=NBLOCKS)
        self.det_state = "ready"
        time.sleep(3.0)
        with self.lock:
            if self.capture["state"] == "done":
                self.set_cap(state="idle", step="")

    def _store_dark(self, data, meta):
        with self.lock:
            self.dark = data
            self.dark_meta = {k: meta[k] for k in ("min", "max", "mean", "std", "crc32", "bytes", "window_ticks", "window_ms", "scrubs")}
            self.dark_t = time.time()
            self.dark_saving_until = time.time() + 3.0 / self.args.speed

    def reinit(self):
        def run():
            self.abort.set()
            self.det_state = "discovering"
            self.log("re-init: discovery + PORT_SETUP + Script7")
            time.sleep(1.0 / self.args.speed)
            self.det_state = "connecting"
            time.sleep(1.0 / self.args.speed)
            self.det_state = "ready"
            self.abort.clear()
            self.set_cap(state="idle", step="")
        threading.Thread(target=run, daemon=True).start()

    def run_validate(self):
        def run():
            self.validate = {"state": "running", "msg": "dark in full mode..."}
            time.sleep(1.5 / self.args.speed)
            self.validate = {"state": "running", "msg": "dark in fast mode..."}
            time.sleep(1.5 / self.args.speed)
            self.validate = {"state": "done", "msg": "fast mode matches full mode (mock)", "mean_full": 1789.2, "mean_fast": 1789.9, "std_full": 21.4, "std_fast": 21.7}
        threading.Thread(target=run, daemon=True).start()

    # ---- status
    def status(self):
        self.check_master_timeout()
        with self.lock:
            now = time.time()
            c = dict(self.capture)
            elapsed = int((now - c.pop("started")) * 1000) if c["state"] not in ("idle",) else 0
            c["elapsed_ms"] = elapsed
            fm, dm = self.frame_meta, self.dark_meta
            s = {
                "fw": "0.1.0-mock", "uptime_s": int(now - self.t0),
                "link": {"up": True, "speed": 100, "duplex": "full", "mode": self.cfg["link_mode"], "ip": "192.168.1.1"},
                "wifi": {"ap_ssid": self.cfg["ap_ssid"], "ap_ip": "192.168.4.1", "sta_connected": False, "sta_ssid": "", "sta_ip": ""},
                "detector": {"ip": self.cfg["detector_ip"], "state": self.det_state, "mac": "40:F4:A0:00:78:4D", "serial": "UA45829-7", "model": "5340000-7",
                             "fw": "1.6.0.4.2.0.1.3", "setup_mode": self.cfg["setup_mode"], "script7_done": True},
                "capture": c,
                "frame": ({"valid": True, "seq": self.frame_seq, "type": fm["type"], "image_id": fm["image_id"], "window_ticks": fm["window_ticks"], "window_ms": fm["window_ms"],
                           "scrubs": fm["scrubs"], "exposure_ms": fm["exposure_ms"], "min": fm["min"], "max": fm["max"], "mean": fm["mean"], "std": fm["std"],
                           "crc32": fm["crc32"], "bytes": fm["bytes"], "age_s": int(now - self.frame_t)}
                          if fm else {"valid": False, "seq": self.frame_seq}),
                "dark": ({"valid": True, "window_ticks": dm["window_ticks"], "window_ms": dm["window_ms"], "scrubs": dm["scrubs"], "mean": dm["mean"], "std": dm["std"],
                          "crc32": dm["crc32"], "age_s": int(now - self.dark_t), "saving": now < self.dark_saving_until}
                         if dm else {"valid": False, "saving": False}),
                "xray": {"master_enabled": self.master, "relay_on": self.relay_on(), "gpio": self.cfg["relay_gpio"], "active_high": self.cfg["relay_active_high"],
                         "max_pulse_ms": self.cfg["relay_max_pulse_ms"], "fires": self.fires, "last_fire_ms": self.last_fire_ms},
                "heap": {"free_internal": 123456, "free_psram": 7000000},
            }
            if self.validate:
                s["validate"] = self.validate
            return s


# ---------------------------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-esp/0.1"

    def log_message(self, fmt, *a):
        if self.server.mock.args.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    # -- responses
    def send_bytes(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj).encode(), "application/json")

    def err(self, code, msg):
        self.send_json({"ok": False, "error": msg}, code)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            v = json.loads(raw.decode("utf-8"))
            return v if isinstance(v, dict) else None
        except ValueError:
            return None

    # -- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        m = self.server.mock
        path = urlparse(self.path).path
        if path == "/api/status":
            return self.send_json(m.status())
        if path == "/api/frame":
            return self.send_frame(m.frame, m.frame_meta, False)
        if path == "/api/dark":
            return self.send_frame(m.dark, m.dark_meta, True)
        if path == "/api/config":
            cfg = json.loads(json.dumps(m.cfg))
            cfg["ap_pass"] = ""
            cfg["sta_pass"] = ""
            return self.send_json(cfg)
        if path == "/api/log":
            return self.send_bytes(200, ("\n".join(m.log_lines) + "\n").encode(), "text/plain; charset=utf-8")
        if path == "/mock/uploads":
            return self.send_json(sorted(os.listdir(m.args.upload_dir)))
        if path.startswith("/api/"):
            return self.err(404, "unknown endpoint")
        return self.send_static(path)

    def send_static(self, path):
        a = self.server.mock.args
        if path in ("", "/"):
            path = "/index.html"
        name = os.path.normpath(path.lstrip("/")).replace("\\", "/")
        base = os.path.join(ROOT, "www_gz") if a.gz else os.path.join(ROOT, "www")
        fp = os.path.join(base, name + (".gz" if a.gz else ""))
        if not os.path.abspath(fp).startswith(os.path.abspath(base)) or not os.path.isfile(fp):
            return self.err(404, "not found")
        with open(fp, "rb") as f:
            body = f.read()
        ctype = MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
        self.send_bytes(200, body, ctype, {"Content-Encoding": "gzip"} if a.gz else None)

    def send_frame(self, data, meta, is_dark):
        m = self.server.mock
        if data is None or meta is None:
            return self.err(404, "no %s yet" % ("dark" if is_dark else "frame"))
        hdr = {"X-Frame-Crc32": meta["crc32"], "X-Window-Ticks": str(meta["window_ticks"]), "X-Window-Ms": str(meta["window_ms"]), "X-Scrubs": str(meta["scrubs"]),
               "X-Frame-Mean": str(meta["mean"]), "X-Frame-Std": str(meta["std"]), "X-Frame-Type": "dark" if is_dark else meta["type"],
               "X-Exposure-Ms": str(meta.get("exposure_ms", 0)), "X-Image-Id": str(meta.get("image_id", 0))}
        if is_dark:
            hdr["X-Dark-Age-S"] = str(int(time.time() - m.dark_t))
        else:
            hdr["X-Frame-Seq"] = str(m.frame_seq)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in hdr.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        chunk, rate = 65536, m.args.rate * 1e6
        try:
            for i in range(0, len(data), chunk):
                self.wfile.write(data[i:i + chunk])
                if rate:
                    time.sleep(chunk / rate)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    # -- POST
    def do_POST(self):
        m = self.server.mock
        u = urlparse(self.path)
        path = u.path
        if path == "/mock/upload":
            name = os.path.basename(parse_qs(u.query).get("name", ["upload.bin"])[0])
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            with open(os.path.join(m.args.upload_dir, name), "wb") as f:
                f.write(body)
            return self.send_json({"ok": True, "name": name, "bytes": len(body)})
        j = self.body_json()
        if j is None:
            return self.err(400, "invalid JSON body")
        if path == "/api/capture":
            return self.post_capture(j)
        if path == "/api/abort":
            m.relay_until = 0
            m.abort.set()
            m.log("ABORT: relay OFF, capture aborted")
            return self.send_json({"ok": True})
        if path == "/api/reinit":
            m.reinit()
            return self.send_json({"ok": True})
        if path == "/api/xray/master":
            en = bool(j.get("enabled"))
            with m.lock:
                m.master = en
                m.master_t = time.time()
            m.log("X-ray master %s" % ("ENABLED" if en else "disabled"))
            return self.send_json({"ok": True, "master_enabled": en})
        if path == "/api/xray/test":
            try:
                ms = int(j.get("ms", 100))
            except (TypeError, ValueError):
                return self.err(400, "ms must be a number")
            if not 1 <= ms <= 500:
                return self.err(400, "ms must be 1..500")
            if not m.master:
                return self.err(409, "X-ray master switch is off")
            if m.relay_on():
                return self.err(409, "relay already active")
            m.fire(ms, "test pulse")
            return self.send_json({"ok": True})
        if path == "/api/dark/from_frame":
            if m.frame is None:
                return self.err(409, "no frame yet")
            if time.time() < m.dark_saving_until or m.capture["state"] not in ("idle", "done", "error", "aborted"):
                return self.err(409, "busy")
            m._store_dark(m.frame, m.frame_meta)
            m.log("dark := current frame (saving to flash)")
            return self.send_json({"ok": True})
        if path == "/api/config":
            reboot = False
            with m.lock:
                for k, v in j.items():
                    if k == "defaults" and isinstance(v, dict):
                        m.cfg["defaults"].update(v)
                    elif k in m.cfg:
                        if k in ("detector_ip", "link_mode", "ap_ssid", "ap_pass", "sta_ssid", "sta_pass", "relay_gpio", "relay_active_high") and m.cfg[k] != v:
                            reboot = True
                        m.cfg[k] = v
            m.log("config updated: %s%s" % (", ".join(sorted(k for k in j if "pass" not in k)), " (reboot required)" if reboot else ""))
            return self.send_json({"ok": True, "reboot_required": reboot})
        if path == "/api/validate_fast":
            m.run_validate()
            return self.send_json({"ok": True})
        return self.err(404, "unknown endpoint")

    def post_capture(self, j):
        m = self.server.mock
        typ = j.get("type")
        try:
            win, scr = float(j.get("window_ms")), int(j.get("scrubs", 0))
            exp, dly = float(j.get("exposure_ms", 0) or 0), float(j.get("fire_delay_ms", 0) or 0)
        except (TypeError, ValueError):
            return self.err(400, "window_ms / scrubs / exposure_ms / fire_delay_ms must be numbers")
        if typ not in ("xray", "dark"):
            return self.err(400, "type must be xray or dark")
        if not 20 <= win <= 160000:
            return self.err(400, "window_ms must be 20..160000")
        if not 0 <= scr <= 255:
            return self.err(400, "scrubs must be 0..255")
        if typ == "xray":
            mx = m.cfg["relay_max_pulse_ms"]
            if not 1 <= exp <= mx:
                return self.err(400, "exposure_ms must be 1..%d" % mx)
            if not 0 <= dly <= 5000:
                return self.err(400, "fire_delay_ms must be 0..5000")
            if win < dly + exp + 200:
                return self.err(400, "window_ms must be >= fire_delay_ms + exposure_ms + 200 (= %d)" % (dly + exp + 200))
        with m.lock:
            if m.capture["state"] not in ("idle", "done", "error", "aborted") or m.det_state not in ("ready",):
                return self.err(409, "a capture is already running" if m.det_state == "busy" else "detector not ready (%s)" % m.det_state)
            if time.time() < m.dark_saving_until:
                return self.err(409, "dark is still being saved")
            m.set_cap(state="arming", step="starting", type=typ, blocks=0, started=time.time())
        m.start_capture({"type": typ, "window_ms": int(win), "scrubs": scr, "exposure_ms": int(exp), "fire_delay_ms": int(dly)})
        self.send_json({"ok": True}, 202)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--speed", type=float, default=1.0, help="time acceleration for the capture sequence (default 1 = real time)")
    ap.add_argument("--rate", type=float, default=0.0, help="throttle /api/frame to this many MB/s (0 = unlimited)")
    ap.add_argument("--empty", action="store_true", help="start with no frame and no dark (tests the 404 paths)")
    ap.add_argument("--gz", action="store_true", help="serve www_gz/*.gz with Content-Encoding: gzip, like the firmware")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--upload-dir", default=os.path.join(tempfile.gettempdir(), "flashpad_mock_uploads"))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.upload_dir, exist_ok=True)
    mock = Mock(a)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    srv.mock = mock
    threading.Thread(target=mock.preload, daemon=True).start()
    print("mock FlashPad ESP on http://%s:%d/  (speed x%g, upload dir %s)" % (a.host, a.port, a.speed, a.upload_dir), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
