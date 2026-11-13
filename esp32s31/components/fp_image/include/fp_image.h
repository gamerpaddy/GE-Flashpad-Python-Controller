/*
 * fp_image - the one 8 MiB frame buffer (PSRAM), the 2048-bit received-block map, and the Ethernet RX hook that
 * copies image datagrams ([imageId:4][blockIndex:4][4096 B], udp 8100 -> 6660) straight into the buffer.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t min, max;
    float mean, std;
    uint32_t crc32;
} fp_frame_stats_t;

esp_err_t fp_image_init(void);
uint8_t *fp_image_buf(void);
uint32_t fp_image_size(void);

/* Access control for the single frame buffer.
 *   write_begin: the capture task takes it for the whole receive+verify phase; fails if any reader is active or a write is running.
 *   read_begin : web streaming / the dark-store writer; fails while a capture is writing. Many concurrent readers are fine. */
bool fp_image_write_begin(void);
void fp_image_write_end(void);
bool fp_image_read_begin(void);
void fp_image_read_end(void);
bool fp_image_writing(void);
int fp_image_readers(void);

/* start receiving a new image: clears the block map/counters and opens the hook (does NOT clear the pixel data) */
void fp_image_begin(void);
void fp_image_stop(void);                 /* close the hook */

uint32_t fp_image_blocks(void);           /* distinct blocks received */
bool fp_image_complete(void);
uint32_t fp_image_id(void);               /* imageId of the first accepted datagram (0xFFFFFFFF if none yet) */
int64_t fp_image_last_block_us(void);     /* esp_timer time of the last accepted datagram, 0 if none */
int fp_image_missing(uint16_t *ids, int max);   /* returns the total number missing, fills at most `max` ids ascending */

typedef struct { uint32_t accepted, duplicate, foreign, bad, stray; } fp_image_counters_t;
fp_image_counters_t fp_image_counters(void);

void fp_image_compute_stats(fp_frame_stats_t *out);   /* min/max/mean/std/crc32 of the full 2048x2048 u16 frame */

/* hook to pass to fp_eth_set_rx_hook() */
bool fp_image_eth_hook(const uint8_t *frame, uint32_t len, void *ctx);

#ifdef __cplusplus
}
#endif
