# FlashPad protocol spec used by the ESP32-S31 port

Condensed from `flashpad_acquire.py`, the two working captures in the session logs, and a read of the detector firmware
decompile (`ghidra_work/decomp_comms.c`). Tags: **[CODE]** = read in the Python repo, **[LIVE]** = seen on real hardware,
**[FW]** = firmware reading, *not yet verified on hardware*, **[INF]** = inference. Trust this file over the old comments in
`flashpad_acquire.py` / `PROTOCOL_FINDINGS.md` (see "Known wrong comments").

All integers little-endian unless marked BE. Detector = `192.168.1.30`, host = `192.168.1.1` (the detector replies to a *stored*
address, so the host must own 192.168.1.1 or announce its own IP in SYSTEM_STARTUP).

## Framing

```
URP  (8 B): [SeqId:u32][CmdFlag:u32]      CmdFlag 0 = data packet (PDAP follows); CmdFlag 1 = bare ACK (8 B total, no body)
PDAP (8 B + payload): [cmd:u32][payload_len:u32][payload]
packet to detector = URP + PDAP, UDP to 192.168.1.30:8100, always sent FROM local port 5550
```
- Bare ACK = `[detSeq:u32][1:u32]`, echoing the SeqId of the packet being acknowledged. **Never ACK a bare ACK.**
- Host SeqId: SYSTEM_STARTUP uses 0. Then `seq = last_beacon_SeqId + 1` (only if larger than the current value), +1 per data packet sent.
  The detector silently drops SeqIds it has already seen. Bare ACKs consume no SeqId. [CODE]
- **Every detector data packet (CmdFlag 0) - beacons included - must be ACKed immediately**; the detector re-sends un-ACKed packets
  (replies were observed up to 3x). De-duplicate on `(cmd, payload)` *after* ACKing. There is no other keepalive. [CODE/FW]
- A detector reply has the same `cmd` as the request.

## Ports

| Port | Direction | Use |
|---|---|---|
| 8100 | host -> detector | every command (unicast and broadcast 192.168.1.255 for SYSTEM_STARTUP) |
| 5550 (host, UDP) | detector -> host | all replies/events after PORT_SETUP; also the *source* port of every host packet |
| 6660 (host, UDP) | detector -> host | image datagrams, source port 8100 |
| 48879 (0xBEEF) | detector -> host | where replies go **before** PORT_SETUP (default) |

## Session setup

1. **SYSTEM_STARTUP** (cmd 1, SeqId 0, CmdFlag 0), broadcast + unicast, repeated every 2 s up to 50 s: payload `[port:u16 BE][host_ip:4]`.
   Port used = 5550 (`15ae`). Packet: `00000000 00000000 01000000 06000000 15ae c0a80101` (22 B). (Note: the *default* argument of
   `build_system_startup` is 4500 = `1194`; the working code passes 5550.)
2. Detector -> bare ACK `[0][1]`, then a **beacon**: data packet, cmd 1, plen 10 = `[detector_ip:4][MAC:6]`. ACK it, sync SeqId. Discovery = first beacon
   (or a bare ACK after the timeout). The broadcast reveals the detector IP.
3. **PORT_SETUP** (cmd 2, plen 4): `[cmdPort:u16 BE][imgPort:u16 BE]` = `15ae 1a04` -> `020000000400000015ae1a04`. Reply: bare ACK with the same SeqId (or cmd-2 reply,
   plen 0). Must come before anything whose reply matters, otherwise replies go to 48879.
4. **SIGNATURE_REQUEST** (cmd 3, plen 0) -> reply cmd 3, plen 54: `MAC(6) 4xu32 serial(12) model(12) fw(8 bytes)`. Optional, display only.
   Live: `MAC=40:F4:A0:00:78:4D serial='UA45829-7' model='5340000-7' fw=1.6.0.4.2.0.1.3`.

## Scripts [CODE + FW]

- **GENERIC_SCRIPT** (cmd 5): payload `[scriptID:u16][repeatCount:u16][repeatEvent:u32][packed cmds...][00 00]`. Reply: bare ACK, optionally cmd 5
  `[status:u8][count:u32]` (0 ok, 1 list full / >511 B, 2 validator rejected). **Check the status byte.** [FW]
- **EXECUTE_SCRIPT** (cmd 6, plen 0). Replies: bare ACK, then cmd 6 with plen 5 = `[u32 time/1000][status:u8]` -> **status is `payload[4]`** (0 ok,
  1 script list empty, 2 detector busy) [FW]. The Python logs `payload[0]` ("0xC4/0xC9/0xE3") which is the low byte of the time field - log all 5 bytes.
- **Scripts are not stored by ID** [FW, untested]: each GENERIC_SCRIPT *appends* to a list (<=32); **EXECUTE runs the whole list in download order and then
  clears it**; EXECUTE also clears the 8-slot image registry (the previous image must be fully pulled before the next EXECUTE). Script IDs are labels
  (they end up in the imageId). => the acquisition script must be re-downloaded before every EXECUTE.
- Repeat semantics: 0 or 1 = once, N = N times, 0xFFFF = loop until host event `repeatEvent`. **Never download Script8 (infinite standby loop)**.

Packed commands (first byte = code):

| Code | Cmd | Layout |
|---|---|---|
| 1 | Acquisition (17 B) | `01 typeMode imageId noScrubs scrubDur:u32 maxExpose:u32 tail:u32 transferMode` |
| 2 | ROE (14 B) | `02 respFlag timer:u32 roeCmd:u32 roeData:u32` (default timer 1,000,000 = `40420f00`) |
| 3 | SendHostEvent (5 B) | `03 id:u32` |
| 4 | WaitHostEvent (9 B) | `04 id:u32 timeout:u32` |
| 5 | Delay (5 B) | `05 us:u32` |

Time fields (scrub/maxExpose/tail) are in ticks of **~26.3 MHz** [LIVE-measured]: 250,000,000 ~ 9.5 s; 4,000,000 ~ 150 ms; 400,000 ~ 15 ms (GE's hardware-synced value).

Exact blobs (PDAP only, no URP), from the Python builders:
```
Script7 ROE-init (146 B):
 050000008a000000 0700 0000 00000000
 02 00 40420f00 50400000 0000d200      ROE 0x4050 <- 0x00D20000
 02 00 40420f00 02400000 02000000      ROE 0x4002 <- 2
 02 00 00000000 00790000 10000000      ROE 0x7900 <- 16 (timer 0)
 05 50c30000                           delay 50000 us
 4x { 02 00 40420f00 00000000 00000000 ; 05 10270000 }
 03 11000000                           SendHostEvent(17)
 0000
full hex (as printed by `fp.build_script_7_roe_init().hex()`, 146 B):
 050000008a0000000700000000000000020040420f00504000000000d200020040420f00024000000200000002000000000000790000100000000550c30000020040420f0000000000000000000510270000020040420f0000000000000000000510270000020040420f0000000000000000000510270000020040420f000000000000000000051027000003110000000000
(authoritative source is always the Python builder; the simulator must compare against it, never against this text)

Script0 X-ray (type 0, window 250,000,000 ticks, scrubs 0, scrubDur 50000, tail 250000, transfer 0) (40 B):
 05000000 20000000 | 0000 0000 00000000 | 01 00 01 00 50c30000 80b2e60e 90d00300 00 | 05 10270000 | 0000
Script0 dark (typeMode 1, window 4,000,000, tail 50000, scrubDur 10000 as Script1) : use `build_script_1_dark_acq` or the GUI's Script0 with typeMode 1
```
The GUI's "dark" button actually runs the same Script0 with the *UI's* typeMode and simply never fires the source. The ESP follows the GUI: X-ray and dark
both use `build_generic_script(0, 0, 0, [pack_acquisition(type_mode, image_id=1, no_scrubs, scrub_duration=50000, max_expose_time=window, tail_time=250000, transfer_mode=0), pack_delay(10000)])`.

## Detector -> host messages during a capture

| cmd | Meaning |
|---|---|
| 0x00000001 | beacon (plen 10) |
| 0x00000006 | EXECUTE reply (plen 5, status = byte 4) |
| 0x00040000 `[u32 eventId]` | host-event notice. `[17]` = Script7's SendHostEvent(17) (~1.09 s after EXECUTE); **`[1]` ~0.21 s after EXECUTE = the integration window is open** [LIVE+FW] |
| 0x00050000 (plen 12) | window expired (`[0x10006][5][0]`), ACK only |
| 0x00010000 | EXECUTION_COMPLETE, 16 B payload `[numScripts][numExecuted][u32][u32]` (live: `01000000 01000000 01000000 00000000`) |
| 0x00030000 | image_xfer_status query, 4-byte payload (`[imageId:u16][scriptId:u16]`, e.g. `00000100`); a payload of `DEADBEEF` = slot freed, ignore |
| 0x00000007 | NOT unsolicited - host-pollable status query returning the same 16 B as 0x10000 |

## Image transfer

- **cmd 0x30000 (detector) -> host answers cmd 9**: `[numMissed:u32][blockIdx:u32]*n` (serial form), where entries are **block indexes** (low 16 bits used) [FW]:
  n = 0 -> "all received", the detector **frees the image slot**; 1..256 -> detector re-sends exactly those blocks, then sends a new 0x30000; >256 -> re-sends the
  whole image; a per-image retry counter frees the image after ~11 pushes/retransmits.
  Proven flow answers the **first** 0x30000 (before the push) with `[0..7]` (`0900000024000000 08000000 00000000 ... 07000000`) which keeps the image alive; **never answer
  n=0 until all 2048 blocks are present**.
- **cmd 0x41** `[scriptId:u32]` (payload ignored by firmware) -> reply `[len=count*4+4:u32][id:u32]*count`; `04000000` = zero images, `0800000000000100` = one image id 0x00010000.
  0x41 is a hard prerequisite of 0x98.
- **cmd 0x98** `[imagePort:u16 LE][hostPort:u16 LE]` = `041a ae15` (payload ignored by firmware; the push starts right after the reply) -> reply `[status:u32]` 0 ok, 1 no such image.
  The GUI sends it from the image socket after a 4-byte probe (experiment); the firmware ignores the source port, so sending from 5550 should work [INF].
- **Datagram** (detector :8100 -> host :6660, no URP header, no ACK): `[imageId:u32][blockIndex:u32][4096 B pixels, u16 LE]` = **4104 B UDP payload, one unfragmented ~4146 B
  Ethernet frame (jumbo)**. `blockIndex` = `word & 0xFFFF`, **0-based 0..2047**, block b = image row b, 2048 datagrams = 8 MiB = 2048 x 2048 x 16 bit, row 0 on top.
  imageId = `(scriptId<<16) | counter` (Script 0 -> 0x00000000, Script 1 -> 0x00010000). Push takes ~0.78 s.
  (The Python's "first datagram is 16 B short" is a host parsing bug: `[imageId][0]` looks like a URP data packet. A correct receiver gets 4104 B for block 0 too.)

## One capture, ordered (X-ray, `session` mode)

```
[session already up: STARTUP -> beacon -> PORT_SETUP; beacons ACKed continuously]
1  H->D  GENERIC_SCRIPT Script7          D->H bare ACK (+ optional cmd5 status)
2  H->D  EXECUTE                          D->H bare ACK, cmd6 reply (+0.2 s), 0x40000[17] (+1.09 s), 0x10000 EXECUTION_COMPLETE      (~1.1-1.3 s)
3  H->D  GENERIC_SCRIPT Script0 (open :6660 first)   D->H bare ACK
4  H->D  EXECUTE                          D->H bare ACK, cmd6 reply (+0.2 s), 0x40000[1] (+0.21 s = window open)
5  H     fire relay after fire_delay      (proven Python: right after the EXECUTE ack)
6  D->H  0x50000 at window expiry, then 0x10000 ~0.46 s later (ACK both)
7  D->H  0x30000 (+0.7 s)                 H->D cmd 9 [0..7]  (keeps the image alive)
8  H->D  0x41                              D->H reply 0800000000000000 / ..00000100 (a 2nd 0x30000 arrives within ms: ACK only)
9  H->D  0x98                              D->H reply 00000000
10 D->H  2048 datagrams on :6660 over ~0.78 s
11 D->H  0x30000                           H->D cmd 9 n=0 if bitmap complete, else cmd 9 with missing indexes
total = window + ~2.3 s (+ ~1.2 s for Script7); dark: same without step 5
```
`fast` mode (UNTESTED, opt-in) skips steps 1-2 after the first capture of a session.

## Recovery / abort (all [FW], untested)
- opcode 0x0A (plen 0): abort script list + reset image state, replies `00000000`; opcode 0x0B same but waits for busy to clear.
- A new SYSTEM_STARTUP sets an abort flag for an in-progress push [INF].
- HOST_SCRIPT_COMMAND opcode 4 payload `01 00 00 00` = host event 1: ends the integration window early (send only after 0x40000[1]; pending state is cleared at each EXECUTE).
- If the detector stays busy (flag `DAT_0c07c844` stuck): power-cycle. Nothing here writes detector flash.

## Link requirements
Host link must be **10/100 Mbit** (the detector selects ethernet "net 1" in power state 3 and silently drops every image frame at 1000). GE's own host script
forces `speed 100 duplex full autoneg off`, MTU 5000.

## Known wrong comments in the old Python
"pixel data streams during EXECUTE as 0x0F frames" (it is pull based: 0x41 then 0x98, raw datagrams); "8 buffers / No of Images = 8" (misreading, cmd 9 carries block
indexes); "Script0 will not complete without X-ray" (it completes, frame is unexposed); "blockIndex is 1-based" (0-based); "state 17 = ready for X-ray" (the ready signal is
0x40000[1]); "EXECUTE_SCRIPT_STATUS during execution" (cmd 7 is poll-only).
