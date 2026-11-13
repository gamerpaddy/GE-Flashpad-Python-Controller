/*
 * web_ui - HTTP API (esp32s31/docs/API.md) + the embedded single-page app.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"

#include "fp_assets.h"
#include "fp_capture.h"
#include "fp_config.h"
#include "fp_dark.h"
#include "fp_eth.h"
#include "fp_image.h"
#include "fp_session.h"
#include "fp_wifi.h"
#include "web_ui.h"
#include "xray_relay.h"

static const char *TAG = "web_ui";
#define FW_VERSION "0.1.0"

static SemaphoreHandle_t s_dl_sem;       /* one frame/dark download at a time */

/* ------------------------------------------------------------------------------------------------
 * helpers
 * ---------------------------------------------------------------------------------------------- */
static esp_err_t send_json_status(httpd_req_t *req, const char *status, cJSON *o)
{
    char *s = cJSON_PrintUnformatted(o);
    cJSON_Delete(o);
    if (!s) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "json");
        return ESP_FAIL;
    }
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t e = httpd_resp_send(req, s, HTTPD_RESP_USE_STRLEN);
    free(s);
    return e;
}

static esp_err_t send_json(httpd_req_t *req, cJSON *o) { return send_json_status(req, "200 OK", o); }

static esp_err_t send_error(httpd_req_t *req, const char *status, const char *msg)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddBoolToObject(o, "ok", false);
    cJSON_AddStringToObject(o, "error", msg);
    return send_json_status(req, status, o);
}

static esp_err_t send_ok(httpd_req_t *req)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddBoolToObject(o, "ok", true);
    return send_json(req, o);
}

static cJSON *read_json_body(httpd_req_t *req)
{
    size_t len = req->content_len;
    if (len == 0 || len > 4096) return NULL;
    char *buf = malloc(len + 1);
    if (!buf) return NULL;
    size_t got = 0;
    while (got < len) {
        int r = httpd_req_recv(req, buf + got, len - got);
        if (r <= 0) { free(buf); return NULL; }
        got += (size_t)r;
    }
    buf[len] = 0;
    cJSON *o = cJSON_Parse(buf);
    free(buf);
    return o;
}

static double num_or(const cJSON *o, const char *key, double dflt)
{
    const cJSON *it = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsNumber(it) ? it->valuedouble : dflt;
}

/* ------------------------------------------------------------------------------------------------
 * /api/status
 * ---------------------------------------------------------------------------------------------- */
static void add_frame_meta(cJSON *o, const fp_frame_meta_t *m)
{
    cJSON_AddBoolToObject(o, "valid", m->valid);
    if (!m->valid) return;
    cJSON_AddNumberToObject(o, "seq", m->seq);
    cJSON_AddStringToObject(o, "type", m->xray ? "xray" : "dark");
    cJSON_AddNumberToObject(o, "image_id", m->image_id);
    cJSON_AddNumberToObject(o, "window_ticks", m->window_ticks);
    cJSON_AddNumberToObject(o, "window_ms", m->window_ms);
    cJSON_AddNumberToObject(o, "scrubs", m->scrubs);
    cJSON_AddNumberToObject(o, "exposure_ms", m->exposure_ms);
    cJSON_AddNumberToObject(o, "min", m->st.min);
    cJSON_AddNumberToObject(o, "max", m->st.max);
    cJSON_AddNumberToObject(o, "mean", m->st.mean);
    cJSON_AddNumberToObject(o, "std", m->st.std);
    char crc[12];
    snprintf(crc, sizeof crc, "%08x", (unsigned)m->st.crc32);
    cJSON_AddStringToObject(o, "crc32", crc);
    cJSON_AddNumberToObject(o, "bytes", FP_IMG_BYTES);
    cJSON_AddNumberToObject(o, "age_s", (double)((esp_timer_get_time() - m->t_us) / 1000000));
}

static esp_err_t h_status(httpd_req_t *req)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "fw", FW_VERSION);
    cJSON_AddNumberToObject(o, "uptime_s", (double)(esp_timer_get_time() / 1000000));

    fp_eth_link_t l = fp_eth_link();
    cJSON *lk = cJSON_AddObjectToObject(o, "link");
    cJSON_AddBoolToObject(lk, "up", l.up);
    cJSON_AddNumberToObject(lk, "speed", l.speed_mbps);
    cJSON_AddStringToObject(lk, "duplex", l.full_duplex ? "full" : "half");
    cJSON_AddStringToObject(lk, "mode", fp_link_mode_name(fp_eth_mode()));
    cJSON_AddStringToObject(lk, "ip", CONFIG_FP_HOST_IP);

    fp_wifi_status_t w = fp_wifi_status();
    cJSON *wf = cJSON_AddObjectToObject(o, "wifi");
    cJSON_AddStringToObject(wf, "ap_ssid", w.ap_ssid);
    cJSON_AddStringToObject(wf, "ap_ip", w.ap_ip);
    cJSON_AddBoolToObject(wf, "sta_connected", w.sta_connected);
    cJSON_AddStringToObject(wf, "sta_ssid", w.sta_ssid);
    cJSON_AddStringToObject(wf, "sta_ip", w.sta_ip);

    fp_det_info_t di = fp_session_info();
    fp_capture_status_t cs = fp_capture_status();
    cJSON *dt = cJSON_AddObjectToObject(o, "detector");
    cJSON_AddStringToObject(dt, "ip", di.ip);
    cJSON_AddStringToObject(dt, "state", fp_session_state_name(fp_session_state()));
    if (di.have_mac) {
        char mac[20];
        snprintf(mac, sizeof mac, "%02X:%02X:%02X:%02X:%02X:%02X", di.mac[0], di.mac[1], di.mac[2], di.mac[3], di.mac[4], di.mac[5]);
        cJSON_AddStringToObject(dt, "mac", mac);
    }
    if (di.sig_valid) {
        char fw[40];
        snprintf(fw, sizeof fw, "%u.%u.%u.%u.%u.%u.%u.%u", di.sig.fw[0], di.sig.fw[1], di.sig.fw[2], di.sig.fw[3], di.sig.fw[4], di.sig.fw[5], di.sig.fw[6], di.sig.fw[7]);
        cJSON_AddStringToObject(dt, "serial", di.sig.serial);
        cJSON_AddStringToObject(dt, "model", di.sig.model);
        cJSON_AddStringToObject(dt, "fw", fw);
    }
    cJSON_AddStringToObject(dt, "setup_mode", fp_setup_mode_name(fp_config_get()->setup_mode));
    cJSON_AddBoolToObject(dt, "script7_done", cs.script7_done);
    cJSON_AddNumberToObject(dt, "beacons", di.beacons);
    cJSON_AddNumberToObject(dt, "reconnects", di.reconnects);

    cJSON *cp = cJSON_AddObjectToObject(o, "capture");
    cJSON_AddStringToObject(cp, "state", fp_cap_state_name(cs.state));
    cJSON_AddStringToObject(cp, "step", cs.step);
    cJSON_AddStringToObject(cp, "type", cs.xray ? "xray" : "dark");
    cJSON_AddNumberToObject(cp, "blocks", cs.blocks);
    cJSON_AddNumberToObject(cp, "blocks_total", FP_IMG_BLOCKS);
    cJSON_AddNumberToObject(cp, "elapsed_ms", cs.elapsed_ms);
    cJSON_AddStringToObject(cp, "error", cs.error);
    cJSON_AddBoolToObject(cp, "fast_fallback", cs.fast_fallback);
    cJSON_AddBoolToObject(cp, "fired", cs.fired);

    fp_frame_meta_t fm = fp_capture_frame_meta();
    add_frame_meta(cJSON_AddObjectToObject(o, "frame"), &fm);

    fp_dark_meta_t dm = fp_dark_meta();
    cJSON *dk = cJSON_AddObjectToObject(o, "dark");
    cJSON_AddBoolToObject(dk, "valid", dm.valid);
    cJSON_AddBoolToObject(dk, "saving", fp_dark_saving());
    if (dm.valid) {
        cJSON_AddNumberToObject(dk, "window_ticks", dm.window_ticks);
        cJSON_AddNumberToObject(dk, "window_ms", dm.window_ms);
        cJSON_AddNumberToObject(dk, "scrubs", dm.scrubs);
        cJSON_AddNumberToObject(dk, "mean", dm.mean);
        cJSON_AddNumberToObject(dk, "std", dm.std);
        char crc[12];
        snprintf(crc, sizeof crc, "%08x", (unsigned)dm.crc32);
        cJSON_AddStringToObject(dk, "crc32", crc);
        uint32_t age = fp_dark_age_s();
        cJSON_AddNumberToObject(dk, "age_s", age == 0xFFFFFFFFu ? -1 : (double)age);
    }

    cJSON *xr = cJSON_AddObjectToObject(o, "xray");
    cJSON_AddBoolToObject(xr, "master_enabled", xray_relay_master());
    cJSON_AddBoolToObject(xr, "relay_on", xray_relay_is_on());
    cJSON_AddNumberToObject(xr, "gpio", xray_relay_gpio());
    cJSON_AddBoolToObject(xr, "active_high", xray_relay_active_high());
    cJSON_AddNumberToObject(xr, "max_pulse_ms", xray_relay_max_pulse_ms());
    cJSON_AddNumberToObject(xr, "fires", xray_relay_fire_count());
    cJSON_AddNumberToObject(xr, "last_fire_ms", xray_relay_last_pulse_ms());

    cJSON *hp = cJSON_AddObjectToObject(o, "heap");
    cJSON_AddNumberToObject(hp, "free_internal", (double)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    cJSON_AddNumberToObject(hp, "free_psram", (double)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    fp_image_counters_t ic = fp_image_counters();
    cJSON *dg = cJSON_AddObjectToObject(o, "debug");
    cJSON_AddNumberToObject(dg, "eth_rx_frames", fp_eth_rx_frames());
    cJSON_AddNumberToObject(dg, "img_accepted", ic.accepted);
    cJSON_AddNumberToObject(dg, "img_dup", ic.duplicate);
    cJSON_AddNumberToObject(dg, "img_bad", ic.bad);
    cJSON_AddNumberToObject(dg, "img_stray", ic.stray);
    fp_session_counters_t sc = fp_session_counters();
    cJSON_AddNumberToObject(dg, "ses_rx", sc.rx_packets);
    cJSON_AddNumberToObject(dg, "ses_dups", sc.rx_dups);
    cJSON_AddNumberToObject(dg, "ses_qdrops", sc.queue_drops);
    return send_json(req, o);
}

/* ------------------------------------------------------------------------------------------------
 * actions
 * ---------------------------------------------------------------------------------------------- */
static esp_err_t h_capture(httpd_req_t *req)
{
    cJSON *b = read_json_body(req);
    if (!b) return send_error(req, "400 Bad Request", "JSON body expected");
    const cJSON *ty = cJSON_GetObjectItemCaseSensitive(b, "type");
    const fp_config_t *cfg = fp_config_get();
    fp_capture_req_t rq = {
        .xray = cJSON_IsString(ty) && strcmp(ty->valuestring, "xray") == 0,
        .window_ms = (uint32_t)num_or(b, "window_ms", cfg->def_window_ms),
        .scrubs = (uint32_t)num_or(b, "scrubs", cfg->def_scrubs),
        .exposure_ms = (uint32_t)num_or(b, "exposure_ms", cfg->def_exposure_ms),
        .fire_delay_ms = (uint32_t)num_or(b, "fire_delay_ms", cfg->def_fire_delay_ms),
    };
    bool type_ok = cJSON_IsString(ty) && (!strcmp(ty->valuestring, "xray") || !strcmp(ty->valuestring, "dark"));
    cJSON_Delete(b);
    if (!type_ok) return send_error(req, "400 Bad Request", "type must be 'xray' or 'dark'");
    if (rq.window_ms < 20 || rq.window_ms > 160000) return send_error(req, "400 Bad Request", "window_ms must be 20..160000");
    if (rq.scrubs > 255) return send_error(req, "400 Bad Request", "scrubs must be 0..255");
    if (rq.xray) {
        if (rq.exposure_ms < 1 || rq.exposure_ms > xray_relay_max_pulse_ms()) return send_error(req, "400 Bad Request", "exposure_ms must be 1..max_pulse_ms");
        if (rq.fire_delay_ms > 5000) return send_error(req, "400 Bad Request", "fire_delay_ms must be 0..5000");
        if (rq.window_ms < rq.fire_delay_ms + rq.exposure_ms + 200) return send_error(req, "400 Bad Request", "window_ms must be >= fire_delay_ms + exposure_ms + 200");
    }
    char err[128];
    if (fp_capture_start(&rq, err, sizeof err) != ESP_OK) return send_error(req, "409 Conflict", err);
    httpd_resp_set_status(req, "202 Accepted");
    return send_ok(req);
}

static esp_err_t h_abort(httpd_req_t *req) { fp_capture_abort(); return send_ok(req); }
static esp_err_t h_reinit(httpd_req_t *req) { fp_capture_reinit(); return send_ok(req); }

static esp_err_t h_master(httpd_req_t *req)
{
    cJSON *b = read_json_body(req);
    if (!b) return send_error(req, "400 Bad Request", "JSON body expected");
    const cJSON *en = cJSON_GetObjectItemCaseSensitive(b, "enabled");
    bool want = cJSON_IsTrue(en);
    bool valid = cJSON_IsBool(en);
    cJSON_Delete(b);
    if (!valid) return send_error(req, "400 Bad Request", "'enabled' (bool) required");
    bool now = xray_relay_set_master(want);
    cJSON *o = cJSON_CreateObject();
    cJSON_AddBoolToObject(o, "ok", true);
    cJSON_AddBoolToObject(o, "master_enabled", now);
    return send_json(req, o);
}

static esp_err_t h_xray_test(httpd_req_t *req)
{
    cJSON *b = read_json_body(req);
    if (!b) return send_error(req, "400 Bad Request", "JSON body expected");
    uint32_t ms = (uint32_t)num_or(b, "ms", 100);
    cJSON_Delete(b);
    if (ms < 1 || ms > 500) return send_error(req, "400 Bad Request", "ms must be 1..500");
    if (!xray_relay_master()) return send_error(req, "409 Conflict", "X-ray master switch is off");
    if (fp_capture_status().state > CAP_IDLE && fp_capture_status().state < CAP_DONE) return send_error(req, "409 Conflict", "a capture is running");
    if (xray_relay_fire(ms, "test pulse") != ESP_OK) return send_error(req, "409 Conflict", "relay busy or refused");
    return send_ok(req);
}

static esp_err_t h_dark_from_frame(httpd_req_t *req)
{
    char err[128];
    if (fp_capture_dark_from_frame(err, sizeof err) != ESP_OK) return send_error(req, "409 Conflict", err);
    return send_ok(req);
}

static esp_err_t h_validate_fast(httpd_req_t *req)
{
    return send_error(req, "501 Not Implemented", "validate_fast is not implemented yet");
}

/* ------------------------------------------------------------------------------------------------
 * config / log
 * ---------------------------------------------------------------------------------------------- */
static esp_err_t h_config_get(httpd_req_t *req) { return send_json(req, fp_config_to_json()); }

static esp_err_t h_config_post(httpd_req_t *req)
{
    cJSON *b = read_json_body(req);
    if (!b) return send_error(req, "400 Bad Request", "JSON body expected");
    bool reboot = false;
    char err[96] = "";
    esp_err_t e = fp_config_apply_json(b, &reboot, err, sizeof err);
    cJSON_Delete(b);
    if (e != ESP_OK) return send_error(req, "400 Bad Request", err[0] ? err : "invalid config");
    cJSON *o = cJSON_CreateObject();
    cJSON_AddBoolToObject(o, "ok", true);
    cJSON_AddBoolToObject(o, "reboot_required", reboot);
    return send_json(req, o);
}

static esp_err_t h_log(httpd_req_t *req)
{
    char *buf = malloc(8192);
    if (!buf) return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "oom");
    size_t n = fp_log_ring_read(buf, 8192);
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t e = httpd_resp_send(req, buf, (ssize_t)n);
    free(buf);
    return e;
}

static esp_err_t h_reboot(httpd_req_t *req)
{
    send_ok(req);
    vTaskDelay(pdMS_TO_TICKS(300));
    esp_restart();
    return ESP_OK;
}

/* ------------------------------------------------------------------------------------------------
 * frame / dark downloads (async worker so the server keeps answering /api/status meanwhile)
 * ---------------------------------------------------------------------------------------------- */
typedef struct { httpd_req_t *req; bool dark; fp_frame_meta_t fm; fp_dark_meta_t dm; } dl_arg_t;

static void dl_task(void *a)
{
    dl_arg_t *d = a;
    httpd_req_t *req = d->req;
    char h1[24], h2[24], h3[24], h4[24], h5[24], h6[24], h7[24], h8[24], h9[24];
    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_set_hdr(req, "Access-Control-Expose-Headers", "*");
    if (!d->dark) {
        const fp_frame_meta_t *m = &d->fm;
        snprintf(h1, sizeof h1, "%u", (unsigned)m->seq);          httpd_resp_set_hdr(req, "X-Frame-Seq", h1);
        httpd_resp_set_hdr(req, "X-Frame-Type", m->xray ? "xray" : "dark");
        snprintf(h2, sizeof h2, "%08x", (unsigned)m->st.crc32);   httpd_resp_set_hdr(req, "X-Frame-Crc32", h2);
        snprintf(h3, sizeof h3, "%u", (unsigned)m->window_ticks); httpd_resp_set_hdr(req, "X-Window-Ticks", h3);
        snprintf(h4, sizeof h4, "%u", (unsigned)m->window_ms);    httpd_resp_set_hdr(req, "X-Window-Ms", h4);
        snprintf(h5, sizeof h5, "%u", (unsigned)m->scrubs);       httpd_resp_set_hdr(req, "X-Scrubs", h5);
        snprintf(h6, sizeof h6, "%u", (unsigned)m->exposure_ms);  httpd_resp_set_hdr(req, "X-Exposure-Ms", h6);
        snprintf(h7, sizeof h7, "%u", (unsigned)m->image_id);     httpd_resp_set_hdr(req, "X-Image-Id", h7);
        snprintf(h8, sizeof h8, "%.2f", m->st.mean);              httpd_resp_set_hdr(req, "X-Frame-Mean", h8);
        snprintf(h9, sizeof h9, "%.2f", m->st.std);               httpd_resp_set_hdr(req, "X-Frame-Std", h9);
        int64_t t0 = esp_timer_get_time();
        esp_err_t e = httpd_resp_send(req, (const char *)fp_image_buf(), FP_IMG_BYTES);
        ESP_LOGI(TAG, "frame download %s in %d ms", e == ESP_OK ? "done" : "FAILED", (int)((esp_timer_get_time() - t0) / 1000));
        fp_image_read_end();
    } else {
        const fp_dark_meta_t *m = &d->dm;
        snprintf(h1, sizeof h1, "%u", (unsigned)m->window_ticks); httpd_resp_set_hdr(req, "X-Window-Ticks", h1);
        snprintf(h2, sizeof h2, "%u", (unsigned)m->window_ms);    httpd_resp_set_hdr(req, "X-Window-Ms", h2);
        snprintf(h3, sizeof h3, "%u", (unsigned)m->scrubs);       httpd_resp_set_hdr(req, "X-Scrubs", h3);
        snprintf(h4, sizeof h4, "%08x", (unsigned)m->crc32);      httpd_resp_set_hdr(req, "X-Frame-Crc32", h4);
        snprintf(h5, sizeof h5, "%u", (unsigned)FP_IMG_BYTES);    httpd_resp_set_hdr(req, "X-Content-Bytes", h5);
        snprintf(h6, sizeof h6, "%.2f", m->mean);                 httpd_resp_set_hdr(req, "X-Frame-Mean", h6);
        snprintf(h7, sizeof h7, "%.2f", m->std);                  httpd_resp_set_hdr(req, "X-Frame-Std", h7);
        uint32_t age = fp_dark_age_s();
        snprintf(h8, sizeof h8, "%d", age == 0xFFFFFFFFu ? -1 : (int)age); httpd_resp_set_hdr(req, "X-Dark-Age-S", h8);
        httpd_resp_set_hdr(req, "X-Frame-Type", "dark");
        /* Stream with a real Content-Length (browser shows a true progress bar) and big writes, like the frame path.
         * httpd's chunked mode = 3 tiny writes per chunk, which crawled over Wi-Fi (minutes). Headers are written by hand. */
        int fd = httpd_req_to_sockfd(req);
        int one = 1;
        if (fd >= 0) setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
        const size_t CH = 131072;
        uint8_t *chunk = heap_caps_malloc(CH, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        esp_err_t e = (chunk && fd >= 0) ? ESP_OK : ESP_ERR_NO_MEM;
        int64_t t0 = esp_timer_get_time();
        if (e == ESP_OK) {
            char hdr[640];
            int hl = snprintf(hdr, sizeof hdr,
                "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: %u\r\nCache-Control: no-store\r\n"
                "Access-Control-Expose-Headers: *\r\nX-Window-Ticks: %s\r\nX-Window-Ms: %s\r\nX-Scrubs: %s\r\nX-Frame-Crc32: %s\r\n"
                "X-Content-Bytes: %s\r\nX-Frame-Mean: %s\r\nX-Frame-Std: %s\r\nX-Dark-Age-S: %s\r\nX-Frame-Type: dark\r\n"
                "Connection: close\r\n\r\n", (unsigned)FP_IMG_BYTES, h1, h2, h3, h4, h5, h6, h7, h8);
            for (int off = 0; e == ESP_OK && off < hl;) {
                int w = httpd_socket_send(req->handle, fd, hdr + off, hl - off, 0);
                if (w <= 0) e = ESP_FAIL; else off += w;
            }
        }
        for (size_t off = 0; e == ESP_OK && off < FP_IMG_BYTES; off += CH) {
            e = fp_dark_read(off, chunk, CH);
            for (size_t sent = 0; e == ESP_OK && sent < CH;) {
                int w = httpd_socket_send(req->handle, fd, (const char *)chunk + sent, (int)(CH - sent), 0);
                if (w <= 0) e = ESP_FAIL; else sent += (size_t)w;
            }
        }
        ESP_LOGI(TAG, "dark stream took %d ms", (int)((esp_timer_get_time() - t0) / 1000));
        free(chunk);
        ESP_LOGI(TAG, "dark download %s", e == ESP_OK ? "done" : "FAILED");
        if (fd >= 0) httpd_sess_trigger_close(req->handle, fd);
    }
    httpd_req_async_handler_complete(req);
    free(d);
    xSemaphoreGive(s_dl_sem);
    vTaskDelete(NULL);
}

static esp_err_t start_download(httpd_req_t *req, bool dark)
{
    fp_frame_meta_t fm = fp_capture_frame_meta();
    fp_dark_meta_t dm = fp_dark_meta();
    if (!dark && !fm.valid) return send_error(req, "404 Not Found", "no frame captured yet");
    if (dark && !dm.valid) return send_error(req, "404 Not Found", "no dark stored");
    if (xSemaphoreTake(s_dl_sem, 0) != pdTRUE) return send_error(req, "409 Conflict", "another download is in progress");
    if (!dark && !fp_image_read_begin()) {
        xSemaphoreGive(s_dl_sem);
        return send_error(req, "409 Conflict", "a capture is writing the frame buffer");
    }
    httpd_req_t *copy = NULL;
    dl_arg_t *d = calloc(1, sizeof *d);
    if (!d || httpd_req_async_handler_begin(req, &copy) != ESP_OK) {
        free(d);
        if (!dark) fp_image_read_end();
        xSemaphoreGive(s_dl_sem);
        return send_error(req, "500 Internal Server Error", "cannot start download");
    }
    d->req = copy;
    d->dark = dark;
    d->fm = fm;
    d->dm = dm;
    if (xTaskCreate(dl_task, "dl", 6144, d, 5, NULL) != pdPASS) {
        httpd_req_async_handler_complete(copy);
        free(d);
        if (!dark) fp_image_read_end();
        xSemaphoreGive(s_dl_sem);
        return ESP_FAIL;
    }
    return ESP_OK;
}

static esp_err_t h_frame(httpd_req_t *req) { return start_download(req, false); }
static esp_err_t h_dark(httpd_req_t *req) { return start_download(req, true); }

/* ------------------------------------------------------------------------------------------------
 * static assets
 * ---------------------------------------------------------------------------------------------- */
static const char FALLBACK_HTML[] =
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>FlashPad ESP32</title>"
    "<body style='font-family:sans-serif;margin:16px'><h1>FlashPad ESP32-S31</h1><p>The web app is not embedded in this firmware build "
    "(run <code>python tools/build_www.py</code> and rebuild). The API works:</p><ul><li><a href=/api/status>/api/status</a></li>"
    "<li><a href=/api/log>/api/log</a></li><li><a href=/api/config>/api/config</a></li></ul></body>";

static esp_err_t h_static(httpd_req_t *req)
{
    const char *uri = req->uri;
    char path[96];
    size_t n = strcspn(uri, "?#");
    if (n >= sizeof path) return send_error(req, "404 Not Found", "not found");
    memcpy(path, uri, n);
    path[n] = 0;
    if (strcmp(path, "/") == 0) strcpy(path, "/index.html");
    for (size_t i = 0; i < fp_assets_count; i++) {
        if (strcmp(fp_assets[i].path, path) == 0) {
            httpd_resp_set_type(req, fp_assets[i].mime);
            httpd_resp_set_hdr(req, "Content-Encoding", "gzip");
            httpd_resp_set_hdr(req, "Cache-Control", "no-cache");
            return httpd_resp_send(req, (const char *)fp_assets[i].start, (ssize_t)(fp_assets[i].end - fp_assets[i].start));
        }
    }
    if (strcmp(path, "/index.html") == 0) {
        httpd_resp_set_type(req, "text/html; charset=utf-8");
        return httpd_resp_send(req, FALLBACK_HTML, HTTPD_RESP_USE_STRLEN);
    }
    if (strcmp(path, "/favicon.ico") == 0) {
        httpd_resp_set_status(req, "204 No Content");
        return httpd_resp_send(req, NULL, 0);
    }
    /* captive portal: Android (generate_204), iOS (hotspot-detect), Windows (connecttest) and any typed name that the
     * wildcard DNS sent here get redirected to the UI instead of a 404 */
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, NULL, 0);
}

/* ------------------------------------------------------------------------------------------------
 * server
 * ---------------------------------------------------------------------------------------------- */
esp_err_t web_ui_start(void)
{
    s_dl_sem = xSemaphoreCreateBinary();
    xSemaphoreGive(s_dl_sem);

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.max_uri_handlers = 24;
    cfg.max_open_sockets = 8;
    cfg.stack_size = 8192;
    cfg.lru_purge_enable = true;
    cfg.send_wait_timeout = 20;
    cfg.recv_wait_timeout = 10;
    cfg.uri_match_fn = httpd_uri_match_wildcard;

    httpd_handle_t srv = NULL;
    esp_err_t e = httpd_start(&srv, &cfg);
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "httpd_start failed: %s", esp_err_to_name(e));
        return e;
    }
    static const httpd_uri_t routes[] = {
        { .uri = "/api/status",          .method = HTTP_GET,  .handler = h_status },
        { .uri = "/api/capture",         .method = HTTP_POST, .handler = h_capture },
        { .uri = "/api/abort",           .method = HTTP_POST, .handler = h_abort },
        { .uri = "/api/reinit",          .method = HTTP_POST, .handler = h_reinit },
        { .uri = "/api/xray/master",     .method = HTTP_POST, .handler = h_master },
        { .uri = "/api/xray/test",       .method = HTTP_POST, .handler = h_xray_test },
        { .uri = "/api/dark/from_frame", .method = HTTP_POST, .handler = h_dark_from_frame },
        { .uri = "/api/validate_fast",   .method = HTTP_POST, .handler = h_validate_fast },
        { .uri = "/api/frame",           .method = HTTP_GET,  .handler = h_frame },
        { .uri = "/api/dark",            .method = HTTP_GET,  .handler = h_dark },
        { .uri = "/api/config",          .method = HTTP_GET,  .handler = h_config_get },
        { .uri = "/api/config",          .method = HTTP_POST, .handler = h_config_post },
        { .uri = "/api/log",             .method = HTTP_GET,  .handler = h_log },
        { .uri = "/api/reboot",          .method = HTTP_POST, .handler = h_reboot },
        { .uri = "/*",                   .method = HTTP_GET,  .handler = h_static },
    };
    for (size_t i = 0; i < sizeof routes / sizeof routes[0]; i++) {
        ESP_ERROR_CHECK(httpd_register_uri_handler(srv, &routes[i]));
    }
    ESP_LOGI(TAG, "HTTP server up on port 80 (%u embedded assets)", (unsigned)fp_assets_count);
    return ESP_OK;
}
