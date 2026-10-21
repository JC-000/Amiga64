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
display-register values.  W12-V: each entry also carries "u64", the hash
the Ultimate side can compute (the matrix replaced by disp_mck, the
checksum disp.s keeps in BSS), and "visible", the number of distinct
non-background colours of cells with pixels; every insert-disk conversion
must show at least MIN_VISIBLE of them (asserted here and in leg m3).

    ~/.cache/amiga64/s3probe-venv/bin/python tests/aros/model_frames.py OUT.json
        [--from 250] [--to 450]

W13-W (additive): with --adf DISK [--startup trim|min|full] the model boots
that disk with the HLE trackdisk (aros_boot_model.py --adf --td-hle
--startup, the same overlay as host/adf_overlay.py) and the set holds every
distinct conversion from Workbench's first window (the model's wb_window
marker) to WB_TAIL_S virtual seconds after boot idle; tests/aros_wb.py leg
w3 compares the C64's picture against it.  The disk's bytes are read
locally; only hashes, block numbers and the serial text are written.

    ... model_frames.py OUT.json --adf DISK.adf [--startup trim]
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
MIN_VISIBLE = 3                 # W12-V V3: distinct non-background colours in use
MCK_VERSION = 2                 # W12-V: disp_mck = Fletcher mod 255 (1: mod 256)
CROP_RULE = "top"               # W14-D: the fit view's crop (c2v_ref.disp_decide); a JSON
                                # without it ("centre", the W13 rule) is rebuilt by its loader


def conv_hash(bitmap: bytes, screen: bytes | None, colram: bytes, vic: dict,
              mck: int | None = None) -> str:
    """The hash of one conversion; tests/aros_boot.py computes it from the
    C64 side with the same function (colour RAM low nibbles, masked VIC).
    screen None: the variant without the screen matrix (the U64E cannot read
    the RAM under the I/O window of a running machine without banking it in);
    W12-V: that variant carries mck, disp.s's matrix checksum (disp_mck, u16
    LE), so a wrong palette still changes it."""
    h = hashlib.sha1()
    h.update(bitmap)
    if screen is not None:
        h.update(screen)
    h.update(bytes(b & 0x0F for b in colram))
    for a in sorted(VIC_MASK):
        h.update(bytes([vic[a] & VIC_MASK[a]]))
    if mck is not None:
        h.update(bytes([mck & 0xFF, mck >> 8]))
    return h.hexdigest()


def u64_hash(conv: dict) -> str:
    """W12-V: the Ultimate-side hash of a c2v_ref.disp_convert result."""
    return conv_hash(conv["bitmap"], None, conv["colram"], conv["vic"], conv["mck"])


def visible_colours(conv: dict) -> int:
    """W12-V: distinct foreground colours, other than the background, of the
    cells that have a set pixel (hires: matrix high nibble; mc2: the three
    pen colours if any pixel is non-zero)."""
    bm, sc, bg = conv["bitmap"], conv["screen"], conv["bgc"]
    if conv["decide"]["mode"] == 1:                  # mc2: global colours
        pens = {(b >> s) & 3 for b in bm for s in (0, 2, 4, 6)} - {0}
        col = {1: sc[0] >> 4, 2: sc[0] & 15, 3: conv["colram"][0] & 15}
        return len({col[p] for p in pens} - {bg})
    fg = set()
    for c in range(1000):
        r, cx = divmod(c, 40)
        if any(bm[r * 320 + cx * 8 + y] for y in range(8)):
            fg.add(sc[c] >> 4)
    return len(fg - {bg})


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


WB_TAIL_S = 20.0                # W13-W: frames kept after the model's boot idle


def title_mask_rows(st: dict, title_lines: int) -> list[int]:
    """W13-W: the C64 character rows that show the Amiga lines
    0 .. title_lines-1 (Workbench's title bar, whose free-memory text
    changes) in the fit view: the conversion takes DIW's first 200 lines
    (W14-D: no crop at the top; it was the centre-crop, (L - 200) // 2
    lines skipped).  Empty when the crop hides the whole bar."""
    crop = 0                    # W14-D: c2v_ref.disp_decide's crop
    lines = range(max(0, -crop), max(0, title_lines - crop))
    return sorted({y // 8 for y in lines if y < 200})


def masked(bitmap: bytes, screen: bytes | None, colram: bytes, rows: list[int]):
    """W13-W: the conversion with the masked character rows zeroed."""
    bm, cr = bytearray(bitmap), bytearray(colram)
    sc = None if screen is None else bytearray(screen)
    for r in rows:
        bm[r * 320:(r + 1) * 320] = bytes(320)
        cr[r * 40:(r + 1) * 40] = bytes(40)
        if sc is not None:
            sc[r * 40:(r + 1) * 40] = bytes(40)
    return bytes(bm), None if sc is None else bytes(sc), bytes(cr)


def mask_hashes(conv: dict, rows: list[int]) -> dict:
    """W13-W: {vice, u64} hashes of a conversion with *rows* masked; the
    Ultimate variant drops disp_mck (it sums the whole matrix, title cells
    included) and keeps colour RAM's pens."""
    bm, sc, cr = masked(conv["bitmap"], conv["screen"], conv["colram"], rows)
    return {"vice": conv_hash(bm, sc, cr, conv["vic"]),
            "u64": conv_hash(bm, None, cr, conv["vic"], None if rows else conv["mck"])}


def wb_main(a) -> int:
    """W13-W: the Workbench frame set (see the module doc)."""
    import glob
    import aros_boot_model as M
    from adf import AdfImage
    from host import adf_overlay as O
    from host import c2v_ref as R
    c = glob.glob(os.path.join(ROOT, "third_party", "aros", "aros-rom.bin"))
    if not c:
        print("SKIP: no AROS ROM in third_party/aros (git checkout -- third_party/aros)")
        return 0
    with open(a.adf, "rb") as f:
        raw = f.read()
    ov = O.make_overlay(raw, a.startup)
    adf = AdfImage.from_bytes(O.apply(raw, ov), a.adf)
    scratch = os.path.join(os.path.dirname(os.path.abspath(a.out)), "model-run-wb")
    os.makedirs(scratch, exist_ok=True)
    am = M.Amiga(os.path.dirname(c[0]), scratch, adf=adf, td_hle=True)
    am.wb.stop_after_idle = 3.0
    lut = R.build_lut()
    seen: dict[str, dict] = {}
    convs: dict[str, tuple] = {}
    orig = am.copper
    tail = {"until": None}

    def copper():
        n = orig()
        wb = am.wb
        if "wb_window" in wb.markers:
            st = snapshot(am)
            conv = R.disp_convert(st, am.mem.r_block, lut)
            h = conv_hash(conv["bitmap"], conv["screen"], conv["colram"],
                          conv["vic"]) if "bitmap" in conv else "blank"
            if h not in seen:
                d = conv["decide"]
                if "bitmap" in conv:
                    convs[h] = (conv, st)
                seen[h] = {"frame": am.frames, "icount": am.icount, "mode": d["mode"],
                           "why": d["why"], "count": 0,
                           "u64": u64_hash(conv) if "bitmap" in conv else "blank",
                           "visible": visible_colours(conv) if "bitmap" in conv else 0,
                           "vic_pens": conv.get("vic_pens")}
            seen[h]["count"] += 1
        return n
    am.copper = copper
    orig_frame = am.wb.frame

    def stop_frame(n):
        orig_frame(n)                           # sets wb.done at boot idle
        if am.wb.done and tail["until"] is None:
            tail["until"] = n + int(WB_TAIL_S * 50)
        am.wb.done = tail["until"] is not None and n >= tail["until"]
    am.wb.frame = stop_frame
    am.run(a.seconds, 1000)
    marks = {k: {"icount": v["icount"], "t_s": v["t_s"]} for k, v in am.wb.markers.items()}
    if "wb_window" not in marks:
        print(f"FAIL: the model did not reach Workbench in {a.seconds} s: {sorted(marks)}")
        return 1
    dim = {h[:12]: v["visible"] for h, v in seen.items() if v["visible"] < MIN_VISIBLE}
    # the title bar: Screen.BarHeight (struct Screen +30, intuition/screens.h)
    # + 1 lines, measured on the model's Workbench screen
    ib = am.wb.find_node(378, "intuition.library")
    scr = am.mem.r32(ib + 60) if ib else 0
    title_lines = (am.mem.r8(scr + 30) + 1) if scr else 0
    rows: list[int] = []
    for h, (conv, st) in convs.items():
        rows = title_mask_rows(st, title_lines)
        seen[h]["masked"] = mask_hashes(conv, rows)
        seen[h]["mask_rows"] = rows
    with open(a.out, "w") as fp:
        json.dump({"adf": os.path.basename(a.adf), "startup": a.startup,
                   "overlay": ov.info(), "mck_version": MCK_VERSION,
                   "crop_rule": CROP_RULE,
                   "markers": marks, "frames": am.frames, "tail_s": WB_TAIL_S,
                   "hle_served": am.hle.served, "hle_passed": am.hle.passed,
                   "title_lines": title_lines,
                   "hashes": seen, "dim": dim,
                   "serial": am.serial.decode("latin-1")}, fp, indent=1)
    print(f"{len(seen)} distinct conversions from frame "
          f"{min((v['frame'] for v in seen.values()), default=-1)} to {am.frames}: "
          + ", ".join(f"{v['frame']} ({v['visible']} colours, x{v['count']})"
                      for v in sorted(seen.values(), key=lambda v: v['frame'])))
    return 0


def main(argv=None) -> int:
    import glob
    import aros_boot_model as M
    from host import c2v_ref as R
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out")
    ap.add_argument("--from", dest="lo", type=int, default=250)
    ap.add_argument("--to", dest="hi", type=int, default=450)
    ap.add_argument("--adf", help="W13-W: the Workbench frame set of this disk")
    ap.add_argument("--startup", default="trim", choices=("trim", "min", "full"))
    ap.add_argument("--seconds", type=float, default=120.0)
    a = ap.parse_args(argv)
    if a.adf:
        return wb_main(a)
    c = glob.glob(os.path.join(ROOT, "third_party", "aros", "aros-rom.bin"))
    if not c:
        print("SKIP: no AROS ROM in third_party/aros (git checkout -- third_party/aros)")
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
                           "why": d["why"], "count": 0,
                           "u64": u64_hash(conv) if "bitmap" in conv else "blank",
                           "visible": visible_colours(conv) if "bitmap" in conv else 0,
                           "vic_pens": conv.get("vic_pens")}
            seen[h]["count"] += 1
        return n
    am.copper = copper
    am.run((a.hi + 2) / 50.0, 1000)
    dim = {h[:12]: v["visible"] for h, v in seen.items()
           if v["frame"] >= INSERT_DISK_FRAME and v["visible"] < MIN_VISIBLE}
    if dim:                     # W12-V V3: the insert-disk screen must be visible
        print(f"FAIL: insert-disk conversions with < {MIN_VISIBLE} visible colours: {dim}")
        return 1
    with open(a.out, "w") as fp:
        json.dump({"from": a.lo, "to": a.hi, "insert_disk_frame": INSERT_DISK_FRAME,
                   "mck_version": MCK_VERSION, "crop_rule": CROP_RULE,
                   "hashes": seen,
                   # the boot's serial debug text (milestones 1-2 compare it)
                   "serial": am.serial.decode("latin-1")}, fp, indent=1)
    print(f"{len(seen)} distinct conversions in frames {a.lo}-{a.hi}: "
          + ", ".join(f"{v['frame']} ({v['visible']} colours)"
                      for v in sorted(seen.values(), key=lambda v: v['frame'])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
