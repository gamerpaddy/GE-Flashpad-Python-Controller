// Shared helpers for the Node tests: loads the browser modules, reads the cv2/numpy fixtures (generating them on the
// fly with make_fixtures.py when missing or stale) and mirrors the deterministic synthetic frames of make_fixtures.py.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
export const here = dirname(fileURLToPath(import.meta.url));
export const PC = require('../../www/proc-core.js');
export const Palettes = require('../../www/palettes.js');
export const Png16 = require('../../www/png16.js');
export const PYTHON = process.env.FLASHPAD_PYTHON || 'python';

let cached = null;
export function fixtures() {
  if (cached) return cached;
  const p = join(here, 'fixtures', 'expected.json'), gen = join(here, 'make_fixtures.py');
  if (!existsSync(p) || statSync(p).mtimeMs < statSync(gen).mtimeMs) {
    const r = spawnSync(PYTHON, [gen], { encoding: 'utf8' });
    if (r.status !== 0) throw new Error('make_fixtures.py failed:\n' + r.stdout + r.stderr);
  }
  return (cached = JSON.parse(readFileSync(p, 'utf8')));
}

export function sha256(t) { return createHash('sha256').update(new Uint8Array(t.buffer, t.byteOffset, t.byteLength)).digest('hex'); }
function fromB64(s, Ctor) { const b = Buffer.from(s, 'base64'); return new Ctor(b.buffer.slice(b.byteOffset, b.byteOffset + b.length)); }
export const f32FromB64 = s => fromB64(s, Float32Array);
export const u8FromB64 = s => fromB64(s, Uint8Array);
export const u16FromB64 = s => fromB64(s, Uint16Array);
export function maxAbsDiff(a, b) { let m = 0; for (let i = 0; i < a.length; i++) { const d = Math.abs(a[i] - b[i]); if (d > m) m = d; } return m; }
export function meanAbsDiff(a, b) { let s = 0; for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]); return s / a.length; }
export function idx(mask) { const o = []; for (let i = 0; i < mask.length; i++) if (mask[i]) o.push(i); return o; }

// ---- deterministic synthetic data (integer maths only; identical to make_fixtures.py)
export function fmix32(h) {
  h ^= h >>> 16; h = Math.imul(h, 0x85ebca6b); h ^= h >>> 13; h = Math.imul(h, 0xc2b2ae35); h ^= h >>> 16; return h >>> 0;
}
export function hash2(x, y, seed) {
  return fmix32((Math.imul(x, 0x9E3779B1) + Math.imul(y, 0x85EBCA77) + Math.imul(seed, 0xC2B2AE3D)) >>> 0);
}
export function synthSignal(W, H, seed, o = {}) {
  const out = new Uint16Array(W * H), v = new Int32Array(W * H);
  const cx = Math.floor(W * 7 / 20), cy = Math.floor(H * 9 / 20), rr = Math.floor(Math.min(W, H) / 7);
  const rx0 = Math.floor(W * 12 / 20), rx1 = Math.floor(W * 15 / 20), ry0 = Math.floor(H * 3 / 20), ry1 = Math.floor(H * 7 / 20);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    let s = 1500 + ((x + y) >> 1);
    if ((x - cx) * (x - cx) + (y - cy) * (y - cy) < rr * rr) s += 1500;
    if (x >= rx0 && x < rx1 && y >= ry0 && y < ry1) s += 3000;
    const h = hash2(x, y, seed);
    v[y * W + x] = s + ((h >>> 22) & 63) + ((h >>> 11) & 63);
  }
  for (const r of o.halfRows || []) for (let x = 0; x < W; x++) v[r * W + x] = Math.floor(v[r * W + x] / 2);
  for (const c of o.halfCols || []) for (let y = 0; y < H; y++) v[y * W + c] = Math.floor(v[y * W + c] / 2);
  for (const r of o.deadRows || []) for (let x = 0; x < W; x++) v[r * W + x] = 0;
  for (const c of o.deadCols || []) for (let y = 0; y < H; y++) v[y * W + c] = 0;
  for (const [px, py, val] of o.pixels || []) v[py * W + px] = val;
  out.set(v);
  return out;
}
export function synthDark(W, H, seed) {
  const out = new Uint16Array(W * H);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) out[y * W + x] = 1789 + (x % 7) * 3 + (y % 5) * 2 + ((hash2(x, y, seed) >>> 26) & 31);
  return out;
}
export function bigFrames() { // the 2048x2048 raw + dark used by the hash-compared tests (cached per process)
  if (!bigFrames.c) {
    bigFrames.c = {
      raw: synthSignal(2048, 2048, 1, { deadRows: [48, 1093], deadCols: [783], halfRows: [300], halfCols: [1500], pixels: [[500, 500, 40000], [1500, 1700, 0]] }),
      dark: synthDark(2048, 2048, 2)
    };
  }
  return bigFrames.c;
}
export function rgbaToRgb(rgba) {
  const n = rgba.length / 4, out = new Uint8Array(n * 3);
  for (let i = 0, j = 0, k = 0; i < n; i++, j += 4, k += 3) { out[k] = rgba[j]; out[k + 1] = rgba[j + 1]; out[k + 2] = rgba[j + 2]; }
  return out;
}
