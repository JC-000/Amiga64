"""W15-M: host/modgen.py writes valid M.K. modules (layout, sizes, cells)."""

from __future__ import annotations

import struct

import pytest

from host import mod_ref, modgen
from host.modgen import HEADER_LEN, PERIODS, Sample, Song, pattern


def test_documented_periods():
    # the 36 finetune-0 periods of the public format description
    assert PERIODS[:12] == [856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453]
    assert PERIODS[12:24] == [428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226]
    assert PERIODS[24:] == [214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113]


@pytest.mark.parametrize("name,idx", [("C-1", 0), ("C#1", 1), ("B-1", 11), ("C-2", 12),
                                      ("A-3", 33), ("B-3", 35)])
def test_note_index(name, idx):
    assert modgen.note_index(name) == idx
    assert modgen.note_name(idx) == name


def test_note_out_of_range():
    with pytest.raises(ValueError):
        modgen.note_index("C-4")


def test_cell_packing():
    # sample 0x1A, period 0x358 (856), effect C, param 0x20
    assert modgen.pack_cell(856, 0x1A, 0xC, 0x20) == bytes([0x13, 0x58, 0xAC, 0x20])
    assert modgen.parse_cell("C-1 1A C20") == (856, 0x1A, 0xC, 0x20)
    assert modgen.parse_cell("") == (0, 0, 0, 0)
    assert modgen.parse_cell("--- .. E62") == (0, 0, 0xE, 0x62)
    with pytest.raises(ValueError):
        modgen.parse_cell("C-1 20")                     # instrument 32


def test_pattern_size_and_rows():
    p = pattern({0: ["C-2 01"], 63: ["", "", "", "B-3 02 F06"]})
    assert len(p) == 1024
    assert p[:4] == modgen.pack_cell(428, 1, 0, 0)
    assert p[1020:] == modgen.pack_cell(113, 2, 0xF, 6)
    assert p[4:1020] == bytes(1016)


def test_sample_record():
    s = Sample("x", bytes(64), finetune=-2, volume=40, loop_start=8, loop_len=32)
    r = s.record()
    assert len(r) == 30 and r[:1] == b"x" and r[1:22] == bytes(21)
    assert struct.unpack(">HBBHH", r[22:]) == (32, 0x0E, 40, 4, 16)
    one = Sample("o", bytes(10)).record()             # no loop: start 0, length 1
    assert struct.unpack(">HBBHH", one[22:]) == (5, 0, 64, 0, 1)
    with pytest.raises(ValueError):
        Sample("odd", bytes(3)).record()


def test_header_layout():
    song = Song("hdr", [pattern({}), pattern({0: ["C-1 01"]})], [1, 0, 1], restart=1,
                title="a title that is longer than twenty")
    raw = song.build()
    assert raw[:20] == b"a title that is long"
    assert raw[950] == 3 and raw[951] == 1
    assert raw[952:955] == bytes([1, 0, 1]) and raw[955:1080] == bytes(125)
    assert raw[1080:1084] == b"M.K."
    npat = 2
    samples = sum(len(s.data) for s in modgen.std_samples())
    assert len(raw) == HEADER_LEN + npat * 1024 + samples
    # pattern 1 is second in the file
    assert raw[HEADER_LEN + 1024:HEADER_LEN + 1028] == modgen.pack_cell(856, 1, 0, 0)


def test_song_errors():
    with pytest.raises(ValueError):
        Song("x", [pattern({})], [1]).build()
    with pytest.raises(ValueError):
        Song("x", [pattern({})], []).build()
    with pytest.raises(ValueError):
        Song("x", [pattern({})], [0], samples=[Sample()] * 32).build()


@pytest.mark.parametrize("name", sorted(modgen.SONGS))
def test_songs_parse_back(name):
    raw = modgen.SONGS[name]().build()
    m = mod_ref.parse(raw)
    song = modgen.SONGS[name]()
    assert m.song_len == len(song.order)
    assert m.npat == max(song.order) + 1
    for i, s in enumerate(song.samples):
        ins = m.instruments[i]
        assert ins.length == len(s.data)
        assert raw[ins.offset:ins.offset + ins.length] == s.data
        assert ins.volume == s.volume and ins.finetune == s.finetune
        if s.loop_len > 2:
            assert (ins.rep_start, ins.rep_len) == (s.loop_start, s.loop_len)
        else:
            assert (ins.rep_start, ins.rep_len) == (0, 2)
    last = m.instruments[len(song.samples) - 1]
    assert last.offset + last.length == len(raw)


def test_one_shots_start_with_silence():
    # the one-word repeat block of a one-shot sample is its first word: 0
    for s in modgen.std_samples():
        if s.loop_len <= 2:
            assert s.data[:2] == b"\0\0", s.name


def test_waveforms_signed_and_even():
    for s in modgen.std_samples():
        assert len(s.data) % 2 == 0 and len(s.data) >= 32
        vals = [b - 256 if b & 0x80 else b for b in s.data]
        assert min(vals) < 0 < max(vals), s.name


def test_cli(tmp_path):
    assert modgen.main(["--out", str(tmp_path), "--song", "arp"]) == 0
    assert (tmp_path / "arp.mod").read_bytes() == modgen.SONGS["arp"]().build()
