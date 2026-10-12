"""W15-U: the readability rules (docs/UI_READABLE.md) on the host model.

Green: the font sample and the player screen pass R1-R7 and F1-F3 in both
views.  Teeth: every mutant in ui_preview.MUTANTS fails, each on the rule
it was made to break."""

import pytest

from host import c2v_ref as R
from host import ui_preview as U


def test_font_rules():
    font = U.load_font()
    assert U.check_font(font) == []
    assert U.min_distance(font)[0] >= U.DISTINCT_MIN


def test_font_covers_the_kit():
    font = U.load_font()
    for c in list(range(32, 96)) + [U.GLYPH_PLAY, U.GLYPH_STOP, U.GLYPH_PAUSE]:
        assert c in font
    for c in range(256):                    # any byte shows some glyph
        assert U.glyph_code(c) in font


def test_palette_maps_to_intended():
    lut = U.lut()
    for p, (ocs, vic) in enumerate(zip(U.PALETTE, U.INTENDED)):
        assert lut[ocs] == vic, f"pen {p}"


@pytest.mark.parametrize("which", ["font", "player"])
def test_screens_pass(which):
    s = U.Screen()
    if which == "font":
        U.font_sample(s)
    else:
        U.player_static(s)
        U.player_frame(s, U.mod_view_sample(), U.sample_trail())
    assert U.check_all(s) == []


def test_player_any_view_values():
    """I-1: the UI tolerates any value (clamped), and stays readable."""
    v = U.mod_view_sample()
    v.update(name=bytes(range(200, 220)), songlen=255, pos=255, pattern=255,
             row=255, speed=255, bpm=255, flags=255, time=0xFFFFFFFF)
    for c in v["ch"]:
        c.update(inst=255, vol=255, note=200, fx=255, param=255, vu=255)
    s = U.Screen()
    U.player_static(s)
    U.player_frame(s, v)
    assert U.check_all(s, views=(0,)) == []


def test_pack_mod_view():
    b = U.pack_mod_view(U.mod_view_sample())
    assert len(b) == 72 and b[:4] == b"MVW1"


def test_display_decision():
    d = R.disp_decide(U.Screen().disp_st())
    assert d["mode"] == R.MODE_HIRES and d["lines"] == 200 and d["planes"] == 3


EXPECT = {"thin": "R2", "stroke1": "F2", "close": "R4", "close2": "R4",
          "same": "F3", "lores": "R1", "tall": "R6", "cell": "R5"}


@pytest.mark.parametrize("name", U.MUTANTS)
def test_mutant_killed(name):
    f = U.check_all(U.mutant(name))
    assert f, f"mutant {name} survived"
    assert any(x.startswith(EXPECT[name]) for x in f), f


def test_generated_includes_pinned():
    """fixtures/mod/ui/font.i and ui_tab.i are the generator's output."""
    assert open(U.FONT_INC).read() == U.font_inc()
    assert open(U.TAB_INC).read() == U.tab_inc()


@pytest.mark.parametrize("variant", sorted(U.TRAIL))
def test_trail_variants_pass(variant):
    s = U.Screen()
    U.player_static(s, variant)
    U.player_frame(s, U.mod_view_sample(), U.sample_ring(), variant)
    assert U.check_all(s) == []
    assert U.trail_rows(variant)[-1] == 24


def test_ring_captures_position_row_changes():
    r = U.Ring()
    v = U.mod_view_sample()
    for row in (1, 1, 2, 2, 2, 3):
        r.capture({**v, "row": row})
    assert [x[0] for x in r.rows] == [1, 2, 3]
    r.capture({**v, "row": 3, "pos": 9})          # same row, new position
    assert len(r.rows) == 4
    for k in range(20):
        r.capture({**v, "row": 10 + k})
    assert len(r.rows) == U.RING
