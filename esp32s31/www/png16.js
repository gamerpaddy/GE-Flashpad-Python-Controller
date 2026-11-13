/* png16.js - minimal PNG encoder: 16-bit grayscale and 8-bit RGB (also 8-bit gray / RGBA via `channels`).
 * Scanlines use the best of filters None / Sub / Up per row; zlib via CompressionStream('deflate') when the browser
 * has it, otherwise a pure-JS "stored" (uncompressed) zlib stream. UMD: usable from the worker, the page and Node.
 *
 *   Png16.encode({width, height, channels: 1|3|4, depth: 8|16, data}, {forceStored}) -> Promise<Uint8Array>
 *   data: Uint8Array (depth 8) or Uint16Array holding host-order sample values (depth 16, channels 1)
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Png16 = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TABLE = (function () {
    var t = new Uint32Array(256);
    for (var n = 0; n < 256; n++) { var c = n; for (var k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
    return t;
  })();

  /** CRC-32 (IEEE). Pass the previous result as `crc` to continue over several buffers. */
  function crc32(buf, crc, start, end) {
    var c = ((crc || 0) ^ 0xffffffff) >>> 0, i = start || 0, e = end === undefined ? buf.length : end;
    for (; i < e; i++) c = (TABLE[(c ^ buf[i]) & 255] ^ (c >>> 8)) >>> 0;
    return (c ^ 0xffffffff) >>> 0;
  }
  function adler32(buf) {
    var a = 1, b = 0, n = buf.length, i = 0;
    while (i < n) {
      var e = Math.min(i + 5552, n);
      for (; i < e; i++) { a += buf[i]; b += a; }
      a %= 65521; b %= 65521;
    }
    return ((b << 16) | a) >>> 0;
  }

  /** zlib stream made of stored (uncompressed) deflate blocks. */
  function storedZlib(raw) {
    var n = raw.length, nb = Math.max(1, Math.ceil(n / 65535)), out = new Uint8Array(2 + n + nb * 5 + 4), o = 0, i, b;
    out[o++] = 0x78; out[o++] = 0x01;
    for (b = 0, i = 0; b < nb; b++) {
      var len = Math.min(65535, n - i);
      out[o++] = b === nb - 1 ? 1 : 0;
      out[o++] = len & 255; out[o++] = len >> 8; out[o++] = ~len & 255; out[o++] = (~len >> 8) & 255;
      out.set(raw.subarray(i, i + len), o); o += len; i += len;
    }
    var ad = adler32(raw);
    out[o++] = ad >>> 24; out[o++] = (ad >>> 16) & 255; out[o++] = (ad >>> 8) & 255; out[o++] = ad & 255;
    return out;
  }
  async function zlibDeflate(raw, forceStored) {
    if (!forceStored && typeof CompressionStream === 'function' && typeof Response === 'function') {
      try {
        var cs = new CompressionStream('deflate'), w = cs.writable.getWriter();
        var res = new Response(cs.readable).arrayBuffer();           // start reading first: no back-pressure deadlock
        w.write(raw).catch(function () {});
        await w.close();
        return new Uint8Array(await res);
      } catch (e) { /* fall through */ }
    }
    return storedZlib(raw);
  }

  /** filtered scanlines: [filter byte][row bytes] * height */
  function scanlines(img) {
    var w = img.width, h = img.height, ch = img.channels, depth = img.depth, bpp = ch * (depth >> 3), stride = w * bpp;
    var out = new Uint8Array((stride + 1) * h), row = new Uint8Array(stride), prev = new Uint8Array(stride), data = img.data, x, y, i;
    for (y = 0; y < h; y++) {
      if (depth === 16) { var o16 = y * w; for (x = 0; x < w; x++) { var v = data[o16 + x]; row[2 * x] = v >> 8; row[2 * x + 1] = v & 255; } }
      else row.set(data.subarray(y * stride, (y + 1) * stride));
      var s0 = 0, s1 = 0, s2 = 0;
      for (i = 0; i < stride; i++) {
        var p = row[i], a = i >= bpp ? row[i - bpp] : 0, u = prev[i], d1 = (p - a) & 255, d2 = (p - u) & 255;
        s0 += p < 128 ? p : 256 - p; s1 += d1 < 128 ? d1 : 256 - d1; s2 += d2 < 128 ? d2 : 256 - d2;
      }
      var f = s0 <= s1 && s0 <= s2 ? 0 : s1 <= s2 ? 1 : 2, oo = y * (stride + 1);
      out[oo] = f;
      if (f === 0) out.set(row, oo + 1);
      else if (f === 1) for (i = 0; i < stride; i++) out[oo + 1 + i] = (row[i] - (i >= bpp ? row[i - bpp] : 0)) & 255;
      else for (i = 0; i < stride; i++) out[oo + 1 + i] = (row[i] - prev[i]) & 255;
      var t = prev; prev = row; row = t;
    }
    return out;
  }

  function chunk(type, data) {
    var out = new Uint8Array(12 + data.length), dv = new DataView(out.buffer);
    dv.setUint32(0, data.length);
    for (var i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
    out.set(data, 8);
    dv.setUint32(8 + data.length, crc32(out, 0, 4, 8 + data.length));
    return out;
  }

  async function encode(img, opts) {
    opts = opts || {};
    var ct = { 1: 0, 3: 2, 4: 6 }[img.channels];
    if (ct === undefined || (img.depth !== 8 && !(img.depth === 16 && img.channels === 1))) throw new Error('unsupported PNG format');
    if (img.data.length < img.width * img.height * img.channels) throw new Error('PNG data too short');
    var ihdr = new Uint8Array(13), dv = new DataView(ihdr.buffer);
    dv.setUint32(0, img.width); dv.setUint32(4, img.height); ihdr[8] = img.depth; ihdr[9] = ct;
    var idat = await zlibDeflate(scanlines(img), opts.forceStored);
    var parts = [new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', idat), chunk('IEND', new Uint8Array(0))];
    var total = 0, i, o = 0;
    for (i = 0; i < parts.length; i++) total += parts[i].length;
    var png = new Uint8Array(total);
    for (i = 0; i < parts.length; i++) { png.set(parts[i], o); o += parts[i].length; }
    return png;
  }

  return { encode: encode, crc32: crc32, adler32: adler32, storedZlib: storedZlib };
});
