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
outside the repo).  The ROM is read from the bundle
(third_party/aros/), never copied.  Outputs (evidence dir):
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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from adf import AdfImage                                       # noqa: E402
from wb_disk import Floppy, TrackdiskHLE, WbTracker, mfm_track  # noqa: E402
# S3.0: the chipset models live in host/chipset_ref.py (the reference)
from host.chipset_ref import (CIA_REGS, CPU_HZ, CUSTOM_NAMES, ECLK_DIV,  # noqa: E402,F401
                              FRAME_CYC, LINE_CYC, LINES, Chipset)

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


class Amiga(Chipset):
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
        # regs, paula, ciaa/ciab (CIA-A PRA inputs $EB: no disk), blitter,
        # cop (copper), serial, cycles: host/chipset_ref.py
        Chipset.__init__(self, self.mem)
        self.cop.strict = False                  # count copper SKIPs (not modelled)
        self.log = Log()
        self.icount = 0
        self.ecl_rem = 0
        self.idle_cycles = 0
        # --adf (docs/WB_BOOT.md): DF0 holds a disk; the drive answers
        # PRA bits 2-5 instead of the fixed "no disk" value.
        self.floppy = Floppy(adf) if adf is not None else None
        self.hle = TrackdiskHLE(self, adf) if td_hle and adf is not None else None
        self.wb = WbTracker(self, adf) if adf is not None else None
        self.io_calls = 0
        self.dsklen_last = 0
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
        self.request(bit)

    def update_irq(self):
        mask = 0x3FFF
        if "ports_irq_off" in self.ablate:
            mask &= ~(1 << 3)                   # CIA-A never reaches the CPU
        if "exter_irq_off" in self.ablate:
            mask &= ~(1 << 13)                  # CIA-B never reaches the CPU
        if "softint_off" in self.ablate:
            mask &= ~(1 << 2)
        if "blit_irq_off" in self.ablate:
            mask &= ~(1 << 6)
        lvl = self.paula.level(mask)
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
        return Chipset.beam(self)

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
        return self.custom_read(r)              # S3_PLAN §3.4 fixed values

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
        self.custom_write(r, val)

    def dsklen(self, val):
        """Disk DMA, completed at once (docs/WB_BOOT.md): the second DSKLEN
        write with DMAEN set starts it; a read gets the MFM track under the
        head (wb_disk.mfm_track), then INTREQ DSKBLK.  Writes are counted
        and dropped (the disk is write-protected)."""
        start = val & 0x8000 and self.dsklen_last & 0x8000
        self.dsklen_last = val
        if not start or self.floppy is None or not self.floppy.selected:
            return
        if not (self.paula.dmacon & 0x0210 == 0x0210):
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

    # ---- CIAs ------------------------------------------------------------
    def cia_r8(self, addr):
        self.io_calls += 1
        sel = self.cia_select(addr)
        if sel is None:
            self.page_hits[f"$BF{addr & 0xFFFF:04X}(R)"] += 1
            return 0xFF
        which, reg = sel
        if which == "A":                                # $BFExx1 odd: CIA-A
            self.log.hit(("CIAA", CIA_REGS[reg]), "R", self.icount)
            if reg == 0 and self.floppy is not None:
                self.ciaa.pra_in = 0xC3 | self.floppy.pra_bits()
            return self.ciaa.read(reg)
        self.log.hit(("CIAB", CIA_REGS[reg]), "R", self.icount)   # $BFDxx0 even: CIA-B
        return self.ciab.read(reg)

    def cia_w8(self, addr, val):
        self.io_calls += 1
        sel = self.cia_select(addr)
        if sel is None:
            self.page_hits[f"$BF{addr & 0xFFFF:04X}(W)"] += 1
            return
        which, reg = sel
        if which == "A":
            self.log.hit(("CIAA", CIA_REGS[reg]), "W", self.icount)
            self.ciaa.write(reg, val)
        else:
            self.log.hit(("CIAB", CIA_REGS[reg]), "W", self.icount)
            self.ciab.write(reg, val)
            if reg in (1, 3) and self.floppy is not None:   # PRB/DDRB: drive lines
                c = self.ciab
                self.floppy.write_prb((c.prb & c.ddrb) | (~c.ddrb & 0xFF))

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

    # ---- blitter (synchronous; host/chipset_ref.Blitter) --------------------
    def blit(self, size, ecs=False):
        if "no_blitter" in self.ablate:
            self.blits["(ablated)"] += 1
            self.set_intreq_bit(6)
            return
        kind, words = self.blitter.blit(self.regs, size, ecs)
        self.paula.bzero = self.blitter.zero
        self.blits[kind] += 1
        self.blit_words += words
        self.set_intreq_bit(6)

    # ---- copper (once per frame, MOVEs only) -----------------------------
    def copper(self):
        if "no_copper" in self.ablate:
            return 0
        return self.run_copper()

    def copper_write(self, reg, val):
        self.c_w16(0xDFF000 + reg, val, copper=True)

    # ---- display snapshot --------------------------------------------------
    def snapshot(self, path):
        r = self.regs
        con0 = r[0x100 >> 1]
        planes = (con0 >> 12) & 7
        hires = con0 >> 15
        if planes == 0 or not self.paula.dmacon & 0x0100:
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
                                          "dmacon": hex(self.paula.dmacon),
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
        c = glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "third_party", "aros", "aros-rom.bin"))
        if not c:
            print("SKIP: no AROS ROM in third_party/aros (git checkout -- third_party/aros)")
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
    if am.cop.skips:
        report["copper_skips"] = am.cop.skips   # SKIP is not modelled (chipset_ref.Copper)
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
