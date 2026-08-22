"""Builder for small, valid Hunk executables — no m68k toolchain needed.

68000 code is supplied as hand-encoded 16-bit words; the builder emits a
correctly framed Hunk file (HUNK_HEADER, CODE/DATA/BSS blocks with
optional RELOC32, HUNK_END per hunk).  Used to generate test fixtures for
the parser/loader and, later, guest programs for VICE/hardware smoke
tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hunk import (
    FLAG_SHIFT,
    HUNK_BSS,
    HUNK_CODE,
    HUNK_DATA,
    HUNK_END,
    HUNK_HEADER,
    HUNK_RELOC32,
)

# 68000 opcode words used by the canned fixtures.
OP_HALT = 0xAFFF  # A-line HALT pseudo-op (CONTRACTS.md guest model)
OP_MOVEQ_1_D0 = 0x7001  # MOVEQ #1,D0
OP_MOVE_L_ABSL_D0 = 0x2039  # MOVE.L (abs.L),D0 — 32-bit address follows


def _u32(val: int) -> bytes:
    return (val & 0xFFFF_FFFF).to_bytes(4, "big")


def words_to_bytes(words: list[int]) -> bytes:
    """Encode 16-bit big-endian code words."""
    out = bytearray()
    for w in words:
        if not 0 <= w <= 0xFFFF:
            raise ValueError(f"code word ${w:X} does not fit 16 bits")
        out += w.to_bytes(2, "big")
    return bytes(out)


def _pad4(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


@dataclass
class _Hunk:
    type_id: int
    data: bytes  # already longword-padded; b"" for BSS
    size_longs: int  # allocation size in longwords
    memory_flags: int
    relocs: dict[int, list[int]] = field(default_factory=dict)


class HunkBuilder:
    """Assemble a Hunk executable from code/data/bss hunks.

    ``add_*`` return the new hunk's index, for use as a RELOC32 target.
    """

    def __init__(self) -> None:
        self._hunks: list[_Hunk] = []

    def add_code(self, words: list[int], memory_flags: int = 0) -> int:
        """Add a code hunk from a list of 16-bit instruction words."""
        return self._add(HUNK_CODE, words_to_bytes(words), memory_flags)

    def add_data(self, data: bytes, memory_flags: int = 0) -> int:
        return self._add(HUNK_DATA, bytes(data), memory_flags)

    def add_bss(self, size_bytes: int, memory_flags: int = 0) -> int:
        """Add a BSS hunk; *size_bytes* is rounded up to a longword."""
        size_longs = (size_bytes + 3) // 4
        self._hunks.append(_Hunk(HUNK_BSS, b"", size_longs, memory_flags))
        return len(self._hunks) - 1

    def _add(self, type_id: int, data: bytes, memory_flags: int) -> int:
        if memory_flags not in (0, 1, 2):
            raise ValueError(
                "memory_flags must be 0 (any), 1 (chip) or 2 (fast)"
            )
        padded = _pad4(data)
        self._hunks.append(
            _Hunk(type_id, padded, len(padded) // 4, memory_flags)
        )
        return len(self._hunks) - 1

    def add_reloc32(self, hunk: int, target: int, offsets: list[int]) -> None:
        """Relocate 32-bit fields at *offsets* in *hunk* by *target*'s base."""
        src = self._hunks[hunk]  # IndexError for a bogus hunk is fine here
        if not 0 <= target < len(self._hunks):
            raise ValueError(f"reloc target hunk {target} does not exist")
        if src.type_id == HUNK_BSS:
            raise ValueError("cannot attach relocations to a BSS hunk")
        for off in offsets:
            if not 0 <= off <= len(src.data) - 4:
                raise ValueError(
                    f"reloc offset {off} out of range for hunk {hunk} "
                    f"({len(src.data)} bytes)"
                )
        src.relocs.setdefault(target, []).extend(offsets)

    def build(self) -> bytes:
        if not self._hunks:
            raise ValueError("no hunks added")
        out = bytearray()
        out += _u32(HUNK_HEADER)
        out += _u32(0)  # no resident library names
        out += _u32(len(self._hunks))  # table size
        out += _u32(0)  # first hunk
        out += _u32(len(self._hunks) - 1)  # last hunk
        for h in self._hunks:
            out += _u32(h.size_longs | (h.memory_flags << FLAG_SHIFT))
        for h in self._hunks:
            out += _u32(h.type_id)
            out += _u32(h.size_longs)
            out += h.data
            if h.relocs:
                out += _u32(HUNK_RELOC32)
                for target, offsets in sorted(h.relocs.items()):
                    out += _u32(len(offsets))
                    out += _u32(target)
                    for off in offsets:
                        out += _u32(off)
                out += _u32(0)  # reloc terminator
            out += _u32(HUNK_END)
        return bytes(out)


def fixture_minimal_halt() -> bytes:
    """Single code hunk: MOVEQ #1,D0 then HALT."""
    b = HunkBuilder()
    b.add_code([OP_MOVEQ_1_D0, OP_HALT])
    return b.build()


# Byte offset (within the fixture's code hunk) of the 32-bit field that
# RELOC32 patches, and the pre-relocation value stored there (an offset
# into the data hunk).  Tests use these to hand-compute expected bytes.
RELOC_FIXTURE_FIELD_OFFSET = 2
RELOC_FIXTURE_FIELD_PRESET = 0x0000_0004
RELOC_FIXTURE_DATA = bytes.fromhex("1122334455667788")


def fixture_code_data_reloc() -> bytes:
    """Two hunks: code reads a longword from the data hunk, then HALTs.

    The MOVE.L absolute address field (preset to data-hunk offset 4) is
    relocated by the data hunk's base.
    """
    b = HunkBuilder()
    code = b.add_code(
        [
            OP_MOVE_L_ABSL_D0,
            RELOC_FIXTURE_FIELD_PRESET >> 16,
            RELOC_FIXTURE_FIELD_PRESET & 0xFFFF,
            OP_HALT,
        ]
    )
    data = b.add_data(RELOC_FIXTURE_DATA)
    b.add_reloc32(code, data, [RELOC_FIXTURE_FIELD_OFFSET])
    return b.build()
