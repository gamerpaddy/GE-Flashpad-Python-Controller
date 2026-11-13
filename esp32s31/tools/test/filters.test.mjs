import test from 'node:test';
import assert from 'node:assert/strict';
import { PC, fixtures, f32FromB64, synthSignal, maxAbsDiff, meanAbsDiff } from './util.mjs';

const X = fixtures(), F = X.filters, W = F.W, H = F.H;
const u = synthSignal(W, H, 7), f = Float32Array.from(u);   // same input as make_fixtures.py (values 1500..8000)
const diag = (t, name, a, b) => t.diagnostic(name + ': max |d| = ' + maxAbsDiff(a, b).toExponential(2) + ', mean |d| = ' + meanAbsDiff(a, b).toExponential(2));

test('Gaussian blur == cv2.GaussianBlur (reflect-101, same truncated kernel) to float32 precision', (t) => {
  for (const s of [0.8, 2.0]) {
    const out = PC.gaussianBlur(f, W, H, s), ref = f32FromB64(F['gauss_' + s]);
    diag(t, 'gauss ' + s, out, ref);
    assert.ok(maxAbsDiff(out, ref) < 0.01);
  }
});

test('median 3x3 and 5x5 are EXACT vs cv2.medianBlur (16-bit, replicate borders)', async () => {
  assert.deepEqual(await PC.medianFilter(f, W, H, 3), f32FromB64(F.median3));
  assert.deepEqual(await PC.medianFilter(f, W, H, 5), f32FromB64(F.median5));
});

test('bilateral == numpy brute force (circular window, spatial sigma r/2, exact exp)', async (t) => {
  for (const [r, sr] of [[2, 80], [3, 150]]) {
    const out = await PC.bilateral(f, W, H, { radius: r, sigmaRange: sr }), ref = f32FromB64(F['bilateral_' + r + '_' + sr]);
    diag(t, 'bilateral r' + r + ' sr' + sr, out, ref);
    assert.ok(maxAbsDiff(out, ref) < 0.05 * sr / 10, 'max error ' + maxAbsDiff(out, ref));   // LUT quantisation: < 0.5 % of sigma_r
  }
});

test('non-local means == numpy brute-force NLM (patch/search windows, reflect-101 patches)', async (t) => {
  for (const [h, S, p] of [[60, 3, 1], [90, 2, 2]]) {
    const out = await PC.nlm(f, W, H, { h, search: S, patch: p }), ref = f32FromB64(F['nlm_' + h + '_' + S + '_' + p]);
    diag(t, 'nlm h' + h + ' S' + S + ' p' + p, out, ref);
    assert.ok(maxAbsDiff(out, ref) < 0.5, 'max error ' + maxAbsDiff(out, ref));                 // exp-LUT quantisation (<=0.12 % weight error)
    assert.ok(meanAbsDiff(out, ref) < 0.05);
  }
});

test('NLM reduces noise on a noisy flat patch and can be cancelled', async () => {
  const w = 96, h = 96, noisy = new Float32Array(w * h);
  for (let i = 0; i < noisy.length; i++) noisy[i] = 1000 + (((i * 2654435761) >>> 0) % 61) - 30;     // flat + uniform noise (sd ~17.6)
  const sd = a => { let s = 0, s2 = 0; for (const v of a) { s += v; s2 += v * v; } const m = s / a.length; return Math.sqrt(s2 / a.length - m * m); };
  const out = await PC.nlm(noisy, w, h, { h: 40, search: 3, patch: 1 });
  assert.ok(sd(out) < 0.5 * sd(noisy), sd(out) + ' vs ' + sd(noisy));
  let ticks = 0;
  const ctl = { cancelled: false, progress() {}, tick() { if (++ticks > 3) throw new Error(PC.CANCELLED); return Promise.resolve(); } };
  await assert.rejects(PC.nlm(noisy, w, h, { h: 40, search: 3, patch: 1 }, ctl), /cancelled/);
});

test('unsharp mask and multi-scale local contrast == numpy/cv2 formulas', (t) => {
  const us = PC.unsharp(f, W, H, 1.5, 1.2), ru = f32FromB64(F['unsharp_1.5_1.2']);
  diag(t, 'unsharp', us, ru); assert.ok(maxAbsDiff(us, ru) < 0.05);
  const lc = PC.localContrast(f, W, H, 0.8, 1, 3), rl = f32FromB64(F['local_0.8_1_3']);
  diag(t, 'local contrast', lc, rl); assert.ok(maxAbsDiff(lc, rl) < 0.05);
});

test('destripe == numpy (row / column median minus running-median baseline)', (t) => {
  for (const [ax, win, st, key] of [['both', 16, 0.8, 'destripe_both_16_0.8'], ['rows', 8, 1.0, 'destripe_rows_8_1']]) {
    const out = PC.destripe(f, W, H, ax, win, st), ref = f32FromB64(F[key]);
    diag(t, key, out, ref); assert.ok(maxAbsDiff(out, ref) < 1e-3);
  }
});

test('destripe removes injected row and column stripes (sparse outlier lines, smooth content kept)', () => {
  const w = 128, h = 128, a = new Float32Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) a[y * w + x] = 2000 + x + y + ((y % 7 === 3) ? 60 : 0) + ((x % 11 === 5) ? 30 : 0);
  const out = PC.destripe(a, w, h, 'both', 6, 1);
  const rowStep = (arr, x) => { let s = 0; for (let y = 20; y < h - 20; y++) s += Math.abs(arr[y * w + x] - arr[(y - 1) * w + x] - 1); return s / (h - 40); };
  const colStep = (arr, y) => { let s = 0; for (let x = 20; x < w - 20; x++) s += Math.abs(arr[y * w + x] - arr[y * w + x - 1] - 1); return s / (w - 40); };
  assert.ok(rowStep(a, 60) > 15 && rowStep(out, 60) < 2, 'rows: ' + rowStep(a, 60) + ' -> ' + rowStep(out, 60));
  assert.ok(colStep(a, 60) > 4 && colStep(out, 60) < 2, 'cols: ' + colStep(a, 60) + ' -> ' + colStep(out, 60));
});

test('large-sigma blur (box-downsample + Gaussian + bilinear) approximates the exact Gaussian; flatten matches', (t) => {
  const B = X.blur_large, big = Float32Array.from(synthSignal(B.W, B.H, 9));
  const bg = PC.blurLarge(big, B.W, B.H, B.sigma), ref = f32FromB64(B.bg);
  const rel = meanAbsDiff(bg, ref) / (ref.reduce((s, v) => s + v, 0) / ref.length);
  t.diagnostic('blurLarge sigma ' + B.sigma + ': mean rel error ' + rel.toExponential(2) + ', max abs ' + maxAbsDiff(bg, ref).toFixed(2));
  assert.ok(rel < 0.01);
  const fl = PC.flatten(big, B.W, B.H, B.sigma, 0.8), rf = f32FromB64(B['flat_0.8']);
  const rel2 = meanAbsDiff(fl, rf) / (rf.reduce((s, v) => s + v, 0) / rf.length);
  t.diagnostic('flatten: mean rel error ' + rel2.toExponential(2));
  assert.ok(rel2 < 0.02);
});

test('flatten removes a smooth illumination gradient', () => {
  const w = 256, h = 256, a = new Float32Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) a[y * w + x] = (1000 + 6 * x) * (y > 100 && y < 150 && x > 100 && x < 150 ? 1.3 : 1);
  const out = PC.flatten(a, w, h, 40, 1);
  const spread = arr => { let mn = 1e9, mx = -1e9; for (let y = 20; y < 80; y++) for (let x = 20; x < 236; x++) { const v = arr[y * w + x]; mn = Math.min(mn, v); mx = Math.max(mx, v); } return mx - mn; };
  assert.ok(spread(out) < 0.35 * spread(a), spread(out) + ' vs ' + spread(a));
});
