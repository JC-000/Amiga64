"""Host-only WS-B2 tests: LVO table, guest layout mirrors, RAMDISK format.

No VICE involved.  tests/run_dos.py covers the C64 side end to end.
"""

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from host import hle_dos_env as de  # noqa: E402
from host import hle_env  # noqa: E402
from host.rescheck import load_spaces  # noqa: E402


def _guest_equates():
    spaces, errors = load_spaces(ROOT)
    assert not errors
    return {e.name: e.value for e in spaces["guest"].equates}


# --- C3: index range, real LVOs, no double claims -------------------------------

def test_dos_file_funcs_use_ws_b2_index_range():
    idx = [i for _, i in de.DOS_FILE_FUNCS.values()]
    assert all(0x30 <= i <= 0x4F for i in idx)
    assert len(set(idx)) == len(idx)
    for i in (de.HLE_DOS_IDX_READ, de.HLE_DOS_IDX_WRITE):
        assert 0x30 <= i <= 0x4F and i not in idx


def test_real_dos_library_lvos():
    # dos_lib.fd (bias 30, 6 bytes per entry): Open, Close, Read, Write,
    # Input, Output, Seek, ... IoErr is the 18th function.
    assert de.DOS_FILE_FUNCS["Open"][0] == -30
    assert de.DOS_FILE_FUNCS["Close"][0] == -36
    assert de.LVO_READ == -42 and de.LVO_WRITE == -48
    assert de.DOS_FILE_FUNCS["Seek"][0] == -66
    assert de.DOS_FILE_FUNCS["IoErr"][0] == -132


def test_merged_into_dosbase_without_lvo_collision():
    lvos = [lvo for lvo, _ in hle_env.DOS_FUNCS.values()]
    assert len(set(lvos)) == len(lvos), "an LVO is claimed twice"
    for name, val in de.DOS_FILE_FUNCS.items():
        assert hle_env.DOS_FUNCS[name] == val


def test_slots_staged_by_env():
    mem = {}
    for c in hle_env.env_chunks():
        for i, b in enumerate(c.data):
            mem[c.guest_offset + i] = b
    for lvo, idx in de.DOS_FILE_FUNCS.values():
        a = hle_env.DOS_BASE + lvo
        assert (mem[a] << 8 | mem[a + 1]) == 0xA100 | idx


# --- C2/C4: FileHandle pool + RAMDISK region mirror rc/guest.inc -----------------

def test_guest_layout_mirrors_rc():
    eq = _guest_equates()
    assert eq["FH_POOL"] == de.FH_POOL
    assert eq["dosio_rd_base"] == de.RD_BASE
    assert eq["dosio_rd_end"] == de.RD_END
    assert de.fh_addr(de.FH_SLOTS) - 1 <= eq["FH_POOL_END"] <= 0xFFF
    assert de.FH_POOL >= 0xC00


def test_filehandles_longword_aligned_bptrs():
    for n in range(de.FH_SLOTS):
        assert de.fh_addr(n) % 4 == 0
        assert de.fh_bptr(n) << 2 == de.fh_addr(n)
    assert de.FH_SIZE == 44 and de.FH_ARGS_OFF == 36


def test_asm_mirrors_hle_dos_constants():
    eq = de.asm_equates(ROOT / "runtime/ops/hle_dos.s")
    assert eq["FH_SIZE"] == de.FH_SIZE
    assert eq["FH_SLOTS"] == de.FH_SLOTS
    assert eq["FH_ARGS_OFF"] == de.FH_ARGS_OFF
    assert eq["FH_KIND_FILE0"] == de.FH_KIND_FILE0
    assert eq["HLE_DOS_IDX_READ"] == de.HLE_DOS_IDX_READ
    assert eq["HLE_DOS_IDX_WRITE"] == de.HLE_DOS_IDX_WRITE
    for name, (_, idx) in de.DOS_FILE_FUNCS.items():
        assert eq[f"HLE_DOS_IDX_{name.upper()}"] == idx
    for name in ("MODE_OLDFILE", "MODE_NEWFILE", "MODE_READWRITE",
                 "ERROR_NO_FREE_STORE", "ERROR_OBJECT_NOT_FOUND",
                 "ERROR_OBJECT_IN_USE",
                 "ERROR_ACTION_NOT_KNOWN", "ERROR_INVALID_COMPONENT_NAME",
                 "ERROR_INVALID_LOCK", "ERROR_SEEK_ERROR", "ERROR_DISK_FULL"):
        assert eq[name] == getattr(de, name), name


def test_asm_mirrors_ramdisk_format():
    eq = de.asm_equates(ROOT / "runtime/ops/dosio_ram.s")
    assert eq["RD_HDR_SIZE"] == de.RD_HDR_SIZE
    assert eq["RD_ENTRIES"] == de.RD_ENTRIES
    assert eq["RD_ENT_SIZE"] == de.RD_ENT_SIZE
    assert eq["RD_DATA_OFF"] == de.RD_DATA - de.RD_BASE
    assert eq["RD_NAME_OFF"] == de.RD_NAME_OFF
    assert eq["RD_NAME_MAX"] == de.RD_NAME_MAX
    assert eq["RD_NEW_CAP"] == de.RD_NEW_CAP
    assert eq["RD_VERSION"] == de.RD_VERSION
    assert eq["ERROR_NOT_A_DOS_DISK"] == de.ERROR_NOT_A_DOS_DISK


# --- RAMDISK format ---------------------------------------------------------------

def test_ramdisk_roundtrip():
    files = {"in.bin": bytes(range(256)) * 3, "b": b"xy", "Empty": b""}
    img = de.ramdisk_image(files)
    assert img[:4] == b"RDSK"
    ents = de.parse_ramdisk_bytes(img)
    assert {e.name for e in ents.values()} == set(files)
    for name, data in files.items():
        e = ents[name.lower()]
        assert e.data == data and e.size == len(data)
        assert e.addr % 256 == 0 and e.addr >= de.RD_DATA
        assert e.capacity >= max(len(data), de.RD_NEW_CAP)


def test_ramdisk_header_free_ptr_follows_last_extent():
    img = de.ramdisk_image({"a": b"1", "b": b"22"})
    _, _, n, _, free_ptr, data_end = struct.unpack_from("<4sBBHII", img)
    assert n == de.RD_ENTRIES and data_end == de.RD_END + 1
    assert free_ptr == de.RD_DATA + 2 * de.RD_NEW_CAP


def test_ramdisk_rejects_bad_names_and_duplicates():
    with pytest.raises(ValueError):
        de.ramdisk_image({"x" * 32: b""})
    with pytest.raises(ValueError):
        de.ramdisk_image({"": b""})
    with pytest.raises(ValueError):
        de.ramdisk_image([de.RdFile("A", b""), de.RdFile("a", b"")])


def test_ramdisk_chunks_stage_full_directory_page():
    ch = de.ramdisk_chunks({"f": b"hello"})
    assert ch[0].guest_offset == de.RD_BASE
    assert len(ch[0].data) == de.RD_DATA - de.RD_BASE
    assert ch[1].data == b"hello"


def test_parse_directory_rejects_unformatted():
    with pytest.raises(ValueError):
        de.parse_ramdisk_bytes(bytes(64))


# --- review fix round 1 -------------------------------------------------------

def test_ramdisk_region_is_2mb_at_e00000():
    # ruling 6: 32 entries x 64 KB extents; the directory page comes out of
    # the last extent (documented in docs/HLE_DOS.md).
    assert de.RD_BASE == 0xE00000 and de.RD_END == 0xFFFFFF
    room = de.RD_END + 1 - de.RD_DATA
    assert room >= (de.RD_ENTRIES - 1) * de.RD_NEW_CAP + 1
    assert room == de.RD_ENTRIES * de.RD_NEW_CAP - (de.RD_DATA - de.RD_BASE)


def test_parse_directory_rejects_garbage_entries():
    img = bytearray(de.ramdisk_image({"f": b"abc"}))
    off = de.RD_DIR - de.RD_BASE
    img[off + 4:off + 8] = (0x7FFFFFFF).to_bytes(4, "little")  # size > cap
    with pytest.raises(ValueError):
        de.parse_ramdisk_bytes(bytes(img))


def test_uci_opt_in_key_mirrors_asm():
    src = (ROOT / "runtime/ops/hle_dos.s").read_text()
    assert 'dosio_want_uci' in src
    key = de.UCI_OPT_IN_KEY.decode()
    assert f'UCI_OPT_IN_KEY = "{key}"' in src or all(
        f"'{c}'" in src for c in key)
