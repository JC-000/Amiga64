#!/usr/bin/env python3
"""Milestone 3's reference: the boot model's display, converted on the host.

Runs tools/s3probe/aros_boot_model.py (needs machine68k: run it with the
s3probe venv, ~/.cache/amiga64/s3probe-venv/bin/python, built outside the
repo by tools/s3probe/setup_venv.sh) on the pinned ROM and, at every
frame's vblank after the model's copper ran, takes the display registers
as a disp_st snapshot (docs/CONTRACTS.md "Copper and display state") and
converts it with host/c2v_ref.disp_convert, the host twin of disp.s and the
c2v kernels.  Writes the hash of every distinct conversion (bitmap, screen
matrix, colour RAM, the VIC registers) with the first frame it appears in.

tests/aros_boot.py leg m3 compares the C64's conversion against this set
(frames at or after INSERT_DISK_FRAME: the 7.0 s "insert disk" logo and
icons, S3_PLAN §3.1).  Nothing of the ROM is written: only hashes and
display-register values.

    ~/.cache/amiga64/s3probe-venv/bin/python tests/aros/model_frames.py OUT.json
        [--from 250] [--to 450]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "s3probe"))

# The first frame whose conversion shows the "insert disk" logo and icons:
# frame 345 (6.9 s) by this tool on the pinned ROM; S3_PLAN §3.1's "7.0 s
# frame" is the first 0.5 s PNG snapshot that shows them.
INSERT_DISK_FRAME = 345
# the VIC bits the conversion owns (tests/run_pf.py VIC_MASK)
VIC_MASK = {0xD011: 0x7F, 0xD016: 0x1F, 0xD018: 0xFE, 0xD020: 0x0F, 0xD021: 0x0F}


def conv_hash(bitmap: bytes, screen: bytes, colram: bytes, vic: dict) -> str:
    """The hash of one conversion; tests/aros_boot.py computes it from the
    C64 side with the same function (colour RAM low nibbles, masked VIC)."""
    h = hashlib.sha1()
    h.update(bitmap)
    h.update(screen)
    h.update(bytes(b & 0x0F for b in colram))
    for a in sorted(VIC_MASK):
        h.update(bytes([vic[a] & VIC_MASK[a]]))
    return h.hexdigest()


def snapshot(am) -> dict:
    r = am.regs
    return {"dmacon": am.paula.dmacon, "bplcon0": r[0x100 >> 1],
            "bplcon1": r[0x102 >> 1], "bplcon2": r[0x104 >> 1],
            "bplpt": [((r[(0x0E0 + 4 * i) >> 1] & 0xFF) << 16) | r[(0x0E2 + 4 * i) >> 1]
                      for i in range(6)],
            "bpl1mod": r[0x108 >> 1], "bpl2mod": r[0x10A >> 1],
            "diwstrt": r[0x08E >> 1], "diwstop": r[0x090 >> 1],
            "ddfstrt": r[0x092 >> 1], "ddfstop": r[0x094 >> 1],
            "color": [r[(0x180 + 2 * i) >> 1] for i in range(32)]}


def main(argv=None) -> int:
    import glob
    import aros_boot_model as M
    from host import c2v_ref as R
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out")
    ap.add_argument("--from", dest="lo", type=int, default=250)
    ap.add_argument("--to", dest="hi", type=int, default=450)
    a = ap.parse_args(argv)
    c = glob.glob(os.path.expanduser("~/.cache/amiga64/aros/*/aros-rom.bin"))
    if not c:
        print("SKIP: no AROS ROM in ~/.cache/amiga64/aros (run tools/aros/fetch.sh)")
        return 0
    scratch = os.path.join(os.path.dirname(os.path.abspath(a.out)), "model-run")
    os.makedirs(scratch, exist_ok=True)
    am = M.Amiga(os.path.dirname(c[0]), scratch)
    lut = R.build_lut()
    seen: dict[str, dict] = {}
    orig = am.copper

    def copper():
        n = orig()
        f = am.frames
        if a.lo <= f <= a.hi:
            st = snapshot(am)
            conv = R.disp_convert(st, am.mem.r_block, lut)
            h = conv_hash(conv["bitmap"], conv["screen"], conv["colram"],
                          conv["vic"]) if "bitmap" in conv else "blank"
            if h not in seen:
                d = conv["decide"]
                seen[h] = {"frame": f, "icount": am.icount, "mode": d["mode"],
                           "why": d["why"], "count": 0}
            seen[h]["count"] += 1
        return n
    am.copper = copper
    am.run((a.hi + 2) / 50.0, 1000)
    with open(a.out, "w") as fp:
        json.dump({"from": a.lo, "to": a.hi, "insert_disk_frame": INSERT_DISK_FRAME,
                   "hashes": seen,
                   # the boot's serial debug text (milestones 1-2 compare it)
                   "serial": am.serial.decode("latin-1")}, fp, indent=1)
    print(f"{len(seen)} distinct conversions in frames {a.lo}-{a.hi}: "
          + ", ".join(f"{v['frame']}" for v in sorted(seen.values(), key=lambda v: v['frame'])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
