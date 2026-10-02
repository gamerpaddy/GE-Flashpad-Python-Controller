#!/usr/bin/env python3
"""FlashPad Capture -- standalone GUI for the GE FlashPad detector.

One click: arm detector (two-phase EXECUTE) -> fire the X-ray source over serial
-> receive the 2048 image datagrams (with live progress) -> reassemble to
2048x2048 16-bit -> subtract dark/offset -> repair dead rows/cols/pixels
-> display with pan/zoom, CLAHE, palettes -> crop and save PNG/TIFF.

Dark frames live in <output>/darks/. Capturing a new dark auto-loads it as the active
correction and deletes the older ones. Loading a different offset, or changing any
display/defect setting, updates the image already on screen -- no re-exposure needed.

Requires: pyserial, Pillow, numpy, opencv (cv2), and flashpad_acquire.py alongside.
Host: detector on its own NIC, host 192.168.1.1/24, link 100 Mbps full, MTU >= 5000.
"""
import os
import sys
import glob
import struct
import threading
import queue
import time
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import flashpad_acquire as fp
except Exception as e:  # pragma: no cover
    print("Cannot import flashpad_acquire.py from %s: %s" % (HERE, e))
    raise

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

from PIL import Image, ImageTk

STRIDE = 4104          # datagram: [imageId:4LE][blockIndex:4LE][4096 B pixels]
PAYLOAD = 4096
NBLOCKS = 2048
DIM = 2048
TICK_HZ = 26.3e6       # measured: 250e6 ticks -> ~9.5 s window

PALETTES = {
    "Grayscale": None,
    "Bone": cv2.COLORMAP_BONE,
    "Hot": cv2.COLORMAP_HOT,
    "Inferno": cv2.COLORMAP_INFERNO,
    "Magma": cv2.COLORMAP_MAGMA,
    "Plasma": cv2.COLORMAP_PLASMA,
    "Viridis": cv2.COLORMAP_VIRIDIS,
    "Turbo": cv2.COLORMAP_TURBO,
    "Jet": cv2.COLORMAP_JET,
}

HELP_TEXT = """\
X-RAY SOURCE
------------
Exposure (ms)
    How long the Arduino holds pin 13 HIGH = how long the tube actually fires.
    Sent over serial as  C<ms>  . Longer = more dose = brighter. Main brightness control.

Serial port / Terminator
    Arduino trigger link (9600 8N1). The sketch ends a command on '\\r' OR '\\n', so
    cr, lf and crlf are all valid.

DTR / RTS
    CH340 clones tie DTR (sometimes RTS) to RESET through a capacitor, so merely opening
    the port can reboot the board and the bootloader can swallow the first command.
    'assert' = normal pyserial behaviour. 'deassert' = never touch the lines (no reset).
    'pulse' = deliberately reset, then wait.

Boot wait (s)
    Pause after opening the port before sending anything, so the sketch is running.

Test link (CW0)
    Sends CW0: moves the stepper zero steps and replies "OK". Fires NOTHING. The only
    way to prove the board is receiving, because the C command sends no reply at all.


DETECTOR
--------
Detector IP
    The FlashPad's address (default 192.168.1.30). The host must be 192.168.1.1/24.

Window (ticks)  =  the acquisition primitive's "MaxExpose Time"
    How long the panel keeps its integration window OPEN, in ~26 MHz ticks (measured),
    so 250,000,000 ~= 9.5 s and 4,000,000 ~= 150 ms.
    The X-ray must fire INSIDE this window -- the panel does NOT self-detect radiation
    (it is not AED); it is armed and then waits.
    GE's own Script0 uses 400,000 (~15 ms) because their system hardware-syncs the
    generator. We fire from software, so a longer window is more forgiving.
    TRADE-OFF: a longer window integrates more dark current, raising the baseline and
    noise. Shorten it once your timing is reliable, and re-shoot the dark when you do.

Scrubs  =  "No Of Scrubs"
    Readout/clear cycles run BEFORE the window opens, to flush residual charge off the
    panel. More scrubs = less ghosting/lag from the previous exposure, but each costs
    ~Scrub Duration (50,000 ticks ~= 2 ms) and delays the window. GE's Script0/1 use 0;
    their Script2 uses 15. If you see a faint copy of the previous shot, raise this.

TypeMode
    Which mode the detector's acquisition primitive runs.
      0 = standard acquisition (GE Script0 -- expects an exposure)
      1 = dark / offset acquisition (GE Script1 -- reads out with no X-ray)
    Leave at 0 for real shots. The DARK button works either way: it simply never fires.

(Fixed here: Scrub Duration 50,000 ; Tail Time 250,000 (~10 ms settle) ;
 Transfer Mode 0 (normal push-to-host) ; ImageId 1.)


OFFSET / DARK CORRECTION
------------------------
A dark frame is an exposure-free readout: the panel's fixed offset plus dark current for
that window length. Subtracting it removes the pedestal and most fixed-pattern noise.
IMPORTANT: a dark is only valid for the SAME Window (ticks) value, because dark current
scales with integration time. Re-shoot the dark whenever you change the window.
Capturing a dark auto-loads it and deletes older darks in <output>/darks/.


DEFECT CORRECTION
-----------------
Repair dead rows / columns
    Fixes the dark lines -- defective detector rows/columns -- by interpolating across
    them from their good neighbours.

Sensitivity
    How far a row/column median must sit from its local neighbourhood before being
    called defective, in robust (MAD-based) sigmas. LOWER = more aggressive.
    6 is a sane start; 4 catches faint lines; 10 is gentle if real structure is lost.

Also isolated pixels
    Repairs single hot/dead pixels too. The defect map comes from the DARK frame when one
    is loaded (correct -- defects are fixed-pattern); otherwise from the image itself,
    which is less reliable and can clip genuine detail.


DISPLAY  (affects the view and the saved PNG, never the 16-bit data)
-------
CLAHE + Clip / Tiles
    Contrast Limited Adaptive Histogram Equalisation: equalises contrast in local tiles
    instead of globally, so both dense and thin areas stay readable. Clip limits how much
    contrast any tile may gain (higher = stronger, but amplifies noise). Tiles sets the
    grid (8 = 8x8 tiles; fewer, larger tiles = more global, more = more local).

Palette / Invert
    False-colour map and polarity. Invert gives the familiar "film" look where dense
    material is bright.

Low % / High %
    Percentile window used to map 16-bit values into the 8-bit display range. Widen for
    a flatter image, narrow to stretch contrast around the mid-tones.


VIEW AND CROP
-------------
Mouse wheel        zoom about the cursor
Left-drag          pan (or move/resize the crop box when you grab it)
Right-drag         always pans
Fit / 1:1          zoom presets
Crop box           drag inside to move, grab an edge or corner handle to resize.
                   "Save PNG/TIFF (crop)" writes ONLY the boxed region --
                   PNG uses what you see (palette/CLAHE/invert), TIFF keeps the
                   original 16-bit values for quantitative work.
"""


# ----------------------------------------------------------------- reassembly
def reassemble(raw: bytes):
    blocks = {}
    ids = set()
    head = len(raw) % STRIDE      # first record often arrives 16 B short, header-less
    off = 0
    if head:
        blocks[0] = raw[:head].ljust(PAYLOAD, b"\x00")
        off = head
    while off + 8 <= len(raw):
        img_id, blk = struct.unpack_from("<II", raw, off)
        ids.add(img_id)
        if blk not in blocks:
            blocks[blk] = raw[off + 8: off + STRIDE].ljust(PAYLOAD, b"\x00")
        off += STRIDE
    buf = bytearray()
    missing = []
    for b in range(NBLOCKS):
        if b in blocks:
            buf += blocks[b]
        else:
            missing.append(b)
            buf += b"\x00" * PAYLOAD
    a = np.frombuffer(bytes(buf), dtype="<u2").reshape(DIM, DIM)
    return a, {"blocks": len(blocks), "missing": missing,
               "ids": sorted(ids), "short_first": head}


# ------------------------------------------------------------ defect handling
BORDER = 12     # outer rows/cols always look odd; don't let them dominate detection


def _bad_lines(a, axis, thresh, win=16, border=BORDER):
    med = np.median(a, axis=1 - axis).astype(np.float64)
    n = med.size
    base = np.empty(n)
    for i in range(n):
        lo, hi = max(0, i - win), min(n, i + win + 1)
        base[i] = np.median(med[lo:hi])
    dev = med - base
    mad = np.median(np.abs(dev - np.median(dev)))
    sigma = 1.4826 * mad if mad > 0 else 1.0
    bad = (np.abs(dev) / sigma) > thresh
    mx = a.max(axis=1 - axis)
    bad |= (mx == 0)                      # completely dead line
    if border:
        bad[:border] = False
        bad[-border:] = False
    return bad


def _interp_lines(f, bad, axis):
    if not bad.any():
        return f
    work = f if axis == 0 else f.T
    good = np.where(~bad)[0]
    if good.size < 2:
        return f
    badi = np.where(bad)[0]
    idx = np.searchsorted(good, badi)
    lo = good[np.clip(idx - 1, 0, good.size - 1)]
    hi = good[np.clip(idx, 0, good.size - 1)]
    span = np.maximum(hi - lo, 1)
    w = np.where(hi != lo, (badi - lo) / span, 0.0).astype(np.float32)[:, None]
    work[badi, :] = work[lo, :] * (1.0 - w) + work[hi, :] * w
    return work if axis == 0 else work.T


def build_defect_map(signal, dark, thresh):
    """Defective rows/columns from the SIGNAL frame unioned with the DARK frame.

    Dead lines here are mostly GAIN defects: they only show up where there is signal,
    so a dark frame alone cannot find them (measured: col 783 scores 320 sigma in the
    corrected image but 4 in the dark). The dark still contributes offset-type defects.

    Deliberately lines only. An isolated-pixel pass was tried and removed: its neighbour
    averages are taken from an image that still contains the dead lines, so pixels beside
    row 48 / 1093 / col 783 get flagged (10196 of them) and then "repaired" by averaging
    the dead line's zeros back in -- which smears the line WIDER instead of removing it.
    """
    a = signal.astype(np.float32)
    bad_rows = _bad_lines(a, 0, thresh)
    bad_cols = _bad_lines(a, 1, thresh)
    if dark is not None and dark.shape == signal.shape:
        d = dark.astype(np.float32)
        bad_rows |= _bad_lines(d, 0, thresh)
        bad_cols |= _bad_lines(d, 1, thresh)
    return {"rows": bad_rows, "cols": bad_cols}


def apply_defect_map(img, dmap):
    f = img.astype(np.float32).copy()
    f = _interp_lines(f, dmap["rows"], 0)
    f = _interp_lines(f, dmap["cols"], 1)
    return np.clip(f, 0, 65535).astype(np.uint16)


# ------------------------------------------------------------ display pipeline
def to_u8(img16, lo_pct, hi_pct):
    s = np.sort(img16[4:DIM - 8:3, ::3].ravel())
    lo = float(s[int(s.size * lo_pct)])
    hi = float(s[min(s.size - 1, int(s.size * hi_pct))])
    rng = max(1.0, hi - lo)
    return np.clip((img16.astype(np.float32) - lo) * (255.0 / rng), 0, 255).astype(np.uint8)


def render_rgb(img16, lo_pct, hi_pct, clahe_on, clip, tiles, palette, invert):
    u8 = to_u8(img16, lo_pct, hi_pct)
    if clahe_on:
        t = max(1, int(tiles))
        u8 = cv2.createCLAHE(clipLimit=float(clip), tileGridSize=(t, t)).apply(u8)
    if invert:
        u8 = 255 - u8
    cmap = PALETTES.get(palette)
    if cmap is None:
        rgb = np.dstack([u8, u8, u8])
    else:
        rgb = cv2.applyColorMap(u8, cmap)[:, :, ::-1]   # BGR -> RGB
    return Image.fromarray(np.ascontiguousarray(rgb), mode="RGB")


class Tip:
    def __init__(self, widget, text):
        self.w, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _=None):
        if self.tip:
            return
        x = self.w.winfo_rootx() + 20
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=390,
                 font=("Segoe UI", 8)).pack()

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# -------------------------------------------------------------------- viewer
class ImageView:
    """Canvas with wheel-zoom, drag-pan and a draggable/resizable red crop box."""
    HANDLE = 7

    def __init__(self, parent, on_crop=None):
        self.canvas = tk.Canvas(parent, bg="#101010", highlightthickness=1,
                                highlightbackground="#444", cursor="crosshair")
        self.img = None
        self.W = self.H = 0
        self.scale = 1.0
        self.ox = self.oy = 0.0
        self.crop = None
        self.on_crop = on_crop
        self._photo = None
        self._mode = None
        self._last = (0, 0)
        c = self.canvas
        c.bind("<Configure>", lambda e: self.render())
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self._wheel(e, 1))
        c.bind("<Button-5>", lambda e: self._wheel(e, -1))
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<ButtonPress-3>", self._press_pan)
        c.bind("<B3-Motion>", self._drag_pan)
        c.bind("<Motion>", self._hover)

    # -- coordinate helpers
    def i2c(self, x, y):
        return (x - self.ox) * self.scale, (y - self.oy) * self.scale

    def c2i(self, cx, cy):
        return self.ox + cx / self.scale, self.oy + cy / self.scale

    def set_image(self, pil, keep_view=True):
        first = self.img is None or (self.W, self.H) != pil.size
        self.img = pil
        self.W, self.H = pil.size
        if self.crop is None:
            m = int(min(self.W, self.H) * 0.1)
            self.crop = [m, m, self.W - m, self.H - m]
        if first or not keep_view:
            self.fit()
        else:
            self.render()

    def fit(self):
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        if not self.W:
            return
        self.scale = min(cw / self.W, ch / self.H)
        self.ox = (self.W - cw / self.scale) / 2.0
        self.oy = (self.H - ch / self.scale) / 2.0
        self.render()

    def one_to_one(self):
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        cx, cy = self.c2i(cw / 2, ch / 2)
        self.scale = 1.0
        self.ox = cx - cw / 2.0
        self.oy = cy - ch / 2.0
        self.render()

    def reset_crop(self):
        m = int(min(self.W, self.H) * 0.1)
        self.crop = [m, m, self.W - m, self.H - m]
        self.render()
        self._notify()

    def crop_full(self):
        self.crop = [0, 0, self.W, self.H]
        self.render()
        self._notify()

    def _notify(self):
        if self.on_crop:
            x0, y0, x1, y1 = self.get_crop()
            self.on_crop(x1 - x0, y1 - y0)

    def get_crop(self):
        x0, y0, x1, y1 = self.crop
        x0, x1 = sorted((int(round(x0)), int(round(x1))))
        y0, y1 = sorted((int(round(y0)), int(round(y1))))
        x0 = max(0, min(self.W - 1, x0)); x1 = max(x0 + 1, min(self.W, x1))
        y0 = max(0, min(self.H - 1, y0)); y1 = max(y0 + 1, min(self.H, y1))
        return x0, y0, x1, y1

    # -- rendering
    def render(self):
        c = self.canvas
        c.delete("all")
        if self.img is None:
            return
        cw = max(1, c.winfo_width())
        ch = max(1, c.winfo_height())
        ix0 = max(0, int(np.floor(self.ox)))
        iy0 = max(0, int(np.floor(self.oy)))
        ix1 = min(self.W, int(np.ceil(self.ox + cw / self.scale)) + 1)
        iy1 = min(self.H, int(np.ceil(self.oy + ch / self.scale)) + 1)
        if ix1 <= ix0 or iy1 <= iy0:
            return
        sub = self.img.crop((ix0, iy0, ix1, iy1))
        dw = max(1, int(round((ix1 - ix0) * self.scale)))
        dh = max(1, int(round((iy1 - iy0) * self.scale)))
        rs = Image.NEAREST if self.scale >= 1.5 else Image.BILINEAR
        self._photo = ImageTk.PhotoImage(sub.resize((dw, dh), rs))
        px, py = self.i2c(ix0, iy0)
        c.create_image(int(round(px)), int(round(py)), anchor="nw", image=self._photo)
        # crop rectangle + handles
        x0, y0, x1, y1 = self.get_crop()
        a = self.i2c(x0, y0)
        b = self.i2c(x1, y1)
        c.create_rectangle(a[0], a[1], b[0], b[1], outline="#ff2d2d", width=2)
        for hx, hy in self._handles(a, b):
            c.create_rectangle(hx - self.HANDLE, hy - self.HANDLE,
                               hx + self.HANDLE, hy + self.HANDLE,
                               outline="#ff2d2d", fill="#ff2d2d")
        c.create_text(a[0] + 4, a[1] - 10, anchor="w", fill="#ff6a6a",
                      font=("Consolas", 8),
                      text="crop %dx%d  @%.0f%%" % (x1 - x0, y1 - y0, self.scale * 100))

    def _handles(self, a, b):
        mx = (a[0] + b[0]) / 2.0
        my = (a[1] + b[1]) / 2.0
        return [(a[0], a[1]), (mx, a[1]), (b[0], a[1]),
                (a[0], my), (b[0], my),
                (a[0], b[1]), (mx, b[1]), (b[0], b[1])]

    # -- interaction
    def _wheel(self, e, direction=None):
        if self.img is None:
            return
        d = direction if direction is not None else (1 if e.delta > 0 else -1)
        ix, iy = self.c2i(e.x, e.y)
        f = 1.25 if d > 0 else 1 / 1.25
        new = min(40.0, max(0.05, self.scale * f))
        self.scale = new
        self.ox = ix - e.x / self.scale
        self.oy = iy - e.y / self.scale
        self.render()

    def _hit(self, e):
        if self.img is None or self.crop is None:
            return None
        x0, y0, x1, y1 = self.get_crop()
        a = self.i2c(x0, y0)
        b = self.i2c(x1, y1)
        names = ["nw", "n", "ne", "w", "e", "sw", "s", "se"]
        for (hx, hy), nm in zip(self._handles(a, b), names):
            if abs(e.x - hx) <= self.HANDLE + 2 and abs(e.y - hy) <= self.HANDLE + 2:
                return nm
        if min(a[0], b[0]) < e.x < max(a[0], b[0]) and min(a[1], b[1]) < e.y < max(a[1], b[1]):
            return "move"
        return None

    def _hover(self, e):
        h = self._hit(e)
        cur = {"nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw",
               "n": "size_ns", "s": "size_ns", "w": "size_we", "e": "size_we",
               "move": "fleur"}.get(h, "crosshair")
        self.canvas.configure(cursor=cur)

    def _press(self, e):
        self._mode = self._hit(e) or "pan"
        self._last = (e.x, e.y)

    def _press_pan(self, e):
        self._mode = "pan"
        self._last = (e.x, e.y)

    def _drag_pan(self, e):
        self._mode = "pan"
        self._drag(e)

    def _drag(self, e):
        if self.img is None or self._mode is None:
            return
        dx = (e.x - self._last[0]) / self.scale
        dy = (e.y - self._last[1]) / self.scale
        self._last = (e.x, e.y)
        if self._mode == "pan":
            self.ox -= dx
            self.oy -= dy
        elif self._mode == "move":
            w = self.crop[2] - self.crop[0]
            h = self.crop[3] - self.crop[1]
            self.crop[0] = min(max(0, self.crop[0] + dx), self.W - w)
            self.crop[1] = min(max(0, self.crop[1] + dy), self.H - h)
            self.crop[2] = self.crop[0] + w
            self.crop[3] = self.crop[1] + h
        else:
            m = self._mode
            if "w" in m:
                self.crop[0] = min(max(0, self.crop[0] + dx), self.crop[2] - 4)
            if "e" in m:
                self.crop[2] = max(min(self.W, self.crop[2] + dx), self.crop[0] + 4)
            if "n" in m:
                self.crop[1] = min(max(0, self.crop[1] + dy), self.crop[3] - 4)
            if "s" in m:
                self.crop[3] = max(min(self.H, self.crop[3] + dy), self.crop[1] + 4)
        self.render()

    def _release(self, e):
        if self._mode and self._mode != "pan":
            self._notify()
        self._mode = None


# ----------------------------------------------------------------------- app
class App:
    def __init__(self, root):
        self.root = root
        root.title("FlashPad Capture")
        self.q = queue.Queue()
        self.busy = False
        self.offset = None
        self.dmap = None
        self.last_img = None       # raw uint16 of the last capture
        self.final = None          # processed uint16 (offset + defects)
        self.last_base = None
        self.last_tag = None
        self.offset_name = tk.StringVar(value="(none - images saved uncorrected)")
        self._build()
        self._autoload_dark()
        self.root.after(80, self._drain)

    # ---------------------------------------------------------------- layout
    def _build(self):
        main = ttk.Frame(self.root, padding=6)
        main.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # left column: tabbed settings (keeps the panel short) + always-visible actions
        left = ttk.Frame(main)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        nb = ttk.Notebook(left, width=252)
        nb.grid(row=0, column=0, sticky="new")
        tab_cap = ttk.Frame(nb, padding=4)
        tab_cor = ttk.Frame(nb, padding=4)
        tab_dsp = ttk.Frame(nb, padding=4)
        for t, n in ((tab_cap, "Capture"), (tab_cor, "Correct"), (tab_dsp, "Display")):
            t.columnconfigure(0, weight=1)
            nb.add(t, text=n)
        r = 1

        box = ttk.LabelFrame(tab_cap, text="X-ray source", padding=5)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        lab = ttk.Label(box, text="Exposure (ms)"); lab.grid(row=0, column=0, sticky="w")
        Tip(lab, "How long the tube fires (Arduino pin 13 HIGH), sent as C<ms>.\n"
                 "Main brightness / dose control.")
        self.expose_ms = tk.StringVar(value="250")
        ttk.Entry(box, textvariable=self.expose_ms, width=10).grid(row=0, column=1, padx=3, sticky="w")
        ttk.Label(box, text="Serial port").grid(row=1, column=0, sticky="w")
        self.port = tk.StringVar(value="COM22")
        ports = ["COM22"]
        if list_ports:
            try:
                found = [p.device for p in list_ports.comports()]
                if found:
                    ports = found
                    self.port.set("COM22" if "COM22" in found else found[0])
            except Exception:
                pass
        ttk.Combobox(box, textvariable=self.port, values=ports, width=8).grid(
            row=1, column=1, padx=3, sticky="w")
        ttk.Label(box, text="Terminator").grid(row=2, column=0, sticky="w")
        self.term = tk.StringVar(value="crlf")
        ttk.Combobox(box, textvariable=self.term, values=["cr", "lf", "crlf"], width=8,
                     state="readonly").grid(row=2, column=1, padx=3, sticky="w")
        l2 = ttk.Label(box, text="DTR"); l2.grid(row=3, column=0, sticky="w")
        Tip(l2, "CH340 clones tie DTR to RESET via a cap, so opening the port can reboot\n"
                "the board. assert = normal; deassert = never touch the lines;\n"
                "pulse = deliberate reset then wait.")
        self.dtr_mode = tk.StringVar(value="assert")
        ttk.Combobox(box, textvariable=self.dtr_mode, width=8, state="readonly",
                     values=["assert", "deassert", "pulse"]).grid(row=3, column=1, padx=3, sticky="w")
        ttk.Label(box, text="RTS").grid(row=4, column=0, sticky="w")
        self.rts_mode = tk.StringVar(value="assert")
        ttk.Combobox(box, textvariable=self.rts_mode, width=8, state="readonly",
                     values=["assert", "deassert"]).grid(row=4, column=1, padx=3, sticky="w")
        ttk.Label(box, text="Boot wait (s)").grid(row=5, column=0, sticky="w")
        self.open_delay = tk.StringVar(value="2.5")
        ttk.Entry(box, textvariable=self.open_delay, width=10).grid(row=5, column=1, padx=3, sticky="w")
        b = ttk.Button(box, text="Test link (CW0 - fires nothing)", command=self.test_link)
        b.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        Tip(b, "CW0 moves the stepper zero steps and replies OK.\n"
               "The only way to prove the board is receiving.")

        box2 = ttk.LabelFrame(tab_cap, text="Detector", padding=5)
        box2.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(box2, text="Detector IP").grid(row=0, column=0, sticky="w")
        self.det_ip = tk.StringVar(value=fp.DETECTOR_IP)
        ttk.Entry(box2, textvariable=self.det_ip, width=14).grid(row=0, column=1, padx=3, sticky="w")
        l3 = ttk.Label(box2, text="Window (ticks)"); l3.grid(row=1, column=0, sticky="w")
        Tip(l3, "MaxExpose Time: how long the panel keeps its window open.\n"
                "The X-ray must fire INSIDE it (the panel is not AED).\n"
                "Longer = easier timing, more dark current. GE uses 400,000 (~15 ms).")
        self.window = tk.StringVar(value="250000000")
        ttk.Entry(box2, textvariable=self.window, width=14).grid(row=1, column=1, padx=3, sticky="w")
        self.window_lbl = ttk.Label(box2, text="", foreground="#666")
        self.window_lbl.grid(row=2, column=0, columnspan=2, sticky="w")
        self.window.trace_add("write", lambda *a: self._upd_window())
        self._upd_window()
        l4 = ttk.Label(box2, text="Scrubs"); l4.grid(row=3, column=0, sticky="w")
        Tip(l4, "Clear cycles BEFORE the window opens, to flush residual charge.\n"
                "More = less ghosting from the previous shot, but delays the window.\n"
                "GE Script0/1 use 0; Script2 uses 15.")
        self.scrubs = tk.StringVar(value="0")
        ttk.Entry(box2, textvariable=self.scrubs, width=14).grid(row=3, column=1, padx=3, sticky="w")
        l5 = ttk.Label(box2, text="TypeMode"); l5.grid(row=4, column=0, sticky="w")
        Tip(l5, "0 = standard acquisition (GE Script0, expects an exposure)\n"
                "1 = dark/offset acquisition (GE Script1, no X-ray)\n"
                "Leave at 0 for real shots.")
        self.type_mode = tk.StringVar(value="0")
        ttk.Entry(box2, textvariable=self.type_mode, width=14).grid(row=4, column=1, padx=3, sticky="w")

        box3 = ttk.LabelFrame(tab_cor, text="Offset / dark correction", padding=5)
        box3.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        box3.columnconfigure(0, weight=1)
        ttk.Label(box3, textvariable=self.offset_name, wraplength=210,
                  foreground="#444").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(box3, text="Load offset...", command=self.load_offset).grid(
            row=1, column=0, sticky="ew", pady=(3, 0))
        ttk.Button(box3, text="Clear", command=self.clear_offset).grid(
            row=1, column=1, sticky="ew", pady=(3, 0))

        box5 = ttk.LabelFrame(tab_cor, text="Defect correction", padding=5)
        box5.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        self.fix_on = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(box5, text="repair dead rows / columns",
                             variable=self.fix_on, command=self.reprocess)
        cb.grid(row=0, column=0, columnspan=2, sticky="w")
        Tip(cb, "Removes the dark lines by interpolating across defective rows and\n"
                "columns, detected on the offset-corrected image (these are gain\n"
                "defects, so a dark frame alone cannot see them) plus the dark.")
        l6 = ttk.Label(box5, text="Sensitivity"); l6.grid(row=1, column=0, sticky="w")
        Tip(l6, "Robust sigmas a row/col median must deviate from its neighbours.\n"
                "LOWER = more aggressive. Default 25: measured dead lines score\n"
                "150-320 here while normal structure stays under ~21.")
        self.fix_thresh = tk.StringVar(value="25.0")
        e6 = ttk.Entry(box5, textvariable=self.fix_thresh, width=8)
        e6.grid(row=1, column=1, padx=3, sticky="w")
        e6.bind("<Return>", lambda e: self.reprocess())
        ttk.Button(box5, text="Re-apply", command=self.reprocess).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        self.defect_lbl = ttk.Label(box5, text="", foreground="#666", wraplength=210)
        self.defect_lbl.grid(row=3, column=0, columnspan=2, sticky="w")

        box6 = ttk.LabelFrame(tab_dsp, text="Display (view + saved PNG only)", padding=5)
        box6.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.clahe_on = tk.BooleanVar(value=False)
        cbx = ttk.Checkbutton(box6, text="CLAHE contrast", variable=self.clahe_on,
                              command=self.redisplay)
        cbx.grid(row=0, column=0, columnspan=2, sticky="w")
        Tip(cbx, "Contrast Limited Adaptive Histogram Equalisation: equalises contrast in\n"
                 "local tiles, so dense and thin areas stay readable at once.")
        ttk.Label(box6, text="Clip").grid(row=1, column=0, sticky="w")
        self.clahe_clip = tk.DoubleVar(value=2.0)
        s1 = ttk.Scale(box6, from_=0.5, to=12.0, variable=self.clahe_clip,
                       command=lambda v: self.redisplay())
        s1.grid(row=1, column=1, sticky="ew", padx=3)
        ttk.Label(box6, text="Tiles").grid(row=2, column=0, sticky="w")
        self.clahe_tiles = tk.IntVar(value=8)
        s2 = ttk.Scale(box6, from_=2, to=32, variable=self.clahe_tiles,
                       command=lambda v: self.redisplay())
        s2.grid(row=2, column=1, sticky="ew", padx=3)
        ttk.Label(box6, text="Palette").grid(row=3, column=0, sticky="w")
        self.palette = tk.StringVar(value="Grayscale")
        ttk.Combobox(box6, textvariable=self.palette, values=list(PALETTES.keys()),
                     width=11, state="readonly").grid(row=3, column=1, padx=3, sticky="w")
        self.palette.trace_add("write", lambda *a: self.redisplay())
        self.invert = tk.BooleanVar(value=False)
        ttk.Checkbutton(box6, text="Invert (film look)", variable=self.invert,
                        command=self.redisplay).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(box6, text="Low %").grid(row=5, column=0, sticky="w")
        self.lo_pct = tk.DoubleVar(value=1.0)
        ttk.Scale(box6, from_=0.0, to=20.0, variable=self.lo_pct,
                  command=lambda v: self.redisplay()).grid(row=5, column=1, sticky="ew", padx=3)
        ttk.Label(box6, text="High %").grid(row=6, column=0, sticky="w")
        self.hi_pct = tk.DoubleVar(value=99.5)
        ttk.Scale(box6, from_=80.0, to=100.0, variable=self.hi_pct,
                  command=lambda v: self.redisplay()).grid(row=6, column=1, sticky="ew", padx=3)
        box6.columnconfigure(1, weight=1)

        box4 = ttk.LabelFrame(tab_dsp, text="Output", padding=5)
        box4.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        box4.columnconfigure(0, weight=1)
        self.outdir = tk.StringVar(value=os.path.join(HERE, "captures"))
        ttk.Entry(box4, textvariable=self.outdir, width=22).grid(row=0, column=0, sticky="ew")
        ttk.Button(box4, text="...", width=3, command=self.pick_dir).grid(row=0, column=1)
        self.save_tif = tk.BooleanVar(value=True)
        ttk.Checkbutton(box4, text="auto-save 16-bit TIFF",
                        variable=self.save_tif).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Button(box4, text="Open captures folder", command=self.open_outdir).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(3, 0))

        self.btn_cap = ttk.Button(left, text="CAPTURE  (fires X-ray)", command=self.do_capture)
        self.btn_cap.grid(row=r, column=0, sticky="ew", ipady=5); r += 1
        self.btn_dark = ttk.Button(left, text="Capture DARK (no X-ray)", command=self.do_dark)
        self.btn_dark.grid(row=r, column=0, sticky="ew", pady=(3, 0)); r += 1
        ttk.Button(left, text="?  Explain every setting", command=self.show_help).grid(
            row=r, column=0, sticky="ew", pady=(3, 0)); r += 1

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(left, textvariable=self.status, wraplength=225,
                  foreground="#063").grid(row=r, column=0, sticky="w", pady=(6, 0)); r += 1
        self.prog = ttk.Progressbar(left, mode="determinate", maximum=NBLOCKS)
        self.prog.grid(row=r, column=0, sticky="ew", pady=(3, 0)); r += 1
        self.prog_lbl = ttk.Label(left, text="", foreground="#666", font=("Consolas", 8))
        self.prog_lbl.grid(row=r, column=0, sticky="w")

        # ---- right: toolbar + view + stats + log
        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=3)
        right.rowconfigure(3, weight=1)

        tb = ttk.Frame(right)
        tb.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        ttk.Button(tb, text="Fit", width=5, command=lambda: self.view.fit()).pack(side="left")
        ttk.Button(tb, text="1:1", width=5, command=lambda: self.view.one_to_one()).pack(side="left", padx=2)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tb, text="Crop: reset", command=lambda: self.view.reset_crop()).pack(side="left")
        ttk.Button(tb, text="Crop: full frame", command=lambda: self.view.crop_full()).pack(side="left", padx=2)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tb, text="Save PNG (crop)", command=self.save_png_crop).pack(side="left")
        ttk.Button(tb, text="Save TIFF (crop)", command=self.save_tiff_crop).pack(side="left", padx=2)
        self.crop_lbl = ttk.Label(tb, text="", foreground="#a33", font=("Consolas", 8))
        self.crop_lbl.pack(side="left", padx=8)

        self.view = ImageView(right, on_crop=self._crop_info)
        self.view.canvas.grid(row=1, column=0, sticky="nsew")
        self.stats_lbl = ttk.Label(right, text="no image yet", font=("Consolas", 9))
        self.stats_lbl.grid(row=2, column=0, sticky="w", pady=3)
        logf = ttk.LabelFrame(right, text="Log", padding=3)
        logf.grid(row=3, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, height=8, wrap="none", font=("Consolas", 8))
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)

    def _crop_info(self, w, h):
        self.crop_lbl.configure(text="crop %d x %d px" % (w, h))

    def _upd_window(self):
        try:
            t = int(self.window.get())
            self.window_lbl.configure(text="~ %.2f s open window" % (t / TICK_HZ))
        except ValueError:
            self.window_lbl.configure(text="(invalid)")

    def show_help(self):
        w = tk.Toplevel(self.root)
        w.title("What every setting does")
        w.geometry("800x680")
        t = tk.Text(w, wrap="word", font=("Consolas", 9), padx=10, pady=10)
        t.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(w, command=t.yview)
        sb.pack(side="right", fill="y")
        t.configure(yscrollcommand=sb.set)
        t.insert("1.0", HELP_TEXT)
        t.configure(state="disabled")

    # ---------------------------------------------------------------- offsets
    def darks_dir(self):
        return os.path.join(self.outdir.get().strip() or HERE, "darks")

    def _autoload_dark(self):
        try:
            hits = glob.glob(os.path.join(self.darks_dir(), "*_u16.raw"))
            if hits:
                self._set_offset(max(hits, key=os.path.getmtime), quiet=True)
        except Exception:
            pass

    def _set_offset(self, path, quiet=False):
        d = open(path, "rb").read()
        if len(d) != DIM * DIM * 2:
            a, _ = reassemble(d)
        else:
            a = np.frombuffer(d, dtype="<u2").reshape(DIM, DIM)
        self.offset = a.copy()
        self.dmap = None
        self.q.put(("offsetname", "offset: %s  (mean %.1f)"
                    % (os.path.basename(path), float(a.mean()))))
        if not quiet:
            self._log("offset loaded: %s" % path)

    def load_offset(self):
        p = filedialog.askopenfilename(
            title="Pick a dark/offset frame (_u16.raw or a raw capture)",
            initialdir=self.darks_dir() if os.path.isdir(self.darks_dir()) else HERE,
            filetypes=[("raw", "*.raw"), ("all", "*.*")])
        if not p:
            return
        try:
            self._set_offset(p)
            self.reprocess()
        except Exception as e:
            messagebox.showerror("Offset", "Could not load:\n%s" % e)

    def clear_offset(self):
        self.offset = None
        self.dmap = None
        self.offset_name.set("(none - images saved uncorrected)")
        self.reprocess()

    # ---------------------------------------------------------------- helpers
    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or HERE)
        if d:
            self.outdir.set(d)

    def open_outdir(self):
        d = self.outdir.get().strip() or HERE
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)                       # Windows file explorer
        except AttributeError:                    # non-Windows fallback
            import subprocess
            subprocess.Popen(["xdg-open", d])
        except Exception as e:
            messagebox.showerror("Open folder", str(e))

    def _log(self, msg):
        self.q.put(("log", msg))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log.insert("end", payload.rstrip() + "\n")
                    self.log.see("end")
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "stats":
                    self.stats_lbl.configure(text=payload)
                elif kind == "defects":
                    self.defect_lbl.configure(text=payload)
                elif kind == "offsetname":
                    self.offset_name.set(payload)
                elif kind == "prog":
                    n = payload
                    self.prog.configure(value=min(n, NBLOCKS))
                    self.prog_lbl.configure(text="%d / %d blocks (%.1f%%)"
                                            % (n, NBLOCKS, 100.0 * n / NBLOCKS))
                elif kind == "progmode":
                    if payload == "busy":
                        self.prog.configure(mode="indeterminate")
                        self.prog.start(12)
                    else:
                        self.prog.stop()
                        self.prog.configure(mode="determinate", value=0)
                        self.prog_lbl.configure(text="")
                elif kind == "image":
                    self.view.set_image(payload)
                elif kind == "done":
                    self.busy = False
                    self.prog.stop()
                    self.btn_cap.configure(state="normal")
                    self.btn_dark.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    # ------------------------------------------------------- serial plumbing
    def _open_serial(self, port, dtr_mode, rts_mode, delay):
        s = serial.Serial()
        s.port = port
        s.baudrate = 9600
        s.bytesize, s.parity, s.stopbits = 8, "N", 1
        s.timeout, s.write_timeout = 1, 2
        s.dsrdtr = s.rtscts = s.xonxoff = False
        s.dtr = (dtr_mode == "assert")
        s.rts = (rts_mode == "assert")
        s.open()
        if dtr_mode == "pulse":
            s.dtr = True
            time.sleep(0.15)
            s.dtr = False
        self._log("serial %s open: dtr=%s rts=%s, waiting %.1fs"
                  % (s.name, s.dtr, s.rts, delay))
        time.sleep(delay)
        try:
            s.reset_input_buffer()
            s.reset_output_buffer()
        except Exception:
            pass
        return s

    def _probe_link(self, ser, term):
        ser.write(("CW0%s" % term).encode())
        ser.flush()
        got = b""
        t0 = time.time()
        while time.time() - t0 < 2.5:
            d = ser.read(64)
            if d:
                got += d
            if b"OK" in got:
                return True, got
        return False, got

    def test_link(self):
        if self.busy:
            return
        if serial is None:
            messagebox.showerror("Serial", "pyserial is not installed.")
            return
        self._lock()
        self.q.put(("progmode", "busy"))
        port, dtr, rts = self.port.get().strip(), self.dtr_mode.get(), self.rts_mode.get()
        try:
            delay = float(self.open_delay.get())
        except ValueError:
            delay = 2.5
        term = {"cr": "\r", "lf": "\n", "crlf": "\r\n"}[self.term.get()]

        def run():
            ser = None
            try:
                self.q.put(("status", "testing %s ..." % port))
                ser = self._open_serial(port, dtr, rts, delay)
                ok, got = self._probe_link(ser, term)
                if ok:
                    self._log("LINK OK -- board replied %r to CW0" % got)
                    self.q.put(("status", "Link OK: Arduino alive and listening."))
                else:
                    self._log("NO REPLY to CW0 (got %r). Try DTR=deassert, or "
                              "DTR=pulse with boot wait 3-4 s." % got)
                    self.q.put(("status", "No reply - not reaching the board."))
            except Exception as e:
                self._log("ERROR: %s" % e)
                self.q.put(("status", "FAILED: %s" % e))
            finally:
                if ser:
                    ser.close()
                self.q.put(("progmode", "idle"))
                self.q.put(("done", None))
        threading.Thread(target=run, daemon=True).start()

    def _lock(self):
        self.busy = True
        self.btn_cap.configure(state="disabled")
        self.btn_dark.configure(state="disabled")

    # ---------------------------------------------------------------- actions
    def do_capture(self):
        self._start(fire=True)

    def do_dark(self):
        self._start(fire=False)

    def _start(self, fire):
        if self.busy:
            return
        try:
            p = {
                "expose_ms": int(self.expose_ms.get()),
                "window": int(self.window.get()),
                "scrubs": int(self.scrubs.get()),
                "type_mode": int(self.type_mode.get()),
                "det_ip": self.det_ip.get().strip(),
                "port": self.port.get().strip(),
                "term": {"cr": "\r", "lf": "\n", "crlf": "\r\n"}[self.term.get()],
                "dtr": self.dtr_mode.get(),
                "rts": self.rts_mode.get(),
                "delay": float(self.open_delay.get()),
                "outdir": self.outdir.get().strip() or HERE,
                "fire": fire,
                "tif": bool(self.save_tif.get()),
            }
        except ValueError as e:
            messagebox.showerror("Input", "Check the numeric fields:\n%s" % e)
            return
        if fire and serial is None:
            messagebox.showerror("Serial", "pyserial is not installed.")
            return
        self._lock()
        self.log.delete("1.0", "end")
        threading.Thread(target=self._worker, args=(p,), daemon=True).start()

    # ----------------------------------------------------------------- worker
    def _worker(self, p):
        try:
            tag = "xray" if p["fire"] else "dark"
            outdir = p["outdir"] if p["fire"] else os.path.join(p["outdir"], "darks")
            os.makedirs(outdir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.join(outdir, "flashpad_%s_%s" % (tag, ts))

            ser = None
            if p["fire"]:
                self.q.put(("progmode", "busy"))
                self.q.put(("status", "opening %s ..." % p["port"]))
                ser = self._open_serial(p["port"], p["dtr"], p["rts"], p["delay"])
                ok, got = self._probe_link(ser, p["term"])
                self._log("link check: %s%s" % ("OK" if ok else "NO REPLY",
                                                " %r" % got if got else ""))

            s = fp.FlashPadSession(detector_ip=p["det_ip"], host_ip=fp.HOST_IP,
                                   timeout=5.0, verbose=True)
            # hook the session logger for live receive progress
            frames = [0]
            orig_log = s._log

            def hooked(msg):
                m = str(msg)
                if "ImgSock" in m:
                    frames[0] += 1
                    if frames[0] % 16 == 0 or frames[0] >= NBLOCKS:
                        self.q.put(("prog", frames[0]))
                elif ("[OK]" in m or "WARN" in m or "ERROR" in m
                      or "EXECUTION_COMPLETE" in m or "missed" in m):
                    self._log(m.strip())
            s._log = hooked

            img_port = fp.HOST_IMAGE_PORT
            try:
                self.q.put(("progmode", "busy"))
                self.q.put(("status", "discovering detector ..."))
                s._open_sockets()
                if not s.discover():
                    raise RuntimeError("discovery failed (check IP / 100 Mbps / MTU 5000)")
                s.send_port_setup(host_cmd_port=s.reply_port, host_img_port=img_port)
                s.request_signature()

                self.q.put(("status", "arming: phase 1 (ROE init) ..."))
                if not s.download_script(fp.build_script_7_roe_init(), "Script7"):
                    raise RuntimeError("Script7 download failed")
                if not s.execute_script():
                    raise RuntimeError("Script7 execute failed")
                if not s.wait_for_execution_complete(timeout_s=60.0):
                    raise RuntimeError("no EXECUTION_COMPLETE for Script7")

                self.q.put(("status", "arming: phase 2 (acquisition) ..."))
                cmds = [
                    fp.pack_acquisition(type_mode=p["type_mode"], image_id=1,
                                        no_scrubs=p["scrubs"], scrub_duration=50000,
                                        max_expose_time=p["window"],
                                        tail_time=250000, transfer_mode=0),
                    fp.pack_delay(10000),
                ]
                if not s.download_script(fp.build_generic_script(0, 0, 0, cmds), "Script0"):
                    raise RuntimeError("Script0 download failed")
                if not s._open_image_socket(img_port):
                    self._log("WARNING: image socket did not open")
                if not s.execute_script():
                    raise RuntimeError("Script0 execute failed")

                if p["fire"]:
                    msg = ("C%d%s" % (p["expose_ms"], p["term"])).encode()
                    ser.write(msg)
                    ser.flush()
                    self._log("sent %r (C commands are not acked by the sketch)" % msg)
                self.q.put(("progmode", "idle"))
                self.q.put(("status", "receiving image ..."))
                raw = s.receive_image(output_path=base + ".raw", timeout_s=90.0,
                                      script_id=0, image_port=img_port,
                                      wait_exec_complete=True)
                if not raw:
                    raise RuntimeError("no image data received")
                self.q.put(("prog", NBLOCKS))
            finally:
                s._log = orig_log
                try:
                    s._close_sockets()
                except Exception:
                    pass
                if ser:
                    ser.close()

            self.q.put(("status", "reassembling ..."))
            img, info = reassemble(raw)
            self._log("blocks %d/%d  missing %d  ids %s"
                      % (info["blocks"], NBLOCKS, len(info["missing"]),
                         [hex(i) for i in info["ids"]]))
            img.tofile(base + "_u16.raw")

            if not p["fire"]:
                self._prune_darks(outdir, keep_base=os.path.basename(base))
                self._set_offset(base + "_u16.raw")
                self._log("dark auto-loaded as the active offset correction")

            self.last_img = img
            self.last_base = base
            self.last_tag = tag
            self._process(img, base, tag, p["tif"])
            self.q.put(("status", "Done: %s" % os.path.basename(base)))
        except Exception as e:
            self._log("ERROR: %s" % e)
            self._log(traceback.format_exc())
            self.q.put(("status", "FAILED: %s" % e))
        finally:
            self.q.put(("done", None))

    def _prune_darks(self, d, keep_base):
        removed = 0
        for f in glob.glob(os.path.join(d, "flashpad_dark_*")):
            if os.path.basename(f).startswith(keep_base):
                continue
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
        if removed:
            self._log("removed %d old dark file(s)" % removed)

    # ------------------------------------------------- processing / display
    def _process(self, img, base, tag, want_tif):
        """Offset subtraction + defect repair -> self.final, then display."""
        line = "%s raw mean=%.1f min=%d max=%d" % (tag.upper(), img.mean(),
                                                   img.min(), img.max())
        view = img
        if self.offset is not None and tag != "dark" and self.offset.shape == img.shape:
            view = np.clip(img.astype(np.int32) - self.offset.astype(np.int32),
                           0, 65535).astype(np.uint16)
            line += " | offset-corr mean=%.1f max=%d" % (view.mean(), view.max())

        if self.fix_on.get():
            try:
                th = float(self.fix_thresh.get())
            except ValueError:
                th = 25.0
            # detect on the offset-corrected signal (gain defects) unioned with the dark
            self.dmap = build_defect_map(view, self.offset, th)
            nr = int(self.dmap["rows"].sum())
            nc = int(self.dmap["cols"].sum())
            rows_list = list(np.where(self.dmap["rows"])[0][:6])
            cols_list = list(np.where(self.dmap["cols"])[0][:6])
            view = apply_defect_map(view, self.dmap)
            msg = "repaired %d rows %s, %d cols %s" % (nr, rows_list, nc, cols_list)
            self.q.put(("defects", msg))
            line += " | defects fixed"
        else:
            self.q.put(("defects", "defect repair off"))

        self.final = view
        if want_tif:
            Image.fromarray(view, mode="I;16").save(base + ".tif")
        view.tofile(base + "_final_u16.raw")
        self.q.put(("stats", line))
        self._redisplay_and_autosave(base)

    def _redisplay_and_autosave(self, base):
        pil = self._make_rgb()
        if pil is not None:
            self.q.put(("image", pil))
            if base:
                pil.save(base + ".png")
                self._log("saved %s.{raw,_u16.raw,_final_u16.raw,png,tif}"
                          % os.path.basename(base))

    def _make_rgb(self):
        if self.final is None:
            return None
        return render_rgb(self.final,
                          max(0.0, self.lo_pct.get()) / 100.0,
                          min(100.0, self.hi_pct.get()) / 100.0,
                          bool(self.clahe_on.get()),
                          float(self.clahe_clip.get()),
                          int(self.clahe_tiles.get()),
                          self.palette.get(),
                          bool(self.invert.get()))

    def redisplay(self):
        """Display-only update (CLAHE / palette / invert / stretch). Cheap."""
        if self.final is None:
            return
        pil = self._make_rgb()
        if pil is not None:
            self.view.set_image(pil, keep_view=True)

    def reprocess(self):
        """Re-run offset + defect correction on the captured frame (no new exposure)."""
        if self.last_img is None or self.busy:
            return
        def run():
            try:
                self.q.put(("status", "re-processing ..."))
                self._process(self.last_img, self.last_base, self.last_tag,
                              bool(self.save_tif.get()))
                self.q.put(("status", "Re-processed."))
            except Exception as e:
                self._log("ERROR during re-process: %s" % e)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------- crop saves
    def save_png_crop(self):
        if self.final is None:
            messagebox.showinfo("Save", "No image yet.")
            return
        x0, y0, x1, y1 = self.view.get_crop()
        pil = self._make_rgb()
        if pil is None:
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG", "*.png")],
            initialdir=self.outdir.get(),
            initialfile=os.path.basename(self.last_base or "flashpad") + "_crop.png")
        if not p:
            return
        pil.crop((x0, y0, x1, y1)).save(p)
        self._log("saved crop PNG %dx%d -> %s" % (x1 - x0, y1 - y0, p))
        self.q.put(("status", "Saved crop PNG (%dx%d)" % (x1 - x0, y1 - y0)))

    def save_tiff_crop(self):
        if self.final is None:
            messagebox.showinfo("Save", "No image yet.")
            return
        x0, y0, x1, y1 = self.view.get_crop()
        p = filedialog.asksaveasfilename(
            defaultextension=".tif", filetypes=[("TIFF", "*.tif")],
            initialdir=self.outdir.get(),
            initialfile=os.path.basename(self.last_base or "flashpad") + "_crop.tif")
        if not p:
            return
        sub = self.final[y0:y1, x0:x1]
        Image.fromarray(sub, mode="I;16").save(p)
        sub.tofile(os.path.splitext(p)[0] + "_u16.raw")
        self._log("saved crop TIFF %dx%d (16-bit values) -> %s" % (x1 - x0, y1 - y0, p))
        self.q.put(("status", "Saved crop TIFF (%dx%d)" % (x1 - x0, y1 - y0)))


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.1)
    except Exception:
        pass
    App(root)
    # size to the screen so everything fits without the user resizing
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(1180, sw - 120), min(860, sh - 140)
    root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(0, (sh - h) // 2 - 20)))
    root.minsize(900, 600)
    root.mainloop()


if __name__ == "__main__":
    main()
