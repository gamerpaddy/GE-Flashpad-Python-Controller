import test from 'node:test';
import assert from 'node:assert/strict';
import zlib from 'node:zlib';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { Png16, PC, Palettes, PYTHON, here, synthSignal } from './util.mjs';

function gray16(w, h, seed) {
  const a = new Uint16Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) a[y * w + x] = (((x * 131 + y * 7919 + seed) * 2654435761) >>> 16) ^ (x * 40 + y * 20);   // full 16-bit range, noisy
  return a;
}
function rgb8(w, h) {
  const a = new Uint8Array(w * h * 3), pal = Palettes.get('Turbo');
  for (let i = 0; i < w * h; i++) { const v = (i * 7 + (i >> 5)) & 255; a[3 * i] = pal[3 * v]; a[3 * i + 1] = pal[3 * v + 1]; a[3 * i + 2] = pal[3 * v + 2]; }
  return a;
}
function idat(png) { // concatenated IDAT payloads
  const parts = []; let o = 8;
  while (o < png.length) {
    const len = new DataView(png.buffer, png.byteOffset + o).getUint32(0), type = String.fromCharCode(...png.subarray(o + 4, o + 8));
    if (type === 'IDAT') parts.push(png.subarray(o + 8, o + 8 + len));
    o += 12 + len;
  }
  return Buffer.concat(parts);
}

test('crc32 / adler32 match zlib', () => {
  const d = Buffer.from('The quick brown fox jumps over the lazy dog'.repeat(100));
  assert.equal(Png16.crc32(d), zlib.crc32(d));
  assert.equal(Png16.crc32(d.subarray(5), Png16.crc32(d.subarray(0, 5))), zlib.crc32(d));
  assert.equal(Png16.adler32(Buffer.from('Wikipedia')), 0x11E60398);                 // the textbook Adler-32 example
});

test('stored-block zlib stream is a valid zlib stream (inflates to the input)', () => {
  for (const n of [0, 1, 65535, 65536, 200000]) {
    const d = Uint8Array.from({ length: n }, (_, i) => (i * 31) & 255);
    assert.deepEqual(new Uint8Array(zlib.inflateSync(Png16.storedZlib(d))), d, 'n=' + n);
  }
});

test('PNG16 + RGB8 files (CompressionStream and stored fallback) decode identically in Pillow/numpy', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'fp-png-')), manifest = [];
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const cases = [['g16_a', 512, 400, 1], ['g16_odd', 7, 5, 2], ['g16_one', 1, 1, 3], ['g16_wide', 1640, 3, 4]];
  for (const forceStored of [false, true]) for (const [name, w, h, seed] of cases) {
    const data = gray16(w, h, seed), png = await Png16.encode({ width: w, height: h, channels: 1, depth: 16, data }, { forceStored });
    const base = name + (forceStored ? '_stored' : '_deflate');
    writeFileSync(join(dir, base + '.png'), png); writeFileSync(join(dir, base + '.bin'), Buffer.from(data.buffer));
    manifest.push({ png: base + '.png', raw: base + '.bin', w, h, kind: 'gray16' });
    const raw = zlib.inflateSync(idat(png)); assert.equal(raw.length, (w * 2 + 1) * h);   // valid zlib, right size
    if (forceStored) assert.ok(png.length > w * h * 2);                                   // stored really is uncompressed
  }
  for (const forceStored of [false, true]) for (const [w, h] of [[300, 200], [13, 9]]) {
    const data = rgb8(w, h), png = await Png16.encode({ width: w, height: h, channels: 3, depth: 8, data }, { forceStored });
    const base = 'rgb_' + w + 'x' + h + (forceStored ? '_stored' : '_deflate');
    writeFileSync(join(dir, base + '.png'), png); writeFileSync(join(dir, base + '.bin'), Buffer.from(data.buffer));
    manifest.push({ png: base + '.png', raw: base + '.bin', w, h, kind: 'rgb8' });
  }
  writeFileSync(join(dir, 'manifest.json'), JSON.stringify(manifest));
  const r = spawnSync(PYTHON, [join(here, 'verify_png.py'), join(dir, 'manifest.json')], { encoding: 'utf8' });
  t.diagnostic(r.stdout.trim().split('\n').length + ' files verified by Pillow');
  assert.equal(r.status, 0, r.stdout + r.stderr);
});

test('full-size 2048x2048 16-bit PNG (processed frame) round-trips and compresses', async (t) => {
  const w = 2048, h = 2048, data = synthSignal(w, h, 5);
  const t0 = Date.now(), png = await Png16.encode({ width: w, height: h, channels: 1, depth: 16, data });
  t.diagnostic('encode ' + (Date.now() - t0) + ' ms, ' + (png.length / 1048576).toFixed(2) + ' MiB');
  const raw = zlib.inflateSync(idat(png));
  // un-filter and compare in JS (independent of Pillow)
  const stride = w * 2, out = new Uint8Array(stride * h);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)], o = y * stride, r = y * (stride + 1) + 1;
    for (let i = 0; i < stride; i++) {
      const a = i >= 2 ? out[o + i - 2] : 0, b = y ? out[o - stride + i] : 0;
      out[o + i] = (raw[r + i] + (f === 0 ? 0 : f === 1 ? a : f === 2 ? b : 0)) & 255;
    }
  }
  for (let i = 0; i < w * h; i += 997) assert.equal((out[2 * i] << 8) | out[2 * i + 1], data[i]);
  assert.ok(png.length < w * h * 2);
});
