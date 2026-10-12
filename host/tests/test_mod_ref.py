"""W15-M: host/mod_ref.py, the reference replay (effect semantics, the Paula
write protocol, the mod_view encoding).  Expected values are worked out by
hand from the rules in mod_ref.SEMANTICS, not taken from the model."""

from __future__ import annotations

import struct

import pytest

from host import mod_ref, modgen
from host.mod_ref import AUD, DMACON, SINE, Replay, parse, play
from host.modgen import Song, pattern

BASE = 0x080000
S1 = BASE + 1084 + 1024          # instrument 1 (sine, 32 bytes, looped) in a 1-pattern song
LEN = {i + 1: len(s.data) for i, s in enumerate(modgen.std_samples())}


def ins_addr(n: int, npat: int = 1) -> int:
    return BASE + 1084 + 1024 * npat + sum(LEN[i] for i in range(1, n))


def song(rows, order=(0,), pats=None, **kw) -> bytes:
    pats = pats or [pattern(rows)]
    return Song("t", pats, list(order), **kw).build()


def run(rows, n=64, **kw):
    ticks, r = play(song(rows), n, **kw)
    return ticks, r


def view(t) -> dict:
    v = t.view
    d = {"magic": v[:4], "ticks": struct.unpack(">I", v[4:8])[0], "name": v[8:28],
         "len": v[28], "pos": v[29], "pat": v[30], "row": v[31], "speed": v[32],
         "bpm": v[33], "flags": v[34], "pad": v[35],
         "elapsed": struct.unpack(">I", v[68:72])[0], "ch": []}
    for c in range(4):
        per, ins, vol, note, eff, par, vu = struct.unpack(">HBBBBBB", v[36 + 8 * c:44 + 8 * c])
        d["ch"].append(dict(per=per, ins=ins, vol=vol, note=note, eff=eff, par=par, vu=vu))
    return d


def per(t, c=0):
    return view(t)["ch"][c]["per"]


def vol(t, c=0):
    return view(t)["ch"][c]["vol"]


# --- tables -----------------------------------------------------------------------------

def test_sine_table():
    assert SINE[:9] == [0, 24, 49, 74, 97, 120, 141, 161, 180]
    assert SINE[16] == 255 and SINE[31] == 24
    assert len(SINE) == 32


def test_finetune_rows():
    assert mod_ref.PERIOD_TABLE[0] == modgen.PERIODS
    assert mod_ref.PERIOD_TABLE[1][0] == round(856 * 2 ** (-1 / 96))     # 850
    assert mod_ref.PERIOD_TABLE[-8][0] == 907
    for f in range(-8, 8):
        row = mod_ref.PERIOD_TABLE[f]
        assert all(a > b for a, b in zip(row, row[1:]))


def test_nearest_note():
    assert mod_ref.nearest_note(428) == 12
    assert mod_ref.nearest_note(430) == 12
    assert mod_ref.nearest_note(1000) == 0
    assert mod_ref.nearest_note(50) == 35


def test_cia_period_and_elapsed():
    assert mod_ref.tick_period_e(125) == 14188           # one guest frame
    assert mod_ref.tick_period_e(32) == 55421
    assert mod_ref.tick_period_e(255) == 6954
    assert mod_ref.tick_period_e(125, 20455) == 20455    # real-time pacing, s = 0
    assert mod_ref.elapsed_inc(125) == 0x10000


# --- the write protocol -------------------------------------------------------------------

def test_init_and_end_writes():
    assert mod_ref.init_writes() == [(DMACON, 0xF), (0xA8, 0), (0xB8, 0), (0xC8, 0), (0xD8, 0)]
    assert mod_ref.END_WRITES == [(DMACON, 0xF)]


def test_trigger_writes():
    ticks, _ = run({0: ["C-2 01"]}, 2)
    assert ticks[0].writes == [
        (DMACON, 0x0001),
        (0xA0, S1 >> 16), (0xA2, S1 & 0xFFFF), (0xA4, 16),
        (0xA6, 428), (0xA8, 64),
        (DMACON, 0x8001),
        (0xA0, S1 >> 16), (0xA2, S1 & 0xFFFF), (0xA4, 16)]
    assert ticks[1].writes == []                          # nothing changed


def test_trigger_two_channels_and_oneshot_repeat():
    ticks, _ = run({0: ["", "C-2 04", "", "E-2 06"]}, 1)
    n4, n6 = ins_addr(4), ins_addr(6)
    assert ticks[0].writes == [
        (DMACON, 0x000A),
        (0xB0, n4 >> 16), (0xB2, n4 & 0xFFFF), (0xB4, LEN[4] // 2),
        (0xD0, n6 >> 16), (0xD2, n6 & 0xFFFF), (0xD4, LEN[6] // 2),
        (0xB6, 428), (0xB8, 40), (0xD6, 339), (0xD8, 32),
        (DMACON, 0x800A),
        (0xB0, n4 >> 16), (0xB2, n4 & 0xFFFF), (0xB4, 1),           # one-shot: 1 word
        (0xD0, (n6 + 64) >> 16), (0xD2, (n6 + 64) & 0xFFFF), (0xD4, 32)]


def test_instrument_without_note_sets_volume_only():
    ticks, _ = run({0: ["C-2 01 C20"], 1: ["--- 02"]}, 7)
    assert vol(ticks[0]) == 0x20
    assert ticks[6].writes == [(0xA8, 48)]               # no DMACON: no trigger
    assert view(ticks[6])["ch"][0]["ins"] == 2


def test_note_without_any_instrument_stops_channel():
    ticks, _ = run({0: ["C-2 .."]}, 1)
    assert ticks[0].writes == [(DMACON, 1), (0xA6, 428), (0xA8, 0)]


# --- effects -------------------------------------------------------------------------------

def test_arpeggio():
    ticks, _ = run({0: ["C-2 01 037"], 1: ["--- .. 0C0"]}, 12)
    assert [per(t) for t in ticks[:6]] == [428, 360, 285, 428, 360, 285]
    # +12 from C-2 = C-3 (214); +0 -> C-2
    assert [per(t) for t in ticks[6:9]] == [428, 214, 428]


def test_arpeggio_capped_at_b3():
    ticks, _ = run({0: ["A-3 01 0F1"]}, 3)
    assert [per(t) for t in ticks] == [127, 113, 120]


def test_portamento_up_down_and_clamps():
    ticks, _ = run({0: ["C-1 01 110", "B-3 02 220"], 1: ["--- .. 1FF", "--- .. 2FF"]}, 12)
    assert [per(t) for t in ticks[:6]] == [856, 840, 824, 808, 792, 776]
    assert [per(t, 1) for t in ticks[:6]] == [113, 145, 177, 209, 241, 273]
    assert [per(t) for t in ticks[6:10]] == [776, 521, 266, 113]
    assert [per(t, 1) for t in ticks[6:10]] == [273, 528, 783, 856]


def test_fine_slides_tick0_only():
    ticks, _ = run({0: ["C-2 01 E13", "C-2 01 E25"], 1: ["--- .. E1F", "--- .. E2F"]}, 12)
    assert [per(t) for t in ticks[:6]] == [425] * 6
    assert [per(t, 1) for t in ticks[:6]] == [433] * 6
    assert per(ticks[6]) == 410 and per(ticks[6], 1) == 448


def test_tone_portamento():
    ticks, _ = run({0: ["C-1 01"], 1: ["C-3 .. 308"], 2: ["--- .. 300"],
                    3: ["C-1 .. 311"], 4: ["--- .. 300"]}, 64)
    row1 = ticks[6:12]
    assert all(w[0] != DMACON for t in row1 for w in t.writes)   # no trigger
    assert [per(t) for t in row1] == [856, 848, 840, 832, 824, 816]
    assert per(ticks[17]) == 816 - 40                     # 300 keeps speed 8
    # back down to C-1 at 17/tick (776 + 85 > 856): never past the target
    assert [per(t) for t in ticks[18:24]] == [776, 793, 810, 827, 844, 856]
    assert max(per(t) for t in ticks[18:]) == 856
    assert per(ticks[-1]) == 856


def test_tone_portamento_reaches_target_and_sets_note():
    ticks, r = run({0: ["C-2 01"], 1: ["C#2 .. 3FF"], 2: ["--- .. 047"]}, 15)
    assert per(ticks[7]) == 404
    assert [per(t) for t in ticks[12:15]] == [404, 320, 269]      # arpeggio from C#2


def test_vibrato():
    ticks, _ = run({0: ["C-2 01 448"]}, 6)
    off = lambda p: SINE[p & 31] * 8 >> 7
    assert [per(t) for t in ticks] == [428, 428 + off(0), 428 + off(4), 428 + off(8),
                                        428 + off(12), 428 + off(16)]
    assert per(ticks[2]) == 434


def test_vibrato_negative_half_and_memory():
    ticks, _ = run({0: ["C-2 01 4F2"], 1: ["--- .. 400"], 2: ["--- .. 600"]}, 18)
    # speed 15: positions 0, 15, 30, 45 (neg), 60 (neg) ...
    assert per(ticks[3]) == 428 + (SINE[30] * 2 >> 7)
    assert per(ticks[4]) == 428 + (-SINE[45 & 31] * 2 >> 7)
    assert per(ticks[6]) == 428                         # tick 0: no offset
    assert per(ticks[7]) == 428 + (SINE[75 & 63] * 2 >> 7)    # pos 11: continues


def test_vibrato_waves_and_retrigger_bit():
    ticks, _ = run({0: ["C-2 01 E41"], 1: ["C-2 .. 488"]}, 9)
    assert per(ticks[7]) == 428 + (255 * 8 >> 7)        # ramp at pos 0: 255
    t2, _ = run({0: ["C-2 01 E42"], 1: ["C-2 .. 488"]}, 9)
    assert per(t2[8]) == 428 + (255 * 8 >> 7)          # square
    t3, _ = run({0: ["C-2 01 488"], 1: ["--- .. E44"], 2: ["C-2 .. 400"]}, 14)
    # wave bit 2: the new note at row 2 keeps the position (5 steps of 8 = 40)
    assert per(t3[13]) == 428 + (-SINE[(40 + 0) & 31] * 8 >> 7)


def test_tremolo():
    ticks, _ = run({0: ["C-2 01 748"], 1: ["--- .. C20"]}, 8)
    assert [vol(t) for t in ticks[:3]] == [64, 64, 64]   # +, clamped at 64
    t2, _ = run({0: ["C-2 01 C20"], 1: ["--- .. 7F8"]}, 12)
    assert vol(t2[7]) == 32
    assert vol(t2[8]) == 32 + (SINE[15] * 8 >> 6)
    assert vol(t2[9]) == 32 + (SINE[30] * 8 >> 6)
    assert vol(t2[10]) == 32 + (-SINE[45 & 31] * 8 >> 6)


def test_volume_slides_and_clamps():
    ticks, _ = run({0: ["C-2 01 A0F", "C-2 02 AF0"], 1: ["--- .. A01", "--- .. A30"]}, 12)
    assert [vol(t) for t in ticks[:6]] == [64, 49, 34, 19, 4, 0]
    assert [vol(t, 1) for t in ticks[:6]] == [48, 63, 64, 64, 64, 64]
    assert vol(ticks[11]) == 0
    assert vol(ticks[11], 1) == 64


def test_set_volume_and_fine_volume():
    ticks, _ = run({0: ["C-2 01 C50", "C-2 02 EA4", "C-2 06 EB8", "C-2 03 CFF"],
                    1: ["", "--- .. EAF", "--- .. EBF", ""]}, 7)
    assert [vol(ticks[0], c) for c in range(4)] == [64, 52, 24, 64]
    assert [vol(ticks[6], c) for c in range(4)] == [64, 64, 9, 64]


def test_sample_offset():
    ticks, r = run({0: ["C-2 05 902"], 1: ["C-2 05 900"], 2: ["--- .. 904"],
                    3: ["C-2 05 ..."]}, 19)
    a5 = ins_addr(5)
    assert ticks[0].writes[1:4] == [(0xA0, (a5 + 512) >> 16), (0xA2, (a5 + 512) & 0xFFFF),
                                    (0xA4, (LEN[5] - 512) // 2)]
    assert ticks[6].writes[2] == (0xA2, (a5 + 512) & 0xFFFF)     # 900: memory
    assert ticks[18].writes[1:4] == [(0xA0, a5 >> 16), (0xA2, a5 & 0xFFFF),
                                     (0xA4, LEN[5] // 2)]


def test_sample_offset_past_end():
    ticks, _ = run({0: ["C-2 05 9FF", "C-2 06 9FF"]}, 1)
    a6 = ins_addr(6)
    assert ticks[0].writes == [
        (DMACON, 0x0003),
        (0xB0, (a6 + 64) >> 16), (0xB2, (a6 + 64) & 0xFFFF), (0xB4, 32),
        (0xA6, 434), (0xA8, 60), (0xB6, 428), (0xB8, 32),   # pluck: finetune -2
        (DMACON, 0x8002),                                   # the one-shot stopped
        (0xB0, (a6 + 64) >> 16), (0xB2, (a6 + 64) & 0xFFFF), (0xB4, 32)]


def test_retrigger_cut_delay():
    ticks, _ = run({0: ["C-2 05 E92", "C-2 04 EC3", "C-2 01 ED2", "--- .. ED1"]}, 6)
    trig = lambda t, c: any(w == (DMACON, 1 << c) or (w[0] == DMACON and not w[1] & 0x8000
                                                     and w[1] & (1 << c)) for w in t.writes)
    assert [trig(t, 0) for t in ticks] == [True, False, True, False, True, False]
    assert [vol(t, 1) for t in ticks] == [40, 40, 40, 0, 0, 0]
    assert [trig(t, 2) for t in ticks] == [False, False, True, False, False, False]
    assert per(ticks[1], 2) == 0 and per(ticks[2], 2) == 428
    assert not any(trig(t, 3) for t in ticks)          # ED without a note
    assert [view(t)["ch"][0]["vu"] for t in ticks] == [60, 58, 60, 58, 60, 58]


def test_cut_at_tick0():
    ticks, _ = run({0: ["C-2 01 EC0"]}, 1)
    assert vol(ticks[0]) == 0


def test_speed_and_tempo():
    ticks, r = run({0: ["C-2 01 F03"], 1: ["--- .. F96"], 2: ["--- .. F00"]}, 12)
    assert [view(t)["row"] for t in ticks[:9]] == [0, 0, 0, 1, 1, 1, 2, 2, 2]
    assert view(ticks[2])["bpm"] == 125 and view(ticks[3])["bpm"] == 150
    assert [t.cia for t in ticks[2:6]] == [False, False, True, True]
    assert view(ticks[3])["flags"] == 1                  # CIA from the next tick
    assert view(ticks[4])["flags"] == 1 | 4
    assert r.ignored == {"F00": 1}
    # elapsed: 3 frames at 125, then (125<<16)//150 per tick from tick 4 on
    acc = 3 * 0x10000 + 7 * ((125 << 16) // 150)
    assert view(ticks[9])["elapsed"] == acc >> 16


def test_position_jump_and_break():
    pats = [pattern({0: ["C-2 01"], 1: ["", "--- .. B02", "", "--- .. D07"]}),
            pattern({}), pattern({7: ["E-2 01"], 8: ["--- .. D99"]})]
    ticks, r = play(song(None, order=[0, 1, 2], pats=pats), 100)
    rows = [(view(t)["pos"], view(t)["row"]) for t in ticks[::6]]
    assert rows[:4] == [(0, 0), (0, 1), (2, 7), (2, 8)]
    # D99 -> row 0 of position 3 = the end: looped, halted (stop flag)
    assert len(ticks) == 24 and r.halted and r.looped


def test_break_decimal():
    pats = [pattern({0: ["", "", "", "--- .. D12"]}), pattern({12: ["C-2 01"]})]
    ticks, _ = play(song(None, order=[0, 1], pats=pats), 7)
    assert (view(ticks[6])["pos"], view(ticks[6])["row"]) == (1, 12)


def test_pattern_loop():
    ticks, _ = run({1: ["--- .. E60"], 3: ["--- .. E62"]}, 200)
    rows = [view(t)["row"] for t in ticks[::6]][:12]
    assert rows == [0, 1, 2, 3, 1, 2, 3, 1, 2, 3, 4, 5]


def test_pattern_delay():
    ticks, _ = run({0: ["C-2 01 037", "", "", "--- .. EE2"], 1: ["E-2 01"]}, 20)
    assert [view(t)["row"] for t in ticks[:19]] == [0] * 18 + [1]
    assert [per(t) for t in ticks[15:18]] == [428, 360, 285]      # phase = t % 3


def test_song_loop_without_stop():
    s = song({0: ["C-2 01"]}, order=[0, 0], restart=1)
    ticks, r = play(s, 64 * 6 * 3, stop_at_end=False)
    assert len(ticks) == 64 * 6 * 3 and r.looped and not r.halted
    assert view(ticks[64 * 6 * 2])["pos"] == 1             # restart position
    assert view(ticks[64 * 6 * 2])["flags"] & 2
    assert not view(ticks[64 * 6 * 2 - 1])["flags"] & 2


def test_stop_at_end():
    ticks, r = play(song({}), 10_000)
    assert len(ticks) == 64 * 6 and r.halted


def test_backwards_jump_is_a_loop():
    ticks, r = run({2: ["--- .. B00"]}, 1000)
    assert len(ticks) == 18 and r.looped


def test_ignored_effects_counted():
    _, r = run({0: ["C-2 01 E01", "--- .. 812", "--- .. E31", "--- .. EF1"]}, 1)
    assert r.ignored == {"E0": 1, "8": 1, "E3": 1, "EF": 1}


def test_view_layout():
    raw = Song("view", [pattern({0: ["C-2 03 A01", "", "C#3 02 C30"]})], [0],
               title="name").build()
    ticks, _ = play(raw, 2)
    v = view(ticks[1])
    assert v["magic"] == b"MVW1" and v["ticks"] == 2 and v["name"] == b"name" + bytes(16)
    assert (v["len"], v["pos"], v["pat"], v["row"], v["speed"], v["bpm"]) == (1, 0, 0, 0, 6, 125)
    assert v["flags"] == 1 and v["pad"] == 0 and v["elapsed"] == 2
    p3 = mod_ref.PERIOD_TABLE[3][12]
    assert v["ch"][0] == dict(per=p3, ins=3, vol=55, note=12, eff=0xA, par=1, vu=54)
    assert v["ch"][1] == dict(per=0, ins=0, vol=0, note=0xFF, eff=0, par=0, vu=0)
    assert v["ch"][2] == dict(per=202, ins=2, vol=48, note=25, eff=0xC, par=0x30, vu=46)
    assert len(ticks[0].view) == 72


def test_parse_rejects():
    raw = bytearray(song({}))
    with pytest.raises(mod_ref.ModError):
        parse(bytes(raw[:1000]))
    bad = bytearray(raw)
    bad[1080:1084] = b"FLT4"
    with pytest.raises(mod_ref.ModError):
        parse(bytes(bad))
    bad = bytearray(raw)
    bad[950] = 0
    with pytest.raises(mod_ref.ModError):
        parse(bytes(bad))
    bad = bytearray(raw)
    bad[952] = 5                        # pattern 5 is not in the file
    with pytest.raises(mod_ref.ModError):
        parse(bytes(bad[:1084 + 2048]))


@pytest.mark.parametrize("name", sorted(modgen.SONGS))
def test_named_songs_run(name):
    ticks, r = play(modgen.SONGS[name]().build(), 5000)
    assert ticks and r.halted
    assert [t.n for t in ticks] == list(range(1, len(ticks) + 1))
    for t in ticks:
        for reg, val in t.writes:
            assert reg == DMACON or 0xA0 <= reg <= 0xD8, hex(reg)
            assert 0 <= val <= 0xFFFF
        v = view(t)
        for c in v["ch"]:
            assert 0 <= c["vol"] <= 64 and 0 <= c["vu"] <= 64
            assert c["per"] == 0 or mod_ref.PERIOD_TABLE[7][35] <= c["per"] <= 907


def test_named_songs_cover_effects():
    """Every supported command appears in some named song."""
    seen = set()
    for name, f in modgen.SONGS.items():
        m = parse(f().build())
        for p in range(m.npat):
            for row in range(64):
                for c in range(4):
                    _, _, e, x = m.cell(p, row, c)
                    seen.add(f"E{x >> 4:X}" if e == 0xE else f"{e:X}")
    want = {"0", "1", "2", "3", "4", "5", "6", "7", "9", "A", "B", "C", "D", "F",
            "E1", "E2", "E4", "E5", "E6", "E7", "E9", "EA", "EB", "EC", "ED", "EE"}
    assert want <= seen, want - seen


def test_tables_file_current():
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures", "mod", "tables.i")
    assert open(path).read() == mod_ref.tables_asm()


# --- rev15-mod fix round 1 (H1): the rows of modgen's "edge" and "restart" -------

def _edge():
    return play(modgen.SONGS["edge"]().build(), 3000)


def test_tremolo_clamps_below_zero():
    ticks, _ = _edge()
    # row 1, volume 4, 7F8: positions 0, 15, 30, 45, 60 -> 4, 4+31, 4+6, 4-28, 4-13
    assert [vol(t, 0) for t in ticks[6:12]] == [4, 4, 35, 10, 0, 0]


def test_tone_portamento_down_onto_target():
    ticks, _ = _edge()
    # 856 -> C-2 (428) at 255 per tick: 601, then 346 is clamped to the target
    assert [per(t, 1) for t in ticks[6:10]] == [856, 601, 428, 428]


def test_offset_memory_without_note():
    ticks, _ = _edge()
    a5 = ins_addr(5, npat=2)
    w = ticks[18].writes                    # row 3: C-2 05 900
    assert (0xD0, (a5 + 0x200) >> 16) in w and (0xD2, (a5 + 0x200) & 0xFFFF) in w
    assert (0xD4, (LEN[5] - 0x200) // 2) in w
    assert all(r != DMACON for r, _ in ticks[12].writes)   # row 2: 902 alone: no trigger


def test_e6_two_channels_and_ee_with_d():
    ticks, _ = _edge()
    starts = [i for i in range(len(ticks)) if i == 0 or ticks[i].view[29:32] != ticks[i - 1].view[29:32]]
    seq = [(ticks[i].view[29], ticks[i].view[31]) for i in starts]
    # ch0 E61 and ch1 E62 on row 6, both marked at row 4: ch0 re-arms whenever
    # its count is back at 0, so the loop body plays six times
    assert seq[4:23] == [(0, 4), (0, 5), (0, 6)] * 6 + [(0, 7)]
    # row 7: D05 then EE1: the row lasts 2 x speed, then position 1, row 5
    i7 = starts[22]
    assert starts[23] - i7 == 12 and seq[23] == (1, 5)


def test_e6_mark_reset_by_a_new_position():
    ticks, r = _edge()
    starts = [i for i in range(len(ticks)) if i == 0 or ticks[i].view[29:32] != ticks[i - 1].view[29:32]]
    seq = [(ticks[i].view[29], ticks[i].view[31]) for i in starts]
    # position 1: E62 at row 6 with no E60 there: the mark is row 0 (reset)
    assert seq[23:] == [(1, 5), (1, 6)] + [(1, k) for k in range(7)] * 2 + [(1, 7), (1, 8)]
    assert r.halted


def test_restart_position_without_stop():
    ticks, r = play(modgen.SONGS["restart"]().build(), 120, stop_at_end=False)
    assert [t.view[29] for t in ticks[::12]] == [0, 1, 2, 1, 2, 1, 2, 1, 2, 1]
    assert view(ticks[36])["flags"] & 2 and not view(ticks[35])["flags"] & 2


# --- M1: malformed input --------------------------------------------------------------

def _raw(rows=None, **kw):
    return bytearray(song(rows or {0: ["C-2 01", "C-2 04"]}, **kw))


def test_bad_address_rejected():
    raw = bytes(_raw())
    with pytest.raises(mod_ref.ModError):
        parse(raw, base=0x080001)
    with pytest.raises(mod_ref.ModError):
        parse(raw, base=0x200000)


def test_samples_clamped_to_the_file():
    raw = bytes(_raw())
    m = parse(raw[:1084 + 1024 + 10])           # 10 bytes of sample data left
    assert m.instruments[0].length == 10 and m.instruments[0].rep_len == 10  # the loop cut at 10
    assert all(i.length == 0 for i in m.instruments[1:])


def test_samples_clamped_to_the_mod_area():
    raw = bytes(_raw())
    m = parse(raw, base=0x200000 - 2108 - 20)   # 20 bytes of the area after the patterns
    assert m.instruments[0].length == 20
    assert m.instruments[1].length == 0


def test_loop_past_the_end():
    raw = _raw()
    struct.pack_into(">HH", raw, 20 + 26, 0x7000, 0x1000)   # sine: loop far past its 32 bytes
    assert parse(bytes(raw)).instruments[0].rep_len == 2     # dropped: one-shot
    struct.pack_into(">HH", raw, 20 + 26, 4, 100)            # starts inside, runs past
    i = parse(bytes(raw)).instruments[0]
    assert (i.rep_start, i.rep_len) == (8, 24)                # cut at the sample's end


def test_instrument_above_31_is_none():
    raw = _raw()
    raw[1084] |= 0xF0
    raw[1086] |= 0xF0                                         # instrument 255 on ch0
    ticks, _ = play(bytes(raw), 1)
    assert view(ticks[0])["ch"][0]["ins"] == 0
    assert ticks[0].writes[0] == (DMACON, 0x3)                # ch0: no instrument: stops


def test_no_period_effect_before_a_note():
    raw = song({0: ["--- 01 4F8", "--- 01 037", "--- 01 205", "--- 01 E92"],
                1: ["--- .. 1FF", "--- .. 0FF", "--- .. 310", "--- .. E91"]})
    ticks, _ = play(raw, 12)
    for t in ticks:
        assert all(view(t)["ch"][c]["per"] == 0 for c in range(4))
        assert all(w[0] not in (0xA6, 0xB6, 0xC6, 0xD6) for w in t.writes)
        assert all(w[0] != DMACON for w in t.writes)


def test_output_period_clamped():
    # finetune -8 C-1 = 907 with vibrato depth 15: never above 907
    raw = _raw({0: ["C-1 01 E58"], 1: ["--- .. 4FF"]})
    ticks, _ = play(bytes(raw), 12)
    assert max(per(t) for t in ticks) == 907
    raw = _raw({0: ["B-3 01 E57"], 1: ["--- .. 4FF"]})
    ticks, _ = play(bytes(raw), 12)
    assert min(per(t) for t in ticks[6:]) == 107


def test_final_view_clears_playing():
    ticks, r = play(song({}), 1000)
    v = r.final_view(ticks[-1].view)
    assert v[34] == ticks[-1].view[34] & ~1 and v[:34] == ticks[-1].view[:34]


def test_fine_slides_need_a_note():
    # rev15-mod round 2 (REV2_E10): E1x/E2x on a channel with no note do nothing
    raw = song({0: ["--- 01 E13", "--- 01 E25"], 2: ["--- .. 0FF", "--- .. 4F8"]})
    ticks, _ = play(raw, 18)
    for t in ticks:
        assert per(t, 0) == 0 and per(t, 1) == 0
        assert all(w[0] not in (0xA6, 0xB6) for w in t.writes)


def test_odd_cut_rounds_down():
    # rev15-mod round 2 (REV2_ODD): 17 usable sample bytes -> length 16; the
    # loop at 14 is then 2 bytes long: no loop
    raw = _raw()
    struct.pack_into(">HH", raw, 20 + 26, 7, 16)
    i = parse(bytes(raw[:1084 + 1024 + 17])).instruments[0]
    assert (i.length, i.rep_start, i.rep_len) == (16, 0, 2)
    i = parse(bytes(raw[:1084 + 1024 + 19])).instruments[0]   # 18: a 4-byte loop at 14
    assert (i.length, i.rep_start, i.rep_len) == (18, 14, 4)
