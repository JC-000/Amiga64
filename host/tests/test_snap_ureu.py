"""Byte-level protocol test for the UCI REU I/O (runtime/ops/ureu.s, W12-T).

Nothing here touches a device.  ``ureu.s`` and ``dosio_uci.s`` are
assembled standalone with a shim (the dosio_* block they import) and RUN
in the 6502 emulator of host/tests/test_hle_dos_uci.py against its UCI
model, whose DOS targets are extended here (test-side only) with
DOS_CMD_LOAD_REU $21 / DOS_CMD_SAVE_REU $22 exactly as the firmware
implements them: 1541ultimate software/filemanager/dos.cc:254-319 at
~/Documents/1541u-315preview (@871ad034, the U64E 3.15 line; the brief
relays dos.h/dos.cc as identical in the C64U 1.1.0 build):

  - payload message[2..5] addr u32 LE, [6..9] len u32 LE; addr &= $FFFFFF,
    len &= $1FFFFFF; no open file -> "85,NO FILE OPEN", empty reply;
  - addr + len > $1000000 -> len clamped, status "02,REQUEST TRUNCATED";
  - file->read / write at the file's current position;
  - one reply block "$%6x BYTES LOADED TO REU $%6x" (35 bytes) /
    "$%6x BYTES SAVED FROM REU $%6x" (36 bytes), formatted by the
    firmware's own sprintf (software/system/small_printf.cc: %x -> _hex,
    lines 178-184, exactly `width` digits of hexchar[] = "0123456789ABCDEF",
    line 11: zero-filled, upper case, truncated to the width), with the TRANSFERRED
    count; status "00,OK" even for a short read; a FatFS error replaces
    the status (file_system.cc get_error_string, no "NN," prefix).

The test asserts the exact command bytes, the chunking, the count rule
(the count, not the status, decides), the error codes and the handshake
discipline (assert_clean).
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from host.tests import test_hle_dos_uci as U

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("cl65") is None,
                                reason="cl65 (cc65) not installed")

LOAD, SAVE = 0x21, 0x22                # dos.h:29-30
REU_SIZE = 0x1000000
CHUNK = 0x40000                        # ureu.s UREU_CHUNK_HI = 4
E_OPEN, E_LOST, E_STAT, E_SHORT, E_CLOSE = 1, 2, 3, 5, 6


def x6(v: int) -> bytes:
    """small_printf.cc "%6x": 6 upper-case digits, zero-filled, truncated."""
    return b"%06X" % (v & 0xFFFFFF)


class ReuTarget(U.DosTarget):
    """DosTarget + LOAD_REU / SAVE_REU (dos.cc:254-319)."""

    def parse(self, msg, dev):
        cmd = msg[1]
        if cmd not in (LOAD, SAVE):
            return super().parse(msg, dev)
        dev.reu_cmds.append(bytes(msg))
        if self.file is None:                          # dos.cc:255-259
            return [b""], b"85,NO FILE OPEN"
        addr = struct.unpack_from("<I", msg, 2)[0] & 0xFFFFFF
        n = struct.unpack_from("<I", msg, 6)[0] & 0x1FFFFFF
        status = b"00,OK"
        if addr + n > REU_SIZE:                        # dos.cc:269-272
            n = REU_SIZE - addr
            status = b"02,REQUEST TRUNCATED"
        data = self.fs[self.file]
        if cmd == LOAD:
            if dev.io_error:
                return [b""], dev.io_error
            got = bytes(data[self.pos:self.pos + n])
            dev.reu[addr:addr + len(got)] = got
            self.pos += len(got)
            text = b"$" + x6(len(got)) + b" BYTES LOADED TO REU $" + x6(addr)
        else:
            if dev.io_error:
                return [b""], dev.io_error
            if self.pos > len(data):
                data.extend(bytes(self.pos - len(data)))
            data[self.pos:self.pos + n] = dev.reu[addr:addr + n]
            self.pos += n
            got = b"x" * n
            text = b"$" + x6(n) + b" BYTES SAVED FROM REU $" + x6(addr)
        if dev.fake_count is not None:
            text = b"$" + x6(dev.fake_count) + text[7:]
        if dev.fake_text is not None:
            text = dev.fake_text
        return [text], status


class ReuDevice(U.UciDevice):
    def __post_init__(self):
        self.targets = {1: ReuTarget(self.fs), 2: ReuTarget(self.fs)}
        self.reu = bytearray(REU_SIZE)
        self.reu_cmds = []
        self.io_error = None
        self.fake_count = None
        self.fake_text = None


SHIM = U.SHIM + """
.export code_invall, ci_calls
.segment "BSS"
ci_calls: .res 1
.segment "CODE"
code_invall:                    ; W12-C stand-in: counts the calls (A1)
        inc ci_calls
        rts
"""

CFG = """
MEMORY {
    ZP:   start = $0080, size = $0070, type = rw;
    MAIN: start = $1000, size = $4000, file = %O;
    BSSM: start = $6000, size = $2000, type = rw;
}
SEGMENTS {
    ZEROPAGE: load = ZP, type = zp, optional = yes;
    CODE:     load = MAIN, type = ro;
    SCODE:    load = MAIN, type = ro;
    RODATA:   load = MAIN, type = ro, optional = yes;
    DATA:     load = MAIN, type = rw, optional = yes;
    BSS:      load = BSSM, type = bss, optional = yes, define = yes;
    SBSS:     load = BSSM, type = bss, optional = yes;
}
"""


def build_ureu(d: Path, defines=()) -> tuple[bytes, dict]:
    (d / "shim.s").write_text(SHIM)
    (d / "t.cfg").write_text(CFG)
    out, lbl = d / "u.bin", d / "u.lbl"
    cmd = ["cl65", "-t", "none", "-C", str(d / "t.cfg"), "-o", str(out),
           "-Ln", str(lbl), "--asm-define", "DOSIO_UCI_FENCE_OUTER=1",
           "--asm-define", "DOSIO_UCI_FENCE_INNER=1",
           "--asm-define", "DOSIO_UCI_POLLS_HI=4"]
    for x in defines:
        cmd += ["--asm-define", x]
    cmd += [str(ROOT / "runtime/ops/dosio_uci.s"), str(d / "shim.s"),
            str(ROOT / "runtime/ops/ureu.s")]       # ureu.s last (its BSS-end assert)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    labels = {}
    for line in lbl.read_text().splitlines():
        p = line.split()
        if len(p) == 3 and p[0] == "al":
            labels[p[2].lstrip(".")] = int(p[1], 16)
    return out.read_bytes(), labels


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    return build_ureu(tmp_path_factory.mktemp("ureu"))


class H(U.Harness):
    def __init__(self, build, fs=None, latency=3):
        self.image, self.lab = build
        self.dev = ReuDevice(fs=fs if fs is not None else {}, latency=latency)
        self.bus = U.Bus(self.dev)
        self.bus.ram[0x1000:0x1000 + len(self.image)] = self.image
        self.bus.ram[0x01] = 0x37
        self.cpu = U.CPU(self.bus)

    def run(self, entry: str, name: str, ra: int, rl: int) -> int:
        raw = name.encode()
        self.poke("dosio_name", raw)
        self.poke("dosio_nlen", bytes([len(raw)]))
        self.poke("ureu_ra", ra.to_bytes(3, "little"))
        self.poke("ureu_rl", struct.pack("<I", rl))
        self.poke("ureu_err", b"\xAA")
        cf = self.call(entry, limit=30_000_000)
        assert self.bus.ram[0x01] == 0x37, "caller's $01 not restored"
        return cf

    def err(self):
        return self.peek("ureu_err", 1)[0]


def pattern(n: int, k: int = 7) -> bytes:
    return bytes((i * k + (i >> 8) * 13 + 5) & 0xFF for i in range(n))


def xfer_cmds(h):
    return [m for m in h.dev.log if len(m) > 1 and m[1] in (LOAD, SAVE)]


def expect_chunks(cmd: int, addr: int, n: int) -> list[bytes]:
    out = []
    while n:
        c = min(n, CHUNK)
        out.append(bytes([1, cmd]) + struct.pack("<II", addr, c))
        addr += c
        n -= c
    return out


# ---------------------------------------------------------------------------

def test_entries_and_constants(build):
    _, lab = build
    for e in ("ureu_ld", "ureu_sv", "ureu_bank", "ureu_open", "ureu_xfer",
              "ureu_close", "ureu_ra", "ureu_rl", "ureu_err", "ureu_us"):
        assert e in lab, e
    # the payload's addr/len layout is what dos.cc:261-266 decodes
    assert lab["UREU_LOAD"] == LOAD and lab["UREU_SAVE"] == SAVE


def test_load_multi_chunk_exact_bytes(build):
    n = 3 * CHUNK + 0x1235
    data = pattern(n)
    h = H(build, fs={"/SD/amiga64/rom.bin": bytearray(data)})
    assert h.run("ureu_ld", "/SD/amiga64/rom.bin", 0xE00000 - 0x1000, n) == 0
    assert h.err() == 0
    a = 0xE00000 - 0x1000
    assert bytes(h.dev.reu[a:a + n]) == data
    assert h.dev.reu[a - 1] == 0 and h.dev.reu[a + n] == 0
    log = [bytes(m) for m in h.dev.log]
    # dosio_uci_init: CLOSE 1, CLOSE 2; then OPEN read, the chunks, CLOSE
    assert log[0] == b"\x01\x03" and log[1] == b"\x02\x03"
    assert log[2] == b"\x01\x02\x01/SD/amiga64/rom.bin"
    assert xfer_cmds(h) == expect_chunks(LOAD, a, n)
    assert log[-1] == b"\x01\x03"
    assert h.peek("ureu_rl", 4) == bytes(4)
    U.assert_clean(h)


def test_load_invalidates_code_and_bumps_the_epoch(build):
    """A1: after any ureu_ld (even a failing one: a partial load may have
    landed) the code windows are invalidated and disp_st's pixel epoch
    (+105) moves, so neither a stale code window nor a stale picture
    survives a load into guest memory."""
    for fs, rl, ok in (({"x.bin": bytearray(pattern(0x100))}, 0x100, 0),
                       ({"x.bin": bytearray(pattern(0x80))}, 0x100, 1)):
        h = H(build, fs=fs)
        h.bus.ram[0xB800 + 105] = 0x41
        assert h.run("ureu_ld", "x.bin", 0x1000, rl) == ok
        assert h.peek("ci_calls", 1)[0] == 1
        assert h.bus.ram[0xB800 + 105] == 0x42
    h = H(build, fs={"x.bin": bytearray(pattern(0x100))})
    h.bus.ram[0xB800 + 105] = 0x41
    assert h.run("ureu_sv", "y.bin", 0x1000, 0x100) == 0     # a save: nothing
    assert h.peek("ci_calls", 1)[0] == 0 and h.bus.ram[0xB800 + 105] == 0x41


def test_save_round_trip(build):
    n = CHUNK + 0x300
    h = H(build)
    h.dev.reu[0x200000:0x200000 + n] = pattern(n, 11)
    assert h.run("ureu_sv", "snap.a64", 0x200000, n) == 0
    assert h.err() == 0
    assert h.dev.log[2] == b"\x01\x02\x0Asnap.a64"   # FA_WRITE|FA_CREATE_ALWAYS
    assert bytes(h.dev.fs["snap.a64"]) == pattern(n, 11)
    assert xfer_cmds(h) == expect_chunks(SAVE, 0x200000, n)
    h.dev.reu[0x200000:0x200000 + n] = bytes(n)     # and back
    assert h.run("ureu_ld", "snap.a64", 0x200000, n) == 0
    assert bytes(h.dev.reu[0x200000:0x200000 + n]) == pattern(n, 11)
    U.assert_clean(h)


def test_short_file_status_ok_but_count_short_fails(build):
    """dos.cc: a short file answers "00,OK" with a smaller count: the count
    is the truth (brief T1)."""
    n = CHUNK + 100
    h = H(build, fs={"short.bin": bytearray(pattern(n - 1))})
    assert h.run("ureu_ld", "short.bin", 0, n) == 1
    assert h.err() == E_SHORT
    assert h.dev.log[-1] == b"\x01\x03"             # closed anyway
    U.assert_clean(h)


def test_short_on_a_chunk_boundary(build):
    """The file ends exactly where a chunk starts: that chunk reads 0."""
    h = H(build, fs={"b.bin": bytearray(pattern(CHUNK))})
    assert h.run("ureu_ld", "b.bin", 0, CHUNK + 1) == 1
    assert h.err() == E_SHORT
    assert len(xfer_cmds(h)) == 2


def test_missing_file_no_transfer(build):
    h = H(build)
    assert h.run("ureu_ld", "nope.bin", 0, 0x100) == 1
    assert h.err() == E_OPEN
    assert xfer_cmds(h) == []
    U.assert_clean(h)


def test_io_error_status_is_kept(build):
    h = H(build, fs={"x.bin": bytearray(pattern(0x100))})
    h.dev.io_error = b"DISK ERROR"
    assert h.run("ureu_ld", "x.bin", 0, 0x100) == 1
    assert h.err() == E_STAT
    assert h.peek("ureu_us", 2) == b"DI"
    U.assert_clean(h)


def test_truncated_request_fails(build):
    """addr + len past 16 MB: the firmware clamps and says "02": refused,
    whatever the count."""
    h = H(build, fs={"x.bin": bytearray(pattern(0x200))})
    assert h.run("ureu_ld", "x.bin", 0xFFFF00, 0x200) == 1
    assert h.err() == E_STAT
    assert h.peek("ureu_us", 2) == b"02"


def test_end_of_reu_exact_is_fine(build):
    h = H(build, fs={"x.bin": bytearray(pattern(0x100))})
    assert h.run("ureu_ld", "x.bin", 0xFFFF00, 0x100) == 0
    assert bytes(h.dev.reu[0xFFFF00:]) == pattern(0x100)


@pytest.mark.parametrize("count", [0x3FFFF, 0x40001, 0x4, 0x140000])
def test_any_other_count_fails(build, count):
    h = H(build, fs={"x.bin": bytearray(pattern(2 * CHUNK))})
    h.dev.fake_count = count
    assert h.run("ureu_ld", "x.bin", 0, CHUNK) == 1
    assert h.err() == E_SHORT


@pytest.mark.parametrize("text", [b"", b"$040000", b"#040000 BYTES",
                                  b"$040000XBYTES", b"$ 40000 BYTES",
                                  b"$04OOOO BYTES", b"$040000 BYTES"[:7],
                                  b"$40000 BYTES"])
def test_malformed_reply_fails(build, text):
    h = H(build, fs={"x.bin": bytearray(pattern(CHUNK))})
    h.dev.fake_text = text
    assert h.run("ureu_ld", "x.bin", 0, CHUNK) == 1
    assert h.err() == E_SHORT


def test_reply_padding_and_case(build):
    """small_printf "%6x": "$001A2B BYTES ..." (zero-filled, upper case).
    The C library's "%6x" ("$  1a2b") is NOT what the firmware sends."""
    n = 0x1A2B
    h = H(build, fs={"x.bin": bytearray(pattern(n))})
    assert h.run("ureu_ld", "x.bin", 0x123456, n) == 0
    assert h.peek("dosio_buf", 8) == b"$001A2B "
    for bad in (b"$  1a2b BYTES LOADED TO REU $123456",
                b"$  1A2B BYTES LOADED TO REU $123456",
                b"$001a2b BYTES LOADED TO REU $123456"):
        h2 = H(build, fs={"x.bin": bytearray(pattern(n))})
        h2.dev.fake_text = bad
        assert h2.run("ureu_ld", "x.bin", 0x123456, n) == 1, bad


def test_no_reply_is_lost_and_aborts(build):
    h = H(build, fs={"x.bin": bytearray(pattern(0x100))})
    h.dev.hang_cmds = {LOAD}
    assert h.run("ureu_ld", "x.bin", 0, 0x100) == 1
    assert h.err() == E_LOST
    assert 0x04 in h.dev.ctl_writes                 # ABORT


def test_zero_length_is_open_close_only(build):
    h = H(build, fs={"x.bin": bytearray(b"abc")})
    assert h.run("ureu_ld", "x.bin", 0, 0) == 0
    assert xfer_cmds(h) == []


def test_mutant_nocount_survives_nothing(tmp_path):
    """Mutant (≤ 10 per package): ureu_chk skipped -> the short-file case
    must catch it (red)."""
    src = (ROOT / "runtime/ops/ureu.s").read_text()
    assert "jsr ureu_chk" in src
    d = tmp_path / "mut"
    d.mkdir()
    mut = src.replace("jsr ureu_chk", "clc")
    (d / "ureu.s").write_text(mut)
    import shutil as sh
    sh.copytree(ROOT / "runtime", d / "runtime_copy")
    (d / "runtime_copy" / "ops" / "ureu.s").write_text(mut)
    (d / "shim.s").write_text(SHIM)
    (d / "t.cfg").write_text(CFG)
    out, lbl = d / "u.bin", d / "u.lbl"
    r = subprocess.run(
        ["cl65", "-t", "none", "-C", str(d / "t.cfg"), "-o", str(out), "-Ln", str(lbl),
         "--asm-define", "DOSIO_UCI_FENCE_OUTER=1", "--asm-define", "DOSIO_UCI_FENCE_INNER=1",
         "--asm-define", "DOSIO_UCI_POLLS_HI=4",
         str(d / "runtime_copy/ops/dosio_uci.s"), str(d / "shim.s"),
         str(d / "runtime_copy/ops/ureu.s")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    lab = {}
    for line in lbl.read_text().splitlines():
        p = line.split()
        if len(p) == 3 and p[0] == "al":
            lab[p[2].lstrip(".")] = int(p[1], 16)
    n = CHUNK + 100
    h = H((out.read_bytes(), lab), fs={"short.bin": bytearray(pattern(n - 1))})
    assert h.run("ureu_ld", "short.bin", 0, n) == 0, "mutant must look green"
    # ... and the real build refuses the same case (test above): killed.
