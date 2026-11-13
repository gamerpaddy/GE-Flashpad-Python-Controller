#!/usr/bin/env python3
"""
flashpad_sim.py - PC-side GE FlashPad detector simulator for the ESP32-S31 controller.

It binds ONE UDP socket (default 192.168.1.30:8100). That socket receives every host command AND is the source of
every datagram the "detector" sends (replies, events, image blocks), exactly like the real detector.

It models the *documented* behaviour in esp32s31/docs/PROTOCOL.md (tags [CODE]/[LIVE]/[FW]/[INF]).  It therefore
proves a host implementation against the documented model, NOT against real-detector quirks.  Every place where the
simulator had to invent a behaviour is listed in tools/README-sim.md ("Where the simulator guesses") and is switchable
with an option where that is cheap.

Self-checking: every datagram the host sends is parsed and then rebuilt with the REAL reference builders from
flashpad_acquire.py (make_urp_packet, build_system_startup, build_port_setup, build_generic_script + pack_*,
build_execute_script, build_image_xfer_status_reply, build_image_retrival_request, build_image_retrival, ...).
Any packet that is not byte-identical is logged and counted in `protocol_mismatches`.

HTTP side channel (default 127.0.0.1:8199):
    GET  /truth    JSON: crc32 of the last registered image, counters, session state, mismatch log
    GET  /frame    the last registered image, 8 MiB u16 LE (ground truth for pixel level diffs)
    GET  /log      last ~2000 log lines (text/plain)
    POST /config   JSON, change runtime options (drop_rate, drop_blocks, needs_roe, stuck_busy, reset_roe ...)
    POST /reset    JSON {"counters":true,"session":true}

Self test (starts a sim subprocess on loopback and drives the EXISTING Python host against it):
    python flashpad_sim.py --selftest

Run for the bench (PC NIC = 192.168.1.30/24, ESP = 192.168.1.1):
    python flashpad_sim.py
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import heapq
import itertools
import json
import os
import queue
import random
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("FLASHPAD_REPO") or os.path.abspath(os.path.join(HERE, "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import numpy as np                      # noqa: E402
import flashpad_acquire as fp           # noqa: E402  (the REAL reference host / builders)

# --------------------------------------------------------------------------- constants
TICKS_PER_S = 26.3e6            # acquisition time fields are in ticks of ~26.3 MHz [LIVE-measured]
DIM = 2048
NBLOCKS = 2048
PAYLOAD = 4096
DGRAM = 8 + PAYLOAD             # 4104 B UDP payload: [imageId:u32][blockIndex:u32][4096 B]
# bytes on the wire per image datagram: UDP 8 + IP 20 + Eth 14 + FCS 4 + preamble 8 + IFG 12
WIRE_BYTES = DGRAM + 8 + 20 + 14 + 4 + 8 + 12

CMD_STARTUP = 0x01
CMD_PORT_SETUP = 0x02
CMD_SIGNATURE = 0x03
CMD_HOST_SCRIPT_COMMAND = 0x04
CMD_GENERIC_SCRIPT = 0x05
CMD_EXECUTE = 0x06
CMD_EXEC_STATUS_POLL = 0x07
CMD_XFER_REPLY = 0x09
CMD_ABORT_A = 0x0A
CMD_ABORT_B = 0x0B
CMD_ENUM_IMAGES = 0x41
CMD_PULL_IMAGE = 0x98
EV_EXEC_COMPLETE = 0x00010000
EV_XFER_QUERY = 0x00030000
EV_HOST_EVENT = 0x00040000
EV_WINDOW_END = 0x00050000

SIG_MAC = bytes.fromhex("40F4A000784D")
SIG_SERIAL = b"UA45829-7"
SIG_MODEL = b"5340000-7"
SIG_FW = bytes([1, 6, 0, 4, 2, 0, 1, 3])

# model timing (seconds at --time-scale 1).  Overridable with --timing key=value
TIMING = dict(
    startup_beacon=0.05,      # STARTUP ack -> first beacon
    exec_reply=0.20,          # EXECUTE -> cmd 6 reply                         [LIVE] "+0.2 s"
    acq_window_open=0.21,     # acquisition start -> 0x40000[1] (window open)   [LIVE] "+0.21 s"
    roe_init=0.33,            # a real ROE command (cmd != 0)                   [INF] chosen so Script7 ~1.1 s
    roe_pulse=0.005,          # ROE(0,0) zero pulse
    list_tail=0.02,           # last command -> EXECUTION_COMPLETE
    readout=0.43,             # window end (0x50000) -> image ready; +10 ms delay +list_tail = 0.46 s [LIVE]
    exec_complete_to_30000=0.70,   # EXECUTION_COMPLETE -> first 0x30000       [LIVE] "+0.7 s"
    post_push_30000=0.05,     # end of image push -> next 0x30000               [INF]
    deadbeef=0.20,            # slot freed -> DEADBEEF 0x30000                  [INF]
    second_30000_gap=0.002,   # 2nd 0x30000 after 0x41                          [LIVE] "within ms"
    push_start=0.005,         # 0x98 reply -> first datagram                    [INF]
)

SIO_UDP_CONNRESET = 0x9800000C


# --------------------------------------------------------------------------- helpers
class ScriptError(Exception):
    pass


Acq = collections.namedtuple("Acq", "type_mode image_id no_scrubs scrub_dur max_expose tail transfer")
Roe = collections.namedtuple("Roe", "resp timer cmd data")
Evt = collections.namedtuple("Evt", "id")
Wait = collections.namedtuple("Wait", "id timeout")
Dly = collections.namedtuple("Dly", "us")


class Script:
    def __init__(self, sid, repeat, repeat_event, cmds, payload):
        self.sid, self.repeat, self.repeat_event, self.cmds, self.payload = sid, repeat, repeat_event, cmds, payload

    def has_acq(self):
        return any(isinstance(c, Acq) for c in self.cmds)

    def is_roe_init(self):
        return any(isinstance(c, Roe) and c.cmd == fp.ROE_SCAN_SETUP_CMD for c in self.cmds)

    def describe(self):
        parts = []
        for c in self.cmds:
            if isinstance(c, Acq):
                parts.append("ACQ(type=%d win=%d scrubs=%d)" % (c.type_mode, c.max_expose, c.no_scrubs))
            elif isinstance(c, Roe):
                parts.append("ROE(%#x,%#x)" % (c.cmd, c.data))
            elif isinstance(c, Evt):
                parts.append("EVT(%d)" % c.id)
            elif isinstance(c, Wait):
                parts.append("WAIT(%d)" % c.id)
            else:
                parts.append("DLY(%d)" % c.us)
        return "Script%d rep=%d [%s]" % (self.sid, self.repeat, " ".join(parts))


_CMD_SIZES = {1: 17, 2: 14, 3: 5, 4: 9, 5: 5}


def parse_script_payload(p: bytes) -> Script:
    """GENERIC_SCRIPT payload -> Script.  Raises ScriptError on anything the firmware validator would reject."""
    if len(p) < 10:
        raise ScriptError("payload too short (%d)" % len(p))
    sid, rep, rev = struct.unpack_from("<HHI", p, 0)
    off, cmds = 8, []
    while True:
        if off >= len(p):
            raise ScriptError("missing 00 00 terminator")
        code = p[off]
        if code == 0:
            if p[off:off + 2] != b"\x00\x00" or off + 2 != len(p):
                raise ScriptError("bad terminator / trailing bytes")
            break
        size = _CMD_SIZES.get(code)
        if size is None:
            raise ScriptError("bad command code %d at offset %d" % (code, off))
        if off + size > len(p):
            raise ScriptError("truncated command code %d" % code)
        if code == 1:
            _, t, i, ns, sd, mx, tl, tm = struct.unpack_from("<BBBBIIIB", p, off)
            cmds.append(Acq(t, i, ns, sd, mx, tl, tm))
        elif code == 2:
            _, resp, timer, rc, rd = struct.unpack_from("<BBIII", p, off)
            cmds.append(Roe(resp, timer, rc, rd))
        elif code == 3:
            cmds.append(Evt(struct.unpack_from("<I", p, off + 1)[0]))
        elif code == 4:
            cmds.append(Wait(*struct.unpack_from("<II", p, off + 1)))
        else:
            cmds.append(Dly(struct.unpack_from("<I", p, off + 1)[0]))
        off += size
    return Script(sid, rep, rev, cmds, p)


def rebuild_script_pdap(s: Script) -> bytes:
    """Rebuild a GENERIC_SCRIPT PDAP with the REAL builders from the parsed fields."""
    packed = []
    for c in s.cmds:
        if isinstance(c, Acq):
            packed.append(fp.pack_acquisition(type_mode=c.type_mode, image_id=c.image_id, no_scrubs=c.no_scrubs,
                                              scrub_duration=c.scrub_dur, max_expose_time=c.max_expose,
                                              tail_time=c.tail, transfer_mode=c.transfer))
        elif isinstance(c, Roe):
            packed.append(fp.pack_roe_command(c.cmd, c.data, response_flag=c.resp, timer_value=c.timer))
        elif isinstance(c, Evt):
            packed.append(fp.pack_send_host_event(c.id))
        elif isinstance(c, Wait):
            packed.append(fp.pack_wait_for_host_event(c.id, c.timeout))
        else:
            packed.append(fp.pack_delay(c.us))
    return fp.build_generic_script(s.sid, s.repeat, s.repeat_event, packed)


def _enable_hires_timer():
    if os.name == "nt":
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


def _no_udp_connreset(sock: socket.socket):
    """Windows: an ICMP port-unreachable must not turn the next recvfrom() into WSAECONNRESET."""
    if os.name != "nt":
        return
    try:
        val, ret = ctypes.c_ulong(0), ctypes.c_ulong(0)
        ctypes.windll.ws2_32.WSAIoctl(ctypes.c_size_t(sock.fileno()), SIO_UDP_CONNRESET,
                                      ctypes.byref(val), 4, None, 0, ctypes.byref(ret), None, None)
    except Exception:
        pass


# --------------------------------------------------------------------------- configuration
class Cfg:
    FIELDS = dict(
        time_scale=(float, 1.0), window_scale=(float, 1.0), rate_mbps=(float, 100.0),
        drop_blocks=(int, 0), drop_rate=(float, 0.0), drop_retx_rate=(float, 0.0), drop_seed=(int, 1),
        drop_ctrl_rate=(float, 0.0), latency_ms=(float, 0.0), jitter_ms=(float, 0.0),
        needs_roe=(bool, False), needs_roe_mode=(str, "status2"), keep_list_on_reject=(bool, False),
        stuck_busy_after=(int, 0), max_retries=(int, 10), retx_interval=(float, 0.5), max_retx=(int, 3),
        beacon_interval=(float, 2.0), beacon_giveup=(int, 0),
        second_30000=(str, "before"), prepull_resend=(bool, False), deadbeef=(bool, True),
        suppress_window_event=(bool, False), early_close=(bool, True),
        seed=(int, 1), fixed_seed=(bool, False),
    )

    def __init__(self, **kw):
        for k, (_t, d) in self.FIELDS.items():
            setattr(self, k, d)
        self.update(kw)

    def update(self, d: dict):
        changed = {}
        for k, v in d.items():
            if k not in self.FIELDS:
                continue
            t = self.FIELDS[k][0]
            if t is bool:
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            else:
                v = t(v)
            setattr(self, k, v)
            changed[k] = v
        return changed

    def as_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}


# --------------------------------------------------------------------------- synthetic frames
class FrameFactory:
    """Deterministic 2048x2048 u16 frames per (kind 'dark'|'xray', seed)."""

    DARK_LEVEL = 1789.0
    XRAY_GAIN = 1400.0
    DEAD_ROWS = (48, 1093)
    DEAD_COLS = (783,)

    def __init__(self):
        self._fpn = None
        self._phantom = None
        self._cache = collections.OrderedDict()
        self._lock = threading.Lock()

    def _fixed_pattern(self):
        if self._fpn is None:
            rng = np.random.default_rng(0xF1A5)
            row = rng.normal(0, 2.0, DIM).astype(np.float32)
            col = rng.normal(0, 3.0, DIM).astype(np.float32)
            col += ((np.arange(DIM) % 128) - 64).astype(np.float32) * (5.0 / 64.0)      # amplifier banding
            yy = np.linspace(-1, 1, DIM, dtype=np.float32)[:, None]
            xx = np.linspace(-1, 1, DIM, dtype=np.float32)[None, :]
            fpn = rng.standard_normal((DIM, DIM), dtype=np.float32) * np.float32(4.0)
            fpn += row[:, None]
            fpn += col[None, :]
            fpn += np.float32(12.0) * (xx * yy)
            self._fpn = fpn
        return self._fpn

    def _phantom_signal(self):
        """Smooth chest-like phantom, ADU above dark with no noise (cached)."""
        if self._phantom is None:
            y = np.linspace(-1, 1, DIM, dtype=np.float32)[:, None]
            x = np.linspace(-1, 1, DIM, dtype=np.float32)[None, :]
            t = np.zeros((DIM, DIM), np.float32)
            body = np.clip(1 - ((x / 0.62) ** 2) - (((y - 0.05) / 0.85) ** 2), 0, None)
            t += 2.2 * np.sqrt(body)
            for cx in (-0.28, 0.28):                                  # lungs: less attenuation
                lung = np.clip(1 - (((x - cx) / 0.2) ** 2) - (((y + 0.22) / 0.38) ** 2), 0, None)
                t -= 1.3 * np.sqrt(lung)
            t += 0.9 * np.exp(-((x / 0.045) ** 2)) * (np.abs(y) < 0.8)        # spine
            for k in range(8):                                                # ribs
                yc = -0.45 + 0.11 * k + 0.10 * (x ** 2)
                t += 0.45 * np.exp(-(((y - yc) / 0.012) ** 2)) * (np.abs(x) > 0.07) * (np.abs(x) < 0.55)
            for i in range(6):                                                # step wedge, bottom left
                m = (x >= -0.95 + i * 0.07) & (x < -0.95 + (i + 1) * 0.07) & (y >= 0.72) & (y < 0.9)
                t += (0.5 * i) * m
            sig = self.XRAY_GAIN * np.exp(-0.55 * t) * (1 - 0.12 * (x * x + y * y))
            sig[40:240, 40:60] *= 0.05                                       # lead "L" marker (orientation check)
            sig[220:240, 40:160] *= 0.05
            self._phantom = sig.astype(np.float32)
        return self._phantom

    def get(self, kind: str, seed: int):
        key = (kind, int(seed))
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        rng = np.random.default_rng([int(seed) & 0xFFFFFFFF, 1 if kind == "dark" else 2, 0x51A])
        img = self._fixed_pattern() + np.float32(self.DARK_LEVEL)
        noise = rng.standard_normal((DIM, DIM), dtype=np.float32)
        if kind == "dark":
            img += noise * np.float32(9.0)
        else:
            sig = self._phantom_signal()
            img += sig
            img += noise * (np.float32(9.0) + np.float32(0.6) * np.sqrt(sig))
        np.clip(img, 0, 65535, out=img)
        out = img.astype("<u2")
        for r in self.DEAD_ROWS:                               # dead lines (stuck near zero)
            out[r, :] = rng.integers(20, 40, DIM, dtype=np.uint16)
        for c in self.DEAD_COLS:
            out[:, c] = rng.integers(20, 40, DIM, dtype=np.uint16)
        out = np.ascontiguousarray(out)
        res = self._wrap(out)
        with self._lock:
            self._cache[key] = res
            while len(self._cache) > 3:
                self._cache.popitem(last=False)
        return res

    @staticmethod
    def _wrap(out):
        data = out.tobytes()
        return dict(arr=out, data=data, crc=zlib.crc32(data) & 0xFFFFFFFF,
                    mean=float(out.mean()), std=float(out.std()))

    def bad(self):
        return self._wrap(np.full((DIM, DIM), 0xFFFF, dtype="<u2"))


# --------------------------------------------------------------------------- scheduler
class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sim-sched")
        self._heap, self._cv, self._n = [], threading.Condition(), itertools.count()
        self._halt = False

    def call_later(self, delay, fn, *args):
        with self._cv:
            heapq.heappush(self._heap, (time.perf_counter() + max(0.0, delay), next(self._n), fn, args))
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._halt = True
            self._cv.notify()

    def run(self):
        while True:
            with self._cv:
                while not self._halt and (not self._heap or self._heap[0][0] > time.perf_counter()):
                    timeout = None if not self._heap else max(0.0, self._heap[0][0] - time.perf_counter())
                    self._cv.wait(timeout)
                if self._halt:
                    return
                _, _, fn, args = heapq.heappop(self._heap)
            try:
                fn(*args)
            except Exception:
                traceback.print_exc()


class _Aborted(Exception):
    pass


class Pending:
    __slots__ = ("seq", "pkt", "dest", "tries", "label", "t0")

    def __init__(self, seq, pkt, dest, label):
        self.seq, self.pkt, self.dest, self.tries, self.label, self.t0 = seq, pkt, dest, 1, label, time.perf_counter()


class Image:
    def __init__(self, iid, sid, counter, kind, seed, frame, bad=False):
        self.id, self.sid, self.counter, self.kind, self.seed = iid, sid, counter, kind, seed
        self.frame, self.bad = frame, bad
        self.state = "held"             # held | freed
        self.enumerated = False         # 0x41 answered
        self.pulled = False             # 0x98 accepted
        self.retries = 0
        self.delivered = set()          # blocks that were actually put on the wire at least once
        self.second_sent = False


class Run:
    def __init__(self, scripts):
        self.scripts = scripts
        self.abort = threading.Event()
        self.hang = False
        self.bad_frame = False
        self.counters = collections.defaultdict(int)
        self.images = []


class PushJob:
    __slots__ = ("image", "blocks", "first", "gen", "label")

    def __init__(self, image, blocks, first, gen, label):
        self.image, self.blocks, self.first, self.gen, self.label = image, blocks, first, gen, label


# --------------------------------------------------------------------------- the simulator
class Sim:
    def __init__(self, args):
        self.args = args
        self.cfg = Cfg(**{k: getattr(args, k) for k in Cfg.FIELDS if hasattr(args, k)})
        self.timing = dict(TIMING)
        for kv in (args.timing or []):
            k, v = kv.split("=", 1)
            if k not in self.timing:
                raise SystemExit("unknown --timing key %r (known: %s)" % (k, ", ".join(sorted(self.timing))))
            self.timing[k] = float(v)
        self.lock = threading.RLock()
        self.t0 = time.perf_counter()
        self.rng = random.Random(args.drop_seed)
        self.frames = FrameFactory()
        self.sched = Scheduler()
        self.stop_evt = threading.Event()
        self.logbuf = collections.deque(maxlen=2000)
        self.mismatch_log = collections.deque(maxlen=40)
        self.flow_log = collections.deque(maxlen=40)
        self.cnt = collections.defaultdict(int)
        self.push_q: "queue.Queue[PushJob]" = queue.Queue()
        self.push_gen = 0
        self.beacon_gen = 0
        self.http = None
        self._reset_session(first=True)
        self.last = dict(crc=None, id=None, kind=None, seed=None, mean=None, std=None, bad=False, arr=None, data=None,
                         acq=None)
        det_ip = args.detector_ip or (args.bind_ip if args.bind_ip not in ("0.0.0.0", "") else "192.168.1.30")
        self.detector_ip = det_ip
        self.mac = bytes(int(x, 16) for x in args.mac.split(":"))
        self.sock = None

    # ----- logging / counters
    def log(self, msg, pkt=False):
        t = time.time()
        line = "%s.%03d %s" % (time.strftime("%H:%M:%S", time.localtime(t)), int(t * 1000) % 1000, msg)
        self.logbuf.append(line)
        if self.args.log_file:
            try:
                with open(self.args.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        if pkt and self.args.quiet:
            return
        try:
            print(line, flush=True)
        except Exception:
            pass

    def bump(self, name, n=1):
        with self.lock:
            self.cnt[name] += n

    def mismatch(self, reason, data=b"", expected=None):
        with self.lock:
            self.cnt["protocol_mismatches"] += 1
            self.mismatch_log.append(reason)
        msg = "!! PROTOCOL MISMATCH: %s\n      got      %s" % (reason, data[:96].hex())
        if expected is not None:
            msg += "\n      expected %s" % expected[:96].hex()
        self.log(msg)

    def flow(self, reason):
        with self.lock:
            self.cnt["flow_violations"] += 1
            self.flow_log.append(reason)
        self.log("!! FLOW VIOLATION: " + reason)

    @property
    def ts(self):
        return self.cfg.time_scale

    # ----- session state
    def _reset_session(self, first=False):
        with self.lock:
            if not first:
                self._abort_all("reset")
            self.host_ip = self.args.host_ip
            self.sys_port = None
            self.cmd_port = 48879
            self.img_port = 6660
            self.last_host_seq = -1
            self.det_seq = self.args.seq_start
            self.pending = {}
            self.acked_recent = collections.deque(maxlen=256)
            self.scripts = []
            self.cur_run = None
            self.registry = []
            self.roe_done = False
            self.stuck = False
            self.window_open = False
            self.host_events = {}
            self.beacon_gen += 1
            self.beacon_unacked = 0
            self.beacon_lost = False
            self.last_startup_t = -10.0
            self.last_exec_complete = b"\x00" * 16
            self.announced_ports = set()

    def _abort_all(self, why):
        """0x0A semantics: abort script list + reset image state."""
        with self.lock:
            if self.cur_run is not None:
                self.cur_run.abort.set()
                self.cur_run = None
            self.scripts = []
            self.registry = []
            self.window_open = False
            self.abort_push()
        self.log("abort: %s" % why)

    def abort_push(self):
        with self.lock:
            self.push_gen += 1
            try:
                while True:
                    self.push_q.get_nowait()
            except queue.Empty:
                pass

    def _ev(self, ev_id):
        with self.lock:
            e = self.host_events.get(ev_id)
            if e is None:
                e = self.host_events[ev_id] = threading.Event()
            return e

    # ----- start / stop
    def start(self):
        _enable_hires_timer()
        a = self.args
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        s.bind((a.bind_ip, a.cmd_port))
        s.settimeout(0.2)
        _no_udp_connreset(s)
        self.sock = s
        self.sched.start()
        threading.Thread(target=self._rx_loop, daemon=True, name="sim-rx").start()
        threading.Thread(target=self._push_worker, daemon=True, name="sim-push").start()
        self.log("detector simulator up: UDP %s:%d (detector ip in beacons %s), host %s, time-scale %.3g, window-scale %.3g, "
                 "image rate %s" % (a.bind_ip, a.cmd_port, self.detector_ip, a.host_ip, self.cfg.time_scale,
                                    self.cfg.window_scale,
                                    "burst" if self.cfg.rate_mbps <= 0 else "%.0f Mbit/s" % self.cfg.rate_mbps))
        if a.punch:
            self._punch()
        if a.http_port:
            self._start_http()

    def stop(self):
        self.stop_evt.set()
        self.sched.stop()
        with self.lock:
            if self.cur_run:
                self.cur_run.abort.set()
        self.abort_push()
        if self.http:
            self.http.shutdown()
        try:
            self.sock.close()
        except Exception:
            pass

    def _punch(self):
        """Send a harmless bare ACK out of :8100 so a stateful (Windows) firewall lets the ESP's replies in."""
        for port in (5550, 6660):
            try:
                self.sock.sendto(struct.pack("<II", 0, 1), (self.args.host_ip, port))
            except OSError:
                pass
        self.log("firewall punch sent from :%d to %s:5550/6660" % (self.args.cmd_port, self.args.host_ip))

    # ----- transmit side
    def _raw_send(self, pkt, dest):
        try:
            self.sock.sendto(pkt, dest)
        except OSError as e:
            self.bump("send_errors")
            self.log("send error to %s:%d: %s" % (dest[0], dest[1], e))

    def _xmit(self, pkt, dest, desc):
        c = self.cfg
        if c.drop_ctrl_rate and self.rng.random() < c.drop_ctrl_rate:
            self.bump("ctrl_dropped")
            self.log("TX %s -> %s:%d [DROPPED by --drop-ctrl-rate]" % (desc, dest[0], dest[1]), pkt=True)
            return
        delay = (c.latency_ms + (self.rng.random() * c.jitter_ms if c.jitter_ms else 0.0)) / 1000.0
        self.log("TX %s -> %s:%d" % (desc, dest[0], dest[1]), pkt=True)
        if delay > 0:
            self.sched.call_later(delay, self._raw_send, pkt, dest)
        else:
            self._raw_send(pkt, dest)

    def send_ack(self, seq, dest):
        self.bump("acks_tx")
        self._xmit(struct.pack("<II", seq, 1), dest, "bare-ACK seq=%d" % seq)

    def send_data(self, cmd, payload=b"", dest=None, label=None):
        """Detector data packet (CmdFlag 0): registered for retransmission until the host ACKs it."""
        with self.lock:
            seq = self.det_seq
            self.det_seq = (self.det_seq + 1) & 0xFFFFFFFF
            pkt = struct.pack("<IIII", seq, 0, cmd, len(payload)) + payload
            dest = dest or (self.host_ip, self.cmd_port)
            label = label or ("cmd 0x%X" % cmd)
            self.pending[seq] = Pending(seq, pkt, dest, label)
            self.cnt["data_tx"] += 1
        self._xmit(pkt, dest, "data seq=%d %s plen=%d %s" % (seq, label, len(payload), payload[:24].hex()))
        self.sched.call_later(self.cfg.retx_interval, self._retx, seq)
        return seq

    def _retx(self, seq):
        with self.lock:
            p = self.pending.get(seq)
            if p is None:
                return
            if p.tries > self.cfg.max_retx:
                del self.pending[seq]
                self.cnt["unacked"] += 1
                if p.label == "beacon":
                    self.beacon_unacked += 1
                self.log("!! no ACK for %s seq=%d after %d transmissions - giving up" % (p.label, seq, p.tries))
                return
            p.tries += 1
            self.cnt["retx_pkts"] += 1
            pkt, dest, tries = p.pkt, p.dest, p.tries
        self._xmit(pkt, dest, "RETX#%d data seq=%d %s" % (tries - 1, seq, p.label))
        self.sched.call_later(self.cfg.retx_interval, self._retx, seq)

    # ----- beacons
    def _beacon_tick(self, gen):
        with self.lock:
            if gen != self.beacon_gen or self.stop_evt.is_set() or self.sys_port is None:
                return
            if self.cfg.beacon_giveup and self.beacon_unacked >= self.cfg.beacon_giveup:
                if not self.beacon_lost:
                    self.beacon_lost = True
                    self.log("!! %d beacons un-ACKed in a row -> session lost, beacons stop until next SYSTEM_STARTUP"
                             % self.beacon_unacked)
                return
            dest = (self.host_ip, self.sys_port)
        payload = socket.inet_aton(self.detector_ip) + self.mac
        self.send_data(1, payload, dest=dest, label="beacon")
        self.bump("beacons_sent")
        self.sched.call_later(self.cfg.beacon_interval, self._beacon_tick, gen)

    # ----- receive side
    def _rx_loop(self):
        while not self.stop_evt.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            except OSError:
                if self.stop_evt.is_set():
                    return
                continue
            try:
                self.on_packet(data, addr)
            except Exception:
                self.log("!! exception in on_packet:\n" + traceback.format_exc())

    def on_packet(self, data, addr):
        self.bump("rx_pkts")
        if len(data) < 8:
            if data == b"\x00\x00\x00\x00":
                self.bump("quirk_probe4")
                self.log("RX %s:%d 4-byte zero probe (existing Python host quirk; ignored)" % addr, pkt=True)
            else:
                self.mismatch("datagram shorter than a URP header (%d B)" % len(data), data)
            return
        seq, flag = struct.unpack_from("<II", data, 0)
        if flag != 0:
            self._on_bare_ack(seq, flag, data, addr)
            return
        self._on_data(seq, data, addr)

    def _ack_dest(self, cmd, payload, addr):
        """Bare ACKs go to the *system* port announced in SYSTEM_STARTUP (run_full_acquisition docstring: 'used for bare
        ACKs'), NOT to the source port of the packet - a 0x98 the existing host sends from :6660 must not be ACKed to the
        image port (real captures show no extra record on :6660)."""
        if cmd == CMD_STARTUP and len(payload) >= 6:
            return (socket.inet_ntoa(payload[2:6]), struct.unpack_from(">H", payload, 0)[0])
        with self.lock:
            if self.sys_port:
                return (self.host_ip, self.sys_port)
        return addr

    def _check_src(self, addr, what):
        ok = {5550} | self.announced_ports
        if self.sys_port:
            ok.add(self.sys_port)
        if self.args.expect_src_port:
            ok.add(self.args.expect_src_port)
        if addr[1] not in ok:
            self.mismatch("%s sent from UDP port %d (expected one of %s)" % (what, addr[1], sorted(ok)))

    def _on_bare_ack(self, seq, flag, data, addr):
        self.bump("acks_rx")
        self._check_src(addr, "bare ACK")
        if flag != 1 or len(data) != 8:
            self.mismatch("bare ACK malformed (flag=%d len=%d)" % (flag, len(data)), data, struct.pack("<II", seq, 1))
        with self.lock:
            p = self.pending.pop(seq, None)
            if p is not None:
                self.acked_recent.append(seq)
                if p.label == "beacon":
                    self.beacon_unacked = 0
                    self.cnt["beacon_acks"] += 1
                self.log("RX %s:%d bare-ACK seq=%d (%s, %.0f ms)" % (addr[0], addr[1], seq, p.label,
                                                                    (time.perf_counter() - p.t0) * 1000), pkt=True)
                return
            dup = seq in self.acked_recent
        self.bump("acks_dup" if dup else "stray_acks")
        self.log("RX %s:%d bare-ACK seq=%d (%s)" % (addr[0], addr[1], seq, "duplicate" if dup else "unknown seq"), pkt=True)

    def _on_data(self, seq, data, addr):
        if len(data) < 16:
            self.mismatch("data packet with CmdFlag 0 but no PDAP header (%d B)" % len(data), data)
            self.send_ack(seq, addr)
            return
        cmd, plen = struct.unpack_from("<II", data, 8)
        payload = data[16:16 + plen]
        self.log("RX %s:%d data seq=%d cmd=0x%X plen=%d %s" % (addr[0], addr[1], seq, cmd, plen, payload[:24].hex()),
                 pkt=True)
        # --- SeqId rule: the detector silently drops SeqIds it has already seen (SYSTEM_STARTUP always restarts)
        with self.lock:
            if cmd != CMD_STARTUP:
                if seq <= self.last_host_seq:
                    self.cnt["seq_drops"] += 1
                    self.log("!! SeqId rule: dropping seq=%d (<= last seen %d) cmd=0x%X - NOT ACKed"
                             % (seq, self.last_host_seq, cmd))
                    return
                self.last_host_seq = seq
        # --- script parse (GENERIC_SCRIPT) + reference-builder comparison
        script, script_err = None, None
        if cmd == CMD_GENERIC_SCRIPT:
            try:
                script = parse_script_payload(payload)
            except ScriptError as e:
                script_err = str(e)
        reason = self._check_packet(data, seq, cmd, plen, payload, script, script_err, addr)
        if reason:
            self.mismatch("cmd 0x%X seq=%d: %s" % (cmd, seq, reason), data)
        self.send_ack(seq, self._ack_dest(cmd, payload, addr))
        handler = {
            CMD_STARTUP: self._h_startup, CMD_PORT_SETUP: self._h_port_setup, CMD_SIGNATURE: self._h_signature,
            CMD_GENERIC_SCRIPT: self._h_script, CMD_EXECUTE: self._h_execute,
            CMD_HOST_SCRIPT_COMMAND: self._h_host_command, CMD_EXEC_STATUS_POLL: self._h_poll,
            CMD_XFER_REPLY: self._h_xfer_reply, CMD_ABORT_A: self._h_abort, CMD_ABORT_B: self._h_abort,
            CMD_ENUM_IMAGES: self._h_enum, CMD_PULL_IMAGE: self._h_pull,
        }.get(cmd)
        if handler is None:
            self.bump("unknown_cmds")
            self.log("unsupported host command 0x%X (ACKed, no reply)" % cmd)
            return
        handler(seq, cmd, payload, addr, script, script_err)

    # ----- reference-builder self check
    def _check_packet(self, data, seq, cmd, plen, payload, script, script_err, addr):
        self._check_src(addr, "cmd 0x%X" % cmd)

        def urp(pdap):
            return fp.make_urp_packet(pdap, reply_port=0, flag=seq)

        try:
            if plen != len(data) - 16:
                return "PDAP length field %d != bytes present %d" % (plen, len(data) - 16)
            if cmd == CMD_STARTUP:
                if plen != 6:
                    return "SYSTEM_STARTUP plen %d != 6" % plen
                if seq != 0:
                    return "SYSTEM_STARTUP SeqId %d != 0" % seq
                port = struct.unpack_from(">H", payload, 0)[0]
                exp = urp(fp.build_system_startup(socket.inet_ntoa(payload[2:6]), port))
                if port != addr[1]:
                    return "SYSTEM_STARTUP announces port %d but is sent from UDP port %d (byte order? the host must own the port it announces)" % (port, addr[1])
            elif cmd == CMD_PORT_SETUP:
                if plen != 4:
                    return "PORT_SETUP plen %d != 4" % plen
                cp, ip_ = struct.unpack(">HH", payload)
                exp = urp(fp.build_port_setup(cp, ip_))
                if cp != addr[1]:
                    return "PORT_SETUP announces cmd port %d but is sent from UDP port %d (byte order? announced reply port must be the sending socket)" % (cp, addr[1])
            elif cmd == CMD_SIGNATURE:
                exp = urp(fp.build_signature_request())
            elif cmd == CMD_GENERIC_SCRIPT:
                if script_err:
                    return "unparseable script: " + script_err
                if script.repeat == 0xFFFF:
                    return "infinite-loop script (repeat 0xFFFF, Script8 style) must never be downloaded"
                exp = urp(rebuild_script_pdap(script))
                if script.sid == 7 and script.is_roe_init() and script.repeat == 0:
                    gold = urp(fp.build_script_7_roe_init())
                    if data != gold:
                        return "Script7 differs from fp.build_script_7_roe_init()"
            elif cmd == CMD_EXECUTE:
                exp = urp(fp.build_execute_script())
            elif cmd == CMD_HOST_SCRIPT_COMMAND:   # no reference builder in flashpad_acquire.py: compare with make_pdap
                if plen != 4:
                    return "HOST_SCRIPT_COMMAND plen %d != 4" % plen
                exp = urp(fp.make_pdap(CMD_HOST_SCRIPT_COMMAND, payload))
            elif cmd == CMD_EXEC_STATUS_POLL:
                exp = urp(fp.make_pdap(CMD_EXEC_STATUS_POLL))
            elif cmd == CMD_XFER_REPLY:
                n = struct.unpack_from("<I", payload, 0)[0] if plen >= 4 else -1
                if n < 0 or plen != 4 + 4 * n:
                    return "cmd 9 plen %d inconsistent with numMissed %d" % (plen, n)
                ids = list(struct.unpack_from("<%dI" % n, payload, 4)) if n else []
                exp = urp(fp.build_image_xfer_status_reply(missed_ids=ids))
            elif cmd == CMD_ENUM_IMAGES:
                if plen != 4:
                    return "0x41 plen %d != 4" % plen
                exp = urp(fp.build_image_retrival_request(struct.unpack("<I", payload)[0]))
            elif cmd == CMD_PULL_IMAGE:
                if plen != 4:
                    return "0x98 plen %d != 4" % plen
                image_port, host_port = struct.unpack("<HH", payload)
                exp = urp(fp.build_image_retrival(host_port=host_port, image_port=image_port))
            elif cmd in (CMD_ABORT_A, CMD_ABORT_B):
                exp = urp(fp.make_pdap(cmd))
            else:
                return "command 0x%X is not part of the capture flow" % cmd
        except (struct.error, ValueError, OSError) as e:
            return "unparseable: %s" % e
        if data != exp:
            self.log("      expected %s" % exp[:96].hex())
            return "not byte-identical to the reference builder (%d B vs %d B)" % (len(data), len(exp))
        return None

    # ----- command handlers (all run on the RX thread)
    def _reply_dest(self):
        return (self.host_ip, self.cmd_port)

    def _h_startup(self, seq, cmd, payload, addr, script, err):
        if len(payload) < 6:
            return
        port = struct.unpack_from(">H", payload, 0)[0]
        ip = socket.inet_ntoa(payload[2:6])
        now = time.perf_counter()
        with self.lock:
            dup = (now - self.last_startup_t) < 0.5
            self.last_startup_t = now
            if ip != self.args.host_ip:
                self.log("note: host announced %s (differs from --host-ip %s); using the announced address" % (ip, self.args.host_ip))
            self.host_ip, self.sys_port = ip, port
            self.announced_ports.add(port)
            self.last_host_seq = 0
            self.roe_done = False
            self.beacon_unacked, self.beacon_lost = 0, False
            self.cnt["startups"] += 1
            self.abort_push()                       # a new SYSTEM_STARTUP aborts an in-progress push [INF]
            if not dup:
                self.beacon_gen += 1
                gen = self.beacon_gen
        self.log("SYSTEM_STARTUP: host %s, beacon/ack port %d%s" % (ip, port, " (repeat within 0.5 s)" if dup else ""))
        if not dup:
            self.sched.call_later(self.timing["startup_beacon"] * self.ts, self._beacon_tick, gen)

    def _h_port_setup(self, seq, cmd, payload, addr, script, err):
        if len(payload) < 4:
            return
        cp, ip = struct.unpack(">HH", payload[:4])
        with self.lock:
            self.cmd_port, self.img_port = cp, ip
            self.announced_ports |= {cp, ip}
        self.log("PORT_SETUP: replies -> %s:%d, images -> %s:%d" % (self.host_ip, cp, self.host_ip, ip))

    def _h_signature(self, seq, cmd, payload, addr, script, err):
        sig = (SIG_MAC + struct.pack("<IIII", 1, 0, 0, 0) + SIG_SERIAL.ljust(12, b"\x00")
               + SIG_MODEL.ljust(12, b"\x00") + SIG_FW)
        assert len(sig) == 54
        self.send_data(CMD_SIGNATURE, sig, label="signature")

    def _h_script(self, seq, cmd, payload, addr, script, err):
        status = 0
        with self.lock:
            if len(payload) > 511:
                status = 1
                why = "payload %d B > 511" % len(payload)
            elif len(self.scripts) >= 32:
                status = 1
                why = "script list full (32)"
            elif err or script is None:
                status = 2
                why = err
            else:
                self.scripts.append(script)
                why = script.describe()
            count = len(self.scripts)
            self.cnt["scripts" if status == 0 else "script_rejects"] += 1
        self.log("GENERIC_SCRIPT %s -> status %d, list=%d" % (why, status, count))
        self.send_data(CMD_GENERIC_SCRIPT, struct.pack("<BI", status, count), label="script-status")

    def _roe_ok(self, scripts):
        if self.roe_done:
            return True
        for sc in scripts:
            if sc.has_acq():
                return False
            if sc.is_roe_init():
                return True
        return True

    def _h_execute(self, seq, cmd, payload, addr, script, err):
        t_ms = int((time.perf_counter() - self.t0) * 1000) & 0xFFFFFFFF
        status, run = 0, None
        with self.lock:
            if self.stuck or self.cur_run is not None:
                status = 2
            elif not self.scripts:
                status = 1
            else:
                scripts = list(self.scripts)
                run = Run(scripts)
                if self.cfg.needs_roe and not self._roe_ok(scripts):
                    mode = self.cfg.needs_roe_mode
                    self.cnt["needs_roe_hits"] += 1
                    if mode == "status2":
                        status, run = 2, None
                        self.cnt["needs_roe_rejects"] += 1
                        if not self.cfg.keep_list_on_reject:
                            self.scripts = []
                    elif mode == "silent":
                        run.hang = True
                    else:
                        run.bad_frame = True
                if run is not None:
                    self.scripts = []
                    self.registry = []
                    self.abort_push()
                    self.host_events = {}
                    self.cur_run = run
                    self.cnt["executes"] += 1
            if status:
                self.cnt["execute_rejects"] += 1
        self.log("EXECUTE -> status %d (%s)" % (status, {0: "ok", 1: "list empty", 2: "busy/rejected"}[status]))
        self.sched.call_later(self.timing["exec_reply"] * self.ts, self.send_data, CMD_EXECUTE,
                              struct.pack("<IB", t_ms, status), None, "execute-reply")
        if run is not None:
            threading.Thread(target=self._run_list, args=(run,), daemon=True, name="sim-run").start()

    def _h_host_command(self, seq, cmd, payload, addr, script, err):
        ev = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else -1
        self.bump("host_events_rx")
        if not self.cfg.early_close:
            self.log("HOST_SCRIPT_COMMAND event %d ignored (--no-early-close)" % ev)
            return
        with self.lock:
            win = self.window_open
        if ev == 1 and not win:
            self.bump("early_events_ignored")
            self.flow("HOST_SCRIPT_COMMAND event 1 sent while no integration window is open (must follow 0x40000[1])")
            return
        self._ev(ev).set()
        self.log("HOST_SCRIPT_COMMAND event %d (window open: %s)" % (ev, win))

    def _h_poll(self, seq, cmd, payload, addr, script, err):
        self.send_data(CMD_EXEC_STATUS_POLL, self.last_exec_complete, label="status-poll")

    def _h_abort(self, seq, cmd, payload, addr, script, err):
        self.bump("aborts")
        self._abort_all("opcode 0x%02X" % cmd)
        self.send_data(cmd, struct.pack("<I", 0), label="abort-reply")

    # ----- script runner
    def _sleep_real(self, run, secs):
        if secs > 0 and run.abort.wait(secs):
            raise _Aborted()
        if run.abort.is_set():
            raise _Aborted()

    def _sleep(self, run, model_secs):
        self._sleep_real(run, model_secs * self.ts)

    def _wait_event(self, run, ev_id, real_timeout):
        ev = self._ev(ev_id)
        deadline = None if real_timeout is None else time.perf_counter() + real_timeout
        while True:
            if run.abort.is_set():
                raise _Aborted()
            rem = 0.05 if deadline is None else min(0.02, deadline - time.perf_counter())
            if deadline is not None and rem <= 0:
                return False
            if ev.wait(max(rem, 0.001)):
                return True

    def _run_list(self, run):
        T = self.timing
        n_exec = 0
        try:
            for sc in run.scripts:
                iters = 0
                while True:
                    for c in sc.cmds:
                        self._exec_cmd(run, sc, c)
                    iters += 1
                    if sc.repeat == 0xFFFF:
                        if self._ev(sc.repeat_event).is_set():
                            break
                    elif iters >= max(1, sc.repeat):
                        break
                if sc.is_roe_init():
                    with self.lock:
                        self.roe_done = True
                    self.log("ROE init script finished (session ROE state: initialised)")
                n_exec += 1
            self._sleep(run, T["list_tail"])
        except _Aborted:
            self.log("script run aborted")
            return
        except Exception:
            self.log("!! script runner crashed:\n" + traceback.format_exc())
            with self.lock:
                if self.cur_run is run:
                    self.cur_run = None
            return
        words = (len(run.scripts), n_exec, 0 if run.bad_frame else 1, 0)
        payload = struct.pack("<IIII", *words)
        with self.lock:
            if self.cur_run is not run:
                return
            self.cur_run = None
            self.last_exec_complete = payload
            self.cnt["exec_complete"] += 1
            imgs = list(run.images)
        self.log("EXECUTION_COMPLETE %s" % payload.hex())
        self.send_data(EV_EXEC_COMPLETE, payload, label="exec-complete")
        for img in imgs:
            self.sched.call_later(T["exec_complete_to_30000"] * self.ts, self._send_xfer_query, img, True)

    def _exec_cmd(self, run, sc, c):
        T = self.timing
        if isinstance(c, Roe):
            self._sleep(run, T["roe_pulse"] if (c.cmd == 0 and c.data == 0) else T["roe_init"])
        elif isinstance(c, Dly):
            self._sleep_real(run, c.us / 1e6 * self.ts)
        elif isinstance(c, Evt):
            self.send_data(EV_HOST_EVENT, struct.pack("<I", c.id), label="host-event %d" % c.id)
        elif isinstance(c, Wait):
            self._wait_event(run, c.id, None if c.timeout == 0 else c.timeout / 1e6 * self.ts)
        elif isinstance(c, Acq):
            self._do_acquisition(run, sc, c)

    def _do_acquisition(self, run, sc, c):
        T, cfg = self.timing, self.cfg
        kind = "dark" if c.type_mode == 1 else "xray"
        self._sleep(run, T["acq_window_open"] + c.no_scrubs * c.scrub_dur / TICKS_PER_S)
        if run.hang:
            self.log("acquisition hangs (--needs-roe-mode silent): no window event, no completion")
            run.abort.wait()
            raise _Aborted()
        ev1 = self._ev(1)
        ev1.clear()
        with self.lock:
            self.window_open = True
            seed = cfg.seed if cfg.fixed_seed else cfg.seed + self.cnt["captures"]
            self.last["acq"] = dict(type_mode=c.type_mode, kind=kind, window_ticks=c.max_expose, scrubs=c.no_scrubs,
                                    scrub_dur=c.scrub_dur, tail=c.tail, transfer=c.transfer, script_id=sc.sid)
        window_s = c.max_expose / TICKS_PER_S * cfg.window_scale * cfg.time_scale
        if cfg.suppress_window_event:
            self.log("window open (0x40000[1] suppressed by --suppress-window-event), %.3f s" % window_s)
        else:
            self.send_data(EV_HOST_EVENT, struct.pack("<I", 1), label="window-open")
            self.log("window open %.3f s (%s, %d ticks)" % (window_s, kind, c.max_expose))
        early = self._wait_event(run, 1, window_s) if cfg.early_close else (self._sleep_real(run, window_s) or False)
        with self.lock:
            self.window_open = False
        self.log("window closed (%s)" % ("early, host event 1" if early else "expired"))
        self.send_data(EV_WINDOW_END, struct.pack("<III", 0x10006, 5, 0), label="window-end")
        t_gen = time.perf_counter()
        fr = self.frames.bad() if run.bad_frame else self.frames.get(kind, seed)
        rem = T["readout"] * self.ts - (time.perf_counter() - t_gen)
        self._sleep_real(run, max(rem, 0.0))
        sid = sc.sid
        counter = run.counters[sid]
        run.counters[sid] += 1
        img = Image(((sid & 0xFFFF) << 16) | (counter & 0xFFFF), sid, counter, kind, seed, fr, run.bad_frame)
        with self.lock:
            if self.cur_run is not run:
                raise _Aborted()
            self.registry.append(img)
            self.registry = self.registry[-8:]
            run.images.append(img)
            self.cnt["captures"] += 1
            self.last.update(crc=fr["crc"], id=img.id, kind=kind, seed=seed, mean=fr["mean"], std=fr["std"],
                             bad=run.bad_frame, arr=fr["arr"], data=fr["data"])
            if cfg.stuck_busy_after and self.cnt["captures"] >= cfg.stuck_busy_after and not self.stuck:
                self.stuck = True
                self.log("!! --stuck-busy: detector now permanently busy (EXECUTE -> status 2)")
        self.log("image registered id=0x%08X %s seed=%d crc32=%08x mean=%.1f%s" % (img.id, kind, seed, fr["crc"], fr["mean"],
                                                                               " [BAD FRAME]" if run.bad_frame else ""))
        self._sleep(run, 0.0)

    # ----- image transfer
    def _held(self):
        return [i for i in self.registry if i.state == "held"]

    def _send_xfer_query(self, img, first=False):
        with self.lock:
            if img.state != "held" or img not in self.registry:
                return
            if not first and not self.push_q.empty():
                return
        self.bump("xfer_queries")
        self.send_data(EV_XFER_QUERY, struct.pack("<I", img.id), label="xfer-query")

    def _h_enum(self, seq, cmd, payload, addr, script, err):
        with self.lock:
            held = self._held()
            for i in held:
                i.enumerated = True
            body = struct.pack("<I", len(held) * 4 + 4) + b"".join(struct.pack("<I", i.id) for i in held)
            first = held[0] if held else None
            mode = self.cfg.second_30000
        self.log("0x41 enumerate -> %d image(s) %s" % (len(held), [hex(i.id) for i in held]))
        if first is not None and not first.second_sent and mode != "none":
            first.second_sent = True
            if mode == "before":
                self._send_xfer_query(first, True)
            else:
                self.sched.call_later(self.timing["second_30000_gap"] * self.ts, self._send_xfer_query, first, True)
        self.send_data(CMD_ENUM_IMAGES, body, label="enum-reply")

    def _h_pull(self, seq, cmd, payload, addr, script, err):
        with self.lock:
            held = self._held()
            img = held[0] if held else None
            status = 0
            if img is None:
                status = 1
            elif not img.enumerated:
                status = 1
                self.flow("0x98 without a preceding 0x41 (0x41 is a hard prerequisite) - refused with status 1")
            else:
                img.pulled = True
                img.retries += 1
                self.cnt["pushes"] += 1
        self.log("0x98 pull -> status %d" % status)
        self.send_data(CMD_PULL_IMAGE, struct.pack("<I", status), label="pull-reply")
        if status == 0:
            self.sched.call_later(self.timing["push_start"] * self.ts, self._queue_push, img, list(range(NBLOCKS)), True,
                                  self.push_gen, "push")

    def _queue_push(self, img, blocks, first, gen, label):
        if gen != self.push_gen:
            return
        self.push_q.put(PushJob(img, blocks, first, gen, label))

    def _h_xfer_reply(self, seq, cmd, payload, addr, script, err):
        n = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
        ids = [struct.unpack_from("<I", payload, 4 + 4 * k)[0] & 0xFFFF for k in range(min(n, (len(payload) - 4) // 4))]
        with self.lock:
            held = self._held()
            img = held[0] if held else None
            if img is None:
                self.log("cmd 9 (n=%d) but no image held - ignored" % n)
                self.cnt["cmd9_no_image"] += 1
                return
            bad_claim = False
            if n == 0:
                missing = NBLOCKS - len(img.delivered)
                bad_claim = (missing > 0) or (not img.pulled)
                self.cnt["cmd9_free"] += 1
                img.state = "freed"
                self.cnt["images_freed"] += 1
                self.abort_push()
            elif not img.pulled:
                self.cnt["cmd9_prepull"] += 1
            else:
                img.retries += 1
        if n == 0:
            self.log("cmd 9 n=0: image 0x%08X freed (%d/%d blocks had been delivered)" % (img.id, len(img.delivered), NBLOCKS))
            if bad_claim:
                self.flow("cmd 9 n=0 sent but image 0x%08X is incomplete on the wire (%d/%d blocks delivered, pulled=%s)"
                          % (img.id, len(img.delivered), NBLOCKS, img.pulled))
            if self.cfg.deadbeef:
                self.sched.call_later(self.timing["deadbeef"] * self.ts, self.send_data, EV_XFER_QUERY,
                                      b"\xEF\xBE\xAD\xDE", None, "xfer-query DEADBEEF")
            return
        if not img.pulled:
            self.log("cmd 9 n=%d before 0x98: keep-alive, nothing re-sent%s" % (
                n, " (--prepull-resend: re-sending those blocks)" if self.cfg.prepull_resend else ""))
            if self.cfg.prepull_resend:
                valid = sorted({b for b in ids if b < NBLOCKS})
                self._queue_push(img, valid, False, self.push_gen, "prepull-resend")
            return
        if img.retries > self.cfg.max_retries + 1:      # push counts as #1; push + max_retries repairs are served
            with self.lock:
                img.state = "freed"
                self.cnt["images_freed"] += 1
                self.cnt["retry_exhausted"] += 1
            self.log("!! image 0x%08X: retry counter %d > %d -> image FREED by the detector" % (img.id, img.retries, self.cfg.max_retries + 1))
            self.flow("retry counter exhausted (%d > %d): host needed too many repair rounds" % (img.retries, self.cfg.max_retries + 1))
            self.sched.call_later(self.timing["deadbeef"] * self.ts, self.send_data, EV_XFER_QUERY, b"\xEF\xBE\xAD\xDE", None,
                                  "xfer-query DEADBEEF")
            return
        bad = [b for b in ids if b >= NBLOCKS]
        if bad:
            self.flow("cmd 9 lists block indexes >= 2048: %s" % bad[:8])
        if n > 256:
            self.bump("cmd9_whole")
            self.log("cmd 9 n=%d > 256 -> re-sending the WHOLE image (round %d)" % (n, img.retries))
            blocks = list(range(NBLOCKS))
        else:
            self.bump("cmd9_selective")
            blocks = sorted({b for b in ids if b < NBLOCKS})
            self.log("cmd 9 n=%d -> re-sending %d block(s) (round %d)" % (n, len(blocks), img.retries))
        self.bump("resend_rounds")
        self._queue_push(img, blocks, False, self.push_gen, "resend")

    def _push_worker(self):
        while not self.stop_evt.is_set():
            try:
                job = self.push_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if job.gen != self.push_gen:
                continue
            try:
                self._do_push(job)
            except Exception:
                self.log("!! push crashed:\n" + traceback.format_exc())

    def _do_push(self, job):
        img, cfg = job.image, self.cfg
        if img.state != "held":
            return
        rng = random.Random(cfg.drop_seed * 1000003 + img.seed * 7919 + (0 if job.first else img.retries))
        dropped = set()
        if job.first:
            if cfg.drop_blocks:
                dropped |= set(rng.sample(range(NBLOCKS), min(NBLOCKS, cfg.drop_blocks)))
            if cfg.drop_rate:
                dropped |= {b for b in job.blocks if rng.random() < cfg.drop_rate}
        elif cfg.drop_retx_rate:
            dropped |= {b for b in job.blocks if rng.random() < cfg.drop_retx_rate}
        dest = (self.host_ip, self.img_port)
        interval = WIRE_BYTES * 8.0 / (cfg.rate_mbps * 1e6) if cfg.rate_mbps > 0 else 0.0
        data = img.frame["data"]
        sent = nd = 0
        t0 = time.perf_counter()
        aborted = False
        for i, b in enumerate(job.blocks):
            if job.gen != self.push_gen or img.state != "held":
                aborted = True
                break
            if interval:
                target = t0 + i * interval
                while True:
                    dt = target - time.perf_counter()
                    if dt <= 0:
                        break
                    if dt > 0.002:
                        time.sleep(dt - 0.001)
            if b in dropped:
                nd += 1
                continue
            pkt = struct.pack("<II", img.id, b) + data[b * PAYLOAD:(b + 1) * PAYLOAD]
            try:
                self.sock.sendto(pkt, dest)
            except OSError:
                self.bump("send_errors")
            img.delivered.add(b)
            sent += 1
        dt = time.perf_counter() - t0
        with self.lock:
            self.cnt["blocks_sent"] += sent
            self.cnt["drops"] += nd
            self.cnt["blocks_sent_last_push"] = sent
            self.cnt["push_jobs"] += 1
        self.log("%s of image 0x%08X: %d/%d datagrams sent, %d dropped on purpose%s, %.3f s (%.1f Mbit/s payload)"
                 % (job.label, img.id, sent, len(job.blocks), nd, " [ABORTED]" if aborted else "", dt,
                    sent * DGRAM * 8 / max(dt, 1e-9) / 1e6))
        if not aborted:
            self.sched.call_later(self.timing["post_push_30000"] * self.ts, self._send_xfer_query, img, False)

    # ----- HTTP side channel
    def truth(self):
        with self.lock:
            c = dict(self.cnt)
            last = self.last
            reg = [dict(id="0x%08X" % i.id, state=i.state, retries=i.retries, enumerated=i.enumerated, pulled=i.pulled,
                        delivered=len(i.delivered)) for i in self.registry]
            sess = dict(host_ip=self.host_ip, beacon_port=self.sys_port, cmd_port=self.cmd_port, img_port=self.img_port,
                        last_host_seq=self.last_host_seq, det_seq=self.det_seq, script_list=len(self.scripts),
                        busy=self.cur_run is not None, stuck=self.stuck, roe_done=self.roe_done,
                        window_open=self.window_open, pending_unacked=len(self.pending), registry=reg,
                        beacon_lost=self.beacon_lost,
                        state=("lost" if self.beacon_lost else "no-session" if self.sys_port is None
                               else "configured" if self.cmd_port != 48879 else "started"))
            keys = ("executes", "execute_rejects", "scripts", "script_rejects", "captures", "protocol_mismatches",
                    "flow_violations", "seq_drops", "stray_acks", "acks_dup", "unacked", "retx_pkts", "blocks_sent",
                    "blocks_sent_last_push", "drops", "pushes", "cmd9_selective", "cmd9_whole", "cmd9_free",
                    "cmd9_prepull", "images_freed", "resend_rounds", "needs_roe_rejects", "needs_roe_hits", "aborts",
                    "beacons_sent", "beacon_acks", "startups", "retry_exhausted", "exec_complete", "xfer_queries",
                    "send_errors", "quirk_probe4", "unknown_cmds", "early_events_ignored", "ctrl_dropped")
            out = {k: c.get(k, 0) for k in keys}
            out.update(
                crc32=("%08x" % last["crc"]) if last["crc"] is not None else None, crc32_int=last["crc"],
                image_id=last["id"], frame_type=last["kind"], frame_seed=last["seed"], frame_mean=last["mean"],
                frame_std=last["std"], frame_bad=last["bad"], last_acquisition=last["acq"],
                counters=c, mismatch_log=list(self.mismatch_log), flow_log=list(self.flow_log), session=sess,
                config=self.cfg.as_dict(), uptime_s=round(time.perf_counter() - self.t0, 1),
                geometry=dict(dim=DIM, blocks=NBLOCKS, datagram_bytes=DGRAM))
        return out

    def apply_config(self, d: dict):
        d = dict(d)
        with self.lock:
            if d.pop("reset_roe", False):
                self.roe_done = False
                self.log("config: ROE state forgotten (next acquisition without Script7 behaves per --needs-roe)")
            if "stuck_busy" in d:
                self.stuck = bool(d.pop("stuck_busy"))
                self.log("config: stuck-busy %s" % self.stuck)
            if d.get("stuck_busy_after") == 0:
                self.stuck = False
            changed = self.cfg.update(d)
        if changed:
            self.log("config changed: %s" % changed)
        return self.cfg.as_dict()

    def apply_reset(self, d: dict):
        if d.get("session", True):
            self._reset_session()
        if d.get("counters", True):
            with self.lock:
                self.cnt.clear()
                self.mismatch_log.clear()
                self.flow_log.clear()
        self.log("reset: %s" % d)

    def _start_http(self):
        sim = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, indent=1).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                p = self.path.split("?")[0]
                if p in ("/truth", "/"):
                    self._send(200, sim.truth())
                elif p == "/frame":
                    d = sim.last["data"]
                    if d is None:
                        self._send(404, {"ok": False, "error": "no frame yet"})
                    else:
                        self._send(200, d, "application/octet-stream")
                elif p == "/log":
                    self._send(200, "\n".join(sim.logbuf), "text/plain")
                else:
                    self._send(404, {"ok": False, "error": "unknown path"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    d = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"ok": False, "error": "bad json"})
                p = self.path.split("?")[0]
                if p == "/config":
                    self._send(200, {"ok": True, "config": sim.apply_config(d)})
                elif p == "/reset":
                    sim.apply_reset(d)
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"ok": False, "error": "unknown path"})

        self.http = ThreadingHTTPServer((self.args.http_host, self.args.http_port), H)
        threading.Thread(target=self.http.serve_forever, daemon=True, name="sim-http").start()
        self.log("HTTP side channel on http://%s:%d/truth" % (self.args.http_host, self.args.http_port))


# --------------------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(description="PC-side GE FlashPad detector simulator (see module docstring / README-sim.md)",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--bind-ip", default="192.168.1.30", help="local IP to bind (use 127.0.0.1 for loopback self tests, "
                                                             "0.0.0.0 to also receive subnet broadcasts)")
    p.add_argument("--host-ip", default="192.168.1.1", help="address replies go to before/without SYSTEM_STARTUP")
    p.add_argument("--cmd-port", type=int, default=8100, help="UDP port the detector listens on (also source of images)")
    p.add_argument("--detector-ip", default=None, help="IP reported in beacons (default: --bind-ip, 192.168.1.30 for 0.0.0.0)")
    p.add_argument("--mac", default="40:F4:A0:00:78:4D")
    p.add_argument("--http-host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=8199, help="0 disables the HTTP side channel")
    p.add_argument("--seq-start", type=int, default=100, help="first detector SeqId (host syncs to beacon SeqId+1)")
    p.add_argument("--expect-src-port", type=int, default=0, help="extra allowed host source port")
    p.add_argument("--timing", action="append", metavar="KEY=SECONDS", help="override a model timing constant "
                   "(%s)" % ", ".join(TIMING))
    g = p.add_argument_group("time / rate")
    g.add_argument("--time-scale", type=float, default=1.0, help="multiply ALL model delays and the window (0.1 = 10x faster)")
    g.add_argument("--window-scale", type=float, default=1.0, help="extra multiplier for the integration window only")
    g.add_argument("--rate-mbps", type=float, default=100.0, help="image datagram pacing (wire rate); 0 = burst")
    g.add_argument("--burst", action="store_true", help="same as --rate-mbps 0")
    g = p.add_argument_group("loss / latency")
    g.add_argument("--drop-blocks", type=int, default=0, help="drop N random blocks in the FIRST push of each image")
    g.add_argument("--drop-rate", type=float, default=0.0, help="per-datagram loss probability in the first push")
    g.add_argument("--drop-retx-rate", type=float, default=0.0, help="per-datagram loss probability in re-sent blocks")
    g.add_argument("--drop-seed", type=int, default=1)
    g.add_argument("--drop-ctrl-rate", type=float, default=0.0, help="loss probability for control packets (incl. ACKs)")
    g.add_argument("--latency-ms", type=float, default=0.0, help="added delay on every control packet")
    g.add_argument("--jitter-ms", type=float, default=0.0)
    g = p.add_argument_group("unknown real behaviour")
    g.add_argument("--needs-roe", action="store_true", help="an acquisition EXECUTE without Script7 earlier in the session "
                   "is rejected / produces a bad frame")
    g.add_argument("--needs-roe-mode", choices=["status2", "silent", "badframe"], default="status2")
    g.add_argument("--keep-list-on-reject", action="store_true", help="a rejected EXECUTE leaves the script list intact")
    g.add_argument("--stuck-busy", dest="stuck_busy_after", type=int, default=0, metavar="N",
                   help="after N captures the detector stays busy (EXECUTE -> status 2) until /config stuck_busy=false")
    g.add_argument("--max-retries", type=int, default=10, help="retry counter limit before the image is freed")
    g.add_argument("--retx-interval", type=float, default=0.5, help="seconds before an un-ACKed detector packet is re-sent")
    g.add_argument("--max-retx", type=int, default=3, help="max re-sends of an un-ACKed detector packet")
    g.add_argument("--beacon-interval", type=float, default=2.0)
    g.add_argument("--beacon-giveup", type=int, default=0, help="stop beaconing after N un-ACKed beacons in a row (0 = never)")
    g.add_argument("--second-0x30000", dest="second_30000", choices=["before", "after", "none"], default="before",
                   help="second 0x30000 right after 0x41: before/after the 0x41 reply, or never")
    g.add_argument("--prepull-resend", action="store_true", help="cmd 9 [..] before 0x98 re-sends those blocks")
    g.add_argument("--no-deadbeef", dest="deadbeef", action="store_false", help="do not send the DEADBEEF 0x30000 after a free")
    g.add_argument("--suppress-window-event", action="store_true", help="never send 0x40000[1]")
    g.add_argument("--no-early-close", dest="early_close", action="store_false", help="ignore HOST_SCRIPT_COMMAND event 1")
    g = p.add_argument_group("frames")
    g.add_argument("--seed", type=int, default=1, help="base seed; frame seed = seed + capture index")
    g.add_argument("--fixed-seed", action="store_true", help="every capture of a type returns the identical frame")
    p.add_argument("--punch", action="store_true", help="send a bare ACK from :8100 to host:5550/6660 at start (firewall)")
    p.add_argument("--quiet", action="store_true", help="do not print per-packet lines")
    p.add_argument("--log-file", default=None)
    p.add_argument("--selftest", action="store_true", help="run the loopback self test with the EXISTING Python host and exit")
    p.add_argument("--selftest-flows", default="dark-two-exec,gui-xray,negative", help="comma list: dark-two-exec, gui-xray, negative")
    return p


def run_server(args):
    if args.burst:
        args.rate_mbps = 0.0
    sim = Sim(args)
    sim.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        sim.log("Ctrl-C: shutting down")
    finally:
        sim.stop()


# --------------------------------------------------------------------------- self test (existing Python host)
def _http(url, data=None, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body


def host_reassemble(raw: bytes):
    """Copy of flashpad_capture_gui.reassemble() (numpy only) - what the existing GUI does with the receive_image() bytes."""
    stride, blocks, ids = DGRAM, {}, set()
    head = len(raw) % stride
    off = 0
    if head:
        blocks[0] = raw[:head].ljust(PAYLOAD, b"\x00")
        off = head
    while off + 8 <= len(raw):
        img_id, blk = struct.unpack_from("<II", raw, off)
        ids.add(img_id)
        if blk not in blocks:
            blocks[blk] = raw[off + 8: off + stride].ljust(PAYLOAD, b"\x00")
        off += stride
    buf = bytearray()
    missing = []
    for b in range(NBLOCKS):
        if b in blocks:
            buf += blocks[b]
        else:
            missing.append(b)
            buf += b"\x00" * PAYLOAD
    return np.frombuffer(bytes(buf), dtype="<u2").reshape(DIM, DIM), dict(missing=missing, ids=sorted(ids), head=head)


def _compare_to_truth(got, truth_bytes):
    ref = np.frombuffer(truth_bytes, dtype="<u2").reshape(DIM, DIM)
    rows_exact = int(np.sum(np.all(got == ref, axis=1)))
    row0_exact = bool(np.array_equal(got[0], ref[0]))
    # the documented host bug: block 0 loses its first 8 payload bytes (parsed as a URP/PDAP packet) -> row 0 shifted by 4 px
    row0_bug = bool(np.array_equal(got[0, :DIM - 4], ref[0, 4:]) and not got[0, DIM - 4:].any())
    return rows_exact, row0_exact, row0_bug


def negative_flow(base):
    """Prove the simulator's self-checks bite: send deliberately wrong packets from a raw socket and read the counters."""
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.bind(("127.0.0.1", 5550))
    sk.settimeout(0.3)
    dest = ("127.0.0.1", 8100)
    seq = [0]

    def send(pdap, s=None, flag=0):
        s = seq[0] if s is None else s
        sk.sendto(fp.make_urp_packet(pdap, reply_port=flag, flag=s), dest)

    def drain(t=0.6, ack=True):
        out, end = [], time.time() + t
        while time.time() < end:
            try:
                d, _ = sk.recvfrom(65535)
            except socket.timeout:
                continue
            sq, fl = struct.unpack_from("<II", d, 0)
            if fl == 0 and ack:
                sk.sendto(struct.pack("<II", sq, 1), dest)
            out.append((sq, fl, struct.unpack_from("<II", d, 8) if fl == 0 and len(d) >= 16 else None, d[16:]))
        return out

    def T():
        return json.loads(_http(base + "/truth"))

    checks = []
    t0 = T()
    send(fp.build_system_startup("127.0.0.1", 5550), 0)
    got = drain(0.5, ack=False)                                           # beacon is deliberately NOT ACKed
    checks.append(("STARTUP -> bare ACK + beacon", any(g[1] == 1 for g in got) and any(g[2] and g[2][0] == 1 for g in got)))
    time.sleep(1.3)                                                       # retx interval 0.2 s x3 then give up
    t1 = T()
    checks.append(("un-ACKed beacon retransmitted and counted", t1["retx_pkts"] - t0["retx_pkts"] >= 3 and t1["unacked"] - t0["unacked"] >= 1))
    drain(0.1)
    seq[0] = 200                                                          # PORT_SETUP with little-endian ports: wrong
    send(make_bad_port_setup())
    drain(0.3)
    t2 = T()
    checks.append(("wrong PORT_SETUP byte order flagged", t2["protocol_mismatches"] - t1["protocol_mismatches"] == 1))
    send(fp.build_port_setup(5550, 6660), 201)
    drain(0.3)
    send(fp.build_port_setup(5550, 6660), 201)                            # same SeqId again -> silently dropped
    drain(0.3)
    t3 = T()
    checks.append(("SeqId <= last seen dropped, no reply", t3["seq_drops"] - t2["seq_drops"] == 1))
    bad_script = struct.pack("<HHI", 3, 0, 0) + b"\x09\x00\x00" + b"\x00\x00"           # command code 9 does not exist
    send(fp.make_pdap(5, bad_script), 202)
    got = drain(0.5)
    st = [g[3][0] for g in got if g[2] and g[2][0] == 5]
    t4 = T()
    checks.append(("bad command code -> status 2 + mismatch", st == [2] and t4["protocol_mismatches"] - t3["protocol_mismatches"] == 1))
    send(fp.build_execute_script(), 203)
    got = drain(0.8)
    st = [g[3][4] for g in got if g[2] and g[2][0] == 6]
    checks.append(("EXECUTE with empty list -> status 1", st == [1]))
    send(fp.build_image_retrival(5550, 6660), 204)
    got = drain(0.4)
    st = [struct.unpack("<I", g[3][:4])[0] for g in got if g[2] and g[2][0] == 0x98]
    checks.append(("0x98 without an image -> status 1", st == [1]))
    send(fp.build_signature_request(), 205)
    got = drain(0.4)
    sig = [g[3] for g in got if g[2] and g[2][0] == 3]
    ok_sig = bool(sig) and len(sig[0]) == 54 and sig[0][22:31] == b"UA45829-7" and sig[0][34:43] == b"5340000-7"
    checks.append(("SIGNATURE reply is 54 B with serial/model", ok_sig))
    big = struct.pack("<HHI", 4, 0, 0) + bytes([5]) + struct.pack("<I", 1) * 1 + b"\x05\x01\x00\x00\x00" * 110 + b"\x00\x00"
    send(fp.make_pdap(5, big), 206)
    got = drain(0.4)
    st = [g[3][0] for g in got if g[2] and g[2][0] == 5]
    checks.append(("script > 511 B -> status 1", st == [1]))
    sk.close()
    bad = [n for n, ok in checks if not ok]
    return {"pass": not bad, "detail": "%d/%d negative checks ok%s" % (len(checks) - len(bad), len(checks),
                                                                         "; FAILED: %s" % bad if bad else "")}


def make_bad_port_setup():
    return fp.make_pdap(fp.CMD_PORT_SETUP, struct.pack("<HH", 5550, 6660))


def selftest(args):
    import tempfile
    port_http = 18199
    sim_args = ["--bind-ip", "127.0.0.1", "--host-ip", "127.0.0.1", "--http-port", str(port_http), "--quiet",
                "--time-scale", "0.1", "--window-scale", "0.1", "--rate-mbps", "100", "--retx-interval", "0.2"]
    import tempfile as _tf
    sim_log = os.path.join(_tf.gettempdir(), "flashpad_sim_selftest.log")
    open(sim_log, "w").close()
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sim_args + ["--log-file", sim_log],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port_http
    t_end = time.time() + 15
    while time.time() < t_end:
        try:
            _http(base + "/truth", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        print("FAIL: simulator did not come up")
        return 2
    results = []
    tmp = tempfile.mkdtemp(prefix="fpsim_")
    try:
        for flow in [f.strip() for f in args.selftest_flows.split(",") if f.strip()]:
            _http(base + "/reset", {"counters": True, "session": True})
            t0 = time.time()
            print("=== self test flow: %s" % flow, flush=True)
            sess = fp.FlashPadSession(detector_ip="127.0.0.1", host_ip="127.0.0.1", broadcast_addr="127.0.0.1",
                                      timeout=5.0, verbose=False)
            raw = b""
            try:
                if flow == "dark-two-exec":
                    # the proven command line:  flashpad_acquire.py --dark-only --two-exec   (REAL run_full_acquisition)
                    raw = sess.run_full_acquisition(output_dir=tmp, dark_only=True, two_exec=True, exec_timeout=30)
                elif flow == "gui-xray":
                    # flashpad_capture_gui._worker / capture_minimal.py sequence (serial trigger omitted)
                    sess._open_sockets()
                    ok = sess.discover()
                    sess.send_port_setup(host_cmd_port=sess.reply_port, host_img_port=fp.HOST_IMAGE_PORT)
                    sess.request_signature()
                    ok = ok and sess.download_script(fp.build_script_7_roe_init(), "Script7") and sess.execute_script() \
                        and sess.wait_for_execution_complete(timeout_s=30.0)
                    cmds = [fp.pack_acquisition(type_mode=0, image_id=1, no_scrubs=0, scrub_duration=50000,
                                                max_expose_time=250_000_000, tail_time=250000, transfer_mode=0),
                            fp.pack_delay(10000)]
                    ok = ok and sess.download_script(fp.build_generic_script(0, 0, 0, cmds), "Script0")
                    sess._open_image_socket(fp.HOST_IMAGE_PORT)
                    ok = ok and sess.execute_script()
                    raw = sess.receive_image(output_path=os.path.join(tmp, "gui.raw"), timeout_s=60.0, script_id=0,
                                             image_port=fp.HOST_IMAGE_PORT, wait_exec_complete=True) if ok else b""
                    sess._close_sockets()
                elif flow == "negative":
                    res = negative_flow(base)
                    results.append(res)
                    print("   result: %s | %s" % ("PASS" if res["pass"] else "FAIL", res["detail"]), flush=True)
                    continue
                else:
                    print("unknown flow", flow)
                    continue
            except Exception:
                traceback.print_exc()
            truth = json.loads(_http(base + "/truth"))
            frame = _http(base + "/frame", timeout=30) if truth["crc32"] else b""
            res = dict(flow=flow, seconds=round(time.time() - t0, 1), raw_bytes=len(raw), truth=truth)
            if raw and frame:
                got, info = host_reassemble(raw)
                rows_exact, row0_exact, row0_bug = _compare_to_truth(got, frame)
                res.update(rows_exact=rows_exact, row0_exact=row0_exact, row0_host_bug=row0_bug, missing=len(info["missing"]),
                           ids=[hex(i) for i in info["ids"]])
                res["pass"] = (rows_exact >= DIM - 1 and (row0_exact or row0_bug) and not info["missing"]
                               and truth["protocol_mismatches"] == 0 and truth["seq_drops"] == 0 and truth["unacked"] == 0)
            else:
                res["pass"] = False
            results.append(res)
            t = res["truth"]
            print("   result: %s | raw %d B | rows bit-exact %s/2048 | row0 exact=%s known-host-bug=%s | missing blocks %s | "
                  "mismatches=%d flow=%d seq_drops=%d unacked=%d stray_acks=%d retx=%d | %.1f s"
                  % ("PASS" if res["pass"] else "FAIL", len(raw), res.get("rows_exact"), res.get("row0_exact"),
                     res.get("row0_host_bug"), res.get("missing"), t["protocol_mismatches"], t["flow_violations"],
                     t["seq_drops"], t["unacked"], t["stray_acks"], t["retx_pkts"], res["seconds"]), flush=True)
            if t["mismatch_log"] or t["flow_log"]:
                print("   mismatch_log:", t["mismatch_log"], "flow_log:", t["flow_log"])
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
    ok = bool(results) and all(r["pass"] for r in results)
    if not ok:
        print("simulator log: %s" % sim_log)
    print("SELFTEST %s (%d flow(s))" % ("PASS" if ok else "FAIL", len(results)))
    return 0 if ok else 1


def main():
    args = build_parser().parse_args()
    if args.selftest:
        sys.exit(selftest(args))
    run_server(args)


if __name__ == "__main__":
    main()
