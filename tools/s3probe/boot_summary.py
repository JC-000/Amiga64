#!/usr/bin/env python3
"""Summarise aros_boot_model.py's boot_report.json (W6-D, S3 plan Q3).

Prints markdown: boot phases (first touch of landmark registers), the
instruction-class mix (opcode high byte), trapped chip accesses by the CPU
(the accesses the C64-side trap would service), and boot-time estimates for
given interpreter speeds.  stdlib only.

Usage: python3 tools/s3probe/boot_summary.py build/evidence/w6-s3plan/boot/boot_report.json
           [--screen-icount N] [--ips 50000,60000,75000,1600]
"""
from __future__ import annotations

import argparse
import json

CLASSES = [
    ("imm ALU / bit ops ($0x)", range(0x00, 0x10)),
    ("MOVE / MOVEA ($1x-$3x)", range(0x10, 0x40)),
    ("misc $4x (LEA/CLR/TST/JSR/RTS/MOVEM/...)", range(0x40, 0x50)),
    ("  of which MOVEM/EXT/PEA/SWAP ($48,$4C)", (0x48, 0x4C)),
    ("  of which JSR/JMP/RTS/LINK/UNLK/TRAP ($4E)", (0x4E,)),
    ("ADDQ/SUBQ/Scc/DBcc ($5x)", range(0x50, 0x60)),
    ("Bcc/BRA/BSR ($6x)", range(0x60, 0x70)),
    ("MOVEQ ($7x)", range(0x70, 0x80)),
    ("OR/DIV ($8x)", range(0x80, 0x90)),
    ("SUB ($9x)", range(0x90, 0xA0)),
    ("CMP/EOR ($Bx)", range(0xB0, 0xC0)),
    ("AND/MUL/EXG ($Cx)", range(0xC0, 0xD0)),
    ("ADD ($Dx)", range(0xD0, 0xE0)),
    ("shift/rotate ($Ex)", range(0xE0, 0xF0)),
    ("A-line/F-line", list(range(0xA0, 0xB0)) + list(range(0xF0, 0x100))),
]

LANDMARKS = [
    ("CIAA", "PRA", "overlay off (end of ROM checksum + memory test)"),
    ("CIAA", "TODLO", "kernel: CIA-A TOD vs timer A calibration"),
    ("custom", "SERDAT", "first serial debug character"),
    ("custom", "DIWSTRT", "kernel IRQ init (display window defaults)"),
    ("CIAB", "PRB", "disk.resource: drive ID / select"),
    ("custom", "VHPOSR", "timer: PAL/NTSC beam probe"),
    ("custom", "DENISEID", "amigavideo: chip revision probe"),
    ("custom", "COP2LCH", "first copper list"),
    ("custom", "BLTSIZE", "first blit"),
    ("custom", "COLOR01", "boot screen palette (copper)"),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("report")
    ap.add_argument("--screen-icount", type=int,
                    help="instruction count at which the insert-disk screen is up")
    ap.add_argument("--ips", default="50000,60000,75000,1600")
    a = ap.parse_args(argv)
    r = json.load(open(a.report))
    regs = {(g["chip"], g["reg"]): g for g in r["registers"]}
    print("## Phases (first CPU touch)\n\n| icount | register | meaning |\n|---:|---|---|")
    for chip, reg, what in LANDMARKS:
        g = regs.get((chip, reg))
        if g:
            print(f"| {g['first_icount']:,} | {chip}.{reg} | {what} |")
    hist = {int(k, 16): v for k, v in r["opcode_hi_histogram"].items()}
    tot = sum(hist.values())
    print(f"\n## Instruction mix over the whole run ({tot:,} instructions)\n")
    print("| class | share |\n|---|---:|")
    for name, rng in CLASSES:
        n = sum(hist.get(k, 0) for k in rng)
        print(f"| {name} | {100.0 * n / tot:.1f}% |")
    cpu_acc = {"custom": 0, "CIAA": 0, "CIAB": 0}
    cop = 0
    for g in r["registers"]:
        cpu_acc[g["chip"]] += g["reads"] + g["cpu_writes"]
        cop += g["copper_writes"]
    print("\n## Trapped accesses by the CPU (whole run)\n")
    for k, v in cpu_acc.items():
        print(f"- {k}: {v:,}")
    print(f"- copper MOVEs (not CPU traps): {cop:,}")
    print(f"- blits: {sum(r['blits'].values())}, {r['blit_words']:,} words; kinds {r['blits']}")
    print(f"- IRQ deliveries by level: {r['irq_deliveries_by_level']}")
    print(f"- privileged/exception opcodes: {r['privileged_or_exception_opcodes']}")
    n = a.screen_icount or r["instructions"]
    print(f"\n## Time to execute {n:,} guest instructions\n")
    print("| interpreter speed | time |\n|---:|---:|")
    for ips in (int(x) for x in a.ips.split(",")):
        s = n / ips
        print(f"| {ips:,} IPS | {s:,.0f} s ({s / 60:.1f} min) |")


if __name__ == "__main__":
    main()
