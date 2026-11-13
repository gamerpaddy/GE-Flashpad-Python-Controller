/*
 * fp_eth - Ethernet link to the detector: RGMII + YT8531 on the ESP32-S31-Function-CoreBoard-1, static 192.168.1.1/24,
 * link-mode control (the detector only works at 10/100 Mbit) and a raw RX hook used to pull image datagrams out of the
 * stream before lwIP sees them.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_netif.h"
#include "esp_eth.h"
#include "fp_config.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool up;
    int speed_mbps;       /* 10 / 100 / 1000, 0 when down */
    bool full_duplex;
    uint32_t link_changes;
} fp_eth_link_t;

/* Raw RX hook: called from the EMAC RX task for EVERY received frame (14 byte Ethernet header first).
 * Return true if the frame was consumed (the hook must not keep the pointer); false to hand it to lwIP. */
typedef bool (*fp_eth_rx_hook_t)(const uint8_t *frame, uint32_t len, void *ctx);
typedef void (*fp_eth_link_cb_t)(bool up, void *ctx);

esp_err_t fp_eth_start(fp_link_mode_t mode, const char *host_ip, const char *netmask);
void fp_eth_set_rx_hook(fp_eth_rx_hook_t hook, void *ctx);
void fp_eth_set_link_cb(fp_eth_link_cb_t cb, void *ctx);
fp_eth_link_t fp_eth_link(void);
esp_netif_t *fp_eth_netif(void);
esp_eth_handle_t fp_eth_handle(void);
fp_link_mode_t fp_eth_mode(void);

/* PHY register access for diagnostics (returns ESP_OK and fills *val) */
esp_err_t fp_eth_phy_read(uint32_t reg, uint32_t *val);

/* counters */
uint32_t fp_eth_rx_frames(void);
uint32_t fp_eth_rx_consumed(void);

#ifdef __cplusplus
}
#endif
