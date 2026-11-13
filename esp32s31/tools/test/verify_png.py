#!/usr/bin/env python
"""Verify PNG files written by www/png16.js with Pillow + numpy.

  python verify_png.py manifest.json
manifest = [{"png": "a.png", "raw": "a.bin", "w": 512, "h": 400, "kind": "gray16"|"rgb8"}, ...]  (paths relative to the manifest)
gray16: raw = u16 little-endian samples;  rgb8: raw = interleaved RGB bytes.  Exit status 0 only if every pixel matches.
"""
import json
import os
import sys

import numpy as np
from PIL import Image


def check(entry, base):
    png = os.path.join(base, entry["png"])
    exp = np.fromfile(os.path.join(base, entry["raw"]), dtype="<u2" if entry["kind"] == "gray16" else np.uint8)
    w, h = entry["w"], entry["h"]
    with Image.open(png) as im:
        im.load()
        if entry["kind"] == "gray16":
            assert im.mode in ("I;16", "I;16B", "I"), "mode %s" % im.mode
            got = np.array(im).astype(np.uint16)
            exp = exp.reshape(h, w)
        else:
            assert im.mode == "RGB", "mode %s" % im.mode
            got = np.array(im)
            exp = exp.reshape(h, w, 3)
        assert got.shape == exp.shape, "shape %s != %s" % (got.shape, exp.shape)
        assert np.array_equal(got, exp), "pixel mismatch in %s (%d differing)" % (entry["png"], int((got != exp).sum()))
    return "%s ok (%s %dx%d)" % (entry["png"], entry["kind"], w, h)


def main():
    path = sys.argv[1]
    base = os.path.dirname(os.path.abspath(path))
    with open(path) as f:
        entries = json.load(f)
    bad = 0
    for e in entries:
        try:
            print(check(e, base))
        except Exception as ex:  # noqa: BLE001
            bad += 1
            print("FAIL", e.get("png"), ex)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
