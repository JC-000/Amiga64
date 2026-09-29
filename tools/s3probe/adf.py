#!/usr/bin/env python3
"""Read-only Amiga DD floppy image (ADF) access, plus a synthetic-ADF writer.

WB-measure (docs/WB_BOOT.md).  The boot model (aros_boot_model.py --adf)
serves disk DMA from an ADF through ``AdfImage``; ``wb_fsuae.py`` and the
report list a disk's files (names and sizes only) through ``walk``.  The
user's Workbench images are proprietary: they are opened read-only where
they lie and never copied.  ``build_adf`` makes a small synthetic OFS/FFS
image for the host tests, so no test needs a real disk.

Layout: 80 cylinders x 2 heads x 11 sectors x 512 bytes = 1760 blocks,
root block 880.  Block formats follow the AmigaDOS "ADF" description
(headers T_HEADER=2, data T_DATA=8, list T_LIST=16; secondary types
ST_ROOT=1, ST_USERDIR=2, ST_FILE=-3).
"""
from __future__ import annotations

import struct
import sys

BLOCK = 512
SECTORS = 11
BLOCKS = 80 * 2 * SECTORS          # 1760
ADF_SIZE = BLOCKS * BLOCK           # 901120
ROOT = BLOCKS // 2                  # 880
HT_SIZE = BLOCK // 4 - 56           # 72 hash slots
T_HEADER, T_DATA, T_LIST = 2, 8, 16
ST_ROOT, ST_USERDIR, ST_FILE = 1, 2, -3


def _l(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _sl(b: bytes, off: int) -> int:
    return struct.unpack_from(">i", b, off)[0]


def name_hash(name: str, intl: bool = False) -> int:
    h = len(name)
    for c in name:
        o = ord(c)
        if 97 <= o <= 122 or (intl and 224 <= o <= 254 and o != 247):
            o -= 32
        h = (h * 13 + o) & 0x7FF
    return h % HT_SIZE


def block_checksum(b: bytes | bytearray, at: int = 20) -> int:
    """The value to store at offset *at* so the block's longs sum to 0."""
    s = sum(struct.unpack(">128I", bytes(b[:at]) + b"\0\0\0\0" + bytes(b[at + 4:BLOCK])))
    return (-s) & 0xFFFFFFFF


def bootblock_checksum(b: bytes | bytearray) -> int:
    """Boot block (1024 bytes) checksum, stored at offset 4: add with carry."""
    s = 0
    for (v,) in struct.iter_unpack(">I", bytes(b[:4]) + b"\0\0\0\0" + bytes(b[8:1024])):
        s += v
        if s > 0xFFFFFFFF:
            s = (s & 0xFFFFFFFF) + 1
    return (~s) & 0xFFFFFFFF


class AdfImage:
    """An ADF opened read-only.  Only the bytes are held; nothing is written."""

    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = f.read()
        if len(self.data) != ADF_SIZE:
            raise ValueError(f"{path}: {len(self.data)} bytes, not a DD ADF ({ADF_SIZE})")
        self.path = path

    @classmethod
    def from_bytes(cls, data: bytes, path: str = "<bytes>") -> "AdfImage":
        img = cls.__new__(cls)
        if len(data) != ADF_SIZE:
            raise ValueError(f"{path}: {len(data)} bytes, not a DD ADF")
        img.data, img.path = bytes(data), path
        return img

    def block(self, n: int) -> bytes:
        return self.data[n * BLOCK:(n + 1) * BLOCK]

    def track(self, t: int) -> bytes:
        """Track t = cylinder * 2 + head: 11 consecutive blocks."""
        return self.data[t * SECTORS * BLOCK:(t + 1) * SECTORS * BLOCK]

    @property
    def dostype(self) -> bytes:
        return self.data[:4]

    @property
    def ffs(self) -> bool:
        return self.data[:3] == b"DOS" and self.data[3] & 1 == 1

    @property
    def intl(self) -> bool:
        return self.data[:3] == b"DOS" and self.data[3] >= 2

    def bootable(self) -> bool:
        return self.data[:3] == b"DOS" and bootblock_checksum(self.data) == _l(self.data, 4)

    @staticmethod
    def bname(b: bytes) -> str:
        n = b[BLOCK - 80]
        return b[BLOCK - 79:BLOCK - 79 + n].decode("latin-1")

    def volume_name(self) -> str:
        return self.bname(self.block(ROOT))

    def entries(self, dirblock: int):
        """(name, header block, sec_type) for every entry of a directory."""
        b = self.block(dirblock)
        for i in range(HT_SIZE):
            k = _l(b, 24 + 4 * i)
            seen = 0
            while k and seen < BLOCKS:
                h = self.block(k)
                yield self.bname(h), k, _sl(h, BLOCK - 4)
                k = _l(h, BLOCK - 16)
                seen += 1

    def lookup(self, path: str) -> int | None:
        """Header block of a path like 'S/Startup-Sequence' (case-insensitive)."""
        cur = ROOT
        for part in [p for p in path.replace(":", "/").split("/") if p]:
            for name, k, _t in self.entries(cur):
                if name.lower() == part.lower():
                    cur = k
                    break
            else:
                return None
        return cur

    def file_size(self, header: int) -> int:
        return _l(self.block(header), BLOCK - 188)

    def file_blocks(self, header: int) -> list[int]:
        """Data blocks of a file in order (header's table, then extensions)."""
        out, k = [], header
        while k:
            b = self.block(k)
            n = _l(b, 8)
            table = [_l(b, 24 + 4 * i) for i in range(HT_SIZE)]
            out += [table[HT_SIZE - 1 - i] for i in range(min(n, HT_SIZE))]
            k = _l(b, BLOCK - 8)
        return out

    def read_file(self, header: int) -> bytes:
        size = self.file_size(header)
        buf = bytearray()
        for k in self.file_blocks(header):
            b = self.block(k)
            buf += b if self.ffs else b[24:24 + _l(b, 12)]
        return bytes(buf[:size])

    def patched(self, path: str, content: bytes) -> "AdfImage":
        """A copy *in memory* with file *path* holding *content* (it must fit
        in the file's existing data blocks).  Used for the trimmed
        Startup-Sequence experiment; nothing is ever written to disk."""
        hk = self.lookup(path)
        if hk is None:
            raise KeyError(path)
        blocks = self.file_blocks(hk)
        per = BLOCK if self.ffs else BLOCK - 24
        if len(content) > per * len(blocks):
            raise ValueError(f"{path}: {len(content)} bytes do not fit in {len(blocks)} blocks")
        img = bytearray(self.data)
        used = max(1, -(-len(content) // per))
        for i, k in enumerate(blocks):
            b = bytearray(img[k * BLOCK:(k + 1) * BLOCK])
            chunk = content[i * per:(i + 1) * per] if i < used else b""
            if self.ffs:
                b[:] = chunk.ljust(BLOCK, b"\0")
            else:
                b[24:] = chunk.ljust(BLOCK - 24, b"\0")
                struct.pack_into(">II", b, 12, len(chunk),
                                 blocks[i + 1] if i + 1 < used else 0)
                struct.pack_into(">I", b, 20, block_checksum(b))
            img[k * BLOCK:(k + 1) * BLOCK] = b
        h = bytearray(img[hk * BLOCK:(hk + 1) * BLOCK])
        struct.pack_into(">I", h, BLOCK - 188, len(content))
        struct.pack_into(">I", h, 20, block_checksum(h))
        img[hk * BLOCK:(hk + 1) * BLOCK] = h
        return AdfImage.from_bytes(bytes(img), self.path + f" [{path} patched in memory]")

    def walk(self, dirblock: int = ROOT, prefix: str = ""):
        """(path, size or None for a directory, header block), depth first."""
        for name, k, t in sorted(self.entries(dirblock), key=lambda e: e[0].lower()):
            p = prefix + name
            if t == ST_USERDIR:
                yield p + "/", None, k
                yield from self.walk(k, p + "/")
            else:
                yield p, self.file_size(k), k


def hunk_kind(exe: bytes) -> str:
    """'bcpl', 'c' or 'data' for an AmigaDOS load file.  A BCPL (Tripos)
    code hunk starts with its own length in longwords (1.3 BCPL commands
    carry a small non-BCPL startup hunk first, so every code hunk is
    checked)."""
    if len(exe) < 32 or _l(exe, 0) != 0x3F3:
        return "data"
    try:
        i = 4
        while _l(exe, i):                      # resident library names
            i += 4 + 4 * _l(exe, i)
        i += 4
        first, last = _l(exe, i + 4), _l(exe, i + 8)
        i += 12 + 4 * (last - first + 1)
        code = False
        while i + 4 <= len(exe):
            t = _l(exe, i) & 0x3FFFFFFF
            if t in (0x3E9, 0x3EA):            # CODE, DATA
                n = _l(exe, i + 4)
                if t == 0x3E9:
                    code = True
                    if n and _l(exe, i + 8) == n:
                        return "bcpl"
                i += 8 + 4 * n
            elif t == 0x3EB:                   # BSS
                i += 8
            elif t in (0x3EC, 0x3ED, 0x3EE):   # RELOC32/16/8
                i += 4
                while True:
                    n = _l(exe, i)
                    i += 4
                    if not n:
                        break
                    i += 4 + 4 * n
            elif t == 0x3F0:                   # SYMBOL
                i += 4
                while True:
                    n = _l(exe, i) & 0xFFFFFF
                    i += 4
                    if not n:
                        break
                    i += 4 * n + 4
            elif t == 0x3F1:                   # DEBUG
                i += 8 + 4 * _l(exe, i + 4)
            elif t == 0x3F2:                   # END
                i += 4
            else:
                break
        return "c" if code else "data"
    except struct.error:
        return "data"


# ---- synthetic images for the host tests --------------------------------

def build_adf(files: dict[str, bytes], ffs: bool = False, volume: str = "Synth",
              bootcode: bytes | None = None) -> bytes:
    """A minimal, valid DD image holding *files* ({'S/Startup-Sequence': b'..'}).
    Directories are created as needed; blocks are allocated from 882 upward.
    With *bootcode* the boot block is 'DOS\\0'/'DOS\\1' + checksum + root 880 +
    that code at offset 12 (checksummed, so it is bootable)."""
    img = bytearray(ADF_SIZE)
    blocks: dict[int, bytearray] = {}
    nxt = [ROOT + 2]

    def alloc() -> int:
        k = nxt[0]
        nxt[0] += 1
        blocks[k] = bytearray(BLOCK)
        return k

    def setname(b, name):
        b[BLOCK - 80] = len(name)
        b[BLOCK - 79:BLOCK - 79 + len(name)] = name.encode("latin-1")

    root = blocks.setdefault(ROOT, bytearray(BLOCK))
    struct.pack_into(">I", root, 0, T_HEADER)
    struct.pack_into(">I", root, 12, HT_SIZE)
    struct.pack_into(">i", root, BLOCK - 200, -1)          # bm_flag: valid
    struct.pack_into(">i", root, BLOCK - 4, ST_ROOT)
    setname(root, volume)
    dirs = {"": ROOT}

    def link(parent: int, name: str, k: int):
        slot = 24 + 4 * name_hash(name)
        pb = blocks[parent]
        chain = _l(pb, slot)
        struct.pack_into(">I", blocks[k], BLOCK - 16, chain)   # hash chain
        struct.pack_into(">I", pb, slot, k)

    def mkdir(path: str) -> int:
        if path in dirs:
            return dirs[path]
        parent, _, name = path.rpartition("/")
        pk = mkdir(parent)
        k = alloc()
        b = blocks[k]
        struct.pack_into(">I", b, 0, T_HEADER)
        struct.pack_into(">I", b, 4, k)
        struct.pack_into(">i", b, BLOCK - 4, ST_USERDIR)
        struct.pack_into(">I", b, BLOCK - 12, pk)
        setname(b, name)
        link(pk, name, k)
        dirs[path] = k
        return k

    for path, content in files.items():
        parent, _, name = path.rpartition("/")
        pk = mkdir(parent)
        hk = alloc()
        per = BLOCK if ffs else BLOCK - 24
        chunks = [content[i:i + per] for i in range(0, len(content), per)] or []
        if len(chunks) > HT_SIZE:
            raise ValueError("synthetic files are limited to one header block")
        dks = [alloc() for _ in chunks]
        h = blocks[hk]
        struct.pack_into(">I", h, 0, T_HEADER)
        struct.pack_into(">I", h, 4, hk)
        struct.pack_into(">I", h, 8, len(dks))
        struct.pack_into(">I", h, 16, dks[0] if dks else 0)
        for i, dk in enumerate(dks):
            struct.pack_into(">I", h, 24 + 4 * (HT_SIZE - 1 - i), dk)
        struct.pack_into(">I", h, BLOCK - 188, len(content))
        struct.pack_into(">I", h, BLOCK - 12, pk)
        struct.pack_into(">i", h, BLOCK - 4, ST_FILE)
        setname(h, name)
        link(pk, name, hk)
        for i, (dk, c) in enumerate(zip(dks, chunks)):
            d = blocks[dk]
            if ffs:
                d[:len(c)] = c
            else:
                struct.pack_into(">IIIII", d, 0, T_DATA, hk, i + 1, len(c),
                                 dks[i + 1] if i + 1 < len(dks) else 0)
                d[24:24 + len(c)] = c
                struct.pack_into(">I", d, 20, block_checksum(d))
    for k, b in blocks.items():
        if _l(b, 0) == T_HEADER:
            struct.pack_into(">I", b, 20, block_checksum(b))
        img[k * BLOCK:(k + 1) * BLOCK] = b
    img[:4] = b"DOS\1" if ffs else b"DOS\0"
    struct.pack_into(">I", img, 8, ROOT)
    if bootcode is not None:
        img[12:12 + len(bootcode)] = bootcode
        struct.pack_into(">I", img, 4, bootblock_checksum(img))
    return bytes(img)


def main(argv=None) -> int:
    """List an ADF's files (names and sizes only)."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: adf.py IMAGE.adf", file=sys.stderr)
        return 2
    a = AdfImage(argv[0])
    print(f"# {a.volume_name()}  dostype={a.dostype!r}  bootable={a.bootable()}")
    for p, size, k in a.walk():
        kind = "" if size is None else hunk_kind(a.read_file(k))
        print(f"{'' if size is None else size:>8}  {kind:4}  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
