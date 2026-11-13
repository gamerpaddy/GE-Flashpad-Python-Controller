/*
 * FlashPad ESP32-S31 controller - app_main.
 *
 *   Ethernet (RGMII, 100 Mbit, 192.168.1.1)  <->  GE FlashPad detector
 *   Wi-Fi SoftAP (+ optional STA)            <->  browser UI (http://192.168.4.1/)
 *   GPIO relay                               ->   X-ray source
 */
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "fp_capture.h"
#include "fp_config.h"
#include "fp_dark.h"
#include "fp_eth.h"
#include "fp_image.h"
#include "fp_proto.h"
#include "fp_session.h"
#include "fp_wifi.h"
#include "web_ui.h"
#include "xray_relay.h"

static const char *TAG = "main";

static void on_link(bool up, void *ctx)
{
    (void)ctx;
    fp_session_link_changed(up);
    if (!up) {
        xray_relay_off("Ethernet link lost");          /* never leave the source enabled across a link loss */
    }
}

static void log_selftest(void)
{
    /* print the protocol blobs so they can be compared to the Python builders (flashpad_acquire.py) over the console */
    uint8_t b[FP_MAX_PACKET];
    size_t n = fp_build_script7(b, sizeof b);
    printf("SELFTEST script7 %u ", (unsigned)n);
    for (size_t i = 0; i < n; i++) printf("%02x", b[i]);
    printf("\n");
}

void app_main(void)
{
    /* 1. relay pin to its inactive level BEFORE anything else (default GPIO from menuconfig; NVS may change it just below) */
#ifdef CONFIG_FP_RELAY_ACTIVE_LOW
    xray_relay_early_init(CONFIG_FP_RELAY_GPIO, false);
#else
    xray_relay_early_init(CONFIG_FP_RELAY_GPIO, true);
#endif
    fp_log_ring_start();

    ESP_LOGI(TAG, "FlashPad ESP32-S31 controller, IDF %s", esp_get_idf_version());
    ESP_ERROR_CHECK(fp_config_init());
    const fp_config_t *cfg = fp_config_get();
    xray_relay_cfg_t rc = { .gpio = cfg->relay_gpio, .active_high = cfg->relay_active_high, .max_pulse_ms = cfg->relay_max_pulse_ms,
                            .idle_disable_min = cfg->xray_idle_disable_min };
    ESP_ERROR_CHECK(xray_relay_init(&rc));

    ESP_ERROR_CHECK(fp_image_init());                 /* 8 MiB PSRAM frame buffer first, before anything fragments the heap */
    ESP_ERROR_CHECK(fp_dark_init());
    ESP_LOGI(TAG, "PSRAM: %u KB free; internal: %u KB free", (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
    log_selftest();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* Session first (it only needs a UDP socket), then the Ethernet link: the link-up event can fire while fp_eth_start() is still running */
    ESP_ERROR_CHECK(fp_session_start(cfg->detector_ip, CONFIG_FP_HOST_IP));
    ESP_ERROR_CHECK(fp_capture_init());
    fp_eth_set_rx_hook(fp_image_eth_hook, NULL);
    fp_eth_set_link_cb(on_link, NULL);
    ESP_ERROR_CHECK(fp_eth_start(cfg->link_mode, CONFIG_FP_HOST_IP, "255.255.255.0"));
    if (fp_eth_link().up) {
        fp_session_link_changed(true);                 /* make sure the manager sees a link that came up during startup */
    }

    /* Wi-Fi + web UI */
    ESP_ERROR_CHECK(fp_wifi_start(cfg));
    ESP_ERROR_CHECK(web_ui_start());
    ESP_LOGI(TAG, "ready: browse to http://192.168.4.1/ (Wi-Fi '%s') or http://%s/ over the detector Ethernet", cfg->ap_ssid, CONFIG_FP_HOST_IP);

    /* heartbeat */
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        fp_eth_link_t l = fp_eth_link();
        fp_capture_status_t cs = fp_capture_status();
        ESP_LOGI(TAG, "alive: eth %s %dM | detector %s | capture %s | psram %uK int %uK", l.up ? "up" : "DOWN", l.speed_mbps,
                 fp_session_state_name(fp_session_state()), fp_cap_state_name(cs.state),
                 (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024), (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
    }
}
