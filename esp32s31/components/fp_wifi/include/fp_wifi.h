/* fp_wifi - SoftAP (always on, so the UI is reachable anywhere) + optional STA joining an existing network. */
#pragma once

#include <stdbool.h>
#include "esp_err.h"
#include "fp_config.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool started;
    char ap_ssid[33];
    char ap_ip[16];
    int ap_clients;
    bool sta_configured;
    bool sta_connected;
    char sta_ssid[33];
    char sta_ip[16];
    int sta_rssi;
} fp_wifi_status_t;

esp_err_t fp_wifi_start(const fp_config_t *cfg);
fp_wifi_status_t fp_wifi_status(void);

#ifdef __cplusplus
}
#endif
