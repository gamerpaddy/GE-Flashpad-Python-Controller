/*
 * fp_config - persistent settings (NVS) + JSON view used by /api/config.
 * Defaults come from menuconfig (main/Kconfig.projbuild); anything stored in NVS overrides them.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { FP_LINK_ADV100FD = 0, FP_LINK_FORCED100FD = 1, FP_LINK_AUTO = 2 } fp_link_mode_t;
typedef enum { FP_SETUP_FULL = 0, FP_SETUP_SESSION = 1, FP_SETUP_FAST = 2, FP_SETUP_COMBINED = 3 } fp_setup_mode_t;
typedef enum { FP_FIRE_WINDOW_OPEN = 0, FP_FIRE_AFTER_EXECUTE = 1 } fp_fire_trigger_t;

typedef struct {
    char detector_ip[16];
    fp_link_mode_t link_mode;
    fp_setup_mode_t setup_mode;
    fp_fire_trigger_t fire_trigger;
    bool early_close;

    int relay_gpio;
    bool relay_active_high;
    uint32_t relay_max_pulse_ms;
    uint32_t xray_idle_disable_min;

    char ap_ssid[33];
    char ap_pass[65];
    char sta_ssid[33];
    char sta_pass[65];

    uint32_t def_window_ms;
    uint32_t def_scrubs;
    uint32_t def_exposure_ms;
    uint32_t def_fire_delay_ms;
} fp_config_t;

esp_err_t fp_config_init(void);                 /* nvs_flash_init + load (defaults for anything missing) */
const fp_config_t *fp_config_get(void);         /* never NULL after init */
fp_config_t fp_config_copy(void);

/* Apply a JSON object (any subset of the fields in docs/API.md). Validates, saves to NVS.
 * Returns ESP_OK or ESP_ERR_INVALID_ARG (err_msg filled).  *reboot_required set when a change needs a restart (link mode, GPIO, Wi-Fi). */
esp_err_t fp_config_apply_json(const cJSON *obj, bool *reboot_required, char *err_msg, size_t err_len);

/* Build the JSON object for GET /api/config (passwords blank). Caller owns the result (cJSON_Delete). */
cJSON *fp_config_to_json(void);

const char *fp_link_mode_name(fp_link_mode_t m);
const char *fp_setup_mode_name(fp_setup_mode_t m);
const char *fp_fire_trigger_name(fp_fire_trigger_t m);

#ifdef __cplusplus
}
#endif
