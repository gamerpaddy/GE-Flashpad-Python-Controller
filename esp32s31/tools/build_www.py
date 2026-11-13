#!/usr/bin/env python
"""Gzip every file in www/ into www_gz/<name>.gz and write www_gz/manifest.json for embedding in the firmware.

  python tools/build_www.py [--budget-kb 150]

manifest.json: {"files": [{"name": "index.html", "content_type": "text/html; charset=utf-8", "gz_size": 1234, "size": 4567}, ...],
                "total_gz": N, "total_raw": M}
Deterministic (gzip mtime 0, level 9) so the firmware build only changes when the web assets do. README.md and dotfiles
are not shipped. The firmware serves "GET /" as index.html and sends "Content-Encoding: gzip".
"""
import argparse
import gzip
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(ROOT, "www")
DST = os.path.join(ROOT, "www_gz")
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8"}
SKIP = {"README.md"}
CRLF, LF = bytes([13, 10]), bytes([10])


def gz_bytes(data):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as g:
        g.write(data)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-kb", type=float, default=150.0)
    a = ap.parse_args()
    os.makedirs(DST, exist_ok=True)
    files, wanted = [], set()
    for dirpath, _, names in os.walk(SRC):
        for n in sorted(names):
            if n.startswith(".") or n in SKIP:
                continue
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, SRC).replace(os.sep, "/")
            ext = os.path.splitext(n)[1].lower()
            if ext not in TYPES:
                print("skipping %s (unknown type %s)" % (rel, ext))
                continue
            with open(full, "rb") as f:
                raw = f.read().replace(CRLF, LF)      # LF only: identical output regardless of the editor / OS
            gz = gz_bytes(raw)
            out = os.path.join(DST, rel + ".gz")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(gz)
            wanted.add(os.path.abspath(out))
            files.append({"name": rel, "content_type": TYPES[ext], "gz_size": len(gz), "size": len(raw)})
    for dirpath, _, names in os.walk(DST):           # drop stale outputs
        for n in names:
            p = os.path.abspath(os.path.join(dirpath, n))
            if n.endswith(".gz") and p not in wanted:
                os.remove(p)
    files.sort(key=lambda f: f["name"])
    man = {"files": files, "total_gz": sum(f["gz_size"] for f in files), "total_raw": sum(f["size"] for f in files)}
    with open(os.path.join(DST, "manifest.json"), "w", newline="\n") as f:
        json.dump(man, f, indent=1)
        f.write("\n")
    for f in files:
        print("%-16s %8d -> %7d bytes gz  %s" % (f["name"], f["size"], f["gz_size"], f["content_type"]))
    kb = man["total_gz"] / 1024.0
    print("total %d -> %d bytes gz (%.1f KiB, budget %.0f KiB)" % (man["total_raw"], man["total_gz"], kb, a.budget_kb))
    if kb > a.budget_kb:
        print("WARNING: over the page-weight budget")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
