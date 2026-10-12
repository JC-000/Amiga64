"""W15-M: the player program's host side (tests/run_mod.py): the mirror of
fixtures/mod/mod.i, the parameter block, the trace parser, the CIA timing
prediction, and the source-level checks of the replay (no device)."""

from __future__ import annotations

import os
import re
import struct

import pytest

from host import mod_ref, modgen
from tests import run_mod as R

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = os.path.join(ROOT, "fixtures", "mod")


def equates(path: str) -> dict[str, int]:
    out = {}
    for line in open(path):
        m = re.match(r"(\w+)\s+equ\s+(\$[0-9a-fA-F]+|\d+)", line)
        if m:
            v = m.group(2)
            out[m.group(1)] = int(v[1:], 16) if v.startswith("$") else int(v)
    return out


def test_mod_i_mirror():
    e = equates(os.path.join(FIX, "mod.i"))
    assert e["VIEW"] == R.VIEW == 0x400 and e["VIEW_LEN"] == R.VIEW_LEN == 72
    assert e["PARAM"] == R.PARAM and e["STATUS"] == R.STATUS
    assert (e["TR_W"], e["TR_WEND"], e["TR_T"], e["TR_TEND"], e["TR_REC"]) == (
        R.TR_W, R.TR_WEND, R.TR_T, R.TR_TEND, R.TR_REC)
    assert 1 << e["PF_STOPEND"] == R.PF_STOPEND
    assert 1 << e["PF_TRACE"] == R.PF_TRACE
    assert 1 << e["PF_REALTIME"] == R.PF_REALTIME
    # the status block layout run_mod reads
    assert (e["S_TICKS"], e["S_IGN"], e["S_MAXCU"], e["S_TOVF"], e["S_FLIM"],
            e["S_TWP"], e["S_TTP"], e["S_FLUSH"], e["S_L6"]) == (
        8, 16, 28, 29, 30, 32, 36, 40, 42)
    # the map of the M0 ruling: image below W15-U's $040000, MOD at $080000
    assert R.IMAGE == 0x010000 and R.MOD_AT == 0x080000
    assert R.TR_W >= 0xC00000 and R.TR_TEND <= 0xDC0000       # slow RAM only


def test_view_header_matches_i1():
    h = open(os.path.join(FIX, "mod_view.h")).read()
    assert "#define MOD_VIEW_ADDR   0x000400UL" in h
    assert "#define MOD_VIEW_SIZE   72" in h
    assert "MV_CIA          0x04" in h


def test_param_block():
    c = R.Case("t", modgen.songs()["arp"], R.PF_STOPEND | R.PF_TRACE, tick_e=20455,
               max_ticks=99, start=0)
    p = c.param()
    assert p[:4] == b"MPAR"
    mod, ln, fl, te, mt, st = struct.unpack_from(">IIHHIB", p, 4)
    assert p[21] == 0
    assert (mod, ln, fl, te, mt, st) == (R.MOD_AT, len(c.raw), 3, 20455, 99, 0)
    assert struct.unpack_from(">I", p, 24)[0] == c.max_frames > 0
    ch = dict(c.chunks(b"img"))
    assert ch[0] == struct.pack(">II", 0x10000, 0x10000)
    assert ch[R.MOD_AT] == c.raw and ch[R.IMAGE] == b"img"


def _stream(entries):
    return b"".join(struct.pack(">HH", r, v) for r, v in entries)


def test_parse_trace():
    s = _stream([(0x96, 0xF), (0xA8, 0), (0xFFFF, 1), (0xA6, 428), (0xFFFF, 2),
                 (0xFFFF, 3), (0x96, 0x8001), (0xFFFE, 0), (0x96, 0xF)])
    init, ticks, end = R.parse_trace(s)
    assert init == [(0x96, 0xF), (0xA8, 0)]
    assert ticks == [[(0xA6, 428)], [], [(0x96, 0x8001)]]
    assert end == [(0x96, 0xF)]
    with pytest.raises(ValueError):
        R.parse_trace(_stream([(0xFFFF, 2)]))            # a skipped tick marker
    assert R.parse_trace(_stream([(0xFFFF, 1)]))[2] is None   # no end marker


def test_rec_time():
    rec = bytes(72) + struct.pack(">II", 3, (1 << 16) | (0x20 << 8) | 100)
    # V = 256 + 32 = 288, cc = 227 * 288 + 100
    assert R.rec_time(rec, 0) == 3 * 14188 + (227 * 288 + 100) // 5


def test_predict_cia_constant_and_split():
    raw = modgen.Song("t", [modgen.pattern({0: ["C-2 01 F7D"]})], [0]).build()  # 125 BPM
    c = R.Case("t", raw, R.PF_STOPEND)
    a = next(i for i, t in enumerate(c.ticks) if t.cia)
    assert a == 1
    pred = R.predict_cia(c, a)
    assert pred[:4] == [0, 14188, 2 * 14188, 3 * 14188]
    raw = modgen.Song("t", [modgen.pattern({0: ["C-2 01 F20"]})], [0]).build()  # 32 BPM
    c = R.Case("t", raw, R.PF_STOPEND | R.PF_REALTIME, tick_e=20455)
    n, latch = mod_ref.cia_split(20455 * 125 // 32)
    assert n == 2 and latch == 39950
    pred = R.predict_cia(c, 0)
    # tick 1 -> 2: NSPLIT = 2 at once, and the first underflow still counts
    # the 125 BPM latch (a running timer reloads its new latch only then)
    assert pred[1] == 20455 * 1 + (latch + 1)
    assert pred[2] - pred[1] == 2 * (latch + 1)


def test_mutant_and_variant_defines_exist():
    src = open(os.path.join(FIX, "replay.s")).read()
    for defs in list(R.MUTANTS.values()) + list(R.VARIANTS.values()):
        for d in defs:
            assert re.search(rf"if(n?)d\s+{d}\b", src), d
    assert len(R.MUTANTS) <= 12            # 10 + rev15-mod round 2's two (waiver asked)


def test_cases_are_the_named_songs():
    names = [c.name for c in R.cases()]
    assert {"basic", "flow", "tempo", "rt_tempo", "demo_loop"} <= set(names)
    for c in R.cases():
        assert c.ticks and c.max_frames > len(c.ticks)


def test_ownership_rule():
    ok = [(0x09A, 0x7FFF), (0x09C, 0x7FFF), (0x09A, 0x2028), (0x096, 0x000F),
          (0x09A, 0xE028), (0x096, 0x8005), (0x09C, 0x0020), (0x09A, 0x2028)]
    assert R.ownership(ok) == []
    assert R.ownership(ok + [(0x09A, 0x7FFF)])          # a blanket clear after ui_init
    assert R.ownership([(0x096, 0x8200)])                # DMAEN
    assert R.ownership([(0x096, 0x7FFF)])
    assert R.ownership([(0x09A, 0x7FFF), (0x09C, 0x7FFF), (0x09A, 0x4000)])  # CLR INTEN
    assert R.ownership([(0x09A, 0x7FFF), (0x09C, 0x7FFF), (0x09C, 0x0040)])  # BLIT


def test_no_tas():
    """The 68k core halts HALT_ILLEGAL on TAS (PROGRESS.md Findings): the
    program must not use it (the busy guard is a bset)."""
    for name in ("replay.s", "crt0.s"):
        for line in open(os.path.join(FIX, name)):
            code = line.split(";")[0]
            assert not re.search(r"\btas(\.b)?\b", code, re.I), (name, line)


def test_ui_ownership_rule():
    assert R.ui_ownership([(0x096, 0x8380), (0x096, 0x8240), (0x180, 0x0FFF), (0x096, 0x8200)]) == []
    assert R.ui_ownership([(0x096, 0x8201)])            # AUD0EN from the UI
    assert R.ui_ownership([(0x0A8, 0x0040)])            # AUD0VOL from the UI
    assert R.ui_ownership([(0x09A, 0xC020)])            # INTENA from the UI


def test_memory_diff():
    before = bytes(0x100000)
    allowed = [(0, 0x10000), (0x10000, 0x12000), (0x40000, 0x80000)]
    after = bytearray(before)
    after[0x400] = 1
    after[0x11FFF] = 1
    after[0x50000] = 1
    assert R.memory_diff(before, bytes(after), allowed) == []
    after[0x80000] = 1                                  # the MOD area
    assert "$080000" in R.memory_diff(before, bytes(after), allowed)[0]
    after = bytearray(before)
    after[0x12000] = 1                                  # just past the bss
    assert R.memory_diff(before, bytes(after), allowed)


def test_malformed_cases_expectations():
    by = {c.name: c for c in R.malformed_cases()}
    assert by["m_odd_modaddr"].err_expect == 5
    assert by["m_no_mk"].err_expect == 2 and by["m_tiny"].err_expect == 2
    assert by["m_songlen0"].err_expect == 3 and by["m_songlen129"].err_expect == 3
    assert by["m_order_unplayed_40"].err_expect == 4
    for n in ("m_inst255", "m_huge_samples", "m_loop_past_end", "m_vib_no_note",
              "m_e9_no_note", "m_end_of_area", "m_period_extremes"):
        assert by[n].err_expect == 0 and by[n].ticks, n


def test_fuzz_is_seeded():
    a = [c.raw for c in R.fuzz_cases(5)]
    assert a == [c.raw for c in R.fuzz_cases(5)] and len(set(a)) == 5
