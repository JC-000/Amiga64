"""Parser for the AmigaDOS Hunk executable format (M0 loader, host side).

Supported hunk blocks:

    HUNK_HEADER  ($3F3)  file header with hunk-table sizes
    HUNK_CODE    ($3E9)  code hunk (payload follows)
    HUNK_DATA    ($3EA)  initialized data hunk (payload follows)
    HUNK_BSS     ($3EB)  uninitialized data hunk (size only)
    HUNK_RELOC32 ($3EC)  32-bit relocations, attached to the preceding hunk
    HUNK_RELOC32SHORT ($3FC, and $3F7 in executables)
                         the same relocations with 16-bit count/target/offset
                         words, padded to a longword at the end.  In a load
                         file dos.library's LoadSeg treats $3F7 (the object-
                         file id HUNK_DREL32) as RELOC32SHORT, and that is
                         what vlink -Rshort (vbcc's default +aos68k link)
                         emits.
    HUNK_END     ($3F2)  terminates the current hunk
    HUNK_SYMBOL  ($3F0)  skipped
    HUNK_DEBUG   ($3F1)  skipped

All longwords in the file are big-endian.  Size longwords (both in the
header table and in CODE/DATA/BSS blocks) carry memory-placement flags in
their top two bits (bit 31 = FAST, bit 30 = CHIP); when both bits are set
one extra longword with explicit AllocMem attributes follows the size.
The flags are irrelevant on our target (all guest memory is REU) but are
parsed and preserved.

Malformed input raises :class:`HunkError` with a description of the fault.

Not supported (raise :class:`HunkError` "unsupported hunk type"): base-
relative / data-relative relocations HUNK_DREL16 ($3F8), HUNK_DREL8 ($3F9),
HUNK_RELRELOC32 ($3FD), HUNK_ABSRELOC16 ($3FE), overlays, and any other
block.  (vbcc's small-data model does not need them in executables: its
A4-relative accesses are resolved by the linker.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Hunk block type ids (low 30 bits of the type longword).
HUNK_HEADER = 0x3F3
HUNK_CODE = 0x3E9
HUNK_DATA = 0x3EA
HUNK_BSS = 0x3EB
HUNK_RELOC32 = 0x3EC
HUNK_RELOC32SHORT = 0x3FC
HUNK_DREL32 = 0x3F7  # == RELOC32SHORT inside a load file (LoadSeg V37+)
HUNK_SYMBOL = 0x3F0
HUNK_DEBUG = 0x3F1
HUNK_END = 0x3F2

_DEFINING = {HUNK_CODE: "code", HUNK_DATA: "data", HUNK_BSS: "bss"}

# Top two bits of a size/type longword: memory-placement flags.
FLAG_SHIFT = 30
SIZE_MASK = 0x3FFF_FFFF
MEMF_CHIP = 0b01  # bit 30
MEMF_FAST = 0b10  # bit 31
MEMF_EXT = 0b11  # both set: explicit attribute longword follows


class HunkError(ValueError):
    """Raised for structurally invalid or truncated Hunk files."""


@dataclass
class Segment:
    """One loadable hunk.

    ``data`` is the stored payload (empty for BSS).  ``alloc_size`` is the
    allocation size in bytes from the header table; it is always a multiple
    of 4 and at least ``len(data)`` — the loader zero-fills the remainder.
    ``relocs`` maps a target hunk index to the sorted byte offsets (within
    this segment) of 32-bit big-endian fields that must be relocated by the
    target hunk's load address.
    """

    kind: str  # "code" | "data" | "bss"
    data: bytes
    alloc_size: int
    memory_flags: int = 0  # 2-bit flag field from the header size longword
    memory_attrs: int | None = None  # explicit AllocMem attrs (flags == 0b11)
    relocs: dict[int, list[int]] = field(default_factory=dict)


@dataclass
class Executable:
    """Parsed Hunk executable: an ordered list of loadable segments."""

    segments: list[Segment]
    first_hunk: int
    last_hunk: int


class _Reader:
    def __init__(self, blob: bytes) -> None:
        self.blob = blob
        self.pos = 0

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.blob)

    def u32(self, what: str) -> int:
        if self.pos + 4 > len(self.blob):
            raise HunkError(
                f"truncated file: expected {what} at offset {self.pos}, "
                f"file is {len(self.blob)} bytes"
            )
        val = int.from_bytes(self.blob[self.pos : self.pos + 4], "big")
        self.pos += 4
        return val

    def u16(self, what: str) -> int:
        if self.pos + 2 > len(self.blob):
            raise HunkError(
                f"truncated file: expected {what} at offset {self.pos}, "
                f"file is {len(self.blob)} bytes"
            )
        val = int.from_bytes(self.blob[self.pos : self.pos + 2], "big")
        self.pos += 2
        return val

    def take(self, n: int, what: str) -> bytes:
        if self.pos + n > len(self.blob):
            raise HunkError(
                f"truncated file: expected {n} bytes of {what} at offset "
                f"{self.pos}, file is {len(self.blob)} bytes"
            )
        out = self.blob[self.pos : self.pos + n]
        self.pos += n
        return out


def _read_size_flags(r: _Reader, what: str) -> tuple[int, int, int | None]:
    """Read a size longword; return (size_longs, flags, explicit_attrs)."""
    raw = r.u32(what)
    flags = raw >> FLAG_SHIFT
    attrs = r.u32(f"{what} memory attributes") if flags == MEMF_EXT else None
    return raw & SIZE_MASK, flags, attrs


def parse(blob: bytes) -> Executable:
    """Parse a Hunk executable image into an :class:`Executable`."""
    r = _Reader(blob)

    magic = r.u32("HUNK_HEADER magic")
    if magic != HUNK_HEADER:
        raise HunkError(
            f"not a Hunk executable: first longword is ${magic:X}, "
            f"expected HUNK_HEADER (${HUNK_HEADER:X})"
        )

    # Resident library name table: (longs, name...)* terminated by 0.
    # Always empty for plain executables, but skip it if present.
    while (name_longs := r.u32("resident-name length")) != 0:
        r.take(name_longs * 4, "resident name")

    table_size = r.u32("hunk table size")
    first_hunk = r.u32("first hunk index")
    last_hunk = r.u32("last hunk index")
    if table_size == 0:
        raise HunkError("header declares zero hunks")
    if not (first_hunk <= last_hunk < table_size):
        raise HunkError(
            f"bad hunk table range: first={first_hunk} last={last_hunk} "
            f"table_size={table_size}"
        )
    if first_hunk != 0 or last_hunk != table_size - 1:
        # Overlay files use partial ranges; the M0 loader does not.
        raise HunkError(
            f"unsupported partial hunk range {first_hunk}..{last_hunk} "
            f"(table size {table_size})"
        )

    header_sizes: list[tuple[int, int, int | None]] = [
        _read_size_flags(r, f"header size of hunk {i}") for i in range(table_size)
    ]

    segments: list[Segment] = []
    open_hunk = False  # a defining hunk was seen and HUNK_END not yet

    while not r.eof:
        type_raw = r.u32("hunk type")
        # CODE/DATA/BSS type longwords may also carry memory flags.
        hunk_type = type_raw & SIZE_MASK

        if hunk_type in _DEFINING:
            # A new defining hunk implicitly closes the previous one; some
            # linkers omit HUNK_END between hunks.
            if len(segments) >= table_size:
                raise HunkError(
                    f"more hunks in file than the header table size "
                    f"({table_size})"
                )
            idx = len(segments)
            size_longs, _, _ = _read_size_flags(
                r, f"size of {_DEFINING[hunk_type]} hunk {idx}"
            )
            alloc_longs, flags, attrs = header_sizes[idx]
            data = b""
            if hunk_type != HUNK_BSS:
                data = r.take(size_longs * 4, f"payload of hunk {idx}")
            if size_longs > alloc_longs:
                raise HunkError(
                    f"hunk {idx}: block size {size_longs} longs exceeds "
                    f"header allocation {alloc_longs} longs"
                )
            segments.append(
                Segment(
                    kind=_DEFINING[hunk_type],
                    data=data,
                    alloc_size=alloc_longs * 4,
                    memory_flags=flags,
                    memory_attrs=attrs,
                )
            )
            open_hunk = True

        elif hunk_type in (HUNK_RELOC32, HUNK_RELOC32SHORT, HUNK_DREL32):
            short = hunk_type != HUNK_RELOC32
            name = "reloc32short" if short else "reloc32"
            if not segments or not open_hunk:
                raise HunkError(
                    f"HUNK_{name.upper()} at offset {r.pos - 4} is not "
                    f"attached to a hunk"
                )
            seg = segments[-1]
            if seg.kind == "bss":
                raise HunkError(
                    f"HUNK_{name.upper()} attached to BSS hunk "
                    f"{len(segments) - 1}"
                )
            read = r.u16 if short else r.u32
            nwords = 0
            while (count := read(f"{name} count")) != 0:
                target = read(f"{name} target hunk")
                nwords += 2
                if target >= table_size:
                    raise HunkError(
                        f"{name} target hunk {target} out of range "
                        f"(table size {table_size})"
                    )
                offsets = seg.relocs.setdefault(target, [])
                for _ in range(count):
                    off = read(f"{name} offset")
                    nwords += 1
                    if off + 4 > len(seg.data):
                        raise HunkError(
                            f"{name} offset {off} out of range in hunk "
                            f"{len(segments) - 1} ({len(seg.data)} data bytes)"
                        )
                    offsets.append(off)
                offsets.sort()
            if short:
                nwords += 1                    # the terminating 0 count
                if nwords % 2:
                    r.u16(f"{name} longword padding")

        elif hunk_type == HUNK_END:
            if not open_hunk:
                raise HunkError(f"stray HUNK_END at offset {r.pos - 4}")
            open_hunk = False

        elif hunk_type == HUNK_SYMBOL:
            while (name_longs := r.u32("symbol name length")) != 0:
                r.take(name_longs * 4, "symbol name")
                r.u32("symbol value")

        elif hunk_type == HUNK_DEBUG:
            n = r.u32("debug block size")
            r.take(n * 4, "debug payload")

        else:
            raise HunkError(
                f"unsupported hunk type ${hunk_type:X} at offset {r.pos - 4}"
            )

    if len(segments) != table_size:
        raise HunkError(
            f"file contains {len(segments)} hunks, header declares "
            f"{table_size}"
        )

    return Executable(
        segments=segments, first_hunk=first_hunk, last_hunk=last_hunk
    )


def parse_file(path: str) -> Executable:
    with open(path, "rb") as f:
        return parse(f.read())
