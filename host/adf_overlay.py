#!/usr/bin/env python3
"""W13-W: the Startup-Sequence block overlay for the staged Workbench disk.

docs/WAVE13_BRIEF.md W1, docs/WB_BOOT.md §4.3; contract docs/CONTRACTS.md
"HLE trackdisk and DF0".  The user's ADF is read locally (read-only) and
never changed; the overlay is a handful of 512-byte blocks that replace
``S/Startup-Sequence`` in the REU copy after staging (the C64's LOAD_REU
on the Ultimates, test_load_chunk on VICE).  The result equals, byte for
byte, the image the boot model boots with ``--startup`` (tools/s3probe/
adf.AdfImage.patched), so the C64's screen can be compared with the
model's: host/tests/test_adf_overlay.py pins that on synthetic images.

Scripts: ``trim`` (WB_BOOT §4.3, the default), ``min`` and ``full`` (no
overlay: the disk's own script).

On-disk format (public AmigaDOS/ADF description; the NDK's dos/filehandler
docs): 1760 blocks of 512 bytes, root block 880; a header block (type
T_HEADER = 2) holds the hash table (72 longs from +24), the data-block
table (from +308 downwards), the byte size at +324, the hash chain at
+496, the parent at +500, the secondary type at +508 (ST_ROOT 1,
ST_USERDIR 2, ST_FILE -3) and the name (BCPL string at +432); its checksum
at +20 makes the 128 longs sum to 0.  OFS data blocks (T_DATA = 8) carry a
24-byte header with their own checksum at +20; FFS data blocks are raw.
The name hash: h = len; h = (h * 13 + upper(c)) & $7FF per char; h % 72.

    python3 -m host.adf_overlay DISK.adf [--script trim|min|full] [--json]

prints the overlay's block numbers and checksums (never disk bytes).
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass, field

BLOCK = 512
BLOCKS = 1760                       # 80 cylinders x 2 heads x 11 sectors
ADF_SIZE = BLOCKS * BLOCK           # 901120
ROOT = BLOCKS // 2                  # 880
HT_SIZE = BLOCK // 4 - 56           # 72 hash slots
T_HEADER, T_DATA, T_LIST = 2, 8, 16
ST_ROOT, ST_USERDIR, ST_FILE = 1, 2, -3
SS_PATH = "S/Startup-Sequence"

SCRIPTS: dict[str, bytes | None] = {
    "trim": (b"C:MakeDir RAM:T RAM:ENV\n"
             b"C:Assign >NIL: ENV: RAM:ENV\n"
             b"C:Assign >NIL: T: RAM:T\n"
             b"C:LoadWB\n"
             b"EndCLI >NIL:\n"),
    "min": b"C:LoadWB\nEndCLI >NIL:\n",
    "full": None,
}


class AdfError(ValueError):
    """The image or the overlay is not acceptable."""


def _l(b, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _sl(b, off: int) -> int:
    return struct.unpack_from(">i", b, off)[0]


def block_checksum(b, at: int = 20) -> int:
    """The long to store at *at* so the block's 128 longs sum to 0."""
    s = sum(struct.unpack(">128I", bytes(b[:at]) + b"\0\0\0\0" + bytes(b[at + 4:BLOCK])))
    return (-s) & 0xFFFFFFFF


def checksum_ok(b, at: int = 20) -> bool:
    return sum(struct.unpack(">128I", bytes(b[:BLOCK]))) & 0xFFFFFFFF == 0


class Adf:
    """A DD image held in memory (the caller's bytes are never changed)."""

    def __init__(self, data: bytes):
        if len(data) != ADF_SIZE:
            raise AdfError(f"{len(data)} bytes, not a DD ADF ({ADF_SIZE})")
        if data[:3] != b"DOS":
            raise AdfError("no DOS boot block")
        if data[3] >= 4:                # DOS\4.. dircache / long names: other block types
            raise AdfError(f"dostype DOS\\{data[3]}: only OFS/FFS (DOS\\0-\\3) are supported")
        self.data = bytes(data)
        root = self.block(ROOT)
        if _l(root, 0) != T_HEADER or _sl(root, BLOCK - 4) != ST_ROOT or not checksum_ok(root):
            raise AdfError("root block 880: bad type or checksum")

    @property
    def ffs(self) -> bool:
        return self.data[3] & 1 == 1

    @property
    def intl(self) -> bool:
        return self.data[3] >= 2

    def block(self, n: int) -> bytes:
        if not 0 <= n < BLOCKS:
            raise AdfError(f"block {n} out of range")
        return self.data[n * BLOCK:(n + 1) * BLOCK]

    @staticmethod
    def bname(b: bytes) -> str:
        n = min(b[BLOCK - 80], 30)
        return b[BLOCK - 79:BLOCK - 79 + n].decode("latin-1")

    def fold(self, name: str) -> str:
        """The filesystem's upper case: a-z, and on an intl disk also the
        Latin-1 a-grave..thorn (not the division sign); W16-D, rev16-d L6:
        names compare by this, not by Python's lower()."""
        return "".join(chr(ord(c) - 32) if 97 <= ord(c) <= 122 or
                       (self.intl and 224 <= ord(c) <= 254 and ord(c) != 247) else c
                       for c in name)

    def name_hash(self, name: str) -> int:
        h = len(name)
        for c in self.fold(name):
            h = (h * 13 + ord(c)) & 0x7FF
        return h % HT_SIZE

    def header(self, k: int) -> bytes:
        b = self.block(k)
        if _l(b, 0) != T_HEADER or not checksum_ok(b):
            raise AdfError(f"block {k}: not a valid header block")
        return b

    def lookup(self, path: str) -> int | None:
        """Header block of a path (case-insensitive), through the hash
        tables and chains, as the filesystem finds it."""
        cur = ROOT
        for part in [p for p in path.replace(":", "/").split("/") if p]:
            d = self.header(cur)
            k = _l(d, 24 + 4 * self.name_hash(part))
            for _ in range(BLOCKS):
                if not k:
                    return None
                h = self.header(k)
                if self.fold(self.bname(h)) == self.fold(part):
                    break
                k = _l(h, BLOCK - 16)
            else:
                raise AdfError(f"hash chain loop at {part!r}")
            cur = k
        return cur

    def file_size(self, hk: int) -> int:
        return _l(self.header(hk), BLOCK - 188)

    def file_blocks(self, hk: int) -> list[int]:
        """Data blocks in file order: the header's table, then the extension
        blocks' (type T_LIST, secondary type ST_FILE, parent = the header;
        each holds up to 72 more, in the same reversed order)."""
        out, k = [], hk
        for i in range(BLOCKS):
            if not k:
                return out
            if i == 0:
                b = self.header(k)
            else:
                b = self.block(k)
                if (_l(b, 0) != T_LIST or _sl(b, BLOCK - 4) != ST_FILE
                        or _l(b, BLOCK - 12) != hk or not checksum_ok(b)):
                    raise AdfError(f"block {k}: not a valid extension block of {hk}")
            n = min(_l(b, 8), HT_SIZE)
            out += [_l(b, 24 + 4 * (HT_SIZE - 1 - j)) for j in range(n)]
            k = _l(b, BLOCK - 8)
        raise AdfError("extension chain loop")

    def read_file(self, hk: int) -> bytes:
        buf = bytearray()
        for k in self.file_blocks(hk):
            b = self.block(k)
            buf += b if self.ffs else b[24:24 + _l(b, 12)]
        return bytes(buf[:self.file_size(hk)])


@dataclass
class Overlay:
    script: str
    blocks: list[tuple[int, bytes]] = field(default_factory=list)   # (block, 512 bytes)
    header: int | None = None       # the file's header block
    data_blocks: list[int] = field(default_factory=list)
    files: bool = False             # W16-D: files added/deleted (host/adf_files.py)

    @property
    def first_data(self) -> int | None:
        return self.data_blocks[0] if self.data_blocks else None

    def info(self) -> dict:
        """Names, block numbers and checksums only (no disk bytes)."""
        return {"script": self.script, "header": self.header,
                "data_blocks": self.data_blocks,
                "blocks": [{"block": k, "sum32": f"{sum(struct.unpack('>128I', b)) & 0xFFFFFFFF:08X}",
                            "crc": f"{__import__('zlib').crc32(b):08X}"} for k, b in self.blocks]}


def make_overlay(data: bytes, script: str = "trim", content: bytes | None = None,
                 path: str = SS_PATH) -> Overlay:
    """The overlay that makes *path* hold *content* (default: the script's
    text) in the file's existing data blocks: the same bytes as the boot
    model's AdfImage.patched (data blocks rewritten from the start, OFS
    headers and chain re-made for the used blocks, the header's size and
    checksum updated)."""
    if script not in SCRIPTS:
        raise AdfError(f"unknown script {script!r}")
    a = Adf(data)
    if content is None:
        content = SCRIPTS[script]
    hk = a.lookup(path)
    if hk is None:
        raise AdfError(f"{path}: not on the disk")
    if _sl(a.header(hk), BLOCK - 4) != ST_FILE:
        raise AdfError(f"{path}: not a file")
    blocks = a.file_blocks(hk)
    if content is None:
        return Overlay(script, [], hk, blocks)
    per = BLOCK if a.ffs else BLOCK - 24
    if not blocks or len(content) > per * len(blocks):
        raise AdfError(f"{path}: {len(content)} bytes do not fit in {len(blocks)} blocks")
    used = max(1, -(-len(content) // per))
    new: dict[int, bytes] = {}
    for i, k in enumerate(blocks):
        b = bytearray(a.block(k))
        chunk = content[i * per:(i + 1) * per] if i < used else b""
        if a.ffs:
            b[:] = chunk.ljust(BLOCK, b"\0")
        else:
            b[24:] = chunk.ljust(BLOCK - 24, b"\0")
            struct.pack_into(">II", b, 12, len(chunk), blocks[i + 1] if i + 1 < used else 0)
            struct.pack_into(">I", b, 20, block_checksum(b))
        new[k] = bytes(b)
    h = bytearray(a.block(hk))
    struct.pack_into(">I", h, BLOCK - 188, len(content))
    struct.pack_into(">I", h, 20, block_checksum(h))
    new[hk] = bytes(h)
    ov = Overlay(script, [(k, b) for k, b in sorted(new.items()) if b != a.block(k)],
                 hk, blocks)
    check(data, ov)
    return ov


def check(data: bytes, ov: Overlay) -> None:
    """Refuse an overlay that would stage a broken disk: a block outside
    the image or the boot blocks, a block not 512 bytes, a header (or OFS
    data) block whose checksum does not hold, or a header that no longer
    names the file's blocks."""
    a = Adf(data)
    for k, b in ov.blocks:
        if not 2 <= k < BLOCKS:
            raise AdfError(f"overlay block {k} outside 2..{BLOCKS - 1}")
        if len(b) != BLOCK:
            raise AdfError(f"overlay block {k}: {len(b)} bytes")
        if ov.files:                    # W16-D: FFS data blocks are raw bytes, so
            continue                    # the whole applied image is checked below
        t = _l(b, 0)
        if k == ov.header or t == T_HEADER or (not a.ffs and t == T_DATA):
            if not checksum_ok(b):
                raise AdfError(f"overlay block {k}: bad block checksum")
        if k == ov.header:
            n = min(_l(b, 8), HT_SIZE)
            table = [_l(b, 24 + 4 * (HT_SIZE - 1 - i)) for i in range(n)]
            if table != ov.data_blocks[:n]:
                raise AdfError(f"overlay header {k}: data-block table changed")
    if ov.files:                        # W16-D: the whole tree and the bitmap
        from host.adf_files import fsck
        img = bytearray(data)
        for k, b in ov.blocks:
            img[k * BLOCK:(k + 1) * BLOCK] = b
        fsck(bytes(img))


def apply(data: bytes, ov: Overlay) -> bytes:
    """The staged image: *data* with the overlay's blocks (checked first)."""
    check(data, ov)
    img = bytearray(data)
    for k, b in ov.blocks:
        img[k * BLOCK:(k + 1) * BLOCK] = b
    return bytes(img)


def reu_writes(ov: Overlay, base: int) -> list[tuple[int, bytes]]:
    """(REU address, block) pairs for an image staged at *base*.  W16-D
    (rev16-p L7): every block must be 0 <= k < 1760 and 512 bytes, so no
    overlay can write past the image (W16-P's profile block and the app
    anchor follow it at $2DC000)."""
    for k, b in ov.blocks:                                       # W16-D
        if not 0 <= k < BLOCKS or len(b) != BLOCK:               # W16-D
            raise AdfError(f"reu_writes: block {k} ({len(b)} bytes) outside the image")
    return [(base + k * BLOCK, b) for k, b in ov.blocks]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("adf")
    ap.add_argument("--script", default="trim", choices=sorted(SCRIPTS))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    with open(a.adf, "rb") as f:
        data = f.read()
    ov = make_overlay(data, a.script)
    info = ov.info()
    print(json.dumps(info, indent=1) if a.json else
          f"{a.script}: header {ov.header}, data {ov.data_blocks}, "
          f"overlay blocks {[k for k, _ in ov.blocks]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
