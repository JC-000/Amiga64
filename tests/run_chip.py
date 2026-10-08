#!/usr/bin/env python3
"""S3.1 I/O trap: guest and direct-API legs for the $BF/$DF bank trap (VICE).

docs/S3_PLAN.md §1.2-1.3 and §4 "S3.1", docs/WAVE7_BRIEF.md C2.  Guest
accesses whose bank is $DF (custom chips) or $BF (CIAs) must never reach
the REU: the memory API hands them to runtime/ops/chip.s, whose writes land
in a 256-word register shadow plus a log, whose reads return the §3.4 fixed
values (else 0 for write-only registers, else the shadow), and whose SERDAT
writes append to a C64-side ring the host reads here.

Legs (each stages a small guest program through test_load_chunk, seeds the
REU underneath the I/O addresses with sentinels -- by raw REC DMA, never
through the API --, runs it with test_run_program and checks registers,
REU sentinels and the chip state):

  main   S3_PLAN red/green: move.w #$1234,$DFF180 / move.w $DFF180,d0, the
         §3.4 fixed reads, a SERDAT string, a second write right after
  cold   the trapped access is the FIRST data call after a window refill
  split  a long at $DFFFFE (the $DF half trapped, the $E0 half in REU)
  carry  move.l $BEFFFE,d3 / move.l d4,$DEFFFE (bank carry into $BF/$DF)
  byte   byte reads and writes of custom registers
  cia    CIA-A odd / CIA-B even selection, open-bus holes, PRA inputs
  serial 1300 SERDAT writes from a zero count: the 1024-byte chip_ser ring
         (CHIP_SERSZ) wraps past its end; the whole ring image is compared
  serwrap 1500 SERDAT writes from chip_sern = $FD44: the u16 count wraps
         through 0 and the ring wraps again
  api    direct mem_read*/mem_write* calls (cold and warm) over custom,
         CIA, split and carry addresses: result vs the reference model,
         REC command stores counted by VICE watchpoints (a trapped byte
         issues no DMA), X/Y preserved, REU sentinels intact

After every call and run the invariant is checked: m_rbk (the REC bank
shadow) never holds $BF or $DF.

    python3 -m tests.run_chip --prg build/w7-trap/runner.prg [--legs a,b]
    python3 -m tests.run_chip --prg ... --mutants     # teeth: 4 core.s + 4 chip.s mutants
    python3 -m tests.run_chip --prg ... --perf --base build/w7-base/runner.prg

Exit 0 = every selected leg passed (--mutants: every mutant failed a leg;
--perf: exactly +8 cycles per bank switch and 0 on the same-bank loop).
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys

from host import chipset_ref, fixturegen, hunk, image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ZP_M_ADDR, ZP_M_VAL, ZP_T_TMP = 0x58, 0x5C, 0x70
STUB = 0xC700            # buf_case tail (staging is done before it is used)
DBUF = 0xC780            # raw-DMA data buffer (<= 0x60 bytes)
XY_OUT = 0xC7F0
REC_FIRST, REC_LAST, REC_COMMAND = 0xDF00, 0xDF0A, 0xDF01
CPU_STORE, CPU_LOAD, CPU_EXEC = 0x02, 0x01, 0x04
BASE = image.BASE_ADDR   # $010000: first hunk, so absolute addresses work

# --------------------------------------------------------------------------
# Reference model of the S3.1 stub (the spec, docs/S3_PLAN.md §1.3/§3.4)
# --------------------------------------------------------------------------

FIXED = {0x004: 0x8000,     # VPOSR: LOF, Agnus ID 0 (OCS PAL)
         0x016: 0xFF00,     # POTINP: right button up
         0x018: 0x3000,     # SERDATR: TBE | TSRE
         0x07C: 0xFFFF}     # DENISEID: OCS has none, floats high
SERDAT = 0x030
CIAA_PRA_IN = 0xEB          # /RDY high, /TK0 low, /WPRO high, /CHNG low, FIR up
LOG_ENTRIES = 64
CIA_ID = (0x0A00, 0x0B00)   # log ids: CIA-A $0A00+reg, CIA-B $0B00+reg


def is_ro(reg: int) -> bool:
    """Read-only custom register slot ($000-$01E, DENISEID $07C)."""
    return reg < 0x020 or reg == 0x07C


class ChipRef:
    """Byte-exact model of runtime/ops/chip.s state and results."""

    def __init__(self):
        self.shd = bytearray(512)       # BE register image, index = offset
        # W9-C (S3.4): the CIAs are chipset_ref.CIA (runtime/ops/cia.s);
        # no guest time passes in these legs (no mach_init)
        self.cias = (chipset_ref.CIA(), chipset_ref.CIA())
        self.cias[0].pra_in = CIAA_PRA_IN
        self.log: list[tuple[int, int]] = []
        self.ser = bytearray()          # SERDAT bytes written by this run
        self.ser0 = 0                   # chip_sern before the run (a leg's seed)
        self.nopen = 0

    def sern(self) -> int:
        return (self.ser0 + len(self.ser)) & 0xFFFF

    def ser_ring(self, sz: int) -> bytes:
        """chip_ser after the run: zero (chip_reset), byte k of the run at
        slot (ser0 + k) mod sz."""
        ring = bytearray(sz)
        for k, b in enumerate(self.ser):
            ring[(self.ser0 + k) % sz] = b
        return bytes(ring)

    # custom word access at a 24-bit address in bank $DF
    def c_rw(self, a: int) -> int:
        off = a & 0xFFFF
        if not 0xF000 <= off < 0xF200:
            self.nopen += 1
            return 0
        r = off & 0x1FE
        if r in FIXED:
            return FIXED[r]
        if is_ro(r):
            return self.shd[r] << 8 | self.shd[r + 1]
        return 0                        # write-only register

    def c_ww(self, a: int, v: int) -> None:
        off = a & 0xFFFF
        if not 0xF000 <= off < 0xF200:
            self.nopen += 1
            return
        r = off & 0x1FE
        self.log.append((r, v))
        if not is_ro(r):
            self.shd[r], self.shd[r + 1] = v >> 8, v & 0xFF
        if r == SERDAT:
            self.ser.append(v & 0xFF)

    def cia_sel(self, a: int):
        hi = (a >> 8) & 0xFF
        if a & 1 and not hi & 0x10:
            return 0, hi & 0x0F         # CIA-A: odd byte, A12 = 0
        if not a & 1 and not hi & 0x20:
            return 1, hi & 0x0F         # CIA-B: even byte, A13 = 0
        return None, 0

    def rb(self, a: int) -> int:
        a &= 0xFFFFFF
        if a >> 16 == 0xDF:
            w = self.c_rw(a & ~1)
            return w & 0xFF if a & 1 else w >> 8
        c, r = self.cia_sel(a)
        if c is None:
            self.nopen += 1
            return 0
        return self.cias[c].read(r)

    def wb(self, a: int, v: int) -> None:
        a &= 0xFFFFFF
        if a >> 16 == 0xDF:
            self.c_ww(a & ~1, v << 8 | v)
            return
        c, r = self.cia_sel(a)
        if c is None:
            self.nopen += 1
            return
        self.cias[c].write(r, v)
        self.log.append((CIA_ID[c] + r, v))


def is_io(a: int) -> bool:
    return (a >> 16) & 0xFF in (0xBF, 0xDF)


def straddles(a: int, size: int) -> bool:
    return (a & 0x7FFFF) + size - 1 >= 0x80000


def split_route(a: int, size: int) -> bool:
    """True when the API takes the byte-wise path (mio_split)."""
    if size == 1:
        return False
    if straddles(a, size):
        return True
    return ((a & 0xFFFF) + size - 1 > 0xFFFF
            and (a >> 16) + 1 in (0xBF, 0xDF))


class GuestRef:
    """Guest memory as the trapping API sees it: REU + ChipRef."""

    def __init__(self, chip: ChipRef, reu: dict[int, int]):
        self.chip, self.reu = chip, reu

    def read(self, a: int, size: int) -> tuple[int, int]:
        """-> (value, REU DMA count)."""
        a &= 0xFFFFFF
        if split_route(a, size):
            v, dma = 0, 0
            for i in range(size):
                b = (a + i) & 0xFFFFFF
                if is_io(b):
                    x = self.chip.rb(b)
                else:
                    x, dma = self.reu.get(b, 0), dma + 1
                v = v << 8 | x
            return v, dma
        if not is_io(a):
            return int.from_bytes(bytes(self.reu.get((a + i) & 0xFFFFFF, 0)
                                        for i in range(size)), "big"), 1
        if size == 1:
            return self.chip.rb(a), 0
        if a >> 16 == 0xDF:
            if size == 2:
                return self.chip.c_rw(a & ~1), 0
            return (self.chip.c_rw(a & ~1) << 16
                    | self.chip.c_rw((a + 2) & ~1)), 0
        v = 0
        for i in range(size):
            v = v << 8 | self.chip.rb(a + i)
        return v, 0

    def write(self, a: int, size: int, v: int) -> int:
        a &= 0xFFFFFF
        bs = v.to_bytes(size, "big")
        if split_route(a, size):
            dma = 0
            for i, x in enumerate(bs):
                b = (a + i) & 0xFFFFFF
                if is_io(b):
                    self.chip.wb(b, x)
                else:
                    self.reu[b], dma = x, dma + 1
            return dma
        if not is_io(a):
            for i, x in enumerate(bs):
                self.reu[(a + i) & 0xFFFFFF] = x
            return 1
        if size == 1:
            self.chip.wb(a, v)
        elif a >> 16 == 0xDF:
            if size == 2:
                self.chip.c_ww(a & ~1, v)
            else:
                self.chip.c_ww(a & ~1, v >> 16)
                self.chip.c_ww((a + 2) & ~1, v & 0xFFFF)
        else:
            for i, x in enumerate(bs):
                self.chip.wb(a + i, x)
        return 0


# --------------------------------------------------------------------------
# 68000 encodings (enough for the legs)
# --------------------------------------------------------------------------

def hi_lo(a: int) -> list[int]:
    return [(a >> 16) & 0xFFFF, a & 0xFFFF]


def moveq(v: int, d: int) -> list[int]:
    return [0x7000 | d << 9 | (v & 0xFF)]


def move_imm_abs(size: int, v: int, a: int) -> list[int]:
    op = {1: 0x13FC, 2: 0x33FC, 4: 0x23FC}[size]
    imm = hi_lo(v) if size == 4 else [v & (0xFF if size == 1 else 0xFFFF)]
    return [op] + imm + hi_lo(a)


def move_d_abs(size: int, d: int, a: int) -> list[int]:
    return [{1: 0x13C0, 2: 0x33C0, 4: 0x23C0}[size] | d] + hi_lo(a)


def move_abs_d(size: int, a: int, d: int) -> list[int]:
    return [{1: 0x1039, 2: 0x3039, 4: 0x2039}[size] | d << 9] + hi_lo(a)


def move_imm_d(size: int, v: int, d: int) -> list[int]:
    op = {2: 0x303C, 4: 0x203C}[size] | d << 9
    return [op] + (hi_lo(v) if size == 4 else [v & 0xFFFF])


def jmp_abs(a: int) -> list[int]:
    return [0x4EF9] + hi_lo(a)


HALT, NOP = 0xAFFF, 0x4E71


def pad_to(words: list[int], byte_off: int) -> list[int]:
    """NOP-fill so the next word starts at byte offset *byte_off*."""
    assert 2 * len(words) <= byte_off, (len(words), byte_off)
    return words + [NOP] * ((byte_off - 2 * len(words)) // 2)


# --------------------------------------------------------------------------
# Legs: program words, REU seeds, expected results
# --------------------------------------------------------------------------

class Leg:
    def __init__(self, name, words, seeds, regs, reu_after, chip_fn, sern0=0):
        self.name, self.words, self.seeds = name, words, seeds
        self.regs = regs                  # {"D0": value}
        self.reu_after = reu_after        # {addr: byte} expected after run
        self.chip_fn = chip_fn            # ChipRef -> None (expected ops)
        self.sern0 = sern0                # chip_sern seeded after chip_reset


SENT = 0xA5


def leg_main() -> Leg:
    msg = b"Hi!\n"
    code = (moveq(-1, 0)
            + move_imm_abs(2, 0x1234, 0xDFF180)       # COLOR00 := $1234
            + move_abs_d(2, 0xDFF180, 0)              # d0.w := 0 (write-only)
            + move_abs_d(2, 0xDFF004, 2)              # VPOSR    $8000
            + move_abs_d(2, 0xDFF018, 3)              # SERDATR  $3000
            + move_abs_d(2, 0xDFF016, 4)              # POTINP   $FF00
            + move_abs_d(2, 0xDFF07C, 5)              # DENISEID $FFFF
            + move_abs_d(1, 0xBFE001, 6)              # CIA-A PRA  $EB
            + moveq(0, 1))
    lea_at = len(code)
    code += [0x41F9, 0, 0]                            # lea str,a0
    code += [0x1218,                                  # loop: move.b (a0)+,d1
             0x6708]                                  #       beq.s done
    code += move_d_abs(2, 1, 0xDFF030)                #       move.w d1,SERDAT
    code += [0x60F4]                                  #       bra.s loop
    code += move_imm_abs(2, 0x5678, 0xDFF182)         # done: COLOR01 := $5678
    code += [HALT]
    s = BASE + 2 * len(code)
    code[lea_at + 1:lea_at + 3] = hi_lo(s)
    code += [msg[i] << 8 | msg[i + 1] for i in range(0, len(msg), 2)] + [0]
    seeds = {}
    for a in (0xDFF180, 0xDFF182, 0xDFF004, 0xDFF018, 0xDFF016, 0xDFF07C,
              0xDFF030):
        seeds[a], seeds[a + 1] = SENT, 0x5A
    seeds[0xBFE001] = 0x11

    def chip(c: ChipRef):
        c.c_ww(0xDFF180, 0x1234)
        for ch in msg:
            c.c_ww(0xDFF030, ch)
        c.c_ww(0xDFF182, 0x5678)
    return Leg("main", code, seeds,
               {"D0": 0xFFFF0000, "D2": 0x8000, "D3": 0x3000,
                "D4": 0xFF00, "D5": 0xFFFF, "D6": 0xEB, "D1": 0},
               dict(seeds), chip)


def leg_cold() -> Leg:
    # page 0: set up, then JMP into page 1: the fetch refills the window
    # (m_rok := 0), so the trapped write is the first data call -> cold.
    # Page 2 repeats it with a trapped READ as the first call.
    p0 = moveq(0, 1) + move_imm_d(2, 0x4321, 1) + jmp_abs(BASE + 0x100)
    p1 = move_d_abs(2, 1, 0xDFF180) + jmp_abs(BASE + 0x200)
    p2 = (move_abs_d(2, 0xDFF004, 2) + move_abs_d(2, 0xDFF180, 3)
          + move_abs_d(1, 0xBFE001, 4) + [HALT])
    code = pad_to(p0, 0x100) + pad_to(p1, 0x100) + p2
    seeds = {0xDFF180: SENT, 0xDFF181: SENT, 0xDFF004: 0x12, 0xDFF005: 0x34,
             0xBFE001: 0x22}

    def chip(c: ChipRef):
        c.c_ww(0xDFF180, 0x4321)
    return Leg("cold", code, seeds,
               {"D1": 0x4321, "D2": 0x8000, "D3": 0, "D4": 0xEB},
               dict(seeds), chip)


def leg_split() -> Leg:
    code = (move_imm_d(4, 0x11223344, 1) + move_d_abs(4, 1, 0xDFFFFE)
            + moveq(-1, 2) + move_abs_d(4, 0xDFFFFE, 2) + [HALT])
    seeds = {0xDFFFFE: SENT, 0xDFFFFF: SENT, 0xE00000: 0, 0xE00001: 0}
    after = dict(seeds)
    after.update({0xE00000: 0x33, 0xE00001: 0x44})
    def chip(c: ChipRef):
        for a, v in ((0xDFFFFE, 0x11), (0xDFFFFF, 0x22)):
            c.wb(a, v)                  # open bus: counted, not logged
        c.rb(0xDFFFFE), c.rb(0xDFFFFF)
    return Leg("split", code, seeds, {"D1": 0x11223344, "D2": 0x00003344},
               after, chip)


def leg_carry() -> Leg:
    code = (move_imm_abs(1, 0x5C, 0xBFD000)           # CIA-B PRA := $5C
            + move_abs_d(4, 0xBEFFFE, 3)              # REU, REU, CIA-B, CIA-A
            # (W9-C: CIA-B PRA reads its input pins, $FF, with DDRA = 0)
            + move_imm_d(4, 0xCAFEBABE, 4)
            + move_d_abs(4, 4, 0xDEFFFE)              # REU, REU, open, open
            + moveq(-1, 5) + move_abs_d(4, 0xDEFFFE, 5) + [HALT])
    seeds = {0xBEFFFE: 0x12, 0xBEFFFF: 0x34, 0xBF0000: SENT, 0xBF0001: SENT,
             0xBFD000: 0x77, 0xDEFFFE: 0, 0xDEFFFF: 0, 0xDF0000: SENT,
             0xDF0001: SENT}
    after = dict(seeds)
    after.update({0xDEFFFE: 0xCA, 0xDEFFFF: 0xFE})

    def chip(c: ChipRef):
        c.wb(0xBFD000, 0x5C)
        c.rb(0xBF0000), c.rb(0xBF0001)
        c.wb(0xDF0000, 0xBA), c.wb(0xDF0001, 0xBE)
        c.rb(0xDF0000), c.rb(0xDF0001)
    return Leg("carry", code, seeds,
               {"D3": 0x1234FFEB, "D4": 0xCAFEBABE, "D5": 0xCAFE0000},
               after, chip)


def leg_byte() -> Leg:
    code = (moveq(-1, 1) + moveq(-1, 2) + moveq(-1, 3) + moveq(-1, 4)
            + moveq(-1, 5) + moveq(-1, 6)
            + move_imm_abs(1, 0xC3, 0xDFF181)         # COLOR00 := $C3C3
            + move_abs_d(1, 0xDFF004, 1)              # VPOSR hi  $80
            + move_abs_d(1, 0xDFF005, 2)              # VPOSR lo  $00
            + move_abs_d(1, 0xDFF018, 3)              # SERDATR hi $30
            + move_abs_d(1, 0xDFF016, 4)              # POTINP hi $FF
            + move_abs_d(1, 0xDFF181, 5)              # COLOR00 lo (w/o) 0
            + move_imm_abs(1, 0x5A, 0xDFF031)         # SERDAT lo: 'Z'
            + move_abs_d(1, 0xDFF07D, 6)              # DENISEID lo $FF
            + [HALT])
    seeds = {0xDFF180: SENT, 0xDFF181: SENT, 0xDFF004: 1, 0xDFF005: 2,
             0xDFF018: 3, 0xDFF016: 4, 0xDFF030: 5, 0xDFF031: 6,
             0xDFF07D: 7}

    def chip(c: ChipRef):
        c.wb(0xDFF181, 0xC3)
        c.wb(0xDFF031, 0x5A)
    return Leg("byte", code, seeds,
               {"D1": 0xFFFFFF80, "D2": 0xFFFFFF00, "D3": 0xFFFFFF30,
                "D4": 0xFFFFFFFF, "D5": 0xFFFFFF00, "D6": 0xFFFFFFFF},
               dict(seeds), chip)


def leg_cia() -> Leg:
    code = (moveq(-1, 1) + moveq(-1, 2) + moveq(-1, 3) + moveq(-1, 4)
            + moveq(-1, 5) + moveq(-1, 6)
            + move_imm_abs(1, 0x03, 0xBFE201)         # CIA-A DDRA (odd, A12=0)
            + move_imm_abs(1, 0xC0, 0xBFD200)         # CIA-B DDRA (even, A13=0)
            + move_imm_abs(1, 0x77, 0xBFE200)         # even, A13=1: open bus
            + move_imm_abs(1, 0x66, 0xBFD201)         # odd, A12=1: open bus
            + move_imm_abs(1, 0x02, 0xBFE001)         # CIA-A PRA := $02
            + move_abs_d(1, 0xBFE201, 1)              # $03
            + move_abs_d(1, 0xBFD200, 2)              # $C0
            + move_abs_d(1, 0xBFE200, 3)              # 0
            + move_abs_d(1, 0xBFD201, 4)              # 0
            + move_abs_d(1, 0xBFE001, 5)              # ($02&$03)|($EB&$FC)=$EA
            + move_abs_d(2, 0xBFE000, 6)              # open | CIA-A PRA: $00EA
            + [HALT])
    seeds = {0xBFE201: 0x11, 0xBFD200: 0x22, 0xBFE200: 0x33, 0xBFD201: 0x44,
             0xBFE001: 0x55, 0xBFE000: 0x66}

    def chip(c: ChipRef):
        for a, v in ((0xBFE201, 3), (0xBFD200, 0xC0), (0xBFE200, 0x77),
                     (0xBFD201, 0x66), (0xBFE001, 2)):
            c.wb(a, v)
        for a in (0xBFE201, 0xBFD200, 0xBFE200, 0xBFD201, 0xBFE001,
                  0xBFE000, 0xBFE001):
            c.rb(a)
    return Leg("cia", code, seeds,
               {"D1": 0xFFFFFF03, "D2": 0xFFFFFFC0, "D3": 0xFFFFFF00,
                "D4": 0xFFFFFF00, "D5": 0xFFFFFFEA, "D6": 0xFFFF00EA},
               dict(seeds), chip)


def _serial_leg(name: str, n: int, sern0: int) -> Leg:
    """n SERDAT writes of byte i = (i & $FF) ^ (i >> 8) (distinct per
    256-byte page of the run, so a ring page written to the wrong slot
    shows), chip_sern seeded to sern0 first."""
    code = (moveq(0, 1) + move_imm_d(2, n - 1, 0)
            + [0x3601,                                # loop: move.w d1,d3
               0xE04B,                                #       lsr.w #8,d3
               0xB303]                                #       eor.b d1,d3
            + move_d_abs(2, 3, 0xDFF030)              #       SERDAT := d3
            + [0x5241,                                #       addq.w #1,d1
               0x51C8, 0xFFF0, HALT])                 #       dbra d0,loop
    seeds = {0xDFF030: SENT, 0xDFF031: SENT}

    def chip(c: ChipRef):
        c.ser0 = sern0
        for i in range(n):
            c.c_ww(0xDFF030, ((i & 0xFF) ^ (i >> 8)) & 0xFF)
    return Leg(name, code, seeds, {"D0": 0xFFFF, "D1": n}, dict(seeds), chip,
               sern0)


def leg_serial() -> Leg:
    """1300 SERDAT writes from a zero count: the 1024-byte ring (chip.s
    CHIP_SERSZ) wraps past its end, every page is written, and both u16
    counters (chip_sern, chip_logn) carry into their high byte."""
    return _serial_leg("serial", 1300, 0)


def leg_serwrap() -> Leg:
    """1500 SERDAT writes from chip_sern = $FD44 (65536 - 700): the u16
    count wraps through 0 mid-run (1024 divides 65536, so the slot stays
    continuous) and the ring wraps past 1024 again."""
    return _serial_leg("serwrap", 1500, 0x10000 - 700)


LEGS = {"main": leg_main, "cold": leg_cold, "split": leg_split,
        "carry": leg_carry, "byte": leg_byte, "cia": leg_cia,
        "serial": leg_serial, "serwrap": leg_serwrap}

# Direct-API cases: (entry, address, value-or-None, warm?)
API_ADDRS = [
    (0xDFF180, (1, 2, 4)), (0xDFF181, (1, 2, 4)), (0xDFF004, (1, 2, 4)),
    (0xDFF016, (2, 4)), (0xDFF07C, (2,)), (0xDFF1FE, (2, 4)),
    (0xDFF030, (1, 2)), (0xDF0100, (1, 2, 4)), (0xDFFFFC, (4,)),
    (0xBFE001, (1, 2, 4)), (0xBFE000, (1, 2, 4)), (0xBFD000, (1, 2)),
    (0xBFE0FF, (2, 4)), (0xBFFFFC, (4,)),
    # split / carry routes
    (0xDFFFFE, (2, 4)), (0xDFFFFD, (4,)), (0xDFFFFF, (2, 4)),
    (0xBFFFFE, (2, 4)), (0xBFFFFF, (2,)), (0xBEFFFE, (4,)),
    (0xBEFFFF, (2, 4)), (0xBEFFFD, (4,)), (0xDEFFFE, (4,)),
    (0xDEFFFF, (2, 4)), (0xDEFFFD, (4,)),
    # control: not I/O (fast path / plain straddle), must behave as RAM
    (0xC00000, (2, 4)), (0xE0FFFE, (4,)), (0xBDFFFE, (4,)),
]


# --------------------------------------------------------------------------
# VICE driver
# --------------------------------------------------------------------------

class Rig:
    """One VICE target with the runner PRG, plus raw REU access."""

    def __init__(self, target, labels):
        from tests.harness_util import lookup_label
        from tests.run_program import Session
        self.tg, self.t, self.labels = target, target.transport, labels
        self.lab = lambda n: lookup_label(labels, n)
        self.sess = Session(target=target, labels=labels, timeout=120.0)
        self.m_rbk = self.lab("m_rok") + 4       # core.s ZEROPAGE order
        self.chip = {}
        for n in ("chip_shd", "chip_log", "chip_logn",
                  "chip_ser", "chip_sern", "chip_nopen", "chip_reset"):
            try:
                self.chip[n] = self.lab(n)
            except KeyError:
                pass
        self.has_chip = len(self.chip) == 7
        if self.has_chip:
            from tests.aros_boot import ser_size
            self.sersz = ser_size(self.lab)     # CHIP_SERSZ, label-checked
        self.cps = None

    def call(self, addr: int, timeout: float = 30.0) -> None:
        from tests.harness_util import call_runner
        call_runner(self.tg, addr, timeout)

    def _dma(self, a: int, n: int, cmd: int) -> None:
        from c64_test_harness import write_bytes
        rd = self.lab("mem_rec_dirty")
        c = bytearray()
        for reg, v in ((0x02, DBUF & 0xFF), (0x03, DBUF >> 8),
                       (0x04, a & 0xFF), (0x05, (a >> 8) & 0xFF),
                       (0x06, a >> 16), (0x07, n), (0x08, 0), (0x0A, 0),
                       (0x01, cmd)):
            c += bytes([0xA9, v, 0x8D, reg, 0xDF])
        c += bytes([0x20, rd & 0xFF, rd >> 8, 0x60])   # jsr mem_rec_dirty
        write_bytes(self.t, STUB, bytes(c))
        self.call(STUB)

    def reu_poke(self, a: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, DBUF, data)
        self._dma(a, len(data), 0x90)

    def reu_peek(self, a: int) -> int:
        from c64_test_harness import read_bytes
        self._dma(a, 1, 0x91)
        return read_bytes(self.t, DBUF, 1)[0]

    def rbk(self) -> int:
        from c64_test_harness import read_bytes
        return read_bytes(self.t, self.m_rbk, 1)[0]

    def chip_reset(self) -> None:
        if self.has_chip:
            self.call(self.chip["chip_reset"])

    def chip_state(self) -> dict | None:
        from c64_test_harness import read_bytes
        if not self.has_chip:
            return None
        c = self.chip
        logn = struct.unpack("<H", read_bytes(self.t, c["chip_logn"], 2))[0]
        sern = struct.unpack("<H", read_bytes(self.t, c["chip_sern"], 2))[0]
        raw = read_bytes(self.t, c["chip_log"], 4 * LOG_ENTRIES)
        n = min(logn, LOG_ENTRIES)
        first = logn - n
        log = []
        for k in range(first, logn):
            i = 4 * (k % LOG_ENTRIES)
            log.append(struct.unpack("<HH", raw[i:i + 4]))
        ring = bytes(read_bytes(self.t, c["chip_ser"], self.sersz))
        return {"shd": bytes(read_bytes(self.t, c["chip_shd"], 512)),
                "logn": logn, "log": log, "ring": ring, "sern": sern,
                "nopen": struct.unpack(
                    "<H", read_bytes(self.t, c["chip_nopen"], 2))[0]}

    # watchpoints: count REC command stores and all REC accesses
    def counters(self) -> None:
        from c64_test_harness.backends.vice_binary import CMD_CHECKPOINT_SET
        self.cps = {}
        for name, lo, hi, op in (("cmd", REC_COMMAND, REC_COMMAND, CPU_STORE),
                                 ("rec", REC_FIRST, REC_LAST,
                                  CPU_STORE | CPU_LOAD)):
            body = struct.pack("<HHBBBB", lo, hi, 0, 1, op, 0)
            r = self.t._send_and_recv(CMD_CHECKPOINT_SET, body)
            self.cps[name] = struct.unpack_from("<I", r.body, 0)[0]

    def count(self) -> dict:
        hits = {cp["number"]: cp["hit_count"] for cp in self.t.checkpoint_list()}
        return {k: hits[n] for k, n in self.cps.items()}


def check_chip(rig: Rig, want: ChipRef, got: dict | None, fails: list,
               tag: str) -> None:
    if got is None:
        fails.append(f"{tag}: chip state labels absent (no runtime/ops/chip.s)")
        return
    if (got["log"] != want.log[-LOG_ENTRIES:]
            or got["logn"] != len(want.log) & 0xFFFF):
        fails.append(f"{tag}: log {[(hex(r), hex(v)) for r, v in got['log']]}"
                     f" want {[(hex(r), hex(v)) for r, v in want.log[-LOG_ENTRIES:]]}"
                     f" (logn {got['logn']} want {len(want.log)})")
    wring = want.ser_ring(len(got["ring"]))
    if got["sern"] != want.sern():
        fails.append(f"{tag}: chip_sern {got['sern']} want {want.sern()}")
    if got["ring"] != wring:
        diff = [f"{i}:{got['ring'][i]:02X}/{wring[i]:02X}"
                for i in range(len(wring)) if got["ring"][i] != wring[i]]
        fails.append(f"{tag}: serial ring differs in {len(diff)} slot(s) "
                     f"(slot:got/want) {diff[:8]}")
    if got["shd"] != bytes(want.shd):
        diff = [f"${i:03X}:{got['shd'][i]:02X}/{want.shd[i]:02X}"
                for i in range(512) if got["shd"][i] != want.shd[i]]
        fails.append(f"{tag}: shadow differs {diff[:8]}")
    if got["nopen"] != want.nopen:
        fails.append(f"{tag}: open-bus count {got['nopen']} want {want.nopen}")


def seed(rig: Rig, seeds: dict[int, int]) -> None:
    for a, v in sorted(seeds.items()):
        rig.reu_poke(a, bytes([v]))


def reset_checked(rig: Rig, fails: list, tag: str) -> None:
    """chip_reset over POISONED chip state, then every byte it owns must
    read 0: the shadow, the log, the whole serial ring and the three u16
    counters.  Every leg does this before its run, so a partial clear is
    caught whatever ran before (round 1, R5: a reset clearing only the
    ring's first page is killed by any single leg)."""
    from c64_test_harness import read_bytes, write_bytes
    if not rig.has_chip:
        rig.chip_reset()
        return
    c, sz = rig.chip, rig.sersz
    for name, n in (("chip_shd", 512), ("chip_log", 4 * LOG_ENTRIES),
                    ("chip_ser", sz)):
        write_bytes(rig.t, c[name], bytes([0xEE]) * n)
    for name in ("chip_logn", "chip_sern", "chip_nopen"):
        write_bytes(rig.t, c[name], b"\x34\x12")
    rig.chip_reset()
    for name, n in (("chip_shd", 512), ("chip_log", 4 * LOG_ENTRIES),
                    ("chip_ser", sz), ("chip_logn", 2), ("chip_sern", 2),
                    ("chip_nopen", 2)):
        got = bytes(read_bytes(rig.t, c[name], n))
        if any(got):
            nz = [i for i, b in enumerate(got) if b]
            fails.append(f"{tag}: chip_reset left {name} non-zero at {len(nz)} "
                         f"byte(s), first offsets {nz[:6]}")


def run_leg(rig: Rig, leg: Leg) -> list[str]:
    fails: list[str] = []
    exe = hunk.parse(_build(leg.words))
    chunks, man = image.build_image(exe)
    rig.sess.stage(chunks)
    seed(rig, leg.seeds)
    reset_checked(rig, fails, leg.name)
    if leg.sern0:
        from c64_test_harness import write_bytes
        write_bytes(rig.t, rig.chip["chip_sern"], struct.pack("<H", leg.sern0))
    res = rig.sess.run_program(man["entry"], man["stack_top"], 10000)
    if not (res.halt and res.reason == 0 and not res.timeout):
        fails.append(f"{leg.name}: no clean HALT (halt={res.halt} "
                     f"reason={res.reason} timeout={res.timeout})")
    for r, v in leg.regs.items():
        if res.regs[r] != v:
            fails.append(f"{leg.name}: {r}={res.regs[r]:08X} want {v:08X}")
    for a, v in sorted(leg.reu_after.items()):
        g = rig.reu_peek(a)
        if g != v:
            fails.append(f"{leg.name}: REU ${a:06X}={g:02X} want {v:02X}")
    rb = rig.rbk()
    if rb in (0xBF, 0xDF):
        fails.append(f"{leg.name}: m_rbk = ${rb:02X} after the run")
    want = ChipRef()
    leg.chip_fn(want)
    check_chip(rig, want, rig.chip_state(), fails, leg.name)
    return fails


def _build(words: list[int]) -> bytes:
    b = fixturegen.HunkBuilder()
    b.add_code(words)
    return b.build()


def api_leg(rig: Rig) -> list[str]:
    """Direct API calls; the model predicts value, DMA count and chip state."""
    from c64_test_harness import read_bytes, write_bytes
    fails: list[str] = []
    names = {(op, s): rig.lab(f"mem_{op}{8 * s}")
             for op in ("read", "write") for s in (1, 2, 4)}
    rd8, dirty = names[("read", 1)], rig.lab("mem_rec_dirty")
    rig.counters()
    chip = ChipRef()
    rig.chip_reset()
    reu: dict[int, int] = {}
    n = 0

    def tramp(target: int) -> None:
        write_bytes(rig.t, STUB, bytes([
            0xA2, 0x5A, 0xA0, 0xA5, 0x20, target & 0xFF, target >> 8,
            0x8E, XY_OUT & 0xFF, XY_OUT >> 8,
            0x8C, (XY_OUT + 1) & 0xFF, (XY_OUT + 1) >> 8, 0x60]))

    def call(target, a, v=None):
        tramp(target)
        write_bytes(rig.t, ZP_M_ADDR, struct.pack("<I", a | 0xAB000000))
        write_bytes(rig.t, ZP_M_VAL, struct.pack(
            "<I", 0xDEADBEEF if v is None else v))
        write_bytes(rig.t, XY_OUT, b"\0\0")
        c0 = rig.count()
        rig.call(STUB)
        c1 = rig.count()
        xy = read_bytes(rig.t, XY_OUT, 2)
        val = struct.unpack("<I", read_bytes(rig.t, ZP_M_VAL, 4))[0]
        return val, c1["cmd"] - c0["cmd"], c1["rec"] - c0["rec"], xy

    val = 0x31
    for a, sizes in API_ADDRS:
        for s in sizes:
            span = [(a + i) & 0xFFFFFF for i in range(-1, s + 1)]
            for warm in (False, True):
                for op in ("write", "read"):
                    n += 1
                    val = (val * 73 + 17) & 0xFFFFFFFF
                    # REU sentinels around and under the access
                    for i, b in enumerate(span):
                        reu[b] = (0x40 + 3 * i + (a & 0x3F)) & 0xFF
                        rig.reu_poke(b, bytes([reu[b]]))
                    if warm:
                        call(rd8, 0x000123)           # shadow valid, bank 0
                    else:
                        rig.call(dirty)
                    g = GuestRef(chip, reu)
                    tag = f"api {op}{8 * s} ${a:06X} {'warm' if warm else 'cold'}"
                    if op == "write":
                        v = val & ((1 << 8 * s) - 1)
                        dma = g.write(a, s, v)
                        _, cmd, rec, xy = call(names[(op, s)], a, v)
                    else:
                        want, dma = g.read(a, s)
                        got, cmd, rec, xy = call(names[(op, s)], a)
                        mask = (1 << 8 * s) - 1
                        keep = 0xDEADBEEF & ~mask & 0xFFFFFFFF
                        if got != (want | keep):
                            fails.append(f"{tag}: m_val={got:08X} want "
                                         f"{want | keep:08X}")
                    if cmd != dma:
                        fails.append(f"{tag}: {cmd} REC command store(s), "
                                     f"want {dma}")
                    if xy != b"\x5A\xA5":
                        fails.append(f"{tag}: X/Y clobbered ({xy.hex()})")
                    rb = rig.rbk()
                    if rb in (0xBF, 0xDF):
                        fails.append(f"{tag}: m_rbk = ${rb:02X}")
                    for b in span:
                        got_b = rig.reu_peek(b)
                        if got_b != reu[b]:
                            fails.append(f"{tag}: REU ${b:06X}={got_b:02X} "
                                         f"want {reu[b]:02X}")
    check_chip(rig, chip, rig.chip_state(), fails, "api")
    print(f"  api: {n} calls")
    return fails


VERBOSE = False


def run_all(prg: str, legs: list[str]) -> list[str]:
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    fails: list[str] = []
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        try:
            prepare_target(tg, prg)
            rig = Rig(tg, labels)
            for name in legs:
                f = api_leg(rig) if name == "api" else run_leg(rig, LEGS[name]())
                print(f"[{name}] {'PASS' if not f else 'FAIL'}")
                lim = len(f) if VERBOSE else 40
                for x in f[:lim]:
                    print(f"    {x}")
                if len(f) > lim:
                    print(f"    ... {len(f) - lim} more")
                fails += f
        finally:
            mgr.release(tg)
    return fails


# --------------------------------------------------------------------------
# Teeth: the four S3_PLAN mutants of runtime/core.s
# --------------------------------------------------------------------------

MUTANTS = {
    # the cold path (mio_full) forgets the I/O test before its REC stores
    "nocold": [("        lda m_addr+2            ; W7-T cold path: I/O bank?  Tested before\n"
                "        cmp #$DF                ; W7-T any REC store, so a trapped cold\n"
                "        beq io                  ; W7-T call touches no REC register\n"
                "        cmp #$BF                ; W7-T\n"
                "        beq io                  ; W7-T\n", "")],
    # mio_split moves every byte through the REU, I/O or not
    "nosplit": [("        cmp #$DF                ; W7-T split: I/O byte?  (The address\n"
                 "        beq io                  ; W7-T stores above are harmless: shadow\n"
                 "        cmp #$BF                ; W7-T already invalid, and no command.)\n"
                 "        beq io                  ; W7-T\n", "")],
    # STRADDLE has no bank+1 route: $BEFFFE/$DEFFFE carry into the I/O bank
    "nobank1": [("        lda m_addr+2            ; W7-T bank carry into $BF/$DF?\n"
                 "        cmp #$BE                ; W7-T (only reached at $xxFFFD-$xxFFFF:\n"
                 "        beq slow                ; W7-T  plain branches, not hot)\n"
                 "        cmp #$DE                ; W7-T\n"
                 "        beq slow                ; W7-T\n", "")],
    # m_rbk is stored before the trap test (the invariant breaks)
    "rbkfirst": [("        BRNC beq, done                ; 3\n"
                  "        cmp #$DF                ; W7-T 2  bank changed: I/O?\n",
                  "        BRNC beq, done                ; 3\n"
                  "        sta m_rbk               ; MUTANT\n"
                  "        cmp #$DF                ; W7-T 2  bank changed: I/O?\n")],
}


# Source mutants of runtime/ops/chip.s (review round 1, L3)
CHIP_MUTANTS = {
    # the SERDAT byte counter never carries into its high byte
    "ser_nowrap_hi": [("sinc:   inc chip_sern           ; then the count (u16; the store came first)\n"
                       "        bne done\n        inc chip_sern+1\n",
                       "sinc:   inc chip_sern           ; MUTANT\n"
                       "        bne done\n")],
    # c64u ser-ring: the old 256-byte ring (slot = count mod 256; the page
    # bits ignored, every byte to page 0)
    "ser_mod256": [("        lda chip_sern+1         ; X = slot bits 7-0, the page = count bits 9-8\n",
                    "        lda #0                  ; MUTANT\n")],
    # round 1, R5: chip_reset clears only the ring's first page (the 256 B
    # ring's clear)
    "ser_clr0": [("        sta chip_ser+256,x\n        sta chip_ser+512,x\n"
                  "        sta chip_ser+768,x\n", "")],
    # c64u ser-ring: pages 1 and 2 swapped (count bits 8 and 9 read the
    # wrong way round)
    "ser_pageswap": [("s2:     sta chip_ser+512,x      ; page 2\n",
                      "s2:     sta chip_ser+256,x      ; MUTANT\n"),
                     ("        sta chip_ser+256,x      ; page 1\n",
                      "        sta chip_ser+512,x      ; MUTANT\n")],
}


def build_mutant(name: str, out_dir: str) -> str:
    from tests.rec_build import make_sources
    out_dir = os.path.join(ROOT, out_dir)
    gen = os.path.join(out_dir, "gen", "dispatch.s")
    os.makedirs(os.path.dirname(gen), exist_ok=True)
    subprocess.run([sys.executable, "host/gen_dispatch.py", "-o", gen],
                   cwd=ROOT, check=True)
    rel = "ops/chip.s" if name in CHIP_MUTANTS else "core.s"
    core = os.path.join(ROOT, "runtime", rel)
    text = open(core).read()
    for a, b in {**MUTANTS, **CHIP_MUTANTS}[name]:
        if text.count(a) < 1:
            raise RuntimeError(f"mutant {name}: anchor not found")
        text = text.replace(a, b)
    mpath = os.path.join(out_dir, f"mut_{name}_{os.path.basename(rel)}")
    with open(mpath, "w") as f:
        f.write(text)
    srcs = [mpath if s == core else s for s in make_sources()]
    prg = os.path.join(out_dir, "runner.prg")
    cmd = ["cl65", "-t", "none", "-C", "runtime/c64.cfg", "-o", prg,
           "-Ln", os.path.join(out_dir, "runner.lbl"),
           "--asm-define", "BRNC_WARN_ONLY", "--asm-include-dir", "runtime",
           "--asm-include-dir", "runtime/ops"]
    cmd += [os.path.relpath(s, ROOT) for s in srcs] + [gen]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"mutant build failed: {r.stdout}\n{r.stderr}")
    return prg


# --------------------------------------------------------------------------
# Perf: +0 on same-bank calls, exactly +8 cycles per bank switch
# --------------------------------------------------------------------------

DATA_A, DATA_B = 0x008000, 0x028000       # bank $00 / bank $02


def _loop(body: list[int], setup: list[int], n: int) -> bytes:
    loop_len = 2 * len(body) + 4
    return _build([0x303C, n - 1] + setup + body
                  + [0x51C8, (-(loop_len - 2)) & 0xFFFF, HALT])


PERF_LOOPS = {
    # name: (setup, body, bank switches per iteration)
    "same":   ([0x41F9] + hi_lo(DATA_A) + [0x43F9] + hi_lo(DATA_A + 0x10),
               [0x2210, 0x2411], 0),          # MOVE.L (A0),D1 / (A1),D2
    "alt":    ([0x41F9] + hi_lo(DATA_A) + [0x43F9] + hi_lo(DATA_B),
               [0x2210, 0x2411], 2),          # banks $00/$02 alternate
}


def refill_prog(n: int, cross: bool) -> bytes:
    """Loop that jumps between two code pages every iteration (cross) or
    stays in one page (control): JMP abs.L + DBRA, same instructions."""
    tgt = BASE + (0x100 if cross else 0x10)
    p0 = [0x303C, n - 1]                                  # 0: move.w #n-1,d0
    loop = BASE + 2 * len(p0)
    p0 += jmp_abs(tgt)                                    # 4: jmp tgt
    words = pad_to(p0, tgt - BASE)
    here = tgt + 2
    words += [0x51C8, (loop - here) & 0xFFFF, HALT]       # tgt: dbra d0,loop
    return _build(words)


def cold_prog(n: int, cross: bool) -> bytes:
    """refill_prog with a MOVE.L (A0),D1 after each landing: across pages
    every such read is the first data call after a refill (cold path);
    in-page it is a warm same-bank call."""
    tgt = BASE + (0x100 if cross else 0x20)
    p0 = [0x303C, n - 1] + [0x41F9] + hi_lo(DATA_A)       # move.w / lea
    loop = BASE + 2 * len(p0)
    p0 += [0x2210] + jmp_abs(tgt)                          # loop: read, jmp
    words = pad_to(p0, tgt - BASE)
    here = tgt + 4
    words += [0x2210, 0x51C8, (loop - here) & 0xFFFF, HALT]  # read, dbra
    return _build(words)


def perf_measure(prg: str) -> dict:
    from c64_test_harness import (Labels, UnifiedManager, ViceConfig,
                                  read_bytes, write_bytes)
    from tests.harness_util import REU_VICE_ARGS, call_runner, lookup_label
    from tests.heap_cost import stub_code
    from tests.run_program import BUF_CASE, RESULT_LEN, RunResult, Session
    from tests.run_vectors import prepare_target
    sys.path.insert(0, os.path.join(ROOT, "tools", "s3probe"))
    from bank_probe import bank_sites
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    sites = bank_sites(prg)
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    out = {"sites": len(sites)}
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        try:
            prepare_target(tg, prg)
            t = tg.transport
            write_bytes(t, 0xD011, bytes([0x0B]))              # DEN = 0
            from c64_test_harness.backends.vice_binary import CMD_CHECKPOINT_SET
            cps = []
            for s in sites:
                body = struct.pack("<HHBBBB", s, s, 0, 1, CPU_EXEC, 0)
                r = t._send_and_recv(CMD_CHECKPOINT_SET, body)
                cps.append(struct.unpack_from("<I", r.body, 0)[0])

            def switches():
                h = {c["number"]: c["hit_count"] for c in t.checkpoint_list()}
                return sum(h[c] for c in cps)
            sess = Session(target=tg, labels=labels, timeout=120.0)
            run_addr = lookup_label(labels, "test_run_program")

            def run(blob):
                chunks, man = image.build_image(hunk.parse(blob))
                sess.stage(chunks)
                write_bytes(t, BUF_CASE, struct.pack(
                    "<III", man["entry"], image.STACK_TOP, 0))
                write_bytes(t, STUB, stub_code(run_addr))
                s0 = switches()
                call_runner(tg, STUB, 120.0)
                s1 = switches()
                a = read_bytes(t, 0xDD04, 4)
                ta, tb = a[0] | a[1] << 8, a[2] | a[3] << 8
                rr = RunResult.from_bytes(read_bytes(t, 0xC800, RESULT_LEN))
                if not (rr.halt and rr.reason == 0):
                    raise RuntimeError(f"loop did not HALT: {rr}")
                return (0xFFFF - ta) + 0x10000 * (0xFFFF - tb), s1 - s0
            n1, n2 = 40, 240
            for name, (setup, body, _sw) in PERF_LOOPS.items():
                c1, s1 = run(_loop(body, setup, n1))
                c2, s2 = run(_loop(body, setup, n2))
                out[name] = {"cyc_per_iter": (c2 - c1) / (n2 - n1),
                             "switch_per_iter": (s2 - s1) / (n2 - n1)}
            for name, fn, cross in (("refill_cross", refill_prog, True),
                                    ("refill_same", refill_prog, False),
                                    ("cold_cross", cold_prog, True),
                                    ("cold_same", cold_prog, False)):
                c1, _ = run(fn(n1, cross))
                c2, _ = run(fn(n2, cross))
                out[name] = {"cyc_per_iter": (c2 - c1) / (n2 - n1)}
        finally:
            mgr.release(tg)
    return out


def perf(prg: str, base: str, json_out: str) -> int:
    new, old = perf_measure(prg), perf_measure(base)
    fails = []
    print(f"6502 cycles per iteration (40/240 delta, screen off, step mode)")
    for name in PERF_LOOPS:
        d = new[name]["cyc_per_iter"] - old[name]["cyc_per_iter"]
        sw = new[name]["switch_per_iter"]
        per = d / sw if sw else None
        print(f"  {name:6} base {old[name]['cyc_per_iter']:9.3f}  trap "
              f"{new[name]['cyc_per_iter']:9.3f}  delta {d:+.3f}  "
              f"switches/iter {sw:.3f} (base {old[name]['switch_per_iter']:.3f})"
              + (f"  = {per:+.3f}/switch" if per is not None else ""))
        if sw != old[name]["switch_per_iter"]:
            fails.append(f"{name}: switch count changed")
        if sw == 0 and abs(d) > 1e-9:
            fails.append(f"{name}: same-bank delta {d:+.3f} != 0")
        if sw and abs(per - 8.0) > 1e-9:
            fails.append(f"{name}: {per:+.3f} cycles per switch != +8")
    rc, rs = new["refill_cross"]["cyc_per_iter"], new["refill_same"]["cyc_per_iter"]
    print(f"  refill: cross-page loop {rc:.3f}, in-page control {rs:.3f} -> "
          f"{(rc - rs) / 2:.3f} cycles per window refill (2 per iteration)")
    for k in ("cold_cross", "cold_same"):
        d = new[k]["cyc_per_iter"] - old[k]["cyc_per_iter"]
        print(f"  {k:10} base {old[k]['cyc_per_iter']:9.3f}  trap "
              f"{new[k]['cyc_per_iter']:9.3f}  delta {d:+.3f}"
              + ("  = %+.3f per cold call (2 per iteration)" % (d / 2)
                 if k == "cold_cross" else ""))
    print(f"  bank-store sites: trap {new['sites']}, base {old['sites']}")
    if json_out:
        with open(json_out, "w") as f:
            json.dump({"trap": new, "base": old, "fails": fails}, f, indent=1)
    for f in fails:
        print("PERF FAIL", f)
    return 1 if fails else 0


# --------------------------------------------------------------------------
# Hardware (U64E): one unattended ABBA session at 48 MHz, base vs trap,
# plus the cost of one code-window refill (docs/S3_PLAN.md §3.5, ~90 us)
# --------------------------------------------------------------------------

EXPECT_FW, EXPECT_GIT = "3.15", "c47155b7"


def _hw_loops():
    from tests.bench import u64bench as ub
    same_s, same_b, _ = PERF_LOOPS["same"]
    alt_s, alt_b, _ = PERF_LOOPS["alt"]
    io = lambda n: _build([0x303C, n - 1] + move_abs_d(2, 0xDFF004, 1)
                          + [0x51C8, 0xFFF8, HALT])
    return [
        ("alu", ub.hunk_alu68k, ub.alu68k_icount),
        ("mem", ub.hunk_mem68k, ub.mem68k_icount),
        ("stream", ub.hunk_stream68k, ub.mem68k_icount),
        ("rmw", ub.hunk_rmw68k, ub.mem68k_icount),
        ("same", lambda n: _loop(same_b, same_s, n), lambda n: 4 + 3 * n),
        ("alt", lambda n: _loop(alt_b, alt_s, n), lambda n: 4 + 3 * n),
        ("refill_same", lambda n: refill_prog(n, False), lambda n: 2 + 2 * n),
        ("refill_cross", lambda n: refill_prog(n, True), lambda n: 2 + 2 * n),
        # MOVE.W $DFF004,D1: base reads the REU (same bank every time),
        # trap runs chip_r16 on every iteration (informational)
        ("io_vposr", io, lambda n: 2 + 2 * n),
    ]


def hw_session(trap: str, base: str, out_dir: str, backend: str,
               mhz: int = 48) -> int:
    from tests.bench import u64bench as ub
    from tests.bench.u64_session import Hw, dump, log, u64_hold
    from tests.run_program import Session
    trap, base = os.path.abspath(trap), os.path.abspath(base)
    os.makedirs(out_dir, exist_ok=True)
    rows: list[dict] = []

    def body(hw: Hw) -> None:
        for i, (tag, prg) in enumerate((("base", base), ("trap", trap),
                                        ("trap", trap), ("base", base))):
            labels = hw.load(prg)
            hw.speed(mhz)
            row = {"mhz": mhz, "core": tag, "rep": (0, 0, 1, 1)[i],
                   "prg": os.path.relpath(prg, ROOT)}
            oc2 = min(255, 2 + 6 * mhz)
            r = ub.two_point(hw.target, ub.ALU_ADDR, ub.code_alu, 2, oc2, 600.0)
            eff = r["dc"] * ub.ALU_CYCLES_PER_OC / r["dt"] / 1e6
            row["eff_mhz"] = eff
            sess = Session(target=hw.target, labels=labels, timeout=900.0)
            n2 = min(32000, max(2200, 500 * mhz))
            for name, build, icf in _hw_loops():
                r = ub.bench_68k(sess, build, icf, 200, n2)
                iters = (icf(n2) - icf(200)) / (icf(1) - icf(0))
                row[f"{name}_ips"] = r["ips"]
                row[f"{name}_us_iter"] = r["dt"] / iters * 1e6
                row[f"{name}_dt"] = r["dt"]
            log(f"  {mhz} MHz {tag:<4} eff {eff:6.3f}  " + "  ".join(
                f"{n} {row[n + '_ips']:7.0f}" for n, _, _ in _hw_loops()))
            rows.append(row)
            dump(os.path.join(out_dir, "rows.json"), rows)
        hw.speed(1)

    if backend == "vice":
        from tests.bench.u64bench import ViceBoot
        vice = ViceBoot()
        try:
            body(Hw(None, out_dir, vice=vice))
        finally:
            vice.close()
        info = {"backend": "vice (dry run: no performance meaning)"}
    else:
        info = _hw_gate()
        if "error" in info:
            log(f"firmware gate FAILED: {info}")
            dump(os.path.join(out_dir, "gate.json"), info)
            return 2
        with u64_hold(out_dir) as hold:
            info = hold["info"]
            if not (info.get("firmware_version") == EXPECT_FW
                    and info.get("git_commit_hash") == EXPECT_GIT):
                raise RuntimeError(f"firmware changed under the lock: {info}")
            body(Hw(hold["target"], out_dir))
        info["restored"] = hold["restored"]
        info["reset_ok"] = hold["reset_ok"]
    res = hw_summary(rows)
    res["device"] = info
    res["rows"] = rows
    dump(os.path.join(out_dir, "hw_bench.json"), res)
    for line in res["lines"]:
        print(line)
    if backend != "vice" and not info.get("restored"):
        print("CONFIG NOT RESTORED -- see config_after.json")
        return 1
    return 0


def _hw_gate() -> dict:
    """Bodyless GET /v1/info under the DeviceLock (nestable, so u64_hold
    joins this hold): the resting build or nothing runs."""
    from c64_test_harness import DeviceLock, Ultimate64Client
    from c64_test_harness.backends.device_lock import device_key
    host = os.environ.get("U64_HOST")
    if not host:
        return {"error": "U64_HOST not set"}
    lock = DeviceLock(device_key(host), allow_nested=True)
    lock.acquire_or_raise(timeout=6 * 3600.0)
    ok = False
    try:
        client = Ultimate64Client(host)
        try:
            info = client.get_info()
        finally:
            client.close()
        if (info.get("firmware_version") == EXPECT_FW
                and info.get("git_commit_hash") == EXPECT_GIT):
            ok = True
            _HELD.append(lock)
            return info
        return {"error": f"not fw {EXPECT_FW} git {EXPECT_GIT}", "info": info}
    finally:
        if not ok:
            lock.release()


_HELD: list = []


def hw_summary(rows: list[dict]) -> dict:
    def mean(core, key):
        v = [r[key] for r in rows if r["core"] == core]
        return sum(v) / len(v) if v else float("nan")
    keys = [n for n, _, _ in _hw_loops()]
    out = {"lines": []}
    for k in keys:
        b, t = mean("base", f"{k}_ips"), mean("trap", f"{k}_ips")
        spread = [r[f"{k}_ips"] for r in rows]
        out[k] = {"base_ips": b, "trap_ips": t, "change": t / b - 1,
                  "base_us_iter": mean("base", f"{k}_us_iter"),
                  "trap_us_iter": mean("trap", f"{k}_us_iter"),
                  "reps": spread}
        out["lines"].append(
            f"  {k:13} base {b:9.0f} IPS  trap {t:9.0f} IPS  "
            f"{100 * (t / b - 1):+6.2f}%   us/iter base "
            f"{out[k]['base_us_iter']:8.2f} trap {out[k]['trap_us_iter']:8.2f}")
    for core in ("base", "trap"):
        cross = mean(core, "refill_cross_us_iter")
        same = mean(core, "refill_same_us_iter")
        eff = mean(core, "eff_mhz")
        out[f"refill_{core}"] = {"us": (cross - same) / 2,
                                 "eff_cycles": (cross - same) / 2 * eff}
        out["lines"].append(
            f"  one code-window refill ({core}): {(cross - same) / 2:.2f} us "
            f"= {(cross - same) / 2 * eff:.0f} cycles at {eff:.3f} MHz eff")
    alt = out["alt"]["trap_us_iter"] - out["alt"]["base_us_iter"]
    same = out["same"]["trap_us_iter"] - out["same"]["base_us_iter"]
    out["lines"].append(f"  bank-alternating loop: trap - base = {alt * 1e3:+.0f} ns"
                        f"/iter (2 switches), same-bank {same * 1e3:+.0f} ns/iter")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default=",".join(list(LEGS) + ["api"]))
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only-mutant", default="")
    ap.add_argument("--perf", action="store_true")
    ap.add_argument("--base", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--hw", choices=["u64", "vice"],
                    help="hardware ABBA session at 48 MHz (--base needed); "
                         "vice = dry run of the session logic")
    ap.add_argument("--out-dir", default=os.path.join(
        ROOT, "build", "evidence", "w7-trap", "hw"))
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every failure, not the first 40 per leg")
    a = ap.parse_args(argv)
    global VERBOSE
    VERBOSE = a.verbose
    legs = [x for x in a.legs.split(",") if x]
    if a.hw:
        if not a.base:
            ap.error("--hw needs --base")
        try:
            return hw_session(a.prg, a.base, a.out_dir, a.hw,
                              mhz=48 if a.hw == "u64" else 4)
        finally:
            for lk in _HELD:
                lk.release()
    if a.perf:
        if not a.base:
            ap.error("--perf needs --base")
        return perf(a.prg, a.base, a.json_out)
    if a.mutants:
        names = [a.only_mutant] if a.only_mutant else list(MUTANTS) + list(
            CHIP_MUTANTS)
        survived = []
        bdir = os.path.relpath(os.path.dirname(os.path.abspath(a.prg)), ROOT)
        for m in names:
            prg = build_mutant(m, f"{bdir}-mut-{m}")
            print(f"=== mutant {m}: {prg}")
            fails = run_all(prg, legs)
            print(f"=== mutant {m}: {'KILLED' if fails else 'SURVIVED'} "
                  f"({len(fails)} failure(s))")
            if not fails:
                survived.append(m)
        print(f"mutants: {len(names) - len(survived)}/{len(names)} killed"
              + (f"; SURVIVED: {survived}" if survived else ""))
        return 1 if survived else 0
    fails = run_all(a.prg, legs)
    print(f"run_chip: {len(legs)} leg(s), {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
