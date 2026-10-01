"""W9-I (S3.3): the interrupt-level tables of runtime/ops/mach.s against
host/chipset_ref (IRQ_LEVELS, Paula.level), and the run_irq guest fixtures.

mach_lvl maps INTENA & INTREQ to the IPL Paula presents: bits 8-13 by
three tests in code (bit 13 -> 6, 11-12 -> 5, 8-10 -> 4), bits 0-7 by the
mach_lvlo table (levels 1-4).  mach_wk / mach_ablo / mach_abhi give the
INTREQ bits whose level is above an IPL of 0-7.  Both are mirrored here
from the source text and compared with the reference model.  Hardware-free.
"""

from __future__ import annotations

import os
import random
import re

import pytest

from host import chipset_ref
from tests import run_irq

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = open(os.path.join(ROOT, "runtime", "ops", "mach.s")).read()


def table(name: str) -> list[int]:
    m = re.search(rf"^{name}:\s*\.byte\s+(.*)$", SRC, re.M)
    assert m, name
    return [int(x.strip().lstrip("$"), 16) for x in m.group(1).split(",")]


LVLO = table("mach_lvlo")
ABLO, ABHI = table("mach_ablo"), table("mach_abhi")


def proc(name: str) -> str:
    m = re.search(rf"^\.proc {name}\n(.*?)^\.endproc", SRC, re.M | re.S)
    assert m, name
    return m.group(1)


def mach_lvl(intena: int, intreq: int) -> int:
    """Python mirror of mach_lvl (see the module docstring)."""
    if not intena & 0x4000:
        return 0
    hi = (intena >> 8) & (intreq >> 8) & 0x3F
    lo = intena & intreq & 0xFF
    if hi:
        return 6 if hi & 0x20 else 5 if hi & 0x18 else 4
    for x in (4, 3, 2, 1):
        if lo & LVLO[x - 1]:
            return x
    return 0


def test_mach_lvl_code_shape() -> None:
    body = proc("mach_lvl")
    for line in ("and #$40", "and #$3F", "and #$20", "and #$18",
                 "lda #4", "lda #6", "lda #5", "and mach_lvlo-1,x", "ldx #4"):
        assert line in body, line


@pytest.mark.parametrize("bit", range(15))
def test_single_bits(bit: int) -> None:
    p = chipset_ref.Paula()
    p.write(0x09A, 0xC000 | 1 << bit)
    p.write(0x09C, 0x8000 | 1 << bit)
    assert mach_lvl(p.intena, p.intreq) == p.level()


def test_random_masks_match_paula() -> None:
    rnd = random.Random(0x5333)
    for _ in range(20000):
        p = chipset_ref.Paula()
        p.intena = rnd.getrandbits(15)
        p.intreq = rnd.getrandbits(15)
        assert mach_lvl(p.intena, p.intreq) == p.level(), (hex(p.intena), hex(p.intreq))


@pytest.mark.parametrize("ipl", range(8))
def test_above_ipl_tables(ipl: int) -> None:
    want = 0
    for b, lv in chipset_ref.IRQ_LEVELS:
        if lv > ipl:
            want |= 1 << b
    assert ABHI[ipl] << 8 | ABLO[ipl] == want


def test_delivery_rule_in_source() -> None:
    """level > I2-I0 (not >=), vector 24 + level, I := level after the
    frame, INTEN gates everything (mach_lvl), exception mode only."""
    body = proc("mach_dlv")
    assert "lda st_exl" in body and "beq none" in body
    assert re.search(r"cmp mach_lv\n\s+bcs none", body)
    assert "adc #MACH_AUTOVEC" in body and "MACH_AUTOVEC  = 24" in SRC
    assert re.search(r"and #\$F8\n\s+ora mach_lv\n\s+sta r_sr\+1", body)


def test_setclr_fixture_matches_reference() -> None:
    assert run_irq.setclr_want() == [0x4020, 0x4000, 0x2008, 0x2000, 0x7FFF,
                                     0x5FDF, 0x20A0, 0x0000]


def test_level_fixture_covers_every_bit() -> None:
    g, nexts = run_irq.prog_levels()
    assert len(nexts) == 14
    assert sorted(run_irq.LEVEL_OF) == list(range(14))
