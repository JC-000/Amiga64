"""S3.7 (W11-A): the machine-mode guest profile and the host ROM loader.

runtime/rc/machine.inc is a second guest map checked on its own by
host/rescheck.py (check g): overlap/coverage/equates, the trapped I/O banks
reserved in EVERY guest map (guest.inc included), the profile's host
mirrors (host/machine.py), and the boot driver's literals judged against
the profile.  host/machine.py finds the pinned ROM cache fail-closed and
plans the REU staging.  No ROM bytes anywhere: the loader tests use a fake
cache of synthetic bytes; the one test that opens the real cache skips
without it (as tests/aros_oracle.py does).
"""

from __future__ import annotations

import hashlib
import shutil
import struct
from pathlib import Path

import pytest

from host import machine, rescheck

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "runtime" / "rc" / "machine.inc"


def _prof(text: str, path: str = "machine.inc") -> list[str]:
    space, errs = rescheck.parse_rc_text(text, path)
    assert space is not None
    return errs + rescheck.check_space(space) + rescheck.check_io_banks(space)


def test_repo_rescheck_green():
    assert rescheck.run_checks(ROOT) == 0


def test_profile_loaded_and_clean():
    profiles, errs = rescheck.load_profiles(ROOT)
    assert not errs and set(profiles) == {"machine"}
    p = profiles["machine"]
    assert p.name == "guest"
    assert not rescheck.check_space(p)
    assert not rescheck.check_io_banks(p)
    assert not rescheck.check_profile_mirrors(ROOT, "machine", p)
    # the map the boot model runs on (S3_PLAN §3.3)
    own = {(r.lo, r.hi): r.owner for r in p.regions}
    assert own[(0x000000, 0x1FFFFF)] == "aros"          # chip RAM, 2 MB
    assert own[(0xC00000, 0xDBFFFF)] == "aros"          # slow RAM
    assert own[(0xE00000, 0xE7FFFF)] == "aros"          # extension ROM
    assert own[(0xF80000, 0xFFFFFF)] == "aros"          # Kickstart ROM
    assert own[(0xBF0000, 0xBFFFFF)] == "io"
    assert own[(0xDF1000, 0xDF3FFF)] == "snap"         # W12-T: suspend snapshot (Q3)
    assert own[(0xDF4100, 0xDFFFFF)] == "io"           # W12-T: was $DF1000-; W13-D: $DF4000-
    assert own[(0xDF4000, 0xDF40FF)] == "disp"         # W13-D: the zero row
    assert own[(0xDF0000, 0xDF0FFF)] == "disp"


def test_hle_profile_reserves_io_banks():
    spaces, errs = rescheck.load_spaces(ROOT)
    assert not errs
    assert not rescheck.check_io_banks(spaces["guest"])
    g = spaces["guest"]
    assert g.region_at(0xBF8000).owner == "io"
    assert g.region_at(0xDFF180).owner == "io"
    assert g.region_at(0xBEFFFF).owner == "free"
    assert g.region_at(0xC00000).owner == "free"


def test_teeth_io_bank_left_free():
    t = PROFILE.read_text().replace(
        "; @region io $BF0000-$BFFFFF", "; @region free $BF0000-$BFFFFF")
    errs = _prof(t)
    assert any("trapped I/O bank $BF0000" in e for e in errs)
    assert any("no 'io' region reserves the I/O bank $BF0000" in e for e in errs)


def test_teeth_guest_inc_old_free_region():
    """The pre-W11 HLE map (free $800000-$DEFFFF) is now refused."""
    g = (ROOT / "runtime" / "rc" / "guest.inc").read_text()
    old = g.replace(
        "; @region free $800000-$BEFFFF", "; @region free $800000-$DEFFFF").replace(
        "; @region io $BF0000-$BFFFFF", ";").replace(
        "; @region free $C00000-$DEFFFF", ";")
    space, errs = rescheck.parse_rc_text(old, "guest.inc")
    assert not errs and not rescheck.check_space(space)
    assert any("$BF0000" in e for e in rescheck.check_io_banks(space))


def test_teeth_region_straddles_bank():
    t = PROFILE.read_text().replace(
        "; @region free $2DC000-$BEFFFF", "; @region free $2DC000-$BF0FFF").replace(   # W13-W: was $200000-
        "; @region io $BF0000-$BFFFFF", "; @region io $BF1000-$BFFFFF")
    assert any("straddles the I/O bank" in e for e in _prof(t))


def test_teeth_profile_overlap_and_gap():
    t = PROFILE.read_text().replace(
        "; @region free $E80000-$F7FFFF", "; @region free $E80000-$F8FFFF")
    assert any("overlaps" in e for e in _prof(t))
    t = PROFILE.read_text().replace(
        "; @region free $E80000-$F7FFFF", "; @region free $E80000-$F6FFFF")
    assert any("undeclared gap" in e for e in _prof(t))


def test_teeth_profile_wrong_space(tmp_path):
    root = _tree(tmp_path)
    p = root / "runtime" / "rc" / "machine.inc"
    p.write_text(p.read_text().replace("; @space guest", "; @space ram"))
    _, errs = rescheck.load_profiles(root)
    assert any("expected 'guest'" in e for e in errs)


def _tree(tmp_path: Path) -> Path:
    """A copy of what rescheck reads (rc files, scanned sources, mirrors)."""
    root = tmp_path / "repo"
    for rel in ["runtime", "tests/runner", "tests/aros", "host/hle_env.py",
                "host/image.py", "host/machine.py"]:
        src = ROOT / rel
        dst = root / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.o"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
    return root


def test_teeth_mirror_mismatch(tmp_path):
    root = _tree(tmp_path)
    prof, _ = rescheck.load_profiles(root)
    assert not rescheck.check_profile_mirrors(root, "machine", prof["machine"])
    m = root / "host" / "machine.py"
    m.write_text(m.read_text().replace("AROS_ROM = 0xF80000", "AROS_ROM = 0xF00000"))
    errs = rescheck.check_profile_mirrors(root, "machine", prof["machine"])
    assert any("AROS_ROM" in e and "mismatch" in e for e in errs)


def _scan_driver(extra: str) -> list:
    spaces, _ = rescheck.load_spaces(ROOT)
    prof, _ = rescheck.load_profiles(ROOT)
    view = {**spaces, "guest": prof["machine"]}
    text = (ROOT / "tests" / "aros" / "mboot.s").read_text() + extra
    return [f for f in rescheck.scan_source("tests/aros/mboot.s", text, "aros", view)
            if f.fatal]


def test_teeth_driver_literal_judged_by_profile():
    """tests/aros/*.s literals are classified against machine.inc (the view
    run_checks builds for PROFILE_SOURCES): a raw $F80000 (an aros region)
    is fine for the aros owner, a raw $DFF180 (io) is a violation; against
    guest.inc $F80000 would be the HLE RAMDISK."""
    assert _scan_driver("") == []
    assert _scan_driver("        .dword $F80000\n") == []
    assert _scan_driver("        lda $DFF180\n")
    assert [p for g, p in rescheck.PROFILE_SOURCES
            if Path("tests/aros/mboot.s").match(g)] == ["machine"]


def test_driver_registered():
    owners, unreg = rescheck.source_owners(ROOT)
    assert owners["tests/aros/mboot.s"] == "aros" and not unreg


# --- host/machine.py ---------------------------------------------------------

def test_fletcher_reference_values():
    assert machine.fletcher(b"") == 0
    assert machine.fletcher(b"\x01\x02") == (4 << 16) | 3
    assert machine.fletcher(bytes(256)) == 0
    a = machine.fletcher(b"\x10\x20\x30")
    b = machine.fletcher(b"\x20\x10\x30")       # a swap changes b, not a
    assert a & 0xFFFF == b & 0xFFFF and a != b
    big = bytes(range(256)) * 512                # wraps both sums
    s = machine.fletcher(big)
    assert s == machine.fletcher(big[:65536] + big[65536:])


def _fake_cache(tmp_path: Path, tamper: str | None = None) -> tuple[str, str]:
    ext = bytes((i * 7) & 0xFF for i in range(machine.BANK_SIZE))
    rom = struct.pack(">II", 0x5A5AC3C3, 0x00F800D2) + bytes(machine.BANK_SIZE - 8)
    arch = "ab" * 32
    d = tmp_path / "cache" / arch
    d.mkdir(parents=True)
    (d / "aros-ext.bin").write_bytes(ext)
    (d / "aros-rom.bin").write_bytes(rom)
    sha = {n: hashlib.sha256(b).hexdigest() for n, b in
           (("aros-ext.bin", ext), ("aros-rom.bin", rom))}
    if tamper:
        b = (d / tamper).read_bytes()
        (d / tamper).write_bytes(bytes([b[0] ^ 0xFF]) + b[1:])
    man = tmp_path / "manifest.txt"
    man.write_text(f"archive|https://example.invalid/x.zip|{arch}\n" +
                   "".join(f"file|{n}|x!{n}|{h}\n" for n, h in sha.items()))
    return str(man), str(tmp_path / "cache")


def test_find_rom_and_plan(tmp_path):
    man, root = _fake_cache(tmp_path)
    rom, why = machine.find_rom(man, root)
    assert rom is not None and why == ""
    assert rom.ssp == 0x5A5AC3C3 and rom.pc == 0x00F800D2
    plan = machine.stage_plan(rom)
    assert [a for a, _ in plan] == [machine.AROS_EXT, machine.AROS_ROM, 0]
    assert plan[2][1] == rom.rom[:8]                 # the overlay longs
    assert len(plan[0][1]) == len(plan[1][1]) == machine.BANK_SIZE
    assert machine.ZERO_SPANS == ((0, 1 << 24),)     # the whole REU first


@pytest.mark.parametrize("tamper", ["aros-ext.bin", "aros-rom.bin"])
def test_find_rom_fails_closed(tmp_path, tamper):
    man, root = _fake_cache(tmp_path, tamper)
    rom, why = machine.find_rom(man, root)
    assert rom is None and "SHA256 mismatch" in why and tamper in why


def test_find_rom_absent(tmp_path):
    man, _ = _fake_cache(tmp_path)
    rom, why = machine.find_rom(man, str(tmp_path / "nowhere"))
    assert rom is None and "fetch.sh" in why
    rom, why = machine.find_rom(str(tmp_path / "no-manifest"), str(tmp_path))
    assert rom is None and "manifest" in why


def test_real_cache_if_present():
    rom, why = machine.find_rom()
    if rom is None:
        pytest.skip(f"no AROS ROM cache: {why}")
    # the reset PC points into the Kickstart bank (the boot entry jumps there)
    assert machine.AROS_ROM <= rom.pc & 0xFFFFFF <= machine.AROS_ROM_END
