#!/usr/bin/env python3
"""
Downloads precomputed openWakeWord negative features (HF davidscripka/openwakeword_features):
  - validation_set_features.npy in full (~0.18 GB) — for estimating false activations per hour;
  - the first --gb gigabytes of openwakeword_features_ACAV100M_2000_hrs_16bit.npy (17.3 GB in full).

A .npy file is a header followed by contiguous rows, so a prefix of the file is also a valid array:
fetch the needed number of rows with a Range request and rewrite the shape in the header.
Resuming is supported (a rerun continues from where it stopped).
"""
import argparse
import ast
import os
import sys
import urllib.request

BASE = "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/"
ACAV = "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VALID = "validation_set_features.npy"
CHUNK = 8 << 20


def fetch(url, dst, start=0, end=None):
    """Download bytes [start, end) appending to dst (resumes based on current file size)."""
    have = os.path.getsize(dst) if os.path.exists(dst) else 0
    if end is not None and have >= end - start:
        return
    rng = f"bytes={start + have}-" + (str(end - 1) if end is not None else "")
    req = urllib.request.Request(url, headers={"Range": rng})
    total = (end - start) if end is not None else None
    with urllib.request.urlopen(req) as r, open(dst, "ab") as f:
        if total is None:
            total = have + int(r.headers.get("Content-Length", 0))
        done = have
        while True:
            buf = r.read(CHUNK)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            print(f"\r  {os.path.basename(dst)}: {done / 1e9:6.2f} / {total / 1e9:.2f} GB", end="", flush=True)
    print()


def npy_header(url):
    """Read the .npy header: (header length in bytes, dict with descr/shape/fortran_order)."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-4095"})
    head = urllib.request.urlopen(req).read()
    assert head[:6] == b"\x93NUMPY", "не .npy"
    major = head[6]
    if major == 1:
        hlen = int.from_bytes(head[8:10], "little")
        start = 10
    else:
        hlen = int.from_bytes(head[8:12], "little")
        start = 12
    meta = ast.literal_eval(head[start:start + hlen].decode("latin1"))
    return start + hlen, start, meta


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gb", type=float, default=4.0, help="how many GB of ACAV to download")
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "data"))
    args = p.parse_args()
    os.makedirs(args.out, exist_ok=True)

    print("validation set:")
    fetch(BASE + VALID, os.path.join(args.out, VALID))

    data_off, hdr_start, meta = npy_header(BASE + ACAV)
    import numpy as np
    dtype = np.dtype(meta["descr"])
    shape = meta["shape"]
    row = dtype.itemsize * int(np.prod(shape[1:]))
    rows = min(shape[0], int(args.gb * 1e9) // row)
    print(f"ACAV: всего {shape} {dtype}, беру {rows} строк ({rows / shape[0] * 100:.0f}%, "
          f"~{rows / shape[0] * 2000:.0f} ч)")

    dst = os.path.join(args.out, f"acav_{rows}.npy")
    fetch(BASE + ACAV, dst, 0, data_off + rows * row)

    # rewrite shape in the header, keeping its length (pad with spaces)
    new = repr({"descr": meta["descr"], "fortran_order": meta["fortran_order"], "shape": (rows, *shape[1:])})
    hlen = data_off - hdr_start
    new_bytes = new.encode("latin1")
    if len(new_bytes) + 1 > hlen:
        sys.exit("новый заголовок длиннее старого — так не должно быть")
    with open(dst, "r+b") as f:
        f.seek(hdr_start)
        f.write(new_bytes + b" " * (hlen - len(new_bytes) - 1) + b"\n")

    arr = np.load(dst, mmap_mode="r")
    print(f"готово: {dst} {arr.shape} {arr.dtype}")


if __name__ == "__main__":
    main()
