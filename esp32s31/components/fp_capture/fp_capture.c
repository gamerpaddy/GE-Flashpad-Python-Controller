#include "fp_capture.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "fp_config.h"
#include "fp_dark.h"
#include "fp_eth.h"
#include "fp_image.h"
#include "fp_proto.h"
#include "fp_session.h"
#include "xray_relay.h"

static const char *TAG = "fp_capture";

/* ------------------------------------------------------------------------------------------------
 * state shared with the web UI
 * ---------------------------------------------------------------------------------------------- */
static SemaphoreHandle_t s_lock;
static QueueHandle_t s_req_q;
static fp_capture_status_t s_st;
static fp_frame_meta_t s_meta;
static volatile bool s_abort;
static volatile bool s_running;
static int64_t s_t_start_us;
static uint32_t s_s7_reconnects = 0xFFFFFFFFu;      /* session generation in which Script7 last completed OK */
static uint32_t s_seq;
static fp_capture_req_t s_last_req;

static const char *const STATE_NAMES[] = { "idle", "arming", "wait_window", "exposing", "readout", "receiving", "verifying", "saving_dark", "done", "error", "aborted" };
const char *fp_cap_state_name(fp_cap_state_t s) { return (unsigned)s < 11 ? STATE_NAMES[s] : "?"; }

static void set_state(fp_cap_state_t st, const char *fmt, ...)
{
    char step[96] = "";
    if (fmt) {
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(step, sizeof step, fmt, ap);
        va_end(ap);
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_st.state = st;
    if (fmt) strlcpy(s_st.step, step, sizeof s_st.step);
    xSemaphoreGive(s_lock);
    if (fmt) ESP_LOGI(TAG, "[%s] %s", fp_cap_state_name(st), step);
}

fp_capture_status_t fp_capture_status(void)
{
    fp_capture_status_t s;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s = s_st;
    xSemaphoreGive(s_lock);
    s.blocks = (s.state >= CAP_ARMING && s.state <= CAP_VERIFYING) || s.state == CAP_DONE ? fp_image_blocks() : s.blocks;
    if (s_running) s.elapsed_ms = (uint32_t)((esp_timer_get_time() - s_t_start_us) / 1000);
    s.script7_done = (s_s7_reconnects == fp_session_info().reconnects);
    if ((s.state == CAP_DONE || s.state == CAP_IDLE) && fp_dark_saving()) {
        s.state = CAP_SAVING_DARK;
        strlcpy(s.step, "saving dark frame to flash", sizeof s.step);
    }
    return s;
}

fp_frame_meta_t fp_capture_frame_meta(void)
{
    fp_frame_meta_t m;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    m = s_meta;
    xSemaphoreGive(s_lock);
    return m;
}

/* ------------------------------------------------------------------------------------------------
 * message tracker: everything the detector says during a capture is folded into this struct
 * ---------------------------------------------------------------------------------------------- */
typedef struct {
    bool script_reply;   int script_status;
    bool exec_reply;     int exec_status;  uint32_t exec_time;
    bool ev17;
    bool ev1;            int64_t ev1_us;
    bool win_expired;
    bool exec_complete;  int64_t exec_complete_us;
    int xfer_seen;       bool xfer_pending;  int64_t xfer_us;
    bool r41;            int r41_count;
    bool r98;            uint32_t r98_status;
} ctx_t;

static void pump(ctx_t *c, uint32_t timeout_ms)
{
    fp_msg_t m;
    if (!fp_session_wait_msg(&m, timeout_ms)) return;
    fp_pdap_t p = { .cmd = m.cmd, .payload_len = m.declared, .payload = m.data, .payload_avail = m.len };
    switch (m.cmd) {
    case FP_CMD_GENERIC_SCRIPT: {
        uint32_t cnt;
        c->script_status = fp_parse_script_reply(&p, &cnt);
        c->script_reply = true;
        ESP_LOGD(TAG, "script reply status=%d count=%u", c->script_status, (unsigned)cnt);
        break;
    }
    case FP_CMD_EXECUTE_SCRIPT:
        c->exec_status = fp_parse_execute_reply(&p, &c->exec_time);
        c->exec_reply = true;
        ESP_LOGI(TAG, "EXECUTE reply: status byte %d (time field 0x%08x)", c->exec_status, (unsigned)c->exec_time);
        break;
    case FP_CMD_STATE_NOTIFY: {
        uint32_t ev = 0;
        fp_parse_state_notify(&p, &ev);
        ESP_LOGI(TAG, "detector event 0x%x", (unsigned)ev);
        if (ev == FP_EVENT_ROE_DONE) c->ev17 = true;
        if (ev == FP_EVENT_WINDOW_OPEN) { c->ev1 = true; c->ev1_us = m.t_us; }
        break;
    }
    case FP_CMD_WINDOW_EXPIRED:
        c->win_expired = true;
        ESP_LOGI(TAG, "integration window expired");
        break;
    case FP_CMD_EXECUTION_COMPLETE:
        c->exec_complete = true;
        c->exec_complete_us = m.t_us;
        ESP_LOGI(TAG, "EXECUTION_COMPLETE");
        break;
    case FP_CMD_IMG_XFER_QUERY: {
        uint16_t iid = 0, sid = 0;
        int r = fp_parse_xfer_query(&p, &iid, &sid);
        if (r == 0) {
            c->xfer_seen++;
            c->xfer_pending = true;
            c->xfer_us = m.t_us;
            ESP_LOGI(TAG, "image_xfer_status query #%d (image 0x%04x, script %u)", c->xfer_seen, iid, sid);
        } else {
            ESP_LOGD(TAG, "image_xfer_status query ignored (r=%d)", r);
        }
        break;
    }
    case FP_CMD_IMG_RETRIEVAL_REQ:
        c->r41_count = fp_parse_retrieval_request_reply(&p, NULL);
        c->r41 = true;
        ESP_LOGI(TAG, "0x41 reply: %d image(s)", c->r41_count);
        break;
    case FP_CMD_IMG_RETRIEVAL:
        c->r98_status = m.len >= 4 ? fp_get_u32(m.data) : 0xFFFFFFFFu;
        c->r98 = true;
        ESP_LOGI(TAG, "0x98 reply: status %u", (unsigned)c->r98_status);
        break;
    default:
        ESP_LOGD(TAG, "ignored detector message 0x%x (len %u)", (unsigned)m.cmd, (unsigned)m.declared);
        break;
    }
}

/* ------------------------------------------------------------------------------------------------
 * helpers
 * ---------------------------------------------------------------------------------------------- */
typedef struct { char msg[128]; bool recoverable; } fail_t;
static fail_t s_fail;

static esp_err_t fail(bool recoverable, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s_fail.msg, sizeof s_fail.msg, fmt, ap);
    va_end(ap);
    s_fail.recoverable = recoverable;
    ESP_LOGE(TAG, "capture failed: %s", s_fail.msg);
    return ESP_FAIL;
}

#define ABORT_CHECK() do { if (s_abort) return fail(false, "aborted"); } while (0)
#define TRY(x) do { esp_err_t _e = (x); if (_e != ESP_OK) return _e; } while (0)

static int64_t now_us(void) { return esp_timer_get_time(); }

static esp_err_t send_pdap_ack(const uint8_t *pdap, size_t n, const char *what, bool recoverable)
{
    uint32_t seq;
    if (fp_session_send(pdap, n, &seq) != ESP_OK) return fail(recoverable, "%s: send failed", what);
    if (!fp_session_wait_ack(seq, 3000)) return fail(recoverable, "%s: not acknowledged by the detector", what);
    return ESP_OK;
}

static esp_err_t download_script(ctx_t *c, const uint8_t *script, size_t n, const char *name, bool recoverable)
{
    c->script_reply = false;
    TRY(send_pdap_ack(script, n, name, recoverable));
    /* the optional cmd-5 reply carries a status byte (0 ok, 1 list full, 2 rejected) */
    int64_t end = now_us() + 400000;
    while (!c->script_reply && now_us() < end) pump(c, 40);
    if (c->script_reply && c->script_status != 0) {
        return fail(recoverable, "%s rejected by the detector (status %d)", name, c->script_status);
    }
    return ESP_OK;
}

static esp_err_t do_execute(ctx_t *c, const char *what, bool recoverable, int64_t *ack_us)
{
    uint8_t pdap[16];
    size_t n = fp_build_execute(pdap, sizeof pdap);
    c->exec_reply = false;
    TRY(send_pdap_ack(pdap, n, what, recoverable));
    if (ack_us) *ack_us = now_us();
    int64_t end = now_us() + 3000000;
    while (!c->exec_reply && now_us() < end) { ABORT_CHECK(); pump(c, 40); }
    if (!c->exec_reply) return fail(recoverable, "%s: no EXECUTE reply", what);
    if (c->exec_status != 0) {
        return fail(recoverable, "%s: detector refused (status %d: %s)", what, c->exec_status,
                    c->exec_status == 1 ? "no script loaded" : c->exec_status == 2 ? "detector busy - power-cycle it if this persists" : "unknown");
    }
    return ESP_OK;
}

static void reply_xfer(const uint16_t *ids, uint32_t n)
{
    static uint8_t pkt[FP_MAX_PACKET];            /* only the capture task builds these */
    size_t len = fp_build_xfer_reply(pkt, sizeof pkt, ids, n);
    if (len) fp_session_send(pkt, len, NULL);
}

/* ------------------------------------------------------------------------------------------------
 * one capture attempt
 * ---------------------------------------------------------------------------------------------- */
static esp_err_t run_once(const fp_capture_req_t *rq, fp_setup_mode_t mode, bool allow_recover)
{
    ctx_t c;
    memset(&c, 0, sizeof c);
    const fp_config_t *cfg = fp_config_get();
    fp_det_info_t di = fp_session_info();
    const bool rec = allow_recover && mode == FP_SETUP_FAST;

    ABORT_CHECK();
    set_state(CAP_ARMING, "preparing (%s mode)", fp_setup_mode_name(mode));
    fp_session_flush_msgs();

    /* ---- Script 7 (ROE init) ---- */
    bool need_s7 = true;
    if (mode == FP_SETUP_FAST && s_s7_reconnects == di.reconnects) need_s7 = false;
    bool combined = (mode == FP_SETUP_COMBINED);
    static uint8_t buf[FP_MAX_PACKET];             /* static: keeps the capture task stack small (single capture task) */

    if (need_s7 && !combined) {
        set_state(CAP_ARMING, "ROE initialisation (script 7)");
        size_t n = fp_build_script7(buf, sizeof buf);
        TRY(download_script(&c, buf, n, "script 7", rec));
        TRY(do_execute(&c, "script 7", rec, NULL));
        int64_t end = now_us() + 20000000;
        while (!c.exec_complete && now_us() < end) { ABORT_CHECK(); pump(&c, 50); }
        if (!c.exec_complete) return fail(rec, "script 7 never completed");
        s_s7_reconnects = di.reconnects;
        c.exec_complete = false;
        c.ev17 = false;
        fp_session_flush_msgs();
    } else if (!combined) {
        ESP_LOGI(TAG, "fast mode: skipping script 7 (done earlier in this session)");
    }

    /* ---- acquisition script ---- */
    const uint32_t window_ticks = fp_ms_to_ticks(rq->window_ms);
    fp_acq_params_t ap = { .type_mode = 0, .image_id = 1, .no_scrubs = (uint8_t)rq->scrubs, .scrub_duration = 50000, .max_expose = window_ticks,
                           .tail_time = 250000, .transfer_mode = 0 };
    size_t an = fp_build_acq_script(buf, sizeof buf, &ap);
    set_state(CAP_ARMING, "downloading acquisition script (window %u ms = %u ticks)", (unsigned)rq->window_ms, (unsigned)window_ticks);
    if (combined && need_s7) {
        static uint8_t s7[FP_MAX_PACKET];
        size_t n7 = fp_build_script7(s7, sizeof s7);
        TRY(download_script(&c, s7, n7, "script 7 (combined)", false));
    }
    TRY(download_script(&c, buf, an, "acquisition script", rec));

    /* ---- EXECUTE ---- */
    if (!fp_image_write_begin()) return fail(false, "frame buffer is in use (download or dark save running)");
    fp_image_begin();                       /* open the RX hook before the detector can push anything */
    int64_t exec_ack_us = 0;
    set_state(CAP_WAIT_WINDOW, rq->xray ? "executing - waiting for the window to open" : "executing dark acquisition");
    TRY(do_execute(&c, "acquisition", rec, &exec_ack_us));
    if (combined) s_s7_reconnects = di.reconnects;

    /* ---- main wait loop ---- */
    const bool trigger_on_window = (cfg->fire_trigger == FP_FIRE_WINDOW_OPEN);
    const bool master = xray_relay_master();
    bool fire_scheduled = false, fired = false, early_sent = false, fire_skipped = false;
    int64_t fire_at = 0, pulse_end = 0;
    bool first_reply_done = false, pulled = false, sent41 = false, sent98 = false, pending_query = false;
    int64_t t41 = 0, t98 = 0, t_pull = 0, last_reply = 0;
    int retries = 0;
    int64_t t_overall_end = exec_ack_us + (int64_t)(rq->window_ms + 20000 + rq->scrubs * 5) * 1000 + 30000000;
    bool complete_wait = false; int64_t complete_t = 0;

    for (;;) {
        ABORT_CHECK();
        pump(&c, 20);
        const int64_t t = now_us();
        if (t > t_overall_end) return fail(rec, "timeout waiting for the detector");

        /* -- relay timing (X-ray only) -- */
        if (rq->xray && !fire_scheduled) {
            if (trigger_on_window) {
                if (c.ev1) { fire_at = c.ev1_us + (int64_t)rq->fire_delay_ms * 1000; fire_scheduled = true; }
                else if (t - exec_ack_us > 3000000) {
                    return fail(false, "detector never reported 'window open' - the X-ray was NOT fired (try fire trigger = after_execute)");
                }
            } else {
                fire_at = exec_ack_us + (int64_t)rq->fire_delay_ms * 1000;
                fire_scheduled = true;
            }
        }
        if (fire_scheduled && !fired && t >= fire_at) {
            fired = true;
            if (master) {
                if (xray_relay_fire(rq->exposure_ms, "capture") == ESP_OK) {
                    pulse_end = t + (int64_t)rq->exposure_ms * 1000;
                    set_state(CAP_EXPOSING, "X-ray ON for %u ms", (unsigned)rq->exposure_ms);
                    xSemaphoreTake(s_lock, portMAX_DELAY); s_st.fired = true; xSemaphoreGive(s_lock);
                } else {
                    fire_skipped = true;
                }
            } else {
                fire_skipped = true;
                set_state(CAP_EXPOSING, "window open - X-ray output disabled (external firing)");
            }
        }
        if (cfg->early_close && fired && pulse_end && !early_sent && t >= pulse_end + 30000) {
            uint8_t pdap[16];
            size_t n = fp_build_host_event(pdap, sizeof pdap, FP_EVENT_WINDOW_OPEN);
            fp_session_send(pdap, n, NULL);
            early_sent = true;
            ESP_LOGW(TAG, "early_close: host event 1 sent");
        }
        if ((c.win_expired || c.exec_complete) && s_st.state != CAP_READOUT && s_st.state != CAP_RECEIVING) {
            if (xray_relay_is_on()) xray_relay_off("window ended");
            set_state(CAP_READOUT, "detector reading out");
        }

        /* -- image pull protocol -- */
        if (c.xfer_pending && !first_reply_done) {
            c.xfer_pending = false;
            uint16_t ids[8];
            for (uint16_t i = 0; i < 8; i++) ids[i] = i;
            reply_xfer(ids, 8);                       /* non-zero list keeps the image alive; NEVER n=0 before the data arrived */
            first_reply_done = true;
            uint8_t pdap[16];
            size_t n = fp_build_img_retrieval_request(pdap, sizeof pdap, 0);
            fp_session_send(pdap, n, NULL);
            sent41 = true;
            t41 = t;
            ESP_LOGI(TAG, "answered first image query, sent 0x41");
        }
        if (sent41 && !sent98) {
            if (c.r41) {
                if (c.r41_count <= 0) return fail(false, "detector holds no image (0x41 reported 0 images)");
                uint8_t pdap[16];
                size_t n = fp_build_img_retrieval(pdap, sizeof pdap, FP_HOST_IMAGE_PORT, FP_HOST_CMD_PORT);
                fp_session_send(pdap, n, NULL);
                sent98 = true;
                t98 = t;
                set_state(CAP_RECEIVING, "receiving image (0x98 sent)");
            } else if (t - t41 > 2500000) {
                return fail(false, "no reply to 0x41 (image enumerate)");
            }
        }
        if (sent98 && !pulled) {
            if (c.r98) {
                if (c.r98_status != 0) return fail(false, "detector refused 0x98 (status %u: no such image)", (unsigned)c.r98_status);
                pulled = true;
                t_pull = t;
                last_reply = t;
            } else if (t - t98 > 2500000) {
                return fail(false, "no reply to 0x98 (image retrieval)");
            }
        }
        if (pulled) {
            if (c.xfer_pending) { c.xfer_pending = false; pending_query = true; }
            const uint32_t blocks = fp_image_blocks();
            const int64_t last_blk = fp_image_last_block_us();
            if (blocks == 0 && t - t_pull > 6000000) {
                fp_eth_link_t l = fp_eth_link();
                return fail(false, "no image data arrived (link %d Mbit %s; needs 100 Mbit full duplex and jumbo RX)", l.speed_mbps, l.full_duplex ? "FD" : "HD");
            }
            if (blocks >= FP_IMG_BLOCKS) {
                /* complete: answer the pending/next query with numMissed = 0 so the detector frees its buffer */
                if (!complete_wait) { complete_wait = true; complete_t = t; }
                if (pending_query || t - complete_t > 2000000) {
                    reply_xfer(NULL, 0);
                    ESP_LOGI(TAG, "all 2048 blocks received - released the detector image (cmd 9, n=0)");
                    break;
                }
            } else if (blocks > 0 && last_blk && t - last_blk > 150000 && (pending_query || t - last_reply > 3000000)) {
                uint16_t ids[300];
                int missing = fp_image_missing(ids, 256);
                if (missing > 256) {            /* >256 means 'send the whole image' to the detector */
                    for (int i = 0; i < 300; i++) ids[i] = (uint16_t)i;
                    reply_xfer(ids, 300);
                } else {
                    reply_xfer(ids, (uint32_t)missing);
                }
                retries++;
                pending_query = false;
                last_reply = t;
                set_state(CAP_RECEIVING, "re-requesting %d missing blocks (round %d)", missing, retries);
                if (retries > 10) return fail(false, "image incomplete after 10 retransmit rounds (%d blocks missing)", missing);
            } else if (t - t_pull > 40000000) {
                return fail(false, "image transfer timed out (%u/2048 blocks)", (unsigned)blocks);
            }
        }
    }

    (void)fire_skipped;
    /* ---- verify ---- */
    fp_image_stop();
    if (xray_relay_is_on()) xray_relay_off("capture finished");
    set_state(CAP_VERIFYING, "computing statistics");
    fp_frame_stats_t st;
    fp_image_compute_stats(&st);
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_meta.valid = true;
    s_meta.seq = ++s_seq;
    s_meta.xray = rq->xray;
    s_meta.image_id = fp_image_id();
    s_meta.window_ticks = window_ticks;
    s_meta.window_ms = rq->window_ms;
    s_meta.scrubs = rq->scrubs;
    s_meta.exposure_ms = rq->xray ? rq->exposure_ms : 0;
    s_meta.st = st;
    s_meta.t_us = now_us();
    s_st.blocks = FP_IMG_BLOCKS;
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "frame #%u: min %u max %u mean %.1f std %.1f crc32 %08x", (unsigned)s_seq, (unsigned)st.min, (unsigned)st.max, st.mean, st.std, (unsigned)st.crc32);
    return ESP_OK;
}

/* ------------------------------------------------------------------------------------------------
 * task
 * ---------------------------------------------------------------------------------------------- */
static void run_capture(const fp_capture_req_t *rq)
{
    s_abort = false;
    s_running = true;
    s_t_start_us = now_us();
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_st.xray = rq->xray;
    s_st.error[0] = 0;
    s_st.fired = false;
    s_st.blocks = 0;
    xSemaphoreGive(s_lock);
    s_last_req = *rq;

    fp_session_set_busy(true);
    fp_setup_mode_t mode = fp_config_get()->setup_mode;
    bool image_writing_was = false;
    esp_err_t r = run_once(rq, mode, true);

    if (r != ESP_OK && s_fail.recoverable && mode == FP_SETUP_FAST && !s_abort) {
        /* fast path did not work on this detector: redo once with the full proven sequence and remember it */
        ESP_LOGW(TAG, "fast mode failed (%s) - retrying with the full sequence", s_fail.msg);
        xSemaphoreTake(s_lock, portMAX_DELAY); s_st.fast_fallback = true; xSemaphoreGive(s_lock);
        if (fp_image_writing()) { fp_image_stop(); fp_image_write_end(); }
        s_s7_reconnects = 0xFFFFFFFFu;
        fp_session_set_busy(false);
        fp_session_reconnect();
        int64_t end = now_us() + 20000000;
        while (!fp_session_ready() && now_us() < end) vTaskDelay(pdMS_TO_TICKS(100));
        fp_session_set_busy(true);
        r = fp_session_ready() ? run_once(rq, FP_SETUP_SESSION, false) : fail(false, "detector did not come back after the fast-mode failure");
    }
    (void)image_writing_was;

    xray_relay_off(r == ESP_OK ? "capture end" : "capture failed");
    fp_image_stop();
    if (r == ESP_OK) {
        /* the dark is NOT stored in flash any more (too slow): the browser keeps it, the user captures it per session */
        fp_image_write_end();
        set_state(CAP_DONE, rq->xray ? "capture complete" : "dark captured");
    } else {
        if (fp_image_writing()) fp_image_write_end();
        bool aborted = s_abort;
        if (aborted) {
            uint8_t pdap[16];
            size_t n = fp_build_abort(pdap, sizeof pdap, FP_CMD_ABORT_A);
            fp_session_send(pdap, n, NULL);
        }
        xSemaphoreTake(s_lock, portMAX_DELAY);
        strlcpy(s_st.error, s_fail.msg, sizeof s_st.error);
        xSemaphoreGive(s_lock);
        set_state(aborted ? CAP_ABORTED : CAP_ERROR, "%s", s_fail.msg);
        /* a failed run leaves the detector in an unknown state: rediscover before the next capture */
        fp_session_set_busy(false);
        fp_session_reconnect();
    }
    fp_session_set_busy(false);
    s_running = false;
}

static void capture_task(void *arg)
{
    (void)arg;
    fp_capture_req_t rq;
    for (;;) {
        if (xQueueReceive(s_req_q, &rq, portMAX_DELAY) == pdTRUE) {
            run_capture(&rq);
        }
    }
}

esp_err_t fp_capture_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    s_req_q = xQueueCreate(1, sizeof(fp_capture_req_t));
    s_st.state = CAP_IDLE;
    xTaskCreate(capture_task, "fp_capture", 12288, NULL, 8, NULL);
    return ESP_OK;
}

esp_err_t fp_capture_start(const fp_capture_req_t *rq, char *err, size_t errlen)
{
    if (s_running || uxQueueMessagesWaiting(s_req_q)) {
        snprintf(err, errlen, "a capture is already running");
        return ESP_ERR_INVALID_STATE;
    }
    if (fp_dark_saving()) {
        snprintf(err, errlen, "the dark frame is still being saved to flash");
        return ESP_ERR_INVALID_STATE;
    }
    if (!fp_session_ready()) {
        snprintf(err, errlen, "detector not ready (%s)", fp_session_state_name(fp_session_state()));
        return ESP_ERR_INVALID_STATE;
    }
    fp_eth_link_t l = fp_eth_link();
    if (!l.up) {
        snprintf(err, errlen, "Ethernet link is down");
        return ESP_ERR_INVALID_STATE;
    }
    if (l.speed_mbps == 1000) {
        snprintf(err, errlen, "link negotiated 1000 Mbit - the detector only sends images at 10/100 Mbit (set link mode to adv100fd/forced100fd)");
        return ESP_ERR_INVALID_STATE;
    }
    if (fp_image_readers() > 0) {
        snprintf(err, errlen, "the frame is being downloaded or saved - try again in a moment");
        return ESP_ERR_INVALID_STATE;
    }
    if (xQueueSend(s_req_q, rq, 0) != pdTRUE) {
        snprintf(err, errlen, "busy");
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_st.state = CAP_ARMING;
    s_st.error[0] = 0;
    strlcpy(s_st.step, "queued", sizeof s_st.step);
    s_st.xray = rq->xray;
    xSemaphoreGive(s_lock);
    s_running = true;                     /* reserve immediately so a second request is refused */
    s_t_start_us = now_us();
    return ESP_OK;
}

void fp_capture_abort(void)
{
    xray_relay_off("abort");               /* first, unconditionally */
    s_abort = true;
    ESP_LOGW(TAG, "ABORT requested");
}

void fp_capture_reinit(void)
{
    s_s7_reconnects = 0xFFFFFFFFu;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_st.fast_fallback = false;
    xSemaphoreGive(s_lock);
    fp_session_reconnect();
}

esp_err_t fp_capture_dark_from_frame(char *err, size_t errlen)
{
    fp_frame_meta_t m = fp_capture_frame_meta();
    if (!m.valid) { snprintf(err, errlen, "no frame captured yet"); return ESP_ERR_NOT_FOUND; }
    if (s_running) { snprintf(err, errlen, "a capture is running"); return ESP_ERR_INVALID_STATE; }
    if (fp_dark_saving()) { snprintf(err, errlen, "dark save already running"); return ESP_ERR_INVALID_STATE; }
    if (!fp_image_read_begin()) { snprintf(err, errlen, "frame buffer busy"); return ESP_ERR_INVALID_STATE; }
    fp_dark_meta_t dm = { .valid = true, .window_ticks = m.window_ticks, .window_ms = m.window_ms, .scrubs = m.scrubs, .mean = m.st.mean, .std = m.st.std,
                          .crc32 = m.st.crc32 };
    esp_err_t e = fp_dark_save_async(fp_image_buf(), &dm);
    if (e != ESP_OK) { fp_image_read_end(); snprintf(err, errlen, "could not start saving: %s", esp_err_to_name(e)); }
    return e;
}
