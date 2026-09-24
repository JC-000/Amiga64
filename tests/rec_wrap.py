#!/usr/bin/env python3
"""Runner-level 512 KB-straddle + bank-carry test (WS-A fix round).

The REC's REU address counter carries only through bits 0-18 (VICE
reu.c; measured on the U64E by tests/bench/rec_probe.py), so one DMA that
straddles $x7FFFF -> $x80000 wraps to $x00000.  The memory API must split
such accesses.  This test runs a multi-instruction guest program through
the real M0 path (test_load_chunk staging, test_run_program), then checks
registers AND the REU contents with an independent reader (16-byte,
16-aligned raw DMAs that never straddle).

Program (PC = $010000):
    LEA $07FFFE,A0 ; MOVE.L #$A1B2C3D4,(A0)   straddling write
    MOVE.L (A0),D1                            straddling read
    MOVE.W (A0),D2                            non-straddling word after a split
    MOVE.L (A0),D3                            repeat straddle
    LEA $17FFFE,A1 ; MOVE.L (A0),(A1)         straddle -> straddle
    MOVE.L (A1),D4
    LEA $01FFFE,A2 ; MOVE.L #$55667788,(A2)   bank carry inside a block
    MOVE.L #$99AABBCC,$010100                 same bank after the carry
    MOVE.L (A2),D5 ; MOVE.L $010100,D6
    MOVEA.L #$00FFFFFE,A3 ; MOVE.L #$0F1E2D3C,(A3)  24-bit wrap to $000000
    MOVE.L (A3),D7
    HALT
Second program: the WS-B2 reviewer's repro (build/review2/core_wrap.py in
the ws-b2 tree, re-hosted here without run_dos): after staging ONE 32-byte
chunk across $080000 through test_load_chunk (itself checked byte-exact
with the raw reader -- staging is per-byte mem_write8, so it must never
straddle):
    MOVE.L #$11223344,$07FFFE ; MOVE.B $080000,D2 ; MOVE.B $000000,D3
    MOVE.B $07FFFF,D4 ; HALT       -> D2=$33, D3=[$000000] unchanged, D4=$22
Usage: python3 -m tests.rec_wrap --prg PRG [--backend vice|u64]
Exit 0 = every check passed.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

from host import fixturegen, hunk, image
from tests import run_program as rp

SENT = {   # guest addr -> bytes staged before the run (must survive/change)
    0x07FFF0: bytes([0xEE] * 16), 0x080000: bytes([0xEE] * 16),
    0x000000: bytes([0x5A, 0x5B, 0x5C, 0x5D]),      # REU-wrap target
    0x17FFF0: bytes([0xEE] * 16), 0x180000: bytes([0xEE] * 16),
    0x100000: bytes([0x6A, 0x6B, 0x6C, 0x6D]),
    0x01FFF0: bytes([0xEE] * 16), 0x020000: bytes([0xEE] * 16),
    0x010100: bytes([0xEE] * 16), 0x020100: bytes([0x77] * 16),
    0xFFFFF0: bytes([0xEE] * 16), 0xF80000: bytes([0x7A, 0x7B, 0x7C, 0x7D]),
}


def program() -> bytes:
    w = [0x41F9, 0x0007, 0xFFFE,               # LEA $07FFFE,A0
         0x20BC, 0xA1B2, 0xC3D4,               # MOVE.L #$A1B2C3D4,(A0)
         0x2210,                               # MOVE.L (A0),D1
         0x3410,                               # MOVE.W (A0),D2
         0x2610,                               # MOVE.L (A0),D3
         0x43F9, 0x0017, 0xFFFE,               # LEA $17FFFE,A1
         0x2290,                               # MOVE.L (A0),(A1)
         0x2811,                               # MOVE.L (A1),D4
         0x45F9, 0x0001, 0xFFFE,               # LEA $01FFFE,A2
         0x24BC, 0x5566, 0x7788,               # MOVE.L #$55667788,(A2)
         0x23FC, 0x99AA, 0xBBCC, 0x0001, 0x0100,   # MOVE.L #..,$010100
         0x2A12,                               # MOVE.L (A2),D5
         0x2C39, 0x0001, 0x0100,               # MOVE.L $010100,D6
         0x267C, 0x00FF, 0xFFFE,               # MOVEA.L #$FFFFFE,A3
         0x26BC, 0x0F1E, 0x2D3C,               # MOVE.L #$0F1E2D3C,(A3)
         0x2E13,                               # MOVE.L (A3),D7
         0xAFFF]
    b = fixturegen.HunkBuilder()
    b.add_code(w)
    return b.build()


EXP_REGS = {"D1": 0xA1B2C3D4, "D2": 0x0000A1B2, "D3": 0xA1B2C3D4,
            "D4": 0xA1B2C3D4, "D5": 0x55667788, "D6": 0x99AABBCC,
            "D7": 0x0F1E2D3C, "A0": 0x07FFFE, "A1": 0x17FFFE,
            "A2": 0x01FFFE, "A3": 0xFFFFFE, "A7": image.STACK_TOP}


def expected_mem() -> dict[int, bytes]:
    exp = dict(SENT)

    def put(addr, data):
        for i, b in enumerate(data):
            a = (addr + i) & 0xFFFFFF
            base = next(k for k in exp if k <= a < k + len(exp[k]))
            buf = bytearray(exp[base])
            buf[a - base] = b
            exp[base] = bytes(buf)
    put(0x07FFFE, bytes.fromhex("A1B2C3D4"))
    put(0x17FFFE, bytes.fromhex("A1B2C3D4"))
    put(0x01FFFE, bytes.fromhex("55667788"))
    put(0x010100, bytes.fromhex("99AABBCC"))
    put(0xFFFFFE, bytes.fromhex("0F1E2D3C"))
    return exp


def run(target, labels, backend) -> list[str]:
    from c64_test_harness import read_bytes, write_bytes
    from tests.harness_util import BUF_CASE, BUF_RESULT
    from tests.rec_teeth import Leg
    leg = Leg(target, labels, backend)
    sess = rp.Session(target=target, labels=labels, timeout=60.0)
    # sentinels + program through the real staging path
    for addr, data in SENT.items():
        write_bytes(target.transport, BUF_CASE,
                    struct.pack("<II", addr, len(data)) + data)
        leg.call("test_load_chunk")
        if read_bytes(target.transport, BUF_RESULT, 1)[0] != 0xFF:
            leg.problems.append(f"staging {addr:06X} did not complete")
    exe = hunk.parse(program())
    chunks, manifest = image.build_image(exe)
    sess.stage(chunks)
    res = sess.run_program(manifest["entry"], manifest["stack_top"], 1000)
    res.print_state()
    if not res.halt or res.reason != rp.HALT_OK or res.timeout:
        leg.problems.append(f"program did not HALT cleanly: halt={res.halt} "
                            f"reason={res.reason} timeout={res.timeout}")
    for r in rp.REG_NAMES:
        want = EXP_REGS.get(r, 0)
        if res.regs[r] != want:
            leg.problems.append(f"{r} expected {want:08X} got {res.regs[r]:08X}")
    for addr, want in sorted(expected_mem().items()):
        got = leg.ref_read(addr & ~0xF)[addr & 0xF:(addr & 0xF) + len(want)]
        if got != want:
            leg.problems.append(f"REU[{addr:06X}] expected {want.hex()} "
                                f"got {got.hex()}")
    return leg.problems


CROSS = bytes((0x30 + i) & 0xFF for i in range(32))   # at $07FFF0..$08000F


def b2_program() -> bytes:
    b = fixturegen.HunkBuilder()
    b.add_code([0x23FC, 0x1122, 0x3344, 0x0007, 0xFFFE,  # MOVE.L #..,$7FFFE
                0x1439, 0x0008, 0x0000,                  # MOVE.B $80000,D2
                0x1639, 0x0000, 0x0000,                  # MOVE.B $0,D3
                0x1839, 0x0007, 0xFFFF,                  # MOVE.B $7FFFF,D4
                0xAFFF])
    return b.build()


def run_b2(target, labels, backend) -> list[str]:
    from c64_test_harness import read_bytes, write_bytes
    from tests.harness_util import BUF_CASE, BUF_RESULT
    from tests.rec_teeth import Leg
    leg = Leg(target, labels, backend)
    sess = rp.Session(target=target, labels=labels, timeout=60.0)
    for addr, data in ((0x07FFF0, CROSS), (0x000000, bytes([0x5A] * 4))):
        write_bytes(target.transport, BUF_CASE,
                    struct.pack("<II", addr, len(data)) + data)
        leg.call("test_load_chunk")
        if read_bytes(target.transport, BUF_RESULT, 1)[0] != 0xFF:
            leg.problems.append(f"staging {addr:06X} did not complete")
    got = leg.ref_read(0x07FFF0) + leg.ref_read(0x080000)
    if got != CROSS:
        leg.problems.append(f"chunk staged across $080000 wrong: expected "
                            f"{CROSS.hex()} got {got.hex()}")
    exe = hunk.parse(b2_program())
    chunks, manifest = image.build_image(exe)
    sess.stage(chunks)
    res = sess.run_program(manifest["entry"], manifest["stack_top"], 100)
    res.print_state()
    want = {"D2": 0x33, "D3": 0x5A, "D4": 0x22}
    for r, v in want.items():
        if res.regs[r] != v:
            leg.problems.append(f"b2 repro: {r} expected {v:08X} got "
                                f"{res.regs[r]:08X}")
    got = leg.ref_read(0x07FFF0)[14:] + leg.ref_read(0x080000)[:2]
    if got != bytes.fromhex("11223344"):
        leg.problems.append(f"b2 repro: REU[$07FFFE] expected 11223344 got "
                            f"{got.hex()}")
    if leg.ref_read(0x000000)[:4] != bytes([0x5A] * 4):
        leg.problems.append("b2 repro: REU[$000000] was overwritten")
    return leg.problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prg", required=True)
    ap.add_argument("--backend", default="vice", choices=["vice", "u64"])
    args = ap.parse_args(argv)
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(args.prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend=args.backend, vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            probs = run(target, labels, args.backend)
            probs += run_b2(target, labels, args.backend)
        finally:
            mgr.release(target)
    print(f"\nrec_wrap prg={args.prg} backend={args.backend}")
    for p in probs:
        print(f"  FAIL {p}")
    print("all checks passed" if not probs else f"{len(probs)} check(s) failed")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
