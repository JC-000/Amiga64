"""W16-A: run_player's audio analysis against the sup16 probe recordings.

The fixtures (tests/fixtures/player_audio/make.py) are excerpts of the
2026-10-13 "probe" captures: the C64U (C64 Ultimate fw 1.1.0, 64 MHz),
whose stream sits at a DC offset before the song and steps 0.222 s before
the first note, and the U64E (48 MHz, no step).  The C64U's first
find_tone window was that step, so every pitch window landed 0.222 s
early: PER 339 measured 259.54 Hz (PER 428's pitch) and the stream-rate
cross-check read 51137 Hz.  The sampler's sample time is (rate + 1) x 160
ns + 80 ns on the C64U and + 120 ns on the U64E (docs/HARDWARE.md).
"""

import os

import numpy as np
import pytest

from host import paula_ref as PR
from tests import run_player as P
from tests.run_paula import PITCH_TOL, find_tone

FIX = os.path.join(os.path.dirname(__file__), "..", "..", "tests", "fixtures", "player_audio")
RATE = 47940.34090909091                         # NTSC phi2 x 3 / 64
PHI2 = 1022727
KEPT = (856, 339, 113)                           # the notes the fixtures keep
PRODUCT = {"c64u": "C64 Ultimate", "u64e": "Ultimate 64 Elite"}


def load(dev: str) -> tuple[dict, dict]:
    z = np.load(os.path.join(FIX, f"probe_{dev}.npz"))
    left = np.full(int(z["n"]), float(z["fill_l"]))
    pos = 0
    for s, n in zip(z["starts"], z["lens"]):
        left[s:s + n] = z["data"][pos:pos + n]
        pos += n
    right = np.full(len(left), float(z["fill_r"]))
    au = {"l": left, "r": right, "mono": (left + right) / 2, "rate": RATE}
    return au, {"recs": z["recs"].tobytes()}


@pytest.fixture(scope="module")
def probe():
    return P.cases(PHI2, 0, 20.0, False, "demo")[0]


def test_find_tone_hold_skips_a_dc_step():
    """A single 10 ms window over a DC step is not a tone; a held tone is."""
    n = int(RATE)
    x = np.full(n, -8500.0)
    x[int(0.2 * RATE):] = 4600.0                 # the step
    t = np.arange(n - int(0.5 * RATE))
    x[int(0.5 * RATE):] += 7000 * np.sin(2 * np.pi * 129.45 * t / RATE)
    assert find_tone(x, RATE)[0] / RATE == pytest.approx(0.2, abs=0.011)
    assert find_tone(x, RATE, hold=0.05)[0] / RATE == pytest.approx(0.5, abs=0.011)


@pytest.mark.parametrize("dev,want", [("c64u", 0.7793), ("u64e", 0.8793)])
def test_align_is_the_first_note(dev, want):
    au, _ = load(dev)
    assert P.align(au) / RATE == pytest.approx(want, abs=0.011)


@pytest.mark.parametrize("dev", ["c64u", "u64e"])
def test_pitch_check(dev, probe):
    """The kept notes sit on the device's sampler law within PITCH_TOL and
    the stream-rate cross-check passes (the U64E's also passed before)."""
    au, res = load(dev)
    law = PR.sample_c(PRODUCT[dev])
    f, rows = P.pitch_check(probe, res, au, PHI2, 0, law_c=law)
    got = {r["per"]: r for r in rows if "per" in r}
    for per in KEPT:
        r = got[per]
        assert abs(r["f"] / r["f_sampler"] - 1) <= PITCH_TOL, (per, r)
        assert not [x for x in f if f"PER {per} " in x], f
    chk = [r for r in rows if "rate_check" in r]
    assert chk and abs(chk[0]["rate_check"] / RATE - 1) <= P.RATE_CHECK_TOL, chk
    assert not [x for x in f if "cross-check" in x], f


def test_c64u_needs_its_own_law(probe):
    """Teeth for the per-device law: against the old 118 ns law the C64U's
    PER 113 is +0.12 % (> PITCH_TOL) although it is aligned correctly."""
    au, res = load("c64u")
    f, rows = P.pitch_check(probe, res, au, PHI2, 0, law_c=PR.SAMPLE_C_LAW)
    assert [x for x in f if "PER 113 " in x and "sampler law" in x], f


def test_misalignment_fails_the_cross_check(probe, monkeypatch):
    """Teeth for the assertion: aligned by the first find_tone window (the
    old way), the C64U's cross-check reads about 51.1 kHz and fails."""
    au, res = load("c64u")
    monkeypatch.setattr(P, "align", lambda au, thr=500.0: find_tone(au["mono"], au["rate"],
                                                                     thr=thr)[0])
    f, rows = P.pitch_check(probe, res, au, PHI2, 0, law_c=PR.sample_c("C64 Ultimate"))
    chk = [r for r in rows if "rate_check" in r]
    assert chk and chk[0]["rate_check"] == pytest.approx(51137, rel=0.002)
    assert [x for x in f if "cross-check" in x], f


def test_note_ends_aligned(probe):
    """The first trigger's 3 ms window is the note's start, not the C64U's
    DC step (largest step 10502 there)."""
    au, res = load("c64u")
    ne = P.note_ends(probe, res, au, PHI2, 0, law_c=PR.sample_c("C64 Ultimate"))
    assert ne["triggers"][0]["max_step"] < 2000, ne["triggers"][0]
    assert ne["floor_rms"] < 100, ne["floor_rms"]       # the floor after the step, not across it


def test_demo_clicks_uses_align(probe, monkeypatch):
    au, res = load("c64u")
    seen = []
    real = P.align

    def spy(au, thr=500.0):
        seen.append(thr)
        return real(au, thr)
    monkeypatch.setattr(P, "align", spy)
    P.demo_clicks(probe, res, au, PHI2, 0, law_c=PR.sample_c("C64 Ultimate"))
    assert seen == [200.0]


def test_sampler_law_per_device():
    assert PR.sample_c("C64 Ultimate") == 80.0
    assert PR.sample_c("Ultimate 64 Elite") == 120.0
    assert PR.sample_c("something else") == PR.SAMPLE_C_LAW == 118.0
    assert PR.sample_c(None) == PR.SAMPLE_C_LAW
    assert PR.sample_ns(752) == 752 * 160 + 160 + 118      # the default: the W10-H law
    assert PR.sample_ns(752, 80.0) == 120560.0             # the C64U's PER 428, measured
