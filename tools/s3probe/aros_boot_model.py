#!/usr/bin/env python3
"""Minimal-OCS reference model: boot the AROS m68k ROM to "insert disk".

W6-D, docs/S3_PLAN.md question 3.  A HOST-SIDE experiment, not runtime
code: it answers "what does the AROS ROM touch before the insert-disk
screen, and how many 68000 instructions does it take", using exactly the
machine model the plan proposes for the C64 side:

  * guest memory = 16 MB of flat RAM (the REU model: no chip-RAM mirrors,
    no bus holes, ROM images loaded as ordinary RAM at $F80000/$E00000),
    except the two trapped banks $BF (CIAs) and $DF (custom chips);
  * a synchronous blitter (a blit completes at the BLTSIZE write);
  * a copper that is interpreted once per frame at vblank, MOVEs only
    (WAITs ignored: the last value per frame wins);
  * CIA timers/TOD/ICR, Paula INTENA/INTREQ, autovectored IRQ levels 1-6;
  * beam position (VPOSR/VHPOSR) derived from the virtual cycle count.

The CPU is Musashi via machine68k (tools/s3probe/setup_venv.sh builds it,
outside the repo).  The ROM is read from the W6-O cache
(~/.cache/amiga64/aros/<sha>/), never copied.  Outputs (evidence dir):
register-access table (first instruction index, R/W counts, CPU vs copper),
instruction mix by opcode high byte, privileged/exception opcode counts,
IRQ deliveries per level, blitter statistics, idle (STOP) time, and PNG
snapshots of the display (pure-stdlib PNG writer) to check the screen.

Timing is virtual: 7.09 MHz 68000 cycles as Musashi counts them (no DMA
contention), 454 cycles per line, 313 lines per frame (PAL).  Instruction
counts are exact for this model; they are not an A500 cycle-exact trace.

WB-measure (docs/WB_BOOT.md) adds DF0: with a disk: --adf IMAGE serves disk
DMA from an ADF (MFM-encoded on the host, decoded by AROS), --td-hle traps
trackdisk BeginIO instead, --startup swaps S/Startup-Sequence in memory; the
report then carries boot milestones and per-task counts (wb_disk.py).

Usage: ~/.cache/amiga64/s3probe-venv/bin/python tools/s3probe/aros_boot_model.py
           [--rom-dir DIR] [--seconds 40] [--out build/evidence/w6-s3plan/boot]
           [--adf DISK.adf [--td-hle] [--startup FILE] [--stop-idle 3]]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import struct
import sys
import time
import zlib

import machine68k as m68

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adf import AdfImage                                       # noqa: E402
from wb_disk import Floppy, TrackdiskHLE, WbTracker, mfm_track  # noqa: E402

CPU_HZ = 7_093_790
LINE_CYC = 454
LINES = 313
FRAME_CYC = LINE_CYC * LINES
ECLK_DIV = 10

CUSTOM_NAMES = {
    0x000: "BLTDDAT", 0x002: "DMACONR", 0x004: "VPOSR", 0x006: "VHPOSR",
    0x008: "DSKDATR", 0x00A: "JOY0DAT", 0x00C: "JOY1DAT", 0x00E: "CLXDAT",
    0x010: "ADKCONR", 0x012: "POT0DAT", 0x014: "POT1DAT", 0x016: "POTINP",
    0x018: "SERDATR", 0x01A: "DSKBYTR", 0x01C: "INTENAR", 0x01E: "INTREQR",
    0x020: "DSKPTH", 0x022: "DSKPTL", 0x024: "DSKLEN", 0x026: "DSKDAT",
    0x028: "REFPTR", 0x02A: "VPOSW", 0x02C: "VHPOSW", 0x02E: "COPCON",
    0x030: "SERDAT", 0x032: "SERPER", 0x034: "POTGO", 0x036: "JOYTEST",
    0x040: "BLTCON0", 0x042: "BLTCON1", 0x044: "BLTAFWM", 0x046: "BLTALWM",
    0x048: "BLTCPTH", 0x04A: "BLTCPTL", 0x04C: "BLTBPTH", 0x04E: "BLTBPTL",
    0x050: "BLTAPTH", 0x052: "BLTAPTL", 0x054: "BLTDPTH", 0x056: "BLTDPTL",
    0x058: "BLTSIZE", 0x05A: "BLTCON0L", 0x05C: "BLTSIZV", 0x05E: "BLTSIZH",
    0x060: "BLTCMOD", 0x062: "BLTBMOD", 0x064: "BLTAMOD", 0x066: "BLTDMOD",
    0x070: "BLTCDAT", 0x072: "BLTBDAT", 0x074: "BLTADAT", 0x07C: "DENISEID",
    0x07E: "DSKSYNC", 0x080: "COP1LCH", 0x082: "COP1LCL", 0x084: "COP2LCH",
    0x086: "COP2LCL", 0x088: "COPJMP1", 0x08A: "COPJMP2", 0x08C: "COPINS",
    0x08E: "DIWSTRT", 0x090: "DIWSTOP", 0x092: "DDFSTRT", 0x094: "DDFSTOP",
    0x096: "DMACON", 0x098: "CLXCON", 0x09A: "INTENA", 0x09C: "INTREQ",
    0x09E: "ADKCON", 0x100: "BPLCON0", 0x102: "BPLCON1", 0x104: "BPLCON2",
    0x106: "BPLCON3", 0x108: "BPL1MOD", 0x10A: "BPL2MOD", 0x10C: "BPLCON4",
    0x1DC: "BEAMCON0", 0x1FC: "FMODE",
}
for _i in range(4):
    for _j, _n in enumerate(("LCH", "LCL", "LEN", "PER", "VOL", "DAT")):
        CUSTOM_NAMES[0x0A0 + 16 * _i + 2 * _j] = f"AUD{_i}{_n}"
for _i in range(6):
    CUSTOM_NAMES[0x0E0 + 4 * _i] = f"BPL{_i + 1}PTH"
    CUSTOM_NAMES[0x0E2 + 4 * _i] = f"BPL{_i + 1}PTL"
    CUSTOM_NAMES[0x110 + 2 * _i] = f"BPL{_i + 1}DAT"
for _i in range(8):
    CUSTOM_NAMES[0x120 + 4 * _i] = f"SPR{_i}PTH"
    CUSTOM_NAMES[0x122 + 4 * _i] = f"SPR{_i}PTL"
    for _j, _n in enumerate(("POS", "CTL", "DATA", "DATB")):
        CUSTOM_NAMES[0x140 + 8 * _i + 2 * _j] = f"SPR{_i}{_n}"
for _i in range(32):
    CUSTOM_NAMES[0x180 + 2 * _i] = f"COLOR{_i:02d}"

CIA_REGS = ["PRA", "PRB", "DDRA", "DDRB", "TALO", "TAHI", "TBLO", "TBHI",
            "TODLO", "TODMID", "TODHI", "UNUSED", "SDR", "ICR", "CRA", "CRB"]

PRIV_CLASSES = [
    ("RTE", lambda op: op == 0x4E73), ("STOP", lambda op: op == 0x4E72),
    ("RESET", lambda op: op == 0x4E70),
    ("MOVE to SR", lambda op: op & 0xFFC0 == 0x46C0),
    ("MOVE from SR", lambda op: op & 0xFFC0 == 0x40C0),
    ("ORI/ANDI/EORI to SR", lambda op: op in (0x007C, 0x027C, 0x0A7C)),
    ("MOVE USP", lambda op: op & 0xFFF0 == 0x4E60),
    ("TRAP #n", lambda op: op & 0xFFF0 == 0x4E40),
    ("MOVEC (illegal on 68000)", lambda op: op in (0x4E7A, 0x4E7B)),
    ("ILLEGAL", lambda op: op == 0x4AFC),
    ("A-line", lambda op: op & 0xF000 == 0xA000),
    ("F-line", lambda op: op & 0xF000 == 0xF000),
    ("MOVE to CCR", lambda op: op & 0xFFC0 == 0x44C0),
    ("TRAPV/CHK", lambda op: op == 0x4E76 or op & 0xF1C0 == 0x4180),
]


class Log:
    def __init__(self):
        self.regs = {}          # key -> [first_icount, reads, writes, copper_writes]

    def hit(self, key, kind, ic):
        e = self.regs.get(key)
        if e is None:
            e = self.regs[key] = [ic, 0, 0, 0]
        e[{"R": 1, "W": 2, "C": 3}[kind]] += 1


class CIA:
    def __init__(self, name, model, intreq_bit):
        self.name, self.m, self.bit = name, model, intreq_bit
        self.pra_in, self.prb_in = 0xFF, 0xFF
        self.pra = self.prb = self.ddra = self.ddrb = 0
        self.ta = self.tb = 0xFFFF
        self.ta_latch = self.tb_latch = 0xFFFF
        self.cra = self.crb = 0
        self.icr_data = self.icr_mask = 0
        self.tod = 0
        self.alarm = 0
        self.tod_latched = None
        self.tod_halt = False
        self.sdr = 0

    def eclock(self, n):
        """Advance the timers by n E-clock ticks."""
        ta_under = 0
        if self.cra & 1 and not self.cra & 0x20:
            if n > self.ta:
                span = self.ta_latch + 1 if self.ta_latch else 0x10000
                left = n - self.ta - 1
                ta_under = 1 + left // span
                if self.cra & 8:            # one-shot
                    ta_under = 1
                    self.cra &= ~1
                    self.ta = self.ta_latch
                else:
                    self.ta = self.ta_latch - (left % span)
                self.icr_data |= 1
            else:
                self.ta -= n
        if self.crb & 1:
            mode = (self.crb >> 5) & 3
            steps = n if mode == 0 else (ta_under if mode >= 2 else 0)
            if steps:
                if steps > self.tb:
                    span = self.tb_latch + 1 if self.tb_latch else 0x10000
                    left = steps - self.tb - 1
                    if self.crb & 8:
                        self.crb &= ~1
                        self.tb = self.tb_latch
                    else:
                        self.tb = self.tb_latch - (left % span)
                    self.icr_data |= 2
                else:
                    self.tb -= steps
        self.update()

    def tod_tick(self, n=1):
        if self.tod_halt:
            return
        for _ in range(n):
            self.tod = (self.tod + 1) & 0xFFFFFF
            if self.tod == self.alarm:
                self.icr_data |= 4
        self.update()

    def update(self):
        if self.icr_data & self.icr_mask & 0x1F:
            self.m.set_intreq_bit(self.bit)

    def read(self, r):
        if r == 0:
            return (self.pra & self.ddra) | (self.pra_in & ~self.ddra & 0xFF)
        if r == 1:
            return (self.prb & self.ddrb) | (self.prb_in & ~self.ddrb & 0xFF)
        if r == 2:
            return self.ddra
        if r == 3:
            return self.ddrb
        if r == 4:
            return self.ta & 0xFF
        if r == 5:
            return self.ta >> 8
        if r == 6:
            return self.tb & 0xFF
        if r == 7:
            return self.tb >> 8
        if r in (8, 9, 10):
            if r == 10:
                self.tod_latched = self.tod
            v = self.tod_latched if self.tod_latched is not None else self.tod
            if r == 8:
                self.tod_latched = None
            return (v >> (8 * (r - 8))) & 0xFF
        if r == 12:
            return self.sdr
        if r == 13:
            v = self.icr_data | (0x80 if self.icr_data & self.icr_mask & 0x1F else 0)
            self.icr_data = 0
            return v
        if r == 14:
            return self.cra & 0xEF
        if r == 15:
            return self.crb & 0xEF
        return 0xFF

    def write(self, r, v):
        if r == 0:
            self.pra = v
        elif r == 1:
            self.prb = v
        elif r == 2:
            self.ddra = v
        elif r == 3:
            self.ddrb = v
        elif r == 4:
            self.ta_latch = (self.ta_latch & 0xFF00) | v
        elif r == 5:
            self.ta_latch = (self.ta_latch & 0xFF) | (v << 8)
            if not self.cra & 1:
                self.ta = self.ta_latch
            if self.cra & 8:                # one-shot: hi write starts
                self.ta = self.ta_latch
                self.cra |= 1
        elif r == 6:
            self.tb_latch = (self.tb_latch & 0xFF00) | v
        elif r == 7:
            self.tb_latch = (self.tb_latch & 0xFF) | (v << 8)
            if not self.crb & 1:
                self.tb = self.tb_latch
            if self.crb & 8:
                self.tb = self.tb_latch
                self.crb |= 1
        elif r in (8, 9, 10):
            sh = 8 * (r - 8)
            if self.crb & 0x80:
                self.alarm = (self.alarm & ~(0xFF << sh)) | (v << sh)
            else:
                self.tod = (self.tod & ~(0xFF << sh)) | (v << sh)
                self.tod_halt = r != 8
        elif r == 12:
            self.sdr = v
        elif r == 13:
            if v & 0x80:
                self.icr_mask |= v & 0x1F
            else:
                self.icr_mask &= ~v & 0x1F
            self.update()
        elif r == 14:
            if v & 0x10:
                self.ta = self.ta_latch
            self.cra = v & 0xEF
        elif r == 15:
            if v & 0x10:
                self.tb = self.tb_latch
            self.crb = v & 0xEF


class Amiga:
    CPU_HZ = CPU_HZ
    WIN_SIZES = (2, 4, 8, 16)
    ABLATIONS = ("no_blitter", "no_copper", "ciaa_tod_frozen", "ciab_tod_frozen",
                 "beam_frozen", "no_vertb", "cia_timers_frozen", "no_irq",
                 "ports_irq_off", "exter_irq_off", "softint_off", "blit_irq_off",
                 "ciab_timers_frozen")

    def __init__(self, rom_dir, out, ablate=(), adf=None, td_hle=False):
        self.out = out
        self.ablate = set(ablate)
        self.mach = m68.Machine(m68.CPUType.M68000, 16384)
        self.cpu, self.mem = self.mach.cpu, self.mach.mem
        rom = open(os.path.join(rom_dir, "aros-rom.bin"), "rb").read()
        ext = open(os.path.join(rom_dir, "aros-ext.bin"), "rb").read()
        self.mem.w_block(0xF80000, rom)
        self.mem.w_block(0xE00000, ext)
        self.mem.w_block(0, rom[:8])            # OVL: reset vectors from ROM
        self.log = Log()
        self.icount = 0
        self.cycles = 0                          # virtual 68000 cycles
        self.ecl_rem = 0
        self.idle_cycles = 0
        self.regs = [0] * 256                    # custom word registers
        self.intena = self.intreq = self.dmacon = self.adkcon = 0
        self.cop1lc = self.cop2lc = 0
        self.ciaa = CIA("CIAA", self, 3)
        self.ciab = CIA("CIAB", self, 13)
        # CIA-A PRA inputs: /FIR1 /FIR0 up, /RDY high, /TK0 low (track 0),
        # /WPRO high, /CHNG low (no disk); outputs LED/OVL
        self.ciaa.pra_in = 0xEB
        # --adf (docs/WB_BOOT.md): DF0 holds a disk; the drive answers
        # PRA bits 2-5 instead of the fixed "no disk" value.
        self.floppy = Floppy(adf) if adf is not None else None
        self.hle = TrackdiskHLE(self, adf) if td_hle and adf is not None else None
        self.wb = WbTracker(self, adf) if adf is not None else None
        self.io_calls = 0
        self.dsklen_last = 0
        self.serial = bytearray()
        self.irq_level = 0
        self.irq_count = collections.Counter()
        self.ophi = collections.Counter()
        self.priv = collections.Counter()
        self.blits = collections.Counter()
        self.blit_words = 0
        self.frames = 0
        self.timeline = []
        self.page_hits = collections.Counter()
        self.pcpage = collections.Counter()
        self.pcblk = collections.Counter()
        self.win_page = -1
        self.win_changes = 0
        self.win_lru = []
        self.win_miss = {n: 0 for n in self.WIN_SIZES}
        self.mark_icount = 3_333_413            # the no-media frame (boot/)
        self.win_at_mark = None
        self.mem.set_special_range_read_funcs(0xDF0000, 1, self.c_r8, self.c_r16, self.c_r32)
        self.mem.set_special_range_write_funcs(0xDF0000, 1, self.c_w8, self.c_w16, self.c_w32)
        self.mem.set_special_range_read_funcs(0xBF0000, 1, self.cia_r8, self.cia_r16, self.cia_r32)
        self.mem.set_special_range_write_funcs(0xBF0000, 1, self.cia_w8, self.cia_w16, self.cia_w32)
        self.cpu.set_instr_hook_callback(self.on_instr)
        self.cpu.pulse_reset()

    # ---- instruction hook ------------------------------------------------
    def on_trace(self, mode, width, addr, val):
        """--bank-stats: model the proposed bank-change trap on the data
        accesses (opcode/extension fetches, i.e. reads inside the current
        instruction's first 10 bytes, go through the fetch window instead)."""
        addr &= 0xFFFFFF
        if mode == "R" and 0 <= addr - self.cur_pc < 10:
            return
        self.bs_calls += 1
        bank = addr >> 16
        if mode == "W" and (bank >= 0xF8 or 0xE0 <= bank <= 0xE7):
            self.bs_romw += 1                   # write into a ROM image
        if bank in (0xBF, 0xDF):
            self.bs_io += 1                     # trapped; shadow bank unchanged
            return
        if bank != self.bs_last:
            self.bs_switch += 1
            self.bs_last = bank

    cur_pc = 0
    bs_calls = bs_io = bs_switch = bs_romw = 0
    bs_last = -1

    def on_instr(self, pc):
        self.cur_pc = pc & 0xFFFFFF
        self.icount += 1
        self.pcpage[pc >> 12] += 1
        self.pcblk[pc >> 6] += 1
        pg = (pc & 0xFFFFFF) >> 8               # fetch-window model: 256-byte code page
        if pg != self.win_page:
            self.win_changes += 1
            self.win_page = pg
            # WB-measure: refills left with an N-page LRU of code windows
            lru = self.win_lru
            if pg in lru:
                i = lru.index(pg)
                for n in self.WIN_SIZES:
                    if i >= n:
                        self.win_miss[n] += 1
                del lru[i]
            else:
                for n in self.WIN_SIZES:
                    self.win_miss[n] += 1
                if len(lru) >= self.WIN_SIZES[-1]:
                    lru.pop()
            lru.insert(0, pg)
        if self.icount == self.mark_icount:
            self.win_at_mark = self.win_changes
        if self.wb is not None:
            if self.hle is not None and pc == self.hle.beginio and self.hle.trap():
                return
            if self.wb.bb_pending and pc < 0xE00000:
                self.wb.check_bootblock(pc)
        op = self.mem.r16(pc & 0xFFFFFE)
        self.ophi[op >> 8] += 1
        if op >> 12 in (0x0, 0x4, 0xA, 0xF):
            for name, f in PRIV_CLASSES:
                if f(op):
                    self.priv[name] += 1
                    if name not in self.first_priv:
                        self.first_priv[name] = self.icount
                    break

    first_priv: dict = {}

    def find_node(self, list_off, name):
        return self.wb.find_node(list_off, name) if self.wb else None

    # ---- interrupts ------------------------------------------------------
    def set_intreq_bit(self, bit):
        self.intreq |= 1 << bit
        self.update_irq()

    def update_irq(self):
        lvl = 0
        if self.intena & 0x4000:
            act = self.intena & self.intreq & 0x3FFF
            if "ports_irq_off" in self.ablate:
                act &= ~(1 << 3)                # CIA-A never reaches the CPU
            if "exter_irq_off" in self.ablate:
                act &= ~(1 << 13)               # CIA-B never reaches the CPU
            if "softint_off" in self.ablate:
                act &= ~(1 << 2)
            if "blit_irq_off" in self.ablate:
                act &= ~(1 << 6)
            for b, l in ((13, 6), (12, 5), (11, 5), (10, 4), (9, 4), (8, 4), (7, 4),
                         (6, 3), (5, 3), (4, 3), (3, 2), (2, 1), (1, 1), (0, 1)):
                if act & (1 << b):
                    lvl = l
                    break
        if "no_irq" in self.ablate:
            lvl = 0
        if lvl != self.irq_level:
            if lvl > self.irq_level:
                self.irq_count[lvl] += 1
            self.irq_level = lvl
            self.cpu.set_irq(lvl)

    # ---- beam ------------------------------------------------------------
    def beam(self):
        if "beam_frozen" in self.ablate:
            return 0, 0
        c = self.cycles % FRAME_CYC
        return c // LINE_CYC, (c % LINE_CYC) // 2

    # ---- custom chips ------------------------------------------------------
    def c_r16(self, addr):
        off = addr & 0xFFFF
        if off < 0xF000 or off >= 0xF200:
            self.page_hits[f"$DF{off:04X}(R)"] += 1
            return 0                            # plan §1.1: open bus reads 0
                                                # (an A500 mirrors the regs; AROS never goes here)
        r = off & 0x1FE
        name = CUSTOM_NAMES.get(r, f"${r:03X}")
        self.io_calls += 1
        self.log.hit(("custom", name), "R", self.icount)
        v, h = self.beam()
        if r == 0x002:
            return self.dmacon & 0x03FF
        if r == 0x004:
            return 0x8000 | (v >> 8)            # LOF, Agnus ID 0 (OCS PAL)
        if r == 0x006:
            return ((v & 0xFF) << 8) | (h & 0xFF)
        if r == 0x010:
            return self.adkcon
        if r == 0x016:
            return 0xFF00                       # POTINP: buttons up
        if r == 0x018:
            return 0x3000                       # SERDATR: TBE | TSRE
        if r == 0x01C:
            return self.intena
        if r == 0x01E:
            return self.intreq
        if r == 0x07C:
            return 0xFFFF                       # DENISEID: OCS has none
        return 0

    def c_r8(self, addr):
        w = self.c_r16(addr & ~1)
        return (w >> 8) if not addr & 1 else w & 0xFF

    def c_r32(self, addr):
        return (self.c_r16(addr) << 16) | self.c_r16(addr + 2)

    def c_w8(self, addr, val):
        # byte writes to custom registers write the byte to both halves
        self.c_w16(addr & ~1, (val << 8) | val)

    def c_w32(self, addr, val):
        self.c_w16(addr, val >> 16)
        self.c_w16(addr + 2, val & 0xFFFF)

    def c_w16(self, addr, val, copper=False):
        off = addr & 0xFFFF
        if off < 0xF000 or off >= 0xF200:
            self.page_hits[f"$DF{off:04X}(W)"] += 1
            return
        r = off & 0x1FE
        name = CUSTOM_NAMES.get(r, f"${r:03X}")
        if not copper:
            self.io_calls += 1
        self.log.hit(("custom", name), "C" if copper else "W", self.icount)
        self.regs[r >> 1] = val
        if r == 0x096:
            self.dmacon = self._setclr(self.dmacon, val)
        elif r == 0x09A:
            self.intena = self._setclr(self.intena, val)
            self.update_irq()
        elif r == 0x09C:
            self.intreq = self._setclr(self.intreq, val)
            self.ciaa.update()
            self.ciab.update()
            self.update_irq()
        elif r == 0x09E:
            self.adkcon = self._setclr(self.adkcon, val)
        elif r == 0x030:
            self.serial.append(val & 0xFF)
            self.set_intreq_bit(0)              # TBE
        elif r == 0x080:
            self.cop1lc = (self.cop1lc & 0xFFFF) | (val << 16)
        elif r == 0x082:
            self.cop1lc = (self.cop1lc & 0xFFFF0000) | val
        elif r == 0x084:
            self.cop2lc = (self.cop2lc & 0xFFFF) | (val << 16)
        elif r == 0x086:
            self.cop2lc = (self.cop2lc & 0xFFFF0000) | val
        elif r == 0x024:
            self.dsklen(val)
        elif r == 0x058:
            self.blit(val)
        elif r == 0x05E:                         # ECS BLTSIZH starts a blit
            self.blit(((self.regs[0x05C >> 1] & 0x3FF) << 6) | (val & 0x3F), ecs=True)

    def dsklen(self, val):
        """Disk DMA, completed at once (docs/WB_BOOT.md): the second DSKLEN
        write with DMAEN set starts it; a read gets the MFM track under the
        head (wb_disk.mfm_track), then INTREQ DSKBLK.  Writes are counted
        and dropped (the disk is write-protected)."""
        start = val & 0x8000 and self.dsklen_last & 0x8000
        self.dsklen_last = val
        if not start or self.floppy is None or not self.floppy.selected:
            return
        if not (self.dmacon & 0x0210 == 0x0210):
            return
        words = val & 0x3FFF
        if val & 0x4000:
            self.floppy.writes += 1
        else:
            ptr = ((self.regs[0x020 >> 1] << 16) | self.regs[0x022 >> 1]) & 0x1FFFFE
            self.mem.w_block(ptr, mfm_track(self.floppy.adf, self.floppy.track, words))
            self.floppy.reads.append((self.icount, self.floppy.track))
            self.wb.on_disk_read(self.floppy.track)
        self.dsklen_last = 0
        self.set_intreq_bit(1)                  # DSKBLK

    @staticmethod
    def _setclr(reg, val):
        return reg | (val & 0x7FFF) if val & 0x8000 else reg & ~val & 0x7FFF

    # ---- CIAs ------------------------------------------------------------
    def cia_r8(self, addr):
        off = addr & 0xFFFF
        reg = (off >> 8) & 0xF
        self.io_calls += 1
        if off & 0x1000 == 0 and addr & 1:              # $BFExx1 odd: CIA-A
            self.log.hit(("CIAA", CIA_REGS[reg]), "R", self.icount)
            if reg == 0 and self.floppy is not None:
                self.ciaa.pra_in = 0xC3 | self.floppy.pra_bits()
            return self.ciaa.read(reg)
        if off & 0x2000 == 0 and not addr & 1:          # $BFDxx0 even: CIA-B
            self.log.hit(("CIAB", CIA_REGS[reg]), "R", self.icount)
            return self.ciab.read(reg)
        self.page_hits[f"$BF{off:04X}(R)"] += 1
        return 0xFF

    def cia_w8(self, addr, val):
        self.io_calls += 1
        off = addr & 0xFFFF
        reg = (off >> 8) & 0xF
        if off & 0x1000 == 0 and addr & 1:
            self.log.hit(("CIAA", CIA_REGS[reg]), "W", self.icount)
            self.ciaa.write(reg, val)
        elif off & 0x2000 == 0 and not addr & 1:
            self.log.hit(("CIAB", CIA_REGS[reg]), "W", self.icount)
            self.ciab.write(reg, val)
            if reg in (1, 3) and self.floppy is not None:   # PRB/DDRB: drive lines
                c = self.ciab
                self.floppy.write_prb((c.prb & c.ddrb) | (~c.ddrb & 0xFF))
        else:
            self.page_hits[f"$BF{off:04X}(W)"] += 1

    def cia_r16(self, addr):
        return (self.cia_r8(addr) << 8) | self.cia_r8(addr + 1)

    def cia_r32(self, addr):
        return (self.cia_r16(addr) << 16) | self.cia_r16(addr + 2)

    def cia_w16(self, addr, val):
        self.cia_w8(addr, val >> 8)
        self.cia_w8(addr + 1, val & 0xFF)

    def cia_w32(self, addr, val):
        self.cia_w16(addr, val >> 16)
        self.cia_w16(addr + 2, val & 0xFFFF)

    # ---- blitter (synchronous) ---------------------------------------------
    def blit(self, size, ecs=False):
        if "no_blitter" in self.ablate:
            self.blits["(ablated)"] += 1
            self.set_intreq_bit(6)
            return
        r = self.regs
        con0, con1 = r[0x040 >> 1], r[0x042 >> 1]
        if ecs:
            h, w = (size >> 6) or 1024, size & 0x3F or 64
        else:
            h, w = (size >> 6) & 0x3FF or 1024, size & 0x3F or 64
        mem = self.mem
        ptr = lambda hi: ((r[hi >> 1] << 16) | r[(hi + 2) >> 1]) & 0x1FFFFE
        if con1 & 1:
            self.blits["line"] += 1
            self.blit_line(h)
            self.set_intreq_bit(6)
            return
        usea, useb, usec, used = (con0 >> 11) & 1, (con0 >> 10) & 1, (con0 >> 9) & 1, (con0 >> 8) & 1
        lf = con0 & 0xFF
        ash, bsh = con0 >> 12, con1 >> 12
        desc = con1 & 2
        fill = con1 & 0x18
        kind = f"{'A' if usea else ''}{'B' if useb else ''}{'C' if usec else ''}{'D' if used else ''}" \
               f" lf={lf:02X}{' desc' if desc else ''}{' fill' if fill else ''}" \
               f"{' shift' if ash or bsh else ''}"
        self.blits[kind] += 1
        self.blit_words += w * h
        apt, bpt, cpt, dpt = ptr(0x050), ptr(0x04C), ptr(0x048), ptr(0x054)
        s16 = lambda v: v - 0x10000 if v & 0x8000 else v
        amod, bmod, cmod, dmod = (s16(r[x >> 1]) for x in (0x064, 0x062, 0x060, 0x066))
        step = -2 if desc else 2
        if desc:
            amod, bmod, cmod, dmod = -amod, -bmod, -cmod, -dmod
        afwm, alwm = r[0x044 >> 1], r[0x046 >> 1]
        adat, bdat, cdat = r[0x074 >> 1], r[0x072 >> 1], r[0x070 >> 1]
        aold = bold = 0
        for _y in range(h):
            fc = (con1 >> 2) & 1
            for x in range(w):
                if usea:
                    adat = mem.r16(apt)
                    apt += step
                if useb:
                    bdat = mem.r16(bpt)
                    bpt += step
                if usec:
                    cdat = mem.r16(cpt)
                    cpt += step
                a = adat
                if x == 0:
                    a &= afwm
                if x == w - 1:
                    a &= alwm
                if desc:
                    ash_v = ((a << 16 | aold) >> (16 - ash)) & 0xFFFF if ash else a
                    bsh_v = ((bdat << 16 | bold) >> (16 - bsh)) & 0xFFFF if bsh else bdat
                else:
                    ash_v = ((aold << 16 | a) >> ash) & 0xFFFF
                    bsh_v = ((bold << 16 | bdat) >> bsh) & 0xFFFF
                aold, bold = a, bdat
                d = 0
                na, nb, nc = ~ash_v & 0xFFFF, ~bsh_v & 0xFFFF, ~cdat & 0xFFFF
                if lf & 0x80: d |= ash_v & bsh_v & cdat
                if lf & 0x40: d |= ash_v & bsh_v & nc
                if lf & 0x20: d |= ash_v & nb & cdat
                if lf & 0x10: d |= ash_v & nb & nc
                if lf & 0x08: d |= na & bsh_v & cdat
                if lf & 0x04: d |= na & bsh_v & nc
                if lf & 0x02: d |= na & nb & cdat
                if lf & 0x01: d |= na & nb & nc
                if fill:                         # right to left, carry per row
                    out = 0
                    for bit in range(16):
                        s = (d >> bit) & 1
                        if con1 & 0x08:          # IFE: edges drawn
                            o = fc | s
                            fc ^= s
                        else:                    # EFE: closing edge not drawn
                            fc ^= s
                            o = fc
                        out |= o << bit
                    d = out
                if used:
                    mem.w16(dpt, d)
                    dpt += step
            apt += amod if usea else 0
            bpt += bmod if useb else 0
            cpt += cmod if usec else 0
            dpt += dmod if used else 0
        for reg, p in ((0x050, apt), (0x04C, bpt), (0x048, cpt), (0x054, dpt)):
            r[reg >> 1], r[(reg + 2) >> 1] = (p >> 16) & 0x1F, p & 0xFFFF
        self.set_intreq_bit(6)

    def blit_line(self, h):
        """Blitter line mode (HRM algorithm, single-pixel lines drawn with
        the minterm against C; enough to count and approximate)."""
        r = self.regs
        con0, con1 = r[0x040 >> 1], r[0x042 >> 1]
        cpt = ((r[0x048 >> 1] << 16) | r[0x04A >> 1]) & 0x1FFFFE
        apt = ((r[0x050 >> 1] << 16) | r[0x052 >> 1])
        apt = apt - 0x100000000 if apt & 0x80000000 else apt
        amod = r[0x064 >> 1] - (0x10000 if r[0x064 >> 1] & 0x8000 else 0)
        bmod = r[0x062 >> 1] - (0x10000 if r[0x062 >> 1] & 0x8000 else 0)
        cmod = r[0x060 >> 1] - (0x10000 if r[0x060 >> 1] & 0x8000 else 0)
        ash = con0 >> 12
        octant = (con1 >> 2) & 7
        sud, sul, aul = (con1 >> 4) & 1, (con1 >> 3) & 1, (con1 >> 2) & 1
        texture, bsh = r[0x072 >> 1], con1 >> 12
        lf = con0 & 0xFF
        err = apt
        mem = self.mem
        for _ in range(h):
            bit = 0x8000 >> ash
            c = mem.r16(cpt)
            a = bit
            b = 0xFFFF if (texture >> (15 - bsh)) & 1 else 0
            d = 0
            na, nb, nc = ~a & 0xFFFF, ~b & 0xFFFF, ~c & 0xFFFF
            for mt, t in ((0x80, a & b & c), (0x40, a & b & nc), (0x20, a & nb & c),
                          (0x10, a & nb & nc), (0x08, na & b & c), (0x04, na & b & nc),
                          (0x02, na & nb & c), (0x01, na & nb & nc)):
                if lf & mt:
                    d |= t
            mem.w16(cpt, d)
            bsh = (bsh + 1) & 15
            # step: major axis every pixel, minor when error >= 0
            def move(dx_major):
                nonlocal ash, cpt
                if dx_major == "x+":
                    ash += 1
                    if ash == 16:
                        ash = 0
                        cpt += 2
                elif dx_major == "x-":
                    ash -= 1
                    if ash < 0:
                        ash = 15
                        cpt -= 2
                elif dx_major == "y+":
                    cpt += cmod
                else:
                    cpt -= cmod
            # SUD: major is Y; SUL/AUL give directions
            if sud:
                move("y-" if sul else "y+")
                if err >= 0:
                    move("x-" if aul else "x+")
            else:
                move("x-" if sul else "x+")
                if err >= 0:
                    move("y-" if aul else "y+")
            err += amod if err >= 0 else bmod

    # ---- copper (once per frame, MOVEs only) -----------------------------
    def copper(self):
        if "no_copper" in self.ablate:
            return 0
        if not (self.dmacon & 0x0200 and self.dmacon & 0x0080):
            return 0
        pc = self.cop1lc & 0x1FFFFE
        n = 0
        while n < 4000:
            w1, w2 = self.mem.r16(pc), self.mem.r16(pc + 2)
            pc += 4
            n += 1
            if w1 & 1 == 0:                     # MOVE
                reg = w1 & 0x1FE
                if reg < 0x040 and not self.regs[0x02E >> 1] & 2:
                    break                       # illegal MOVE -> copper stops
                if reg == 0x088:
                    pc = self.cop1lc & 0x1FFFFE
                    continue
                if reg == 0x08A:
                    pc = self.cop2lc & 0x1FFFFE
                    continue
                self.c_w16(0xDFF000 + reg, w2, copper=True)
            elif w1 == 0xFFFF and w2 == 0xFFFE:
                break
        return n

    # ---- display snapshot --------------------------------------------------
    def snapshot(self, path):
        r = self.regs
        con0 = r[0x100 >> 1]
        planes = (con0 >> 12) & 7
        hires = con0 >> 15
        if planes == 0 or not self.dmacon & 0x0100:
            return None
        ddfs, ddfe = r[0x092 >> 1] & 0xFC, r[0x094 >> 1] & 0xFC
        words = ((ddfe - ddfs) // 4 + 2) if hires else ((ddfe - ddfs) // 8 + 1)
        diws, diwe = r[0x08E >> 1], r[0x090 >> 1]
        vs, ve = diws >> 8, (diwe >> 8) | (0 if diwe & 0x8000 else 0x100)
        height = max(1, min(ve - vs, 600))
        width = words * 16
        mods = [r[0x108 >> 1], r[0x10A >> 1]]
        mods = [mv - 0x10000 if mv & 0x8000 else mv for mv in mods]
        pts = [((r[(0x0E0 + 4 * i) >> 1] << 16) | r[(0x0E2 + 4 * i) >> 1]) & 0x1FFFFE
               for i in range(planes)]
        pal = []
        for i in range(32):
            c = r[(0x180 + 2 * i) >> 1]
            pal.append(((c >> 8 & 15) * 17, (c >> 4 & 15) * 17, (c & 15) * 17))
        rows = []
        for y in range(height):
            data = [self.mem.r_block(pts[p], words * 2) for p in range(planes)]
            row = bytearray()
            for x in range(width):
                idx = 0
                for p in range(planes):
                    idx |= ((data[p][x >> 3] >> (7 - (x & 7))) & 1) << p
                row += bytes(pal[idx])
            rows.append(bytes(row))
            for p in range(planes):
                pts[p] += words * 2 + mods[p & 1]
        write_png(path, width, height, rows)
        return {"planes": planes, "hires": bool(hires), "width": width,
                "height": height, "bplpt": [hex(p) for p in pts]}

    # ---- main loop ---------------------------------------------------------
    def run(self, seconds, snap_every=2.0):
        total = int(seconds * CPU_HZ)
        next_snap = snap_every * CPU_HZ
        line_in_frame = 0
        last_ic = 0
        t0 = time.monotonic()
        while self.cycles < total:
            res = self.mach.execute(LINE_CYC)
            ran = res.cycles if res.cycles > 0 else LINE_CYC
            if self.icount == last_ic:
                self.idle_cycles += ran             # STOPped: no instruction ran
            if self.wb is not None:
                self.wb.slice(self.icount - last_ic)
            last_ic = self.icount
            self.cycles += ran
            e = self.ecl_rem + ran
            if "cia_timers_frozen" not in self.ablate:
                self.ciaa.eclock(e // ECLK_DIV)
                if "ciab_timers_frozen" not in self.ablate:
                    self.ciab.eclock(e // ECLK_DIV)
            self.ecl_rem = e % ECLK_DIV
            if "ciab_tod_frozen" not in self.ablate:
                self.ciab.tod_tick(1)               # CIA-B TOD = HSYNC
            line_in_frame += 1
            if line_in_frame >= LINES:
                line_in_frame = 0
                self.frames += 1
                if "ciaa_tod_frozen" not in self.ablate:
                    self.ciaa.tod_tick(1)           # CIA-A TOD = VSYNC (50 Hz)
                self.copper()
                if "no_vertb" not in self.ablate:
                    self.set_intreq_bit(5)          # VERTB
                if self.wb is not None:
                    self.wb.frame(self.frames)
                    if self.wb.done:
                        break
                if self.frames % 50 == 0:
                    self.timeline.append({"t_s": round(self.cycles / CPU_HZ, 2),
                                          "icount": self.icount,
                                          "idle_s": round(self.idle_cycles / CPU_HZ, 2),
                                          "bplcon0": hex(self.regs[0x100 >> 1]),
                                          "dmacon": hex(self.dmacon),
                                          "wall_s": round(time.monotonic() - t0, 1)})
            if self.cycles >= next_snap:
                next_snap += snap_every * CPU_HZ
                info = self.snapshot(os.path.join(
                    self.out, f"screen_{self.cycles / CPU_HZ:05.1f}s.png"))
                if info:
                    self.timeline.append({"snapshot_t_s": round(self.cycles / CPU_HZ, 1),
                                          "icount": self.icount,
                                          "busy_s": round((self.cycles - self.idle_cycles) / CPU_HZ, 3),
                                          **info})
        return time.monotonic() - t0


def write_png(path, w, h, rows):
    raw = b"".join(b"\0" + r for r in rows)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def wb_report(am, a):
    """--adf: milestones, per-task counts, the command timeline, disk use."""
    wb, fl = am.wb, am.floppy
    return {
        "adf": os.path.basename(a.adf), "volume": am.floppy.adf.volume_name(),
        "td_hle": bool(a.td_hle),
        "markers": wb.markers,
        "end": {"icount": am.icount, "t_s": round(am.cycles / CPU_HZ, 3),
                "idle_s": round(am.idle_cycles / CPU_HZ, 3),
                "code_page_changes": am.win_changes, "blit_words": am.blit_words,
                "io_calls": am.io_calls, "disk_reads": wb.disk_ops,
                "code_window_lru_misses": dict(am.win_miss),
                "tasks": dict(wb.task_counts), "pcblk": dict(am.pcblk)},
        "commands": wb.commands, "tasks_seen": wb.tasks_seen, "screens": wb.screens,
        "disk": {"dma_reads": len(fl.reads), "dma_writes": fl.writes, "steps": fl.steps,
                 "tracks": wb.tracks_read,
                 "hle_served": am.hle.served if am.hle else None,
                 "hle_bytes": am.hle.bytes if am.hle else None,
                 "hle_passed": am.hle.passed if am.hle else None,
                 "hle_beginio": hex(am.hle.beginio) if am.hle and am.hle.beginio else None},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rom-dir")
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--snap-every", type=float, default=2.0)
    ap.add_argument("--ablate", default="", help="comma list of " + ",".join(Amiga.ABLATIONS))
    ap.add_argument("--bank-stats", action="store_true",
                    help="trace data accesses: bank switches for the trap cost model (slower)")
    ap.add_argument("--adf", help="DF0: holds this ADF (read-only; docs/WB_BOOT.md)")
    ap.add_argument("--startup", help="with --adf: replace S/Startup-Sequence with this "
                    "file's text in the model's in-memory copy of the image")
    ap.add_argument("--td-hle", action="store_true",
                    help="with --adf: serve quick trackdisk reads from the ADF (no MFM)")
    ap.add_argument("--stop-idle", type=float, default=3.0,
                    help="with --adf: stop after LoadWB once the CPU is >=70%% idle "
                    "with no disk reads for this many virtual seconds")
    ap.add_argument("--expect", help="file of sha1 prefixes (10 hex) of insert-disk frames; "
                    "reports the first snapshot matching one")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "build", "evidence", "w6-s3plan", "boot"))
    a = ap.parse_args(argv)
    rom_dir = a.rom_dir
    if not rom_dir:
        c = glob.glob(os.path.expanduser("~/.cache/amiga64/aros/*/aros-rom.bin"))
        if not c:
            print("SKIP: no AROS ROM in ~/.cache/amiga64/aros (run tools/aros/fetch.sh)")
            return 0
        rom_dir = os.path.dirname(c[0])
    os.makedirs(a.out, exist_ok=True)
    abl = [x for x in a.ablate.split(",") if x]
    for x in abl:
        if x not in Amiga.ABLATIONS:
            raise SystemExit(f"unknown ablation {x}")
    adf = AdfImage(a.adf) if a.adf else None
    if adf is not None and a.startup:
        adf = adf.patched("S/Startup-Sequence", open(a.startup, "rb").read())
    am = Amiga(rom_dir, a.out, abl, adf=adf, td_hle=a.td_hle)
    if am.wb is not None:
        am.wb.stop_after_idle = a.stop_idle
    if a.bank_stats:
        am.mem.set_trace_func(am.on_trace)
        am.mem.set_trace_mode(1)
    wall = am.run(a.seconds, a.snap_every)
    regs = sorted(am.log.regs.items(), key=lambda kv: kv[1][0])
    reached = None
    if a.expect:
        import hashlib
        want = {l.split()[0] for l in open(a.expect) if l.strip()}
        for t in am.timeline:
            if "snapshot_t_s" in t:
                fn = os.path.join(a.out, f"screen_{t['snapshot_t_s']:05.1f}s.png")
                if os.path.exists(fn) and hashlib.sha1(open(fn, "rb").read()).hexdigest()[:10] in want:
                    reached = {"t_s": t["snapshot_t_s"], "icount": t["icount"]}
                    break
    report = {
        "ablate": abl, "insert_disk_reached": reached,
        "rom_dir": rom_dir,
        "bank_stats": {"data_calls": am.bs_calls, "io_calls": am.bs_io,
                       "bank_switches": am.bs_switch, "rom_writes": am.bs_romw}
        if a.bank_stats else None, "virtual_seconds": a.seconds, "wall_seconds": round(wall, 1),
        "instructions": am.icount, "frames": am.frames,
        "idle_seconds": round(am.idle_cycles / CPU_HZ, 3),
        "busy_seconds": round((am.cycles - am.idle_cycles) / CPU_HZ, 3),
        "irq_deliveries_by_level": dict(sorted(am.irq_count.items())),
        "privileged_or_exception_opcodes": dict(am.priv.most_common()),
        "first_privileged_icount": am.first_priv,
        "blits": dict(am.blits.most_common()), "blit_words": am.blit_words,
        "serial": am.serial.decode("latin-1"),
        "odd_page_hits": dict(am.page_hits.most_common(20)),
        "registers": [{"chip": k[0], "reg": k[1], "first_icount": v[0], "reads": v[1],
                       "cpu_writes": v[2], "copper_writes": v[3]} for k, v in regs],
        "opcode_hi_histogram": {f"{k:02X}": v for k, v in sorted(am.ophi.items())},
        "timeline": am.timeline,
        "pc_4k_pages_top": {f"{k << 12:06X}": v for k, v in am.pcpage.most_common(40)},
        "pc_64b_blocks_top": {f"{k << 6:06X}": v for k, v in am.pcblk.most_common(40)},
        "code_page_changes": {"total": am.win_changes,
                              "at_icount": am.mark_icount,
                              "until_mark": am.win_at_mark},
    }
    if am.wb is not None:
        report["wb"] = wb_report(am, a)
        am.snapshot(os.path.join(a.out, "final.png"))
    with open(os.path.join(a.out, "boot_report.json"), "w") as f:
        json.dump(report, f, indent=1)
    print(json.dumps({k: report[k] for k in ("ablate", "insert_disk_reached", "bank_stats", "instructions", "frames", "idle_seconds",
                                             "busy_seconds", "wall_seconds",
                                             "irq_deliveries_by_level", "blit_words")}))
    print("privileged:", report["privileged_or_exception_opcodes"])
    print("blits:", report["blits"])
    print("registers touched:", len(regs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
