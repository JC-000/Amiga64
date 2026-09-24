"""Tests for host/image.py — layout, relocation, image/manifest emission."""

import json
import struct
import sys
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host import fixturegen, hunk, image
from host.hunk import Executable, HunkError, Segment
from host.image import BASE_ADDR, STACK_TOP, Chunk


def make_exe(*segments: Segment) -> Executable:
    return Executable(
        segments=list(segments), first_hunk=0, last_hunk=len(segments) - 1
    )


# --- layout -----------------------------------------------------------


def test_layout_of_code_data_fixture():
    exe = hunk.parse(fixturegen.fixture_code_data_reloc())
    chunks, manifest = image.build_image(exe)

    assert manifest["entry"] == BASE_ADDR
    assert manifest["stack_top"] == STACK_TOP
    assert manifest["segments"] == [
        {"guest_addr": 0x010000, "size": 8, "kind": "code"},
        {"guest_addr": 0x010008, "size": 8, "kind": "data"},
    ]
    assert [c.guest_offset for c in chunks] == [0x010000, 0x010008]


def test_unaligned_alloc_sizes_are_aligned_up():
    # The parser only produces longword allocations, but the layout must
    # not rely on that: a 6-byte segment still 4-byte aligns its successor.
    exe = make_exe(
        Segment(kind="code", data=bytes(6), alloc_size=6),
        Segment(kind="data", data=bytes(4), alloc_size=4),
    )
    _, manifest = image.build_image(exe)
    assert manifest["segments"][1]["guest_addr"] == BASE_ADDR + 8


def test_bss_chunk_is_explicit_zeros():
    exe = make_exe(
        Segment(kind="code", data=bytes.fromhex("afff0000"), alloc_size=4),
        Segment(kind="bss", data=b"", alloc_size=16),
    )
    chunks, manifest = image.build_image(exe)
    assert chunks[1].guest_offset == BASE_ADDR + 4
    assert chunks[1].data == bytes(16)
    assert manifest["segments"][1] == {
        "guest_addr": BASE_ADDR + 4,
        "size": 16,
        "kind": "bss",
    }


def test_short_data_hunk_zero_padded_to_alloc_size():
    exe = make_exe(Segment(kind="data", data=b"\xaa\xbb\xcc\xdd", alloc_size=12))
    chunks, _ = image.build_image(exe)
    assert chunks[0].data == b"\xaa\xbb\xcc\xdd" + bytes(8)


def test_empty_executable_rejected():
    with pytest.raises(HunkError, match="no segments"):
        image.build_image(make_exe())


def test_image_beyond_24bit_space_rejected():
    exe = make_exe(Segment(kind="bss", data=b"", alloc_size=0x1000000))
    with pytest.raises(HunkError, match="24-bit"):
        image.build_image(exe)


# --- relocation -------------------------------------------------------


def test_reloc_math_on_fixture_hand_computed():
    exe = hunk.parse(fixturegen.fixture_code_data_reloc())
    chunks, _ = image.build_image(exe)
    code = chunks[0].data
    # Data hunk lands at $010008; the field held preset $00000004, so the
    # relocated MOVE.L absolute address must be $01000C.
    expected = (0x010008 + fixturegen.RELOC_FIXTURE_FIELD_PRESET).to_bytes(4, "big")
    off = fixturegen.RELOC_FIXTURE_FIELD_OFFSET
    assert code[off : off + 4] == expected
    # Everything around the field is untouched.
    assert code[:2] == fixturegen.OP_MOVE_L_ABSL_D0.to_bytes(2, "big")
    assert code[6:8] == fixturegen.OP_HALT.to_bytes(2, "big")
    # The data hunk is not relocated.
    assert chunks[1].data == fixturegen.RELOC_FIXTURE_DATA


def test_reloc_to_self_and_to_later_hunk():
    code = Segment(
        kind="code",
        data=bytes.fromhex("00000000" "00000010" "afff0000"),
        alloc_size=12,
        relocs={0: [0], 2: [4]},
    )
    data = Segment(kind="data", data=bytes(8), alloc_size=8)
    bss = Segment(kind="bss", data=b"", alloc_size=4)
    chunks, _ = image.build_image(make_exe(code, data, bss))
    out = chunks[0].data
    # Self-reloc: 0 + own base.
    assert out[0:4] == (0x010000).to_bytes(4, "big")
    # Field preset $10 + bss base ($010000 + 12 + 8 = $010014).
    assert out[4:8] == (0x010014 + 0x10).to_bytes(4, "big")


def test_reloc_addition_wraps_to_32_bits():
    seg = Segment(
        kind="data",
        data=b"\xff\xff\xff\xff",
        alloc_size=4,
        relocs={0: [0]},
    )
    chunks, _ = image.build_image(make_exe(seg))
    assert chunks[0].data == (BASE_ADDR - 1).to_bytes(4, "big")


# --- image.bin framing and file emission ------------------------------


def test_image_bytes_framing_is_le_u32_offset_len():
    chunks = [Chunk(0x010000, b"\x01\x02\x03"), Chunk(0x020000, b"")]
    blob = image.image_bytes(chunks)
    assert blob == (
        struct.pack("<II", 0x010000, 3) + b"\x01\x02\x03"
        + struct.pack("<II", 0x020000, 0)
    )


def test_image_round_trip():
    exe = hunk.parse(fixturegen.fixture_code_data_reloc())
    chunks, _ = image.build_image(exe)
    parsed = image.parse_image(image.image_bytes(chunks))
    assert [(c.guest_offset, c.data) for c in parsed] == [
        (c.guest_offset, c.data) for c in chunks
    ]


def test_parse_image_rejects_truncation():
    blob = image.image_bytes([Chunk(0x010000, b"\x01\x02\x03\x04")])
    with pytest.raises(HunkError, match="truncated chunk payload"):
        image.parse_image(blob[:-1])
    with pytest.raises(HunkError, match="truncated chunk header"):
        image.parse_image(blob[:4])


def test_save_image_and_manifest(tmp_path):
    exe = hunk.parse(fixturegen.fixture_code_data_reloc())
    chunks, manifest = image.build_image(exe)
    img = tmp_path / "image.bin"
    man = tmp_path / "manifest.json"
    image.save_image(chunks, str(img))
    image.save_manifest(manifest, str(man))

    assert img.read_bytes() == image.image_bytes(chunks)
    reloaded = json.loads(man.read_text())
    assert reloaded == manifest
    assert set(reloaded) == {"entry", "segments", "stack_top"}


def test_entry_is_first_hunk_base():
    exe = hunk.parse(fixturegen.fixture_minimal_halt())
    _, manifest = image.build_image(exe)
    assert manifest["entry"] == 0x010000


def test_image_past_load_region_is_rejected():
    """R3 (b1-review): a program reaching into the library tables must fail
    in build_image itself, for every driver — not only in run_vbcc."""
    from host.image import PROG_END, BASE_ADDR
    fits = make_exe(Segment(kind="bss", data=b"", alloc_size=PROG_END + 1 - BASE_ADDR))
    image.build_image(fits)
    too_big = make_exe(Segment(kind="bss", data=b"", alloc_size=PROG_END + 2 - BASE_ADDR))
    with pytest.raises(HunkError, match="program load region"):
        image.build_image(too_big)
