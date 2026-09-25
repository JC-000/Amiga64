#!/usr/bin/env python3
"""W5-H allocator cost per call, measured in VICE (docs/HLE.md "Heap").

Calls hle_alloc / hle_free / hle_pool_delete directly (host-driven jsr,
arguments poked into their BSS), wrapped in a small stub in buf_case that
times the call with the CIA2 timers (A counts phi2 cycles, B counts A's
underflows: 32-bit, exact in VICE).  A stub calling a bare RTS is the
baseline and is subtracted.  Two non-stopping exec checkpoints on
mem_read32 / mem_write32 count the memory-API calls (every allocator
metadata access is one of them: one 4-byte REU DMA each).

    python3 -m tests.heap_cost --prg build/w5-heap-wired/runner.prg [--json-out F]

Scenarios start from a pristine heap (ENV_STATE+0 = HEAP_BASE staged) and
build the neighbourhood they need with real calls.  VICE only.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys

from host import hle_env
from host.image import Chunk

STUB = 0xC700            # buf_case tail (test_load_chunk records are small)
RTS_AT = STUB + 0xF0
CPU_EXEC = 0x04
H = hle_env.HEAP_BASE


def stub_code(target: int) -> bytes:
    c = bytearray()
    c += bytes([0x78])                                  # sei
    c += bytes([0xA9, 0x7F, 0x8D, 0x0D, 0xDD])          # CIA2 ICR: mask all
    c += bytes([0xA9, 0x00, 0x8D, 0x0E, 0xDD, 0x8D, 0x0F, 0xDD])   # stop A, B
    c += bytes([0xA9, 0xFF])
    for r in (0x04, 0x05, 0x06, 0x07):                  # latches $FFFF
        c += bytes([0x8D, r, 0xDD])
    c += bytes([0xA9, 0x51, 0x8D, 0x0F, 0xDD])          # B: A underflows, load, start
    c += bytes([0xA9, 0x11, 0x8D, 0x0E, 0xDD])          # A: phi2, load, start
    c += bytes([0x20, target & 0xFF, target >> 8])      # jsr target
    c += bytes([0xA9, 0x00, 0x8D, 0x0E, 0xDD])          # stop A (B follows A)
    c += bytes([0x60])                                  # rts
    return bytes(c)


class Bench:
    def __init__(self, target, labels):
        from c64_test_harness.backends.vice_binary import CMD_CHECKPOINT_SET
        from tests.harness_util import lookup_label
        self.t = target
        self.tr = target.transport
        self.lab = lambda n: lookup_label(labels, n)
        self.cp = {}
        for name, lo, hi in (("mem_read32",) + (self.lab("mem_read32"),) * 2,
                             ("mem_write32",) + (self.lab("mem_write32"),) * 2,
                             ("instr", 0x0801, 0x9FFF)):   # the whole PRG
            body = struct.pack("<HHBBBB", lo, hi, 0x00, 0x01, CPU_EXEC, 0x00)
            resp = self.tr._send_and_recv(CMD_CHECKPOINT_SET, body)
            self.cp[name] = struct.unpack_from("<I", resp.body, 0)[0]
        from c64_test_harness import write_bytes
        write_bytes(self.tr, RTS_AT, b"\x60")
        self.base = 0
        self.base = self.timed(RTS_AT)["cycles"]    # (0 PRG instructions)

    def hits(self):
        h = {cp["number"]: cp["hit_count"] for cp in self.tr.checkpoint_list()}
        return {n: h[k] for n, k in self.cp.items()}

    def timed(self, addr: int) -> dict:
        from c64_test_harness import read_bytes, write_bytes
        from tests.harness_util import call_runner
        write_bytes(self.tr, STUB, stub_code(addr))
        h0 = self.hits()
        call_runner(self.t, STUB, 60.0)
        h1 = self.hits()
        a = read_bytes(self.tr, 0xDD04, 4)
        ta, tb = a[0] | a[1] << 8, a[2] | a[3] << 8
        cyc = (0xFFFF - ta) + 0x10000 * (0xFFFF - tb)
        return {"cycles": cyc - self.base,
                "reads": h1["mem_read32"] - h0["mem_read32"],
                "writes": h1["mem_write32"] - h0["mem_write32"],
                "instr": h1["instr"] - h0["instr"]}

    def poke(self, label: str, data: bytes):
        from c64_test_harness import write_bytes
        write_bytes(self.tr, self.lab(label), data)

    def peek(self, label: str, n: int) -> bytes:
        from c64_test_harness import read_bytes
        return read_bytes(self.tr, self.lab(label), n)

    def alloc(self, n: int, clear: bool = False, pool: int = 0):
        self.poke("hle_sz", struct.pack("<I", n))
        self.poke("hle_flg", bytes([1 if clear else 0]))
        if pool:
            from c64_test_harness import write_bytes
            write_bytes(self.tr, self.lab("r_a0"), struct.pack("<I", pool))
        m = self.timed(self.lab("hle_pool_alloc" if pool else "hle_alloc"))
        m["addr"] = struct.unpack("<I", self.peek("hle_old", 4))[0]
        return m

    def free(self, p: int, n: int):
        self.poke("hle_ptr", struct.pack("<I", p))
        self.poke("hle_sz", struct.pack("<I", n))
        return self.timed(self.lab("hle_free"))

    def pool_delete(self, pool: int):
        from c64_test_harness import write_bytes
        write_bytes(self.tr, self.lab("r_a0"), struct.pack("<I", pool))
        return self.timed(self.lab("hle_pool_delete"))


def fresh(sess):
    sess.stage([Chunk(hle_env.ENV_STATE, struct.pack(">I", H))])


def measure(sess, b: Bench) -> dict:
    out = {}

    def rec(name, m, expect_addr=None):
        out[name] = {k: m[k] for k in ("cycles", "instr", "reads", "writes")}
        acc = m["reads"] + m["writes"]
        extra = ""
        if expect_addr is not None:
            ok = m["addr"] == expect_addr
            extra = f"  addr=${m['addr']:06X}" + ("" if ok else f" (!! want ${expect_addr:06X})")
            if not ok:
                out[name]["bad_addr"] = m["addr"]
        print(f"  {name:34s} {m['cycles']:6d} cyc {m['instr']:5d} ins  {m['reads']:2d} R + "
              f"{m['writes']:2d} W = {acc:2d} DMA{extra}")

    fresh(sess)
    rec("alloc: first call (formats ctl)", b.alloc(64), H + 0x10)
    rec("alloc: wilderness", b.alloc(64), H + 0x58)
    b.alloc(64)                                       # C  = H+$A0
    b.alloc(64)                                       # D  = H+$E8
    rec("free: both neighbours used", b.free(H + 0x58, 64))
    rec("alloc: reuse, whole block", b.alloc(64), H + 0x58)
    b.free(H + 0x58, 64)
    rec("alloc: reuse, split", b.alloc(24), H + 0x58)
    rec("free: upper neighbour free", b.free(H + 0x58, 24))
    rec("free: lower neighbour free", b.free(H + 0xA0, 64))
    rec("free: lower free + wilderness", b.free(H + 0xE8, 64))
    rec("free: invalid (double free)", b.free(H + 0xE8, 64))
    rec("free: top block, lower used", (b.free(H + 0x10, 64)))

    # first-fit walk cost: k small holes in front of the fitting one
    fresh(sess)
    k = 10
    small = [b.alloc(8)["addr"] for _ in range(2 * k)]   # 16-byte blocks
    for i in range(0, 2 * k, 2):                         # holes, list LIFO
        b.free(small[i], 8)
    w0 = b.alloc(64)                                     # walks k holes
    fresh(sess)
    b.alloc(8)
    w1 = b.alloc(64)                                     # empty list
    per = {key: (w0[key] - w1[key]) / k
           for key in ("cycles", "instr", "reads", "writes")}
    out["walk: per skipped free block"] = per
    print(f"  {'walk: per skipped free block':34s} {per['cycles']:6.0f} cyc "
          f"{per['instr']:5.0f} ins  "
          f"{per['reads']:.0f} R + {per['writes']:.0f} W")

    # MEMF_CLEAR per byte
    fresh(sess)
    b.alloc(8)                                           # formats the heap
    c0 = b.alloc(4096)
    c1 = b.alloc(4096, clear=True)
    pb = (c1["cycles"] - c0["cycles"]) / 4096
    out["clear: per byte"] = {"cycles": pb,
                              "instr": (c1["instr"] - c0["instr"]) / 4096,
                              "writes": (c1["writes"] - c0["writes"]) / 4096}
    print(f"  {'MEMF_CLEAR: per byte':34s} {pb:6.1f} cyc "
          f"{(c1['instr'] - c0['instr']) / 4096:5.1f} ins  "
          f"({c1['writes'] - c0['writes']} W for 4096 bytes)")

    # DeletePool: per walked block
    fresh(sess)
    pool = b.alloc(8)["addr"]
    for _ in range(10):
        b.alloc(40, pool=pool)
        b.alloc(40)
    d10 = b.pool_delete(pool)
    fresh(sess)
    pool = b.alloc(8)["addr"]
    d0 = b.pool_delete(pool)
    rec("DeletePool: empty pool", d0)
    # walk only: an empty pool below 20 unrelated live blocks
    fresh(sess)
    pool = b.alloc(8)["addr"]
    for _ in range(20):
        b.alloc(40)
    dw = b.pool_delete(pool)
    per = {key: (dw[key] - d0[key]) / 20
           for key in ("cycles", "instr", "reads", "writes")}
    out["DeletePool: per block walked, not owned"] = per
    print(f"  {'DeletePool: per block walked only':34s} {per['cycles']:6.0f} cyc "
          f"{per['instr']:5.0f} ins  {per['reads']:.1f} R + {per['writes']:.1f} W")
    per = {key: (d10[key] - d0[key]) / 20
           for key in ("cycles", "instr", "reads", "writes")}
    out["DeletePool: per walked block"] = per
    print(f"  {'DeletePool: per block (avg of 20)':34s} {per['cycles']:6.0f} cyc "
          f"{per['instr']:5.0f} ins  "
          f"{per['reads']:.1f} R + {per['writes']:.1f} W")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--json-out")
    args = ap.parse_args(argv)
    prg = os.path.abspath(args.prg)
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_program import Session
    from tests.run_vectors import prepare_target
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            sess = Session(target=target, labels=labels, timeout=120.0)
            b = Bench(target, labels)
            print(f"stub baseline {b.base} cycles (subtracted)")
            out = measure(sess, b)
        finally:
            mgr.release(target)
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(out, f, indent=2)
    bad = [k for k, v in out.items() if "bad_addr" in v]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
