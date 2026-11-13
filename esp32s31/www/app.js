/* FlashPad controller UI. Vanilla JS, no dependencies. All pixel work happens in proc-worker.js (see proc-core.js).
 * Contract: esp32s31/docs/API.md. Sections: helpers - state - API/poll - capture/safety/dark panels - settings/log -
 * processing sidebar - worker/render scheduling - viewer - histogram/ROI/profile - saves - init. */
(function () {
  'use strict';
  var PC = ProcCore;
  var DIM = 2048, TICKS_PER_MS = 26.3e3;

  /* ============================================================== helpers */
  function $(id) { return document.getElementById(id); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function merge(dst, src) { // deep-merge plain objects (arrays replaced)
    for (var k in src) {
      if (src[k] && typeof src[k] === 'object' && !Array.isArray(src[k]) && dst[k] && typeof dst[k] === 'object') merge(dst[k], src[k]);
      else dst[k] = src[k];
    }
    return dst;
  }
  function store(k, v) {
    try { if (v === undefined) return JSON.parse(localStorage.getItem('fp.' + k)); localStorage.setItem('fp.' + k, JSON.stringify(v)); } catch (e) { /* private mode */ }
    return null;
  }
  function h(tag, attrs) {
    var e = document.createElement(tag), i;
    for (var k in attrs || {}) {
      var v = attrs[k];
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v);
      else if (v === true) e.setAttribute(k, '');
      else if (v !== false && v != null) e.setAttribute(k, v);
    }
    for (i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (Array.isArray(c)) c.forEach(function (x) { if (x != null) e.append(x); }); else if (c != null) e.append(c);
    }
    return e;
  }
  function fmt(n, d) { return (n === undefined || n === null || isNaN(n)) ? '–' : Number(n).toLocaleString('en-US', { maximumFractionDigits: d === undefined ? 0 : d, minimumFractionDigits: d || 0 }); }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function stamp(d) { return d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate()) + '_' + pad2(d.getHours()) + pad2(d.getMinutes()) + pad2(d.getSeconds()); }
  var msgTimer = 0;
  function notify(text, kind) {
    var m = $('msg'); m.textContent = text; m.className = kind || '';
    clearTimeout(msgTimer);
    if (kind !== 'err') msgTimer = setTimeout(function () { m.textContent = ''; m.className = ''; }, 7000);
  }
  function confirmDlg(title, body, ok, danger) {
    return new Promise(function (res) {
      var d = $('dlg');
      if (!d.showModal) { res(window.confirm(title + ' - ' + body)); return; }
      $('dlg-title').textContent = title; $('dlg-body').textContent = body; $('dlg-ok').textContent = ok || 'OK';
      $('dlg-ok').className = 'btn ' + (danger ? 'danger' : 'primary'); d.classList.toggle('danger', !!danger);
      function done(v) { $('dlg-ok').onclick = $('dlg-cancel').onclick = d.oncancel = null; if (d.open) d.close(); res(v); }
      $('dlg-ok').onclick = function (e) { e.preventDefault(); done(true); };
      $('dlg-cancel').onclick = function (e) { e.preventDefault(); done(false); };
      d.oncancel = function (e) { e.preventDefault(); done(false); };           // Esc
      d.showModal(); if (danger) $('dlg-cancel').focus();
    });
  }
  async function api(path, body) {
    var opt = body === undefined ? { cache: 'no-store' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    var r = await fetch(path, opt), j = null;
    try { j = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok || (j && j.ok === false)) throw new Error((j && j.error) || ('HTTP ' + r.status));
    return j;
  }
  async function fetchBin(url, onProgress) {
    var r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) { var t = ''; try { t = (await r.json()).error; } catch (e) { /* */ } throw new Error(t || ('HTTP ' + r.status)); }
    var len = +r.headers.get('Content-Length') || DIM * DIM * 2, buf = new Uint8Array(len), got = 0, rd = r.body.getReader();
    for (;;) {
      var x = await rd.read();
      if (x.done) break;
      if (got + x.value.length > len) throw new Error('response longer than announced');
      buf.set(x.value, got); got += x.value.length; onProgress(got / len);
    }
    if (got !== len) throw new Error('short read: ' + got + ' of ' + len + ' bytes');
    return { buf: buf, headers: r.headers };
  }

  /* ============================================================== state */
  var params = merge(PC.defaultParams(), store('params') || {});
  var S = {
    status: null, online: false, frame: null, loadedSeq: -1, loading: false, retryAt: 0, firstFrame: true,
    darkSource: store('darksrc') === 'none' ? 'none' : 'local', deviceDark: null, localDark: null, loadingDark: false,   // a 'local' dark does not survive a reload
    proc: null, procW: DIM, procH: DIM, hist: null, lo: 0, hi: 65535, defects: { rows: [], cols: [], npix: 0 },
    rid: 0, inflight: null, again: null, fullTimer: 0, lastGood: clone(params), haveImage: false, xid: 1e9, markKind: 'p', lastErr: ''
  };
  var worker = null, exportWaiters = {};
  window.fp = { S: S, params: function () { return params; }, V: null };      // handle for debugging / the browser tests

  /* ============================================================== status polling + header */
  async function poll() {
    var t0 = performance.now();
    try {
      var s = await api('/api/status');
      S.online = true; S.status = s;
      renderStatus(s); sideEffects(s);
    } catch (e) {
      S.online = false; renderOffline(e);
    }
    setTimeout(poll, Math.max(S.online ? 250 - (performance.now() - t0) : 1000, 30));
  }
  function setPill(id, text, cls, title) { var e = $(id); e.textContent = text; e.className = 'pill' + (id === 'st-xray' ? ' xr' : '') + (cls ? ' ' + cls : ''); if (title) e.title = title; }
  function renderOffline(e) {
    setPill('st-conn', 'ESP offline', 'bad', String(e && e.message));
    ['st-link', 'st-det', 'st-sess'].forEach(function (i) { setPill(i, '–', ''); });
  }
  function renderStatus(s) {
    setPill('st-conn', 'ESP online · fw ' + s.fw + ' · up ' + fmt(s.uptime_s) + ' s', 'ok');
    var l = s.link || {};
    setPill('st-link', l.up ? ('link ' + l.speed + ' Mbps ' + l.duplex) : 'link DOWN', l.up ? (l.speed === 100 ? 'ok' : 'warn') : 'bad', 'Ethernet to the detector: ' + (l.mode || '') + ' ' + (l.ip || ''));
    var d = s.detector || {};
    setPill('st-det', 'detector ' + d.state, d.state === 'ready' ? 'ok' : (d.state === 'busy' || d.state === 'connecting' || d.state === 'discovering') ? 'warn' : 'bad',
      [d.model, d.serial, d.ip, 'fw ' + d.fw].join(' · '));
    setPill('st-sess', (d.setup_mode || '–') + (d.script7_done ? ' · Script7 ✓' : ''), d.script7_done ? 'ok' : '', 'Detector setup mode');
    var x = s.xray || {}, armed = !!x.master_enabled;
    setPill('st-xray', x.relay_on ? 'X-RAY RELAY ON' : armed ? 'X-RAY OUTPUT ENABLED' : 'X-RAY OUTPUT: disabled', armed || x.relay_on ? 'on' : '');
    document.body.classList.toggle('armed', armed);
    var c = s.capture || {};
    $('cap-state').textContent = c.state; $('cap-step').textContent = c.error ? ('ERROR: ' + c.error) : (c.step || '');
    $('cap-bar').max = c.blocks_total || 2048; $('cap-bar').value = c.blocks || 0;
    $('cap-blocks').textContent = (c.blocks || 0) + ' / ' + (c.blocks_total || 2048);
    if (c.state === 'error' && c.error && c.error !== S.lastErr) { S.lastErr = c.error; notify('Capture failed: ' + c.error, 'err'); }
    if (c.state !== 'error') S.lastErr = '';
    renderCapturePanel(); renderSafety(); renderDarkPanel();
    if ($('cfg-validate')) renderValidate(s.validate);
  }
  function sideEffects(s) {
    var f = s.frame || {};
    if (f.valid && f.seq !== S.loadedSeq && !S.loading && Date.now() >= S.retryAt) loadFrame(f.seq);
    if (S.darkSource === 'device') ensureDeviceDark();
  }

  /* ============================================================== capture panel */
  var cap = { win: $('cap-win'), unit: $('cap-unit'), scrubs: $('cap-scrubs'), exp: $('cap-exp'), dly: $('cap-dly') };
  function capVals() {
    var ms = parseFloat(cap.win.value) * (cap.unit.value === 's' ? 1000 : 1);
    return { window_ms: Math.round(ms), scrubs: parseInt(cap.scrubs.value, 10), exposure_ms: parseInt(cap.exp.value, 10), fire_delay_ms: parseInt(cap.dly.value, 10) };
  }
  function capError(type) {
    var v = capVals(), mx = (S.status && S.status.xray && S.status.xray.max_pulse_ms) || 3000;
    if (!(v.window_ms >= 20 && v.window_ms <= 160000)) return 'Window must be 20 ms … 160 s.';
    if (!(v.scrubs >= 0 && v.scrubs <= 255)) return 'Scrubs must be 0 … 255.';
    if (type === 'xray') {
      if (!(v.exposure_ms >= 1 && v.exposure_ms <= mx)) return 'Exposure must be 1 … ' + mx + ' ms (relay max pulse).';
      if (!(v.fire_delay_ms >= 0 && v.fire_delay_ms <= 5000)) return 'Fire delay must be 0 … 5000 ms.';
      var need = v.fire_delay_ms + v.exposure_ms + 200;
      if (v.window_ms < need) return 'Window must be ≥ fire delay + exposure + 200 ms = ' + need + ' ms.';
    }
    return '';
  }
  function effectiveDark() {
    if (S.darkSource === 'device') return S.status && S.status.dark && S.status.dark.valid ? S.status.dark : null;
    if (S.darkSource === 'local') return S.localDark ? S.localDark.meta : null;
    return null;
  }
  function darkMismatch(dm, win, scrubs) {
    if (!dm) return '';
    var dw = dm.window_ms !== undefined ? dm.window_ms : Math.round(dm.window_ticks / TICKS_PER_MS);
    if (Math.abs(dw - win) > 0.5 || (dm.scrubs || 0) !== scrubs)
      return 'Dark was taken with window ' + fmt(dw) + ' ms / ' + (dm.scrubs || 0) + ' scrubs, but the window is ' + fmt(win) + ' ms / ' + scrubs + ' scrubs. Dark subtraction will be inexact.';
    return '';
  }
  function renderCapturePanel() {
    var s = S.status, v = capVals(), running = false, ready = false;
    if (s) { running = ['idle', 'done', 'error', 'aborted'].indexOf(s.capture.state) < 0; ready = s.detector.state === 'ready'; }
    var ticks = Math.round(v.window_ms * TICKS_PER_MS);
    $('cap-derived').textContent = isNaN(ticks) ? '–' : 'Window = ' + fmt(ticks) + ' ticks (ms × 26.3 k) · X-ray needs ≥ ' + fmt((v.fire_delay_ms || 0) + (v.exposure_ms || 0) + 200) + ' ms';
    var ex = capError('xray'), ed = capError('dark'), w = [];
    var mm = darkMismatch(effectiveDark(), v.window_ms, v.scrubs);
    if (mm) w.push(mm);
    if (ex && !ed) w.push(ex);
    if (s && s.xray && !s.xray.master_enabled) w.push('X-ray master switch is OFF: an X-RAY capture runs the sequence but never pulses the relay (fire the source externally).');
    var el = $('cap-warn'); el.hidden = !w.length; el.textContent = w.join('  '); el.className = 'warn' + (ex && !ed ? ' err' : '');
    $('btn-xray').disabled = !S.online || running || !ready || !!ex;
    $('btn-dark').disabled = !S.online || running || !ready || !!ed || !!(s && s.dark && s.dark.saving);
    $('btn-abort').disabled = !S.online;
    $('btn-reinit').disabled = !S.online || (s && s.detector.state === 'discovering');
    $('cap-info').textContent = s && s.dark && s.dark.saving ? 'Dark is being saved to flash – capturing is blocked until it finishes.' : (s && !ready ? 'Detector is ' + s.detector.state + '.' : '');
    renderViewerInfo();
  }
  async function doCapture(type) {
    var v = capVals(), err = capError(type);
    if (err) { notify(err, 'err'); return; }
    var x = S.status && S.status.xray;
    if (type === 'xray' && x && x.master_enabled && $('sf-confirm').checked) {
      var ok = await confirmDlg('Fire X-ray?', 'X-ray output is ENABLED. This capture will pulse the relay for ' + v.exposure_ms + ' ms, ' + v.fire_delay_ms + ' ms after the window opens.\n\nMake sure the area is clear and the generator interlock is in the state you intend.', 'Capture + FIRE', true);
      if (!ok) return;
    }
    var body = { type: type, window_ms: v.window_ms, scrubs: v.scrubs };
    if (type === 'xray') { body.exposure_ms = v.exposure_ms; body.fire_delay_ms = v.fire_delay_ms; }
    store('cap', { win: cap.win.value, unit: cap.unit.value, scrubs: cap.scrubs.value, exp: cap.exp.value, dly: cap.dly.value });
    try { await api('/api/capture', body); notify('Capture started (' + type + ').', 'ok'); } catch (e) { notify('Capture refused: ' + e.message, 'err'); }
  }
  $('btn-xray').onclick = function () { doCapture('xray'); };
  $('btn-dark').onclick = function () { doCapture('dark'); };
  $('btn-abort').onclick = async function () { try { await api('/api/abort', {}); notify('Aborted – relay OFF.', 'ok'); } catch (e) { notify('Abort failed: ' + e.message, 'err'); } };
  $('btn-reinit').onclick = async function () {
    if (!(await confirmDlg('Re-initialise the detector session?', 'Drops the session and redoes discovery, port setup and Script7. Takes a few seconds; a running capture is aborted.', 'Re-init'))) return;
    try { await api('/api/reinit', {}); notify('Re-initialising…', 'ok'); } catch (e) { notify('Re-init failed: ' + e.message, 'err'); }
  };
  [cap.win, cap.unit, cap.scrubs, cap.exp, cap.dly].forEach(function (e) { e.addEventListener('input', renderCapturePanel); e.addEventListener('change', function () { renderCapturePanel(); store('cap', { win: cap.win.value, unit: cap.unit.value, scrubs: cap.scrubs.value, exp: cap.exp.value, dly: cap.dly.value }); }); });

  /* ============================================================== safety panel */
  function renderSafety() {
    var x = S.status && S.status.xray; if (!x) return;
    var m = $('sf-master'); m.checked = !!x.master_enabled;
    $('sf-master-l').textContent = x.master_enabled ? 'X-RAY OUTPUT ENABLED' : 'X-ray output master enable (off)';
    $('p-safety').classList.toggle('armed', !!x.master_enabled);
    var p = $('sf-pill'); p.textContent = x.master_enabled ? 'ENABLED' : 'disabled'; p.className = 'pill xr small' + (x.master_enabled ? ' on' : '');
    $('sf-relay').textContent = x.relay_on ? 'ON (firing)' : 'off';
    $('sf-gpio').textContent = 'GPIO' + x.gpio + ' active-' + (x.active_high ? 'high' : 'low') + ', max ' + x.max_pulse_ms + ' ms';
    $('sf-fires').textContent = x.fires + (x.fires ? ' (last ' + x.last_fire_ms + ' ms)' : '');
    var busy = S.status.capture.state !== 'idle' && ['done', 'error', 'aborted'].indexOf(S.status.capture.state) < 0;
    $('btn-test').disabled = !x.master_enabled || x.relay_on || busy;
    $('sf-master').disabled = !S.online;
  }
  $('sf-master').addEventListener('change', async function (e) {
    var want = e.target.checked;
    e.target.checked = !want;                                          // the switch follows the DEVICE state only
    if (want && !(await confirmDlg('ENABLE X-RAY OUTPUT?', 'While enabled, X-RAY captures pulse the relay (GPIO ' + (S.status.xray.gpio) + ') and the generator will fire.\n\nOnly enable this with the source safely arranged. It switches itself off after the idle timeout and after every reboot.', 'Enable output', true))) return;
    try { await api('/api/xray/master', { enabled: want }); notify(want ? 'X-RAY OUTPUT ENABLED' : 'X-ray output disabled.', want ? 'err' : 'ok'); } catch (err) { notify('Master switch: ' + err.message, 'err'); }
  });
  $('btn-test').onclick = async function () {
    var ms = parseInt($('sf-ms').value, 10);
    if (!(ms >= 1 && ms <= 500)) { notify('Test pulse must be 1 … 500 ms.', 'err'); return; }
    if (!(await confirmDlg('Fire a ' + ms + ' ms test pulse?', 'The relay output will switch on for ' + ms + ' ms. Do not do this with an X-ray source connected unless you intend it to fire.', 'Pulse', true))) return;
    try { await api('/api/xray/test', { ms: ms }); notify('Test pulse sent.', 'ok'); } catch (e) { notify('Test pulse: ' + e.message, 'err'); }
  };

  /* ============================================================== dark panel */
  function setDarkSource(src, quiet) {
    S.darkSource = src; store('darksrc', src === 'none' ? 'none' : 'local');
    $$('input[name=darksrc]').forEach(function (r) { r.checked = r.value === src; });
    if (!quiet) pushDark(true);
    renderDarkPanel(); renderCapturePanel();
  }
  function darkU16() {
    if (S.darkSource === 'local') return S.localDark ? S.localDark.u16 : null;
    if (S.darkSource === 'device') return S.deviceDark ? S.deviceDark.u16 : null;
    return null;
  }
  function pushDark(render) {
    var d = darkU16();
    if (worker) worker.postMessage({ t: 'dark', buf: d ? d.buffer : null });
    if (render && S.frame) requestRender('full');
  }
  function renderDarkPanel() {
    var s = S.status, d = s && s.dark, t;
    if (S.darkSource === 'none') t = 'No dark subtraction.';
    else if (S.darkSource === 'local') t = S.localDark ? ('Local dark: ' + S.localDark.meta.type + ' frame #' + S.localDark.meta.seq + ', window ' + fmt(S.localDark.meta.window_ms) + ' ms, ' + (S.localDark.meta.scrubs || 0) + ' scrubs, mean ' + fmt(S.localDark.meta.mean, 1)) : 'No local dark picked yet – use "Use last frame as dark".';
    else t = d && d.valid ? ('Device dark: window ' + fmt(d.window_ms) + ' ms, ' + d.scrubs + ' scrubs, mean ' + fmt(d.mean, 1) + ' σ ' + fmt(d.std, 1) + ', ' + fmt(d.age_s) + ' s old' + (d.saving ? ' (saving to flash…)' : '') + (S.deviceDark ? '' : ' – downloading…')) : 'The device has no stored dark yet – capture one.';
    $('dk-info').textContent = t;
    var w = darkMismatch(effectiveDark(), capVals().window_ms, capVals().scrubs), el = $('dk-warn'); el.hidden = !w; el.textContent = w;
    $('btn-dk-local').disabled = !S.frame;
    $('btn-dk-save').disabled = !S.online || !S.frame || !!(d && d.saving);
  }
  $$('input[name=darksrc]').forEach(function (r) { r.addEventListener('change', function () { if (r.checked) setDarkSource(r.value); }); });
  $('btn-dk-local').onclick = async function () {
    if (!S.frame) return;
    if (S.frame.meta.type !== 'dark' && !(await confirmDlg('Use an X-ray frame as dark?', 'The displayed frame is an X-ray exposure, not a dark capture. Use it as dark anyway?', 'Use as dark'))) return;
    S.localDark = { u16: S.frame.u16, meta: clone(S.frame.meta) }; setDarkSource('local'); notify('Frame #' + S.frame.meta.seq + ' is now the local dark.', 'ok');
  };
  $('btn-dk-save').onclick = async function () {
    if (!(await confirmDlg('Replace the device dark?', 'Copies the device\'s current frame into its dark store in flash (takes a while, capturing is blocked meanwhile).', 'Save as dark'))) return;
    try { await api('/api/dark/from_frame', {}); notify('Saving the current frame as the stored dark…', 'ok'); } catch (e) { notify('Dark save: ' + e.message, 'err'); }
  };
  async function ensureDeviceDark() {
    var d = S.status && S.status.dark;
    if (!d || !d.valid || d.saving || S.loadingDark || Date.now() < S.retryAt) return;
    var key = [d.crc32, d.window_ticks, d.scrubs].join('|');
    if (S.deviceDark && S.deviceDark.key === key) return;
    S.loadingDark = true;
    try {
      var r = await fetchBin('/api/dark', function (p) { dlProgress('Downloading dark…', p); });
      S.deviceDark = { key: key, u16: new Uint16Array(r.buf.buffer), meta: { type: 'dark', seq: 0, window_ms: d.window_ms, scrubs: d.scrubs, mean: d.mean } };
      dlHide();
      if (S.darkSource === 'device') pushDark(true);
    } catch (e) { dlHide(); notify('Dark download: ' + e.message, 'err'); S.retryAt = Date.now() + 3000; }
    S.loadingDark = false;
  }

  /* ============================================================== frame download */
  function dlProgress(text, p) { $('dl').hidden = false; $('dl-text').textContent = text + ' ' + Math.round(p * 100) + ' %'; $('dl-bar').value = p; }
  function dlHide() { $('dl').hidden = true; }
  async function loadFrame(seq) {
    S.loading = true;
    try {
      var r = await fetchBin('/api/frame', function (p) { dlProgress('Downloading frame', p); });
      var hd = function (k) { return r.headers.get(k); }, crc = (Png16.crc32(r.buf) >>> 0).toString(16).padStart(8, '0');
      if (hd('X-Frame-Crc32') && parseInt(hd('X-Frame-Crc32'), 16) !== parseInt(crc, 16)) notify('Frame CRC mismatch (device ' + hd('X-Frame-Crc32') + ', received ' + crc + ') - the download may be corrupt.', 'err');
      var gseq = +hd('X-Frame-Seq') || seq, sf = (S.status && S.status.frame && S.status.frame.seq === gseq) ? S.status.frame : {};   // status.frame backs up missing headers
      var num = function (h, f) { var v = hd(h); return v !== null && v !== '' && !isNaN(+v) ? +v : (f === undefined ? 0 : f); };
      var meta = {
        seq: gseq, type: hd('X-Frame-Type') || sf.type || 'xray', window_ms: num('X-Window-Ms', sf.window_ms), window_ticks: num('X-Window-Ticks', sf.window_ticks), scrubs: num('X-Scrubs', sf.scrubs),
        exposure_ms: num('X-Exposure-Ms', sf.exposure_ms), crc32: crc, mean: num('X-Frame-Mean', sf.mean), std: num('X-Frame-Std', sf.std), image_id: num('X-Image-Id', sf.image_id)
      };
      S.frame = { bytes: r.buf, u16: new Uint16Array(r.buf.buffer), meta: meta, stamp: stamp(new Date()) };
      S.loadedSeq = meta.seq;
      if (meta.type === 'dark' && !S.firstFrame && S.darkSource !== 'none') {
        S.localDark = { u16: S.frame.u16, meta: clone(meta) }; S.darkSource = 'local';
        $$('input[name=darksrc]').forEach(function (x) { x.checked = x.value === 'local'; });
        notify('Dark frame #' + meta.seq + ' captured – using it as the local dark.', 'ok');
      }
      S.firstFrame = false;
      worker.postMessage({ t: 'raw', buf: S.frame.u16.buffer, ftype: meta.type });
      pushDark(false);
      $('empty').hidden = true; V.needFit = !S.haveImage;
      requestRender('full');
      renderViewerInfo(); renderDarkPanel();
    } catch (e) { notify('Frame download failed: ' + e.message, 'err'); S.retryAt = Date.now() + 2500; }
    dlHide(); S.loading = false;
  }
  function renderViewerInfo() {
    var f = S.frame, el = $('frame-info'), w = $('frame-warn');
    if (!f) { el.textContent = S.online ? '–' : ''; w.hidden = true; return; }
    var m = f.meta, d = S.darkSource === 'none' ? 'no dark' : (darkU16() ? (S.darkSource + ' dark') : 'dark not loaded');
    el.textContent = m.type.toUpperCase() + ' frame #' + m.seq + ' · window ' + fmt(m.window_ms) + ' ms · ' + m.scrubs + ' scrubs' + (m.type === 'xray' ? ' · exposure ' + m.exposure_ms + ' ms' : '') +
      ' · raw mean ' + fmt(m.mean, 1) + ' σ ' + fmt(m.std, 1) + ' · CRC ' + m.crc32 + ' · ' + d + (m.type === 'dark' ? ' (dark frames are not dark-subtracted)' : '');
    var mm = m.type === 'xray' ? darkMismatch(effectiveDark(), m.window_ms, m.scrubs) : '';
    w.hidden = !mm; w.textContent = mm ? 'This frame: ' + mm : '';
  }

  /* ============================================================== settings + log */
  var CFG = [
    ['detector_ip', 'Detector IP', 'text'], ['link_mode', 'Link mode', ['adv100fd', 'forced100fd', 'auto']], ['setup_mode', 'Setup mode', ['full', 'session', 'fast', 'combined']],
    ['fire_trigger', 'Fire trigger', ['window_open', 'after_execute']], ['early_close', 'Early close (experimental)', 'check'],
    ['relay_gpio', 'Relay GPIO', 'number'], ['relay_active_high', 'Relay active-high', 'check'], ['relay_max_pulse_ms', 'Max pulse (ms)', 'number'], ['xray_idle_disable_min', 'Master idle off (min)', 'number'],
    ['ap_ssid', 'AP SSID', 'text'], ['ap_pass', 'AP password (write-only)', 'password'], ['sta_ssid', 'STA SSID', 'text'], ['sta_pass', 'STA password (write-only)', 'password'],
    ['defaults.window_ms', 'Default window (ms)', 'number'], ['defaults.scrubs', 'Default scrubs', 'number'], ['defaults.exposure_ms', 'Default exposure (ms)', 'number'], ['defaults.fire_delay_ms', 'Default fire delay (ms)', 'number']
  ];
  var cfgOrig = null;
  function cfgGet(o, k) { return k.indexOf('.') < 0 ? o[k] : (o[k.split('.')[0]] || {})[k.split('.')[1]]; }
  function buildCfg() {
    var g = $('cfg-grid');
    CFG.forEach(function (r) {
      var id = 'cfg-' + r[0].replace('.', '-'), el;
      if (Array.isArray(r[2])) el = h('select', { id: id }, r[2].map(function (o) { return h('option', { value: o, text: o }); }));
      else if (r[2] === 'check') el = h('input', { type: 'checkbox', id: id });
      else el = h('input', { type: r[2], id: id, autocomplete: 'off' });
      g.append(h('label', { for: id, text: r[1] }), el);
    });
  }
  async function loadCfg() {
    try {
      var c = await api('/api/config'); cfgOrig = c;
      CFG.forEach(function (r) {
        var el = $('cfg-' + r[0].replace('.', '-')), v = cfgGet(c, r[0]);
        if (r[2] === 'check') el.checked = !!v; else el.value = v === undefined ? '' : v;
      });
      $('cfg-msg').textContent = '';
      var d = c.defaults;
      if (d && !store('cap')) { cap.win.value = d.window_ms; cap.unit.value = 'ms'; cap.scrubs.value = d.scrubs; cap.exp.value = d.exposure_ms; cap.dly.value = d.fire_delay_ms; renderCapturePanel(); }
    } catch (e) { $('cfg-msg').textContent = 'Could not load settings: ' + e.message; }
  }
  $('cfg-form').addEventListener('submit', async function (e) {
    e.preventDefault();
    if (!cfgOrig) return;
    var body = {};
    CFG.forEach(function (r) {
      var el = $('cfg-' + r[0].replace('.', '-')), v = r[2] === 'check' ? el.checked : r[2] === 'number' ? Number(el.value) : el.value;
      if (r[2] === 'password' ? v !== '' : v !== cfgGet(cfgOrig, r[0])) {
        if (r[0].indexOf('.') < 0) body[r[0]] = v; else { var p = r[0].split('.'); (body[p[0]] = body[p[0]] || {})[p[1]] = v; }
      }
    });
    if (!Object.keys(body).length) { $('cfg-msg').textContent = 'No changes.'; return; }
    try {
      var r = await api('/api/config', body);
      $('cfg-msg').textContent = 'Saved' + (r.reboot_required ? ' – REBOOT REQUIRED for some changes to take effect.' : '.');
      await loadCfg(); $('cfg-msg').textContent = 'Saved' + (r.reboot_required ? ' – REBOOT REQUIRED for some changes to take effect.' : '.');
    } catch (err) { $('cfg-msg').textContent = 'Save failed: ' + err.message; }
  });
  $('cfg-reload').onclick = loadCfg;
  $('cfg-validate').onclick = async function () { try { await api('/api/validate_fast', {}); notify('Fast-mode validation started.', 'ok'); } catch (e) { notify('Validate: ' + e.message, 'err'); } };
  function renderValidate(v) {
    if (!v) return;
    var t = 'Validate fast: ' + v.state + (v.msg ? ' – ' + v.msg : '');
    if (v.state === 'done' && v.mean_full !== undefined) t += ' (mean ' + fmt(v.mean_full, 1) + ' / ' + fmt(v.mean_fast, 1) + ', σ ' + fmt(v.std_full, 1) + ' / ' + fmt(v.std_fast, 1) + ')';
    $('cfg-val').textContent = t;
  }
  var logTimer = 0;
  async function pollLog() {
    if (!$('p-log').open) { logTimer = 0; return; }
    try {
      var r = await fetch('/api/log', { cache: 'no-store' }), t = await r.text(), el = $('log');
      if (el.textContent !== t) { el.textContent = t; if ($('log-follow').checked) el.scrollTop = el.scrollHeight; }
    } catch (e) { /* offline */ }
    logTimer = setTimeout(pollLog, 2000);
  }
  $('p-log').addEventListener('toggle', function () { if ($('p-log').open && !logTimer) pollLog(); });

  /* ============================================================== processing sidebar (generated from a spec) */
  var CURVE = { gamma: { min: 0.2, max: 3, step: 0.05, def: 1 }, log: { min: 1, max: 200, step: 1, def: 30 }, sigmoid: { min: 1, max: 20, step: 0.5, def: 6 } };
  var pct = function (P) { return P.win.mode === 'pct'; }, abs = function (P) { return P.win.mode === 'abs'; };
  var dnm = function (m) { return function (P) { return P.denoise.mode === m; }; };
  var SPEC = [
    { t: 'Recipes', open: true, rows: [{ k: 'recipes' }] },
    { t: 'Defects', open: true, rows: [
      { k: 'check', p: 'lines.on', l: 'Repair dead rows / columns', st: 'base' },
      { k: 'range', p: 'lines.thresh', l: 'Line sensitivity (σ threshold)', min: 3, max: 100, step: 0.5, st: 'base', f: function (P) { return P.lines.on; } },
      { k: 'check', p: 'manual.on', l: 'Apply manual defect list', st: 'base' }, { k: 'manual' },
      { k: 'check', p: 'pixels.on', l: 'Repair hot / dead pixels (automatic, after lines)', st: 'base' },
      { k: 'range', p: 'pixels.thresh', l: 'Pixel threshold (σ)', min: 3, max: 30, step: 0.5, st: 'base', f: function (P) { return P.pixels.on; } },
      { k: 'defects' }] },
    { t: 'Window', open: true, rows: [
      { k: 'hist' }, { k: 'seg', p: 'win.mode', opts: [['pct', 'Percentile'], ['abs', 'Manual']] }, { k: 'btn', l: 'Auto', act: 'auto' },
      { k: 'range', p: 'win.loPct', l: 'Low percentile (%)', min: 0, max: 20, step: 0.1, f: pct }, { k: 'range', p: 'win.hiPct', l: 'High percentile (%)', min: 80, max: 100, step: 0.1, f: pct },
      { k: 'select', p: 'win.region', l: 'Percentiles from', opts: [['full', 'full frame (as Python)'], ['crop', 'crop box only']], f: pct },
      { k: 'num', p: 'win.lo', l: 'Low (ADU)', min: 0, max: 65535, step: 1, f: abs }, { k: 'num', p: 'win.hi', l: 'High (ADU)', min: 1, max: 65535, step: 1, f: abs },
      { k: 'winlohi' },
      { k: 'select', p: 'win.curve', l: 'Curve (before 8-bit)', opts: [['none', 'Linear (cv2 exact)'], ['gamma', 'Gamma'], ['log', 'Log'], ['sigmoid', 'Sigmoid']] },
      { k: 'range', p: 'win.cparam', l: 'Curve strength', min: 0.2, max: 3, step: 0.05, f: function (P) { return P.win.curve !== 'none'; } }] },
    { t: 'CLAHE', rows: [
      { k: 'check', p: 'clahe.on', l: 'CLAHE on the 8-bit window' },
      { k: 'range', p: 'clahe.clip', l: 'Clip limit', min: 0.5, max: 12, step: 0.1, f: function (P) { return P.clahe.on; } },
      { k: 'range', p: 'clahe.tiles', l: 'Tiles per side', min: 2, max: 32, step: 1, f: function (P) { return P.clahe.on; } },
      { k: 'select', p: 'clahe.bits', num: true, l: 'Precision', opts: [[8, '8-bit – identical to cv2.createCLAHE'], [12, '12-bit – finer, NOT cv2']], f: function (P) { return P.clahe.on; } }] },
    { t: 'Palette', rows: [{ k: 'select', p: 'palette', l: 'Palette', opts: Palettes.names.map(function (n) { return [n, n]; }) }, { k: 'swatch' }, { k: 'check', p: 'invert', l: 'Invert (after CLAHE)' }] },
    { t: 'Denoise', rows: [
      { k: 'select', p: 'denoise.mode', st: 'enh', l: 'Method', opts: [['none', 'Off'], ['gauss', 'Gaussian'], ['median3', 'Median 3×3'], ['median5', 'Median 5×5'], ['bilateral', 'Bilateral'], ['nlm', 'Non-local means (slow)']] },
      { k: 'range', p: 'denoise.sigma', st: 'enh', l: 'σ (px)', min: 0.3, max: 6, step: 0.1, f: dnm('gauss') },
      { k: 'range', p: 'denoise.radius', st: 'enh', l: 'Radius (px)', min: 1, max: 4, step: 1, f: dnm('bilateral') },
      { k: 'range', p: 'denoise.sigmaR', st: 'enh', l: 'Range σ (ADU)', min: 10, max: 1000, step: 5, f: dnm('bilateral') },
      { k: 'range', p: 'denoise.h', st: 'enh', l: 'Strength h (ADU)', min: 5, max: 400, step: 5, f: dnm('nlm') },
      { k: 'range', p: 'denoise.search', st: 'enh', l: 'Search radius (px)', min: 1, max: 5, step: 1, f: dnm('nlm') },
      { k: 'range', p: 'denoise.patch', st: 'enh', l: 'Patch radius (px)', min: 1, max: 2, step: 1, f: dnm('nlm') }] },
    { t: 'Enhance', rows: [
      { k: 'check', p: 'destripe.on', st: 'enh', l: 'Destripe (row / column median)' },
      { k: 'select', p: 'destripe.axis', st: 'enh', l: 'Direction', opts: [['rows', 'Rows'], ['cols', 'Columns'], ['both', 'Both']], f: function (P) { return P.destripe.on; } },
      { k: 'range', p: 'destripe.win', st: 'enh', l: 'Baseline window (lines)', min: 2, max: 128, step: 1, f: function (P) { return P.destripe.on; } },
      { k: 'range', p: 'destripe.strength', st: 'enh', l: 'Strength', min: 0, max: 1, step: 0.05, f: function (P) { return P.destripe.on; } },
      { k: 'check', p: 'flatten.on', st: 'enh', l: 'Flatten background (large-kernel division)' },
      { k: 'range', p: 'flatten.sigma', st: 'enh', l: 'Kernel σ (px)', min: 10, max: 600, step: 5, f: function (P) { return P.flatten.on; } },
      { k: 'range', p: 'flatten.strength', st: 'enh', l: 'Strength', min: 0, max: 1, step: 0.05, f: function (P) { return P.flatten.on; } },
      { k: 'check', p: 'unsharp.on', st: 'enh', l: 'Unsharp mask' },
      { k: 'range', p: 'unsharp.amount', st: 'enh', l: 'Amount', min: 0, max: 5, step: 0.1, f: function (P) { return P.unsharp.on; } },
      { k: 'range', p: 'unsharp.sigma', st: 'enh', l: 'Radius σ (px)', min: 0.5, max: 10, step: 0.1, f: function (P) { return P.unsharp.on; } },
      { k: 'check', p: 'local.on', st: 'enh', l: 'Local / multi-scale contrast' },
      { k: 'range', p: 'local.amount', st: 'enh', l: 'Amount', min: 0, max: 3, step: 0.05, f: function (P) { return P.local.on; } },
      { k: 'range', p: 'local.sigma', st: 'enh', l: 'Finest scale σ (px)', min: 1, max: 16, step: 0.5, f: function (P) { return P.local.on; } },
      { k: 'range', p: 'local.levels', st: 'enh', l: 'Scales (octaves)', min: 1, max: 5, step: 1, f: function (P) { return P.local.on; } }] },
    { t: 'Orientation & crop', rows: [{ k: 'geom' }, { k: 'check', p: 'geom.flipH', st: 'enh', l: 'Flip horizontal' }, { k: 'check', p: 'geom.flipV', st: 'enh', l: 'Flip vertical' }, { k: 'crop' }] }
  ];
  var RECIPES = {
    'Default': {},
    'Bone': { win: { loPct: 0.5, hiPct: 99.8 }, clahe: { on: true, clip: 3, tiles: 8 }, unsharp: { on: true, amount: 0.8, sigma: 1.5 } },
    'Soft tissue': { win: { loPct: 1, hiPct: 99, curve: 'gamma', cparam: 1.4 }, denoise: { mode: 'bilateral', radius: 2, sigmaR: 60 }, local: { on: true, amount: 0.5, sigma: 4, levels: 3 }, clahe: { on: true, clip: 2, tiles: 8 } },
    'Metal': { win: { loPct: 0.1, hiPct: 99.95, curve: 'log', cparam: 30 }, flatten: { on: true, sigma: 200, strength: 0.7 }, unsharp: { on: true, amount: 0.6, sigma: 1.5 } },
    'Low dose': { win: { loPct: 1, hiPct: 99.5 }, destripe: { on: true, axis: 'both', win: 16, strength: 1 }, denoise: { mode: 'nlm', h: 50, search: 3, patch: 1 } }
  };
  var ctrls = [], procRoot = $('proc');
  function getP(path) { return path.split('.').reduce(function (o, k) { return o[k]; }, params); }
  function setP(path, v) { var ks = path.split('.'), o = params, i; for (i = 0; i < ks.length - 1; i++) o = o[ks[i]]; o[ks[ks.length - 1]] = v; }
  function decimals(step) { var s = String(step), i = s.indexOf('.'); return i < 0 ? 0 : s.length - i - 1; }

  function buildProc() {
    SPEC.forEach(function (sec) {
      var body = h('div', { class: 'body' }), det = h('details', { class: 'panel', open: !!sec.open }, h('summary', { text: sec.t }), body);
      sec.rows.forEach(function (r) {
        if (r.p) body.append(mkCtl(r)); else body.append(mkCustom(r));
      });
      procRoot.append(det);
    });
  }
  function mkCtl(r) {
    var id = 'c-' + r.p.replace(/\./g, '-'), c = { r: r, p: r.p, k: r.k, f: r.f }, wrap, inp, out;
    if (r.k === 'check') { inp = h('input', { type: 'checkbox', id: id }); wrap = h('label', { class: 'check' }, inp, r.l); c.inputs = [inp]; }
    else if (r.k === 'range') { inp = h('input', { type: 'range', id: id, min: r.min, max: r.max, step: r.step }); out = h('output', { for: id }); wrap = h('div', { class: 'sl' }, h('label', { for: id, text: r.l }), out, inp); c.inputs = [inp]; c.out = out; }
    else if (r.k === 'num') { inp = h('input', { type: 'number', id: id, min: r.min, max: r.max, step: r.step, class: 'num' }); wrap = h('div', { class: 'sl' }, h('label', { for: id, text: r.l }), h('span'), inp); c.inputs = [inp]; }
    else if (r.k === 'select') { inp = h('select', { id: id }, r.opts.map(function (o) { return h('option', { value: o[0], text: o[1] }); })); wrap = h('div', { class: 'sl' }, h('label', { for: id, text: r.l }), h('span'), inp); c.inputs = [inp]; }
    else if (r.k === 'seg') { c.inputs = r.opts.map(function (o) { return h('input', { type: 'radio', name: id, value: o[0] }); }); wrap = h('span', { class: 'seg', role: 'radiogroup', 'aria-label': r.p }, r.opts.map(function (o, i) { return h('label', {}, c.inputs[i], h('span', { text: o[1] })); })); }
    c.wrap = wrap; ctrls.push(c);
    c.inputs.forEach(function (i) {
      i.addEventListener('input', function (e) { if (r.k === 'range') onCtl(c, e); });
      i.addEventListener('change', function (e) { onCtl(c, e); });
    });
    return wrap;
  }
  function readCtl(c) {
    var i = c.inputs[0];
    if (c.k === 'check') return i.checked;
    if (c.k === 'range' || c.k === 'num') return parseFloat(i.value);
    if (c.k === 'select') return c.r.num ? Number(i.value) : i.value;
    var on = c.inputs.filter(function (x) { return x.checked; })[0]; return on ? on.value : getP(c.p);
  }
  function writeCtl(c) {
    var v = getP(c.p), i = c.inputs[0];
    if (c.k === 'check') i.checked = !!v;
    else if (c.k === 'range') {
      if (c.p === 'win.cparam') { var cv = CURVE[params.win.curve] || CURVE.gamma; i.min = cv.min; i.max = cv.max; i.step = cv.step; }
      i.value = v; c.out.textContent = Number(v).toFixed(decimals(i.step));
    }
    else if (c.k === 'num') { if (document.activeElement !== i) i.value = Math.round(v); }
    else if (c.k === 'select') i.value = String(v);
    else c.inputs.forEach(function (x) { x.checked = x.value === String(v); });
    if (c.f) c.wrap.hidden = !c.f(params);
  }
  function syncUI() {
    ctrls.forEach(writeCtl);
    updateCustom();
  }
  function onCtl(c, e) {
    var v = readCtl(c);
    if (typeof v === 'number' && isNaN(v)) return;
    if (c.p === 'win.mode' && v === 'abs' && params.win.mode === 'pct') { params.win.lo = Math.round(S.lo); params.win.hi = Math.round(S.hi); }
    if (c.p === 'win.curve') params.win.cparam = (CURVE[v] || CURVE.gamma).def;
    if (c.p === 'win.lo' || c.p === 'win.hi') { /* keep lo < hi */ }
    setP(c.p, v);
    if (c.p === 'win.lo' && params.win.hi <= params.win.lo) params.win.hi = Math.min(65535, params.win.lo + 1);
    syncUI(); persist();
    if (!S.frame) return;
    var heavy = c.r.st === 'enh';
    if (e.type === 'input') { if (heavy) previewSoon(); else requestRender('full', true); }
    else requestRender('full');
  }
  var persistTimer = 0;
  function persist() { clearTimeout(persistTimer); persistTimer = setTimeout(function () { store('params', params); }, 300); }

  function mkCustom(r) {
    if (r.k === 'recipes') {
      var box = h('div', { class: 'btns' });
      Object.keys(RECIPES).forEach(function (n) { box.append(h('button', { class: 'btn small', type: 'button', text: n, onclick: function () { applyRecipe(n); } })); });
      box.append(h('button', { class: 'btn small', type: 'button', text: 'Reset all', title: 'Reset every processing setting (keeps crop and manual defects)', onclick: function () { applyRecipe('Default'); } }));
      return box;
    }
    if (r.k === 'btn') return h('button', { class: 'btn small', type: 'button', text: 'Auto (1.0 % / 99.5 %)', title: 'Back to the percentile autorange of flashpad_capture_gui', onclick: function () { params.win.mode = 'pct'; params.win.loPct = 1.0; params.win.hiPct = 99.5; params.win.region = 'full'; syncUI(); persist(); requestRender('full'); } });
    if (r.k === 'hist') { var cv = h('canvas', { id: 'hist', height: 96, role: 'img', 'aria-label': 'Histogram of the processed data with draggable window handles' }); histInit(cv); return cv; }
    if (r.k === 'winlohi') return h('p', { class: 'note mono', id: 'winlohi', text: '' });
    if (r.k === 'defects') return h('p', { class: 'note', id: 'defect-info', text: '' });
    if (r.k === 'swatch') return h('canvas', { id: 'swatch', width: 256, height: 1, 'aria-hidden': 'true' });
    if (r.k === 'manual') {
      return h('div', { id: 'manual-box' },
        h('div', { class: 'inline' }, h('label', { for: 'mark-kind', text: 'Marker' }), h('select', { id: 'mark-kind', onchange: function (e) { S.markKind = e.target.value; } },
          h('option', { value: 'p', text: 'Pixel' }), h('option', { value: 'r', text: 'Row' }), h('option', { value: 'c', text: 'Column' })),
          h('button', { class: 'btn small', type: 'button', text: 'Mark tool', onclick: function () { selectTool('mark'); } }),
          h('button', { class: 'btn small', type: 'button', text: 'Clear', onclick: function () { params.manual.list = []; syncUI(); persist(); requestRender('full'); } })),
        h('div', { class: 'chips', id: 'manual-chips' }));
    }
    if (r.k === 'geom') {
      return h('div', { class: 'inline' }, h('button', { class: 'btn small', type: 'button', text: '⟲ 90°', onclick: function () { rotate(-1); } }), h('button', { class: 'btn small', type: 'button', text: '⟳ 90°', onclick: function () { rotate(1); } }),
        h('span', { class: 'note', id: 'rot-info' }));
    }
    if (r.k === 'crop') {
      var g = h('div', { class: 'grid2' });
      ['x0', 'y0', 'x1', 'y1'].forEach(function (k) { g.append(h('label', { for: 'crop-' + k, text: k }), h('input', { type: 'number', id: 'crop-' + k, min: 0, max: DIM, step: 1, onchange: function () { cropField(k); } })); });
      return h('div', { class: 'body', style: 'padding:0' }, g,
        h('div', { class: 'btns' }, h('button', { class: 'btn small', type: 'button', text: 'Full frame', onclick: function () { setCrop(0, 0, DIM, DIM); } }),
          h('button', { class: 'btn small', type: 'button', text: 'Default box', onclick: function () { setCrop(204, 204, 1844, 1844); } }),
          h('button', { class: 'btn small', type: 'button', text: 'Edit on image', onclick: function () { selectTool('crop'); } })),
        h('p', { class: 'note', text: 'The box is drawn on the image (outside is dimmed). It limits the processed / PNG saves and, optionally, the percentile range.' }));
    }
    return h('span');
  }
  function updateCustom() {
    var chips = $('manual-chips');
    if (chips) {
      chips.textContent = '';
      params.manual.list.forEach(function (m, i) {
        chips.append(h('span', { class: 'chip' }, (m.k === 'p' ? 'px ' + m.x + ',' + m.y : m.k === 'r' ? 'row ' + m.y : 'col ' + m.x),
          h('button', { type: 'button', 'aria-label': 'Remove marker', text: '×', onclick: function () { params.manual.list.splice(i, 1); syncUI(); persist(); requestRender('full'); } })));
      });
    }
    var d = $('defect-info');
    if (d) { var f = S.defects; d.textContent = 'Repaired: rows [' + f.rows.slice(0, 12).join(', ') + (f.rows.length > 12 ? ', …' : '') + '] · cols [' + f.cols.slice(0, 12).join(', ') + (f.cols.length > 12 ? ', …' : '') + '] · ' + f.npix + ' pixel(s)'; }
    var wl = $('winlohi'); if (wl) wl.textContent = 'window ' + fmt(S.lo) + ' … ' + fmt(S.hi) + ' ADU';
    var ri = $('rot-info'); if (ri) ri.textContent = 'rotation ' + (params.geom.rot * 90) + '°';
    ['x0', 'y0', 'x1', 'y1'].forEach(function (k) { var e = $('crop-' + k); if (e && document.activeElement !== e) e.value = params.crop[k]; });
    var sw = $('swatch');
    if (sw) { var t = Palettes.get(params.palette), ctx = sw.getContext('2d'), im = ctx.createImageData(256, 1); for (var i = 0; i < 256; i++) { var v = params.invert ? 255 - i : i; im.data[4 * i] = t[3 * v]; im.data[4 * i + 1] = t[3 * v + 1]; im.data[4 * i + 2] = t[3 * v + 2]; im.data[4 * i + 3] = 255; } ctx.putImageData(im, 0, 0); }
    var mk = $('mark-kind'); if (mk) mk.value = S.markKind;
    $('sv-crop').checked = !!params.crop.saves;
    drawHist(); draw();
  }
  function applyRecipe(name) {
    var keep = { dark: params.dark, lines: params.lines, manual: params.manual, geom: params.geom, crop: params.crop, palette: params.palette, pixels: params.pixels }, d = PC.defaultParams();
    params = merge(d, clone(keep)); merge(params, clone(RECIPES[name] || {}));
    syncUI(); persist(); if (S.frame) requestRender('full'); notify('Recipe "' + name + '" applied.', 'ok');
  }
  function rotate(d) { params.geom.rot = ((params.geom.rot + d) % 4 + 4) % 4; syncUI(); persist(); fitAfter = true; requestRender('full'); }
  function setCrop(x0, y0, x1, y1) {
    var c = params.crop; c.x0 = clamp(Math.round(x0), 0, DIM - 16); c.y0 = clamp(Math.round(y0), 0, DIM - 16); c.x1 = clamp(Math.round(x1), c.x0 + 16, DIM); c.y1 = clamp(Math.round(y1), c.y0 + 16, DIM);
    updateCustom(); persist(); if (params.win.mode === 'pct' && params.win.region === 'crop' && S.frame) requestRender('full');
  }
  function cropField(k) { var c = params.crop, v = parseFloat($('crop-' + k).value); if (isNaN(v)) return; c[k] = v; setCrop(c.x0, c.y0, c.x1, c.y1); }

  /* ============================================================== worker + render scheduling */
  function initWorker() {
    worker = new Worker('proc-worker.js');
    worker.onmessage = function (e) {
      var m = e.data;
      if (m.t === 'result') onResult(m);
      else if (m.t === 'progress') { if (S.inflight && m.id === S.inflight.id) $('busy-text').textContent = m.text + '…'; }
      else if (m.t === 'cancelled') onCancelled(m);
      else if (m.t === 'exported') { var w = exportWaiters[m.id]; if (w) { delete exportWaiters[m.id]; w.res(m); } }
      else if (m.t === 'error') {
        var w2 = exportWaiters[m.id];
        if (w2) { delete exportWaiters[m.id]; w2.rej(new Error(m.msg)); return; }
        if (S.inflight && m.id === S.inflight.id) { S.inflight = null; busyEnd(); }
        notify('Processing error: ' + m.msg, 'err');
      }
    };
    worker.onerror = function (e) { notify('Worker error: ' + e.message, 'err'); };
  }
  var busyTimer = 0;
  function busyStart(mode) {
    clearTimeout(busyTimer);
    if (mode === 'preview') $('busy').hidden = true;
    if (mode === 'full') busyTimer = setTimeout(function () { $('busy-text').textContent = 'Processing…'; $('busy').hidden = false; }, 200);
  }
  function busyEnd() { clearTimeout(busyTimer); $('busy').hidden = true; }
  function previewFactor() { var side = Math.max(V.cw, V.ch) * V.dpr || 800, f = 2; while (f < 8 && DIM / (f * 2) >= side * 0.9) f *= 2; return f; }
  function send(mode) {
    var id = ++S.rid;
    S.inflight = { id: id, mode: mode, params: clone(params) };
    worker.postMessage({ t: 'render', id: id, params: S.inflight.params, mode: mode, f: previewFactor() });
    busyStart(mode);
  }
  function requestRender(mode, throttle) {
    if (!S.frame || !worker) return;
    clearTimeout(S.fullTimer); S.fullTimer = 0;
    if (throttle && S.inflight) { if (S.again !== 'full') S.again = mode; return; }
    S.again = null; send(mode);
  }
  function previewSoon() {                      // slider drag: quick low-resolution preview now, full resolution when it settles
    if (S.inflight && S.inflight.mode === 'preview') S.again = 'preview';      // one preview in flight at a time, latest settings win
    else send('preview');                                                       // (a running full render is superseded: its result would be stale)
    clearTimeout(S.fullTimer); S.fullTimer = setTimeout(function () { S.fullTimer = 0; requestRender('full'); }, 400);
  }
  function putImage(canvas, m) {
    if (canvas.width !== m.W || canvas.height !== m.H) { canvas.width = m.W; canvas.height = m.H; }
    canvas.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(m.rgba), m.W, m.H), 0, 0);
  }
  var imgC = document.createElement('canvas'), prevC = document.createElement('canvas'), fitAfter = false;
  function onResult(m) {
    if (!S.inflight || m.id !== S.inflight.id) return;               // stale
    var job = S.inflight; S.inflight = null; busyEnd();
    if (m.mode === 'preview') { putImage(prevC, m); V.layer = 'preview'; V.f = V.W / m.W; $('pv').hidden = false; }
    else {
      putImage(imgC, m); V.layer = 'full'; V.W = m.W; V.H = m.H; $('pv').hidden = true;
      S.lo = m.lo; S.hi = m.hi; S.defects = m.defects; S.lastGood = job.params;
      if (m.proc) { S.proc = new Uint16Array(m.proc); S.procW = m.W; S.procH = m.H; S.hist = new Uint32Array(m.hist); }
      S.haveImage = true;
      if (V.needFit || fitAfter) { if (fitView(false)) { V.needFit = false; fitAfter = false; } }
      updateCustom(); updateRoi(); drawProfile();
    }
    draw();
    if (S.again) { var a = S.again; S.again = null; if (a === 'preview') previewSoon(); else send(a); }
  }
  function onCancelled(m) {
    if (!S.inflight || m.id !== S.inflight.id) return;
    S.inflight = null; busyEnd();
    params = clone(S.lastGood); syncUI(); persist(); notify('Cancelled – settings reverted.', 'ok');
    requestRender('full');
  }
  $('busy-cancel').onclick = function () { worker.postMessage({ t: 'cancel' }); $('busy-text').textContent = 'Cancelling…'; };

  /* ============================================================== viewer */
  var view = $('view'), vctx = view.getContext('2d');
  var V = { s: 1, ox: 0, oy: 0, W: DIM, H: DIM, layer: 'full', f: 1, tool: 'pan', roi: null, line: null, drag: null, ptr: {}, cw: 0, ch: 0, dpr: 1, needFit: true, manual: false, hover: null };
  window.fp.V = V;
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888'; }
  function resize() {
    var r = view.getBoundingClientRect(); V.dpr = window.devicePixelRatio || 1; V.cw = r.width; V.ch = r.height;
    view.width = Math.max(1, Math.round(r.width * V.dpr)); view.height = Math.max(1, Math.round(r.height * V.dpr));
    if ((V.needFit || !V.manual) && S.haveImage) { fitView(false); }
    draw(); drawHist(); drawProfile();
  }
  new ResizeObserver(resize).observe(view);
  function setZoomLabel() { $('z-val').textContent = Math.round(V.s * 100) + ' %'; }
  function fitBox(x0, y0, x1, y1, margin) {
    if (V.cw < 50 || V.ch < 50) { V.needFit = true; return false; }          // not laid out yet: resize() fits as soon as it is
    var w = x1 - x0, h = y1 - y0; V.s = clamp(Math.min(V.cw / w, V.ch / h) * (margin || 0.98), 0.05, 40);
    V.ox = (V.cw - w * V.s) / 2 - x0 * V.s; V.oy = (V.ch - h * V.s) / 2 - y0 * V.s; V.needFit = false; V.manual = false; setZoomLabel(); draw(); return true;
  }
  function fitView(crop) { var c = params.crop; if (crop && !(c.x0 === 0 && c.y0 === 0 && c.x1 >= DIM && c.y1 >= DIM)) return fitBox(c.x0, c.y0, c.x1, c.y1); return fitBox(0, 0, V.W, V.H); }
  function zoomAt(mx, my, k) {
    var s2 = clamp(V.s * k, 0.05, 40); if (s2 === V.s) return;
    V.ox = mx - (mx - V.ox) * s2 / V.s; V.oy = my - (my - V.oy) * s2 / V.s; V.s = s2; V.manual = true; setZoomLabel(); draw();
  }
  function toImg(px, py) { return [(px - V.ox) / V.s, (py - V.oy) / V.s]; }
  function toScr(ix, iy) { return [V.ox + ix * V.s, V.oy + iy * V.s]; }
  function selectTool(t) {
    V.tool = t; $$('input[name=tool]').forEach(function (r) { r.checked = r.value === t; });
    view.className = (V.drag ? 'dragging ' : '') + 'tool-' + t; draw();
  }
  $$('input[name=tool]').forEach(function (r) { r.addEventListener('change', function () { if (r.checked) selectTool(r.value); }); });
  $('z-in').onclick = function () { zoomAt(V.cw / 2, V.ch / 2, 1.25); };
  $('z-out').onclick = function () { zoomAt(V.cw / 2, V.ch / 2, 1 / 1.25); };
  $('z-fit').onclick = function () { fitView(false); };
  $('z-crop').onclick = function () { fitView(true); };
  V.showCrop = store('showCrop') === true;
  function syncCropBtn() { var b = $('z-cropvis'); b.textContent = V.showCrop ? 'Crop box: on' : 'Crop box: off'; b.setAttribute('aria-pressed', V.showCrop ? 'true' : 'false'); }
  $('z-cropvis').onclick = function () { V.showCrop = !V.showCrop; store('showCrop', V.showCrop); syncCropBtn(); draw(); };
  syncCropBtn();
  $('z-100').onclick = function () { var cx = (V.cw / 2 - V.ox) / V.s, cy = (V.ch / 2 - V.oy) / V.s; V.s = 1; V.ox = V.cw / 2 - cx; V.oy = V.ch / 2 - cy; V.manual = true; setZoomLabel(); draw(); };

  function draw() {
    var ctx = vctx; ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, view.width, view.height);
    if (!S.haveImage) return;
    ctx.setTransform(V.dpr, 0, 0, V.dpr, 0, 0);
    var src = V.layer === 'preview' ? prevC : imgC, k = V.layer === 'preview' ? V.f : 1;
    ctx.imageSmoothingEnabled = V.layer === 'preview' || V.s < 1.5; ctx.imageSmoothingQuality = 'high';
    var ix0 = Math.max(0, Math.floor(-V.ox / V.s)), iy0 = Math.max(0, Math.floor(-V.oy / V.s)), ix1 = Math.min(V.W, Math.ceil((V.cw - V.ox) / V.s)), iy1 = Math.min(V.H, Math.ceil((V.ch - V.oy) / V.s));
    if (ix1 > ix0 && iy1 > iy0) ctx.drawImage(src, ix0 / k, iy0 / k, (ix1 - ix0) / k, (iy1 - iy0) / k, V.ox + ix0 * V.s, V.oy + iy0 * V.s, (ix1 - ix0) * V.s, (iy1 - iy0) * V.s);
    overlays(ctx);
  }
  function overlays(ctx) {
    var c = params.crop, a = toScr(c.x0, c.y0), b = toScr(c.x1, c.y1), full = c.x0 === 0 && c.y0 === 0 && c.x1 >= V.W && c.y1 >= V.H, accent = css('--accent'), hs = (window.matchMedia && matchMedia('(pointer: coarse)').matches) ? 9 : 4;
    if (!full && (V.showCrop || V.tool === 'crop')) {
      ctx.fillStyle = 'rgba(0,0,0,.45)';
      var L = V.ox, T = V.oy, R = V.ox + V.W * V.s, B = V.oy + V.H * V.s;
      ctx.fillRect(L, T, R - L, a[1] - T); ctx.fillRect(L, b[1], R - L, B - b[1]); ctx.fillRect(L, a[1], a[0] - L, b[1] - a[1]); ctx.fillRect(b[0], a[1], R - b[0], b[1] - a[1]);
      ctx.strokeStyle = accent; ctx.lineWidth = 1.5; ctx.setLineDash([6, 4]); ctx.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]); ctx.setLineDash([]);
      if (V.tool === 'crop') { ctx.fillStyle = accent; [[a[0], a[1]], [b[0], a[1]], [a[0], b[1]], [b[0], b[1]], [(a[0] + b[0]) / 2, a[1]], [(a[0] + b[0]) / 2, b[1]], [a[0], (a[1] + b[1]) / 2], [b[0], (a[1] + b[1]) / 2]].forEach(function (p) { ctx.fillRect(p[0] - hs, p[1] - hs, 2 * hs, 2 * hs); }); }
    }
    ctx.lineWidth = 1.5;
    if (V.roi) { var r0 = toScr(V.roi.x0, V.roi.y0), r1 = toScr(V.roi.x1, V.roi.y1); ctx.strokeStyle = '#ffd400'; ctx.strokeRect(r0[0], r0[1], r1[0] - r0[0], r1[1] - r0[1]); }
    if (V.line) { var l0 = toScr(V.line.x0, V.line.y0), l1 = toScr(V.line.x1, V.line.y1); ctx.strokeStyle = '#00e0ff'; ctx.beginPath(); ctx.moveTo(l0[0], l0[1]); ctx.lineTo(l1[0], l1[1]); ctx.stroke(); ctx.fillStyle = '#00e0ff'; ctx.fillRect(l0[0] - 3, l0[1] - 3, 6, 6); ctx.fillRect(l1[0] - 3, l1[1] - 3, 6, 6); }
    ctx.strokeStyle = '#ff3df0'; ctx.fillStyle = '#ff3df0';
    if (params.manual.on) params.manual.list.forEach(function (m) {
      var g = params.geom, p, q;
      if (m.k === 'p') { p = PC.sensorToDisplay(m.x, m.y, DIM, DIM, g); var s = toScr(p[0] + 0.5, p[1] + 0.5); ctx.beginPath(); ctx.arc(s[0], s[1], Math.max(4, V.s * 0.7), 0, 6.283); ctx.stroke(); }
      else { p = m.k === 'r' ? PC.sensorToDisplay(0, m.y, DIM, DIM, g) : PC.sensorToDisplay(m.x, 0, DIM, DIM, g); q = m.k === 'r' ? PC.sensorToDisplay(DIM - 1, m.y, DIM, DIM, g) : PC.sensorToDisplay(m.x, DIM - 1, DIM, DIM, g); var s0 = toScr(p[0] + 0.5, p[1] + 0.5), s1 = toScr(q[0] + 0.5, q[1] + 0.5); ctx.beginPath(); ctx.moveTo(s0[0], s0[1]); ctx.lineTo(s1[0], s1[1]); ctx.stroke(); }
    });
  }
  function pos(e) { var r = view.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
  function inImg(ix, iy) { return ix >= 0 && iy >= 0 && ix < V.W && iy < V.H; }
  function cropHit(px, py) {
    var c = params.crop, s = V.s, L = V.ox + c.x0 * s, R = V.ox + c.x1 * s, T = V.oy + c.y0 * s, B = V.oy + c.y1 * s, t = (window.matchMedia && matchMedia('(pointer: coarse)').matches) ? 26 : 10;
    if (!(px > L - t && px < R + t && py > T - t && py < B + t)) return 'new';
    var m = '';
    if (Math.abs(py - T) < t) m += 'n'; else if (Math.abs(py - B) < t) m += 's';
    if (Math.abs(px - L) < t) m += 'w'; else if (Math.abs(px - R) < t) m += 'e';
    return m || (px > L && px < R && py > T && py < B ? 'move' : 'new');
  }
  view.addEventListener('wheel', function (e) { e.preventDefault(); var p = pos(e); zoomAt(p[0], p[1], e.deltaY < 0 ? 1.25 : 1 / 1.25); }, { passive: false });
  view.addEventListener('pointerdown', function (e) {
    view.focus({ preventScroll: true }); view.setPointerCapture(e.pointerId);
    var p = pos(e); V.ptr[e.pointerId] = p;
    var ids = Object.keys(V.ptr);
    if (ids.length === 2) { V.drag = { t: 'pinch', d: Math.hypot(V.ptr[ids[0]][0] - V.ptr[ids[1]][0], V.ptr[ids[0]][1] - V.ptr[ids[1]][1]) }; return; }
    var im = toImg(p[0], p[1]), tool = e.button === 1 ? 'pan' : V.tool;
    if (tool === 'pan') { V.drag = { t: 'pan', x: p[0], y: p[1], ox: V.ox, oy: V.oy }; V.manual = true; }
    else if (tool === 'roi') { V.drag = { t: 'roi' }; V.roi = { x0: clamp(im[0], 0, V.W), y0: clamp(im[1], 0, V.H), x1: clamp(im[0], 0, V.W), y1: clamp(im[1], 0, V.H), ax: clamp(im[0], 0, V.W), ay: clamp(im[1], 0, V.H) }; }
    else if (tool === 'line') { V.drag = { t: 'line' }; V.line = { x0: im[0], y0: im[1], x1: im[0], y1: im[1] }; }
    else if (tool === 'crop') { var m = cropHit(p[0], p[1]); V.drag = { t: 'crop', m: m, ax: im[0], ay: im[1], c: clone(params.crop) }; if (m === 'new') V.drag.c = { x0: im[0], y0: im[1], x1: im[0], y1: im[1] }; }
    else if (tool === 'mark') {
      if (inImg(im[0], im[1])) {
        var sp = PC.displayToSensor(Math.floor(im[0]), Math.floor(im[1]), DIM, DIM, params.geom), k = S.markKind;
        var ent = { k: k, x: sp[0], y: sp[1] };
        if (!params.manual.list.some(function (q) { return q.k === k && (k === 'r' ? q.y === ent.y : k === 'c' ? q.x === ent.x : q.x === ent.x && q.y === ent.y); })) {
          params.manual.list.push(ent); params.manual.on = true; syncUI(); persist(); requestRender('full'); notify('Marked ' + (k === 'p' ? 'pixel ' + ent.x + ',' + ent.y : k === 'r' ? 'row ' + ent.y : 'column ' + ent.x) + ' (sensor coordinates).', 'ok');
        }
      }
    }
    view.className = 'dragging tool-' + V.tool; draw();
  });
  view.addEventListener('pointermove', function (e) {
    var p = pos(e), d = V.drag, im = toImg(p[0], p[1]);
    if (V.ptr[e.pointerId]) V.ptr[e.pointerId] = p;
    readout(im);
    if (!d) { if (V.tool === 'crop') view.style.cursor = { n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize', nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', move: 'move', 'new': 'crosshair' }[cropHit(p[0], p[1])]; return; }
    if (d.t === 'pan') { V.ox = d.ox + p[0] - d.x; V.oy = d.oy + p[1] - d.y; }
    else if (d.t === 'pinch') {
      var ids = Object.keys(V.ptr); if (ids.length < 2) return;
      var a = V.ptr[ids[0]], b = V.ptr[ids[1]], dist = Math.hypot(a[0] - b[0], a[1] - b[1]);
      if (d.d > 0 && dist > 0) zoomAt((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, dist / d.d); d.d = dist;
    }
    else if (d.t === 'roi') { var r = V.roi; r.x0 = Math.min(r.ax, clamp(im[0], 0, V.W)); r.x1 = Math.max(r.ax, clamp(im[0], 0, V.W)); r.y0 = Math.min(r.ay, clamp(im[1], 0, V.H)); r.y1 = Math.max(r.ay, clamp(im[1], 0, V.H)); }
    else if (d.t === 'line') { V.line.x1 = clamp(im[0], 0, V.W - 1); V.line.y1 = clamp(im[1], 0, V.H - 1); }
    else if (d.t === 'crop') dragCrop(d, im);
    draw();
  });
  function dragCrop(d, im) {
    var c = d.c, n = { x0: c.x0, y0: c.y0, x1: c.x1, y1: c.y1 }, dx = im[0] - d.ax, dy = im[1] - d.ay;
    if (d.m === 'new') { n.x0 = Math.min(d.ax, im[0]); n.x1 = Math.max(d.ax, im[0]); n.y0 = Math.min(d.ay, im[1]); n.y1 = Math.max(d.ay, im[1]); }
    else if (d.m === 'move') { var w = c.x1 - c.x0, h = c.y1 - c.y0; n.x0 = clamp(c.x0 + dx, 0, V.W - w); n.y0 = clamp(c.y0 + dy, 0, V.H - h); n.x1 = n.x0 + w; n.y1 = n.y0 + h; }
    else { if (d.m.indexOf('w') >= 0) n.x0 = im[0]; if (d.m.indexOf('e') >= 0) n.x1 = im[0]; if (d.m.indexOf('n') >= 0) n.y0 = im[1]; if (d.m.indexOf('s') >= 0) n.y1 = im[1]; if (n.x0 > n.x1) { var t = n.x0; n.x0 = n.x1; n.x1 = t; } if (n.y0 > n.y1) { var u = n.y0; n.y0 = n.y1; n.y1 = u; } }
    var pc = params.crop;
    pc.x0 = clamp(Math.round(n.x0), 0, V.W - 16); pc.y0 = clamp(Math.round(n.y0), 0, V.H - 16); pc.x1 = clamp(Math.round(n.x1), pc.x0 + 16, V.W); pc.y1 = clamp(Math.round(n.y1), pc.y0 + 16, V.H);
    updateCustom();
  }
  function endDrag(e) {
    var d = V.drag; delete V.ptr[e.pointerId];
    if (!d) return;
    if (Object.keys(V.ptr).length === 0) V.drag = null; else if (d.t === 'pinch') V.drag = null;
    view.className = 'tool-' + V.tool;
    if (d.t === 'roi') updateRoi();
    if (d.t === 'line') drawProfile();
    if (d.t === 'crop') { persist(); if (params.win.mode === 'pct' && params.win.region === 'crop' && S.frame) requestRender('full'); }
    draw();
  }
  var lastTap = 0;
  view.addEventListener('pointerup', function (e) {
    if (e.pointerType === 'touch' && V.tool === 'pan' && !(V.drag && V.drag.t === 'pinch')) {
      var now = Date.now(); if (now - lastTap < 320) { fitView(false); draw(); } lastTap = now;
    }
  });
  view.addEventListener('pointerup', endDrag); view.addEventListener('pointercancel', endDrag);
  view.addEventListener('pointerleave', function () { $('readout').textContent = 'x – y – · raw – · proc –'; });
  view.addEventListener('keydown', function (e) {
    var k = e.key, st = 40;
    if (k.indexOf('Arrow') === 0) V.manual = true;
    if (k === 'ArrowLeft') V.ox += st; else if (k === 'ArrowRight') V.ox -= st; else if (k === 'ArrowUp') V.oy += st; else if (k === 'ArrowDown') V.oy -= st;
    else if (k === '+' || k === '=') zoomAt(V.cw / 2, V.ch / 2, 1.25); else if (k === '-') zoomAt(V.cw / 2, V.ch / 2, 1 / 1.25);
    else if (k === '0') fitView(false); else if (k === '1') $('z-100').click(); else return;
    e.preventDefault(); draw();
  });
  function readout(im) {
    var x = Math.floor(im[0]), y = Math.floor(im[1]), el = $('readout');
    if (!S.haveImage || !inImg(x, y)) { el.textContent = 'x – y – · raw – · proc –'; return; }
    var sp = PC.displayToSensor(x, y, DIM, DIM, params.geom), raw = S.frame ? S.frame.u16[sp[1] * DIM + sp[0]] : NaN, pr = S.proc && S.procW === V.W ? S.proc[y * S.procW + x] : NaN;
    var g = params.geom, moved = g.rot || g.flipH || g.flipV;
    el.textContent = 'x ' + x + '  y ' + y + (moved ? ' (sensor ' + sp[0] + ',' + sp[1] + ')' : '') + ' · raw ' + raw + ' · proc ' + (isNaN(pr) ? '–' : pr);
  }
  function updateRoi() {
    var el = $('roi-info'), r = V.roi;
    if (!r || !S.proc) { el.textContent = 'ROI: use the ROI tool'; return; }
    var x0 = Math.floor(r.x0), y0 = Math.floor(r.y0), x1 = Math.ceil(r.x1), y1 = Math.ceil(r.y1), st = x1 > x0 && y1 > y0 ? PC.roiStats(S.proc, S.procW, x0, y0, Math.min(x1, S.procW), Math.min(y1, S.procH)) : null;
    el.textContent = st ? 'ROI ' + x0 + ',' + y0 + ' – ' + x1 + ',' + y1 + ' (' + st.n + ' px) · mean ' + fmt(st.mean, 1) + ' · SD ' + fmt(st.sd, 2) + ' · SNR ' + (isFinite(st.snr) ? fmt(st.snr, 1) : '∞') + ' · min ' + st.min + ' max ' + st.max : 'ROI: use the ROI tool';
  }
  function drawProfile() {
    var cv = $('profile'), w = cv.clientWidth, hh = cv.clientHeight, dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr); cv.height = Math.round(hh * dpr);
    var ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, hh);
    ctx.fillStyle = css('--muted'); ctx.font = '11px system-ui';
    if (!V.line || !S.proc) { ctx.fillText('Profile: drag a line with the Profile tool', 8, 16); return; }
    var l = V.line, v = PC.lineProfile(S.proc, S.procW, S.procH, l.x0, l.y0, l.x1, l.y1), mn = Infinity, mx = -Infinity, i;
    for (i = 0; i < v.length; i++) { if (v[i] < mn) mn = v[i]; if (v[i] > mx) mx = v[i]; }
    if (mx === mn) mx = mn + 1;
    ctx.strokeStyle = '#00a6c0'; ctx.lineWidth = 1.5; ctx.beginPath();
    for (i = 0; i < v.length; i++) { var x = 4 + (w - 8) * i / Math.max(1, v.length - 1), y = hh - 14 - (hh - 24) * (v[i] - mn) / (mx - mn); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }
    ctx.stroke(); ctx.fillStyle = css('--muted');
    ctx.fillText('max ' + fmt(mx) + ' · min ' + fmt(mn) + ' · length ' + (v.length - 1) + ' px · start (' + Math.round(l.x0) + ',' + Math.round(l.y0) + ') end (' + Math.round(l.x1) + ',' + Math.round(l.y1) + ')', 6, hh - 3);
  }

  /* ============================================================== histogram with draggable window handles */
  var HI = { xmax: 4096, drag: null };
  function histInit(cv) {
    cv.addEventListener('pointerdown', function (e) {
      var r = cv.getBoundingClientRect(), x = e.clientX - r.left, w = r.width, xl = S.lo / HI.xmax * w, xh = S.hi / HI.xmax * w;
      HI.drag = Math.abs(x - xl) <= Math.abs(x - xh) ? 'lo' : 'hi'; cv.setPointerCapture(e.pointerId); histMove(e);
    });
    cv.addEventListener('pointermove', function (e) { if (HI.drag) histMove(e); });
    cv.addEventListener('pointerup', function () { if (HI.drag) { HI.drag = null; requestRender('full'); } });
    cv.addEventListener('pointercancel', function () { HI.drag = null; });
  }
  function histMove(e) {
    var cv = $('hist'), r = cv.getBoundingClientRect(), v = Math.round(clamp((e.clientX - r.left) / r.width, 0, 1) * HI.xmax), w = params.win;
    if (w.mode !== 'abs') { w.mode = 'abs'; w.lo = Math.round(S.lo); w.hi = Math.round(S.hi); }
    if (HI.drag === 'lo') w.lo = clamp(v, 0, w.hi - 1); else w.hi = clamp(v, w.lo + 1, 65535);
    S.lo = w.lo; S.hi = w.hi; syncUI(); persist(); requestRender('full', true);
  }
  function drawHist() {
    var cv = $('hist'); if (!cv) return;
    var w = cv.clientWidth || 300, hh = cv.clientHeight || 96, dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(w * dpr); cv.height = Math.round(hh * dpr);
    var ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, hh);
    if (!S.hist) { ctx.fillStyle = '#8d97a4'; ctx.font = '12px system-ui'; ctx.fillText('histogram appears with the first frame', 8, 18); return; }
    var h = S.hist, n = 0, i, cum = 0, vmax = 255;
    for (i = 0; i < 65536; i++) n += h[i];
    for (i = 0; i < 65536; i++) { cum += h[i]; if (cum >= n * 0.99995) { vmax = i; break; } }
    HI.xmax = clamp(Math.ceil(Math.max(vmax * 1.1, S.hi * 1.08, 255)), 255, 65535);
    var nb = Math.max(32, Math.floor(w / 2)), bins = new Float64Array(nb), mx = 0;
    for (i = 0; i < 65536 && i <= HI.xmax; i++) bins[Math.min(nb - 1, Math.floor(i / HI.xmax * nb))] += h[i];
    for (i = 0; i < nb; i++) { bins[i] = Math.log1p(bins[i]); if (bins[i] > mx) mx = bins[i]; }
    ctx.fillStyle = '#5b8def';
    for (i = 0; i < nb; i++) { var bh = mx ? bins[i] / mx * (hh - 16) : 0; ctx.fillRect(i * w / nb, hh - 12 - bh, Math.max(1, w / nb - 0.5), bh); }
    var xl = S.lo / HI.xmax * w, xh = S.hi / HI.xmax * w;
    ctx.fillStyle = 'rgba(0,0,0,.45)'; ctx.fillRect(0, 0, xl, hh - 12); ctx.fillRect(xh, 0, w - xh, hh - 12);
    ctx.strokeStyle = '#ffd400'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(xl, 0); ctx.lineTo(xl, hh - 12); ctx.moveTo(xh, 0); ctx.lineTo(xh, hh - 12); ctx.stroke();
    ctx.fillStyle = '#aab4c0'; ctx.font = '10px system-ui'; ctx.fillText('0', 2, hh - 2); var t = fmt(HI.xmax); ctx.fillText(t, w - ctx.measureText(t).width - 2, hh - 2);
    ctx.fillText('log counts', w / 2 - 22, hh - 2);
  }

  /* ============================================================== saves */
  function wexport(kind, crop) {
    return new Promise(function (res, rej) { var id = ++S.xid; exportWaiters[id] = { res: res, rej: rej }; worker.postMessage({ t: 'export', id: id, kind: kind, crop: crop }); });
  }
  function saveBlob(blob, name) {
    var a = h('a', { href: URL.createObjectURL(blob), download: name, style: 'display:none' });
    document.body.append(a); a.click(); a.remove(); setTimeout(function () { URL.revokeObjectURL(a.href); }, 15000);
    notify('Saved ' + name + ' (' + fmt(blob.size) + ' bytes).', 'ok');
  }
  function saveCrop() {
    if (!params.crop.saves) return null;
    var c = params.crop, W = S.procW, H = S.procH;
    return [clamp(c.x0, 0, W - 1), clamp(c.y0, 0, H - 1), clamp(c.x1, 1, W), clamp(c.y1, 1, H)];
  }
  function fname(suffix, ext, dims) { var m = S.frame.meta; return 'flashpad_' + m.type + '_' + S.frame.stamp + '_' + suffix + (dims ? '_' + dims : '') + '.' + ext; }
  async function doSave(kind) {
    if (!S.frame) { notify('No frame to save yet.', 'err'); return; }
    try {
      if (kind === 'raw') { saveBlob(new Blob([S.frame.bytes], { type: 'application/octet-stream' }), fname('raw', 'raw')); return; }
      if (S.fullTimer) requestRender('full');                              // flush a pending full-resolution render first
      var crop = saveCrop(), r = await wexport(kind, crop), dims = crop ? r.w + 'x' + r.h : '';
      if (kind === 'raw16') saveBlob(new Blob([r.buf], { type: 'application/octet-stream' }), fname('processed', 'raw', dims));
      else saveBlob(new Blob([r.buf], { type: 'image/png' }), fname(kind === 'png8' ? 'display' : 'gray16', 'png', dims));
    } catch (e) { notify('Save failed: ' + e.message, 'err'); }
  }
  $('sv-raw').onclick = function () { doSave('raw'); };
  $('sv-proc').onclick = function () { doSave('raw16'); };
  $('sv-png').onclick = function () { doSave('png8'); };
  $('sv-png16').onclick = function () { doSave('png16'); };
  $('sv-crop').addEventListener('change', function (e) { params.crop.saves = e.target.checked; persist(); });

  /* ============================================================== theme + init */
  function applyTheme(t) { document.documentElement.dataset.theme = t; store('theme', t); }
  $('theme').onclick = function () {
    var cur = document.documentElement.dataset.theme, dark = cur === 'dark' || (cur === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    applyTheme(dark ? 'light' : 'dark'); setTimeout(function () { draw(); drawHist(); drawProfile(); updateCustom(); }, 30);
  };
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { draw(); drawHist(); });

  function init() {
    applyTheme(store('theme') || 'auto');
    var c = store('cap');
    if (c) { cap.win.value = c.win; cap.unit.value = c.unit; cap.scrubs.value = c.scrubs; cap.exp.value = c.exp; cap.dly.value = c.dly; }
    if (params.crop.saves === undefined) params.crop.saves = true;
    buildProc(); buildCfg(); syncUI();
    $$('input[name=darksrc]').forEach(function (r) { r.checked = r.value === S.darkSource; });
    initWorker(); selectTool('pan'); setZoomLabel();
    renderCapturePanel(); loadCfg(); poll();
  }
  init();
})();
