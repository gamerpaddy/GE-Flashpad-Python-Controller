import test from 'node:test';
import assert from 'node:assert/strict';
import { PC, Palettes, fixtures, sha256, bigFrames, rgbaToRgb, synthSignal, synthDark } from './util.mjs';

const X = fixtures();
const { raw, dark } = bigFrames();

async function runCase(p, c) {
  const P = PC.defaultParams();
  Object.assign(P.win, { loPct: c.lo, hiPct: c.hi });
  Object.assign(P.clahe, { on: c.clahe, clip: c.clip, tiles: c.tiles });
  P.palette = c.pal; P.invert = c.inv;
  return p.run(P, { mode: 'full' });
}

test('whole pipeline (dark -> lines -> window -> CLAHE -> invert -> palette) == flashpad_capture_gui.render_rgb, bit-exact', async () => {
  const p = new PC.Pipeline(Palettes); p.setRaw(raw, 'xray'); p.setDark(dark);
  for (const c of X.pipeline_cases) {
    const r = await runCase(p, c);
    assert.equal(r.W, 2048); assert.equal(r.rgba.length, 2048 * 2048 * 4);
    assert.equal(sha256(rgbaToRgb(r.rgba)), c.sha, c.name);
  }
  assert.equal(sha256(p.proc), X.pipeline_final_sha);     // the "processed 16-bit data"
});

test('without a dark the pipeline detects lines on the raw frame (Python: offset is None)', async () => {
  const p = new PC.Pipeline(Palettes); p.setRaw(raw, 'xray');
  const r = await p.run(PC.defaultParams(), { mode: 'full' });
  assert.equal(sha256(rgbaToRgb(r.rgba)), X.pipeline_nodark.sha);
});

test('a dark-type frame is never dark-subtracted (as in _process)', async () => {
  const p = new PC.Pipeline(Palettes); p.setRaw(dark, 'dark'); p.setDark(dark);
  const P = PC.defaultParams(); P.lines.on = false;
  await p.run(P, { mode: 'full' });
  assert.deepEqual(p.proc, dark);
});

test('stage caching: changing only the window / palette does not recompute the processed data', async () => {
  const p = new PC.Pipeline(Palettes); p.setRaw(raw, 'xray'); p.setDark(dark);
  const P = PC.defaultParams();
  const a = await p.run(P, { mode: 'full' }); assert.equal(a.procChanged, true);
  P.win.loPct = 3; P.palette = 'Inferno';
  const b = await p.run(P, { mode: 'full' }); assert.equal(b.procChanged, false); assert.equal(b.proc, a.proc);
  P.unsharp.on = true;
  const c = await p.run(P, { mode: 'full' }); assert.equal(c.procChanged, true);
});

test('every enhancement is individually bypassable: off == identical to the plain chain', async () => {
  const p = new PC.Pipeline(Palettes); p.setRaw(raw, 'xray'); p.setDark(dark);
  const P0 = PC.defaultParams();
  await p.run(P0, { mode: 'full' });
  const ref = Uint16Array.from(p.proc);
  const P = PC.defaultParams();
  Object.assign(P.destripe, { on: false, axis: 'both' }); Object.assign(P.flatten, { on: false }); P.denoise.mode = 'none';
  Object.assign(P.unsharp, { on: false, amount: 3 }); Object.assign(P.local, { on: false, amount: 3 });
  await p.run(P, { mode: 'full' });
  assert.deepEqual(p.proc, ref);
  P.unsharp.on = true;                                    // and switching one on changes the result
  await p.run(P, { mode: 'full' });
  assert.notDeepEqual(p.proc, ref);
});

test('preview mode returns a reduced-resolution image of the same scene; full result is unaffected', async () => {
  const p = new PC.Pipeline(Palettes); p.setRaw(raw, 'xray'); p.setDark(dark);
  const P = PC.defaultParams(); P.denoise.mode = 'gauss'; P.denoise.sigma = 2;
  const full = await p.run(P, { mode: 'full' });
  const pv = await p.run(P, { mode: 'preview', f: 4 });
  assert.equal(pv.W, 512); assert.equal(pv.H, 512); assert.equal(pv.rgba.length, 512 * 512 * 4);
  assert.equal(p.proc, full.proc);                         // preview does not replace the processed frame
  // mean brightness of the preview ~ that of the full image
  const mean = (a, step) => { let s = 0, n = 0; for (let i = 0; i < a.length; i += 4 * step) { s += a[i]; n++; } return s / n; };
  assert.ok(Math.abs(mean(pv.rgba, 1) - mean(full.rgba, 64)) < 6);
});

test('geometry in the pipeline: rotate+flip applies to the processed data and the displayed image', async () => {
  const w = 64, h = 48, a = synthSignal(w, h, 3), d = synthDark(w, h, 4);
  const p = new PC.Pipeline(Palettes); p.setRaw(a, 'xray', w, h); p.setDark(d);
  const P = PC.defaultParams(); P.lines.on = false;
  await p.run(P, { mode: 'full' }); const plain = Uint16Array.from(p.proc);
  P.geom = { rot: 1, flipH: true, flipV: false };
  const r = await p.run(P, { mode: 'full' });
  assert.equal(r.W, 48); assert.equal(r.H, 64);
  const t = PC.transform(plain, w, h, P.geom);
  assert.deepEqual(p.proc, t.data);
});

test('exports: processed u16 (crop) and displayed RGB (crop) have the right shape / content', async () => {
  const p = new PC.Pipeline(Palettes); p.setRaw(raw, 'xray'); p.setDark(dark);
  const P = PC.defaultParams(); P.palette = 'Viridis';
  const r = await p.run(P, { mode: 'full' });
  const crop = [204, 204, 1844, 1844], e = p.exportProcU16(crop), rgb = p.exportRgb(crop);
  assert.equal(e.w, 1640); assert.equal(e.h, 1640); assert.equal(e.data[0], p.proc[204 * 2048 + 204]); assert.equal(e.data[1640 * 1640 - 1], p.proc[1843 * 2048 + 1843]);
  const k = (5 * 1640 + 7) * 3, s = ((204 + 5) * 2048 + 204 + 7) * 4;
  assert.deepEqual(Array.from(rgb.data.subarray(k, k + 3)), Array.from(r.rgba.subarray(s, s + 3)));
});
