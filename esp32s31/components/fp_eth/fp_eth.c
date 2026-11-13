#include "fp_eth.h"

#include <stdlib.h>
#include <string.h>

#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_eth.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "fp_eth";

/* Board: ESP32-S31-Function-CoreBoard-1, schematic page 3 (YT8531DC-CA, RGMII). */
#define PHY_RST_GPIO 7
#define PHY_ADDR     (-1)      /* auto-detect */

static esp_eth_handle_t s_eth;
static esp_netif_t *s_netif;
static esp_eth_netif_glue_handle_t s_glue;
static fp_link_mode_t s_mode = FP_LINK_ADV100FD;
static fp_eth_rx_hook_t s_hook;
static void *s_hook_ctx;
static fp_eth_link_cb_t s_link_cb;
static void *s_link_ctx;
static volatile fp_eth_link_t s_link;
static volatile uint32_t s_rx_frames, s_rx_consumed;

/* ------------------------------------------------------------------------------------------------
 * PHY register helpers (esp_eth_ioctl)
 * ---------------------------------------------------------------------------------------------- */
static esp_err_t phy_rd(uint32_t reg, uint32_t *val)
{
    esp_eth_phy_reg_rw_data_t d = { .reg_addr = reg, .reg_value_p = val };
    return esp_eth_ioctl(s_eth, ETH_CMD_READ_PHY_REG, &d);
}

static esp_err_t phy_wr(uint32_t reg, uint32_t val)
{
    esp_eth_phy_reg_rw_data_t d = { .reg_addr = reg, .reg_value_p = &val };
    return esp_eth_ioctl(s_eth, ETH_CMD_WRITE_PHY_REG, &d);
}

esp_err_t fp_eth_phy_read(uint32_t reg, uint32_t *val)
{
    return s_eth ? phy_rd(reg, val) : ESP_ERR_INVALID_STATE;
}

/* YT8531 extended registers (indirect: 0x1E = address, 0x1F = data) - same procedure as IDF examples/ethernet/basic */
static esp_err_t yt8531_ext_rmw(uint32_t ext_reg, uint32_t clear_mask, uint32_t set_mask)
{
    uint32_t v = 0;
    ESP_RETURN_ON_ERROR(phy_wr(0x1E, ext_reg), TAG, "ext addr");
    ESP_RETURN_ON_ERROR(phy_rd(0x1F, &v), TAG, "ext read");
    v = (v & ~clear_mask) | set_mask;
    ESP_RETURN_ON_ERROR(phy_wr(0x1E, ext_reg), TAG, "ext addr");
    ESP_RETURN_ON_ERROR(phy_wr(0x1F, v), TAG, "ext write");
    return ESP_OK;
}

static esp_err_t yt8531_init_delays(void)
{
    /* The generic PHY driver's reset leaves auto-negotiation disabled on this PHY - enable it again (as the IDF example does) */
    bool an = true;
    ESP_RETURN_ON_ERROR(esp_eth_ioctl(s_eth, ETH_CMD_S_AUTONEGO, &an), TAG, "autoneg on");
    /* RGMII timing: RX clock delay ~2 ns (EXT 0xA001 bit8), TX delay 13 x 150 ps for both 1000 (bits 3:0) and 10/100 (bits 7:4) (EXT 0xA003) */
    ESP_RETURN_ON_ERROR(yt8531_ext_rmw(0xA001, 0, 1u << 8), TAG, "rxc delay");
    ESP_RETURN_ON_ERROR(yt8531_ext_rmw(0xA003, 0x00FF, (13u << 4) | 13u), TAG, "tx delay");
    return ESP_OK;
}

/* Restrict what the PHY offers: 100BASE-TX full duplex only, no 1000BASE-T, no 10BASE-T, no half duplex. */
static esp_err_t phy_advertise_100fd_only(void)
{
    uint32_t anar = 0, gbcr = 0, bmcr = 0;
    ESP_RETURN_ON_ERROR(phy_rd(4, &anar), TAG, "ANAR");
    anar = (anar & ~0x03E0u) | 0x0100u;     /* clear 10HD/10FD/100HD/100FD/100T4, set 100FD */
    ESP_RETURN_ON_ERROR(phy_wr(4, anar), TAG, "ANAR wr");
    ESP_RETURN_ON_ERROR(phy_rd(9, &gbcr), TAG, "GBCR");
    gbcr &= ~0x0300u;                         /* no 1000BASE-T FD/HD */
    ESP_RETURN_ON_ERROR(phy_wr(9, gbcr), TAG, "GBCR wr");
    ESP_RETURN_ON_ERROR(phy_rd(0, &bmcr), TAG, "BMCR");
    bmcr |= (1u << 12) | (1u << 9);           /* AN enable + restart */
    ESP_RETURN_ON_ERROR(phy_wr(0, bmcr), TAG, "BMCR wr");
    return ESP_OK;
}

/* ------------------------------------------------------------------------------------------------
 * events
 * ---------------------------------------------------------------------------------------------- */
static void eth_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base;
    esp_eth_handle_t h = *(esp_eth_handle_t *)data;
    if (id == ETHERNET_EVENT_CONNECTED) {
        eth_speed_t sp = ETH_SPEED_10M;
        eth_duplex_t du = ETH_DUPLEX_HALF;
        esp_eth_ioctl(h, ETH_CMD_G_SPEED, &sp);
        esp_eth_ioctl(h, ETH_CMD_G_DUPLEX_MODE, &du);
        s_link.speed_mbps = sp == ETH_SPEED_1000M ? 1000 : sp == ETH_SPEED_100M ? 100 : 10;
        s_link.full_duplex = du == ETH_DUPLEX_FULL;
        s_link.up = true;
        s_link.link_changes++;
        ESP_LOGI(TAG, "link UP %d Mbit %s duplex%s", s_link.speed_mbps, s_link.full_duplex ? "full" : "HALF",
                 s_link.speed_mbps == 1000 ? "  (!! detector needs 10/100)" : "");
        if (s_link_cb) s_link_cb(true, s_link_ctx);
    } else if (id == ETHERNET_EVENT_DISCONNECTED) {
        s_link.up = false;
        s_link.speed_mbps = 0;
        s_link.link_changes++;
        ESP_LOGW(TAG, "link DOWN");
        if (s_link_cb) s_link_cb(false, s_link_ctx);
    }
}

/* frames from the MAC: image hook first, everything else to lwIP (same pattern as esp_eth_netif_glue.c) */
static esp_err_t eth_input(esp_eth_handle_t h, uint8_t *buffer, uint32_t length, void *priv, void *info)
{
    (void)h; (void)info; (void)priv;
    s_rx_frames++;
    if (s_hook && s_hook(buffer, length, s_hook_ctx)) {
        s_rx_consumed++;
        free(buffer);
        return ESP_OK;
    }
    return esp_netif_receive(s_netif, buffer, length, NULL);
}

void fp_eth_set_rx_hook(fp_eth_rx_hook_t hook, void *ctx) { s_hook_ctx = ctx; s_hook = hook; }
void fp_eth_set_link_cb(fp_eth_link_cb_t cb, void *ctx) { s_link_ctx = ctx; s_link_cb = cb; }

fp_eth_link_t fp_eth_link(void)
{
    fp_eth_link_t l;
    l.up = s_link.up; l.speed_mbps = s_link.speed_mbps; l.full_duplex = s_link.full_duplex; l.link_changes = s_link.link_changes;
    return l;
}

esp_netif_t *fp_eth_netif(void) { return s_netif; }
esp_eth_handle_t fp_eth_handle(void) { return s_eth; }
fp_link_mode_t fp_eth_mode(void) { return s_mode; }
uint32_t fp_eth_rx_frames(void) { return s_rx_frames; }
uint32_t fp_eth_rx_consumed(void) { return s_rx_consumed; }

/* ------------------------------------------------------------------------------------------------
 * start
 * ---------------------------------------------------------------------------------------------- */
esp_err_t fp_eth_start(fp_link_mode_t mode, const char *host_ip, const char *netmask)
{
    s_mode = mode;

    eth_mac_config_t mac_config = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_config = ETH_PHY_DEFAULT_CONFIG();
    phy_config.phy_addr = PHY_ADDR;
    phy_config.reset_gpio_num = PHY_RST_GPIO;
    phy_config.hw_reset_assert_time_us = 15000;
    phy_config.post_hw_reset_delay_ms = 150;

    /* ETH_ESP32_EMAC_DEFAULT_CONFIG() on the esp32s31 already equals this board: MDC 5 / MDIO 6, RGMII data GPIO8-12 / 15-19, RXC 14, TXC 13 */
    eth_esp32_emac_config_t emac_config = ETH_ESP32_EMAC_DEFAULT_CONFIG();

    esp_eth_mac_t *mac = esp_eth_mac_new_esp32(&emac_config, &mac_config);
    ESP_RETURN_ON_FALSE(mac, ESP_FAIL, TAG, "EMAC create failed");
    esp_eth_phy_t *phy = esp_eth_phy_new_generic(&phy_config);
    ESP_RETURN_ON_FALSE(phy, ESP_FAIL, TAG, "PHY create failed");

    esp_eth_config_t cfg = ETH_DEFAULT_CONFIG(mac, phy);
    ESP_RETURN_ON_ERROR(esp_eth_driver_install(&cfg, &s_eth), TAG, "driver install failed");

    ESP_RETURN_ON_ERROR(yt8531_init_delays(), TAG, "YT8531 init failed");

    switch (mode) {
    case FP_LINK_FORCED100FD: {
        /* GE's configureNetworkForDetector.sh: speed 100 duplex full autoneg off */
        bool an = false;
        eth_speed_t sp = ETH_SPEED_100M;
        eth_duplex_t du = ETH_DUPLEX_FULL;
        ESP_RETURN_ON_ERROR(esp_eth_ioctl(s_eth, ETH_CMD_S_AUTONEGO, &an), TAG, "autoneg off");
        ESP_RETURN_ON_ERROR(esp_eth_ioctl(s_eth, ETH_CMD_S_SPEED, &sp), TAG, "force 100M");
        ESP_RETURN_ON_ERROR(esp_eth_ioctl(s_eth, ETH_CMD_S_DUPLEX_MODE, &du), TAG, "force full duplex");
        ESP_LOGI(TAG, "link mode: forced 100 Mbit full duplex, autoneg off");
        break;
    }
    case FP_LINK_ADV100FD:
        ESP_RETURN_ON_ERROR(phy_advertise_100fd_only(), TAG, "advertise 100FD");
        ESP_LOGI(TAG, "link mode: autoneg, advertising 100BASE-TX full duplex only");
        break;
    case FP_LINK_AUTO:
    default:
        ESP_LOGW(TAG, "link mode: full autoneg (may negotiate 1000 Mbit - the detector will NOT send images)");
        break;
    }

    ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, &eth_event_handler, NULL));

    esp_netif_config_t ncfg = ESP_NETIF_DEFAULT_ETH();
    s_netif = esp_netif_new(&ncfg);
    ESP_RETURN_ON_FALSE(s_netif, ESP_FAIL, TAG, "netif create failed");
    s_glue = esp_eth_new_netif_glue(s_eth);
    ESP_RETURN_ON_FALSE(s_glue, ESP_FAIL, TAG, "glue create failed");
    ESP_RETURN_ON_ERROR(esp_netif_attach(s_netif, s_glue), TAG, "netif attach failed");

    /* static address: the detector sends every reply to 192.168.1.1 */
    ESP_ERROR_CHECK(esp_netif_dhcpc_stop(s_netif));
    esp_netif_ip_info_t ip = { 0 };
    ESP_RETURN_ON_FALSE(esp_netif_str_to_ip4(host_ip, &ip.ip) == ESP_OK && esp_netif_str_to_ip4(netmask, &ip.netmask) == ESP_OK,
                        ESP_ERR_INVALID_ARG, TAG, "bad host ip");
    ESP_ERROR_CHECK(esp_netif_set_ip_info(s_netif, &ip));

    /* our own input path: image datagrams are taken here, the rest goes to lwIP exactly like the stock glue does */
    ESP_RETURN_ON_ERROR(esp_eth_update_input_path_info(s_eth, eth_input, NULL), TAG, "input path");

    ESP_RETURN_ON_ERROR(esp_eth_start(s_eth), TAG, "eth start failed");
    ESP_LOGI(TAG, "Ethernet started, host %s/%s", host_ip, netmask);
    return ESP_OK;
}
