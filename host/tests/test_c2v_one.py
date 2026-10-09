"""W13-D: the 1:1 view's host reference (host/c2v_ref.py convert_one,
disp_decide_view, disp_convert_view).  docs/C2V.md "1:1 view (W13-D)"."""

import random

from host import c2v_ref as R

LUT = R.build_lut()


def _planes(cells, lines=200):
    """cells(r, cx, y, x) -> pen; 4 planes of 40-byte rows."""
    pl = [bytearray(lines * R.ONE_ROW) for _ in range(4)]
    for y in range(lines):
        for cx in range(40):
            for x in range(8):
                pen = cells(y // 8, cx, y, x)
                for p in range(4):
                    if pen >> p & 1:
                        pl[p][y * R.ONE_ROW + cx] |= 0x80 >> x
    return [bytes(b) for b in pl]


def _shown(out, y, X):
    cell = (y // 8) * 40 + X // 8
    b = out["bitmap"][(y >> 3) * 320 + (X // 8) * 8 + (y & 7)]
    s = out["screen"][cell]
    return s >> 4 if b & (0x80 >> (X & 7)) else s & 15


def test_two_pen_text_is_pixel_exact():
    rng = random.Random(1)
    pairs = {(r, cx): rng.choice([(0, 1), (2, 1), (1, 2), (0, 3)])
             for r in range(25) for cx in range(40)}
    bits = {(y, cx): rng.randrange(256) for y in range(200) for cx in range(40)}

    def pen(r, cx, y, x):
        bg, fg = pairs[r, cx]
        return fg if bits[y, cx] >> (7 - x) & 1 else bg
    pal = [0x000, 0x111, 0x222, 0x024] + [0] * 12      # the insert-disk pens
    out = R.convert_one(_planes(pen), pal, LUT)
    vic = out["vic_pens"]
    assert len({vic[0], vic[1], vic[2], vic[3]}) == 4   # separated (W12-V)
    for y in range(200):
        for X in range(320):
            assert _shown(out, y, X) == vic[pen(y // 8, X // 8, y, X % 8)]


def test_bg_is_top_left_pixel_and_fg_first_other():
    def pen(r, cx, y, x):
        if (r, cx) == (0, 0):
            return 5 if (y, x) == (0, 0) else (9 if (y, x) == (3, 6) else 5)
        return 0
    out = R.convert_one(_planes(pen), [0] * 16, LUT)
    assert out["cells"][0] == (9, 5)
    assert out["bitmap"][3] == 0x02                      # line 3: x = 6 set
    assert out["cnt"][9] == 1 and out["cnt"][5] == 1 and out["cnt"][0] == R.CNT_MAX


def test_uniform_cell_has_no_fg():
    out = R.convert_one(_planes(lambda r, cx, y, x: 4 if cx == 5 else 0), [0] * 16, LUT)
    assert out["cells"][5] == (R.NO_FG, 4)
    assert out["bitmap"] == bytes(8000)
    v = out["vic_pens"][4]
    assert out["screen"][5] == v << 4 | v


def test_many_pairs_all_exact():
    # every (fg, bg) combination of 16 pens on one screen: no table limit
    def pen(r, cx, y, x):
        bg = cx % 16
        fg = (bg + 1 + (r * 40 + cx) // 16) % 16
        fg = fg if fg != bg else (bg + 1) % 16
        return fg if (x + y) % 3 == 0 else bg
    pal = [0x000, 0xFFF, 0xF00, 0x0F0, 0x00F, 0xFF0, 0x0FF, 0xF0F,
           0x888, 0x444, 0xCCC, 0x840, 0x048, 0x480, 0x804, 0x408]
    out = R.convert_one(_planes(pen), pal, LUT)
    vic = out["vic_pens"]
    assert len(set(vic)) == 16
    for y in range(0, 200, 3):
        for X in range(320):
            assert _shown(out, y, X) == vic[pen(y // 8, X // 8, y, X % 8)]


def _st(lines, hires=True, bpu=4):
    vstart = 0x2C
    stop = (vstart + lines) & 0x1FF
    return {"gen": 0, "dmacon": 0x0380, "bplcon0": (0x8000 if hires else 0) | bpu << 12,
            "bplpt": [0x10000 + p * 0x8000 for p in range(6)], "bpl1mod": 0, "bpl2mod": 0,
            "diwstrt": vstart << 8 | 0x81, "diwstop": (stop & 0xFF) << 8 | 0xC1,
            "ddfstrt": 0x3C, "ddfstop": 0xD4, "color": list(range(32))}


def test_view_window_and_clamp():
    st = _st(256)
    fit = R.disp_decide_view(st)
    assert not fit["one"] and fit["ymax"] == 56 and fit["crop"] == 28
    one = R.disp_decide_view(st, 1, px=7, py=200)
    assert one["one"] and one["crop"] == 56 and one["px"] == 7
    assert one["src"][0] == 0x10000 + 56 * 80
    small = R.disp_decide_view(_st(180), 1, py=30)
    assert small["crop"] == 0 and small["ymax"] == 0
    mc2 = R.disp_decide_view(_st(256, hires=False, bpu=2), 1)
    assert not mc2["one"]


def test_view_convert_matches_window():
    rng = random.Random(3)
    mem = bytearray(rng.randrange(256) for _ in range(0x30000))

    def read(a, n):
        return bytes(mem[a - 0x8000:a - 0x8000 + n])
    st = _st(256)
    st["bplpt"] = [0x8000 + p * 0x6000 for p in range(6)]
    out = R.disp_convert_view(st, read, LUT, view=1, px=11, py=5)
    win = [b"".join(read(st["bplpt"][p] + (5 + y) * 80 + 11, 40) for y in range(200))
           for p in range(4)]
    ref = R.convert_one(win, st["color"][:16], LUT)
    assert out["bitmap"] == ref["bitmap"] and out["screen"] == ref["screen"]
    assert out["mck"] == R.screen_sum(ref["screen"])
    fit = R.disp_convert_view(st, read, LUT, view=0)
    assert fit["bitmap"] == R.disp_convert(st, read, LUT)["bitmap"]


def test_hires_bpu_1_to_3_convert_absent_planes_zero():
    """W13-D: hires BPU 1-3 convert (were BLANK, why DEPTH) in both views;
    the absent planes read as 0."""
    rng = random.Random(9)
    mem = bytearray(rng.randrange(256) for _ in range(0x30000))

    def read(a, n):
        return bytes(mem[a - 0x8000:a - 0x8000 + n])
    for bpu in (1, 2, 3):
        st = _st(256, bpu=bpu)
        st["bplpt"] = [0x8000 + p * 0x6000 for p in range(6)]
        st["bpl1mod"] = st["bpl2mod"] = 0
        d = R.disp_decide(st)
        assert d["mode"] == R.MODE_HIRES and d["planes"] == bpu
        fit = R.disp_convert(st, read, LUT)
        st4 = dict(st, bplcon0=0xC200)
        for p in range(bpu, 4):                 # the same picture with zero planes
            mem[st["bplpt"][p] - 0x8000:st["bplpt"][p] - 0x8000 + 256 * 80] = bytes(256 * 80)
        assert fit["bitmap"] == R.disp_convert(st4, read, LUT)["bitmap"]
        one = R.disp_convert_view(st, read, LUT, view=1, px=5, py=9)
        assert one["decide"]["one"] and one["bitmap"] == \
            R.disp_convert_view(st4, read, LUT, view=1, px=5, py=9)["bitmap"]
    assert R.disp_decide(_st(256, bpu=0))["why"] == R.WHY_DEPTH
    assert R.disp_decide(_st(256, bpu=5))["why"] == R.WHY_DEPTH


def test_workbench_2_plane_text_pixel_exact():
    """W13-D: 3.1's Workbench (hires, 2 planes, $AAA/$000/$FFF/$68B):
    title-bar text (pen 1 on pen 2) and window text (pen 1 on pen 0) are
    pixel-exact in the 1:1 view."""
    rng = random.Random(4)
    bits = {(y, cx): rng.randrange(256) for y in range(200) for cx in range(40)}

    def pen(r, cx, y, x):
        bg = 2 if r < 2 else 0
        return 1 if bits[y, cx] >> (7 - x) & 1 else bg
    pl = _planes(pen)
    out = R.convert_one(pl[:2] + [bytes(8000)] * 2, [0xAAA, 0x000, 0xFFF, 0x68B] + [0] * 12, LUT)
    vic = out["vic_pens"]
    assert len({vic[0], vic[1], vic[2]}) == 3
    for y in range(200):
        for X in range(320):
            assert _shown(out, y, X) == vic[pen(y // 8, X // 8, y, X % 8)]


def test_three_pen_cell_is_lossy_as_documented():
    """W13-D (CONTRACTS): a cell with 3+ pens: the top-left pixel's pen is
    bg, every other pen shows as fg (the first non-bg pixel's pen)."""
    def pen(r, cx, y, x):
        if (r, cx) != (0, 0):
            return 0
        return {0: 1, 1: 2, 2: 3}[x % 3]          # pens 1, 2, 3 across the cell
    out = R.convert_one(_planes(pen), [0x000, 0xF00, 0x0F0, 0x00F] + [0] * 12, LUT)
    vic = out["vic_pens"]
    assert out["cells"][0] == (2, 1)
    for x in range(8):
        assert _shown(out, 0, x) == (vic[1] if x % 3 == 0 else vic[2])


def test_lace_1to1_shows_both_fields_pixel_exact():
    """W13-D: BPLCON0 LACE: the 1:1 window is 200 consecutive frame lines
    (both fields), so 8-line text is whole; the fit view keeps one field."""
    rng = random.Random(5)
    frame = [bytearray(512 * 80) for _ in range(2)]
    pens = {}
    for y in range(512):
        for bx in range(80):
            b = rng.randrange(256) if y % 8 else 0
            for x in range(8):
                pen = 1 if b >> (7 - x) & 1 else (2 if y < 28 else 0)
                pens[y, bx * 8 + x] = pen
                for p in range(2):
                    if pen >> p & 1:
                        frame[p][y * 80 + bx] |= 0x80 >> x
    base = [0x10000, 0x20000]
    mem = {base[p] + i: v for p in range(2) for i, v in enumerate(frame[p])}

    def read(a, n):
        return bytes(mem.get(a + i, 0) for i in range(n))
    st = _st(256, bpu=2)
    st.update(bplcon0=0xA204, bplpt=base + [0] * 4, bpl1mod=80, bpl2mod=80,
              color=[0xAAA, 0x000, 0xFFF, 0x68B] + [0] * 28)
    d = R.disp_decide_view(st, 1, px=3, py=10)
    assert d["one"] and d["lace"] and d["ymax"] == 156 and d["crop"] == 10
    assert d["skip"][:2] == [0, 0] and d["lines"] == 200
    out = R.disp_convert_view(st, read, LUT, view=1, px=3, py=10)
    vic = out["vic_pens"]
    for y in range(200):
        for X in range(320):
            assert _shown(out, y, X) == vic[pens[20 + y, 24 + X]]   # frame line 2 * crop + y
    assert R.disp_decide_view(st, 0)["ymax"] == 156
    nl = dict(st, bplcon0=0xA200, bpl1mod=0, bpl2mod=0)
    d2 = R.disp_decide_view(nl, 1, px=3, py=10)
    assert d2["skip"][:2] == [0, 0] and d2["ymax"] == 56 and not d2["lace"]
