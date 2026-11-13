/* proc-core.js - pure image-processing functions for the FlashPad web UI.
 *
 * UMD: loaded with importScripts() in proc-worker.js, with <script> in the page (geometry / ROI helpers), and with
 * require()/import in the Node tests. No DOM, no network, typed arrays only. Frame = Uint16 row-major, row 0 on top.
 *
 * Ported 1:1 from flashpad_capture_gui.py (the real code, not its stale help text):
 *   dark subtraction (_process), _bad_lines / build_defect_map / _interp_lines / apply_defect_map, to_u8,
 *   cv2.createCLAHE(...).apply (bit-exact, float32 arithmetic emulated with Math.fround), cv2.applyColorMap tables.
 * Added on top: single-pixel repair, denoise (gauss / median / bilateral / NLM), destripe, background flatten,
 * unsharp, multi-scale local contrast, geometry, 12-bit CLAHE (non-cv2), window curves, and the Pipeline class that
 * chains everything with per-stage caching.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ProcCore = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var fround = Math.fround;
  var BORDER = 12;
  var DIM = 2048;
  var LE = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;

  function reflect(i, n) { // BORDER_REFLECT_101
    if (n === 1) return 0;
    while (i < 0 || i >= n) i = i < 0 ? -i : 2 * (n - 1) - i;
    return i;
  }
  function clampi(i, n) { return i < 0 ? 0 : i >= n ? n - 1 : i; }
  function roundHalfEven(x) { // float -> int like cvRound (lrint)
    var f = Math.floor(x), d = x - f;
    return d > 0.5 ? f + 1 : d < 0.5 ? f : (f & 1) ? f + 1 : f;
  }
  var NOCTL = { cancelled: false, tick: function () { return Promise.resolve(); }, progress: function () {} };

  /* ------------------------------------------------------------------ dark */
  /** _process: int32 subtract, clamp 0..65535, no pedestal added back. */
  function subtractDark(raw, dark, out) {
    var n = raw.length;
    out = out || new Uint16Array(n);
    for (var i = 0; i < n; i++) { var v = raw[i] - dark[i]; out[i] = v < 0 ? 0 : v; }
    return out;
  }

  /* ------------------------------------------------------- defect lines */
  function medianSorted(s, n) { return n & 1 ? s[n >> 1] : (s[(n >> 1) - 1] + s[n >> 1]) / 2; }
  function medianOf(arr) { // np.median, arr is copied
    var c = Float64Array.from(arr); c.sort();
    return medianSorted(c, c.length);
  }

  /** per-line median and max. axis 0 = rows (n = H), axis 1 = columns (n = W). */
  function lineStats(a, W, H, axis) {
    var n = axis === 0 ? H : W, len = axis === 0 ? W : H, i, j;
    var med = new Float64Array(n), mx = new Float64Array(n);
    var tmp = new a.constructor(len);
    for (i = 0; i < n; i++) {
      var m = 0;
      if (axis === 0) { var o = i * W; for (j = 0; j < len; j++) { var v = a[o + j]; tmp[j] = v; if (v > m) m = v; } }
      else for (j = 0; j < len; j++) { var v2 = a[j * W + i]; tmp[j] = v2; if (v2 > m) m = v2; }
      tmp.sort();
      med[i] = medianSorted(tmp, len); mx[i] = m;
    }
    return { med: med, mx: mx };
  }

  /** _bad_lines(a, axis, thresh, win=16, border=12) -> Uint8Array(n) of 0/1 */
  function badLines(a, W, H, axis, thresh, win, border) {
    if (win === undefined) win = 16;
    if (border === undefined) border = BORDER;
    var st = lineStats(a, W, H, axis), med = st.med, n = med.length, i;
    var base = new Float64Array(n), dev = new Float64Array(n), buf = new Float64Array(2 * win + 1);
    for (i = 0; i < n; i++) {
      var lo = Math.max(0, i - win), hi = Math.min(n, i + win + 1), c = hi - lo;
      for (var k = 0; k < c; k++) buf[k] = med[lo + k];
      var s = buf.subarray(0, c); s.sort();
      base[i] = medianSorted(s, c);
      dev[i] = med[i] - base[i];
    }
    var md = medianOf(dev), ad = new Float64Array(n);
    for (i = 0; i < n; i++) ad[i] = Math.abs(dev[i] - md);
    var mad = medianOf(ad), sigma = mad > 0 ? 1.4826 * mad : 1.0;
    var bad = new Uint8Array(n);
    for (i = 0; i < n; i++) bad[i] = (Math.abs(dev[i]) / sigma > thresh || st.mx[i] === 0) ? 1 : 0;
    if (border) { for (i = 0; i < border && i < n; i++) bad[i] = 0; for (i = Math.max(0, n - border); i < n; i++) bad[i] = 0; }
    return bad;
  }

  /** build_defect_map: lines of the SIGNAL frame OR'd with lines found in the DARK frame. */
  function buildDefectMap(signal, dark, W, H, thresh) {
    var rows = badLines(signal, W, H, 0, thresh), cols = badLines(signal, W, H, 1, thresh), i;
    if (dark && dark.length === signal.length) {
      var dr = badLines(dark, W, H, 0, thresh), dc = badLines(dark, W, H, 1, thresh);
      for (i = 0; i < rows.length; i++) rows[i] |= dr[i];
      for (i = 0; i < cols.length; i++) cols[i] |= dc[i];
    }
    return { rows: rows, cols: cols };
  }

  /** _interp_lines on a Float32Array (in place). Float32 arithmetic emulated exactly (numpy float32 ops). */
  function interpLines(f, W, H, bad, axis) {
    var n = bad.length, i, good = [], any = false;
    for (i = 0; i < n; i++) { if (bad[i]) any = true; else good.push(i); }
    if (!any || good.length < 2) return;
    var g = good.length, p = 0; // p = number of good lines below i
    for (i = 0; i < n; i++) {
      if (!bad[i]) { p++; continue; }
      var lo = good[Math.max(0, Math.min(g - 1, p - 1))], hi = good[Math.max(0, Math.min(g - 1, p))];
      var span = Math.max(hi - lo, 1);
      var w = hi !== lo ? fround((i - lo) / span) : fround(0), om = fround(1 - w), x;
      if (axis === 0) {
        var oi = i * W, ol = lo * W, oh = hi * W;
        for (x = 0; x < W; x++) f[oi + x] = fround(fround(f[ol + x] * om) + fround(f[oh + x] * w));
      } else {
        for (var y = 0; y < H; y++) { var r = y * W; f[r + i] = fround(fround(f[r + lo] * om) + fround(f[r + hi] * w)); }
      }
    }
  }

  /** apply_defect_map: rows first, then columns, in float32; clip 0..65535 and truncate (no rounding). */
  function applyDefectMap(img, dmap, W, H) {
    var f = Float32Array.from(img), i, n = f.length;
    interpLines(f, W, H, dmap.rows, 0);
    interpLines(f, W, H, dmap.cols, 1);
    var out = new Uint16Array(n);
    for (i = 0; i < n; i++) { var v = f[i]; out[i] = v < 0 ? 0 : v > 65535 ? 65535 : v; } // typed-array store truncates
    return out;
  }

  /* ---------------------------------------------------- single-pixel repair */
  function med8sorted(a, k) { // a[0..k) sorted in place by insertion; returns 2*median
    for (var i = 1; i < k; i++) { var v = a[i], j = i - 1; while (j >= 0 && a[j] > v) { a[j + 1] = a[j]; j--; } a[j + 1] = v; }
    return k & 1 ? 2 * a[k >> 1] : a[(k >> 1) - 1] + a[k >> 1];
  }

  /**
   * Hot/dead single-pixel repair (runs AFTER line repair). Detection: d = v - median(8 neighbours); sigma = 1.4826 *
   * median(|d|) over the frame (1.0 if 0); flagged if |d| > thr*sigma. Repair: floor(median of the UNFLAGGED 3x3
   * neighbours + 0.5) (5x5 if none, else the all-neighbour median). manual = array of pixel indices that are always
   * flagged. opts.auto=false repairs only the manual pixels. Returns {img, count}.
   */
  function repairPixels(img, W, H, opts) {
    var thr = opts.thr === undefined ? 12 : opts.thr, N = W * H, x, y, i, k;
    var flag = new Uint8Array(N), nflag = 0, nb = new Int32Array(25);
    if (opts.auto) {
      var m2 = new Int32Array(N), hist = new Int32Array(131072);
      for (y = 0; y < H; y++) for (x = 0; x < W; x++) {
        k = 0;
        for (var dy = -1; dy <= 1; dy++) {
          var yy = y + dy; if (yy < 0 || yy >= H) continue;
          for (var dx = -1; dx <= 1; dx++) {
            var xx = x + dx; if (xx < 0 || xx >= W || (dx === 0 && dy === 0)) continue;
            nb[k++] = img[yy * W + xx];
          }
        }
        i = y * W + x;
        var mm = med8sorted(nb, k), d2 = 2 * img[i] - mm;
        m2[i] = mm; hist[d2 < 0 ? -d2 : d2]++;
      }
      // median of |d| (d2 units are half-ADU) from the histogram: ranks N/2-1 and N/2 (N even) or N>>1
      var ra = (N & 1) ? N >> 1 : (N >> 1) - 1, rb = N >> 1, va = -1, vb = -1, cum = 0;
      for (i = 0; i < hist.length && vb < 0; i++) { cum += hist[i]; if (va < 0 && cum > ra) va = i; if (cum > rb) vb = i; }
      var mad = (va + vb) / 4, sigma = mad > 0 ? 1.4826 * mad : 1.0; // /2 (mean of two) /2 (d2 -> d)
      for (i = 0; i < N; i++) { var dd = 2 * img[i] - m2[i]; if ((dd < 0 ? -dd : dd) / 2 > thr * sigma) { flag[i] = 1; nflag++; } }
      m2 = null;
    }
    var man = opts.manual || [];
    for (i = 0; i < man.length; i++) if (man[i] >= 0 && man[i] < N && !flag[man[i]]) { flag[man[i]] = 1; nflag++; }
    var out = Uint16Array.from(img);
    if (!nflag) return { img: out, count: 0 };
    for (i = 0; i < N; i++) {
      if (!flag[i]) continue;
      y = (i / W) | 0; x = i - y * W;
      var res = -1;
      for (var rad = 1; rad <= 2 && res < 0; rad++) {
        k = 0;
        for (var ey = -rad; ey <= rad; ey++) {
          var y2 = y + ey; if (y2 < 0 || y2 >= H) continue;
          for (var ex = -rad; ex <= rad; ex++) {
            var x2 = x + ex; if (x2 < 0 || x2 >= W || (ex === 0 && ey === 0) || flag[y2 * W + x2]) continue;
            nb[k++] = img[y2 * W + x2];
          }
        }
        if (k) res = (med8sorted(nb, k) + 1) >> 1;
      }
      if (res >= 0) out[i] = res;
    }
    return { img: out, count: nflag };
  }

  /* ------------------------------------------------------------ display */
  function histogram16(img, out) {
    var h = out || new Uint32Array(65536), n = img.length;
    if (out) h.fill(0);
    for (var i = 0; i < n; i++) h[img[i]]++;
    return h;
  }
  function valueAtRank(hist, k) { var c = 0; for (var v = 0; v < hist.length; v++) { c += hist[v]; if (c > k) return v; } return hist.length - 1; }

  /**
   * to_u8's percentile step. Default sampling = img16[4:H-8:3, ::3] (463,757 samples for 2048x2048).
   * opts.region = [x0,y0,x1,y1] samples that box (step 3); opts.step overrides the stride (preview uses 1).
   * lo = s[int(n*loP)], hi = s[min(n-1,int(n*hiP))] with loP/hiP as fractions.
   */
  function percentileRange(img, W, H, loP, hiP, opts) {
    opts = opts || {};
    var step = opts.step || 3, hist = new Uint32Array(65536), n = 0, x, y, y0 = 4, y1 = H - 8, x0 = 0, x1 = W;
    if (opts.region) { x0 = opts.region[0]; y0 = opts.region[1]; x1 = opts.region[2]; y1 = opts.region[3]; }
    for (y = y0; y < y1; y += step) { var o = y * W; for (x = x0; x < x1; x += step) { hist[img[o + x]]++; n++; } }
    if (!n) return { lo: 0, hi: 1, n: 0 };
    return { lo: valueAtRank(hist, Math.floor(n * loP)), hi: valueAtRank(hist, Math.min(n - 1, Math.floor(n * hiP))), n: n };
  }

  function curveFn(curve, p) {
    if (curve === 'gamma') { var ig = 1 / Math.max(0.05, p); return function (t) { return Math.pow(t, ig); }; }
    if (curve === 'log') { var c = Math.max(0.01, p), lc = Math.log(1 + c); return function (t) { return Math.log(1 + c * t) / lc; }; }
    if (curve === 'sigmoid') {
      var a = Math.max(0.1, p), s = function (u) { return 1 / (1 + Math.exp(-a * (u - 0.5))); }, s0 = s(0), s1 = s(1);
      return function (t) { return (s(t) - s0) / (s1 - s0); };
    }
    return null;
  }

  /**
   * 65536-entry window LUT. curve 'none' reproduces to_u8 exactly (float32 arithmetic, truncation):
   *   u8 = clip((x - lo) * (255 / max(1, hi - lo)), 0, 255) trunc.
   * With a curve: t = clip((x-lo)/rng,0,1); y = curve(t); u8 = trunc(255*y). levels=4095 -> 12-bit output (rounded).
   */
  function windowLut(lo, hi, curve, cparam, levels) {
    levels = levels || 255;
    var lut = levels > 255 ? new Uint16Array(65536) : new Uint8Array(65536), v;
    var rng = Math.max(1.0, hi - lo), fn = curveFn(curve, cparam);
    if (!fn && levels === 255) {
      var k = fround(255 / rng), flo = fround(lo);
      for (v = 0; v < 65536; v++) { var x = fround(fround(v - flo) * k); lut[v] = x < 0 ? 0 : x > 255 ? 255 : x; }
    } else {
      for (v = 0; v < 65536; v++) {
        var t = (v - lo) / rng; t = t < 0 ? 0 : t > 1 ? 1 : t;
        var y = fn ? fn(t) : t; y = y < 0 ? 0 : y > 1 ? 1 : y;
        lut[v] = levels > 255 ? Math.floor(y * levels + 0.5) : Math.floor(y * levels);
      }
    }
    return lut;
  }
  function applyLut(img, lut, out) {
    var n = img.length; out = out || new lut.constructor(n);
    for (var i = 0; i < n; i++) out[i] = lut[img[i]];
    return out;
  }
  /** to_u8(img16, lo_pct, hi_pct) with the fractions loP/hiP; exact. Returns {u8, lo, hi}. */
  function toU8(img, W, H, loP, hiP) {
    var r = percentileRange(img, W, H, loP, hiP);
    return { u8: applyLut(img, windowLut(r.lo, r.hi, 'none', 0, 255)), lo: r.lo, hi: r.hi };
  }

  /* --------------------------------------------------------------- CLAHE */
  /**
   * CLAHE core. cv2 path: src Uint8Array, N=256, outMax=255, quant=true  -> bit-exact with
   * cv2.createCLAHE(clip,(t,t)).apply(src). 12-bit path: src Uint16Array 0..4095, N=4096, quant=false (non-cv2).
   * Mirrors OpenCV: float32 maths (Math.fround), LUT rounding half-even, bilinear tile interpolation, reflect-101
   * padding when W or H is not divisible by the tile grid.
   */
  function claheCore(src, W, H, tilesX, tilesY, clip, N, outMax, quant) {
    var WL = W, HL = H, srcL = src, x, y, i;
    if (!(W % tilesX === 0 && H % tilesY === 0)) {
      WL = W + (tilesX - W % tilesX); HL = H + (tilesY - H % tilesY);
      srcL = new src.constructor(WL * HL);
      for (y = 0; y < HL; y++) { var sy = reflect(y, H) * W, dy = y * WL; for (x = 0; x < WL; x++) srcL[dy + x] = src[sy + reflect(x, W)]; }
    }
    var tw = WL / tilesX, th = HL / tilesY, area = tw * th;
    var clipLimit = clip > 0 ? Math.max(Math.trunc(clip * area / N), 1) : 0;
    var lutScale = fround(outMax / area);
    var luts = new Float32Array(tilesX * tilesY * N), hist = new Int32Array(N);
    for (var ty = 0; ty < tilesY; ty++) for (var tx = 0; tx < tilesX; tx++) {
      hist.fill(0);
      for (y = ty * th; y < (ty + 1) * th; y++) { var o = y * WL + tx * tw; for (x = 0; x < tw; x++) hist[srcL[o + x]]++; }
      if (clipLimit > 0) {
        var clipped = 0;
        for (i = 0; i < N; i++) if (hist[i] > clipLimit) { clipped += hist[i] - clipLimit; hist[i] = clipLimit; }
        var batch = Math.floor(clipped / N), resid = clipped - batch * N;
        for (i = 0; i < N; i++) hist[i] += batch;
        if (resid) { var step = Math.max(Math.floor(N / resid), 1); for (i = 0; i < N && resid > 0; i += step, resid--) hist[i]++; }
      }
      var sum = 0, lo = (ty * tilesX + tx) * N;
      for (i = 0; i < N; i++) {
        sum += hist[i];
        var v = fround(sum * lutScale);
        luts[lo + i] = quant ? Math.min(255, Math.max(0, roundHalfEven(v))) : Math.min(outMax, v);
      }
    }
    // per-column interpolation tables
    var i1 = new Int32Array(W), i2 = new Int32Array(W), xa = new Float32Array(W), xa1 = new Float32Array(W);
    var invTw = fround(1 / tw), invTh = fround(1 / th);
    for (x = 0; x < W; x++) {
      var txf = fround(fround(x * invTw) - 0.5), t1 = Math.floor(txf), t2 = t1 + 1;
      xa[x] = fround(txf - t1); xa1[x] = fround(1 - xa[x]);
      i1[x] = Math.max(t1, 0) * N; i2[x] = Math.min(t2, tilesX - 1) * N;
    }
    var out = new Uint8Array(W * H);
    for (y = 0; y < H; y++) {
      var tyf = fround(fround(y * invTh) - 0.5), y1 = Math.floor(tyf), y2 = y1 + 1;
      var ya = fround(tyf - y1), ya1 = fround(1 - ya);
      y1 = Math.max(y1, 0); y2 = Math.min(y2, tilesY - 1);
      var b1 = y1 * tilesX * N, b2 = y2 * tilesX * N, ro = y * W;
      for (x = 0; x < W; x++) {
        var sv = src[ro + x], a1 = xa1[x], a2 = xa[x], p1 = i1[x] + sv, p2 = i2[x] + sv;
        var r = fround(fround(fround(fround(luts[b1 + p1] * a1) + fround(luts[b1 + p2] * a2)) * ya1) +
                       fround(fround(fround(luts[b2 + p1] * a1) + fround(luts[b2 + p2] * a2)) * ya));
        var q = roundHalfEven(r); out[ro + x] = q < 0 ? 0 : q > 255 ? 255 : q;
      }
    }
    return out;
  }
  /** cv2.createCLAHE(clipLimit=clip, tileGridSize=(tiles,tiles)).apply(u8) */
  function clahe(u8, W, H, clip, tiles) { var t = Math.max(1, tiles | 0); return claheCore(u8, W, H, t, t, clip, 256, 255, true); }
  /** NON-cv2 variant: same algorithm on 4096 bins (input = 12-bit windowed data), un-quantised LUT. */
  function clahe12(u12, W, H, clip, tiles) { var t = Math.max(1, tiles | 0); return claheCore(u12, W, H, t, t, clip, 4096, 255, false); }

  /* ------------------------------------------------------------- palette */
  /** u8 (+optional invert) -> RGBA. table = Uint8Array(768) from palettes.js. */
  function applyPalette(u8, table, invert, out) {
    var n = u8.length, i, v;
    out = out || new Uint8ClampedArray(n * 4);
    if (LE) {
      var lut = new Uint32Array(256), o32 = new Uint32Array(out.buffer, out.byteOffset, n);
      for (i = 0; i < 256; i++) lut[i] = (table[i * 3] | (table[i * 3 + 1] << 8) | (table[i * 3 + 2] << 16) | 0xff000000) >>> 0;
      if (invert) for (i = 0; i < n; i++) o32[i] = lut[255 - u8[i]]; else for (i = 0; i < n; i++) o32[i] = lut[u8[i]];
    } else {
      for (i = 0; i < n; i++) { v = invert ? 255 - u8[i] : u8[i]; out[4 * i] = table[v * 3]; out[4 * i + 1] = table[v * 3 + 1]; out[4 * i + 2] = table[v * 3 + 2]; out[4 * i + 3] = 255; }
    }
    return out;
  }

  /* ------------------------------------------------------------- filters */
  function gaussKernel(sigma) {
    var r = Math.max(1, Math.ceil(3.5 * sigma)), k = new Float64Array(2 * r + 1), s = 0, i;
    for (i = -r; i <= r; i++) { k[i + r] = Math.exp(-(i * i) / (2 * sigma * sigma)); s += k[i + r]; }
    for (i = 0; i < k.length; i++) k[i] /= s;
    return { r: r, k: k };
  }
  /** separable Gaussian, kernel radius ceil(3.5 sigma), BORDER_REFLECT_101. Returns Float32Array. */
  function gaussianBlur(src, W, H, sigma) {
    var g = gaussKernel(sigma), r = g.r, k = g.k, x, y, j;
    var tmp = new Float32Array(W * H), out = new Float32Array(W * H), acc = new Float64Array(W);
    for (y = 0; y < H; y++) {
      var o = y * W;
      for (x = 0; x < W; x++) {
        var s = 0;
        if (x >= r && x < W - r) for (j = -r; j <= r; j++) s += k[j + r] * src[o + x + j];
        else for (j = -r; j <= r; j++) s += k[j + r] * src[o + reflect(x + j, W)];
        tmp[o + x] = s;
      }
    }
    for (y = 0; y < H; y++) {
      acc.fill(0);
      for (j = -r; j <= r; j++) { var yo = ((y + j >= 0 && y + j < H) ? y + j : reflect(y + j, H)) * W, w = k[j + r]; for (x = 0; x < W; x++) acc[x] += w * tmp[yo + x]; }
      var oo = y * W; for (x = 0; x < W; x++) out[oo + x] = acc[x];
    }
    return out;
  }
  function boxDownsample(src, W, H, f) {
    var w = Math.ceil(W / f), h = Math.ceil(H / f), out = new Float32Array(w * h), x, y;
    for (y = 0; y < h; y++) for (x = 0; x < w; x++) {
      var s = 0, c = 0, ye = Math.min(H, (y + 1) * f), xe = Math.min(W, (x + 1) * f);
      for (var yy = y * f; yy < ye; yy++) { var o = yy * W; for (var xx = x * f; xx < xe; xx++) { s += src[o + xx]; c++; } }
      out[y * w + x] = s / c;
    }
    return { data: out, w: w, h: h };
  }
  function upsampleBilinear(s, w, h, W, H, f) {
    var out = new Float32Array(W * H), x, y, xi0 = new Int32Array(W), xi1 = new Int32Array(W), xw = new Float32Array(W);
    for (x = 0; x < W; x++) { var sx = Math.max(0, (x + 0.5) / f - 0.5), x0 = Math.min(Math.floor(sx), w - 1); xi0[x] = x0; xi1[x] = Math.min(x0 + 1, w - 1); xw[x] = sx - x0; }
    for (y = 0; y < H; y++) {
      var sy = Math.max(0, (y + 0.5) / f - 0.5), y0 = Math.min(Math.floor(sy), h - 1), y1 = Math.min(y0 + 1, h - 1), wy = sy - y0;
      var r0 = y0 * w, r1 = y1 * w, o = y * W;
      for (x = 0; x < W; x++) {
        var wx = xw[x], a = s[r0 + xi0[x]] * (1 - wx) + s[r0 + xi1[x]] * wx, b = s[r1 + xi0[x]] * (1 - wx) + s[r1 + xi1[x]] * wx;
        out[o + x] = a * (1 - wy) + b * wy;
      }
    }
    return out;
  }
  /** Gaussian for any sigma: exact up to 6 px, beyond that box-downsample -> blur -> bilinear upsample (O(1) in sigma). */
  function blurLarge(src, W, H, sigma) {
    if (sigma <= 6) return gaussianBlur(src, W, H, sigma);
    var f = 2; while (sigma / (f * 2) >= 3) f *= 2;
    var d = boxDownsample(src, W, H, f), b = gaussianBlur(d.data, d.w, d.h, sigma / f);
    return upsampleBilinear(b, d.w, d.h, W, H, f);
  }

  function sort2(p, i, j) { if (p[i] > p[j]) { var t = p[i]; p[i] = p[j]; p[j] = t; } }
  function med9(p) { // Devillard's 19-exchange network
    sort2(p, 1, 2); sort2(p, 4, 5); sort2(p, 7, 8); sort2(p, 0, 1); sort2(p, 3, 4); sort2(p, 6, 7);
    sort2(p, 1, 2); sort2(p, 4, 5); sort2(p, 7, 8); sort2(p, 0, 3); sort2(p, 5, 8); sort2(p, 4, 7);
    sort2(p, 3, 6); sort2(p, 1, 4); sort2(p, 2, 5); sort2(p, 4, 7); sort2(p, 4, 2); sort2(p, 6, 4); sort2(p, 4, 2);
    return p[4];
  }
  function selectK(a, n, k) { // Hoare quickselect
    var lo = 0, hi = n - 1;
    while (lo < hi) {
      var pv = a[(lo + hi) >> 1], i = lo, j = hi;
      while (i <= j) { while (a[i] < pv) i++; while (a[j] > pv) j--; if (i <= j) { var t = a[i]; a[i] = a[j]; a[j] = t; i++; j--; } }
      if (k <= j) hi = j; else if (k >= i) lo = i; else break;
    }
    return a[k];
  }
  /** k x k median (k = 3|5), BORDER_REPLICATE like cv2.medianBlur. Exact for integer data. Returns Float32Array. */
  async function medianFilter(src, W, H, k, ctl) {
    ctl = ctl || NOCTL;
    var r = k >> 1, out = new Float32Array(W * H), p = new Float32Array(k * k), mid = (k * k) >> 1, x, y, i, j;
    for (y = 0; y < H; y++) {
      if ((y & 15) === 0) await ctl.tick();
      var rows = [];
      for (j = 0; j < k; j++) rows.push(clampi(y + j - r, H) * W);
      for (x = 0; x < W; x++) {
        var n = 0;
        for (j = 0; j < k; j++) for (i = 0; i < k; i++) p[n++] = src[rows[j] + clampi(x + i - r, W)];
        out[y * W + x] = k === 3 ? med9(p) : selectK(p, n, mid);
      }
    }
    return out;
  }

  /** Bilateral filter: circular window of `radius`, spatial sigma = max(0.5, radius/2), range sigma in ADU, reflect-101. */
  async function bilateral(src, W, H, o, ctl) {
    ctl = ctl || NOCTL;
    var r = Math.max(1, o.radius | 0), ss = o.sigmaSpace || Math.max(0.5, r / 2), sr = Math.max(1e-3, o.sigmaRange);
    var offs = [], x, y, k;
    for (var dy = -r; dy <= r; dy++) for (var dx = -r; dx <= r; dx++) {
      var d2 = dx * dx + dy * dy; if ((dx || dy) && d2 <= r * r) offs.push([dx, dy, Math.exp(-d2 / (2 * ss * ss))]);
    }
    var Q = 256, tsize = 6 * Q + 2, lut = new Float32Array(tsize), qs = Q / sr;
    for (k = 0; k < tsize; k++) lut[k] = Math.exp(-0.5 * (k / Q) * (k / Q));
    var pw = W + 2 * r, ph = H + 2 * r, P = new Float32Array(pw * ph);
    for (y = 0; y < ph; y++) { var sy = reflect(y - r, H) * W, py = y * pw; for (x = 0; x < pw; x++) P[py + x] = src[sy + reflect(x - r, W)]; }
    var out = new Float32Array(W * H), no = offs.length, oo = new Int32Array(no), ow = new Float64Array(no);
    for (k = 0; k < no; k++) { oo[k] = offs[k][1] * pw + offs[k][0]; ow[k] = offs[k][2]; }
    for (y = 0; y < H; y++) {
      if ((y & 15) === 0) await ctl.tick();
      for (x = 0; x < W; x++) {
        var pi = (y + r) * pw + x + r, c = P[pi], s = c, ws = 1;
        for (k = 0; k < no; k++) {
          var v = P[pi + oo[k]], d = v - c; if (d < 0) d = -d;
          var qi = (d * qs + 0.5) | 0; if (qi >= tsize) continue;
          var w = ow[k] * lut[qi]; s += w * v; ws += w;
        }
        out[y * W + x] = s / ws;
      }
    }
    return out;
  }

  /**
   * Non-local means, fast formulation: for every search offset the patch distance map is built with separable box sums
   * (no per-pixel patch loops), weights come from an exp LUT, and each unordered pixel pair is evaluated once and
   * scattered to both ends (half-plane of offsets). Weight w = exp(-D / h^2), D = mean squared patch difference
   * (patch (2p+1)^2, reflect-101 beyond the image); the centre pixel has weight 1; window pixels outside the image are
   * simply not used. Processed in 32-row strips with cooperative yielding so the UI can cancel it.
   */
  async function nlm(src, W, H, o, ctl) {
    ctl = ctl || NOCTL;
    var p = Math.max(1, o.patch | 0), S = Math.max(1, o.search | 0), h = Math.max(1e-3, o.h);
    var pw = W + 2 * p, ph = H + 2 * p, P = new Float32Array(pw * ph), x, y, i;
    for (y = 0; y < ph; y++) { var sy = reflect(y - p, H) * W, py = y * pw; for (x = 0; x < pw; x++) P[py + x] = src[sy + reflect(x - p, W)]; }
    var N = W * H, acc = Float32Array.from(src), wsum = new Float32Array(N).fill(1);
    var np2 = (2 * p + 1) * (2 * p + 1), TMAX = 10, LS = 4096, tscale = LS / TMAX, lut = new Float32Array(LS), invH2 = 1 / (h * h * np2);
    for (i = 0; i < LS; i++) lut[i] = Math.exp(-(i + 0.5) / tscale);
    var SR = 32, E = new Float32Array((SR + 2 * p) * (W + 2 * p)), Hs = new Float32Array((SR + 2 * p) * W), tot = 0, doneN = 0;
    for (var oy = 0; oy <= S; oy++) for (var ox = -S; ox <= S; ox++) if (oy > 0 || ox > 0) tot++;
    for (var dy = 0; dy <= S; dy++) for (var dx = -S; dx <= S; dx++) {
      if (dy === 0 && dx <= 0) continue;
      var xs = Math.max(0, -dx), xe = Math.min(W, W - dx), nx = xe - xs;
      if (nx > 0) for (var y0 = 0; y0 < H - dy; y0 += SR) {
        await ctl.tick();
        var yE = Math.min(y0 + SR, H - dy), ny = yE - y0, nr = ny + 2 * p, nc = nx + 2 * p, r, c;
        for (r = 0; r < nr; r++) {
          var ia = (y0 - p + r + p) * pw + xs - p + p, ib = (y0 - p + r + dy + p) * pw + xs - p + dx + p, eo = r * nc;
          for (c = 0; c < nc; c++) { var df = P[ia + c] - P[ib + c]; E[eo + c] = df * df; }
        }
        for (r = 0; r < nr; r++) {
          var ho = r * nx, eo2 = r * nc;
          for (c = 0; c < nx; c++) { var s = 0; for (var k = 0; k <= 2 * p; k++) s += E[eo2 + c + k]; Hs[ho + c] = s; }
        }
        for (r = 0; r < ny; r++) {
          var q = (y0 + r) * W + xs;
          for (c = 0; c < nx; c++) {
            var d = 0; for (var k2 = 0; k2 <= 2 * p; k2++) d += Hs[(r + k2) * nx + c];
            var t = d * invH2; if (t >= TMAX) continue;
            var w = lut[(t * tscale) | 0], q1 = q + c, q2 = q1 + dy * W + dx;
            acc[q1] += w * src[q2]; wsum[q1] += w; acc[q2] += w * src[q1]; wsum[q2] += w;
          }
        }
      }
      doneN++; ctl.progress('NLM denoise ' + Math.round(100 * doneN / tot) + ' %');
    }
    var out = new Float32Array(N);
    for (i = 0; i < N; i++) out[i] = acc[i] / wsum[i];
    return out;
  }

  /* -------------------------------------------------------- enhancements */
  /** out = src + amount * (src - gaussian(src, sigma)) */
  function unsharp(src, W, H, amount, sigma) {
    var b = gaussianBlur(src, W, H, sigma), out = new Float32Array(src.length);
    for (var i = 0; i < out.length; i++) out[i] = src[i] + amount * (src[i] - b[i]);
    return out;
  }
  /** Multi-scale local contrast: bands b0 = src - G(s0), bk = G(s_{k-1}) - G(s_k), s_k = s0*2^k; out = src + amount * sum(bk/(k+1)). */
  function localContrast(src, W, H, amount, sigma0, levels) {
    var out = Float32Array.from(src), prev = src, i;
    for (var k = 0; k < levels; k++) {
      var g = blurLarge(src, W, H, sigma0 * Math.pow(2, k)), c = amount / (k + 1);
      for (i = 0; i < out.length; i++) out[i] += c * (prev[i] - g[i]);
      prev = g;
    }
    return out;
  }
  /** Background flatten: out = src * (1 - s + s * m / max(bg, 0.02 m)), bg = large Gaussian, m = mean(bg). */
  function flatten(src, W, H, sigma, strength) {
    var bg = blurLarge(src, W, H, sigma), n = src.length, m = 0, i;
    for (i = 0; i < n; i++) m += bg[i];
    m /= n;
    var floor = 0.02 * m, out = new Float32Array(n);
    for (i = 0; i < n; i++) out[i] = src[i] * (1 - strength + strength * m / Math.max(bg[i], floor));
    return out;
  }
  /** Destripe: subtract (line median - running median of the line medians over +-win) * strength, per row and/or column. */
  function destripe(src, W, H, axis, win, strength) {
    var out = Float32Array.from(src);
    function pass(ax) {
      var st = lineStats(out, W, H, ax), med = st.med, n = med.length, buf = new Float64Array(2 * win + 1), shift = new Float64Array(n), i, j;
      for (i = 0; i < n; i++) {
        var lo = Math.max(0, i - win), hi = Math.min(n, i + win + 1), c = hi - lo;
        for (j = 0; j < c; j++) buf[j] = med[lo + j];
        var s = buf.subarray(0, c); s.sort();
        shift[i] = strength * (med[i] - medianSorted(s, c));
      }
      if (ax === 0) for (i = 0; i < H; i++) { var o = i * W, d = shift[i]; for (j = 0; j < W; j++) out[o + j] = out[o + j] - d; }
      else for (i = 0; i < H; i++) { var o2 = i * W; for (j = 0; j < W; j++) out[o2 + j] = out[o2 + j] - shift[j]; }
    }
    if (axis === 'rows' || axis === 'both') pass(0);
    if (axis === 'cols' || axis === 'both') pass(1);
    return out;
  }
  function toU16(f) {
    var n = f.length, out = new Uint16Array(n);
    for (var i = 0; i < n; i++) { var v = Math.floor(f[i] + 0.5); out[i] = v < 0 ? 0 : v > 65535 ? 65535 : v; }
    return out;
  }

  /* ------------------------------------------------------------ geometry */
  /** rot = clockwise quarter turns (0..3), then flipH, then flipV. Same as np.rot90(a,-rot)[:, ::-1][::-1]. */
  function transform(src, W, H, g) {
    var rot = ((g.rot || 0) % 4 + 4) % 4, fh = !!g.flipH, fv = !!g.flipV;
    if (!rot && !fh && !fv) return { data: src, W: W, H: H };
    var w2 = rot & 1 ? H : W, h2 = rot & 1 ? W : H, out = new src.constructor(w2 * h2), x, y;
    for (y = 0; y < H; y++) for (x = 0; x < W; x++) {
      var dx, dy;
      if (rot === 0) { dx = x; dy = y; } else if (rot === 1) { dx = H - 1 - y; dy = x; } else if (rot === 2) { dx = W - 1 - x; dy = H - 1 - y; } else { dx = y; dy = W - 1 - x; }
      if (fh) dx = w2 - 1 - dx;
      if (fv) dy = h2 - 1 - dy;
      out[dy * w2 + dx] = src[y * W + x];
    }
    return { data: out, W: w2, H: h2 };
  }
  /** display (x,y) -> sensor (x,y) for the same transform; W,H are the SENSOR dimensions. */
  function displayToSensor(dx, dy, W, H, g) {
    var rot = ((g.rot || 0) % 4 + 4) % 4, w2 = rot & 1 ? H : W, h2 = rot & 1 ? W : H;
    if (g.flipH) dx = w2 - 1 - dx;
    if (g.flipV) dy = h2 - 1 - dy;
    if (rot === 0) return [dx, dy];
    if (rot === 1) return [dy, H - 1 - dx];
    if (rot === 2) return [W - 1 - dx, H - 1 - dy];
    return [W - 1 - dy, dx];
  }

  /** sensor (x,y) -> display (x,y): the forward mapping of transform(). W,H = SENSOR dimensions. */
  function sensorToDisplay(sx, sy, W, H, g) {
    var rot = ((g.rot || 0) % 4 + 4) % 4, w2 = rot & 1 ? H : W, h2 = rot & 1 ? W : H, x, y;
    if (rot === 0) { x = sx; y = sy; } else if (rot === 1) { x = H - 1 - sy; y = sx; } else if (rot === 2) { x = W - 1 - sx; y = H - 1 - sy; } else { x = sy; y = W - 1 - sx; }
    if (g.flipH) x = w2 - 1 - x;
    if (g.flipV) y = h2 - 1 - y;
    return [x, y];
  }

  /* ------------------------------------------------------------- ROI etc. */
  /** mean / SD (population) / SNR = mean/SD of img[y0:y1, x0:x1] (x1,y1 exclusive). */
  function roiStats(img, W, x0, y0, x1, y1) {
    var n = 0, s = 0, s2 = 0, mn = Infinity, mx = -Infinity;
    for (var y = y0; y < y1; y++) for (var x = x0; x < x1; x++) { var v = img[y * W + x]; s += v; s2 += v * v; n++; if (v < mn) mn = v; if (v > mx) mx = v; }
    if (!n) return null;
    var mean = s / n, sd = Math.sqrt(Math.max(0, s2 / n - mean * mean));
    return { n: n, mean: mean, sd: sd, snr: sd > 0 ? mean / sd : Infinity, min: mn, max: mx };
  }
  /** samples along (x0,y0)->(x1,y1), nearest neighbour, one sample per pixel of length. */
  function lineProfile(img, W, H, x0, y0, x1, y1) {
    var len = Math.max(1, Math.round(Math.hypot(x1 - x0, y1 - y0))), n = len + 1, out = new Float32Array(n);
    for (var i = 0; i < n; i++) {
      var t = i / len, x = clampi(Math.round(x0 + (x1 - x0) * t), W), y = clampi(Math.round(y0 + (y1 - y0) * t), H);
      out[i] = img[y * W + x];
    }
    return out;
  }

  /* ------------------------------------------------------------- pipeline */
  function defaultParams() {
    return {
      dark: { on: true },
      lines: { on: true, thresh: 25 },
      manual: { on: true, list: [] },                       // [{k:'p'|'r'|'c', x, y}] in sensor coordinates
      pixels: { on: false, thresh: 12 },
      destripe: { on: false, axis: 'rows', win: 16, strength: 1 },
      flatten: { on: false, sigma: 128, strength: 1 },
      denoise: { mode: 'none', sigma: 1.0, radius: 2, sigmaR: 60, h: 40, search: 3, patch: 1 },
      unsharp: { on: false, amount: 1.0, sigma: 2.0 },
      local: { on: false, amount: 0.6, sigma: 4, levels: 3 },
      geom: { rot: 0, flipH: false, flipV: false },
      win: { mode: 'pct', loPct: 1.0, hiPct: 99.5, lo: 0, hi: 65535, curve: 'none', cparam: 1, region: 'full' },
      clahe: { on: false, clip: 2.0, tiles: 8, bits: 8 },
      invert: false,
      palette: 'Grayscale',
      crop: { x0: 204, y0: 204, x1: 1844, y1: 1844 }
    };
  }
  function K() { return JSON.stringify(Array.prototype.slice.call(arguments)); }
  var CANCELLED = 'cancelled';

  /**
   * Stateful processing chain with per-stage caching:
   *  A base      : dark subtract -> line defects -> manual + single-pixel repair   (u16, full resolution, always)
   *  B1 destripe -> flatten ; B2 denoise ; B3 unsharp -> local contrast            (float32, cached each)
   *  C  proc     : round to u16 -> geometry                                         (= "processed 16-bit data")
   *  D  window -> (CLAHE) -> invert -> palette                                      (u8 / RGBA)
   * mode 'preview' runs B..D on a box-downsampled copy of the base (factor f, spatial parameters scaled by 1/f).
   */
  function Pipeline(palettes) {
    this.palettes = palettes; this.raw = null; this.dark = null; this.rawType = 'xray'; this.W = DIM; this.H = DIM;
    this.rawVer = 0; this.darkVer = 0; this.c = {}; this.proc = null; this.procW = 0; this.procH = 0; this.procKey = ''; this.procVer = 0;
    this.rgba = null;
  }
  Pipeline.prototype.setRaw = function (u16, type, W, H) {
    this.raw = u16; this.rawType = type || 'xray'; this.W = W || DIM; this.H = H || DIM; this.rawVer++; this.c = {}; this.proc = null; this.rgba = null;
  };
  Pipeline.prototype.setDark = function (u16) { this.dark = u16 || null; this.darkVer++; this.c = {}; };

  Pipeline.prototype._base = async function (P, ctl) {
    var W = this.W, H = this.H, haveDark = !!(P.dark.on && this.dark && this.dark.length === this.raw.length);
    var useDark = haveDark && this.rawType !== 'dark';     // a dark frame is never dark-subtracted (as in _process)
    var man = P.manual.on ? P.manual.list : [];
    var key = K(this.rawVer, haveDark ? this.darkVer : 0, P.lines, man, P.pixels, P.manual.on, this.rawType);
    if (this.c.base && this.c.base.key === key) return this.c.base;
    ctl.progress('Dark subtraction'); await ctl.tick();
    var img = useDark ? subtractDark(this.raw, this.dark) : this.raw, defects = { rows: [], cols: [], npix: 0 }, i;
    var dmap = { rows: new Uint8Array(H), cols: new Uint8Array(W) };
    if (P.lines.on) {
      ctl.progress('Detecting defect lines'); await ctl.tick();
      dmap = buildDefectMap(img, haveDark ? this.dark : null, W, H, P.lines.thresh);   // detect on the offset-corrected frame OR the dark
    }
    var mpix = [];
    for (i = 0; i < man.length; i++) {
      var m = man[i];
      if (m.k === 'r' && m.y >= 0 && m.y < H) dmap.rows[m.y] = 1;
      else if (m.k === 'c' && m.x >= 0 && m.x < W) dmap.cols[m.x] = 1;
      else if (m.k === 'p' && m.x >= 0 && m.x < W && m.y >= 0 && m.y < H) mpix.push(m.y * W + m.x);
    }
    for (i = 0; i < H; i++) if (dmap.rows[i]) defects.rows.push(i);
    for (i = 0; i < W; i++) if (dmap.cols[i]) defects.cols.push(i);
    if (defects.rows.length || defects.cols.length) { ctl.progress('Repairing lines'); await ctl.tick(); img = applyDefectMap(img, dmap, W, H); }
    if (P.pixels.on || mpix.length) {
      ctl.progress('Repairing pixels'); await ctl.tick();
      var r = repairPixels(img, W, H, { auto: P.pixels.on, thr: P.pixels.thresh, manual: mpix });
      img = r.img; defects.npix = r.count;
    }
    return (this.c.base = { key: key, img: img, defects: defects });
  };

  /** B stages on (src, w, h) with spatial parameters scaled by s (=1/f). cacheKey != null -> cache B1..B3 in this.c. */
  Pipeline.prototype._enhance = async function (src, w, h, P, s, ctl, baseKey) {
    var cache = baseKey !== null, key1, key2, key3, cur = src, self = this;
    async function step(slot, key, label, fn) {
      if (cache && self.c[slot] && self.c[slot].key === key) return self.c[slot].img;
      ctl.progress(label); await ctl.tick();
      var out = await fn();
      if (cache) self.c[slot] = { key: key, img: out };
      return out;
    }
    var d = P.destripe, fl = P.flatten, dn = P.denoise, us = P.unsharp, lc = P.local;
    key1 = K(baseKey, d.on && d, fl.on && fl);
    if (d.on || fl.on) cur = await step('b1', key1, 'Destripe / flatten', async function () {
      var t = cur;
      if (d.on) t = destripe(t, w, h, d.axis, Math.max(1, Math.round(d.win * s)), d.strength);
      if (fl.on) t = flatten(t, w, h, Math.max(1, fl.sigma * s), fl.strength);
      return t;
    });
    key2 = K(key1, dn.mode, dn.mode === 'none' ? 0 : dn);
    if (dn.mode !== 'none') cur = await step('b2', key2, 'Denoise: ' + dn.mode, async function () {
      var t = cur;
      if (dn.mode === 'gauss') return gaussianBlur(t, w, h, Math.max(0.3, dn.sigma * s));
      if (dn.mode === 'median3') return medianFilter(t, w, h, 3, ctl);
      if (dn.mode === 'median5') return medianFilter(t, w, h, 5, ctl);
      if (dn.mode === 'bilateral') return bilateral(t, w, h, { radius: dn.radius, sigmaRange: dn.sigmaR }, ctl);
      return nlm(t, w, h, { h: dn.h, search: dn.search, patch: dn.patch }, ctl);
    });
    key3 = K(key2, us.on && us, lc.on && lc);
    if (us.on || lc.on) cur = await step('b3', key3, 'Unsharp / local contrast', async function () {
      var t = cur;
      if (us.on) t = unsharp(t, w, h, us.amount, Math.max(0.3, us.sigma * s));
      if (lc.on) t = localContrast(t, w, h, lc.amount, Math.max(0.5, lc.sigma * s), lc.levels);
      return t;
    });
    return { img: cur, key: key3 };
  };

  /**
   * run(params, {mode:'full'|'preview', f:4}, ctl) -> {mode, W, H, rgba, lo, hi, procChanged, proc?, defects}
   * For 'full': this.proc / this.rgba hold the processed 16-bit frame and the displayed RGBA (used by the exports).
   */
  Pipeline.prototype.run = async function (P, opts, ctl) {
    ctl = ctl || NOCTL; opts = opts || {};
    if (!this.raw) throw new Error('no frame');
    var preview = opts.mode === 'preview', f = preview ? Math.max(2, opts.f | 0) : 1, t0 = Date.now();
    var base = await this._base(P, ctl), W = this.W, H = this.H, w = W, h = H, src = base.img, baseKey = base.key, s = 1;
    if (preview) {
      var kp = K(baseKey, f);
      if (!this.c.pv || this.c.pv.key !== kp) { var ds = boxDownsample(base.img, W, H, f); this.c.pv = { key: kp, img: ds.data, w: ds.w, h: ds.h }; }
      src = this.c.pv.img; w = this.c.pv.w; h = this.c.pv.h; s = 1 / f; baseKey = null;
    }
    var en = await this._enhance(src, w, h, P, s, ctl, baseKey), data;
    await ctl.tick();
    var procKey = K(en.key, P.geom, preview);
    var oriented, ow, oh;
    if (!preview && this.c.proc && this.c.proc.key === procKey) { oriented = this.c.proc.img; ow = this.c.proc.w; oh = this.c.proc.h; }
    else {
      data = en.img instanceof Uint16Array ? en.img : toU16(en.img);
      var tr = transform(data, w, h, P.geom); oriented = tr.data; ow = tr.W; oh = tr.H;
      if (!preview) this.c.proc = { key: procKey, img: oriented, w: ow, h: oh };
    }
    var procChanged = false;
    if (!preview && this.procKey !== procKey + '|' + this.rawVer) { this.proc = oriented; this.procW = ow; this.procH = oh; this.procKey = procKey + '|' + this.rawVer; this.procVer++; procChanged = true; }
    // window
    ctl.progress('Window / palette'); await ctl.tick();
    var lo, hi, wn = P.win, region = null;
    if (wn.mode === 'abs') { lo = wn.lo; hi = wn.hi; }
    else {
      if (wn.region === 'crop' && P.crop) region = [Math.round(P.crop.x0 * ow / W), Math.round(P.crop.y0 * oh / H), Math.round(P.crop.x1 * ow / W), Math.round(P.crop.y1 * oh / H)];
      var pr = percentileRange(oriented, ow, oh, Math.max(0, wn.loPct) / 100, Math.min(100, wn.hiPct) / 100, { region: region, step: preview ? 1 : 3 });
      lo = pr.lo; hi = pr.hi;
    }
    var use12 = P.clahe.on && P.clahe.bits === 12, lut = windowLut(lo, hi, wn.curve, wn.cparam, use12 ? 4095 : 255);
    var win = applyLut(oriented, lut), u8;
    if (P.clahe.on) {
      ctl.progress('CLAHE'); await ctl.tick();
      u8 = use12 ? clahe12(win, ow, oh, P.clahe.clip, P.clahe.tiles) : clahe(win, ow, oh, P.clahe.clip, P.clahe.tiles);
    } else u8 = win;
    var rgba = applyPalette(u8, this.palettes.get(P.palette), P.invert);
    if (!preview) this.rgba = rgba;
    var res = { mode: opts.mode || 'full', W: ow, H: oh, rgba: rgba, lo: lo, hi: hi, procChanged: procChanged, defects: base.defects, ms: Date.now() - t0 };
    if (!preview) { res.procVer = this.procVer; res.proc = this.proc; }
    return res;
  };

  /** u16 LE bytes of the processed frame (optionally cropped: [x0,y0,x1,y1] in processed/oriented coordinates). */
  Pipeline.prototype.exportProcU16 = function (crop) {
    var W = this.procW, H = this.procH, c = crop || [0, 0, W, H], w = c[2] - c[0], h = c[3] - c[1], out = new Uint16Array(w * h);
    for (var y = 0; y < h; y++) out.set(this.proc.subarray((c[1] + y) * W + c[0], (c[1] + y) * W + c[0] + w), y * w);
    if (!LE) for (var i = 0; i < out.length; i++) out[i] = ((out[i] & 255) << 8) | (out[i] >> 8);
    return { data: out, w: w, h: h };
  };
  /** RGB bytes of what is displayed (cropped), from the last full render. */
  Pipeline.prototype.exportRgb = function (crop) {
    var W = this.procW, H = this.procH, c = crop || [0, 0, W, H], w = c[2] - c[0], h = c[3] - c[1], out = new Uint8Array(w * h * 3), rg = this.rgba;
    for (var y = 0; y < h; y++) {
      var s = ((c[1] + y) * W + c[0]) * 4, d = y * w * 3;
      for (var x = 0; x < w; x++, s += 4, d += 3) { out[d] = rg[s]; out[d + 1] = rg[s + 1]; out[d + 2] = rg[s + 2]; }
    }
    return { data: out, w: w, h: h };
  };

  return {
    DIM: DIM, BORDER: BORDER, CANCELLED: CANCELLED, NOCTL: NOCTL,
    subtractDark: subtractDark, lineStats: lineStats, badLines: badLines, buildDefectMap: buildDefectMap, applyDefectMap: applyDefectMap,
    repairPixels: repairPixels, histogram16: histogram16, valueAtRank: valueAtRank, percentileRange: percentileRange,
    windowLut: windowLut, applyLut: applyLut, toU8: toU8, clahe: clahe, clahe12: clahe12, claheCore: claheCore, applyPalette: applyPalette,
    gaussianBlur: gaussianBlur, blurLarge: blurLarge, boxDownsample: boxDownsample, medianFilter: medianFilter, bilateral: bilateral,
    nlm: nlm, unsharp: unsharp, localContrast: localContrast, flatten: flatten, destripe: destripe, toU16: toU16,
    transform: transform, displayToSensor: displayToSensor, sensorToDisplay: sensorToDisplay, roiStats: roiStats, lineProfile: lineProfile,
    defaultParams: defaultParams, Pipeline: Pipeline, roundHalfEven: roundHalfEven
  };
});
