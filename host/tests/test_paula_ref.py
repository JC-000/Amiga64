"""W15-P: host/paula_ref.py against HRM Paula rules and the sampler's
measured timing law (docs/HARDWARE.md "U64 sampler")."""

import pytest

from host import paula_ref as P
from host.paula_ref import Paula, AUD_BASE, DMACON, DMAEN, REU_BASE

LCH, LCL, LEN, PER, VOL = (AUD_BASE + r for r in (0, 2, 4, 6, 8))


def reg(x, r):
    return r + 16 * x


def setup(p, x, lc, ln, per, vol, now=0):
    p.write(now, reg(x, LCH), lc >> 16)
    p.write(now, reg(x, LCL), lc & 0xFFFF)
    p.write(now, reg(x, LEN), ln)
    p.write(now, reg(x, PER), per)
    p.write(now, reg(x, VOL), vol)


# --- the sampler mapping -----------------------------------------------------

@pytest.mark.parametrize("per", [113, 124, 214, 254, 428, 508, 856, 1016, 1712, 3424])
def test_rate_matches_paula_period(per):
    """The integer RATE is the nearest sampler step to Paula's sample time:
    within half a 160 ns step, i.e. < 0.25 % at the Amiga minimum period."""
    ns = P.sample_ns(P.sampler_rate(per))
    assert abs(ns - P.paula_ns(per)) <= 80.0 + 1e-6
    assert abs(ns / P.paula_ns(per) - 1) < 0.0025


def test_rate_is_nearest():
    """Nearest step everywhere; only exact ties may round either way."""
    for per in range(2, 0x10000):
        r = P.sampler_rate(per)
        exact = (P.paula_ns(per) - 118) / 160 - 1
        if exact > 0xFFFF:
            assert r == 0xFFFF, per                 # clamped (period >= 37193)
            continue
        assert abs(r - exact) <= 0.502, per


def test_rate_clamps():
    assert P.sampler_rate(0) == 0
    assert P.sampler_rate(1) == 0
    assert P.sampler_rate(0xFFFF) == 0xFFFF


@pytest.mark.parametrize("v,eff,hw", [
    (0, 0, 0), (1, 1, 1), (32, 32, 32), (63, 63, 63), (64, 64, 63),
    (0x7F, 64, 63), (0x41, 64, 63), (0xFF3F, 63, 63), (0x80, 0, 0)])
def test_volume(v, eff, hw):
    assert P.eff_vol(v) == eff
    assert P.hw_vol(v) == hw


def test_r16():
    assert P.R16_AMIGA == 13107                     # 1/5
    assert P.R16_U64_S0 == 18897                    # 1022727 / 3546895
    with pytest.raises(ValueError):
        P.r16_for(P.PAL_CCK)


# --- the channel state machine ------------------------------------------------

def test_latch_at_dma_start():
    p = Paula()
    setup(p, 0, 0x012340, 100, 428, 64)
    p.write(0, DMACON, DMAEN | 1)
    c, v = p.ch[0], p.vc[0]
    assert c.on and c.cur_lc == 0x012340 and c.cur_len == 100
    assert p.posts == [(0, 0)]                      # AUD0 at DMA start
    assert v.start == REU_BASE + 0x012340 and v.length == 200
    assert v.rate == P.sampler_rate(428) and v.vol == 63 and v.pan == 0
    # the next block's latches, written after the start, do not touch it
    setup(p, 0, 0x020000, 10, 428, 64, now=5)
    assert c.cur_lc == 0x012340 and c.cur_len == 100


def test_lc_bit0_and_high_bits_ignored():
    p = Paula()
    p.write(0, reg(1, LCH), 0xFFFF)
    p.write(0, reg(1, LCL), 0x1235)
    assert p.ch[1].lc == 0x1F1234


def test_dma_needs_dmaen_and_auden():
    p = Paula()
    setup(p, 2, 0x1000, 8, 300, 40)
    p.write(0, DMACON, 1 << 2)                      # AUD2EN without DMAEN
    assert not p.ch[2].on and p.posts == []
    p.write(1, DMACON, DMAEN | 1 << 2)
    assert p.ch[2].on and p.posts == [(1, 2)]
    p.write(2, DMACON, DMAEN | 1 << 2)              # still on: no restart
    assert p.posts == [(1, 2)] and p.vc[2].programs == 1
    p.write(3, DMACON, 1 << 2)                      # DMAEN off stops it
    assert not p.ch[2].on


def test_block_end_reload_and_interrupt_times():
    """Blocks of 2*LEN*PER colour clocks; R = 1/5 E per colour clock."""
    p = Paula(r16=P.R16_AMIGA)
    setup(p, 0, 0x2000, 50, 500, 64)               # 50000 CCK = 10000 E
    p.write(0, DMACON, DMAEN | 1)
    blk = 2 * 50 * 500 * P.R16_AMIGA
    assert p.ch[0].dl == blk
    assert p.next_due() == -(-blk // 65536)         # 10000 E (r16 slightly < 1/5)
    assert p.next_due() == 10000
    setup(p, 0, 0x3000, 25, 500, 64, now=100)      # next block: half as long
    p.run_until(p.next_due())
    c = p.ch[0]
    assert c.cur_lc == 0x3000 and c.cur_len == 25 and c.nblk == 2
    assert p.posts[-1] == (10000, 0)
    assert c.dl == blk + 2 * 25 * 500 * P.R16_AMIGA


def test_no_drift_over_many_blocks():
    p = Paula(r16=P.R16_U64_S0)
    setup(p, 1, 0x4000, 1, 113, 10)                 # shortest block
    p.write(0, DMACON, DMAEN | 2)
    p.run_until(10**7)
    n = p.ch[1].nblk - 1
    exact = 10**7 * 65536 // (2 * 113 * P.R16_U64_S0)
    assert n == exact
    for (t0, _), (t1, _) in zip(p.posts, p.posts[1:]):
        assert t1 - t0 in (65, 66)                  # 226 CCK * 0.28834 = 65.2 E


def test_len0_is_65536_words():
    p = Paula()
    setup(p, 3, 0, 0, 200, 1)
    p.write(0, DMACON, DMAEN | 8)
    assert p.vc[3].length == 0x20000
    assert p.ch[3].dl == 2 * 0x10000 * 200 * p.r16


def test_per_mid_block_keeps_the_block_end():
    p = Paula()
    setup(p, 0, 0x100, 100, 400, 64)
    p.write(0, DMACON, DMAEN | 1)
    d0 = p.ch[0].dl
    p.write(1000, reg(0, PER), 200)                 # half the period from E 1000
    assert p.ch[0].dl == d0                          # this block keeps its end
    assert p.vc[0].rate == P.sampler_rate(200)       # the pitch changes at once
    p.run_until(p.next_due())
    assert p.ch[0].dl == d0 + 2 * 100 * 200 * p.r16  # the next block: new PER


def test_vol_live():
    p = Paula()
    setup(p, 0, 0x100, 100, 400, 10)
    p.write(0, DMACON, DMAEN | 1)
    p.write(5, reg(0, VOL), 64)
    assert p.vc[0].vol == 63 and p.vc[0].programs == 1


def test_dma_off_plays_silence():
    p = Paula(zero_reu=0xDF4000)
    setup(p, 0, 0x100, 100, 400, 64)
    p.write(0, DMACON, DMAEN | 1)
    p.write(9, DMACON, 0)
    v = p.vc[0]
    assert not p.ch[0].on and v.start == REU_BASE + 0xDF4000 and v.length == 2
    assert v.repeat is None
    assert p.next_due() is None
    p.run_until(10**6)
    assert len(p.posts) == 1


# --- hardware loops vs 6510 reprogramming -----------------------------------------

def test_same_block_loops_in_hardware():
    p = Paula()
    setup(p, 0, 0x8000, 64, 300, 64)
    p.write(0, DMACON, DMAEN | 1)
    assert p.vc[0].repeat == (0, 128)
    p.run_until(10**6)
    assert p.ch[0].nblk > 10 and p.vc[0].programs == 1   # no reprogram


def test_contiguous_loop_upgrade():
    """One-shot start then a loop at the sample's tail: the latches written
    after DMA start upgrade the playing voice to REPEAT mode."""
    p = Paula()
    setup(p, 0, 0x8000, 100, 300, 64)
    p.write(0, DMACON, DMAEN | 1)
    setup(p, 0, 0x8000 + 120, 40, 300, 64, now=3)   # loop = bytes 120..199
    assert p.vc[0].repeat == (120, 200)
    p.run_until(10**6)
    assert p.vc[0].programs == 1
    assert p.ch[0].cur_lc == 0x8000 + 120 and p.ch[0].cur_len == 40


def test_null_loop_reprograms_once():
    """A one-shot sample whose latches then name a 1-word block elsewhere
    (a silent loop): reprogrammed at the first block end, then looped."""
    p = Paula()
    setup(p, 0, 0x8000, 100, 300, 64)
    p.write(0, DMACON, DMAEN | 1)
    setup(p, 0, 0x0000, 1, 300, 64, now=3)
    assert p.vc[0].repeat is None
    p.run_until(10**6)
    v = p.vc[0]
    assert v.programs == 2 and v.start == REU_BASE and v.repeat == (0, 2)


def test_four_channels_share_one_deadline_queue():
    p = Paula()
    for x, per in enumerate((113, 200, 300, 400)):
        setup(p, x, 0x1000 * (x + 1), 10, per, 64)
    p.write(0, DMACON, DMAEN | 0xF)
    assert p.next_due() == min(Paula.due(c.dl) for c in p.ch)
    p.run_until(100000)
    for x, c in enumerate(p.ch):
        blk = 2 * 10 * c.per * p.r16
        assert c.nblk - 1 == 100000 * 65536 // blk, x
    ts = [t for t, _ in p.posts]
    assert ts == sorted(ts)


# --- rev15-paula round 1: the ref mutants seamless_any, loop_le, per0 ----------

def test_nonseamless_block_reprograms():
    """A block the hardware loop does not play is reprogrammed at its start
    (kills a _seamless that always says yes)."""
    p = Paula()
    setup(p, 0, 0x8000, 16, 300, 64)
    p.write(0, DMACON, DMAEN | 1)
    setup(p, 0, 0x9000, 8, 300, 64, now=1)          # elsewhere: not the loop
    assert p.vc[0].repeat is None
    p.fire(p.next_due())
    v = p.vc[0]
    assert v.programs == 2 and v.start == REU_BASE + 0x9000 and v.length == 16


def test_loop_must_end_at_the_span_end():
    """A latched block ending short of the voice's span is no hardware loop
    (kills a <= in _loop)."""
    p = Paula()
    setup(p, 0, 0x8000, 16, 300, 64)
    p.write(0, DMACON, DMAEN | 1)
    setup(p, 0, 0x8008, 11, 300, 64, now=1)         # 8 + 22 = 30 < 32
    assert p.vc[0].repeat is None
    p.fire(p.next_due())
    assert p.vc[0].programs == 2


def test_per0_counts_as_one():
    """PER 0: a block of 2 x LEN colour clocks (kills a dur without max)."""
    p = Paula()
    setup(p, 0, 0x100, 10, 0, 64)
    p.write(0, DMACON, DMAEN | 1)
    assert p.ch[0].dl == 2 * 10 * 1 * p.r16
    assert p.next_due() > 0
