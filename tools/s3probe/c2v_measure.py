#!/usr/bin/env python3
"""Measure the planar -> VIC-II conversion kernels of c2v.s in VICE (W6-D).

Builds tools/s3probe/c2v.s, boots it in VICE (x64sc, NOT warp-sensitive:
the kernels time themselves with the emulated CIA2 timers, i.e. exact
emulated phi2 cycles of a 1 MHz C64 with the screen blanked), fills the
plane rows with seeded random data, runs each kernel over one 200-line
frame and checks the bitmap against a Python reference model (a wrong
kernel would make the cycle number meaningless).

Scaling to 48 MHz uses HARDWARE.md's measured effective clock; that part
is an estimate (the kernels are pure CPU + C64 RAM, the regime HARDWARE.md
finding 5 shows scales ~linearly).

Usage: python3 tools/s3probe/c2v_measure.py [--json-out FILE] [--mutant]
  --mutant  corrupt PAIRLO[$FF] after table build: the hires check must FAIL
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "build", "w6-s3plan", "c2v")
P = [0x2000, 0x2100, 0x2200, 0x2300]
BMP = 0x4000
PAIRLO = 0x2500


def build() -> str:
    os.makedirs(OUT, exist_ok=True)
    prg = os.path.join(OUT, "c2v.prg")
    subprocess.run(["cl65", "-t", "none", "-C", os.path.join(HERE, "c2v.cfg"),
                    "-o", prg, "-Ln", os.path.join(OUT, "c2v.lbl"),
                    os.path.join(HERE, "c2v.s")], check=True, cwd=HERE)
    return prg


def pairor(b: int) -> int:
    return sum(((b >> (7 - 2 * k) | b >> (6 - 2 * k)) & 1) << (3 - k) for k in range(4))


def ref_hires(planes: list[bytes]) -> bytes:
    bmp = bytearray(8000)
    for y in range(200):
        for cx in range(40):
            o = [0, 0]
            for h in range(2):
                v = 0
                for p in planes:
                    v |= p[2 * cx + h]
                o[h] = pairor(v)
            bmp[(y >> 3) * 320 + cx * 8 + (y & 7)] = (o[0] << 4) | o[1]
    return bytes(bmp)


def ref_mc2(planes: list[bytes]) -> bytes:
    bmp = bytearray(8000)
    for y in range(200):
        for cx in range(40):
            v = (planes[1][cx] & 0xAA) | ((planes[0][cx] & 0xAA) >> 1)
            bmp[(y >> 3) * 320 + cx * 8 + (y & 7)] = v
    return bytes(bmp)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json-out")
    ap.add_argument("--mutant", action="store_true")
    a = ap.parse_args(argv)
    from c64_test_harness import (Labels, UnifiedManager, ViceConfig, jsr,
                                  read_bytes, write_bytes)
    prg = build()
    lab = Labels.from_file(os.path.join(OUT, "c2v.lbl"))

    def addr(n):
        return lab.address(n) if hasattr(lab, "address") else lab[n]

    rng = random.Random(0x5335)
    planes = [bytes(rng.randrange(256) for _ in range(80)) for _ in range(4)]
    results = {}
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False)
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        t = mgr.acquire()
        tr = t.transport
        try:
            deadline = time.monotonic() + 60
            while read_bytes(tr, addr("ready"), 1)[0] != 0xA5:
                if time.monotonic() > deadline:
                    raise TimeoutError("c2v PRG never became ready")
                tr.resume()
                time.sleep(0.25)
            jsr(tr, addr("make_tables"), timeout=30)
            if a.mutant:
                write_bytes(tr, PAIRLO + 0xFF, bytes([0]))
            for p, data in zip(P, planes):
                write_bytes(tr, p, data + bytes(256 - len(data)))
            for name, entry, ref in (("hires4_640to320", "k_hires_entry", ref_hires),
                                     ("mc2_320to160", "k_mc2_entry", ref_mc2)):
                write_bytes(tr, BMP, bytes(8000))
                jsr(tr, addr(entry), timeout=120)
                cyc = int.from_bytes(read_bytes(tr, addr("res_cyc"), 4), "little")
                got = read_bytes(tr, BMP, 8000)
                want = ref(planes)
                bad = sum(1 for i in range(8000) if got[i] != want[i])
                results[name] = {"cycles_frame": cyc, "cycles_per_out_byte": cyc / 8000,
                                 "bytes_wrong": bad}
                print(f"{name}: {cyc} cycles/frame ({cyc / 8000:.1f}/output byte), "
                      f"bitmap {'OK' if bad == 0 else f'WRONG in {bad} bytes'}")
        finally:
            mgr.release(t)
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump(results, f, indent=1)
    return 1 if any(r["bytes_wrong"] for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
