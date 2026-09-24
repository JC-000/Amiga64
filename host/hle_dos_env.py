"""File-DOS half of the HLE environment (owner: WS-B2, docs/WAVE4_BRIEF.md C3).

``DOS_FILE_FUNCS`` maps dos.library function name -> (LVO, HLE index) for the
file-I/O functions serviced by ``runtime/ops/hle_dos.s``.  Indices MUST lie
in $30-$4F (hle.s routes that range to ``hle_dos_dispatch``).
``host/hle_env.py`` merges this table into ``DOS_FUNCS``.

This module is also the host mirror of the WS-B2 guest layout (FileHandle
pool, RAMDISK region) and of the on-REU RAMDISK directory format, so hosts
can stage initial files before a run and read files back after it.  The
format is specified in ``docs/HLE_DOS.md``; the constants here mirror
``runtime/rc/guest.inc`` (``FH_POOL`` / ``dosio_rd_*``) and
``runtime/ops/hle_dos.s`` / ``runtime/ops/dosio_ram.s`` — cross-checked by
``host/tests/test_hle_dos_env.py``.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .image import Chunk

# --- LVO -> HLE index table (C3: indices $30-$4F) -----------------------------
# Real dos.library LVOs (dos_lib.fd: Open -30, Close -36, Read -42, Write -48,
# Input -54, Output -60, Seek -66, ..., IoErr -132).
DOS_FILE_FUNCS: dict[str, tuple[int, int]] = {
    "Open": (-30, 0x30),
    "Close": (-36, 0x31),
    "Seek": (-66, 0x32),
    "IoErr": (-132, 0x33),
}

# Direct file-handle Read/Write entries (hle_dos_read / hle_dos_write).  NOT
# part of DOS_FILE_FUNCS: in production Read/Write are WS-B1's single HLE
# indices in hle.s, which jmp to hle_dos_read/hle_dos_write for file kinds
# (C2).  These two indices exist so a TEST-ONLY env (tests/run_dos.py) can
# map the Read/Write LVO slots straight onto the WS-B2 entry points.
HLE_DOS_IDX_READ = 0x4E
HLE_DOS_IDX_WRITE = 0x4F
LVO_READ = -42
LVO_WRITE = -48

# --- dos/dos.h constants --------------------------------------------------------
MODE_OLDFILE = 1005
MODE_NEWFILE = 1006
MODE_READWRITE = 1004
OFFSET_BEGINNING = -1
OFFSET_CURRENT = 0
OFFSET_END = 1
DOSTRUE = 0xFFFFFFFF
DOSFALSE = 0

ERROR_NO_FREE_STORE = 103
ERROR_OBJECT_NOT_FOUND = 205
ERROR_OBJECT_TOO_LARGE = 207
ERROR_ACTION_NOT_KNOWN = 209
ERROR_INVALID_COMPONENT_NAME = 210
ERROR_INVALID_LOCK = 211
ERROR_DEVICE_NOT_MOUNTED = 218
ERROR_SEEK_ERROR = 219
ERROR_DISK_FULL = 221

# --- FileHandle pool (guest $000C00-$000FFF, C2/C4) ----------------------------
FH_POOL = 0x000C00           # slot n at FH_POOL + FH_SIZE * n (longword aligned)
FH_SIZE = 44                 # sizeof(struct FileHandle), dos/dosextens.h
FH_SLOTS = 8
FH_ARGS_OFF = 36             # fh_Args: handle kind (C2)
FH_KIND_FILE0 = 0x10         # kind of WS-B2 slot n = $10 + n
FH_KIND_DEAD = 0xFFFFFFFF    # fh_Args after Close (still routes to WS-B2 -> error)


def fh_addr(slot: int) -> int:
    return FH_POOL + FH_SIZE * slot


def fh_bptr(slot: int) -> int:
    return fh_addr(slot) >> 2


# --- RAMDISK (guest/REU $F00000-$FFFFFF) ----------------------------------------
RD_BASE = 0xF00000           # mirrors rc/guest.inc dosio_rd_base
RD_END = 0xFFFFFF            # mirrors rc/guest.inc dosio_rd_end (inclusive)
RD_MAGIC = b"RDSK"
RD_VERSION = 1
RD_HDR_SIZE = 0x20
RD_ENTRIES = 32
RD_ENT_SIZE = 64
RD_DIR = RD_BASE + RD_HDR_SIZE                  # first directory entry
RD_DATA = RD_BASE + 0x1000                      # first data byte (page aligned)
RD_NAME_OFF = 32
RD_NAME_MAX = 31                                # chars, excl. NUL
RD_NEW_CAP = 0x10000                            # extent of a guest-created file
RD_FLAG_USED = 0x01

# Header: magic[4] u8 version u8 n_entries u16 0 u32 free_ptr u32 data_end
#         u8 reserved[16]
# Entry:  u8 flags u8 name_len u16 0 u32 size u32 capacity u32 data_addr
#         u8 reserved[16] char name[32]
# All integers LITTLE-endian: C64-side metadata, not guest-visible data.


@dataclass
class RdFile:
    name: str
    data: bytes
    capacity: int | None = None       # default: max(len(data), RD_NEW_CAP)


def _check_name(name: str) -> bytes:
    raw = name.encode("latin-1")
    if not 1 <= len(raw) <= RD_NAME_MAX or b"\0" in raw:
        raise ValueError(f"bad RAMDISK file name {name!r}")
    return raw


def ramdisk_image(files: list[RdFile] | dict[str, bytes] = ()) -> bytes:
    """Byte image of the RAMDISK metadata + data, starting at :data:`RD_BASE`.

    Files are laid out consecutively from :data:`RD_DATA`, each extent page
    aligned.  Returns bytes covering ``RD_BASE .. end of last extent``.
    """
    if isinstance(files, dict):
        files = [RdFile(n, d) for n, d in files.items()]
    if len(files) > RD_ENTRIES:
        raise ValueError("too many RAMDISK files")
    seen: set[bytes] = set()
    img = bytearray(RD_DATA - RD_BASE)
    ptr = RD_DATA
    entries = []
    for f in files:
        raw = _check_name(f.name)
        if raw.lower() in seen:
            raise ValueError(f"duplicate RAMDISK name {f.name!r}")
        seen.add(raw.lower())
        cap = f.capacity if f.capacity is not None else max(len(f.data), RD_NEW_CAP)
        if cap < len(f.data):
            raise ValueError("capacity smaller than data")
        cap = (cap + 0xFF) & ~0xFF
        if ptr + cap > RD_END + 1:
            raise ValueError("RAMDISK full")
        entries.append((raw, len(f.data), cap, ptr))
        img += f.data + bytes(cap - len(f.data))
        ptr += cap
    struct.pack_into("<4sBBHII", img, 0, RD_MAGIC, RD_VERSION, RD_ENTRIES, 0,
                     ptr, RD_END + 1)
    for i, (raw, size, cap, addr) in enumerate(entries):
        off = RD_DIR - RD_BASE + i * RD_ENT_SIZE
        struct.pack_into("<BBHIII", img, off, RD_FLAG_USED, len(raw), 0,
                         size, cap, addr)
        img[off + RD_NAME_OFF:off + RD_NAME_OFF + len(raw)] = raw
    return bytes(img)


def ramdisk_chunks(files: list[RdFile] | dict[str, bytes] = ()) -> list[Chunk]:
    """Image chunks staging a formatted RAMDISK holding *files*.

    The header + directory page ($1000 bytes) is always staged in full so
    no stale entry survives; each file's data is staged, its capacity slack
    is not (the size field bounds every read).
    """
    img = ramdisk_image(files)
    chunks = [Chunk(RD_BASE, img[:RD_DATA - RD_BASE])]
    for ent in parse_ramdisk_bytes(img).values():
        if ent.data:
            chunks.append(Chunk(ent.addr, ent.data))
    return chunks


@dataclass
class RdEntry:
    index: int
    name: str
    size: int
    capacity: int
    addr: int
    data: bytes = b""


def parse_directory(read: Callable[[int, int], bytes],
                    with_data: bool = True) -> dict[str, RdEntry]:
    """Parse a RAMDISK through *read(addr, n)* (guest addresses).

    Returns ``{lowercased name: RdEntry}``; raises ValueError on a bad
    header.  Used by the VICE driver to read files back after a run.
    """
    hdr = read(RD_BASE, RD_HDR_SIZE)
    magic, ver, n, _, free_ptr, data_end = struct.unpack_from("<4sBBHII", hdr)
    if magic != RD_MAGIC or ver != RD_VERSION or n != RD_ENTRIES:
        raise ValueError(f"no RAMDISK header (magic={magic!r} ver={ver} n={n})")
    out: dict[str, RdEntry] = {}
    for i in range(RD_ENTRIES):
        base = RD_DIR + i * RD_ENT_SIZE
        flags = read(base, 1)[0]
        if not flags & RD_FLAG_USED:
            continue
        ent = read(base, RD_ENT_SIZE)
        _, nlen, _, size, cap, addr = struct.unpack_from("<BBHIII", ent)
        name = ent[RD_NAME_OFF:RD_NAME_OFF + nlen].decode("latin-1")
        e = RdEntry(i, name, size, cap, addr)
        if with_data and size:
            e.data = read(addr, size)
        out[name.lower()] = e
    return out


def parse_ramdisk_bytes(img: bytes) -> dict[str, RdEntry]:
    """:func:`parse_directory` over an in-memory image starting at RD_BASE."""
    def read(addr: int, n: int) -> bytes:
        off = addr - RD_BASE
        chunk = img[off:off + n]
        return chunk + bytes(n - len(chunk))
    return parse_directory(read)


# --- Assembly mirror check helpers ---------------------------------------------

def asm_equates(path: Path) -> dict[str, int]:
    """``NAME = $hex`` / ``NAME = dec`` equates of an assembly source."""
    out: dict[str, int] = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\$[0-9A-Fa-f]+|\d+)\b",
                     line)
        if m:
            v = m.group(2)
            out[m.group(1)] = int(v[1:], 16) if v.startswith("$") else int(v)
    return out
