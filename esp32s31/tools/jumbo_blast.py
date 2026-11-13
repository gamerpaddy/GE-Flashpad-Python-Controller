#!/usr/bin/env python3
"""
jumbo_blast.py - UDP burst generator that mimics the detector's image push, to prove the ESP32-S31 EMAC accepts
~4146 B Ethernet frames (milestone 2, the "jumbo spike").

Each datagram is exactly 4104 B of UDP payload:   [imageId:u32 LE][blockIndex:u32 LE][4096 B pattern]
= 4104 + 8 (UDP) + 20 (IP) + 14 (Ethernet) = 4146 B frame (+4 FCS).  Sent from local port 8100 (like the detector)
to <host>:6660 (the image port).  Stdlib only.

  python jumbo_blast.py 192.168.1.1                       # 2048 datagrams, line-rate burst
  python jumbo_blast.py 192.168.1.1 --rate 100            # paced to 100 Mbit/s wire rate (~0.68 s for 2048)
  python jumbo_blast.py 192.168.1.1 -n 2048 --repeat 5 --gap 1
  python jumbo_blast.py 192.168.1.1 --verify-from-esp http://192.168.1.1
  python jumbo_blast.py --sink --bind-ip 127.0.0.1        # receiver: counts datagrams / lengths / block coverage
  python jumbo_blast.py --selftest                         # loopback sender + sink, no hardware

Notes for the bench: the PC NIC must have MTU >= 4200 (5000 recommended) or Windows IP-fragments each datagram into
3 frames and you are no longer testing jumbo frames at all  (check: netsh interface ipv4 show subinterfaces).
Port 8100 must be free (stop flashpad_sim.py first, or pass --src-port).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.request

PAYLOAD = 4096
DGRAM = 8 + PAYLOAD
WIRE_BYTES = DGRAM + 8 + 20 + 14 + 4 + 8 + 12      # + preamble + IFG
FRAME_BYTES = DGRAM + 8 + 20 + 14                  # Ethernet frame without FCS (~4146)


def make_payload(kind: str, blk: int) -> bytes:
    if kind == "index":                                    # every 16-bit pixel = block index
        return struct.pack("<H", blk & 0xFFFF) * (PAYLOAD // 2)
    if kind == "ramp":
        base = bytes(range(256)) * (PAYLOAD // 256)
        k = blk & 0xFF
        return base[k:] + base[:k]
    if kind == "const":
        return b"\xA5" * PAYLOAD
    if kind == "random":
        return os.urandom(PAYLOAD)
    raise ValueError(kind)


def wait_until(t_target):
    while True:
        dt = t_target - time.perf_counter()
        if dt <= 0:
            return
        if dt > 0.002:
            time.sleep(dt - 0.001)


def blast(host, port, n, image_id, bind_ip, src_port, rate_mbps, pattern, quiet=False):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
    try:
        s.bind((bind_ip, src_port))
    except OSError as e:
        raise SystemExit("cannot bind %s:%d (%s). Is flashpad_sim.py still running? Use --src-port." % (bind_ip, src_port, e))
    dest = (host, port)
    pkts = [struct.pack("<II", image_id, b) + make_payload(pattern, b) for b in range(n)]
    interval = WIRE_BYTES * 8.0 / (rate_mbps * 1e6) if rate_mbps > 0 else 0.0
    errors, sent = 0, 0
    t0 = time.perf_counter()
    for i, pkt in enumerate(pkts):
        if interval:
            wait_until(t0 + i * interval)
        try:
            s.sendto(pkt, dest)
            sent += 1
        except OSError as e:
            errors += 1
            if errors <= 3:
                print("send error: %s" % e)
    dt = time.perf_counter() - t0
    s.close()
    if not quiet:
        print("sent %d/%d datagrams of %d B (frame ~%d B) from %s:%d to %s:%d in %.3f s = %.1f Mbit/s payload, "
              "%.1f Mbit/s on the wire%s"
              % (sent, n, DGRAM, FRAME_BYTES, bind_ip or "0.0.0.0", src_port, host, port, dt,
                 sent * DGRAM * 8 / dt / 1e6, sent * WIRE_BYTES * 8 / dt / 1e6,
                 ", %d send errors" % errors if errors else ""))
    return sent, dt, errors


# ----------------------------------------------------------------------------- sink
class Sink:
    def __init__(self, bind_ip, port, n_expect=2048):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        self.s.bind((bind_ip, port))
        self.s.settimeout(0.2)
        self.n_expect = n_expect
        self.count, self.bad_len, self.blocks, self.ids, self.t_first, self.t_last = 0, 0, set(), set(), None, None
        self.lens = {}

    def run(self, idle_s, stop=None):
        last = time.time()
        while True:
            if stop is not None and stop.is_set():
                break
            try:
                d, a = self.s.recvfrom(65535)
            except socket.timeout:
                if self.count and time.time() - last > idle_s:
                    break
                if not self.count and stop is None and time.time() - last > 3600:
                    break
                continue
            now = time.perf_counter()
            last = time.time()
            self.t_first = self.t_first or now
            self.t_last = now
            self.count += 1
            self.lens[len(d)] = self.lens.get(len(d), 0) + 1
            if len(d) != DGRAM:
                self.bad_len += 1
                continue
            iid, blk = struct.unpack_from("<II", d, 0)
            self.ids.add(iid)
            self.blocks.add(blk)

    def report(self):
        miss = [b for b in range(self.n_expect) if b not in self.blocks]
        dt = (self.t_last - self.t_first) if self.t_first and self.t_last and self.t_last > self.t_first else 0
        print("sink: %d datagrams, lengths %s, imageIds %s, %d/%d distinct blocks, missing %d %s, span %.3f s"
              % (self.count, self.lens, [hex(i) for i in sorted(self.ids)], len(self.blocks), self.n_expect, len(miss),
                 miss[:10], dt))
        return len(miss) == 0 and self.bad_len == 0 and self.count >= self.n_expect


# ----------------------------------------------------------------------------- ESP verification
def _get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _flatten(o, prefix=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(_flatten(v, prefix + k + "."))
    elif isinstance(o, (int, float)) and not isinstance(o, bool):
        out[prefix[:-1]] = o
    return out


def esp_snapshot(base):
    snap = {"status": {}, "log": ""}
    try:
        snap["status"] = _flatten(json.loads(_get(base.rstrip("/") + "/api/status")))
    except Exception as e:
        snap["err"] = "status: %s" % e
    try:
        snap["log"] = _get(base.rstrip("/") + "/api/log").decode("utf-8", "replace")
    except Exception as e:
        snap["err"] = snap.get("err", "") + " log: %s" % e
    return snap


def esp_compare(before, after, n):
    """Look for any counter that grew by n (or at least moved) and for 'truncated' in the log delta."""
    ok, notes = None, []
    if "err" in before or "err" in after:
        notes.append("ESP not (fully) reachable: %s %s" % (before.get("err", ""), after.get("err", "")))
    keys = ("block", "jumbo", "rx", "frame", "trunc", "dgram", "udp", "eth", "err", "drop")
    moved = {k: (before["status"].get(k), v) for k, v in after["status"].items()
             if any(w in k.lower() for w in keys) and before["status"].get(k) != v}
    for k, (a, b) in sorted(moved.items()):
        notes.append("status %s: %s -> %s (%+g)" % (k, a, b, (b - a) if a is not None else b))
    hit = [k for k, (a, b) in moved.items() if a is not None and b - a == n and ("block" in k.lower() or "rx" in k.lower())]
    if hit:
        ok = True
        notes.append("block counter(s) %s advanced by exactly %d" % (hit, n))
    elif not any("block" in k.lower() or "rx" in k.lower() for k in after["status"]):
        notes.append("no block/rx counter in /api/status (the firmware may add e.g. eth.rx_blocks later) - "
                     "check the serial log for the hook counters instead")
    new_log = after["log"][len(before["log"]):] if after["log"].startswith(before["log"][:200]) else after["log"][-2000:]
    trunc = new_log.lower().count("truncated")
    if trunc:
        ok = False
        notes.append("device log reports %d 'truncated' message(s) -> EMAC still cuts the jumbo frames!" % trunc)
    elif after["log"]:
        notes.append("device log: no 'truncated' message")
    return ok, notes


# ----------------------------------------------------------------------------- main
def selftest():
    import random
    port = random.randint(20000, 40000)
    sink = Sink("127.0.0.1", port)
    stop = threading.Event()
    th = threading.Thread(target=sink.run, args=(0.5, stop), daemon=True)
    th.start()
    sent, dt, err = blast("127.0.0.1", port, 2048, 0x00010000, "127.0.0.1", 0, 100.0, "index")
    time.sleep(0.8)
    stop.set()
    th.join(2)
    ok = sink.report() and sent == 2048
    sent2, _, _ = blast("127.0.0.1", port, 16, 0x1234, "127.0.0.1", 0, 0, "ramp")      # burst mode runs too
    print("SELFTEST %s" % ("PASS" if ok and sent2 == 16 else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="?", default="192.168.1.1", help="destination (the ESP)")
    ap.add_argument("--port", type=int, default=6660, help="destination UDP port (image port)")
    ap.add_argument("-n", type=int, default=2048, help="datagrams per burst")
    ap.add_argument("--repeat", type=int, default=1, help="number of bursts")
    ap.add_argument("--gap", type=float, default=1.0, help="seconds between bursts")
    ap.add_argument("--image-id", type=lambda x: int(x, 0), default=0x00010000)
    ap.add_argument("--bind-ip", default="", help="local IP to send from (default: any; use 192.168.1.30 on the bench)")
    ap.add_argument("--src-port", type=int, default=8100, help="local source port (detector uses 8100)")
    ap.add_argument("--rate", type=float, default=0.0, help="wire rate in Mbit/s (0 = line-rate burst)")
    ap.add_argument("--pattern", choices=["index", "ramp", "const", "random"], default="index")
    ap.add_argument("--verify-from-esp", metavar="URL", default=None,
                    help="ESP base URL (e.g. http://192.168.1.1): snapshot /api/status + /api/log before/after and report")
    ap.add_argument("--settle", type=float, default=0.6, help="seconds to wait before the 'after' snapshot")
    ap.add_argument("--sink", action="store_true", help="receiver mode: bind --bind-ip:--port and count datagrams")
    ap.add_argument("--idle", type=float, default=2.0, help="sink: stop after this many idle seconds")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())
    if a.sink:
        s = Sink(a.bind_ip or "0.0.0.0", a.port, a.n)
        print("sink listening on %s:%d (stop after %.1f s idle) ..." % (a.bind_ip or "0.0.0.0", a.port, a.idle), flush=True)
        try:
            s.run(a.idle)
        except KeyboardInterrupt:
            pass
        sys.exit(0 if s.report() else 1)

    before = esp_snapshot(a.verify_from_esp) if a.verify_from_esp else None
    total_sent, any_err = 0, 0
    for r in range(a.repeat):
        sent, dt, err = blast(a.host, a.port, a.n, a.image_id, a.bind_ip, a.src_port, a.rate, a.pattern)
        total_sent += sent
        any_err += err
        if r + 1 < a.repeat:
            time.sleep(a.gap)
    if before is not None:
        time.sleep(a.settle)
        after = esp_snapshot(a.verify_from_esp)
        ok, notes = esp_compare(before, after, total_sent)
        print("ESP check: %s" % {True: "PASS", False: "FAIL", None: "no counter available (informational)"}[ok])
        for n in notes:
            print("  - " + n)
        if ok is False:
            sys.exit(1)
    sys.exit(1 if any_err else 0)


if __name__ == "__main__":
    main()
