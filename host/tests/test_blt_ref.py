"""W11-B: chipset_ref.Blitter.write_bdat, the CPU's BLTBDAT write (supervisor
ruling, Wave 11: FS-UAE wins).  The CPU word is shifted alone by the BSH in
BLTCON1 at the write: descending a left shift, ascending a right shift; the
previous B word (CPU-written or fetched) never enters.  FS-UAE evidence:
tests/chip_oracle.py cases bdat_desc_bsh, bdat_asc_prev_cpu, bdat_after_bdma,
b_stale_hold."""
from __future__ import annotations

import pytest

from host import chipset_ref as cr


def regs(con1: int) -> list[int]:
    r = [0] * 256
    r[0x042 >> 1] = con1
    return r


@pytest.mark.parametrize("bsh", range(16))
@pytest.mark.parametrize("prev", [0x0000, 0xF00F, 0xFFFF])
def test_cpu_bdat_shifted_alone(bsh, prev):
    for desc in (0, 2):
        b = cr.Blitter(cr.Ram(0x1000))
        r = regs(bsh << 12 | desc)
        b.write_bdat(r, prev)               # an earlier CPU write
        b.write_bdat(r, 0x8001)
        want = (0x8001 << bsh) & 0xFFFF if desc else 0x8001 >> bsh
        assert b.bhold == want, (bsh, prev, desc)


def test_cpu_bdat_after_a_b_dma_blit():
    """bdat_after_bdma: a B-DMA blit leaves its last word behind; the CPU
    write that follows is still shifted alone."""
    m = cr.Ram(0x1000)
    m.w_block(0x100, b"\xAB\xCD\x12\x34\xFF\xFF")
    b = cr.Blitter(m)
    r = regs(0x5000)
    r[0x040 >> 1] = 0x05CC
    r[0x04C >> 1], r[0x04E >> 1] = 0, 0x100
    r[0x054 >> 1], r[0x056 >> 1] = 0, 0x200
    b.blit(r, (3 << 6) | 1)
    b.write_bdat(r, 0x8001)
    assert b.bhold == 0x0400


def test_b_hold_after_b_dma_blit_is_last_shifted_b():
    """b_stale_hold (MATCH before and after the fix): with B DMA off the
    next blit uses the last shifted B of the previous blit."""
    m = cr.Ram(0x1000)
    m.w_block(0x100, b"\xAB\xCD\x12\x34\xFF\xFF")
    b = cr.Blitter(m)
    r = regs(0x5000)
    r[0x040 >> 1] = 0x05CC
    r[0x04C >> 1], r[0x04E >> 1] = 0, 0x100
    r[0x054 >> 1], r[0x056 >> 1] = 0, 0x200
    b.blit(r, (3 << 6) | 1)
    assert b.bhold == ((0x1234 << 16 | 0xFFFF) >> 5) & 0xFFFF
