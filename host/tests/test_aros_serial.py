"""c64u ser-ring: the host side of the chip_ser capture (tests/aros_boot.py).

A fake target runs a "guest" that appends SERDAT bytes to a chip_ser ring
exactly as runtime/ops/chip.s does (byte at slot count mod size, then the
u16 count) WHILE the host reads: every host GET lets the guest write a
scheduled number of bytes before the GET samples memory.  The fake models
the two U64 read paths of tests/aros_boot.py:

  one_get=True   Machine.seg on the U64E/C64U: one readmem GET per ring
                 segment (the transport's read_memory, round 1 R3);
  one_get=False  the harness's read_bytes: a read above 256 bytes is split
                 into 128-byte GETs (c64_test_harness.memory
                 read_bytes_chunked), the guest writing between them.

Count reads can be torn across the low byte's carry both ways: +256 (low
byte $FF read before the carry, high byte after) and -256 (low byte $00
read after the carry, high byte stale).  The ring bytes are random, so a
slot overwritten one lap later (index + 1024) never reads back equal.

Cases: the C64U burst (257 bytes inside one read: a 1024 ring captures it,
a 256 ring fails loudly); a lap during the ring read (the closing count
must reject it); a -256 torn closing count during a lap; a +256 torn
opening count; the u16 wrap; steady-rate headroom of both read paths; the
final read after mach_run returns (including more than a ring's worth,
counted as lost) and the ablate verdict on a lossy capture.
"""

from __future__ import annotations

import random
import struct

import pytest

from tests import aros_boot as AB

SER = 0x9000
HARNESS_CHUNK, HARNESS_THRESHOLD = 128, 256     # c64_test_harness.memory


def rnd(n: int, seed: int = 1) -> bytes:
    return bytes(random.Random(seed).randrange(256) for _ in range(n))


class FakeGuest:
    """chip_ser/chip_sern in a 64 KiB RAM image.  rate(i) = bytes the guest
    writes during host GET number i (before the GET samples memory);
    tear[i] = "+" or "-" makes GET i, a count read, return a torn count."""

    def __init__(self, sz: int, text: bytes, rate, sern0: int = 0):
        self.sz, self.text = sz, text
        self.rate = rate if callable(rate) else (
            lambda i, r=list(rate): r[i] if i < len(r) else 0)
        self.ram = bytearray(0x10000)
        self.k = 0                      # bytes of text written so far
        self.sern0 = sern0
        self.lbl = {"chip_ser": SER, "chip_sern": SER + sz + 2,
                    "chip_logn": SER + sz, "CHIP_SERSZ": sz}
        struct.pack_into("<H", self.ram, self.lbl["chip_sern"], sern0)
        self.gets = 0
        self.tear: dict[int, str] = {}
        self.returned = False

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

    def get(self, addr: int, n: int) -> bytes:
        """ONE host GET of n bytes at addr."""
        i = self.gets
        self.gets += 1
        w = self.rate(i)
        kind = self.tear.get(i)
        if kind and addr == self.lbl["chip_sern"]:
            if kind == "+":             # low byte before the carry, high after
                assert self.count() & 0xFF == 0xFF, "a + tear needs low byte $FF"
                lo = self.ram[addr]
                self.step(max(w, 1))
                return bytes([lo, self.ram[addr + 1]])
            self.step(w)                # "-": low byte after the carry, high stale
            assert self.count() & 0xFF == 0x00, "a - tear needs low byte $00"
            return bytes([0x00, (self.ram[addr + 1] - 1) & 0xFF])
        self.step(w)
        return bytes(self.ram[addr:addr + n])


class FakeTransport:
    def __init__(self, g: FakeGuest):
        self.g = g

    def read_memory(self, addr: int, n: int) -> bytes:   # one readmem GET
        return self.g.get(addr, n)


class FakeMachine(AB.Machine):
    def __init__(self, g: FakeGuest, one_get: bool = True):  # no target, no driver
        self.g, self.vice, self.retries = g, False, 0
        self.t = FakeTransport(g)
        self.one_get = one_get
        self.lab = lambda n: g.lbl[n]
        self.sersz = AB.ser_size(self.lab)

    def r(self, addr: int, n: int) -> bytes:
        """The harness's read_bytes: above 256 bytes, 128-byte GETs."""
        if addr == AB.R:
            return bytes([0xFF if self.g.returned else 0]) + bytes(15)
        if addr == AB.ZP_FRAMES:
            return bytes(4)
        if n > HARNESS_THRESHOLD:
            return b"".join(self.g.get(addr + o, min(HARNESS_CHUNK, n - o))
                            for o in range(0, n, HARNESS_CHUNK))
        return self.g.get(addr, n)

    def cpu_state(self) -> str:
        return "fake"


def capture(m: FakeMachine, total: int) -> bytes:
    """Poll like watch() from the count the boot started at (the guest's
    sern0) until *total* bytes are taken; a lost byte fails."""
    text, seen = bytearray(), m.g.sern0
    for _ in range(100000):
        n, ring = m.serial(seen)
        seen, lost = AB.take(text, ring, seen, n, m.sersz)
        assert lost == 0
        if len(text) >= total:
            return bytes(text)
    raise AssertionError("capture did not finish")


def c64u_rate() -> list[int]:
    # 130 bytes at a trickle, then 257 during one GET, then the rest
    return [5] * 26 + [257] + [3] * 20


TEXT = rnd(416)


def test_ser_size_from_labels():
    g = FakeGuest(1024, TEXT, [])
    assert AB.ser_size(g.lbl.__getitem__) == 1024
    g.lbl["chip_logn"] += 1                     # labels disagree: refuse
    with pytest.raises(RuntimeError):
        AB.ser_size(g.lbl.__getitem__)


@pytest.mark.parametrize("one_get", [True, False])
def test_c64u_burst_1024_ring_captures_every_byte(one_get):
    g = FakeGuest(1024, TEXT, c64u_rate())
    assert capture(FakeMachine(g, one_get), len(TEXT)) == TEXT


def test_c64u_burst_256_ring_fails_loudly():
    """The old ring: the same burst laps it inside one GET -> CaptureError."""
    g = FakeGuest(256, TEXT, c64u_rate())
    with pytest.raises(AB.CaptureError):
        capture(FakeMachine(g), len(TEXT))


def test_lap_during_ring_read_is_rejected():
    """800 bytes pending; 300 more land while the ring is read (one GET), so
    slots 0..75 hold bytes 1024..1099 when sampled.  The closing count
    (1100 > seen + 1024) must reject the snapshot: CaptureError, never the
    overwritten bytes (kills: no closing read; a bound of sz + 256)."""
    text = rnd(1100, 2)
    g = FakeGuest(1024, text, [0, 300])
    g.step(800)
    with pytest.raises(AB.CaptureError):
        capture(FakeMachine(g), len(text))


def test_lap_by_one_byte_is_rejected():
    """The bound is exact: 225 bytes land during the ring GET, so the
    closing count is seen + 1025 and byte 1024 already sits in slot 0.
    One byte past the ring must reject (kills: a bound of sz + 1)."""
    text = rnd(1025, 4)
    g = FakeGuest(1024, text, [0, 225])
    g.step(800)
    with pytest.raises(AB.CaptureError):
        capture(FakeMachine(g), len(text))


def test_minus_torn_closing_count_during_a_lap_is_rejected():
    """As above, but 480 bytes land during the ring GET (count 1280 = $0500,
    slots 0..255 overwritten) and the closing count read tears to -256
    ($0400 = 1024: low byte $00 after the carry, high byte stale), which
    fits the bracket.  A count with low byte $00 must be read again (kills:
    raw count() for the closing read; re-reading only $FF)."""
    text = rnd(1280, 3)
    g = FakeGuest(1024, text, [0, 480])
    g.step(800)
    g.tear = {2: "-"}                   # GET 0 count, GET 1 ring, GET 2 count
    with pytest.raises(AB.CaptureError):
        capture(FakeMachine(g), len(text))


def test_plus_torn_opening_count_is_not_trusted():
    """GET 0, the opening count, tears to +256 (511 for 256: low byte $FF
    before the carry, high byte after); a burst of 300 lands during the
    next GETs, so the closing count brackets the torn one and only the
    tear check stands between it and slots the guest has not written."""
    text = rnd(700, 4)
    g = FakeGuest(1024, text, [0, 300] + [4] * 400)
    m = FakeMachine(g)
    g.step(255)                                 # count $00FF: next bump carries
    g.tear = {0: "+"}
    out, seen = bytearray(), 0
    while len(out) < len(text):
        n, ring = m.serial(seen)
        seen, lost = AB.take(out, ring, seen, n, m.sersz)
        assert lost == 0
    assert bytes(out) == text
    assert m.retries >= 1                       # the torn read was re-read


def test_u16_wrap():
    text = rnd(1500, 5)
    g = FakeGuest(1024, text, [9] * 1000, sern0=0x10000 - 700)
    assert capture(FakeMachine(g), len(text)) == text
    assert g.count() == (0x10000 - 700 + 1500) & 0xFFFF


@pytest.mark.parametrize("one_get,rate,ok", [
    (True, 150, True),          # one GET per segment: every rate < 160 B/GET holds
    (True, 200, False),         # (sweep: first lap at 160, all lap from 175)
    (False, 70, True),          # the 128-byte split: every rate < 80 B/GET holds
    (False, 100, False),        # (sweep: first lap at 80)
])
def test_steady_rate_headroom(one_get, rate, ok):
    """A steady *rate* bytes per host GET for 4000 bytes.  With the split,
    a backlog of k bytes costs k/128 GETs during which rate*k/128 more
    arrive, so the backlog grows toward a lap well below 128 B/GET (the
    sweep in build/evidence/c64u-ser/round1/headroom.txt: below 80 B/GET
    holds).  One GET per segment keeps the backlog at ~3-5 GETs' worth:
    below 160 B/GET holds (~1380 B/s at the C64U's ~112 ms per GET).  The AROS
    boot's worst is one 257-byte burst, then ~400 B/s."""
    text = rnd(4000, 6)
    g = FakeGuest(1024, text, lambda i: rate)
    m = FakeMachine(g, one_get)
    if ok:
        assert capture(m, len(text)) == text
    else:
        with pytest.raises(AB.CaptureError):
            capture(m, len(text))


def test_final_read_after_return(monkeypatch):
    """The guest prints its last 40 bytes (the whole m1 line) between the
    last serial poll and the read of the return flag, then mach_run
    returns: watch's final read must take them."""
    monkeypatch.setattr(AB, "POLL_S", 0.0)
    text = b"x" * 60 + b"ROMInfo: 1MiB ROM detected\n" + b"t" * 13
    g = FakeGuest(1024, text, [])
    m = FakeMachine(g)
    g.step(len(text) - 40)
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
    assert AB.ablate_verdict(out) == ["ablate: m1 reached with exception mode off"]


def test_final_read_more_than_a_ring_reads_the_last_1024():
    """1500 bytes since the last poll, then the return: serial_final reads
    the LAST 1024 slots (not zeros) and take() counts 476 lost."""
    text = rnd(1500, 7)
    g = FakeGuest(1024, text, [])
    g.step(1500)
    m = FakeMachine(g)
    n, ring = m.serial_final(0)
    out = bytearray()
    seen, lost = AB.take(out, ring, 0, n, m.sersz)
    assert (n, seen, lost) == (1500, 1500, 476)
    assert bytes(out) == text[476:]


def test_ablate_lossy_capture_is_not_a_pass(monkeypatch):
    """A boot that printed more than a ring's worth after the last poll and
    then halted: the capture is lossy, so ablate must not pass (INFRA)."""
    monkeypatch.setattr(AB, "POLL_S", 0.0)
    text = rnd(1500, 8).replace(b"ROMInfo", b"romInfo")
    g = FakeGuest(1024, text, [])
    m = FakeMachine(g)
    orig = m.r

    def r(addr, n):
        if addr == AB.ZP_FRAMES and not g.returned:
            g.step(1500)
            g.returned = True
        return orig(addr, n)
    m.r = r
    out: dict = {}
    AB.watch(m, ["m1"], 5.0, out)
    assert out["lost"] == 476 and "returned" in out
    v = AB.ablate_verdict(out)
    assert len(v) == 1 and v[0].startswith(AB.INFRA) and "lost" in v[0]


def test_ablate_verdicts():
    ok = {"at": {}, "returned": {"st_halt": 1}, "lost": 0}
    assert AB.ablate_verdict(ok) == []
    assert AB.ablate_verdict({**ok, "capture_error": "x"})[0].startswith(AB.INFRA)
    assert AB.ablate_verdict({"at": {}, "lost": 0}) == [
        "ablate: the boot neither halted nor reached m1"]
