"""host/c2v_ref.py: palette LUT, reference conversions, argument block (W7-V)."""

from __future__ import annotations

import random
import struct
import sys
from pathlib import Path

import pytest

from host import c2v_ref as R

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "s3probe"))
import c2v_measure as probe  # noqa: E402  W6-D's independent models


@pytest.fixture(scope="module")
def lut() -> bytes:
    return R.build_lut()


def test_lut_shape_and_anchors(lut):
    assert len(lut) == 4096 and set(lut) <= set(range(16))
    assert lut[0x000] == 0 and lut[0xFFF] == 1
    assert lut[0x444] == 11          # $44 grey is VIC dark grey exactly
    assert lut[0x666] == 12 and lut[0x999] == 15
    assert len(set(lut)) == 16       # every VIC colour is someone's nearest


def test_lut_is_nearest(lut):
    rng = random.Random(1)
    for c in rng.sample(range(4096), 64):
        lab = R._lab(R.ocs_rgb(c))
        d = [sum((p - q) ** 2 for p, q in zip(lab, R._lab(v))) for v in R.VIC_RGB]
        assert d[lut[c]] == min(d)
        assert lut[c] == d.index(min(d))     # ties -> lower index


def test_pairor_matches_probe():
    for b in range(256):
        assert R.pairor(b) == probe.pairor(b)
    assert R.pairor(0x80) == 8 and R.pairor(0x01) == 1 and R.pairor(0x0C) == 2


def _repeat(rows: list[bytes], n: int) -> list[bytes]:
    return [r * n for r in rows]


@pytest.mark.parametrize("seed", [0x5335, 7])
@pytest.mark.parametrize("nrows", [25, 22, 20])
def test_bitmaps_match_probe_models(seed, nrows, lut):
    # the probe models are 200-line frames; the reference is line-count
    # agnostic: 25 rows = the whole probe frame, fewer rows = its prefix
    rng = random.Random(seed)
    rows = [bytes(rng.randrange(256) for _ in range(80)) for _ in range(4)]
    want = probe.ref_hires(rows)[:nrows * 320]
    got = R.convert_hires(_repeat(rows, nrows * 8), [0] * 16, lut, rows=nrows)
    assert got["bitmap"] == want
    lrows = [r[:40] for r in rows[:2]]
    want = probe.ref_mc2(lrows)[:nrows * 320]
    got = R.convert_mc2(_repeat(lrows, nrows * 8), [0] * 4, lut, rows=nrows)
    assert got["bitmap"] == want
    assert R.C2V_LINES == 200 and len(probe.ref_mc2(lrows)) == R.BITMAP_LEN


def test_mc2_pixels_are_colour_indices(lut):
    p0 = bytearray(R.C2V_LINES * 40)
    p1 = bytearray(R.C2V_LINES * 40)
    y, cx = 13, 7                     # line 13 = char row 1, line 5 in the cell
    p0[y * 40 + cx] = 0b10_00_10_00   # pairs 0 and 2 (left pixels) in plane 0
    p1[y * 40 + cx] = 0b00_00_10_10   # pairs 2 and 3 in plane 1: indices 1,0,3,2
    out = R.convert_mc2([bytes(p0), bytes(p1)], [0, 1, 2, 3], lut)
    got = out["bitmap"][320 + cx * 8 + 5]
    assert got == 0b01_00_11_10
    assert sum(out["bitmap"]) == got


def test_hires_rows_advance_and_silhouette(lut):
    planes = [bytearray(R.C2V_LINES * 80) for _ in range(4)]
    planes[3][199 * 80 + 79] = 0x01   # last line, last byte, last pixel, plane 3
    planes[0][0] = 0x40               # first pixel pair
    out = R.convert_hires([bytes(p) for p in planes], [0x000, 0xFFF], lut)
    bmp = out["bitmap"]
    assert bmp[0] == 0x80
    assert bmp[24 * 320 + 39 * 8 + 7] == 0x01
    assert sum(1 for b in bmp if b) == 2


def test_colours_and_blank_band(lut):
    pal = [0x000, 0xFFF, 0x444, 0x999]
    m = R.convert_mc2([bytes(R.C2V_LINES * 40)] * 2, pal, lut)
    assert m["bgc"] == 0
    assert m["screen"] == bytes([0x1B] * 1000)          # 25 rows: no band
    assert m["colram"] == bytes([15] * 1000)
    m = R.convert_mc2([bytes(176 * 40)] * 2, pal, lut, rows=22)
    assert m["screen"] == bytes([0x1B] * 880 + [0] * 120)
    assert m["colram"] == bytes([15] * 880 + [0] * 120)
    h = R.convert_hires([bytes(176 * 80)] * 4, [0xFFF, 0x000], lut, rows=22)
    assert h["screen"] == bytes([0x01] * 880 + [0x11] * 120)
    # W12-V: converted rows carry each cell's pen (0: no pixel); the band keeps c0
    assert h["colram"] == bytes([0] * 880 + [1] * 120) and h["bgc"] == 1


def test_pack_args_layout():
    a = R.pack_args([0x123456, 0x100000], 0xABCDEF, [0x0F00, 0x1234])
    assert len(a) == 48
    assert a[0:3] == bytes([0x56, 0x34, 0x12]) and a[3:6] == bytes([0, 0, 0x10])
    assert a[6:12] == bytes(6)
    assert a[12:16] == bytes([0xEF, 0xCD, 0xAB, 0])
    assert struct.unpack_from("<2H", a, 16) == (0x0F00, 0x1234)


def test_check_placement():
    R.check_placement(0x100000, R.C2V_LINES * 80, "plane")
    with pytest.raises(ValueError):
        R.check_placement(0x07FFF0, 0x20, "plane")
    with pytest.raises(ValueError):
        R.check_placement(0xFFFFF0, 0x20, "plane")
