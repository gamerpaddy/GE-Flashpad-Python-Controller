/*
 * xray_relay - GPIO relay output that fires the X-ray source, with safety logic.
 *
 * Design rules (see esp32s31/README.md "X-ray output safety"):
 *   - the pin is driven to its INACTIVE level as the very first thing the firmware does (xray_relay_early_init)
 *   - a software master switch gates every pulse; it is OFF after every boot and auto-clears after an idle time
 *   - a pulse is always bounded: requests are clamped to max_pulse_ms and a second, independent timer
 *     force-releases the pin shortly after the nominal end even if the first timer is lost
 *   - anything that goes wrong (abort, link loss, session error, timeout) calls xray_relay_off()
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int gpio;                  /* output pin                                              */
    bool active_high;          /* level that energises the relay                          */
    uint32_t max_pulse_ms;     /* hard cap for one pulse (clamped, 1..60000)              */
    uint32_t idle_disable_min; /* master switch auto-clears after this many idle minutes (0 = never) */
} xray_relay_cfg_t;

/* Call before anything else in app_main(): configure the pin as an output at the inactive level. */
esp_err_t xray_relay_early_init(int gpio, bool active_high);

/* Full init (timers, config). May be called again after the config changed. */
esp_err_t xray_relay_init(const xray_relay_cfg_t *cfg);

/* Master enable. Returns the new state. Enabling resets the idle timer. Always false after boot. */
bool xray_relay_set_master(bool enable);
bool xray_relay_master(void);

/* Pulse the relay for `ms` (clamped to max_pulse_ms). Fails with ESP_ERR_INVALID_STATE if the master switch is off
 * or a pulse is already active.  `source` is only used for the log. */
esp_err_t xray_relay_fire(uint32_t ms, const char *source);

/* Force the relay to the inactive level now and cancel any pulse. Safe from any task. */
void xray_relay_off(const char *reason);

bool xray_relay_is_on(void);
uint32_t xray_relay_fire_count(void);
uint32_t xray_relay_last_pulse_ms(void);
uint32_t xray_relay_max_pulse_ms(void);
int xray_relay_gpio(void);
bool xray_relay_active_high(void);

/* UI activity ping (resets the idle auto-disable). */
void xray_relay_touch(void);

#ifdef __cplusplus
}
#endif
