"""W16-P: the profile block and the app anchor (host/profile_block.py)."""

import re
import struct

import pytest

from host import paula_ref as PR
from host import profile_block as PB


def test_table_matches_the_m0_note():
    # s = 0 is the W15 player profile (R3), s = 3 the Workbench on the Ultimates
    assert (PB.r16(PR.NTSC_PHI2, 0), PB.tick_e(PR.NTSC_PHI2, 0)) == (18897, 20455)
    assert (PB.r16(PR.NTSC_PHI2, 3), PB.tick_e(PR.NTSC_PHI2, 3)) == (2362, 2557)
    assert (PB.r16(PR.NTSC_PHI2, 9), PB.tick_e(PR.NTSC_PHI2, 9)) == (37, 40)
    assert PB.r16(PR.NTSC_PHI2, 0) == PR.R16_U64_S0


def test_layout_by_hand_s3_u64e():
    b = PB.build(3, PB.HOST_U64E, PB.F_AUD | PB.F_LIVE | PB.F_RT, 48)
    assert len(b) == 64
    assert b[0:4] == b"A64P"
    assert b[4:6] == b"\x00\x01" and b[6:8] == b"\x00\x40"
    assert struct.unpack(">I", b[8:12])[0] == round(1022727 / 8 * 256) == 32727264
    assert struct.unpack(">I", b[12:16])[0] == 2557
    assert struct.unpack(">I", b[16:20])[0] == 14188
    assert struct.unpack(">I", b[20:24])[0] == 1022727
    assert list(b[24:28]) == [3, 1, 7, 0]
    assert struct.unpack(">HH", b[28:32]) == (2362, 48)
    assert b[32:60] == bytes(28)
    longs = struct.unpack(">16I", b)
    assert (sum(longs[:15]) + longs[15]) & 0xFFFFFFFF == 0xFFFFFFFF


def test_roundtrip():
    for s in (0, 3, 9):
        for host, fl in ((PB.HOST_VICE, PB.F_AUD), (PB.HOST_C64U, 7), (PB.HOST_U64E, 0)):
            p = PB.parse(PB.build(s, host, fl, 64))
            assert p is not None
            assert (p["s"], p["host"], p["flags"]) == (s, host, fl)
            assert p["tick_e"] == PB.tick_e(PR.NTSC_PHI2, s)
            assert p["r16"] == PB.r16(PR.NTSC_PHI2, s)


def test_pal_phi2():
    p = PB.parse(PB.build(0, PB.HOST_VICE, 0, 1, phi2_hz=PR.PAL_PHI2))
    assert p["phi2_hz"] == 985248 and p["tick_e"] == 19705


def test_zero_and_corruption_are_rejected():
    assert PB.parse(bytes(64)) is None              # a session without the block
    good = PB.build(3, PB.HOST_U64E, PB.F_AUD, 48)
    assert PB.parse(good[:63]) is None
    for i in range(64):                             # any single-byte change
        for x in (0x01, 0x80):
            bad = bytearray(good)
            bad[i] ^= x
            assert PB.parse(bytes(bad)) is None, f"byte {i} ^ ${x:02X} accepted"


def test_version_and_size_checked_even_with_a_good_check():
    good = bytearray(PB.build(3, PB.HOST_U64E, 0, 48))
    for off, val in ((4, b"\x00\x02"), (6, b"\x00\x80")):
        bad = bytearray(good)
        bad[off:off + 2] = val
        bad[60:64] = struct.pack(">I", PB.check(bytes(bad)))
        assert PB.parse(bytes(bad)) is None


def test_build_refuses_bad_args():
    with pytest.raises(ValueError):
        PB.build(17, PB.HOST_VICE, 0, 1)
    with pytest.raises(ValueError):
        PB.build(3, 3, 0, 1)
    with pytest.raises(ValueError):
        PB.build(3, PB.HOST_U64E, 0x08, 48)
    with pytest.raises(ValueError):
        PB.build(3, PB.HOST_U64E, PB.F_LIVE, 48)


def test_anchor():
    assert PB.parse_anchor(bytes(16)) is None       # not yet published
    a = PB.parse_anchor(b"MWBA" + struct.pack(">III", 0x012340, 7, 0))
    assert a == {"ptr": 0x012340, "seq": 7, "reserved": 0, "ptr_ok": True}
    slow = PB.parse_anchor(b"MWBA" + struct.pack(">III", 0xC10000, 0, 0))
    assert slow["ptr_ok"]
    for bad in (0, 0x012341, 0x200000, 0x2DC000, 0xBFE001, 0xDC0000, 0xF80000):
        assert not PB.parse_anchor(b"MWBA" + struct.pack(">III", bad, 0, 0))["ptr_ok"], hex(bad)
    assert PB.parse_anchor(b"MWB") is None
    # rev16-p M4: the struct must fit inside its span
    mv = lambda p, n: PB.parse_anchor(b"MWBA" + struct.pack(">III", p, 0, 0), size=n)["ptr_ok"]
    assert mv(0x1FFFB8, 72) and not mv(0x1FFFBA, 72)          # chip RAM ends at $1FFFFF
    assert mv(0xDBFFB8, 72) and not mv(0xDBFFBA, 72)          # slow RAM ends at $DBFFFF
    assert not mv(0x012340, 0)


def test_addresses_inside_the_rc_region():
    lo, hi = PB.PROF_REGION
    assert lo <= PB.PROF_ADDR and PB.PROF_ADDR + PB.PROF_SIZE <= PB.ANCHOR_ADDR
    assert PB.ANCHOR_ADDR + PB.ANCHOR_SIZE - 1 <= hi
    import os
    inc = open(os.path.join(os.path.dirname(__file__), "..", "..", "runtime", "rc",
                            "machine.inc")).read()
    m = re.search(r"@region prof \$([0-9A-F]{6})-\$([0-9A-F]{6})", inc)
    assert m is not None, "machine.inc: no prof region (rev16-p L3: a hard check)"
    assert (int(m.group(1), 16), int(m.group(2), 16)) == PB.PROF_REGION
