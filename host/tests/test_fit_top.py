"""W14-D: the fit view keeps the top of the screen (docs/WAVE14_BRIEF.md
W14-D; CONTRACTS "Display refresh (S3.5)").  A picture taller than 200
lines is cropped from the bottom: DIWSTRT's first 200 lines are shown."""

from host import c2v_ref as R


def _st(lines, bplcon0=0xC200, lace=False):
    vstart = 0x2C
    stop = (vstart + lines) & 0x1FF
    return {"gen": 0, "dmacon": 0x0380, "bplcon0": bplcon0 | (4 if lace else 0),
            "bplpt": [0x10000 + p * 0x8000 for p in range(6)],
            "bpl1mod": 80 if lace else 0, "bpl2mod": 80 if lace else 0,
            "diwstrt": vstart << 8 | 0x81, "diwstop": (stop & 0xFF) << 8 | 0xC1,
            "ddfstrt": 0x3C, "ddfstop": 0xD4, "color": list(range(32))}


def test_fit_crop_is_zero_for_tall_pictures():
    for lines in (201, 256, 283):
        d = R.disp_decide(_st(lines))
        assert d["lines"] == 200
        assert d["src"][0] == 0x10000          # row 0: DIWSTRT's first line
    assert R.disp_decide_view(_st(256))["crop"] == 0


def test_fit_lace_also_from_the_top():
    d = R.disp_decide_view(_st(256, lace=True))
    assert d["crop"] == 0 and d["src"][0] == 0x10000


def test_short_pictures_unchanged():
    d = R.disp_decide(_st(180))
    assert d["lines"] == 180 and d["src"][0] == 0x10000


def test_one_to_one_default_is_the_top():
    assert R.ONE_PY_DEFAULT == 0
