"""Chip oracle: our own m68k programs on FS-UAE's OCS chips vs host/chipset_ref.py.

W7-M (docs/WAVE7_BRIEF.md C1, docs/S3_PLAN.md §4 S3.0).  Every case below
is a short register program (blitter copy, cookie-cut $CA, clear,
descending shifted copy, area fill, ...; CIA one-shot/continuous timers, TB
counting TA, ICR read-clear and mask; Paula INTENA/INTREQ/DMACON).  The
same op list runs twice:

* **FS-UAE** (A500, OCS, 512 KB chip): ``emit_asm`` turns the ops into
  68000 code after ``tests/chip_oracle/head.s``, vasm builds a 256 KB ROM
  image of **our own** (no Kickstart, no AROS: the ROM *is* the test
  program, bare metal), FS-UAE boots it headless in warp and the program
  prints its recorded reads and memory dumps over the serial port.
* **the model**: ``run_model`` applies the ops to a fresh
  ``chipset_ref.Chipset`` over a ``chipset_ref.Ram``.

``diff`` compares the recorded reads and every dumped byte.  Reads whose
exact value depends on timing (a timer read a few E clocks after an event)
are recorded under a mask chosen so that only the rule under test is
visible; waits are polls ("until CRA bit 0 clears"), never fixed delays.

Usage:
    python3 -m tests.chip_oracle                  # build, run FS-UAE, diff
    python3 -m tests.chip_oracle --record          # ... and rewrite the recorded dumps
    python3 -m tests.chip_oracle --recorded        # diff against the recorded dumps (no FS-UAE)
    python3 -m tests.chip_oracle [--recorded] --mutant NAME   # the diff with a mutated model
    python3 -m tests.chip_oracle [--recorded] --mutants       # every mutant must be killed
        [--only a,b] [--keep DIR] [--timeout 120] [--json FILE]

The recorded FS-UAE output (``tests/chip_oracle/fsuae_dumps.json``, output
of our own programs only) lets ``host/tests/test_chipset_ref_oracle.py``
check the model and kill every mutant without an emulator.  Without FS-UAE
or vasm a live run prints ``SKIP chip_oracle:`` and exits 0.  Exit code 1
on any diff (for ``--mutants``: on any surviving mutant).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
ORACLE_DIR = os.path.join(HERE, "chip_oracle")
RECORDED = os.path.join(ORACLE_DIR, "fsuae_dumps.json")
CHIPSET_REF = os.path.join(PROJECT_ROOT, "host", "chipset_ref.py")
FS_UAE = os.environ.get("FS_UAE", "/Applications/FS-UAE.app/Contents/MacOS/fs-uae")
VBCC = os.environ.get("VBCC", os.path.expanduser("~/.local/opt/vbcc"))
ROM_SIZE = 0x40000
# The 68000 interrupt acknowledge reads the vector number from the ROM's last
# 16 bytes ($FFFFF0 + 2 * level, low byte); a Kickstart holds 24..31 there,
# i.e. the autovectors.  Zero padding made every interrupt take vector 0
# (w7-models-review REVIEW2 Q3, probe r2/iack/run.py).
IACK_OFF = ROM_SIZE - 16
IACK_BYTES = b"".join(bytes([0, 24 + i]) for i in range(8))
CHIP_SIZE = 0x80000
POLL_CAP = 2_000_000            # CWAIT gives up after this many polls (FS-UAE) ...
MODEL_POLL_CAP = 200_000        # ... or model polls (the longest real wait is ~32k)
TIMEOUT_REC = 0xDEAD            # ... and records this

CIA_BASE = {"A": 0xBFE001, "B": 0xBFD000}
REG = {"BLTCON0": 0x040, "BLTCON1": 0x042, "BLTAFWM": 0x044, "BLTALWM": 0x046,
       "BLTCPT": 0x048, "BLTBPT": 0x04C, "BLTAPT": 0x050, "BLTDPT": 0x054,
       "BLTCMOD": 0x060, "BLTBMOD": 0x062, "BLTAMOD": 0x064, "BLTDMOD": 0x066,
       "BLTCDAT": 0x070, "BLTBDAT": 0x072, "BLTADAT": 0x074, "DMACONR": 0x002,
       "INTENAR": 0x01C, "INTREQR": 0x01E, "DMACON": 0x096, "INTENA": 0x09A,
       "INTREQ": 0x09C}
CIAR = {"TALO": 4, "TAHI": 5, "TBLO": 6, "TBHI": 7, "ICR": 13, "CRA": 14, "CRB": 15}

# chip RAM layout of the blitter cases
SRC, SRCB, SRCC, DST, BIG = 0x10000, 0x11000, 0x12000, 0x13000, 0x14000
COPLIST = 0x15000


# ---- the op language ---------------------------------------------------------
# ("W", off, val)                       custom word write $DFF000+off
# ("BLIT", size)                        BLTSIZE write, then wait for BBUSY = 0
# ("R", off, mask)                      custom word read, record v & mask
# ("CW", cia, reg, val)                 CIA byte write (cia "A" or "B")
# ("CR", cia, reg, mask)                CIA byte read, record v & mask
# ("CWAIT", cia, reg, mask, want, rec)  poll until v & mask == want, then
#                                       record v & rec (rec 0: record nothing);
#                                       a timeout records $DEAD, which FS-UAE
#                                       must never produce (INCONCLUSIVE)
# ("FRAMES", n)                         wait for n vertical blanks (INTREQ
#                                       VERTB edges); the model runs the
#                                       copper, VERTB and CIA-A TOD per frame
# ("IPL", m)                            lower the CPU mask to m for a few NOPs:
#                                       record the level of the interrupt taken
#                                       (its handler clears INTEN), or 0
#
# Model time: every CIA access costs 1 E clock, a CWAIT poll 2; hsync every
# LINE_CYC/ECLK_DIV E clocks (CIA-B TOD), a frame every LINES lines.
@dataclass
class Case:
    name: str
    ops: list
    init: list = field(default_factory=list)       # [(addr, bytes)]
    dumps: list = field(default_factory=list)      # [(addr, nbytes)]
    rule: str = ""                                  # what it pins (docs)
    limit: str = ""         # a documented model limit: the model must raise
    diverge: str = ""       # W11-B: a documented divergence of the model from FS-UAE
                            # (backlog): a differing dump/read is the LIMIT verdict
                            # NotImplementedError; the verdict is LIMIT


def rnd(tag: str, n: int) -> bytes:
    """Deterministic pseudo-random bytes (sha256 counter mode)."""
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(f"{tag}:{i}".encode()).digest()
        i += 1
    return bytes(out[:n])


def w(name, val):
    return ("W", REG[name], val & 0xFFFF)


def ptr(name, addr):
    off = REG[name]
    return [("W", off, (addr >> 16) & 0xFFFF), ("W", off + 2, addr & 0xFFFF)]


def blit_setup(con0, con1=0, fwm=0xFFFF, lwm=0xFFFF, a=None, b=None, c=None, d=None,
               amod=0, bmod=0, cmod=0, dmod=0, adat=None, bdat=None, cdat=None):
    ops = [w("BLTCON0", con0), w("BLTCON1", con1), w("BLTAFWM", fwm), w("BLTALWM", lwm)]
    for nm, p in (("BLTAPT", a), ("BLTBPT", b), ("BLTCPT", c), ("BLTDPT", d)):
        if p is not None:
            ops += ptr(nm, p)
    ops += [w("BLTAMOD", amod), w("BLTBMOD", bmod), w("BLTCMOD", cmod), w("BLTDMOD", dmod)]
    for nm, v in (("BLTADAT", adat), ("BLTBDAT", bdat), ("BLTCDAT", cdat)):
        if v is not None:
            ops.append(w(nm, v))
    return ops


def size(h, wd):
    return ((h & 0x3FF) << 6) | (wd & 0x3F)


def blit_init(name, extra=()):
    """Random sources and a random destination (so C and untouched D bytes show)."""
    return [(SRC, rnd(name + "A", 512)), (SRCB, rnd(name + "B", 512)),
            (SRCC, rnd(name + "C", 512)), (DST, rnd(name + "D", 512)), *extra]


DUMP_DST = [(DST, 512)]


def edges(rows):
    """Fill-case source: rows of 16-bit words given as bit strings (MSB first)."""
    return b"".join(int(x, 2).to_bytes(2, "big") for r in rows for x in r)


FILL_ROWS = [
    ("0000000000000000", "0001000000010000"),   # one span inside word 2
    ("0000010000000000", "0000000000100000"),   # a span across the word boundary
    ("1000000000000001", "1000000000000001"),   # two spans at the extremes
    ("0000000000000000", "0000000000000100"),   # odd edge count: fill runs to the row end
    ("0110000000000000", "0000000000000110"),   # adjacent edges
    ("0000000000000000", "0000000000000000"),   # empty (FCI shows)
]


def fill_case(name, con1, rule):
    src = edges(FILL_ROWS)
    rows = len(FILL_ROWS)
    last = 4 * rows - 2                          # last word, descending start
    return Case(name, [*blit_setup(0x09F0, con1, a=SRC + last, d=DST + 64 + last),
                       ("BLIT", size(rows, 2))],
                init=blit_init(name, [(SRC, src)]), dumps=DUMP_DST, rule=rule)


def cia_prep(cia="B"):
    return [("CW", cia, CIAR["ICR"], 0x7F)]


def oneshot(cia, t="A", latch=0x0040):
    """Start timer t one-shot by the high-byte write, wait until it stops."""
    lo, hi, cr = (4, 5, 14) if t == "A" else (6, 7, 15)
    return [("CW", cia, cr, 0x08), ("CW", cia, lo, latch & 0xFF),
            ("CW", cia, hi, latch >> 8), ("CWAIT", cia, cr, 0x01, 0x00, 0)]


def cop_case(name, words, copcon, rule, limit=""):
    """A copper list at COPLIST (then END) runs for 3 frames with COPEN;
    what it did shows in INTREQR COPER (the lists end with MOVE INTREQ)."""
    data = b"".join(x.to_bytes(2, "big") for x in words + [0xFFFF, 0xFFFE])
    ops = [w("INTREQ", 0x7FFF), ("W", 0x02E, copcon), ("W", 0x080, COPLIST >> 16),
           ("W", 0x082, COPLIST & 0xFFFF), ("W", 0x096, 0x8280), ("FRAMES", 3),
           ("W", 0x096, 0x0080), ("W", 0x02E, 0), ("R", REG["INTREQR"], 0x0010)]
    return Case(name, ops, init=[(COPLIST, data)], rule=rule, limit=limit)


def ipl_case(name, setup, rule):
    return Case(name, [w("INTENA", 0x7FFF), w("INTREQ", 0x7FFF), *setup,
                       w("INTENA", 0x7FFF), w("INTREQ", 0x7FFF)], rule=rule)


def cases() -> list[Case]:
    cs: list[Case] = []
    add = cs.append
    # ---- blitter ------------------------------------------------------------
    add(Case("copy_a", [*blit_setup(0x09F0, a=SRC + 16, d=DST + 32, amod=10, dmod=10),
                        ("BLIT", size(4, 3))],
             init=blit_init("copy_a"), dumps=DUMP_DST, rule="A->D copy, modulos"))
    add(Case("copy_a_shift_mask",
             [*blit_setup(0x59F0, 0, 0xF0FF, 0xFF0E, a=SRC + 2, d=DST + 18, amod=8, dmod=8),
              ("BLIT", size(5, 4))],
             init=blit_init("copy_a_shift_mask"), dumps=DUMP_DST,
             rule="A shift 5, FWM/LWM before the shift, old A carried across rows"))
    add(Case("cookie_ca",
             [*blit_setup(0x7FCA, 0x7000, 0xFFFF, 0x0000, a=SRC, b=SRCB, c=DST + 40,
                          d=DST + 40, amod=2, bmod=6, cmod=10, dmod=10),
              ("BLIT", size(3, 3))],
             init=blit_init("cookie_ca"), dumps=DUMP_DST,
             rule="cookie-cut $CA, A/B shift 7, LWM 0, four channels, distinct modulos"))
    add(Case("clear_d", [*blit_setup(0x0100, d=DST + 4, dmod=4), ("BLIT", size(6, 2))],
             init=blit_init("clear_d"), dumps=DUMP_DST, rule="D-only clear, D modulo"))
    add(Case("desc_shift_copy",
             [*blit_setup(0x39F0, 0x0002, a=SRC + 16 + 52, d=DST + 32 + 52, amod=10, dmod=10),
              ("BLIT", size(4, 3))],
             init=blit_init("desc_shift_copy"), dumps=DUMP_DST,
             rule="descending A->D, A shift 3 (left), modulos subtracted"))
    add(Case("desc_ad_c0_bdat",
             [*blit_setup(0x19C0, 0x0002, a=SRC + 16 + 52, d=DST + 32 + 52, amod=10, dmod=10,
                          bdat=0x5555),
              ("BLIT", size(4, 3))],
             init=blit_init("desc_ad_c0_bdat"), dumps=DUMP_DST,
             rule="trackdisk-style decode: desc A shift 1 AND BDAT $5555 (B DMA off)"))
    add(fill_case("fill_ife", 0x000A, "inclusive fill, carry per row"))
    add(fill_case("fill_ife_fci", 0x000E, "inclusive fill, FCI = 1"))
    add(fill_case("fill_efe", 0x0012, "exclusive fill"))
    add(Case("b_shift",
             [*blit_setup(0x05CC, 0x9000, 0x0000, 0x0000, b=SRCB + 2, d=DST + 2, bmod=10, dmod=10),
              ("BLIT", size(4, 3))],
             init=blit_init("b_shift"), dumps=DUMP_DST,
             rule="B->D, B shift 9, masks 0 (B is never masked)"))
    add(Case("c_copy", [*blit_setup(0x03AA, c=SRCC, d=DST, cmod=2, dmod=10),
                        ("BLIT", size(4, 3))],
             init=blit_init("c_copy"), dumps=DUMP_DST, rule="C->D, C modulo"))
    sweep = []
    for k in range(8):
        sweep += [*blit_setup(0x0F00 | (1 << k), a=SRC, b=SRCB, c=SRCC, d=DST + 16 * k),
                  ("BLIT", size(1, 4))]
    add(Case("minterm_sweep", sweep, init=blit_init("minterm_sweep"), dumps=DUMP_DST,
             rule="each of the 8 minterm terms alone, A/B/C/D"))
    add(Case("minterm_mix",
             [*blit_setup(0x0F96, a=SRC, b=SRCB, c=SRCC, d=DST), ("BLIT", size(2, 8)),
              *blit_setup(0x0FE8, a=SRC + 64, b=SRCB + 64, c=SRCC + 64, d=DST + 64),
              ("BLIT", size(2, 8)),
              *blit_setup(0x0F1E, a=SRC + 128, b=SRCB + 128, c=SRCC + 128, d=DST + 128),
              ("BLIT", size(2, 8))],
             init=blit_init("minterm_mix"), dumps=DUMP_DST, rule="minterms $96 $E8 $1E"))
    add(Case("continue_ptr",
             [*blit_setup(0x09F0, a=SRC, d=DST, amod=4, dmod=6), ("BLIT", size(2, 2)),
              ("BLIT", size(2, 2))],
             init=blit_init("continue_ptr"), dumps=DUMP_DST,
             rule="pointers (after the last modulo) are written back"))
    add(Case("size_zero_w", [*blit_setup(0x01FF, d=DST + 64), ("BLIT", size(2, 0))],
             init=blit_init("size_zero_w"), dumps=DUMP_DST, rule="BLTSIZE width 0 = 64 words"))
    add(Case("size_zero_h", [*blit_setup(0x01FF, d=BIG + 16, dmod=2), ("BLIT", size(0, 1))],
             init=[(BIG, rnd("size_zero_h", 4128))], dumps=[(BIG, 4128)],
             rule="BLTSIZE height 0 = 1024 rows"))
    add(Case("width1_masks",
             [*blit_setup(0x49F0, 0, 0x0FF0, 0x3C3C, a=SRC, d=DST + 2, amod=2, dmod=2),
              ("BLIT", size(3, 1))],
             init=blit_init("width1_masks"), dumps=DUMP_DST,
             rule="width 1: FWM and LWM both apply; shift 4 carries across rows"))
    add(Case("neg_mod", [*blit_setup(0x09F0, a=SRC + 64, d=DST, amod=-22, dmod=10),
                         ("BLIT", size(3, 3))],
             init=blit_init("neg_mod"), dumps=DUMP_DST, rule="negative A modulo"))
    add(Case("adat_preload",
             [*blit_setup(0x41F0, 0, 0x00FF, 0xFF00, d=DST, dmod=10, adat=0xF00F),
              ("BLIT", size(2, 3))],
             init=blit_init("adat_preload"), dumps=DUMP_DST,
             rule="A DMA off: BLTADAT, masked then shifted 4"))
    add(Case("bdat_preload", [*blit_setup(0x01CC, d=DST, dmod=2, bdat=0x1234),
                              ("BLIT", size(3, 3))],
             init=blit_init("bdat_preload"), dumps=DUMP_DST, rule="B DMA off: BLTBDAT"))
    add(Case("cd_ca_consts",
             [*blit_setup(0x03CA, 0, 0x00FF, 0xFF00, c=SRCC, d=DST, cmod=2, dmod=10,
                          adat=0x0FF0, bdat=0xAAAA),
              ("BLIT", size(3, 3))],
             init=blit_init("cd_ca_consts"), dumps=DUMP_DST,
             rule="AROS text-style C,D $CA: constant A (masked) and B"))
    # ---- Paula ----------------------------------------------------------------
    add(Case("intena_setclr",
             [w("INTENA", 0x7FFF), w("INTENA", 0xC00C), ("R", REG["INTENAR"], 0xFFFF),
              w("INTENA", 0x0004), ("R", REG["INTENAR"], 0xFFFF),
              w("INTENA", 0x8000), ("R", REG["INTENAR"], 0xFFFF),
              w("INTENA", 0x7FFF), ("R", REG["INTENAR"], 0xFFFF)],
             rule="INTENA SET/CLR, INTENAR"))
    add(Case("intreq_softint",
             [("R", REG["INTREQR"], 0x0004), w("INTREQ", 0x8004), ("R", REG["INTREQR"], 0x0004),
              w("INTREQ", 0x0004), ("R", REG["INTREQR"], 0x0004)],
             rule="INTREQ SET/CLR (SOFTINT: no hardware source)"))
    add(Case("dmacon_setclr",
             [w("DMACON", 0x7FFF), w("DMACON", 0x8150), ("R", REG["DMACONR"], 0x03FF),
              w("DMACON", 0x0050), ("R", REG["DMACONR"], 0x03FF), w("DMACON", 0x7FFF)],
             rule="DMACON SET/CLR with DMAEN off, DMACONR enables"))
    add(Case("dmacon_bltpri",
             [w("DMACON", 0x7FFF), w("DMACON", 0x8400), ("R", REG["DMACONR"], 0x07FF),
              w("DMACON", 0x7FFF)],
             rule="DMACONR shows BLTPRI (bit 10)"))
    add(Case("blit_irq_bzero",
             [*blit_setup(0x0100, d=DST), w("INTREQ", 0x0040), ("BLIT", size(1, 2)),
              ("R", REG["INTREQR"], 0x0040), ("R", REG["DMACONR"], 0x2000),
              *blit_setup(0x01FF, d=DST + 16), w("INTREQ", 0x0040), ("BLIT", size(1, 2)),
              ("R", REG["INTREQR"], 0x0040), ("R", REG["DMACONR"], 0x2000)],
             init=blit_init("blit_irq_bzero"), dumps=DUMP_DST,
             rule="blit done raises INTREQ BLIT; BZERO after an all-zero / nonzero blit"))
    # ---- CIA --------------------------------------------------------------------
    B = "B"
    add(Case("cia_oneshot",
             [*cia_prep(), ("CW", B, 14, 0x08), ("CW", B, 4, 0x40), ("CW", B, 5, 0x00),
              ("CWAIT", B, 14, 0x01, 0x00, 0xFF), ("CR", B, 4, 0xFF), ("CR", B, 5, 0xFF),
              ("CR", B, 13, 0xFF), ("CR", B, 13, 0xFF)],
             rule="one-shot: the TAHI write starts it; at underflow START clears, the "
                  "counter reloads; ICR TA, then read-clear"))
    add(Case("cia_stopped_load",
             [*cia_prep(), ("CW", B, 4, 0x34), ("CW", B, 5, 0x12), ("CR", B, 4, 0xFF),
              ("CR", B, 5, 0xFF), ("CR", B, 14, 0xFF), ("CW", B, 6, 0x78), ("CW", B, 7, 0x56),
              ("CR", B, 6, 0xFF), ("CR", B, 7, 0xFF), ("CR", B, 15, 0xFF)],
             rule="continuous mode, stopped: the high-byte write loads the counter, no start"))
    add(Case("cia_force_load",
             [*cia_prep(), ("CW", B, 14, 0x01), ("CW", B, 4, 0x34), ("CW", B, 5, 0x12),
              ("CR", B, 5, 0xF0), ("CW", B, 14, 0x00), ("CR", B, 5, 0xF0),
              ("CW", B, 14, 0x10), ("CR", B, 4, 0xFF), ("CR", B, 5, 0xFF), ("CR", B, 14, 0xFF)],
             rule="running: the latch write does not load; LOAD strobe does (and reads 0)"))
    add(Case("cia_oneshot_start_bit",
             [*cia_prep(), ("CW", B, 4, 0x20), ("CW", B, 5, 0x00), ("CW", B, 14, 0x19),
              ("CWAIT", B, 14, 0x01, 0x00, 0xFF), ("CR", B, 13, 0xFF), ("CR", B, 13, 0xFF)],
             rule="CRA START|RUNMODE|LOAD starts a one-shot"))
    add(Case("cia_continuous",
             [*cia_prep(), ("CW", B, 4, 0x00), ("CW", B, 5, 0xF8), ("CW", B, 14, 0x11),
              ("CWAIT", B, 13, 0x01, 0x01, 0x03), ("CW", B, 14, 0x00),
              ("CR", B, 5, 0xF0), ("CR", B, 14, 0xFF), ("CW", B, 14, 0x11),
              ("CWAIT", B, 13, 0x01, 0x01, 0x03), ("CWAIT", B, 13, 0x01, 0x01, 0x03),
              ("CR", B, 14, 0xFF), ("CW", B, 14, 0x00)],
             rule="continuous: reloads from the latch and keeps running"))
    add(Case("cia_tb_counts_ta",
             [*cia_prep(), ("CW", B, 6, 0x00), ("CW", B, 7, 0x01), ("CW", B, 15, 0x41),
              ("CW", B, 4, 0x05), ("CW", B, 5, 0x00),
              *[op for _ in range(5) for op in (("CW", B, 14, 0x19),
                                                 ("CWAIT", B, 14, 0x01, 0x00, 0))],
              ("CW", B, 15, 0x40), ("CR", B, 6, 0xFF), ("CR", B, 7, 0xFF),
              ("CR", B, 13, 0x03)],
             rule="TB INMODE %10 counts TA underflows (5 one-shots -> TB $0100-5)"))
    add(Case("cia_tb_oneshot_under",
             [*cia_prep(), ("CW", B, 6, 0x03), ("CW", B, 7, 0x00), ("CW", B, 15, 0x49),
              ("CW", B, 4, 0x20), ("CW", B, 5, 0x00), ("CW", B, 14, 0x11),
              ("CWAIT", B, 13, 0x02, 0x02, 0x02), ("CR", B, 15, 0xFF), ("CR", B, 14, 0x01),
              ("CW", B, 14, 0x00), ("CR", B, 6, 0xFF), ("CR", B, 7, 0xFF)],
             rule="TB one-shot on TA underflows: stops and reloads, TA keeps running"))
    add(Case("cia_icr_readclear",
             [*cia_prep(), *oneshot(B), ("R", REG["INTREQR"], 0x2000), ("CR", B, 13, 0xFF),
              ("CR", B, 13, 0xFF), ("CW", B, 13, 0x81), *oneshot(B),
              ("R", REG["INTREQR"], 0x2000), ("CR", B, 13, 0xFF), ("CR", B, 13, 0xFF),
              ("R", REG["INTREQR"], 0x2000), w("INTREQ", 0x2000), ("R", REG["INTREQR"], 0x2000),
              ("CW", B, 13, 0x7F)],
             rule="ICR read clears; IR only when masked; CIA-B /INT -> INTREQ EXTER, "
                  "which stays until INTREQ is cleared"))
    add(Case("cia_icr_mask_after",
             [*cia_prep(), *oneshot(B), ("R", REG["INTREQR"], 0x2000), ("CW", B, 13, 0x81),
              ("R", REG["INTREQR"], 0x2000), ("CR", B, 13, 0xFF), ("CW", B, 13, 0x7F)],
             rule="setting the mask over a pending flag asserts /INT and IR"))
    add(Case("cia_icr_mask_clear",
             [*cia_prep(), ("CW", B, 13, 0x83), ("CW", B, 13, 0x01), *oneshot(B, "A"),
              ("CR", B, 13, 0xFF), *oneshot(B, "B", 0x0010), ("CR", B, 13, 0xFF),
              ("CW", B, 13, 0x7F)],
             rule="an ICR write with bit 7 = 0 clears only its bits of the mask"))
    add(Case("cia_int_level",
             [*cia_prep(), ("CW", B, 13, 0x81), *oneshot(B), ("R", REG["INTREQR"], 0x2000),
              w("INTREQ", 0x2000), ("R", REG["INTREQR"], 0x2000), ("CR", B, 13, 0xFF),
              w("INTREQ", 0x2000), ("R", REG["INTREQR"], 0x2000), ("CW", B, 13, 0x7F)],
             rule="/INT is a level: clearing INTREQ EXTER before the ICR read re-raises it"))
    add(Case("ciaa_oneshot_ports",
             [*cia_prep("A"), ("CW", "A", 13, 0x81), *oneshot("A"),
              ("R", REG["INTREQR"], 0x0008), ("CR", "A", 13, 0x83),
              w("INTREQ", 0x0008), ("R", REG["INTREQR"], 0x0008), ("CW", "A", 13, 0x7F)],
             rule="CIA-A (odd $BFExx1) /INT -> INTREQ PORTS"))
    # ---- fix round 1: the reviewer's 23 cases (w7-models-review/review_oracle.py) --
    add(cop_case("cop_control", [0x0180, 0x0123, 0x009C, 0x8010], 0,
                 "MOVE COLOR00 then INTREQ COPER"))
    add(cop_case("cop_blt_nodang", [0x0044, 0x1234, 0x009C, 0x8010], 0,
                 "OCS: MOVE to $40-$7E without CDANG stops the copper"))
    add(cop_case("cop_blt_dang", [0x0044, 0x1234, 0x009C, 0x8010], 2,
                 "OCS: MOVE to $40-$7E with CDANG is allowed"))
    add(cop_case("cop_low_dang", [0x0020, 0x0000, 0x009C, 0x8010], 2,
                 "OCS: MOVE below $40 stops the copper even with CDANG"))
    add(cop_case("cop_skip", [0x0001, 0xFFFF, 0x009C, 0x8010], 0,
                 "SKIP (always true) skips the next MOVE",
                 limit="copper SKIP is not modelled (chipset_ref.Copper raises)"))
    add(cop_case("cop_jmp2", [0x0084, COPLIST >> 16, 0x0086, (COPLIST + 0x100) & 0xFFFF,
                              0x008A, 0x0000, 0x0180, 0x0F00], 0,
                 "COPJMP2 to COP2LC (list 2 is empty: END) -> no COPER"))
    add(Case("b_off_bsh", [*blit_setup(0x01CC, 0x5000, d=DST, dmod=2, bdat=0xF00F),
                           ("BLIT", size(3, 3))],
             init=blit_init("b_off_bsh"), dumps=DUMP_DST,
             rule="B DMA off, BSH 5: BLTBDAT is shifted once at write time (hold)"))
    add(Case("a_stale_dat", [*blit_setup(0x09F0, a=SRC, d=DST), ("BLIT", size(1, 2)),
                             *blit_setup(0x01F0, d=DST + 64), ("BLIT", size(1, 2))],
             init=blit_init("a_stale_dat"), dumps=DUMP_DST,
             rule="A DMA off after an A blit: BLTADAT holds the last fetched word"))
    add(Case("c_stale_dat", [*blit_setup(0x03AA, c=SRCC, d=DST), ("BLIT", size(1, 2)),
                             *blit_setup(0x01AA, d=DST + 64), ("BLIT", size(1, 2))],
             init=blit_init("c_stale_dat"), dumps=DUMP_DST,
             rule="C DMA off after a C blit: BLTCDAT holds the last fetched word"))
    add(Case("a_old_across_blits",
             [*blit_setup(0x09F0, a=SRC, d=DST), ("BLIT", size(1, 2)),
              *blit_setup(0x49F0, a=SRC + 64, d=DST + 64), ("BLIT", size(1, 2))],
             init=blit_init("a_old_across_blits"), dumps=DUMP_DST,
             rule="the A old word starts at 0 in each blit"))
    add(Case("desc_masks",
             [*blit_setup(0x29F0, 0x0002, 0x0FF0, 0xF00F, a=SRC + 16 + 52, d=DST + 32 + 52,
                          amod=10, dmod=10), ("BLIT", size(4, 3))],
             init=blit_init("desc_masks"), dumps=DUMP_DST,
             rule="descending: FWM on the first (rightmost) word, LWM on the last"))
    add(Case("desc_cookie_bsh",
             [*blit_setup(0x6FCA, 0x5002, 0xFFFF, 0x00FF, a=SRC + 40, b=SRCB + 60, c=DST + 100,
                          d=DST + 100, amod=2, bmod=6, cmod=10, dmod=10), ("BLIT", size(3, 3))],
             init=blit_init("desc_cookie_bsh"), dumps=DUMP_DST,
             rule="descending cookie-cut, A shift 6, B shift 5"))
    fsrc = edges(FILL_ROWS)
    add(Case("fill_ascending", [*blit_setup(0x09F0, 0x0008, a=SRC, d=DST + 64),
                                ("BLIT", size(6, 2))],
             init=blit_init("fill_ascending", [(SRC, fsrc)]), dumps=DUMP_DST,
             rule="IFE in ascending mode"))
    flast = 4 * len(FILL_ROWS) - 2
    add(Case("fill_both", [*blit_setup(0x09F0, 0x001A, a=SRC + flast, d=DST + 64 + flast),
                           ("BLIT", size(6, 2))],
             init=blit_init("fill_both", [(SRC, fsrc)]), dumps=DUMP_DST,
             rule="IFE and EFE both set: exclusive"))
    add(Case("bzero_d_off", [*blit_setup(0x08F0, a=SRC), ("BLIT", size(2, 2)),
                             ("R", REG["DMACONR"], 0x2000),
                             *blit_setup(0x0800, a=SRC), ("BLIT", size(2, 2)),
                             ("R", REG["DMACONR"], 0x2000)],
             init=blit_init("bzero_d_off"), dumps=DUMP_DST,
             rule="BZERO computed with D DMA off"))
    add(Case("intreq_bit14", [w("INTREQ", 0x7FFF), w("INTREQ", 0xC000),
                              ("R", REG["INTREQR"], 0xFFFF), w("INTREQ", 0x7FFF),
                              ("R", REG["INTREQR"], 0xFFFF)],
             rule="INTREQ bit 14 readback"))
    add(Case("adkcon_setclr", [("W", 0x09E, 0x7FFF), ("W", 0x09E, 0x8C05), ("R", 0x010, 0xFFFF),
                               ("W", 0x09E, 0x0401), ("R", 0x010, 0xFFFF),
                               ("W", 0x09E, 0x7FFF)],
             rule="ADKCON SET/CLR, ADKCONR"))
    add(Case("dmacon_hi_bits", [w("DMACON", 0x7FFF), w("DMACON", 0x9800),
                                ("R", REG["DMACONR"], 0x1FFF), w("DMACON", 0x7FFF),
                                w("DMACON", 0x8240)],
             rule="DMACON bits 11/12 readback"))
    add(Case("cia_latch0_tb",
             [*cia_prep(), ("CW", B, 6, 0x10), ("CW", B, 7, 0x00), ("CW", B, 15, 0x41),
              ("CW", B, 4, 0x00), ("CW", B, 5, 0x00), ("CW", B, 14, 0x01),
              ("CWAIT", B, 13, 0x02, 0x02, 0x02), ("CW", B, 14, 0x00), ("CW", B, 15, 0x00),
              ("CR", B, 4, 0xFF), ("CR", B, 5, 0xFF)],
             rule="TA latch 0 continuous: underflows every E clock; TB counts them"))
    add(Case("cia_tb_counts_ta_cont",
             [*cia_prep(), ("CW", B, 6, 0x07), ("CW", B, 7, 0x00), ("CW", B, 15, 0x49),
              ("CW", B, 4, 0x03), ("CW", B, 5, 0x00), ("CW", B, 14, 0x11),
              ("CWAIT", B, 15, 0x01, 0x00, 0), ("CW", B, 14, 0x00), ("CR", B, 13, 0x02),
              ("CR", B, 6, 0xFF)],
             rule="TB one-shot counting a continuous TA (latch 3): underflows after latch+1"))
    add(Case("cia_oneshot_tb_hi_start",
             [*cia_prep(), ("CW", B, 15, 0x08), ("CW", B, 6, 0x30), ("CW", B, 7, 0x00),
              ("CR", B, 15, 0x01), ("CWAIT", B, 15, 0x01, 0x00, 0xFF), ("CR", B, 13, 0x02)],
             rule="TB one-shot: TBHI write starts it"))
    add(Case("cia_tb_exact_count",
             [*cia_prep(), ("CW", B, 6, 0x02), ("CW", B, 7, 0x00), ("CW", B, 15, 0x49),
              ("CW", B, 4, 0x05), ("CW", B, 5, 0x00),
              *[op for _ in range(2) for op in (("CW", B, 14, 0x19),
                                                 ("CWAIT", B, 14, 0x01, 0x00, 0))],
              ("CR", B, 13, 0x02), ("CR", B, 15, 0x01), ("CR", B, 6, 0xFF),
              ("CW", B, 14, 0x19), ("CWAIT", B, 14, 0x01, 0x00, 0),
              ("CR", B, 13, 0x02), ("CR", B, 15, 0x01)],
             rule="TB latch 2 counting TA one-shots: underflows on the 3rd, not the 2nd"))
    add(Case("cia_icr_mask_accumulate",
             [*cia_prep(), ("CW", B, 13, 0x82), ("CW", B, 13, 0x81), *oneshot(B, "B", 0x0010),
              ("CR", B, 13, 0xFF), ("CW", B, 13, 0x7F)],
             rule="ICR SET writes accumulate mask bits"))
    # ---- fix round 1: the other gaps the review lists ------------------------------
    j2 = cop_case("cop_jmp2_list2", [0x0084, COPLIST >> 16, 0x0086, (COPLIST + 0x100) & 0xFFFF,
                                     0x008A, 0x0000], 0,
                  "COPJMP2 runs list 2 (MOVE INTREQ COPER there), not list 1 again")
    j2.init.append((COPLIST + 0x100, bytes.fromhex("009c8010fffffffe")))
    add(j2)
    add(Case("cia_period_latch_plus_1",
             [*cia_prep(), ("CW", B, 15, 0x41), ("CW", B, 4, 0x01), ("CW", B, 5, 0x00),
              ("CW", "A", 14, 0x08), ("CW", "A", 4, 0xE7), ("CW", "A", 5, 0x03),
              ("CW", B, 14, 0x01), ("CWAIT", "A", 14, 0x01, 0x00, 0),
              ("CW", B, 14, 0x00), ("CW", B, 15, 0x40), ("CR", B, 7, 0xFF), ("CR", B, 6, 0xC0)],
             rule="period = latch + 1: CIA-B TA (latch 1) underflows ~500 times in the "
                  "1000 E clocks of a CIA-A one-shot (TB = $FFFF - ~500)"))
    add(Case("desc_masks_bzero",
             [*blit_setup(0x0DC0, 0x0002, 0x00FF, 0xFF00, a=SRC + 30, b=SRCB + 30, d=DST + 30),
              ("BLIT", size(2, 4)), ("R", REG["DMACONR"], 0x2000),
              *blit_setup(0x0D0F, 0x0002, 0x0000, 0x0000, a=SRC + 30, b=SRCB + 30,
                          d=DST + 94), ("BLIT", size(2, 4)), ("R", REG["DMACONR"], 0x2000)],
             init=blit_init("desc_masks_bzero", [(SRCB, b"\x00" * 64)]), dumps=DUMP_DST,
             rule="descending A.B with masks (B = 0: BZERO set), then ~A.~B (nonzero)"))
    # ---- W11-B (S3.6 blitter v0): shapes the C64 blitter takes special paths for --
    add(Case("aros_cd_ca_wide",
             [*blit_setup(0x03CA, 0, 0x0FFF, 0xFFF0, c=BIG, d=BIG, adat=0xFFFF, bdat=0x5A5A),
              ("BLIT", size(40, 20)), ("R", REG["DMACONR"], 0x2000)],
             init=[(BIG, rnd("aros_cd_ca_wide", 4128))], dumps=[(BIG, 4128)],
             rule="AROS text/clear shape: C,D $CA, constant A (masked) and B, 20 words x 40"))
    add(Case("aros_bcd_ca",
             [*blit_setup(0x07CA, 0, 0x00FF, 0xFF00, b=SRCB, c=DST, d=DST, bmod=4, cmod=8,
                          dmod=8, adat=0xFFFF), ("BLIT", size(12, 8))],
             init=blit_init("aros_bcd_ca"), dumps=DUMP_DST,
             rule="AROS shape: B,C,D $CA, constant A with masks, distinct modulos"))
    add(Case("desc_fill_ash",
             [*blit_setup(0x49F0, 0x000E, 0xFF0F, 0xF0FF, a=SRC + flast, d=DST + 64 + flast),
              ("BLIT", size(6, 2)), ("R", REG["DMACONR"], 0x2000)],
             init=blit_init("desc_fill_ash", [(SRC, fsrc)]), dumps=DUMP_DST,
             rule="descending, A shift 4 (left) with masks, then inclusive fill with FCI"))
    add(Case("bdat_desc_bsh",
             [*blit_setup(0x01CC, 0x7002, d=DST + 40, dmod=2, bdat=0xF00F), ("BLIT", size(3, 3)),
              w("BLTBDAT", 0x8001), ("BLIT", size(2, 2))],
             init=blit_init("bdat_desc_bsh"), dumps=DUMP_DST,
             rule="B DMA off, descending, BSH 7: the CPU's BLTBDAT is shifted left "
                  "alone, no previous B word enters (Wave 11 write_bdat ruling)"))
    add(Case("bdat_asc_prev_cpu",
             [*blit_setup(0x01CC, 0x5000, d=DST + 4, dmod=2, bdat=0xF00F), w("BLTBDAT", 0x8001),
              ("BLIT", size(2, 2))],
             init=blit_init("bdat_asc_prev_cpu"), dumps=DUMP_DST,
             rule="two CPU BLTBDAT writes, ascending BSH 5: the second is shifted alone, "
                  "not against the first"))
    add(Case("bdat_after_bdma",
             [*blit_setup(0x05CC, 0x5000, b=SRCB, d=DST, dmod=0, bmod=0), ("BLIT", size(1, 3)),
              w("BLTBDAT", 0x8001), w("BLTCON0", 0x01CC), ("BLIT", size(2, 2))],
             init=blit_init("bdat_after_bdma"), dumps=DUMP_DST,
             rule="a CPU BLTBDAT write after a B-DMA blit, ascending BSH 5: shifted alone, "
                  "not against the last fetched B"))
    add(Case("b_stale_hold",
             [*blit_setup(0x05CC, 0x5000, b=SRCB, d=DST, dmod=0, bmod=0), ("BLIT", size(1, 3)),
              w("BLTCON0", 0x01CC), ("BLIT", size(2, 2))],
             init=blit_init("b_stale_hold"), dumps=DUMP_DST,
             rule="B DMA off after a B-DMA blit (BSH 5): the hold register is the last "
                  "shifted B"))
    # W11-B review (rev-blt) M1/L1: documented divergences, pinned here (backlog)
    add(Case("p_overlap_asc2", [*blit_setup(0x09F0, a=DST, d=DST + 4), ("BLIT", size(1, 12))],
             init=blit_init("p_overlap_asc2"), dumps=DUMP_DST,
             rule="D two words ahead of A in one row: the model (word-serial) equals FS-UAE; "
                  "blt.s fetches the whole row before writing D (run_blt: row-wise LIMIT)"))
    add(Case("p_overlap_asc", [*blit_setup(0x09F0, a=DST, d=DST + 2), ("BLIT", size(2, 8))],
             init=blit_init("p_overlap_asc"), dumps=DUMP_DST,
             diverge="D one word ahead of A in a row: FS-UAE writes D one word late (its "
                     "pipeline), the word-serial model smears; blt.s is row-wise (backlog)",
             rule="in-row overlap at distance 1, ascending"))
    add(Case("p_odd_mod", [*blit_setup(0x09F0, a=SRC, d=DST, amod=3, dmod=5), ("BLIT", size(4, 4))],
             init=blit_init("p_odd_mod"), dumps=DUMP_DST,
             diverge="FS-UAE ignores modulo bit 0; the model and blt.s honour it (backlog)",
             rule="odd A/D modulos"))
    add(Case("wide_shift_negmod",
             [*blit_setup(0x99F0, 0, 0x7FFF, 0xFFFE, a=SRC + 2, d=DST, amod=-2, dmod=0),
              ("BLIT", size(2, 0))],
             init=blit_init("wide_shift_negmod"), dumps=DUMP_DST,
             rule="width 0 = 64 words, A shift 9 carried across rows, negative A modulo"))
    add(ipl_case("ipl_levels",
                 [op for b in range(14) for op in (w("INTREQ", 0x8000 | 1 << b),
                                                    w("INTENA", 0xC000 | 1 << b), ("IPL", 0),
                                                    w("INTENA", 0x7FFF), w("INTREQ", 1 << b))],
                 "each INTREQ bit alone reaches the CPU at its autovector level"))
    add(ipl_case("ipl_priority_mask",
                 [w("INTREQ", 0xA014), w("INTENA", 0x8000 | 0x2014), ("IPL", 0),
                  w("INTENA", 0xC000), ("IPL", 0), w("INTREQ", 0x2000),
                  w("INTENA", 0xC000), ("IPL", 0), w("INTREQ", 0x0010),
                  w("INTENA", 0xC000), ("IPL", 1), ("IPL", 0)],
                 "no INTEN: nothing; highest pending first; the CPU mask holds level <= mask"))
    add(Case("tod_hi_halts",
             [("CW", "A", 10, 0x00), ("CW", "A", 9, 0x00), ("CW", "A", 8, 0x34),
              ("CW", "A", 10, 0x00), ("CW", "A", 9, 0x56), ("FRAMES", 3),
              ("CR", "A", 10, 0xFF), ("CR", "A", 9, 0xFF), ("CR", "A", 8, 0xFF),
              ("CW", "A", 8, 0x00), ("CWAIT", "A", 8, 0xFF, 0x01, 0), ("CR", "A", 9, 0xFF)],
             rule="CIA-A TOD (VSYNC): a TODHI write halts it, the TODLO write restarts it"))
    add(Case("tod_mid_no_halt",
             [("CW", "A", 10, 0x00), ("CW", "A", 9, 0x00), ("CW", "A", 8, 0x00),
              ("CW", "A", 9, 0x00), ("CWAIT", "A", 8, 0xFF, 0x02, 0), ("CR", "A", 9, 0xFF)],
             rule="a TODMID write does not halt TOD"))
    add(Case("tod_latch",
             [("CW", B, 10, 0x00), ("CW", B, 9, 0x00), ("CW", B, 8, 0x00),
              ("CR", B, 10, 0xFF), ("CR", B, 9, 0xFF), ("CR", B, 8, 0xF0),
              ("CWAIT", B, 9, 0xFF, 0x01, 0), ("CR", B, 10, 0xFF), ("FRAMES", 2),
              ("CR", B, 9, 0xFF), ("CR", B, 8, 0x00)],
             rule="CIA-B TOD (HSYNC): a TODHI read latches all three until TODLO is read"))
    add(Case("tod_alarm",
             [*cia_prep(), ("CW", B, 15, 0x80), ("CW", B, 10, 0x00), ("CW", B, 9, 0x00),
              ("CW", B, 8, 0x40), ("CW", B, 15, 0x00), ("CW", B, 10, 0x00), ("CW", B, 9, 0x00),
              ("CW", B, 8, 0x00), ("CWAIT", B, 13, 0x04, 0x04, 0x04), ("CR", B, 13, 0x04),
              ("CW", B, 15, 0x80), ("CW", B, 10, 0xFF), ("CW", B, 9, 0xFF), ("CW", B, 8, 0xFF),
              ("CW", B, 15, 0x00)],
             rule="TOD alarm (CRB bit 7 selects it) sets ICR bit 2 at the match; read clears"))
    return cs


# ---- the FS-UAE side -----------------------------------------------------------

def cia_addr(cia, reg):
    return CIA_BASE[cia] + reg * 0x100


def emit_case(k: int, c: Case) -> str:
    L = [f"; ---- {c.name}: {c.rule}", f"case{k}:", "        bsr     prologue"]
    for j, (addr, data) in enumerate(c.init):
        L += [f"        lea     c{k}d{j},a0", f"        lea     ${addr:X},a1",
              f"        move.w  #{len(data)},d2", "        bsr     copy"]
    for j, op in enumerate(c.ops):
        t = op[0]
        if t == "W":
            L.append(f"        move.w  #${op[2]:04X},${op[1]:X}(a6)")
        elif t == "BLIT":
            L += [f"        move.w  #${op[1]:04X},$58(a6)", "        bsr     bwait"]
        elif t == "R":
            L += [f"        move.w  ${op[1]:X}(a6),d0", f"        and.w   #${op[2]:04X},d0",
                  "        move.w  d0,(a4)+"]
        elif t == "CW":
            L.append(f"        move.b  #${op[3]:02X},${cia_addr(op[1], op[2]):X}")
        elif t == "CR":
            L += ["        moveq   #0,d0", f"        move.b  ${cia_addr(op[1], op[2]):X},d0",
                  f"        and.w   #${op[3]:04X},d0", "        move.w  d0,(a4)+"]
        elif t == "FRAMES":
            lb = f"c{k}f{j}"
            L += ["        move.w  #$0020,$9c(a6)", f"        moveq   #{op[1]}-1,d6",
                  f"{lb}:  btst    #5,$1f(a6)", f"        beq.s   {lb}",
                  "        move.w  #$0020,$9c(a6)", f"        dbra    d6,{lb}"]
        elif t == "IPL":
            L += ["        moveq   #0,d5", f"        move.w  #${0x2000 | op[1] << 8:04X},sr",
                  "        nop", "        nop", "        nop", "        nop",
                  "        move.w  #$2700,sr", "        move.w  d5,(a4)+"]
        elif t == "CWAIT":
            _, cia, reg, mask, want, rec = op
            lb = f"c{k}w{j}"
            L += [f"        move.l  #{POLL_CAP},d7",
                  f"{lb}p:  moveq   #0,d0", f"        move.b  ${cia_addr(cia, reg):X},d0",
                  "        move.w  d0,d1", f"        and.w   #${mask:02X},d1",
                  f"        cmp.w   #${want:02X},d1", f"        beq.s   {lb}ok",
                  "        subq.l  #1,d7", f"        bne.s   {lb}p",
                  f"        move.w  #${TIMEOUT_REC:04X},(a4)+", f"        bra.s   {lb}e",
                  f"{lb}ok:"]
            if rec:
                L += [f"        and.w   #${rec:04X},d0", "        move.w  d0,(a4)+"]
            L.append(f"{lb}e:")
        else:
            raise ValueError(op)
    L += [f"        lea     c{k}n,a0", "        bsr     puts", "        bsr     records"]
    for addr, n in c.dumps:
        L += [f"        lea     ${addr:X},a0", f"        move.w  #{n},d2", "        bsr     dump"]
    L.append("        rts")
    L.append(f"c{k}n:   dc.b    \"@{c.name}\",10,0")
    L.append("        even")
    for j, (_addr, data) in enumerate(c.init):
        L.append(f"c{k}d{j}:")
        for i in range(0, len(data), 16):
            L.append("        dc.b    " + ",".join(f"${x:02X}" for x in data[i:i + 16]))
    L.append("        even")
    return "\n".join(L) + "\n"


def emit_asm(cs: list[Case]) -> str:
    """cases.s: the ``cases`` routine (every case in order) and each case."""
    out = ["; generated by tests/chip_oracle.py -- do not edit", "cases:"]
    out += [f"        bsr     case{k}" for k in range(len(cs))]
    out.append("        rts")
    return "\n".join(out) + "\n" + "".join(emit_case(k, c) for k, c in enumerate(cs))


def case_hash(cs: list[Case]) -> str:
    head = open(os.path.join(ORACLE_DIR, "head.s"), "rb").read()
    return hashlib.sha256(head + emit_asm(cs).encode()).hexdigest()[:16]


def build_rom(cs: list[Case], work: str) -> str:
    with open(os.path.join(work, "cases.s"), "w") as f:
        f.write(emit_asm(cs))
    binp = os.path.join(work, "oracle.bin")
    res = subprocess.run([os.path.join(VBCC, "bin", "vasmm68k_mot"), "-quiet", "-Fbin",
                          "-I", work, "-o", binp, os.path.join(ORACLE_DIR, "head.s")],
                         capture_output=True, text=True)
    if res.returncode:
        raise SystemExit(f"error: vasm failed:\n{res.stdout}{res.stderr}")
    rom = open(binp, "rb").read()
    if len(rom) > ROM_SIZE:
        raise SystemExit(f"error: oracle ROM is {len(rom)} bytes (> {ROM_SIZE})")
    rom = bytearray(rom + bytes(ROM_SIZE - len(rom)))
    if any(rom[IACK_OFF:]):
        raise SystemExit("error: oracle ROM code reaches the IACK vector bytes")
    rom[IACK_OFF:] = IACK_BYTES
    romp = os.path.join(work, "oracle.rom")
    with open(romp, "wb") as f:
        f.write(rom)
    return romp


def run_fsuae(rom: str, work: str, timeout: float) -> str:
    serial = os.path.join(work, "serial.txt")
    open(serial, "w").close()
    cfg = os.path.join(work, "oracle.fs-uae")
    tmpl = open(os.path.join(ORACLE_DIR, "a500.fs-uae.in")).read()
    with open(cfg, "w") as f:
        f.write(tmpl.format(rom=rom, serial=serial, base=os.path.join(work, "base")))
    t0 = time.monotonic()
    text = ""
    with open(os.path.join(work, "fs-uae.stdout.txt"), "w") as log:
        proc = subprocess.Popen([FS_UAE, cfg], cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        try:
            while time.monotonic() - t0 < timeout and proc.poll() is None:
                text = open(serial, "rb").read().decode("latin-1")
                if "\nEND\n" in text or "!VECTOR0" in text:
                    break
                time.sleep(0.2)
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    text = open(serial, "rb").read().decode("latin-1")
    if "!VECTOR0" in text:
        raise SystemExit(f"error: an interrupt went through vector 0 (ROM IACK bytes?); see {work}")
    if "\nEND\n" not in text:
        raise SystemExit(f"error: FS-UAE produced no END within {timeout} s "
                         f"({len(text)} serial bytes; see {work})")
    return text


def parse_serial(text: str) -> dict:
    res: dict = {}
    cur = None
    for line in text.replace("\r", "").splitlines():
        if line.startswith("@"):
            cur = res[line[1:]] = {"R": [], "D": {}}
        elif line.startswith("R") and cur is not None:
            cur["R"] = [int(x, 16) for x in line[1:].split()]
        elif line.startswith("D ") and cur is not None:
            _, addr, hx = line.split()
            cur["D"][addr] = hx.lower()
    return res


# ---- the model side --------------------------------------------------------------

def load_ref(source: str | None = None) -> types.ModuleType:
    """host/chipset_ref.py, or a module built from ``source`` (a mutant)."""
    if source is None:
        spec = importlib.util.spec_from_file_location("chipset_ref_oracle", CHIPSET_REF)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    mod = types.ModuleType("chipset_ref_mutant")
    exec(compile(source, CHIPSET_REF, "exec"), mod.__dict__)
    return mod


def run_model(c: Case, ref) -> dict:
    try:
        return _run_model(c, ref)
    except NotImplementedError as e:            # a documented model limit
        return {"E": f"{type(e).__name__}: {e}"}


def _run_model(c: Case, ref) -> dict:
    ram = ref.Ram(CHIP_SIZE)
    chip = ref.Chipset(ram)
    chip.custom_write(0x096, 0x8240)            # the prologue's DMAEN | BLTEN
    cias = {"A": chip.ciaa, "B": chip.ciab}
    for addr, data in c.init:
        ram.w_block(addr, data)
    rec = []
    t = {"e": 0, "lines": 0}                    # E clocks, lines so far
    frame_e = ref.LINES * ref.LINE_CYC / ref.ECLK_DIV

    def tick(n):
        chip.ciaa.eclock(n)
        chip.ciab.eclock(n)
        t["e"] += n
        lines = t["e"] * ref.ECLK_DIV // ref.LINE_CYC
        for ln in range(t["lines"] + 1, lines + 1):
            chip.ciab.tod_tick(1)               # CIA-B TOD = HSYNC
            if ln % ref.LINES == 0:             # vertical blank
                chip.ciaa.tod_tick(1)           # CIA-A TOD = VSYNC
                chip.run_copper()
                chip.request(5)                 # VERTB
        t["lines"] = lines

    for op in c.ops:
        t_ = op[0]
        if t_ == "W":
            chip.custom_write(op[1], op[2])
        elif t_ == "BLIT":
            chip.custom_write(0x058, op[1])
        elif t_ == "R":
            rec.append(chip.custom_read(op[1]) & op[2])
        elif t_ == "CW":
            tick(1)
            cias[op[1]].write(op[2], op[3])
        elif t_ == "CR":
            tick(1)
            rec.append(cias[op[1]].read(op[2]) & op[3])
        elif t_ == "CWAIT":
            _, cia, reg, mask, want, recm = op
            for _ in range(MODEL_POLL_CAP):
                tick(2)
                v = cias[cia].read(reg)
                if v & mask == want:
                    if recm:
                        rec.append(v & recm)
                    break
            else:
                rec.append(TIMEOUT_REC)
        elif t_ == "FRAMES":
            chip.custom_write(0x09C, 0x0020)
            for _ in range(op[1]):
                nxt = (int(t["e"] // frame_e) + 1) * frame_e
                tick(max(1, int(nxt - t["e"]) + 1))
                chip.custom_write(0x09C, 0x0020)
        elif t_ == "IPL":
            lvl = chip.paula.level()
            if lvl > op[1]:
                rec.append(lvl)
                chip.custom_write(0x09A, 0x4000)   # the handler: INTEN off
            else:
                rec.append(0)
        else:
            raise ValueError(op)
    dumps = {f"{addr:06X}".lower(): ram.r_block(addr, n).hex() for addr, n in c.dumps}
    return {"R": rec, "D": dumps}


def diff(c: Case, hw: dict | None, mdl: dict) -> list[str]:
    """Difference lines; [] is a MATCH.  A line starting with LIMIT is the
    documented-limit verdict (not a difference, see ``is_diff``)."""
    if hw is None:
        return ["no FS-UAE output for this case"]
    out = []
    if TIMEOUT_REC in hw["R"]:
        out.append(f"INCONCLUSIVE: FS-UAE timed out in a poll (read #{hw['R'].index(TIMEOUT_REC)})")
    if "E" in mdl:
        if c.limit:
            return out + [f"LIMIT: {c.limit} (model: {mdl['E']})"]
        return out + [f"model raised {mdl['E']}"]
    if hw["R"] != mdl["R"]:
        recs = [op for op in c.ops if op[0] in ("R", "CR") or (op[0] == "CWAIT" and op[5])]
        for i in range(max(len(hw["R"]), len(mdl["R"]))):
            a = hw["R"][i] if i < len(hw["R"]) else None
            b = mdl["R"][i] if i < len(mdl["R"]) else None
            if a != b:
                what = recs[i] if i < len(recs) else "?"
                out.append(f"read #{i} {what}: FS-UAE {a if a is None else f'${a:04X}'} "
                           f"model {b if b is None else f'${b:04X}'}")
    for addr, hx in mdl["D"].items():
        hh = hw["D"].get(addr)
        if hh is None:
            out.append(f"dump ${addr}: missing on FS-UAE")
            continue
        if hh != hx:
            base = int(addr, 16)
            bad = [i for i in range(0, min(len(hh), len(hx)), 2) if hh[i:i + 2] != hx[i:i + 2]]
            shown = ", ".join(f"${base + i // 2:05X} {hh[i:i + 2]}/{hx[i:i + 2]}" for i in bad[:6])
            out.append(f"dump ${addr}: {len(bad)} bytes differ (FS-UAE/model): {shown}")
    if out and c.diverge and not any(x.startswith("INCONCLUSIVE") for x in out):
        return [f"LIMIT: {c.diverge}"] + [f"LIMIT   ({x})" for x in out]
    return out


def is_diff(lines: list[str]) -> bool:
    return any(not x.startswith("LIMIT") for x in lines)


def compare(cs: list[Case], hw_all: dict, ref) -> dict[str, list[str]]:
    return {c.name: diff(c, hw_all.get(c.name), run_model(c, ref)) for c in cs}


# ---- mutants: one per blitter channel/shift/mask/fill/size rule, CIA timer/ICR rule
# (old, new) edits of host/chipset_ref.py (or a list of them); each must make
# at least one case differ.
MUTANTS: dict[str, tuple[str, str] | list[tuple[str, str]]] = {
    # blitter
    "minterm_term_swap": ("if lf & 0x40: d |= ash_v & bsh_v & nc", "if lf & 0x40: d |= ash_v & nb & cdat"),
    "minterm_bit_off": ("if lf & 0x01: d |= na & nb & nc", "if lf & 0x01: d |= 0"),
    "a_channel_ignored": ("                    adat = mem.r16(apt)\n", "                    mem.r16(apt)\n"),
    "b_channel_ignored": ("                    bdat = mem.r16(bpt)\n", "                    mem.r16(bpt)\n"),
    "c_channel_ignored": ("                    cdat = mem.r16(cpt)\n", "                    mem.r16(cpt)\n"),
    "d_not_written": ("                    mem.w16(dpt, d)\n", "                    pass\n"),
    "fwm_ignored": ("                    a &= afwm\n", "                    pass\n"),
    "lwm_ignored": ("                    a &= alwm\n", "                    pass\n"),
    "lwm_before_fwm_exclusive": ("                if x == w - 1:\n                    a &= alwm",
                                 "                elif x == w - 1:\n                    a &= alwm"),
    "mask_after_shift": ("                aold, bold = a, bdat\n",
                         "                aold, bold = adat, bdat\n"),
    "b_masked": ("                if useb:\n                    bdat = mem.r16(bpt)\n",
                 "                if useb:\n                    bdat = mem.r16(bpt) & (afwm if x == 0 else 0xFFFF)\n"),
    "ashift_ignored": ("                    ash_v = ((aold << 16 | a) >> ash) & 0xFFFF",
                       "                    ash_v = a"),
    "bshift_uses_ash": ("                    bsh_v = ((bold << 16 | bdat) >> bsh) & 0xFFFF",
                        "                    bsh_v = ((bold << 16 | bdat) >> ash) & 0xFFFF"),
    "old_reset_per_row": ("            fc = (con1 >> 2) & 1\n",
                          "            fc = (con1 >> 2) & 1\n            aold = bold = 0\n"),
    "desc_shift_right": ("                    ash_v = ((a << 16 | aold) >> (16 - ash)) & 0xFFFF if ash else a",
                         "                    ash_v = ((aold << 16 | a) >> ash) & 0xFFFF"),
    "desc_mod_added": ("            amod, bmod, cmod, dmod = -amod, -bmod, -cmod, -dmod",
                       "            pass"),
    "dmod_ignored": ("            dpt += dmod if used else 0", "            dpt += 0"),
    "amod_unsigned": ("    return v - 0x10000 if v & 0x8000 else v", "    return v"),
    "ptr_not_written_back": ("            r[reg >> 1], r[(reg + 2) >> 1] = (p >> 16) & 0x1F, p & 0xFFFF",
                             "            pass"),
    "width0_is_0": ("            h, w = (size >> 6) & 0x3FF or 1024, size & 0x3F or 64",
                    "            h, w = (size >> 6) & 0x3FF or 1024, size & 0x3F or 1"),
    "height0_is_1": ("            h, w = (size >> 6) & 0x3FF or 1024, size & 0x3F or 64",
                     "            h, w = (size >> 6) & 0x3FF or 1, size & 0x3F or 64"),
    "fill_ife_efe_swapped": ("                        if con1 & 0x18 == 0x08:  # IFE",
                             "                        if con1 & 0x18 == 0x10:  # IFE"),
    "fill_fci_ignored": ("            fc = (con1 >> 2) & 1\n", "            fc = 0\n"),
    "fill_carry_not_reset": ("            fc = (con1 >> 2) & 1\n",
                             "            fc = (con1 >> 2) & 1 if _y == 0 else fc\n"),
    "blit_irq_missing": ("        self.request(6)                          # BLIT done",
                         "        pass"),
    # CIA timers
    "oneshot_no_stop": ("                    self.cra &= ~1\n", "                    pass\n"),
    "oneshot_no_reload": ("                    ta_under = 1\n                    self.cra &= ~1\n                    self.ta = self.ta_latch",
                          "                    ta_under = 1\n                    self.cra &= ~1\n                    self.ta = 0"),
    "oneshot_hi_no_start": ("                self.ta = self.ta_latch\n                self.cra |= 1\n",
                            "                self.ta = self.ta_latch\n"),
    "stopped_hi_no_load": ("            if not self.cra & 1:\n                self.ta = self.ta_latch\n",
                           "            if False:\n                self.ta = self.ta_latch\n"),
    "running_hi_loads": ("            if not self.cra & 1:\n                self.ta = self.ta_latch\n",
                         "            if True:\n                self.ta = self.ta_latch\n"),
    "force_load_ignored": ("            if v & 0x10:\n                self.ta = self.ta_latch\n",
                           "            if False:\n                self.ta = self.ta_latch\n"),
    "load_bit_reads_back": [("            self.cra = v & 0xEF", "            self.cra = v"),
                            ("            return self.cra & 0xEF", "            return self.cra")],
    "continuous_stops": ("                    self.ta = self.ta_latch - (left % span)",
                         "                    self.ta = self.ta_latch - (left % span)\n                    self.cra &= ~1"),
    "continuous_reload_zero": ("                    self.ta = self.ta_latch - (left % span)",
                               "                    self.ta = 0"),
    "tb_counts_phi2": ("            steps = n if mode == 0 else (ta_under if mode >= 2 else 0)",
                       "            steps = n"),
    "tb_oneshot_no_stop": ("                        self.crb &= ~1\n", "                        pass\n"),
    # CIA ICR
    "icr_no_clear": ("            self.icr_data = 0\n            return v", "            return v"),
    "icr_ir_ignores_mask": ("            v = self.icr_data | (0x80 if self.icr_data & self.icr_mask & 0x1F else 0)",
                            "            v = self.icr_data | (0x80 if self.icr_data else 0)"),
    "icr_clear_clears_all": ("                self.icr_mask &= ~v & 0x1F", "                self.icr_mask = 0"),
    "icr_mask_write_no_update": ("                self.icr_mask &= ~v & 0x1F\n            self.update()",
                                 "                self.icr_mask &= ~v & 0x1F"),
    "cia_int_wrong_bit": ("        self.ciab = CIA(lambda: self.request(13), \"CIAB\")",
                          "        self.ciab = CIA(lambda: self.request(3), \"CIAB\")"),
    # fix round 1 (the review's findings and gaps)
    "copper_danger_ecs": ("                if reg < (0x040 if regs[0x02E >> 1] & 2 else 0x080):",
                          "                if reg < 0x040 and not regs[0x02E >> 1] & 2:"),
    "copper_no_danger_check": ("                if reg < (0x040 if regs[0x02E >> 1] & 2 else 0x080):",
                               "                if False:"),
    "copper_skip_silent": ("                if self.strict:\n                    raise CopperSkipError",
                           "                if False:\n                    raise CopperSkipError"),
    "copper_jmp2_uses_cop1": ("                    pc = ((regs[0x084 >> 1] << 16) | regs[0x086 >> 1]) & 0x1FFFFE",
                              "                    pc = ((regs[0x080 >> 1] << 16) | regs[0x082 >> 1]) & 0x1FFFFE"),
    "adat_not_held": ("        if usea:\n            r[0x074 >> 1] = adat\n", ""),
    "cdat_not_held": ("        if usec:\n            r[0x070 >> 1] = cdat\n", ""),
    "bdat_reshifted_per_word": ("        bsh_v = self.bhold                      # B DMA off: the hold register, as written",
                                "        bsh_v = self.bhold = ((self.bprev << 16 | bdat) >> bsh) & 0xFFFF"),
    "bdat_write_not_shifted": ("            self.bhold = v >> bsh", "            self.bhold = v"),
    "fill_both_inclusive": ("                        if con1 & 0x18 == 0x08:  # IFE alone: edges drawn (both = EFE)",
                            "                        if con1 & 0x08:"),
    "latch0_span_64k": ("                span = self.ta_latch + 1                # latch 0: underflow every tick",
                        "                span = self.ta_latch + 1 if self.ta_latch else 0x10000"),
    "tb_ta_off_by_one": ("                if steps > self.tb:", "                if steps >= self.tb:"),
    # (the review's ta_under_off_by_one, `n >= self.ta`, is no coherent rule: with
    # the model's 2-tick polls it still gives period latch+1, so only the hand
    # test test_cia_oneshot_hi_write_starts_and_underflow_stops_reloads sees it;
    # this is the coherent form of that error, period = latch)
    "period_is_latch": [("                span = self.ta_latch + 1                # latch 0: underflow every tick",
                         "                span = max(self.ta_latch, 1)"),
                        ("                    self.ta = self.ta_latch - (left % span)",
                         "                    self.ta = max(self.ta_latch - 1 - (left % span), 0)")],
    "desc_mask_swap": [("                if x == 0:\n                    a &= afwm",
                        "                if x == (w - 1 if desc else 0):\n                    a &= afwm"),
                       ("                if x == w - 1:\n                    a &= alwm",
                        "                if x == (0 if desc else w - 1):\n                    a &= alwm")],
    "desc_bshift_ascending": ("                        bsh_v = ((bdat << 16 | bold) >> (16 - bsh)) & 0xFFFF if bsh else bdat",
                              "                        bsh_v = ((bold << 16 | bdat) >> bsh) & 0xFFFF"),
    "bzero_only_if_d": ("                nz |= d\n", "                nz |= d if used else 0\n"),
    "icr_mask_set_replaces": ("                self.icr_mask |= v & 0x1F", "                self.icr_mask = v & 0x1F"),
    "tb_oneshot_hi_no_start": ("                self.tb = self.tb_latch\n                self.crb |= 1\n",
                               "                self.tb = self.tb_latch\n"),
    "level_ignores_inten": ("        if not self.intena & 0x4000:\n            return 0\n", ""),
    "level_table_shifted": ("IRQ_LEVELS = ((13, 6), (12, 5), (11, 5), (10, 4),",
                            "IRQ_LEVELS = ((13, 6), (12, 5), (11, 4), (10, 4),"),
    "tod_mid_halts": ("                elif r == 8:\n                    self.tod_halt = False",
                      "                elif r == 8:\n                    self.tod_halt = False\n                else:\n                    self.tod_halt = True"),
    "tod_hi_no_halt": ("                    self.tod_halt = True        # TODHI write stops the clock ...",
                       "                    pass"),
    "tod_latch_never_released": ("                self.tod_latched = None\n            return",
                                 "                pass\n            return"),
    "tod_no_latch": ("                self.tod_latched = self.tod\n", "                pass\n"),
    "tod_alarm_never": ("                self.icr_data |= 4\n", "                pass\n"),
    "adkcon_not_setclr": ("            self.adkcon = setclr(self.adkcon, val)", "            self.adkcon = val & 0x7FFF"),
    "setclr_14bit": ("    return reg | (val & 0x7FFF) if val & 0x8000 else reg & ~val & 0x7FFF",
                     "    return reg | (val & 0x3FFF) if val & 0x8000 else reg & ~val & 0x3FFF"),
    # Paula
    "setclr_inverted": ("    return reg | (val & 0x7FFF) if val & 0x8000 else reg & ~val & 0x7FFF",
                        "    return reg | (val & 0x7FFF) if not val & 0x8000 else reg & ~val & 0x7FFF"),
    "intreq_clear_no_cia_update": ("                self.ciaa.update()               # a CIA still asserting /INT re-raises\n                self.ciab.update()\n",
                                   ""),
}


def mutant_source(name: str) -> str:
    edits = MUTANTS[name]
    src = open(CHIPSET_REF).read()
    for old, new in [edits] if isinstance(edits, tuple) else edits:
        if src.count(old) != 1:
            raise ValueError(f"mutant {name}: its target occurs {src.count(old)} times")
        src = src.replace(old, new)
    return src


# ---- driver ---------------------------------------------------------------------------

def live(cs: list[Case], timeout: float, keep: str | None) -> tuple[dict, str] | None:
    vasm = os.path.join(VBCC, "bin", "vasmm68k_mot")
    if not os.path.exists(FS_UAE):
        print(f"SKIP chip_oracle: no FS-UAE at {FS_UAE}")
        return None
    if not os.path.exists(vasm):
        print(f"SKIP chip_oracle: no vasm at {vasm} (tools/vbcc/install.sh)")
        return None
    work = os.path.abspath(keep) if keep else tempfile.mkdtemp(prefix="chiporacle-")
    os.makedirs(work, exist_ok=True)
    rom = build_rom(cs, work)
    text = run_fsuae(rom, work, timeout)
    return parse_serial(text), work


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--recorded", nargs="?", const=RECORDED,
                    help="diff against recorded FS-UAE dumps (default file: %(const)s)")
    ap.add_argument("--record", action="store_true", help="live run, then rewrite the recorded dumps")
    ap.add_argument("--mutant", help="diff with one MUTANTS entry applied to the model (red leg)")
    ap.add_argument("--mutants", action="store_true", help="run every mutant; each must be killed")
    ap.add_argument("--model-file", help="diff against this chipset_ref.py variant instead")
    ap.add_argument("--only", help="comma list of case names")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--keep", help="keep the work dir (ROM, cases.s, serial.txt) here")
    ap.add_argument("--json", help="write per-case results here")
    a = ap.parse_args(argv)
    cs = cases()
    if a.only:
        want = set(a.only.split(","))
        cs = [c for c in cs if c.name in want]
    h = case_hash(cases())
    if a.recorded:
        rec = json.load(open(a.recorded))
        if rec.get("case_hash") != h:
            print(f"error: {a.recorded} was recorded for cases {rec.get('case_hash')}, "
                  f"these are {h}: re-record with --record")
            return 1
        hw_all = rec["cases"]
        src = "recorded"
    else:
        got = live(cs, a.timeout, a.keep)
        if got is None:
            return 0
        hw_all, work = got
        src = f"FS-UAE ({work})"
        if a.record:
            with open(RECORDED, "w") as f:
                json.dump({"case_hash": h, "fs_uae": FS_UAE, "cases": hw_all}, f, indent=0,
                          sort_keys=True)
                f.write("\n")
            print(f"recorded {len(hw_all)} cases -> {RECORDED}")
    if a.mutants:
        names = list(MUTANTS)
        survivors = []
        for n in names:
            res = compare(cs, hw_all, load_ref(mutant_source(n)))
            killed = [c for c, d in res.items() if is_diff(d)]
            print(f"{'KILLED ' if killed else 'SURVIVED'} {n}: "
                  f"{len(killed)} case(s) differ{': ' + ','.join(killed[:6]) if killed else ''}")
            if not killed:
                survivors.append(n)
        print(f"mutants: {len(names) - len(survivors)}/{len(names)} killed"
              f"{'; survivors: ' + ','.join(survivors) if survivors else ''}")
        return 1 if survivors else 0
    ref = load_ref(mutant_source(a.mutant)) if a.mutant else load_ref()
    if a.mutant:
        src += f", model = mutant {a.mutant}"
    if a.model_file:
        ref = load_ref(open(a.model_file).read())
        src += f", model = {a.model_file}"
    res = compare(cs, hw_all, ref)
    for c in cs:
        d = res[c.name]
        v = "DIFF " if is_diff(d) else "LIMIT" if d else "MATCH"
        print(f"{v} {c.name}  ({c.rule})")
        for line in d:
            print(f"      {line}")
    nd = sum(1 for d in res.values() if is_diff(d))
    nl = sum(1 for d in res.values() if d and not is_diff(d))
    print(f"chip_oracle vs {src}: {len(cs) - nd - nl} MATCH, {nl} LIMIT, {nd} DIFF")
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"source": src, "case_hash": h, "results": res}, f, indent=1)
    return 1 if nd else 0


if __name__ == "__main__":
    sys.exit(main())
