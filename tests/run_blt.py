#!/usr/bin/env python3
"""S3.6 blitter v0: guest legs (VICE), perf (VICE / U64E) and mutants.

docs/WAVE11_BRIEF.md C1/C3, docs/S3_PLAN.md §4 "S3.6", contract
docs/CONTRACTS.md "Blitter (S3.6)".  Reference: host/chipset_ref.Blitter
(through Chipset: Paula's DMACONR/BZERO and the INTREQ BLIT request) and the
recorded FS-UAE output of tests/chip_oracle.py.  Every case is a register
program (the chip oracle's op language) emitted as a 68000 guest program:
it writes DMACON = $8240, then the ops (a BLIT op writes BLTSIZE and polls
DMACONR BBUSY, as WaitBlit does), storing every recorded read at RECBUF.
Before each case the host resets the chips (chip_reset, cop_reset,
blt_reset) and stages the case's memory by raw REU DMA; afterwards it
reads the records and the dumped guest memory back.  A PRG without blt.s
(the base build) runs the same programs: its destinations stay unchanged,
so every blit case is red there.

Legs (one VICE session):
  oracle  every chip-oracle case made only of custom-register ops (the
          blitter cases: copy, cookie-cut $CA with shifts and masks, clear,
          descending shifted copy, fills, minterm sweep, size 0, BZERO,
          INTREQ BLIT, the BLTxDAT holds; Paula/DMACONR cases): records and
          dumps equal FS-UAE's recorded output AND the model, case by case
  rand    seeded random blits (channels, minterm, shifts, masks, modulos,
          ascending/descending, fills, CPU BLTBDAT writes, sizes up to 64
          words), each followed by a blit that continues from the written-
          back pointers: equal to the model
  big     AROS-shaped blits (CD/BCD $CA over 40 x 64 rows, a 64-word x 64-row
          D clear): equal to the model
  iobank  pointers in banks $DF/$BF and above 2 MB are masked into chip RAM
          ($1FFFFE): the REU under $DF0000 (the c2v LUT) and $BF0000 is
          unchanged, the masked addresses hold the model's bytes; rows that
          cross a 512 KB REC line and a descending blit wrapping below 0
  line    line mode: memory and pointers untouched, blt_nline + 1, INTREQ
          BLIT set, BZERO and the pixel epoch unchanged
  pix     disp_st+105 moves by one per blit with D on, not otherwise
  smc     a blit that rewrites the next instruction in the running code
          page: the new instruction runs (instruction window invalidated)
  trap    a long write whose second word is BLTSIZE (BLTDPTL + BLTSIZE) and
          one whose first is (BLTSIZE + BLTCON0L: the second still lands),
          a byte write to BLTSIZE (both halves), D1-D7/A0-A6 kept; a blit
          started by the copper (COPCON CDANG) equal to the model
  irq     exception mode (mach_run): INTENA $C040, IPL 0: the level-3
          interrupt is taken at the instruction after the BLTSIZE write

    python3 -m tests.run_blt --prg build/X/runner.prg [--legs a,b] [--json F]
    python3 -m tests.run_blt --prg ... --mutants [--only m1,m2]
    python3 -m tests.run_blt --prg ... --perf [--backend vice|u64] [--json F]

Exit 0 = every selected leg passed (--mutants: every mutant failed a leg).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import struct
import sys
import time

from tests import chip_oracle as co
from tests import run_irq, run_mach
from tests.run_mach import DMA_BUF, DMA_STUB, G, HALT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CUSTOM = 0xDFF000
CODE_AT = 0x40000           # guest program (above every case's data)
RECBUF = 0x41000            # recorded reads (a4)
A7 = 0x3F000
CHIP = 0x200000             # the model's chip range (Blitter._ptr: $1FFFFE)
DISP_ST, DISP_PIX = 0xB800, 105
PORT_RAM = 0x34
ZERO_SPANS = [(0x10000, 0x8000), (0x20000, 0x10000)]
VERBOSE = False


# --------------------------------------------------------------------------
# Guest programs from the op language
# --------------------------------------------------------------------------
# ("W", off, v16) word write   ("WL", off, v32) long write   ("WB", addr_off, v8)
# ("BLIT", size)               ("R", off, mask) record a word read
# ("HOLD", d1)                 (trap leg: registers are loaded before, checked after)

def emit(ops: list) -> list[int]:
    g = G()
    g(0x33FC, 0x8240, *g.abs(CUSTOM + 0x096))          # DMAEN | BLTEN
    g(0x49F9, *g.abs(RECBUF))                           # lea RECBUF,a4
    for op in ops:
        t = op[0]
        if t == "W":
            g(0x33FC, op[2] & 0xFFFF, *g.abs(CUSTOM + op[1]))
        elif t == "WL":
            g(0x23FC, op[2] >> 16, op[2] & 0xFFFF, *g.abs(CUSTOM + op[1]))
        elif t == "WB":
            g(0x13FC, op[2] & 0xFF, *g.abs(CUSTOM + op[1]))
        elif t == "BLIT":
            g(0x33FC, op[1], *g.abs(CUSTOM + 0x058))
        elif t == "R":
            g(0x3039, *g.abs(CUSTOM + op[1]))           # move.w abs,d0
            g(0x0240, op[2])                            # andi.w #m,d0
            g(0x38C0)                                   # move.w d0,(a4)+
        else:
            raise ValueError(op)
        if t in ("BLIT", "WL", "WB"):                   # WaitBlit: BBUSY
            g(0x0839, 0x0006, *g.abs(CUSTOM + 0x002), 0x66F6)
    g(HALT)
    return g.words()


def row_ram(ref):
    """A Ram whose word writes land only after a whole row (blt_rowlen words)
    was written: every read of a row sees memory as it was before the row,
    i.e. blt.s's row-wise fetch-then-store (rev-blt M1: in-row overlap)."""
    class RowRam(ref.Ram):
        rowlen = 1

        def __init__(self, size):
            super().__init__(size)
            self.pend = []

        def w16(self, a, v):
            self.pend.append((a, v))
            if len(self.pend) == self.rowlen:
                self.flush()

        def flush(self):
            for a, v in self.pend:
                ref.Ram.w16(self, a, v)
            self.pend = []
    return RowRam


def model_run(ops: list, init: list, dumps: list, ref=None, line_noop: bool = True,
              chip=None, rowwise: bool = False) -> dict:
    """The model fed the same CPU writes.  Line mode is a no-op (C1: logged
    and counted only; chipset_ref's line() is an unverified approximation).
    *rowwise*: reads of a row happen before its D writes (blt.s), not word by
    word (the model, FS-UAE): differs only for D inside a row of A/B/C."""
    ref = ref or co.load_ref()
    ram = row_ram(ref)(CHIP) if rowwise else ref.Ram(CHIP)
    for a, d in init:
        ram.w_block(a % CHIP, d)
    c = chip or ref.Chipset(ram)
    if rowwise:
        blit = c.blit

        def rblit(size, ecs=False):
            ram.rowlen = size & 0x3F or 64
            blit(size, ecs)
            ram.flush()
        c.blit = rblit
    if line_noop:
        c.blitter.line = lambda r, h: None
    c.custom_write(0x096, 0x8240)
    rec = []
    for op in ops:
        t = op[0]
        if t == "W":
            c.custom_write(op[1], op[2] & 0xFFFF)
        elif t == "WL":
            c.custom_write(op[1], op[2] >> 16)
            c.custom_write(op[1] + 2, op[2] & 0xFFFF)
        elif t == "WB":
            c.custom_write(op[1] & 0x1FE, (op[2] & 0xFF) * 0x101)
        elif t == "BLIT":
            c.custom_write(0x058, op[1])
        elif t == "R":
            rec.append(c.custom_read(op[1]) & op[2])
    return {"R": rec, "D": {f"{a:06x}": ram.r_block(a % CHIP, n).hex() for a, n in dumps},
            "chip": c}


# --------------------------------------------------------------------------
# The rig: run_irq.Rig (one session: plain HLE runs and mach_run runs)
# --------------------------------------------------------------------------

class BltRig:
    def __init__(self, rig: run_irq.Rig):
        self.rig = rig
        self.deadline = None            # per-leg wall-clock bound (U64E runs)
        self.blt = self.has("blt_reset")
        self.cop = self.has("cop_reset")

    def has(self, n: str) -> bool:
        try:
            self.rig.lab(n)
            return True
        except KeyError:
            return False

    def lab(self, n: str) -> int:
        return self.rig.lab(n)

    def u16(self, n: str) -> int:
        return struct.unpack("<H", self.rig.r(self.lab(n), 2))[0] if self.has(n) else 0

    def reset(self) -> None:
        self.rig.call(self.lab("chip_reset"))
        if self.cop:
            self.rig.call(self.lab("cop_reset"))
        if self.blt:
            self.rig.call(self.lab("blt_reset"))

    def rec_write(self, a: int, data: bytes) -> None:
        """Raw REC stash (bypasses the memory API: banks $BF/$DF too)."""
        rd = self.lab("mem_rec_dirty")
        for i in range(0, len(data), 256):
            part = data[i:i + 256]
            k, aa = len(part), a + i
            self.rig.w(DMA_BUF, part)
            c = bytearray()
            for reg, v in ((0x02, DMA_BUF & 0xFF), (0x03, DMA_BUF >> 8),
                           (0x04, aa & 0xFF), (0x05, (aa >> 8) & 0xFF),
                           (0x06, (aa >> 16) & 0xFF), (0x07, k & 0xFF), (0x08, k >> 8),
                           (0x0A, 0), (0x01, 0x90)):
                c += bytes([0xA9, v, 0x8D, reg, 0xDF])
            c += bytes([0x20, rd & 0xFF, rd >> 8, 0x60])
            self.rig.w(DMA_STUB, bytes(c))
            self.rig.call(DMA_STUB)

    def read(self, a: int, n: int) -> bytes:
        return self.rig.guest_read(a, n)

    def ram(self, addr: int, n: int) -> bytes:
        """C64 RAM under the ROMs (n <= 256), copied by a 6502 stub under sei
        with $01 = $34 into buf_case: a host write to $01 does not bank the
        U64E's host reads (they kept returning the BASIC ROM byte)."""
        from c64_test_harness import read_bytes, write_bytes
        stub, buf = 0xC700, 0xC000
        write_bytes(self.rig.t, stub, bytes([
            0x78, 0xA5, 0x01, 0x48, 0xA9, PORT_RAM, 0x85, 0x01, 0xA2, n - 1,
            0xBD, addr & 0xFF, addr >> 8, 0x9D, buf & 0xFF, buf >> 8, 0xCA, 0xE0, 0xFF,
            0xD0, 0xF5, 0x68, 0x85, 0x01, 0x58, 0x60]))
        self.rig.call(stub)
        return read_bytes(self.rig.t, buf, n)

    def shd(self, off: int) -> int:
        return struct.unpack(">H", self.rig.r(self.lab("chip_shd") + off, 2))[0]

    def run(self, ops: list, init: list, dumps: list, budget: int = 200_000,
            code: list[int] | None = None, reset: bool = True) -> dict:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise TimeoutError("leg timeout (U64_LEG_TIMEOUT)")
        if reset:
            self.reset()
        for a, d in init:
            self.rec_write(a, d)
        words = code if code is not None else emit(ops)
        self.rec_write(CODE_AT, b"".join(struct.pack(">H", w) for w in words))
        res = self.rig.sess.run_program(CODE_AT, A7, budget)
        nrec = sum(1 for op in ops if op[0] == "R")
        raw = self.read(RECBUF, 2 * nrec) if nrec else b""
        return {"R": list(struct.unpack(f">{nrec}H", raw)),
                "D": {f"{a:06x}": self.read(a, n).hex() for a, n in dumps},
                "res": res}


def ok_run(got: dict, tag: str, f: list[str]) -> bool:
    r = got["res"]
    if r.timeout or r.halt != 1 or r.reason != 0:
        f.append(f"{tag}: no clean HALT (halt=${r.halt:02X} reason={r.reason} "
                 f"timeout={r.timeout})")
        return False
    return True


def cmp(tag: str, got: dict, want: dict, who: str, f: list[str]) -> bool:
    bad = []
    if got["R"] != want["R"]:
        bad.append(f"reads {['%04X' % x for x in got['R']]} vs {who} "
                   f"{['%04X' % x for x in want['R']]}")
    for a, hx in want["D"].items():
        gh = got["D"].get(a)
        if gh != hx:
            base = int(a, 16)
            diff = [i for i in range(0, len(hx), 2) if gh[i:i + 2] != hx[i:i + 2]]
            bad.append(f"dump ${a}: {len(diff)} bytes differ (C64/{who}): " + ", ".join(
                f"${base + i // 2:06X} {gh[i:i + 2]}/{hx[i:i + 2]}" for i in diff[:5]))
    for b in bad:
        f.append(f"{tag}: {b}")
    return not bad


# --------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------

def oracle_cases() -> list:
    """The chip-oracle cases made of custom-register ops only that blit or
    read DMACONR (INTENA/INTREQ/ADKCON-only cases belong to other suites)."""
    return [c for c in co.cases() if all(op[0] in ("W", "BLIT", "R") for op in c.ops)
            and any(op[0] == "BLIT" or (op[0] == "R" and op[1] == 0x002) for op in c.ops)]


# rev-blt M1/L1 (documented, backlog): the C64 is pinned to the row-wise
# model for in-row overlap, to the model for odd modulos; FS-UAE differs.
ROWWISE_CASES = {"p_overlap_asc2", "p_overlap_asc"}
MODEL_ONLY_CASES = {"p_odd_mod"}


def leg_oracle(b: BltRig) -> list[str]:
    rec = json.load(open(co.RECORDED))
    if rec.get("case_hash") != co.case_hash(co.cases()):
        return ["oracle: tests/chip_oracle/fsuae_dumps.json is stale (re-record)"]
    f: list[str] = []
    n = 0
    for c in oracle_cases():
        got = b.run(c.ops, c.init, c.dumps)
        if not ok_run(got, c.name, f):
            continue
        hw = rec["cases"][c.name]
        if c.name in ROWWISE_CASES:
            m1 = True
            m2 = cmp(c.name, got, model_run(c.ops, c.init, c.dumps, rowwise=True),
                     "row-wise model", f)
        elif c.name in MODEL_ONLY_CASES:
            m1 = True
            m2 = cmp(c.name, got, model_run(c.ops, c.init, c.dumps), "model", f)
        else:
            m1 = cmp(c.name, got, {"R": hw["R"], "D": {k: v for k, v in hw["D"].items()}},
                     "FS-UAE", f)
            m2 = cmp(c.name, got, model_run(c.ops, c.init, c.dumps), "model", f)
        n += m1 and m2
        if VERBOSE:
            print(f"    {c.name}: {'MATCH' if m1 and m2 else 'DIFF'}")
    k = len(ROWWISE_CASES | MODEL_ONLY_CASES)
    print(f"    oracle: {n}/{len(oracle_cases())} cases pass ({len(oracle_cases()) - k} equal "
          f"FS-UAE and the model; {k} documented LIMITs pinned to the row-wise model / model)")
    return f


SRC_R, DST_R = 0x20000, 0x28000      # rand: sources / destination regions (32 KB each)


def rand_case(rng: random.Random, k: int) -> tuple[list, list, list]:
    w = rng.choice([1, 2, 3, 4, 7, 8, 13, 20, 40, 63, 64])
    h = rng.choice([1, 2, 3, 5, 8])
    con0 = rng.randrange(0x10000) | 0x0100 * rng.randrange(2)
    lf = rng.choice([0xCA, 0xF0, 0xCC, 0xAA, 0x00, 0xFF, rng.randrange(256)])
    con0 = (con0 & 0xFF00) | lf
    desc = rng.random() < 0.4
    fill = rng.choice([0, 0, 0, 0x08, 0x10, 0x18])
    con1 = (rng.randrange(16) << 12) | (2 if desc else 0) | fill | (4 * rng.randrange(2))
    mods = [2 * rng.randrange(-6, 7) for _ in range(4)]
    span = h * (2 * w + 12) + 64
    ptrs = []
    for j in range(3):                       # C, B, A: sources
        base = SRC_R + 0x2000 * j + 0x400 + 2 * rng.randrange(32)
        ptrs.append(base + (span if desc else 0))
    dbase = DST_R + 0x800 + 2 * rng.randrange(32)
    ptrs.append(dbase + (span if desc else 0))
    ops = [("W", 0x040, con0), ("W", 0x042, con1), ("W", 0x044, rng.randrange(0x10000)),
           ("W", 0x046, rng.randrange(0x10000))]
    for j, p in enumerate(ptrs):
        ops += [("W", 0x048 + 4 * j, p >> 16), ("W", 0x04A + 4 * j, p & 0xFFFF)]
    for j, m in enumerate(mods):
        ops.append(("W", 0x060 + 2 * j, m & 0xFFFF))
    ops += [("W", 0x070, rng.randrange(0x10000)), ("W", 0x074, rng.randrange(0x10000)),
            ("W", 0x072, rng.randrange(0x10000))]          # BLTBDAT last: CPU B shift
    sz = ((h & 0x3FF) << 6) | (w & 0x3F)
    ops += [("BLIT", sz), ("R", 0x002, 0x67FF), ("R", 0x01E, 0x0040)]
    ops += [("W", 0x09C, 0x0040), ("BLIT", sz), ("R", 0x002, 0x2000), ("R", 0x01E, 0x0040)]
    init = [(SRC_R, co.rnd(f"rand{k}s", 0x6000)), (DST_R, co.rnd(f"rand{k}d", 0x4000))]
    dumps = [(DST_R, 0x4000)]
    return ops, init, dumps


def leg_rand(b: BltRig, n: int = 64, seed: int = 0x5306) -> list[str]:
    rng = random.Random(seed)
    f: list[str] = []
    good = 0
    for k in range(n):
        ops, init, dumps = rand_case(rng, k)
        got = b.run(ops, init, dumps)
        if ok_run(got, f"rand{k}", f):
            good += cmp(f"rand{k}", got, model_run(ops, init, dumps), "model", f)
    print(f"    rand: {good}/{n} equal the model")
    return f


def aros_ops(con0: int, w: int, h: int, mod: int, a=None, b=None, c=None, d=None,
             adat=0xFFFF, bdat=0, fwm=0xFFFF, lwm=0xFFFF) -> list:
    return [*co.blit_setup(con0, 0, fwm, lwm, a=a, b=b, c=c, d=d, amod=mod, bmod=mod,
                           cmod=mod, dmod=mod, adat=adat, bdat=bdat),
            ("BLIT", ((h & 0x3FF) << 6) | (w & 0x3F)), ("R", 0x002, 0x2000)]


def leg_big(b: BltRig) -> list[str]:
    f: list[str] = []
    cases = {
        "cd_ca": aros_ops(0x03CA, 40, 64, 0, c=DST_R, d=DST_R, adat=0xFFFF, bdat=0x5A5A,
                          fwm=0x0FFF, lwm=0xFFF0),
        "bcd_ca": aros_ops(0x07CA, 40, 64, 0, b=SRC_R, c=DST_R, d=DST_R, adat=0xFFFF,
                           fwm=0x00FF, lwm=0xFF00),
        "clear": aros_ops(0x0100, 64, 64, 0, d=DST_R),
    }
    for name, ops in cases.items():
        init = [(SRC_R, co.rnd(name + "s", 0x2000)), (DST_R, co.rnd(name + "d", 0x2000))]
        dumps = [(DST_R, 0x2000)]
        got = b.run(ops, init, dumps)
        if ok_run(got, name, f):
            cmp(name, got, model_run(ops, init, dumps), "model", f)
    return f


def leg_iobank(b: BltRig) -> list[str]:
    """Pointers in the I/O banks / above 2 MB land in chip RAM (& $1FFFFE)."""
    f: list[str] = []
    lut = co.rnd("lut", 0x1000)
    cia = co.rnd("bfpage", 0x1000)
    b.rec_write(0xDF0000, lut)
    b.rec_write(0xBF0000, cia)
    srcm = co.rnd("io-src", 0x1000)
    init = [(0x1F0000, co.rnd("io-1f", 0x2000)), (0x1F8000, srcm),
            (0x07F000, co.rnd("io-7f", 0x2000)), (0x000100, bytes(0x100)),
            (0x1FF000, co.rnd("io-top", 0x1000))]
    ops = [  # D in $DF (the LUT bank), A in $BF: copy 16 x 4 words
           *co.blit_setup(0x09F0, a=0xBF8000, d=0xDF0040, amod=8, dmod=8), ("BLIT", (4 << 6) | 16),
           # D above 2 MB ($9F0100 -> $1F0100), C in $FF (-> $1F8000+), $CA with A const
           *co.blit_setup(0x03CA, c=0xFF8000 + 0x200, d=0x9F0100, cmod=0, dmod=0,
                          adat=0xF0F0, bdat=0x1234), ("BLIT", (3 << 6) | 20),
           # rows across the 512 KB line at $080000 (ascending, A->D)
           *co.blit_setup(0x09F0, a=0x07FFF0, d=0x07F800, amod=0, dmod=0), ("BLIT", (4 << 6) | 12),
           # descending D wrapping below 0 (-> $1FFFxx) and BZERO
           *co.blit_setup(0x01FF, 0x0002, d=0x000106, dmod=0x100), ("BLIT", (3 << 6) | 8),
           ("R", 0x002, 0x2000)]
    dumps = [(0x1F0000, 0x400), (0x07F800, 0x100), (0x1FF000, 0x1000), (0x000100, 0x10)]
    got = b.run(ops, init, dumps)
    if ok_run(got, "iobank", f):
        cmp("iobank", got, model_run(ops, init, dumps), "model", f)
    if b.read(0xDF0000, 0x1000) != lut:
        f.append("iobank: the REU under $DF0000 (disp_lut) was written")
    if b.read(0xBF0000, 0x1000) != cia:
        f.append("iobank: the REU under $BF0000 was written")
    return f


def leg_line(b: BltRig) -> list[str]:
    f: list[str] = []
    init = [(0x28000, co.rnd("line", 0x400))]
    ops = [*co.blit_setup(0x01FF, d=0x28000), ("BLIT", (1 << 6) | 1),     # BZERO := 0
           ("W", 0x09C, 0x0040),
           *co.blit_setup(0x0BCA, 0x0001, a=0x8000, c=0x28000, d=0x28000, amod=-40, cmod=40,
                          dmod=40, bdat=0xFFFF), ("BLIT", (30 << 6) | 2),
           ("R", 0x01E, 0x0040), ("R", 0x002, 0x2000)]
    dumps = [(0x28000, 0x400)]
    pix0 = b.ram(DISP_ST + DISP_PIX, 1)[0] if b.blt else 0
    got = b.run(ops, init, dumps)
    if not ok_run(got, "line", f):
        return f
    m = model_run(ops, init, dumps)
    cmp("line", got, m, "model", f)
    if got["D"]["028000"][4:] != init[0][1].hex()[4:]:
        f.append("line: line mode wrote memory")
    if b.u16("blt_nline") != 1:
        f.append(f"line: blt_nline = {b.u16('blt_nline')}, want 1")
    for off in (0x048, 0x050, 0x054):
        if b.shd(off + 2) != m["chip"].regs[(off + 2) >> 1]:
            f.append(f"line: pointer ${off:03X} written back")
    if b.blt and b.ram(DISP_ST + DISP_PIX, 1)[0] != (pix0 + 1) & 0xFF:
        f.append("line: the pixel epoch moved for a line blit (or not for the D blit)")
    return f


def leg_pix(b: BltRig) -> list[str]:
    f: list[str] = []
    if not b.blt:
        return ["pix: no blt.s in this PRG"]
    b.reset()
    init = [(0x28000, bytes(0x100))]
    seq = [(0x0100, True), (0x09F0, True), (0x08F0, False), (0x0EF0, False), (0x0FCA, True)]
    p0 = b.ram(DISP_ST + DISP_PIX, 1)[0]
    want = p0
    for con0, dd in seq:
        ops = [*co.blit_setup(con0, a=0x28000, b=0x28000, c=0x28000, d=0x28000), ("BLIT", 0x41)]
        got = b.run(ops, init, [], reset=False)
        ok_run(got, "pix", f)
        want = (want + dd) & 0xFF
        p = b.ram(DISP_ST + DISP_PIX, 1)[0]
        if p != want:
            f.append(f"pix: BLTCON0 ${con0:04X}: epoch {p}, want {want}")
    return f


def leg_epoch(b: BltRig) -> list[str]:
    """Addendum 3: the epoch is a read-modify-write of disp_st+105 (inc), not
    a private counter stored there: preset the byte to the value such a
    counter would store next; one D blit must still move it (+1)."""
    from c64_test_harness import read_bytes, write_bytes
    f: list[str] = []
    if not b.blt:
        return ["epoch: no blt.s in this PRG"]
    try:                                    # the old private counter, if any
        nxt = (read_bytes(b.rig.t, b.lab("blt_pix"), 1)[0] + 1) & 0xFF
    except KeyError:
        nxt = 0x5A
    a = DISP_ST + DISP_PIX
    write_bytes(b.rig.t, 0xC700, bytes([0x78, 0xA5, 0x01, 0x48, 0xA9, PORT_RAM, 0x85, 0x01,
                                        0xA9, nxt, 0x8D, a & 0xFF, a >> 8,
                                        0x68, 0x85, 0x01, 0x58, 0x60]))
    b.rig.call(0xC700)
    if b.ram(a, 1)[0] != nxt:
        return [f"epoch: preset to ${nxt:02X} did not land"]
    got = b.run([*co.blit_setup(0x0100, d=0x28000), ("BLIT", 0x41)], [], [], reset=False)
    ok_run(got, "epoch", f)
    v = b.ram(a, 1)[0]
    if v != (nxt + 1) & 0xFF:
        f.append(f"epoch: preset ${nxt:02X}, after a D blit ${v:02X}, want ${(nxt + 1) & 0xFF:02X} "
                 f"(a blit invisible to a reader whose snapshot equals the counter)")
    return f


def leg_smc(b: BltRig) -> list[str]:
    """moveq #0,d0 just after the BLTSIZE write is overwritten by the blit
    with moveq #1,d0 (A -> D, one word); d0 must be 1."""
    f: list[str] = []
    g = G()
    g(0x33FC, 0x8240, *g.abs(CUSTOM + 0x096))
    for off, v in ((0x040, 0x09F0), (0x042, 0), (0x044, 0xFFFF), (0x046, 0xFFFF),
                   (0x064, 0), (0x066, 0)):
        g(0x33FC, v, *g.abs(CUSTOM + off))
    g(0x23FC, 0, 0x8000, *g.abs(CUSTOM + 0x050))      # BLTAPT = $8000
    at = len(g.w)
    g(0x23FC, 0, 0, *g.abs(CUSTOM + 0x054))            # BLTDPT = (patched)
    g(0x33FC, 0x0041, *g.abs(CUSTOM + 0x058))          # BLTSIZE 1x1
    tgt = CODE_AT + 2 * len(g.w)
    g(0x7000)                                          # moveq #0,d0 (overwritten)
    g(HALT)
    words = g.words()
    words[at + 1], words[at + 2] = tgt >> 16, tgt & 0xFFFF
    got = b.run([], [(0x8000, bytes([0x70, 0x01]))], [], code=words)
    if ok_run(got, "smc", f) and got["res"].regs["D0"] != 1:
        f.append(f"smc: d0 = {got['res'].regs['D0']}, want 1 (stale instruction window)")
    return f


def leg_trap(b: BltRig) -> list[str]:
    f: list[str] = []
    init = [(0x28000, co.rnd("trap", 0x800)), (0x20000, co.rnd("traps", 0x800))]
    dumps = [(0x28000, 0x800)]
    ops = [*co.blit_setup(0x09F0, a=0x20000, d=0x28000, amod=0, dmod=0),
           ("WL", 0x056, (0x8100 << 16) | ((3 << 6) | 4)),          # BLTDPTL + BLTSIZE
           ("WL", 0x058, (((2 << 6) | 2) << 16) | 0xBEEF),          # BLTSIZE + BLTCON0L
           ("W", 0x040, 0x0100), ("W", 0x066, 0), ("W", 0x054, 0x0002),
           ("W", 0x056, 0x8400),
           ("WB", 0x059, 0x41),                                      # $4141: 261 rows x 1
           ("R", 0x002, 0x2000), ("R", 0x01E, 0x0040)]
    # load D1-D7 / A0-A3, A5, A6 with markers first, keep them to the end
    g = G()
    marks = {}
    for i in range(1, 8):
        marks[f"D{i}"] = 0x11111111 * i
        g(0x203C | i << 9, marks[f"D{i}"] >> 16, marks[f"D{i}"] & 0xFFFF)   # move.l #,Di
    for i in (0, 1, 2, 3, 5, 6):
        marks[f"A{i}"] = 0x00100000 + 0x1000 * i
        g(0x207C | i << 9, marks[f"A{i}"] >> 16, marks[f"A{i}"] & 0xFFFF)   # movea.l #,Ai
    code = g.words() + emit(ops)
    got = b.run(ops, init, dumps, code=code)
    if not ok_run(got, "trap", f):
        return f
    m = model_run(ops, init, dumps)
    cmp("trap", got, m, "model", f)
    for k, v in marks.items():
        if got["res"].regs[k] != v and not (k == "D0"):
            f.append(f"trap: {k} = ${got['res'].regs[k]:08X}, want ${v:08X}")
    if b.shd(0x05A) != 0xBEEF:
        f.append(f"trap: BLTCON0L slot ${b.shd(0x05A):04X}, want $BEEF (long write's "
                 f"second word after a blit)")
    f += copper_blit(b)
    return f


def copper_blit(b: BltRig) -> list[str]:
    """COPCON CDANG; a copper list sets the blitter up and writes BLTSIZE."""
    f: list[str] = []
    lst = 0x30000
    moves = [(0x040, 0x0DFC), (0x042, 0x3000), (0x044, 0xFFFF), (0x046, 0xFF00),
             (0x050, 0x0002), (0x052, 0x0000), (0x04C, 0x0002), (0x04E, 0x0400),
             (0x054, 0x0002), (0x056, 0x8000), (0x064, 2), (0x062, 2), (0x066, 2),
             (0x058, (5 << 6) | 6)]
    words = [x for r, v in moves for x in (r, v)] + [0xFFFF, 0xFFFE]
    init = [(lst, b"".join(struct.pack(">H", x) for x in words)),
            (0x20000, co.rnd("copsrc", 0x800)), (0x28000, co.rnd("copdst", 0x800))]
    ops = [("W", 0x02E, 0x0002), ("W", 0x080, lst >> 16), ("W", 0x082, lst & 0xFFFF),
           ("W", 0x088, 0), ("W", 0x096, 0x8280), ("W", 0x096, 0x0080),
           ("R", 0x01E, 0x0040), ("R", 0x002, 0x2000)]
    dumps = [(0x28000, 0x800)]
    got = b.run(ops, init, dumps)
    if not ok_run(got, "copper", f):
        return f
    mops = [("W", 0x02E, 0x0002)] + [("BLIT", v) if r == 0x058 else ("W", r, v)
                                      for r, v in moves] + ops[-2:]
    cmp("copper", got, model_run(mops, init, dumps), "model", f)
    return f


def leg_irq(b: BltRig) -> list[str]:
    f: list[str] = []
    rig = b.rig
    if not rig.seam:
        return ["irq: no machine loop in this PRG"]
    from tests.run_irq import SPC, CNT, move_sr, vec, link, clr_l, addq_l_abs
    g = G()
    vec(g, 27, "h3")
    clr_l(g, CNT)
    clr_l(g, SPC)
    for off, v in ((0x09A, 0x7FFF), (0x09C, 0x7FFF), (0x040, 0x0100), (0x042, 0),
                   (0x066, 0), (0x054, 0x0002), (0x056, 0x8000), (0x09A, 0xC040)):
        g(0x33FC, v, *g.abs(CUSTOM + off))
    move_sr(g, 0x2000)
    g(0x33FC, (2 << 6) | 2, *g.abs(CUSTOM + 0x058))
    g.label("after")
    g(0x4E71)                                           # nop
    g(0x4E71)
    g(HALT)
    g.label("h3")
    addq_l_abs(g, CNT)
    g(0x23EF, 0x0002, *g.abs(SPC))                      # move.l 2(sp),SPC
    g(0x33FC, 0x0040, *g.abs(CUSTOM + 0x09C))           # ack BLIT
    g(0x4E73)                                           # rte
    b.reset()
    (pc,) = rig.stage(link(g, run_mach.BASE))
    res = rig.run(pc, s=4, flags=run_irq.F_INIT | run_irq.F_SUPER, sr=0x27)
    if not run_irq.clean(res, "irq", f):
        return f
    cnt, spc = struct.unpack(">II", rig.guest_read(CNT, 4) + rig.guest_read(SPC, 4))
    want = pc + 2 * g.labels["after"]
    if res["nirq"] != 1 or cnt != 1 or spc != want:
        f.append(f"irq: deliveries {res['nirq']}, handler count {cnt}, stacked PC "
                 f"${spc:06X}, want 1, 1, ${want:06X}")
    return f


def leg_bank(b: BltRig) -> list[str]:
    """blt_w called straight from a host stub with $01 = $37 (BASIC ROM IN),
    $36 and $35 (under sei): the blit runs (BCODE needs BASIC out) and the
    caller's $01 comes back; a BLTBDAT write likewise."""
    from c64_test_harness import read_bytes, write_bytes
    f: list[str] = []
    if not b.blt:
        return ["bank: no blt.s in this PRG"]
    t, shd, bw = b.rig.t, b.lab("chip_shd"), b.lab("blt_w")
    stub, res = 0xC700, 0xC7F0
    for k, port in enumerate((0x37, 0x36, 0x35)):
        b.reset()
        dst = 0x28000 + 0x100 * k
        b.rec_write(dst, bytes(8))
        for r, v in ((0x040, 0x01FF), (0x042, 0), (0x054, dst >> 16), (0x056, dst & 0xFFFF),
                     (0x066, 0), (0x058, (1 << 6) | 3)):
            write_bytes(t, shd + r, struct.pack(">H", v))
        for x, tag in ((0x58, "BLTSIZE"), (0x72, "BLTBDAT")):
            write_bytes(t, res, b"\0\0")
            code = bytes([0x78, 0xA5, 0x01, 0x48,             # sei / lda $01 / pha
                          0xA9, port, 0x85, 0x01,              # lda #port / sta $01
                          0xA2, x, 0x20, bw & 0xFF, bw >> 8,   # ldx #x / jsr blt_w
                          0xA5, 0x01, 0x8D, res & 0xFF, res >> 8,   # lda $01 / sta res
                          0xA9, 0xA5, 0x8D, (res + 1) & 0xFF, res >> 8,
                          0x68, 0x85, 0x01, 0x58, 0x60])       # pla / sta $01 / cli / rts
            write_bytes(t, stub, code)
            b.rig.call(stub)
            got = read_bytes(t, res, 2)
            if got != bytes([port, 0xA5]):
                f.append(f"bank: {tag} with $01 = ${port:02X}: back with $01 = ${got[0]:02X} "
                         f"(done marker ${got[1]:02X})")
        if b.read(dst, 8) != b"\xff" * 6 + b"\0\0":
            f.append(f"bank: blit with $01 = ${port:02X} wrote {b.read(dst, 8).hex()}")
    return f


LEGS = {"oracle": leg_oracle, "rand": leg_rand, "big": leg_big, "iobank": leg_iobank,
        "line": leg_line, "pix": leg_pix, "epoch": leg_epoch, "smc": leg_smc, "trap": leg_trap,
        "irq": leg_irq, "bank": leg_bank}


def verify_bcode(rig, prg: str) -> str:
    """The PRG's bytes from $A000 (BCODE, RAM under BASIC ROM) read back by a
    6502 stub with BASIC banked out, page by page into buf_case: the load
    path really put the banked code there.  -> "" or the failure."""
    from c64_test_harness import read_bytes, write_bytes
    raw = open(prg, "rb").read()
    load, body = raw[0] | raw[1] << 8, raw[2:]
    top = 0xA000 - load
    if len(body) <= top:
        return ""                               # no BCODE in this PRG
    want = body[top:]
    got = bytearray()
    stub = 0xC700
    for pg in range(0, len(want), 256):
        a = 0xA000 + pg
        write_bytes(rig.t, stub, bytes([
            0x78, 0xA5, 0x01, 0x48, 0xA9, 0x36, 0x85, 0x01, 0xA2, 0x00,   # sei, $01 = $36
            0xBD, a & 0xFF, a >> 8, 0x9D, 0x00, 0xC0, 0xE8, 0xD0, 0xF7,   # copy one page
            0x68, 0x85, 0x01, 0x58, 0x60]))
        rig.call(stub)
        got += read_bytes(rig.t, 0xC000, 256)
    bad = [i for i in range(len(want)) if got[i] != want[i]]
    return (f"BCODE ${0xA000 + bad[0]:04X}: {len(bad)} of {len(want)} bytes differ"
            if bad else "")


def zero_fill(b: BltRig) -> None:
    for a, n in ZERO_SPANS:
        b.rec_write(a, bytes(n))


U64_LEG_TIMEOUT = 600.0         # s per leg on the U64E (checked before every case)


def run_legs(rig, prg: str, legs: list[str], leg_timeout: float | None = None) -> list[str]:
    b = BltRig(rig)
    bad = verify_bcode(rig, prg)
    print(f"[load] {'PASS' if not bad else 'FAIL'}: PRG {os.path.getsize(prg)} bytes, "
          f"BCODE verified with BASIC out{': ' + bad if bad else ''}")
    zero_fill(b)
    fails: list[str] = [f"load: {bad}"] if bad else []
    for name in legs:
        t0 = time.monotonic()
        b.deadline = None if leg_timeout is None else t0 + leg_timeout
        try:
            f = LEGS[name](b)
        except Exception as e:           # a hung/crashed leg is a failure
            f = [f"{name}: {type(e).__name__}: {e}"]
            print(f"[{name}] FAIL")
            for x in f:
                print(f"    {x}")
            fails += f
            break                        # the machine state is unknown
        print(f"[{name}] {'PASS' if not f else 'FAIL'} ({time.monotonic() - t0:.0f} s)")
        for x in f[:30]:
            print(f"    {x}")
        fails += f
    return fails


def run_all(prg: str, legs: list[str]) -> list[str]:
    return run_irq.session(prg, lambda rig: run_legs(rig, prg, legs))


# --------------------------------------------------------------------------
# Perf: blt_w timed directly (CIA2 phi2 count), two-point per shape
# --------------------------------------------------------------------------

PERF_SHAPES = {   # name: (BLTCON0, BLTCON1, fwm, lwm)
    "cd_ca": (0x03CA, 0x0000, 0x0FFF, 0xFFF0),     # the boot's shape (A, B constant)
    "bcd_ca": (0x07CA, 0x0000, 0xFFFF, 0xFFFF),
    "ad_copy": (0x09F0, 0x0000, 0xFFFF, 0xFFFF),
    "abcd_ca_sh": (0x4FCA, 0x4000, 0x0FFF, 0xFFF0),  # cookie-cut, A/B shift 4 (generic shift)
    "d_clear": (0x0100, 0x0000, 0xFFFF, 0xFFFF),
    "ad_fill": (0x09F0, 0x000A, 0xFFFF, 0xFFFF),
}
PERF_SIZES = ((1, 1), (20, 40))                    # (w, h): fixed cost, then per word


def perf(prg: str, backend: str, json_out: str | None) -> int:
    from c64_test_harness import read_bytes, write_bytes
    from tests.heap_cost import stub_code
    stub, trg = 0xC700, 0xC780

    res: dict = {}

    def body(rig, tag: str = "vice") -> int:
        b = BltRig(rig)
        if not b.blt:
            print("perf: no blt.s in this PRG")
            return 1
        t = rig.t
        shd = b.lab("chip_shd")
        bw = b.lab("blt_w")
        write_bytes(t, trg, bytes([0xA2, 0x58, 0x4C, bw & 0xFF, bw >> 8]))  # ldx #$58 / jmp blt_w
        write_bytes(t, stub, stub_code(trg))
        b.reset()
        out = {}
        for name, (c0, c1, fw, lw) in PERF_SHAPES.items():
            pts = []
            for w, h in PERF_SIZES:
                regs = {0x040: c0, 0x042: c1, 0x044: fw, 0x046: lw, 0x048: 2, 0x04A: 0x8000,
                        0x04C: 2, 0x04E: 0x0000, 0x050: 2, 0x052: 0x4000, 0x054: 2,
                        0x056: 0x8000, 0x058: (h << 6) | w, 0x060: 0, 0x062: 0, 0x064: 0,
                        0x066: 0, 0x070: 0x1234, 0x072: 0x5A5A, 0x074: 0xFFFF}
                for r, v in regs.items():
                    write_bytes(t, shd + r, struct.pack(">H", v))
                best = None
                for _ in range(3):
                    rig.call(stub, 300.0)
                    a = read_bytes(t, 0xDD04, 4)
                    cyc = (0xFFFF - (a[0] | a[1] << 8)) + 0x10000 * (0xFFFF - (a[2] | a[3] << 8))
                    best = cyc if best is None else min(best, cyc)
                pts.append((w * h, best))
            (n1, c1_), (n2, c2_) = pts
            per = (c2_ - c1_) / (n2 - n1)
            out[name] = {"fixed_phi2": c1_ - per * n1, "per_word_phi2": per,
                         "words_per_s_at_phi2": 985248 / per}
            print(f"  {name:12s} fixed {c1_ - per * n1:8.0f}  per word {per:7.1f} phi2 "
                  f"({985248 / per:8.0f} words/s at 1 MHz phi2 counts)")
        res[tag] = out
        if json_out:
            with open(json_out, "w") as fh:
                json.dump({"backend": backend, "prg": prg, "runs": res}, fh, indent=1)
        return 0

    if backend == "vice":
        return run_irq.session(prg, body)
    return u64_session(prg, body)


U64_LEGS = tuple(k for k in LEGS if k != "rand")   # rand stages ~40 KB per case
                                                     # over 128-byte REST PUTs (~1 h)


def u64_session(prg: str, fn, speeds=(1, 48), legs=U64_LEGS):
    """The U64E leg: ONE DeviceLock hold through tests/bench/u64_session.py's
    u64_hold (full config snapshot on entry, restore + diff on exit, also on
    error); the PRG is booted at 1 MHz, then *fn* runs once per CPU speed.
    The CIA2 timers count phi2 (~1 MHz) at every speed, so a count is ~us."""
    from tests.bench.u64_session import Hw, u64_hold
    out = os.path.join(ROOT, "build", "evidence", "w11-blt", "u64")
    os.makedirs(out, exist_ok=True)
    from c64_test_harness import Labels
    from tests.bench.u64bench import check_loaded
    from tests.run_vectors import prepare_target
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    raw = open(prg, "rb").read()
    main_only = os.path.join(out, "main-only.prg")      # up to $9FFF: the host's
    with open(main_only, "wb") as fh:                   # reads of $A000+ see BASIC ROM
        fh.write(raw[:2 + 0xA000 - (raw[0] | raw[1] << 8)])
    with u64_hold(out) as hold:
        hw = Hw(hold["target"], out)
        hw.speed(1)
        prepare_target(hw.target, prg)                  # the harness load path
        check_loaded(hw.target, main_only, labels)      # MAIN, byte for byte
        rig = run_irq.Rig(hw.target, labels, os.path.abspath(prg))
        bad = verify_bcode(rig, prg)                    # BCODE, read with BASIC out
        print(f"  U64E load: PRG {os.path.getsize(prg)} bytes, BCODE "
              f"{'verified' if not bad else 'FAIL ' + bad} (harness load + check_loaded)")
        rc = 1 if bad else 0
        for mhz in speeds:
            hw.speed(mhz)
            print(f"  --- U64E at {mhz} MHz")
            rc |= fn(rig, tag=f"u64-{mhz}mhz")
        print(f"  --- U64E at {speeds[-1]} MHz: legs {','.join(legs)}")
        fails = run_legs(rig, prg, list(legs), leg_timeout=U64_LEG_TIMEOUT)
        print(f"  U64E legs: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
        rc |= 1 if fails else 0
        hw.speed(1)
    print(f"  config restored: {hold.get('restored')}, reset ok: {hold.get('reset_ok')}")
    return rc


# --------------------------------------------------------------------------
# Teeth: source mutants of runtime/ops/blt.s (<= 10, docs/WAVE11_BRIEF.md C1)
# --------------------------------------------------------------------------

BLT = "runtime/ops/blt.s"
MUTANTS = {
    # minterm constants built from the LF bit pairs in the wrong order
    "lforder": (BLT, [("        sta blt_k,x\n        tya\n        jsr neg\n        sta blt_k+1,x\n",
                       "        sta blt_k+1,x\n        tya\n        jsr neg\n        sta blt_k,x\n")]),
    # descending: the row start not moved down by the row (adj ignored)
    "descaddr": (BLT, [("        sec                     ; row start := pointer - adj\n"
                        "        lda blt_r0,x\n        sbc blt_adj\n",
                        "        sec                     ; MUTANT\n"
                        "        lda blt_r0,x\n        sbc #0\n")]),
    # the fill carry does not restart at FCI on every row
    "fillfci": (BLT, [("        lda blt_c1\n        lsr\n        lsr\n        and #1\n        sta blt_fc\n",
                       "        lda #0\n        sta blt_fc\n")]),
    # AFWM and ALWM swapped
    "maskswap": (BLT, [("        lda blt_fpos\n        sta blt_pr+0\n",
                        "        lda blt_lpos\n        sta blt_pr+0\n"),
                       ("        ldx blt_fpos\n        lda blt_abuf,x\n        and chip_shd+BLT_AFWM\n",
                        "        ldx blt_lpos\n        lda blt_abuf,x\n        and chip_shd+BLT_AFWM\n"),
                       ("        ldx blt_lpos\n        lda blt_abuf,x\n        and chip_shd+BLT_AFWM+2\n",
                        "        ldx blt_fpos\n        lda blt_abuf,x\n        and chip_shd+BLT_AFWM+2\n")]),
    # a modulo ignored (the step is 2w only)
    "modulo": (BLT, [("        lda chip_shd+BLT_CMOD+1,y\n        clc\n        adc blt_wb\n",
                      "        lda #0\n        clc\n        adc blt_wb\n")]),
    # the CPU's BLTBDAT write is not pre-shifted (hold = the raw value)
    "bpre": (BLT, [("go:     jsr blt_shr\n        lda blt_q1\n        sta blt_bhold+1\n"
                    "        lda blt_q0\n        sta blt_bhold\n",
                    "go:     lda chip_vh\n        sta blt_bhold+1\n"
                    "        lda chip_vl\n        sta blt_bhold\n")]),
    # BZERO never set
    "bzero": (BLT, [("zero:   lda #BLT_BZERO\n", "zero:   lda #0\n")]),
    # the 2 MB chip mask dropped from the DMA bank
    "chipmask": (BLT, [("        and #BLT_BANKS          ; never $BF/$DF\n", ""),
                       ("        lda chip_shd+BLT_CPT+1,y\n        and #BLT_BANKS\n",
                        "        lda chip_shd+BLT_CPT+1,y\n")]),
    # no pixel epoch store
    "nopix": (BLT, [("        inc disp_st+BLT_EPOCH   ; C3 (Addendum 3): RMW of the epoch, BASIC out\n", "")]),
    # BCODE entered with BASIC ROM still in ($37 callers)
    "nobank": (BLT, [("        dec CPU_PORT            ; $37 -> $36: BASIC out (BCODE, row buffers)\n",
                      "")]),
}
MUTANT_LEGS = {"lforder": ["oracle"], "descaddr": ["oracle"], "fillfci": ["oracle"],
               "maskswap": ["oracle"], "modulo": ["oracle"], "bpre": ["oracle"],
               "bzero": ["oracle"], "chipmask": ["iobank"], "nopix": ["pix", "epoch"],
               "nobank": ["bank"]}


def build_mutant(name: str, out_dir: str) -> str:
    """run_mach.build_mutant (one cl65 link of the Makefile's sources with
    the mutated file); BLT_CFG (dev only) overrides the linker config."""
    import subprocess
    saved = dict(run_mach.MUTANTS)
    cfg = os.environ.get("BLT_CFG")
    real = subprocess.run
    def run(cmd, *a, **kw):
        if cfg and cmd[:2] == ["cl65", "-t"]:
            cmd = [cfg if c == "runtime/c64.cfg" else c for c in cmd]
        return real(cmd, *a, **kw)
    try:
        run_mach.MUTANTS.clear()
        run_mach.MUTANTS[name] = MUTANTS[name]
        run_mach.subprocess.run = run
        return run_mach.build_mutant(name, out_dir)
    finally:
        run_mach.subprocess.run = real
        run_mach.MUTANTS.clear()
        run_mach.MUTANTS.update(saved)


def teeth(prg: str, only: list[str] | None) -> int:
    bdir = os.path.relpath(os.path.dirname(os.path.abspath(prg)), ROOT)
    bad = []
    names = only or list(MUTANTS)
    for name in names:
        mprg = build_mutant(name, os.path.join(bdir, "blt-mut", name))
        print(f"=== mutant {name} (legs {','.join(MUTANT_LEGS[name])})")
        try:
            fails = run_all(mprg, MUTANT_LEGS[name])
        except Exception as e:
            fails = [f"{type(e).__name__}: {e}"]
        verdict = "KILLED" if fails else "SURVIVED"
        print(f"=== mutant {name}: {verdict}")
        if not fails:
            bad.append(name)
    print(f"teeth: {len(names) - len(bad)} killed, {len(bad)} survived {bad}")
    return 1 if bad else 0


def main(argv=None) -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default=",".join(LEGS))
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--perf", action="store_true")
    ap.add_argument("--backend", default="vice", choices=("vice", "u64"))
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    VERBOSE = a.verbose
    # a shell starts a backgrounded job with SIGINT ignored (SIG_IGN) and Python
    # keeps that: ^C/kill -INT then never reaches u64_hold's restore (W11-B finding)
    import signal
    signal.signal(signal.SIGINT, signal.default_int_handler)
    t0 = time.monotonic()
    if a.mutants:
        rc = teeth(a.prg, a.only.split(",") if a.only else None)
    elif a.perf:
        rc = perf(a.prg, a.backend, a.json)
    elif a.backend == "u64":           # functional legs on the U64E at 48 MHz
        legs = a.legs.split(",") if a.legs != ",".join(LEGS) else list(U64_LEGS)
        rc = u64_session(a.prg, lambda rig, tag: 0, speeds=(48,), legs=tuple(legs))
    else:
        fails = run_all(a.prg, a.legs.split(","))
        print(f"run_blt: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
        rc = 1 if fails else 0
    print(f"run_blt: wall-clock {time.monotonic() - t0:.0f} s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
