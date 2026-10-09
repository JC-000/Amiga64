"""host/snapfmt.py against runtime/ops/snap.s and the rc files (W12-T, I3).

The snapshot format has one version number and one layout, written by
snap.s and read by the host (tests/aros_resume.py) through snapfmt.py:
this pins every shared constant to the assembly source, and the build-ID
stamp's properties (deterministic, idempotent, refuses a PRG without
snap_bid).
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from host import machine
from host import snapfmt as F
from host.hle_dos_env import asm_equates

ROOT = Path(__file__).resolve().parents[2]
SNAP = ROOT / "runtime/ops/snap.s"


def rc(name: str) -> dict[str, int]:
    return asm_equates(ROOT / "runtime/rc" / name)


def test_version_and_header_offsets_match_snap_s():
    eq = asm_equates(SNAP)
    assert eq["SNAP_VER"] == F.VERSION
    assert eq["SNAP_HSUM"] == F.HDR_SUM
    assert (eq["H_NSP"], eq["H_FLAGS"], eq["H_BID"], eq["H_RID"], eq["H_SSUM"]) == \
        (F.H_NSP, F.H_FLAGS, F.H_BID, F.H_RID, F.H_SSUM)
    assert eq["SNAP_NSP"] == len(F.spans(0x1000))


def test_request_and_status_codes_match_snap_s():
    eq = asm_equates(SNAP)
    assert (eq["SNAP_RQ_SUS"], eq["SNAP_RQ_FILE"], eq["SNAP_RQ_CONT"]) == \
        (F.RQ_SUS, F.RQ_FILE, F.RQ_CONT)
    assert (eq["SNAP_S_SUS"], eq["SNAP_S_SUSF"], eq["SNAP_S_RES"]) == \
        (F.ST_SUS, F.ST_SUSF, F.ST_RES)
    codes = {v for k, v in eq.items() if k.startswith("SNAP_E_")}
    assert codes == set(F.ST_ERR)


def test_declared_rom_id_lies_in_the_region_and_in_no_file_span():
    rid = F.SNAP_BASE + F.OFF_RID
    assert rid + 32 - 1 <= F.SNAP_END
    for a, n in F.spans(F.KEEP_MAX - 0x1000):
        assert rid + 32 <= a or rid >= a + n
    assert F.OFF_RID == 0x2FE0           # snap.s SNAP_RID = snap_base + $2FE0
    assert "SNAP_RID  = snap_base + $2FE0" in SNAP.read_text()


def test_scratch_save_is_carved_out():
    """F1: snap_res's 256-byte save of the scratch page lies between the BSS
    image and the declared ROM id, in no file span (snap.s also link-asserts
    it against the real image end)."""
    sav = F.SNAP_BASE + F.OFF_SAV
    assert "SNAP_SAV  = snap_base + $2E00" in SNAP.read_text() and F.OFF_SAV == 0x2E00
    assert sav + 0x100 <= F.SNAP_BASE + F.OFF_RID
    lbl = ROOT / "build" / "w12t" / "runner.lbl"
    keep = 0x2000
    if lbl.exists():
        lab = F.labels(str(lbl))
        keep = lab["snap_keep"] - lab["__BSS_RUN__"]
    for a, n in F.spans(keep):
        assert sav + 0x100 <= a or sav >= a + n, (hex(a), n)


def test_snap_region_in_both_profiles_and_inside_the_io_bank():
    for name in ("guest.inc", "machine.inc"):
        e = rc(name)
        assert (e["snap_base"], e["snap_end"]) == (F.SNAP_BASE, F.SNAP_END), name
    m = rc("machine.inc")
    assert m["disp_lut"] + 0x1000 <= F.SNAP_BASE         # after the LUT
    assert 0xDF0000 <= F.SNAP_BASE and F.SNAP_END <= 0xDFFFFF
    # never a guest range (chip/slow RAM, ROM banks)
    for a, n in F.RANGES:
        assert a + n <= F.SNAP_BASE or a > F.SNAP_END


def test_ranges_are_the_machine_profile():
    assert F.RANGES == (
        (machine.AROS_CHIP, machine.AROS_CHIP_END - machine.AROS_CHIP + 1),
        (machine.AROS_SLOW, 0xDF0000 - machine.AROS_SLOW),   # + $DC0000-$DEFFFF
        (machine.AROS_EXT, machine.AROS_EXT_END - machine.AROS_EXT + 1),
        (machine.AROS_ROM, machine.AROS_ROM_END - machine.AROS_ROM + 1))
    src = SNAP.read_text()
    for sym in ("AROS_CHIP", "AROS_EXT", "AROS_ROM"):
        assert f".faraddr {sym}\n" in src, sym
    assert ".dword disp_lut - AROS_SLOW" in src


def test_fletcher_is_machines():
    data = bytes(range(256)) * 3 + b"\x01\x02"
    assert F.fletcher(data) == machine.fletcher(data)


def test_header_round_trip_and_template_prefix():
    bid, rid = bytes(range(1, 9)), hashlib.sha256(b"rom").digest()
    h = F.build_header(bid, rid, 0x10F1, 0x8E00, 0x12345678, F.RQ_FILE)
    p = F.parse_header(h)
    assert p["sum_ok"] and p["magic"] == b"A64S" and p["version"] == F.VERSION
    assert p["bid"] == bid and p["rid"] == rid and p["flags"] == F.RQ_FILE
    assert p["keep"] == 0x10F1 and p["bss_run"] == 0x8E00
    assert p["spans"][1] == (F.SNAP_BASE + 0x100, 0x200 + 0x1100)
    assert p["spans"][2:] == list(F.RANGES)
    assert all(b == 0 for b in h[100:F.HDR_SUM])
    bad = bytearray(h)
    bad[F.H_FLAGS] ^= 1
    assert not F.parse_header(bytes(bad))["sum_ok"]


BSS_OK = ("al 008E00 .__BSS_RUN__\nal 0010FF .__BSS_SIZE__\nal 009EF1 .snap_keep\n"
          "al 009EF3 .ureu_ra\nal 009EFF .ureu_bend\n")


def _prg(tmp_path, body: bytes, bid_off: int, bss: str = BSS_OK) -> tuple[str, str]:
    prg, lbl = tmp_path / "r.prg", tmp_path / "r.lbl"
    prg.write_bytes(b"\x01\x08" + body)
    lbl.write_text(f"al 000001 .SNAP_VER\nal {0x0801 + bid_off:06X} .snap_bid\n" + bss)
    return str(prg), str(lbl)


def test_stamp_refuses_when_snap_ureu_bss_is_not_last(tmp_path):
    prg, lbl = _prg(tmp_path, bytes(64), 8, BSS_OK.replace("009EFF .ureu_bend", "009E00 .ureu_bend"))
    with pytest.raises(ValueError, match="not last"):
        F.stamp(prg, lbl)


def test_built_runner_has_snap_ureu_bss_last():
    lbl = ROOT / "build" / "w12t" / "runner.lbl"
    if not lbl.exists():
        pytest.skip("no build/w12t")
    F.check_bss_last(F.labels(str(lbl)), str(lbl))


def test_stamp_deterministic_idempotent(tmp_path):
    body = bytearray(b"\xEA" * 600)
    prg, lbl = _prg(tmp_path, bytes(body), 300)
    a = F.stamp(prg, lbl)
    raw1 = open(prg, "rb").read()
    b = F.stamp(prg, lbl)
    assert a == b and open(prg, "rb").read() == raw1      # idempotent
    assert raw1[2 + 300:2 + 308] == a and a != bytes(8)
    z = bytearray(raw1)
    z[302:310] = bytes(8)
    assert hashlib.sha256(bytes(z)).digest()[:8] == a      # the definition
    body[10] ^= 1                                          # another build
    prg2, lbl2 = _prg(tmp_path, bytes(body), 300)
    assert F.stamp(prg2, lbl2) != a


def test_stamp_refuses_without_label(tmp_path):
    prg, lbl = tmp_path / "r.prg", tmp_path / "r.lbl"
    prg.write_bytes(b"\x01\x08" + bytes(16))
    lbl.write_text("al 000801 .main\n")
    with pytest.raises(KeyError):
        F.stamp(str(prg), str(lbl))


def test_stamp_as_id_for_test_builds(tmp_path):
    prg, lbl = _prg(tmp_path, bytes(64), 8)
    F.stamp(prg, lbl, as_id=b"ABCDEFGH")
    assert F.read_bid(prg, lbl) == b"ABCDEFGH"


def test_makefile_stamps_the_runner():
    mk = (ROOT / "Makefile").read_text()
    assert "host.snapfmt --stamp $@" in mk
    # snap.s and ureu.s link last (their BSS after every other module's)
    line = next(x for x in mk.splitlines() if x.startswith("RUNTIME_SRC"))
    # (W13-I: inp.s may link between mach.s and snap.s; snap/ureu stay last)
    assert line.rstrip().endswith(" runtime/ops/snap.s runtime/ops/ureu.s")
    assert line.rindex("runtime/ops/mach.s") < line.rindex("runtime/ops/snap.s")
