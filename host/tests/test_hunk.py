"""Tests for host/hunk.py — Hunk executable parsing."""

import sys
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host import fixturegen, hunk
from host.hunk import HunkError


def u32(val: int) -> bytes:
    return (val & 0xFFFF_FFFF).to_bytes(4, "big")


def header(sizes: list[int], table_size: int | None = None) -> bytes:
    """HUNK_HEADER with raw size longwords (flags included by caller)."""
    n = table_size if table_size is not None else len(sizes)
    return (
        u32(hunk.HUNK_HEADER)
        + u32(0)
        + u32(n)
        + u32(0)
        + u32(n - 1)
        + b"".join(u32(s) for s in sizes)
    )


def code_hunk(data: bytes) -> bytes:
    assert len(data) % 4 == 0
    return u32(hunk.HUNK_CODE) + u32(len(data) // 4) + data


# --- round-trips of generated fixtures --------------------------------


def test_parse_minimal_halt_fixture():
    exe = hunk.parse(fixturegen.fixture_minimal_halt())
    assert exe.first_hunk == 0 and exe.last_hunk == 0
    assert len(exe.segments) == 1
    seg = exe.segments[0]
    assert seg.kind == "code"
    assert seg.data == bytes.fromhex("7001afff")
    assert seg.alloc_size == 4
    assert seg.memory_flags == 0
    assert seg.relocs == {}


def test_parse_code_data_reloc_fixture():
    exe = hunk.parse(fixturegen.fixture_code_data_reloc())
    assert [s.kind for s in exe.segments] == ["code", "data"]
    code, data = exe.segments
    assert code.alloc_size == 8
    assert code.data[-2:] == bytes.fromhex("afff")
    assert code.relocs == {1: [fixturegen.RELOC_FIXTURE_FIELD_OFFSET]}
    assert data.data == fixturegen.RELOC_FIXTURE_DATA
    assert data.relocs == {}


def test_parse_bss_and_memory_flags():
    b = fixturegen.HunkBuilder()
    b.add_code([fixturegen.OP_HALT], memory_flags=2)  # FAST
    b.add_data(b"\x01\x02\x03\x04", memory_flags=1)  # CHIP
    b.add_bss(10)  # rounds up to 12 bytes
    exe = hunk.parse(b.build())
    assert [s.kind for s in exe.segments] == ["code", "data", "bss"]
    assert [s.memory_flags for s in exe.segments] == [2, 1, 0]
    assert exe.segments[2].alloc_size == 12
    assert exe.segments[2].data == b""


def test_multiple_reloc_blocks_merge_and_sort():
    """Two RELOC32 sub-blocks for the same target merge into sorted offsets."""
    data = bytes(16)
    blob = (
        header([4, 1])
        + code_hunk(data)
        + u32(hunk.HUNK_RELOC32)
        + u32(1) + u32(1) + u32(8)
        + u32(2) + u32(1) + u32(0) + u32(4)
        + u32(0)
        + u32(hunk.HUNK_END)
        + u32(hunk.HUNK_BSS) + u32(1)
        + u32(hunk.HUNK_END)
    )
    exe = hunk.parse(blob)
    assert exe.segments[0].relocs == {1: [0, 4, 8]}


# --- graceful skipping ------------------------------------------------


def test_symbol_and_debug_blocks_skipped():
    payload = bytes.fromhex("afff0000")
    blob = (
        header([1])
        + code_hunk(payload)
        + u32(hunk.HUNK_SYMBOL)
        + u32(2) + b"_start\0\0" + u32(0x1234)  # one symbol entry
        + u32(0)  # symbol terminator
        + u32(hunk.HUNK_DEBUG)
        + u32(3) + bytes(12)
        + u32(hunk.HUNK_END)
    )
    exe = hunk.parse(blob)
    assert len(exe.segments) == 1
    assert exe.segments[0].data == payload


def test_missing_hunk_end_between_hunks_tolerated():
    blob = (
        header([1, 1])
        + code_hunk(bytes(4))
        + u32(hunk.HUNK_DATA) + u32(1) + bytes(4)  # no HUNK_END before this
        + u32(hunk.HUNK_END)
    )
    exe = hunk.parse(blob)
    assert [s.kind for s in exe.segments] == ["code", "data"]


def test_extended_memory_flags_consume_attribute_longword():
    # Both flag bits set: an explicit AllocMem attribute longword follows.
    blob = (
        u32(hunk.HUNK_HEADER) + u32(0) + u32(1) + u32(0) + u32(0)
        + u32(1 | (0b11 << hunk.FLAG_SHIFT)) + u32(0xDEAD)
        + code_hunk(bytes(4))
        + u32(hunk.HUNK_END)
    )
    exe = hunk.parse(blob)
    assert exe.segments[0].memory_flags == 0b11
    assert exe.segments[0].memory_attrs == 0xDEAD


# --- malformed input --------------------------------------------------


def test_empty_file_rejected():
    with pytest.raises(HunkError, match="truncated"):
        hunk.parse(b"")


def test_bad_magic_rejected():
    with pytest.raises(HunkError, match="not a Hunk"):
        hunk.parse(u32(hunk.HUNK_CODE) + bytes(16))


def test_truncated_payload_rejected():
    blob = fixturegen.fixture_minimal_halt()
    with pytest.raises(HunkError, match="truncated"):
        hunk.parse(blob[:-6])


def test_truncated_header_table_rejected():
    with pytest.raises(HunkError, match="truncated"):
        hunk.parse(u32(hunk.HUNK_HEADER) + u32(0) + u32(2) + u32(0) + u32(1))


def test_unknown_hunk_type_rejected():
    blob = header([1]) + u32(0x3FF) + u32(1) + bytes(4)
    with pytest.raises(HunkError, match=r"unsupported hunk type \$3FF"):
        hunk.parse(blob)


def test_zero_hunk_table_rejected():
    with pytest.raises(HunkError, match="zero hunks"):
        hunk.parse(u32(hunk.HUNK_HEADER) + u32(0) + u32(0) + u32(0) + u32(0))


def test_partial_hunk_range_rejected():
    blob = (
        u32(hunk.HUNK_HEADER) + u32(0) + u32(3) + u32(1) + u32(2)
        + u32(1) * 3
    )
    with pytest.raises(HunkError, match="partial hunk range"):
        hunk.parse(blob)


def test_inverted_hunk_range_rejected():
    blob = u32(hunk.HUNK_HEADER) + u32(0) + u32(2) + u32(1) + u32(0) + u32(1) * 2
    with pytest.raises(HunkError, match="bad hunk table range"):
        hunk.parse(blob)


def test_hunk_count_mismatch_rejected():
    # Header says two hunks, file contains one.
    blob = header([1, 1]) + code_hunk(bytes(4)) + u32(hunk.HUNK_END)
    with pytest.raises(HunkError, match="declares 2"):
        hunk.parse(blob)


def test_too_many_hunks_rejected():
    blob = (
        header([1])
        + code_hunk(bytes(4)) + u32(hunk.HUNK_END)
        + code_hunk(bytes(4)) + u32(hunk.HUNK_END)
    )
    with pytest.raises(HunkError, match="more hunks"):
        hunk.parse(blob)


def test_block_larger_than_header_allocation_rejected():
    blob = header([1]) + code_hunk(bytes(8))
    with pytest.raises(HunkError, match="exceeds header allocation"):
        hunk.parse(blob)


def test_reloc_target_out_of_range_rejected():
    blob = (
        header([1])
        + code_hunk(bytes(4))
        + u32(hunk.HUNK_RELOC32) + u32(1) + u32(5) + u32(0) + u32(0)
        + u32(hunk.HUNK_END)
    )
    with pytest.raises(HunkError, match="target hunk 5 out of range"):
        hunk.parse(blob)


def test_reloc_offset_out_of_range_rejected():
    # Offset 4 in a 4-byte hunk: the 32-bit field would run off the end.
    blob = (
        header([1])
        + code_hunk(bytes(4))
        + u32(hunk.HUNK_RELOC32) + u32(1) + u32(0) + u32(4) + u32(0)
        + u32(hunk.HUNK_END)
    )
    with pytest.raises(HunkError, match="offset 4 out of range"):
        hunk.parse(blob)


def test_reloc_before_any_hunk_rejected():
    blob = header([1]) + u32(hunk.HUNK_RELOC32) + u32(0)
    with pytest.raises(HunkError, match="not attached"):
        hunk.parse(blob)


def test_reloc_after_hunk_end_rejected():
    blob = (
        header([1])
        + code_hunk(bytes(4))
        + u32(hunk.HUNK_END)
        + u32(hunk.HUNK_RELOC32) + u32(0)
    )
    with pytest.raises(HunkError, match="not attached"):
        hunk.parse(blob)


def test_reloc_on_bss_rejected():
    blob = (
        header([1])
        + u32(hunk.HUNK_BSS) + u32(1)
        + u32(hunk.HUNK_RELOC32) + u32(1) + u32(0) + u32(0) + u32(0)
        + u32(hunk.HUNK_END)
    )
    with pytest.raises(HunkError, match="BSS"):
        hunk.parse(blob)


def test_stray_hunk_end_rejected():
    blob = header([1]) + u32(hunk.HUNK_END) + code_hunk(bytes(4))
    with pytest.raises(HunkError, match="stray HUNK_END"):
        hunk.parse(blob)


def test_truncated_reloc_block_rejected():
    blob = header([1]) + code_hunk(bytes(4)) + u32(hunk.HUNK_RELOC32) + u32(2)
    with pytest.raises(HunkError, match="truncated"):
        hunk.parse(blob)


# --- file helper ------------------------------------------------------


def test_parse_file(tmp_path):
    p = tmp_path / "min.hunk"
    p.write_bytes(fixturegen.fixture_minimal_halt())
    exe = hunk.parse_file(str(p))
    assert len(exe.segments) == 1
