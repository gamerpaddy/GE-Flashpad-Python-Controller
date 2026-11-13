#include "fp_dark.h"

#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "fp_image.h"
#include "fp_proto.h"

static const char *TAG = "fp_dark";

#define DARK_SUBTYPE   0x40
#define HDR_MAGIC      0x4B445046u     /* 'FPDK' */
#define HDR_VERSION    1u
#define DATA_OFFSET    0x10000u
#define ERASE_CHUNK    0x10000u        /* 64 KiB: erase in steps and yield so Wi-Fi/HTTP keep running */
#define WRITE_CHUNK    0x4000u

typedef struct {
    uint32_t magic, version;
    uint32_t window_ticks, window_ms, scrubs;
    float mean, std;
    uint32_t crc32;
    uint32_t boot_id;
    uint32_t reserved[7];
    uint32_t hdr_crc;                  /* crc of the bytes above */
} dark_hdr_t;

static const esp_partition_t *s_part;
static fp_dark_meta_t s_meta;
static volatile bool s_saving;
static uint32_t s_boot_id;

static uint32_t crc_bytes(const void *p, size_t n)
{
    uint32_t c = 0xFFFFFFFFu;
    const uint8_t *b = p;
    for (size_t i = 0; i < n; i++) {
        c ^= b[i];
        for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & -(c & 1));
    }
    return ~c;
}

static void load_boot_id(void)
{
    nvs_handle_t h;
    uint32_t id = 0;
    if (nvs_open("fpdark", NVS_READWRITE, &h) == ESP_OK) {
        nvs_get_u32(h, "boot", &id);
        id++;
        nvs_set_u32(h, "boot", id);
        nvs_commit(h);
        nvs_close(h);
    }
    s_boot_id = id ? id : 1;
}

esp_err_t fp_dark_init(void)
{
    load_boot_id();
    s_part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, (esp_partition_subtype_t)DARK_SUBTYPE, "dark");
    if (!s_part) {
        ESP_LOGE(TAG, "partition 'dark' not found");
        return ESP_ERR_NOT_FOUND;
    }
    dark_hdr_t h;
    if (esp_partition_read(s_part, 0, &h, sizeof h) == ESP_OK && h.magic == HDR_MAGIC && h.version == HDR_VERSION &&
        h.hdr_crc == crc_bytes(&h, offsetof(dark_hdr_t, hdr_crc))) {
        s_meta.valid = true;
        s_meta.window_ticks = h.window_ticks;
        s_meta.window_ms = h.window_ms;
        s_meta.scrubs = h.scrubs;
        s_meta.mean = h.mean;
        s_meta.std = h.std;
        s_meta.crc32 = h.crc32;
        s_meta.boot_id = h.boot_id;
        ESP_LOGI(TAG, "stored dark: window %u ms, scrubs %u, mean %.1f, crc %08x", (unsigned)h.window_ms, (unsigned)h.scrubs, h.mean, (unsigned)h.crc32);
    } else {
        ESP_LOGI(TAG, "no valid dark stored");
    }
    return ESP_OK;
}

fp_dark_meta_t fp_dark_meta(void) { return s_meta; }
bool fp_dark_saving(void) { return s_saving; }

uint32_t fp_dark_age_s(void)
{
    if (!s_meta.valid || s_meta.boot_id != s_boot_id) return 0xFFFFFFFFu;
    return (uint32_t)((esp_timer_get_time() - s_meta.saved_us) / 1000000);
}

esp_err_t fp_dark_read(size_t offset, void *buf, size_t len)
{
    if (!s_part || !s_meta.valid) return ESP_ERR_NOT_FOUND;
    if (offset + len > FP_IMG_BYTES) return ESP_ERR_INVALID_SIZE;
    return esp_partition_read(s_part, DATA_OFFSET + offset, buf, len);
}

typedef struct {
    const uint8_t *frame;
    fp_dark_meta_t meta;
} save_args_t;

static void save_task(void *arg)
{
    save_args_t *a = arg;
    esp_err_t err = ESP_OK;
    int64_t t0 = esp_timer_get_time();
    ESP_LOGI(TAG, "saving dark to flash: erasing %u KiB ...", (unsigned)(s_part->size / 1024));

    /* invalidate first: a power cut now leaves "no dark" */
    s_meta.valid = false;
    for (uint32_t off = 0; off < s_part->size && err == ESP_OK; off += ERASE_CHUNK) {
        uint32_t n = s_part->size - off < ERASE_CHUNK ? s_part->size - off : ERASE_CHUNK;
        err = esp_partition_erase_range(s_part, off, n);
        vTaskDelay(1);
    }
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "erased in %d ms, writing pixels ...", (int)((esp_timer_get_time() - t0) / 1000));
        for (size_t off = 0; off < FP_IMG_BYTES && err == ESP_OK; off += WRITE_CHUNK) {
            err = esp_partition_write(s_part, DATA_OFFSET + off, a->frame + off, WRITE_CHUNK);
            if ((off & 0xFFFFF) == 0) vTaskDelay(1);
        }
    }
    if (err == ESP_OK) {
        dark_hdr_t h = { .magic = HDR_MAGIC, .version = HDR_VERSION, .window_ticks = a->meta.window_ticks, .window_ms = a->meta.window_ms,
                         .scrubs = a->meta.scrubs, .mean = a->meta.mean, .std = a->meta.std, .crc32 = a->meta.crc32, .boot_id = s_boot_id };
        h.hdr_crc = crc_bytes(&h, offsetof(dark_hdr_t, hdr_crc));
        err = esp_partition_write(s_part, 0, &h, sizeof h);
    }
    if (err == ESP_OK) {
        s_meta = a->meta;
        s_meta.valid = true;
        s_meta.boot_id = s_boot_id;
        s_meta.saved_us = esp_timer_get_time();
        ESP_LOGI(TAG, "dark saved in %d ms", (int)((esp_timer_get_time() - t0) / 1000));
    } else {
        ESP_LOGE(TAG, "dark save failed: %s", esp_err_to_name(err));
    }
    fp_image_read_end();
    s_saving = false;
    free(a);
    vTaskDelete(NULL);
}

esp_err_t fp_dark_save_async(const uint8_t *frame, const fp_dark_meta_t *meta)
{
    if (!s_part) return ESP_ERR_NOT_FOUND;
    if (s_saving) return ESP_ERR_INVALID_STATE;
    save_args_t *a = malloc(sizeof *a);
    if (!a) return ESP_ERR_NO_MEM;
    a->frame = frame;
    a->meta = *meta;
    s_saving = true;
    if (xTaskCreate(save_task, "dark_save", 6144, a, 4, NULL) != pdPASS) {
        s_saving = false;
        free(a);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
