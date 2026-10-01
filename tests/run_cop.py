#!/usr/bin/env python3
"""S3.5 copper and display-register state: guest legs (VICE).

docs/WAVE10_BRIEF.md C1/C2, docs/S3_PLAN.md §4 "S3.5", contract
docs/CONTRACTS.md "Copper and display state (S3.5)".  Reference:
host/chipset_ref.DisplayChipset (Chipset + CopperAt + DisplayState).
Every case stages a guest program and its copper lists (one hunk, as
tests/run_mach.py does), runs it, then compares the C64's disp_st block,
the copper counters and the custom shadow ($020-$1FE, INTREQR) with the
model fed the same CPU writes.  A PRG without cop.s (the base build) runs
the same guests: its disp_st stays unwritten, so every leg is red there.

Legs:
  nohook   first, in a fresh session: no disp_init ever ran (cop_dhook as
           the boot left it); a run must not call through it (red: the
           round-0 rc vector at $07E8 holds VICE's power-up $FF)
  sync     C2 red/green: a copper list sets COLOR00/BPLCON0/BPL1PT, the
           guest strobes COPJMP1 and turns copper DMA on (HLE: no
           mach_run), disp_st follows
  model    lists against the model: WAIT satisfied at once, SKIP counted
           as a no-op, the danger threshold ($80; $40 with COPCON CDANG),
           the end marker ($FFFF,$FFFE; $FFFF,$FFFC and $FFFF,$7FFE are
           WAITs), COPJMP1/2 inside a list, a CPU COPJMP2 strobe (COP2LC)
           with copper DMA on and off, the step limit, a list in bank $DF,
           copper writes to DMACON/INTREQ, every display register, and
           seeded random lists
  cpu      CPU word/long/byte writes to every display field; a rewrite of
           the same value does not bump gen, a change does; a long write
           whose first word strobes still writes its second (the trap
           state survives the run and a hook that clobbers it)
  dhook    cop_dhook: called once per run, never when its high byte is 0
  enable   the gate: a strobe with copper DMA off only loads the PC; the
           DMACON write that turns it on runs it (DMAEN and COPEN in either
           order) when a PC was loaded (none since reset: nothing runs
           until the strobe), later DMACON writes do not; a copper stopped
           at its end marker or a danger MOVE stays there (re-enable reruns
           only that);
           the copper's own DMACON writes start no run
  frame    mach_run: cop_reset registered lazily by the guest's first
           display write; the copper re-runs from COP1LC every frame with
           DMAEN+COPEN (an edited list takes effect), not after COPEN is
           cleared; the frame hook runs once per VERTB (cop_nfr =
           mach_frames) and calls cop_dhook every frame

    python3 -m tests.run_cop --prg build/X/runner.prg [--legs a,b]
    python3 -m tests.run_cop --prg ... --mutants [--only m1,m2]

Exit 0 = every selected leg passed (--mutants: every mutant failed a leg).
"""

from __future__ import annotations

import argparse
import os
import random
import struct
import subprocess
import sys

from host import chipset_ref as cr
from tests import run_mach
from tests.run_mach import BASE, HALT, G, build_image, mv_imm_abs_w

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CUSTOM = 0xDFF000
DISP_ST = 0xB800                            # rc claim (runtime/rc/ram.inc)
DHOOK_07E8 = 0x07E8         # the round-0 rc claim of cop_dhook (red builds)
HOOK, HOOKN = 0xCD00, 0xCD80                # fake display hook + its counter
PORT_RAM = 0x34
END = [0xFFFF, 0xFFFE]
COP1LC, COP2LC, COPJMP1, COPJMP2 = 0x080, 0x084, 0x088, 0x08A
COPCON, DMACON, INTREQ, INTREQR = 0x02E, 0x096, 0x09C, 0x01E
COLOR, BPLCON0, BPLPT = 0x180, 0x100, 0x0E0
VERBOSE = False


def mv(r: int, v: int) -> list[int]:
    return [r & 0x1FE, v & 0xFFFF]


def wait(v: int = 0x2C, h: int = 0x01) -> list[int]:
    return [(v << 8) | h | 1, 0xFFFE]


def skip(v: int = 0x30) -> list[int]:
    return [(v << 8) | 1, 0xFFFF]


# --------------------------------------------------------------------------
# A case: copper lists + the guest's CPU writes
# --------------------------------------------------------------------------

class Case:
    """lists: name -> words; ops: (size, reg offset, value | list name),
    size "w"/"l"/"b" (a byte op writes the odd byte at reg + 1).  A long
    whose value names a list writes that list's address."""

    def __init__(self, name: str, lists: dict[str, list[int]], ops: list,
                 hook: bool = False):
        self.name, self.lists, self.ops, self.hook = name, lists, ops, hook

    def prog(self, addr: dict[str, int]) -> list[int]:
        g = G()
        for size, reg, v in self.ops:
            v = addr[v] if isinstance(v, str) else v
            a = CUSTOM + reg
            if size == "l":
                g(0x23FC, v >> 16, v & 0xFFFF, *g.abs(a))
            elif size == "w":
                mv_imm_abs_w(g, v, a)
            else:
                g(0x13FC, v & 0xFF, *g.abs(a + 1))
        g(HALT)
        return g.words()

    def image(self):
        names = list(self.lists)
        n0 = len(self.prog({k: 0 for k in names}))
        addr, at = {}, BASE + 2 * n0
        for k in names:
            addr[k] = at
            at += 2 * len(self.lists[k])
        prog = self.prog(addr)
        chunks, entries = build_image(prog, *[self.lists[k] for k in names])
        assert entries[1:] == [addr[k] for k in names], (entries, addr)
        return chunks, entries[0], addr

    def model(self, addr: dict[str, int], gen: int) -> cr.DisplayChipset:
        mem = cr.Ram(0x1000000)
        for k, words in self.lists.items():
            for i, w in enumerate(words):
                mem.w16(addr[k] + 2 * i, w)
        m = cr.DisplayChipset(mem, gen)
        for size, reg, v in self.ops:
            v = addr[v] if isinstance(v, str) else v
            if size == "l":
                m.cpu_write(reg, v >> 16)
                m.cpu_write(reg + 2, v & 0xFFFF)
            elif size == "w":
                m.cpu_write(reg, v)
            else:
                m.cpu_write(reg, (v & 0xFF) * 0x101)
        return m


# --------------------------------------------------------------------------
# The rig (run_mach.Rig plus cop's labels)
# --------------------------------------------------------------------------

class CopRig:
    def __init__(self, rig: run_mach.Rig):
        self.rig = rig
        try:
            rig.lab("cop_reset")
            self.cop = True
        except KeyError:
            self.cop = False
        try:                    # BSS (cleared at boot); round 0: rc $07E8
            self.dhook = rig.lab("cop_dhook")
        except KeyError:
            self.dhook = DHOOK_07E8

    def lab(self, n: str) -> int:
        return self.rig.lab(n)

    def ram(self, addr: int, n: int) -> bytes:
        """RAM under the ROMs ($01 = $34 around the read)."""
        t = self.rig.t
        port = self.rig.r(1, 1)
        t.write_memory(0x0001, bytes([PORT_RAM]), override="cop: RAM under ROM")
        try:
            return self.rig.r(addr, n)
        finally:
            t.write_memory(0x0001, port, override="cop: RAM under ROM")

    def u16(self, name: str) -> int:
        return struct.unpack("<H", self.rig.r(self.lab(name), 2))[0]

    def hook(self, on: bool) -> None:
        """The fake display hook: counts its calls, then clobbers A/X/Y,
        m_addr, m_val and chip_a (the trap state cop.s must restore)."""
        code = bytes([0xEE, HOOKN & 0xFF, HOOKN >> 8, 0xD0, 0x03,
                      0xEE, (HOOKN + 1) & 0xFF, HOOKN >> 8, 0xA9, 0x5A])
        for zp in range(0x58, 0x60):                    # m_addr, m_val
            code += bytes([0x85, zp])
        if self.cop:
            ca = self.lab("chip_a")
            for i in range(3):
                code += bytes([0x8D, (ca + i) & 0xFF, (ca + i) >> 8])
        code += bytes([0xA0, 0xFF, 0xA2, 0xFF, 0x60])
        self.rig.w(HOOK, code)
        self.rig.w(HOOKN, b"\0\0")
        self.rig.w(self.dhook, struct.pack("<H", HOOK if on else 0))

    def hookn(self) -> int:
        return struct.unpack("<H", self.rig.r(HOOKN, 2))[0]

    def reset(self) -> int:
        """chip_reset + cop_reset; returns gen after them."""
        self.rig.call(self.lab("chip_reset"))
        if self.cop:
            self.rig.call(self.lab("cop_reset"))
        return self.ram(DISP_ST, 1)[0]

    def state(self) -> dict:
        r = {"disp": self.ram(DISP_ST, cr.DISP_SZ),
             "shd": self.rig.r(self.lab("chip_shd"), 512)}
        if self.cop:
            for k in ("cop_n", "cop_nrun", "cop_nfr", "cop_nskip"):
                r[k] = self.u16(k)
            r["cop_why"] = self.rig.r(self.lab("cop_why"), 1)[0]
        return r


def disp_diff(got: bytes, want: bytes) -> list[str]:
    names = ["gen", "dmacon", None, "bplcon0", None, "bplcon1", None,
             "bplcon2", None]
    out = []
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            if i < 9:
                f = names[i] or names[i - 1] + "+1"
            elif i < 27:
                f = f"bplpt[{(i - 9) // 3}]+{(i - 9) % 3}"
            elif i < 31:
                f = f"bpl{(i - 27) // 2 + 1}mod+{(i - 27) % 2}"
            elif i < 39:
                f = ["diwstrt", "diwstop", "ddfstrt", "ddfstop"][(i - 31) // 2] \
                    + f"+{(i - 31) % 2}"
            else:
                f = f"color{(i - 39) // 2:02d}+{(i - 39) % 2}"
            out.append(f"+{i} {f}: ${a:02X} want ${b:02X}")
    return out


def run_case(cr_: CopRig, case: Case) -> list[str]:
    f: list[str] = []
    tag = case.name
    gen0 = cr_.reset()
    if case.hook is not None:           # None: cop_dhook left as it is
        cr_.hook(case.hook)
    chunks, pc, addr = case.image()
    cr_.rig.sess.stage(chunks)
    res = cr_.rig.sess.run_program(pc, BASE + 0x8000, 2_000_000)
    if res.timeout or res.halt != 1 or res.reason != 0:
        return [f"{tag}: no clean HALT (halt=${res.halt:02X} reason={res.reason} "
                f"timeout={res.timeout})"]
    m = case.model(addr, gen0)
    st = cr_.state()
    d = disp_diff(st["disp"], m.disp.block())
    if d:
        f.append(f"{tag}: disp_st differs: " + "; ".join(d[:8])
                 + (f" (+{len(d) - 8} more)" if len(d) > 8 else ""))
    shd = st["shd"]
    for r in range(0x020, 0x200, 2):
        got, want = struct.unpack_from(">H", shd, r)[0], m.regs[r >> 1]
        if got != want:
            f.append(f"{tag}: chip_shd ${r:03X} = ${got:04X}, model ${want:04X}")
    got = struct.unpack_from(">H", shd, INTREQR)[0]
    if got != m.paula.intreq:
        f.append(f"{tag}: INTREQR ${got:04X}, model ${m.paula.intreq:04X}")
    if not cr_.cop:
        return f
    want = {"cop_nrun": m.runs, "cop_nskip": m.cop.skips}
    if m.runs:
        want.update(cop_n=m.n, cop_why=m.cop.why)
    for k, v in want.items():
        if st[k] != v:
            f.append(f"{tag}: {k} = {st[k]}, model {v}")
    hn = cr_.hookn() if case.hook is not None else 0
    if hn != (m.runs if case.hook else 0):
        f.append(f"{tag}: cop_dhook called {hn}x, want "
                 f"{m.runs if case.hook else 0}")
    if VERBOSE:
        print(f"    {tag}: gen {gen0}->{st['disp'][0]} runs {m.runs} n {m.n}")
    return f


def run_cases(cr_: CopRig, cases: list[Case]) -> list[str]:
    f: list[str] = []
    for c in cases:
        f += run_case(cr_, c)
    return f


# --------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------

def strobe1(lst: str = "a") -> list:
    """The HLE fixture's order: COP1LC, COPJMP1 (loads the PC; copper DMA
    is off), DMACON SET DMAEN+COPEN (turns it on: one run)."""
    return [("l", COP1LC, lst), ("w", COPJMP1, 0), ("w", DMACON, 0x8280)]


def leg_sync(c: CopRig) -> list[str]:
    a = mv(COLOR, 0x0F00) + mv(BPLCON0, 0x2200) + mv(BPLPT, 0x0002) \
        + mv(BPLPT + 2, 0x3456) + END
    return run_cases(c, [Case("sync", {"a": a}, strobe1())])


def every_reg() -> list[int]:
    w = []
    for i in range(6):
        w += mv(BPLPT + 4 * i, 0x0010 + i) + mv(BPLPT + 4 * i + 2, 0x1000 * i + 0x0246)
    for i, r in enumerate((0x100, 0x102, 0x104, 0x108, 0x10A, 0x08E, 0x090,
                           0x092, 0x094)):
        w += mv(r, 0x1111 * (i + 1) ^ 0x8001)
    for i in range(32):
        w += mv(COLOR + 2 * i, (i * 0x0123 + 7) & 0x0FFF)
    return w


def random_list(rng: random.Random, n: int) -> list[int]:
    regs = ([0x08E, 0x090, 0x092, 0x094, 0x100, 0x102, 0x104, 0x108, 0x10A]
            + [BPLPT + 2 * i for i in range(12)] + [COLOR + 2 * i for i in range(32)]
            + [0x098, 0x0A0, 0x120, 0x140, 0x1C0, 0x1FE])
    w = []
    for _ in range(n):
        k = rng.random()
        if k < 0.75:
            w += mv(rng.choice(regs), rng.randrange(0x10000))
        elif k < 0.85:
            w += [rng.randrange(0x10000) | 1, rng.randrange(0x10000) & 0xFFFE]
        elif k < 0.92:
            w += [rng.randrange(0x10000) | 1, rng.randrange(0x10000) | 1]
        else:
            w += mv(DMACON, rng.randrange(0x10000))
    return w + END


def model_cases() -> list[Case]:
    cs = []
    # WAIT satisfied at once (the last MOVE wins), SKIP counted, no-op
    cs.append(Case("waitskip", {"a": mv(COLOR + 2, 0x0111) + wait() + mv(COLOR + 2, 0x0222)
                                + skip() + mv(COLOR + 4, 0x0333) + wait(0xFF, 0xDF)
                                + skip(0x80) + mv(COLOR + 6, 0x0444) + END},
                   strobe1()))
    # the danger rule: $040 refused without CDANG (the copper stops) ...
    cs.append(Case("danger80", {"a": mv(COLOR, 0x0123) + mv(0x040, 0x09F0)
                                + mv(COLOR + 2, 0x0BAD) + END}, strobe1()))
    cs.append(Case("danger7e", {"a": mv(COLOR, 0x0124) + mv(0x07E, 0x1234)
                                + mv(COLOR + 2, 0x0BAD) + END}, strobe1()))
    # ... allowed with CDANG; $03E still stops it
    cs.append(Case("dangercd", {"a": mv(COLOR, 0x0125) + mv(0x040, 0x09F0)
                                + mv(0x03E, 0x1234) + mv(COLOR + 2, 0x0BAD) + END},
                   [("w", COPCON, 0x0002)] + strobe1()))
    # $80 itself is not dangerous (COP1LCH from the copper)
    cs.append(Case("danger80ok", {"a": mv(0x084, 0) + mv(COLOR, 0x0126) + END},
                   strobe1()))
    # the end marker: nothing after it runs; $FFFF,$FFFC and $FFFF,$7FFE
    # are WAITs
    cs.append(Case("end", {"a": mv(COLOR, 0x0001) + [0xFFFF, 0xFFFC] + [0xFFFF, 0x7FFE]
                           + mv(COLOR + 2, 0x0002)
                           + [0xFFFE, 0xFFFE] + mv(COLOR + 4, 0x0003) + END
                           + mv(COLOR + 6, 0x0BAD) + END}, strobe1()))
    # a CPU COPJMP2 strobe runs COP2LC's list (copper DMA on); one with
    # copper DMA off loads it, and the DMACON turn-on runs it
    cs.append(Case("cpujmp2", {"a": mv(COLOR, 0x0AAA) + END, "b": mv(COLOR, 0x0BBB)
                               + mv(COLOR + 8, 0x0B0B) + END},
                   [("l", COP2LC, "b")] + strobe1() + [("w", COPJMP2, 0)]))
    cs.append(Case("cpujmp2off", {"a": mv(COLOR, 0x0AAA) + END, "b": mv(COLOR, 0x0BBB)
                                  + mv(COLOR + 8, 0x0B0B) + END},
                   [("l", COP1LC, "a"), ("l", COP2LC, "b"), ("w", COPJMP2, 0),
                    ("w", DMACON, 0x8280)]))
    # COPJMP2 inside a list (to a COP2LC the list itself sets), COPJMP1 back
    cs.append(Case("listjmp", {"a": mv(COLOR, 0x0111) + mv(COPJMP2, 0) + mv(COLOR, 0x0BAD) + END,
                               "b": mv(COLOR + 2, 0x0222) + mv(COP1LC, 0) + mv(COP1LC + 2, 0)
                               + END},
                   [("l", COP2LC, "b")] + strobe1()))
    cs.append(Case("listjmp1", {"a": mv(COLOR, 0x0111) + END,
                                "b": mv(COLOR + 2, 0x0222) + mv(COPJMP1, 0) + END},
                   [("l", COP1LC, "a"), ("l", COP2LC, "b"), ("w", COPJMP2, 0),
                    ("w", DMACON, 0x8280)]))
    # the step limit: a list that jumps back to itself
    cs.append(Case("limit", {"a": mv(COLOR, 0x0777) + wait() + mv(COPJMP1, 0)}, strobe1()))
    # a list in bank $DF: the copper stops before fetching
    cs.append(Case("iobank", {}, [("l", COP1LC, 0xDFF180), ("w", COPJMP1, 0),
                                  ("w", DMACON, 0x8280)]))
    # copper writes to DMACON (SET/CLR) and INTREQ (COPER)
    cs.append(Case("dmairq", {"a": mv(DMACON, 0x83C0) + mv(DMACON, 0x0140) + mv(INTREQ, 0x8010)
                              + mv(DMACON, 0x8001) + END},
                   [("w", DMACON, 0x7FFF), ("w", INTREQ, 0x7FFF)] + strobe1()))
    # every display register from the copper
    cs.append(Case("every", {"a": every_reg() + END}, strobe1()))
    rng = random.Random(0x5355)
    for i in range(6):
        cs.append(Case(f"rand{i}", {"a": random_list(rng, 40 + 20 * i)}, strobe1()))
    return cs


def leg_model(c: CopRig) -> list[str]:
    return run_cases(c, model_cases())


def cpu_cases() -> list[Case]:
    ops = []
    for i in range(6):
        ops.append(("l", BPLPT + 4 * i, 0x00120000 + 0x1111 * i))
    for i, r in enumerate((0x100, 0x102, 0x104, 0x108, 0x10A, 0x08E, 0x090, 0x092, 0x094)):
        ops.append(("w", r, (0x8001 + 0x1010 * i) & 0xFFFF))
    for i in range(32):
        ops.append(("w", COLOR + 2 * i, 0x0FFF - 0x75 * i))
    cs = [Case("cpuall", {}, ops)]
    # SET/CLR, bytes on both halves, longs over two registers
    cs.append(Case("cpumix", {}, [("w", DMACON, 0x8300), ("w", DMACON, 0x0100),
                                  ("b", COLOR + 6, 0x5A), ("b", BPLCON0, 0x92),
                                  ("l", 0x108, 0xFFFE0028), ("l", 0x092, 0x003800D0),
                                  ("l", 0x1BC, 0x0ABC0DEF)]))
    # the same value again: gen unchanged (the model counts changes)
    cs.append(Case("cpusame", {}, [("w", COLOR, 0x0123), ("w", COLOR, 0x0123),
                                   ("w", COLOR, 0x0124), ("b", COLOR + 2, 0x00),
                                   ("w", DMACON, 0x0000), ("w", DMACON, 0x8000),
                                   ("l", BPLPT, 0x00050000), ("l", BPLPT, 0x00050000)]))
    # a long write whose first word strobes: COPJMP1 then COPJMP2 (both
    # run), and COP2LCL then COPJMP1; the fake hook clobbers the trap state
    cs.append(Case("cpulong", {"a": mv(COLOR, 0x0A0A) + mv(COLOR + 2, 0x0A0A) + END,
                               "b": mv(COLOR + 2, 0x0B0B) + END},
                   [("l", COP2LC, "b")] + strobe1() + [("l", COPJMP1, 0),
                    ("w", COLOR + 4, 0x0C0C), ("l", 0x086, 0x0000FFFF)], hook=True))
    return cs


def leg_cpu(c: CopRig) -> list[str]:
    return run_cases(c, cpu_cases())


def leg_dhook(c: CopRig) -> list[str]:
    a = mv(COLOR, 0x0D0D) + END
    ops = [("l", COP2LC, "a")] + strobe1() + [("w", COPJMP1, 0), ("w", COPJMP2, 0)]
    return run_cases(c, [Case("dhook", {"a": a}, ops, hook=True),
                         Case("dhook0", {"a": a}, ops, hook=False)])


def leg_nohook(c: CopRig) -> list[str]:
    """First leg of a fresh session: no disp_init ever ran, cop_dhook is as
    the boot left it.  A run (strobe-then-enable) must not call through it:
    the PRG halts cleanly and the copper's result is there."""
    f: list[str] = []
    if c.dhook == DHOOK_07E8:
        # the round-0 vector outside the PRG: VICE powers it up as $0000,
        # real RAM holds garbage; model that ($FFFF) so the red build shows
        # what a U64 would do.  The BSS vector is cleared by the boot.
        c.rig.w(DHOOK_07E8, b"\xff\xff")
    v = c.rig.r(c.dhook, 2)
    print(f"    cop_dhook at ${c.dhook:04X} = {v.hex()} before the run")
    if v != b"\0\0" and c.dhook != DHOOK_07E8:
        f.append(f"nohook: cop_dhook ${v.hex()} after boot (BSS must be clear)")
    a = mv(COLOR, 0x0E0E) + END
    return f + run_cases(c, [Case("nohook", {"a": a}, strobe1(), hook=None)])


def enable_cases() -> list[Case]:
    a = mv(COLOR, 0x0101) + mv(COLOR + 2, 0x0202) + END
    cs = []
    # a strobe with copper DMA off only loads the PC: nothing runs
    cs.append(Case("offstrobe", {"a": a}, [("l", COP1LC, "a"), ("w", COPJMP1, 0)], hook=True))
    cs.append(Case("offstrobe2", {"a": a}, [("l", COP1LC, "a"), ("w", COPJMP1, 0),
                                            ("w", DMACON, 0x8200), ("w", DMACON, 0x8100)],
                   hook=True))
    # strobe, then DMAEN, then COPEN: the run comes at the turn-on; DMACON
    # writes with copper DMA already on start nothing
    cs.append(Case("enable", {"a": a}, [("l", COP1LC, "a"), ("w", COPJMP1, 0),
                                        ("w", DMACON, 0x8200), ("w", DMACON, 0x8080),
                                        ("w", DMACON, 0x8280), ("w", DMACON, 0x8001)],
                   hook=True))
    # copper DMA on before any PC was loaded: nothing runs until the strobe
    # (W10-V's pf_static order: COP1LC, DMACON, COPJMP1, ..., COPJMP1)
    cs.append(Case("nopc", {"a": a}, [("l", COP1LC, "a"), ("w", DMACON, 0x8380),
                                      ("w", COLOR, 0x0F0F), ("w", COPJMP1, 0),
                                      ("w", COLOR, 0x0F0F), ("w", COPJMP1, 0)],
                   hook=True))
    # off and on again: the copper stays on its end marker (no rerun of the
    # list); a strobe with copper DMA on restarts it
    cs.append(Case("reenable", {"a": a}, strobe1() + [("w", COLOR, 0x0F0F),
                                                      ("w", DMACON, 0x0080),
                                                      ("w", DMACON, 0x8080),
                                                      ("w", COPJMP1, 0)], hook=True))
    # ... and on a danger MOVE
    cs.append(Case("redanger", {"a": mv(COLOR, 0x0303) + mv(0x07E, 0) + END},
                   strobe1() + [("w", COLOR, 0x0F0F), ("w", DMACON, 0x0200),
                                ("w", DMACON, 0x8200)], hook=True))
    # the copper turning its own DMA off and on starts no nested run
    cs.append(Case("copdma", {"a": mv(DMACON, 0x0080) + mv(DMACON, 0x8080)
                              + mv(COLOR, 0x0404) + mv(DMACON, 0x0200) + mv(DMACON, 0x8200)
                              + END}, strobe1(), hook=True))
    return cs


def leg_enable(c: CopRig) -> list[str]:
    return run_cases(c, enable_cases())


def prog_frame(lst: int) -> list[int]:
    """COP1LC := lst, COPJMP1, DMAEN+COPEN (one run), 2 VERTBs; edit the
    list, 3 VERTBs;
    COPEN off, edit again, 3 VERTBs; d3 := $600D, HALT (a timed-out
    VERTB wait HALTs early, d3 unset)."""
    g = G()
    g(0x7A00)                                       # moveq #0,d5
    g(0x23FC, lst >> 16, lst & 0xFFFF, *g.abs(CUSTOM + COP1LC))
    mv_imm_abs_w(g, 0, CUSTOM + COPJMP1)            # PC := COP1LC (DMA off)
    mv_imm_abs_w(g, 0x8280, CUSTOM + DMACON)        # turn-on: one run
    for i in range(2):
        run_mach.wait_vertb(g, f"a{i}")
    mv_imm_abs_w(g, 0x0ABC, lst + 2)
    for i in range(3):
        run_mach.wait_vertb(g, f"b{i}")
    mv_imm_abs_w(g, 0x0080, CUSTOM + DMACON)
    mv_imm_abs_w(g, 0x0DEF, lst + 2)
    for i in range(3):
        run_mach.wait_vertb(g, f"c{i}")
    run_mach.mv_imm_d_w(g, 0x600D, 3)              # d3: every wait came
    g(HALT)
    return g.words()


def leg_frame(c: CopRig) -> list[str]:
    f: list[str] = []
    rig = c.rig
    if not rig.mach:
        return ["frame: no mach_run"]
    c.reset()
    c.hook(True)
    if c.cop:                                    # unregistered: the guest's
        rig.w(c.lab("cop_ok"), b"\0")            # first display write must
        rig.w(c.lab("mach_fhook"), b"\0\0")      # register the frame hook
    lst = mv(COLOR, 0x0123) + END
    n0 = len(prog_frame(0))
    chunks, (pc, la) = build_image(prog_frame(BASE + 2 * n0), lst)
    assert la == BASE + 2 * n0
    rig.sess.stage(chunks)
    rig.w(run_mach.DRV_ORG, rig.drv)
    res = rig.run(pc, s=2)
    if not run_mach.clean(res, "frame", f):
        return f
    if res["d"][3] & 0xFFFF != 0x600D:
        f.append("frame: a VERTB wait timed out")
    st = c.state()
    col = struct.unpack_from("<H", st["disp"], cr.DISP_COLOR)[0]
    if col != 0x0ABC:
        f.append(f"frame: color00 ${col:04X}, want $0ABC (the edited list, "
                 "not the one after COPEN was cleared)")
    dma = struct.unpack_from("<H", st["disp"], cr.DISP_DMACON)[0]
    if dma != 0x0200:
        f.append(f"frame: dmacon ${dma:04X}, want $0200")
    if not c.cop:
        return f
    fr = res["frames"]
    if st["cop_nfr"] != fr & 0xFFFF:
        f.append(f"frame: cop_nfr {st['cop_nfr']} != mach_frames {fr}")
    if not 5 <= st["cop_nrun"] < st["cop_nfr"]:
        f.append(f"frame: cop_nrun {st['cop_nrun']} (want >= 5 runs with COPEN, "
                 f"none after it, of {st['cop_nfr']} frames)")
    if c.hookn() != st["cop_nfr"] + 1:
        f.append(f"frame: cop_dhook called {c.hookn()}x, want once per frame "
                 f"({st['cop_nfr']}) + the turn-on run")
    if VERBOSE:
        print(f"    frame: frames {fr} nfr {st['cop_nfr']} nrun {st['cop_nrun']}")
    return f


LEGS = {"nohook": leg_nohook, "sync": leg_sync, "model": leg_model,
        "cpu": leg_cpu, "dhook": leg_dhook, "enable": leg_enable,
        "frame": leg_frame}


def run_all(prg: str, legs: list[str]) -> list[str]:
    def body(rig: run_mach.Rig) -> list[str]:
        c = CopRig(rig)
        fails: list[str] = []
        for name in legs:
            try:
                f = LEGS[name](c)
            except Exception as e:           # a hung/crashed leg is a failure
                f = [f"{name}: {type(e).__name__}: {e}"]
                print(f"[{name}] FAIL")
                for x in f:
                    print(f"    {x}")
                fails += f
                break                        # the machine state is unknown
            print(f"[{name}] {'PASS' if not f else 'FAIL'}")
            for x in f[:40]:
                print(f"    {x}")
            fails += f
        return fails
    return run_mach.session(prg, body)


# --------------------------------------------------------------------------
# Teeth: source mutants (docs/WAVE10_BRIEF.md C2), one per rule
# --------------------------------------------------------------------------

COP = "runtime/ops/cop.s"
MUTANTS = {
    # a WAIT stops the copper instead of being satisfied at once
    "waitstop": (COP, [("        bcc nxt                 ; WAIT: satisfied at once\n",
                        "        bcc stop                ; MUTANT\n")]),
    # the danger threshold is $40 without CDANG
    "danger": (COP, [("t80:    lda #$80\n", "t80:    lda #$40\n")]),
    # CDANG ignored: the threshold stays $80
    "cdang": (COP, [("        lda #$40\n        bne thr                 ; always\n",
                     "        lda #$80\n        bne thr                 ; always\n")]),
    # the end marker is not recognised (the copper runs on)
    "endmark": (COP, [("        cmp #$FE\n        bne wait\n",
                       "        cmp #$FE\n        jmp wait\n")]),
    # the end test ignores w2's high byte ($FFFF,$xxFE ends)
    "endloose": (COP, [("        and m_val+1\n", "")]),
    # a CPU COPJMP2 strobe starts at COP1LC
    "jmp2lc1": (COP, [("        ldx #COP_COP2LC\n        jmp cop_strobe\n",
                       "        ldx #COP_COP1LC\n        jmp cop_strobe\n")]),
    # COPJMP2 inside a list jumps to COP1LC
    "ljmp2": (COP, [("jmp2:   ldx #COP_COP2LC\n", "jmp2:   ldx #COP_COP1LC\n")]),
    # gen not bumped
    "nogen": (COP, [("        inc cop_sh+DISP_GEN\n", "")]),
    # gen bumped on a write that changes nothing
    "genall": (COP, [("        cmp cop_sh,y\n        beq same\nchg:",
                      "        cmp cop_sh,y\n        nop\n        nop\nchg:")]),
    # the frame hook is never called
    "nofhook": ("runtime/ops/mach.s",
                [("        lda mach_fhook+1        ; W10-P cop: the frame hook (the copper),\n",
                  "        lda #0                  ; MUTANT\n")]),
    # the frame copper ignores COPEN
    "copen": (COP, [("        and #$80                ; COPEN (bit 7)\n        beq off\n", "")]),
    # a strobe with copper DMA off runs the copper (ungated)
    "ungated": (COP, [("        bne cop_sync\n        rts\n", "        jmp cop_sync\n")]),
    # a DMACON write turning copper DMA on does not run it
    "noenable": (COP, [("        lda cop_was             ; a CPU write turning copper DMA on runs it\n"
                        "        bne done\n", "        jmp done\n")]),
    # the copper's own DMACON turn-on starts a nested run
    "copself": (COP, [("        lda cop_busy            ; (never one from the copper itself)\n"
                       "        bne done\n", "")]),
    # a DMACON turn-on runs although no PC was ever loaded (from 0)
    "nopcok": (COP, [("        lda cop_pcok            ; no PC loaded since cop_reset: nothing to run\n"
                      "        beq done\n", "")]),
    # a copper stopped at its end marker does not stay on it
    "pcstay": (COP, [("        cmp #COP_LIMITED\n        bcs done                ; limit / I/O bank: "
                      "the PC stays where it is\n", "        jmp done\n")]),
    # SKIP not counted
    "skipcnt": (COP, [("        inc cop_nskip           ; SKIP: counted, no-op (non-strict model)\n",
                       "        nop                     ; MUTANT\n")]),
    # DMACON stored raw (no SET/CLR)
    "dmaraw": (COP, [("        bit cop_v+1\n        bmi set\n",
                      "        jmp hi0\n")]),
    # the trap state is not restored after a synchronous run
    "nosave": (COP, [("        ldx #3\nrs2:    lda cop_sv+6,x\n        sta m_val,x\n",
                      "        ldx #3\nrs2:    lda cop_sv+6,x\n        nop\n        nop\n")]),
    # the display hook is not called after a strobe run
    "nodhook": (COP, [("        jsr cop_go\n        jsr cop_dhk\n", "        jsr cop_go\n")]),
    # the step limit is one short
    "limit": (COP, [("COP_LIMIT    = 4000", "COP_LIMIT    = 3999")]),
    # BPLxPTH and PTL swapped (C = PTL taken the wrong way)
    "ptswap": (COP, [("        bcs ptl\n", "        bcc ptl\n")]),
}
# "dmaraw" needs a label for its jump target
MUTANTS["dmaraw"][1].append(("hi:     and #$7F\n", "hi:     and #$7F\nhi0:\n"))

MUTANT_LEGS = {"waitstop": ["model"], "danger": ["model"], "cdang": ["model"],
               "endmark": ["model"], "endloose": ["model"], "jmp2lc1": ["model"],
               "ljmp2": ["model"], "nogen": ["cpu"], "genall": ["cpu"],
               "nofhook": ["frame"], "copen": ["frame"], "skipcnt": ["model"],
               "ungated": ["enable"], "noenable": ["sync"], "copself": ["enable"],
               "pcstay": ["enable"], "nopcok": ["enable"],
               "dmaraw": ["cpu"], "nosave": ["cpu"], "nodhook": ["dhook"],
               "limit": ["model"], "ptswap": ["sync"]}


def build_mutant(name: str, out_dir: str) -> str:
    saved = dict(run_mach.MUTANTS)
    try:
        run_mach.MUTANTS.clear()
        run_mach.MUTANTS[name] = MUTANTS[name]
        return run_mach.build_mutant(name, out_dir)
    finally:
        run_mach.MUTANTS.clear()
        run_mach.MUTANTS.update(saved)


def teeth(prg: str, only: list[str] | None) -> int:
    bdir = os.path.relpath(os.path.dirname(os.path.abspath(prg)), ROOT)
    bad = []
    names = only or list(MUTANTS)
    for name in names:
        mprg = build_mutant(name, os.path.join(bdir, "cop-mut", name))
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
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    VERBOSE = a.verbose
    if a.mutants:
        return teeth(a.prg, a.only.split(",") if a.only else None)
    fails = run_all(a.prg, a.legs.split(","))
    print(f"run_cop: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
