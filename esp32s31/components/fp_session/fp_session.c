#include "fp_session.h"

#include <string.h>
#include <stdio.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

static const char *TAG = "fp_session";

#define EV_LINK_UP   (1u << 0)
#define EV_BEACON    (1u << 1)     /* first beacon seen during discovery */
#define EV_RECONNECT (1u << 2)
#define EV_ACK       (1u << 3)     /* some ACK arrived (waiters re-check the ring) */

#define ACK_RING     16
#define DEDUP_RING   24
#define MSG_QUEUE    32

static struct {
    int sock;
    struct sockaddr_in det;          /* detector :8100 */
    struct sockaddr_in bcast;        /* 192.168.1.255:8100 */
    uint8_t host_ip[4];
    char det_ip[16];

    SemaphoreHandle_t tx_lock;
    EventGroupHandle_t ev;
    QueueHandle_t q;

    volatile fp_det_state_t state;
    fp_det_info_t info;
    portMUX_TYPE info_lock;

    uint32_t seq;                    /* next host SeqId */
    uint32_t ack_ring[ACK_RING];
    int ack_head;
    struct { uint32_t seq, cmd; } dedup[DEDUP_RING];
    int dedup_head;

    volatile bool busy;
    volatile bool link_up;
    volatile uint32_t beacon_seq;
    fp_session_counters_t cnt;
    bool started;
} S = { .sock = -1, .info_lock = portMUX_INITIALIZER_UNLOCKED };

static const char *const STATE_NAMES[] = { "link_down", "discovering", "connecting", "ready", "busy", "lost", "error" };
const char *fp_session_state_name(fp_det_state_t s) { return (unsigned)s < 7 ? STATE_NAMES[s] : "?"; }

static void set_state(fp_det_state_t s)
{
    if (S.state != s) {
        ESP_LOGI(TAG, "detector state: %s -> %s", fp_session_state_name(S.state), fp_session_state_name(s));
        S.state = s;
    }
}

fp_det_state_t fp_session_state(void)
{
    fp_det_state_t s = S.state;
    if (s == DET_READY && S.busy) return DET_BUSY;
    return s;
}

bool fp_session_ready(void) { return S.state == DET_READY; }

fp_det_info_t fp_session_info(void)
{
    fp_det_info_t i;
    portENTER_CRITICAL(&S.info_lock);
    i = S.info;
    portEXIT_CRITICAL(&S.info_lock);
    return i;
}

fp_session_counters_t fp_session_counters(void) { return S.cnt; }

/* ------------------------------------------------------------------------------------------------
 * TX
 * ---------------------------------------------------------------------------------------------- */
static esp_err_t raw_send(const uint8_t *buf, size_t len, const struct sockaddr_in *to)
{
    int r = sendto(S.sock, buf, len, 0, (const struct sockaddr *)to, sizeof *to);
    if (r < 0) {
        ESP_LOGW(TAG, "sendto failed: errno %d", errno);
        return ESP_FAIL;
    }
    S.cnt.tx_packets++;
    return ESP_OK;
}

esp_err_t fp_session_send(const uint8_t *pdap, size_t len, uint32_t *seq_out)
{
    static uint8_t pkt[FP_MAX_PACKET];           /* shared, protected by tx_lock (keeps big buffers off the callers' stacks) */
    if (S.sock < 0 || len + FP_URP_LEN > sizeof pkt) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(S.tx_lock, portMAX_DELAY);
    uint32_t seq = S.seq++;
    size_t n = fp_build_urp(pkt, sizeof pkt, seq, FP_URP_FLAG_DATA, pdap, len);
    esp_err_t e = raw_send(pkt, n, &S.det);
    xSemaphoreGive(S.tx_lock);
    if (seq_out) *seq_out = seq;
    uint32_t cmd = len >= 4 ? fp_get_u32(pdap) : 0;
    ESP_LOGD(TAG, "TX cmd=0x%x seq=%u len=%u", (unsigned)cmd, (unsigned)seq, (unsigned)n);
    return e;
}

static void send_ack(uint32_t det_seq)
{
    uint8_t a[FP_URP_LEN];
    fp_build_ack(a, sizeof a, det_seq);
    if (raw_send(a, sizeof a, &S.det) == ESP_OK) S.cnt.tx_acks++;
}

static void send_startup(void)
{
    uint8_t pdap[32], pkt[48];
    size_t n = fp_build_system_startup(pdap, sizeof pdap, FP_HOST_CMD_PORT, S.host_ip);
    size_t m = fp_build_urp(pkt, sizeof pkt, 0, FP_URP_FLAG_DATA, pdap, n);   /* SYSTEM_STARTUP always SeqId 0 */
    xSemaphoreTake(S.tx_lock, portMAX_DELAY);
    raw_send(pkt, m, &S.bcast);
    raw_send(pkt, m, &S.det);
    xSemaphoreGive(S.tx_lock);
}

/* ------------------------------------------------------------------------------------------------
 * RX
 * ---------------------------------------------------------------------------------------------- */
static bool ack_seen(uint32_t seq)
{
    for (int i = 0; i < ACK_RING; i++) {
        if (S.ack_ring[i] == seq) return true;
    }
    return false;
}

bool fp_session_wait_ack(uint32_t seq, uint32_t timeout_ms)
{
    int64_t end = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    for (;;) {
        if (ack_seen(seq)) return true;
        int64_t left = end - esp_timer_get_time();
        if (left <= 0) return false;
        xEventGroupWaitBits(S.ev, EV_ACK, pdTRUE, pdFALSE, pdMS_TO_TICKS((left + 999) / 1000));
    }
}

bool fp_session_wait_msg(fp_msg_t *m, uint32_t timeout_ms)
{
    return xQueueReceive(S.q, m, pdMS_TO_TICKS(timeout_ms)) == pdTRUE;
}

void fp_session_flush_msgs(void)
{
    fp_msg_t tmp;
    while (xQueueReceive(S.q, &tmp, 0) == pdTRUE) { }
}

static bool dedup_seen(uint32_t seq, uint32_t cmd)
{
    for (int i = 0; i < DEDUP_RING; i++) {
        if (S.dedup[i].seq == seq && S.dedup[i].cmd == cmd && !(seq == 0 && cmd == 0)) return true;
    }
    S.dedup[S.dedup_head].seq = seq;
    S.dedup[S.dedup_head].cmd = cmd;
    S.dedup_head = (S.dedup_head + 1) % DEDUP_RING;
    return false;
}

static void handle_packet(const uint8_t *buf, int len, const struct sockaddr_in *from)
{
    S.cnt.rx_packets++;
    fp_urp_t u;
    if (fp_parse_urp(buf, (size_t)len, &u) != 0) return;

    if (u.flag != FP_URP_FLAG_DATA) {                 /* bare ACK from the detector for one of our commands */
        S.cnt.rx_acks++;
        S.ack_ring[S.ack_head] = u.seq;
        S.ack_head = (S.ack_head + 1) % ACK_RING;
        xEventGroupSetBits(S.ev, EV_ACK);
        return;
    }
    if (u.pdap_len < FP_PDAP_HDR_LEN) {               /* data packet without a body: ACK it anyway */
        send_ack(u.seq);
        return;
    }
    S.cnt.rx_data++;
    send_ack(u.seq);                                  /* ALWAYS ACK first, immediately - even duplicates */

    fp_pdap_t p;
    if (fp_parse_pdap(u.pdap, u.pdap_len, &p) != 0) return;
    if (dedup_seen(u.seq, p.cmd)) {
        S.cnt.rx_dups++;
        return;
    }

    if (p.cmd == FP_CMD_SYSTEM_STARTUP && p.payload_avail >= 10) {      /* beacon */
        uint8_t ip[4], mac[6];
        fp_parse_beacon(&p, ip, mac);
        portENTER_CRITICAL(&S.info_lock);
        S.info.beacons++;
        S.info.last_beacon_us = esp_timer_get_time();
        memcpy(S.info.mac, mac, 6);
        S.info.have_mac = true;
        portEXIT_CRITICAL(&S.info_lock);
        S.beacon_seq = u.seq;
        if (S.state == DET_DISCOVERING) {
            char ipstr[16];
            snprintf(ipstr, sizeof ipstr, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
            if (strcmp(ipstr, S.det_ip) != 0) {
                ESP_LOGW(TAG, "beacon from %s (configured %s) - using it", ipstr, S.det_ip);
                fp_session_set_detector_ip(ipstr);
            }
            xEventGroupSetBits(S.ev, EV_BEACON);
        }
        return;
    }

    fp_msg_t m;
    m.cmd = p.cmd;
    m.seq = u.seq;
    m.declared = (uint16_t)(p.payload_len > 0xFFFF ? 0xFFFF : p.payload_len);
    m.len = (uint16_t)(p.payload_avail > sizeof m.data ? sizeof m.data : p.payload_avail);
    memcpy(m.data, p.payload, m.len);
    m.t_us = esp_timer_get_time();
    if (xQueueSend(S.q, &m, 0) != pdTRUE) {
        S.cnt.queue_drops++;
    }
    (void)from;
}

static void rx_task(void *arg)
{
    (void)arg;
    static uint8_t buf[2048];
    for (;;) {
        struct sockaddr_in from;
        socklen_t fl = sizeof from;
        int n = recvfrom(S.sock, buf, sizeof buf, 0, (struct sockaddr *)&from, &fl);
        if (n <= 0) continue;                                 /* timeout (SO_RCVTIMEO) or error */
        if (from.sin_addr.s_addr != S.det.sin_addr.s_addr &&
            !(S.state == DET_DISCOVERING && n >= 26)) {       /* during discovery the beacon may reveal a different detector IP */
            continue;
        }
        handle_packet(buf, n, &from);
    }
}

/* ------------------------------------------------------------------------------------------------
 * manager
 * ---------------------------------------------------------------------------------------------- */
void fp_session_set_detector_ip(const char *ip)
{
    strlcpy(S.det_ip, ip, sizeof S.det_ip);
    inet_pton(AF_INET, ip, &S.det.sin_addr);
    portENTER_CRITICAL(&S.info_lock);
    strlcpy(S.info.ip, ip, sizeof S.info.ip);
    portEXIT_CRITICAL(&S.info_lock);
}

void fp_session_link_changed(bool up)
{
    S.link_up = up;
    if (!S.ev) {                      /* called before fp_session_start(): the state is picked up when the manager starts */
        return;
    }
    if (up) {
        xEventGroupSetBits(S.ev, EV_LINK_UP | EV_RECONNECT);
    } else {
        xEventGroupSetBits(S.ev, EV_RECONNECT);
    }
}

void fp_session_reconnect(void) { xEventGroupSetBits(S.ev, EV_RECONNECT); }
void fp_session_set_busy(bool busy) { S.busy = busy; if (!busy) { portENTER_CRITICAL(&S.info_lock); S.info.last_beacon_us = esp_timer_get_time(); portEXIT_CRITICAL(&S.info_lock); } }

/* PORT_SETUP + SIGNATURE. Returns true when the session is usable. */
static bool connect_sequence(void)
{
    uint8_t pdap[32];
    for (int attempt = 0; attempt < 3; attempt++) {
        size_t n = fp_build_port_setup(pdap, sizeof pdap, FP_HOST_CMD_PORT, FP_HOST_IMAGE_PORT);
        uint32_t seq;
        fp_session_send(pdap, n, &seq);
        ESP_LOGI(TAG, "PORT_SETUP (seq %u)", (unsigned)seq);
        if (fp_session_wait_ack(seq, 2000)) break;
        ESP_LOGW(TAG, "PORT_SETUP not acknowledged (attempt %d)", attempt + 1);
        if (attempt == 2) return false;
    }
    /* signature: informative only */
    fp_session_flush_msgs();
    size_t n = fp_build_signature_request(pdap, sizeof pdap);
    uint32_t seq;
    fp_session_send(pdap, n, &seq);
    int64_t end = esp_timer_get_time() + 3000000;
    fp_msg_t m;
    while (esp_timer_get_time() < end) {
        if (!fp_session_wait_msg(&m, 200)) continue;
        if (m.cmd == FP_CMD_SIGNATURE) {
            fp_pdap_t p = { .cmd = m.cmd, .payload_len = m.declared, .payload = m.data, .payload_avail = m.len };
            fp_signature_t sig;
            if (fp_parse_signature(&p, &sig) == 0) {
                portENTER_CRITICAL(&S.info_lock);
                S.info.sig = sig;
                S.info.sig_valid = true;
                portEXIT_CRITICAL(&S.info_lock);
                ESP_LOGI(TAG, "detector: MAC %02X:%02X:%02X:%02X:%02X:%02X serial '%s' model '%s' fw %u.%u.%u.%u.%u.%u.%u.%u", sig.mac[0], sig.mac[1],
                         sig.mac[2], sig.mac[3], sig.mac[4], sig.mac[5], sig.serial, sig.model, sig.fw[0], sig.fw[1], sig.fw[2], sig.fw[3], sig.fw[4],
                         sig.fw[5], sig.fw[6], sig.fw[7]);
            }
            break;
        }
    }
    return true;
}

static void manager_task(void *arg)
{
    (void)arg;
    int64_t last_startup = 0;
    for (;;) {
        EventBits_t ev = xEventGroupWaitBits(S.ev, EV_RECONNECT | EV_LINK_UP | EV_BEACON, pdFALSE, pdFALSE, pdMS_TO_TICKS(500));

        if (!S.link_up) {
            set_state(DET_LINK_DOWN);
            xEventGroupClearBits(S.ev, EV_RECONNECT | EV_BEACON);
            continue;
        }
        if (ev & EV_RECONNECT) {
            xEventGroupClearBits(S.ev, EV_RECONNECT | EV_BEACON);
            portENTER_CRITICAL(&S.info_lock);
            S.info.reconnects++;
            portEXIT_CRITICAL(&S.info_lock);
            S.busy = false;
            fp_session_flush_msgs();
            S.seq = 1;
            set_state(DET_DISCOVERING);
            last_startup = 0;
        }
        switch (S.state) {
        case DET_LINK_DOWN:
            set_state(DET_DISCOVERING);
            last_startup = 0;
            break;
        case DET_DISCOVERING:
        case DET_LOST:
        case DET_ERROR:
            if (S.state != DET_DISCOVERING) set_state(DET_DISCOVERING);
            if (esp_timer_get_time() - last_startup >= 2000000) {           /* BroadcastInterval = 2 s */
                send_startup();
                last_startup = esp_timer_get_time();
                ESP_LOGD(TAG, "SYSTEM_STARTUP sent");
            }
            if (xEventGroupGetBits(S.ev) & EV_BEACON) {
                xEventGroupClearBits(S.ev, EV_BEACON);
                /* SeqId sync: host seq = beacon seq + 1 (only if larger), as flashpad_acquire.discover() */
                uint32_t want = S.beacon_seq + 1;
                if (want > S.seq) S.seq = want;
                set_state(DET_CONNECTING);
            }
            break;
        case DET_CONNECTING:
            if (connect_sequence()) {
                portENTER_CRITICAL(&S.info_lock);
                S.info.last_beacon_us = esp_timer_get_time();
                portEXIT_CRITICAL(&S.info_lock);
                set_state(DET_READY);
            } else {
                ESP_LOGW(TAG, "connect sequence failed, back to discovery");
                set_state(DET_DISCOVERING);
                last_startup = 0;
            }
            break;
        case DET_READY:
            if (!S.busy) {
                int64_t since = esp_timer_get_time() - S.info.last_beacon_us;
                if (since > 15 * 1000000LL) {
                    ESP_LOGW(TAG, "no beacon for %d s - detector lost", (int)(since / 1000000));
                    set_state(DET_LOST);
                }
            }
            break;
        default:
            break;
        }
    }
}

esp_err_t fp_session_start(const char *detector_ip, const char *host_ip)
{
    if (S.started) return ESP_OK;
    S.tx_lock = xSemaphoreCreateMutex();
    S.ev = xEventGroupCreate();
    S.q = xQueueCreate(MSG_QUEUE, sizeof(fp_msg_t));
    S.seq = 1;
    memset(&S.det, 0, sizeof S.det);
    S.det.sin_family = AF_INET;
    S.det.sin_port = htons(FP_DETECTOR_PORT);
    fp_session_set_detector_ip(detector_ip);
    S.bcast = S.det;
    inet_pton(AF_INET, "192.168.1.255", &S.bcast.sin_addr);
    struct in_addr h;
    inet_pton(AF_INET, host_ip, &h);
    memcpy(S.host_ip, &h, 4);

    S.sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (S.sock < 0) return ESP_FAIL;
    int one = 1;
    setsockopt(S.sock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    setsockopt(S.sock, SOL_SOCKET, SO_BROADCAST, &one, sizeof one);
    struct timeval tv = { .tv_sec = 0, .tv_usec = 200000 };
    setsockopt(S.sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    struct sockaddr_in me = { .sin_family = AF_INET, .sin_port = htons(FP_HOST_CMD_PORT), .sin_addr.s_addr = htonl(INADDR_ANY) };
    if (bind(S.sock, (struct sockaddr *)&me, sizeof me) != 0) {
        ESP_LOGE(TAG, "bind :%u failed (errno %d)", (unsigned)FP_HOST_CMD_PORT, errno);
        return ESP_FAIL;
    }
    S.started = true;
    set_state(DET_LINK_DOWN);
    xTaskCreate(rx_task, "fp_rx", 6144, NULL, 14, NULL);
    xTaskCreate(manager_task, "fp_mgr", 6144, NULL, 6, NULL);
    ESP_LOGI(TAG, "session started: detector %s:%u, control socket :%u", detector_ip, (unsigned)FP_DETECTOR_PORT, (unsigned)FP_HOST_CMD_PORT);
    return ESP_OK;
}
