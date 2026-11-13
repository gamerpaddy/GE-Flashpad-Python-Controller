#include "fp_wifi.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/timers.h"
#include "lwip/sockets.h"

static const char *TAG = "fp_wifi";

static esp_netif_t *s_ap_netif, *s_sta_netif;
static fp_wifi_status_t s_st;
static TimerHandle_t s_retry;

static void retry_cb(TimerHandle_t t)
{
    (void)t;
    esp_wifi_connect();
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT) {
        switch (id) {
        case WIFI_EVENT_AP_START:
            ESP_LOGI(TAG, "SoftAP up: SSID '%s'", s_st.ap_ssid);
            break;
        case WIFI_EVENT_AP_STACONNECTED: {
            wifi_event_ap_staconnected_t *e = data;
            s_st.ap_clients++;
            ESP_LOGI(TAG, "client joined the AP (aid %d)", e->aid);
            break;
        }
        case WIFI_EVENT_AP_STADISCONNECTED:
            if (s_st.ap_clients > 0) s_st.ap_clients--;
            break;
        case WIFI_EVENT_STA_START:
            if (s_st.sta_configured) esp_wifi_connect();
            break;
        case WIFI_EVENT_STA_DISCONNECTED:
            s_st.sta_connected = false;
            s_st.sta_ip[0] = 0;
            if (s_st.sta_configured && s_retry) xTimerStart(s_retry, 0);
            break;
        default:
            break;
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        s_st.sta_connected = true;
        snprintf(s_st.sta_ip, sizeof s_st.sta_ip, IPSTR, IP2STR(&e->ip_info.ip));
        ESP_LOGI(TAG, "STA connected to '%s', IP %s", s_st.sta_ssid, s_st.sta_ip);
    }
}

fp_wifi_status_t fp_wifi_status(void)
{
    fp_wifi_status_t s = s_st;
    if (s.sta_connected) {
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) s.sta_rssi = ap.rssi;
    }
    return s;
}

/* ---- wildcard DNS for the SoftAP: every name (flash.pad, flashpad, connectivitycheck.gstatic.com, ...) resolves to the
 * AP address so a phone lands on the web UI without knowing the IP, and Android/iOS/Windows pop the captive-portal page. */
static void dns_task(void *arg)
{
    uint32_t ip = (uint32_t)(uintptr_t)arg;           /* network byte order */
    int s = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(53), .sin_addr.s_addr = htonl(INADDR_ANY) };
    if (s < 0 || bind(s, (struct sockaddr *)&a, sizeof a) != 0) {
        ESP_LOGE(TAG, "DNS socket/bind failed");
        if (s >= 0) close(s);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "captive DNS: all names -> AP address");
    uint8_t buf[512];
    for (;;) {
        struct sockaddr_in from;
        socklen_t fl = sizeof from;
        int n = recvfrom(s, buf, sizeof buf - 16, 0, (struct sockaddr *)&from, &fl);
        if (n < 17 || (buf[2] & 0x80) || buf[4] != 0 || buf[5] != 1) continue;     /* not a single-question query */
        int i = 12;
        while (i < n && buf[i]) i += buf[i] + 1;                                   /* skip QNAME */
        if (i + 5 > n) continue;
        int qend = i + 5;                                                          /* 0 + QTYPE + QCLASS */
        uint16_t qtype = (uint16_t)((buf[i + 1] << 8) | buf[i + 2]);
        buf[2] = 0x81; buf[3] = 0x80;                                              /* response, RD+RA, NOERROR */
        buf[6] = 0; buf[7] = (qtype == 1) ? 1 : 0;                                 /* A -> 1 answer, AAAA etc -> empty */
        buf[8] = buf[9] = buf[10] = buf[11] = 0;
        int len = qend;
        if (qtype == 1) {
            static const uint8_t hdr[] = { 0xC0, 0x0C, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4 };
            memcpy(buf + len, hdr, sizeof hdr);
            len += sizeof hdr;
            memcpy(buf + len, &ip, 4);
            len += 4;
        }
        sendto(s, buf, len, 0, (struct sockaddr *)&from, fl);
    }
}

static void captive_start(void)
{
    esp_netif_ip_info_t ip;
    if (esp_netif_get_ip_info(s_ap_netif, &ip) != ESP_OK) return;
    /* DHCP: hand out the AP as DNS server and advertise the portal URL (RFC 8910, option 114) */
    esp_netif_dns_info_t dns = { 0 };
    dns.ip.type = ESP_IPADDR_TYPE_V4;
    dns.ip.u_addr.ip4 = ip.ip;
    esp_netif_dhcps_stop(s_ap_netif);
    esp_netif_set_dns_info(s_ap_netif, ESP_NETIF_DNS_MAIN, &dns);
    uint8_t offer = 1;
    esp_netif_dhcps_option(s_ap_netif, ESP_NETIF_OP_SET, ESP_NETIF_DOMAIN_NAME_SERVER, &offer, sizeof offer);
    char uri[40];
    snprintf(uri, sizeof uri, "http://" IPSTR "/", IP2STR(&ip.ip));
    esp_netif_dhcps_option(s_ap_netif, ESP_NETIF_OP_SET, ESP_NETIF_CAPTIVEPORTAL_URI, uri, strlen(uri));
    esp_netif_dhcps_start(s_ap_netif);
    xTaskCreate(dns_task, "fp_dns", 3072, (void *)(uintptr_t)ip.ip.addr, 3, NULL);
}

esp_err_t fp_wifi_start(const fp_config_t *cfg)
{
    memset(&s_st, 0, sizeof s_st);
    strlcpy(s_st.ap_ssid, cfg->ap_ssid, sizeof s_st.ap_ssid);
    s_st.sta_configured = cfg->sta_ssid[0] != 0;
    strlcpy(s_st.sta_ssid, cfg->sta_ssid, sizeof s_st.sta_ssid);

    s_ap_netif = esp_netif_create_default_wifi_ap();
    s_sta_netif = esp_netif_create_default_wifi_sta();
    esp_netif_ip_info_t ip;
    if (esp_netif_get_ip_info(s_ap_netif, &ip) == ESP_OK) snprintf(s_st.ap_ip, sizeof s_st.ap_ip, IPSTR, IP2STR(&ip.ip));

    wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wcfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    s_retry = xTimerCreate("wifi_retry", pdMS_TO_TICKS(5000), pdFALSE, NULL, retry_cb);

    wifi_config_t ap = { 0 };
    strlcpy((char *)ap.ap.ssid, cfg->ap_ssid, sizeof ap.ap.ssid);
    ap.ap.ssid_len = (uint8_t)strlen(cfg->ap_ssid);
    strlcpy((char *)ap.ap.password, cfg->ap_pass, sizeof ap.ap.password);
    ap.ap.channel = 6;
    ap.ap.max_connection = 4;
    ap.ap.authmode = strlen(cfg->ap_pass) >= 8 ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(s_st.sta_configured ? WIFI_MODE_APSTA : WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    if (s_st.sta_configured) {
        wifi_config_t sta = { 0 };
        strlcpy((char *)sta.sta.ssid, cfg->sta_ssid, sizeof sta.sta.ssid);
        strlcpy((char *)sta.sta.password, cfg->sta_pass, sizeof sta.sta.password);
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    }
    ESP_ERROR_CHECK(esp_wifi_start());
    captive_start();
    esp_wifi_set_ps(WIFI_PS_NONE);          /* throughput matters more than power for an 8 MiB frame download */
    s_st.started = true;
    ESP_LOGI(TAG, "Wi-Fi started: AP '%s' at %s%s", cfg->ap_ssid, s_st.ap_ip, s_st.sta_configured ? ", STA enabled" : "");
    return ESP_OK;
}
