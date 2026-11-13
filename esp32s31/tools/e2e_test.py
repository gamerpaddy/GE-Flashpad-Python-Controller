#!/usr/bin/env python3
"""
e2e_test.py - end-to-end test of the ESP32-S31 FlashPad controller against the PC-side detector simulator.

Stdlib only (urllib/socket/zlib/subprocess).  It drives the ESP through its HTTP API (esp32s31/docs/API.md) and
compares the frame the ESP returns with the ground truth held by the simulator (flashpad_sim.py, GET /truth).

  # bench: ESP on 192.168.1.1, simulator running on the PC (NIC 192.168.1.30)
  python flashpad_sim.py                                  # terminal 1
  python e2e_test.py --esp http://192.168.1.1             # terminal 2            (add --only a,b,g for a subset)

  # no hardware: self-test of the simulator + this test with a built-in *software ESP* (RefHost)
  python e2e_test.py --sim-only

--sim-only starts a simulator subprocess on loopback (time-scaled) and replaces the ESP by RefHost, a small in-process
host that implements the ESP's documented behaviour with the REAL flashpad_acquire.py builders (persistent session,
beacon ACKs, two-phase EXECUTE, 0x40000[1] wait, cmd 9 [0..7], 0x41/0x98, bitmap + selective / whole-image repair, fast->full
fallback, abort).  It validates the simulator and the scenarios - it does NOT test the firmware.

Scenarios (ids for --only):
  a  dark capture: frame CRC from the ESP == simulator CRC
  b  X-ray capture with the master switch OFF: no relay pulse, sequence completes, CRC ok
  c  1 / 3 / 5 % dropped blocks -> selective cmd 9 repair (+ c2: repeated loss, several repair rounds)
  d  >256 dropped blocks -> whole-image resend
  e  needs-ROE simulator + setup_mode=fast -> fast->full fallback, capture still succeeds
  f  soak: N consecutive captures, free heap / PSRAM drift from /api/status
  g  abort in the middle of an X-ray capture, then capture again
  h  protocol health: protocol_mismatches == 0, no SeqId drops, no un-ACKed detector packets, no flow violations
  m  (optional, not in the default set) setup modes full / session / fast / combined
Exit status is non-zero when any scenario fails.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("FLASHPAD_REPO") or os.path.abspath(os.path.join(HERE, "..", ".."))
FRAME_BYTES = 2048 * 2048 * 2
NBLOCKS = 2048
TICKS_PER_MS = 26.3e3


# =============================================================================== HTTP helpers
def http(method, url, body=None, timeout=10, raw=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            hdrs = dict(r.headers.items())
            code = r.status
    except urllib.error.HTTPError as e:
        payload, hdrs, code = e.read(), dict(e.headers.items()), e.code
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        return 0, {"ok": False, "error": "connection: %s" % e}, {}
    if raw:
        return code, payload, hdrs
    try:
        return code, json.loads(payload or b"{}"), hdrs
    except ValueError:
        return code, {"raw": payload[:200].decode("utf-8", "replace")}, hdrs


class SimClient:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def truth(self):
        code, d, _ = http("GET", self.base + "/truth")
        if code != 200:
            raise RuntimeError("simulator /truth not reachable at %s (%s)" % (self.base, d))
        return d

    def config(self, **kw):
        return http("POST", self.base + "/config", kw)[1]

    def reset(self, counters=True, session=False):
        return http("POST", self.base + "/reset", {"counters": counters, "session": session})[1]

    def frame(self):
        return http("GET", self.base + "/frame", raw=True, timeout=30)[1]


# =============================================================================== results / drivers
class CaptureResult:
    def __init__(self):
        self.ok = False
        self.state = ""
        self.error = ""
        self.crc32 = None            # hex string of the frame the ESP says it holds
        self.frame = None            # bytes or None
        self.frame_crc = None        # crc we computed over the downloaded frame
        self.seconds = 0.0
        self.fast_fallback = False
        self.states = []
        self.relay_seen = False
        self.fires_delta = 0
        self.heap = {}
        self.status = {}


def crc_hex(b):
    return "%08x" % (zlib.crc32(b) & 0xFFFFFFFF)


class Driver:
    name = "?"
    heap_meaningful = True

    def status(self):
        raise NotImplementedError

    def start_capture(self, kind, window_ms, scrubs=0, exposure_ms=250, fire_delay_ms=20):
        raise NotImplementedError

    def wait_done(self, timeout, fetch_frame=True, on_poll=None):
        raise NotImplementedError

    def abort(self):
        raise NotImplementedError

    def set_config(self, **kw):
        raise NotImplementedError

    def get_config(self):
        raise NotImplementedError

    def reinit(self):
        raise NotImplementedError

    def master(self, enabled):
        raise NotImplementedError

    def wait_ready(self, timeout):
        raise NotImplementedError

    def capture(self, kind, window_ms, scrubs=0, exposure_ms=250, fire_delay_ms=20, timeout=120, fetch_frame=True):
        ok, err = self.start_capture(kind, window_ms, scrubs, exposure_ms, fire_delay_ms)
        if not ok:
            r = CaptureResult()
            r.error = err
            r.state = "rejected"
            return r
        return self.wait_done(timeout, fetch_frame)


class EspDriver(Driver):
    name = "ESP"

    def __init__(self, base):
        self.base = base.rstrip("/")
        self._seq0 = None
        self._fires0 = 0
        self._t0 = 0.0
        self._rx_hdr = {}

    def _get_status(self):
        code, d, _ = http("GET", self.base + "/api/status", timeout=5)
        return d if code == 200 else {}

    def status(self):
        return self._get_status()

    def start_capture(self, kind, window_ms, scrubs=0, exposure_ms=250, fire_delay_ms=20):
        st = self._get_status()
        self._seq0 = st.get("frame", {}).get("seq", 0)
        self._fires0 = st.get("xray", {}).get("fires", 0)
        self._t0 = time.time()
        body = {"type": kind, "window_ms": window_ms, "scrubs": scrubs, "exposure_ms": exposure_ms,
                "fire_delay_ms": fire_delay_ms}
        t_end = time.time() + 120
        while True:
            code, d, _ = http("POST", self.base + "/api/capture", body)
            if code != 409 or time.time() > t_end:       # 409 = previous dark still being saved to flash (~30 s)
                break
            time.sleep(1.0)
        self._t0 = time.time()
        if code not in (200, 202) or not d.get("ok", True):
            return False, "POST /api/capture -> %s %s" % (code, d)
        return True, ""

    def wait_done(self, timeout, fetch_frame=True, on_poll=None):
        r = CaptureResult()
        t_end = time.time() + timeout
        seen_active, last_state = False, None
        while time.time() < t_end:
            st = self._get_status()
            cap = st.get("capture", {})
            state = cap.get("state", "")
            if state != last_state:
                r.states.append(state)
                last_state = state
            if st.get("xray", {}).get("relay_on"):
                r.relay_seen = True
            if on_poll:
                on_poll(st)
            fr = st.get("frame", {})
            active = state not in ("idle", "done", "error", "aborted", "")
            seen_active = seen_active or active
            fresh = fr.get("seq", 0) != self._seq0
            if state == "done" and (fresh or seen_active):
                r.state, r.ok = "done", True
                r.status = st
                break
            if state in ("error", "aborted") and (seen_active or fresh):
                r.state, r.error = state, cap.get("error", "")
                r.status = st
                break
            if state == "idle" and not seen_active and time.time() - self._t0 > 10:
                r.state, r.error = "idle", "capture never started"
                break
            time.sleep(0.25)
        else:
            r.state, r.error = "timeout", "no completion within %.0f s (last state %s)" % (timeout, last_state)
        r.seconds = time.time() - self._t0
        st = r.status or self._get_status()
        r.fast_fallback = bool(st.get("capture", {}).get("fast_fallback"))
        r.heap = st.get("heap", {})
        r.fires_delta = st.get("xray", {}).get("fires", 0) - self._fires0
        if r.ok:
            r.crc32 = st.get("frame", {}).get("crc32")
            if fetch_frame:
                code, body, hdr = http("GET", self.base + "/api/frame", raw=True, timeout=120)
                if code == 200:
                    r.frame = body
                    r.frame_crc = crc_hex(body)
                    h = {k.lower(): v for k, v in hdr.items()}
                    r.crc32 = h.get("x-frame-crc32", r.crc32)
                else:
                    r.ok, r.error = False, "GET /api/frame -> %s" % code
        return r

    def abort(self):
        return http("POST", self.base + "/api/abort", {})[1]

    def set_config(self, **kw):
        return http("POST", self.base + "/api/config", kw)[1]

    def get_config(self):
        return http("GET", self.base + "/api/config")[1]

    def reinit(self):
        return http("POST", self.base + "/api/reinit", {})[1]

    def master(self, enabled):
        return http("POST", self.base + "/api/xray/master", {"enabled": bool(enabled)})[1]

    def wait_ready(self, timeout):
        t_end = time.time() + timeout
        while time.time() < t_end:
            st = self._get_status()
            if st.get("detector", {}).get("state") == "ready" and st.get("capture", {}).get("state") in ("idle", "done", "aborted", "error"):
                return True
            time.sleep(0.5)
        return False


# =============================================================================== software ESP (RefHost)
class Aborted(Exception):
    pass


class RefHost(Driver):
    """In-process stand-in for the ESP's detector client, built on the REAL flashpad_acquire.py builders."""
    name = "RefHost(software ESP)"
    heap_meaningful = False

    def __init__(self, detector_ip="127.0.0.1", host_ip="127.0.0.1", det_port=8100, verbose=False):
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        import flashpad_acquire as fp
        self.fp = fp
        self.detector_ip, self.host_ip, self.det_port, self.verbose = detector_ip, host_ip, det_port, verbose
        self.cv = threading.Condition()
        self.evq = collections.deque()
        self.acks = set()
        self.seen_set, self.seen_q = set(), collections.deque()
        self.seq = 1
        self.beacon_seen = threading.Event()
        self.beacons = 0
        self.running = False
        self._abort = threading.Event()
        self.session_ok = False
        self.script7_done = False
        self.setup_mode = "session"
        self.fast_fallback = False
        self.cap = dict(state="idle", step="", blocks=0, error="")
        self.frame_buf = bytearray(FRAME_BYTES)
        self.have = bytearray(NBLOCKS)
        self.bad_len = 0
        self.rx_blocks = 0
        self.frame_seq = 0
        self.frame_info = {}
        self._thread = None
        self._result = None
        self.sig = None

    # ---- lifecycle
    def start(self):
        self.cs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cs.bind(("", 5550))
        self.cs.settimeout(0.2)
        self.isock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.isock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        self.isock.bind(("", 6660))
        self.isock.settimeout(0.2)
        self.running = True
        threading.Thread(target=self._ctrl_rx, daemon=True, name="ref-ctrl").start()
        threading.Thread(target=self._img_rx, daemon=True, name="ref-img").start()

    def stop(self):
        self.running = False
        time.sleep(0.3)
        for s in (self.cs, self.isock):
            try:
                s.close()
            except OSError:
                pass

    def _log(self, m):
        if self.verbose:
            print("   [ref] " + m, flush=True)

    # ---- RX threads (the ESP's high-priority RX task + the image hook)
    def _ctrl_rx(self):
        while self.running:
            try:
                d, a = self.cs.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                continue
            if a[0] != self.detector_ip or len(d) < 8:
                continue
            seq, flag = struct.unpack_from("<II", d, 0)
            if flag != 0:
                with self.cv:
                    self.acks.add(seq)
                    self.cv.notify_all()
                continue
            try:                                           # ACK every detector data packet at once (never ACK an ACK)
                self.cs.sendto(struct.pack("<II", seq, 1), (self.detector_ip, self.det_port))
            except OSError:
                pass
            if len(d) < 16:
                continue
            cmd, plen = struct.unpack_from("<II", d, 8)
            payload = d[16:16 + plen]
            with self.cv:
                if seq in self.seen_set:
                    continue
                self.seen_set.add(seq)
                self.seen_q.append(seq)
                if len(self.seen_q) > 512:
                    self.seen_set.discard(self.seen_q.popleft())
                if cmd == 1:
                    self.beacons += 1
                    self.seq = max(self.seq, seq + 1)
                    self.beacon_seen.set()
                    continue
                self.evq.append((time.perf_counter(), seq, cmd, payload))
                self.cv.notify_all()

    def _img_rx(self):
        while self.running:
            try:
                d, a = self.isock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                continue
            if a[0] != self.detector_ip:
                continue
            if len(d) != 4104:
                self.bad_len += 1
                continue
            blk = struct.unpack_from("<I", d, 4)[0] & 0xFFFF
            if blk >= NBLOCKS:
                continue
            self.frame_buf[blk * 4096:(blk + 1) * 4096] = d[8:]
            self.have[blk] = 1
            self.rx_blocks += 1

    # ---- command helpers
    def _send(self, pdap):
        with self.cv:
            seq = self.seq
            self.seq += 1
        self.cs.sendto(self.fp.make_urp_packet(pdap, reply_port=0, flag=seq), (self.detector_ip, self.det_port))
        return seq

    def _wait_ack(self, seq, timeout=3.0):
        deadline = time.perf_counter() + timeout
        with self.cv:
            while seq not in self.acks:
                if self._abort.is_set():
                    raise Aborted()
                rem = deadline - time.perf_counter()
                if rem <= 0:
                    raise TimeoutError("no bare ACK for host seq %d" % seq)
                self.cv.wait(min(rem, 0.05))
            self.acks.discard(seq)

    def _wait_event(self, match, timeout, since=0.0):
        deadline = time.perf_counter() + timeout
        with self.cv:
            while True:
                if self._abort.is_set():
                    raise Aborted()
                for i, ev in enumerate(self.evq):
                    if ev[0] >= since and match(ev):
                        del self.evq[i]
                        return ev
                rem = deadline - time.perf_counter()
                if rem <= 0:
                    return None
                self.cv.wait(min(rem, 0.05))

    def _xchg(self, pdap, wait_cmd=None, timeout=3.0):
        seq = self._send(pdap)
        self._wait_ack(seq, timeout)
        if wait_cmd is None:
            return None
        ev = self._wait_event(lambda e: e[2] == wait_cmd, timeout)
        if ev is None:
            raise TimeoutError("no reply cmd 0x%X" % wait_cmd)
        return ev[3]

    # ---- session
    def _ensure_session(self):
        if self.session_ok:
            return
        fp = self.fp
        self.cap["step"] = "discovering"
        self.beacon_seen.clear()
        with self.cv:
            self.evq.clear()
            self.acks.clear()
            self.seen_set.clear()
            self.seen_q.clear()
        t_end = time.perf_counter() + 15
        pkt = fp.make_urp_packet(fp.build_system_startup(self.host_ip, 5550), reply_port=0, flag=0)
        while not self.beacon_seen.is_set():
            if time.perf_counter() > t_end:
                raise TimeoutError("discovery: no beacon from the detector")
            self.cs.sendto(pkt, (self.detector_ip, self.det_port))
            self.beacon_seen.wait(0.5)
            if self._abort.is_set():
                raise Aborted()
        self.cap["step"] = "PORT_SETUP"
        self._xchg(fp.build_port_setup(5550, 6660))
        self.cap["step"] = "signature"
        payload = self._xchg(fp.build_signature_request(), wait_cmd=3)
        self.sig = fp.DetectorSignature(payload)
        self.script7_done = False
        self.session_ok = True
        self._log("session up: %s" % self.sig)

    def _download(self, script_pdap):
        seq = self._send(script_pdap)
        self._wait_ack(seq)
        ev = self._wait_event(lambda e: e[2] == 5, 0.5)         # optional cmd 5 [status][count]
        if ev is not None and ev[3][:1] != b"\x00":
            raise RuntimeError("GENERIC_SCRIPT rejected, status %d" % ev[3][0])

    def _execute(self):
        seq = self._send(self.fp.build_execute_script())
        self._wait_ack(seq)
        ev = self._wait_event(lambda e: e[2] == 6, 3.0)
        if ev is None or len(ev[3]) < 5:
            raise TimeoutError("no EXECUTE reply")
        return ev[3][4]

    def _run_script7(self):
        fp = self.fp
        self.cap["step"] = "Script7 (ROE init)"
        self._download(fp.build_script_7_roe_init())
        st = self._execute()
        if st != 0:
            raise RuntimeError("EXECUTE(Script7) status %d" % st)
        if self._wait_event(lambda e: e[2] == 0x10000, 20.0) is None:
            raise TimeoutError("no EXECUTION_COMPLETE for Script7")
        self.script7_done = True

    # ---- capture state machine
    def start_capture(self, kind, window_ms, scrubs=0, exposure_ms=250, fire_delay_ms=20):
        if self._thread and self._thread.is_alive():
            return False, "capture running"
        self._abort.clear()
        self._result = None
        self.cap.update(state="arming", step="", blocks=0, error="")
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._capture_thread, args=(kind, window_ms, scrubs), daemon=True)
        self._thread.start()
        return True, ""

    def _capture_thread(self, kind, window_ms, scrubs):
        try:
            self._capture(kind, window_ms, scrubs, self.setup_mode)
            self.cap["state"] = "done"
        except Aborted:
            try:
                self._send(self.fp.make_pdap(0x0A))
            except OSError:
                pass
            self.session_ok = False
            self.cap.update(state="aborted", step="aborted")
        except Exception as e:
            self.cap.update(state="error", error="%s: %s" % (type(e).__name__, e))
            self.session_ok = False

    def _capture(self, kind, window_ms, scrubs, mode):
        fp = self.fp
        self._ensure_session()
        if mode == "combined":
            pass
        elif mode in ("full", "session") or (mode == "fast" and not self.script7_done):
            self._run_script7()
        ticks = int(round(window_ms * TICKS_PER_MS))
        acq = fp.build_generic_script(0, 0, 0, [
            fp.pack_acquisition(1 if kind == "dark" else 0, image_id=1, no_scrubs=scrubs, scrub_duration=50000,
                                max_expose_time=ticks, tail_time=250000, transfer_mode=0),
            fp.pack_delay(10000)])
        self.have[:] = bytes(NBLOCKS)
        self.rx_blocks = 0
        with self.cv:
            self.evq.clear()
        self.cap.update(state="arming", step="download acquisition script")
        if mode == "combined":
            self._download(fp.build_script_7_roe_init())
        self._download(acq)
        self.cap["step"] = "EXECUTE"
        st = self._execute()
        if st != 0:
            if mode == "fast" and not self.fast_fallback:
                self.fast_fallback = True
                self.setup_mode = "full"
                self.script7_done = False
                self._log("EXECUTE status %d in fast mode -> falling back to full" % st)
                self.cap["step"] = "fast mode failed - falling back to full"
                return self._capture(kind, window_ms, scrubs, "full")
            raise RuntimeError("EXECUTE status %d" % st)
        if mode == "combined":
            self.script7_done = True
        self.cap.update(state="wait_window", step="waiting for 0x40000[1]")
        ev = self._wait_event(lambda e: e[2] == 0x40000 and e[3][:4] == b"\x01\x00\x00\x00",
                              6.0 if mode == "combined" else 3.0)
        if ev is None:
            raise TimeoutError("no window-open event 0x40000[1] after EXECUTE")
        self.cap.update(state="exposing", step="window open (external firing)")
        if self._wait_event(lambda e: e[2] == 0x50000, window_ms / 1000.0 + 15.0) is None:
            raise TimeoutError("no window-end 0x50000")
        self.cap.update(state="readout", step="readout")
        if self._wait_event(lambda e: e[2] == 0x10000, 15.0) is None:
            raise TimeoutError("no EXECUTION_COMPLETE")
        if self._wait_event(lambda e: e[2] == 0x30000, 10.0) is None:
            raise TimeoutError("no 0x30000 image transfer query")
        self.cap.update(state="receiving", step="cmd 9 [0..7] keep-alive, 0x41, 0x98")
        self._send(fp.build_image_xfer_status_reply(missed_ids=list(range(8))))
        rep = self._xchg(fp.build_image_retrival_request(0), wait_cmd=0x41)
        if len(rep) < 8 or struct.unpack_from("<I", rep, 0)[0] <= 4:
            raise RuntimeError("0x41: no image enumerated (%s)" % rep.hex())
        rep = self._xchg(fp.build_image_retrival(host_port=5550, image_port=6660), wait_cmd=0x98)
        if struct.unpack_from("<I", rep, 0)[0] != 0:
            raise RuntimeError("0x98 refused, status %d" % struct.unpack_from("<I", rep, 0)[0])
        t_pull = time.perf_counter()
        rounds = 0
        while True:
            ev = self._wait_event(lambda e: e[2] == 0x30000 and e[3][:4] != b"\xef\xbe\xad\xde", 12.0, since=t_pull)
            if ev is None:
                raise TimeoutError("no 0x30000 after the push (%d/%d blocks)" % (sum(self.have), NBLOCKS))
            missing = [b for b in range(NBLOCKS) if not self.have[b]]
            self.cap["blocks"] = NBLOCKS - len(missing)
            if not missing:
                self._send(fp.build_image_xfer_status_reply(missed_ids=[]))
                break
            rounds += 1
            if rounds > 10:
                raise RuntimeError("giving up after 10 repair rounds, %d blocks still missing" % len(missing))
            self.cap["step"] = "repair round %d: %d block(s) missing" % (rounds, len(missing))
            self._send(fp.build_image_xfer_status_reply(missed_ids=missing))
            t_pull = time.perf_counter()
        data = bytes(self.frame_buf)
        self.frame_seq += 1
        self.frame_info = dict(crc32=crc_hex(data), seq=self.frame_seq, type=kind, data=data, rounds=rounds)
        self.cap.update(blocks=NBLOCKS, step="done")

    def wait_done(self, timeout, fetch_frame=True, on_poll=None):
        r = CaptureResult()
        t_end = time.time() + timeout
        last = None
        while time.time() < t_end:
            st = self.cap["state"]
            if st != last:
                r.states.append(st)
                last = st
            if on_poll:
                on_poll(self.status())
            if st in ("done", "error", "aborted"):
                break
            time.sleep(0.05)
        else:
            self.cap.update(state="error", error="timeout")
        r.state = self.cap["state"]
        r.error = self.cap.get("error", "")
        r.ok = r.state == "done"
        r.seconds = time.time() - self._t0
        r.fast_fallback = self.fast_fallback
        r.heap = {"free_internal": 200000, "free_psram": 7000000}
        r.status = self.status()
        if r.ok:
            r.crc32 = self.frame_info["crc32"]
            if fetch_frame:
                r.frame = self.frame_info["data"]
                r.frame_crc = r.crc32
        return r

    # ---- Driver API
    def status(self):
        det_state = "ready" if self.session_ok else "discovering"
        return {"detector": {"state": det_state, "setup_mode": self.setup_mode, "script7_done": self.script7_done},
                "capture": dict(self.cap, fast_fallback=self.fast_fallback, blocks_total=NBLOCKS, type="?"),
                "frame": {"valid": self.frame_seq > 0, "seq": self.frame_seq, "crc32": self.frame_info.get("crc32")},
                "xray": {"master_enabled": False, "relay_on": False, "fires": 0},
                "heap": {"free_internal": 200000, "free_psram": 7000000}}

    def abort(self):
        self._abort.set()
        return {"ok": True}

    def set_config(self, **kw):
        if "setup_mode" in kw:
            self.setup_mode = kw["setup_mode"]
            self.fast_fallback = False
        return {"ok": True}

    def get_config(self):
        return {"setup_mode": self.setup_mode}

    def reinit(self):
        self.session_ok = False
        return {"ok": True}

    def master(self, enabled):
        return {"ok": True, "master_enabled": False}

    def wait_ready(self, timeout):
        try:
            self._abort.clear()
            self._ensure_session()
            return True
        except Exception:
            return False


# =============================================================================== scenario framework
class Ctx:
    def __init__(self, drv, sim, args):
        self.drv, self.sim, self.args = drv, sim, args
        self.sim_only = args.sim_only


class Result:
    def __init__(self, sid, title):
        self.sid, self.title = sid, title
        self.passed = True
        self.skipped = False
        self.notes = []
        self.fails = []
        self.seconds = 0.0

    def check(self, cond, ok_msg, fail_msg):
        if cond:
            self.notes.append(ok_msg)
        else:
            self.passed = False
            self.fails.append(fail_msg)
        return cond


def verify_capture(ctx, res, r, kind, window_ms, scrubs, truth_before, expect_captures=1):
    """Common checks on one finished capture: state, size, CRC vs simulator, type/window/scrubs as the sim saw them."""
    ok = res.check(r.ok, "capture done in %.1f s" % r.seconds,
                   "capture did not complete: state=%s error=%r states=%s" % (r.state, r.error, r.states))
    if not ok:
        return False
    t = ctx.sim.truth()
    res.check(t["captures"] - truth_before["captures"] == expect_captures,
              "simulator ran %d acquisition(s)" % expect_captures,
              "simulator ran %d acquisition(s), expected %d" % (t["captures"] - truth_before["captures"], expect_captures))
    sim_crc = t["crc32"]
    if r.frame is not None:
        res.check(len(r.frame) == FRAME_BYTES, "frame is 8388608 B", "frame is %d B, expected %d" % (len(r.frame), FRAME_BYTES))
        res.check(r.frame_crc == sim_crc, "downloaded frame CRC %s == simulator CRC" % sim_crc,
                  "downloaded frame CRC %s != simulator CRC %s%s" % (r.frame_crc, sim_crc, diff_hint(ctx, r.frame)))
    if r.crc32:
        res.check(str(r.crc32).lower() == sim_crc, "ESP-reported CRC matches",
                  "ESP-reported CRC %s != simulator CRC %s" % (r.crc32, sim_crc))
    # PROTOCOL.md: the GUI's "dark" runs the same Script0 with the UI's typeMode (0) and just never fires the source,
    # so a dark capture with typeMode 0 is legitimate; only an X-ray request must never become a dark acquisition.
    res.check(t["frame_type"] == kind or (kind == "dark" and t["frame_type"] == "xray"),
              "simulator saw a %s acquisition (typeMode %s)" % (t["frame_type"], (t.get("last_acquisition") or {}).get("type_mode")),
              "simulator saw %s acquisition, expected %s" % (t["frame_type"], kind))
    acq = t.get("last_acquisition") or {}
    want = int(round(window_ms * TICKS_PER_MS))
    res.check(abs(acq.get("window_ticks", -99) - want) <= 2, "window ticks %d ~ %d ms" % (acq.get("window_ticks", 0), window_ms),
              "window ticks %s != expected %d (%d ms x 26.3e3)" % (acq.get("window_ticks"), want, window_ms))
    res.check(acq.get("scrubs") == scrubs, "scrubs %d" % scrubs, "scrubs %s != %d" % (acq.get("scrubs"), scrubs))
    return True


def diff_hint(ctx, frame):
    try:
        truth = ctx.sim.frame()
        if len(truth) != len(frame):
            return " (sizes differ)"
        bad = [b for b in range(NBLOCKS) if truth[b * 4096:(b + 1) * 4096] != frame[b * 4096:(b + 1) * 4096]]
        return " [%d differing rows(blocks), first %s]" % (len(bad), bad[:10])
    except Exception as e:
        return " (diff unavailable: %s)" % e


def sc_a(ctx):
    res = Result("a", "dark capture, ESP frame CRC == simulator CRC")
    tb = ctx.sim.truth()
    r = ctx.drv.capture("dark", ctx.args.window_ms, 0, timeout=ctx.args.timeout)
    verify_capture(ctx, res, r, "dark", ctx.args.window_ms, 0, tb)
    return res


def sc_b(ctx):
    res = Result("b", "X-ray capture, master switch OFF: no relay pulse, sequence completes")
    ctx.drv.master(False)
    st = ctx.drv.status()
    res.check(not st.get("xray", {}).get("master_enabled", False), "master switch is OFF", "master switch could not be turned off")
    tb = ctx.sim.truth()
    w = max(ctx.args.window_ms, 1000)
    relay = []
    ok, err = ctx.drv.start_capture("xray", w, 0, exposure_ms=250, fire_delay_ms=20)
    if not ok:
        res.passed = False
        res.fails.append("start failed: %s" % err)
        return res
    r = ctx.drv.wait_done(ctx.args.timeout, on_poll=lambda s: relay.append(bool(s.get("xray", {}).get("relay_on"))))
    verify_capture(ctx, res, r, "xray", w, 0, tb)
    res.check(not any(relay) and not r.relay_seen, "relay never energised (%d status polls)" % len(relay),
              "RELAY WAS ON while the master switch was off!")
    res.check(r.fires_delta == 0, "xray.fires unchanged", "xray.fires changed by %d" % r.fires_delta)
    return res


def sc_c(ctx):
    res = Result("c", "1/3/5 % dropped blocks -> selective cmd 9 repair (+ lossy repairs)")
    for rate in (0.01, 0.03, 0.05):
        ctx.sim.config(drop_rate=rate, drop_blocks=0, drop_retx_rate=0.0)
        tb = ctx.sim.truth()
        r = ctx.drv.capture("dark", ctx.args.window_ms, 0, timeout=ctx.args.timeout)
        if verify_capture(ctx, res, r, "dark", ctx.args.window_ms, 0, tb):
            t = ctx.sim.truth()
            dropped = t["drops"] - tb["drops"]
            res.check(dropped > 0 and t["cmd9_selective"] - tb["cmd9_selective"] >= 1,
                      "%.0f %%: %d blocks dropped, selective repair x%d (re-sent %d blocks)" %
                      (rate * 100, dropped, t["cmd9_selective"] - tb["cmd9_selective"], t["blocks_sent"] - tb["blocks_sent"] - (2048 - dropped)),
                      "%.0f %%: expected a selective repair (drops=%d, selective=%d)" %
                      (rate * 100, dropped, t["cmd9_selective"] - tb["cmd9_selective"]))
            res.check(t["cmd9_whole"] == tb["cmd9_whole"], "no whole-image resend needed",
                      "whole-image resend used for only %d dropped blocks" % dropped)
    # c2: loss in the repair rounds too -> several rounds
    ctx.sim.config(drop_rate=0.05, drop_retx_rate=0.30)
    tb = ctx.sim.truth()
    r = ctx.drv.capture("dark", ctx.args.window_ms, 0, timeout=ctx.args.timeout)
    if verify_capture(ctx, res, r, "dark", ctx.args.window_ms, 0, tb):
        t = ctx.sim.truth()
        rounds = t["resend_rounds"] - tb["resend_rounds"]
        res.check(rounds >= 2, "5 %% first-push loss + 30 %% repair loss: %d repair rounds, CRC ok" % rounds,
                  "expected >=2 repair rounds with lossy repairs, got %d" % rounds)
    ctx.sim.config(drop_rate=0.0, drop_blocks=0, drop_retx_rate=0.0)
    return res


def sc_d(ctx):
    res = Result("d", ">256 dropped blocks -> whole-image resend")
    ctx.sim.config(drop_blocks=400, drop_rate=0.0, drop_retx_rate=0.0)
    tb = ctx.sim.truth()
    r = ctx.drv.capture("dark", ctx.args.window_ms, 0, timeout=ctx.args.timeout)
    if verify_capture(ctx, res, r, "dark", ctx.args.window_ms, 0, tb):
        t = ctx.sim.truth()
        res.check(t["cmd9_whole"] - tb["cmd9_whole"] >= 1, "whole-image resend x%d" % (t["cmd9_whole"] - tb["cmd9_whole"]),
                  "expected a whole-image resend (cmd 9 with >256 entries), cmd9_whole did not change")
        res.check(t["drops"] - tb["drops"] == 400, "400 blocks dropped in the first push", "drops=%d" % (t["drops"] - tb["drops"]))
    ctx.sim.config(drop_blocks=0)
    return res


def sc_e(ctx):
    res = Result("e", "needs-ROE simulator + setup_mode=fast: fast->full fallback, capture succeeds")
    old = ctx.drv.get_config().get("setup_mode", "session")
    ctx.drv.set_config(setup_mode="fast")
    ctx.sim.config(needs_roe=True, needs_roe_mode="status2")
    try:
        ctx.drv.reinit()
        ctx.drv.wait_ready(60)
        tb = ctx.sim.truth()
        r = ctx.drv.capture("dark", ctx.args.window_ms, 0, timeout=ctx.args.timeout)      # first capture of the session: Script7 runs
        verify_capture(ctx, res, r, "dark", ctx.args.window_ms, 0, tb)
        ctx.sim.config(reset_roe=True)                                                   # detector "forgets" the ROE init
        tb = ctx.sim.truth()
        r = ctx.drv.capture("dark", ctx.args.window_ms, 0, timeout=ctx.args.timeout)
        verify_capture(ctx, res, r, "dark", ctx.args.window_ms, 0, tb)
        t = ctx.sim.truth()
        res.check(t["needs_roe_rejects"] - tb["needs_roe_rejects"] >= 1,
                  "simulator rejected the fast-mode EXECUTE (status 2) x%d" % (t["needs_roe_rejects"] - tb["needs_roe_rejects"]),
                  "simulator never rejected an EXECUTE - the ESP did not skip Script7 in fast mode (setup_mode not honoured?)")
        res.check(r.fast_fallback, "capture.fast_fallback = true", "capture.fast_fallback is not set after the fallback")
    finally:
        ctx.sim.config(needs_roe=False)
        ctx.drv.set_config(setup_mode=old)
    return res


def sc_f(ctx):
    n = ctx.args.soak_n
    res = Result("f", "soak: %d consecutive captures, heap/PSRAM drift" % n)
    w = ctx.args.soak_window_ms
    heaps, bad, t_start = [], 0, time.time()
    for i in range(n):
        tb = ctx.sim.truth()
        full = (i % ctx.args.soak_full_every == 0) or i == n - 1
        r = ctx.drv.capture("dark", w, 0, timeout=ctx.args.timeout, fetch_frame=full)
        t = ctx.sim.truth()
        good = r.ok and t["captures"] - tb["captures"] == 1
        crc = (r.frame_crc if (full and r.frame is not None) else r.crc32)
        good = good and crc is not None and str(crc).lower() == t["crc32"]
        if not good:
            bad += 1
            if bad <= 3:
                res.fails.append("capture %d/%d failed: state=%s error=%r crc=%s sim=%s" % (i + 1, n, r.state, r.error, crc, t["crc32"]))
            res.passed = False
            if not r.ok:
                ctx.drv.wait_ready(30)
        if r.heap:
            heaps.append((r.heap.get("free_internal"), r.heap.get("free_psram")))
        if (i + 1) % 10 == 0 or i == n - 1:
            print("      soak %d/%d  %.0f s  failures %d  heap %s" % (i + 1, n, time.time() - t_start, bad, heaps[-1] if heaps else "?"), flush=True)
    res.check(bad == 0, "%d/%d captures OK, CRC matched the simulator every time" % (n - bad, n), "%d/%d captures failed" % (bad, n))
    if ctx.drv.heap_meaningful and len(heaps) >= 3 and all(h[0] is not None and h[1] is not None for h in heaps):
        base = heaps[min(1, len(heaps) - 1)]                      # after the warm-up capture
        last = heaps[-1]
        d_int, d_ps = base[0] - last[0], base[1] - last[1]
        res.check(d_int <= ctx.args.heap_tol and d_ps <= ctx.args.psram_tol,
                  "heap drift internal %+d B, PSRAM %+d B (tolerance %d / %d); min internal %d, min PSRAM %d"
                  % (-d_int, -d_ps, ctx.args.heap_tol, ctx.args.psram_tol, min(h[0] for h in heaps), min(h[1] for h in heaps)),
                  "LEAK? free internal fell by %d B (tol %d), free PSRAM by %d B (tol %d) over the soak"
                  % (d_int, ctx.args.heap_tol, d_ps, ctx.args.psram_tol))
    elif not ctx.drv.heap_meaningful:
        res.notes.append("heap check n/a (software ESP)")
    else:
        res.passed = False
        res.fails.append("no heap data in /api/status")
    return res


def sc_g(ctx):
    res = Result("g", "abort mid-capture, then capture again")
    ctx.drv.master(False)
    w = 4000
    if ctx.sim_only:                      # the loopback simulator runs 100x faster; stretch the window back to ~4 s
        ctx.sim.config(window_scale=10.0)
    ok, err = ctx.drv.start_capture("xray", w, 0, exposure_ms=250, fire_delay_ms=20)
    if not ok:
        res.passed, res.fails = False, ["start failed: %s" % err]
        return res
    t_end = time.time() + 30
    st = {}
    while time.time() < t_end:
        st = ctx.drv.status()
        if st.get("capture", {}).get("state") in ("exposing", "readout", "receiving"):
            break
        time.sleep(0.05)
    state_at_abort = st.get("capture", {}).get("state")
    time.sleep(0.3)
    ctx.drv.abort()
    t_end = time.time() + 15
    final = None
    while time.time() < t_end:
        st = ctx.drv.status()
        final = st.get("capture", {}).get("state")
        if final in ("aborted", "idle", "error", "done"):
            break
        time.sleep(0.1)
    res.check(final == "aborted", "capture aborted (was in '%s')" % state_at_abort,
              "after abort the capture state is %r (expected 'aborted'; it was '%s' when aborting)" % (final, state_at_abort))
    res.check(not st.get("xray", {}).get("relay_on", False), "relay is off after abort", "RELAY STILL ON after abort")
    if ctx.sim_only:
        ctx.sim.config(window_scale=1.0)
    ready = ctx.drv.wait_ready(90)
    res.check(ready, "detector session back to 'ready' after abort", "session did not return to 'ready' within 90 s after abort")
    tb = ctx.sim.truth()
    r = ctx.drv.capture("dark", ctx.args.window_ms, 0, timeout=ctx.args.timeout)
    verify_capture(ctx, res, r, "dark", ctx.args.window_ms, 0, tb)
    t = ctx.sim.truth()
    res.notes.append("simulator saw %d abort opcode(s) (0x0A/0x0B)" % t["aborts"])
    return res


def sc_h(ctx):
    res = Result("h", "protocol health (byte-identical packets, SeqId rule, ACKs, flow)")
    t = ctx.sim.truth()
    res.check(t["protocol_mismatches"] == 0, "protocol_mismatches = 0 (every ESP packet byte-identical to flashpad_acquire.py)",
              "protocol_mismatches = %d: %s" % (t["protocol_mismatches"], t["mismatch_log"][-5:]))
    res.check(t["seq_drops"] == 0, "no SeqId-rule drops", "%d host packets dropped by the SeqId rule" % t["seq_drops"])
    res.check(t["unacked"] == 0, "every detector data packet was ACKed (retx=%d)" % t["retx_pkts"],
              "%d detector data packets were never ACKed" % t["unacked"])
    res.check(t["flow_violations"] == 0, "no flow violations", "flow violations: %s" % t["flow_log"][-5:])
    res.check(t["stray_acks"] == 0, "no ACKs for unknown sequence numbers", "%d bare ACKs for unknown detector SeqIds" % t["stray_acks"])
    res.check(t["unknown_cmds"] == 0, "no unsupported commands", "%d unsupported commands" % t["unknown_cmds"])
    res.notes.append("totals: executes=%d scripts=%d captures=%d blocks_sent=%d drops=%d repair rounds=%d retx=%d acks_dup=%d"
                     % (t["executes"], t["scripts"], t["captures"], t["blocks_sent"], t["drops"], t["resend_rounds"],
                        t["retx_pkts"], t["acks_dup"]))
    return res


def sc_m(ctx):
    res = Result("m", "setup modes full / session / fast / combined")
    old = ctx.drv.get_config().get("setup_mode", "session")
    try:
        for mode in ("full", "session", "fast", "combined"):
            ctx.drv.set_config(setup_mode=mode)
            for k in range(2):
                tb = ctx.sim.truth()
                r = ctx.drv.capture("dark", ctx.args.window_ms, 0, timeout=ctx.args.timeout)
                if verify_capture(ctx, res, r, "dark", ctx.args.window_ms, 0, tb):
                    t = ctx.sim.truth()
                    res.notes.append("%s #%d: %.1f s, scripts downloaded %d" % (mode, k + 1, r.seconds, t["scripts"] - tb["scripts"]))
    finally:
        ctx.drv.set_config(setup_mode=old)
    return res


SCENARIOS = [("a", sc_a), ("b", sc_b), ("c", sc_c), ("d", sc_d), ("e", sc_e), ("f", sc_f), ("g", sc_g), ("h", sc_h)]
OPTIONAL = {"m": sc_m}


# =============================================================================== main
def spawn_sim(args_list, http_port, log_path):
    cmd = [sys.executable, os.path.join(HERE, "flashpad_sim.py"), "--http-port", str(http_port), "--quiet"] + args_list
    logf = open(log_path, "w")
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % http_port
    t_end = time.time() + 20
    while time.time() < t_end:
        if p.poll() is not None:
            raise SystemExit("simulator exited immediately (see %s)" % log_path)
        code, _, _ = http("GET", base + "/truth", timeout=1)
        if code == 200:
            return p, base
        time.sleep(0.2)
    p.kill()
    raise SystemExit("simulator did not come up (see %s)" % log_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--esp", default="http://192.168.1.1", help="ESP base URL")
    ap.add_argument("--sim", default="http://127.0.0.1:8199", help="simulator HTTP side channel")
    ap.add_argument("--sim-only", action="store_true", help="no ESP: spawn a loopback simulator and drive it with the software ESP")
    ap.add_argument("--spawn-sim", metavar="ARGS", default=None, help="start flashpad_sim.py with these arguments first "
                    "(e.g. \"--bind-ip 192.168.1.30\")")
    ap.add_argument("--sim-args", default="", help="--sim-only: extra flashpad_sim.py arguments, e.g. "
                    "\"--second-0x30000 after --latency-ms 20 --drop-ctrl-rate 0.05\"")
    ap.add_argument("--only", default="", help="comma list of scenario ids (default a..h)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--window-ms", type=int, default=500, help="integration window for the normal captures")
    ap.add_argument("--soak-n", type=int, default=100)
    ap.add_argument("--soak-window-ms", type=int, default=200)
    ap.add_argument("--soak-full-every", type=int, default=10, help="download + CRC the full frame every Nth soak capture")
    ap.add_argument("--heap-tol", type=int, default=8192, help="allowed drop of free internal heap over the soak (bytes)")
    ap.add_argument("--psram-tol", type=int, default=65536, help="allowed drop of free PSRAM over the soak (bytes)")
    ap.add_argument("--timeout", type=float, default=120, help="per-capture timeout (s)")
    ap.add_argument("--verbose", action="store_true", help="software ESP: print its steps")
    args = ap.parse_args()

    if args.list:
        for sid, fn in SCENARIOS:
            print(sid, "-", (fn.__doc__ or "").strip() or fn.__name__)
        print("m - optional: setup modes")
        return 0
    chosen = [s.strip() for s in args.only.split(",") if s.strip()] or [s for s, _ in SCENARIOS]
    table = dict(SCENARIOS)
    table.update(OPTIONAL)
    for s in chosen:
        if s not in table:
            raise SystemExit("unknown scenario %r" % s)

    procs = []
    tmpdir = os.environ.get("TEMP") or os.environ.get("TMPDIR") or "."
    try:
        if args.sim_only:
            sim_args = ["--bind-ip", "127.0.0.1", "--host-ip", "127.0.0.1", "--time-scale", "0.1", "--window-scale", "0.1",
                        "--rate-mbps", "400"]
            import shlex
            sim_args += shlex.split(args.sim_args)
            p, args.sim = spawn_sim(sim_args, 18199, os.path.join(tmpdir, "flashpad_sim_e2e.log"))
            procs.append(p)
            drv = RefHost(verbose=args.verbose)
            drv.start()
            args.window_ms = min(args.window_ms, 500)
            print("sim-only: simulator on %s (loopback, time-scale 0.1), driver = software ESP (RefHost)" % args.sim)
        else:
            if args.spawn_sim is not None:
                import shlex
                p, args.sim = spawn_sim(shlex.split(args.spawn_sim), int(args.sim.rsplit(":", 1)[-1]),
                                        os.path.join(tmpdir, "flashpad_sim_e2e.log"))
                procs.append(p)
            drv = EspDriver(args.esp)
            st = drv.status()
            if not st:
                print("ESP not reachable at %s (use --sim-only for a hardware-free self test)" % args.esp)
                return 2
            print("ESP %s: fw %s, detector %s, link %s Mbit" % (args.esp, st.get("fw"), st.get("detector", {}).get("state"),
                                                              st.get("link", {}).get("speed")))
        sim = SimClient(args.sim)
        try:
            t = sim.truth()
        except RuntimeError as e:
            print(e)
            return 2
        sim.reset(counters=True, session=False)
        sim.config(drop_rate=0.0, drop_blocks=0, drop_retx_rate=0.0, needs_roe=False)
        print("simulator %s: uptime %.0f s, session %s" % (args.sim, t["uptime_s"], t["session"]["state"]))
        if not args.sim_only and not drv.wait_ready(60):
            print("ESP detector session is not 'ready' within 60 s - is the simulator running on 192.168.1.30:8100?")
            return 2
        ctx = Ctx(drv, sim, args)
        results = []
        for sid in chosen:
            fn = table[sid]
            print("--- scenario %s ..." % sid, flush=True)
            t0 = time.time()
            try:
                r = fn(ctx)
            except Exception as e:
                import traceback
                r = Result(sid, fn.__name__)
                r.passed = False
                r.fails.append("exception: %s\n%s" % (e, traceback.format_exc()))
            r.seconds = time.time() - t0
            results.append(r)
            print("    %s  %s (%.1f s)" % ("PASS" if r.passed else "FAIL", r.title, r.seconds), flush=True)
            for n in r.notes:
                print("      + " + n)
            for f in r.fails:
                print("      ! " + f)
        print()
        print(" id  result  time    scenario")
        print("-" * 78)
        for r in results:
            print(" %-3s %-6s %6.1fs  %s" % (r.sid, "PASS" if r.passed else "FAIL", r.seconds, r.title))
        print("-" * 78)
        nfail = sum(1 for r in results if not r.passed)
        print("%d scenario(s), %d failed%s" % (len(results), nfail, "  [sim-only: validates the simulator, not the firmware]" if args.sim_only else ""))
        return 1 if nfail else 0
    finally:
        try:
            drv.stop()
        except Exception:
            pass
        for p in procs:
            p.terminate()
            try:
                p.wait(5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
