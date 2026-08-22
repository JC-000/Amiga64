"""Tests for host/fixturegen.py — byte-level encoding of built Hunk files."""

import sys
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host import fixturegen, hunk
from host.fixturegen import HunkBuilder


def u32(val: int) -> bytes:
    return val.to_bytes(4, "big")


def test_minimal_halt_exact_bytes():
    assert fixturegen.fixture_minimal_halt() == (
        u32(0x3F3)  # HUNK_HEADER
        + u32(0)  # no resident names
        + u32(1)  # table size
        + u32(0)  # first hunk
        + u32(0)  # last hunk
        + u32(1)  # hunk 0: one longword, no memory flags
        + u32(0x3E9)  # HUNK_CODE
        + u32(1)
        + bytes.fromhex("7001afff")  # MOVEQ #1,D0 ; HALT
        + u32(0x3F2)  # HUNK_END
    )


def test_code_data_reloc_exact_bytes():
    assert fixturegen.fixture_code_data_reloc() == (
        u32(0x3F3) + u32(0) + u32(2) + u32(0) + u32(1)
        + u32(2) + u32(2)  # both hunks: 2 longwords each
        + u32(0x3E9) + u32(2)
        + bytes.fromhex("2039" "00000004" "afff")  # MOVE.L (abs.L),D0 ; HALT
        + u32(0x3EC)  # HUNK_RELOC32
        + u32(1) + u32(1) + u32(2)  # 1 offset, target hunk 1, offset 2
        + u32(0)  # terminator
        + u32(0x3F2)
        + u32(0x3EA) + u32(2)  # HUNK_DATA
        + fixturegen.RELOC_FIXTURE_DATA
        + u32(0x3F2)
    )


def test_both_fixtures_parse_and_halt_terminated():
    for blob in (
        fixturegen.fixture_minimal_halt(),
        fixturegen.fixture_code_data_reloc(),
    ):
        exe = hunk.parse(blob)
        code = exe.segments[0]
        assert code.kind == "code"
        assert code.data[-2:] == fixturegen.OP_HALT.to_bytes(2, "big")


def test_code_padded_to_longword():
    b = HunkBuilder()
    b.add_code([fixturegen.OP_HALT])  # 2 bytes -> padded to 4
    exe = hunk.parse(b.build())
    assert exe.segments[0].data == bytes.fromhex("afff0000")
    assert exe.segments[0].alloc_size == 4


def test_data_padded_to_longword():
    b = HunkBuilder()
    b.add_data(b"\x01\x02\x03\x04\x05")
    exe = hunk.parse(b.build())
    assert exe.segments[0].data == b"\x01\x02\x03\x04\x05\x00\x00\x00"


def test_bss_size_rounded_up():
    b = HunkBuilder()
    b.add_code([fixturegen.OP_HALT])
    b.add_bss(1)
    exe = hunk.parse(b.build())
    assert exe.segments[1].alloc_size == 4


def test_memory_flags_round_trip():
    b = HunkBuilder()
    b.add_code([fixturegen.OP_HALT], memory_flags=1)
    b.add_data(bytes(4), memory_flags=2)
    exe = hunk.parse(b.build())
    assert [s.memory_flags for s in exe.segments] == [1, 2]


def test_reloc_offsets_round_trip_multiple_targets():
    b = HunkBuilder()
    code = b.add_code([0, 0, 0, 0, 0, 0])  # 12 bytes
    data = b.add_data(bytes(8))
    bss = b.add_bss(4)
    b.add_reloc32(code, data, [0, 8])
    b.add_reloc32(code, bss, [4])
    exe = hunk.parse(b.build())
    assert exe.segments[code].relocs == {data: [0, 8], bss: [4]}


def test_builder_hunk_indices_sequential():
    b = HunkBuilder()
    assert b.add_code([fixturegen.OP_HALT]) == 0
    assert b.add_data(bytes(4)) == 1
    assert b.add_bss(4) == 2


# --- builder validation -----------------------------------------------


def test_word_out_of_range_rejected():
    with pytest.raises(ValueError, match="16 bits"):
        fixturegen.words_to_bytes([0x10000])
    with pytest.raises(ValueError, match="16 bits"):
        fixturegen.words_to_bytes([-1])


def test_empty_build_rejected():
    with pytest.raises(ValueError, match="no hunks"):
        HunkBuilder().build()


def test_bad_memory_flags_rejected():
    with pytest.raises(ValueError, match="memory_flags"):
        HunkBuilder().add_code([fixturegen.OP_HALT], memory_flags=3)


def test_reloc_to_missing_target_rejected():
    b = HunkBuilder()
    c = b.add_code([0, 0])
    with pytest.raises(ValueError, match="target hunk 7"):
        b.add_reloc32(c, 7, [0])


def test_reloc_offset_out_of_range_rejected():
    b = HunkBuilder()
    c = b.add_code([0, 0])  # 4 bytes
    with pytest.raises(ValueError, match="out of range"):
        b.add_reloc32(c, c, [2])  # field would span bytes 2..5


def test_reloc_on_bss_hunk_rejected():
    b = HunkBuilder()
    c = b.add_code([0, 0])
    s = b.add_bss(8)
    with pytest.raises(ValueError, match="BSS"):
        b.add_reloc32(s, c, [0])
