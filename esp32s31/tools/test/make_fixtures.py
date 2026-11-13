#!/usr/bin/env python
"""Reference fixtures for the Node tests (tools/test/*.test.mjs).

Everything is computed with the REAL reference code: functions imported from flashpad_capture_gui.py (_bad_lines,
build_defect_map, apply_defect_map, to_u8, render_rgb) and cv2 (CLAHE, medianBlur, GaussianBlur, applyColorMap).
Test inputs are NOT stored: both sides synthesise the same frames from an integer hash (see util.mjs), so the
fixture file only holds hashes of the expected outputs plus a few small arrays (< 300 KB in total).

  python tools/test/make_fixtures.py            -> tools/test/fixtures/expected.json
"""
import base64
import hashlib
import json
import math
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, REPO)
import flashpad_capture_gui as g  # noqa: E402  (the reference implementation)

OUT = os.path.join(HERE, "fixtures", "expected.json")
M32 = 0xFFFFFFFF


# ------------------------------------------------------------ shared synthetic data (mirrored in util.mjs)
def fmix32(h):
    h = h ^ (h >> np.uint32(16))
    h = h * np.uint32(0x85EBCA6B)
    h = h ^ (h >> np.uint32(13))
    h = h * np.uint32(0xC2B2AE35)
    h = h ^ (h >> np.uint32(16))
    return h


def hash2(x, y, seed):
    with np.errstate(over="ignore"):
        k = np.uint32((seed * 0xC2B2AE3D) & M32)
        return fmix32(x.astype(np.uint32) * np.uint32(0x9E3779B1) + y.astype(np.uint32) * np.uint32(0x85EBCA77) + k)


def synth_signal(W, H, seed, dead_rows=(), dead_cols=(), half_rows=(), half_cols=(), pixels=()):
    y, x = np.mgrid[0:H, 0:W]
    x = x.astype(np.int64)
    y = y.astype(np.int64)
    cx, cy, rr = W * 7 // 20, H * 9 // 20, min(W, H) // 7
    rx0, rx1, ry0, ry1 = W * 12 // 20, W * 15 // 20, H * 3 // 20, H * 7 // 20
    v = 1500 + ((x + y) >> 1)
    v = v + 1500 * (((x - cx) ** 2 + (y - cy) ** 2) < rr * rr)
    v = v + 3000 * ((x >= rx0) & (x < rx1) & (y >= ry0) & (y < ry1))
    h = hash2(x, y, seed).astype(np.int64)
    v = v + ((h >> 22) & 63) + ((h >> 11) & 63)
    for r in half_rows:
        v[r, :] //= 2
    for c in half_cols:
        v[:, c] //= 2
    for r in dead_rows:
        v[r, :] = 0
    for c in dead_cols:
        v[:, c] = 0
    for (px, py, val) in pixels:
        v[py, px] = val
    return v.astype(np.uint16)


def synth_dark(W, H, seed):
    y, x = np.mgrid[0:H, 0:W]
    x = x.astype(np.int64)
    y = y.astype(np.int64)
    h = hash2(x, y, seed).astype(np.int64)
    return (1789 + (x % 7) * 3 + (y % 5) * 2 + ((h >> 26) & 31)).astype(np.uint16)


# ------------------------------------------------------------ helpers
def sha(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def b64(a):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def idx(mask):
    return [int(i) for i in np.where(mask)[0]]


def clahe_numpy(u, tiles, clip, N=256, quant=True, out_max=255):
    """The float64 recipe from the task description (generalised to N bins): independent of OpenCV."""
    H, W = u.shape
    t = tiles
    tw, th = W // t, H // t
    area = tw * th
    cl = max(int(clip * area / N), 1)
    L = np.zeros((t, t, N))
    for j in range(t):
        for i in range(t):
            h = np.bincount(u[j * th:(j + 1) * th, i * tw:(i + 1) * tw].ravel(), minlength=N).astype(np.int64)
            clipped = int(np.maximum(h - cl, 0).sum())
            h = np.minimum(h, cl)
            batch, resid = divmod(clipped, N)
            h += batch
            if resid:
                step = max(N // resid, 1)
                k = 0
                while k < N and resid > 0:
                    h[k] += 1
                    k += step
                    resid -= 1
            lut = np.cumsum(h) * out_max / area
            L[j, i] = np.clip(np.round(lut), 0, 255) if quant else np.minimum(lut, out_max)
    ys = np.arange(H)[:, None]
    xs = np.arange(W)[None, :]
    fx = xs / tw - 0.5
    i1 = np.floor(fx).astype(int)
    a = fx - i1
    i2 = np.minimum(i1 + 1, t - 1)
    i1 = np.maximum(i1, 0)
    fy = ys / th - 0.5
    j1 = np.floor(fy).astype(int)
    b = fy - j1
    j2 = np.minimum(j1 + 1, t - 1)
    j1 = np.maximum(j1, 0)
    v = u.astype(int)
    top = L[j1, i1, v] * (1 - a) + L[j1, i2, v] * a
    bot = L[j2, i1, v] * (1 - a) + L[j2, i2, v] * a
    return np.clip(np.round(top * (1 - b) + bot * b), 0, 255).astype(np.uint8)


def pad101(a, p):
    return np.pad(a, p, mode="reflect")  # numpy 'reflect' == BORDER_REFLECT_101


def ref_bilateral(a, radius, sigma_range):
    a = a.astype(np.float64)
    H, W = a.shape
    ss = max(0.5, radius / 2)
    P = pad101(a, radius)
    num = a.copy()
    den = np.ones_like(a)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            d2 = dx * dx + dy * dy
            if (dx or dy) and d2 <= radius * radius:
                nb = P[radius + dy:radius + dy + H, radius + dx:radius + dx + W]
                w = math.exp(-d2 / (2 * ss * ss)) * np.exp(-0.5 * ((nb - a) / sigma_range) ** 2)
                num += w * nb
                den += w
    return (num / den).astype(np.float32)


def ref_nlm(a, h, S, p):
    a = a.astype(np.float64)
    H, W = a.shape
    P = pad101(a, p)
    num = a.copy()
    den = np.ones_like(a)
    n2 = (2 * p + 1) ** 2
    for dy in range(-S, S + 1):
        for dx in range(-S, S + 1):
            if dx == 0 and dy == 0:
                continue
            ys0, ys1 = max(0, -dy), min(H, H - dy)
            xs0, xs1 = max(0, -dx), min(W, W - dx)
            if ys1 <= ys0 or xs1 <= xs0:
                continue
            D = np.zeros((ys1 - ys0, xs1 - xs0))
            for qy in range(-p, p + 1):
                for qx in range(-p, p + 1):
                    A = P[p + ys0 + qy:p + ys1 + qy, p + xs0 + qx:p + xs1 + qx]
                    B = P[p + ys0 + dy + qy:p + ys1 + dy + qy, p + xs0 + dx + qx:p + xs1 + dx + qx]
                    D += (A - B) ** 2
            w = np.exp(-D / (n2 * h * h))
            nbv = a[ys0 + dy:ys1 + dy, xs0 + dx:xs1 + dx]
            num[ys0:ys1, xs0:xs1] += w * nbv
            den[ys0:ys1, xs0:xs1] += w
    return (num / den).astype(np.float32)


def running_median_baseline(med, win):
    n = med.size
    base = np.empty(n)
    for i in range(n):
        base[i] = np.median(med[max(0, i - win):min(n, i + win + 1)])
    return base


def ref_destripe(a, axis, win, strength):
    out = a.astype(np.float64)
    if axis in ("rows", "both"):
        med = np.median(out.astype(np.float32), axis=1).astype(np.float64)
        out = out - (strength * (med - running_median_baseline(med, win)))[:, None]
    if axis in ("cols", "both"):
        med = np.median(out.astype(np.float32), axis=0).astype(np.float64)
        out = out - (strength * (med - running_median_baseline(med, win)))[None, :]
    return out.astype(np.float32)


def ref_repair_pixels(img, thr, auto, manual):
    H, W = img.shape
    v = img.astype(np.float64)
    P = np.full((H + 2, W + 2), np.nan)
    P[1:-1, 1:-1] = v
    stack = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx or dy:
                stack.append(P[1 + dy:1 + dy + H, 1 + dx:1 + dx + W])
    flag = np.zeros((H, W), bool)
    if auto:
        m = np.nanmedian(np.stack(stack), axis=0)
        d = v - m
        mad = np.median(np.abs(d))
        sigma = 1.4826 * mad if mad > 0 else 1.0
        flag = np.abs(d) > thr * sigma
    for (x, y) in manual:
        flag[y, x] = True
    out = img.copy()
    for y, x in zip(*np.where(flag)):
        res = None
        for rad in (1, 2):
            vals = []
            for yy in range(max(0, y - rad), min(H, y + rad + 1)):
                for xx in range(max(0, x - rad), min(W, x + rad + 1)):
                    if (yy, xx) != (y, x) and not flag[yy, xx]:
                        vals.append(v[yy, xx])
            if vals:
                res = int(np.floor(np.median(vals) + 0.5))
                break
        if res is not None:
            out[y, x] = res
    return out, int(flag.sum())


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    X = {"meta": {"cv2": cv2.__version__, "numpy": np.__version__}}

    # ---------------- big frames (hash-compared): dead lines like the real panel (rows 48/1093, col 783) + partial defects
    raw = synth_signal(2048, 2048, 1, dead_rows=[48, 1093], dead_cols=[783], half_rows=[300], half_cols=[1500],
                       pixels=[(500, 500, 40000), (1500, 1700, 0)])
    dark = synth_dark(2048, 2048, 2)
    sub = np.clip(raw.astype(np.int32) - dark.astype(np.int32), 0, 65535).astype(np.uint16)
    X["dark_sub"] = {"sha": sha(sub), "zeros": int((sub == 0).sum())}

    det = {}
    for thr in (3.0, 25.0, 100.0):
        a = sub.astype(np.float32)
        d = dark.astype(np.float32)
        det["%g" % thr] = {
            "rows": idx(g._bad_lines(a, 0, thr)), "cols": idx(g._bad_lines(a, 1, thr)),
            "dark_rows": idx(g._bad_lines(d, 0, thr)), "dark_cols": idx(g._bad_lines(d, 1, thr)),
            "raw_rows": idx(g._bad_lines(raw.astype(np.float32), 0, thr)), "raw_cols": idx(g._bad_lines(raw.astype(np.float32), 1, thr)),
        }
    X["bad_lines"] = det
    dm = g.build_defect_map(sub, dark, 25.0)
    X["defect_map"] = {"rows": idx(dm["rows"]), "cols": idx(dm["cols"])}
    fixed = g.apply_defect_map(sub, dm)
    X["defect_apply"] = {"sha": sha(fixed)}
    dm_nodark = g.build_defect_map(raw, None, 25.0)
    X["defect_map_nodark"] = {"rows": idx(dm_nodark["rows"]), "cols": idx(dm_nodark["cols"]), "sha": sha(g.apply_defect_map(raw, dm_nodark))}
    # hand-made maps: first/last rows (copy-nearest), adjacent runs, columns at the edge
    rows = np.zeros(2048, bool)
    cols = np.zeros(2048, bool)
    rows[[0, 1, 2, 700, 701, 702, 703, 2046, 2047]] = True
    cols[[0, 5, 1000, 1001, 1002, 2047]] = True
    X["defect_apply_edges"] = {"rows": idx(rows), "cols": idx(cols), "sha": sha(g.apply_defect_map(sub, {"rows": rows, "cols": cols}))}

    # ---------------- percentile / to_u8 (exact)
    pct = {}
    for lo_p, hi_p in ((0.01, 0.995), (0.0, 1.0), (0.05, 0.95), (0.001, 0.9999), (0.2, 0.8)):
        s = np.sort(fixed[4:2048 - 8:3, ::3].ravel())
        lo = float(s[int(s.size * lo_p)])
        hi = float(s[min(s.size - 1, int(s.size * hi_p))])
        u8 = g.to_u8(fixed, lo_p, hi_p)
        pct["%g_%g" % (lo_p, hi_p)] = {"lo": lo, "hi": hi, "sha": sha(u8)}
    X["to_u8"] = pct
    reg = fixed[204:1844:3, 204:1844:3].ravel()
    s = np.sort(reg)
    X["percentile_region"] = {"lo": float(s[int(s.size * 0.01)]), "hi": float(s[min(s.size - 1, int(s.size * 0.995))]), "n": int(s.size)}

    # ---------------- CLAHE bit-exact on the windowed 2048^2 u8 image
    u8 = g.to_u8(fixed, 0.01, 0.995)
    X["clahe_input_sha"] = sha(u8)
    cl = {}
    for tiles in (2, 4, 8, 16, 32):
        for clip in (0.5, 2.0, 3.7, 12.0):
            cl["%d_%g" % (tiles, clip)] = sha(cv2.createCLAHE(clipLimit=clip, tileGridSize=(tiles, tiles)).apply(u8))
    X["clahe"] = cl
    # non-divisible sizes (cv2 pads with reflect-101): small arrays are stored in full
    odd = {}
    for (w, h, t, clip) in ((100, 75, 8, 2.0), (97, 61, 5, 4.0), (64, 64, 3, 1.0)):
        a = (synth_signal(w, h, 5) >> 4).astype(np.uint8)
        odd["%dx%d_%d_%g" % (w, h, t, clip)] = b64(cv2.createCLAHE(clipLimit=clip, tileGridSize=(t, t)).apply(a))
    X["clahe_odd"] = odd
    # float64 numpy recipe == cv2 on the 8-bit path (sanity), and the 12-bit non-cv2 reference
    a8 = (synth_signal(256, 192, 6) >> 5).astype(np.uint8)
    # (informational) the float64 recipe differs from cv2 on rare rounding ties; the JS port emulates cv2's float32 maths instead
    X["clahe_recipe_diff_px"] = int((clahe_numpy(a8, 8, 2.0) != cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(a8)).sum())
    a12 = (synth_signal(256, 192, 6) >> 1).astype(np.uint16) & 4095
    X["clahe12"] = {"clip": 3.0, "tiles": 8, "out": b64(clahe_numpy(a12, 8, 3.0, N=4096, quant=False))}

    # ---------------- palettes
    pal = {}
    ramp = np.arange(256, dtype=np.uint8)
    for name, cmap in g.PALETTES.items():
        t = np.stack([ramp] * 3, 1) if cmap is None else cv2.applyColorMap(ramp.reshape(1, 256), cmap)[0][:, ::-1]
        pal[name] = sha(np.ascontiguousarray(t))
    X["palettes"] = pal

    # ---------------- whole pipeline == render_rgb(final)
    cases = [
        {"name": "default", "clahe": False, "clip": 2.0, "tiles": 8, "pal": "Grayscale", "inv": False, "lo": 1.0, "hi": 99.5},
        {"name": "clahe_turbo", "clahe": True, "clip": 2.0, "tiles": 8, "pal": "Turbo", "inv": False, "lo": 1.0, "hi": 99.5},
        {"name": "inv_bone", "clahe": True, "clip": 3.0, "tiles": 16, "pal": "Bone", "inv": True, "lo": 0.5, "hi": 99.9},
        {"name": "hot_noclahe_inv", "clahe": False, "clip": 2.0, "tiles": 8, "pal": "Hot", "inv": True, "lo": 2.0, "hi": 98.0},
    ]
    for c in cases:
        rgb = np.array(g.render_rgb(fixed, max(0.0, c["lo"]) / 100.0, min(100.0, c["hi"]) / 100.0, c["clahe"], c["clip"], c["tiles"], c["pal"], c["inv"]))
        c["sha"] = sha(rgb)
    X["pipeline_cases"] = cases
    nodark = g.apply_defect_map(raw, dm_nodark)
    X["pipeline_nodark"] = {"sha": sha(np.array(g.render_rgb(nodark, 0.01, 0.995, False, 2.0, 8, "Grayscale", False)))}
    X["pipeline_final_sha"] = sha(fixed)

    # ---------------- filters (small images, arrays stored)
    W, H = 64, 48
    u = synth_signal(W, H, 7)
    f = u.astype(np.float32)
    flt = {"W": W, "H": H}
    for sigma in (0.8, 2.0):
        r = max(1, math.ceil(3.5 * sigma))
        flt["gauss_%g" % sigma] = b64(cv2.GaussianBlur(f, (2 * r + 1, 2 * r + 1), sigma, borderType=cv2.BORDER_REFLECT_101))
    flt["median3"] = b64(cv2.medianBlur(u, 3).astype(np.float32))
    flt["median5"] = b64(cv2.medianBlur(u, 5).astype(np.float32))
    flt["bilateral_2_80"] = b64(ref_bilateral(f, 2, 80.0))
    flt["bilateral_3_150"] = b64(ref_bilateral(f, 3, 150.0))
    flt["nlm_60_3_1"] = b64(ref_nlm(f, 60.0, 3, 1))
    flt["nlm_90_2_2"] = b64(ref_nlm(f, 90.0, 2, 2))
    blur = cv2.GaussianBlur(f, (2 * 5 + 1, 2 * 5 + 1), 1.2, borderType=cv2.BORDER_REFLECT_101)
    flt["unsharp_1.5_1.2"] = b64((f + 1.5 * (f - blur)).astype(np.float32))
    # local contrast, all sigmas on the exact-Gaussian path (<= 6 px): sigma0 = 1, levels 3 -> 1, 2, 4
    prev = f.astype(np.float64)
    out = f.astype(np.float64).copy()
    for k in range(3):
        sg = 1.0 * 2 ** k
        r = max(1, math.ceil(3.5 * sg))
        gk = cv2.GaussianBlur(f, (2 * r + 1, 2 * r + 1), sg, borderType=cv2.BORDER_REFLECT_101).astype(np.float64)
        out += 0.8 / (k + 1) * (prev - gk)
        prev = gk
    flt["local_0.8_1_3"] = b64(out.astype(np.float32))
    flt["destripe_both_16_0.8"] = b64(ref_destripe(u, "both", 16, 0.8))
    flt["destripe_rows_8_1"] = b64(ref_destripe(u, "rows", 8, 1.0))
    X["filters"] = flt

    # big smooth image for the large-sigma (approximate) background blur used by 'flatten' / coarse local-contrast bands
    big = synth_signal(128, 96, 9).astype(np.float32)
    sg = 12.0
    r = math.ceil(3.5 * sg)
    bg = cv2.GaussianBlur(big, (2 * r + 1, 2 * r + 1), sg, borderType=cv2.BORDER_REFLECT_101)
    m = float(bg.astype(np.float64).mean())
    fl = big.astype(np.float64) * (1 - 0.8 + 0.8 * m / np.maximum(bg.astype(np.float64), 0.02 * m))
    X["blur_large"] = {"sigma": sg, "W": 128, "H": 96, "bg": b64(bg), "flat_0.8": b64(fl.astype(np.float32))}

    # ---------------- single pixel repair
    px = synth_signal(96, 64, 11, pixels=[(10, 10, 30000), (50, 30, 0), (51, 30, 0), (52, 31, 45000), (0, 0, 20000), (95, 63, 25000), (30, 5, 0)])
    ra, na = ref_repair_pixels(px, 8.0, True, [])
    rm, nm = ref_repair_pixels(px, 8.0, False, [(20, 20), (21, 20)])
    rb, nb = ref_repair_pixels(px, 6.0, True, [(70, 40)])
    X["pixels"] = {"auto": b64(ra), "auto_n": na, "manual": b64(rm), "manual_n": nm, "both": b64(rb), "both_n": nb}

    # ---------------- geometry
    src = np.arange(35, dtype=np.uint16).reshape(5, 7)
    geo = []
    for rot in range(4):
        for fh in (0, 1):
            for fv in (0, 1):
                a = np.rot90(src, -rot)
                if fh:
                    a = a[:, ::-1]
                if fv:
                    a = a[::-1]
                geo.append({"rot": rot, "flipH": fh, "flipV": fv, "shape": list(a.shape), "data": [int(v) for v in a.ravel()]})
    X["geometry"] = geo

    # ---------------- ROI / profile
    X["roi"] = {"box": [5, 7, 40, 30], "n": 35 * 23, "mean": float(u[7:30, 5:40].mean()), "sd": float(u[7:30, 5:40].std()),
                "min": int(u[7:30, 5:40].min()), "max": int(u[7:30, 5:40].max())}
    x0, y0, x1, y1 = 3, 4, 50, 30
    ln = max(1, int(math.floor(math.hypot(x1 - x0, y1 - y0) + 0.5)))
    prof = []
    for i in range(ln + 1):
        t = i / ln
        prof.append(int(u[min(max(int(math.floor(y0 + (y1 - y0) * t + 0.5)), 0), H - 1), min(max(int(math.floor(x0 + (x1 - x0) * t + 0.5)), 0), W - 1)]))
    X["profile"] = {"line": [x0, y0, x1, y1], "values": prof}

    tmp = OUT + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(X, fh, separators=(",", ":"))
    os.replace(tmp, OUT)   # atomic: parallel test processes may race
    print("wrote %s (%d bytes); recipe-vs-cv2 diff px: %d" % (OUT, os.path.getsize(OUT), X["clahe_recipe_diff_px"]))


if __name__ == "__main__":
    main()
