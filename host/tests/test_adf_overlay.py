"""W13-W: host/adf_overlay.py, the Startup-Sequence block overlay.

Synthetic images only (tools/s3probe/adf.build_adf): no test needs the
user's disk.  The overlay must give byte for byte the image the boot model
boots (tools/s3probe/adf.AdfImage.patched, aros_boot_model.py --startup),
so the C64's Workbench screen can be compared with the model's.
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "s3probe"))

from host import adf_overlay as O  # noqa: E402
import adf as S  # noqa: E402  (the boot model's ADF reader)

SS = b"C:SetPatch QUIET\n" + b"C:Version >NIL:\n" * 60 + b"C:LoadWB\nEndCLI >NIL:\n"


def image(ffs: bool, ss: bytes = SS, extra: dict | None = None) -> bytes:
    files = {"S/Startup-Sequence": ss, "C/LoadWB": b"\0\0\x03\xf3" + bytes(300),
             "Devs/system-configuration": bytes(232)}
    files.update(extra or {})
    return S.build_adf(files, ffs=ffs, volume="Synth", bootcode=b"\x4e\x75")


@pytest.mark.parametrize("ffs", [False, True])
@pytest.mark.parametrize("script", ["trim", "min"])
def test_overlay_equals_the_models_patched_image(ffs, script):
    img = image(ffs)
    ov = O.make_overlay(img, script)
    want = S.AdfImage.from_bytes(img).patched("S/Startup-Sequence", O.SCRIPTS[script]).data
    assert O.apply(img, ov) == want
    assert len(ov.blocks) <= 1 + len(ov.data_blocks)
    for k, b in ov.blocks:
        assert len(b) == O.BLOCK and img[k * O.BLOCK:(k + 1) * O.BLOCK] != b


@pytest.mark.parametrize("ffs", [False, True])
def test_patched_file_reads_back(ffs):
    img = O.apply(image(ffs), O.make_overlay(image(ffs), "trim"))
    a = O.Adf(img)
    hk = a.lookup("s/startup-sequence")
    assert a.read_file(hk) == O.SCRIPTS["trim"]
    assert a.read_file(a.lookup("Devs/system-configuration")) == bytes(232)


def test_full_is_no_overlay():
    img = image(True)
    ov = O.make_overlay(img, "full")
    assert ov.blocks == [] and O.apply(img, ov) == img


def test_info_names_header_and_data_blocks():
    img = image(True)
    ov = O.make_overlay(img, "trim")
    a = O.Adf(img)
    hk = a.lookup("S/Startup-Sequence")
    assert ov.header == hk
    assert ov.data_blocks == a.file_blocks(hk)
    assert ov.first_data == ov.data_blocks[0]
    # the model's patched() rewrites every data block (unused ones zeroed)
    assert {k for k, _ in ov.blocks} == {hk, *ov.data_blocks}


@pytest.mark.parametrize("ffs", [False, True])
def test_bad_header_checksum_is_refused(ffs):
    img = image(ffs)
    ov = O.make_overlay(img, "trim")
    bad = [(k, bytearray(b)) for k, b in ov.blocks]
    k, b = next((k, b) for k, b in bad if k == ov.header)
    b[O.BLOCK - 188] ^= 1                  # size changed, checksum not
    ov2 = O.Overlay(ov.script, [(k, bytes(b)) for k, b in bad], ov.header, ov.data_blocks)
    with pytest.raises(O.AdfError, match="checksum"):
        O.check(img, ov2)
    with pytest.raises(O.AdfError, match="checksum"):
        O.apply(img, ov2)


def test_bad_ofs_data_checksum_is_refused():
    img = image(False)
    ov = O.make_overlay(img, "trim")
    blocks = [(k, bytearray(b)) for k, b in ov.blocks]
    k, b = next((k, b) for k, b in blocks if k == ov.first_data)
    b[40] ^= 0x20
    with pytest.raises(O.AdfError, match="checksum"):
        O.check(img, O.Overlay(ov.script, [(k, bytes(b)) for k, b in blocks],
                               ov.header, ov.data_blocks))


def test_overlay_block_range_and_size_refused():
    img = image(True)
    ov = O.make_overlay(img, "trim")
    for blocks in ([(O.BLOCKS, ov.blocks[0][1])], [(ov.header, b"\0" * 100)],
                   [(0, ov.blocks[0][1])]):
        with pytest.raises(O.AdfError):
            O.check(img, O.Overlay(ov.script, blocks, ov.header, ov.data_blocks))


def test_too_long_script_refused():
    img = image(True, ss=b"LoadWB\n")             # one data block
    with pytest.raises(O.AdfError, match="fit"):
        O.make_overlay(img, "trim", content=b"x" * 600)


def test_bad_image_refused():
    with pytest.raises(O.AdfError):
        O.make_overlay(bytes(100), "trim")
    img = bytearray(image(True))
    img[O.ROOT * O.BLOCK + 30] ^= 1                # root block checksum broken
    with pytest.raises(O.AdfError, match="root"):
        O.make_overlay(bytes(img), "trim")
    with pytest.raises(O.AdfError, match="Startup-Sequence"):
        O.make_overlay(S.build_adf({"C/LoadWB": b"x"}, ffs=True), "trim")


def test_scripts_are_wb_boot_4_3():
    assert O.SCRIPTS["trim"] == (b"C:MakeDir RAM:T RAM:ENV\nC:Assign >NIL: ENV: RAM:ENV\n"
                                 b"C:Assign >NIL: T: RAM:T\nC:LoadWB\nEndCLI >NIL:\n")
    assert O.SCRIPTS["min"] == b"C:LoadWB\nEndCLI >NIL:\n"
    assert O.SCRIPTS["full"] is None


def test_block_checksum_matches_public_rule():
    b = bytearray(O.BLOCK)
    struct.pack_into(">I", b, 0, 2)
    struct.pack_into(">I", b, 20, O.block_checksum(b))
    assert sum(struct.unpack(">128I", b)) & 0xFFFFFFFF == 0


def test_reu_writes_cover_overlay_at_base():
    img = image(True)
    ov = O.make_overlay(img, "trim")
    ws = O.reu_writes(ov, 0x200000)
    assert ws == [(0x200000 + k * O.BLOCK, b) for k, b in ov.blocks]


def big_file_image(n: int = 100, ffs: bool = True) -> tuple[bytes, bytes]:
    """A synthetic image with a file "Big" of n > 72 data blocks: its header
    holds 72, an extension block (T_LIST) the rest (rev-wb L3)."""
    img = bytearray(image(ffs))
    content = bytes((i * 31 + (i >> 9)) & 0xFF for i in range(n * O.BLOCK))
    hk, ek, d0 = 1500, 1501, 1502
    data = list(range(d0, d0 + n))
    per = O.BLOCK if ffs else O.BLOCK - 24
    content = content[:per * n]

    def blk(k):
        return memoryview(img)[k * O.BLOCK:(k + 1) * O.BLOCK]
    h = blk(hk)
    struct.pack_into(">IIII", h, 0, O.T_HEADER, hk, O.HT_SIZE, data[0])
    for i in range(O.HT_SIZE):
        struct.pack_into(">I", h, 24 + 4 * (O.HT_SIZE - 1 - i), data[i])
    struct.pack_into(">I", h, O.BLOCK - 188, len(content))
    h[O.BLOCK - 80] = 3
    h[O.BLOCK - 79:O.BLOCK - 76] = b"Big"
    struct.pack_into(">I", h, O.BLOCK - 12, O.ROOT)
    struct.pack_into(">I", h, O.BLOCK - 8, ek)
    struct.pack_into(">i", h, O.BLOCK - 4, O.ST_FILE)
    e = blk(ek)
    struct.pack_into(">III", e, 0, O.T_LIST, ek, n - O.HT_SIZE)
    for i in range(n - O.HT_SIZE):
        struct.pack_into(">I", e, 24 + 4 * (O.HT_SIZE - 1 - i), data[O.HT_SIZE + i])
    struct.pack_into(">I", e, O.BLOCK - 12, hk)
    struct.pack_into(">i", e, O.BLOCK - 4, O.ST_FILE)
    for i, k in enumerate(data):
        d = blk(k)
        chunk = content[i * per:(i + 1) * per]
        if ffs:
            d[:] = chunk
        else:
            struct.pack_into(">IIIII", d, 0, O.T_DATA, hk, i + 1, len(chunk),
                             data[i + 1] if i + 1 < n else 0)
            d[24:24 + len(chunk)] = chunk
            struct.pack_into(">I", d, 20, O.block_checksum(d))
    a = O.Adf(bytes(img))
    slot = 24 + 4 * a.name_hash("Big")
    r = blk(O.ROOT)
    struct.pack_into(">I", h, O.BLOCK - 16, struct.unpack_from(">I", r, slot)[0])
    struct.pack_into(">I", r, slot, hk)
    for b in (h, e, r):
        struct.pack_into(">I", b, 20, O.block_checksum(b))
    return bytes(img), content


@pytest.mark.parametrize("ffs", [False, True])
def test_file_blocks_follow_extension_blocks(ffs):
    img, content = big_file_image(100, ffs)
    a = O.Adf(img)
    hk = a.lookup("Big")
    assert a.file_blocks(hk) == list(range(1502, 1602))
    assert a.read_file(hk) == content
    # the overlay tool still parses the disk (and its Startup-Sequence)
    assert O.apply(img, O.make_overlay(img, "trim"))[1500 * 512:1602 * 512] == img[1500 * 512:1602 * 512]


def test_bad_extension_block_refused():
    img, _ = big_file_image(100, True)
    img = bytearray(img)
    img[1501 * 512 + 40] ^= 1                    # extension checksum broken
    a = O.Adf(bytes(img))
    with pytest.raises(O.AdfError, match="extension"):
        a.file_blocks(a.lookup("Big"))


@pytest.mark.parametrize("dostype", [4, 5, 6, 7])
def test_dircache_and_later_dostypes_refused(dostype):
    img = bytearray(image(True))
    img[3] = dostype
    with pytest.raises(O.AdfError, match="dostype"):
        O.make_overlay(bytes(img), "trim")


@pytest.mark.parametrize("dostype", [0, 1, 2, 3])
def test_ofs_ffs_intl_accepted(dostype):
    img = bytearray(image(dostype & 1 == 1))
    img[3] = dostype
    O.make_overlay(bytes(img), "trim")
