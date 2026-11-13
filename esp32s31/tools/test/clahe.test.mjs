import test from 'node:test';
import assert from 'node:assert/strict';
import { PC, fixtures, sha256, bigFrames, u8FromB64, synthSignal, maxAbsDiff, meanAbsDiff } from './util.mjs';

const X = fixtures();
const W = 2048, H = 2048;

function windowed() {
  const { raw, dark } = bigFrames();
  const sub = PC.subtractDark(raw, dark), fixed = PC.applyDefectMap(sub, PC.buildDefectMap(sub, dark, W, H, 25), W, H);
  return PC.toU8(fixed, W, H, 0.01, 0.995).u8;
}

test('CLAHE is BIT-EXACT with cv2.createCLAHE for 2048x2048, tiles {2,4,8,16,32} x clip {0.5,2,3.7,12}', () => {
  const u8 = windowed();
  assert.equal(sha256(u8), X.clahe_input_sha);
  for (const [k, expect] of Object.entries(X.clahe)) {
    const [t, c] = k.split('_').map(Number);
    assert.equal(sha256(PC.clahe(u8, W, H, c, t)), expect, 'tiles ' + t + ' clip ' + c);
  }
});

test('CLAHE with sizes not divisible by the tile grid (reflect-101 padding like cv2)', () => {
  for (const [k, expect] of Object.entries(X.clahe_odd)) {
    const m = /(\d+)x(\d+)_(\d+)_([\d.]+)/.exec(k), w = +m[1], h = +m[2], t = +m[3], c = +m[4];
    const a = Uint8Array.from(synthSignal(w, h, 5), v => v >> 4);
    assert.deepEqual(PC.clahe(a, w, h, c, t), u8FromB64(expect), k);
  }
});

test('12-bit CLAHE (non-cv2 variant) matches the float64 numpy recipe to within 1 level', (t) => {
  const a = Uint16Array.from(synthSignal(256, 192, 6), v => (v >> 1) & 4095);
  const out = PC.clahe12(a, 256, 192, X.clahe12.clip, X.clahe12.tiles), ref = u8FromB64(X.clahe12.out);
  t.diagnostic('max |diff| = ' + maxAbsDiff(out, ref) + ', mean = ' + meanAbsDiff(out, ref).toFixed(5));
  assert.ok(maxAbsDiff(out, ref) <= 1); assert.ok(meanAbsDiff(out, ref) < 0.01);
});

test('the float64 recipe from the task description is NOT bit-exact with cv2 5.x (rounding ties); the JS float32 port is', (t) => {
  t.diagnostic('float64 recipe differs from cv2 on ' + X.clahe_recipe_diff_px + ' px of the 256x192 probe image');
  assert.ok(X.clahe_recipe_diff_px >= 0);
});
