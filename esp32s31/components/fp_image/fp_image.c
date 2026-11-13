#include "fp_image.h"

#include <math.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/semphr.h"
#include "fp_proto.h"

static const char *TAG = "fp_image";

static uint8_t *s_buf;
static SemaphoreHandle_t s_lock;
static uint32_t s_map[FP_IMG_BLOCKS / 32];
static volatile uint32_t s_count;
static volatile bool s_accept;
static volatile uint32_t s_id = 0xFFFFFFFFu;
static volatile int64_t s_last_us;
static volatile uint32_t s_dup, s_foreign, s_bad, s_stray, s_accepted;

esp_err_t fp_image_init(void)
{
    if (s_buf) {
        return ESP_OK;
    }
    /* One contiguous 8 MiB block, allocated before anything else fragments the PSRAM heap. */
    s_buf = heap_caps_aligned_alloc(64, FP_IMG_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_buf) {
        ESP_LOGE(TAG, "cannot allocate %u bytes of PSRAM (free %u, largest %u)", (unsigned)FP_IMG_BYTES,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
        return ESP_ERR_NO_MEM;
    }
    memset(s_buf, 0, FP_IMG_BYTES);
    s_lock = xSemaphoreCreateMutex();
    ESP_LOGI(TAG, "frame buffer %u bytes in PSRAM at %p (PSRAM free now %u)", (unsigned)FP_IMG_BYTES, s_buf,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    return ESP_OK;
}

uint8_t *fp_image_buf(void) { return s_buf; }
uint32_t fp_image_size(void) { return FP_IMG_BYTES; }

static int s_readers;
static bool s_writing;

bool fp_image_write_begin(void)
{
    bool ok = false;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (!s_writing && s_readers == 0) {
        s_writing = true;
        ok = true;
    }
    xSemaphoreGive(s_lock);
    return ok;
}

void fp_image_write_end(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_writing = false;
    xSemaphoreGive(s_lock);
}

bool fp_image_read_begin(void)
{
    bool ok = false;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (!s_writing) {
        s_readers++;
        ok = true;
    }
    xSemaphoreGive(s_lock);
    return ok;
}

void fp_image_read_end(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (s_readers > 0) s_readers--;
    xSemaphoreGive(s_lock);
}

bool fp_image_writing(void) { return s_writing; }
int fp_image_readers(void) { return s_readers; }

void fp_image_begin(void)
{
    s_accept = false;
    memset(s_map, 0, sizeof s_map);
    s_count = 0;
    s_id = 0xFFFFFFFFu;
    s_last_us = 0;
    s_dup = s_foreign = s_bad = s_stray = s_accepted = 0;
    s_accept = true;
}

void fp_image_stop(void) { s_accept = false; }

uint32_t fp_image_blocks(void) { return s_count; }
bool fp_image_complete(void) { return s_count >= FP_IMG_BLOCKS; }
uint32_t fp_image_id(void) { return s_id; }
int64_t fp_image_last_block_us(void) { return s_last_us; }

int fp_image_missing(uint16_t *ids, int max)
{
    int total = 0;
    for (uint32_t b = 0; b < FP_IMG_BLOCKS; b++) {
        if (!(s_map[b >> 5] & (1u << (b & 31)))) {
            if (ids && total < max) {
                ids[total] = (uint16_t)b;
            }
            total++;
        }
    }
    return total;
}

fp_image_counters_t fp_image_counters(void)
{
    fp_image_counters_t c = { s_accepted, s_dup, s_foreign, s_bad, s_stray };
    return c;
}

/* ------------------------------------------------------------------------------------------------
 * EMAC RX hook
 * ---------------------------------------------------------------------------------------------- */
static inline uint16_t be16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }

bool fp_image_eth_hook(const uint8_t *f, uint32_t len, void *ctx)
{
    (void)ctx;
    /* Ethernet II + IPv4 + UDP only; anything else is none of our business */
    if (len < 14 + 20 + 8 || be16(f + 12) != 0x0800) {
        return false;
    }
    const uint8_t *ip = f + 14;
    if ((ip[0] >> 4) != 4) return false;
    uint32_t ihl = (uint32_t)(ip[0] & 0x0F) * 4;
    if (ihl < 20 || ip[9] != 17) return false;
    if (be16(ip + 6) & 0x3FFF) return false;                 /* fragment */
    if (len < 14 + ihl + 8) return false;
    const uint8_t *udp = ip + ihl;
    if (be16(udp + 2) != FP_HOST_IMAGE_PORT) return false;

    uint32_t ulen = be16(udp + 4);
    const uint8_t *pl = udp + 8;
    if (ulen < 8 || 14 + ihl + ulen > len) { s_bad++; return true; }
    uint32_t plen = ulen - 8;
    if (!s_accept) { s_stray++; return true; }
    if (plen != FP_IMG_DGRAM_BYTES) { s_bad++; return true; }

    uint32_t id = fp_get_u32(pl);
    uint32_t block = fp_get_u32(pl + 4) & 0xFFFFu;
    if (block >= FP_IMG_BLOCKS) { s_bad++; return true; }
    if (s_id == 0xFFFFFFFFu) {
        s_id = id;
    } else if (id != s_id) {
        s_foreign++;
        return true;
    }
    memcpy(s_buf + (size_t)block * FP_IMG_BLOCK_BYTES, pl + 8, FP_IMG_BLOCK_BYTES);
    uint32_t w = block >> 5, bit = 1u << (block & 31);
    if (s_map[w] & bit) {
        s_dup++;
    } else {
        s_map[w] |= bit;
        s_count++;
    }
    s_accepted++;
    s_last_us = esp_timer_get_time();
    return true;
}

/* ------------------------------------------------------------------------------------------------
 * statistics
 * ---------------------------------------------------------------------------------------------- */
void fp_image_compute_stats(fp_frame_stats_t *out)
{
    const uint16_t *p = (const uint16_t *)s_buf;
    const size_t n = FP_IMG_BYTES / 2;
    uint32_t mn = 0xFFFF, mx = 0;
    uint64_t sum = 0, sumsq = 0;
    for (size_t i = 0; i < n; i++) {
        uint32_t v = p[i];
        if (v < mn) mn = v;
        if (v > mx) mx = v;
        sum += v;
        sumsq += (uint64_t)v * v;
    }
    double mean = (double)sum / (double)n;
    double var = (double)sumsq / (double)n - mean * mean;
    out->min = mn;
    out->max = mx;
    out->mean = (float)mean;
    out->std = (float)sqrt(var > 0 ? var : 0);
    out->crc32 = esp_rom_crc32_le(0, s_buf, FP_IMG_BYTES);
}
