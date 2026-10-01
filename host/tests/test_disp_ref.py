"""W10-V: the display refresh model (host/c2v_ref.py disp_*) and its ties
to runtime/ops/disp.s (docs/C2V.md "Display refresh (S3.5)")."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from host import c2v_ref as R

ROOT = Path(__file__).resolve().parents[2]
DISP_S = ROOT / "runtime" / "ops" / "disp.s"
LUT_INC = ROOT / "runtime" / "ops" / "disp_lut.inc"

STD = {"dmacon": 0x0380, "bplcon0": 0x2200, "bplcon1": 0, "bplcon2": 0,
       "bplpt": [0x100000, 0x102000, 0, 0, 0, 0], "bpl1mod": 0, "bpl2mod": 0,
       "diwstrt": 0x2C81, "diwstop": 0xF4C1, "ddfstrt": 0x38, "ddfstop": 0xD0,
       "color": list(range(32))}
HIRES = dict(STD, bplcon0=0xC200, ddfstrt=0x3C, ddfstop=0xD4,
             bplpt=[0x100000 + p * 0x4000 for p in range(4)] + [0, 0])


def st(**kw):
    return {**STD, **kw}


def equates(path: Path) -> dict[str, int]:
    out = {}
    for m in re.finditer(r"^(\w+)\s*=\s*(\$[0-9A-Fa-f]+|\d+)\b", path.read_text(), re.M):
        v = m.group(2)
        out[m.group(1)] = int(v[1:], 16) if v.startswith("$") else int(v)
    return out


# --- the LUT the PRG carries ---------------------------------------------------------

def test_lut_inc_is_generated_from_build_lut():
    assert LUT_INC.read_text() == R.lut_inc()


def test_pack_lut_roundtrip():
    lut = R.build_lut()
    assert len(R.pack_lut(lut)) == 2048
    assert R.unpack_lut(R.pack_lut(lut)) == lut
    rnd = bytes(random.Random(5).randrange(16) for _ in range(4096))
    assert R.unpack_lut(R.pack_lut(rnd)) == rnd


# --- C1 layout -------------------------------------------------------------------

def test_disp_st_layout_c1():
    b = R.pack_disp_st({"gen": 0x5A, "dmacon": 0x1234, "bplcon0": 0x2200,
                        "bplpt": [0x123456, 0xABCDEF], "bpl1mod": 0xFFD8,
                        "bpl2mod": 8, "diwstrt": 0x2C81, "diwstop": 0xF4C1,
                        "ddfstrt": 0x38, "ddfstop": 0xD0,
                        "color": [0x0FFF] + [0] * 30 + [0x0123]})
    assert len(b) == R.DISP_ST_LEN == 104 and b[103] == R.COP_VALID == 0xA5
    assert b[0] == 0x5A and b[1:3] == b"\x34\x12" and b[3:5] == b"\x00\x22"
    assert b[9:12] == b"\x56\x34\x12" and b[12:15] == b"\xEF\xCD\xAB"
    assert b[27:29] == b"\xD8\xFF" and b[29:31] == b"\x08\x00"
    assert b[31:33] == b"\x81\x2C" and b[37:39] == b"\xD0\x00"
    assert b[39:41] == b"\xFF\x0F" and b[101:103] == b"\x23\x01"
    assert R.pack_disp_st(R.unpack_disp_st(b)) == b


def test_disp_s_snapshot_offsets_match_c1():
    """disp.s copies disp_st+1..+70 (C1 offset - 1), then the valid byte."""
    e = equates(DISP_S)
    c1 = {"DISP_S_DMACON": 1, "DISP_S_BPLCON0": 3, "DISP_S_BPLPT": 9,
          "DISP_S_BPL1MOD": 27, "DISP_S_DIWSTRT": 31, "DISP_S_DIWSTOP": 33,
          "DISP_S_DDFSTRT": 35, "DISP_S_DDFSTOP": 37, "DISP_S_COLOR": 39}
    for k, off in c1.items():
        assert e[k] == off - 1, k
    assert e["DISP_S_COLOR"] + 32 == e["DISP_S_VALID"] == 70     # through color[15]
    assert e["DISP_SNAP"] == 71
    assert (e["DISP_ST_VALID"], e["DISP_COPVALID"]) == (103, R.COP_VALID)


def test_disp_s_codes_match_model():
    e = equates(DISP_S)
    assert (e["DISP_NONE"], e["DISP_BLANK"], e["DISP_MC2"], e["DISP_HIRES"]) == (
        R.MODE_NONE, R.MODE_BLANK, R.MODE_MC2, R.MODE_HIRES)
    for k in ("DMA", "HAMDPF", "DEPTH", "DIW", "DDF", "STRADDLE", "NOSTATE"):
        assert e["DISP_WHY_" + k] == getattr(R, "WHY_" + k), k
    assert (e["DISP_VON"], e["DISP_VOFF"], e["DISP_VMC"], e["DISP_VHI"]) == (
        R.VIC_D011_ON, R.VIC_D011_BLANK, R.VIC_D016[R.MODE_MC2],
        R.VIC_D016[R.MODE_HIRES])


def test_guest_claim_matches_model():
    g = (ROOT / "runtime" / "rc" / "guest.inc").read_text()
    m = re.search(r"^disp_lut\s*=\s*\$([0-9A-F]+)", g, re.M)
    assert m and int(m.group(1), 16) == R.DISP_LUT
    R.check_placement(R.DISP_LUT, R.LUT_LEN, "LUT")
    # under the trapped custom-chip bank: no guest access reaches it (S3.1)
    assert R.DISP_LUT >> 16 == 0xDF and (R.DISP_LUT + R.LUT_LEN - 1) >> 16 == 0xDF


# --- decisions -------------------------------------------------------------------

def test_standard_mc2_and_hires():
    d = R.disp_decide(st())
    assert (d["mode"], d["why"], d["fetch"], d["lines"]) == (R.MODE_MC2, 0, 40, 200)
    assert d["src"] == [0x100000, 0x102000, 0, 0] and d["skip"] == [0, 0, 0, 0]
    d = R.disp_decide(HIRES)
    assert (d["mode"], d["fetch"], d["width"]) == (R.MODE_HIRES, 80, 80)


@pytest.mark.parametrize("kw,why", [
    ({"valid": 0x00}, R.WHY_NOSTATE),          # cop_reset never ran
    ({"valid": 0xA4, "dmacon": 0}, R.WHY_NOSTATE),   # checked first
    ({"dmacon": 0x0280}, R.WHY_DMA),           # BPLEN clear
    ({"dmacon": 0x0180}, R.WHY_DMA),           # DMAEN clear
    ({"bplcon0": 0x2A00}, R.WHY_HAMDPF),       # HAM
    ({"bplcon0": 0x2600}, R.WHY_HAMDPF),       # DBLPF
    ({"bplcon0": 0x1200}, R.WHY_DEPTH),        # 1 plane
    ({"bplcon0": 0x3200}, R.WHY_DEPTH),        # 3 planes
    ({"bplcon0": 0xA200}, R.WHY_DEPTH),        # hires 2 planes
    ({"bplcon0": 0x4200}, R.WHY_DEPTH),        # lores 4 planes
    ({"diwstrt": 0x2C81, "diwstop": 0x2CC1}, None),   # $12C: 256 lines
    ({"diwstrt": 0xF481, "diwstop": 0x90C1}, R.WHY_DIW),          # $90 < $F4
    ({"diwstrt": 0x9081, "diwstop": 0x90C1}, R.WHY_DIW),          # empty
    ({"ddfstop": 0xC8}, R.WHY_DDF),            # 38 bytes < 40
    ({"ddfstrt": 0xD8}, R.WHY_DDF),            # stop < start
    ({"bpl1mod": 0x7FFE, "ddfstrt": 0x30}, R.WHY_DDF),   # skip > s16
])
def test_blank_reasons(kw, why):
    d = R.disp_decide(st(**kw))
    if why is None:
        assert d["mode"] == R.MODE_MC2
    else:
        assert (d["mode"], d["why"]) == (R.MODE_BLANK, why)


def test_diw_v8_and_crop():
    d = R.disp_decide(st(diwstop=0x2CC1))        # V7 = 0 -> V8 = 1: $12C
    assert (d["lines"], d["src"][0]) == (200, 0x100000 + 28 * 40)
    d = R.disp_decide(st(diwstop=0xC2C1))        # $C2 - $2C = 150
    assert d["lines"] == 150 and d["src"][0] == 0x100000


def test_modulo_parity_bit0_and_ddf_width():
    d = R.disp_decide(st(bplpt=[0x100001, 0x102003, 0, 0, 0, 0],
                         bpl1mod=41, bpl2mod=0xFFF7, ddfstrt=0x30))
    assert d["fetch"] == 42
    assert d["src"][:2] == [0x100000, 0x102002]   # bit 0 ignored
    assert d["skip"][:2] == [2 + 40, 2 - 10]       # plane 0: BPL1MOD, 1: BPL2MOD
    h = R.disp_decide(dict(HIRES, bpl1mod=2, bpl2mod=4))
    assert h["skip"] == [2, 4, 2, 4]


def test_straddle_boundaries():
    ok = R.disp_decide(st(bplpt=[0x180000 - 40, 0x100000, 0, 0, 0, 0],
                          bpl1mod=0xFFB0))           # rows go down, row 0 ends at the line
    assert ok["mode"] == R.MODE_MC2
    bad = R.disp_decide(st(bplpt=[0x180000 - 38, 0x100000, 0, 0, 0, 0]))
    assert bad["why"] == R.WHY_STRADDLE
    late = R.disp_decide(st(bplpt=[0x180000 - 199 * 40 - 20, 0x100000, 0, 0, 0, 0]))
    assert late["why"] == R.WHY_STRADDLE          # only the last row crosses
    wrap = R.disp_decide(st(bplpt=[0xFFFFF0, 0x100000, 0, 0, 0, 0]))
    assert wrap["why"] == R.WHY_STRADDLE


# --- conversion ------------------------------------------------------------------

def _mem(seed, base, n):
    data = bytes(random.Random(seed).randrange(256) for _ in range(n))
    return data, (lambda a, k: data[a - base:a - base + k])


def test_contiguous_mc2_equals_w7_reference():
    data, read = _mem(1, 0x100000, 0x4000)
    lut = R.build_lut()
    s = st(color=[random.Random(2).randrange(0x10000) for _ in range(32)])
    got = R.disp_convert(s, read, lut)
    want = R.convert_mc2([data[:8000], data[0x2000:0x2000 + 8000]], s["color"][:16], lut)
    for k in ("bitmap", "screen", "colram", "bgc"):
        assert got[k] == want[k], k
    assert got["vic"] == {0xD011: 0x3B, 0xD016: 0x18, 0xD018: 0x48,
                          0xD020: want["bgc"], 0xD021: want["bgc"]}


def test_short_diw_clears_lines_below():
    _, read = _mem(3, 0x100000, 0x4000)
    lut = R.build_lut()
    full = R.disp_convert(st(), read, lut)
    short = R.disp_convert(st(diwstop=0xC2C1), read, lut)     # 150 lines
    for y in range(200):
        for cx in range(40):
            o = R._cell_offset(y, cx)
            assert short["bitmap"][o] == (full["bitmap"][o] if y < 150 else 0)


def test_hires_interleaved_rows():
    data, read = _mem(4, 0x120000, 0x10000)
    s = dict(HIRES, bplpt=[0x120000 + p * 80 for p in range(4)] + [0, 0],
             bpl1mod=240, bpl2mod=240)
    got = R.disp_convert(s, read, R.build_lut())
    planes = [b"".join(data[y * 320 + p * 80:y * 320 + p * 80 + 80] for y in range(200))
              for p in range(4)]
    want = R.convert_hires(planes, s["color"][:16], R.build_lut())
    assert got["bitmap"] == want["bitmap"] and got["vic"][0xD016] == 0x08


def test_blank_border_is_colour0():
    lut = R.build_lut()
    out = R.disp_convert(st(dmacon=0, color=[0x0F00] + [0] * 31), None, lut)
    assert out["decide"]["why"] == R.WHY_DMA and out["border"] == lut[0xF00]
