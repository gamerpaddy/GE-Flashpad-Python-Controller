#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "web_ui.h"

#define RING_SIZE 8192

static char s_ring[RING_SIZE];
static size_t s_head;          /* next write position */
static size_t s_fill;          /* valid bytes (<= RING_SIZE) */
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static vprintf_like_t s_orig;

static void ring_put(const char *p, size_t n)
{
    portENTER_CRITICAL(&s_lock);
    for (size_t i = 0; i < n; i++) {
        s_ring[s_head] = p[i];
        s_head = (s_head + 1) % RING_SIZE;
    }
    s_fill = s_fill + n > RING_SIZE ? RING_SIZE : s_fill + n;
    portEXIT_CRITICAL(&s_lock);
}

static int log_vprintf(const char *fmt, va_list ap)
{
    char line[256];
    va_list ap2;
    va_copy(ap2, ap);
    int n = vsnprintf(line, sizeof line, fmt, ap2);
    va_end(ap2);
    if (n > 0) {
        size_t len = (size_t)n < sizeof line ? (size_t)n : sizeof line - 1;
        /* strip ANSI colour codes (ESC [ ... m) */
        char clean[256];
        size_t o = 0;
        for (size_t i = 0; i < len; i++) {
            if (line[i] == 0x1b && i + 1 < len && line[i + 1] == '[') {
                while (i < len && line[i] != 'm') i++;
                continue;
            }
            clean[o++] = line[i];
        }
        ring_put(clean, o);
    }
    return s_orig ? s_orig(fmt, ap) : n;
}

void fp_log_ring_start(void)
{
    if (!s_orig) s_orig = esp_log_set_vprintf(log_vprintf);
}

size_t fp_log_ring_read(char *buf, size_t max)
{
    if (max == 0) return 0;
    size_t n;
    portENTER_CRITICAL(&s_lock);
    n = s_fill < max - 1 ? s_fill : max - 1;
    size_t start = (s_head + RING_SIZE - n) % RING_SIZE;
    for (size_t i = 0; i < n; i++) buf[i] = s_ring[(start + i) % RING_SIZE];
    portEXIT_CRITICAL(&s_lock);
    buf[n] = 0;
    return n;
}
