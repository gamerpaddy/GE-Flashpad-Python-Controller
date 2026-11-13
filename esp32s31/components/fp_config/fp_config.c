#include "fp_config.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "sdkconfig.h"

static const char *TAG = "fp_config";
#define NS "fpcfg"

#ifndef CONFIG_FP_RELAY_GPIO
#define CONFIG_FP_RELAY_GPIO 2
#endif
#ifndef CONFIG_FP_RELAY_MAX_PULSE_MS
#define CONFIG_FP_RELAY_MAX_PULSE_MS 3000
#endif
#ifndef CONFIG_FP_AP_SSID
#define CONFIG_FP_AP_SSID "FlashPad-ESP"
#endif
#ifndef CONFIG_FP_AP_PASS
#define CONFIG_FP_AP_PASS "flashpad1"
#endif
#ifndef CONFIG_FP_DETECTOR_IP
#define CONFIG_FP_DETECTOR_IP "192.168.1.30"
#endif

static fp_config_t s_cfg;
static SemaphoreHandle_t s_lock;

static const char *const LINK_NAMES[] = { "adv100fd", "forced100fd", "auto" };
static const char *const SETUP_NAMES[] = { "full", "session", "fast", "combined" };
static const char *const FIRE_NAMES[] = { "window_open", "after_execute" };

const char *fp_link_mode_name(fp_link_mode_t m) { return (unsigned)m < 3 ? LINK_NAMES[m] : "?"; }
const char *fp_setup_mode_name(fp_setup_mode_t m) { return (unsigned)m < 4 ? SETUP_NAMES[m] : "?"; }
const char *fp_fire_trigger_name(fp_fire_trigger_t m) { return (unsigned)m < 2 ? FIRE_NAMES[m] : "?"; }

static int name_index(const char *const *names, int n, const char *s)
{
    for (int i = 0; i < n; i++) {
        if (strcmp(names[i], s) == 0) return i;
    }
    return -1;
}

static void set_defaults(fp_config_t *c)
{
    memset(c, 0, sizeof *c);
    strlcpy(c->detector_ip, CONFIG_FP_DETECTOR_IP, sizeof c->detector_ip);
    c->link_mode = FP_LINK_ADV100FD;       /* PC bring-up default; switch to forced100fd for the real detector if needed */
    c->setup_mode = FP_SETUP_SESSION;
    c->fire_trigger = FP_FIRE_WINDOW_OPEN;
    c->early_close = false;
    c->relay_gpio = CONFIG_FP_RELAY_GPIO;
#ifdef CONFIG_FP_RELAY_ACTIVE_LOW
    c->relay_active_high = false;
#else
    c->relay_active_high = true;
#endif
    c->relay_max_pulse_ms = CONFIG_FP_RELAY_MAX_PULSE_MS;
    c->xray_idle_disable_min = 10;
    strlcpy(c->ap_ssid, CONFIG_FP_AP_SSID, sizeof c->ap_ssid);
    strlcpy(c->ap_pass, CONFIG_FP_AP_PASS, sizeof c->ap_pass);
    c->def_window_ms = 2000;
    c->def_scrubs = 0;
    c->def_exposure_ms = 250;
    c->def_fire_delay_ms = 20;
}

/* ------------------------------------------------------------------------------------------------ */
static void nvs_get_str_safe(nvs_handle_t h, const char *key, char *dst, size_t cap)
{
    size_t n = cap;
    char tmp[96];
    if (cap > sizeof tmp) n = sizeof tmp;
    size_t len = n;
    if (nvs_get_str(h, key, tmp, &len) == ESP_OK) {
        strlcpy(dst, tmp, cap);
    }
}

static void load_from_nvs(fp_config_t *c)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    uint8_t u8; int32_t i32; uint32_t u32;
    nvs_get_str_safe(h, "det_ip", c->detector_ip, sizeof c->detector_ip);
    if (nvs_get_u8(h, "link", &u8) == ESP_OK && u8 < 3) c->link_mode = (fp_link_mode_t)u8;
    if (nvs_get_u8(h, "setup", &u8) == ESP_OK && u8 < 4) c->setup_mode = (fp_setup_mode_t)u8;
    if (nvs_get_u8(h, "fire", &u8) == ESP_OK && u8 < 2) c->fire_trigger = (fp_fire_trigger_t)u8;
    if (nvs_get_u8(h, "early", &u8) == ESP_OK) c->early_close = u8 != 0;
    if (nvs_get_i32(h, "rly_gpio", &i32) == ESP_OK) c->relay_gpio = i32;
    if (nvs_get_u8(h, "rly_hi", &u8) == ESP_OK) c->relay_active_high = u8 != 0;
    if (nvs_get_u32(h, "rly_max", &u32) == ESP_OK) c->relay_max_pulse_ms = u32;
    if (nvs_get_u32(h, "xr_idle", &u32) == ESP_OK) c->xray_idle_disable_min = u32;
    nvs_get_str_safe(h, "ap_ssid", c->ap_ssid, sizeof c->ap_ssid);
    nvs_get_str_safe(h, "ap_pass", c->ap_pass, sizeof c->ap_pass);
    nvs_get_str_safe(h, "sta_ssid", c->sta_ssid, sizeof c->sta_ssid);
    nvs_get_str_safe(h, "sta_pass", c->sta_pass, sizeof c->sta_pass);
    if (nvs_get_u32(h, "d_win", &u32) == ESP_OK) c->def_window_ms = u32;
    if (nvs_get_u32(h, "d_scr", &u32) == ESP_OK) c->def_scrubs = u32;
    if (nvs_get_u32(h, "d_exp", &u32) == ESP_OK) c->def_exposure_ms = u32;
    if (nvs_get_u32(h, "d_fdl", &u32) == ESP_OK) c->def_fire_delay_ms = u32;
    nvs_close(h);
}

static esp_err_t save_to_nvs(const fp_config_t *c)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NS, NVS_READWRITE, &h);
    if (err != ESP_OK) return err;
    nvs_set_str(h, "det_ip", c->detector_ip);
    nvs_set_u8(h, "link", c->link_mode);
    nvs_set_u8(h, "setup", c->setup_mode);
    nvs_set_u8(h, "fire", c->fire_trigger);
    nvs_set_u8(h, "early", c->early_close);
    nvs_set_i32(h, "rly_gpio", c->relay_gpio);
    nvs_set_u8(h, "rly_hi", c->relay_active_high);
    nvs_set_u32(h, "rly_max", c->relay_max_pulse_ms);
    nvs_set_u32(h, "xr_idle", c->xray_idle_disable_min);
    nvs_set_str(h, "ap_ssid", c->ap_ssid);
    nvs_set_str(h, "ap_pass", c->ap_pass);
    nvs_set_str(h, "sta_ssid", c->sta_ssid);
    nvs_set_str(h, "sta_pass", c->sta_pass);
    nvs_set_u32(h, "d_win", c->def_window_ms);
    nvs_set_u32(h, "d_scr", c->def_scrubs);
    nvs_set_u32(h, "d_exp", c->def_exposure_ms);
    nvs_set_u32(h, "d_fdl", c->def_fire_delay_ms);
    err = nvs_commit(h);
    nvs_close(h);
    return err;
}

esp_err_t fp_config_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    if (err != ESP_OK) return err;
    if (!s_lock) s_lock = xSemaphoreCreateMutex();
    set_defaults(&s_cfg);
    load_from_nvs(&s_cfg);
    ESP_LOGI(TAG, "config: det=%s link=%s setup=%s fire=%s relay=GPIO%d/%s max=%ums", s_cfg.detector_ip, fp_link_mode_name(s_cfg.link_mode),
             fp_setup_mode_name(s_cfg.setup_mode), fp_fire_trigger_name(s_cfg.fire_trigger), s_cfg.relay_gpio,
             s_cfg.relay_active_high ? "hi" : "lo", (unsigned)s_cfg.relay_max_pulse_ms);
    return ESP_OK;
}

const fp_config_t *fp_config_get(void) { return &s_cfg; }

fp_config_t fp_config_copy(void)
{
    fp_config_t c;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    c = s_cfg;
    xSemaphoreGive(s_lock);
    return c;
}

/* ------------------------------------------------------------------------------------------------ */
static bool valid_ip(const char *s)
{
    unsigned a, b, c, d; char tail;
    if (sscanf(s, "%u.%u.%u.%u%c", &a, &b, &c, &d, &tail) != 4) return false;
    return a < 256 && b < 256 && c < 256 && d < 256;
}

/* GPIOs the user must not pick: Ethernet RGMII/MDIO/PHY reset, flash, USB, strapping, audio, LED/boot, UART0 */
static bool relay_gpio_allowed(int g)
{
    if (g < 0 || g > 61) return false;
    if (g >= 5 && g <= 19) return false;         /* Ethernet */
    if (g >= 26 && g <= 34) return false;        /* flash (26-32), USB D-/D+ (33/34) */
    if (g == 36 || g == 37) return false;        /* strapping */
    if (g >= 50 && g <= 57) return false;        /* audio codec / I2S */
    if (g == 58 || g == 59) return false;        /* UART0 */
    if (g == 60 || g == 61) return false;        /* RGB LED / BOOT strapping */
    if (g == 41 || g == 29) return false;        /* not present */
    return true;
}

#define SET_STR(field, key, maxlen)                                                           \
    do {                                                                                      \
        const cJSON *it = cJSON_GetObjectItemCaseSensitive(obj, key);                         \
        if (cJSON_IsString(it) && it->valuestring) {                                          \
            if (strlen(it->valuestring) > (maxlen)) { snprintf(err_msg, err_len, "%s too long", key); err = ESP_ERR_INVALID_ARG; goto out; } \
            strlcpy(n.field, it->valuestring, sizeof n.field);                                \
        }                                                                                     \
    } while (0)

esp_err_t fp_config_apply_json(const cJSON *obj, bool *reboot_required, char *err_msg, size_t err_len)
{
    esp_err_t err = ESP_OK;
    bool reboot = false;
    if (!cJSON_IsObject(obj)) {
        snprintf(err_msg, err_len, "JSON object expected");
        return ESP_ERR_INVALID_ARG;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    fp_config_t n = s_cfg;
    const cJSON *it;

    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "detector_ip")) && cJSON_IsString(it)) {
        if (!valid_ip(it->valuestring)) { snprintf(err_msg, err_len, "detector_ip invalid"); err = ESP_ERR_INVALID_ARG; goto out; }
        strlcpy(n.detector_ip, it->valuestring, sizeof n.detector_ip);
    }
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "link_mode")) && cJSON_IsString(it)) {
        int i = name_index(LINK_NAMES, 3, it->valuestring);
        if (i < 0) { snprintf(err_msg, err_len, "link_mode invalid"); err = ESP_ERR_INVALID_ARG; goto out; }
        n.link_mode = (fp_link_mode_t)i;
    }
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "setup_mode")) && cJSON_IsString(it)) {
        int i = name_index(SETUP_NAMES, 4, it->valuestring);
        if (i < 0) { snprintf(err_msg, err_len, "setup_mode invalid"); err = ESP_ERR_INVALID_ARG; goto out; }
        n.setup_mode = (fp_setup_mode_t)i;
    }
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "fire_trigger")) && cJSON_IsString(it)) {
        int i = name_index(FIRE_NAMES, 2, it->valuestring);
        if (i < 0) { snprintf(err_msg, err_len, "fire_trigger invalid"); err = ESP_ERR_INVALID_ARG; goto out; }
        n.fire_trigger = (fp_fire_trigger_t)i;
    }
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "early_close")) && cJSON_IsBool(it)) n.early_close = cJSON_IsTrue(it);
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "relay_gpio")) && cJSON_IsNumber(it)) {
        if (!relay_gpio_allowed(it->valueint)) { snprintf(err_msg, err_len, "relay_gpio %d is reserved on this board", it->valueint); err = ESP_ERR_INVALID_ARG; goto out; }
        n.relay_gpio = it->valueint;
    }
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "relay_active_high")) && cJSON_IsBool(it)) n.relay_active_high = cJSON_IsTrue(it);
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "relay_max_pulse_ms")) && cJSON_IsNumber(it)) {
        if (it->valueint < 1 || it->valueint > 60000) { snprintf(err_msg, err_len, "relay_max_pulse_ms must be 1..60000"); err = ESP_ERR_INVALID_ARG; goto out; }
        n.relay_max_pulse_ms = (uint32_t)it->valueint;
    }
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "xray_idle_disable_min")) && cJSON_IsNumber(it)) {
        if (it->valueint < 0 || it->valueint > 1440) { snprintf(err_msg, err_len, "xray_idle_disable_min must be 0..1440"); err = ESP_ERR_INVALID_ARG; goto out; }
        n.xray_idle_disable_min = (uint32_t)it->valueint;
    }
    SET_STR(ap_ssid, "ap_ssid", 32);
    SET_STR(sta_ssid, "sta_ssid", 32);
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "ap_pass")) && cJSON_IsString(it) && it->valuestring[0]) {
        size_t l = strlen(it->valuestring);
        if (l < 8 || l > 63) { snprintf(err_msg, err_len, "ap_pass must be 8..63 characters"); err = ESP_ERR_INVALID_ARG; goto out; }
        strlcpy(n.ap_pass, it->valuestring, sizeof n.ap_pass);
    }
    if ((it = cJSON_GetObjectItemCaseSensitive(obj, "sta_pass")) && cJSON_IsString(it) && it->valuestring[0]) {
        if (strlen(it->valuestring) > 63) { snprintf(err_msg, err_len, "sta_pass too long"); err = ESP_ERR_INVALID_ARG; goto out; }
        strlcpy(n.sta_pass, it->valuestring, sizeof n.sta_pass);
    }
    const cJSON *d = cJSON_GetObjectItemCaseSensitive(obj, "defaults");
    if (cJSON_IsObject(d)) {
        if ((it = cJSON_GetObjectItemCaseSensitive(d, "window_ms")) && cJSON_IsNumber(it) && it->valueint >= 20 && it->valueint <= 160000) n.def_window_ms = it->valueint;
        if ((it = cJSON_GetObjectItemCaseSensitive(d, "scrubs")) && cJSON_IsNumber(it) && it->valueint >= 0 && it->valueint <= 255) n.def_scrubs = it->valueint;
        if ((it = cJSON_GetObjectItemCaseSensitive(d, "exposure_ms")) && cJSON_IsNumber(it) && it->valueint >= 1 && it->valueint <= 60000) n.def_exposure_ms = it->valueint;
        if ((it = cJSON_GetObjectItemCaseSensitive(d, "fire_delay_ms")) && cJSON_IsNumber(it) && it->valueint >= 0 && it->valueint <= 5000) n.def_fire_delay_ms = it->valueint;
    }

    if (strcmp(n.detector_ip, s_cfg.detector_ip) || n.link_mode != s_cfg.link_mode || n.relay_gpio != s_cfg.relay_gpio ||
        n.relay_active_high != s_cfg.relay_active_high || n.relay_max_pulse_ms != s_cfg.relay_max_pulse_ms ||
        n.xray_idle_disable_min != s_cfg.xray_idle_disable_min ||
        strcmp(n.ap_ssid, s_cfg.ap_ssid) || strcmp(n.ap_pass, s_cfg.ap_pass) ||
        strcmp(n.sta_ssid, s_cfg.sta_ssid) || strcmp(n.sta_pass, s_cfg.sta_pass)) {
        reboot = true;
    }
    err = save_to_nvs(&n);
    if (err == ESP_OK) {
        s_cfg = n;
    } else {
        snprintf(err_msg, err_len, "NVS write failed: %s", esp_err_to_name(err));
    }
out:
    xSemaphoreGive(s_lock);
    if (reboot_required) *reboot_required = reboot;
    return err;
}

cJSON *fp_config_to_json(void)
{
    fp_config_t c = fp_config_copy();
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "detector_ip", c.detector_ip);
    cJSON_AddStringToObject(o, "link_mode", fp_link_mode_name(c.link_mode));
    cJSON_AddStringToObject(o, "setup_mode", fp_setup_mode_name(c.setup_mode));
    cJSON_AddStringToObject(o, "fire_trigger", fp_fire_trigger_name(c.fire_trigger));
    cJSON_AddBoolToObject(o, "early_close", c.early_close);
    cJSON_AddNumberToObject(o, "relay_gpio", c.relay_gpio);
    cJSON_AddBoolToObject(o, "relay_active_high", c.relay_active_high);
    cJSON_AddNumberToObject(o, "relay_max_pulse_ms", c.relay_max_pulse_ms);
    cJSON_AddNumberToObject(o, "xray_idle_disable_min", c.xray_idle_disable_min);
    cJSON_AddStringToObject(o, "ap_ssid", c.ap_ssid);
    cJSON_AddStringToObject(o, "ap_pass", "");
    cJSON_AddStringToObject(o, "sta_ssid", c.sta_ssid);
    cJSON_AddStringToObject(o, "sta_pass", "");
    cJSON *d = cJSON_AddObjectToObject(o, "defaults");
    cJSON_AddNumberToObject(d, "window_ms", c.def_window_ms);
    cJSON_AddNumberToObject(d, "scrubs", c.def_scrubs);
    cJSON_AddNumberToObject(d, "exposure_ms", c.def_exposure_ms);
    cJSON_AddNumberToObject(d, "fire_delay_ms", c.def_fire_delay_ms);
    return o;
}
