"""W7-T (S3.1): the host reference model of the chip stub (tests/run_chip.py).

tests/run_chip.py diffs the C64 side against ChipRef/GuestRef, so the
model itself is pinned here against docs/S3_PLAN.md §1.3 and §3.4:
fixed reads, write-only = 0, read-only slots read their shadow and ignore
writes, byte writes land on both halves, CIA odd/even decode, the CIA-A
PRA input mix, SERDAT capture, and which accesses take the byte-wise
(mio_split) route.  Hardware-free.
"""

from __future__ import annotations

import pytest

from tests.run_chip import (FIXED, ChipRef, GuestRef, is_io, split_route,
                            straddles)


@pytest.mark.parametrize("reg,val", sorted(FIXED.items()))
def test_fixed_reads(reg: int, val: int) -> None:
    c = ChipRef()
    c.c_ww(0xDFF000 + reg, 0x1111)          # a write never changes them
    assert c.c_rw(0xDFF000 + reg) == val
    assert c.log == [(reg, 0x1111)]


def test_values_the_s3_plan_names() -> None:
    assert FIXED == {0x004: 0x8000, 0x016: 0xFF00, 0x018: 0x3000,
                     0x07C: 0xFFFF}


def test_write_only_reads_zero_and_lands_in_the_shadow() -> None:
    c = ChipRef()
    c.c_ww(0xDFF180, 0x1234)
    assert c.c_rw(0xDFF180) == 0
    assert c.shd[0x180:0x182] == b"\x12\x34"
    assert c.log == [(0x180, 0x1234)]


def test_read_only_slot_reads_shadow_and_ignores_writes() -> None:
    c = ChipRef()
    c.c_ww(0xDFF01C, 0xC020)                # INTENAR is read-only
    assert c.shd[0x1C:0x1E] == b"\0\0" and c.c_rw(0xDFF01C) == 0
    c.shd[0x1C:0x1E] = b"\x40\x20"          # chip logic / host sets it
    assert c.c_rw(0xDFF01C) == 0x4020


def test_byte_access_semantics() -> None:
    c = ChipRef()
    c.wb(0xDFF181, 0xC3)                    # both halves (OCS bus)
    assert c.log == [(0x180, 0xC3C3)]
    assert c.rb(0xDFF004) == 0x80 and c.rb(0xDFF005) == 0x00
    assert c.rb(0xDFF07D) == 0xFF


def test_serdat_capture() -> None:
    c = ChipRef()
    for ch in b"ok":
        c.c_ww(0xDFF030, ch)
    c.wb(0xDFF031, 0x21)
    assert bytes(c.ser) == b"ok!"


def test_open_bus() -> None:
    c = ChipRef()
    assert c.c_rw(0xDF0100) == 0 and c.c_rw(0xDFF200) == 0
    c.c_ww(0xDFEFFE, 1)
    assert c.nopen == 3 and c.log == []


def test_cia_decode() -> None:
    c = ChipRef()
    c.wb(0xBFE201, 0x03)                    # CIA-A DDRA (odd, A12 = 0)
    c.wb(0xBFD200, 0xC0)                    # CIA-B DDRA (even, A13 = 0)
    c.wb(0xBFE200, 0x77)                    # even, A13 = 1: nothing
    c.wb(0xBFD201, 0x66)                    # odd, A12 = 1: nothing
    assert c.cia[2] == 0x03 and c.cia[18] == 0xC0
    assert c.log == [(0x0A02, 0x03), (0x0B02, 0xC0)]
    assert c.nopen == 2
    assert c.rb(0xBF0001) == c.rb(0xBFE001)  # register = (addr >> 8) & 15


def test_ciaa_pra_inputs() -> None:
    c = ChipRef()
    assert c.rb(0xBFE001) == 0xEB           # DDRA = 0: all inputs
    c.wb(0xBFE201, 0x03)
    c.wb(0xBFE001, 0x02)
    assert c.rb(0xBFE001) == (0x02 & 0x03) | (0xEB & 0xFC)


@pytest.mark.parametrize("a,size,want", [
    (0xDFFFFE, 4, True), (0xDFFFFE, 2, False), (0xDFFFFF, 2, True),
    (0xBEFFFE, 4, True), (0xBEFFFF, 2, True), (0xBEFFFC, 4, False),
    (0xDEFFFD, 4, True), (0xDEFFFE, 2, False), (0x07FFFE, 4, True),
    (0x0EFFFE, 4, False), (0xBFFFFD, 4, True), (0xDFF180, 4, False),
    (0xBDFFFE, 4, False), (0x9EFFFE, 4, False), (0xFEFFFE, 4, False),
])
def test_split_route(a: int, size: int, want: bool) -> None:
    assert split_route(a, size) == want


def test_straddle_is_the_512k_line() -> None:
    assert straddles(0x07FFFF, 2) and not straddles(0x07FFFE, 2)
    assert is_io(0xBF0000) and is_io(0xDFFFFF) and not is_io(0xC00000)


def test_guest_split_long_at_dffffe() -> None:
    reu = {0xDFFFFE: 0xA5, 0xDFFFFF: 0xA5}
    g = GuestRef(ChipRef(), reu)
    assert g.write(0xDFFFFE, 4, 0x11223344) == 2        # the $E0 half only
    assert reu[0xDFFFFE] == 0xA5 and reu[0xE00000] == 0x33
    assert g.read(0xDFFFFE, 4) == (0x00003344, 2)


def test_guest_bank_carry() -> None:
    reu = {0xBEFFFE: 0x12, 0xBEFFFF: 0x34, 0xBF0000: 0xA5}
    c = ChipRef()
    c.wb(0xBFD000, 0x5C)                                # CIA-B PRA
    assert GuestRef(c, reu).read(0xBEFFFE, 4) == (0x12345CEB, 2)


def test_guest_trapped_long_is_two_register_words() -> None:
    c = ChipRef()
    g = GuestRef(c, {})
    assert g.write(0xDFF180, 4, 0x11112222) == 0
    assert c.log == [(0x180, 0x1111), (0x182, 0x2222)]
    assert g.read(0xDFF002, 4) == (0x00008000, 0)       # DMACONR 0, VPOSR
