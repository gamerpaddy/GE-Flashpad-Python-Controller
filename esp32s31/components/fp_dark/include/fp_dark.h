/*
 * fp_dark - the dark (offset) frame, persisted in the raw "dark" flash partition.
 *
 * Layout: 64 KiB header area at offset 0 (only the first sector is used), 8 MiB of u16 LE pixels from offset 0x10000.
 * The header is written LAST so a power loss during saving leaves no valid dark rather than a corrupt one.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool valid;
    uint32_t window_ticks;
    uint32_t window_ms;
    uint32_t scrubs;
    float mean, std;
    uint32_t crc32;          /* of the pixel data */
    uint32_t boot_id;        /* boot counter at save time (0 = unknown) */
    int64_t saved_us;        /* esp_timer time of saving (only meaningful when boot_id == current boot) */
} fp_dark_meta_t;

esp_err_t fp_dark_init(void);
fp_dark_meta_t fp_dark_meta(void);
bool fp_dark_saving(void);
uint32_t fp_dark_age_s(void);              /* seconds since saved, or 0xFFFFFFFF if saved in an earlier boot */

/* Save the 8 MiB frame in the background. The caller must already hold a read reference on the frame buffer
 * (fp_image_read_begin); the saving task releases it when finished. Returns ESP_ERR_INVALID_STATE if already saving. */
esp_err_t fp_dark_save_async(const uint8_t *frame, const fp_dark_meta_t *meta);

/* Read stored pixel data (offset is relative to the start of the pixel area). */
esp_err_t fp_dark_read(size_t offset, void *buf, size_t len);

#ifdef __cplusplus
}
#endif
