"""W16-D: host/adf_files.py (files added to / deleted from the staged disk)
and host/wbicon.py (the .info DiskObject).

Synthetic images (tools/s3probe/adf.build_adf plus a bitmap block written
here); the user's disk only in the local-only test at the end (skipped
without it, nothing from it is printed or kept).
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "s3probe"))

from host import adf_files as F  # noqa: E402
from host import adf_overlay as O  # noqa: E402
from host import wbicon as W  # noqa: E402
import adf as S  # noqa: E402

BM = O.ROOT + 1                         # build_adf allocates from 882: 881 is free
USER_ADF = os.environ.get("AMIGA64_TEST_ADF") or os.path.expanduser("~/Amiga/amiga-os-310-workbench.adf")


def with_bitmap(img: bytes) -> bytes:
    """build_adf's image with a valid bitmap at block 881."""
    used = F.fsck(img, check_bitmap=False) | {BM}
    b = bytearray(img)
    bm = bytearray(O.BLOCK)
    for n in range(2, O.BLOCKS):
        if n not in used:
            i = n - 2
            off = 4 + 4 * (i // 32)
            struct.pack_into(">I", bm, off, struct.unpack_from(">I", bm, off)[0] | 1 << (i % 32))
    struct.pack_into(">I", bm, 0, F.bitmap_checksum(bm))
    b[BM * 512:(BM + 1) * 512] = bm
    r = bytearray(b[O.ROOT * 512:(O.ROOT + 1) * 512])
    struct.pack_into(">iI", r, F.BM_FLAG, -1, BM)
    struct.pack_into(">I", r, 20, O.block_checksum(r))
    b[O.ROOT * 512:(O.ROOT + 1) * 512] = r
    return bytes(b)


def disk(ffs: bool = True, extra: dict | None = None) -> bytes:
    files = {"S/Startup-Sequence": b"C:LoadWB\n" * 40, "C/LoadWB": b"\0\0\x03\xf3" + bytes(300),
             "Classes/DataTypes/big.datatype": bytes(range(256)) * 40, "Disk.info": W.build(
                 W.DiskObject(W.WBDISK, W.image_from_rows(["#.#.", ".#.#"]),
                              drawer=struct.pack(">hhhh", 10, 10, 300, 100) + bytes(48)))}
    files.update(extra or {})
    return with_bitmap(S.build_adf(files, ffs=ffs, volume="Synth", bootcode=b"\x4e\x75"))


def blob(n: int, seed: int = 7) -> bytes:
    return bytes((i * seed + (i >> 8)) & 0xFF for i in range(n))


# -- fsck ---------------------------------------------------------------------

@pytest.mark.parametrize("ffs", [False, True])
def test_fsck_accepts_a_built_disk(ffs):
    used = F.fsck(disk(ffs))
    assert {0, 1, O.ROOT, BM} <= used


def _poke(img: bytes, k: int, off: int, val: int, cks: int | None = 20) -> bytes:
    b = bytearray(img)
    blk = bytearray(b[k * 512:(k + 1) * 512])
    struct.pack_into(">I", blk, off, val & 0xFFFFFFFF)
    if cks is not None:
        struct.pack_into(">I", blk, cks, O.block_checksum(blk, cks))
    b[k * 512:(k + 1) * 512] = blk
    return bytes(b)


def test_fsck_refuses_a_bitmap_that_disagrees():
    img = disk()
    used_k = O.Adf(img).lookup("C/LoadWB")
    i = used_k - 2                       # mark a used block free
    bm = img[BM * 512:(BM + 1) * 512]
    off = 4 + 4 * (i // 32)
    with pytest.raises(O.AdfError, match="bitmap: block"):
        F.fsck(_poke(img, BM, off, struct.unpack_from(">I", bm, off)[0] | 1 << (i % 32), 0))
    with pytest.raises(O.AdfError, match="bitmap block .* checksum"):
        F.fsck(_poke(img, BM, off, struct.unpack_from(">I", bm, off)[0] | 1 << (i % 32), None))


def test_fsck_refuses_double_use_and_bad_links():
    img = disk()
    a = O.Adf(img)
    hk = a.lookup("C/LoadWB")
    other = a.lookup("S/Startup-Sequence")
    d0 = a.file_blocks(other)[0]
    with pytest.raises(O.AdfError, match="already used"):
        F.fsck(_poke(_poke(img, hk, 24 + 4 * 71, d0), hk, 16, d0))   # shares S-S's first block
    with pytest.raises(O.AdfError, match="parent"):
        F.fsck(_poke(img, hk, 500, O.ROOT))
    with pytest.raises(O.AdfError, match="header_key"):
        F.fsck(_poke(img, hk, 4, hk + 1))
    with pytest.raises(O.AdfError, match="not a valid header"):
        F.fsck(_poke(img, hk, 324, 1, None))              # checksum not updated
    with pytest.raises(O.AdfError, match="data blocks for"):
        F.fsck(_poke(img, hk, 324, 5000))


def test_fsck_refuses_a_wrong_hash_slot():
    img = disk()
    a = O.Adf(img)
    c = a.lookup("C")
    hk = a.lookup("C/LoadWB")
    slot = a.name_hash("LoadWB")
    img2 = _poke(_poke(img, c, 24 + 4 * slot, 0), c, 24 + 4 * ((slot + 1) % 72), hk)
    with pytest.raises(O.AdfError, match="hash slot"):
        F.fsck(img2)


# -- adding and deleting ---------------------------------------------------------

@pytest.mark.parametrize("ffs", [False, True])
@pytest.mark.parametrize("n", [0, 1, 511, 512, 20000, 72 * 512 + 1, 150 * 512 + 3])
def test_add_file_reads_back(ffs, n):
    e = F.DiskEdit(disk(ffs))
    free0 = e.free_count()
    content = blob(n)
    hk = e.add_file("Tool", content)
    img = e.data()
    used = F.fsck(img)
    a = O.Adf(img)
    assert a.lookup("tool") == hk and a.read_file(hk) == content
    per = 512 if ffs else 488
    nd = -(-n // per)
    ne = max(0, -(-(nd - 72) // 72))
    assert F.DiskEdit(img).free_count() == free0 - 1 - nd - ne
    assert hk in used


def test_add_in_a_taken_slot_appends_to_the_chain():
    e = F.DiskEdit(disk())
    a = e.adf
    names = [f"N{i}" for i in range(400)]
    slot = a.name_hash("Tool")
    twins = [n for n in names if a.name_hash(n) == slot][:3]
    ks = [e.add_file(n, blob(10, i + 3)) for i, n in enumerate(["Tool"] + twins)]
    img = e.data()
    F.fsck(img)
    a = O.Adf(img)
    chain, k = [], struct.unpack_from(">I", a.block(O.ROOT), 24 + 4 * slot)[0]
    while k:
        chain.append(k)
        k = struct.unpack_from(">I", a.block(k), 496)[0]
    assert chain == ks                   # first in the slot, the rest appended in order
    assert [a.read_file(a.lookup(n)) for n in ["Tool"] + twins] == [blob(10, i + 3) for i in range(4)]


def _chain(a: O.Adf, dk: int, slot: int) -> list[int]:
    out, k = [], struct.unpack_from(">I", a.block(dk), 24 + 4 * slot)[0]
    while k:
        out.append(k)
        k = struct.unpack_from(">I", a.block(k), 496)[0]
    return out


def test_a_chain_stays_in_ascending_block_order():
    """rev16-d L3: a header that lands on a lower block than the chain's
    tail goes in its place (every multi-entry chain on the user's Commodore
    disks ascends)."""
    e = F.DiskEdit(disk())
    slot = e.adf.name_hash("Tool")
    twins = [n for n in (f"N{i}" for i in range(400)) if e.adf.name_hash(n) == slot][:3]
    ks = [e.add_file(n, blob(600, i + 2)) for i, n in enumerate(["Tool"] + twins[:2])]
    e.delete(twins[0])                   # the middle's blocks are free again: the
    k = e.add_file(twins[2], blob(10))   # next header takes the lowest of them
    assert ks[0] < k < ks[2]
    img = e.data()
    F.fsck(img)
    a = O.Adf(img)
    assert _chain(a, O.ROOT, slot) == [ks[0], k, ks[2]]
    assert a.read_file(a.lookup(twins[2])) == blob(10)
    assert [a.read_file(a.lookup(n)) for n in ("Tool", twins[1])] == [blob(600, 2), blob(600, 4)]


def test_files_are_written_with_every_protection_bit_clear():
    """rev16-d L2: protection +320 is 0 (RWED allowed; the bits deny when
    set): Workbench refuses to run a tool with E set."""
    raw = disk()
    out = F.modwb_image(raw, blob(3000), blob(700, 3), drop="Classes/DataTypes/big.datatype")
    a = O.Adf(out)
    for n in (F.MODWB_TOOL, F.MODWB_INFO, F.MODWB_MOD):
        assert struct.unpack_from(">I", a.header(a.lookup(n)), 320)[0] == 0, n


def test_fsck_refuses_an_extension_block_with_a_wrong_own_key():
    """rev16-d L1: an extension block's +4 header_key is itself (FAQ)."""
    e = F.DiskEdit(disk())
    hk = e.add_file("Big", blob(100 * 512))
    img = e.data()
    ek = struct.unpack_from(">I", O.Adf(img).block(hk), 504)[0]
    assert ek
    F.fsck(img)
    with pytest.raises(O.AdfError, match="extension block .* header_key"):
        F.fsck(_poke(img, ek, 4, hk))


def test_a_near_full_disk_allocates_below_the_root():
    """rev16-d L4 (without the user's disk): every block above the root
    taken, the file goes below it."""
    e = F.DiskEdit(disk())
    top = [k for k in range(O.ROOT + 1, O.BLOCKS) if e.is_free(k)]
    e.add_file("Filler", blob(512 * (len(top) - 2)))   # header + 1 extension + data
    assert not any(e.is_free(k) for k in range(O.ROOT + 1, O.BLOCKS))
    hk = e.add_file("Low", blob(3000))
    ks = [hk] + e.adf.file_blocks(hk)
    assert max(ks) < O.ROOT
    F.fsck(e.data())
    assert e.adf.read_file(hk) == blob(3000)


def test_names_compare_as_the_filesystem_folds_them():
    """rev16-d L6: Latin-1 names only; on a non-intl disk only a-z fold,
    so a-grave and A-grave are two names."""
    e = F.DiskEdit(disk())
    assert not e.adf.intl
    with pytest.raises(O.AdfError, match="Latin-1"):
        e.add_file("Tool€", b"x")
    lo, up = "AGRà", "AGRÀ"              # one hash slot on DOS\1
    assert e.adf.name_hash(lo) == e.adf.name_hash(up)
    e.add_file(lo, b"1")
    e.add_file(up, b"2")                 # not a duplicate on DOS\1
    a = e.adf
    assert a.read_file(a.lookup("agrà")) == b"1"
    assert a.read_file(a.lookup("agrÀ")) == b"2"
    F.fsck(e.data())


def test_delete_frees_and_unlinks_a_chain_middle():
    e = F.DiskEdit(disk())
    a = e.adf
    slot = a.name_hash("Tool")
    twins = [n for n in (f"N{i}" for i in range(400)) if a.name_hash(n) == slot][:2]
    for n in ["Tool"] + twins:
        e.add_file(n, blob(3000))
    free0 = e.free_count()
    freed = e.delete(twins[0])           # the middle of the chain
    assert len(freed) == 1 + 6
    assert e.free_count() == free0 + 7
    img = e.data()
    F.fsck(img)
    a = O.Adf(img)
    assert a.lookup(twins[0]) is None and a.lookup(twins[1]) is not None and a.lookup("Tool")
    e.delete("Tool")                     # the slot head
    F.fsck(e.data())
    assert e.adf.lookup(twins[1]) is not None


def test_delete_a_big_file_frees_its_extension_blocks():
    e = F.DiskEdit(disk())
    e.add_file("Big", blob(100 * 512))
    free0 = e.free_count()
    assert len(e.delete("Big")) == 1 + 1 + 100
    assert e.free_count() == free0 + 102
    F.fsck(e.data())


def test_freed_blocks_are_reused():
    e = F.DiskEdit(disk())
    freed = e.delete("Classes/DataTypes/big.datatype")
    k = e.add_file("Tool", blob(5000))
    assert set(e.adf.file_blocks(k)) | {k} <= set(freed)


def test_mkdir_and_a_file_inside():
    e = F.DiskEdit(disk(False))
    dk = e.mkdir("Player")
    hk = e.add_file("Player/Tool", blob(700))
    img = e.data()
    F.fsck(img)
    a = O.Adf(img)
    assert a.lookup("Player") == dk and a.lookup("player/tool") == hk
    with pytest.raises(O.AdfError, match="not empty"):
        e.delete("Player")
    e.delete("Player/Tool")
    e.delete("Player")
    F.fsck(e.data())


def test_refusals():
    e = F.DiskEdit(disk())
    with pytest.raises(O.AdfError, match="exists"):
        e.add_file("C/LoadWB", b"x")
    with pytest.raises(O.AdfError, match="no such drawer"):
        e.add_file("Nope/x", b"x")
    with pytest.raises(O.AdfError, match="not a drawer"):
        e.add_file("C/LoadWB/x", b"x")
    with pytest.raises(O.AdfError, match="bad name"):
        e.add_file("x" * 31, b"x")
    with pytest.raises(O.AdfError, match="not on the disk"):
        e.delete("C/Nope")
    before = e.data()
    with pytest.raises(O.AdfError, match="disk full"):
        e.add_file("Huge", bytes(900 * 1024))
    assert e.data() == before            # nothing allocated by a refused add
    with pytest.raises(O.AdfError, match="bitmap"):
        F.DiskEdit(_poke(disk(), O.ROOT, F.BM_FLAG, 0))


def test_allocation_is_deterministic_from_the_root_up():
    e = F.DiskEdit(disk())
    hk = e.add_file("Tool", blob(2000))
    ks = [hk] + e.adf.file_blocks(hk)
    assert ks == sorted(ks) and ks[0] > O.ROOT
    assert F.DiskEdit(disk()).add_file("Tool", blob(2000)) == hk


# -- the overlay path -----------------------------------------------------------------

def test_overlay_of_applies_and_checks_by_fsck():
    raw = disk()
    e = F.DiskEdit(O.apply(raw, O.make_overlay(raw, "trim")))
    e.delete("Classes/DataTypes/big.datatype")
    e.add_file("Tool", blob(9000))
    img = e.data()
    ov = F.overlay_of(raw, img, "trim+files")
    assert ov.files and O.apply(raw, ov) == img
    assert all(raw[k * 512:(k + 1) * 512] != b for k, b in ov.blocks)
    assert O.reu_writes(ov, 0x200000)[0][0] == 0x200000 + ov.blocks[0][0] * 512
    # a broken overlay (the bitmap block left out) is refused by apply()
    bad = O.Overlay(ov.script, [(k, b) for k, b in ov.blocks if k != BM], files=True)
    with pytest.raises(O.AdfError, match="bitmap"):
        O.apply(raw, bad)


def test_ffs_data_block_that_looks_like_a_header_is_accepted():
    raw = disk()
    e = F.DiskEdit(raw)
    e.add_file("Tool", struct.pack(">I", O.T_HEADER) + bytes(600))   # raw FFS data
    ov = F.overlay_of(raw, e.data())
    assert O.apply(raw, ov) == e.data()


# -- the icon ---------------------------------------------------------------------------

def test_player_icon_parses_back():
    b = W.player_icon(299, 4)
    do = W.parse(b)
    assert (do.type, do.x, do.y, do.stack) == (W.WBTOOL, 299, 4, 8192)
    assert (do.image.width, do.image.height, do.image.depth) == (48, 22, 2)
    pens = {".": 0, "#": 1, "o": 2, "+": 3}
    assert all(do.image.pixel(x, y) == pens[c] for y, r in enumerate(W.PLAYER_ROWS)
               for x, c in enumerate(r))
    assert do.select is None and do.flags & W.GADGIMAGE and not do.flags & W.GADGHIMAGE
    assert struct.unpack_from(">HH", b, 0) == (0xE310, 1) and b[48] == 3
    assert len(b) == 78 + 20 + 2 * 6 * 22


def test_icon_roundtrip_with_every_part():
    img = W.image_from_rows(["#o+.", "+o#."])
    do = W.DiskObject(W.WBDRAWER, img, select=img, x=5, y=6, default_tool="SYS:x",
                      tool_types=["FILE=a.mod", "DONOTWAIT"], drawer=bytes(range(56)),
                      drawer2=b"\0\0\0\1\0\2", tool_window="CON:")
    b = W.build(do)
    assert W.parse(b) == do
    with pytest.raises(W.IconError, match="left after"):
        W.parse(b + b"\0")
    with pytest.raises(W.IconError):
        W.parse(b[:-3])
    with pytest.raises(W.IconError, match="magic"):
        W.parse(b"\xE3\x11" + b[2:])


def test_icon_spot_avoids_positioned_icons():
    icons = {"A.info": W.player_icon(3, 4), "B.info": W.player_icon(70, 4)}
    a = O.Adf(disk(extra=icons))
    x, y = F.icon_spot(a, 48, 22)
    assert (x, y) == (131, 4)       # first 8-step x clear of 70 + 48 + GAP
    for bx in (3, 70):
        assert x + 48 + 8 <= bx or x >= bx + 48 + 8


def test_modwb_image_on_a_synthetic_disk():
    raw = disk()
    img = O.apply(raw, O.make_overlay(raw, "trim"))
    out = F.modwb_image(img, blob(20000), blob(6460, 3), drop="Classes/DataTypes/big.datatype")
    a = O.Adf(out)
    assert a.lookup("Classes/DataTypes/big.datatype") is None
    assert a.read_file(a.lookup(F.MODWB_TOOL)) == blob(20000)
    assert a.read_file(a.lookup(F.MODWB_MOD)) == blob(6460, 3)
    do = W.parse(a.read_file(a.lookup(F.MODWB_INFO)))
    assert do.type == W.WBTOOL and (do.x, do.y) == (3, 4)
    assert a.read_file(a.lookup("S/Startup-Sequence")) == O.SCRIPTS["trim"]
    O.apply(raw, F.overlay_of(raw, out))


@pytest.mark.skipif(not os.path.exists(USER_ADF), reason="the user's disk is local only")
def test_users_disk_local_only():
    raw = open(USER_ADF, "rb").read()
    F.fsck(raw)
    img = O.apply(raw, O.make_overlay(raw, "trim"))
    out = F.modwb_image(img, blob(30000), blob(6460, 3))
    ov = F.overlay_of(raw, out)
    assert O.apply(raw, ov) == out
    a = O.Adf(out)
    assert a.lookup(F.MODWB_DROP) is None and a.lookup(F.MODWB_TOOL)
    di = a.lookup("Disk.info")
    assert a.read_file(di) == O.Adf(raw).read_file(di)      # the boot's milestones unchanged


# -- bounds (rev16-p L7: nothing past $2DBFFF, the profile block follows) -------

@pytest.mark.parametrize("k", [1760, -1, 4000])
def test_reu_writes_refuses_a_block_outside_the_image(k):
    ov = O.Overlay("x", [(k, bytes(512))])
    with pytest.raises(O.AdfError, match="outside the image"):
        O.reu_writes(ov, 0x200000)
    ok = O.reu_writes(O.Overlay("x", [(1759, bytes(512))]), 0x200000)
    assert ok[0][0] + 512 == 0x200000 + O.ADF_SIZE == 0x2DC000


def test_reu_writes_refuses_a_short_or_long_block():
    for n in (511, 513):
        with pytest.raises(O.AdfError):
            O.reu_writes(O.Overlay("x", [(5, bytes(n))]), 0x200000)


@pytest.mark.parametrize("k", [0, 1, 1760])
def test_diskedit_put_refuses_outside_2_1759(k):
    e = F.DiskEdit(disk())
    with pytest.raises(O.AdfError, match="outside"):
        e.put(k, bytes(512))


def test_modwb_overlay_stays_inside_the_image():
    raw = disk()
    out = F.modwb_image(O.apply(raw, O.make_overlay(raw, "trim")), blob(20000), blob(6460),
                        drop="Classes/DataTypes/big.datatype")
    ws = O.reu_writes(F.overlay_of(raw, out), 0x200000)
    assert all(0x200000 <= a and a + len(b) <= 0x2DC000 for a, b in ws)
