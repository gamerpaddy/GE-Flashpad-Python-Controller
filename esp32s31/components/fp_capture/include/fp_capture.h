/*
 * fp_capture - runs one capture at a time on top of the long-lived detector session:
 *   [Script7 ROE init] -> acquisition script -> EXECUTE -> (window open) -> fire relay -> EXECUTION_COMPLETE ->
 *   0x30000 / cmd 9 / 0x41 / 0x98 -> 2048 blocks (EMAC hook) -> selective re-request -> verify -> store.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "fp_image.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    CAP_IDLE = 0,
    CAP_ARMING,
    CAP_WAIT_WINDOW,
    CAP_EXPOSING,
    CAP_READOUT,
    CAP_RECEIVING,
    CAP_VERIFYING,
    CAP_SAVING_DARK,
    CAP_DONE,
    CAP_ERROR,
    CAP_ABORTED,
} fp_cap_state_t;

typedef struct {
    bool xray;                 /* true: X-ray exposure (relay fires), false: dark (no fire) */
    uint32_t window_ms;        /* integration window */
    uint32_t scrubs;
    uint32_t exposure_ms;      /* relay pulse length (X-ray only) */
    uint32_t fire_delay_ms;    /* delay after the window opened (X-ray only) */
} fp_capture_req_t;

typedef struct {
    fp_cap_state_t state;
    char step[96];
    char error[128];
    bool xray;
    uint32_t blocks;
    uint32_t elapsed_ms;
    bool fast_fallback;
    bool script7_done;
    bool fired;               /* the relay pulsed during the current/last capture */
} fp_capture_status_t;

typedef struct {
    bool valid;
    uint32_t seq;
    bool xray;
    uint32_t image_id;
    uint32_t window_ticks, window_ms, scrubs, exposure_ms;
    fp_frame_stats_t st;
    int64_t t_us;              /* esp_timer time the capture finished */
} fp_frame_meta_t;

esp_err_t fp_capture_init(void);
/* Validates and queues a capture. Returns ESP_ERR_INVALID_STATE (err filled) if busy / detector not ready / frame in use. */
esp_err_t fp_capture_start(const fp_capture_req_t *req, char *err, size_t errlen);
void fp_capture_abort(void);                 /* relay OFF immediately, abort the running capture */
void fp_capture_reinit(void);                /* forget Script7 state and rediscover the detector */
fp_capture_status_t fp_capture_status(void);
fp_frame_meta_t fp_capture_frame_meta(void);
const char *fp_cap_state_name(fp_cap_state_t s);

/* Copy the current frame into the dark store (flash). */
esp_err_t fp_capture_dark_from_frame(char *err, size_t errlen);

#ifdef __cplusplus
}
#endif
