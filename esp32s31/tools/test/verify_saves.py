#!/usr/bin/env python
"""Verify the four files the browser app saves (RAW original, RAW processed, PNG display, PNG 16-bit) against the mock server and the
Python reference code (flashpad_capture_gui).  Used by the manual browser test: the page uploads its downloads to the mock server's
/mock/upload (see README "Browser test"), then

  python tools/test/verify_saves.py --server http://127.0.0.1:8099 --dir <upload dir> --params '{"loPct":0.5,"hiPct":99.9,"clahe":true,"clip":3,"tiles":16,"palette":"Viridis","invert":true}' [--crop x0,y0,x1,y1]

Checks (exit status 0 only if all pass):
  1. *_raw.raw            == bytes of GET /api/frame (exactly as received)
  2. *_processed*.raw     == (dark subtract + line repair via the REAL _process code path) [cropped]   (u16 little-endian)
  3. *_gray16*.png        == the processed data, decoded with Pillow (16-bit grayscale)
  4. *_display*.png       == flashpad_capture_gui.render_rgb(processed full frame, ...)[crop]  (8-bit RGB, palette/CLAHE/invert baked in)
Only valid for the default processing chain (no denoise / enhancement / geometry) because the reference is the Python code.
"""
import argparse
import glob
import json
import os
import sys
import urllib.request

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "..")))
import flashpad_capture_gui as g  # noqa: E402


def newest(d, pattern):
    files = sorted(glob.glob(os.path.join(d, pattern)), key=os.path.getmtime)
    if not files:
        raise SystemExit("FAIL: no file matching %s in %s" % (pattern, d))
    return files[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--params", default="{}")
    ap.add_argument("--crop", default="")
    ap.add_argument("--no-dark", action="store_true")
    ap.add_argument("--thresh", type=float, default=25.0)
    a = ap.parse_args()
    P = dict(loPct=1.0, hiPct=99.5, clahe=False, clip=2.0, tiles=8, palette="Grayscale", invert=False)
    P.update(json.loads(a.params))
    crop = [int(v) for v in a.crop.split(",")] if a.crop else None
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (" " + extra if extra else ""))
        ok = ok and bool(cond)

    frame = urllib.request.urlopen(a.server + "/api/frame").read()
    raw = np.frombuffer(frame, "<u2").reshape(2048, 2048)
    dark = None if a.no_dark else np.frombuffer(urllib.request.urlopen(a.server + "/api/dark").read(), "<u2").reshape(2048, 2048)

    f_raw = newest(a.dir, "*_raw.raw")
    with open(f_raw, "rb") as f:
        saved = f.read()
    check("RAW original == /api/frame bytes (%d bytes)" % len(saved), saved == frame)

    # reference chain (same calls as GUI._process)
    view = raw
    if dark is not None:
        view = np.clip(raw.astype(np.int32) - dark.astype(np.int32), 0, 65535).astype(np.uint16)
    dmap = g.build_defect_map(view, dark, a.thresh)
    final = g.apply_defect_map(view, dmap)
    sl = (slice(crop[1], crop[3]), slice(crop[0], crop[2])) if crop else (slice(None), slice(None))
    exp = np.ascontiguousarray(final[sl])
    shape_tag = "_%dx%d" % (exp.shape[1], exp.shape[0]) if crop else ""

    f_proc = newest(a.dir, "*_processed%s.raw" % shape_tag)
    got = np.fromfile(f_proc, "<u2")
    check("RAW processed %s size %s" % (os.path.basename(f_proc), exp.shape), got.size == exp.size)
    if got.size == exp.size:
        got = got.reshape(exp.shape)
        check("RAW processed == dark-subtract + defect repair (Python reference), %d px" % exp.size, np.array_equal(got, exp),
              "(%d differing)" % int((got != exp).sum()))

    f_p16 = newest(a.dir, "*_gray16%s.png" % shape_tag)
    with Image.open(f_p16) as im:
        im.load()
        check("PNG16 mode %s" % im.mode, im.mode in ("I;16", "I;16B", "I"))
        p16 = np.array(im).astype(np.uint16)
    check("PNG16 == processed data exactly", p16.shape == exp.shape and np.array_equal(p16, exp))

    f_png = newest(a.dir, "*_display%s.png" % shape_tag)
    with Image.open(f_png) as im:
        im.load()
        check("PNG display mode %s size %s" % (im.mode, im.size), im.mode == "RGB")
        disp = np.array(im)
    full_rgb = np.array(g.render_rgb(final, max(0.0, P["loPct"]) / 100.0, min(100.0, P["hiPct"]) / 100.0, bool(P["clahe"]), float(P["clip"]), int(P["tiles"]),
                                     P["palette"], bool(P["invert"])))
    exp_rgb = full_rgb[sl]
    check("PNG display == render_rgb(processed) [crop], palette=%s clahe=%s invert=%s" % (P["palette"], P["clahe"], P["invert"]),
          disp.shape == exp_rgb.shape and np.array_equal(disp, exp_rgb), "(%d differing bytes)" % (int((disp != exp_rgb).sum()) if disp.shape == exp_rgb.shape else -1))
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
