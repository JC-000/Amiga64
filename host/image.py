"""Guest-image layout and relocation for the M0 loader (host side).

Layout rules (CONTRACTS.md "M0 loader"):

- First segment loads at guest ``$010000``; each subsequent segment is
  placed 4-byte aligned after the previous segment's allocation.
- BSS segments (and the zero-fill tail of short data hunks) are emitted as
  explicit zero bytes so the C64 loader never has to clear REU memory.
- HUNK_RELOC32 entries add the assigned base of the target hunk to the
  32-bit big-endian field at each offset.

``image.bin`` framing
---------------------

The image file is a concatenation of chunk records, no file header, no
terminator — read records until EOF:

    u32  guest_offset   (little-endian, REU offset == guest address)
    u32  length         (little-endian, number of payload bytes)
    len  bytes          (payload, raw guest memory, big-endian as the
                         68000 sees it)

The record header is little-endian for 6502/host convenience; the payload
is untouched guest memory.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

from .hunk import Executable, HunkError

BASE_ADDR = 0x010000  # guest load address of the first segment
STACK_TOP = 0x00F000  # initial A7
ADDRESS_SPACE = 1 << 24  # 24-bit guest address space
_ALIGN = 4


@dataclass
class Chunk:
    """One sparse image chunk: ``data`` loads at guest/REU ``guest_offset``."""

    guest_offset: int
    data: bytes


def _align(addr: int) -> int:
    return (addr + _ALIGN - 1) & ~(_ALIGN - 1)


def build_image(exe: Executable) -> tuple[list[Chunk], dict[str, Any]]:
    """Lay out and relocate *exe*; return ``(chunks, manifest)``.

    The manifest is the CONTRACTS.md dict:
    ``{"entry", "segments": [{"guest_addr", "size", "kind"}], "stack_top"}``.
    """
    if not exe.segments:
        raise HunkError("executable has no segments")

    bases: list[int] = []
    addr = BASE_ADDR
    for seg in exe.segments:
        addr = _align(addr)
        bases.append(addr)
        addr += seg.alloc_size
    if addr > ADDRESS_SPACE:
        raise HunkError(
            f"image ends at ${addr:X}, beyond the 24-bit address space"
        )

    chunks: list[Chunk] = []
    for i, seg in enumerate(exe.segments):
        buf = bytearray(seg.alloc_size)  # zero-fills BSS and short-data tails
        buf[: len(seg.data)] = seg.data
        for target, offsets in seg.relocs.items():
            base = bases[target]
            for off in offsets:
                field = int.from_bytes(buf[off : off + 4], "big")
                buf[off : off + 4] = ((field + base) & 0xFFFF_FFFF).to_bytes(
                    4, "big"
                )
        chunks.append(Chunk(guest_offset=bases[i], data=bytes(buf)))

    manifest: dict[str, Any] = {
        "entry": bases[exe.first_hunk],
        "segments": [
            {"guest_addr": bases[i], "size": seg.alloc_size, "kind": seg.kind}
            for i, seg in enumerate(exe.segments)
        ],
        "stack_top": STACK_TOP,
    }
    return chunks, manifest


def image_bytes(chunks: list[Chunk]) -> bytes:
    """Serialize chunks per the framing documented in the module docstring."""
    out = bytearray()
    for c in chunks:
        out += struct.pack("<II", c.guest_offset, len(c.data))
        out += c.data
    return bytes(out)


def parse_image(blob: bytes) -> list[Chunk]:
    """Inverse of :func:`image_bytes` (used by tests and tooling)."""
    chunks: list[Chunk] = []
    pos = 0
    while pos < len(blob):
        if pos + 8 > len(blob):
            raise HunkError(f"truncated chunk header at offset {pos}")
        offset, length = struct.unpack_from("<II", blob, pos)
        pos += 8
        if pos + length > len(blob):
            raise HunkError(f"truncated chunk payload at offset {pos}")
        chunks.append(Chunk(guest_offset=offset, data=blob[pos : pos + length]))
        pos += length
    return chunks


def save_image(chunks: list[Chunk], path: str) -> None:
    with open(path, "wb") as f:
        f.write(image_bytes(chunks))


def save_manifest(manifest: dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
