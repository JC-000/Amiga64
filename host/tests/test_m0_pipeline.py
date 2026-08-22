"""Host-only M0 pipeline tests: fixture -> hunk.parse -> image -> staging records.

No VICE / c64-test-harness involved — this covers the image-side logic that
tests/run_program.py drives end to end in the emulator.
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host import fixturegen, hunk, image
from host.image import BASE_ADDR, STACK_TOP
from tests.run_program import MAX_RECORD_PAYLOAD, chunk_records


def _pipeline(blob):
    return image.build_image(hunk.parse(blob))


def _apply_records(records):
    """Replay staging records into a sparse byte map (what REU would hold)."""
    mem = {}
    for rec in records:
        off, length = struct.unpack_from("<II", rec, 0)
        assert len(rec) == 8 + length, "record framing broken"
        for i, b in enumerate(rec[8:]):
            mem[off + i] = b
    return mem


# --- layout / manifest invariants -----------------------------------------

def test_minimal_entry_and_stack_invariants():
    chunks, manifest = _pipeline(fixturegen.fixture_minimal_halt())
    assert manifest["entry"] == BASE_ADDR
    assert manifest["stack_top"] == STACK_TOP
    assert len(chunks) == 1
    assert chunks[0].guest_offset == BASE_ADDR
    # MOVEQ #1,D0 ; HALT — exactly what fixturegen encoded
    assert chunks[0].data == bytes.fromhex("7001afff")


def test_reloc_fixture_layout_and_relocation():
    chunks, manifest = _pipeline(fixturegen.fixture_code_data_reloc())
    code, data = chunks
    assert manifest["entry"] == code.guest_offset == BASE_ADDR
    assert data.guest_offset == BASE_ADDR + len(code.data)
    assert data.guest_offset % 4 == 0
    assert data.data == fixturegen.RELOC_FIXTURE_DATA
    # RELOC32 turned the preset data-hunk offset into an absolute address
    field = int.from_bytes(
        code.data[fixturegen.RELOC_FIXTURE_FIELD_OFFSET:
                  fixturegen.RELOC_FIXTURE_FIELD_OFFSET + 4], "big")
    assert field == data.guest_offset + fixturegen.RELOC_FIXTURE_FIELD_PRESET


def test_stack_does_not_overlap_image():
    for build in (fixturegen.fixture_minimal_halt,
                  fixturegen.fixture_code_data_reloc):
        chunks, manifest = _pipeline(build())
        assert manifest["stack_top"] <= min(c.guest_offset for c in chunks)


# --- image.bin framing round trip ------------------------------------------

@pytest.mark.parametrize("build", [fixturegen.fixture_minimal_halt,
                                   fixturegen.fixture_code_data_reloc])
def test_image_bytes_round_trip(build):
    chunks, _ = _pipeline(build())
    again = image.parse_image(image.image_bytes(chunks))
    assert [(c.guest_offset, c.data) for c in again] == \
           [(c.guest_offset, c.data) for c in chunks]


# --- staging-record framing (what test_load_chunk consumes) -----------------

def test_records_identical_to_image_bytes_when_unsplit():
    chunks, _ = _pipeline(fixturegen.fixture_code_data_reloc())
    records = chunk_records(chunks)
    assert b"".join(records) == image.image_bytes(chunks)


def test_records_fit_buf_case():
    chunks, _ = _pipeline(fixturegen.fixture_code_data_reloc())
    for rec in chunk_records(chunks):
        assert len(rec) <= 8 + MAX_RECORD_PAYLOAD == 0x800


def test_split_records_reassemble_to_same_guest_memory():
    chunks, _ = _pipeline(fixturegen.fixture_code_data_reloc())
    whole = _apply_records(chunk_records(chunks))
    for max_payload in (1, 3, 4, 7):
        split = chunk_records(chunks, max_payload=max_payload)
        assert all(len(r) <= 8 + max_payload for r in split)
        assert _apply_records(split) == whole


def test_split_preserves_order_and_offsets():
    chunks = [image.Chunk(guest_offset=0x010000, data=bytes(range(10)))]
    recs = chunk_records(chunks, max_payload=4)
    hdrs = [struct.unpack_from("<II", r, 0) for r in recs]
    assert hdrs == [(0x010000, 4), (0x010004, 4), (0x010008, 2)]


def test_bad_max_payload_rejected():
    with pytest.raises(ValueError):
        chunk_records([], max_payload=0)


# --- guest-memory model: what the interpreter will fetch --------------------

def test_reloc_fixture_memory_holds_relocated_pointer_and_operand():
    chunks, manifest = _pipeline(fixturegen.fixture_code_data_reloc())
    mem = _apply_records(chunk_records(chunks))
    entry = manifest["entry"]
    # opcode word at entry is MOVE.L (abs.L),D0
    assert (mem[entry] << 8 | mem[entry + 1]) == fixturegen.OP_MOVE_L_ABSL_D0
    ptr = int.from_bytes(bytes(mem[entry + 2 + i] for i in range(4)), "big")
    operand = int.from_bytes(bytes(mem[ptr + i] for i in range(4)), "big")
    assert operand == int.from_bytes(
        fixturegen.RELOC_FIXTURE_DATA[
            fixturegen.RELOC_FIXTURE_FIELD_PRESET:
            fixturegen.RELOC_FIXTURE_FIELD_PRESET + 4], "big")
    # and the instruction after the MOVE.L is the HALT pseudo-op
    assert (mem[entry + 6] << 8 | mem[entry + 7]) == fixturegen.OP_HALT
