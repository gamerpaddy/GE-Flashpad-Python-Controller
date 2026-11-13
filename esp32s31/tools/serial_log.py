#!/usr/bin/env python3
"""Read the ESP console (USB Serial/JTAG on COM50 by default) for N seconds and print it.

    python tools/serial_log.py [--port COM50] [--seconds 10] [--reset] [--filter TEXT]

--reset toggles RTS/DTR like esptool does (USB Serial/JTAG: EN via RTS) so you see the boot log from the start.
Does not use the interactive idf.py monitor, so it works from scripts/automation.
"""
import argparse
import sys
import time

import serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM50")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--reset", action="store_true", help="hard-reset the chip first")
    ap.add_argument("--filter", default=None, help="only print lines containing this text")
    ap.add_argument("--out", default=None, help="also write everything to this file")
    a = ap.parse_args()

    s = serial.Serial(a.port, a.baud, timeout=0.2, dsrdtr=False, rtscts=False)
    if a.reset:
        # USB Serial/JTAG reset sequence: DTR=0 (boot pin high), RTS=1 -> EN low, then release
        s.dtr = False
        s.rts = True
        time.sleep(0.1)
        s.rts = False
        time.sleep(0.05)
    end = time.time() + a.seconds
    buf = b""
    out = open(a.out, "w", encoding="utf-8") if a.out else None
    while time.time() < end:
        chunk = s.read(4096)
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if out:
                out.write(text + "\n")
            if a.filter is None or a.filter in text:
                print(text, flush=True)
    if buf:
        text = buf.decode("utf-8", errors="replace")
        if a.filter is None or a.filter in text:
            print(text)
    s.close()


if __name__ == "__main__":
    sys.exit(main())
