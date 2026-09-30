"""Unit tests for host/chipset_ref.MachTime (W8-L, S3.2 time base).

Expected values are worked out by hand from docs/WAVE8_BRIEF.md C2 and
docs/CONTRACTS.md "Machine loop (S3.2)": E = phi2 >> s, 14188 E ticks per
frame, 5 colour clocks per E tick, 227 colour clocks per line.
"""
import pytest

from host import chipset_ref as cr


def test_constants_are_pal():
    assert cr.MACH_FRAME_E * 50 == 709_400      # ~709379 Hz E clock / 50 Hz
    assert cr.MACH_FRAME_E * cr.MACH_CCK_PER_E == 70_940
    assert 312 * 227 == 70_824                  # line 312 starts here
    assert 70_940 - 70_824 == 116               # last line: h 0-115


@pytest.mark.parametrize("s,frame", [(0, 14188), (2, 56752), (4, 227008),
                                     (8, 3632128), (16, 929824768)])
def test_frame_phi2(s, frame):
    assert cr.MachTime(s).frame_phi2() == frame


def test_s_clamped():
    assert cr.MachTime(200).s == cr.MACH_S_MAX
    assert cr.MachTime().s == 4


@pytest.mark.parametrize("s,fps", [(0, 69.44), (2, 17.36), (4, 4.340)])
def test_fps_vice(s, fps):
    # the brief's 69.4 / 2^s at VICE's 985248 Hz
    assert cr.MachTime(s).fps() == pytest.approx(fps, rel=1e-3)


@pytest.mark.parametrize("e,vh", [(0, (0, 0)), (1, (0, 5)), (45, (0, 225)),
                                  (46, (1, 3)), (227, (5, 0)),
                                  (11622, (255, 225)), (11623, (256, 3)),
                                  (14164, (311, 223)), (14165, (312, 1)),
                                  (14187, (312, 111))])
def test_pos(e, vh):
    assert cr.MachTime.pos(e) == vh


@pytest.mark.parametrize("e", [-1, 14188])
def test_pos_outside_frame(e):
    with pytest.raises(ValueError):
        cr.MachTime.pos(e)


def test_beam_registers():
    m = cr.MachTime(4)
    assert m.beam(0) == (0x8000, 0x0000)
    assert m.beam(15) == (0x8000, 0x0000)       # still E tick 0
    assert m.beam(16) == (0x8000, 0x0005)       # E tick 1
    assert m.beam(11623 * 16) == (0x8001, 0x0003)   # V8 in VPOSR bit 0
    assert m.beam(14187 * 16 + 15) == (0x8001, 0x386F)  # 312 = $138, h 111
    # past the frame end (VERTB not serviced yet): the last tick, clamped
    assert m.beam(10 ** 9) == m.beam(14187 * 16)


def test_beam_s0_s8():
    assert cr.MachTime(0).beam(46) == (0x8000, 0x0103)
    assert cr.MachTime(8).beam(46 * 256 + 255) == (0x8000, 0x0103)


def test_monotonic_and_is_beam():
    last = -1
    for e in range(cr.MACH_FRAME_E):
        v, h = cr.MachTime.pos(e)
        assert cr.MachTime.is_beam(v, h)
        cc = v * 227 + h
        assert cc > last
        last = cc
    assert not cr.MachTime.is_beam(0, 1)        # not a multiple of 5 cc
    assert not cr.MachTime.is_beam(312, 115)    # past the last E tick
    assert not cr.MachTime.is_beam(0, 227)
