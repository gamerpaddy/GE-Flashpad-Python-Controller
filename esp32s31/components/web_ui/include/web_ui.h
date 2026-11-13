#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Start capturing the device log into a ring buffer (call as early as possible in app_main). */
void fp_log_ring_start(void);
/* Copy up to `max` bytes of the most recent log text into buf (NUL terminated); returns the length. */
size_t fp_log_ring_read(char *buf, size_t max);

/* HTTP server implementing esp32s31/docs/API.md */
esp_err_t web_ui_start(void);

#ifdef __cplusplus
}
#endif
