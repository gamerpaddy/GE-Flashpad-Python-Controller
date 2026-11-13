import test from 'node:test';
import assert from 'node:assert/strict';
import { PC, Palettes, fixtures, sha256, idx, bigFrames, synthSignal, u16FromB64, synthDark } from './util.mjs';

const X = fixtures();
const W = 2048, H = 2048;
const { raw, dark } = bigFrames();

test('dark subtraction == numpy int32 clip', () => {
  const sub = PC.subtractDark(raw, dark);
  assert.equal(sha256(sub), X.dark_sub.sha);
  let z = 0; for (const v of sub) if (v === 0) z++;
  assert.equal(z, X.dark_sub.zeros);
});

test('_bad_lines (rows / cols, signal / dark / raw, 3 thresholds) == flashpad_capture_gui._bad_lines', () => {
  const sub = PC.subtractDark(raw, dark);
  for (const thr of [3, 25, 100]) {
    const e = X.bad_lines[String(thr)];
    assert.deepEqual(idx(PC.badLines(sub, W, H, 0, thr)), e.rows, 'rows thr ' + thr);
    assert.deepEqual(idx(PC.badLines(sub, W, H, 1, thr)), e.cols, 'cols thr ' + thr);
    assert.deepEqual(idx(PC.badLines(dark, W, H, 0, thr)), e.dark_rows, 'dark rows thr ' + thr);
    assert.deepEqual(idx(PC.badLines(dark, W, H, 1, thr)), e.dark_cols, 'dark cols thr ' + thr);
    assert.deepEqual(idx(PC.badLines(raw, W, H, 0, thr)), e.raw_rows, 'raw rows thr ' + thr);
    assert.deepEqual(idx(PC.badLines(raw, W, H, 1, thr)), e.raw_cols, 'raw cols thr ' + thr);
  }
});

test('the real panel defects (rows 48/1093, col 783) are found at the default threshold', () => {
  const sub = PC.subtractDark(raw, dark), m = PC.buildDefectMap(sub, dark, W, H, 25);
  for (const r of [48, 1093]) assert.ok(idx(m.rows).includes(r), 'row ' + r);
  assert.ok(idx(m.cols).includes(783));
  assert.deepEqual(idx(m.rows), X.defect_map.rows);
  assert.deepEqual(idx(m.cols), X.defect_map.cols);
});

test('build_defect_map + apply_defect_map (rows then cols, float32 interpolation, truncation) bit-exact', () => {
  const sub = PC.subtractDark(raw, dark), m = PC.buildDefectMap(sub, dark, W, H, 25);
  assert.equal(sha256(PC.applyDefectMap(sub, m, W, H)), X.defect_apply.sha);
  const m2 = PC.buildDefectMap(raw, null, W, H, 25);               // no dark: detection on the raw frame only
  assert.deepEqual(idx(m2.rows), X.defect_map_nodark.rows);
  assert.deepEqual(idx(m2.cols), X.defect_map_nodark.cols);
  assert.equal(sha256(PC.applyDefectMap(raw, m2, W, H)), X.defect_map_nodark.sha);
});

test('apply_defect_map edge cases: nearest-copy at the borders, adjacent runs', () => {
  const sub = PC.subtractDark(raw, dark), e = X.defect_apply_edges;
  const rows = new Uint8Array(H), cols = new Uint8Array(W);
  for (const r of e.rows) rows[r] = 1;
  for (const c of e.cols) cols[c] = 1;
  assert.equal(sha256(PC.applyDefectMap(sub, { rows, cols }, W, H)), e.sha);
});

test('to_u8: percentile range + 8-bit mapping bit-exact (float32 arithmetic, truncation)', () => {
  const sub = PC.subtractDark(raw, dark), fixed = PC.applyDefectMap(sub, PC.buildDefectMap(sub, dark, W, H, 25), W, H);
  assert.equal(sha256(fixed), X.pipeline_final_sha);
  for (const [k, e] of Object.entries(X.to_u8)) {
    const [lo, hi] = k.split('_').map(Number), r = PC.toU8(fixed, W, H, lo, hi);
    assert.equal(r.lo, e.lo, 'lo ' + k); assert.equal(r.hi, e.hi, 'hi ' + k);
    assert.equal(sha256(r.u8), e.sha, 'u8 ' + k);
  }
  const reg = PC.percentileRange(fixed, W, H, 0.01, 0.995, { region: [204, 204, 1844, 1844] });
  assert.equal(reg.n, X.percentile_region.n); assert.equal(reg.lo, X.percentile_region.lo); assert.equal(reg.hi, X.percentile_region.hi);
  assert.equal(PC.percentileRange(fixed, W, H, 0.01, 0.995).n, 463757);   // 679 rows x 683 cols as in the Python sampling
});

test('window LUT with curves: monotone, in range, identity-like for gamma 1', () => {
  const none = PC.windowLut(1000, 5000, 'none', 0, 255);
  for (const [c, p] of [['gamma', 1], ['gamma', 2.2], ['log', 30], ['sigmoid', 8]]) {
    const l = PC.windowLut(1000, 5000, c, p, 255);
    assert.equal(l[0], 0); assert.equal(l[65535], 255); assert.equal(l[1000], 0); assert.equal(l[5000], 255);
    for (let v = 1; v < 65536; v++) assert.ok(l[v] >= l[v - 1], c + ' monotone at ' + v);
    if (c === 'gamma' && p === 1) for (let v = 0; v < 65536; v += 7) assert.ok(Math.abs(l[v] - none[v]) <= 1);
  }
  const l12 = PC.windowLut(1000, 5000, 'none', 0, 4095);
  assert.equal(l12[5000], 4095); assert.equal(l12[3000], 2048);
});

test('single-pixel repair == numpy reference (auto, manual only, both; 5x5 fallback path included)', () => {
  const px = synthSignal(96, 64, 11, { pixels: [[10, 10, 30000], [50, 30, 0], [51, 30, 0], [52, 31, 45000], [0, 0, 20000], [95, 63, 25000], [30, 5, 0]] });
  const e = X.pixels;
  let r = PC.repairPixels(px, 96, 64, { auto: true, thr: 8, manual: [] });
  assert.deepEqual(r.img, u16FromB64(e.auto)); assert.equal(r.count, e.auto_n);
  r = PC.repairPixels(px, 96, 64, { auto: false, manual: [20 * 96 + 20, 20 * 96 + 21] });
  assert.deepEqual(r.img, u16FromB64(e.manual)); assert.equal(r.count, e.manual_n);
  r = PC.repairPixels(px, 96, 64, { auto: true, thr: 6, manual: [40 * 96 + 70] });
  assert.deepEqual(r.img, u16FromB64(e.both)); assert.equal(r.count, e.both_n);
});

test('geometry: all 16 rot/flip combinations == np.rot90(a,-rot)[:, ::-1][::-1]; displayToSensor is the inverse', () => {
  const src = new Uint16Array(35).map((_, i) => i);
  for (const g of X.geometry) {
    const t = PC.transform(src, 7, 5, { rot: g.rot, flipH: !!g.flipH, flipV: !!g.flipV });
    assert.deepEqual([t.H, t.W], g.shape); assert.deepEqual(Array.from(t.data), g.data);
    for (let y = 0; y < t.H; y++) for (let x = 0; x < t.W; x++) {
      const [sx, sy] = PC.displayToSensor(x, y, 7, 5, g);
      assert.equal(src[sy * 7 + sx], t.data[y * t.W + x], JSON.stringify(g) + ' @' + x + ',' + y);
      assert.deepEqual(PC.sensorToDisplay(sx, sy, 7, 5, g), [x, y]);
    }
  }
});

test('ROI statistics and line profile == numpy', () => {
  const u = synthSignal(64, 48, 7), e = X.roi, [x0, y0, x1, y1] = e.box;
  const r = PC.roiStats(u, 64, x0, y0, x1, y1);
  assert.equal(r.n, e.n); assert.equal(r.min, e.min); assert.equal(r.max, e.max);
  assert.ok(Math.abs(r.mean - e.mean) < 1e-9); assert.ok(Math.abs(r.sd - e.sd) < 1e-6);
  assert.ok(Math.abs(r.snr - e.mean / e.sd) < 1e-6);
  const p = PC.lineProfile(u, 64, 48, ...X.profile.line);
  assert.deepEqual(Array.from(p), X.profile.values);
});

test('palettes.js tables are exact cv2 LUTs', () => {
  assert.deepEqual(Palettes.names, Object.keys(X.palettes));
  for (const n of Palettes.names) { const t = Palettes.get(n); assert.equal(t.length, 768); assert.equal(sha256(t), X.palettes[n], n); }
  assert.deepEqual(Array.from(Palettes.get('Grayscale').subarray(0, 9)), [0, 0, 0, 1, 1, 1, 2, 2, 2]);
  assert.deepEqual(Array.from(Palettes.get('Jet').subarray(0, 3)), [0, 0, 128]);   // cv2 JET starts dark blue (RGB 0,0,128)
});

test('manual defect list: marked row/column/pixel are repaired by the pipeline', async () => {
  const a = synthSignal(128, 96, 3), d = synthDark(128, 96, 4);
  const p = new PC.Pipeline(Palettes); p.setRaw(a, 'xray', 128, 96); p.setDark(d);
  const P = PC.defaultParams(); P.lines.on = false;
  P.manual.list = [{ k: 'r', y: 40 }, { k: 'c', x: 60 }, { k: 'p', x: 10, y: 10 }];
  const r = await p.run(P, { mode: 'full' });
  assert.deepEqual(r.defects.rows, [40]); assert.deepEqual(r.defects.cols, [60]); assert.equal(r.defects.npix, 1);
  const sub = PC.subtractDark(a, d), proc = p.proc;
  // untouched pixel identical, repaired row is an interpolation of rows 39/41 (+/- float truncation)
  assert.equal(proc[5 * 128 + 5], sub[5 * 128 + 5]);
  const mid = (sub[39 * 128 + 20] + sub[41 * 128 + 20]) / 2;
  assert.ok(Math.abs(proc[40 * 128 + 20] - mid) <= 1);
});
