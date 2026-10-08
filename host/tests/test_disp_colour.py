"""W12-V: the visible display.  Per-cell hires pens, the contrast-preserving
colour choice (c2v_ref.separate), the run-length LUT and the matrix
checksum the U64 m3 hash carries.  docs/CONTRACTS.md "Display refresh
(S3.5)", "Colour choice (W12-V)".
"""

from __future__ import annotations

import os
import random
import re
import sys
from pathlib import Path

import pytest

from host import c2v_ref as R

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "aros"))
import model_frames as MF  # noqa: E402

# the insert-disk screen's first four pens (docs/WAVE12_BRIEF.md W12-V)
DARK = [0x000, 0x111, 0x222, 0x024]


@pytest.fixture(scope="module")
def lut():
    return R.build_lut()


# --- the ladder and the LUT the PRG carries -------------------------------------

def test_ladder_is_luma_order_and_matches_disp_s():
    assert sorted(R.LADDER) == list(range(16))
    y = [R._luma(R.VIC_RGB[i]) for i in R.LADDER]
    assert y == sorted(y) and R.LADDER[0] == 0 and R.LADDER[-1] == 1
    src = (ROOT / "runtime" / "ops" / "disp.s").read_text()
    m = re.search(r"^disp_lad:\s*\n\s*\.byte ([0-9, ]+)$", src, re.M)
    assert m and tuple(int(v) for v in m.group(1).split(",")) == R.LADDER


def test_rle_lut_roundtrip(lut):
    assert R.unrle_lut(R.rle_lut(lut)) == lut
    assert len(R.rle_lut(lut)) == 821          # vs 2048 nibble-packed
    rnd = bytes(random.Random(12).randrange(16) for _ in range(4096))
    assert R.unrle_lut(R.rle_lut(rnd)) == rnd
    runs = bytes([7] * 4096)
    assert R.rle_lut(runs) == bytes([0xF7] * 256)


def _unrle_twin(stream: bytes) -> bytes:
    """An independent decoder, as disp_init runs it: per byte, colour = low
    nibble, run = high nibble + 1, into 16 chunks of 256; stop at 4096."""
    out = bytearray()
    for b in stream:
        for _ in range((b >> 4) + 1):
            out.append(b & 0x0F)
            if len(out) == 4096:
                return bytes(out)
    return bytes(out)


def test_disp_lut_inc_decodes_to_build_lut(lut):
    """Supervisor condition 1: the stream the PRG carries decodes, with a
    Python twin of disp_init's loader, to exactly c2v_ref.build_lut()."""
    src = (ROOT / "runtime" / "ops" / "disp_lut.inc").read_text()
    stream = bytes(int(v.strip()[1:], 16) for line in src.splitlines()
                   if line.strip().startswith(".byte")
                   for v in line.split(".byte")[1].split(","))
    assert len(stream) == 821
    assert _unrle_twin(stream) == lut and len(lut) == 4096
    assert sum((b >> 4) + 1 for b in stream) == 4096   # no trailing entries


# --- the colour choice ----------------------------------------------------------

def test_dark_palette_pens_are_told_apart(lut):
    assert [lut[c] for c in DARK] == [0, 0, 0, 0]  # the bug: all black
    v = R.separate(DARK, lut, [0, 1, 1, 1] + [0] * 12)
    assert v[0] == 0                               # pen 0 stays the background
    assert len(set(v[:4])) == 4                    # every pen its own colour


def test_unused_pens_get_none_and_keep_out(lut):
    v = R.separate(DARK + [0x000] * 12, lut, [0, 0, 5] + [0] * 13)
    assert v[0] == 0 and v[2] is not None and v[2] != 0
    assert all(v[p] is None for p in (1, 3, 4, 15))


def test_order_by_count_keeps_nearest_for_the_most_used(lut):
    pal = [0x000, 0xCCC, 0xFFF] + [0] * 13
    assert lut[0xCCC] == lut[0xFFF] == 1
    assert R.separate(pal, lut, [0, 1, 9] + [0] * 13)[2] == 1   # pen 2 more used
    assert R.separate(pal, lut, [0, 9, 1] + [0] * 13)[1] == 1
    assert R.separate(pal, lut, [0, 4, 4] + [0] * 13)[1] == 1   # tie: lower pen


def test_equal_ocs_colours_share(lut):
    pal = [0x000, 0x111, 0x111, 0xF00, 0x111] + [0] * 11
    v = R.separate(pal, lut, [0, 3, 2, 1, 7] + [0] * 11)
    assert v[1] == v[2] == v[4] != v[0]
    pal[1] = pal[0]                                # a pen equal to pen 0 shares it
    assert R.separate(pal, lut, [0, 3, 2, 1, 7] + [0] * 11)[1] == v[0]


def test_random_palettes_injective(lut):
    rnd = random.Random(2026)
    for _ in range(300):
        pal = [rnd.randrange(4096) for _ in range(16)]
        if rnd.random() < 0.5:                     # dark palettes
            pal = [c & 0x333 for c in pal]
        cnt = [rnd.choice([0, 0, 1, 2, 255]) for _ in range(16)]
        v = R.separate(pal, lut, cnt)
        use = [0] + [p for p in range(1, 16) if cnt[p]]
        assert v[0] == lut[pal[0]]
        for a in use:
            assert v[a] is not None
            for b in use:
                assert (v[a] == v[b]) == (pal[a] == pal[b]), (pal, cnt, a, b)
        assert all(v[p] is None for p in range(16) if p not in use)


def test_nearest_kept_when_no_collision(lut):
    pal = [0x000, 0xFFF, 0xF00, 0x00F]
    v = R.separate(pal, lut, [0, 1, 1, 1] + [0] * 12)
    assert v[:4] == [lut[c] for c in pal]


# --- per-cell pens ----------------------------------------------------------------

def _hires(cells: dict) -> list[bytearray]:
    """4 planes of 200 x 80 bytes; cells {(row, cx): [(line, hpx, pen)]}."""
    planes = [bytearray(200 * 80) for _ in range(4)]
    for (r, cx), px in cells.items():
        for line, x, pen in px:
            o = (r * 8 + line) * 80 + 2 * cx + x // 8
            for p in range(4):
                if pen >> p & 1:
                    planes[p][o] |= 0x80 >> (x % 8)
    return planes


def test_cell_pen_first_line_then_leftmost():
    pl = _hires({(0, 0): [(3, 12, 5), (3, 9, 2), (6, 0, 7)],
                 (0, 1): [(0, 15, 9)],
                 (2, 39): [(7, 0, 15)]})
    assert R.cell_pen(pl, 0, 0) == 2          # line 3 first, pixel 9 left of 12
    assert R.cell_pen(pl, 0, 1) == 9          # the right byte only
    assert R.cell_pen(pl, 16, 39) == 15
    assert R.cell_pen(pl, 8, 0) == 0
    pens, cnt = R.cell_pens(pl)
    assert pens[0] == 2 and pens[1] == 9 and pens[2 * 40 + 39] == 15
    assert cnt[2] == cnt[9] == cnt[15] == 1 and sum(cnt) == 3


def test_count_saturates():
    pl = _hires({(r, cx): [(0, 0, 3)] for r in range(25) for cx in range(40)})
    assert R.cell_pens(pl)[1][3] == R.CNT_MAX


# --- the insert-disk shape through disp_convert ------------------------------------

def _st(**kw):
    st = {"dmacon": 0x0300, "bplcon0": 0xC200, "bpl1mod": 0, "bpl2mod": 0, "bplpt": [0x1000 + p * 0x4000 for p in range(4)],
          "diwstrt": 0x2C81, "diwstop": 0xF4C1, "ddfstrt": 0x3C, "ddfstop": 0xD4,
          "color": DARK + [0x0F0] * 12 + [0] * 16}
    st.update(kw)
    return st


def test_insert_disk_shape_has_three_visible_colours(lut):
    """Pens 1-3 ($111 $222 $024) on $000, hires: the brief's insert-disk frame."""
    pl = _hires({(10, 5): [(0, 0, 1)], (10, 6): [(2, 3, 2)], (11, 7): [(4, 4, 3)]})
    mem = {0x1000 + p * 0x4000: bytes(pl[p]) for p in range(4)}

    def read(a, n):
        for base, b in mem.items():
            if base <= a < base + len(b):
                return b[a - base:a - base + n]
        return bytes(n)
    out = R.disp_convert(_st(), read, lut)
    assert out["decide"]["mode"] == R.MODE_HIRES
    bg = out["bgc"]
    fg = {out["screen"][r * 40 + cx] >> 4 for r, cx in ((10, 5), (10, 6), (11, 7))}
    assert bg == 0 and len(fg) == 3 and bg not in fg
    assert MF.visible_colours(out) == 3
    assert out["colram"][10 * 40 + 5] == 1 and out["colram"][11 * 40 + 7] == 3
    assert out["mck"] == R.screen_sum(out["screen"])


# --- V2: the U64 hash sees the palette ---------------------------------------------

def test_screen_sum():
    assert R.screen_sum(bytes(1000)) == 0
    assert R.screen_sum(bytes([1] * 1000)) == ((1000 % 255) | ((1000 * 1001 // 2) % 255) << 8)
    a = bytes([0x10] + [0] * 999)
    b = bytes([0] * 999 + [0x10])
    assert R.screen_sum(a) != R.screen_sum(b)       # position-sensitive


def test_u64_hash_sees_a_palette_change(lut):
    pl = _hires({(10, 5): [(0, 0, 1)], (10, 6): [(2, 3, 2)]})
    mem = {0x1000 + p * 0x4000: bytes(pl[p]) for p in range(4)}

    def read(a, n):
        for base, b in mem.items():
            if base <= a < base + len(b):
                return b[a - base:a - base + n]
        return bytes(n)
    a = R.disp_convert(_st(), read, lut)
    b = R.disp_convert(_st(color=[0x000, 0x0F0, 0x222, 0x024] + [0x0F0] * 12 + [0] * 16), read, lut)
    assert a["bitmap"] == b["bitmap"] and a["colram"] == b["colram"]
    assert a["screen"] != b["screen"]
    assert MF.u64_hash(a) != MF.u64_hash(b)
    assert MF.u64_hash(a) == MF.conv_hash(a["bitmap"], None, a["colram"], a["vic"], a["mck"])


# --- fix round 1 (R1): the checksum sees single-pen palette errors ------------------

# a frame-345-like hires screen: cell pens with the model frame's counts
# (pen: cells) and its pen colours (c2v_ref.separate on the AROS palette)
F345_CNT = {1: 29, 2: 6, 4: 1, 5: 6, 6: 8, 7: 5, 8: 2, 10: 1, 12: 23}
F345_VIC = [0, 6, 2, None, 9, 4, 11, 15, 3, None, 13, None, 1, None, None, None]


def _f345_conv(vic: list) -> dict:
    rnd = random.Random(345)
    cells = rnd.sample(range(1000), sum(F345_CNT.values()))
    pens = [0] * 1000
    i = 0
    for p, n in F345_CNT.items():
        for _ in range(n):
            pens[cells[i]] = p
            i += 1
    screen = bytes((vic[k] << 4) | vic[0] for k in pens)
    bitmap = bytes((0x80 if pens[c // 8 % 40 + c // 320 * 40] else 0) for c in range(8000))
    return {"bitmap": bitmap, "screen": screen, "colram": bytes(pens),
            "vic": {0xD011: 0x3B, 0xD016: 0x08, 0xD018: 0x48, 0xD020: vic[0], 0xD021: vic[0]},
            "mck": R.screen_sum(screen)}


def _variants():
    """Every one-pen colour error with one bit flipped (bit 3 included: the
    one the mod-256 sums lost on even cell counts) and every pen swap."""
    used = [p for p in F345_CNT]
    for p in used:
        for bit in range(4):
            v = list(F345_VIC)
            v[p] ^= 1 << bit
            yield f"pen {p} bit {bit}", v
    for a in used:
        for b in used:
            if a < b and F345_VIC[a] != F345_VIC[b]:
                v = list(F345_VIC)
                v[a], v[b] = v[b], v[a]
                yield f"swap {a}<->{b}", v


def test_u64_hash_sees_every_single_pen_error_and_swap():
    good = MF.u64_hash(_f345_conv(F345_VIC))
    same = [name for name, v in _variants() if MF.u64_hash(_f345_conv(v)) == good]
    assert not same, same


def test_mod256_sums_would_miss_them():
    """Why mod 255 (red first): on this layout the old mod-256 Fletcher
    keeps the bit-3 errors of pens 5 and 8 and the swap of pens 4 and 12;
    mod 255 sees every variant (the test above)."""
    def sum256(screen):
        s1 = s2 = 0
        for b in screen:
            s1 = (s1 + b) & 0xFF
            s2 = (s2 + s1) & 0xFF
        return s1 | s2 << 8
    good = _f345_conv(F345_VIC)["screen"]
    lost = [n for n, v in _variants() if sum256(_f345_conv(v)["screen"]) == sum256(good)]
    assert lost == ["pen 5 bit 3", "pen 8 bit 3", "swap 4<->12"]
