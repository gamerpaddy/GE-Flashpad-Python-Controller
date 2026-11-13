#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    const char *path;          /* "/index.html" */
    const char *mime;
    const uint8_t *start;      /* gzip data */
    const uint8_t *end;
} fp_asset_t;

extern const fp_asset_t fp_assets[];     /* terminated by a NULL path entry */
extern const size_t fp_assets_count;
