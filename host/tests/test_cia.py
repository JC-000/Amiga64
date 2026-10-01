"""S3.4 (W9-C): the properties runtime/ops/cia.s's lazy timing relies on.

cia.s applies guest time to a CIA only when the chip is touched (a register
access or an armed event): it runs chipset_ref.CIA.eclock(now - last) at
whatever points that happens.  That is only correct if eclock is
split-invariant: eclock(a); eclock(b) == eclock(a + b) for the state and the
ICR data bits.  The CIA-B TOD line grid and the alarm deadline formula are
pinned here too (docs/CONTRACTS.md "CIA-A/B (S3.4)").
"""

import random

import pytest

from host.chipset_ref import (CIA, CIAA_PRA_IN, MACH_FRAME_E, cia_line_e,
                              cia_lines, cia_tod_kprime)

FIELDS = ("ta", "tb", "ta_latch", "tb_latch", "cra", "crb", "icr_data",
          "icr_mask", "tod", "alarm", "tod_halt")


def state(c: CIA) -> tuple:
    return tuple(getattr(c, f) for f in FIELDS)


def random_cia(rng: random.Random) -> CIA:
    c = CIA()
    c.ta_latch = rng.choice([0, 1, 2, rng.randrange(300), rng.randrange(65536), 0xFFFF])
    c.tb_latch = rng.choice([0, 1, 3, rng.randrange(300), rng.randrange(65536), 0xFFFF])
    c.ta = rng.randrange(c.ta_latch + 1) if rng.random() < 0.8 else rng.randrange(65536)
    c.tb = rng.randrange(c.tb_latch + 1) if rng.random() < 0.8 else rng.randrange(65536)
    c.cra = rng.choice([0, 1, 9, 0x21, 1, 9]) | rng.choice([0, 0x40, 0x80])
    c.crb = rng.choice([0, 1, 9, 0x21, 0x41, 0x49, 0x61, 0x69]) | rng.choice([0, 0x80])
    c.icr_mask = rng.randrange(32)
    return c


@pytest.mark.parametrize("seed", range(40))
def test_eclock_split_invariant(seed):
    rng = random.Random(seed)
    for _ in range(200):
        c = random_cia(rng)
        parts = [rng.choice([0, 1, 2, rng.randrange(400), rng.randrange(1 << 18),
                             rng.randrange(1 << 26)]) for _ in range(rng.randrange(1, 6))]
        whole = CIA()
        whole.__dict__.update(c.__dict__)
        whole.eclock(sum(parts))
        for p in parts:
            c.eclock(p)
        assert state(c) == state(whole), (seed, parts)


def test_eclock_underflow_count_matches_steps():
    """TB counting TA underflows (INMODE 2) sees every one of them."""
    c = CIA()
    c.ta_latch, c.ta, c.cra = 9, 9, 1           # underflow every 10 ticks
    c.tb_latch, c.tb, c.crb = 0xFFFF, 0xFFFF, 0x41
    c.eclock(10 * 1000)
    assert c.tb == 0xFFFF - 1000
    c2 = CIA()
    c2.__dict__.update({**c.__dict__, "tb": 0xFFFF, "ta": 9})
    for _ in range(1000):
        c2.eclock(10)
    assert c2.tb == c.tb


@pytest.mark.parametrize("seed", range(20))
def test_tod_ticks_equals_loop(seed):
    rng = random.Random(seed)
    for _ in range(300):
        a, b = CIA(), CIA()
        a.tod = b.tod = rng.randrange(1 << 24)
        a.alarm = b.alarm = rng.choice([a.tod, (a.tod + rng.randrange(1, 600)) & 0xFFFFFF,
                                        rng.randrange(1 << 24)])
        a.tod_halt = b.tod_halt = rng.random() < 0.1
        n = rng.randrange(700)
        a.tod_tick(n)
        b.tod_ticks(n)
        assert (a.tod, a.icr_data) == (b.tod, b.icr_data)


def test_tod_kprime_wraps():
    assert cia_tod_kprime(5, 6) == 1
    assert cia_tod_kprime(5, 5) == 1 << 24       # equal now: a full wrap
    assert cia_tod_kprime(0xFFFFFF, 0) == 1


def test_line_grid():
    assert cia_lines(MACH_FRAME_E) == 312       # 312.5 lines per 50 Hz frame
    assert cia_lines(709379) == 15625           # 15625 Hz at 709379 E/s
    for k in range(2000):
        e = cia_line_e(k)
        assert cia_lines(e) >= k and (e == 0 or cia_lines(e - 1) < k)


def test_alarm_deadline_formula():
    """cia.s alarmdl: deadline = T + ceil((227 k' - ph) / 5) with ph the
    grid phase at T, i.e. the first E at which lines(E) = lines(T) + k'."""
    rng = random.Random(7)
    for _ in range(3000):
        t = rng.randrange(1 << 30)
        k = rng.randrange(1, 1 << 24)
        ph = (5 * t) % 227
        dl = t + -(-(227 * k - ph) // 5)
        assert dl == cia_line_e(cia_lines(t) + k)


def test_pra_input_pins():
    from host.chipset_ref import Chipset, Ram
    cs = Chipset(Ram(0x1000))
    assert cs.ciaa.read(0) == CIAA_PRA_IN == 0xEB
    assert cs.ciab.read(0) == 0xFF
