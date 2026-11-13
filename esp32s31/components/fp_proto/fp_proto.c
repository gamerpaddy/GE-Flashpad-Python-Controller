#include "fp_proto.h"

#include <string.h>

/* ------------------------------------------------------------------------------------------------
 * low level
 * ---------------------------------------------------------------------------------------------- */
size_t fp_build_pdap(uint8_t *out, size_t cap, uint32_t cmd, const void *payload, uint32_t payload_len)
{
    if (!out || cap < FP_PDAP_HDR_LEN + (size_t)payload_len) {
        return 0;
    }
    fp_put_u32(out, cmd);
    fp_put_u32(out + 4, payload_len);
    if (payload_len) {
        memcpy(out + FP_PDAP_HDR_LEN, payload, payload_len);
    }
    return FP_PDAP_HDR_LEN + (size_t)payload_len;
}

size_t fp_build_urp(uint8_t *out, size_t cap, uint32_t seq, uint32_t flag, const uint8_t *pdap, size_t pdap_len)
{
    if (!out || cap < FP_URP_LEN + pdap_len) {
        return 0;
    }
    fp_put_u32(out, seq);
    fp_put_u32(out + 4, flag);
    if (pdap_len) {
        memmove(out + FP_URP_LEN, pdap, pdap_len);   /* memmove: caller may build in place */
    }
    return FP_URP_LEN + pdap_len;
}

size_t fp_build_ack(uint8_t *out, size_t cap, uint32_t det_seq)
{
    if (!out || cap < FP_URP_LEN) {
        return 0;
    }
    fp_put_u32(out, det_seq);
    fp_put_u32(out + 4, FP_URP_FLAG_ACK);
    return FP_URP_LEN;
}

/* ------------------------------------------------------------------------------------------------
 * simple messages
 * ---------------------------------------------------------------------------------------------- */
size_t fp_build_system_startup(uint8_t *out, size_t cap, uint16_t port, const uint8_t host_ip[4])
{
    uint8_t pl[6];
    fp_put_u16_be(pl, port);
    memcpy(pl + 2, host_ip, 4);
    return fp_build_pdap(out, cap, FP_CMD_SYSTEM_STARTUP, pl, sizeof pl);
}

size_t fp_build_port_setup(uint8_t *out, size_t cap, uint16_t cmd_port, uint16_t img_port)
{
    uint8_t pl[4];
    fp_put_u16_be(pl, cmd_port);
    fp_put_u16_be(pl + 2, img_port);
    return fp_build_pdap(out, cap, FP_CMD_PORT_SETUP, pl, sizeof pl);
}

size_t fp_build_signature_request(uint8_t *out, size_t cap)
{
    return fp_build_pdap(out, cap, FP_CMD_SIGNATURE, NULL, 0);
}

size_t fp_build_execute(uint8_t *out, size_t cap)
{
    return fp_build_pdap(out, cap, FP_CMD_EXECUTE_SCRIPT, NULL, 0);
}

size_t fp_build_img_retrieval_request(uint8_t *out, size_t cap, uint32_t script_id)
{
    uint8_t pl[4];
    fp_put_u32(pl, script_id);
    return fp_build_pdap(out, cap, FP_CMD_IMG_RETRIEVAL_REQ, pl, sizeof pl);
}

/* wire payload = [image_port:u16 LE][host_port:u16 LE]  (flashpad_acquire.build_image_retrival) */
size_t fp_build_img_retrieval(uint8_t *out, size_t cap, uint16_t image_port, uint16_t host_port)
{
    uint8_t pl[4];
    fp_put_u16(pl, image_port);
    fp_put_u16(pl + 2, host_port);
    return fp_build_pdap(out, cap, FP_CMD_IMG_RETRIEVAL, pl, sizeof pl);
}

size_t fp_build_host_event(uint8_t *out, size_t cap, uint32_t event_id)
{
    uint8_t pl[4];
    fp_put_u32(pl, event_id);
    return fp_build_pdap(out, cap, FP_CMD_HOST_SCRIPT_CMD, pl, sizeof pl);
}

size_t fp_build_abort(uint8_t *out, size_t cap, uint32_t opcode)
{
    return fp_build_pdap(out, cap, opcode, NULL, 0);
}

size_t fp_build_xfer_reply(uint8_t *out, size_t cap, const uint16_t *ids, uint32_t n)
{
    size_t need = FP_PDAP_HDR_LEN + 4 + (size_t)n * 4;
    if (!out || cap < need) {
        return 0;
    }
    fp_put_u32(out, FP_CMD_IMG_XFER_REPLY);
    fp_put_u32(out + 4, 4 + n * 4);
    fp_put_u32(out + 8, n);
    for (uint32_t i = 0; i < n; i++) {
        fp_put_u32(out + 12 + i * 4, ids[i]);
    }
    return need;
}

/* ------------------------------------------------------------------------------------------------
 * scripts
 * ---------------------------------------------------------------------------------------------- */
/* Script writer: builds the GENERIC_SCRIPT PDAP in place.
 *   [05 00 00 00][payload_len:4][scriptID:2][repeatCount:2][repeatEvent:4][cmds...][00 00]            */
typedef struct {
    uint8_t *buf;
    size_t cap;
    size_t len;      /* bytes written so far, starting with the 8 byte PDAP header */
    int err;
} script_w_t;

static void sw_begin(script_w_t *w, uint8_t *buf, size_t cap, uint16_t script_id, uint16_t repeat, uint32_t repeat_event)
{
    w->buf = buf; w->cap = cap; w->len = 0; w->err = 0;
    if (cap < 16) { w->err = 1; return; }
    fp_put_u32(buf, FP_CMD_GENERIC_SCRIPT);
    fp_put_u32(buf + 4, 0);                 /* patched in sw_end */
    fp_put_u16(buf + 8, script_id);
    fp_put_u16(buf + 10, repeat);
    fp_put_u32(buf + 12, repeat_event);
    w->len = 16;
}

static uint8_t *sw_reserve(script_w_t *w, size_t n)
{
    if (w->err || w->len + n + 2 > w->cap) {   /* +2 terminator */
        w->err = 1;
        return NULL;
    }
    uint8_t *p = w->buf + w->len;
    w->len += n;
    return p;
}

static void sw_acq(script_w_t *w, const fp_acq_params_t *a)
{
    uint8_t *p = sw_reserve(w, 17);
    if (!p) return;
    p[0] = FP_DC_ACQUISITION;
    p[1] = a->type_mode;
    p[2] = a->image_id;
    p[3] = a->no_scrubs;
    fp_put_u32(p + 4, a->scrub_duration);
    fp_put_u32(p + 8, a->max_expose);
    fp_put_u32(p + 12, a->tail_time);
    p[16] = a->transfer_mode;
}

static void sw_roe(script_w_t *w, uint32_t roe_cmd, uint32_t roe_data, uint32_t timer)
{
    uint8_t *p = sw_reserve(w, 14);
    if (!p) return;
    p[0] = FP_DC_ROE;
    p[1] = 0;                               /* response flag */
    fp_put_u32(p + 2, timer);
    fp_put_u32(p + 6, roe_cmd);
    fp_put_u32(p + 10, roe_data);
}

static void sw_delay(script_w_t *w, uint32_t us)
{
    uint8_t *p = sw_reserve(w, 5);
    if (!p) return;
    p[0] = FP_DC_DELAY;
    fp_put_u32(p + 1, us);
}

static void sw_event(script_w_t *w, uint32_t id)
{
    uint8_t *p = sw_reserve(w, 5);
    if (!p) return;
    p[0] = FP_DC_SEND_EVENT;
    fp_put_u32(p + 1, id);
}

static size_t sw_end(script_w_t *w)
{
    uint8_t *p = sw_reserve(w, 2);
    if (!p) return 0;
    p[0] = 0; p[1] = 0;
    fp_put_u32(w->buf + 4, (uint32_t)(w->len - FP_PDAP_HDR_LEN));
    return w->len;
}

size_t fp_build_acq_script(uint8_t *out, size_t cap, const fp_acq_params_t *p)
{
    script_w_t w;
    sw_begin(&w, out, cap, 0, 0, 0);       /* GUI/capture_minimal always use scriptID 0 */
    sw_acq(&w, p);
    sw_delay(&w, 10000);
    return sw_end(&w);
}

size_t fp_build_script7(uint8_t *out, size_t cap)
{
    /* constants from flashpad_acquire.py: ROE_SCAN_SETUP_CMD 0x4050 / VAL 0x00D20000, ROE_INIT_CMD2 0x4002, ROE_INIT_CMD3 0x7900 */
    script_w_t w;
    sw_begin(&w, out, cap, 7, 0, 0);
    sw_roe(&w, 0x00004050u, 0x00D20000u, 1000000u);
    sw_roe(&w, 0x00004002u, 2u, 1000000u);
    sw_roe(&w, 0x00007900u, 16u, 0u);                 /* timer = 0 */
    sw_delay(&w, 50000);
    for (int i = 0; i < 4; i++) {
        sw_roe(&w, 0u, 0u, 1000000u);
        sw_delay(&w, 10000);
    }
    sw_event(&w, FP_EVENT_ROE_DONE);
    return sw_end(&w);
}

/* ------------------------------------------------------------------------------------------------
 * parsers
 * ---------------------------------------------------------------------------------------------- */
int fp_parse_urp(const uint8_t *buf, size_t len, fp_urp_t *out)
{
    if (!buf || !out || len < FP_URP_LEN) {
        return -1;
    }
    out->seq = fp_get_u32(buf);
    out->flag = fp_get_u32(buf + 4);
    out->pdap = (len > FP_URP_LEN) ? buf + FP_URP_LEN : NULL;
    out->pdap_len = len - FP_URP_LEN;
    return 0;
}

int fp_parse_pdap(const uint8_t *buf, size_t len, fp_pdap_t *out)
{
    if (!buf || !out || len < FP_PDAP_HDR_LEN) {
        return -1;
    }
    out->cmd = fp_get_u32(buf);
    out->payload_len = fp_get_u32(buf + 4);
    out->payload = buf + FP_PDAP_HDR_LEN;
    size_t avail = len - FP_PDAP_HDR_LEN;
    out->payload_avail = (uint32_t)(avail < out->payload_len ? avail : out->payload_len);
    return 0;
}

int fp_parse_beacon(const fp_pdap_t *p, uint8_t ip[4], uint8_t mac[6])
{
    if (!p || p->cmd != FP_CMD_SYSTEM_STARTUP || p->payload_avail < 10) {
        return -1;
    }
    memcpy(ip, p->payload, 4);
    memcpy(mac, p->payload + 4, 6);
    return 0;
}

static void copy_field(char *dst, size_t dstlen, const uint8_t *src, size_t n)
{
    /* strip NUL/space padding on both ends like Python's .strip(b"\x00 ") */
    size_t b = 0, e = n;
    while (b < e && (src[b] == 0 || src[b] == ' ')) b++;
    while (e > b && (src[e - 1] == 0 || src[e - 1] == ' ')) e--;
    size_t k = e - b;
    if (k >= dstlen) k = dstlen - 1;
    memcpy(dst, src + b, k);
    dst[k] = 0;
    for (size_t i = 0; i < k; i++) {
        if (dst[i] < 0x20 || dst[i] > 0x7e) dst[i] = '?';
    }
}

int fp_parse_signature(const fp_pdap_t *p, fp_signature_t *out)
{
    if (!p || !out || p->cmd != FP_CMD_SIGNATURE || p->payload_avail < 54) {
        return -1;
    }
    memcpy(out->mac, p->payload, 6);
    copy_field(out->serial, sizeof out->serial, p->payload + 22, 12);
    copy_field(out->model, sizeof out->model, p->payload + 34, 12);
    memcpy(out->fw, p->payload + 46, 8);
    return 0;
}

int fp_parse_execute_reply(const fp_pdap_t *p, uint32_t *time_field)
{
    if (!p || p->cmd != FP_CMD_EXECUTE_SCRIPT || p->payload_avail < 5) {
        return -1;
    }
    if (time_field) *time_field = fp_get_u32(p->payload);
    return p->payload[4];
}

int fp_parse_script_reply(const fp_pdap_t *p, uint32_t *count)
{
    if (!p || p->cmd != FP_CMD_GENERIC_SCRIPT || p->payload_avail < 1) {
        return -1;
    }
    if (count) *count = (p->payload_avail >= 5) ? fp_get_u32(p->payload + 1) : 0;
    return p->payload[0];
}

int fp_parse_xfer_query(const fp_pdap_t *p, uint16_t *image_id, uint16_t *script_id)
{
    if (!p || p->cmd != FP_CMD_IMG_XFER_QUERY || p->payload_avail < 4) {
        return -1;
    }
    if (fp_get_u32(p->payload) == 0xDEADBEEFu) {
        return 1;
    }
    if (image_id) *image_id = fp_get_u16(p->payload);
    if (script_id) *script_id = fp_get_u16(p->payload + 2);
    return 0;
}

int fp_parse_retrieval_request_reply(const fp_pdap_t *p, uint32_t *first_id)
{
    if (!p || p->cmd != FP_CMD_IMG_RETRIEVAL_REQ || p->payload_avail < 4) {
        return -1;
    }
    uint32_t len_word = fp_get_u32(p->payload);     /* count*4 + 4 */
    if (len_word < 4 || (len_word & 3u)) {
        return -1;
    }
    uint32_t count = (len_word - 4) / 4;
    if (count && p->payload_avail >= 8 && first_id) {
        *first_id = fp_get_u32(p->payload + 4);
    }
    return (int)count;
}

int fp_parse_state_notify(const fp_pdap_t *p, uint32_t *event_id)
{
    if (!p || p->cmd != FP_CMD_STATE_NOTIFY || p->payload_avail < 4) {
        return -1;
    }
    if (event_id) *event_id = fp_get_u32(p->payload);
    return 0;
}

int fp_parse_image_header(const uint8_t *d, size_t len, uint32_t *image_id, uint16_t *block)
{
    if (!d || len != FP_IMG_DGRAM_BYTES) {
        return -1;
    }
    uint32_t word = fp_get_u32(d + 4);
    uint16_t b = (uint16_t)(word & 0xFFFFu);
    if (b >= FP_IMG_BLOCKS) {
        return -1;
    }
    if (image_id) *image_id = fp_get_u32(d);
    if (block) *block = b;
    return 0;
}

uint32_t fp_ms_to_ticks(uint32_t ms)
{
    uint64_t t = (uint64_t)ms * FP_TICKS_PER_MS;
    return t > 0xFFFFFFFFull ? 0xFFFFFFFFu : (uint32_t)t;
}

uint32_t fp_ticks_to_ms(uint32_t ticks)
{
    return ticks / FP_TICKS_PER_MS;
}
