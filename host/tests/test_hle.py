"""Host-only M2 HLE tests: env chunk layout + the hello fixture.

No VICE / c64-test-harness involved — this covers the environment builder
(host/hle_env.py) and the hand-encoded hello program (fixture_hello) that
tests/run_hello.py drives end to end in the emulator.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host import fixturegen, hle_env, hunk, image
from host.image import BASE_ADDR, STACK_TOP


def _env_mem(stack_top=STACK_TOP):
    """Replay the env chunks into a sparse byte map (what REU would hold)."""
    mem = {}
    for c in hle_env.env_chunks(stack_top):
        for i, b in enumerate(c.data):
            assert c.guest_offset + i not in mem, "env chunks overlap"
            mem[c.guest_offset + i] = b
    return mem


def _be32(mem, addr):
    return int.from_bytes(bytes(mem[addr + i] for i in range(4)), "big")


def _be16(mem, addr):
    return int.from_bytes(bytes(mem[addr + i] for i in range(2)), "big")


# --- env layout -------------------------------------------------------------

def test_absexecbase_points_at_execbase():
    mem = _env_mem()
    assert _be32(mem, hle_env.ABSEXECBASE_ADDR) == hle_env.EXEC_BASE


def test_vector_slots_hold_a1xx_words():
    mem = _env_mem()
    expected = {
        hle_env.EXEC_BASE - 552: 0xA100,   # OpenLibrary
        hle_env.EXEC_BASE - 414: 0xA101,   # CloseLibrary
        hle_env.DOS_BASE - 60: 0xA102,     # Output
        hle_env.DOS_BASE - 48: 0xA103,     # Write
    }
    for addr, word in expected.items():
        assert _be16(mem, addr) == word, f"slot at ${addr:06X}"
        # full 6-byte slot staged (tail bytes are don't-care but present)
        for i in range(hle_env.SLOT_SIZE):
            assert addr + i in mem


def test_slots_inside_env_region():
    for base, funcs in hle_env.LIBRARIES.values():
        for lvo, _ in funcs.values():
            addr = hle_env.slot_addr(base, lvo)
            assert hle_env.ENV_BASE <= addr
            assert addr + hle_env.SLOT_SIZE <= hle_env.ENV_END


def test_exit_stub_and_prestaged_return_address():
    mem = _env_mem()
    assert _be16(mem, hle_env.EXIT_ADDR) == fixturegen.OP_HALT  # $AFFF
    assert _be32(mem, STACK_TOP) == hle_env.EXIT_ADDR
    # custom stack_top is honored
    mem2 = _env_mem(stack_top=0x00E000)
    assert _be32(mem2, 0x00E000) == hle_env.EXIT_ADDR


def test_env_avoids_program_and_stack_space():
    """Nothing except the [stack_top] return address may leave the env
    region + AbsExecBase; nothing may reach the first program segment."""
    for c in hle_env.env_chunks():
        end = c.guest_offset + len(c.data)
        assert end <= BASE_ADDR
        if c.guest_offset == STACK_TOP:
            continue
        assert (c.guest_offset == hle_env.ABSEXECBASE_ADDR
                or hle_env.ENV_BASE <= c.guest_offset < hle_env.ENV_END)


def test_env_chunks_round_trip_through_image_framing():
    chunks = hle_env.env_chunks()
    parsed = image.parse_image(image.image_bytes(chunks))
    assert [(c.guest_offset, c.data) for c in parsed] == \
           [(c.guest_offset, c.data) for c in chunks]


def test_slot_word_and_slot_addr_validation():
    assert hle_env.slot_word(0x03) == 0xA103
    import pytest
    with pytest.raises(ValueError):
        hle_env.slot_word(0x100)
    with pytest.raises(ValueError):
        hle_env.slot_addr(hle_env.DOS_BASE, 48)  # positive LVO


# --- hello fixture ----------------------------------------------------------

def _hello_image():
    return image.build_image(hunk.parse(fixturegen.fixture_hello()))


def test_hello_parses_and_lays_out():
    chunks, manifest = _hello_image()
    assert manifest["entry"] == BASE_ADDR
    assert len(chunks) == 2
    code, data = chunks
    assert data.data[:len(fixturegen.HELLO_MESSAGE)] == fixturegen.HELLO_MESSAGE


def test_hello_reloc_patches_message_pointer():
    chunks, manifest = _hello_image()
    code, data = chunks
    field = int.from_bytes(
        code.data[fixturegen.HELLO_RELOC_OFFSET:
                  fixturegen.HELLO_RELOC_OFFSET + 4], "big")
    assert field == data.guest_offset == manifest["segments"][1]["guest_addr"]


def test_hello_code_encodings():
    chunks, _ = _hello_image()
    code = chunks[0].data
    # movea.l ($4).w,a6 — ExecBase load through the env's AbsExecBase
    assert code[0:4] == bytes.fromhex("2C780004")
    # lea dosname(pc),a1: disp is relative to the extension word at $06
    disp = int.from_bytes(code[6:8], "big")
    assert code[4:6] == bytes.fromhex("43FA")
    assert 6 + disp == fixturegen.HELLO_DOSNAME_OFFSET
    # the three jsr d16(a6) calls target the v0 LVOs
    assert code[0x0A:0x0E] == b"\x4E\xAE" + struct.pack(">h", -552)
    assert code[0x10:0x14] == b"\x4E\xAE" + struct.pack(">h", -60)
    assert code[0x1E:0x22] == b"\x4E\xAE" + struct.pack(">h", -48)
    # moveq #len,d3 / rts / terminated name string
    assert code[0x1C:0x1E] == bytes([0x76, len(fixturegen.HELLO_MESSAGE)])
    assert code[0x22:0x24] == b"\x4E\x75"
    assert code[fixturegen.HELLO_DOSNAME_OFFSET:
                fixturegen.HELLO_DOSNAME_OFFSET + 12] == b"dos.library\0"


def test_hello_lvo_slots_match_fixture_calls():
    """The LVOs the fixture jsr's through are exactly the ones the env
    plants $A1xx words for (OpenLibrary via ExecBase; Output/Write via
    DOSBase returned by OpenLibrary)."""
    assert fixturegen.LVO_OPENLIBRARY == hle_env.EXEC_FUNCS["OpenLibrary"][0]
    assert fixturegen.LVO_OUTPUT == hle_env.DOS_FUNCS["Output"][0]
    assert fixturegen.LVO_WRITE == hle_env.DOS_FUNCS["Write"][0]
