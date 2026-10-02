#!/usr/bin/env python3
"""
GE FlashPad Apollo -- URP/PDAP Acquisition Script
Detector: GE Optima XR200/220 AMX FlashPad, firmware 1.6.0.4.2.0.1.3
Protocol: URP (Unified Registration Protocol) + PDAP (Proprietary Detector Access Protocol)
Detector IP: 192.168.1.30, port 8100 (detector listens for all commands here)
Host listens on: 5550 (set via PORT_SETUP)

Protocol determined by reverse engineering the detector's own behaviour on the wire
"""

import socket
import struct
import time
import sys
import os
from datetime import datetime

# -- Network configuration -----------------------------------------------------
# Default network parameters used by the detector
DETECTOR_IP        = "192.168.1.30"   # DetectorIP.Val
HOST_IP            = "192.168.1.1"    # Detector_SysIP.Val
BROADCAST_ADDR     = "192.168.1.255"  # BroadcastAddress.Val
BROADCAST_PORT     = 8100             # BroadcastPort.Val -- detector listens for ALL commands
HOST_DISC_PORT     = 4500             # SystemPort.Val    -- sent in SYSTEM_STARTUP; detector sends beacons here
HOST_CMD_PORT      = 5550             # Detector_HostPort.Val -- detector sends protocol replies here after PORT_SETUP
HOST_IMAGE_PORT    = 6660             # Detector_ImagePort.Val -- detector sends pixel data here
BROADCAST_INTERVAL = 2.0              # BroadcastInterval.Val
BROADCAST_TIMEOUT  = 50.0             # BroadcastTimeout.Val
# NOTE: 48879 (0xBEEF) was invented in early sessions and is not a GE port.
HOST_REPLY_PORT    = HOST_CMD_PORT    # alias -- keep for backward compat; resolves to 5550

# -- PDAP cmd_type constants ---------------------------------------------------
CMD_SYSTEM_STARTUP           = 0x00000001
CMD_PORT_SETUP               = 0x00000002
CMD_SIGNATURE_REQUEST        = 0x00000003
CMD_GENERIC_SCRIPT           = 0x00000005   # download reply also uses this type
CMD_EXECUTE_SCRIPT           = 0x00000006   # host sends; detector replies with same type
CMD_EXECUTE_SCRIPT_STATUS    = 0x00000007   # intermediate status during execution
CMD_EXECUTION_COMPLETE       = 0x00010000   # EXECUTION_COMPLETE
CMD_DETECTOR_CMD_REPLY       = 0x00020000   # detector command reply
CMD_IMAGE_XFER_STATUS_QUERY  = 0x00030000   # image transfer status query
CMD_IMAGE_XFER_STATUS_REPLY  = 0x00000099
# cmd_type=0x40000 observed 1s after EXECUTE_SCRIPT, payload=17 (4 bytes LE).
# Not part of the documented command set.
# Believed to be a detector-firmware state notification: payload = state_id.
# state_id=17=0x11 likely means "detector ready / waiting for X-ray exposure".
CMD_DETECTOR_STATE_NOTIFY    = 0x00040000
# Image retrieval commands:
#   IMAGE_RETRIVAL_REQUEST(scriptId):  cmd_type=0x41, plen=4, payload=scriptId:4LE
#     Detector replies with same cmd_type=0x41 confirming "No of Images to be retrieved = N"
#   IMAGE_RETRIVAL(hostPort, imagePort): cmd_type=0x98, plen=4
#     payload=[imagePort:2LE][hostPort:2LE]  (CONCAT22 stores param_3 at low address)
#     Tells detector to stream image pixel data to host:imagePort
CMD_IMAGE_RETRIVAL_REQUEST   = 0x00000041   # host sends scriptId; detector confirms image count
CMD_IMAGE_RETRIVAL           = 0x00000098   # host sends [imagePort:2LE][hostPort:2LE]

# -- Data UPLOAD (read FROM detector) -- non-destructive ----------------------
# Command set:
#   CONFIGURE_UPLOAD: cmd_type=0x13, plen=4, payload=[uploadId:4LE]
#     reply CONFIGURE_UPLOAD reply: cmd_type=0x13, plen=5,
#           payload=[status:1][totalSize:4LE]   -- detector reports how many bytes it will upload
#   UPLOAD_BUFFER: cmd_type=0x14, plen=8, payload=[bufId:4LE][numBytes:4LE]
#     reply UPLOAD_BUFFER reply: cmd_type=0x14, payload=[bufId:4LE][numBytes:4LE][data]
#   END_OF_UPLOAD: empty command (len=0)
# Data-category upload IDs (EthernetDetectionDevice.cpp ~0x1115 / startFWDownload dispatch):
#   0x71 = HostList (the host list blob -- the REGISTERED-HOST list / pairing state)
#   0x72 = DetectorInfo (the detector info blob -- serial/model/fw/MAC)   0x73 = cal results (likely)
# This is the host's read-back path (the detector's data read-back path). We use it to dump the
# detector's stored registration state with ZERO writes, before considering any 0x71 flash write.
CMD_CONFIGURE_UPLOAD         = 0x00000013
CMD_UPLOAD_BUFFER            = 0x00000014
# DETECTOR_SET_CC (cmd 0x30) -- "Set Connection Context". The real GE host sends this at connect
# (EthernetDetectionDevice, via _sendDetectorSetCC -> SET_CC), payload
# = a 64-byte DetectorConnectionContext = DetectorDeviceId(16)+ConnectionSecretKey(16)+DetectorName(16)
# +DetectorCode(16) -- the SAME 64-byte header as the HostList (upload 0x71). The detector replies
# cmd 0x30 (SET_CC reply). flashpad_acquire.py historically NEVER sent this; it is a
# runtime arming handshake distinct from the flash HostList write. Suspected image-transfer gate.
CMD_DETECTOR_SET_CC          = 0x00000030
UPLOAD_ID_HOSTLIST           = 0x71
UPLOAD_ID_DETECTORINFO       = 0x72
UPLOAD_ID_CALRESULTS         = 0x73

# Number of image-buffer frames per full image.
# The detector reports its transfer buffer count in the status query.
# The detector splits one 2048x2048x16-bit image into this many cmd_type=0x0F IMAGE_BUFFER frames
# and streams them to the image port. This is also the "8" the detector reports in the 0x41 reply
# ("No of Images to be retrieved = 8") -- it is the normal/expected count, NOT an error.
ETHERNET_NO_OF_IMAGES        = 8

# In the PREVIEW transfer path (acquisition transfer_mode=2) the detector advertises imageId with
# the high bit set (0x8000) and reports a 4-buffer image in its 0x41 reply -- a smaller preview
# image, NOT the full 8-buffer frame.  Requesting 8 buffers [0..7] for a 4-buffer preview asks for
# frames that don't exist in that set.  Used to size the 0x30000 missed-buffer reply correctly.
PREVIEW_NO_OF_IMAGES         = 4
PREVIEW_IMAGE_FLAG           = 0x8000   # imageId high bit => preview image

# Sensor-read commands (wire format from the command builder + the sensor read path in
# (sensorId selects the sensor; the reply carries the raw ADC count).
#   raw sensor read:          cmd_type=0x7900, plen=4, payload=[selector:4LE]  (raw)
#   converted sensor read: cmd_type=0x7902  (engineering units; supported on this firmware)
#   detailed sensor read:      cmd_type=0x7904  (Realtek radio board / detailed)
# Reply: same cmd_type, payload=[value:4LE].
CMD_READ_SENSOR              = 0x00007900
CMD_READ_SENSOR_CONVERTED    = 0x00007902
CMD_DETAILED_SENSOR          = 0x00007904
# Accelerometer / vibration-sensor axis selectors (0x7900 with 0x42=X, 0x43=Y, 0x44=Z).
SENSOR_ACCEL_X = 0x42
SENSOR_ACCEL_Y = 0x43
SENSOR_ACCEL_Z = 0x44
# Temperature sensor IDs (DeviceConfig DEM: SurfaceTemp_ID=336=0x150, PanelTemp_ID=352=0x160).
SENSOR_TEMP_SURFACE = 0x150
SENSOR_TEMP_PANEL   = 0x160
# Accelerometer linear conversion (DeviceConfig DEM: Avibration, Bvibration).
ACCEL_A = 0.15258
ACCEL_B = -312.50

# Full sensor map recovered from the detector's own [Sensor] table (backup upload-ID 0x07,
# "DetectorSerialNumber = UA45829-7", Rev 1.2). Format there: Name, Cmd, SensorId, EqNum, coeffs...
# Cmd is 30978 = 0x7902 (the CONVERTED read) for ALL of them -- i.e. the detector is expected to do
# the unit conversion internally; the host-side coefficients were blank for this firmware rev.
# (name, sensorId). Read via cmd 0x7902; reply payload = [value:4LE].
SENSOR_TABLE = [
    ("Temp_Surface",        336),   # 0x150
    ("Temp_Panel",          352),   # 0x160
    ("DCIN_RAW",             10),
    ("LCORE_UNREG",          11),
    ("LPANA_UNREG",          12),
    ("LNANA_UNREG",          13),
    ("SCAN_VCC",             14),
    ("P5V_REF",              16),
    ("V_ON",                 17),
    ("V_OFF",                18),
    ("V_COMMON",             19),
    ("PARCVA_U",             21),
    ("NARCVA_U",             22),
    ("P5VA_SW",              25),
    ("N5VA_SW",              26),
    ("PRAIL_SW",             27),
    ("NRAIL_SW",             28),
    ("3V3",                  29),
    ("FGATE_NVC_L",          34),
    ("FGATE_PVC_L",          36),
    ("VCC_UNREG",            37),
    ("PARCPREG",             38),
    ("NARCPREG",             39),
    ("PANA_UNREG",           45),
    ("NANA_UNREG",           46),
    ("Accelerator",          70),
    ("Gravity",              78),
    ("Battery_Status",      256),
    ("Battery_Name_Report", 257),
    ("Battery_Life",        258),
    ("Battery_Capacity",    259),
    ("Grid_Status",         272),
]

# -- URP flags -----------------------------------------------------------------
URP_FLAG_COMMAND = 0   # host->detector
URP_FLAG_ACK     = 1   # detector->host

# -- Inner command DETECTORCODE values -----------------------------------------
DC_ACQUISITION   = 1
DC_ROE_COMMAND   = 2
DC_SEND_EVENT    = 3
DC_WAIT_EVENT    = 4
DC_DELAY         = 5

# -- ROE register addresses ----------------------------------------------
ROE_SCAN_SETUP_CMD   = 0x00004050   # ScanSetUpCommand
ROE_SCAN_SETUP_VAL   = 0x00D20000   # ScanSetUpValue
ROE_INIT_CMD2        = 0x00004002   # second ROE init step
ROE_INIT_CMD3        = 0x00007900   # third ROE init step
ROE_STANDBY_CMD      = 0x00004080   # standby loop


# -- Low-level packet builders -------------------------------------------------

def make_urp_packet(pdap_data: bytes, reply_port: int = 0,
                    flag: int = URP_FLAG_COMMAND) -> bytes:
    """Wrap PDAP data in a URP header.

    WARNING: the parameter names are historically backwards relative to URP semantics:
      flag       → URP bytes 0-3  = SeqId   (not a flag -- this is the sequence counter)
      reply_port → URP bytes 4-7  = CmdFlag (not a port -- 0=data packet, 1=bare ACK)

    Correct usage for a host command:
      make_urp_packet(pdap, flag=self._seq_id, reply_port=0)
        flag=SeqId  (increment per command; detector drops old SeqIds)
        reply_port=0 (CmdFlag=0 → data packet → detector parses PDAP body)

    NEVER pass a non-zero reply_port:
      A non-zero value goes into CmdFlag → detector treats packet as bare ACK → PDAP is NOT parsed.
    (Historical bug: early scripts passed reply_port=1 or reply_port=48879 causing this exact failure.)
    """
    return struct.pack("<II", flag, reply_port) + pdap_data


def make_pdap(cmd_type: int, payload: bytes = b"") -> bytes:
    """Build PDAP message: [cmd_type:4LE][payload_len:4LE][payload]"""
    return struct.pack("<II", cmd_type, len(payload)) + payload


def parse_pdap(data: bytes) -> tuple:
    """Parse PDAP from raw bytes (after URP header stripped).
    Returns (cmd_type, payload_len, payload) or raises ValueError."""
    if len(data) < 8:
        raise ValueError(f"PDAP too short: {len(data)} bytes")
    cmd_type, payload_len = struct.unpack_from("<II", data, 0)
    payload = data[8:8 + payload_len]
    return cmd_type, payload_len, payload


def parse_urp(data: bytes) -> tuple:
    """Parse URP packet. Returns (seq_id, cmd_flag, pdap_data).

    URP wire format: [SeqId:4LE][CmdFlag:4LE][PDAP...]
      SeqId   -- sequence counter of the sender (increments per data packet)
      CmdFlag -- 0 = data packet (PDAP body follows); non-zero = bare ACK (no body)
    """
    if len(data) < 8:
        raise ValueError(f"URP too short: {len(data)} bytes")
    seq_id, cmd_flag = struct.unpack_from("<II", data, 0)
    return seq_id, cmd_flag, data[8:]


# -- PDAP command builders -----------------------------------------------------

def build_system_startup(host_ip: str = HOST_IP,
                          disc_port: int = HOST_DISC_PORT) -> bytes:
    """SYSTEM_STARTUP -- cmd_type=1, payload=[SystemPort:2BE][host_ip:4BE]
    Tells the detector: "I am at host_ip; send your periodic discovery beacons to disc_port."
    disc_port = the port the detector sends its beacons to (4500).
    Payload carries the host's reply port and IP address.
    Port is encoded big-endian (htons / network byte order).
    Must be the FIRST packet sent; detector ACKs it with a bare URP ACK then starts beaconing.
    """
    ip_bytes = socket.inet_aton(host_ip)
    payload  = struct.pack(">H", disc_port) + ip_bytes   # port big-endian (htons)
    return make_pdap(CMD_SYSTEM_STARTUP, payload)


def build_port_setup(host_cmd_port: int = HOST_CMD_PORT,
                     host_img_port: int = HOST_IMAGE_PORT) -> bytes:
    """PORT_SETUP -- cmd_type=2, payload=[hostCmdPort:2BE][imagePort:2BE]

    Tells the detector two things:
      host_cmd_port:  port to send ALL subsequent protocol replies to (= Detector_HostPort = 5550)
      host_img_port:  port to stream image pixel data to             (= Detector_ImagePort = 6660)

    Both ports are host-chosen.  This command is REQUIRED to get image
    data flowing -- without it the detector does not configure its PurpEngine streaming engine
    and _ptrImageTransferElement on the host side is never initialised.

    Byte order: both shorts are big-endian (same as the port field in SYSTEM_STARTUP).
    The detector bare-ACKs PORT_SETUP; after that all replies arrive on host_cmd_port.
    """
    payload = struct.pack(">HH", host_cmd_port, host_img_port)
    return make_pdap(CMD_PORT_SETUP, payload)


def build_signature_request() -> bytes:
    """SIGNATURE_REQUEST -- cmd_type=3, no payload"""
    return make_pdap(CMD_SIGNATURE_REQUEST)


def build_execute_script() -> bytes:
    """EXECUTE_SCRIPT -- cmd_type=6, no payload"""
    return make_pdap(CMD_EXECUTE_SCRIPT)


def build_image_xfer_status_query(port1: int = HOST_CMD_PORT,
                                   port2: int = HOST_IMAGE_PORT) -> bytes:
    """IMAGE_XFER_STATUS_QUERY -- cmd_type=0x30000"""
    payload = struct.pack(">HH", port1, port2)
    return make_pdap(CMD_IMAGE_XFER_STATUS_QUERY, payload)


def build_image_xfer_status_reply(missed_ids: list = None,
                                   image_id: int = 0,
                                   script_id: int = 1,
                                   parallel: bool = False) -> bytes:
    """IMAGE_XFER_STATUS_REPLY -- sent by host in response to detector's cmd_type=0x30000.

    The detector's 0x30000 query is its "image transfer status query" carrying (imageId, scriptId).
    The host answers with the list of buffers it still needs.  the vendor protocol implementation chooses the reply
    format based on the host's `ParallelImageTransfer` config -- BUT here WE are the host, so we
    pick.  Wire formats:

      Serial   (ParallelImageTransfer=0): image transfer status reply
                 (3-arg, ~334817) -> cmd_type=9
                 payload = [numMissed:4LE][missedId_0:4LE]...
      Parallel (ParallelImageTransfer=1): image transfer status reply
                 (5-arg, ~334759) -> cmd_type=0x99
                 payload = [imageId:2LE][scriptId:2LE][numMissed:4LE][missedId_0:4LE]...
                 NOTE: the two leading shorts are imageId/scriptId echoed back from the query
                 (the caller passes local_18&0xffff=imageId, local_1c&0xffff=scriptId), NOT ports.
                 The previous version of this builder packed [hostPort][imgPort] here -- that was
                 wrong and is fixed.

    Pass missed_ids=[] (or None) when all images were received successfully (no retransmission).
    The serial 0x09 reply demonstrably did NOT start the stream in prior runs (detector just
    re-polls); the 0x99 parallel form is the only status-reply path tied to the image port, so
    --parallel exists to test whether the firmware gates the pixel push on receiving it.
    """
    ids = missed_ids if missed_ids else []
    n = len(ids)
    if parallel:
        # cmd_type=0x99: [imageId:2LE][scriptId:2LE][numMissed:4LE][id_0:4LE]...
        # IMPORTANT: do NOT use make_pdap here. The 5-arg image transfer status reply
        # writes the embedded length field as `count*4 + 0xc`, which
        # is the body length (count*4 + 8) PLUS 4 -- i.e. it counts from the length field itself,
        # off-by-4 vs the serial cmd9 builder. The detector firmware was built against this exact
        # creator, so we reproduce the byte layout precisely instead of the "natural" len(payload).
        body = struct.pack("<HHI", image_id & 0xFFFF, script_id & 0xFFFF, n)
        for mid in ids:
            body += struct.pack("<I", mid)
        length_field = n * 4 + 0xc
        return struct.pack("<II", CMD_IMAGE_XFER_STATUS_REPLY, length_field) + body
    else:
        # cmd_type=9: [numMissed:4LE][id_0:4LE]...
        payload = struct.pack("<I", n)
        for mid in ids:
            payload += struct.pack("<I", mid)
        return make_pdap(0x00000009, payload)


def build_image_retrival_request(script_id: int) -> bytes:
    """IMAGE_RETRIVAL_REQUEST -- cmd_type=0x41, plen=4, payload=scriptId (4 bytes LE).
    Wire format:
      local_10[1] = 0x41  (cmd_type)
      local_10[0] = 4     (plen)
      payload = scriptId  (4 bytes, passed by caller)
    Host sends this after receiving the 0x30000 image-count notification.
    Detector replies with the same cmd_type=0x41 logging "No of Images to be retrieved = N".
    """
    return make_pdap(CMD_IMAGE_RETRIVAL_REQUEST, struct.pack("<I", script_id))


def build_image_retrival(host_port: int = HOST_REPLY_PORT,
                          image_port: int = HOST_REPLY_PORT) -> bytes:
    """IMAGE_RETRIVAL -- cmd_type=0x98, plen=4, payload=[imagePort:2LE][hostPort:2LE].
    Wire format:
      local_10 = 0x98   (cmd_type)
      local_14 = 4      (plen)
      CONCAT22(param_2, param_3) stored as two 2-byte vars:
        local_c   (lower address, bytes 8-9 of msg)  = param_3 = image_port
        uStack_a  (higher address, bytes 10-11)      = param_2 = host_port
    i.e. payload wire order = [image_port:2LE][host_port:2LE]
    Tells detector: stream image pixel data to host:image_port.
    host_port  = port for protocol ACKs (defaults to HOST_CMD_PORT=5550)
    image_port = port for actual pixel data (defaults to HOST_IMAGE_PORT=6660)
    """
    return make_pdap(CMD_IMAGE_RETRIVAL, struct.pack("<HH", image_port, host_port))


def build_set_cc(cc64: bytes) -> bytes:
    """DETECTOR_SET_CC -- cmd 0x30, payload = 64-byte DetectorConnectionContext.
    From SET_CC: message = [cmd=0x30][len=0x40][cc:64], where cc =
    DetectorDeviceId(16)+ConnectionSecretKey(16)+DetectorName(16)+DetectorCode(16) (copied as 4x16).
    cc64 is taken verbatim from the first 64 bytes of the detector's HostList (upload 0x71)."""
    cc = (cc64 + b"\x00" * 64)[:64]
    return make_pdap(CMD_DETECTOR_SET_CC, cc)


def build_configure_upload(upload_id: int) -> bytes:
    """CONFIGURE_UPLOAD -- cmd_type=0x13, plen=4, payload=[uploadId:4LE].
    Asks the detector to stage a data category for upload (read-back). Detector replies cmd_type=0x13
    with [status:1][totalSize:4LE]. Non-destructive (read path)."""
    return make_pdap(CMD_CONFIGURE_UPLOAD, struct.pack("<I", upload_id))


def build_upload_buffer(buf_id: int, num_bytes: int) -> bytes:
    """UPLOAD_BUFFER -- cmd_type=0x14, plen=8, payload=[bufId:4LE][numBytes:4LE].
    Requests numBytes of the staged data. Detector replies cmd_type=0x14 with
    [bufId:4LE][numBytes:4LE][data]. Non-destructive (read path)."""
    return make_pdap(CMD_UPLOAD_BUFFER, struct.pack("<II", buf_id, num_bytes))


# -- Data DOWNLOAD (WRITE to detector) -- *** WRITES THE DETECTOR'S FLASH *** ---
# Data download (host -> detector) command set:
#   CONFIGURE_DOWNLOAD: cmd 0x0E, plen=8, payload=[downloadId:4LE][totalSize:4LE]
#     reply CONFIGURE_DOWNLOAD reply: cmd 0x0E, plen=1, [status:1]
#   DOWNLOAD_BUFFER: cmd 0x0F, payload=[bufId:4LE][numBytes:4LE][data]
#     reply DOWNLOAD_BUFFER reply: cmd 0x0F, plen=5, [status:1][_:4]
#   FLASH_FIRMWARE: cmd 0x10, EMPTY  -- *** the FLASH COMMIT / BURN ***
#     reply FLASH_FIRMWARE reply: cmd 0x10, plen=1, [status:1]
# downloadId 0x71 = HostList (config region @ flash 0x940000) -- NOT the firmware region.
CMD_CONFIGURE_DOWNLOAD       = 0x0000000E
CMD_DOWNLOAD_BUFFER          = 0x0000000F
CMD_FLASH_COMMIT             = 0x00000010
DOWNLOAD_ID_HOSTLIST         = 0x71


def build_configure_download(download_id: int, total_size: int) -> bytes:
    """CONFIGURE_DOWNLOAD -- cmd 0x0E, [downloadId:4LE][totalSize:4LE]."""
    return make_pdap(CMD_CONFIGURE_DOWNLOAD, struct.pack("<II", download_id, total_size))


def build_download_buffer(buf_id: int, data: bytes) -> bytes:
    """DOWNLOAD_BUFFER -- cmd 0x0F, [bufId:4LE][numBytes:4LE][data]."""
    return make_pdap(CMD_DOWNLOAD_BUFFER, struct.pack("<II", buf_id, len(data)) + data)


def build_flash_commit() -> bytes:
    """FLASH_COMMIT (FLASH_FIRMWARE) -- cmd 0x10, empty payload. *** BURNS FLASH ***."""
    return make_pdap(CMD_FLASH_COMMIT, b"")


def hostlist_crc(data: bytes) -> int:
    """CRC used on the detector's HostList/data blobs. Reverse-engineered & verified against the
    the live HostList: poly=0x04C11DB7, init=0, MSB-first,
    augmented-message bit algorithm (data bit shifted into the LSB). To form the 4-byte trailer for a
    blob `d`: struct.pack('>I', hostlist_crc(d + b'\\x00\\x00\\x00\\x00')) -- stored BIG-ENDIAN.
    Self-check: hostlist_crc(blob_including_trailer) == 0.  Verified: matches real trailer B8B86FC3."""
    crc = 0
    for byte in data:
        for bit in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01):
            msb = crc & 0x80000000
            crc = (crc << 1) & 0xFFFFFFFF
            if byte & bit:
                crc |= 1
            if msb:
                crc ^= 0x04C11DB7
    return crc


def hostlist_append_crc(body: bytes) -> bytes:
    """Return body + its correct 4-byte big-endian CRC trailer (for building a modified HostList)."""
    return body + struct.pack(">I", hostlist_crc(body + b"\x00\x00\x00\x00"))


# HostList structure constants (decoded from the live 504-byte read).
HL_HEADER_LEN   = 0x44          # DeviceId+Key+Name+Code + counts
HL_OFF_COUNT    = 0x40          # CurrentNumberOfHosts (u16 LE)
HL_OFF_PRIMARY  = 0x42          # IndexToPrimaryHost (u16 LE)
HL_ENTRY_LEN    = 144           # per host: HostId(16) + FieldA(64) + FieldB(64)


def hostid_from_mac(mac: str) -> str:
    """Replicate generateHostId.py: MAC -> strip ':' -> UPPER -> last4 + full (16 hex chars)."""
    h = mac.replace(":", "").replace("-", "").upper()
    return (h[-4:] + h)[:16]


def build_host_entry(hostid: str, name: str, location: str) -> bytes:
    """Build one 144-byte host entry: HostId(16) + FieldA(64,name) + FieldB(64,location)."""
    hid = hostid.encode("ascii")[:16].ljust(16, b"\x00")
    fa  = name.encode("ascii")[:64].ljust(64, b"\x00")
    fb  = location.encode("ascii")[:64].ljust(64, b"\x00")
    e = hid + fa + fb
    assert len(e) == HL_ENTRY_LEN, len(e)
    return e


def parse_hostlist(blob: bytes):
    """Return (header, [entries], count, primary) from a HostList blob (incl. trailing CRC)."""
    body = blob[:-4]
    count = struct.unpack_from("<H", body, HL_OFF_COUNT)[0]
    primary = struct.unpack_from("<H", body, HL_OFF_PRIMARY)[0]
    entries = []
    for i in range(count):
        off = HL_HEADER_LEN + i * HL_ENTRY_LEN
        entries.append(body[off:off + HL_ENTRY_LEN])
    return body[:HL_HEADER_LEN], entries, count, primary


def build_registered_hostlist(current: bytes, hostid: str, name: str, location: str) -> bytes:
    """Build a new HostList that APPENDS our host as a new entry and sets it as the primary host.
    Preserves all existing entries. Recomputes the CRC. Returns the full new blob (incl. CRC)."""
    header, entries, count, primary = parse_hostlist(current)
    our_index = count                       # appended at the end
    new_entries = entries + [build_host_entry(hostid, name, location)]
    new_header = bytearray(header)
    struct.pack_into("<H", new_header, HL_OFF_COUNT, count + 1)
    struct.pack_into("<H", new_header, HL_OFF_PRIMARY, our_index)
    body = bytes(new_header) + b"".join(new_entries)
    return hostlist_append_crc(body)


# -- Inner command (packed struct) builders ------------------------------------

def pack_roe_command(roe_cmd: int, roe_data: int,
                     response_flag: int = 0,
                     timer_value: int = 1000000) -> bytes:
    """PackedROECommand -- 14 bytes, DETECTORCODE=2
    Layout: [DC=2:1][responseFlag:1][timerValue:4LE][roeCmd:4LE][roeData:4LE]
    """
    return struct.pack("<BBIII", DC_ROE_COMMAND, response_flag, timer_value, roe_cmd, roe_data)


def pack_acquisition(type_mode: int, image_id: int = 1,
                     no_scrubs: int = 0, scrub_duration: int = 50000,
                     max_expose_time: int = 4000000, tail_time: int = 250000,
                     transfer_mode: int = 0) -> bytes:
    """PackedAcquisitionScript -- 17 bytes, DETECTORCODE=1
    Layout: [DC=1:1][typeMode:1][imageId:1][noScrubs:1][scrubDur:4LE]
            [maxExpose:4LE][tailTime:4LE][transferMode:1]
    """
    return struct.pack("<BBBBIIIB", DC_ACQUISITION, type_mode, image_id, no_scrubs,
                       scrub_duration, max_expose_time, tail_time, transfer_mode)


def pack_send_host_event(event_id: int) -> bytes:
    """PackedSendHostEvent -- 5 bytes, DETECTORCODE=3
    Layout: [DC=3:1][eventId:4LE]
    """
    return struct.pack("<BI", DC_SEND_EVENT, event_id)


def pack_wait_for_host_event(event_id: int, timeout_us: int = 0) -> bytes:
    """PackedWaitForHostEvent -- 9 bytes, DETECTORCODE=4
    Layout: [DC=4:1][eventId:4LE][timeout:4LE]
    """
    return struct.pack("<BII", DC_WAIT_EVENT, event_id, timeout_us)


def pack_delay(delay_us: int) -> bytes:
    """PackedDelay -- 5 bytes, DETECTORCODE=5
    Layout: [DC=5:1][delayUs:4LE]
    """
    return struct.pack("<BI", DC_DELAY, delay_us)


# -- GENERIC_SCRIPT builder ----------------------------------------------------

def build_generic_script(script_id: int, repeat_count: int, repeat_event: int,
                          commands: list) -> bytes:
    """Build GENERIC_SCRIPT PDAP packet (cmd_type=5).

    Wire format:
      [05 00 00 00]            cmd_type=5
      [payload_len:4LE]        = 8 + sum(cmd_sizes) + 2
      [scriptID:2LE]           script identifier
      [repeatCount:2LE]        0=once, 65535=infinite loop
      [repeatEvent:4LE]        event ID to break infinite loop (or 0)
      [packed_cmd_1...]        concatenated inner command structs
      [00 00]                  2-byte terminator
    """
    cmds_bytes = b"".join(commands)
    header = struct.pack("<HHI", script_id, repeat_count, repeat_event)
    terminator = b"\x00\x00"
    payload = header + cmds_bytes + terminator
    return make_pdap(CMD_GENERIC_SCRIPT, payload)


# -- Pre-built acquisition scripts (single-energy, 2048x2048) ------------------------

def build_script_7_roe_init(scan_cmd: int = ROE_SCAN_SETUP_CMD,
                              scan_val: int = ROE_SCAN_SETUP_VAL) -> bytes:
    """Script 7 -- ROE Initialization (DETECTOR_SCRIPT_ID=11 in XML).
    scriptID=7, repeatCount=0, repeatEvent=0
    13 commands: init + 4x zero-pulse + SendHostEvent(17)
    """
    cmds = [
        pack_roe_command(scan_cmd, scan_val),          # $1, $2
        pack_roe_command(ROE_INIT_CMD2, 2),             # 0x4002, 2
        pack_roe_command(ROE_INIT_CMD3, 16,
                         timer_value=0),                # 0x7900, 16 (timer=0)
        pack_delay(50000),
    ]
    for _ in range(4):
        cmds.append(pack_roe_command(0, 0))
        cmds.append(pack_delay(10000))
    cmds.append(pack_send_host_event(17))
    return build_generic_script(7, 0, 0, cmds)


def build_script_8_standby(roe_data: int = 0) -> bytes:
    """Script 8 -- Standby loop (DETECTOR_SCRIPT_ID=22 in XML).
    scriptID=8, repeatCount=65535 (infinite), repeatEvent=41
    2 commands: ROECmd(0x4080, $3) + Delay(10000)
    """
    cmds = [
        pack_roe_command(ROE_STANDBY_CMD, roe_data),
        pack_delay(10000),
    ]
    return build_generic_script(8, 65535, 41, cmds)


def build_script_0_std_acq(transfer_mode: int = 0) -> bytes:
    """Script 0 -- Standard Acquisition (DETECTOR_SCRIPT_ID=0 in XML).
    scriptID=0, repeatCount=0, repeatEvent=0
    2 commands: AcqScript(typeMode=0, ...) + Delay(10000)
    """
    cmds = [
        pack_acquisition(type_mode=0, image_id=1, no_scrubs=0,
                         scrub_duration=50000, max_expose_time=4000000,
                         tail_time=250000, transfer_mode=transfer_mode),
        pack_delay(10000),
    ]
    return build_generic_script(0, 0, 0, cmds)


def build_script_1_dark_acq(type_mode: int = 1, transfer_mode: int = 0) -> bytes:
    """Script 1 -- Dark/Offset Acquisition (DETECTOR_SCRIPT_ID=1 in XML).
    scriptID=1, repeatCount=0, repeatEvent=0
    10 commands: 4x[ROE(0,0)+Delay(10000)] + AcqScript(typeMode=1,...) + Delay(10000)
    type_mode/transfer_mode are exposed for the --sweep-acq experiment (defaults match the XML).
    """
    cmds = []
    for _ in range(4):
        cmds.append(pack_roe_command(0, 0))
        cmds.append(pack_delay(10000))
    cmds.append(
        pack_acquisition(type_mode=type_mode, image_id=1, no_scrubs=0,
                         scrub_duration=10000, max_expose_time=4000000,
                         tail_time=50000, transfer_mode=transfer_mode)
    )
    cmds.append(pack_delay(10000))
    return build_generic_script(1, 0, 0, cmds)


# -- FlashPad session ----------------------------------------------------------

class DetectorSignature:
    """Parsed SIGNATURE_REPLY payload (54 bytes, cmd_type=3).
    Payload offsets:
      [0..5]   MAC (6 bytes)
      [6..9]   field1 (uint32 LE)
      [10..13] field2 (uint32 LE)
      [14..17] field3 (uint32 LE)
      [18..21] field4 (uint32 LE)
      [22..33] serial string (12 bytes, space/null padded)
      [34..45] model string (12 bytes)
      [46..53] fw_version (8 bytes)
    Wire format from live capture: MAC=40:F4:A0:00:78:4D serial='UA45829-7' model='5340000-7'
    """
    def __init__(self, payload: bytes):
        if len(payload) < 54:
            raise ValueError(f"Signature reply too short: {len(payload)}")
        self.mac    = payload[0:6]
        self.field1 = struct.unpack_from("<I", payload, 6)[0]
        self.field2 = struct.unpack_from("<I", payload, 10)[0]
        self.field3 = struct.unpack_from("<I", payload, 14)[0]
        self.field4 = struct.unpack_from("<I", payload, 18)[0]
        self.serial = payload[22:34].strip(b"\x00 ").decode("ascii", errors="replace")
        self.model  = payload[34:46].strip(b"\x00 ").decode("ascii", errors="replace")
        self.fw_bytes = list(payload[46:54])

    def __str__(self):
        mac_str = ":".join(f"{b:02X}" for b in self.mac)
        fw_str  = ".".join(str(b) for b in self.fw_bytes)
        return (f"DetectorSignature: MAC={mac_str} serial='{self.serial}' "
                f"model='{self.model}' fw={fw_str}")


class FlashPadSession:
    """Manages URP/PDAP session with a GE FlashPad Apollo detector.

    Port architecture:
      - Host commands always go TO detector at 192.168.1.30:8100
      - SYSTEM_STARTUP payload tells detector to send discovery beacons to host:reply_port
      - PORT_SETUP (cmd_type=2) tells detector:
          hostCmdPort=5550  (Detector_HostPort)  -- all protocol replies come here
          hostImgPort=6660  (Detector_ImagePort) -- pixel data streams here
      - Image pixel data (cmd_type=0x0F) streams to port 6660 DURING script execution,
        BEFORE EXECUTION_COMPLETE.  Image socket MUST be open before execute_script().
      - After streaming, detector sends cmd_type=0x30000 with [imageId:2LE][scriptId:2LE].
        Host MUST reply with cmd_type=9 listing missed frame IDs (0 = all received OK).
    """

    def __init__(self,
                 detector_ip: str = DETECTOR_IP,
                 host_ip: str = HOST_IP,
                 broadcast_addr: str = BROADCAST_ADDR,
                 broadcast_port: int = BROADCAST_PORT,
                 reply_port: int = HOST_CMD_PORT,   # 5550: detector sends replies here
                 disc_port: int = HOST_DISC_PORT,   # 4500: detector sends beacons here
                 timeout: float = 5.0,
                 verbose: bool = True,
                 parallel: bool = False):
        self.detector_ip    = detector_ip
        self.host_ip        = host_ip
        self.broadcast_addr = broadcast_addr
        self.broadcast_port = broadcast_port
        self.reply_port     = reply_port   # 5550 -- detector sends protocol replies here (after PORT_SETUP)
        self.disc_port      = disc_port    # 4500 -- sent in SYSTEM_STARTUP payload; detector sends beacons here
        self.timeout        = timeout
        self.verbose        = verbose
        # parallel=True -> reply to detector's 0x30000 with the cmd_type=0x99 (ParallelImageTransfer)
        # status reply instead of the serial cmd_type=9.  This is the only status-reply path in
        # associated with the image-port transfer; testing whether the firmware
        # needs it to begin pushing 0x0F pixel frames.
        self.parallel       = parallel
        self._img_sock      = None   # separate UDP socket on HOST_IMAGE_PORT (6660) for pixel data
        self._sock          = None   # main socket bound to reply_port (5550) for all control traffic
        self.signature      = None
        # Transfer-state signals updated by receive_image() (read by --sweep-acq / --preview):
        #   _last_xfer_imageid = imageId from the detector's last 0x30000 query (0x8000 => preview)
        #   _last_xfer_count   = "No of Images to retrieve" from the last 0x41 reply (8 normal / 4 preview)
        self._last_xfer_imageid = None
        self._last_xfer_count   = None
        # SeqId counter: host must increment per command (URP bytes 0-3).
        # Detector firmware stores last-seen SeqId and silently drops any command with
        # SeqId <= last_seen (treats it as an old/duplicate packet).
        # The detector expects starting SeqId = beacon_SeqId + 1.
        # We start at 1 (SYSTEM_STARTUP uses 0 during discover(); commands start at 1).
        self._seq_id        = 1

    # -- socket management -----------------------------------------------------

    def _open_sockets(self):
        # Bind to reply_port (5550 = Detector_HostPort).  We also pass this same port in
        # SYSTEM_STARTUP so that discovery beacons AND all subsequent protocol replies arrive
        # here.  The GE config distinguishes SystemPort (4500) from Detector_HostPort (5550),
        # but for a single-socket implementation we unify onto reply_port and put that in
        # both places.  The detector sends to wherever we tell it; using one port is safe.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", self.reply_port))
        s.settimeout(self.timeout)
        self._sock = s
        self._log(f"Socket bound to :{self.reply_port} (Detector_HostPort / cmd reply port)")

    def _close_sockets(self):
        if self._sock:
            self._sock.close()
            self._sock = None
        self._close_image_socket()

    def _open_image_socket(self, image_port: int = HOST_IMAGE_PORT) -> bool:
        """Open a dedicated UDP socket on image_port to receive pixel data.
        Must be called BEFORE execute_script() because the detector streams pixel data
        DURING script execution (before EXECUTION_COMPLETE arrives).
        If image_port == reply_port the existing _sock is used instead (no 2nd socket).
        Returns True on success.
        """
        if image_port == self.reply_port:
            self._log(f"Image port = reply port ({image_port}), using shared socket")
            return True
        if self._img_sock is not None:
            return True   # already open
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)   # 8 MB kernel buf
            s.bind(("", image_port))
            s.settimeout(0.1)
            self._img_sock = s
            self._log(f"Image socket bound to :{image_port} (8 MB recv buf)")
            return True
        except OSError as e:
            self._log(f"[WARN] Cannot bind image socket :{image_port}: {e}")
            return False

    def _close_image_socket(self):
        if self._img_sock is not None:
            try:
                self._img_sock.close()
            except OSError:
                pass
            self._img_sock = None

    # -- helpers ---------------------------------------------------------------

    def _log(self, msg: str):
        if self.verbose:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{ts}] {msg}", flush=True)

    def _send_cmd(self, pdap_data: bytes):
        """Wrap PDAP in URP and send to detector port 8100.

        URP header: [SeqId:4LE][CmdFlag:4LE]
          SeqId=self._seq_id   (incremented after each send -- detector drops old SeqIds)
          CmdFlag=0            (0 = data packet with PDAP; non-zero = bare ACK, no PDAP parsed)
        """
        seq = self._seq_id
        pkt = make_urp_packet(pdap_data, reply_port=0, flag=seq)   # flag=SeqId, reply_port=CmdFlag
        self._sock.sendto(pkt, (self.detector_ip, self.broadcast_port))
        self._seq_id += 1
        cmd_type = struct.unpack_from("<I", pdap_data, 0)[0] if len(pdap_data) >= 4 else 0
        self._log(f"TX  cmd_type=0x{cmd_type:08X} SeqId={seq} len={len(pkt)}")

    def _send_bare_ack(self, det_seq_id: int):
        """Send a bare URP ACK to the detector for a received data packet.

        Protocol: when the detector sends a data packet (CmdFlag=0), the host MUST reply
        with a bare ACK: [det_SeqId:4LE][1:4LE]  (CmdFlag=1, no PDAP body).
        Without this ACK the detector considers the packet undelivered and may not advance
        its own state machine (e.g. continue sending beacons, process commands).
        Sent to (detector_ip, broadcast_port=8100).
        """
        ack = struct.pack("<II", det_seq_id, 1)   # SeqId=det_seq_id, CmdFlag=1
        self._sock.sendto(ack, (self.detector_ip, self.broadcast_port))
        self._log(f"TX  bare-ACK for det_SeqId={det_seq_id}")

    def _send_cmd_via(self, sock, pdap_data: bytes):
        """Like _send_cmd, but transmit from a specific socket (e.g. the image socket on 6660).
        The detector still receives on port 8100; only the SOURCE udp port differs.  Used to test
        whether the detector keys the image-stream destination off the source of the retrieval
        request (or needs the data path 'opened' by a packet from the host's image port).
        Falls back to the control socket if `sock` is None.
        """
        if sock is None:
            self._send_cmd(pdap_data)
            return
        seq = self._seq_id
        pkt = make_urp_packet(pdap_data, reply_port=0, flag=seq)   # flag=SeqId, CmdFlag=0
        sock.sendto(pkt, (self.detector_ip, self.broadcast_port))
        self._seq_id += 1
        cmd_type = struct.unpack_from("<I", pdap_data, 0)[0] if len(pdap_data) >= 4 else 0
        src_port = sock.getsockname()[1]
        self._log(f"TX  (from :{src_port}) cmd_type=0x{cmd_type:08X} SeqId={seq} len={len(pkt)}")

    def _recv_cmd(self, expected_cmd_type: int = None, timeout_s: float = None) -> tuple:
        """Read packets from the socket until we get expected_cmd_type (or any, if None).
        Filters to packets from detector_ip only.

        URP header: [SeqId:4LE][CmdFlag:4LE]
          CmdFlag=0 → data packet; host MUST reply with bare ACK [SeqId:4LE][1:4LE]
          CmdFlag≠0 → bare ACK from detector (acknowledging a host command); no PDAP body

        Bare ACKs from the detector are returned as (seq_id, None, None).
        De-duplicates retransmissions by comparing (cmd_type, payload) tuples.
        Returns (seq_id, cmd_type, payload) or raises TimeoutError.
        """
        if timeout_s is None:
            timeout_s = self.timeout
        deadline = time.time() + timeout_s
        seen = set()
        old_to = self._sock.gettimeout()
        self._sock.settimeout(0.5)
        try:
            while time.time() < deadline:
                try:
                    data, addr = self._sock.recvfrom(65535)
                except socket.timeout:
                    continue
                if addr[0] != self.detector_ip:
                    continue
                self._log(f"RX  {len(data)}b from {addr[0]}:{addr[1]}  raw={data.hex()}")
                if len(data) < 8:
                    continue
                seq_id, cmd_flag, pdap = parse_urp(data)   # seq_id=field0, cmd_flag=field1
                if cmd_flag != 0:
                    # Bare ACK from detector (cmd_flag ≠ 0): detector acknowledges our command.
                    # No PDAP body present. NOT a data packet -- do NOT send an ACK back.
                    self._log(f"    bare-ACK seq_id={seq_id} cmd_flag={cmd_flag}")
                    if expected_cmd_type is None:
                        return seq_id, None, None
                    # bare ACK is not the expected PDAP reply -- keep waiting
                    continue
                # cmd_flag=0 → data packet from detector; MUST send bare ACK
                if len(pdap) < 8:
                    # cmd_flag=0 but no PDAP body (shouldn't happen -- ACK it anyway)
                    self._log(f"    short data pkt seq_id={seq_id} (ACKing)")
                    self._send_bare_ack(seq_id)
                    continue
                cmd_type, plen, payload = parse_pdap(pdap)
                self._log(f"    PDAP cmd_type=0x{cmd_type:08X} plen={plen} seq_id={seq_id}")
                # Always ACK the detector's data packet immediately
                self._send_bare_ack(seq_id)
                # De-duplicate retransmissions (same cmd_type + payload seen before)
                dedup_key = (cmd_type, bytes(payload))
                if dedup_key in seen:
                    self._log("    (retransmit duplicate -- skipping)")
                    continue
                seen.add(dedup_key)
                if expected_cmd_type is None or cmd_type == expected_cmd_type:
                    return seq_id, cmd_type, payload
                self._log(f"    (skipping unexpected 0x{cmd_type:08X})")
        finally:
            self._sock.settimeout(old_to)
        exp_str = f"0x{expected_cmd_type:08X}" if expected_cmd_type is not None else "any"
        raise TimeoutError(f"Did not receive cmd_type={exp_str} within {timeout_s}s")

    # -- protocol steps --------------------------------------------------------

    def discover(self, max_wait: float = None) -> bool:
        """Send SYSTEM_STARTUP and wait for the detector to respond with a beacon.

        Observed flow:
          1. Host broadcasts SYSTEM_STARTUP: URP[SeqId=0, CmdFlag=0] + PDAP payload
               PDAP: [cmd_type=1:4LE][plen=6:4LE][SystemPort:2BE][host_ip:4]
             This tells the detector: "I am at host_ip, send periodic beacons to SystemPort."
          2. Detector sends bare ACK: [SeqId=0:4LE][CmdFlag=1:4LE] (8 bytes)
             NOTE: bare ACK only means the UDP packet was received.  It does NOT confirm
             the PDAP was processed.  CmdFlag=0 in SYSTEM_STARTUP is required for PDAP
             to be parsed; CmdFlag≠0 would make the detector treat it as a bare ACK itself
             and skip PDAP parsing entirely.
          3. Detector starts sending periodic beacons (UDP from detector:* → host:SystemPort).
             Beacons are URP data packets (CmdFlag=0) containing a 10-byte payload:
               [detector_ip:4][?:6]  (last 6 bytes may be MAC or SeqId info)
             Host MUST ACK each beacon with a bare ACK [beacon_SeqId:4LE][1:4LE].
          4. Host extracts beacon SeqId (= detector's outgoing counter) and sets its own
             starting SeqId = beacon_SeqId + 1 (from initializeSequenceID() in library).
             We already initialized self._seq_id=1 which works if the detector just booted.

        There is NO 4-byte null probe ("Phase-1/Phase-2 handshake") — that does not exist
        in the vendor implementation.  The probe was a false hypothesis from earlier sessions.

        URP header note (make_urp_packet parameter names are misleading):
          make_urp_packet(pdap, reply_port=CmdFlag_value, flag=SeqId_value)
          For SYSTEM_STARTUP: SeqId=0, CmdFlag=0 → make_urp_packet(pdap, reply_port=0, flag=0)

        Returns True once a beacon (or bare ACK) is received, False on timeout.
        """
        self._log(f"Discovery: sending SYSTEM_STARTUP -> {self.detector_ip}:{self.broadcast_port}")
        # We tell the detector to send beacons to self.reply_port (5550) because that is the
        # only socket we have open.  GE's config separates SystemPort(4500) from
        # Detector_HostPort(5550) but for a single-socket implementation they unify.
        startup_pdap = build_system_startup(self.host_ip, self.reply_port)
        # SeqId=0, CmdFlag=0 (data packet -- detector parses PDAP)
        # Note: make_urp_packet(flag=SeqId, reply_port=CmdFlag) -- names are inverted
        startup_pkt  = make_urp_packet(startup_pdap, reply_port=0, flag=0)

        deadline = time.time() + (max_wait if max_wait is not None else BROADCAST_TIMEOUT)
        old_to   = self._sock.gettimeout()
        self._sock.settimeout(0.3)
        attempt  = 0
        got_ack  = False
        try:
            while time.time() < deadline:
                # Broadcast SYSTEM_STARTUP every BROADCAST_INTERVAL seconds
                attempt += 1
                self._sock.sendto(startup_pkt, (self.broadcast_addr, self.broadcast_port))
                self._sock.sendto(startup_pkt, (self.detector_ip,    self.broadcast_port))
                self._log(f"TX  SYSTEM_STARTUP #{attempt} host={self.host_ip} disc_port={self.disc_port}")

                # Collect responses for up to BROADCAST_INTERVAL seconds
                interval_deadline = time.time() + BROADCAST_INTERVAL
                while time.time() < interval_deadline:
                    try:
                        data, addr = self._sock.recvfrom(65535)
                    except socket.timeout:
                        continue
                    if addr[0] != self.detector_ip:
                        continue
                    self._log(f"RX  {len(data)}b from {addr[0]}:{addr[1]}  raw={data.hex()}")
                    if len(data) < 8:
                        continue
                    seq_id, cmd_flag = struct.unpack_from("<II", data, 0)
                    if cmd_flag != 0:
                        # Bare ACK from detector -- SYSTEM_STARTUP packet arrived.
                        # (ACK only means UDP arrived; PDAP parsing requires CmdFlag=0 which we set.)
                        self._log(f"    bare-ACK seq_id={seq_id} -- SYSTEM_STARTUP delivered")
                        got_ack = True
                        # Don't return yet -- keep draining; detector may send a beacon next
                        continue
                    # CmdFlag=0 -- data packet from detector (periodic beacon or other PDAP)
                    # ACK it immediately so detector knows we're alive
                    self._send_bare_ack(seq_id)
                    if len(data) >= 16:
                        try:
                            _, _, pdap_body = parse_urp(data)
                            cmd_type, plen, payload = parse_pdap(pdap_body)
                            self._log(f"    beacon PDAP cmd_type=0x{cmd_type:08X} plen={plen} "
                                      f"payload={payload.hex()}")
                        except (ValueError, struct.error) as e:
                            self._log(f"    (parse error: {e})")
                    # Synchronize SeqId: library sets host SeqId = beacon_SeqId + 1.
                    # Only update if the new value would be higher (don't go backwards).
                    new_seq = seq_id + 1
                    if new_seq > self._seq_id:
                        self._log(f"    SeqId sync: {self._seq_id} -> {new_seq} "
                                  f"(detector beacon SeqId={seq_id})")
                        self._seq_id = new_seq
                    self._log("[OK] Discovery complete -- beacon received and ACKed")
                    return True

            if got_ack:
                # Got bare ACK but no beacon yet -- consider discovery partial success.
                # SYSTEM_STARTUP was delivered; beacon may come shortly after.
                self._log("[OK] Discovery: SYSTEM_STARTUP ACKed (no beacon yet -- proceeding)")
                return True

            self._log(f"[FAIL] Discovery timeout after {attempt} attempts")
            return False
        finally:
            self._sock.settimeout(old_to)

    def request_signature(self) -> "DetectorSignature":
        """Request detector signature. Returns DetectorSignature or None."""
        self._log("SIGNATURE_REQUEST")
        self._send_cmd(build_signature_request())
        try:
            seq_id, cmd_type, payload = self._recv_cmd(CMD_SIGNATURE_REQUEST,
                                                        timeout_s=self.timeout)
            sig = DetectorSignature(payload)
            self._log(f"[OK] {sig}")
            self.signature = sig
            return sig
        except (TimeoutError, ValueError) as e:
            self._log(f"[FAIL] Signature: {e}")
            return None

    def send_port_setup(self, host_cmd_port: int = HOST_CMD_PORT,
                        host_img_port: int = HOST_IMAGE_PORT) -> bool:
        """Send PORT_SETUP (cmd_type=2) to configure the detector's reply ports.

        REQUIRED before image transfer: sets the host ports the detector will use.
        host_cmd_port = 5550 (Detector_HostPort) -- all protocol replies come here.
        host_img_port = 6660 (Detector_ImagePort) -- pixel data streams here.

        The detector acknowledges PORT_SETUP with a bare URP ACK.
        Returns True on ACK, False on timeout.
        """
        self._log(f"PORT_SETUP: hostCmdPort={host_cmd_port} hostImgPort={host_img_port}")
        sent_seq = self._seq_id
        self._send_cmd(build_port_setup(host_cmd_port, host_img_port))
        deadline = time.time() + self.timeout
        try:
            while time.time() < deadline:
                remaining = deadline - time.time()
                seq_id, cmd_type, payload = self._recv_cmd(None, timeout_s=max(remaining, 0.1))
                if cmd_type is None:
                    if seq_id == sent_seq:
                        self._log(f"[OK] PORT_SETUP bare-ACKed (seq_id={seq_id})")
                        return True
                    self._log(f"    (stale bare-ACK seq_id={seq_id}, want {sent_seq} -- skip)")
                    continue
                if cmd_type == CMD_PORT_SETUP:
                    self._log(f"[OK] PORT_SETUP reply: payload={payload.hex() if payload else 'none'}")
                    return True
                self._log(f"    (skipping cmd_type=0x{cmd_type:08X} in send_port_setup)")
            raise TimeoutError(f"send_port_setup: no reply in {self.timeout}s")
        except TimeoutError as e:
            self._log(f"[FAIL] {e}")
            return False

    def reply_image_xfer_status(self, missed_ids: list = None,
                                image_id: int = 0, script_id: int = 1) -> None:
        """Reply to detector's cmd_type=0x30000 listing any missed frames.

        This MUST be sent whenever the detector sends cmd_type=0x30000 (image transfer status
        query).  The detector waits for this reply before considering the transfer done.

        Serial   (self.parallel=False): cmd_type=9,    payload=[numMissed:4LE][id_0:4LE]...
        Parallel (self.parallel=True):  cmd_type=0x99, payload=[imageId:2LE][scriptId:2LE]
                                                               [numMissed:4LE][id_0:4LE]...
        numMissed=0 means all image frames were received OK -- no retransmission needed.
        (imageId/scriptId are echoed from the detector's query, per image transfer status reply.)
        """
        ids = missed_ids if missed_ids else []
        self._send_cmd(build_image_xfer_status_reply(
            missed_ids=ids, image_id=image_id, script_id=script_id,
            parallel=self.parallel))

    def download_script(self, script_pdap: bytes, script_name: str = "?") -> bool:
        """Download a GENERIC_SCRIPT. Waits for bare ACK with matching SeqId, or PDAP reply.

        Bare ACK SeqId must match our command's SeqId (detector echoes host SeqId in ACK).
        Stale ACKs from previous commands (lower SeqId) are skipped.
        Other PDAP cmd_types (late replies to previous commands) are also skipped.
        """
        script_id = struct.unpack_from("<H", script_pdap, 8)[0] if len(script_pdap) > 10 else -1
        self._log(f"GENERIC_SCRIPT: {script_name} scriptID={script_id} len={len(script_pdap)}")
        sent_seq = self._seq_id                # capture before send (send increments it)
        self._send_cmd(script_pdap)
        deadline = time.time() + self.timeout
        try:
            while time.time() < deadline:
                remaining = deadline - time.time()
                seq_id, cmd_type, payload = self._recv_cmd(None, timeout_s=max(remaining, 0.1))
                if cmd_type is None:
                    # Bare ACK -- check it matches our sent SeqId
                    if seq_id == sent_seq:
                        self._log("[OK] Script bare-ACKed")
                        return True
                    self._log(f"    (stale bare-ACK seq_id={seq_id}, want {sent_seq} -- skip)")
                    continue
                if cmd_type == CMD_GENERIC_SCRIPT:
                    status = payload[0] if payload else 0
                    self._log(f"[OK] Script reply status={status}")
                    return status == 0
                # Some other PDAP (e.g. beacon 0x1, late reply 0x5) -- skip and keep waiting
                self._log(f"    (skipping stale cmd_type=0x{cmd_type:08X} in download_script)")
            raise TimeoutError(f"download_script: no reply in {self.timeout}s")
        except TimeoutError as e:
            self._log(f"[FAIL] {e}")
            return False

    def execute_script(self) -> bool:
        """Send EXECUTE_SCRIPT. Waits for bare ACK with matching SeqId, or EXECUTE_SCRIPT reply.

        Stale bare ACKs (wrong SeqId) and unrelated PDAP replies are skipped.
        """
        self._log("EXECUTE_SCRIPT")
        sent_seq = self._seq_id
        self._send_cmd(build_execute_script())
        deadline = time.time() + self.timeout
        try:
            while time.time() < deadline:
                remaining = deadline - time.time()
                seq_id, cmd_type, payload = self._recv_cmd(None, timeout_s=max(remaining, 0.1))
                if cmd_type is None:
                    if seq_id == sent_seq:
                        self._log("[OK] Execute bare-ACKed")
                        return True
                    self._log(f"    (stale bare-ACK seq_id={seq_id}, want {sent_seq} -- skip)")
                    continue
                if cmd_type == CMD_EXECUTE_SCRIPT:
                    status = payload[4] if payload and len(payload) >= 5 else 0
                    self._log(f"[OK] Execute reply status={status}")
                    return status == 0
                # Late/unrelated PDAP (e.g. 0x00000005 reply for Script0) -- skip
                self._log(f"    (skipping stale cmd_type=0x{cmd_type:08X} in execute_script)")
            raise TimeoutError(f"execute_script: no reply in {self.timeout}s")
        except TimeoutError as e:
            self._log(f"[FAIL] {e}")
            return False

    def wait_for_execution_complete(self, timeout_s: float = 30.0) -> bool:
        """Wait for EXECUTION_COMPLETE (cmd_type=0x10000).

        Known intermediate packets received before EXECUTION_COMPLETE:
          cmd_type=0x6  (EXECUTE_SCRIPT reply, ~0.2s after EXECUTE_SCRIPT)
          cmd_type=0x40000 (detector state notify, ~1s after; payload=state_id=17 = ready/wait)
          cmd_type=0x7  (EXECUTE_SCRIPT_STATUS, 16-byte status packet during execution)
        These are all skipped; only 0x10000 satisfies this wait.

        NOTE: For Script0 (standard acquisition), EXECUTION_COMPLETE will NOT arrive without
        an actual X-ray exposure. Use dark_only=True (Script1) for testing without X-ray.
        """
        self._log(f"Waiting for EXECUTION_COMPLETE (timeout={timeout_s}s)")
        deadline = time.time() + timeout_s
        try:
            while time.time() < deadline:
                remaining = deadline - time.time()
                seq_id, cmd_type, payload = self._recv_cmd(None, timeout_s=max(remaining, 0.1))
                if cmd_type is None:
                    # Bare ACK -- not execution complete; keep waiting
                    continue
                if cmd_type == CMD_EXECUTION_COMPLETE:
                    self._log(f"[OK] EXECUTION_COMPLETE payload={payload.hex() if payload else 'none'}")
                    return True
                # Log known intermediates clearly, unknown ones as warnings
                if cmd_type == CMD_EXECUTE_SCRIPT:
                    status_byte = payload[0] if payload else 0
                    self._log(f"    EXECUTE_SCRIPT reply status_byte=0x{status_byte:02X} "
                              f"(normal -- waiting for EXECUTION_COMPLETE)")
                elif cmd_type == CMD_EXECUTE_SCRIPT_STATUS:
                    self._log(f"    EXECUTE_SCRIPT_STATUS (script running...)")
                elif cmd_type == CMD_DETECTOR_STATE_NOTIFY:
                    state_id = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
                    self._log(f"    detector state notify: state_id=0x{state_id:02X} ({state_id})")
                elif cmd_type == CMD_GENERIC_SCRIPT:
                    self._log(f"    GENERIC_SCRIPT reply (late, from script download phase)")
                else:
                    self._log(f"    unknown cmd_type=0x{cmd_type:08X} payload="
                              f"{payload.hex() if payload else 'none'}")
            raise TimeoutError(f"EXECUTION_COMPLETE not received in {timeout_s}s")
        except TimeoutError:
            self._log("[FAIL] EXECUTION_COMPLETE not received")
            return False

    def request_image_transfer(self, script_id: int = 0, timeout_s: float = 15.0,
                                image_port: int = HOST_IMAGE_PORT) -> int:
        """After EXECUTION_COMPLETE: send image retrieval commands and return image count.

        Protocol:
          1. Detector sends cmd_type=0x30000 (spontaneously, ~0.7s after EXECUTION_COMPLETE)
               payload = [image_count:4LE]  -- number of images ready for transfer
             Host ACKs it (already done by _recv_cmd).
          2. Host sends cmd_type=0x41 (IMAGE_RETRIVAL_REQUEST), payload=[scriptId:4LE]
             Detector replies with same cmd_type=0x41 confirming image count.
          3. Host sends cmd_type=0x98 (IMAGE_RETRIVAL), payload=[imagePort:2LE][hostPort:2LE]
             Detector then streams image pixel data to host:image_port.

        image_port: UDP port the detector will send pixel data to (default=HOST_IMAGE_PORT=6660).
                    Use self.reply_port (5550) to receive all data on one socket instead.
        Returns the confirmed image count (from 0x30000 or 0x41 reply), or 0 on timeout.
        """
        self._log(f"IMAGE_TRANSFER: waiting for 0x30000 status notification (timeout={timeout_s}s)")
        image_count = 0
        deadline = time.time() + timeout_s
        old_to = self._sock.gettimeout()
        self._sock.settimeout(0.5)
        try:
            # Step 1: wait for detector's spontaneous 0x30000 notification
            while time.time() < deadline:
                remaining = deadline - time.time()
                try:
                    seq_id, cmd_type, payload = self._recv_cmd(
                        CMD_IMAGE_XFER_STATUS_QUERY, timeout_s=max(remaining, 0.1))
                    if len(payload) >= 4:
                        # payload = [imageCount:4LE] or possibly [short1:2LE][short2:2LE]
                        image_count = struct.unpack_from("<I", payload, 0)[0]
                    self._log(f"[OK] Image transfer status notification "
                              f"(payload={payload.hex()}, raw_count_field={image_count})")
                    break
                except TimeoutError:
                    self._log("[WARN] No 0x30000 image-count notification received -- "
                              "proceeding with script_id query anyway")
                    image_count = 1   # assume at least 1 image
                    break

            # Step 2: send IMAGE_RETRIVAL_REQUEST (cmd_type=0x41) with scriptId
            self._log(f"  Sending IMAGE_RETRIVAL_REQUEST (0x41) scriptId={script_id}")
            self._send_cmd(build_image_retrival_request(script_id))
            # Wait for 0x41 reply (detector confirms "No of Images to be retrieved = N")
            try:
                seq_id, cmd_type, payload = self._recv_cmd(
                    CMD_IMAGE_RETRIVAL_REQUEST, timeout_s=5.0)
                if len(payload) >= 4:
                    confirmed = struct.unpack_from("<I", payload, 0)[0]
                    self._log(f"[OK] IMAGE_RETRIVAL_REQUEST reply: {confirmed} images confirmed "
                              f"(full payload={payload.hex()})")
                    image_count = confirmed
                else:
                    self._log(f"[OK] IMAGE_RETRIVAL_REQUEST reply: short payload={payload.hex()}")
            except TimeoutError:
                self._log("[WARN] No 0x41 reply received -- proceeding with retrival command")

            # Step 3: send IMAGE_RETRIVAL (cmd_type=0x98) with port info.
            # Payload wire format: [imagePort:2LE][hostPort:2LE]
            # (from CONCAT22(param_2=hostPort, param_3=imagePort) → param_3 at lower address)
            # image_port: detector sends pixel data here (HOST_IMAGE_PORT=6660 by default)
            # host_port:  detector sends protocol ACKs here (HOST_CMD_PORT=5550)
            self._log(f"  Sending IMAGE_RETRIVAL (0x98) imagePort={image_port} "
                      f"hostPort={self.reply_port}")
            self._send_cmd(build_image_retrival(host_port=self.reply_port,
                                                 image_port=image_port))
            # ACK from detector (bare ACK for our 0x98 command)
            try:
                seq_id, cmd_type, payload = self._recv_cmd(None, timeout_s=3.0)
                if cmd_type is None:
                    self._log(f"[OK] IMAGE_RETRIVAL bare-ACKed (seq_id={seq_id})")
                else:
                    self._log(f"[OK] IMAGE_RETRIVAL response: cmd_type=0x{cmd_type:08X} "
                              f"payload={payload.hex() if payload else 'none'}")
            except TimeoutError:
                self._log("[WARN] No ACK for IMAGE_RETRIVAL -- detector may still send data")

            return image_count
        finally:
            self._sock.settimeout(old_to)

    def receive_image(self, output_path: str = None, timeout_s: float = 90.0,
                      script_id: int = 0,
                      image_port: int = HOST_IMAGE_PORT,
                      wait_exec_complete: bool = True) -> bytes:
        """Collect image pixel data that the detector streams DURING script execution.

        CRITICAL TIMING: The detector streams pixel data to image_port DURING execute_script(),
        BEFORE EXECUTION_COMPLETE arrives.  _open_image_socket() MUST be called before
        execute_script() so no pixel data is dropped.  This method uses self._img_sock
        (already open) rather than opening a new socket.

        Unified select loop on ctrl socket (5550) + image socket (6660):
          - ctrl: EXECUTION_COMPLETE, 0x30000 → reply cmd_type=9, other protocol
          - img:  raw pixel data from detector
          Exits after EXECUTION_COMPLETE + 0x30000 handled + SILENCE_S seconds silence on img.

        If self._img_sock was not pre-opened (socket opened AFTER execute_script), a warning
        is logged and a new socket is opened -- but early-arriving data will already be lost.

        wait_exec_complete=False: skip waiting for EXECUTION_COMPLETE (for testing/recovery).
        """
        import select as _select

        # Use pre-opened image socket if available.  Fall back to opening now (with warning).
        img_sock = self._img_sock
        if img_sock is None and image_port != self.reply_port:
            self._log("[WARN] Image socket not pre-opened -- opening now "
                      "(pixel data arriving DURING execute_script may already be lost!)")
            if self._open_image_socket(image_port):
                img_sock = self._img_sock
            else:
                self._log("[WARN] Falling back to shared ctrl socket for image data")
                image_port = self.reply_port

        self._log(f"Waiting for image data on :{image_port} (timeout={timeout_s}s, "
                  f"wait_exec={wait_exec_complete})")

        chunks      = []
        seen_keys   = set()
        ctrl_seen   = set()
        deadline    = time.time() + timeout_s
        last_img_t  = time.time()
        SILENCE_S   = 5.0
        # State flags
        exec_done     = not wait_exec_complete  # True once EXECUTION_COMPLETE received
        xfer_replied  = False                   # True once cmd_type=9 with numMissed=0 sent
        xfer_requested= False                   # True once we've requested missed buffers
        pull_done     = False                   # True once 0x41/0x98 retrieval trigger sent

        socks_to_watch = [s for s in [self._sock, img_sock] if s is not None]

        while time.time() < deadline:
            # Once EXECUTION_COMPLETE + 0x30000 reply done, exit on image silence.
            # (Silence timer also resets on EXECUTION_COMPLETE so we give a full window.)
            if exec_done and (xfer_replied or xfer_requested):
                if time.time() - last_img_t > SILENCE_S:
                    if chunks:
                        self._log(f"  ({SILENCE_S}s silence on image port -- receive done)")
                    else:
                        self._log(f"  ({SILENCE_S}s silence -- no image data received)")
                    break

            ready, _, _ = _select.select(socks_to_watch, [], [], 0.2)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                if addr[0] != self.detector_ip:
                    continue

                is_img = (img_sock is not None) and (sock is img_sock) and (img_sock is not self._sock)

                if is_img:
                    # ---- Image socket (6660): pixel data from detector ----
                    last_img_t = time.time()
                    if len(data) < 4:
                        continue
                    dedup_key = bytes(data)
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    # Check if pixel data is wrapped in URP/PDAP
                    if len(data) >= 16:
                        try:
                            seq_id, cmd_flag, pdap = parse_urp(data)
                            if cmd_flag == 0:
                                self._send_bare_ack(seq_id)
                                cmd_type, plen, payload = parse_pdap(pdap)
                                self._log(f"  ImgSock PDAP 0x{cmd_type:08X} "
                                          f"plen={plen} {len(payload)}b")
                                chunks.append(payload)
                                continue
                        except (ValueError, struct.error):
                            pass
                    # Plain raw pixel data (no URP/PDAP header)
                    chunks.append(data)
                    self._log(f"  ImgSock raw #{len(chunks)}: {len(data)}b "
                              f"from {addr[0]}:{addr[1]}")

                else:
                    # ---- Control socket (5550): protocol traffic ----
                    if len(data) < 8:
                        continue
                    seq_id, cmd_flag = struct.unpack_from("<II", data, 0)
                    if cmd_flag != 0:
                        # Bare ACK from detector (not a data packet)
                        self._log(f"  ctrl bare-ACK seq_id={seq_id}")
                        continue
                    # Data packet → ACK it
                    self._send_bare_ack(seq_id)
                    if len(data) < 16:
                        continue
                    try:
                        _, _, pdap = parse_urp(data)
                        cmd_type, plen, payload = parse_pdap(pdap)
                    except (ValueError, struct.error):
                        continue

                    dedup_key = (seq_id, cmd_type)
                    if dedup_key in ctrl_seen:
                        continue
                    ctrl_seen.add(dedup_key)

                    if cmd_type == CMD_EXECUTION_COMPLETE:
                        self._log(f"[OK] EXECUTION_COMPLETE "
                                  f"payload={payload.hex() if payload else 'none'}")
                        exec_done  = True
                        last_img_t = time.time()   # reset silence timer from this moment

                    elif cmd_type == CMD_IMAGE_XFER_STATUS_QUERY:
                        # 0x30000: detector's "image transfer status query".  Per
                        # the host status reply +
                        # the pending-buffer query in the vendor protocol implementation, the
                        # detector is ASKING which image-buffer frames the host still needs, and
                        # the host replies cmd_type=9 with the list of MISSED buffer IDs.  The
                        # detector then (re)transmits exactly those frames (cmd_type=0x0F) to the
                        # image port.  numMissed=0 means "I have everything" -> detector sends
                        # nothing.  getPendingImageBuffers computes missed = totalBuffers - received
                        # (the vendor protocol implementation:345272), so on the first query (received=0) the host
                        # must request ALL ETHERNET_NO_OF_IMAGES (=8) buffers.  THIS is what
                        # triggers the stream; replying 0 here is why no pixel data ever arrived.
                        #
                        # A DEADBEEF payload is the detector's uninitialized/already-freed image
                        # slot (appears once a transfer has been marked complete) -- ignore it.
                        raw32 = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
                        if raw32 == 0xDEADBEEF:
                            self._log(f"  ctrl 0x30000 DEADBEEF payload -- image slot "
                                      f"uninitialized/freed; ignoring (not replying)")
                            continue
                        img_id = struct.unpack_from("<H", payload, 0)[0] if len(payload) >= 2 else 0
                        scr_id = struct.unpack_from("<H", payload, 2)[0] if len(payload) >= 4 else 0
                        self._last_xfer_imageid = img_id   # behavior signal for --sweep-acq
                        # We have no per-frame index in the 0x0F frames, so approximate which
                        # buffers are still outstanding by arrival order: frames come in sequence,
                        # so received = collected chunks, still-missing = [received .. N-1].
                        received = len(chunks)
                        # Honor the buffer count the detector actually advertised instead of blindly
                        # asking for 8.  In the preview path (imageId high-bit 0x8000) it reports a
                        # 4-buffer image; requesting [0..7] asks for frames that don't exist in the
                        # preview set, which may be why it discards the request.  Prefer the count
                        # learned from the previous 0x41 reply; else infer from the preview flag.
                        preview = bool(img_id & PREVIEW_IMAGE_FLAG)
                        if self._last_xfer_count:
                            n_buf = self._last_xfer_count
                        elif preview:
                            n_buf = PREVIEW_NO_OF_IMAGES
                        else:
                            n_buf = ETHERNET_NO_OF_IMAGES
                        missed = list(range(received, n_buf))
                        reply_kind = "0x99 parallel" if self.parallel else "cmd9 serial"
                        self._log(f"  ctrl 0x30000 image_xfer_status payload={payload.hex()} "
                                  f"imageId=0x{img_id:04X}{' PREVIEW' if preview else ''} "
                                  f"scriptId={scr_id} received={received}/{n_buf} -> "
                                  f"replying {reply_kind} requesting {len(missed)} "
                                  f"missed buffers {missed}")
                        # Status reply with the missed-buffer list. Crucially this is NOT
                        # numMissed=0 -- reporting 0 tells the detector "transfer complete" and it
                        # FREES the image buffer (that is what produced DEADBEEF on the subsequent
                        # 0x98 in earlier runs). A non-zero missed list keeps the image alive.
                        # With --parallel this goes out as cmd_type=0x99 (echoing imageId/scriptId),
                        # the only status-reply form tied to the image-port transfer path.
                        self.reply_image_xfer_status(missed_ids=missed,
                                                     image_id=img_id, script_id=scr_id)
                        if not xfer_requested:
                            xfer_requested = True
                            last_img_t = time.time()  # fresh window for the stream to begin
                        if not missed:
                            xfer_replied = True

                        # The cmd9 status reply alone does NOT start the stream -- it only keeps
                        # the image alive and reports what we still need. The actual trigger is the
                        # IMAGE_RETRIVAL (0x98) pull, queried by IMAGE_RETRIVAL_REQUEST (0x41).
                        # Send it ONCE, AFTER the first non-zero cmd9 (so the image is still alive).
                        if missed and not pull_done:
                            pull_done = True
                            self._log(f"  Sending IMAGE_RETRIVAL_REQUEST (0x41) scriptId={scr_id}")
                            self._send_cmd(make_pdap(CMD_IMAGE_RETRIVAL_REQUEST,
                                                     struct.pack("<I", scr_id)))
                            _41_pl = None
                            _41_deadline = time.time() + 2.0
                            while time.time() < _41_deadline:
                                try:
                                    _s, _ct, _pl = self._recv_cmd(
                                        None, timeout_s=max(0.05, _41_deadline - time.time()))
                                except Exception:
                                    break
                                if _ct is None:        # bare-ACK -- keep waiting for the PDAP reply
                                    continue
                                if _ct == CMD_IMAGE_RETRIVAL_REQUEST:
                                    _41_pl = _pl
                                    break
                                self._log(f"  0x41 wait: skipping cmd_type=0x{_ct:08X}")
                            self._log(f"  0x41 reply: payload="
                                      f"{_41_pl.hex() if _41_pl else 'none'}")
                            if _41_pl and len(_41_pl) >= 4:
                                self._last_xfer_count = struct.unpack_from("<I", _41_pl, 0)[0]
                            if getattr(self, "_recover_98", None):
                                # RECOVER MODE: send 0x98 with the CORRECT recoverLostImage
                                # payload [imageId:2LE][scriptId:2LE] (per IMAGE_RETRIVAL:
                                # wire = [param_3][param_2], recoverLostImage(p1,p2)->cmd(p1,p2) =>
                                # [imageId][scriptId]) -- NOT the [imagePort][hostPort] form prior
                                # runs used. Sweep candidate (imageId,scriptId) pairs on the CTRL
                                # socket (the normal command path recoverLostImage uses), built from
                                # the live 0x30000 values + the reverse ordering as a hedge.
                                _cands = [(img_id, scr_id), (scr_id, img_id),
                                          (img_id & 0x7FFF, scr_id), (0, scr_id)]
                                _seen98 = set()
                                for _iid, _sid in _cands:
                                    if (_iid, _sid) in _seen98:
                                        continue
                                    _seen98.add((_iid, _sid))
                                    pl = struct.pack("<HH", _iid & 0xFFFF, _sid & 0xFFFF)
                                    self._log(f"  [recover] 0x98 payload=[imageId={_iid:#06x}]"
                                              f"[scriptId={_sid}] = {pl.hex()}")
                                    self._send_cmd(make_pdap(CMD_IMAGE_RETRIVAL, pl))
                                    time.sleep(0.3)
                                last_img_t = time.time()
                            else:
                                # EXPERIMENT: send the 0x98 retrieval FROM the image socket (6660) so
                                # the detector sees the request originating at the host's image
                                # endpoint. First emit a tiny probe datagram from 6660 -> detector:8100
                                # to 'open' the UDP path / let the detector learn host:6660.
                                if img_sock is not None and img_sock is not self._sock:
                                    try:
                                        img_sock.sendto(b"\x00\x00\x00\x00",
                                                        (self.detector_ip, self.broadcast_port))
                                        self._log(f"  probe: 4B from :{img_sock.getsockname()[1]} "
                                                  f"-> {self.detector_ip}:{self.broadcast_port}")
                                    except OSError as e:
                                        self._log(f"  probe send failed: {e}")
                                self._log(f"  Sending IMAGE_RETRIVAL (0x98) from image port "
                                          f"imgPort={image_port} hostPort={self.reply_port}")
                                self._send_cmd_via(img_sock,
                                                   build_image_retrival(host_port=self.reply_port,
                                                                        image_port=image_port))
                                last_img_t = time.time()  # fresh window after issuing the trigger

                    elif cmd_type == CMD_EXECUTE_SCRIPT:
                        status = payload[0] if payload else 0
                        self._log(f"  ctrl EXECUTE_SCRIPT reply status=0x{status:02X}")

                    elif cmd_type == CMD_EXECUTE_SCRIPT_STATUS:
                        self._log(f"  ctrl EXECUTE_SCRIPT_STATUS (script running...)")

                    elif cmd_type == CMD_DETECTOR_STATE_NOTIFY:
                        state_id = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
                        self._log(f"  ctrl detector state=0x{state_id:02X} ({state_id})")

                    elif cmd_type == CMD_GENERIC_SCRIPT:
                        self._log(f"  ctrl GENERIC_SCRIPT reply (late, from download phase)")

                    elif cmd_type == CMD_IMAGE_RETRIVAL:
                        self._log(f"  ctrl 0x98 reply plen={plen} "
                                  f"payload={payload.hex()}")
                        if plen > 4:
                            chunks.append(payload)

                    else:
                        self._log(f"  ctrl 0x{cmd_type:08X} plen={plen} "
                                  f"payload={payload.hex()}")

        raw = b"".join(chunks)
        self._log(f"Image: {len(raw)} total bytes in {len(chunks)} chunks")
        if output_path and raw:
            with open(output_path, "wb") as f:
                f.write(raw)
            self._log(f"Saved -> {output_path}")
        elif output_path and not raw:
            self._log("[WARN] No image data to save")
        return raw

    # -- detector data read-back (non-destructive) -----------------------------

    def read_detector_data(self, upload_id: int, name: str = "?",
                           chunk: int = 1024, timeout_s: float = 5.0,
                           stop_after: int = None) -> bytes:
        """Read a stored data category FROM the detector via the upload protocol (read-only).

        If stop_after is set, stop once at least that many bytes have been read (used to verify a
        patch near the start of a large category without reading the whole thing).

        Sequence (wire format from the vendor protocol implementation):
          1. host -> det  cmd 0x13 [uploadId:4LE]
          2. det  -> host cmd 0x13 [status:1][totalSize:4LE]
          3. loop: host -> det cmd 0x14 [bufId:4LE][numBytes:4LE]
                   det  -> host cmd 0x14 [bufId:4LE][numBytes:4LE][data]
          4. host -> det  EndOfUpload (empty)
        Returns the raw bytes (e.g. a the host list blob / the detector info blob blob), or b"" on failure.
        NO writes to the detector -- this only reads what it already has stored.
        """
        self._log(f"UPLOAD/read 0x{upload_id:02X} ({name}): CONFIGURE_UPLOAD")
        self._send_cmd(build_configure_upload(upload_id))
        total = None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                seq, ct, pl = self._recv_cmd(None, timeout_s=max(0.1, deadline - time.time()))
            except TimeoutError:
                break
            if ct is None:
                continue   # bare ACK -- keep waiting for the 0x13 reply
            if ct == CMD_CONFIGURE_UPLOAD:
                status = pl[0] if pl else 0xFF
                total = struct.unpack_from("<I", pl, 1)[0] if len(pl) >= 5 else 0
                self._log(f"  CONFIGURE_UPLOAD reply: status=0x{status:02X} totalSize={total} "
                          f"(payload={pl.hex()})")
                if status != 0:
                    self._log(f"  [WARN] non-zero status -- detector refused upload of 0x{upload_id:02X}")
                    return b""
                break
            self._log(f"  (skipping cmd_type=0x{ct:08X} waiting for 0x13 reply)")
        if total is None:
            self._log(f"  [WARN] no CONFIGURE_UPLOAD (0x13) reply -- detector may not support "
                      f"uploadId 0x{upload_id:02X}, or it is gated")
            return b""
        if total == 0:
            self._log("  totalSize=0 -- nothing stored for this category")
            return b""

        data = bytearray()
        buf_id = 0
        while len(data) < total:
            req = min(chunk, total - len(data))
            self._log(f"  UPLOAD_BUFFER bufId={buf_id} numBytes={req} (have {len(data)}/{total})")
            self._send_cmd(build_upload_buffer(buf_id, req))
            got = None
            d2 = time.time() + timeout_s
            while time.time() < d2:
                try:
                    seq, ct, pl = self._recv_cmd(None, timeout_s=max(0.1, d2 - time.time()))
                except TimeoutError:
                    break
                if ct is None:
                    continue
                if ct == CMD_UPLOAD_BUFFER:
                    # payload = [bufId:4LE][numBytes:4LE][data]
                    if len(pl) >= 8:
                        r_buf, r_n = struct.unpack_from("<II", pl, 0)
                        got = pl[8:8 + r_n]
                        self._log(f"    reply bufId={r_buf} numBytes={r_n} got={len(got)}b")
                    else:
                        got = b""
                    break
                self._log(f"    (skipping cmd_type=0x{ct:08X} waiting for 0x14 reply)")
            if not got:
                self._log("  [WARN] no/empty UPLOAD_BUFFER reply -- stopping")
                break
            data += got
            buf_id += 1
            if len(got) < req:
                # detector returned a short chunk; assume that's all it has
                break
            if stop_after is not None and len(data) >= stop_after:
                self._log(f"  [stop_after] reached {len(data)} >= {stop_after} bytes; stopping early")
                break

        # Best-effort EndOfUpload (empty command). cmd_type for end-of-upload is internal;
        # a read-only probe does not strictly need it, so we skip to avoid sending a guessed type.
        self._log(f"  [OK] read {len(data)} bytes for 0x{upload_id:02X} ({name})")
        return bytes(data)

    def probe_detector_data(self, skip_discovery: bool = False) -> None:
        """Connect (working handshake) then READ the detector's stored DetectorInfo + HostList +
        cal-results via the upload protocol. Pure read-back -- no writes, no flash. Used to learn the
        detector's registration/pairing state (is any host registered? what HostId does it expect?)
        before deciding on anything destructive."""
        self._open_sockets()
        try:
            if not skip_discovery and not self.discover():
                self._log("[WARN] discovery failed")
                return
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            self.request_signature()
            categories = [
                (UPLOAD_ID_DETECTORINFO, "DetectorInfo"),   # known-good: validates the read path
                (UPLOAD_ID_HOSTLIST,     "HostList"),        # the registration / pairing state
                (UPLOAD_ID_CALRESULTS,   "CalResults"),
            ]
            print("\n=== Detector stored-data read-back (non-destructive) ===")
            for uid, name in categories:
                blob = self.read_detector_data(uid, name)
                print(f"\n--- 0x{uid:02X} {name}: {len(blob)} bytes ---")
                if blob:
                    self._hexdump(blob)
                    # If it looks like text (.dyn config), also print decoded
                    printable = sum(1 for b in blob if 9 <= b <= 13 or 32 <= b < 127)
                    if printable > 0.8 * len(blob):
                        print("  [decoded text]")
                        print("  " + blob.decode("latin1").replace("\n", "\n  "))
            print("\n=======================================================\n")
        finally:
            self._close_sockets()

    def _hexdump(self, data: bytes, maxlen: int = 512) -> None:
        for i in range(0, min(len(data), maxlen), 16):
            chunk = data[i:i + 16]
            hexs = " ".join(f"{b:02X}" for b in chunk)
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"  {i:04X}  {hexs:<48}  {ascii_}")
        if len(data) > maxlen:
            print(f"  ... ({len(data) - maxlen} more bytes)")

    def configure_upload_probe(self, upload_id: int, timeout_s: float = 2.0):
        """Send only CONFIGURE_UPLOAD (cmd 0x13) for upload_id and return (status, totalSize).
        Non-destructive: this just asks the detector "can I read category X, and how big is it".
        Returns (status, size); status==0 means readable. (None, None) if no 0x13 reply."""
        self._send_cmd(build_configure_upload(upload_id))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                seq, ct, pl = self._recv_cmd(None, timeout_s=max(0.1, deadline - time.time()))
            except TimeoutError:
                break
            if ct is None:
                continue
            if ct == CMD_CONFIGURE_UPLOAD:
                status = pl[0] if pl else 0xFF
                size = struct.unpack_from("<I", pl, 1)[0] if len(pl) >= 5 else 0
                return status, size
        return None, None

    def enumerate_upload_ids(self, lo: int = 0x00, hi: int = 0x2000,
                             skip_discovery: bool = False) -> None:
        """Enumerate READABLE upload regions over a wide uploadId range using ONLY the cheap
        CONFIGURE_UPLOAD (cmd 0x13) probe (no data transfer -> safe, fast, read-only).

        The uploadId field is 4 bytes but only 0x00-0xFF was ever swept.  Higher ids might map to
        other firmware regions -- crucially, possibly the SDRAM image DMA buffers where the acquired
        image is held (which is NOT in any 0x00-0xFF flash category).  Any readable id outside the
        known flash set is a candidate to read the live image out.  Run once idle, then (if a new
        region appears) acquire a dark and re-read that id to see if it carries pixels."""
        KNOWN = {0x06,0x07,0x08,0x0A,0x0B,0x0E,0x0F,0x10,0x11,0x12,0x50,0x51,0x52,0x71,0x72,
                 0xFD,0xFE,0xFF}
        self._open_sockets()
        try:
            if not skip_discovery and not self.discover():
                self._log("[WARN] discovery failed"); return
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            self.request_signature()
            print(f"\n=== uploadId enumeration 0x{lo:X}..0x{hi:X} (0x13 probe only, read-only) ===")
            readable = []
            t0 = time.time(); ntimeout = 0
            for uid in range(lo, hi + 1):
                status, size = self.configure_upload_probe(uid, timeout_s=0.4)
                if status is None:
                    ntimeout += 1
                if status == 0 and size and size > 0:
                    new = "" if uid in KNOWN else "   <<< NEW (not in known 0x00-0xFF set)"
                    print(f"  0x{uid:X}: readable size={size} ({size/1048576:.2f} MB){new}")
                    readable.append((uid, size, uid not in KNOWN))
                if uid % 0x80 == 0 and uid:
                    rate = (uid - lo + 1) / max(0.1, time.time() - t0)
                    print(f"  ...0x{uid:X}  ({ntimeout} no-reply, {rate:.0f} ids/s)")
            news = [r for r in readable if r[2]]
            print(f"\n  {len(readable)} readable ids; {len(news)} NEW (>0xFF or unknown):")
            for uid, size, _ in news:
                print(f"    0x{uid:X}  size={size} ({size/1048576:.2f} MB)  <- candidate; "
                      f"read with --backup-range 0x{uid:X}-0x{uid:X}")
        finally:
            self._close_sockets()

    def backup_detector_data(self, out_dir: str = ".",
                             lo: int = 0x00, hi: int = 0xFF,
                             skip_discovery: bool = False) -> None:
        """Back up EVERYTHING the detector will hand over via the upload protocol (read-only).

        The internal NOR flash can't be read as raw firmware, but the detector exposes a set of
        data/config/calibration FILES by upload-ID (HostList, DetectorInfo/shock, cal results, DAT,
        Map, SensorInfo, ...). We don't have a clean ID table, so we SWEEP ids [lo..hi]: for each,
        CONFIGURE_UPLOAD reports status+size; for every readable one we pull the full blob and save
        it. CONFIGURE_UPLOAD only stages a read -- nothing is written to the detector.

        Saves: <out_dir>/detector_backup_<timestamp>/upload_0x<id>_<size>.bin  + manifest.txt
        Do this BEFORE any registration/flash write so we have a complete copy of the current state.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(out_dir, f"detector_backup_{ts}")
        os.makedirs(backup_dir, exist_ok=True)
        self._open_sockets()
        manifest = []
        try:
            if not skip_discovery and not self.discover():
                self._log("[WARN] discovery failed")
                return
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            sig = self.request_signature()
            if sig is not None:
                with open(os.path.join(backup_dir, "signature.txt"), "w") as fh:
                    fh.write(str(sig) + "\n")
                manifest.append(f"signature: {sig}")

            print(f"\n=== Detector full backup -> {backup_dir} ===")
            print(f"Sweeping upload IDs 0x{lo:02X}..0x{hi:02X} (read-only)...")
            found = 0
            for uid in range(lo, hi + 1):
                status, size = self.configure_upload_probe(uid)
                if status is None:
                    continue          # no reply -- id not handled
                if status != 0:
                    continue          # detector refused this id
                if size == 0:
                    print(f"  0x{uid:02X}: readable but empty (size 0)")
                    manifest.append(f"0x{uid:02X}: empty")
                    continue
                print(f"  0x{uid:02X}: readable, {size} bytes -> reading...")
                blob = self.read_detector_data(uid, name=f"id0x{uid:02X}")
                fn = os.path.join(backup_dir, f"upload_0x{uid:02X}_{len(blob)}.bin")
                with open(fn, "wb") as fh:
                    fh.write(blob)
                printable = sum(1 for b in blob if 9 <= b <= 13 or 32 <= b < 127)
                kind = "text" if blob and printable > 0.8 * len(blob) else "binary"
                print(f"        saved {len(blob)}/{size} bytes ({kind}) -> {os.path.basename(fn)}")
                manifest.append(f"0x{uid:02X}: {len(blob)} bytes ({kind}) -> {os.path.basename(fn)}")
                found += 1
            with open(os.path.join(backup_dir, "manifest.txt"), "w") as fh:
                fh.write("\n".join(manifest) + "\n")
            print(f"\n[OK] Backup complete: {found} data blobs saved to {backup_dir}")
            print("=" * 60 + "\n")
        finally:
            self._close_sockets()

    # -- detector data WRITE (DESTRUCTIVE -- writes flash) ----------------------

    def write_detector_data(self, download_id: int, data: bytes,
                            chunk: int = 1024, timeout_s: float = 8.0) -> bool:
        """Write `data` to the detector's `download_id` category and COMMIT it to flash.

        Sequence (wire format from the vendor protocol implementation downloadData):
          1. host -> det  cmd 0x0E [downloadId:4LE][totalSize:4LE]   ; reply 0x0E [status:1]
          2. loop bufId=0,1,..: host -> det cmd 0x0F [bufId:4LE][numBytes:4LE][data]
                                reply 0x0F [status:1][_:4]
          3. host -> det  cmd 0x10 (empty)  = FLASH COMMIT/BURN      ; reply 0x10 [status:1]
        *** STEP 3 WRITES THE DETECTOR'S FLASH AND IS IRREVERSIBLE. ***
        Returns True only if every step reported status 0.
        """
        total = len(data)
        self._log(f"WRITE: CONFIGURE_DOWNLOAD id=0x{download_id:02X} totalSize={total}")
        self._send_cmd(build_configure_download(download_id, total))
        if not self._await_status(CMD_CONFIGURE_DOWNLOAD, "CONFIGURE_DOWNLOAD", timeout_s):
            return False
        buf_id = 0
        off = 0
        while off < total:
            piece = data[off:off + chunk]
            self._log(f"  DOWNLOAD_BUFFER bufId={buf_id} numBytes={len(piece)} "
                      f"({off}/{total})")
            self._send_cmd(build_download_buffer(buf_id, piece))
            if not self._await_status(CMD_DOWNLOAD_BUFFER, f"DOWNLOAD_BUFFER[{buf_id}]", timeout_s):
                return False
            off += len(piece)
            buf_id += 1
        self._log("  FLASH_COMMIT (cmd 0x10) -- *** writing flash ***")
        self._send_cmd(build_flash_commit())
        if not self._await_status(CMD_FLASH_COMMIT, "FLASH_COMMIT", max(timeout_s, 15.0)):
            return False
        self._log("  [OK] write+commit reported success")
        return True

    def _await_status(self, expect_cmd: int, label: str, timeout_s: float) -> bool:
        """Wait for `expect_cmd` reply and check its leading status byte == 0."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                seq, ct, pl = self._recv_cmd(None, timeout_s=max(0.1, deadline - time.time()))
            except TimeoutError:
                break
            if ct is None:
                continue
            if ct == expect_cmd:
                status = pl[0] if pl else 0xFF
                ok = (status == 0)
                self._log(f"    {label} reply: status=0x{status:02X} "
                          f"{'OK' if ok else '*** NONZERO ***'} (payload={pl.hex() if pl else 'none'})")
                return ok
            self._log(f"    ({label}: skipping cmd 0x{ct:08X})")
        self._log(f"    [FAIL] {label}: no reply in {timeout_s}s")
        return False

    def smoke_test_hostlist(self, commit: bool = False,
                            skip_discovery: bool = False) -> None:
        """SAFEST write validation: read the current HostList (0x71), then write back the IDENTICAL
        bytes and read again to confirm. Targets the HostList/config region (flash 0x940000), NOT the
        firmware. Worst case on a botched write = HostList (registration) corruption, which we have
        fully backed up. Dry-run by default; pass commit=True to actually perform the flash write.
        """
        self._open_sockets()
        try:
            if not skip_discovery and not self.discover():
                self._log("[WARN] discovery failed"); return
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            self.request_signature()

            print("\n=== HostList write SMOKE-TEST (identical bytes) ===")
            before = self.read_detector_data(DOWNLOAD_ID_HOSTLIST, "HostList(before)")
            if not before:
                print("[ABORT] could not read current HostList -- not writing."); return
            print(f"Current HostList: {len(before)} bytes")
            self._hexdump(before, maxlen=len(before))
            # sanity: the augmented-message CRC over the WHOLE blob (incl. its trailer) must be 0.
            # (equivalently: hostlist_crc(body + 0x00000000) == trailer_BE)
            crc_whole = hostlist_crc(before)
            crc_regen = hostlist_crc(before[:-4] + b"\x00\x00\x00\x00")
            crc_trailer = struct.unpack(">I", before[-4:])[0]
            crc_ok = (crc_whole == 0) and (crc_regen == crc_trailer)
            print(f"CRC self-check: whole-blob={crc_whole:#010x} (want 0); "
                  f"regen={crc_regen:#010x} vs trailer={crc_trailer:#010x} -> "
                  f"{'OK' if crc_ok else 'MISMATCH'}")

            if not commit:
                print("\n[DRY-RUN] Would now send (NOTHING sent without --commit):")
                print(f"  1. CONFIGURE_DOWNLOAD id=0x{DOWNLOAD_ID_HOSTLIST:02X} size={len(before)}")
                nchunks = (len(before) + 1023) // 1024
                print(f"  2. {nchunks} x DOWNLOAD_BUFFER (the {len(before)} identical bytes above)")
                print(f"  3. FLASH_COMMIT (cmd 0x10) -- writes flash")
                print("  Re-run with --commit to actually perform the write.")
                print("=" * 52 + "\n")
                return

            print("\n*** --commit set: performing the flash write of IDENTICAL bytes ***")
            ok = self.write_detector_data(DOWNLOAD_ID_HOSTLIST, before)
            if not ok:
                print("[FAIL] write/commit did not report success -- verifying state...")
            after = self.read_detector_data(DOWNLOAD_ID_HOSTLIST, "HostList(after)")
            print(f"\nRead-back after write: {len(after)} bytes")
            if after == before:
                print("[OK] *** SMOKE-TEST PASSED: HostList identical after write -- "
                      "the write/commit path works and is safe. ***")
            else:
                print("[WARN] HostList differs after write! before != after:")
                self._hexdump(after, maxlen=len(after))
                print("  (Restore from backup upload_0x71_504.bin if needed.)")
            print("=" * 52 + "\n")
        finally:
            self._close_sockets()

    def register_self(self, commit: bool = False, skip_discovery: bool = False,
                      host_mac: str = "00:6f:00:01:0a:3a",
                      name: str = "FLASHPAD_RE", location: str = "RE_HOST") -> None:
        """Register THIS host in the detector's HostList and set it as the primary host, so the
        detector will stream images to us. Appends our entry (HostId derived from host_mac, which MUST
        be the MAC of the eth interface that talks to the detector), preserves the existing
        registrations, sets IndexToPrimaryHost to our new index, recomputes the CRC, and writes via
        0x71 + flash commit. Dry-run by default; --commit performs the flash write.
        Fully reversible: restore the 504-byte backup (upload_0x71_504.bin) to undo.
        """
        our_mac = host_mac
        our_hostid = hostid_from_mac(our_mac)
        self._open_sockets()
        try:
            if not skip_discovery and not self.discover():
                self._log("[WARN] discovery failed"); return
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            self.request_signature()

            print("\n=== REGISTER SELF in detector HostList ===")
            cur = self.read_detector_data(DOWNLOAD_ID_HOSTLIST, "HostList(before)")
            if not cur or hostlist_crc(cur) != 0:
                print("[ABORT] could not read a valid current HostList -- not writing."); return
            _, entries, count, primary = parse_hostlist(cur)
            print(f"Current: {count} hosts, IndexToPrimaryHost={primary} "
                  f"({'NONE' if primary == 0xFFFF else primary})")
            for i, e in enumerate(entries):
                hid = e[:16].split(b'\x00')[0].decode('latin1')
                nm  = e[16:80].split(b'\x00')[0].decode('latin1')
                print(f"    host[{i}] HostId={hid:18s} name={nm!r}")
            print(f"Our MAC={our_mac or '(unknown, using fallback)'}  -> HostId={our_hostid}")

            new = build_registered_hostlist(cur, our_hostid, name, location)
            _, nentries, ncount, nprimary = parse_hostlist(new)
            print(f"\nNEW: {ncount} hosts, IndexToPrimaryHost={nprimary} (our appended entry), "
                  f"{len(cur)} -> {len(new)} bytes, CRC self-check="
                  f"{'OK' if hostlist_crc(new) == 0 else 'BAD'}")
            print(f"    + host[{ncount-1}] HostId={our_hostid} name={name!r} loc={location!r}  PRIMARY")

            if not commit:
                print("\n[DRY-RUN] Would CONFIGURE_DOWNLOAD 0x71 / "
                      f"{len(new)} -> DOWNLOAD_BUFFER(s) -> FLASH_COMMIT.")
                print("  New HostList bytes:")
                self._hexdump(new, maxlen=len(new))
                print("  Re-run with --commit to write it. Then RECONNECT and run --dark-only to test.")
                print("=" * 44 + "\n")
                return

            print("\n*** --commit: writing modified HostList (registering us as primary) ***")
            ok = self.write_detector_data(DOWNLOAD_ID_HOSTLIST, new)
            after = self.read_detector_data(DOWNLOAD_ID_HOSTLIST, "HostList(after)")
            if after == new:
                print("[OK] *** REGISTERED: HostList written & verified. We are primary host "
                      f"index {ncount-1} (HostId {our_hostid}). ***")
                print("  NEXT: reconnect and run  python flashpad_acquire.py --dark-only --no-standby")
                print("  to see whether the detector now streams 0x0F image data to us.")
            else:
                print(f"[WARN] read-back ({len(after)}B) != written ({len(new)}B). "
                      "Restore upload_0x71_504.bin if needed.")
                self._hexdump(after, maxlen=min(len(after), 256))
            print("=" * 44 + "\n")
        finally:
            self._close_sockets()

    def restore_hostlist(self, path: str, commit: bool = False,
                         skip_discovery: bool = False) -> None:
        """Write a saved HostList blob (e.g. the backup upload_0x71_504.bin) back to the detector via
        0x71 + flash commit. The safety 'undo' button. Dry-run unless commit=True."""
        with open(path, "rb") as fh:
            blob = fh.read()
        print(f"\n=== RESTORE HostList from {path} ({len(blob)} bytes) ===")
        if len(blob) < HL_HEADER_LEN + 4 or hostlist_crc(blob) != 0:
            print(f"[ABORT] {path} is not a valid HostList blob (CRC self-check != 0). "
                  "Refusing to write."); return
        _, ents, cnt, pri = parse_hostlist(blob)
        print(f"  valid HostList: {cnt} hosts, primary={pri}, CRC OK")
        if not commit:
            print("  [DRY-RUN] re-run with --commit to write it back."); print("=" * 44 + "\n"); return
        self._open_sockets()
        try:
            if not skip_discovery and not self.discover():
                self._log("[WARN] discovery failed"); return
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            self.request_signature()
            print("*** --commit: restoring HostList ***")
            self.write_detector_data(DOWNLOAD_ID_HOSTLIST, blob)
            after = self.read_detector_data(DOWNLOAD_ID_HOSTLIST, "HostList(after)")
            print("[OK] restored & verified" if after == blob else
                  f"[WARN] read-back ({len(after)}B) != file ({len(blob)}B)")
            print("=" * 44 + "\n")
        finally:
            self._close_sockets()

    # -- sensor reads ----------------------------------------------------------

    def read_sensor(self, selector: int, cmd_type: int = CMD_READ_SENSOR,
                    timeout_s: float = 3.0):
        """Read a detector sensor.  Sends a PDAP command (0x7900 raw / 0x7902 converted /
        0x7904 detailed) with a 4-byte selector and returns the 4-byte sensor value from the
        matching reply, or None on timeout.  Independent of the (broken) image transfer path.
        Format raw sensor read + the sensor read path.
        """
        self._send_cmd(make_pdap(cmd_type, struct.pack("<I", selector)))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                seq, ct, pl = self._recv_cmd(None, timeout_s=max(0.05, deadline - time.time()))
            except Exception:
                break
            if ct is None:            # bare-ACK -- keep waiting for the PDAP reply
                continue
            if ct == cmd_type:
                if pl and len(pl) >= 4:
                    return struct.unpack_from("<I", pl, 0)[0]
                return None
            self._log(f"  sensor wait: skipping cmd_type=0x{ct:08X}")
        return None

    def read_sensors(self, skip_discovery: bool = False, roe_init: bool = True) -> bool:
        """Connect and read detector sensors (accelerometer, temperature).  Does NOT touch the
        image transfer path, so it works even though image streaming is blocked.
        """
        self._open_sockets()
        try:
            if not skip_discovery and not self.discover():
                self._log("[WARN] discovery failed")
                return False
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            self.request_signature()
            if roe_init:
                # The accelerometer/vibration + temperature sensors hang off the ROE; Script7
                # powers/initializes it.  Harmless (~1s) and improves the odds of valid reads.
                self._log("Running Script7 (ROE init) before sensor reads")
                if self.download_script(build_script_7_roe_init(), "Script7-ROEInit"):
                    if self.execute_script():
                        self.wait_for_execution_complete(timeout_s=10.0)

            # Read the FULL sensor table from the detector's own [Sensor] map (backup 0x07).
            # Primary read = cmd 0x7902 (CONVERTED -- detector does unit conversion, per the table).
            # We also show the 0x7900 (RAW) value alongside for comparison / when 0x7902 is unsupported.
            def s32(v):
                return None if v is None else (v - 0x100000000 if (v & 0x80000000) else v)

            print("\n=== Detector sensor readings (full table from 0x07) ===")
            print(f"  {'Sensor':22s} {'id':>4s}  {'converted (0x7902)':>18s}  {'raw':>6s}  decoded")
            print("  " + "-" * 70)
            any_ok = False
            seen = {}                # (conv,raw) -> first sensor name that produced it
            for name, sid in SENSOR_TABLE:
                conv = self.read_sensor(sid, CMD_READ_SENSOR_CONVERTED)
                raw  = self.read_sensor(sid, CMD_READ_SENSOR)
                if conv is None and raw is None:
                    print(f"  {name:22s} {sid:4d}  {'(no reply)':>18s}")
                    continue
                any_ok = True
                cs, rs = s32(conv), s32(raw)
                # Flag stale/unsupported: ids whose (conv,raw) exactly duplicate an earlier sensor's
                # are the detector returning leftover conversion-register contents (unimplemented id).
                key = (conv, raw)
                dup = seen.get(key)
                if dup is None:
                    seen[key] = name
                # decode
                note = ""
                if dup is not None:
                    note = f"** stale/unsupported (== {dup})"
                elif rs is not None and (rs & 0xFFFF) == 0x3FF:
                    note = "** ADC railed (open/idle) -- invalid"
                elif name.startswith(("Temp_",)):
                    note = f"~{cs/10.0:.1f} degC (railed)" if cs is not None else ""
                elif cs is not None and abs(cs) <= 30000:
                    note = f"{cs/1000.0:+.3f} V"   # rails are in mV
                cstr = f"0x{conv:08X}({cs})" if conv is not None else "-"
                rstr = f"{rs}" if rs is not None else "-"
                print(f"  {name:22s} {sid:4d}  {cstr:>18s}  {rstr:>6s}  {note}")
            print("  " + "-" * 70)
            print("  converted (0x7902) = detector engineering units: power rails in mV (shown as V),")
            print("  temps in 0.1 degC. raw (0x7900) = 12-bit ADC counts (0x3FF=1023=railed/open).")
            print("=" * 72 + "\n")
            if not any_ok:
                self._log("[WARN] No sensor replied -- the sensor path may be gated too, or "
                          "the panel/ROE is not powered.")
            return any_ok
        finally:
            self._close_sockets()

    def sweep_acquisition(self, type_modes=(1, 0), transfer_modes=(0, 1, 2, 3, 4, 5),
                          watch_s: float = 12.0) -> None:
        """EXPERIMENT: run the dark acquisition across (type_mode, transfer_mode) combinations and
        report which (if any) makes the detector finally stream 0x0F pixel frames. The acquisition
        'Transfer Mode' byte is the one knob we control that's literally named for image-data transfer;
        we've only ever sent 0. Fresh connection per combo. Non-destructive (acquisitions only)."""
        print("\n=== Acquisition transfer-mode SWEEP (looking for a 0x0F push) ===")
        print("(gentle mode: bounded discovery + recovery pause; stops if detector goes unresponsive)")
        results = []
        dead_streak = 0
        for tm in type_modes:
            for xfer in transfer_modes:
                self._open_sockets()
                got = 0
                self._last_xfer_imageid = None
                self._last_xfer_count = None
                try:
                    if not self.discover(max_wait=8.0):
                        dead_streak += 1
                        self._log(f"[WARN] discover failed (detector unresponsive, streak={dead_streak})")
                        if dead_streak >= 2:
                            print("\n[STOP] detector stopped responding -- POWER-CYCLE it and re-run. "
                                  f"(last combo attempted: type_mode={tm} transfer_mode={xfer})")
                            return
                        continue
                    dead_streak = 0
                    self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
                    self.request_signature()
                    if not self.download_script(build_script_7_roe_init(), "Script7"):
                        continue
                    if not self.download_script(
                            build_script_1_dark_acq(type_mode=tm, transfer_mode=xfer),
                            f"Dark(tm={tm},xfer={xfer})"):
                        continue
                    self._open_image_socket(HOST_IMAGE_PORT)
                    if not self.execute_script():
                        continue
                    print(f"  --- type_mode={tm} transfer_mode={xfer}: acquiring, watching {watch_s}s ---")
                    raw = self.receive_image(output_path=None, timeout_s=watch_s,
                                             script_id=1, image_port=HOST_IMAGE_PORT,
                                             wait_exec_complete=True)
                    got = len(raw)
                finally:
                    self._close_sockets()
                iid = self._last_xfer_imageid
                cnt = self._last_xfer_count
                beh = f"imageId={'0x%04X' % iid if iid is not None else '?'} count={cnt}"
                tag = f"*** {got} BYTES PUSHED ***" if got else f"no 0x0F  ({beh})"
                print(f"  RESULT type_mode={tm} transfer_mode={xfer}: {tag}")
                results.append((tm, xfer, got, iid, cnt))
                time.sleep(3.0)   # recovery pause: let the detector return to idle between acqs
        print("\n=== sweep summary (imageId/count = the detector's transfer-state response) ===")
        hits = [r for r in results if r[2]]
        for t, x, g, iid, cnt in results:
            beh = f"imageId={'0x%04X' % iid if iid is not None else '?'} count={cnt}"
            print(f"  type_mode={t} transfer_mode={x}: "
                  f"{'PUSH %d bytes' % g if g else 'no push'}   [{beh}]")
        if hits:
            print(f"\n[!!!] PUSH detected at: " +
                  ", ".join(f"(type={t},xfer={x})" for t, x, _, _, _ in hits))
        else:
            print("\nNo combo pushed 0x0F. But note any combo where imageId/count CHANGED "
                  "(e.g. imageId=0x8000) -- that's transfer-mode altering the state machine.")
        print("=" * 60 + "\n")

    def force_wired(self, watch_s: float = 10.0) -> bytes:
        """LEGACY EXPERIMENT -- no longer needed.

        This dates from before image retrieval was understood.  It sweeps runtime transport
        config values trying to force image egress onto ethernet.  That is unnecessary: the
        detector boots with ethernet already selected, and images transfer correctly once the
        host link settings are right and the IMAGE_RETRIVAL_REQUEST (0x41) -> IMAGE_RETRIVAL
        (0x98) sequence is used.  Kept only for reference.
        Non-destructive (the config writes are volatile RAM, cleared by power-cycle).

        Sweep:
          - DETECTOR_CONFIG (cmd 0x11) configIds {1, 0x16, 0x17} x values {0,1,2,3}  (the only valid
            runtime config params; one may be the transport/wired selector), each then a dark acq.
          - For completeness also re-asserts the config right before EXECUTE.
        Run with the detector ON.  Watch tshark on host 192.168.1.30 in parallel to confirm any push."""
        from itertools import product
        combos = [(cid, val) for cid in (0x01, 0x16, 0x17) for val in (0, 1, 2, 3)]
        print(f"\n=== FORCE-WIRED sweep: {len(combos)} DetectorConfig combos, watching for 0x0F ===")
        for cid, val in combos:
            self._open_sockets()
            self._last_xfer_imageid = None; self._last_xfer_count = None
            raw = b""
            try:
                if not self.discover(max_wait=8.0):
                    self._log(f"  cfg(0x{cid:X}={val}): no discover (wedged?) -- power-cycle");
                    self._close_sockets(); continue
                self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
                self.request_signature()
                # set the candidate transport config (cmd 0x11)
                self._send_cmd(make_pdap(0x11, struct.pack("<III", cid, val, 0)))
                time.sleep(0.3)
                try:
                    dl = time.time() + 1.0
                    while time.time() < dl:
                        try: self._recv_cmd(None, timeout_s=0.3)
                        except Exception: break
                except Exception: pass
                if not self.download_script(build_script_7_roe_init(), "s7"): self._close_sockets(); continue
                if not self.download_script(build_script_1_dark_acq(type_mode=1, transfer_mode=0), "s1"):
                    self._close_sockets(); continue
                self._open_image_socket(HOST_IMAGE_PORT)
                if not self.execute_script(): self._close_sockets(); continue
                raw = self.receive_image(output_path=None, timeout_s=watch_s, script_id=1,
                                         image_port=HOST_IMAGE_PORT, wait_exec_complete=True)
            finally:
                self._close_sockets()
            if raw:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                out = f"flashpad_wired_{cid:02X}_{val}_{ts}.raw"
                with open(out, "wb") as f: f.write(raw)
                print(f"\n[!!!!] WIRED PUSH at cfg(0x{cid:X}={val}): {len(raw)} bytes -> {out}")
                return raw
            print(f"  cfg(0x{cid:X}={val}): no push (iid={self._last_xfer_imageid} cnt={self._last_xfer_count})")
            time.sleep(1.5)
        print("\nNo config combo forced wired egress. => egress is firmware-routed, not config-"
              "selectable at runtime; use the firmware-patch route (see memory/notes).")
        return b""

    def preview_acquire(self, watch_s: float = 12.0,
                        type_mode: int = 1, transfer_mode: int = 2) -> bytes:
        """Single-shot dark acquisition on the PREVIEW transfer path (acquisition transfer_mode=2).

        In tm=2 the detector advertises imageId=0x8000 (preview flag) and a 4-buffer image in its
        0x41 reply.  The 0x30000 status reply now honors that 4-buffer count (instead of hardcoding
        8), so we ask for exactly the buffers the preview set contains, then watch port 6660 for a
        0x0F push.  ONE clean connection (unlike --sweep-acq, which cycles modes and tends to wedge
        the detector), so it's safe to run repeatedly.  Run a tshark capture on host 192.168.1.30 in
        parallel to confirm any push on the wire.  Non-destructive (acquisition only)."""
        print(f"\n=== single-shot dark acq (type_mode={type_mode}, transfer_mode={transfer_mode}) ===")
        self._open_sockets()
        self._last_xfer_imageid = None
        self._last_xfer_count   = None
        raw = b""
        try:
            if not self.discover(max_wait=8.0):
                self._log("[FAIL] discovery failed -- power-cycle the detector and retry")
                return b""
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            self.request_signature()
            if not self.download_script(build_script_7_roe_init(), "Script7-ROEInit"):
                return b""
            if not self.download_script(
                    build_script_1_dark_acq(type_mode=type_mode, transfer_mode=transfer_mode),
                    f"Dark(type={type_mode},xfer={transfer_mode})"):
                return b""
            self._open_image_socket(HOST_IMAGE_PORT)
            if not self.execute_script():
                return b""
            print(f"  --- preview acquiring, watching {watch_s}s for a 0x0F push ---")
            raw = self.receive_image(output_path=None, timeout_s=watch_s,
                                     script_id=1, image_port=HOST_IMAGE_PORT,
                                     wait_exec_complete=True)
        finally:
            self._close_sockets()
        iid = self._last_xfer_imageid
        cnt = self._last_xfer_count
        if raw:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = f"flashpad_preview_{ts}.raw"
            with open(out, "wb") as f:
                f.write(raw)
            print(f"\n[!!!] PREVIEW PUSH: {len(raw)} bytes streamed -> {out}")
        else:
            beh = f"imageId={'0x%04X' % iid if iid is not None else '?'} count={cnt}"
            print(f"\n  no 0x0F pushed  ({beh})")
        return raw

    def setcc_acquire(self, cc64: bytes, transfer_mode: int = 0, watch_s: float = 12.0) -> bytes:
        """Connect, send DETECTOR_SET_CC (cmd 0x30) with the 64-byte connection context, THEN acquire
        a dark and watch for a 0x0F push.  This replicates the runtime arming handshake the real GE
        host performs at connect (which flashpad_acquire never sent).  Non-destructive."""
        print(f"\n=== SET_CC arming test (cmd 0x30, then dark acq, transfer_mode={transfer_mode}) ===")
        self._open_sockets()
        self._last_xfer_imageid = None; self._last_xfer_count = None
        raw = b""
        try:
            if not self.discover(max_wait=8.0):
                self._log("[FAIL] discovery failed -- power-cycle and retry"); return b""
            self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=HOST_IMAGE_PORT)
            self.request_signature()
            # --- the new step: SET_CC ---
            self._log(f"  Sending DETECTOR_SET_CC (0x30) cc={cc64[:16].hex()}... ({len(cc64)}B)")
            self._send_cmd(build_set_cc(cc64))
            deadline = time.time() + 3.0
            got_reply = False
            while time.time() < deadline:
                try:
                    seq, ct, pl = self._recv_cmd(None, timeout_s=max(0.1, deadline - time.time()))
                except Exception:
                    break
                if ct is None:
                    continue
                if ct == CMD_DETECTOR_SET_CC:
                    self._log(f"  [OK] SET_CC reply: payload={pl.hex() if pl else '(empty)'}")
                    got_reply = True; break
                self._log(f"  (SET_CC wait: saw cmd_type=0x{ct:08X})")
            if not got_reply:
                self._log("  [WARN] no SET_CC (0x30) reply -- detector may not implement it, or wrong CC")
            # --- proceed to acquire ---
            if not self.download_script(build_script_7_roe_init(), "Script7-ROEInit"):
                return b""
            if not self.download_script(
                    build_script_1_dark_acq(type_mode=1, transfer_mode=transfer_mode),
                    f"Dark(type=1,xfer={transfer_mode})"):
                return b""
            self._open_image_socket(HOST_IMAGE_PORT)
            if not self.execute_script():
                return b""
            print(f"  --- acquiring (SET_CC sent), watching {watch_s}s for 0x0F push ---")
            raw = self.receive_image(output_path=None, timeout_s=watch_s, script_id=1,
                                     image_port=HOST_IMAGE_PORT, wait_exec_complete=True)
        finally:
            self._close_sockets()
        if raw:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = f"flashpad_setcc_{ts}.raw"
            with open(out, "wb") as f:
                f.write(raw)
            print(f"\n[!!!] SET_CC PUSH: {len(raw)} bytes streamed -> {out}")
        else:
            beh = (f"imageId={'0x%04X' % self._last_xfer_imageid if self._last_xfer_imageid is not None else '?'} "
                   f"count={self._last_xfer_count}")
            print(f"\n  no 0x0F pushed  ({beh})")
        return raw

    # -- high-level acquisition ------------------------------------------------

    def run_full_acquisition(self,
                              output_dir: str = ".",
                              do_dark: bool = False,
                              dark_only: bool = False,
                              skip_discovery: bool = False,
                              skip_standby: bool = False,
                              two_exec: bool = False,
                              exec_timeout: float = 60.0,
                              image_port: int = HOST_IMAGE_PORT) -> bytes:
        """Full acquisition sequence:
          1.  Open ctrl socket on port 5550 (Detector_HostPort)
          2.  SYSTEM_STARTUP (sets our IP on detector; bare ACKs use this port)
          3.  PORT_SETUP (hostCmdPort=5550, hostImgPort=6660) -- MUST be before SIGNATURE_REQUEST
          4.  SIGNATURE_REQUEST → SIGNATURE_REPLY (54-byte detector ID)
          5.  Download Script 7 (ROE init)
          6.  Download Script 8 (standby loop) [unless skip_standby]
          7a. [dark_only=True]  Download Script 1 (dark acq, no X-ray needed)
          7b. [do_dark=True]    Download Script 1 (dark), then Script 0 (std acq)
          7c. [default]         Download Script 0 (standard acq, needs X-ray)
          8.  Open image socket on port 6660 ← BEFORE execute_script (data streams during exec)
          9.  EXECUTE_SCRIPT
          10. receive_image() unified loop (ctrl + img sockets via select):
                - ctrl: await EXECUTION_COMPLETE, handle 0x30000 → reply cmd_type=9
                - img:  collect pixel data until 5s silence after EXECUTION_COMPLETE + 0x30000
        Returns raw image bytes (empty bytes on failure).

        NOTE on Script8: Script8 is a standby loop with repeatCount=65535 that only terminates
        on event 41.  If Script8 blocks the acquisition (detector runs Script8 before Script0/1),
        use skip_standby=True (--no-standby) to omit Script8 and run the acquisition directly.
        NOTE on PORT_SETUP ordering: The detector stores TWO separate port values:
          - system/beacon port: updated by SYSTEM_STARTUP payload (used for bare ACKs)
          - cmd reply port:     updated by PORT_SETUP hostCmdPort (used for full command replies)
        SIGNATURE_REPLY goes to cmd reply port. PORT_SETUP MUST be sent before SIGNATURE_REQUEST,
        otherwise SIGNATURE_REPLY goes to the old cached cmd reply port (e.g. 48879 from a prior
        session) and the signature step always times out.
        NOTE on two_exec: If two_exec=True, Script7 is executed ALONE first and the host waits for
        its EXECUTION_COMPLETE before downloading and executing Script1.  Hypothesis: the GE host
        software does this because Script7 ends with SendHostEvent(17) and the host processes that
        event before queueing the acquisition script.  Running both scripts in one EXECUTE_SCRIPT
        may result in the image subsystem being uninitialized when Script1 runs.
        """
        self._open_sockets()
        try:
            if not skip_discovery:
                if not self.discover():
                    return b""
            else:
                self._log(f"skip_discovery=True -- assuming detector already initialized")

            # PORT_SETUP first: updates detector's cmd reply port to 5550 so all subsequent
            # command replies (including SIGNATURE_REPLY) arrive on our socket.
            # Must come before SIGNATURE_REQUEST.
            if not self.send_port_setup(host_cmd_port=self.reply_port, host_img_port=image_port):
                self._log("Warning: PORT_SETUP failed, image data may not arrive")

            sig = self.request_signature()
            if sig is None:
                self._log("Warning: no signature received, continuing anyway")

            if two_exec and dark_only:
                # Two-EXECUTE_SCRIPT approach for dark acquisition.
                # Script7 executed alone first; host waits for EXECUTION_COMPLETE (incl. event 17)
                # before downloading and executing Script1. Mirrors the hypothesized GE host flow
                # where event 17 triggers a state change before the acquisition script runs.
                self._log("[two-exec] Phase 1: Script7 alone")
                if not self.download_script(build_script_7_roe_init(), "Script7-ROEInit"):
                    return b""
                if not self.execute_script():
                    return b""
                if not self.wait_for_execution_complete(timeout_s=exec_timeout):
                    return b""
                self._log("[two-exec] Script7 EXECUTION_COMPLETE; Phase 2: Script1")
                if not self.download_script(build_script_1_dark_acq(), "Script1-DarkAcq"):
                    return b""
                if not self._open_image_socket(image_port):
                    self._log("Warning: image socket failed to open -- pixel data may be lost")
                if not self.execute_script():
                    return b""
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_path = os.path.join(output_dir, f"flashpad_image_{ts}.raw")
                return self.receive_image(output_path=out_path,
                                          timeout_s=exec_timeout + 30.0,
                                          script_id=1,
                                          image_port=image_port,
                                          wait_exec_complete=True)

            if not self.download_script(build_script_7_roe_init(), "Script7-ROEInit"):
                return b""
            if not skip_standby:
                if not self.download_script(build_script_8_standby(), "Script8-Standby"):
                    return b""
            else:
                self._log("skip_standby=True -- omitting Script8 standby loop")

            if dark_only:
                # Dark acquisition only -- no X-ray needed; EXECUTION_COMPLETE arrives ~seconds
                if not self.download_script(build_script_1_dark_acq(), "Script1-DarkAcq"):
                    return b""
            elif do_dark:
                # Download dark script first, then standard acq script
                if not self.download_script(build_script_1_dark_acq(), "Script1-DarkAcq"):
                    return b""
                if not self.download_script(build_script_0_std_acq(), "Script0-StdAcq"):
                    return b""
            else:
                # Standard acquisition -- requires actual X-ray exposure to complete
                if not self.download_script(build_script_0_std_acq(), "Script0-StdAcq"):
                    return b""

            # Open the image socket BEFORE execute_script.
            # CRITICAL: The detector streams pixel data to image_port DURING script
            # execution, BEFORE EXECUTION_COMPLETE.  If the socket is not open in time,
            # all early pixel packets are dropped by the OS.
            if not self._open_image_socket(image_port):
                self._log("Warning: image socket failed to open -- pixel data may be lost")

            if not self.execute_script():
                return b""

            # Determine scriptId for image retrieval:
            # dark_only uses Script1 (scriptID=1); all other paths use Script0 (scriptID=0)
            img_script_id = 1 if dark_only else 0

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(output_dir, f"flashpad_image_{ts}.raw")
            # receive_image() handles EXECUTION_COMPLETE + 0x30000 → cmd_type=9 + pixel data
            # in a single select() loop.  exec_timeout + 30s extra for image streaming.
            return self.receive_image(output_path=out_path,
                                      timeout_s=exec_timeout + 30.0,
                                      script_id=img_script_id,
                                      image_port=image_port,
                                      wait_exec_complete=True)

        finally:
            self._close_sockets()


# -- CLI entry point -----------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="GE FlashPad Apollo -- URP/PDAP acquisition tool")
    parser.add_argument("--detector-ip",   default=DETECTOR_IP,
                        help=f"Detector IP (default: {DETECTOR_IP})")
    parser.add_argument("--host-ip",       default=HOST_IP,
                        help=f"Host IP sent in SYSTEM_STARTUP (default: {HOST_IP})")
    parser.add_argument("--output-dir",    default=".",
                        help="Directory to save raw image (default: .)")
    parser.add_argument("--dark",          action="store_true",
                        help="Also run dark/offset acquisition Script 1 before Script 0")
    parser.add_argument("--dark-only",     action="store_true",
                        help="Run dark acquisition only (Script 1, no X-ray needed). "
                             "Use this to test the full protocol flow without X-ray exposure.")
    parser.add_argument("--no-standby",    action="store_true",
                        help="Omit Script8 standby loop (use if Script8 blocks acquisition)")
    parser.add_argument("--two-exec",      action="store_true",
                        help="Execute Script7 alone first, wait for EXECUTION_COMPLETE, "
                             "then execute Script1. Tests whether GE host does two separate "
                             "EXECUTE_SCRIPTs in response to Script7's SendHostEvent(17). "
                             "Only applies with --dark-only.")
    parser.add_argument("--exec-timeout",  type=float, default=60.0,
                        help="Seconds to wait for EXECUTION_COMPLETE (default: 60)")
    parser.add_argument("--timeout",       type=float, default=5.0,
                        help="Socket receive timeout in seconds (default: 5.0)")
    parser.add_argument("--dump-scripts",  action="store_true",
                        help="Dump all script wire bytes as hex and exit")
    parser.add_argument("--skip-discovery", action="store_true",
                        help="Skip discovery loop; send SYSTEM_STARTUP once then proceed")
    parser.add_argument("--sensors",       action="store_true",
                        help="Connect and read detector sensors (accelerometer, temperature) "
                             "via cmd 0x7900. Independent of the image transfer path.")
    parser.add_argument("--probe-data",    action="store_true",
                        help="NON-DESTRUCTIVE: connect, then READ the detector's stored "
                             "DetectorInfo + HostList (registration/pairing state) + cal results "
                             "via the upload protocol (cmd 0x13/0x14). No writes. Use this to see "
                             "whether any host is registered and what HostId the detector expects.")
    parser.add_argument("--backup",        nargs="?", const=".", default=None, metavar="DIR",
                        help="NON-DESTRUCTIVE full backup: sweep ALL upload IDs (0x00-0xFF) and save "
                             "every readable data/config/calibration blob to "
                             "DIR/detector_backup_<timestamp>/ (default DIR='.'). Do this FIRST, "
                             "before any registration/flash write.")
    parser.add_argument("--enum-ids",      nargs="?", const="0x0-0x400", default=None, metavar="LO-HI",
                        help="Enumerate readable upload regions over a WIDE uploadId range via the cheap "
                             "0x13 probe (read-only). uploadId is 4 bytes but only 0x00-0xFF was swept; "
                             "a higher readable id may map to the SDRAM image buffers. Flags NEW ids. "
                             "Default range 0x0-0x2000.")
    parser.add_argument("--backup-range",  default=None, metavar="LO-HI",
                        help="With --backup, limit the ID sweep, e.g. --backup-range 0x40-0xA0 "
                             "(default 0x00-0xFF).")
    parser.add_argument("--smoke-test-write", action="store_true",
                        help="HostList write SMOKE-TEST: read the HostList (0x71) and write back the "
                             "IDENTICAL bytes to validate the write/commit path. DRY-RUN unless "
                             "--commit is also given. Targets the config region (not firmware); fully "
                             "backed up. Requires a prior --backup.")
    parser.add_argument("--commit",         action="store_true",
                        help="Arm the actual flash write for --smoke-test-write / --register-self "
                             "(otherwise dry-run). THIS WRITES THE DETECTOR'S FLASH.")
    parser.add_argument("--register-self",  action="store_true",
                        help="Register THIS host in the detector HostList as the PRIMARY host so it "
                             "will stream images to us (the suspected image-transfer gate). Appends "
                             "our entry, sets IndexToPrimaryHost, recomputes CRC, writes via 0x71. "
                             "DRY-RUN unless --commit. Reversible via backup upload_0x71_504.bin.")
    parser.add_argument("--host-mac",       default="00:6f:00:01:0a:3a", metavar="MAC",
                        help="MAC of the eth interface talking to the detector (for --register-self "
                             "HostId derivation). Default 00:6f:00:01:0a:3a.")
    parser.add_argument("--restore-hostlist", metavar="FILE", default=None,
                        help="Write a saved HostList blob (e.g. detector_backup_*/upload_0x71_504.bin) "
                             "back to the detector. The undo button. DRY-RUN unless --commit.")
    parser.add_argument("--sweep-acq",      action="store_true",
                        help="EXPERIMENT: sweep the dark acquisition across type_mode/transfer_mode "
                             "values and report which (if any) makes the detector push 0x0F pixels. "
                             "Non-destructive. Tests whether the acquisition transfer-mode is the gate.")
    parser.add_argument("--preview",       action="store_true",
                        help="EXPERIMENT: single-shot dark acquisition on the PREVIEW path "
                             "(transfer_mode=2). The detector advertises imageId=0x8000 + a 4-buffer "
                             "image; the 0x30000 reply is matched to that 4-buffer count (vs the usual "
                             "hardcoded 8) and we watch :6660 for a 0x0F push. One clean connection "
                             "(won't wedge the detector like --sweep-acq). Non-destructive. Run tshark "
                             "on host 192.168.1.30 in parallel.")
    parser.add_argument("--no-roe-init",   action="store_true",
                        help="With --sensors, skip the Script7 ROE init before reading")
    parser.add_argument("--parallel",      action="store_true",
                        help="Reply to the detector's 0x30000 status query with the cmd_type=0x99 "
                             "(ParallelImageTransfer) form -- [imageId:2][scriptId:2][numMissed:4]"
                             "[ids...] -- instead of the serial cmd_type=9. This is the only "
                             "status-reply path in the vendor implementation tied to the image port; use it to "
                             "test whether the firmware needs it to start pushing 0x0F pixels.")
    parser.add_argument("--xfer-mode",     type=int, default=None, metavar="N",
                        help="EXPERIMENT: single-shot dark acquisition at acquisition transfer_mode=N "
                             "(0-7), watching :5550/:6660 for a 0x0F push. transfer_mode is a real "
                             "firmware lever (tm=2 -> preview path). tm 4-7 were never tested. "
                             "Use --type-mode to also set the acquisition type byte. Non-destructive.")
    parser.add_argument("--type-mode",     type=int, default=1, metavar="N",
                        help="Acquisition type_mode byte for --xfer-mode (default 1=dark).")
    parser.add_argument("--force-wired",   action="store_true",
                        help="LEGACY EXPERIMENT, not needed: sweeps runtime transport config values "
                             "trying to force ethernet image egress. Ethernet is already the default; "
                             "use the normal capture path instead. Non-destructive (volatile config).")
    parser.add_argument("--set-cc",        nargs="?", const="auto", default=None, metavar="HOSTLIST",
                        help="EXPERIMENT: send DETECTOR_SET_CC (cmd 0x30) -- the runtime connection-context "
                             "arming handshake the original host software sends at connect (and flashpad never did) "
                             "-- then acquire a dark and watch for a 0x0F push. CC = first 64 bytes of the "
                             "HostList; pass a HostList .bin or omit to auto-find upload_0x71_*.bin. "
                             "Non-destructive (no flash write).")
    parser.add_argument("--recover",       action="store_true",
                        help="EXPERIMENT: dark acquisition, then on the 0x30000 query send the 0x98 "
                             "IMAGE_RETRIVAL with the CORRECT recoverLostImage payload "
                             "[imageId:2LE][scriptId:2LE] (prior runs wrongly sent [imagePort][hostPort]). "
                             "recoverLostImage is the firmware's 'resend a held image' path; the image is "
                             "acquired and held, so this asks the detector to (re)push its held buffers. "
                             "Sweeps a few imageId/scriptId orderings, watches :5550 and :6660. "
                             "Non-destructive.")
    parser.add_argument("--listen",        action="store_true",
                        help=f"Passively listen on port {HOST_REPLY_PORT} for 30s")
    parser.add_argument("--quiet",         action="store_true",
                        help="Suppress verbose output")
    args = parser.parse_args()

    if args.listen:
        print(f"Listening on port {HOST_REPLY_PORT} for 30 seconds...")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", HOST_REPLY_PORT))
        s.settimeout(1.0)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(65535)
                print(f"  {addr[0]}:{addr[1]} -> {len(data)}b  {data.hex()}")
            except socket.timeout:
                pass
        s.close()
        return

    if args.dump_scripts:
        scripts = {
            "Script7 ROEInit": build_script_7_roe_init(),
            "Script8 Standby": build_script_8_standby(),
            "Script0 StdAcq":  build_script_0_std_acq(),
            "Script1 DarkAcq": build_script_1_dark_acq(),
        }
        for name, data in scripts.items():
            print(f"\n=== {name} ({len(data)} bytes PDAP) ===")
            urp = make_urp_packet(data, reply_port=0, flag=URP_FLAG_COMMAND)
            print(f"Full UDP payload ({len(urp)} bytes):")
            for i in range(0, len(urp), 16):
                chunk = urp[i:i+16]
                hex_part = " ".join(f"{b:02X}" for b in chunk)
                asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                print(f"  {i:04X}  {hex_part:<48}  {asc_part}")
        return

    session = FlashPadSession(
        detector_ip=args.detector_ip,
        host_ip=args.host_ip,
        timeout=args.timeout,
        verbose=not args.quiet,
        parallel=args.parallel,
    )

    if args.smoke_test_write:
        session.smoke_test_hostlist(commit=args.commit,
                                    skip_discovery=args.skip_discovery)
        sys.exit(0)

    if args.register_self:
        session.register_self(commit=args.commit, skip_discovery=args.skip_discovery,
                              host_mac=args.host_mac)
        sys.exit(0)

    if args.restore_hostlist:
        session.restore_hostlist(args.restore_hostlist, commit=args.commit,
                                 skip_discovery=args.skip_discovery)
        sys.exit(0)

    if args.sweep_acq:
        session.sweep_acquisition()
        sys.exit(0)

    if args.preview:
        raw = session.preview_acquire()
        sys.exit(0 if raw else 1)

    if args.xfer_mode is not None:
        raw = session.preview_acquire(type_mode=args.type_mode, transfer_mode=args.xfer_mode)
        sys.exit(0 if raw else 1)

    if args.force_wired:
        raw = session.force_wired()
        sys.exit(0 if raw else 1)

    if args.set_cc is not None:
        path = args.set_cc
        if path == "auto":
            import glob as _glob
            cands = (_glob.glob("detector_backup_*/upload_0x71_*.bin") +
                     _glob.glob("upload_0x71_*.bin"))
            if not cands:
                print("No HostList backup found (upload_0x71_*.bin). Pass one explicitly to --set-cc.")
                sys.exit(2)
            path = sorted(cands)[0]
        cc = open(path, "rb").read()[:64]
        print(f"Using connection context from {path}: {cc[:16].hex()}...")
        raw = session.setcc_acquire(cc)
        sys.exit(0 if raw else 1)

    if args.recover:
        session._recover_98 = True
        raw = session.run_full_acquisition(
            output_dir=args.output_dir,
            dark_only=True,
            skip_discovery=args.skip_discovery,
            skip_standby=True,
            exec_timeout=args.exec_timeout,
        )
        sys.exit(0 if raw else 1)

    if args.enum_ids is not None:
        try:
            a, b = args.enum_ids.replace(" ", "").split("-"); lo, hi = int(a, 0), int(b, 0)
        except ValueError:
            print(f"Bad --enum-ids '{args.enum_ids}' (use e.g. 0x0-0x2000)"); sys.exit(2)
        session.enumerate_upload_ids(lo=lo, hi=hi, skip_discovery=args.skip_discovery)
        sys.exit(0)

    if args.backup is not None:
        lo, hi = 0x00, 0xFF
        if args.backup_range:
            try:
                a, b = args.backup_range.replace(" ", "").split("-")
                lo, hi = int(a, 0), int(b, 0)
            except ValueError:
                print(f"Bad --backup-range '{args.backup_range}' (use e.g. 0x40-0xA0)")
                sys.exit(2)
        session.backup_detector_data(out_dir=args.backup, lo=lo, hi=hi,
                                     skip_discovery=args.skip_discovery)
        sys.exit(0)

    if args.probe_data:
        session.probe_detector_data(skip_discovery=args.skip_discovery)
        sys.exit(0)

    if args.sensors:
        ok = session.read_sensors(skip_discovery=args.skip_discovery,
                                  roe_init=not args.no_roe_init)
        sys.exit(0 if ok else 1)

    image_data = session.run_full_acquisition(
        output_dir=args.output_dir,
        do_dark=args.dark,
        dark_only=args.dark_only,
        skip_discovery=args.skip_discovery,
        skip_standby=args.no_standby,
        two_exec=args.two_exec,
        exec_timeout=args.exec_timeout,
    )

    if image_data:
        print(f"\nAcquisition complete: {len(image_data)} bytes received")
        sys.exit(0)
    else:
        print("\nAcquisition failed -- see log above")
        sys.exit(1)


if __name__ == "__main__":
    main()
