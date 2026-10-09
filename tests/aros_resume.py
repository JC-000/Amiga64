#!/usr/bin/env python3
"""W12-T: suspend the AROS boot, reset, resume from a fresh runner load.

docs/WAVE12_BRIEF.md "W12-T", contract docs/CONTRACTS.md "Suspend/resume",
format host/snapfmt.py, C64 side runtime/ops/snap.s (+ ureu.s for files).

Every leg boots AROS exactly as tests/aros_boot.py does (zeroed REU, the
ROM banks staged and verified, aros_boot), with tests/aros/mboot.s calling
snap_arm before mach_run (its W12-T line).  The host declares the ROM id (rom_id, into the
REU at snapfmt.OFF_RID) and asks for a suspend through snap_req; snap.s takes the
machine state into the REU snapshot area at the next frame hook and
freezes (sei).  Then the machine is RESET and the runner loaded FRESH
(VICE: harness reset + run_prg_via_sys; U64: harness reset + run_prg), the
ROM id written again and snap_resume launched.  REU-resident: the guest
ranges and the snapshot stay in the REU across the reset.  The file path
(snap_req bit 6: SAVE_REU/LOAD_REU) is leg m3file (U64E: device storage
writes allowed there); host/tests/test_snap_file.py pins it on the host.

Legs:
  mid    suspend early in the boot, at guest frame SUSPEND_FRAME (before
         the first serial byte on every backend), resume: ALL the serial
         text then comes after the resume and must equal the boot model's
         byte for byte through "romtaginit done" (m2), and m3's
         insert-disk screen hash must then come
  m3     boot to m3 (the insert-disk screen hash), suspend, resume: the
         hash must match a model insert-disk frame again within
         M3_BUDGET_S (15 s, the Ultimate) of launching the resume (VICE:
         M3_BUDGET_VICE_S, warp speed is the host's; the time is reported).  Before the resume,
         the refusals (red, each must leave the fresh runner untouched):
           E8 unstamped build (snap_bid zeroed in RAM)
           E3 another build (one snap_bid byte flipped in RAM)
           E4 another ROM id
           E2 one header byte corrupted in the REU (the flags byte, which
              the layout compare skips: only the checksum sees it)
           E5 one byte of the state image corrupted in the REU
  m3file (Ultimate) variant B: suspend at m3 to <vol>/amiga64/snap-test.bin
         (--vol, default /USB1: the U64E; the C64U: /SD),
         the file read back over FTP and checked, reset + fresh load, the
         WHOLE REU zeroed, resume from the file, picture within budget;
         the test file deleted
  (VICE, both legs: right after the resume W12-C's code-window directory
  must agree with its page-low map, cwin_check: I1)
  --mutants  snap.s test-only variants (<= 10 with ureu.s's host mutant),
         each built once and stamped with the base build's ID (only SCODE
         differs; the MAIN bytes are checked equal): a block left out of
         the restore must fail its continuation check; a check removed
         must accept the corruption it exists for.

    python3 -m tests.aros_resume --prg build/X/runner.prg [--legs mid,m3]
           [--backend vice|u64] [--mhz 64] [--out DIR] [--mutants]

Exit 0 = every selected leg passed (or SKIP without the ROM cache).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from host import machine  # noqa: E402
from host import snapfmt as F  # noqa: E402
from tests import aros_boot as AB  # noqa: E402

EVID = os.path.join(ROOT, "build", "evidence", "w12-reu")
SUSPEND_FRAME = 50              # mid: the suspend request (the model's first serial line is at frame 95)
M3_BUDGET_S = 15.0              # brief: m3's hash again within 15 s (the Ultimate)
M3_BUDGET_VICE_S = 60.0         # VICE: a 1 MHz C64 in warp (host-dependent), reported
STUB = 0xCC00                   # host REU access stub (buf_result, free in machine mode; W13-W: was $C700, now the td/spr modules' area)
SBUF = 0xCB00                   # its data page (buf_result, unused here)
RUN_TIMEOUT = 900.0
INFRA = AB.INFRA
log = AB.log


def rom_id(rom: machine.Rom) -> bytes:
    """The ROM id the host declares (I3): SHA-256 of ext bank || ROM bank."""
    return hashlib.sha256(rom.ext + rom.rom).digest()


# --------------------------------------------------------------------------
# the driver: mboot.s with snap_arm before mach_run
# --------------------------------------------------------------------------

def setup_driver(out_dir: str) -> None:
    """tests/aros/mboot.s calls snap_arm (W12-T line); the driver is built
    once per label file (parallel mutant sessions share the base PRG, so
    they would race on aros-drv/)."""
    import threading
    if "jsr snap_arm" not in open(AB.DRV_SRC).read() or "snap_arm" not in AB.DRV_SYMS:
        raise RuntimeError("tests/aros/mboot.s must call snap_arm (W12-T)")
    if getattr(AB.build_driver, "_snap_cached", False):
        return
    real, lock, cache = AB.build_driver, threading.Lock(), {}

    def cached(labels_path, out, src=None):
        with lock:
            k = (os.path.abspath(labels_path), src)
            if k not in cache:
                cache[k] = real(labels_path, out, src)
            return cache[k]
    cached._snap_cached = True
    AB.build_driver = cached


# --------------------------------------------------------------------------
# host helpers on a prepared Machine
# --------------------------------------------------------------------------

class Snap:
    def __init__(self, m: AB.Machine, prg: str, rid: bytes):
        self.m, self.prg, self.rid = m, os.path.abspath(prg), rid
        L = m.lab
        self.a_req, self.a_st = L("snap_req"), L("snap_st")
        self.a_bid = L("snap_bid")
        self.a_dirty = L("mem_rec_dirty")

    def st(self) -> int:
        return self.m.r(self.a_st, 1)[0]

    def wait_st(self, done, timeout: float) -> int:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            v = self.st()
            if done(v):
                return v
            time.sleep(0.05 if not self.m.vice else 0.25)
        return self.st()

    def declare_rid(self, rid: bytes | None = None) -> None:
        """The ROM id lives in the REU (snapfmt.OFF_RID): the stager's."""
        self.reu_write(F.SNAP_BASE + F.OFF_RID, rid if rid is not None else self.rid)

    def request(self, rq: int) -> None:
        self.m.w(self.a_req, bytes([rq]))

    def launch(self, addr: int) -> None:
        """Start a never-returning entry (as aros_boot.Machine.start)."""
        m = self.m
        if m.vice:
            from c64_test_harness import goto
            m.w(AB.TRAMP, bytes([0x20, addr & 0xFF, addr >> 8,
                                 0x4C, AB.TRAMP + 3 & 0xFF, (AB.TRAMP + 3) >> 8]))
            goto(m.t, AB.TRAMP)
        else:
            from c64_test_harness import write_bytes
            disp = m.tg._u64_dispatch
            write_bytes(m.t, disp["jsr_operand"], bytes([addr & 0xFF, addr >> 8]))
            write_bytes(m.t, disp["mbox"], bytes([0x01, 0x00]))

    def resume(self, rq: int = 0, timeout: float = 120.0) -> tuple[int, float]:
        """Launch snap_resume; -> (snap_st when it settled, seconds)."""
        self.request(rq)
        self.m.w(self.a_st, b"\0")
        t0 = time.monotonic()
        self.launch(self.m.lab("snap_resume"))
        v = self.wait_st(lambda v: v == F.ST_RES or v in F.ST_ERR, timeout)
        return v, time.monotonic() - t0

    def _rec(self, cmd: int, addr: int, n: int) -> None:
        from tests.harness_util import call_runner
        code = bytes([
            0xA9, SBUF & 0xFF, 0x8D, 0x02, 0xDF, 0xA9, SBUF >> 8, 0x8D, 0x03, 0xDF,
            0xA9, addr & 0xFF, 0x8D, 0x04, 0xDF, 0xA9, (addr >> 8) & 0xFF, 0x8D, 0x05, 0xDF,
            0xA9, addr >> 16, 0x8D, 0x06, 0xDF, 0xA9, n & 0xFF, 0x8D, 0x07, 0xDF,
            0xA9, n >> 8, 0x8D, 0x08, 0xDF, 0xA9, 0x00, 0x8D, 0x0A, 0xDF,
            0xA9, cmd, 0x8D, 0x01, 0xDF,
            0x20, self.a_dirty & 0xFF, self.a_dirty >> 8, 0x60])
        self.m.w(STUB, code)
        call_runner(self.m.tg, STUB, 30.0)

    def reu_read(self, addr: int, n: int) -> bytes:
        assert 0 < n <= 256
        self._rec(0x91, addr, n)
        return self.m.r(SBUF, n)

    def reu_write(self, addr: int, data: bytes) -> None:
        assert 0 < len(data) <= 256
        self.m.w(SBUF, data)
        self._rec(0x90, addr, len(data))


def fresh_load(m: AB.Machine, prg: str) -> None:
    """Reset the machine and load *prg* fresh (BSS cleared, DATA/code from
    the file).  The REU is not touched by either."""
    from c64_test_harness import write_bytes
    from tests.harness_util import wait_ready
    prg = os.path.abspath(prg)
    write_bytes(m.t, 0xCFFF, b"\0")             # the ready flag survives a reset
    if m.vice:
        # the harness's run_prg_via_sys reads the body back through the CPU
        # map, so it sees BASIC ROM at $A000 and refuses: load it here --
        # monitor writes land in the RAM under ROM -- and start the BASIC
        # stub's SYS target ($080D) once the KERNAL has come up (READY.)
        from c64_test_harness import goto, read_bytes
        raw = open(prg, "rb").read()
        load = raw[0] | raw[1] << 8
        m.t.reset("cpu")
        ready = bytes([18, 5, 1, 4, 25, 46])           # "READY." screen codes
        t0 = time.monotonic()
        while ready not in read_bytes(m.t, 0x0400, 1000):
            m.t.resume()
            if time.monotonic() - t0 > 30:
                raise RuntimeError("VICE: no READY. after the reset")
            time.sleep(0.25)
        for a in range(0, len(raw) - 2, 0x800):
            m.w(load + a, raw[2 + a:2 + a + 0x800])
        top = 0xA000 - load
        if m.r(load, top) != raw[2:2 + top]:
            raise RuntimeError("VICE: PRG readback mismatch below $A000")
        goto(m.t, 0x080D)
        wait_ready(m.t)
    else:
        from c64_test_harness.backends.ultimate64_helpers import reset
        from tests.run_vectors import prepare_target
        reset(m.t.client)
        prepare_target(m.tg, prg)
    m.load_driver()
    scrub(m)


SCRUB_PAGES = ((0xA9, 0xB0),     # W12-C code windows $A900-$AFFF (invalid on a fresh load)
               (0xB6, 0xC0),     # blt rows, disp_st, c2v tables/args
               (0xE0, 0xFF))     # the VIC bitmap


def scrub_code(base: int) -> bytes:
    """6502: fill every page range of SCRUB_PAGES with $55 (stores under
    ROM land in RAM), rts."""
    code = bytearray([0xA9, 0x55, 0xA0, 0x00])          # lda #$55 / ldy #0
    for lo, hi in SCRUB_PAGES:
        at = base + len(code)
        code += bytes([0x99, 0x00, lo,                  # sta lo00,y
                       0xC8, 0xD0, 0xFA,                # iny / bne
                       0xEE, (at + 2) & 0xFF, (at + 2) >> 8,   # inc hi
                       0xAE, (at + 2) & 0xFF, (at + 2) >> 8,   # ldx hi
                       0xE0, hi, 0xD0, 0xF0])           # cpx #hi / bne
    return bytes(code + b"\x60")


def scrub(m: AB.Machine) -> None:
    """Fill the runtime's RAM outside the PRG's meaningful bytes with $55:
    RAM outlives a reset, and another power-on has none of it; the W12-C
    windows load as zeros but are don't-care until a fetch fills one (a
    resume that left stale window tags valid would execute this)."""
    from tests.harness_util import call_runner
    m.w(STUB, scrub_code(STUB))
    call_runner(m.tg, STUB, 30.0)


def serial_text(m: AB.Machine, seen: int, text: bytearray) -> int:
    n, ring = m.serial(seen)
    seen, lost = AB.take(text, ring, seen, n, m.sersz)
    if lost:
        raise AB.CaptureError(f"{lost} serial bytes lost")
    return seen


def boot(m: AB.Machine, rom, s: int, rep: dict, sn: "Snap") -> None:
    rep["stage"] = m.stage(rom)                 # zero-fills the whole REU
    log(f"  staged: {rep['stage']}")
    sn.declare_rid()                            # then the ROM id (REU)
    m.start(s)


def suspend(m: AB.Machine, sn: Snap, rep: dict, timeout: float = 120.0) -> int:
    """Request a REU-resident suspend; -> the chip_sern when frozen."""
    t0 = time.monotonic()
    sn.request(F.RQ_SUS)
    v = sn.wait_st(lambda v: v == F.ST_SUS or v in F.ST_ERR, timeout)
    rep["suspend_s"] = round(time.monotonic() - t0, 2)
    rep["suspend_st"] = f"${v:02X}"
    if v != F.ST_SUS:
        raise RuntimeError(f"suspend did not complete: snap_st ${v:02X}")
    rep["frozen_frame"] = m.frames()
    log(f"  suspended (frozen) in {rep['suspend_s']} s at guest frame {rep['frozen_frame']}")
    return m.count()


def fast_hash(m: AB.Machine) -> str:
    """aros_boot.c64_hash (W12-V owns its composition), with the Ultimate's
    reads done as one REST GET per <= 1024 bytes (transport.read_memory,
    docs/HARDWARE.md) instead of read_bytes' 128-byte GETs: ~1.5 s per
    picture on the C64U instead of ~8 s."""
    if m.vice:
        return AB.c64_hash(m)[0]
    import c64_test_harness as H
    real = H.read_bytes

    def fast(t, addr, n):
        out = bytearray()
        while len(out) < n:
            k = min(1024, n - len(out))
            b = t.read_memory(addr + len(out), k)
            if len(b) != k:
                return real(t, addr, n)
            out += b
        return bytes(out)
    H.read_bytes = fast
    try:
        return AB.c64_hash(m)[0]
    finally:
        H.read_bytes = real


def hash_wait(m: AB.Machine, ok: set, t0: float, timeout: float) -> float | None:
    """Seconds from t0 until the C64 picture hashes into *ok*, or None.
    Between pictures only disp_nconv is polled (one short read): a picture
    is hashed when a conversion has finished since the last one, and at
    least every 3 s (a torn hash of a conversion in progress heals)."""
    a = m.lab("disp_nconv")
    last, t_h = None, -1e9
    while time.monotonic() - t0 < timeout:
        n = m.r(a, 2)
        if n != last or time.monotonic() - t_h > 3.0:
            last, t_h = n, time.monotonic()
            if fast_hash(m) in ok:
                return time.monotonic() - t0
        time.sleep(0.25 if m.vice else 0.05)
    return None


def model_sets(m: AB.Machine) -> tuple[str, set]:
    mf, why = AB.model_frames()
    if mf is None:
        raise RuntimeError(f"no model frames: {why}")
    key = (lambda h, v: h) if m.vice else (lambda h, v: v["u64"])   # = aros_boot.leg_m3 (W12-V)
    sys.path.insert(0, os.path.join(ROOT, "tests", "aros"))
    import model_frames as MF
    ok = {key(h, v) for h, v in mf["hashes"].items()     # F8: aros_boot's guard
          if v["frame"] >= mf["insert_disk_frame"] and v.get("visible", 0) >= MF.MIN_VISIBLE}
    return mf["serial"], ok


# --------------------------------------------------------------------------
# legs
# --------------------------------------------------------------------------

def cwin_check(m: AB.Machine, rep: dict, tries: int = 4) -> list[str]:
    """VICE (BHI is not host-readable on the Ultimate): right after a resume
    W12-C's directory must agree with its page-low map (core.s "W12-C
    code-window cache": bits 2-0 = valid windows with page-low p, bit 7 =
    any, bit 6 = any with page-low p+1; valid tags unique).  The image
    restores cw_val/tags but not the map, so only code_invall after the
    restore (I1) makes them agree.  Read in one monitor stop (the guest
    halted); an inconsistent read is retried (a stop inside cw_drop), a
    persistent one fails."""
    from c64_test_harness import read_bytes
    n = m.lab("CWIN_N")
    bad = ""
    for _ in range(tries):
        hand = read_bytes(m.t, m.lab("cw_hand"), 1)[0]
        val = read_bytes(m.t, m.lab("cw_val"), n)
        tlo = read_bytes(m.t, m.lab("cw_tlo"), n)
        thi = read_bytes(m.t, m.lab("cw_thi"), n)
        mp = read_bytes(m.t, m.lab("cwin_map"), 256)     # $01 = $35 in mach_run
        m.t.resume()
        lows = [tlo[k] for k in range(n) if val[k]]
        tags = [(tlo[k], thi[k]) for k in range(n) if val[k]]
        bad = ""
        if hand >= n:
            bad = f"cw_hand {hand} >= CWIN_N {n}"
        if len(set(tags)) != len(tags):
            bad = f"duplicate valid tags {tags}"
        for p in range(256):
            c, c1 = lows.count(p), lows.count((p + 1) & 0xFF)
            if (mp[p] & 7) != c or bool(mp[p] & 0x80) != (c > 0) or bool(mp[p] & 0x40) != (c1 > 0):
                bad = bad or (f"page-low ${p:02X}: map ${mp[p]:02X}, {c} valid window(s) "
                              f"(+1: {c1})")
        if not bad:
            rep["cwin_check"] = {"valid": len(tags), "ok": True}
            return []
        time.sleep(0.1)
    rep["cwin_check"] = {"ok": False, "why": bad}
    return [f"cwin: W12-C's directory and map disagree after the resume: {bad}"]


def leg_mid(m: AB.Machine, rom, rid: bytes, s: int, prg_resume: str,
            rep: dict, timeout: float = 600.0) -> list[str]:
    """Suspend mid-text, resume: the text continues the model's to m2, then
    m3.  *prg_resume*: the PRG loaded fresh for the resume (a mutant)."""
    ref, ok = model_sets(m)
    sn = Snap(m, m_prg(m), rid)
    boot(m, rom, s, rep, sn)
    text, seen, t0 = bytearray(), 0, time.monotonic()
    while m.frames() < SUSPEND_FRAME:
        if time.monotonic() - t0 > timeout:
            return [f"mid: guest frame {m.frames()} < {SUSPEND_FRAME} after {timeout:.0f} s"]
        time.sleep(AB.POLL_VICE_S if m.vice else 0.05)
    suspend(m, sn, rep)
    n, ring = m.serial_final(seen)              # frozen: the count is exact
    AB.take(text, ring, seen, n, m.sersz)
    before = bytes(text).decode("latin-1")
    rep["serial_before"] = len(before)
    if not ref.startswith(before):
        return [f"mid: serial before the suspend differs from the model"]
    fresh_load(m, prg_resume)
    sn = Snap(m, prg_resume, rid)
    sn.declare_rid()
    f = survive_check(m, sn, rep)
    if f:
        return f
    v, dt = sn.resume()
    rep["resume_st"], rep["resume_s"] = f"${v:02X}", round(dt, 2)
    if v != F.ST_RES:
        return [f"mid: resume refused: ${v:02X} ({F.ST_ERR.get(v)})"]
    if m.vice:
        f = cwin_check(m, rep)
        if f:
            return f
    c = m.count()
    log(f"  resumed in {dt:.2f} s; serial count {c} (frozen at {n})")
    if (c - n) & 0xFFFF > m.sersz:
        return [f"mid: the serial count was not restored: {c} after the resume, "
                f"{n} when frozen"]
    t1 = time.monotonic()
    after, seen = bytearray(), n
    want = ref[len(before):]
    while len(after) < len(want):
        if time.monotonic() - t1 > timeout:
            break
        try:
            seen = serial_text(m, seen, after)
        except AB.CaptureError as e:
            return [f"{INFRA}mid: {e}"]
        got = bytes(after).decode("latin-1")
        k = next((i for i in range(min(len(got), len(want))) if got[i] != want[i]), None)
        if k is not None:
            return [f"mid: serial after the resume diverges at byte {len(before) + k}: "
                    f"{got[k]!r} (model {want[k]!r})"]
        time.sleep(AB.POLL_VICE_S if m.vice else AB.POLL_S)
    rep["serial_after"] = len(after)
    rep["serial_after_s"] = round(time.monotonic() - t1, 1)
    if len(after) < len(want):
        return [f"mid: only {len(after)} of {len(want)} serial bytes after the resume "
                f"(frame {m.frames()}; {AB._cpu_state(m)})"]
    log(f"  serial continued: {len(before)} + {len(after)} bytes = the model's {len(ref)}")
    dt = hash_wait(m, ok, t1, timeout)
    rep["m3_after_resume_s"] = None if dt is None else round(dt, 1)
    if dt is None:
        return [f"mid: no model insert-disk picture within {timeout:.0f} s of the resume"]
    log(f"  m3 picture {dt:.1f} s after the resume")
    return []


def m_prg(m: AB.Machine) -> str:
    return getattr(m, "_prg")


def scratch_page(m: AB.Machine) -> bytes:
    return m.r(m.lab("__BSS_RUN__"), 0x100)


def scratch_bad(m: AB.Machine, page0: bytes) -> str:
    """F1: after a refusal the scratch page (W12-C's cw_* lie there) is as
    it was; a fresh load's cache directory: every cw_val 0, cw_hand < N."""
    n = m.lab("CWIN_N")
    val, hand = m.r(m.lab("cw_val"), n), m.r(m.lab("cw_hand"), 1)[0]
    page = scratch_page(m)
    why = []
    if page != page0:
        k = next(i for i in range(0x100) if page[i] != page0[i])
        why.append(f"scratch page changed (first at +${k:02X})")
    if hand >= n:
        why.append(f"cw_hand {hand} >= {n}")
    if any(val):
        why.append(f"cw_val {val.hex()} not all 0")
    return ", ".join(why)


def refusals(m: AB.Machine, sn: Snap, rep: dict) -> list[str]:
    """The red cases on a frozen-then-reset machine whose REU holds a good
    snapshot; each restores what it broke.  mach_on stays 0: nothing
    was restored."""
    fails = []
    bid = m.r(sn.a_bid, 8)
    hdr = sn.reu_read(F.SNAP_BASE, 256)
    p = F.parse_header(hdr)
    rep["header"] = {"sum_ok": p["sum_ok"], "keep": p["keep"], "bss_run": f"${p['bss_run']:04X}",
                     "bid": p["bid"].hex(), "spans": [(f"${a:06X}", n) for a, n in p["spans"]]}
    if not p["sum_ok"] or p["bid"] != bid:
        return [f"refuse: the snapshot header in the REU is not this build's: {rep['header']}"]
    st1 = F.SNAP_BASE + F.OFF_BSS + 0x40          # a BSS image byte
    st1v = sn.reu_read(st1, 1)

    cases = [
        ("E8 unstamped", lambda: m.w(sn.a_bid, bytes(8)), lambda: m.w(sn.a_bid, bid), 0xE8),
        ("E3 build ID", lambda: m.w(sn.a_bid, bytes([bid[0] ^ 0x5A]) + bid[1:]),
         lambda: m.w(sn.a_bid, bid), 0xE3),
        ("E4 ROM id", lambda: sn.declare_rid(hashlib.sha256(b"another ROM").digest()),
         lambda: sn.declare_rid(), 0xE4),
        ("EA no ROM id", lambda: sn.declare_rid(bytes(32)), lambda: sn.declare_rid(), 0xEA),
        ("E2 header byte", lambda: sn.reu_write(F.SNAP_BASE + F.H_FLAGS, b"\x01"),
         lambda: sn.reu_write(F.SNAP_BASE + F.H_FLAGS, hdr[F.H_FLAGS:F.H_FLAGS + 1]), 0xE2),
        ("E5 state byte", lambda: sn.reu_write(st1, bytes([st1v[0] ^ 0xFF])),
         lambda: sn.reu_write(st1, st1v), 0xE5),
    ]
    rep["refusals"] = {}
    page0 = scratch_page(m)
    for name, brk, fix, want in cases:
        brk()
        v, dt = sn.resume(timeout=60.0)
        on = m.r(m.lab("mach_on"), 1)[0]
        f1 = scratch_bad(m, page0)
        fix()
        rep["refusals"][name] = {"st": f"${v:02X}", "mach_on": on, "s": round(dt, 2),
                                 "scratch": f1 or "intact"}
        if v != want or on or f1:
            fails.append(f"refuse {name}: snap_st ${v:02X} (want ${want:02X}), mach_on {on}"
                         + (f"; {f1}" if f1 else ""))
        else:
            log(f"  refused as expected: {name} -> ${v:02X}")
    if sn.reu_read(F.SNAP_BASE, 256) != hdr:
        fails.append("refuse: the header did not read back intact after the cases")
    return fails


# the unmapped spans no file span carries; $DC0000-$DEFFFF is NOT among
# them: AROS writes there (found by this check, now in the slow-RAM span)
ZERO_CHECK = ((0x200000, 0x9F0000), (0xE80000, 0x100000))


def survive_check(m: AB.Machine, sn: "Snap", rep: dict) -> list[str]:
    """Variant A's assumption, proven per run: the REU contents survive the
    reset and the fresh load.  The suspend computed the state span's
    fletcher on the C64 BEFORE the reset (header +96, under the header's
    own checksum); aros_sum recomputes it on the C64 AFTER.  Also the REU
    config as the device reports it (u64: set inside the hold by
    prepare_target before the boot and again before the resume)."""
    hdr = sn.reu_read(F.SNAP_BASE, 256)
    p = F.parse_header(hdr)
    a, n = F.spans(p["keep"])[1]
    m.w(AB.P + 4, struct.pack("<II", a, n))
    got = struct.unpack_from("<I", m.call("aros_sum"), 4)[0]
    bid = m.r(sn.a_bid, 8)
    out = {"magic": p["magic"].decode("latin-1"), "bid_ok": p["bid"] == bid,
           "header_sum_ok": p["sum_ok"], "state_sum_at_suspend": f"${p['state_sum']:08X}",
           "state_sum_after_reset": f"${got:08X}", "span": f"${a:06X}+${n:X}"}
    if not m.vice:
        from c64_test_harness.backends.ultimate64_helpers import get_reu_config
        out["reu_config_after_reset"] = list(get_reu_config(m.t.client))
    rep["survive"] = out
    log(f"  REU across the reset: {out}")
    if p["magic"] != F.MAGIC or p["bid"] != bid or not p["sum_ok"] or got != p["state_sum"]:
        return [f"survive: the snapshot did not survive the reset: {out}"]
    return []


def zero_check(m: AB.Machine, rep: dict) -> list[str]:
    """(1) The inventory's claim that a snapshot need not carry the machine
    profile's unmapped regions: after a boot to m3 they are still all zero
    (Ultimate only: ~10 MB through a 1 MHz VICE is minutes).  (2) Every
    backend: the REU under the trapped $DF bank outside the snapshot's used
    part (and outside the LUT) is zero after m3: no guest access, chip trap
    or DMA reaches $DF1000+ (only snap.s writes there)."""
    out, fails = {}, []
    keepr = F.keepr(F.parse_header(Snap(m, m_prg(m), b"").reu_read(F.SNAP_BASE, 256))["keep"])
    lo = F.SNAP_BASE + F.OFF_BSS + keepr
    sav = F.SNAP_BASE + F.OFF_SAV                # F1's scratch save: carved out
    rid = (F.SNAP_BASE + F.OFF_RID) & ~0xFF
    df = [(lo, sav - lo), (sav + 0x100, rid - sav - 0x100), (F.SNAP_END + 1, 0xE00000 - F.SNAP_END - 1)]
    df = [(a, n) for a, n in df if n > 0]
    for a, n in (df if m.vice else list(ZERO_CHECK) + df):
        m.w(AB.P + 4, struct.pack("<II", a, n))
        got = struct.unpack_from("<I", m.call("aros_sum"), 4)[0]
        out[f"${a:06X}+${n:X}"] = f"${got:08X}"
        if got != machine.fletcher(b""):
            fails.append(f"zero: the span ${a:06X}+${n:X} is not zero after m3")
    rep["unmapped_zero"] = out
    log(f"  unmapped regions after m3: {out}")
    return fails


def leg_m3(m: AB.Machine, rom, rid: bytes, s: int, prg_resume: str, rep: dict,
           timeout: float = 900.0, red: bool = True) -> list[str]:
    _, ok = model_sets(m)
    sn = Snap(m, m_prg(m), rid)
    boot(m, rom, s, rep, sn)
    t0 = time.monotonic()
    dt = hash_wait(m, ok, t0, timeout)
    if dt is None:
        return [f"m3: the boot never showed the insert-disk picture ({timeout:.0f} s)"]
    rep["boot_m3_s"] = round(dt, 1)
    log(f"  m3 reached in {dt:.1f} s (frame {m.frames()})")
    suspend(m, sn, rep)
    from host.hle_dos_env import asm_equates
    epa = asm_equates(__import__("pathlib").Path(ROOT, "runtime/rc/ram.inc"))["disp_st"] + 105
    ep0 = m.r(epa, 1)[0] if m.vice else None    # frozen in mach_run: $01 = $35
    fresh_load(m, prg_resume)
    sn = Snap(m, prg_resume, rid)
    sn.declare_rid()
    rep["fresh_hash_is_model"] = fast_hash(m) in ok
    fails = survive_check(m, sn, rep)
    fails += refusals(m, sn, rep) if red else []
    if red:
        fails += zero_check(m, rep)
    t1 = time.monotonic()
    v, rdt = sn.resume()
    rep["resume_st"], rep["resume_s"] = f"${v:02X}", round(rdt, 2)
    if v != F.ST_RES:
        return fails + [f"m3: resume refused: ${v:02X} ({F.ST_ERR.get(v)})"]
    if m.vice:
        fails += cwin_check(m, rep)
    if ep0 is not None:
        # VICE (BHI is not host-readable on the Ultimate): resume bumps the
        # epoch (I2).  The animated insert-disk screen converts by itself
        # within ~30 s anyway (a blit bumps it too), so the picture time
        # alone cannot tell a missing bump; the first read here comes
        # before the guest's next blit can land (checked: base vs noepoch)
        ep1 = m.r(epa, 1)[0]
        rep["epoch"] = {"frozen": ep0, "after_resume": ep1}
        if ep1 == ep0:
            fails.append(f"m3: the pixel epoch was not bumped by the resume (${ep0:02X})")
    budget = M3_BUDGET_VICE_S if m.vice else M3_BUDGET_S
    dt = hash_wait(m, ok, t1, max(budget * 2, 60.0))
    rep["m3_again_s"], rep["m3_budget_s"] = (None if dt is None else round(dt, 2)), budget
    if dt is None or dt > budget:
        fails.append(f"m3: the insert-disk picture came back after "
                     f"{'never' if dt is None else f'{dt:.1f} s'} (budget {budget:.0f} s)")
    else:
        log(f"  m3 picture again {dt:.2f} s after launching the resume "
            f"(resume itself {rdt:.2f} s)")
    return fails


SNAP_FILE = "/USB1/amiga64/snap-test.bin"     # created and deleted by m3file (--vol)

def check_vol(vol: str) -> str:
    """The device storage volume for the file legs: absolute, one
    component, never Flash or Temp (test rule; the harness refuses writes
    there too)."""
    if not vol.startswith("/") or vol.count("/") != 1 or len(vol) < 2:
        raise ValueError(f"--vol {vol!r}: one absolute volume, e.g. /USB1 or /SD")
    if vol[1:].lower() in ("flash", "temp"):
        raise ValueError(f"--vol {vol!r}: Flash and Temp are never used")
    return vol


def set_vol(vol: str) -> None:
    global SNAP_FILE
    SNAP_FILE = f"{check_vol(vol)}/amiga64/snap-test.bin"


def leg_m3file(m: AB.Machine, rom, rid: bytes, s: int, prg_resume: str, rep: dict,
               timeout: float = 900.0) -> list[str]:
    """Variant B (U64E: device storage writes allowed there, fix round 1):
    boot to m3, suspend WITH the file (SAVE_REU of every span to
    SNAP_FILE), read the file back over FTP and check its header, reset +
    fresh load, ZERO THE WHOLE REU (the resume can only come from the
    file), LOAD_REU + resume: m3's picture again within M3_BUDGET_S of the
    launch.  The test file is deleted afterwards (the ADF is not touched)."""
    if m.vice:
        return ["m3file: Ultimate only (VICE has no UCI)"]
    from c64_test_harness import storage_delete_file, storage_get_file
    host = os.environ["U64_HOST"]
    cl = m.t.client
    cat, item = "C64 and Cartridge Settings", "Command Interface"
    rep["ci"] = [cl.get_config_value(cat, item)]
    cl.set_config_item(cat, item, "Enabled")    # inside the hold: u64_hold restores it
    rep["ci"].append(cl.get_config_value(cat, item))
    log(f"  Command Interface {rep['ci'][0]} -> {rep['ci'][1]} (restored by the hold)")
    fails: list[str] = []
    try:
        _, ok = model_sets(m)
        sn = Snap(m, m_prg(m), rid)
        boot(m, rom, s, rep, sn)
        t0 = time.monotonic()
        dt = hash_wait(m, ok, t0, timeout)
        if dt is None:
            return [f"m3file: no insert-disk picture ({timeout:.0f} s)"]
        rep["boot_m3_s"] = round(dt, 1)
        log(f"  m3 reached in {dt:.1f} s (frame {m.frames()})")
        name = SNAP_FILE.encode()
        m.w(m.lab("dosio_name"), name)
        m.w(m.lab("dosio_nlen"), bytes([len(name)]))
        t1 = time.monotonic()
        sn.request(F.RQ_SUS | F.RQ_FILE)
        v = sn.wait_st(lambda v: v == F.ST_SUSF or v in F.ST_ERR, 300.0)
        rep["suspend_file_s"], rep["suspend_st"] = round(time.monotonic() - t1, 2), f"${v:02X}"
        if v != F.ST_SUSF:
            return [f"m3file: suspend to the file failed: snap_st ${v:02X}"]
        data = storage_get_file(host, SNAP_FILE)
        h = F.parse_header(data[:256])
        sp = F.spans(h["keep"])
        rep["file"] = {"size": len(data), "spans_total": sum(n for _, n in sp),
                       "sum_ok": h["sum_ok"], "flags": h["flags"], "bid": h["bid"].hex(),
                       "state_sum_ok": F.fletcher(data[256:256 + sp[1][1]]) == h["state_sum"]}
        log(f"  suspended to {SNAP_FILE} in {rep['suspend_file_s']} s: {rep['file']}")
        if (len(data) != rep["file"]["spans_total"] or not h["sum_ok"]
                or not rep["file"]["state_sum_ok"] or h["flags"] != F.RQ_FILE):
            fails.append(f"m3file: the file is not a whole snapshot: {rep['file']}")
        fresh_load(m, prg_resume)
        m.w(AB.P, bytes([0, 0]))                # aros_fill: every REU bank
        m.call("aros_fill")
        sn = Snap(m, prg_resume, rid)
        sn.declare_rid()
        rep["reu_header_after_fill"] = sn.reu_read(F.SNAP_BASE, 4).hex()
        m.w(m.lab("dosio_name"), name)
        m.w(m.lab("dosio_nlen"), bytes([len(name)]))
        t2 = time.monotonic()
        v, rdt = sn.resume(F.RQ_FILE, timeout=300.0)
        rep["resume_st"], rep["resume_file_s"] = f"${v:02X}", round(rdt, 2)
        if v != F.ST_RES:
            return fails + [f"m3file: resume from the file refused: ${v:02X} ({F.ST_ERR.get(v)})"]
        dt = hash_wait(m, ok, t2, max(M3_BUDGET_S * 4, 60.0))
        rep["m3_again_s"] = None if dt is None else round(dt, 2)
        if dt is None or dt > M3_BUDGET_S:
            fails.append(f"m3file: the picture came back after "
                         f"{'never' if dt is None else f'{dt:.1f} s'} (budget {M3_BUDGET_S:.0f} s)")
        else:
            log(f"  from the file: resume {rdt:.2f} s (LOAD_REU included), picture again "
                f"{dt:.2f} s after the launch")
    finally:
        rep["deleted"] = storage_delete_file(host, SNAP_FILE, missing_ok=True)
        log(f"  {SNAP_FILE} deleted: {rep['deleted']}")
    return fails


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def session(prg: str, backend: str, out: str, mhz: int, fn):
    def wrap(m: AB.Machine):
        m._prg = os.path.abspath(prg)
        return fn(m)
    if backend == "vice":
        return AB.vice_session(prg, wrap)
    return AB.u64_session(prg, out, mhz, wrap)


# --------------------------------------------------------------------------
# mutants
# --------------------------------------------------------------------------

MUTANTS = {                     # name -> (define, leg)
    "nozp": ("SNAP_MUT_NOZP", "mid"),
    "nobss": ("SNAP_MUT_NOBSS", "mid"),
    "nodst": ("SNAP_MUT_NODST", "mid"),
    "nocia": ("SNAP_MUT_NOCIA", "mid"),
    "noexc": ("SNAP_MUT_NOEXC", "mid"),
    "noepoch": ("SNAP_MUT_NOEPOCH", "m3"),
    "noscr": ("SNAP_MUT_NOSCR", "refuse:E2"),     # F1 (replaces nossum: E5 is pinned by the refusal legs)
    "nohsum": ("SNAP_MUT_NOHSUM", "accept:E2"),
    "noinvall": ("SNAP_MUT_NOINVALL", "mid"),   # I1 (replaces nobid; E3 is the m3 leg's refusal)
}


def build_mutant(prg: str, name: str) -> str:
    from tests.run_c2v import makefile_sources
    bdir = os.path.dirname(os.path.abspath(prg))
    disp = os.path.join(bdir, "gen", "dispatch.s")
    out = os.path.join(bdir, "snapmut", name)
    os.makedirs(out, exist_ok=True)
    mprg, mlbl = os.path.join(out, "runner.prg"), os.path.join(out, "runner.lbl")
    define = MUTANTS[name][0]
    cmd = ["cl65", "-t", "none", "-C", os.path.join(ROOT, "runtime", "c64.cfg"),
           "--asm-define", f"{define}=1", "-o", mprg, "-Ln", mlbl,
           "-m", os.path.join(out, "runner.map")] + makefile_sources(ROOT) + [disp]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)
    base_lbl = os.path.splitext(prg)[0] + ".lbl"
    F.stamp(mprg, mlbl, as_id=F.read_bid(prg, base_lbl))
    # the snapshot layout is MAIN's: every label below SNAPHI at the same
    # address, and MAIN bytes equal except operands in snap.s/ureu.s CODE
    # that point into SCODE (it moves when a mutant's snap SCODE shrinks)
    la, lb = F.labels(base_lbl), F.labels(mlbl)
    low = {k: v for k, v in la.items() if 0x0801 <= v < 0xB100}
    bad = [k for k, v in low.items() if lb.get(k) != v]
    if bad:
        raise RuntimeError(f"mutant {name}: MAIN labels moved: {bad[:5]}")
    a, b = open(prg, "rb").read(), open(mprg, "rb").read()
    top = 0xB100 - 0x0801 + 2
    lo, hi = la["snap_arm"], la["ureu_sv"] + 8      # snap.s + ureu.s MAIN code
    diff = [i - 2 + 0x0801 for i in range(top) if a[i] != b[i]]
    if any(not lo <= x < hi for x in diff):
        raise RuntimeError(f"mutant {name}: MAIN bytes differ outside snap/ureu code: "
                           f"{[hex(x) for x in diff if not lo <= x < hi][:5]}")
    if a[top:] == b[top:]:
        raise RuntimeError(f"mutant {name}: identical to the base (define unused?)")
    return mprg


def accept_leg(m: AB.Machine, rom, rid: bytes, s: int, mprg: str, what: str,
               rep: dict) -> list[str]:
    """Suspend early, break what *what*'s check guards, resume with the
    mutant: the mutant must ACCEPT (the base refuses: the m3 leg)."""
    sn = Snap(m, m_prg(m), rid)
    boot(m, rom, s, rep, sn)
    t0 = time.monotonic()
    while m.frames() < 20 and time.monotonic() - t0 < 300:
        time.sleep(0.5)
    suspend(m, sn, rep)
    fresh_load(m, mprg)
    sn = Snap(m, mprg, rid)
    sn.declare_rid()
    if what == "E2":
        sn.reu_write(F.SNAP_BASE + F.H_FLAGS, b"\x01")
    elif what == "E3":
        bid = m.r(sn.a_bid, 8)
        m.w(sn.a_bid, bytes([bid[0] ^ 0x5A]) + bid[1:])
    elif what == "E5":
        a = F.SNAP_BASE + F.OFF_BSS + 0x40
        sn.reu_write(a, bytes([sn.reu_read(a, 1)[0] ^ 0xFF]))
    v, _ = sn.resume(timeout=60.0)
    rep["st"] = f"${v:02X}"
    # the base refuses this (leg m3's refusals): a resume kills the mutant
    return [f"accepted the corruption ({what})"] if v == F.ST_RES else []


def refuse_leg(m: AB.Machine, rom, rid: bytes, s: int, mprg: str, what: str,
               rep: dict) -> list[str]:
    """Suspend early, corrupt the header's flags byte, resume with the
    mutant: it must refuse (E2) AND leave the scratch page as it was (F1);
    a changed page kills the mutant."""
    sn = Snap(m, m_prg(m), rid)
    boot(m, rom, s, rep, sn)
    t0 = time.monotonic()
    while m.frames() < 20 and time.monotonic() - t0 < 300:
        time.sleep(0.5)
    suspend(m, sn, rep)
    fresh_load(m, mprg)
    sn = Snap(m, mprg, rid)
    sn.declare_rid()
    page0 = scratch_page(m)
    sn.reu_write(F.SNAP_BASE + F.H_FLAGS, b"\x01")
    v, _ = sn.resume(timeout=60.0)
    rep["st"] = f"${v:02X}"
    bad = scratch_bad(m, page0)
    return [f"refused ${v:02X} but {bad}"] if bad else []


def teeth(prg: str, only, s: int, out: str, jobs: int = 3) -> int:
    rom, why = machine.find_rom()
    if rom is None:
        print(f"SKIP aros_resume --mutants: {why}")
        return 0
    rid = rom_id(rom)
    setup_driver(os.path.join(os.path.dirname(os.path.abspath(prg)), "aros-drv"))
    names = [n for n in MUTANTS if not only or n in only]
    built = {n: build_mutant(prg, n) for n in names}     # serial builds
    rep, killed = {}, {}

    def one(n: str) -> None:
        leg = MUTANTS[n][1]
        r: dict = {"leg": leg}
        t0 = time.monotonic()
        log(f"[mutant {n}] leg {leg}")
        try:
            if leg == "mid":
                f = session(prg, "vice", out, 0,
                            lambda m: leg_mid(m, rom, rid, s, built[n], r, timeout=300.0))
            elif leg == "m3":
                f = session(prg, "vice", out, 0,
                            lambda m: leg_m3(m, rom, rid, s, built[n], r, red=False))
            elif leg.startswith("refuse:"):
                f = session(prg, "vice", out, 0,
                            lambda m: refuse_leg(m, rom, rid, s, built[n],
                                                 leg.split(":")[1], r))
            else:
                f = session(prg, "vice", out, 0,
                            lambda m: accept_leg(m, rom, rid, s, built[n],
                                                 leg.split(":")[1], r))
        except Exception as e:
            f = [f"{INFRA}{type(e).__name__}: {e}"]
        k = bool(f) and not all(x.startswith(INFRA) for x in f)
        killed[n] = k
        r.update(fails=f, killed=k, wall_s=round(time.monotonic() - t0, 1))
        rep[n] = r
        log(f"[mutant {n}] {'KILLED' if k else 'SURVIVED'}: {f[:1]}")

    from concurrent.futures import ThreadPoolExecutor
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs) as ex:   # one VICE per session
        list(ex.map(one, names))
    rep["_wall_s"] = round(time.monotonic() - t0, 1)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "mutants.json"), "w") as fh:
        json.dump(rep, fh, indent=1)
    nk = sum(killed.values())
    print(f"wall-clock {rep['_wall_s']} s")
    print(f"aros_resume mutants: {nk} killed / {len(killed) - nk} survived")
    return 0 if nk == len(killed) else 1


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default="mid,m3")
    ap.add_argument("--s", type=int, default=None)
    ap.add_argument("--backend", default="vice", choices=("vice", "u64"))
    ap.add_argument("--mhz", type=int, default=64)
    ap.add_argument("--out", default=os.path.join(EVID, "resume"))
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--vol", default="/USB1",
                    help="m3file: the storage volume (U64E /USB1, C64U /SD)")
    ap.add_argument("--jobs", type=int, default=3, help="--mutants: VICE sessions at once")
    ap.add_argument("--real-input", action="store_true",   # W13-I
                    help="W13-I: boot with the real joystick/keys (default: idle "
                         "override, inp_src = 1; the snapshot image carries it)")
    a = ap.parse_args(argv)
    AB.REAL_INPUT = a.real_input        # W13-I: aros_boot.Machine writes inp_src
    set_vol(a.vol)
    s = a.s if a.s is not None else (AB.BOOT_S_VICE if a.backend == "vice" else AB.BOOT_S_U64)
    if a.mutants:
        return teeth(a.prg, a.only.split(",") if a.only else None, s, a.out, a.jobs)
    rom, why = machine.find_rom()
    if rom is None:
        print(f"SKIP aros_resume: {why}")
        return 0
    rid = rom_id(rom)
    setup_driver(os.path.join(os.path.dirname(os.path.abspath(a.prg)), "aros-drv"))
    os.makedirs(a.out, exist_ok=True)
    rep: dict = {"prg": os.path.abspath(a.prg), "backend": a.backend, "s": s,
                 "bid": F.read_bid(a.prg, os.path.splitext(a.prg)[0] + ".lbl").hex()}
    if a.backend != "vice":
        rep["mhz"] = a.mhz
    fails = []
    for leg in [x for x in a.legs.split(",") if x]:
        r = rep.setdefault(leg, {})
        t0 = time.monotonic()
        log(f"[{leg}] {a.backend}")
        fn = {"mid": lambda m: leg_mid(m, rom, rid, s, a.prg, r),
              "m3": lambda m: leg_m3(m, rom, rid, s, a.prg, r),
              "m3file": lambda m: leg_m3file(m, rom, rid, s, a.prg, r)}[leg]
        try:
            f = session(a.prg, a.backend, os.path.join(a.out, f"{a.backend}-{leg}"),
                        a.mhz, fn)
        except Exception as e:
            f = [f"{INFRA}{leg}: {type(e).__name__}: {e}"]
        r["wall_s"] = round(time.monotonic() - t0, 1)
        log(f"[{leg}] {'PASS' if not f else 'FAIL'} ({r['wall_s']} s)")
        fails += f
    rep["fails"] = fails
    with open(os.path.join(a.out, f"{a.backend}.json"), "w") as fh:
        json.dump(rep, fh, indent=1)
    for x in fails:
        print(f"FAIL {x}")
    print(f"aros_resume: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
