#!/usr/bin/env python3
"""Apply (idempotently) the local ESP-IDF patches this project needs.

Usage:  python idf_patches/apply_idf_patches.py [--idf C:\\esp\\v6.1\\esp-idf] [--revert]

IDF_PATH is used when --idf is not given. Each patch is checked with `git apply --check` first; a patch that is already
applied (reverse-check succeeds) is skipped, so running this twice is harmless.

Patches:
  0001-esp_eth-jumbo-rx.patch   CONFIG_ETH_RX_JUMBO: sets the EMAC Jumbo Frame Enable bit and raises the RX frame cap so
                                the ~4146 byte frames the FlashPad sends are received instead of truncated (v6.1 / esp32s31).
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idf", default=os.environ.get("IDF_PATH"))
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()
    if not args.idf or not Path(args.idf, "components", "esp_eth").is_dir():
        sys.exit("ESP-IDF not found: pass --idf or export IDF_PATH")
    here = Path(__file__).resolve().parent
    rc = 0
    for patch in sorted(here.glob("*.patch")):
        fwd = run(["git", "apply", "--check", str(patch)], args.idf)
        rev = run(["git", "apply", "--check", "--reverse", str(patch)], args.idf)
        if args.revert:
            if rev.returncode == 0:
                r = run(["git", "apply", "--reverse", str(patch)], args.idf)
                print(f"reverted {patch.name}" if r.returncode == 0 else f"FAILED to revert {patch.name}: {r.stderr}")
                rc |= r.returncode
            else:
                print(f"{patch.name}: not applied")
        elif rev.returncode == 0:
            print(f"{patch.name}: already applied")
        elif fwd.returncode == 0:
            r = run(["git", "apply", str(patch)], args.idf)
            print(f"applied {patch.name}" if r.returncode == 0 else f"FAILED to apply {patch.name}: {r.stderr}")
            rc |= r.returncode
        else:
            print(f"{patch.name}: does not apply to this ESP-IDF ({fwd.stderr.strip()})")
            rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
