"""W10-P (S3.5): the copper/display reference additions in host/chipset_ref
(CopperAt, DisplayState, DisplayChipset), the equates of runtime/ops/cop.s
and runtime/rc/ram.inc they mirror, and the tests/run_cop.py fixtures.
Hardware-free.
"""

from __future__ import annotations

import os
import random
import re

import pytest

from host import chipset_ref as cr
from tests import run_cop

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = open(os.path.join(ROOT, "runtime", "ops", "cop.s")).read()
RAM = open(os.path.join(ROOT, "runtime", "rc", "ram.inc")).read()


def equ(text: str, name: str) -> int:
    m = re.search(rf"^{name}\s*=\s*(\$[0-9A-Fa-f]+|\d+)", text, re.M)
    assert m, name
    v = m.group(1)
    return int(v[1:], 16) if v.startswith("$") else int(v)


@pytest.mark.parametrize("name", ["DISP_GEN", "DISP_DMACON", "DISP_BPLCON",
                                  "DISP_BPLPT", "DISP_BPLMOD", "DISP_DIW",
                                  "DISP_COLOR", "DISP_VALID", "DISP_SZ", "COP_VALID",
                                  "COP_LIMIT", "COP_END",
                                  "COP_DANGER", "COP_LIMITED", "COP_IOBANK"])
def test_cop_equates_match_model(name):
    assert equ(SRC, name) == getattr(cr, name)


def test_c1_layout():
    """WAVE10_BRIEF C1: the field offsets are the brief's table."""
    assert (cr.DISP_GEN, cr.DISP_DMACON, cr.DISP_BPLCON, cr.DISP_BPLPT,
            cr.DISP_BPLMOD, cr.DISP_DIW, cr.DISP_COLOR) == (0, 1, 3, 9, 27, 31, 39)
    assert cr.DISP_VALID == cr.DISP_COLOR + 64          # appended (supervisor)
    assert cr.DISP_SZ == cr.DISP_VALID + 1
    assert cr.COP_LIMIT == cr.Copper.run.__defaults__[0]


def test_rc_claims_match_runner():
    assert equ(RAM, "disp_st") == run_cop.DISP_ST
    assert run_cop.DISP_ST + cr.DISP_SZ <= 0xB900        # the claimed page
    assert "cop_dhook" not in RAM                        # BSS in cop.s
    assert re.search(r"^cop_dhook:\s*\.res 2", SRC, re.M)


def rand_list(rng: random.Random, n: int) -> list[int]:
    w = []
    for _ in range(n):
        k = rng.random()
        if k < 0.6:
            w += [rng.randrange(0x40, 0x200) & 0x1FE, rng.randrange(0x10000)]
        elif k < 0.7:
            w += [rng.choice((0x088, 0x08A)), 0]
        elif k < 0.85:
            w += [rng.randrange(0x10000) | 1, rng.randrange(0x10000) & 0xFFFE]
        else:
            w += [rng.randrange(0x10000) | 1, rng.randrange(0x10000) | 1]
    return w + [0xFFFF, 0xFFFE]


@pytest.mark.parametrize("seed", range(40))
def test_run_at_cop1lc_equals_run(seed):
    """CopperAt.run_at from COP1LC is Copper.run (non-strict) below 2 MB."""
    rng = random.Random(seed)
    a, b = 0x1000 + 4 * rng.randrange(64), 0x8000 + 4 * rng.randrange(64)
    la, lb = rand_list(rng, 30), rand_list(rng, 30)
    outs = []
    for cop in (cr.Copper(strict=False), cr.CopperAt()):
        mem = cr.Ram()
        for base, lst in ((a, la), (b, lb)):
            for i, x in enumerate(lst):
                mem.w16(base + 2 * i, x)
        regs = [0] * 256
        regs[0x080 >> 1], regs[0x082 >> 1] = a >> 16, a & 0xFFFF
        regs[0x084 >> 1], regs[0x086 >> 1] = b >> 16, b & 0xFFFF
        regs[0x02E >> 1] = rng.choice((0, 2)) if not outs else outs[0][3]
        log = []
        if isinstance(cop, cr.CopperAt):
            n = cop.run_at(mem, regs, lambda r, v: log.append((r, v)), a)
        else:
            n = cop.run(mem, regs, lambda r, v: log.append((r, v)))
        outs.append((n, log, cop.skips, regs[0x02E >> 1]))
    assert outs[0] == outs[1]


def test_run_at_reasons():
    mem = cr.Ram()
    regs = [0] * 256
    c = cr.CopperAt()
    for i, x in enumerate([0x0180, 1, 0xFFFF, 0xFFFE]):
        mem.w16(0x100 + 2 * i, x)
    assert c.run_at(mem, regs, lambda r, v: None, 0x100) == 2 and c.why == cr.COP_END
    mem.w16(0x200, 0x0040)
    assert c.run_at(mem, regs, lambda r, v: None, 0x200) == 1 and c.why == cr.COP_DANGER
    regs[0x02E >> 1] = 2
    mem.w16(0x204, 0x003E)
    assert c.run_at(mem, regs, lambda r, v: None, 0x200) == 2 and c.why == cr.COP_DANGER
    regs[0x080 >> 1], regs[0x082 >> 1] = 0, 0x300
    mem.w16(0x300, 0x0088)
    assert c.run_at(mem, regs, lambda r, v: None, 0x300) == cr.COP_LIMIT
    assert c.why == cr.COP_LIMITED
    assert c.run_at(mem, regs, lambda r, v: None, 0xDFF180) == 0
    assert c.why == cr.COP_IOBANK
    # 24-bit pointers (the model's Copper.run masks with $1FFFFE)
    mem24 = cr.Ram(0x1000000)
    for i, x in enumerate([0x0182, 0x0ABC, 0xFFFF, 0xFFFE]):
        mem24.w16(0x400000 + 2 * i, x)
    log = []
    assert c.run_at(mem24, regs, lambda r, v: log.append((r, v)), 0x400001) == 2
    assert log == [(0x182, 0x0ABC)]


def test_display_state_fields_and_gen():
    d = cr.DisplayState(gen=250)
    d.write(0x180, 0x0123)                       # change: +1
    d.write(0x180, 0x0123)                       # same: no bump
    d.write(0x0E4, 0x1234)                       # BPL2PTH: low byte only
    d.write(0x0E6, 0x5678)
    d.write(0x096, 0x8281)                       # DMACON SET
    d.write(0x096, 0x0001)                       # CLR
    d.write(0x096, 0x0000)                       # CLR nothing: no bump
    d.write(0x10A, 0xFFFE)
    d.write(0x094, 0x00D0)
    d.write(0x106, 0x1111)                       # BPLCON3: not a field
    d.write(0x1C0, 0x2222)                       # past COLOR31
    b = d.block()
    assert b[0] == (250 + 7) & 0xFF
    assert b[cr.DISP_COLOR:cr.DISP_COLOR + 2] == bytes([0x23, 0x01])
    assert b[cr.DISP_BPLPT + 3:cr.DISP_BPLPT + 6] == bytes([0x78, 0x56, 0x34])
    assert b[cr.DISP_DMACON:cr.DISP_DMACON + 2] == bytes([0x80, 0x02])
    assert b[cr.DISP_BPLMOD + 2:cr.DISP_BPLMOD + 4] == bytes([0xFE, 0xFF])
    assert b[cr.DISP_DIW + 6:cr.DISP_DIW + 8] == bytes([0xD0, 0x00])
    assert sum(b[1:]) == sum((0x23, 0x01, 0x78, 0x56, 0x34, 0x80, 0x02, 0xFE,
                              0xFF, 0xD0, cr.COP_VALID))
    d.reset()
    assert d.block() == (bytes([(250 + 8) & 0xFF]) + bytes(cr.DISP_VALID - 1)
                         + bytes([cr.COP_VALID]))


def test_display_chipset_triggers():
    mem = cr.Ram()
    for i, x in enumerate([0x0180, 0x0AAA, 0xFFFF, 0xFFFE]):
        mem.w16(0x1000 + 2 * i, x)
    for i, x in enumerate([0x0180, 0x0BBB, 0xFFFF, 0xFFFE]):
        mem.w16(0x2000 + 2 * i, x)
    m = cr.DisplayChipset(mem)
    m.cpu_write(0x082, 0x1000)
    m.cpu_write(0x086, 0x2000)
    assert m.frame() == 0 and m.runs == 0        # DMACON off: no frame run
    m.cpu_write(0x08A, 0)                        # strobe, DMA off: PC only
    assert m.runs == 0 and m.cop.pc == 0x2000
    m.cpu_write(0x096, 0x8200)                   # DMAEN without COPEN
    assert m.runs == 0
    m.cpu_write(0x096, 0x8080)                   # copper DMA on: runs COP2LC's
    assert m.runs == 1 and m.regs[0x180 >> 1] == 0x0BBB
    assert (m.cop.why, m.cop.pc) == (cr.COP_END, 0x2004)  # stays on the end
    m.cpu_write(0x096, 0x8280)                   # already on: nothing
    assert m.runs == 1
    m.cpu_write(0x088, 0)                        # strobe with DMA on: runs
    assert m.runs == 2 and m.regs[0x180 >> 1] == 0x0AAA
    m.cpu_write(0x180, 0)
    m.cpu_write(0x096, 0x0080)
    m.cpu_write(0x096, 0x8080)                   # re-enable: the end again
    assert m.runs == 3 and m.n == 1 and m.regs[0x180 >> 1] == 0
    assert m.frame() == 2 and m.regs[0x180 >> 1] == 0x0AAA and m.runs == 4


def test_turn_on_without_a_pc_runs_nothing():
    m = cr.DisplayChipset(cr.Ram())
    m.cpu_write(0x096, 0x8280)
    assert m.runs == 0 and not m.loaded


def test_copper_own_dmacon_starts_nothing():
    mem = cr.Ram()
    for i, x in enumerate([0x0096, 0x0080, 0x0096, 0x8080, 0x0180, 0x0444,
                           0xFFFF, 0xFFFE]):
        mem.w16(0x1000 + 2 * i, x)
    m = cr.DisplayChipset(mem)
    m.cpu_write(0x082, 0x1000)
    m.cpu_write(0x088, 0)
    m.cpu_write(0x096, 0x8280)
    assert m.runs == 1 and m.n == 4 and m.regs[0x180 >> 1] == 0x0444


def test_runner_cases_build():
    """Every run_cop case assembles and its model runs (no device)."""
    cases = (run_cop.model_cases() + run_cop.cpu_cases() + run_cop.enable_cases())
    assert len({c.name for c in cases}) == len(cases)
    runs = {}
    for c in cases:
        chunks, pc, addr = c.image()
        runs[c.name] = c.model(addr, 0).runs
    assert runs["offstrobe"] == runs["offstrobe2"] == 0
    assert runs["nopc"] == 2
    assert runs["enable"] == 1 and runs["reenable"] == 3 and runs["copdma"] == 1
    assert runs["cpujmp2"] == 2 and runs["cpulong"] == 4
    assert all(runs[c.name] >= 1 for c in run_cop.model_cases())
    lim = next(c for c in cases if c.name == "limit")
    m = lim.model(lim.image()[2], 0)
    assert (m.n, m.cop.why) == (cr.COP_LIMIT, cr.COP_LIMITED)
