/* proc-worker.js - all pixel work runs here so the UI thread never blocks.
 *
 * main -> worker                                          worker -> main
 *   {t:'raw',  buf, ftype}   new frame (ArrayBuffer)        {t:'ready'}
 *   {t:'dark', buf|null}     dark frame or none             {t:'progress', id, text}
 *   {t:'render', id, params, mode:'full'|'preview', f}      {t:'result', id, mode, W, H, rgba, lo, hi, defects, ms,
 *   {t:'cancel'}                                                         procChanged, proc?, hist?}   (buffers transferred)
 *   {t:'export', id, kind:'raw16'|'png16'|'png8', crop}     {t:'cancelled', id} | {t:'error', id, msg}
 *                                                           {t:'exported', id, kind, w, h, buf}
 * Messages are queued; a newer render supersedes (and cancels) an older one. Long filters yield cooperatively so a
 * newer message / Cancel is noticed within ~30 ms.
 */
'use strict';
importScripts('proc-core.js', 'palettes.js', 'png16.js');

var pipe = new ProcCore.Pipeline(Palettes);
var queue = [], busy = false, active = null;

self.onmessage = function (e) {
  var m = e.data;
  if (m.t === 'cancel') {
    if (active) active.ctl.cancelled = true;
    queue = queue.filter(function (q) { return q.t !== 'render'; });
    return;
  }
  if (m.t === 'render' || m.t === 'raw' || m.t === 'dark') {
    queue = queue.filter(function (q) { return q.t !== 'render'; });         // superseded
    if (active && active.kind === 'render') active.ctl.cancelled = true;
  }
  queue.push(m);
  pump();
};

async function pump() {
  if (busy) return;
  busy = true;
  while (queue.length) {
    var m = queue.shift();
    try { await handle(m); }
    catch (err) { self.postMessage({ t: 'error', id: m.id, msg: String(err && err.message || err) }); }
  }
  busy = false;
}

function isCancel(err) { return err === ProcCore.CANCELLED || (err && err.message === ProcCore.CANCELLED); }

function makeCtl(id) {
  var last = performance.now(), lastProg = 0, c = {
    cancelled: false,
    tick: async function () {
      if (c.cancelled) throw ProcCore.CANCELLED;
      var n = performance.now();
      if (n - last > 30) { await new Promise(function (r) { setTimeout(r, 0); }); last = performance.now(); if (c.cancelled) throw ProcCore.CANCELLED; }
    },
    progress: function (text) {
      var n = performance.now();
      if (n - lastProg > 100) { lastProg = n; self.postMessage({ t: 'progress', id: id, text: text }); }
    }
  };
  return c;
}

async function handle(m) {
  if (m.t === 'raw') { pipe.setRaw(new Uint16Array(m.buf), m.ftype || 'xray'); return; }
  if (m.t === 'dark') { pipe.setDark(m.buf ? new Uint16Array(m.buf) : null); return; }
  if (m.t === 'render') {
    var ctl = makeCtl(m.id); active = { ctl: ctl, kind: 'render' };
    try {
      var r = await pipe.run(m.params, { mode: m.mode, f: m.f }, ctl), tr = [], msg;
      msg = { t: 'result', id: m.id, mode: r.mode, W: r.W, H: r.H, lo: r.lo, hi: r.hi, defects: r.defects, ms: r.ms, procChanged: r.procChanged };
      msg.rgba = r.mode === 'preview' ? r.rgba.buffer : r.rgba.slice().buffer;        // the full-size RGBA is kept for PNG export
      tr.push(msg.rgba);
      if (r.mode !== 'preview' && r.procChanged) {
        msg.proc = r.proc.slice().buffer; msg.hist = ProcCore.histogram16(r.proc).buffer; msg.procW = r.W; msg.procH = r.H;
        tr.push(msg.proc, msg.hist);
      }
      self.postMessage(msg, tr);
    } catch (err) {
      if (isCancel(err)) self.postMessage({ t: 'cancelled', id: m.id }); else throw err;
    } finally { active = null; }
    return;
  }
  if (m.t === 'export') {
    if (!pipe.proc || !pipe.rgba) throw new Error('nothing to export yet');
    var buf, w, h;
    if (m.kind === 'raw16') { var e = pipe.exportProcU16(m.crop); buf = e.data.buffer; w = e.w; h = e.h; }
    else if (m.kind === 'png16') {
      var e2 = pipe.exportProcU16(m.crop); w = e2.w; h = e2.h;
      buf = (await Png16.encode({ width: w, height: h, channels: 1, depth: 16, data: e2.data })).buffer;
    } else {
      var e3 = pipe.exportRgb(m.crop); w = e3.w; h = e3.h;
      buf = (await Png16.encode({ width: w, height: h, channels: 3, depth: 8, data: e3.data })).buffer;
    }
    self.postMessage({ t: 'exported', id: m.id, kind: m.kind, w: w, h: h, buf: buf }, [buf]);
  }
}

self.postMessage({ t: 'ready' });
