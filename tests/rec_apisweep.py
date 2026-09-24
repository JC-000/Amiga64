#!/usr/bin/env python3
"""Direct memory-API sweep around 512 KB lines, bank lines and $FFFFFF.

Guest vectors can only reach EVEN word/long addresses (odd ones are 68000
address errors, filtered out of every vector set), but HLE code calls
mem_read16/32 and mem_write16/32 at arbitrary guest addresses.  This test
drives the API directly (host-poked m_addr/m_val, VICE jsr) at every
offset -5..+2 around each line, for sizes 2 and 4.  Modelled on the WS-A
delta reviewer's build/review2/apisweep.py.  Per case:

  * m_addr byte 3 holds garbage ($AB): the API must ignore it;
  * the bytes just outside the access are $EE guards that must survive;
  * write via mem_writeN, read back via mem_readN AND byte-wise via
    mem_read8 (a 1-byte DMA never straddles: the independent reference),
    compared against a 24-bit guest-memory model;
  * split-path check: after the write and after the read, m_rok == 0 iff
    the access straddles a 512 KB line (the split path invalidates the
    shadow, the fast path leaves it valid) -- so the STRADDLE predicate is
    pinned exactly, not just "correct results";
  * X/Y preservation: every API call goes through a trampoline that loads
    X=$5A, Y=$A5 and stores X/Y after the call; the memory API contract
    clobbers A only.

Usage: python3 -m tests.rec_apisweep --prg build/ws-a/runner.prg [--seed N]
Exit 0 = every case correct.  VICE only (direct jsr entry).
"""

from __future__ import annotations

import argparse
import os
import random
import struct
import sys

M_ADDR, M_VAL = 0x58, 0x5C
TRAMP = 0xC700          # buf_case: free, this test stages no cases
XY_OUT = 0xC7F0
CENTERS = (0x080000, 0x100000, 0x180000, 0x800000, 0x000000, 0x020000,
           0x010000, 0xF80000)


def trampoline(target: int) -> bytes:
    """ldx #$5A / ldy #$A5 / jsr target / stx XY_OUT / sty XY_OUT+1 / rts"""
    return bytes([0xA2, 0x5A, 0xA0, 0xA5, 0x20, target & 0xFF, target >> 8,
                  0x8E, XY_OUT & 0xFF, XY_OUT >> 8,
                  0x8C, (XY_OUT + 1) & 0xFF, (XY_OUT + 1) >> 8, 0x60])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prg", required=True)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    from c64_test_harness import (Labels, UnifiedManager, ViceConfig,
                                  read_bytes, write_bytes)
    from tests.harness_util import REU_VICE_ARGS, call_runner, lookup_label
    from tests.run_vectors import prepare_target

    prg = os.path.abspath(args.prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    m_rok = lookup_label(labels, "m_rok")
    names = ("mem_read8", "mem_write8", "mem_read16", "mem_write16",
             "mem_read32", "mem_write32")
    lab = {k: lookup_label(labels, k) for k in names}
    rng = random.Random(args.seed)
    bad, n, xybad = [], 0, 0
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        t = mgr.acquire()
        try:
            prepare_target(t, prg)
            T = t.transport
            cur = [None]

            def call(name, a, v=None):
                nonlocal xybad
                if cur[0] != name:
                    write_bytes(T, TRAMP, trampoline(lab[name]))
                    cur[0] = name
                write_bytes(T, M_ADDR, struct.pack("<I", a | 0xAB000000))
                if v is not None:
                    write_bytes(T, M_VAL, struct.pack("<I", v))
                write_bytes(T, XY_OUT, b"\x00\x00")
                call_runner(t, TRAMP, 10.0)
                if read_bytes(T, XY_OUT, 2) != b"\x5A\xA5":
                    xybad += 1
                return struct.unpack("<I", read_bytes(T, M_VAL, 4))[0]

            def rok():
                return read_bytes(T, m_rok, 1)[0]

            for ctr in CENTERS:
                for off in range(-5, 3):
                    a = (ctr + off) & 0xFFFFFF
                    for size in (2, 4):
                        for i in range(-1, size + 1):
                            call("mem_write8", (a + i) & 0xFFFFFF, 0xEE)
                        v = rng.getrandbits(8 * size)
                        call("mem_read8", a)                 # warm shadow
                        call(f"mem_write{8 * size}", a, v)
                        w_rok = rok()
                        call("mem_read8", (a + 0x100) & 0xFFFFFF)
                        got = call(f"mem_read{8 * size}", a) & \
                            ((1 << 8 * size) - 1)
                        r_rok = rok()
                        got_b = [call("mem_read8", (a + i) & 0xFFFFFF) & 0xFF
                                 for i in range(-1, size + 1)]
                        exp_b = [0xEE] + [(v >> (8 * (size - 1 - i))) & 0xFF
                                          for i in range(size)] + [0xEE]
                        straddle = ((a & 0x7FFFF) + size - 1) >= 0x80000
                        ok = (got == v and got_b == exp_b
                              and (w_rok == 0) == straddle
                              and (r_rok == 0) == straddle)
                        n += 1
                        if not ok:
                            bad.append(
                                f"size{size} a={a:06X} v={v:0{2 * size}X} "
                                f"got={got:0{2 * size}X} bytes="
                                f"{bytes(got_b).hex()} exp={bytes(exp_b).hex()} "
                                f"split(w,r)={w_rok == 0},{r_rok == 0} "
                                f"want {straddle}")
        finally:
            mgr.release(t)
    for b in bad:
        print("BAD", b)
    print(f"{n} cases, {len(bad)} bad; X/Y clobbered on {xybad} call(s)")
    return 1 if bad or xybad else 0


if __name__ == "__main__":
    sys.exit(main())
