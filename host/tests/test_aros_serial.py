"""c64u ser-ring: the host side of the chip_ser capture (tests/aros_boot.py).

A fake target runs a "guest" that appends SERDAT bytes to a chip_ser ring
exactly as runtime/ops/chip.s does (byte at slot count mod size, then the
u16 count) while the host reads, so Machine.serial / serial_final / watch
are exercised against bursts, wraps and torn count reads without a device:

  * the C64U case: 257 bytes land during ONE read (count 130 -> 387, REST
    GET ~112 ms); a 1024-byte ring captures every byte, the old 256-byte
    ring cannot (CaptureError, never a silent pass);
  * a count read torn across the low byte's carry (+256) is not trusted;
  * the u16 count wraps through 0;
  * bytes printed after the last poll, just before mach_run returns, are
    taken by the final read (review finding: the ablate leg could pass on
    dropped bytes).
"""

from __future__ import annotations

import struct

import pytest

from tests import aros_boot as AB

SER = 0x9000


class FakeGuest:
    """chip_ser/chip_sern in a dict-backed C64 RAM, plus a write schedule:
    sched[i] = bytes the guest writes just before host read i completes."""

    def __init__(self, sz: int, text: bytes, sched: list[int], sern0: int = 0):
        self.sz, self.text, self.sched = sz, text, list(sched)
        self.ram = bytearray(0x10000)
        self.k = 0                      # bytes of text written so far
        self.sern0 = sern0
        self.lbl = {"chip_ser": SER, "chip_sern": SER + sz + 2,
                    "chip_logn": SER + sz, "CHIP_SERSZ": sz}
        self.reads = 0
        self.tear: set[int] = set()     # read numbers whose count read tears
        self.returned = False
        self.halt_after = None          # read number at which mach_run returns

    def count(self) -> int:
        return (self.sern0 + self.k) & 0xFFFF

    def step(self, n: int) -> None:
        for _ in range(n):
            if self.k >= len(self.text):
                return
            c = self.count()
            self.ram[SER + c % self.sz] = self.text[self.k]   # store first,
            self.k += 1                                       # then count
            struct.pack_into("<H", self.ram, self.lbl["chip_sern"], self.count())

    def read(self, addr: int, n: int) -> bytes:
        i = self.reads
        self.reads += 1
        if addr == self.lbl["chip_sern"] and i in self.tear:
            # low byte read before a carry, high byte after it: +256
            lo = self.count() | 0xFF
            self.step(1)
            out = bytes([lo & 0xFF, self.ram[addr + 1]])
            return out
        out = bytes(self.ram[addr:addr + n])
        self.step(self.sched[i] if i < len(self.sched) else 0)
        if self.halt_after is not None and i >= self.halt_after:
            self.returned = True
        return out


class FakeMachine(AB.Machine):
    def __init__(self, g: FakeGuest):          # no target, no driver
        self.g, self.vice, self.retries = g, False, 0
        self.lab = lambda n: g.lbl[n]
        self.sersz = AB.ser_size(self.lab)

    def r(self, addr: int, n: int) -> bytes:
        if addr == AB.R:
            st = 0xFF if self.g.returned else 0
            return bytes([st]) + bytes(15)
        if addr == AB.ZP_FRAMES:
            return bytes(4)
        return self.g.read(addr, n)

    def cpu_state(self) -> str:
        return "fake"


TEXT = bytes((i * 37 + 11) & 0xFF for i in range(416))


def capture(m: FakeMachine, total: int) -> bytes:
    text, seen = bytearray(), 0
    for _ in range(10000):
        n, ring = m.serial(seen)
        seen, lost = AB.take(text, ring, seen, n, m.sersz)
        assert lost == 0
        if len(text) >= total:
            return bytes(text)
    raise AssertionError("capture did not finish")


def c64u_sched() -> list[int]:
    # 130 bytes at a trickle, then 257 during one read, then the rest
    return [5] * 26 + [257] + [3] * 20


def test_ser_size_from_labels():
    g = FakeGuest(1024, TEXT, [])
    assert AB.ser_size(g.lbl.__getitem__) == 1024
    g.lbl["chip_logn"] += 1                     # labels disagree: refuse
    with pytest.raises(RuntimeError):
        AB.ser_size(g.lbl.__getitem__)


def test_c64u_burst_1024_ring_captures_every_byte():
    g = FakeGuest(1024, TEXT, c64u_sched())
    assert capture(FakeMachine(g), len(TEXT)) == TEXT


def test_c64u_burst_256_ring_fails_loudly():
    """The old ring: the same burst laps it inside one read -> CaptureError."""
    g = FakeGuest(256, TEXT, c64u_sched())
    with pytest.raises(AB.CaptureError):
        capture(FakeMachine(g), len(TEXT))


def test_torn_count_is_not_trusted():
    """A count read torn to +256 (low byte $FF) must not return slots the
    guest has not written yet."""
    text = bytes((i * 13 + 1) & 0xFF for i in range(700))
    # read 0: the count, torn (511 for 256); read 1: the ring, during which
    # a burst of 300 bytes lands, so the closing count (556) brackets the
    # torn one and only the tear check stands between it and a bogus pass
    g = FakeGuest(1024, text, [0, 300] + [4] * 400)
    m = FakeMachine(g)
    g.step(255)                                 # count $00FF: next bump carries
    g.tear = {0}                                # the first count read tears
    assert capture(m, len(text)) == text
    assert m.retries >= 1                       # the torn read was re-read


def test_u16_wrap():
    text = bytes((i * 7 + 3) & 0xFF for i in range(1500))
    g = FakeGuest(1024, text, [9] * 400, sern0=0x10000 - 700)
    m = FakeMachine(g)
    seen = g.count()
    out, cur = bytearray(), seen
    while len(out) < len(text):
        n, ring = m.serial(cur)
        cur, lost = AB.take(out, ring, cur, n, m.sersz)
        assert lost == 0
    assert bytes(out) == text
    assert g.count() == (0x10000 - 700 + 1500) & 0xFFFF


def test_final_read_after_return(monkeypatch):
    """The guest prints its last 40 bytes between the last serial poll and
    the read of the return flag, then mach_run returns: watch's final read
    must take them."""
    monkeypatch.setattr(AB, "POLL_S", 0.0)
    text = b"x" * 60 + b"ROMInfo: 1MiB ROM detected\n" + b"t" * 13
    g = FakeGuest(1024, text, [])
    m = FakeMachine(g)
    g.step(len(text) - 40)
    # every host read before the return-flag read: nothing more is written;
    # make the guest write the last 40 bytes and return during the frames
    # read that precedes the flag read
    orig = m.r

    def r(addr, n):
        if addr == AB.ZP_FRAMES and not g.returned:
            g.step(40)
            g.returned = True
        return orig(addr, n)
    m.r = r
    out: dict = {}
    AB.watch(m, ["m1"], 5.0, out)
    assert out.get("returned") is not None
    assert out["serial"].encode("latin-1") == text
    assert out["final_count"] == len(text)
    assert "m1" in out["at"] and out["at"]["m1"].get("final_read")
