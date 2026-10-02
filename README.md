# GE FlashPad Python Controller

Use a **GE FlashPad** flat-panel X-ray detector from any computer, over plain Ethernet.

These detectors were sold bolted to a GE Optima XR200/220 AMX mobile X-ray system. Separated
from that console the hardware still works perfectly, but nothing can talk to it. This project
talks to it: connect, configure, acquire, and pull a full **2048 × 2048 16-bit image** over a
normal network cable.

No vendor software, no console, no licence dongle, and no modification to the detector.

![status](https://img.shields.io/badge/image%20readout-working-brightgreen)

📖 **Full write-up, teardown photos and protocol reference:**
[GE Medical FlashPad Digital X-ray Detector on the Recessim wiki](https://wiki.recessim.com/view/GE_Medical_Flashpad_Digital_Xray_Detector)

That page covers the hardware in depth — internal photos, the tether cable pinout, sensor
tables, the shock/drop event log, flash layout and the complete protocol documentation. This
repository is the software side of it.

---

## What it does

* Discovers the detector and completes the control handshake
* Reads identity, firmware version, power rails and all internal sensors
* Backs up the detector's entire 64 MB internal flash over the network (read-only)
* Arms the panel, triggers an X-ray source, and receives the image
* Reassembles the 2048 datagrams into a 2048 × 2048 16-bit frame
* Subtracts a dark frame and repairs dead rows and columns
* Saves raw, 16-bit TIFF and PNG; views with pan/zoom, CLAHE, colour palettes and cropping

## What you need

**Hardware**

| Item | Notes |
|---|---|
| GE FlashPad detector | Tested on a `5340000-7`, firmware `1.6.0.4.2.0.1.3` |
| 12 V supply + Ethernet from the tether cable | The tether carries both; the panel does not need its battery |
| A dedicated Ethernet port on your PC | Strongly recommended — the required link settings affect the whole adapter |
| An X-ray source you can trigger | Only for real exposures. Everything else works without one. |
| Arduino (optional) | For software-triggered exposures. See [`arduino/`](arduino/). |

**Software**

```
python -m pip install -r requirements.txt
```

Python 3.8+. `flashpad_acquire.py` and `capture_minimal.py` need only the standard library;
the GUI additionally uses numpy, Pillow, OpenCV and pyserial.

---

## Setup — read this part carefully

Three network settings are **mandatory**. If any one is wrong you get a perfectly working
control connection and **no image data at all**, with no error message anywhere. This is the
single most common reason people conclude the detector is broken.

| Setting | Required value | Why |
|---|---|---|
| Host IP | `192.168.1.1/24` | The detector sends replies to a stored address, not back to whoever asked. From any other IP you get silence. |
| Link speed | **100 Mbit, full duplex, autoneg off** | The detector checks the negotiated PHY speed and refuses to use a link that does not match its expectation. On a mismatch it silently discards every image frame while control traffic keeps working. |
| Interface MTU | **5000 or larger** | Image datagrams are 4104 bytes. At the default MTU of 1500 your network card drops them before they ever reach the program. |

The detector's default address is `192.168.1.30`.

**Linux**

```bash
sudo ip addr add 192.168.1.1/24 dev eth0
sudo ip link set eth0 mtu 5000
sudo ethtool -s eth0 speed 100 duplex full autoneg off
```

**Windows**

1. Adapter properties → IPv4 → static `192.168.1.1`, mask `255.255.255.0`
2. Adapter properties → *Configure…* → **Advanced**:
   * *Speed & Duplex* → **100 Mbps Full Duplex**
   * *Jumbo Frame* / *Jumbo Packet* → **5000** or larger (9014 is fine)
3. Administrator rights are needed for step 2.

> **Windows firewall:** if Windows classifies the adapter as a *Public* network it blocks
> unsolicited inbound UDP and you will see no replies. Either set the network to *Private*,
> or allow Python through the firewall on that adapter.

Check it works:

```bash
ping 192.168.1.30
python flashpad_acquire.py --probe-data
```

---

## Quick start

**1. Back up the detector first.** This is read-only and takes a few minutes. Do it before
anything else, so you always have a way back.

```bash
python flashpad_acquire.py --backup
```

**2. Test the whole chain without any X-rays.**

```bash
python flashpad_acquire.py --dark-only --two-exec
```

This arms the panel, reads it out and saves a raw capture. No radiation involved. If this
produces a file, everything from here on is just configuration.

**3. Turn the raw capture into an image.**

```bash
python reassemble.py
```

With no argument it uses the newest capture in its own folder and writes a 2048 × 2048
16-bit raw plus a 16-bit PGM.

**4. For real exposures, use the GUI.**

```bash
python flashpad_capture_gui.py
```

Set an exposure time, press **CAPTURE**. It arms the detector, fires the source, receives,
corrects and displays the result in one step.

---

## The tools

### `flashpad_capture_gui.py` — the main application

Everything in one window.

* **CAPTURE** arms the detector, triggers the source over serial, receives with a live
  progress bar, and produces a finished image
* **Capture DARK** does the same with no trigger. The result is stored in `darks/`, loaded
  automatically as the active offset correction, and older darks are cleaned up
* **Test link (CW0)** proves the Arduino is listening without firing anything
* Dead row/column repair, pan/zoom, a draggable crop box, CLAHE, nine palettes, invert
* **Save PNG (crop)** writes what you see; **Save TIFF (crop)** keeps the original 16-bit values
* **? Explain every setting** documents every control in the app

Saved per capture: raw datagram stream, 2048 × 2048 16-bit raw, 16-bit TIFF, PNG.

### `flashpad_acquire.py` — protocol library and CLI

The engine the other tools build on, and a command-line tool in its own right.

Read-only / diagnostic:

| Option | Does |
|---|---|
| `--probe-data` | Reads identity and configuration — good first connectivity test |
| `--sensors` | All internal sensors and power rails in engineering units |
| `--backup [DIR]` | Dumps every readable data blob including the full 64 MB flash |
| `--listen` | Passively watches what the detector sends |
| `--dump-scripts` | Prints the acquisition script encoding, no network needed |

Acquisition:

| Option | Does |
|---|---|
| `--dark-only --two-exec` | Full readout test, no X-ray needed |
| `--two-exec` | Required for any acquisition — see [How it works](#how-it-works) |
| (no flag) | Standard acquisition |

Writing (optional, affects detector flash — every write is a dry run unless you add
`--commit`, and `--restore-hostlist` is the undo):

| Option | Does |
|---|---|
| `--register-self` | Adds this host to the detector's host list |
| `--restore-hostlist FILE` | Writes a saved host list back |
| `--commit` | Arms the actual write |

Host registration is **not** required for image transfer — the detector tested here has no
host registered and streams images normally.

### `capture_minimal.py` — the 26-line version

A complete capture with no features, for reading rather than using. If you want to port this
to another language, start here.

### `reassemble.py` — raw to image

Turns a captured datagram stream into a 2048 × 2048 16-bit raw plus a 16-bit PGM. Run with no
argument for the newest capture, or pass a filename or part of one.

### `arduino/xray_trigger.ino` — example source trigger

Minimal sketch for triggering an X-ray source and rotating a sample stage over serial
(9600 baud):

| Command | Does |
|---|---|
| `C<ms>` | Pulls pin 13 high for `<ms>` milliseconds — fires the source. Sends no reply. |
| `CW<deg>` / `CCW<deg>` | Rotates a stepper, replies `OK` |

`CW0` rotates nothing and still replies `OK`, which makes it a safe way to test the link.
Adapt the sketch to your own hardware — it is an example, not a requirement.

---

## How it works

Three things are easy to get wrong and account for nearly all failures.

**1. The panel does not detect X-rays.** It is not an AED detector. You arm it, it opens an
integration window for a set time, and the exposure has to happen *inside* that window. If
the window expires you still get a complete frame — just an unexposed one, which is exactly
how the no-X-ray test works.

The window length is in ticks of a ~26 MHz clock:

| Ticks | Roughly | Use |
|---|---|---|
| 400,000 | 15 ms | What the original system uses, with a hardware-synchronised generator |
| 4,000,000 | 150 ms | Reasonable for software triggering |
| 250,000,000 | 9.5 s | Very forgiving, good for manual triggering and first tests |

A longer window collects more dark current, which raises the noise floor. Use the shortest
one your timing can reliably hit, and re-shoot your dark frame whenever you change it.

**2. Two separate execute commands are required.** The readout electronics init script must
run and complete *on its own* before the acquisition script is executed. Combined into one
execute, the detector returns status `0xE3`, never completes, and produces nothing. This is
what `--two-exec` does, and the GUI always does it.

**3. The image is not pushed automatically.** After the acquisition you have to ask for it:
first a request that makes the detector list the images it is holding, then a retrieval
command that starts the transfer. Asking in the wrong order fails. Both tools handle this.

The image then arrives as 2048 UDP datagrams of 4104 bytes each:

```
[imageId : 4 bytes LE] [blockIndex : 4 bytes LE] [4096 bytes of pixels]
```

2048 × 4096 = 8 MiB = 2048 × 2048 × 16-bit, little-endian. Always reassemble by
`blockIndex` rather than arrival order. One quirk: the first datagram often arrives 16 bytes
short and without its header — `reassemble.py` handles that for you.

---

## Image correction

**Dark / offset frame.** Capture one with the same settings but no exposure and subtract it.
This removes the panel's fixed pedestal and most of its fixed-pattern noise. A dark is only
valid for the window length it was taken at, since dark current scales with integration time.

**Dead rows and columns.** Panels have defective lines that show up as dark streaks. They are
*gain* defects — they only appear where there is signal, so **a dark frame cannot find them**.
Detect them on the offset-corrected image instead: compare each row and column median against
its local neighbourhood, and interpolate across the outliers. The GUI does this automatically.

Do not run an isolated hot-pixel filter before repairing the lines. Its neighbour averages
come from an image that still contains the dead lines, so it flags their neighbours and then
smears the defect wider. Lines first, or lines only.

For quantitative work, the detector stores proper per-dose gain and offset calibration maps
that `--backup` retrieves.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Detector pings but never replies | Host is not `192.168.1.1`, or you are listening on the wrong port. Before the port-setup step the detector replies to UDP 48879; afterwards to 5550. On Windows, check the firewall profile. |
| Control works, no image ever arrives | Link speed or MTU. Force 100 Mbit full duplex and set MTU ≥ 5000. This is almost always the answer. |
| Execute returns `0xE3`, nothing completes | The init script was not executed as a separate step. Use `--two-exec`. |
| Retrieval fails with status 1 | The enumeration request was not sent first. |
| A retrieval reply that looks like "4 images" | That first value is a length field, not a count. It means *zero* images. |
| Image arrives but is blank | The exposure missed the window, or did not happen. Lengthen the window while testing. |
| Faint ghost of the previous exposure | Increase the scrub count. |
| Dark lines across the image | Dead rows/columns. Enable defect repair, and make sure a dark frame is loaded. |
| An operation leaves the detector stuck | Power-cycle it. Nothing here writes flash unless you explicitly pass `--commit`. |

---

## Safety

**This drives X-ray generating equipment.** Ionising radiation causes real harm and the dose
is invisible. Running an X-ray source is regulated in most countries.

* Know and follow the rules where you live
* Shield the beam properly and keep yourself and everyone else out of it
* Never operate a source you cannot verify the state of
* Use the lowest exposure that gets you a usable image

**Not for clinical use.** This project is for research, repair and education. It applies no
validated calibration, has no quality assurance, and must not be used for diagnosis or any
other medical purpose.

---

## Status and scope

Working: discovery, handshake, identity and sensor readout, full flash backup, host
registration, script download, two-phase execute, dark acquisition, real X-ray acquisition,
image transfer, reassembly, offset and defect correction.

All findings come from a single detector. Other units, firmware versions or panel variants may
behave differently. Pull requests and corrections are welcome — especially confirmations from
different hardware.

Hardware details, teardown photos and the full protocol reference live on the
[Recessim wiki page](https://wiki.recessim.com/view/GE_Medical_Flashpad_Digital_Xray_Detector).
Discussion happens on the [Recessim Discord](https://discord.gg/recessim).

This is an independent reverse-engineering effort for interoperability and repair. It is not
affiliated with, endorsed by, or derived from the manufacturer's software, and no vendor code
or data files are included or required.

## Licence

[Unlicense](LICENSE) — public domain. Use it, fork it, put a detector back to work.
