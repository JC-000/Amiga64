#!/usr/bin/env python3
"""S3.4 guest CIA-A/B: direct-API sweeps and guest legs (VICE).

docs/S3_PLAN.md §1.5 and §4 "S3.4", docs/WAVE9_BRIEF.md C3, contract
docs/CONTRACTS.md "CIA-A/B (S3.4)".  The reference is host/chipset_ref.CIA
(two of them, wired as chipset_ref.Chipset: CIA-A PRA inputs $EB, INTREQ
PORTS = bit 3, CIA-B EXTER = bit 13), advanced with eclock() by the guest
E time the C64 side used, CIA-A's TOD by one tick per VERTB and CIA-B's by
the line grid of chipset_ref.cia_lines.

Legs:
  sweep   random sequences of byte reads/writes through mem_read8 /
          mem_write8 at the CIA addresses (as rec_apisweep), time advances,
          VERTB ticks (mach_cbv[0]), INTREQ writes through the API (SET/CLR,
          then | mach_hold) and a host-played service pass (mach_am/mach_dl,
          one pass per step, slot order), diffed against the model after
          every step: read values, X/Y kept, INTREQR bits 3/13, mach_hold,
          and every slot's armed deadline to the tick.  The model follows
          each chip's own sync time (cia_st CIA_T).  Live PRG: the real
          counter (mach_init at s = 0/2/4), time passes in 6502 delay loops.
          Host-time PRG (--build-shim: CIA_HOSTTIME + tests/cia/shim.s):
          guest time is set by the host, exactly, up to 2^28-tick jumps.
  wrap    the sweep with guest time starting just below 2^32 (host-time
          PRG only): lazy counts, posts and deadlines across the u32 wrap
  probe   the clean-room calibration probe (guest, $BF trap, mach_run):
          CIA-A timer A ticks between two CIA-A TOD ticks -> 50 Hz
          (14188 ticks), s = 8 and 4.  Red: 0 Hz (the TOD never moves)
  tirq    timer interrupts through INTREQ (no CIA access while polling):
          one-shot TA posts PORTS, ICR $81 then $00, CRA stopped; a PORTS
          clear with the ICR still asserting comes straight back (level
          hold), after the ICR read a clear sticks
  todb    CIA-B TOD counts 312.5 lines per frame; a CIA-B TOD alarm 100
          lines ahead posts EXTER through its event (slot 5), ICR $84

    python3 -m tests.run_cia --prg build/X/runner.prg [--legs a,b]
        [--seeds N] [--ops N]
    python3 -m tests.run_cia --prg x --build-shim DIR [--legs sweep,wrap]
    python3 -m tests.run_cia --prg ... --mutants [--only m1,m2]

A PRG without cia.s (the base build) runs the same API sequences with
guest time frozen (the red run).  Exit 0 = every selected leg passed
(--mutants: every mutant failed a leg).
"""

from __future__ import annotations

import argparse
import os
import random
import struct
import subprocess
import sys

from host import chipset_ref
from host.chipset_ref import CIA, CIAA_PRA_IN, cia_line_e, cia_lines, cia_tod_kprime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ZP_M_ADDR, ZP_M_VAL = 0x58, 0x5C
STUB = 0xC700            # buf_case tail
XY_OUT = 0xC7F0
VERBOSE = False

PORTS, EXTER = 3, 13
CASCADE_CAP = 1 << 30     # cia.s caps a cascade deadline at 2^30 E ahead
SLOT_ATA, SLOT_ATB, SLOT_BTA, SLOT_BTB, SLOT_BTOD = 1, 2, 3, 4, 5


def cia_addr(chip: int, reg: int) -> int:
    """chip 0 = CIA-A ($BFE001 + reg << 8), 1 = CIA-B ($BFD000 + reg << 8)."""
    return (0xBFE001 if chip == 0 else 0xBFD000) + (reg << 8)


def s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


# --------------------------------------------------------------------------
# The reference: two chipset_ref.CIA on one guest E clock
# --------------------------------------------------------------------------

def cia_equates() -> dict[str, int]:
    """CIA_* plain-value equates of runtime/ops/cia.s (the cia_st layout)."""
    import re
    out = {}
    for line in open(os.path.join(ROOT, "runtime", "ops", "cia.s")):
        mt = re.match(r"(CIA_[A-Z0-9_]+)\s*=\s*(\$?[0-9A-Fa-f]+)\s*;", line)
        if mt:
            v = mt.group(2)
            out[mt.group(1)] = int(v[1:], 16) if v[0] == "$" else int(v)
    return out


class Model:
    """Two chipset_ref.CIA, each on its own guest E time: cia.s syncs a chip
    only when it is touched, so the model follows each chip's CIA_T."""

    def __init__(self, t0: int):
        self.req = 0
        self.a = CIA(lambda: self._req(PORTS), "CIAA")
        self.b = CIA(lambda: self._req(EXTER), "CIAB")
        self.a.pra_in = CIAA_PRA_IN
        self.t0 = t0                    # E time of cia_reset: line grid 0
        self.ts = [t0, t0]
        self.ph = 0                     # CIA-B line phase (colour clocks)

    @property
    def t(self) -> int:
        return self.ts[0]

    def _req(self, bit: int) -> None:
        self.req |= 1 << bit

    def chip(self, c: int) -> CIA:
        return self.a if c == 0 else self.b

    def advance_chip(self, c: int, t: int) -> None:
        old = self.ts[c]
        n = (t - old) & 0xFFFFFFFF
        if n & 0x80000000:          # cia.s: a negative gap is 0 ticks
            n = 0
        self.ts[c] = t & 0xFFFFFFFF
        self.chip(c).eclock(n)
        if c == 1:                  # the grid keeps its phase across a
            x = chipset_ref.CIA_CC_PER_E * n + self.ph      # re-anchor
            self.ph = x % chipset_ref.CIA_LINE_CC
            self.b.tod_ticks(x // chipset_ref.CIA_LINE_CC)

    def advance(self, t: int) -> None:
        self.advance_chip(0, t)
        self.advance_chip(1, t)

    def vbl(self) -> None:
        self.a.tod_tick(1)

    def arms(self) -> dict[int, int | None]:
        """Wanted slot deadlines (cia.s rearm policy), at each chip's time."""
        out = {}
        for c, (sa, sb) in ((0, (SLOT_ATA, SLOT_ATB)), (1, (SLOT_BTA, SLOT_BTB))):
            x, t = self.chip(c), self.ts[c]
            out[sa] = out[sb] = None
            ta_runs = x.cra & 0x21 == 0x01
            # a timer's own event only while its interrupt can still rise:
            # mask bit set and ICR data bit still clear (round-1 F1)
            if ta_runs and x.icr_mask & 1 and not x.icr_data & 1:
                out[sa] = (t + x.ta + 1) & 0xFFFFFFFF
            if not (x.crb & 1 and x.icr_mask & 2 and not x.icr_data & 2):
                continue
            if x.crb & 0x60 == 0:                       # TB counts E ticks
                out[sb] = (t + x.tb + 1) & 0xFFFFFFFF
            elif x.crb & 0x40 and ta_runs:              # TB counts TA underflows
                if x.cra & 0x08:                        # one-shot TA: one pulse
                    d = x.ta + 1 if x.tb == 0 else None
                else:                                   # TB's own underflow
                    d = x.ta + 1 + x.tb * (x.ta_latch + 1)
                if d is not None:
                    out[sb] = (t + min(d, CASCADE_CAP)) & 0xFFFFFFFF
        b = self.b
        if not b.tod_halt and b.icr_mask & 4:
            k = cia_tod_kprime(b.tod, b.alarm)
            out[SLOT_BTOD] = (self.ts[1] + -(-(chipset_ref.CIA_LINE_CC * k - self.ph)
                                             // chipset_ref.CIA_CC_PER_E)) & 0xFFFFFFFF
        else:
            out[SLOT_BTOD] = None
        return out


# --------------------------------------------------------------------------
# VICE rig
# --------------------------------------------------------------------------

class Rig:
    def __init__(self, target, labels):
        from tests.harness_util import lookup_label
        self.tg, self.t, self.labels = target, target.transport, labels
        self.lab = lambda n: lookup_label(labels, n)
        self.has = {}
        for n in ("cia_read", "cia_reset", "cia_now", "shim_t", "mach_on",
                  "chip_reset"):
            try:
                self.has[n] = self.lab(n)
            except KeyError:
                pass
        self.cia = "cia_read" in self.has
        self.shim = "shim_t" in self.has
        self._tramp = None
        self._mr = None
        self.prg = ""
        self.probe: list[tuple] = []

    def fresh(self) -> None:
        """Power-up CIAs before a guest run (chip_reset -> cia_reset): no
        state carries over from the previous leg (a halted TOD, a mask)."""
        self.call(self.lab("chip_reset"))

    def mach_rig(self):
        """tests.run_mach.Rig on this target (driver built against us)."""
        if self._mr is None:
            from tests.run_mach import Rig as MachRig
            self._mr = MachRig(self.tg, self.labels, self.prg)
        return self._mr

    def call(self, addr: int, timeout: float = 60.0) -> None:
        from tests.harness_util import call_runner
        call_runner(self.tg, addr, timeout)

    def w(self, addr: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, addr, data)

    def r(self, addr: int, n: int) -> bytes:
        from c64_test_harness import read_bytes
        return read_bytes(self.t, addr, n)

    def api(self, target: int, a: int, v: int | None = None):
        """mem_* call through a trampoline that loads X/Y sentinels."""
        if self._tramp != target:
            self.w(STUB, bytes([0xA2, 0x5A, 0xA0, 0xA5, 0x20, target & 0xFF,
                                target >> 8, 0x8E, XY_OUT & 0xFF, XY_OUT >> 8,
                                0x8C, (XY_OUT + 1) & 0xFF, (XY_OUT + 1) >> 8,
                                0x60]))
            self._tramp = target
        self.w(ZP_M_ADDR, struct.pack("<I", a | 0xAB000000)
               + struct.pack("<I", 0xDEADBE00 if v is None else v))
        self.call(STUB)
        val = struct.unpack("<I", self.r(ZP_M_VAL, 4))[0]
        return val, self.r(XY_OUT, 2)


# --------------------------------------------------------------------------
# sweep: random API sequences vs the model
# --------------------------------------------------------------------------

def gen_macro(rng: random.Random, m: Model) -> list[tuple]:
    """A targeted sequence: program a timer, or point a TOD alarm just
    ahead of the counter (needs the model's TOD), then let time run."""
    c = rng.randrange(2)
    cia = m.chip(c)
    k = rng.random()
    big = rng.choice([rng.randrange(1 << 17, 1 << 20), rng.randrange(1 << 20, 1 << 24)])
    if k < 0.08:                    # round-1 F2: div with a divisor > 32768
        latch = rng.choice([rng.randrange(0x8000, 0xFFFF), 0x8000, 0xFFFE, 0xFFFF])
        return [("wr", c, 4, latch & 0xFF), ("wr", c, 5, latch >> 8),
                ("wr", c, 14, 0x11), ("time", big), ("rd", c, 4), ("rd", c, 5),
                ("time", rng.randrange(1, 3 * latch)), ("rd", c, 5), ("rd", c, 4),
                ("wr", c, 14, 0x00)]
    if k < 0.14:                    # round-1 F2: span 65536, TB cascade
        tal = rng.choice([0xFFFF, 0, 1, rng.randrange(0x8000, 0x10000)])
        return [("wr", c, 4, tal & 0xFF), ("wr", c, 5, tal >> 8),
                ("wr", c, 6, 0xFF), ("wr", c, 7, 0xFF),
                ("wr", c, 15, rng.choice([0x51, 0x41, 0x59])), ("wr", c, 14, 0x11),
                ("wr", c, 13, 0x80 | rng.choice([2, 3, 0])),
                ("time", big), ("rd", c, 6), ("rd", c, 7), ("rd", c, 13),
                ("time", big), ("rd", c, 7), ("rd", c, 6), ("rd", c, 13),
                ("wr", c, 14, 0x00), ("wr", c, 15, 0x00)]
    if k < 0.6:
        tb = rng.random() < 0.4
        lo, hi, cr = (6, 7, 15) if tb else (4, 5, 14)
        latch = rng.choice([0, 1, 2, 3, rng.randrange(40), rng.randrange(400),
                            rng.randrange(5000)])
        mode = rng.choice([0x01, 0x09, 0x11, 0x19, 0x08])
        if tb:
            mode |= rng.choice([0, 0, 0x40, 0x60])
        ops = [("wr", c, lo, latch & 0xFF)]
        if mode & 0x08 and rng.random() < 0.5:
            ops += [("wr", c, cr, mode & ~1 & 0xFF), ("wr", c, hi, latch >> 8)]
        else:
            ops += [("wr", c, hi, latch >> 8), ("wr", c, cr, mode)]
        if tb and mode & 0x40:      # TA feeds TB: keep TA running
            ops += [("wr", c, 4, rng.randrange(1, 30)), ("wr", c, 5, 0),
                    ("wr", c, 14, 0x11)]
        ops.append(("wr", c, 13, 0x80 | (2 if tb else 1) | rng.choice([0, 1, 2])))
        for _ in range(rng.randrange(1, 6)):
            ops.append(("time", rng.choice([1, rng.randrange(1, 2 * latch + 3),
                                             rng.randrange(1, 300)])))
            ops.append(("rd", c, rng.choice([lo, hi, 13, lo])))
        return ops
    if k < 0.75:                    # TODHI read latches until TODLO
        tick = ("vbl",) if c == 0 else ("time", rng.choice([46, 227, 3000, 70000]))
        return [("rd", c, 10), tick, ("rd", c, 9), tick, ("rd", c, 8),
                ("rd", c, 9), ("rd", c, 8)]
    ahead = rng.choice([1, 2, rng.randrange(1, 20)])
    al = (cia.tod + ahead) & 0xFFFFFF
    crb = cia.crb
    ops = [("wr", c, 15, crb | 0x80), ("wr", c, 10, al >> 16),
           ("wr", c, 9, (al >> 8) & 0xFF), ("wr", c, 8, al & 0xFF),
           ("wr", c, 15, crb & 0x7F), ("wr", c, 13, 0x84)]
    for _ in range(ahead + rng.randrange(3)):
        ops.append(("vbl",) if c == 0 else ("time", rng.choice([46, 45, 100, 227])))
    ops += [("rd", c, 13), ("rd", c, 10), ("rd", c, 9), ("rd", c, 8)]
    return ops


def gen_op(rng: random.Random) -> tuple:
    k = rng.random()
    if k < 0.22:
        dt = rng.choice([0, 1, 2, rng.randrange(1, 60), rng.randrange(1, 3000),
                         rng.randrange(1, 80000), rng.randrange(1, 1 << 22)]
                        + ([rng.randrange(1, 1 << 28)] if rng.random() < 0.05 else []))
        return ("time", dt)
    if k < 0.27:
        return ("vbl",)
    if k < 0.30:
        return ("intreq",)
    chip = rng.randrange(2)
    if k < 0.62:
        reg = rng.choice([4, 5, 6, 7, 8, 9, 10, 13, 13, 13, 14, 15, 0, 1, 2, 3, 11, 12])
        return ("rd", chip, reg)
    reg = rng.choice([4, 5, 6, 7, 14, 15, 14, 15, 13, 13, 8, 9, 10, 0, 1, 2, 3, 11, 12])
    if reg in (4, 6):
        v = rng.choice([0, 1, 2, 5, rng.randrange(256)])
    elif reg in (5, 7):
        v = rng.choice([0, 0, 0, 1, rng.randrange(256)])
    elif reg in (14, 15):
        v = rng.choice([0x00, 0x01, 0x11, 0x09, 0x19, 0x08, 0x10, 0x21])
        if reg == 15:
            v |= rng.choice([0, 0, 0x40, 0x60, 0x20]) | rng.choice([0, 0x80])
        v |= rng.choice([0, 0, 0x02, 0x04, 0x06])
    elif reg == 13:
        v = rng.choice([0x80, 0]) | rng.randrange(32) | rng.choice([0, 0x60])
    elif reg in (8, 9, 10):
        v = rng.randrange(256)
    else:
        v = rng.randrange(256)
    return ("wr", chip, reg, v)


TIME_STUB = 0xC680       # 6502 helpers: mach_etime, delay loop
DELAY_STUB = 0xC690


def sweep(rig: Rig, seed: int, nops: int, start: int | None = None,
          macro: float = 0.15, stats: dict | None = None,
          script: list[tuple] | None = None) -> list[str]:
    """One random API sequence.  Host-time PRG (rig.shim): guest E time is
    shim_t, set by the host, stepped exactly.  Live PRG: the real counter
    (mach_init at a random s); time passes in 6502 delay loops and the
    model follows cia_now, the E time each sync used.  Both: the host plays
    the service pass (due slots of mach_am/mach_dl, in slot order, each
    disarmed before its callback), VERTB ticks call mach_cbv[0], and INTREQ
    writes go through the memory API (chip.s: SET/CLR, then | mach_hold)."""
    fails: list[str] = []
    rng = random.Random(seed)
    rd8, wr8, wr16 = rig.lab("mem_read8"), rig.lab("mem_write8"), rig.lab("mem_write16")
    now_lab, shd = rig.lab("cia_now"), rig.lab("chip_shd")
    am_lab, dl_lab = rig.lab("mach_am"), rig.lab("mach_dl")
    t = 0
    if rig.shim:
        t = rng.choice([0, 0xFFFFF000, rng.randrange(1 << 32)]) if start is None else start
        rig.w(rig.has["shim_t"], struct.pack("<I", t))
        rig.w(rig.has["mach_on"], b"\x01")
    else:
        s = rng.choice([0, 2, 4])
        rig.w(rig.lab("mach_s"), bytes([s]))
        rig.call(rig.lab("mach_init"))
        et = rig.lab("mach_etime")
        rig.w(TIME_STUB, bytes([0x20, et & 0xFF, et >> 8, 0x60]))
    rig.call(rig.lab("chip_reset"))         # also cia_reset: grid anchor now
    rig.w(am_lab, b"\0")
    rig.w(shd + 0x1E, b"\0\0")
    t0 = struct.unpack("<I", rig.r(now_lab, 4))[0]
    if rig.shim and t0 != t:
        fails.append(f"sweep seed {seed}: cia_now ${t0:08X} after reset, want ${t:08X}")
    m = Model(t0)
    cbv = struct.unpack("<8H", rig.r(rig.lab("mach_cbv"), 16))
    tag0 = f"sweep seed {seed}"
    st = {"reads": 0, "icr_set": 0, "tmr_moved": 0, "armed": 0, "posted": 0,
          "fired": 0, "tod_alarm": 0, "wraps": 0}

    eq = cia_equates()
    st_lab = rig.lab("cia_st")

    def follow() -> None:
        """Each model chip := the E time of that chip's last sync (CIA_T)."""
        raw = rig.r(st_lab, 2 * eq["CIA_SZ"])
        for c in (0, 1):
            o = c * eq["CIA_SZ"] + eq["CIA_T"]
            m.advance_chip(c, struct.unpack_from("<I", raw, o)[0])

    def etime() -> int:
        if rig.shim:
            return t
        rig.call(TIME_STUB)
        return struct.unpack("<I", rig.r(rig.lab("mach_et"), 4))[0]

    def service() -> None:
        """One pass, as mach_svc: one E snapshot, every slot due by it fired
        once, in slot order (disarmed, then its callback)."""
        now = etime()
        for k in range(1, 8):
            am = rig.r(am_lab, 1)[0]
            dl = struct.unpack("<I", rig.r(dl_lab + 4 * k, 4))[0]
            if am >> k & 1 and s32(dl - now) <= 0:
                st["fired"] += 1
                rig.w(am_lab, bytes([am & ~(1 << k) & 0xFF]))
                rig.call(cbv[k])
                follow()

    def check(i: int, op: tuple) -> None:
        tag = f"{tag0} op {i} {op}"
        req = struct.unpack(">H", rig.r(shd + 0x1E, 2))[0] & 0x2008
        want = m.req & 0x2008
        hold = struct.unpack("<H", rig.r(rig.lab("mach_hold"), 2))[0] & 0x2008
        whold = ((m.a.icr_data & m.a.icr_mask & 0x1F and 0x0008 or 0)
                 | (m.b.icr_data & m.b.icr_mask & 0x1F and 0x2000 or 0))
        st["posted"] += bool(req)
        if req != want:
            fails.append(f"{tag}: INTREQR & $2008 = ${req:04X} want ${want:04X}")
        if hold != whold:
            fails.append(f"{tag}: mach_hold & $2008 = ${hold:04X} want ${whold:04X}")
        am = rig.r(am_lab, 1)[0]
        dl = struct.unpack("<8I", rig.r(dl_lab, 32))
        for k, w in m.arms().items():
            got = dl[k] if am >> k & 1 else None
            st["armed"] += got is not None
            if got != w:
                fails.append(f"{tag}: slot {k} armed "
                             f"{'-' if got is None else f'${got:08X}'} want "
                             f"{'-' if w is None else f'${w:08X}'} (chip times "
                             f"${m.ts[0]:08X}/${m.ts[1]:08X})")

    queue: list[tuple] = list(script or [])
    if script:
        nops = len(script)
    for i in range(nops):
        if not queue:
            if rng.random() < macro:
                queue = gen_macro(rng, m)
            else:
                queue = [gen_op(rng)]
        op = queue.pop(0)
        if rig.shim and not script and rng.random() < 0.02:    # F3: time backwards
            op = ("back", rng.choice([1, rng.randrange(1, 5000), rng.randrange(1, 1 << 30)]))
        if op[0] == "back":
            t = (t - op[1]) & 0xFFFFFFFF
            rig.w(rig.has["shim_t"], struct.pack("<I", t))
        elif op[0] == "time":
            if rig.shim:
                st["wraps"] += t + op[1] > 0xFFFFFFFF
                t = (t + op[1]) & 0xFFFFFFFF
                rig.w(rig.has["shim_t"], struct.pack("<I", t))
            else:                           # ~5 phi2 per inner iteration
                n = min(max(op[1] // 5, 1), 254 * 256)
                rig.w(DELAY_STUB, bytes([0xA2, (n >> 8) + 1, 0xA0, n & 0xFF,
                                         0x88, 0xD0, 0xFD, 0xCA, 0xD0, 0xFA,
                                         0x60]))
                rig.call(DELAY_STUB)
        elif op[0] == "vbl":
            m.vbl()
            rig.call(cbv[0])
            follow()
        elif op[0] == "intreq":
            v = rng.choice([0x2008, 0x2008, 0x0008, 0x2000, 0x8008, 0xA000])
            m.req = chipset_ref.setclr(m.req, v)
            m.a.update()
            m.b.update()
            rig.api(wr16, 0xDFF09C, v)
        elif op[0] == "rd":
            _, c, reg = op
            got, xy = rig.api(rd8, cia_addr(c, reg))
            follow()
            want = m.chip(c).read(reg)
            st["reads"] += 1
            st["icr_set"] += reg == 13 and want & 0x1F != 0
            st["tod_alarm"] += reg == 13 and want & 4 != 0
            st["tmr_moved"] += reg in (4, 6) and want not in (0xFF,)
            if got != (0xDEADBE00 | want):
                fails.append(f"{tag0} op {i} read {'AB'[c]} reg {reg}: "
                             f"${got & 0xFF:02X} want ${want:02X} (m_val ${got:08X})")
            if xy != b"\x5A\xA5":
                fails.append(f"{tag0} op {i}: X/Y {xy.hex()} not kept")
        else:
            _, c, reg, v = op
            _, xy = rig.api(wr8, cia_addr(c, reg), v)
            follow()
            m.chip(c).write(reg, v)
            if reg == 13:
                m.chip(c).update()
            if xy != b"\x5A\xA5":
                fails.append(f"{tag0} op {i}: X/Y {xy.hex()} not kept")
        service()
        check(i, op)
        if len(fails) > 60:
            fails.append(f"{tag0}: stopped after op {i}")
            break
    print(f"    {tag0}: {nops} ops, coverage {st}", flush=True)
    if stats is not None:
        stats.update(st)
    return fails


def leg_wrap(rig: Rig, seeds: int, nops: int) -> list[str]:
    """Lazy counts across the u32 E-time wrap (ruling R3): guest time starts
    a few hundred to a few thousand ticks below 2^32, timers and the CIA-B
    TOD alarm are programmed (macro-heavy), and time runs through the wrap
    with every read, post and deadline checked.  Shim time only."""
    if not rig.shim:
        return ["wrap: needs host-set E time (shim PRG); the real seam's "
                "counter cannot be preset"] if rig.cia else \
               ["wrap: no cia_read label (no runtime/ops/cia.s)"]
    fails = []
    for seed in range(seeds):
        start = (1 << 32) - random.Random(seed).choice([200, 900, 3000])
        f, st = [], {}
        f = sweep(rig, 0x3A90 + seed, min(nops, 250), start, macro=0.5, stats=st)
        if not f and st.get("wraps", 0) < 1:
            f.append(f"wrap seed {seed}: time never crossed 2^32")
        fails += f
    return fails


def st_field(rig: Rig, c: int, name: str, n: int = 1) -> int:
    eq = cia_equates()
    a = rig.lab("cia_st") + c * eq["CIA_SZ"] + eq[name]
    return int.from_bytes(rig.r(a, n), "little")


def leg_neggap(rig: Rig, *_a) -> list[str]:
    """A negative E-time gap (a later mach_init restarts the counter) is 0
    ticks plus a re-anchor (round-1 F3).  Host time only."""
    if not rig.shim:
        return ["neggap: needs the host-time PRG"]
    f: list[str] = []
    rd8, wr8 = rig.lab("mem_read8"), rig.lab("mem_write8")
    st = rig.has["shim_t"]

    def at(t):
        rig.w(st, struct.pack("<I", t & 0xFFFFFFFF))

    def ta(c):
        lo = rig.api(rd8, cia_addr(c, 4))[0] & 0xFF
        return (rig.api(rd8, cia_addr(c, 5))[0] & 0xFF) << 8 | lo

    rig.w(rig.has["mach_on"], b"\x01")
    for c in (0, 1):
        at(0x40000000)
        rig.call(rig.lab("chip_reset"))
        for reg, v in ((4, 0xE8), (5, 0x03), (14, 0x11)):    # 1000, continuous
            rig.api(wr8, cia_addr(c, reg), v)
        at(0x40000000 + 100)
        v1 = ta(c)                                          # 1000 - 100
        tod1 = rig.api(rd8, cia_addr(c, 8))[0] & 0xFF
        at(0x40000000 - 7777)                               # backwards
        v2 = ta(c)
        t2 = st_field(rig, c, "CIA_T", 4)
        at(0x40000000 - 7777 + 10)
        v3 = ta(c)
        tag = f"neggap {'AB'[c]}"
        if (v1, v2, v3) != (900, 900, 890):
            f.append(f"{tag}: TA {v1}, {v2} (after going back), {v3}; want 900, 900, 890")
        if t2 != 0x40000000 - 7777:
            f.append(f"{tag}: CIA_T ${t2:08X} after the negative gap, want re-anchored "
                     f"${0x40000000 - 7777:08X}")
        if c == 1:
            tod2 = rig.api(rd8, cia_addr(c, 8))[0] & 0xFF
            if tod2 != tod1:
                f.append(f"{tag}: TOD-B {tod1} -> {tod2} over 10 E ticks after the gap")
    return f


def leg_resync(rig: Rig, *_a) -> list[str]:
    """256 VERTB hooks sync both chips (round-1 F3).  Host time only."""
    if not rig.shim:
        return ["resync: needs the host-time PRG"]
    f: list[str] = []
    st = rig.has["shim_t"]
    rig.w(rig.has["mach_on"], b"\x01")
    rig.w(st, struct.pack("<I", 5000))
    rig.call(rig.lab("chip_reset"))
    rig.w(st, struct.pack("<I", 5000 + 12345))
    cb = struct.unpack("<H", rig.r(rig.lab("mach_cbv"), 2))[0]
    rig.w(STUB, bytes([0xA9, 0, 0x8D, XY_OUT & 0xFF, XY_OUT >> 8,           # n = 0
                       0x20, cb & 0xFF, cb >> 8,                           # jsr hook
                       0xEE, XY_OUT & 0xFF, XY_OUT >> 8,                   # inc n
                       0xD0, 0xF8, 0x60]))                                 # 256 x
    rig._tramp = None
    rig.call(STUB)
    for c in (0, 1):
        t = st_field(rig, c, "CIA_T", 4)
        if t != 5000 + 12345:
            f.append(f"resync: chip {'AB'[c]} CIA_T ${t:08X} after 256 VERTBs, "
                     f"want ${5000 + 12345:08X}")
    tod = st_field(rig, 0, "CIA_TOD", 3)
    if tod != 256:
        f.append(f"resync: CIA-A TOD {tod} after 256 VERTBs, want 256")
    return f


COST_CASES = {               # TAL, CRA, CRB, ICR (round-1 F1, the reviewer's)
    "ta20_mskTA":        (0x0014, 0x11, 0x00, 0x81),
    "ta0_mskTA":         (0x0000, 0x11, 0x00, 0x81),
    "ta0_cascade_mskTB": (0x0000, 0x11, 0x51, 0x82),
    "ta7_cascade_mskTB": (0x0007, 0x11, 0x51, 0x82),
    "ta63_cascade_mskTB": (0x003F, 0x11, 0x51, 0x82),
    "ta255_cascade_mskTB": (0x00FF, 0x11, 0x51, 0x82),
}
COST_N = 3000


def prog_cost(tal: int, cra: int, crb: int, icr: int) -> list[int]:
    """Program CIA-A, then a register-only dbra loop of COST_N; HALT."""
    from tests.run_mach import G, HALT, mv_imm_d_w
    g = G()
    mvb_imm(g, 0x7F, ca(0, 13))
    mvb_imm(g, tal & 0xFF, ca(0, 4))
    mvb_imm(g, tal >> 8, ca(0, 5))
    mvb_imm(g, 0xFF, ca(0, 6))
    mvb_imm(g, 0xFF, ca(0, 7))
    mvb_imm(g, cra, ca(0, 14))
    mvb_imm(g, crb, ca(0, 15))
    mvb_imm(g, icr, ca(0, 13))
    mv_imm_d_w(g, COST_N, 7)
    g.label("L")
    g.dbra(7, "L")
    mvb_imm(g, 0x00, ca(0, 14))
    mvb_imm(g, 0x00, ca(0, 15))
    mvb_imm(g, 0x7F, ca(0, 13))
    g(HALT)
    return g.words()


def leg_cost(rig, *_a) -> list[str]:
    """A fast timer must not starve the guest (round-1 F1): the same loop
    with an interrupt-enabled fast timer (ICR never read) or a fast TA
    cascaded into TB takes at most 2x the frames of the idle loop (+1).
    Red: no HALT (a pass per underflow is longer than the period)."""
    from tests.run_mach import clean
    f: list[str] = []
    mr = rig.mach_rig()
    progs = [prog_cost(0, 0, 0, 0)] + [prog_cost(*v) for v in COST_CASES.values()]
    pcs = mr.stage(*progs)
    rig.fresh()
    idle = mr.run(pcs[0], s=4, budget=50_000_000)
    if not clean(idle, "cost idle", f):
        return f
    base = idle["frames"]
    print(f"    cost idle: {base} frames")
    for (name, _), pc in zip(COST_CASES.items(), pcs[1:]):
        rig.fresh()
        try:
            res = mr.run(pc, s=4, budget=50_000_000)
        except Exception as e:               # the machine state is unknown now
            f.append(f"cost {name}: no HALT ({type(e).__name__}: the guest starved)")
            break
        if not clean(res, f"cost {name}", f):
            continue
        print(f"    cost {name}: {res['frames']} frames")
        if res["frames"] > 2 * base + 1:
            f.append(f"cost {name}: {res['frames']} frames, idle {base}: over 2x")
    return f


def bigdiv_script() -> list[tuple]:
    """Round-1 F2, directed: continuous TA with a latch > 32768 (the 17-bit
    division remainder) and $FFFF (span 65536: the shift path), TB counting
    TA's underflows (latch $FFFF), then gaps of several to 2^24 E ticks;
    TA, TB and the ICR are read after every gap."""
    ops: list[tuple] = []
    for c, tal in ((0, 0xFFFF), (1, 0x8000), (0, 0x9ABC), (1, 0xFFFE), (0, 0xFFFF)):
        ops += [("wr", c, 13, 0x7F), ("wr", c, 4, tal & 0xFF), ("wr", c, 5, tal >> 8),
                ("wr", c, 6, 0xFF), ("wr", c, 7, 0xFF), ("wr", c, 15, 0x51),
                ("wr", c, 14, 0x11)]
        for dt in (3 * 65536 + 5, 65536 * 5 + 77, 1 << 20, (1 << 24) + 3):
            ops += [("time", dt), ("rd", c, 4), ("rd", c, 5), ("rd", c, 6),
                    ("rd", c, 7), ("rd", c, 13)]
        ops += [("wr", c, 14, 0x00), ("wr", c, 15, 0x00)]
    return ops


def leg_bigdiv(rig: Rig, *_a) -> list[str]:
    if not rig.cia:
        return ["bigdiv: no cia_read label (no runtime/ops/cia.s)"]
    return sweep(rig, 0xB16D, 0, script=bigdiv_script())


def leg_sweep(rig: Rig, seeds: int, nops: int) -> list[str]:
    fails = []
    if not rig.cia:
        fails.append("sweep: no cia_read label (no runtime/ops/cia.s): "
                     "frozen-time run")
    if not rig.cia:
        return fails + red_sweep(rig, seeds, nops)
    for seed in range(seeds):
        fails += sweep(rig, 0xC1A0 + seed, nops)
    return fails


def red_sweep(rig: Rig, seeds: int, nops: int) -> list[str]:
    """The base build (no cia.s, no seam labels): the same register
    sequences with guest time frozen, values only."""
    fails = []
    rd8, wr8 = rig.lab("mem_read8"), rig.lab("mem_write8")
    for seed in range(seeds):
        rng = random.Random(0xC1A0 + seed)
        rig.call(rig.lab("chip_reset"))
        m = Model(0)
        for i in range(nops):
            op = gen_op(rng)
            if op[0] == "rd":
                _, c, reg = op
                got, _ = rig.api(rd8, cia_addr(c, reg))
                want = m.chip(c).read(reg)
                if got & 0xFF != want:
                    fails.append(f"red seed {seed} op {i} read {'AB'[c]} reg {reg}: "
                                 f"${got & 0xFF:02X} want ${want:02X}")
            elif op[0] == "wr":
                _, c, reg, v = op
                rig.api(wr8, cia_addr(c, reg), v)
                m.chip(c).write(reg, v)
    return fails


# --------------------------------------------------------------------------
# Guest legs: 68000 programs through the $BF trap under mach_run
# (tests/mach/drv.s via tests.run_mach.Rig; the base PRG runs them through
# test_run_program with an instruction budget: the red run)
# --------------------------------------------------------------------------

CIAA, CIAB = 0xBFE001, 0xBFD000
INTREQR, INTREQ = 0xDFF01E, 0xDFF09C
E_HZ = 709379               # A500 E clock (chipset_ref: CPU_HZ / 10)
POLL_BOUND = 60000          # dbra polls before a leg gives up (d2 = code)


def ca(chip: int, reg: int) -> int:
    return (CIAA if chip == 0 else CIAB) + (reg << 8)


def mvb_imm(g, v: int, a: int):                 # move.b #v,abs.l
    return g(0x13FC, v & 0xFF, a >> 16, a & 0xFFFF)


def mvb_abs_d(g, a: int, d: int):               # move.b abs.l,Dd
    return g(0x1039 | d << 9, a >> 16, a & 0xFFFF)


def mvb_d_abs(g, d: int, a: int):               # move.b Dd,abs.l
    return g(0x13C0 | d, a >> 16, a & 0xFFFF)


def wait_tod_a(g, tag: str, cur: int, nxt: int, code: int):
    """Poll CIA-A TODLO (live, unlatched) into d<nxt> until it differs from
    d<cur>; d2 := code and HALT after POLL_BOUND polls."""
    from tests.run_mach import BNE, HALT, moveq, mv_imm_d_w
    mv_imm_d_w(g, POLL_BOUND - 1, 7)
    g.label(tag)
    mvb_abs_d(g, ca(0, 8), nxt)
    g(0xB000 | nxt << 9 | cur)                  # cmp.b d<cur>,d<nxt>
    g.bcc(BNE, tag + "_ok")
    g.dbra(7, tag)
    moveq(g, code, 2)
    g(HALT)
    return g.label(tag + "_ok")


def prog_probe() -> list[int]:
    """The calibration probe (clean room, from the 8520/Paula behaviour):
    CIA-A timer A counts E ticks; CIA-A's TOD counts vertical blanks.  Wait
    for a TOD edge, start TA from $FFFF (force load, continuous), wait for
    the next edge, stop TA: ticks = $FFFF - TA; the TOD frequency is
    E_HZ / ticks, rounded.  Out: d0 = Hz, d1 = ticks, d2 = 0 (else 1/2: no
    first/second TOD edge within the poll bound)."""
    from tests.run_mach import G, HALT, moveq
    g = G()
    moveq(g, 0, 0)
    moveq(g, 0, 1)
    moveq(g, 0, 2)
    mvb_imm(g, 0x00, ca(0, 14))                 # CRA: stopped
    mvb_imm(g, 0xFF, ca(0, 4))                  # latch $FFFF (a stopped
    mvb_imm(g, 0xFF, ca(0, 5))                  # timer loads on the hi write)
    mvb_abs_d(g, ca(0, 8), 6)                   # d6 = TODLO
    wait_tod_a(g, "w1", 6, 5, 1)                # d5 = first new value
    mvb_imm(g, 0x11, ca(0, 14))                 # force load + start
    wait_tod_a(g, "w2", 5, 6, 2)
    mvb_imm(g, 0x00, ca(0, 14))                 # stop
    moveq(g, 0, 3)
    mvb_abs_d(g, ca(0, 5), 3)                   # d3 = TA hi
    g(0xE14B)                                   # lsl.w #8,d3
    mvb_abs_d(g, ca(0, 4), 3)                   # d3 = TA
    g(0x223C, 0, 0xFFFF)                        # move.l #$FFFF,d1
    g(0x9283)                                   # sub.l d3,d1: ticks
    g(0x2801)                                   # move.l d1,d4
    g(0xE28C)                                   # lsr.l #1,d4
    g(0x203C, E_HZ >> 16, E_HZ & 0xFFFF)        # move.l #E_HZ,d0
    g(0xD084)                                   # add.l d4,d0 (rounding)
    g(0x80C1)                                   # divu.w d1,d0
    g(0x0280, 0, 0xFFFF)                        # andi.l #$FFFF,d0
    g(HALT)
    return g.words()


def leg_probe(rig, *_a) -> list[str]:
    """S3.4 red/green: the probe reports 50 Hz (14188 ticks per TOD tick).
    Red: 0 (TOD never moves: d2 = 1)."""
    from tests.run_mach import clean
    f: list[str] = []
    mr = rig.mach_rig()
    (pc,) = mr.stage(prog_probe())
    for s in (8, 4):
        rig.fresh()
        res = mr.run(pc, s=s, budget=2_000_000)
        tag = f"probe s={s}"
        if not clean(res, tag, f):
            continue
        hz, ticks, code = res["d"][0], res["d"][1], res["d"][2]
        print(f"    {tag}: {hz} Hz, {ticks} ticks per TOD tick (code {code})")
        rig.probe.append((s, hz, ticks, code))
        if code or hz != 50:
            f.append(f"{tag}: reported {hz} Hz ({ticks} ticks, code {code}), want 50 Hz")
        # one poll period of jitter at most: ~10 E at s = 8, ~160 at s = 4
        tol = 40 if s == 8 else 300
        if not code and abs(ticks - chipset_ref.MACH_FRAME_E) > tol:
            f.append(f"{tag}: {ticks} ticks per TOD tick, want "
                     f"{chipset_ref.MACH_FRAME_E} +-{tol}")
        if res["base"]:
            break
    return f


def wait_req(g, tag: str, bit: int, code: int):
    """Poll INTREQR (no CIA access: only an armed event can post) until
    bit *bit* is set; d2 := code and HALT after POLL_BOUND polls."""
    from tests.run_mach import BNE, HALT, moveq, mv_abs_d, mv_imm_d_w
    mv_imm_d_w(g, POLL_BOUND - 1, 7)
    g.label(tag)
    mv_abs_d(g, "w", INTREQR, 4)
    g(0x0244, 1 << bit)                         # andi.w #bit,d4
    g.bcc(BNE, tag + "_ok")
    g.dbra(7, tag)
    moveq(g, code, 2)
    g(HALT)
    return g.label(tag + "_ok")


def prog_tirq() -> list[int]:
    """Timer interrupts through INTREQ.  CIA-A TA one-shot (latch 3000,
    ICR mask TA): PORTS rises with no CIA access, ICR reads $81 then $00,
    CRA reads stopped.  CIA-A TB continuous (latch $6000, mask TB): PORTS
    cleared in INTREQ without reading the ICR comes straight back (the CIA
    still asserts /INT); after the ICR read a clear sticks.
    Out: d0 = ICR, ICR again, CRA, INTREQR after the uncleared clear
    (bytes 3..0: d0 = $81 00 08|0 bit); d1 = the INTREQR word after the
    unacknowledged clear (hi) and after the acknowledged one (lo);
    d2 = 0, else the step that timed out; d3 = second ICR read ($82)."""
    from tests.run_mach import G, HALT, moveq, mv_abs_d, mv_imm_abs_w
    g = G()
    for d in (0, 1, 2, 3):
        moveq(g, 0, d)
    mvb_imm(g, 0x7F, ca(0, 13))                 # ICR: every mask bit off
    mvb_abs_d(g, ca(0, 13), 0)                  # (drop stale data)
    mvb_imm(g, 0x00, ca(0, 14))
    mvb_imm(g, 0x00, ca(0, 15))
    mv_imm_abs_w(g, 0x0008, INTREQ)             # PORTS clear
    mvb_imm(g, 0x81, ca(0, 13))                 # mask TA
    mvb_imm(g, 3000 & 0xFF, ca(0, 4))
    mvb_imm(g, 0x08, ca(0, 14))                 # one-shot (stopped)
    mvb_imm(g, 3000 >> 8, ca(0, 5))             # hi write: load + start
    wait_req(g, "t1", 3, 1)
    mvb_abs_d(g, ca(0, 13), 0)                  # ICR: $81
    g(0xE188)                                   # lsl.l #8,d0
    mvb_abs_d(g, ca(0, 13), 0)                  # ICR again: $00
    g(0xE188)
    mvb_abs_d(g, ca(0, 14), 0)                  # CRA: $08 (stopped)
    g(0xE188)                                   # d0 = ICR, ICR, CRA, 0
    # --- re-raise: TB continuous, mask TB only
    mvb_imm(g, 0x7F, ca(0, 13))
    mvb_imm(g, 0x82, ca(0, 13))
    mvb_imm(g, 0x00, ca(0, 6))
    mvb_imm(g, 0x60, ca(0, 7))                  # latch $6000 (stopped: loads)
    mv_imm_abs_w(g, 0x0008, INTREQ)
    mvb_imm(g, 0x01, ca(0, 15))                 # start, continuous, E ticks
    wait_req(g, "t2", 3, 2)
    mv_imm_abs_w(g, 0x0008, INTREQ)             # clear, ICR not read
    mv_abs_d(g, "w", INTREQR, 1)                # d1.w: PORTS back
    g(0x4841)                                   # swap d1
    mvb_abs_d(g, ca(0, 13), 3)                  # ICR: $82
    mv_imm_abs_w(g, 0x0008, INTREQ)             # acknowledged: the clear sticks
    mv_abs_d(g, "w", INTREQR, 1)
    mvb_imm(g, 0x00, ca(0, 15))                 # stop TB
    mvb_imm(g, 0x7F, ca(0, 13))
    g(HALT)
    return g.words()


def leg_tirq(rig, *_a) -> list[str]:
    from tests.run_mach import clean
    f: list[str] = []
    mr = rig.mach_rig()
    (pc,) = mr.stage(prog_tirq())
    rig.fresh()
    res = mr.run(pc, s=4, budget=2_000_000)
    if not clean(res, "tirq", f):
        return f
    d0, d1, d2, d3 = res["d"]
    if d2:
        f.append(f"tirq: step {d2} timed out: PORTS never rose (d0=${d0:08X})")
        return f
    icr1, icr2, cra = d0 >> 24, (d0 >> 16) & 0xFF, (d0 >> 8) & 0xFF
    if (icr1, icr2) != (0x81, 0x00):
        f.append(f"tirq: ICR reads ${icr1:02X}, ${icr2:02X}, want $81, $00")
    if cra != 0x08:
        f.append(f"tirq: CRA after the one-shot ${cra:02X}, want $08 (stopped)")
    if not (d1 >> 16) & 0x0008:
        f.append(f"tirq: INTREQR ${d1 >> 16:04X} after clearing PORTS with the "
                 "ICR still asserting: PORTS not raised again")
    if d3 & 0xFF != 0x82:
        f.append(f"tirq: TB ICR read ${d3 & 0xFF:02X}, want $82")
    if d1 & 0x0008:
        f.append(f"tirq: INTREQR ${d1 & 0xFFFF:04X}: PORTS back after the ICR "
                 "was acknowledged")
    return f


def prog_todb() -> list[int]:
    """CIA-B TOD counts lines: its value across one CIA-A TOD tick (one
    frame).  Also arms a CIA-B TOD alarm 100 lines ahead with mask ALRM and
    polls INTREQR for EXTER with no CIA-B access.
    Out: d0 = lines in one frame (TOD-B mid:lo delta); d1 = ICR-B after
    EXTER ($84); d3 = TOD-B when EXTER was seen - the alarm (lines late);
    d2 = 0, else the step that timed out."""
    from tests.run_mach import G, HALT, moveq, mv_imm_abs_w
    g = G()
    for d in (0, 1, 2, 3):
        moveq(g, 0, d)
    mvb_abs_d(g, ca(0, 8), 6)
    wait_tod_a(g, "f1", 6, 5, 1)
    mvb_abs_d(g, ca(1, 10), 0)                  # TOD-B hi: latch
    mvb_abs_d(g, ca(1, 9), 0)
    g(0xE148)                                   # lsl.w #8,d0
    mvb_abs_d(g, ca(1, 8), 0)                   # d0.w = TOD-B (mid:lo)
    wait_tod_a(g, "f2", 5, 6, 2)
    mvb_abs_d(g, ca(1, 10), 3)
    mvb_abs_d(g, ca(1, 9), 3)
    g(0xE14B)                                   # lsl.w #8,d3
    mvb_abs_d(g, ca(1, 8), 3)
    g(0x9640)                                   # sub.w d0,d3
    g(0x3003)                                   # move.w d3,d0: lines
    # --- alarm: 100 lines ahead of the TOD (24 bit), mask ALRM
    mvb_imm(g, 0x7F, ca(1, 13))
    mvb_abs_d(g, ca(1, 13), 1)                  # drop stale data
    moveq(g, 0, 3)
    mvb_abs_d(g, ca(1, 10), 3)
    g(0x4843)                                   # swap d3
    mvb_abs_d(g, ca(1, 9), 3)
    g(0xE14B)                                   # lsl.w #8,d3
    mvb_abs_d(g, ca(1, 8), 3)                   # d3 = TOD-B
    g(0x0683, 0, 100)                           # addi.l #100,d3: the alarm
    mvb_imm(g, 0x80, ca(1, 15))                 # CRB bit 7: writes the alarm
    g(0x2803)                                   # move.l d3,d4
    g(0x4844)                                   # swap d4
    mvb_d_abs(g, 4, ca(1, 10))
    g(0x2803)
    g(0xE08C)                                   # lsr.l #8,d4
    mvb_d_abs(g, 4, ca(1, 9))
    mvb_d_abs(g, 3, ca(1, 8))
    mvb_imm(g, 0x00, ca(1, 15))
    mv_imm_abs_w(g, 0x2000, INTREQ)             # EXTER clear
    mvb_imm(g, 0x84, ca(1, 13))                 # mask ALRM
    wait_req(g, "a1", 13, 3)
    moveq(g, 0, 4)
    mvb_abs_d(g, ca(1, 10), 4)                  # TODHI first: latch now
    g(0x4844)                                   # swap d4
    mvb_abs_d(g, ca(1, 9), 4)
    g(0xE14C)                                   # lsl.w #8,d4
    mvb_abs_d(g, ca(1, 8), 4)                   # d4 = TOD-B at detection
    moveq(g, 0, 1)
    mvb_abs_d(g, ca(1, 13), 1)                  # ICR: $84
    g(0x9883)                                   # sub.l d3,d4
    g(0x2604)                                   # move.l d4,d3: lines late
    mvb_imm(g, 0x7F, ca(1, 13))
    g(HALT)
    return g.words()


TODB_S = (8, 4)
LATE_PHI2 = 24000           # alarm detection latency budget (phi2)


def leg_todb(rig, *_a) -> list[str]:
    from tests.run_mach import clean
    f: list[str] = []
    mr = rig.mach_rig()
    (pc,) = mr.stage(prog_todb())
    for s in TODB_S:
        rig.fresh()
        res = mr.run(pc, s=s, budget=3_000_000)
        tag = f"todb s={s}"
        if not clean(res, tag, f):
            continue
        d0, d1, d2, d3 = res["d"]
        lines, late = d0 & 0xFFFF, d3 - (1 << 32) if d3 & 0x80000000 else d3
        print(f"    {tag}: {lines} lines per frame, alarm seen {late} line(s) "
              f"late, ICR ${d1 & 0xFF:02X} (code {d2})")
        if d2:
            f.append(f"{tag}: step {d2} timed out")
        else:
            # 312.5 lines per 14188 E frame, +- the poll jitter (~3 lines at
            # s = 4, under 1 at s = 8)
            tol = 2 if s == 8 else 6
            if abs(lines - 312.5) > tol:
                f.append(f"{tag}: CIA-B TOD counted {lines} lines in a frame, "
                         f"want 312-313 +-{tol}")
            if d1 & 0xFF != 0x84:
                f.append(f"{tag}: ICR-B ${d1 & 0xFF:02X} after EXTER, want $84")
            # Lateness is a fixed phi2 cost, so it shrinks as 2^-s: the
            # firing (<= 4096 phi2, CONTRACTS "Event seam"), the service
            # pass (callback: sync + alarm deadline, ~3000), one INTREQR
            # poll and the TODHI trap: LATE_PHI2 in all.  Exactness of the
            # deadline itself is the sweep's job (host time, to the tick).
            late_max = -(-LATE_PHI2 // ((chipset_ref.CIA_LINE_CC << s) // 5)) + 1
            if not 0 <= late <= late_max:
                f.append(f"{tag}: EXTER seen with the TOD {late} lines past the "
                         f"alarm (budget {late_max})")
        if res["base"]:
            break
    return f


# --------------------------------------------------------------------------
# Builds, runner
# --------------------------------------------------------------------------

def make_sources() -> list[str]:
    from tests.rec_build import make_sources as ms
    return ms()


def build_shim(out_dir: str, replace: dict[str, str] | None = None,
               shim: bool = True) -> str:
    """runtime (+ tests/cia/shim.s: the pre-seam test PRG), with sources
    swapped per *replace* (mutants)."""
    out_dir = os.path.join(ROOT, out_dir)
    gen = os.path.join(out_dir, "gen", "dispatch.s")
    os.makedirs(os.path.dirname(gen), exist_ok=True)
    subprocess.run([sys.executable, "host/gen_dispatch.py", "-o", gen],
                   cwd=ROOT, check=True)
    srcs = [(replace or {}).get(s, s) for s in make_sources()]
    if shim:
        srcs.append(os.path.join(ROOT, "tests", "cia", "shim.s"))
    prg = os.path.join(out_dir, "runner.prg")
    cmd = ["cl65", "-t", "none", "-C", "runtime/c64.cfg", "-o", prg,
           *(["--asm-define", "CIA_HOSTTIME"] if shim else []),
           "-Ln", os.path.join(out_dir, "runner.lbl"),
           "-m", os.path.join(out_dir, "runner.map"),
           "--asm-include-dir", "runtime", "--asm-include-dir", "runtime/ops"]
    cmd += [os.path.relpath(s, ROOT) for s in srcs] + [gen]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"shim build failed: {r.stdout}\n{r.stderr}")
    return prg


def run_all(prg: str, legs: list[str], seeds: int, nops: int) -> list[str]:
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False, ntsc=False,
                     extra_args=list(REU_VICE_ARGS))
    fails: list[str] = []
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        try:
            prepare_target(tg, prg)
            rig = Rig(tg, labels)
            rig.prg = prg
            for name in legs:
                f = LEGS[name](rig, seeds, nops)
                print(f"[{name}] {'PASS' if not f else 'FAIL'}", flush=True)
                lim = len(f) if VERBOSE else 40
                for x in f[:lim]:
                    print(f"    {x}")
                if len(f) > lim:
                    print(f"    ... {len(f) - lim} more")
                fails += f
        finally:
            mgr.release(tg)
    return fails


LIVE_LEGS = ("sweep", "bigdiv", "probe", "tirq", "todb", "cost")   # runtime PRG
HOST_LEGS = ("sweep", "bigdiv", "wrap", "neggap", "resync")        # host-time PRG
LEGS = {"sweep": leg_sweep, "wrap": leg_wrap, "neggap": leg_neggap,
        "resync": leg_resync, "cost": leg_cost, "bigdiv": leg_bigdiv,
        "probe": leg_probe, "tirq": leg_tirq,
        "todb": leg_todb}

# --------------------------------------------------------------------------
# Teeth: one source mutant of runtime/ops/cia.s per CIA rule (C3)
# --------------------------------------------------------------------------

MUTANTS = {
    # a one-shot timer A keeps running after its underflow
    "oneshot_stop": [("        lda cia_st+CIA_CRA,x      ; one-shot: stop\n"
                      "        and #$08\n        beq tb\n",
                      "        lda cia_st+CIA_CRA,x      ; one-shot: stop\n"
                      "        and #$00\n        beq tb\n")],
    # CRA bit 4 (force load) ignored
    "force_load": [("wr_cra: lda cia_v               ; bit 4: force load (strobe, not stored)\n"
                    "        and #$10\n",
                    "wr_cra: lda cia_v               ; bit 4: force load (strobe, not stored)\n"
                    "        and #$00\n")],
    # the TAHI write in one-shot mode loads but does not start the timer
    "hibyte_start": [("        lda cia_st+CIA_CRA,x\n        ora #$01\n"
                      "        sta cia_st+CIA_CRA,x\n:       jmp wdone\n",
                      "        lda cia_st+CIA_CRA,x\n        ora #$00\n"
                      "        sta cia_st+CIA_CRA,x\n:       jmp wdone\n")],
    # reading the ICR leaves the data bits set
    "icr_readclear": [("        lda #0\n        sta cia_st+CIA_ICR,x\n"
                       "        jsr post                ; IR is 0 now",
                       "        jsr post                ; IR is 0 now")],
    # ICR write: SET and CLR swapped
    "mask_setclr": [("wr_icr: lda cia_v               ; bit 7: SET, else CLR, bits 0-4\n"
                     "        bmi set\n",
                     "wr_icr: lda cia_v               ; bit 7: SET, else CLR, bits 0-4\n"
                     "        bpl set\n")],
    # TB counts E ticks whatever CRB INMODE says
    "tb_inmode": [("        and #$30                ; INMODE << 4 (after lsr)\n",
                   "        and #$00                ; INMODE << 4 (after lsr)\n")],
    # a TODHI read does not latch the counter
    "tod_latch": [("        lda #1\n        sta cia_st+CIA_TLF,x\n",
                   "        lda #0\n        sta cia_st+CIA_TLF,x\n")],
    # CIA-B TOD (line grid) never raises the alarm bit
    "tod_alarm_b": [("        bcc add\n        lda cia_st+CIA_ICR,x\n        ora #$04\n",
                     "        bcc add\n        lda cia_st+CIA_ICR,x\n        ora #$00\n")],
    # the ICR read-clear does not release the mach_hold bit (a later
    # INTREQ CLR never sticks)
    "hold_release": [("        jsr post                ; IR is 0 now: release the hold\n", "")],
    # no level hold: the INTREQ bit is posted but never held (a guest CLR
    # with the ICR still asserting sticks)
    "hold_set": [("        lda mach_hold,y\n        ora cia_st+CIA_HMK,x\n"
                  "        sta mach_hold,y\n", "")],
    # the CIA-B line grid runs 1 colour clock per E tick instead of 5
    "line_rate": [("        asl cia_dr\n        rol cia_dr+1\n        asl cia_dr\n"
                   "        rol cia_dr+1\n", "")],
    # round-1 F1a: TA's own event armed although its ICR bit is already set
    "own_icr": [("        lda cia_st+CIA_ICR,x    ; mask TA and ICR TA still clear\n"
                 "        eor #$FF\n",
                 "        lda #$FF                ; mask TA and ICR TA still clear\n")],
    # round-1 F1a: the same for TB
    "own_icr_tb": [("        lda cia_st+CIA_ICR,x\n        eor #$FF\n"
                    "        and cia_st+CIA_MSK,x\n        and #$02\n",
                    "        lda #$FF\n"
                    "        and cia_st+CIA_MSK,x\n        and #$02\n")],
    # round-1 F1b: a cascaded TB is armed at TA's next underflow, not TB's
    "casc_first": [("        and #$08\n        beq cont\n", "        and #$08\n        beq first\n")],
    # round-1 F2 (reviewer): the division drops the 17th remainder bit
    "div17": [("        bcs sub                 ; bit 16 set: certainly >= divisor\n", "")],
    # round-1 F2 (reviewer): latch $FFFF takes the 16-bit divisor path
    "span64k": [("        bne small\n", "        jmp small\n")],
    # round-1 F3 (reviewer): a negative gap counts as a huge forward one
    "neg_gap": [("        bpl fwd\n", "        jmp fwd\n")],
    # round-1 F3 (reviewer): no 256-frame resync in the VERTB hook
    "no_resync": [("        inc cia_vc\n        bne tod\n", "        inc cia_vc\n        jmp tod\n")],
    # CIA-A TOD (VERTB) never raises the alarm bit
    "tod_alarm_a": [("        lda cia_st+CIA_ICR\n        ora #$04\n",
                     "        lda cia_st+CIA_ICR\n        ora #$00\n")],
}


HOST_ONLY_MUTANTS = ("neg_gap", "no_resync")


def build_mutant(name: str, out_dir: str, shim: bool) -> str:
    src = os.path.join(ROOT, "runtime", "ops", "cia.s")
    text = open(src).read()
    for a, b in MUTANTS[name]:
        if text.count(a) != 1:
            raise RuntimeError(f"mutant {name}: anchor found {text.count(a)} times")
        text = text.replace(a, b)
    os.makedirs(os.path.join(ROOT, out_dir), exist_ok=True)
    mpath = os.path.join(ROOT, out_dir, f"mut_{name}_cia.s")
    with open(mpath, "w") as f:
        f.write(text)
    return build_shim(out_dir, {src: mpath}, shim=shim)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default="",
                    help=f"default: {','.join(LIVE_LEGS)} (live PRG), "
                         f"{','.join(HOST_LEGS)} (--build-shim)")
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--ops", type=int, default=500)
    ap.add_argument("--build-shim", default="",
                    help="build the shim PRG into this dir and use it")
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    global VERBOSE
    VERBOSE = a.verbose
    prg = a.prg
    if a.build_shim:
        prg = build_shim(a.build_shim)
        print(f"shim PRG: {prg}")
    host = ".shim_t" in open(os.path.splitext(prg)[0] + ".lbl").read()
    legs = [x for x in a.legs.split(",") if x] or list(
        HOST_LEGS if host else LIVE_LEGS)
    if a.mutants:
        # neg_gap/no_resync need host-set time (legs neggap, resync)
        names = [x for x in a.only.split(",") if x] or [
            m for m in MUTANTS if host or m not in HOST_ONLY_MUTANTS]
        survived = []
        shim = bool(a.build_shim)
        bdir = os.path.relpath(os.path.dirname(os.path.abspath(prg)), ROOT)
        for mname in names:
            mprg = build_mutant(mname, f"{bdir}-mut-{mname}", shim)
            print(f"=== mutant {mname}: {mprg}", flush=True)
            fails = run_all(mprg, legs, a.seeds, a.ops)
            print(f"=== mutant {mname}: {'KILLED' if fails else 'SURVIVED'} "
                  f"({len(fails)} failure(s))", flush=True)
            if not fails:
                survived.append(mname)
        print(f"mutants: {len(names) - len(survived)}/{len(names)} killed"
              + (f"; SURVIVED: {survived}" if survived else ""))
        return 1 if survived else 0
    fails = run_all(prg, legs, a.seeds, a.ops)
    print(f"run_cia: {len(legs)} leg(s), {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
