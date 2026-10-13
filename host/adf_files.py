#!/usr/bin/env python3
"""W16-D: files and drawers added to (or deleted from) the staged Workbench disk.

docs/WAVE16_BRIEF.md "W16-D" (1); contract docs/CONTRACTS.md "HLE trackdisk
and DF0" (the overlay).  Like host/adf_overlay.py (whose reader this
reuses), it works on an in-memory copy: the user's ADF is read, never
written, and only the changed blocks travel to the REU copy (the same
Overlay block list, so both staging paths are unchanged).

On-disk format (public: Laurent Clevy, "The .ADF (Amiga Disk File) format
FAQ", the root / user directory / file header / file extension / data /
bitmap block sections):

  file header (T_HEADER 2, ST_FILE -3): +4 header_key (itself), +8 high_seq
    (data pointers in this block), +16 first_data, +20 checksum, +24..+311
    the data-block table (first block at +308, downwards), +320 protection,
    +324 byte size, +420/+424/+428 date (days since 1978-01-01, minutes,
    ticks), +432 BCPL name (<= 30), +496 hash_chain, +500 parent, +504
    extension (the first T_LIST block), +508 secondary type
  user directory (ST_USERDIR 2): the same, with the hash table at +24
  extension block (T_LIST 16, ST_FILE): +4 itself, +8 pointers, the table
    as in the header, +500 the file header, +504 the next extension
  OFS data block (T_DATA 8): +4 the file header, +8 seq (1..), +12 bytes,
    +16 next data block, +20 checksum, 488 bytes of data; FFS: 512 raw bytes
  bitmap block: +0 checksum (the 128 longs sum to 0), then one bit per block
    from block 2 (bit (n-2) % 32 of long 1 + (n-2) // 32; 1 = free); the
    root's +312 bm_flag (-1 = valid), +316 bm_pages[25], +416 bm_ext

fsck(image) walks the whole tree and checks every header, chain, data and
extension block, that no block is used twice, and that the bitmap is exactly
the complement of the used set.  Every image this module produces passes it.

    python3 -m host.adf_files DISK.adf [--fsck] [--free]
"""

from __future__ import annotations

import argparse
import struct
import sys

from host import adf_overlay as O
from host.adf_overlay import (BLOCK, BLOCKS, HT_SIZE, ROOT, ST_FILE, ST_ROOT,
                              ST_USERDIR, T_DATA, T_HEADER, T_LIST, AdfError,
                              _l, _sl, block_checksum, checksum_ok)

BM_FLAG, BM_PAGES, BM_EXT = BLOCK - 200, BLOCK - 196, BLOCK - 96
ST_SOFTLINK, ST_LINKDIR, ST_LINKFILE = 3, 4, -4
# a fixed date for every block this module writes (reproducible overlays):
# 2026-10-12 00:00 = 17,817 days after 1978-01-01
DATE = (17817, 0, 0)


def bitmap_checksum(b) -> int:
    return block_checksum(b, 0)


def bitmap_pages(root: bytes) -> list[int]:
    if _l(root, BM_EXT):
        raise AdfError("bitmap extension blocks: not a DD disk")
    return [k for k in (_l(root, BM_PAGES + 4 * i) for i in range(25)) if k]


def _is_free(bms: list[bytes], n: int) -> bool:
    i = n - 2
    page, bit = divmod(i, 127 * 32)
    return bool(_l(bms[page], 4 + 4 * (bit // 32)) >> (bit % 32) & 1)


# --------------------------------------------------------------------------
# the checker
# --------------------------------------------------------------------------

def fsck(data: bytes, check_bitmap: bool = True) -> set[int]:
    """Raise AdfError on any inconsistency; return the set of used blocks
    (boot blocks, root, bitmap, headers, extension and data blocks)."""
    a = O.Adf(data)
    owner: dict[int, str] = {0: "boot", 1: "boot", ROOT: "root"}

    def use(k: int, what: str) -> None:
        if not 2 <= k < BLOCKS:
            raise AdfError(f"{what}: block {k} out of range")
        if k in owner:
            raise AdfError(f"{what}: block {k} already used by {owner[k]}")
        owner[k] = what

    root = a.block(ROOT)
    if _l(root, 12) != HT_SIZE:
        raise AdfError("root: ht_size is not 72")
    bms = bitmap_pages(root) if check_bitmap else []
    for k in bms:
        use(k, "bitmap")
    per = BLOCK if a.ffs else BLOCK - 24

    def walk(dk: int, path: str) -> None:
        d = a.header(dk)
        for slot in range(HT_SIZE):
            k = _l(d, 24 + 4 * slot)
            while k:
                h = a.header(k)
                name = a.bname(h)
                p = f"{path}/{name}" if path else name
                use(k, p)
                if _l(h, 4) != k:
                    raise AdfError(f"{p}: header_key {_l(h, 4)} != {k}")
                if _l(h, BLOCK - 12) != dk:
                    raise AdfError(f"{p}: parent {_l(h, BLOCK - 12)} != {dk}")
                if a.name_hash(name) != slot:
                    raise AdfError(f"{p}: in hash slot {slot}, its hash is {a.name_hash(name)}")
                st = _sl(h, BLOCK - 4)
                if st == ST_USERDIR:
                    walk(k, p)
                elif st == ST_FILE:
                    file(k, h, p)
                elif st not in (ST_SOFTLINK, ST_LINKDIR, ST_LINKFILE):
                    raise AdfError(f"{p}: secondary type {st}")
                k = _l(h, BLOCK - 16)

    def file(hk: int, h: bytes, p: str) -> None:
        size = _l(h, BLOCK - 188)
        blocks = a.file_blocks(hk)             # checks the extension blocks
        need = -(-size // per)
        if len(blocks) < need:          # more is tolerated: the W13-W script overlay
            raise AdfError(f"{p}: {len(blocks)} data blocks for {size} bytes")   # keeps them
        if _l(h, 8) != min(len(blocks), HT_SIZE):
            raise AdfError(f"{p}: high_seq {_l(h, 8)}")
        if blocks and _l(h, 16) != blocks[0]:
            raise AdfError(f"{p}: first_data {_l(h, 16)} != {blocks[0]}")
        k = _l(h, BLOCK - 8)
        while k:
            use(k, f"{p} (extension)")
            k = _l(a.block(k), BLOCK - 8)
        for i, k in enumerate(blocks):
            use(k, f"{p} (data {i})")
            if not a.ffs:
                b = a.block(k)
                n = max(0, min(per, size - i * per))
                nxt = blocks[i + 1] if i + 1 < need else 0
                if (_l(b, 0), _l(b, 4), _l(b, 8), _l(b, 12), _l(b, 16)) != (T_DATA, hk, i + 1, n, nxt) \
                        or not checksum_ok(b):
                    raise AdfError(f"{p}: OFS data block {k} (#{i + 1}) header or checksum")

    walk(ROOT, "")
    if check_bitmap:
        if _l(root, BM_FLAG) != 0xFFFFFFFF:
            raise AdfError("root: bm_flag is not valid (-1)")
        pages = [a.block(k) for k in bms]
        for k, b in zip(bms, pages):
            if bitmap_checksum(b) != _l(b, 0):
                raise AdfError(f"bitmap block {k}: bad checksum")
        if len(pages) * 127 * 32 < BLOCKS - 2:
            raise AdfError("bitmap: too few pages")
        for n in range(2, BLOCKS):
            if _is_free(pages, n) == (n in owner):
                raise AdfError(f"bitmap: block {n} is {'free' if n not in owner else 'used'} "
                               f"({owner.get(n, 'unused')}) but marked "
                               f"{'free' if n in owner else 'used'}")
    return set(owner)


# --------------------------------------------------------------------------
# the editor
# --------------------------------------------------------------------------

class DiskEdit:
    """An editable copy of an image.  Every operation keeps the disk
    consistent (fsck holds after each one)."""

    def __init__(self, data: bytes):
        O.Adf(data)
        self.img = bytearray(data)
        root = self.blk(ROOT)
        if _l(root, BM_FLAG) != 0xFFFFFFFF:
            raise AdfError("root: bitmap not valid")
        self.bms = bitmap_pages(root)
        if not self.bms:
            raise AdfError("root: no bitmap block")

    # -- raw block access ---------------------------------------------------
    def blk(self, k: int) -> bytearray:
        return bytearray(self.img[k * BLOCK:(k + 1) * BLOCK])

    def put(self, k: int, b, cks_at: int | None = 20) -> None:
        if not 2 <= k < BLOCKS or len(b) != BLOCK:   # rev16-p L7: never past the image
            raise AdfError(f"put: block {k} ({len(b)} bytes) outside 2..{BLOCKS - 1}")
        b = bytearray(b)
        if cks_at is not None:
            struct.pack_into(">I", b, cks_at, block_checksum(b, cks_at))
        self.img[k * BLOCK:(k + 1) * BLOCK] = b

    @property
    def adf(self) -> O.Adf:
        return O.Adf(bytes(self.img))

    # -- the bitmap -----------------------------------------------------------
    def _bit(self, n: int, free: bool) -> None:
        i = n - 2
        page, bit = divmod(i, 127 * 32)
        k = self.bms[page]
        b = self.blk(k)
        off = 4 + 4 * (bit // 32)
        v = _l(b, off)
        v = v | (1 << bit % 32) if free else v & ~(1 << bit % 32)
        struct.pack_into(">I", b, off, v & 0xFFFFFFFF)
        self.put(k, b, 0)

    def is_free(self, n: int) -> bool:
        return _is_free([self.blk(k) for k in self.bms], n)

    def free_count(self) -> int:
        return sum(self.is_free(n) for n in range(2, BLOCKS))

    def alloc(self, n: int) -> list[int]:
        """n free blocks, deterministic: from the root upwards, then below."""
        order = list(range(ROOT + 1, BLOCKS)) + list(range(2, ROOT))
        got = [k for k in order if self.is_free(k)][:n]
        if len(got) < n:
            raise AdfError(f"disk full: {n} blocks wanted, {len(got)} free")
        for k in got:
            self._bit(k, False)
        return got

    # -- directory entries ------------------------------------------------------
    def _split(self, path: str) -> tuple[int, str]:
        parent, _, name = path.strip("/").rpartition("/")
        if not 1 <= len(name) <= 30 or any(c in name for c in ":/"):
            raise AdfError(f"{path!r}: bad name")
        pk = self.adf.lookup(parent) if parent else ROOT
        if pk is None:
            raise AdfError(f"{parent}: no such drawer")
        if _sl(self.adf.header(pk), BLOCK - 4) not in (ST_ROOT, ST_USERDIR):
            raise AdfError(f"{parent}: not a drawer")
        return pk, name

    def _link(self, pk: int, k: int, name: str) -> None:
        """Append header k at the end of its hash slot's chain in pk."""
        slot = 24 + 4 * self.adf.name_hash(name)
        p = self.blk(pk)
        c = _l(p, slot)
        if not c:
            struct.pack_into(">I", p, slot, k)
            self.put(pk, p)
            return
        while True:
            h = self.blk(c)
            nxt = _l(h, BLOCK - 16)
            if not nxt:
                struct.pack_into(">I", h, BLOCK - 16, k)
                self.put(c, h)
                return
            c = nxt

    def _unlink(self, pk: int, k: int, name: str) -> None:
        slot = 24 + 4 * self.adf.name_hash(name)
        nxt = _l(self.blk(k), BLOCK - 16)
        p = self.blk(pk)
        if _l(p, slot) == k:
            struct.pack_into(">I", p, slot, nxt)
            self.put(pk, p)
            return
        c = _l(p, slot)
        while c:
            h = self.blk(c)
            if _l(h, BLOCK - 16) == k:
                struct.pack_into(">I", h, BLOCK - 16, nxt)
                self.put(c, h)
                return
            c = _l(h, BLOCK - 16)
        raise AdfError(f"{name}: not in its parent's hash chain")

    def _header(self, k: int, pk: int, name: str, st: int) -> bytearray:
        h = bytearray(BLOCK)
        struct.pack_into(">II", h, 0, T_HEADER, k)
        struct.pack_into(">III", h, BLOCK - 92, *DATE)
        h[BLOCK - 80] = len(name)
        h[BLOCK - 79:BLOCK - 79 + len(name)] = name.encode("latin-1")
        struct.pack_into(">I", h, BLOCK - 12, pk)
        struct.pack_into(">i", h, BLOCK - 4, st)
        return h

    # -- the operations ---------------------------------------------------------
    def mkdir(self, path: str) -> int:
        pk, name = self._split(path)
        if self.adf.lookup(path) is not None:
            raise AdfError(f"{path}: exists")
        (k,) = self.alloc(1)
        self.put(k, self._header(k, pk, name, ST_USERDIR))
        self._link(pk, k, name)
        return k

    def add_file(self, path: str, content: bytes, protect: int = 0) -> int:
        """A new file (refused if the name exists).  Blocks: header, then
        extension blocks, then data, all from alloc()."""
        pk, name = self._split(path)
        if self.adf.lookup(path) is not None:
            raise AdfError(f"{path}: exists")
        ffs = self.adf.ffs
        per = BLOCK if ffs else BLOCK - 24
        nd = -(-len(content) // per)
        ne = max(0, -(-(nd - HT_SIZE) // HT_SIZE))
        ks = self.alloc(1 + ne + nd)
        hk, eks, dks = ks[0], ks[1:1 + ne], ks[1 + ne:]
        tables = [dks[i:i + HT_SIZE] for i in range(0, nd, HT_SIZE)] or [[]]
        h = self._header(hk, pk, name, ST_FILE)
        struct.pack_into(">I", h, 8, len(tables[0]))
        struct.pack_into(">I", h, 16, dks[0] if dks else 0)
        struct.pack_into(">I", h, BLOCK - 192, protect)
        struct.pack_into(">I", h, BLOCK - 188, len(content))
        struct.pack_into(">I", h, BLOCK - 8, eks[0] if eks else 0)
        for i, d in enumerate(tables[0]):
            struct.pack_into(">I", h, 24 + 4 * (HT_SIZE - 1 - i), d)
        self.put(hk, h)
        for j, ek in enumerate(eks):
            e = bytearray(BLOCK)
            t = tables[j + 1]
            struct.pack_into(">III", e, 0, T_LIST, ek, len(t))
            for i, d in enumerate(t):
                struct.pack_into(">I", e, 24 + 4 * (HT_SIZE - 1 - i), d)
            struct.pack_into(">I", e, BLOCK - 12, hk)
            struct.pack_into(">I", e, BLOCK - 8, eks[j + 1] if j + 1 < ne else 0)
            struct.pack_into(">i", e, BLOCK - 4, ST_FILE)
            self.put(ek, e)
        for i, dk in enumerate(dks):
            c = content[i * per:(i + 1) * per]
            if ffs:
                self.put(dk, c.ljust(BLOCK, b"\0"), None)
            else:
                d = bytearray(BLOCK)
                struct.pack_into(">IIIII", d, 0, T_DATA, hk, i + 1, len(c),
                                 dks[i + 1] if i + 1 < nd else 0)
                d[24:24 + len(c)] = c
                self.put(dk, d)
        self._link(pk, hk, name)
        return hk

    def delete(self, path: str) -> list[int]:
        """Remove a file (or an empty drawer): unlinked, its blocks freed.
        The freed blocks keep their bytes (as AmigaDOS leaves them)."""
        a = self.adf
        k = a.lookup(path)
        if k is None:
            raise AdfError(f"{path}: not on the disk")
        h = a.header(k)
        st = _sl(h, BLOCK - 4)
        if st == ST_USERDIR:
            if any(_l(h, 24 + 4 * i) for i in range(HT_SIZE)):
                raise AdfError(f"{path}: drawer not empty")
            freed = [k]
        elif st == ST_FILE:
            freed = [k] + a.file_blocks(k)
            e = _l(h, BLOCK - 8)
            while e:
                freed.append(e)
                e = _l(a.block(e), BLOCK - 8)
        else:
            raise AdfError(f"{path}: secondary type {st} not deletable")
        self._unlink(_l(h, BLOCK - 12), k, a.bname(h))
        for n in freed:
            self._bit(n, True)
        return sorted(freed)

    def data(self) -> bytes:
        return bytes(self.img)


# --------------------------------------------------------------------------
# the Wave 16 player disk (supervisor ruling 2026-10-12: amigaguide.datatype
# is deleted from the REU copy to make room; the trim boot never reads it)
# --------------------------------------------------------------------------
MODWB_DROP = "Classes/DataTypes/amigaguide.datatype"
MODWB_TOOL, MODWB_INFO, MODWB_MOD = "ModPlayer", "ModPlayer.info", "ModPlayer.mod"
LABEL_H, GAP = 12, 8                    # room for the icon's label; spacing


def icon_spot(a: O.Adf, w: int, h: int, drawer: str = "") -> tuple[int, int]:
    """A free drawer position for a w x h icon: the first spot, row by row
    (y from 4, x from 3, steps of 8), clear of every positioned icon in the
    drawer (box + label + GAP) and inside the drawer window's DrawerData
    width (less 30 for the borders).  Deterministic for a given disk."""
    from host import wbicon as W
    dk = a.lookup(drawer) if drawer else ROOT
    own = a.lookup(f"{drawer}.info" if drawer else "Disk.info")
    win_w = 300
    if own is not None:
        do = W.parse(a.read_file(own))
        if do.drawer:
            win_w = struct.unpack_from(">h", do.drawer, 4)[0]
    boxes = []
    d = a.header(dk)
    for slot in range(HT_SIZE):
        k = _l(d, 24 + 4 * slot)
        while k:
            hb = a.header(k)
            n = a.bname(hb)
            if n.lower().endswith(".info") and n.lower() != "disk.info" \
                    and _sl(hb, BLOCK - 4) == ST_FILE:
                try:
                    do = W.parse(a.read_file(k))
                except W.IconError:
                    do = None
                if do and do.x != W.NO_POSITION:
                    boxes.append((do.x, do.y, do.x + do.image.width, do.y + do.image.height + LABEL_H))
            k = _l(hb, BLOCK - 16)
    for y in range(4, 400, 8):
        for x in range(3, max(4, win_w - 30 - w), 8):
            x1, y1 = x + w, y + h + LABEL_H
            if all(x1 + GAP <= b[0] or x >= b[2] + GAP or y1 + GAP <= b[1] or y >= b[3] + GAP
                   for b in boxes):
                return x, y
    raise AdfError("no free icon position")


def modwb_image(image: bytes, player: bytes, mod: bytes, drop: str | None = MODWB_DROP,
                stack: int = 8192, with_info: bool = True) -> bytes:
    """*image* (already script-overlaid) with the player, its icon and the
    MOD in the root; *drop* deleted first to make room."""
    from host import wbicon as W
    e = DiskEdit(image)
    if drop:
        e.delete(drop)
    w, h = len(W.PLAYER_ROWS[0]), len(W.PLAYER_ROWS)
    x, y = icon_spot(e.adf, w, h)
    e.add_file(MODWB_TOOL, player)
    if with_info:                       # (red: the e2e's noinfo mutant)
        e.add_file(MODWB_INFO, W.player_icon(x, y, stack))
    e.add_file(MODWB_MOD, mod)
    out = e.data()
    fsck(out)
    return out


def overlay_of(raw: bytes, image: bytes, script: str = "files", header: int | None = None,
               data_blocks: list[int] | None = None) -> O.Overlay:
    """The Overlay (changed blocks only) that turns *raw* into *image*;
    image must pass fsck.  ov.files marks it: adf_overlay.check() then
    validates the applied image with fsck instead of the per-block rule."""
    fsck(image)
    blocks = [(k, image[k * BLOCK:(k + 1) * BLOCK]) for k in range(BLOCKS)
              if image[k * BLOCK:(k + 1) * BLOCK] != raw[k * BLOCK:(k + 1) * BLOCK]]
    if blocks and blocks[0][0] < 2:
        raise AdfError("the boot blocks changed")
    return O.Overlay(script, blocks, header, list(data_blocks or []), files=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("adf")
    ap.add_argument("--fsck", action="store_true")
    ap.add_argument("--free", action="store_true")
    a = ap.parse_args(argv)
    with open(a.adf, "rb") as f:
        data = f.read()
    if a.fsck:
        used = fsck(data)
        print(f"fsck: OK, {len(used)} blocks used, {BLOCKS - len(used)} free")
    if a.free:
        print(f"free blocks: {DiskEdit(data).free_count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
