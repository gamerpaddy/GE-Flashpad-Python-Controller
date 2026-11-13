# Detector simulator + bench test tools

PC-side tooling that lets the ESP32-S31 controller be tested **without the real FlashPad**: the PC plays the detector
over the direct Ethernet cable. Python 3.11, numpy for the synthetic frames, everything else stdlib. Run from
`esp32s31/tools/`; the tools import the *real* reference host `../../flashpad_acquire.py` (override with `FLASHPAD_REPO`).

| File | What it is |
|---|---|
| `flashpad_sim.py` | UDP detector simulator (binds `:8100`, replies/events/image datagrams all leave from that socket). Self-checks every packet the host sends against the real `flashpad_acquire.py` builders. HTTP ground-truth side channel on `127.0.0.1:8199`. `--selftest` drives the *existing* Python host against it on loopback. |
| `jumbo_blast.py` | 2048 x 4104 B UDP burst from `:8100` to `<host>:6660` (EMAC jumbo-frame spike) + a loopback sink. |
| `e2e_test.py` | Drives the ESP over its HTTP API (`docs/API.md`) and compares the returned frame with the simulator's ground truth. `--sim-only` replaces the ESP by a built-in *software ESP* so the simulator + scenarios can be self-tested without hardware. |

## 1. Quick start

```bash
# no hardware at all
python flashpad_sim.py --selftest            # existing Python host (FlashPadSession) vs the sim on loopback + negative checks
python jumbo_blast.py --selftest             # loopback sender + sink
python e2e_test.py --sim-only                # simulator + software ESP, scenarios a..h (~1 min incl. the 100-capture soak)

# bench (ESP on 192.168.1.1, this PC on 192.168.1.30)
python flashpad_sim.py --bind-ip 192.168.1.30 --host-ip 192.168.1.1        # leave running in terminal 1
python e2e_test.py --esp http://192.168.1.1                                 # terminal 2 (needs the HTTP API of docs/API.md)
python jumbo_blast.py 192.168.1.1 --bind-ip 192.168.1.30 --rate 100         # EMAC jumbo spike (stop the simulator first: both use :8100)
```

## 2. Bench setup

```
 PC  "Flashpad-ETH1" 192.168.1.30/24          ESP32-S31 Ethernet 192.168.1.1/24
      detector simulator UDP :8100  <---- cable ---->  host (UDP :5550 ctrl, :6660 images)
```

* **IPs**: PC `192.168.1.30/24` (= the detector), ESP `192.168.1.1/24`. Simulator defaults: `--bind-ip 192.168.1.30 --host-ip 192.168.1.1`.
* **Link speed 100 Mbit full duplex** on both ends. The real detector drops image frames when the host link is 1000; the ESP forces 100 (`adv100fd`/`forced100fd`);
  set the PC NIC to *100 Mbps Full Duplex* (Device Manager -> NIC -> Advanced -> "Speed & Duplex", admin) so the burst timing/back-pressure resembles the real thing. The simulator also paces the image push to
  ~100 Mbit/s wire rate itself (`--rate-mbps`, `--burst` = no pacing, i.e. NIC line rate).
* **MTU 5000** on the PC NIC (image datagrams are 4104 B UDP = ~4146 B frames; with MTU 1500 Windows would IP-fragment each datagram into 3 frames and the test would no longer exercise jumbo frames):
  `netsh interface ipv4 show subinterfaces` shows the MTU; set with `netsh interface ipv4 set subinterface "Flashpad-ETH1" mtu=5000 store=persistent` (admin).
  Current bench state (reported by the firmware session): `Flashpad-ETH1` is already 192.168.1.30/24, 100 Mbit, MTU 5000. **None of these tools change adapter settings.**
* **Windows firewall.** If the NIC is classified *Public*, unsolicited inbound UDP to `:8100` (the ESP's SYSTEM_STARTUP) is blocked and the first Python start pops a
  "allow python on public networks" dialog. Options, best first:
  1. one-time admin rule: `New-NetFirewallRule -DisplayName "FlashPad sim udp8100" -Direction Inbound -Protocol UDP -LocalPort 8100 -RemoteAddress 192.168.1.1 -Action Allow -Profile Any`
  2. make the adapter Private: `Set-NetConnectionProfile -InterfaceAlias "Flashpad-ETH1" -NetworkCategory Private`
  3. `--punch`: the simulator sends one harmless bare ACK `[0][1]` *out of* `:8100` to `192.168.1.1:5550/6660` at start. Windows' stateful UDP filtering then
     treats the ESP's packets from `5550` -> `8100` as return traffic for ~30-60 s. This is also why every datagram the simulator emits leaves from the one bound `8100` socket.
  Symptom of a blocked inbound path: the ESP keeps sending SYSTEM_STARTUP and the simulator log stays silent; `GET /truth` shows `session.state = "no-session"`.
* The simulator binds a *specific* IP, so it receives the ESP's **unicast** SYSTEM_STARTUP (the ESP sends unicast + broadcast; Windows does not deliver `192.168.1.255` broadcasts to a socket bound to a unicast address). Use `--bind-ip 0.0.0.0` to receive broadcasts too.
* Port `8100` must be free: stop other senders on `192.168.1.30:8100` (e.g. `jumbo_blast.py`, which also sends from `:8100`) before starting the simulator, or use `jumbo_blast.py --src-port`.
* Loopback self tests use `127.0.0.1` everywhere: `--bind-ip 127.0.0.1 --host-ip 127.0.0.1`.

## 3. `flashpad_sim.py`

### What it models (documented behaviour, tags as in `docs/PROTOCOL.md`)

| Host does | Simulator does |
|---|---|
| SYSTEM_STARTUP (cmd 1, SeqId 0) | bare ACK `[0][1]`, resets host SeqId/ROE state, then a **beacon** (cmd 1, plen 10 = detector IP + MAC, own SeqId counter starting at `--seq-start 100`) ~50 ms later and every 2 s. |
| PORT_SETUP (cmd 2) | bare ACK; replies go to the announced cmd port, image datagrams to the announced image port (default before PORT_SETUP: 48879 / 6660). |
| SIGNATURE (cmd 3) | cmd 3 reply, 54 B: MAC `40:F4:A0:00:78:4D`, serial `UA45829-7`, model `5340000-7`, fw `1.6.0.4.2.0.1.3`. |
| GENERIC_SCRIPT (cmd 5) | validated like the firmware, **appended** to a list (<= 32); reply bare ACK + cmd 5 `[status u8][count u32]`; status 1 = list full or payload > 511 B, 2 = validator rejected (bad command code, truncated, no `00 00` terminator). |
| EXECUTE (cmd 6) | bare ACK; cmd 6 reply `[u32 ms][status]` after 0.2 s: status 1 list empty, 2 busy; otherwise runs the **whole list in download order, clears the list, wipes the 8-slot image registry**. Repeat counts incl. `0xFFFF` (loop until the repeat event) are honoured, so a downloaded Script8 really hangs the "detector" (and is flagged as a protocol mismatch). |
| script commands | ROE = 0.33 s (0.005 s for `ROE(0,0)` pulses; Script7 therefore ~1.1 s), Delay, SendHostEvent -> cmd `0x40000 [id]`, WaitHostEvent. Script7 ends `0x40000 [17]` ~1.1 s after EXECUTE, then `0x10000` EXECUTION_COMPLETE (`01000000 01000000 01000000 00000000`). |
| acquisition command (code 1) | `0x40000 [1]` (window open) 0.21 s after the command starts (+ scrubs x scrubDur/26.3e6), window = `maxExpose / 26.3e6` s x `--window-scale` x `--time-scale`, `HOST_SCRIPT_COMMAND` (cmd 4, `01 00 00 00`) after the window opened ends it early; `0x50000 [0x10006,5,0]` at the end, image registered 0.43 s later, `0x10000` ~0.46 s after `0x50000`, first `0x30000 [imageId]` 0.7 s after that. `typeMode 1` = dark, anything else = X-ray. |
| 0x30000 answer cmd 9 `[n][block...]` | `n=0`: frees the image slot (later `0x30000` carries `DEADBEEF`); before the pull (before 0x98): keep-alive, nothing re-sent (so `[0..7]` keeps the image alive); after the pull: `1..256` ids -> exactly those blocks re-sent, `>256` -> whole image re-sent, then a new `0x30000`; retry counter (push = #1, each repair +1) beyond `--max-retries`+1 frees the image. |
| 0x41 `[scriptId]` | reply `[len=count*4+4][imageId]*count`; a 2nd `0x30000` follows (`--second-0x30000`). |
| 0x98 | reply `[status]` (0 ok, 1 no image / **0x41 not done first**), then 2048 datagrams `[imageId u32][blockIndex u32][4096 B]` = 4104 B, block b = row b, paced to ~100 Mbit/s, source port 8100, destination = announced image port. |
| 0x0A / 0x0B | abort: stop the running script, clear list + registry + push; reply `00000000`. |
| un-ACKed detector data packet | re-sent every `--retx-interval 0.5` s, up to `--max-retx 3` times, then counted in `unacked`. |
| host SeqId <= last seen | silently dropped, not ACKed, counted in `seq_drops`. |

Frames are deterministic per (type, seed): dark ~1789 ADU with row/column/pixel fixed pattern + noise; X-ray = dark + chest-like phantom (~+1400 ADU open field, lead "L"
marker top-left, step wedge bottom-left); dead rows 48/1093 and column 783 (~30 ADU). Frame seed = `--seed` + capture index (use `--fixed-seed` for identical frames).
Image id = `(scriptId << 16) | counter` (Script0 -> `0x00000000`, Script1 -> `0x00010000`).

### Self check (the reason the ESP is tested with it)

Every datagram from the host is parsed and **rebuilt with the real builders** (`make_urp_packet`, `build_system_startup`, `build_port_setup`, `build_signature_request`, `build_generic_script` +
`pack_acquisition/roe_command/send_host_event/wait_for_host_event/delay` incl. a comparison of Script7 with `build_script_7_roe_init()`, `build_execute_script`,
`build_image_xfer_status_reply`, `build_image_retrival_request`, `build_image_retrival`; `make_pdap` for cmd 4/7/0x0A/0x0B which have no builder). Not byte-identical ->
`protocol_mismatches++` with a hexdump of both. Also counted as mismatch: unknown commands, wrong lengths, STARTUP/PORT_SETUP announcing a port other than the socket they are sent from
(a byte-order swap is byte-identical when rebuilt, so this is checked semantically), packets not sent from 5550 / announced ports, infinite-loop scripts, malformed bare ACKs.
`flow_violations` = behaviour outside the documented flow: 0x98 without 0x41, `cmd 9 n=0` while blocks are still missing on the wire, event 1 before the window opened, ids >= 2048, retry counter exhausted.

### HTTP side channel (`--http-host 127.0.0.1 --http-port 8199`, 0 disables)

* `GET /truth` - `crc32` (8 hex) + `crc32_int` of the last registered image, `image_id`, `frame_type`, `frame_seed`, `frame_mean/std`, `last_acquisition` (type, window ticks, scrubs, script id as the host sent them), counters
  `executes execute_rejects scripts script_rejects captures protocol_mismatches flow_violations seq_drops stray_acks acks_dup unacked retx_pkts blocks_sent blocks_sent_last_push drops pushes cmd9_selective cmd9_whole cmd9_free cmd9_prepull resend_rounds images_freed needs_roe_rejects aborts beacons_sent beacon_acks startups send_errors`,
  `mismatch_log`, `flow_log` (last 40), `session` (host ip, ports, SeqIds, busy, stuck, roe_done, registry, `state`), `config`.
* `GET /frame` - the ground-truth frame (8 MiB u16 LE) for pixel-level diffs; `GET /log` - last 2000 log lines.
* `POST /config` (JSON, applies to following captures): `drop_rate`, `drop_blocks`, `drop_retx_rate`, `drop_ctrl_rate`, `latency_ms`, `jitter_ms`, `needs_roe`, `needs_roe_mode`,
  `stuck_busy_after`, `stuck_busy` (true/false = set/clear now), `reset_roe` (true = forget the ROE init, the next acquisition without Script7 reacts per `needs_roe`), `rate_mbps`, `time_scale`, `window_scale`,
  `max_retries`, `retx_interval`, `second_30000`, `prepull_resend`, `deadbeef`, `suppress_window_event`, `early_close`, `seed`, `fixed_seed`.
* `POST /reset` `{"counters":true,"session":true}`.

### Options (all `--help`)

`--bind-ip --host-ip --cmd-port --detector-ip --mac --seq-start` | `--time-scale` (all model delays and the window; 0.1 = 10x faster) `--window-scale --rate-mbps --burst --timing KEY=SECONDS` |
loss: `--drop-blocks N` (first push of each image) `--drop-rate --drop-retx-rate --drop-seed --drop-ctrl-rate --latency-ms --jitter-ms` |
unknown behaviour: `--needs-roe [--needs-roe-mode status2|silent|badframe] [--keep-list-on-reject] --stuck-busy N --max-retries --retx-interval --max-retx --beacon-interval --beacon-giveup N --second-0x30000 before|after|none --prepull-resend --no-deadbeef --suppress-window-event --no-early-close` |
`--seed --fixed-seed --punch --quiet --log-file --selftest`.

### Where the simulator guesses (no hardware evidence; every item is switchable or documented)

1. **Where bare ACKs go**: to the *system port* announced in SYSTEM_STARTUP (per the `run_full_acquisition` docstring), not to the packet's source port. (Sending them to the source made the existing host's image socket collect an extra 8 B record after its 0x98-from-:6660; real captures do not show that.) PDAP replies/events go to the PORT_SETUP cmd port (48879 before it), beacons to the STARTUP port.
2. **SYSTEM_STARTUP** restarts the host SeqId state (last seen := 0), forgets the ROE state, aborts a push in flight; a repeat within 0.5 s is only ACKed. Cmd/image ports persist across STARTUPs.
3. **Timing constants** other than the live-measured ones (EXECUTE reply +0.2 s, window open +0.21 s, Script7 ~1.1 s, EXECUTION_COMPLETE +0.46 s after the window, first 0x30000 +0.7 s): ROE 0.33 s, post-push 0x30000 after 50 ms, DEADBEEF after 0.2 s, push start 5 ms after the 0x98 reply, retransmit interval 0.5 s. (`--timing`)
4. **cmd 5 status reply** is always sent (docs say "optionally"); a rejected EXECUTE (status 1/2) answers after the same 0.2 s.
5. **cmd 9 before 0x98** = keep-alive (`--prepull-resend` makes it re-send those blocks, which stray-blocks the ESP before 0x98). The "second 0x30000" after 0x41 is sent *before* the 0x41 reply (`--second-0x30000 after|none`).
6. **Retry counter**: push counts as #1 and each repair round +1; the image is freed when it exceeds `--max-retries`+1 (i.e. push + 10 repairs are served). Exact off-by-one on the real detector is unknown.
7. **needs-ROE** is an assumption to exercise the ESP's fast->full fallback: `status2` rejects the EXECUTE (list consumed unless `--keep-list-on-reject`), `silent` accepts it and never sends 0x40000[1]/0x10000, `badframe` completes but sends an all-0xFFFF frame with EXECUTION_COMPLETE word 3 = 0. `--stuck-busy N` makes every EXECUTE after N captures return status 2 until `/config stuck_busy=false` (0x0A does not clear it).
8. Signature words 1-4 (`1,0,0,0`), EXECUTE reply time field (ms since sim start), abort reply payload, HOST_SCRIPT_COMMAND has only a bare ACK, `0x07` poll returns the last EXECUTION_COMPLETE payload.
9. Frame content (pixel values, phantom) is synthetic; real detectors have different noise, offset and gain structure.

## 4. `jumbo_blast.py`

```
python jumbo_blast.py 192.168.1.1 --bind-ip 192.168.1.30 --rate 100          # 2048 x 4104 B at 100 Mbit/s wire rate (0.68 s)
python jumbo_blast.py 192.168.1.1 --bind-ip 192.168.1.30                     # line-rate burst
python jumbo_blast.py 192.168.1.1 --repeat 5 --gap 1 --verify-from-esp http://192.168.1.1
python jumbo_blast.py --sink --bind-ip 192.168.1.30                           # receiver: counts datagrams/lengths/blocks (check the NIC path from the ESP side)
python jumbo_blast.py --selftest
```
Datagram = `[imageId u32][blockIndex u32][4096 B pattern]` (pattern `index` = every pixel is the block index, so the receiver can check content), source port 8100, destination port 6660. Prints sent
count, time, payload and wire Mbit/s. `--verify-from-esp URL` snapshots `/api/status` and `/api/log` before/after and reports any counter whose name contains block/rx/frame/trunc/drop that moved, flags a block counter that advanced
by exactly N, and fails when the device log gained a "truncated" message (the stock IDF EMAC driver message for >1522 B frames). The current spike firmware has no HTTP API, so there use the serial log counters instead.

## 5. `e2e_test.py`

```
python e2e_test.py --esp http://192.168.1.1                  # all scenarios a..h against the real ESP + the running simulator
python e2e_test.py --esp http://192.168.1.1 --only a,c,d     # subset ;  --list shows ids ; m = optional setup-mode sweep
python e2e_test.py --esp http://192.168.1.1 --spawn-sim "--bind-ip 192.168.1.30 --host-ip 192.168.1.1"   # start the simulator for you
python e2e_test.py --sim-only [--sim-args "--second-0x30000 after --latency-ms 20 --drop-ctrl-rate 0.03"]
```

| id | scenario | pass criteria |
|---|---|---|
| a | dark capture | state done; `GET /api/frame` 8 MiB; its CRC32 (and `X-Frame-Crc32`) equals the simulator's; the simulator saw type dark, the requested window ticks and scrubs |
| b | X-ray, master OFF | sequence completes, CRC ok, `relay_on` never true in any status poll, `xray.fires` unchanged |
| c | 1 / 3 / 5 % lost blocks; + 5 % lost and 30 % of the repairs lost | CRC ok, selective cmd 9 used (>=1 round), no whole-image resend; the lossy variant needs >=2 rounds |
| d | 400 blocks lost in the first push | CRC ok, cmd 9 with >256 entries -> whole-image resend seen |
| e | `needs_roe` simulator + `setup_mode=fast` | warm-up capture ok, simulator forgets ROE init, next capture: simulator rejected an EXECUTE (status 2), `capture.fast_fallback = true`, capture still ok, CRC ok |
| f | `--soak-n 100` dark captures (`--soak-window-ms 200`) | every capture ok + CRC equals the simulator's (full frame download every 10th), free internal heap drift <= `--heap-tol` (8 KiB), free PSRAM drift <= `--psram-tol` (64 KiB) after the warm-up capture |
| g | abort during an X-ray window, then capture | state `aborted`, relay off, detector back to `ready` within 90 s, next dark capture ok + CRC |
| h | protocol health (whole run) | `protocol_mismatches == 0`, `seq_drops == 0`, `unacked == 0`, `flow_violations == 0`, `stray_acks == 0`, `unknown_cmds == 0` |

Prints a PASS/FAIL table and exits non-zero on any failure. The e2e resets the simulator's counters at start and restores its fault-injection config after each scenario.

**`--sim-only`**: spawns a loopback simulator (`--time-scale 0.1 --window-scale 0.1 --rate-mbps 400`, log in `%TEMP%\flashpad_sim_e2e.log`) and replaces the ESP by `RefHost`, an in-process host that implements the *documented ESP
behaviour* with the real builders (persistent session, ACK thread, two-phase EXECUTE, 0x40000[1], cmd 9 `[0..7]`, 0x41/0x98, bitmap, selective/whole-image repair <= 10 rounds, fast->full fallback, abort + 0x0A + re-discovery).
It proves the simulator and the scenario logic; it says nothing about the firmware, and the heap check is "n/a" there. It uses UDP ports 5550/6660 on the PC, so do not run it while the real ESP bench is using them through the same sim.

## 6. What was verified (2026-11-13, Windows 10, Python 3.11, loopback)

* **Existing Python host vs the simulator**, `python flashpad_sim.py --selftest`: (1) the real `FlashPadSession.run_full_acquisition(dark_only=True, two_exec=True)` (= `flashpad_acquire.py --dark-only --two-exec`) and (2) the GUI / `capture_minimal.py`
  sequence (Script7 -> Script0 X-ray window -> `receive_image`, serial trigger omitted): both receive a raw capture of exactly 2048 x 4104 - 16 B (the "first record 16 B short" of the real captures), `protocol_mismatches = 0`, `seq_drops = 0`, `unacked = 0`, no flow violations;
  after the GUI's `reassemble()` **rows 1..2047 are bit-exact** with the simulator's frame (`/frame`). Row 0 is *not* exact, by the existing host's own bug (block 0 looks like a URP/PDAP packet to `receive_image`, which strips 8 payload bytes, so row 0 comes out shifted by 4 pixels with a zero tail); the self test checks that exact pattern. A *correct* receiver (the RefHost in `e2e_test.py`, 8 MiB CRC32) is bit-exact over the whole frame. The existing host also sends one stray bare ACK `[imageId][1]` (same bug) - counted as `stray_acks`, expected to be 0 for the ESP.
* 9 negative checks (wrong PORT_SETUP byte order, SeqId replay, bad script code, >511 B script, empty EXECUTE, 0x98 without image, un-ACKed beacon retransmit + give up, signature layout).
* `e2e_test.py --sim-only` (default, 100-capture soak): **a-h all PASS**, 110 acquisitions, 222 EXECUTEs, 227k blocks sent, 715 deliberately dropped, 0 mismatches / seq drops / un-ACKed / flow violations, 51 s soak. Also PASS with
  `--sim-args "--second-0x30000 after --latency-ms 15 --jitter-ms 10"`.
* Known behaviours that fail by design (they show the simulator finds weak hosts): with `--max-retries 3` the lossy-repair scenario exhausts the retry counter -> image freed, host times out, `flow_violations` reports "retry counter exhausted";
  with `--prepull-resend` the extra `0x30000` that follows the pre-pull re-send can arrive right after the 0x98 reply *before any block of the push*, and a host that answers every post-pull `0x30000` blindly (the RefHost does) sends a >256-id cmd 9 and triggers a
  needless whole-image resend - the firmware should only judge the bitmap once the stream has gone idle or all blocks are in (this mode is a hypothesis, off by default).
* `jumbo_blast.py`: `--selftest`, and `--rate 100` into `--sink` on loopback: 2048/2048 datagrams of 4104 B, one imageId, all blocks present, 0.683 s; `--verify-from-esp` ran against `mock_esp_server.py` (status + log reachable, no counter -> informational).
  **Not verified (no firmware API / session yet)**: `e2e_test.py --esp` against the real ESP, the simulator on the bench cable with the ESP's own session client, Windows firewall behaviour for an inbound ESP packet, the 100 Mbit forced-NIC pacing on the real link (the sim binds `192.168.1.30:8100` fine).
  `EspDriver` (HTTP side) was only smoke-tested against `mock_esp_server.py` (state polling worked; the mock's own frame thread crashed on a `None` pool, so no end-to-end CRC comparison was possible there).
