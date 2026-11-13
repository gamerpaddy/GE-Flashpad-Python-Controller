/*
 * fp_proto - portable GE FlashPad wire protocol (URP/PDAP framing, packet builders, parsers).
 *
 * No ESP-IDF headers on purpose: everything here is plain C99 so it can be reviewed and unit-tested
 * anywhere.  Byte layouts follow esp32s31/docs/PROTOCOL.md and are checked byte-for-byte against the
 * Python builders in flashpad_acquire.py by tools/flashpad_sim.py.
 *
 * All multi-byte fields are little-endian unless the name ends in _be.
 */
#ifndef FP_PROTO_H
#define FP_PROTO_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- network constants ------------------------------------------------------------------- */
#define FP_DETECTOR_PORT       8100u   /* detector listens here for every command            */
#define FP_HOST_CMD_PORT       5550u   /* host control socket (source of every host packet)   */
#define FP_HOST_IMAGE_PORT     6660u   /* image datagrams (detector :8100 -> host :6660)      */
#define FP_DEFAULT_REPLY_PORT  48879u  /* where replies go before PORT_SETUP                  */

/* ---- framing ------------------------------------------------------------------------------ */
#define FP_URP_LEN             8u
#define FP_PDAP_HDR_LEN        8u
#define FP_URP_FLAG_DATA       0u      /* PDAP follows            */
#define FP_URP_FLAG_ACK        1u      /* bare ACK, no body       */

/* ---- PDAP command ids -------------------------------------------------------------------- */
#define FP_CMD_SYSTEM_STARTUP     0x00000001u
#define FP_CMD_PORT_SETUP         0x00000002u
#define FP_CMD_SIGNATURE          0x00000003u
#define FP_CMD_HOST_SCRIPT_CMD    0x00000004u  /* HOST_SCRIPT_COMMAND (opcode 4: post host event) */
#define FP_CMD_GENERIC_SCRIPT     0x00000005u
#define FP_CMD_EXECUTE_SCRIPT     0x00000006u
#define FP_CMD_EXEC_STATUS_POLL   0x00000007u
#define FP_CMD_IMG_XFER_REPLY     0x00000009u  /* serial form of image_xfer_status reply (host->det) */
#define FP_CMD_ABORT_A            0x0000000Au  /* abort script list + reset image state (untested)  */
#define FP_CMD_ABORT_B            0x0000000Bu
#define FP_CMD_IMG_RETRIEVAL_REQ  0x00000041u
#define FP_CMD_IMG_RETRIEVAL      0x00000098u
#define FP_CMD_EXECUTION_COMPLETE 0x00010000u
#define FP_CMD_IMG_XFER_QUERY     0x00030000u
#define FP_CMD_STATE_NOTIFY       0x00040000u  /* payload [u32 eventId]; [1]=window open, [17]=ROE init done */
#define FP_CMD_WINDOW_EXPIRED     0x00050000u

/* packed script commands */
#define FP_DC_ACQUISITION  1
#define FP_DC_ROE          2
#define FP_DC_SEND_EVENT   3
#define FP_DC_WAIT_EVENT   4
#define FP_DC_DELAY        5

/* host events seen in state notifications */
#define FP_EVENT_WINDOW_OPEN 1u
#define FP_EVENT_ROE_DONE    17u

/* sizes */
#define FP_IMG_BLOCKS        2048u
#define FP_IMG_BLOCK_BYTES   4096u
#define FP_IMG_DGRAM_BYTES   (8u + FP_IMG_BLOCK_BYTES)          /* 4104 */
#define FP_IMG_BYTES         (FP_IMG_BLOCKS * FP_IMG_BLOCK_BYTES) /* 8 MiB */
#define FP_IMG_DIM           2048u

/* ~26.3 MHz detector tick used by scrub/window/tail fields (measured, see PROTOCOL.md) */
#define FP_TICKS_PER_MS      26300u

/* Largest packet the host ever builds (script7 = 146 B PDAP + 8 B URP) with margin. */
#define FP_MAX_PACKET        1536u

/* ---- little-endian helpers ---------------------------------------------------------------- */
static inline void fp_put_u16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static inline void fp_put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static inline void fp_put_u16_be(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
static inline uint16_t fp_get_u16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static inline uint32_t fp_get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* ---- builders (all return the number of bytes written, 0 if the buffer is too small) ------- */

/* PDAP: [cmd:u32][len:u32][payload] */
size_t fp_build_pdap(uint8_t *out, size_t cap, uint32_t cmd, const void *payload, uint32_t payload_len);

/* URP wrapper: [seq:u32][flag:u32][pdap...] ; flag = FP_URP_FLAG_DATA for commands */
size_t fp_build_urp(uint8_t *out, size_t cap, uint32_t seq, uint32_t flag, const uint8_t *pdap, size_t pdap_len);

/* bare ACK for a received detector data packet: [det_seq][1] (8 bytes) */
size_t fp_build_ack(uint8_t *out, size_t cap, uint32_t det_seq);

/* individual PDAP messages (pdap only, no URP) */
size_t fp_build_system_startup(uint8_t *out, size_t cap, uint16_t port, const uint8_t host_ip[4]);
size_t fp_build_port_setup(uint8_t *out, size_t cap, uint16_t cmd_port, uint16_t img_port);
size_t fp_build_signature_request(uint8_t *out, size_t cap);
size_t fp_build_execute(uint8_t *out, size_t cap);
size_t fp_build_img_retrieval_request(uint8_t *out, size_t cap, uint32_t script_id);
size_t fp_build_img_retrieval(uint8_t *out, size_t cap, uint16_t image_port, uint16_t host_port);
size_t fp_build_host_event(uint8_t *out, size_t cap, uint32_t event_id);   /* HOST_SCRIPT_COMMAND opcode 4 */
size_t fp_build_abort(uint8_t *out, size_t cap, uint32_t opcode);           /* 0x0A / 0x0B, plen 0 */

/* cmd 9 reply to the detector's image_xfer_status query: [numMissed:u32][blockIdx:u32]*n
 * ids are block indices (0..2047). n may be 0 (== "all received"). */
size_t fp_build_xfer_reply(uint8_t *out, size_t cap, const uint16_t *ids, uint32_t n);

/* GENERIC_SCRIPT builders */
typedef struct {
    uint8_t type_mode;        /* 0 = X-ray acquisition, 1 = dark/offset                       */
    uint8_t image_id;         /* 1                                                           */
    uint8_t no_scrubs;        /* clear cycles before the window                              */
    uint32_t scrub_duration;  /* ticks, GUI uses 50000                                       */
    uint32_t max_expose;      /* ticks - the integration window                              */
    uint32_t tail_time;       /* ticks, GUI uses 250000                                      */
    uint8_t transfer_mode;    /* 0                                                           */
} fp_acq_params_t;

/* [Script 0] as the GUI/capture_minimal builds it: Acquisition + Delay(10000) */
size_t fp_build_acq_script(uint8_t *out, size_t cap, const fp_acq_params_t *p);
/* [Script 7] ROE initialisation (146 B PDAP) */
size_t fp_build_script7(uint8_t *out, size_t cap);

/* ---- parsers -------------------------------------------------------------------------------- */
typedef struct {
    uint32_t seq;
    uint32_t flag;            /* 0 data, !=0 bare ACK */
    const uint8_t *pdap;      /* NULL for a bare ACK / short packet */
    size_t pdap_len;
} fp_urp_t;

typedef struct {
    uint32_t cmd;
    uint32_t payload_len;     /* as declared */
    const uint8_t *payload;   /* points into the input buffer; at most the bytes actually present */
    uint32_t payload_avail;
} fp_pdap_t;

/* return 0 on success, -1 if too short */
int fp_parse_urp(const uint8_t *buf, size_t len, fp_urp_t *out);
int fp_parse_pdap(const uint8_t *buf, size_t len, fp_pdap_t *out);

/* beacon (cmd 1, plen 10): [detector_ip:4][MAC:6] */
int fp_parse_beacon(const fp_pdap_t *p, uint8_t ip[4], uint8_t mac[6]);

typedef struct {
    uint8_t mac[6];
    char serial[13];
    char model[13];
    uint8_t fw[8];
} fp_signature_t;
int fp_parse_signature(const fp_pdap_t *p, fp_signature_t *out);

/* EXECUTE reply (cmd 6, plen 5): [u32 time/1000][status:u8] -> returns status (0 ok, 1 list empty, 2 busy), -1 if malformed */
int fp_parse_execute_reply(const fp_pdap_t *p, uint32_t *time_field);

/* GENERIC_SCRIPT reply (cmd 5, plen >=1): [status:u8][count:u32]; returns status or -1 */
int fp_parse_script_reply(const fp_pdap_t *p, uint32_t *count);

/* 0x30000 image_xfer_status query. Returns 0 and fills fields; returns 1 if the payload is the DEADBEEF
 * "slot freed" marker (caller must ignore), -1 if malformed. */
int fp_parse_xfer_query(const fp_pdap_t *p, uint16_t *image_id, uint16_t *script_id);

/* 0x41 reply: [len=count*4+4][ids...] -> number of images (0 if none) or -1 */
int fp_parse_retrieval_request_reply(const fp_pdap_t *p, uint32_t *first_id);

/* 0x40000 state notification: [eventId:u32] */
int fp_parse_state_notify(const fp_pdap_t *p, uint32_t *event_id);

/* image datagram header: [imageId:u32][word:u32]; block = word & 0xFFFF; returns 0 if len == 4104 and block < 2048 */
int fp_parse_image_header(const uint8_t *dgram, size_t len, uint32_t *image_id, uint16_t *block);

/* ticks <-> ms helpers for the integration window */
uint32_t fp_ms_to_ticks(uint32_t ms);
uint32_t fp_ticks_to_ms(uint32_t ticks);

#ifdef __cplusplus
}
#endif
#endif /* FP_PROTO_H */
