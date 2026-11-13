# ESP32-S31 FlashPad controller - HTTP API contract

Single source of truth shared by the firmware (`components/web_ui`), the browser app (`www/`), the mock server (`tools/mock_esp_server.py`) and
the end-to-end test (`tools/e2e_test.py`). Base URL: `http://<esp>/` (SoftAP default `192.168.4.1`, Ethernet `192.168.1.1`).
JSON in/out unless stated, UTF-8. No auth in v1 (SoftAP password only). All POSTs take `Content-Type: application/json`.
Errors: HTTP 400/409/500 with `{"ok":false,"error":"human readable"}`. Success of an action: `{"ok":true}` (+ extra fields).

## GET /api/status  (poll at ~4 Hz; small, never blocks on the capture task)

```json
{
  "fw": "0.1.0", "uptime_s": 1234,
  "link":     {"up": true, "speed": 100, "duplex": "full", "mode": "adv100fd", "ip": "192.168.1.1"},
  "wifi":     {"ap_ssid": "FlashPad-ESP", "ap_ip": "192.168.4.1", "sta_connected": false, "sta_ssid": "", "sta_ip": ""},
  "detector": {"ip": "192.168.1.30", "state": "ready", "mac": "40:F4:A0:00:78:4D", "serial": "UA45829-7", "model": "5340000-7", "fw": "1.6.0.4.2.0.1.3",
               "setup_mode": "session", "script7_done": true},
  "capture":  {"state": "idle", "step": "", "type": "xray", "blocks": 0, "blocks_total": 2048, "elapsed_ms": 0, "error": "", "fast_fallback": false},
  "frame":    {"valid": true, "seq": 17, "type": "xray", "image_id": 0, "window_ticks": 52600000, "window_ms": 2000, "scrubs": 0, "exposure_ms": 250,
               "min": 120, "max": 9000, "mean": 3260.7, "std": 410.2, "crc32": "9a3b00ff", "bytes": 8388608, "age_s": 12},
  "dark":     {"valid": true, "window_ticks": 52600000, "window_ms": 2000, "scrubs": 0, "mean": 1789.2, "std": 21.4, "crc32": "11223344", "age_s": 3600, "saving": false},
  "xray":     {"master_enabled": false, "relay_on": false, "gpio": 2, "active_high": true, "max_pulse_ms": 3000, "fires": 0, "last_fire_ms": 0},
  "heap":     {"free_internal": 123456, "free_psram": 7000000}
}
```
- `detector.state`: `link_down | discovering | connecting | ready | busy | lost | error`.
- `capture.state`: `idle | arming | wait_window | exposing | readout | receiving | verifying | saving_dark | done | error | aborted`; `step` is free text for the UI log line.
- `capture.error` non-empty only in `error`. `capture.fast_fallback` true once `fast` mode escalated to `full` this session.
- `frame.seq` increments on every completed capture (also dark); the UI refetches `/api/frame` when it changes. `frame.valid` false until the first capture after boot.
- `window_ticks` = ms x 26.3e3 (rounded); `window_ms` is what the user entered.

## POST /api/capture
`{"type":"xray"|"dark","window_ms":2000,"scrubs":0,"exposure_ms":250,"fire_delay_ms":20}` -> `202 {"ok":true}` immediately; progress via `/api/status`.
409 if a capture is running or the dark is still being saved; 400 on invalid values (`window_ms` 20..160000, `scrubs` 0..255, `exposure_ms` 1..`max_pulse_ms`,
`fire_delay_ms` 0..5000; for xray `window_ms >= fire_delay_ms + exposure_ms + 200`). `exposure_ms`/`fire_delay_ms` are ignored for `dark`.
`type:"xray"` with `xray.master_enabled == false` still runs the sequence but never pulses the relay (status `step` says "armed - external firing").

## POST /api/abort  -> relay OFF immediately, aborts capture, `{"ok":true}`
## POST /api/reinit -> drops the session and redoes discovery + PORT_SETUP + Script7, `{"ok":true}`
## POST /api/xray/master `{"enabled":true|false}` -> `{"ok":true,"master_enabled":bool}` (always false after boot; also auto-clears after idle timeout)
## POST /api/xray/test `{"ms":100}` -> test pulse (<=500 ms, needs master enabled) `{"ok":true}`
## POST /api/dark/from_frame -> copy the current frame into the dark store (flash) `{"ok":true}`; progress shows `dark.saving`

## GET /api/frame  (binary)
`200 application/octet-stream`, `Content-Length: 8388608`, body = 2048x2048 u16 little-endian, row-major, row 0 = top (= block 0). Headers:
`X-Frame-Seq`, `X-Frame-Type` (xray|dark), `X-Frame-Crc32` (hex), `X-Window-Ticks`, `X-Window-Ms`, `X-Scrubs`, `X-Exposure-Ms`, `X-Image-Id`, `X-Frame-Mean`, `X-Frame-Std`.
404 if no frame yet. Streamed in chunks; the browser may read it with `fetch` + `ReadableStream` to show download progress.
## GET /api/dark (binary) same layout/headers for the stored dark (`X-Window-Ticks`, `X-Scrubs`, `X-Dark-Age-S`); 404 if none.

## GET /api/config, POST /api/config  (NVS-backed; POST accepts any subset; reply `{"ok":true,"reboot_required":bool}`)
```json
{ "detector_ip": "192.168.1.30",
  "link_mode": "adv100fd",              "// adv100fd | forced100fd | auto",
  "setup_mode": "session",              "// full | session | fast | combined",
  "fire_trigger": "window_open",        "// window_open | after_execute",
  "early_close": false,
  "relay_gpio": 2, "relay_active_high": true, "relay_max_pulse_ms": 3000, "xray_idle_disable_min": 10,
  "ap_ssid": "FlashPad-ESP", "ap_pass": "flashpad1", "sta_ssid": "", "sta_pass": "",
  "defaults": {"window_ms": 2000, "scrubs": 0, "exposure_ms": 250, "fire_delay_ms": 20} }
```
Passwords are returned as `""` by GET (write-only).

## GET /api/log  -> `text/plain`, last ~8 KB of the device log ring buffer
## POST /api/validate_fast -> starts the "validate fast mode" routine (dark in `full`, dark in `fast`, compare) `{"ok":true}`; result appears in `/api/status` under `"validate":{"state","msg","mean_full","mean_fast","std_full","std_fast"}`

## Static assets
`GET /` -> `index.html` (gzip, `Content-Encoding: gzip`), plus `/app.js`, `/proc-worker.js`, `/palettes.js`, `/png16.js`, `/app.css`.
