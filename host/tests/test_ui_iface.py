"""W15-U fix round 1: independent checks of the UI against I-1.

* M3 (rev15-ui): the host model's planes, decoded back to text cell by cell
  (tests.run_modui.decode), against the text each field must show written
  from I-1 alone (tests.run_modui.expect_fields), over special and random
  views.  Any disagreement is a bug in the model or in the expectation.
* L3: the mod_view offsets ui.s uses (MV_*, CH_*) against the C header
  (fixtures/mod/ui/mod_view_mirror.h, W15-M's mod_view.h mirrored; also
  against W15-M's own copy when that worktree is present).
* B1: the font against the C64 character ROM (evidence-only; skipped when
  the ROM is not installed locally).
"""

import os
import random
import re

import pytest

from host import ui_preview as U
from tests import run_modui as M

UI_S = os.path.join(U.ROOT, "fixtures", "mod", "ui", "ui.s")
MIRROR = os.path.join(U.ROOT, "fixtures", "mod", "ui", "mod_view_mirror.h")
W15M = os.path.join(U.ROOT, "..", "w15-mod", "fixtures", "mod", "mod_view.h")


def screen(v):
    s = U.Screen()
    U.player_static(s)
    U.player_frame(s, v, U.Ring())
    return s


def random_view(rnd):
    v = U.mod_view_sample()
    v.update(name=bytes(rnd.randrange(256) for _ in range(20)),
             songlen=rnd.randrange(256), pos=rnd.randrange(256),
             pattern=rnd.randrange(256), row=rnd.randrange(256),
             speed=rnd.randrange(256), bpm=rnd.randrange(256),
             flags=rnd.randrange(256), time=rnd.randrange(1 << 32))
    for c in v["ch"]:
        c.update(inst=rnd.randrange(256), vol=rnd.randrange(256),
                 note=rnd.randrange(256), fx=rnd.randrange(256),
                 param=rnd.randrange(256), vu=rnd.randrange(256))
    return v


@pytest.mark.parametrize("name,view", M.special() + [("extreme", M.extreme()),
                                                     ("sample", U.mod_view_sample())])
def test_decode_special(name, view):
    s = screen(view)
    assert M.decode_check(bytes(s.mem), view, name) == []


def test_decode_random():
    rnd = random.Random(1515)
    for i in range(60):
        v = random_view(rnd)
        assert M.decode_check(bytes(screen(v).mem), v, f"random {i}") == []


def test_decode_catches_a_wrong_field():
    v = U.mod_view_sample()
    s = screen(v)
    w = dict(v, bpm=v["bpm"] + 1)
    assert M.decode_check(bytes(s.mem), w, "bpm+1")


def _equates():
    eq = {}
    for ln in open(UI_S):
        m = re.match(r"(MV_\w+|CH_\w+)\s+equ\s+(\d+)", ln)
        if m:
            eq[m.group(1)] = int(m.group(2))
    return eq


def _struct_offsets(path):
    """Member offsets of struct mod_chan and struct mod_view (no padding
    is needed: every member is naturally aligned in I-1)."""
    src = open(path).read()
    size = {"unsigned long": 4, "unsigned short": 2, "unsigned char": 1, "char": 1}
    out = {}
    for name in ("mod_chan", "mod_view"):
        body = re.search(r"struct %s \{(.*?)\};" % name, src, re.S).group(1)
        off = 0
        for m in re.finditer(r"^\s*(unsigned long|unsigned short|unsigned char|char|"
                             r"struct mod_chan)\s+(\w+)(?:\[(\d+)\])?;", body, re.M):
            t, field, n = m.group(1), m.group(2), int(m.group(3) or 1)
            out[(name, field)] = off
            off += (8 if t == "struct mod_chan" else size[t]) * n
        out[(name, "sizeof")] = off
    return out


@pytest.mark.parametrize("path", [MIRROR, W15M])
def test_mod_view_offsets(path):
    if not os.path.exists(path):
        pytest.skip(f"{path} not present")
    o, eq = _struct_offsets(path), _equates()
    assert o[("mod_view", "sizeof")] == 72 and o[("mod_chan", "sizeof")] == 8
    for f, e in (("name", "MV_NAME"), ("song_length", "MV_LEN"), ("position", "MV_POS"),
                 ("pattern", "MV_PAT"), ("row", "MV_ROW"), ("speed", "MV_SPD"),
                 ("bpm", "MV_BPM"), ("flags", "MV_FLAGS"), ("ch", "MV_CH"),
                 ("elapsed", "MV_ELAPSED")):
        assert o[("mod_view", f)] == eq[e], f
    for f, e in (("instrument", "CH_INST"), ("volume", "CH_VOL"), ("note", "CH_NOTE"),
                 ("effect", "CH_FX"), ("param", "CH_PAR"), ("vu", "CH_VU")):
        assert o[("mod_chan", f)] == eq[e], f


def test_mirror_is_w15m_copy():
    if not os.path.exists(W15M):
        pytest.skip("W15-M's worktree is not present")
    assert open(MIRROR, "rb").read() == open(W15M, "rb").read()


def test_pack_mod_view_matches_header():
    o = _struct_offsets(MIRROR)
    v = U.mod_view_sample()
    b = U.pack_mod_view(v)
    assert b[o[("mod_view", "bpm")]] == v["bpm"]
    assert b[o[("mod_view", "ch")] + 8 * 2 + o[("mod_chan", "vu")]] == v["ch"][2]["vu"]


def test_font_vs_c64_rom():
    rc, text = M.font_vs_rom()
    if "SKIPPED" in text:
        pytest.skip(text)
    assert rc == 0, text
