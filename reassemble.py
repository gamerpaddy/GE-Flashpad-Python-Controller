#!/usr/bin/env python3
"""Reassemble the FlashPad image from the raw UDP capture.

Wire format (confirmed live 2026-10-01, matches firmware FUN_0c00c978/FUN_0c01cec8):
  each image datagram = [imageId:4LE][blockIndex:4LE][4096 bytes pixel data] = 4104 bytes
  2048 blocks x 4096 B = 8388608 B = 2048 x 2048 x 16-bit
The capture's first record is 16 B short (4088), so block 0's payload is padded.
"""
import struct
import sys
import os
import array

STRIDE = 4104
PAY = 4096

HERE = os.path.dirname(os.path.abspath(__file__))


def resolve(arg):
    """Find the capture whether given as a full path, a bare name, or not at all."""
    import glob
    if arg:
        for cand in (arg, os.path.join(HERE, arg), os.path.join(os.getcwd(), arg)):
            if os.path.isfile(cand):
                return cand
        # allow a partial name / timestamp fragment
        hits = sorted(glob.glob(os.path.join(HERE, "*%s*" % arg)))
        hits = [h for h in hits if h.endswith(".raw") and "_2048x2048" not in h]
        if hits:
            return hits[-1]
        print("Could not find %r. Captures available in\n  %s :" % (arg, HERE))
        for h in sorted(glob.glob(os.path.join(HERE, "flashpad_*.raw"))):
            if "_2048x2048" not in h:
                print("   %s" % os.path.basename(h))
        sys.exit(2)
    # no argument: use the newest raw capture next to this script
    hits = [h for h in sorted(glob.glob(os.path.join(HERE, "flashpad_*.raw")))
            if "_2048x2048" not in h]
    if not hits:
        print("No flashpad_*.raw captures found in %s" % HERE)
        sys.exit(2)
    newest = max(hits, key=os.path.getmtime)
    print("(no file given -- using newest capture: %s)" % os.path.basename(newest))
    return newest


SRC = resolve(sys.argv[1] if len(sys.argv) > 1 else None)

d = open(SRC, "rb").read()
print("input %s : %d bytes" % (SRC, len(d)))

blocks = {}

# the first record is short (no header captured) -> treat as block 0 payload
head_len = len(d) % STRIDE
if head_len:
    blocks[0] = d[:head_len].ljust(PAY, b"\x00")
    print("first short record: %d bytes -> block 0 (padded to %d)" % (head_len, PAY))
    off = head_len
else:
    off = 0

ids = set()
while off + 8 <= len(d):
    img_id, blk = struct.unpack_from("<II", d, off)
    payload = d[off + 8: off + STRIDE]
    ids.add(img_id)
    if blk in blocks:
        print("  dup block %d (ignored)" % blk)
    else:
        blocks[blk] = payload.ljust(PAY, b"\x00")
    off += STRIDE

print("imageIds seen: %s" % [hex(x) for x in ids])
print("blocks collected: %d   range %d..%d" % (len(blocks), min(blocks), max(blocks)))
missing = [b for b in range(0, 2048) if b not in blocks]
print("missing blocks: %d %s" % (len(missing), missing[:10]))

out = bytearray()
for b in range(0, 2048):
    out += blocks.get(b, b"\x00" * PAY)
print("reassembled: %d bytes (%.2f MiB)" % (len(out), len(out) / 1048576.0))

base = os.path.splitext(SRC)[0]
raw_out = base + "_2048x2048_u16.raw"
with open(raw_out, "wb") as fh:
    fh.write(out)
print("-> %s" % raw_out)

# stats
a = array.array("H")
a.frombytes(bytes(out))
nz = [x for x in a if x]
print("pixels=%d  min=%d max=%d mean=%.1f" % (len(a), min(a), max(a), sum(a) / float(len(a))))

# write a PGM for easy viewing (16-bit, big-endian per PGM spec)
pgm = base + "_2048x2048.pgm"
mx = max(a)
with open(pgm, "wb") as fh:
    fh.write(b"P5\n2048 2048\n65535\n")
    be = array.array("H", a)
    be.byteswap()
    fh.write(be.tobytes())
print("-> %s (16-bit PGM, max=%d)" % (pgm, mx))

# row statistics to confirm it is a coherent frame
print("\nrow means (every 256th row):")
for r in range(0, 2048, 256):
    row = a[r * 2048:(r + 1) * 2048]
    print("   row %4d  mean=%.1f  min=%d max=%d" % (r, sum(row) / 2048.0, min(row), max(row)))
