"""The e2e leg's guest-side helpers (tests/aros_wb.py): find_icon and
wb_screen on synthetic guest memory (public NDK offsets; no ROM, no disk)."""

from __future__ import annotations

import struct

import pytest

from tests import aros_wb as W


class Mem:
    def __init__(self):
        self.b = bytearray(1 << 24)

    def read(self, a, n):
        return bytes(self.b[a:a + n])

    def l(self, a, v):
        self.b[a:a + 4] = struct.pack(">I", v)

    def s(self, a, t):
        self.b[a:a + len(t) + 1] = t.encode() + b"\0"


PL0, PL1 = 0x3AB8, 0x8AB8


def screen_st():
    return {"bplcon0": 0xA200, "bpl1mod": 0, "bplpt": [PL0, PL1, 0, 0, 0, 0]}


def draw_box(mem, x0, x1, y0, y1, plane=PL0):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            mem.b[plane + y * 80 + (x >> 3)] |= 0x80 >> (x & 7)


def test_find_icon_takes_the_rightmost_icon_below_the_title():
    m = Mem()
    draw_box(m, 0, 639, 0, 14)                 # the title bar (excluded)
    draw_box(m, 16, 48, 15, 31)                # RAM Disk
    draw_box(m, 96, 128, 14 + 2, 30, PL1)      # the disk icon (plane 1 only)
    draw_box(m, 300, 303, 20, 21)              # a 4-px speck: not an icon
    b = W.find_icon(m.read, screen_st(), 15)
    assert (b["x0"], b["x1"], b["cx"]) == (96, 128, 112)
    assert (b["y0"], b["y1"]) == (16, 30) and len(b["all"]) == 2


def test_find_icon_needs_two_icons():
    m = Mem()
    draw_box(m, 16, 48, 15, 31)
    with pytest.raises(RuntimeError, match="cluster"):
        W.find_icon(m.read, screen_st(), 15)


def test_find_icon_honours_the_modulo():
    m = Mem()
    st = screen_st()
    st["bpl1mod"] = 8                           # 88 bytes per row
    for y in range(15, 32):
        for x0, x1 in ((16, 48), (96, 128)):
            for x in range(x0, x1 + 1):
                m.b[PL0 + y * 88 + (x >> 3)] |= 0x80 >> (x & 7)
    assert W.find_icon(m.read, st, 15)["cx"] == 112


def test_wb_screen_walks_libs_screen_and_windows():
    m = Mem()
    sb, ib, scr = 0xC00568, 0xC10000, 0xC20000
    m.l(4, sb)
    lh = sb + 378
    libs = [(0xC11000, "graphics.library"), (ib, "intuition.library")]
    for i, (n, name) in enumerate(libs):
        m.s(0xC30000 + 64 * i, name)
        m.l(n + 10, 0xC30000 + 64 * i)
        m.l(n, libs[i + 1][0] if i + 1 < len(libs) else lh + 4)
    m.l(lh, libs[0][0])
    m.l(ib + 60, scr)
    m.s(0xC31000, "Workbook 1.0")
    m.l(scr + 22, 0xC31000)
    w1, w2 = 0xC40000, 0xC41000
    m.l(scr + 4, w1)
    m.l(w1, w2)
    m.s(0xC32000, "Workbench3.1")
    m.l(w1 + 32, 0xC32000)
    m.l(w2 + 32, 0)
    lys = [0xC50000, 0xC50200, 0xC50400]        # LayerInfo.top_layer -> back -> back
    m.l(scr + 224, lys[0])
    m.l(lys[0] + 4, lys[1])
    m.l(lys[1] + 4, lys[2])
    s = W.wb_screen(m.read)
    assert s == {"title": "Workbook 1.0", "windows": ["Workbench3.1", ""], "layers": 3}


def test_wb_screen_without_intuition():
    m = Mem()
    m.l(4, 0xC00568)
    m.l(0xC00568 + 378, 0xC00568 + 382)
    assert W.wb_screen(m.read) == {"title": "", "windows": [], "layers": 0}


def test_prefetch_fetches_every_page_in_one_park():
    """GuestPeek.prefetch: every 256-byte page of the ranges, not yet
    cached, in ONE _fetch call (one park: rev-wb E2E F3)."""
    calls = []

    class G(W.GuestPeek):
        def __init__(self):
            self.cache = {0x3A00: bytes(256)}

        def _fetch(self, pages):
            calls.append(list(pages))
            return {p: bytes([p >> 8 & 0xFF]) * 256 for p in pages}

    g = G()
    g.prefetch([(0x3AB8, 80 * 3), (0x8AB8, 10), (0xC1FFF0, 0x20)])
    assert calls == [[0x3B00, 0x8A00, 0xC1FF00, 0xC20000]]
    assert g.read(0x3B10, 2) == b"\x3b\x3b" and g.read(0x3AFF, 2) == b"\0\x3b"
    g.prefetch([(0x3B00, 16)])
    assert len(calls) == 1                      # all cached: no park at all
