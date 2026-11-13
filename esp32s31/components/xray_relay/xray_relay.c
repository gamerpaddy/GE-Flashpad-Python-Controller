#include "xray_relay.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"

static const char *TAG = "xray_relay";

static struct {
    int gpio;
    bool active_high;
    uint32_t max_pulse_ms;
    uint32_t idle_disable_min;
    bool inited;

    volatile bool master;
    volatile bool on;
    uint32_t fire_count;
    uint32_t last_pulse_ms;
    int64_t last_activity_us;
    int64_t pulse_start_us;

    esp_timer_handle_t pulse_timer;     /* nominal end of pulse                      */
    esp_timer_handle_t guard_timer;     /* independent guard, fires a bit later      */
    esp_timer_handle_t idle_timer;      /* periodic: master auto-disable             */
    portMUX_TYPE lock;
} s = { .gpio = -1, .active_high = true, .max_pulse_ms = 3000, .lock = portMUX_INITIALIZER_UNLOCKED };

static inline int level_inactive(void) { return s.active_high ? 0 : 1; }
static inline int level_active(void)   { return s.active_high ? 1 : 0; }

static void IRAM_ATTR drive_inactive(void)
{
    if (s.gpio >= 0) {
        gpio_set_level((gpio_num_t)s.gpio, level_inactive());
    }
}

esp_err_t xray_relay_early_init(int gpio, bool active_high)
{
    if (gpio < 0) {
        return ESP_ERR_INVALID_ARG;
    }
    /* Preload the output latch with the inactive level BEFORE enabling the output driver, so the pin never glitches. */
    gpio_set_level((gpio_num_t)gpio, active_high ? 0 : 1);
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << gpio,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&io);
    if (err != ESP_OK) {
        return err;
    }
    gpio_set_level((gpio_num_t)gpio, active_high ? 0 : 1);
    s.gpio = gpio;
    s.active_high = active_high;
    s.on = false;
    s.master = false;
    return ESP_OK;
}

static void pulse_end_cb(void *arg)
{
    (void)arg;
    portENTER_CRITICAL(&s.lock);
    drive_inactive();
    bool was_on = s.on;
    s.on = false;
    if (was_on) {
        s.last_pulse_ms = (uint32_t)((esp_timer_get_time() - s.pulse_start_us + 500) / 1000);
    }
    portEXIT_CRITICAL(&s.lock);
    if (s.guard_timer) {
        esp_timer_stop(s.guard_timer);
    }
    if (was_on) {
        ESP_LOGI(TAG, "relay OFF after %u ms", (unsigned)s.last_pulse_ms);
    }
}

static void guard_cb(void *arg)
{
    /* Independent backstop: should never be needed. */
    if (s.on) {
        ESP_LOGE(TAG, "guard timer released a stuck relay pulse");
    }
    pulse_end_cb(arg);
}

static void idle_cb(void *arg)
{
    (void)arg;
    if (s.master && s.idle_disable_min > 0 && !s.on) {
        int64_t idle_us = esp_timer_get_time() - s.last_activity_us;
        if (idle_us > (int64_t)s.idle_disable_min * 60 * 1000000) {
            s.master = false;
            ESP_LOGW(TAG, "X-ray master switch auto-disabled after %u idle minutes", (unsigned)s.idle_disable_min);
        }
    }
}

esp_err_t xray_relay_init(const xray_relay_cfg_t *cfg)
{
    if (!cfg || cfg->gpio < 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s.inited && (s.gpio != cfg->gpio || s.active_high != cfg->active_high)) {
        /* pin changed at runtime: park the old one inactive first */
        xray_relay_off("reconfigure");
        gpio_reset_pin((gpio_num_t)s.gpio);
    }
    if (s.gpio != cfg->gpio || s.active_high != cfg->active_high) {   /* early_init may already have configured the same pin */
        esp_err_t err = xray_relay_early_init(cfg->gpio, cfg->active_high);
        if (err != ESP_OK) {
            return err;
        }
    }
    uint32_t cap = cfg->max_pulse_ms;
    if (cap < 1) cap = 1;
    if (cap > 60000) cap = 60000;
    s.max_pulse_ms = cap;
    s.idle_disable_min = cfg->idle_disable_min;
    s.last_activity_us = esp_timer_get_time();

    if (!s.inited) {
        esp_timer_create_args_t a = { .callback = pulse_end_cb, .name = "xr_pulse", .dispatch_method = ESP_TIMER_TASK };
        ESP_ERROR_CHECK(esp_timer_create(&a, &s.pulse_timer));
        esp_timer_create_args_t g = { .callback = guard_cb, .name = "xr_guard", .dispatch_method = ESP_TIMER_TASK };
        ESP_ERROR_CHECK(esp_timer_create(&g, &s.guard_timer));
        esp_timer_create_args_t i = { .callback = idle_cb, .name = "xr_idle", .dispatch_method = ESP_TIMER_TASK };
        ESP_ERROR_CHECK(esp_timer_create(&i, &s.idle_timer));
        ESP_ERROR_CHECK(esp_timer_start_periodic(s.idle_timer, 10 * 1000000));
        s.inited = true;
    }
    ESP_LOGI(TAG, "relay on GPIO%d active-%s, max pulse %u ms, master OFF", s.gpio, s.active_high ? "high" : "low", (unsigned)s.max_pulse_ms);
    return ESP_OK;
}

bool xray_relay_set_master(bool enable)
{
    s.master = enable;
    s.last_activity_us = esp_timer_get_time();
    if (!enable) {
        xray_relay_off("master disabled");
    }
    ESP_LOGW(TAG, "X-ray master switch %s", enable ? "ENABLED" : "disabled");
    return s.master;
}

bool xray_relay_master(void) { return s.master; }

esp_err_t xray_relay_fire(uint32_t ms, const char *source)
{
    if (!s.inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s.master) {
        ESP_LOGW(TAG, "fire(%u ms, %s) refused: master switch off", (unsigned)ms, source ? source : "?");
        return ESP_ERR_INVALID_STATE;
    }
    if (ms == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (ms > s.max_pulse_ms) {
        ESP_LOGW(TAG, "pulse %u ms clamped to %u ms", (unsigned)ms, (unsigned)s.max_pulse_ms);
        ms = s.max_pulse_ms;
    }
    portENTER_CRITICAL(&s.lock);
    if (s.on) {
        portEXIT_CRITICAL(&s.lock);
        return ESP_ERR_INVALID_STATE;
    }
    s.on = true;
    s.pulse_start_us = esp_timer_get_time();
    gpio_set_level((gpio_num_t)s.gpio, level_active());
    portEXIT_CRITICAL(&s.lock);

    /* Arm both timers AFTER switching on; if arming the first one fails, switch straight back off. */
    if (esp_timer_start_once(s.pulse_timer, (uint64_t)ms * 1000ULL) != ESP_OK) {
        pulse_end_cb(NULL);
        return ESP_FAIL;
    }
    esp_timer_start_once(s.guard_timer, ((uint64_t)ms + 250ULL) * 1000ULL);
    s.fire_count++;
    s.last_activity_us = s.pulse_start_us;
    ESP_LOGW(TAG, "relay ON for %u ms (source: %s)", (unsigned)ms, source ? source : "?");
    return ESP_OK;
}

void xray_relay_off(const char *reason)
{
    if (!s.inited && s.gpio < 0) {
        return;
    }
    if (s.pulse_timer) esp_timer_stop(s.pulse_timer);
    if (s.guard_timer) esp_timer_stop(s.guard_timer);
    portENTER_CRITICAL(&s.lock);
    bool was_on = s.on;
    drive_inactive();
    s.on = false;
    if (was_on) {
        s.last_pulse_ms = (uint32_t)((esp_timer_get_time() - s.pulse_start_us + 500) / 1000);
    }
    portEXIT_CRITICAL(&s.lock);
    if (was_on) {
        ESP_LOGW(TAG, "relay forced OFF (%s) after %u ms", reason ? reason : "?", (unsigned)s.last_pulse_ms);
    }
}

bool xray_relay_is_on(void) { return s.on; }
uint32_t xray_relay_fire_count(void) { return s.fire_count; }
uint32_t xray_relay_last_pulse_ms(void) { return s.last_pulse_ms; }
uint32_t xray_relay_max_pulse_ms(void) { return s.max_pulse_ms; }
int xray_relay_gpio(void) { return s.gpio; }
bool xray_relay_active_high(void) { return s.active_high; }
void xray_relay_touch(void) { s.last_activity_us = esp_timer_get_time(); }
