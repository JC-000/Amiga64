"""The file path of suspend/resume (snap.s snap_req bit 6) at host level.

snap.s, ureu.s and dosio_uci.s are assembled standalone with a shim (the
runtime symbols snap.s imports: mach_run/mach_rdc/emu_reset/disp_init/
mem_rec_dirty as counting stubs, mach_fhook/mach_now storage, the dosio_*
block) and RUN in the 6502 emulator of test_hle_dos_uci.py.  The bus adds
a model of the REU controller ($DF01-$DF0A: execute, stash/fetch, 0 = 64 KB)
over the same 16 MB REU that the UCI model of test_snap_ureu.py serves
LOAD_REU/SAVE_REU from (the firmware's real reply format, small_printf.cc).

The hardware has not run this path yet (device storage writes are blocked
until the harness owner decides): this pins the exact sequence -- the
spans written in order, a file that resumes, its refusals, M1 (a resumed or
continued REU snapshot is consumed) and M3 (an all-zero ROM id refuses).
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from host import snapfmt as F
from host.tests import test_hle_dos_uci as U
from host.tests import test_snap_ureu as R

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("cl65") is None,
                                reason="cl65 (cc65) not installed")

SHIM = U.SHIM + """
.export mach_fhook, mach_now, mach_rdc, mach_run, emu_reset, disp_init
.export mem_rec_dirty, calls, code_invall, spr_vec, inp_mon
.segment "BSS"
mach_fhook: .res 2
mach_now:   .res 4
.segment "SBSS"               ; (after all BSS: outside the image)
calls:      .res 6            ; mach_rdc, mach_run, emu_reset, disp_init, rec_dirty, code_invall
spr_vec:    .res 2            ; W13-D: snap.s disarms it on resume (outside the image here)
inp_mon:    .res 1            ; W14-M: snap.s disarms the input module on resume (supervisor, at the merge)
.segment "CODE"
mach_rdc:   inc calls+0
            rts
mach_run:   inc calls+1
            rts
emu_reset:  inc calls+2
            rts
disp_init:  inc calls+3
            rts
mem_rec_dirty:
            inc calls+4
            rts
code_invall:
            inc calls+5
            rts
"""

CFG = """
MEMORY {
    ZP:   start = $0080, size = $0070, type = rw;
    MAIN: start = $1000, size = $5000, file = %O;
    BSSM: start = $6000, size = $2000, type = rw;
}
SEGMENTS {
    ZEROPAGE: load = ZP, type = zp, optional = yes;
    CODE:     load = MAIN, type = ro;
    LOWCODE:  load = MAIN, type = ro, optional = yes;   # Wave 13: ureu.s
    SCODE:    load = MAIN, type = ro;
    RODATA:   load = MAIN, type = ro, optional = yes;
    DATA:     load = MAIN, type = rw, optional = yes;
    BSS:      load = BSSM, type = bss, optional = yes, define = yes;
    SBSS:     load = BSSM, type = bss, optional = yes;
}
"""

DISP_ST = 0xB800
RID = hashlib.sha256(b"a ROM").digest()
BID = bytes(range(0x11, 0x19))


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("snapf"))


@pytest.fixture(scope="module")
def build_noscr(tmp_path_factory):
    """Mutant SNAP_MUT_NOSCR: a refusal does not put the scratch page back."""
    return _build(tmp_path_factory.mktemp("snapf-noscr"), ["SNAP_MUT_NOSCR=1"])


def _build(d, defines=()):
    # as in the runner since W12-C: the 256-byte resume scratch at
    # __BSS_RUN__ overlaps dosio_uci.s's BSS, which the file loads use
    # (sized so dosio_uci_max, written by every LOAD_REU chunk, lies exactly
    # on the copy's state sum, header +96: test_layout_... pins it)
    (d / "first.s").write_text('.segment "BSS"\nbss0: .res $32\n')
    (d / "pad.s").write_text('.segment "BSS"\nbss1: .res $20\n')
    (d / "shim.s").write_text(SHIM)
    (d / "t.cfg").write_text(CFG)
    out, lbl = d / "s.bin", d / "s.lbl"
    r = subprocess.run(
        ["cl65", "-t", "none", "-C", str(d / "t.cfg"), "-o", str(out), "-Ln", str(lbl),
         "--asm-define", "DOSIO_UCI_FENCE_OUTER=1", "--asm-define", "DOSIO_UCI_FENCE_INNER=1",
         "--asm-define", "DOSIO_UCI_POLLS_HI=4",
         *[x for dd in defines for x in ("--asm-define", dd)],
         str(d / "first.s"), str(ROOT / "runtime/ops/dosio_uci.s"), str(d / "pad.s"),
         str(d / "shim.s"),
         str(ROOT / "runtime/ops/snap.s"), str(ROOT / "runtime/ops/ureu.s")],
        capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    return out.read_bytes(), F.labels(str(lbl))


class RecBus(U.Bus):
    """+ the REU controller: $DF02-$DF08 latched, a write of bit 7 to $DF01
    executes (bits 1-0: 0 stash C64 -> REU, 1 fetch REU -> C64)."""

    def __init__(self, dev):
        super().__init__(dev)
        self.rec = bytearray(16)
        self.dmas = []

    def write(self, a, v):
        if 0xDF00 <= a <= 0xDF0A:
            self.rec[a - 0xDF00] = v
            if a == 0xDF01 and v & 0x80:
                c = self.rec[2] | self.rec[3] << 8
                r = self.rec[4] | self.rec[5] << 8 | self.rec[6] << 16
                n = (self.rec[7] | self.rec[8] << 8) or 0x10000
                assert self.rec[0x0A] == 0, "address control must be 0"
                assert c + n <= 0x10000 and r + n <= len(self.dev.reu)
                if v & 3 == 0:
                    self.dev.reu[r:r + n] = self.ram[c:c + n]
                elif v & 3 == 1:
                    self.ram[c:c + n] = self.dev.reu[r:r + n]
                else:
                    raise AssertionError(f"REC command ${v:02X}")
                self.dmas.append((v & 3, c, r, n))
            return
        super().write(a, v)


class Rig(U.Harness):
    def __init__(self, build, fs=None):
        self.image, self.lab = build
        self.dev = R.ReuDevice(fs=fs if fs is not None else {}, latency=2)
        self.bus = RecBus(self.dev)
        self.bus.ram[0x1000:0x1000 + len(self.image)] = self.image
        self.bus.ram[0x01] = 0x37
        self.cpu = U.CPU(self.bus)
        self.poke("snap_bid", BID)
        self.reu_w(F.SNAP_BASE + F.OFF_RID, RID)

    # --- helpers
    def reu_w(self, a, b):
        self.dev.reu[a:a + len(b)] = b

    def reu_r(self, a, n):
        return bytes(self.dev.reu[a:a + n])

    def name(self, s: str):
        self.poke("dosio_name", s.encode())
        self.poke("dosio_nlen", bytes([len(s)]))

    @property
    def keep(self):
        return self.lab["snap_keep"] - self.lab["__BSS_RUN__"]

    def image_range(self):
        return self.lab["__BSS_RUN__"], self.lab["snap_keep"]

    def st(self):
        return self.peek("snap_st", 1)[0]

    def suspend(self, rq):
        self.poke("snap_req", bytes([rq]))
        self.call("snap_fhook", limit=60_000_000)

    def resume(self, rq):
        self.poke("snap_req", bytes([rq]))
        self.poke("snap_st", b"\0")
        return self.call("snap_resume", limit=60_000_000)


def pat(n, k):
    return bytes((i * k + 7) & 0xFF for i in range(n))


def machine_state(r: Rig):
    """A recognisable C64 state + guest memory."""
    lo, hi = r.image_range()
    r.bus.ram[lo:hi] = pat(hi - lo, 13)
    r.poke("snap_chain", b"\0\0")                    # no chained frame hook
    r.bus.ram[0x10:0x9A] = pat(0x8A, 5)
    r.bus.ram[DISP_ST:DISP_ST + 256] = pat(256, 3)
    r.poke("mach_now", struct.pack("<I", 0x12345678))
    for i, (a, n) in enumerate(F.RANGES):
        r.reu_w(a, pat(n, 17 + i))


def test_layout_scratch_overlaps_uci_state(build):
    """The fixture reproduces the runner's hazard (dosio_uci's BSS inside
    the 256-byte scratch at __BSS_RUN__), so the resume tests below run
    with the UCI loads writing into the loaded-header copy."""
    _, lab = build
    lo = lab["__BSS_RUN__"]
    assert lab["dosio_uci_max"] == lo + F.H_SSUM
    assert lab["dosio_buf"] >= lo + 0x100


def test_file_suspend_writes_the_spans_in_order(build):
    r = Rig(build)
    machine_state(r)
    r.name("snap.a64")
    lo, hi = r.image_range()
    img = bytes(r.bus.ram[lo:hi])
    r.suspend(F.RQ_SUS | F.RQ_FILE | F.RQ_CONT)
    assert r.st() == F.ST_SUSF
    f = bytes(r.dev.fs["snap.a64"])
    sp = F.spans(r.keep)
    assert len(f) == sum(n for _, n in sp)
    h = F.parse_header(f[:256])
    assert h["sum_ok"] and h["magic"] == b"A64S" and h["bid"] == BID and h["rid"] == RID
    assert h["flags"] == F.RQ_FILE and h["spans"] == sp and h["keep"] == r.keep
    state = f[256:256 + sp[1][1]]
    assert F.fletcher(state) == h["state_sum"]
    assert state[F.OFF_BSS - 0x100:F.OFF_BSS - 0x100 + r.keep] == img
    assert state[0x10:0x9A] == pat(0x8A, 5)
    o = 256 + sp[1][1]
    for i, (a, n) in enumerate(F.RANGES):
        assert f[o:o + n] == pat(n, 17 + i), f"range {i}"
        o += n
    # the SAVE_REU commands: the spans, in order, chunked
    cmds = [m for m in r.dev.reu_cmds if m[1] == R.SAVE]
    want = [c for a, n in sp for c in R.expect_chunks(R.SAVE, a, n)]
    assert cmds == want
    # M1: the guest continues -> the REU copy is consumed
    assert r.reu_r(F.SNAP_BASE, 4) != b"A64S"
    assert r.dev.targets[1].file is None                 # closed
    U.assert_clean(r)


def suspended_file(build) -> tuple[bytes, Rig]:
    r = Rig(build)
    machine_state(r)
    r.name("snap.a64")
    lo, hi = r.image_range()
    r.img = bytes(r.bus.ram[lo:hi])                    # the image as suspended
    r.suspend(F.RQ_SUS | F.RQ_FILE | F.RQ_CONT)
    assert r.st() == F.ST_SUSF
    return bytes(r.dev.fs["snap.a64"]), r


def fresh(build, fbytes: bytes) -> Rig:
    """Another power-on: zero REU (but the declared ROM id), scrambled C64."""
    r = Rig(build, fs={"snap.a64": bytearray(fbytes)})
    lo, hi = r.image_range()
    r.bus.ram[lo:hi] = bytes([0x55]) * (hi - lo)
    r.bus.ram[0x10:0x9A] = bytes([0xAA]) * 0x8A
    r.bus.ram[DISP_ST:DISP_ST + 256] = bytes(256)
    r.poke("calls", bytes(6))
    r.name("snap.a64")
    return r


def test_file_resume_restores_everything(build):
    f, r0 = suspended_file(build)
    lo, hi = r0.image_range()
    r = fresh(build, f)
    assert r.resume(F.RQ_FILE) == 0
    assert r.st() == F.ST_RES
    assert r.peek("calls", 5)[1] == 1                  # mach_run entered
    assert r.peek("calls", 5)[2] == 1 and r.peek("calls", 5)[3] == 1   # emu_reset, disp_init
    assert r.peek("calls", 6)[5] == 1                  # I1: code_invall, after the restore
    assert bytes(r.bus.ram[lo:hi]) == r0.img
    assert bytes(r.bus.ram[0x10:0x80]) == pat(0x8A, 5)[:0x70]
    assert bytes(r.bus.ram[0x88:0x9A]) == pat(0x8A, 5)[0x78:]
    d = pat(256, 3)
    want = d[:105] + bytes([(d[105] + 1) & 0xFF]) + d[106:]           # I2
    assert bytes(r.bus.ram[DISP_ST:DISP_ST + 256]) == want
    for i, (a, n) in enumerate(F.RANGES):
        assert r.reu_r(a, n) == pat(n, 17 + i), f"range {i}"
    cmds = [m for m in r.dev.reu_cmds if m[1] == R.LOAD]
    sp = F.spans(r.keep)
    assert cmds == [c for a, n in sp for c in R.expect_chunks(R.LOAD, a, n)]
    assert r.dev.targets[1].file is None
    # CIA #2 time base := ~mach_now (then the latches $FFFF: the RAM model
    # keeps the last write, so check the order of writes instead)
    assert r.dev.violations == []
    # M1: consumed -- a second (REU-resident) resume refuses
    assert r.resume(0) == 1 and r.st() == 0xE2
    U.assert_clean(r)


def test_reu_resident_resume_then_consumed(build):
    r = Rig(build)
    machine_state(r)
    r.suspend(F.RQ_SUS | F.RQ_CONT)                    # REU only, continue
    assert r.st() == F.ST_SUS
    assert r.reu_r(F.SNAP_BASE, 4) != b"A64S"          # M1: consumed at once
    assert r.resume(0) == 1 and r.st() == 0xE2


def test_frozen_suspend_spins_with_the_snapshot_intact(build):
    r = Rig(build)
    machine_state(r)
    lo, hi = r.image_range()
    img = bytes(r.bus.ram[lo:hi])
    r.poke("snap_req", bytes([F.RQ_SUS]))
    with pytest.raises(AssertionError, match="did not return"):
        r.call("snap_fhook", limit=2_000_000)
    assert r.st() == F.ST_SUS and r.reu_r(F.SNAP_BASE, 4) == b"A64S"
    # REU-resident resume (same machine: REU kept, C64 state scrambled)
    lo, hi = r.image_range()
    r.bus.ram[lo:hi] = bytes(hi - lo)
    assert r.resume(0) == 0 and r.st() == F.ST_RES
    assert bytes(r.bus.ram[lo:hi]) == img


@pytest.mark.parametrize("case,code", [
    ("header byte", 0xE2), ("state byte", 0xE5), ("truncated", 0xE7),
    ("rom id", 0xE4), ("zero rom id", 0xEA), ("missing", 0xE6), ("build id", 0xE3),
    ("version 1", 0xE1)])                               # W13-W: a SNAP_VER 1 file
def test_file_refusals(build, case, code):
    f, _ = suspended_file(build)
    f = bytearray(f)
    if case == "header byte":
        f[F.H_FLAGS] ^= 0x01
    elif case == "version 1":                          # W13-W: Wave 12's layout, consistently summed
        f[4] = 1
        struct.pack_into("<I", f, 10 + 2 * 7 + 3, 0x200000)    # its chip span
        struct.pack_into("<I", f, F.HDR_SUM, F.fletcher(bytes(f[:F.HDR_SUM])))
    elif case == "state byte":
        f[256 + 0x340] ^= 0xFF
    elif case == "truncated":
        f = f[:-1000]
    r = fresh(build, bytes(f))
    if case == "rom id":
        r.reu_w(F.SNAP_BASE + F.OFF_RID, hashlib.sha256(b"another ROM").digest())
    elif case == "zero rom id":
        r.reu_w(F.SNAP_BASE + F.OFF_RID, bytes(32))
    elif case == "missing":
        r.dev.fs.clear()
    elif case == "build id":
        r.poke("snap_bid", bytes([BID[0] ^ 1]) + BID[1:])
    scr = r.lab["__BSS_RUN__"]
    page = bytes(r.bus.ram[scr:scr + 0x100])
    assert r.resume(F.RQ_FILE) == 1
    # F1: the scratch page (cw_*, dosio_uci's state in the runner) is
    # byte-identical after every refusal, whether the header fetch ran or not
    assert bytes(r.bus.ram[scr:scr + 0x100]) == page, "scratch page changed"
    assert r.st() == code, f"${r.st():02X}"
    c = r.peek("calls", 5)                             # nothing restored:
    assert c[1] == 0 and c[2] == 0 and c[3] == 0       # no mach_run/emu_reset/disp_init
    assert bytes(r.bus.ram[0x10:0x9A]) == bytes([0xAA]) * 0x8A
    assert bytes(r.bus.ram[DISP_ST:DISP_ST + 256]) == bytes(256)
    assert r.dev.targets[1].file is None               # the file is closed
    U.assert_clean(r)


REU_CASES = [("header byte", 0xE2), ("state byte", 0xE5), ("rom id", 0xE4),
             ("zero rom id", 0xEA), ("build id", 0xE3), ("unstamped", 0xE8),
             ("layout", 0xE2)]


def reu_refusal(build, case) -> tuple[Rig, bytes, bytes]:
    """A REU-resident snapshot (frozen suspend), then one corruption, then
    snap_resume: -> (rig, scratch page before, after)."""
    r = Rig(build)
    machine_state(r)
    r.poke("snap_req", bytes([F.RQ_SUS]))
    with pytest.raises(AssertionError, match="did not return"):
        r.call("snap_fhook", limit=2_000_000)
    if case == "header byte":
        r.dev.reu[F.SNAP_BASE + F.H_FLAGS] ^= 0x01
    elif case == "state byte":
        r.dev.reu[F.SNAP_BASE + F.OFF_BSS + 0x40] ^= 0xFF
    elif case == "rom id":
        r.reu_w(F.SNAP_BASE + F.OFF_RID, hashlib.sha256(b"another ROM").digest())
    elif case == "zero rom id":
        r.reu_w(F.SNAP_BASE + F.OFF_RID, bytes(32))
    elif case == "build id":
        r.poke("snap_bid", bytes([BID[0] ^ 1]) + BID[1:])
    elif case == "unstamped":
        r.poke("snap_bid", bytes(8))
    elif case == "layout":
        r.dev.reu[F.SNAP_BASE + 6] ^= 0x01              # KEEP (also breaks the sum)
    scr = r.lab["__BSS_RUN__"]
    r.bus.ram[scr:scr + 0x100] = bytes((i * 37 + 11) & 0xFF for i in range(0x100))
    before = bytes(r.bus.ram[scr:scr + 0x100])
    assert r.resume(0) == 1
    return r, before, bytes(r.bus.ram[scr:scr + 0x100])


@pytest.mark.parametrize("case,code", REU_CASES)
def test_reu_refusals_leave_the_scratch_page(build, case, code):
    """F1: a refused REU-resident resume leaves the scratch page (W12-C's
    cw_val/cw_hand/cw_mok ... in the runner) byte for byte as it was."""
    r, before, after = reu_refusal(build, case)
    assert r.st() == code, f"${r.st():02X}"
    assert after == before


def test_mutant_noscr_is_caught(build_noscr):
    """Mutant SNAP_MUT_NOSCR (<= 10 per package): the refusal leaves the
    loaded header in the scratch page -- the test above must see it."""
    r, before, after = reu_refusal(build_noscr, "header byte")
    assert r.st() == 0xE2
    assert after != before, "mutant must change the page"
    assert after[:4] == b"A64S"                         # the loaded header


def test_unstamped_build_refuses_before_any_uci(build):
    f, _ = suspended_file(build)
    r = fresh(build, f)
    r.poke("snap_bid", bytes(8))
    assert r.resume(F.RQ_FILE) == 1 and r.st() == 0xE8
    assert r.dev.log == [b"\x01\x03"]                  # no OPEN: only the failure close


# --- Wave 13 (supervisor; rev-vis round 3): a resume disarms the pointer module
def test_file_resume_disarms_the_pointer_module(build):
    f, _r0 = suspended_file(build)
    r = fresh(build, f)
    r.poke("spr_vec", b"\x00\xC6")                 # armed (outside the image in this shim)
    assert r.resume(F.RQ_FILE) == 0 and r.st() == F.ST_RES
    assert r.peek("spr_vec", 2)[1] == 0


def test_refused_resume_leaves_spr_vec(build):
    f, _r0 = suspended_file(build)
    f = bytearray(f)
    f[300] ^= 1                                    # a state byte: refused before the restore
    r = fresh(build, bytes(f))
    r.poke("spr_vec", b"\x00\xC6")
    assert r.resume(F.RQ_FILE) == 1
    assert r.peek("spr_vec", 2)[1] == 0xC6


# --- Wave 14 (supervisor): a resume disarms the input module (W14-M M2) as it
# does the pointer module; the launcher re-arms (tests/aros_wb.arm_inpm).
# The snap.s store (inp_mon := 0 next to spr_vec+1) is the supervisor's at
# the merge: until then the disarm test is an expected failure (strict: it
# must be flipped when the store lands).
@pytest.mark.xfail(strict=True, reason="snap.s inp_mon store: supervisor, at the W14-M merge")
def test_file_resume_disarms_the_input_module(build):
    f, _r0 = suspended_file(build)
    r = fresh(build, f)
    r.poke("inp_mon", b"\x5A")                    # armed (outside the image in this shim)
    assert r.resume(F.RQ_FILE) == 0 and r.st() == F.ST_RES
    assert r.peek("inp_mon", 1)[0] == 0


def test_refused_resume_leaves_inp_mon(build):
    f, _r0 = suspended_file(build)
    f = bytearray(f)
    f[300] ^= 1                                    # a state byte: refused before the restore
    r = fresh(build, bytes(f))
    r.poke("inp_mon", b"\x5A")
    assert r.resume(F.RQ_FILE) == 1
    assert r.peek("inp_mon", 1)[0] == 0x5A
