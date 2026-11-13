/*
 * fp_session - the long-lived control session with the detector (udp :5550 <-> detector :8100).
 *
 *   RX task   : receives every control packet, bare-ACKs every detector data packet immediately (beacons included),
 *               de-duplicates by (SeqId, cmd) and queues the interesting messages for whoever drives the protocol.
 *   Manager   : link up -> SYSTEM_STARTUP until the first beacon -> PORT_SETUP -> SIGNATURE -> READY; watches the beacon
 *               and falls back to discovery when it stops.
 *
 * Set up ONCE (not per capture). The capture task only uses send/wait_ack/wait_msg.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"
#include "fp_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    DET_LINK_DOWN = 0,
    DET_DISCOVERING,
    DET_CONNECTING,
    DET_READY,
    DET_BUSY,
    DET_LOST,
    DET_ERROR,
} fp_det_state_t;

typedef struct {
    char ip[16];
    uint8_t mac[6];
    bool have_mac;
    bool sig_valid;
    fp_signature_t sig;
    uint32_t beacons;
    int64_t last_beacon_us;
    uint32_t reconnects;
} fp_det_info_t;

typedef struct {
    uint32_t cmd;
    uint32_t seq;
    uint16_t len;          /* payload bytes stored in data (<= sizeof data) */
    uint16_t declared;     /* payload length declared in the PDAP header */
    uint8_t data[120];
    int64_t t_us;          /* esp_timer time of arrival */
} fp_msg_t;

esp_err_t fp_session_start(const char *detector_ip, const char *host_ip);
const char *fp_session_state_name(fp_det_state_t s);
fp_det_state_t fp_session_state(void);
fp_det_info_t fp_session_info(void);
bool fp_session_ready(void);

void fp_session_link_changed(bool up);
void fp_session_reconnect(void);                 /* drop to discovery (a new SYSTEM_STARTUP resets the detector's SeqId tracking) */
void fp_session_set_busy(bool busy);             /* capture running: disables the beacon-loss watchdog, state shows BUSY */
void fp_session_set_detector_ip(const char *ip);

/* Send one PDAP message (without URP). Returns the SeqId used in *seq_out. Thread-safe. */
esp_err_t fp_session_send(const uint8_t *pdap, size_t len, uint32_t *seq_out);

/* Wait until a bare ACK (or an equivalent reply) with SeqId == seq arrives. */
bool fp_session_wait_ack(uint32_t seq, uint32_t timeout_ms);

/* Pop the next queued detector message. Returns false on timeout. */
bool fp_session_wait_msg(fp_msg_t *m, uint32_t timeout_ms);
void fp_session_flush_msgs(void);

/* diagnostics */
typedef struct { uint32_t rx_packets, rx_data, rx_acks, rx_dups, tx_packets, tx_acks, queue_drops; } fp_session_counters_t;
fp_session_counters_t fp_session_counters(void);

#ifdef __cplusplus
}
#endif
