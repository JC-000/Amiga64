#!/usr/bin/env python3
"""W13-I: the C64 joystick (port 2) moves the Amiga pointer (VICE, C64U).

docs/WAVE13_BRIEF.md "W13-I", contract docs/CONTRACTS.md "Input (joystick
-> mouse)".  The reference is host/inp_ref.Inp (frame by frame) and
host/inp_ref.scan (the CIA1 side).  One build, one emulator session per
backend, many cases.

Legs:
  api     inp_frame called by the host frame by frame with the override
          (inp_src = 1): a scripted and a seeded random input sequence;
          after every frame JOY0DAT (chip_shd), the CIA-A pins, inp_view
          and inp_n against the model; every 8th frame also JOY0DAT,
          POTINP and CIA-A PRA read through the guest memory API (the
          chip.s/cia.s read paths).  Covers the counter wrap, the
          acceleration and its cap, both buttons, both at once while
          moving, the F1 edge (the host clears inp_view like W13-D),
          /FIR1 forced high, pins 0-5 kept.
  guest   fixtures/inp/inp_log.s under mach_run (tests/mach/drv.s): per
          VERTB the guest logs JOY0DAT, CIA-A PRA and POTINP; six runs of
          16 frames with different override input, logs against the model
          (inp_frame runs once in the service pass that posts each VERTB).
  real    inp_src = 0, the real CIA1 scan: rows held low (made by the CIA
          itself through DDRB/PRB: VICE 3.10 ignores JOYPORT_SET, see
          leg_real) never reach inp_joy and make their key unknown (it
          keeps its last state: no phantom F1/C=); readable rows read up;
          CIA1 left DDRA = $FF, DDRB = $00; nothing held = all idle.
  noinj   Ultimate only: the harness inject_joystick writes the CIA1 PRA
          latch ($DC00, c64_test_harness backends/ultimate64.py
          inject_joystick); inp.s reads port 2 with DDRA = $00, so the
          latch is not on the pins and the write must NOT reach inp_joy.
          (Unattended: the physical stick must be left alone.)

    python3 -m tests.run_inp --prg build/X/runner.prg [--legs api,guest,real]
    python3 -m tests.run_inp --prg ... --mutants [--only m1,m2]
    U64_HOST=10.53.21.158 python3 -m tests.run_inp --prg ... --backend u64 \
        [--mhz 64]                      # api, guest, noinj on the device
    U64_HOST=10.53.21.158 python3 -m tests.run_inp --prg ... --attended \
        [--seconds 60] [--mhz 64]       # the user moves the real stick

Exit 0 = every selected leg passed (--mutants: every mutant killed;
--attended: every input was seen).
"""

from __future__ import annotations

import argparse
import os
import random
import struct
import subprocess
import sys
import time

from host import inp_ref
from host.inp_ref import CBM, F1, FIRE, DOWN, LEFT, RIGHT, UP, Inp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(ROOT, "build", "evidence", "w13-inp")
VBCC = os.environ.get("VBCC", os.path.expanduser("~/.local/opt/vbcc"))
FIXTURE = os.path.join(ROOT, "fixtures", "inp", "inp_log.s")

FSTUB = 0xC780            # buf_case tail: php / sei / jsr inp_frame / plp / rts
JOY0DAT, POTINP, CIAA_PRA = 0xDFF00A, 0xDFF016, 0xBFE001
JOY0 = 0x0A               # JOY0DAT in chip_shd (inp.s INP_JOY0)
LOG = 0x20000             # fixtures/inp/inp_log.s LOG
FRAMES = 16               # fixtures/inp/inp_log.s FRAMES
GUEST_S = 2               # mach_s for the guest runs (18 frames/s on NTSC phi2)
ALL_LEGS = ["api", "guest", "real"]
U64_LEGS = ["api", "guest", "real", "noinj"]
VERBOSE = False


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def equate(path: str, name: str) -> int:
    import re
    for line in open(os.path.join(ROOT, path)):
        m = re.match(rf"{name}\s*=\s*(\$?[0-9A-Fa-f]+)\s*;", line)
        if m:
            v = m.group(1)
            return int(v[1:], 16) if v[0] == "$" else int(v)
    raise KeyError(f"{name} not in {path}")


CIA_PIN = equate("runtime/ops/cia.s", "CIA_PIN")


# --------------------------------------------------------------------------
# Rig: run_cia's (API trampoline) plus run_mach's (guest runs)
# --------------------------------------------------------------------------

class Rig:
    def __init__(self, target, labels, prg: str):
        from tests.run_cia import Rig as CiaRig
        self.tg, self.t, self.labels = target, target.transport, labels
        self.cia = CiaRig(target, labels)
        self.cia.prg = prg
        self.lab = self.cia.lab
        self.prg = prg
        try:
            self.src = self.lab("inp_src")
            self.frame_addr = self.lab("inp_frame")
            self.ok = True
        except KeyError:
            self.ok = False
        self.shd = self.lab("chip_shd")
        self.pin = self.lab("cia_st") + CIA_PIN
        self._stub = False

    w = property(lambda s: s.cia.w)
    r = property(lambda s: s.cia.r)

    def need(self) -> None:
        if not self.ok:
            raise RuntimeError("this PRG has no inp_frame (no W13-I)")

    def reset(self, src: int = 1) -> None:
        """chip_reset (JOY0DAT 0, CIAs to power-up: pins $EB), then inp's
        BSS block (inp_src .. inp_kp, 6 bytes) to idle."""
        self.cia.call(self.lab("chip_reset"))
        if self.ok:
            assert self.lab("inp_n") == self.src + 4, "inp BSS layout"
            self.w(self.src, bytes([src, 0, 0, 0, 0, 0]))

    def set_input(self, joy: int, key: int) -> None:
        self.w(self.src + 1, bytes([joy, key]))

    def frame(self) -> None:
        if not self._stub:
            a = self.frame_addr
            self.w(FSTUB, bytes([0x08, 0x78, 0x20, a & 0xFF, a >> 8, 0x28, 0x60]))
            self._stub = True
        self.cia.call(FSTUB)

    def state(self) -> dict:
        y, x = self.r(self.shd + JOY0, 2)
        b = self.r(self.src, 5)
        return {"joy0dat": y << 8 | x, "pra": self.r(self.pin, 1)[0],
                "view": b[3], "n": b[4], "joy": b[1], "key": b[2]}

    def api_read(self) -> dict:
        """The guest's view through the memory API (chip.s / cia.s)."""
        rd16, rd8 = self.lab("mem_read16"), self.lab("mem_read8")
        return {"joy0dat": self.cia.api(rd16, JOY0DAT)[0] & 0xFFFF,
                "potinp": self.cia.api(rd16, POTINP)[0] & 0xFFFF,
                "pra": self.cia.api(rd8, CIAA_PRA)[0] & 0xFF}


# --------------------------------------------------------------------------
# api: host-driven frames vs the model
# --------------------------------------------------------------------------

def script_fixed() -> list[tuple[int, int, bool]]:
    """(joy, key, host clears inp_view after the frame)."""
    s = []
    s += [(RIGHT, 0, False)] * 40                 # accel to the cap, X wraps
    s += [(0, 0, False)] * 2                      # centred: n resets
    s += [(UP, 0, False)] * 3 + [(0, 0, False)]   # short taps: 1 per frame
    s += [(UP, 0, False)] * 30                    # Y wraps below 0
    s += [(UP | LEFT, 0, False)] * 6              # diagonal (no reset on change)
    s += [(DOWN | FIRE, CBM, False)] * 8          # both buttons while moving
    s += [(LEFT, CBM, False)] * 5                 # menu drag: C= held + move
    s += [(FIRE, 0, False)] * 2 + [(0, 0, False)]
    s += [(0, F1, False), (0, F1, False), (0, F1, True), (0, F1, False),
          (0, 0, False), (0, F1, True), (RIGHT, F1 | CBM, False),
          (RIGHT, 0, False)]                      # F1 edges, view cleared by the host
    s += [(UP | DOWN, 0, False)] * 3              # opposite bits cancel
    s += [(LEFT | RIGHT | FIRE, CBM, False)] * 3
    return s


def script_random(seed: int, n: int) -> list[tuple[int, int, bool]]:
    rng = random.Random(seed)
    s, joy, key = [], 0, 0
    for _ in range(n):
        if rng.random() < 0.25:
            joy = rng.choice([0, UP, DOWN, LEFT, RIGHT, UP | LEFT, DOWN | RIGHT,
                              UP | RIGHT, DOWN | LEFT]) | (FIRE if rng.random() < 0.3 else 0)
        if rng.random() < 0.2:
            key = rng.choice([0, 0, F1, CBM, F1 | CBM])
        s.append((joy, key, rng.random() < 0.15))
    return s


def leg_api(rig: Rig, nrand: int = 160) -> list[str]:
    rig.need()
    f: list[str] = []
    for name, sc in (("fixed", script_fixed()), ("random", script_random(13, nrand))):
        rig.reset(src=1)
        # pins 0-5 must be kept and /FIR1 forced high: start from pins $2A
        rig.w(rig.pin, b"\x2A")
        m = Inp(pin=0x2A)
        for i, (joy, key, clr) in enumerate(sc):
            rig.set_input(joy, key)
            rig.frame()
            want = m.frame(joy, key)
            got = rig.state()
            bad = [k for k in ("joy0dat", "pra", "view") if got[k] != want[k]]
            if got["n"] != m.n:
                bad.append("n")
            if i % 8 == 7 or i == len(sc) - 1:
                g = rig.api_read()
                for k in ("joy0dat", "potinp", "pra"):
                    if g[k] != want[k]:
                        bad.append(f"api.{k}")
                        got[f"api.{k}"] = g[k]
            if bad:
                f.append(f"api/{name} frame {i} joy=${joy:02X} key=${key:02X}: "
                         f"{', '.join(bad)}: got {got}, want {want} n={m.n}")
                if len(f) > 6:
                    return f
            if VERBOSE:
                print(f"    {name} {i:3d} joy=${joy:02X} key=${key:02X} -> {got}")
            if clr:
                rig.w(rig.src + 3, b"\0")
                m.view = 0
        log(f"  api/{name}: {len(sc)} frames, end JOY0DAT ${m.joy0dat:04X}, "
            f"{'ok' if not f else 'FAIL'}")
    return f


# --------------------------------------------------------------------------
# guest: the 68k fixture under mach_run
# --------------------------------------------------------------------------

GUEST_RUNS = [(RIGHT, 0), (RIGHT | DOWN | FIRE, 0), (LEFT, CBM),
              (0, F1), (UP | LEFT | FIRE, CBM), (UP, 0)]


def fixture_words() -> list[int]:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "inp_log.bin")
        r = subprocess.run([os.path.join(VBCC, "bin", "vasmm68k_mot"), "-quiet",
                            "-Fbin", "-o", out, FIXTURE], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"vasm failed: {r.stdout}{r.stderr}")
        b = open(out, "rb").read()
    return list(struct.unpack(f">{len(b) // 2}H", b))


def leg_guest(rig: Rig) -> list[str]:
    from tests.run_mach import clean
    f: list[str] = []
    mr = rig.cia.mach_rig()
    (pc,) = mr.stage(fixture_words())
    rig._stub = False                  # the driver load ($C200..) covers both
    rig.cia._tramp = None              # trampolines: write them again
    if pc != 0x10000:
        return [f"guest: fixture staged at ${pc:06X}, built for $010000"]
    rig.reset(src=1)
    m = Inp()
    for k, (joy, key) in enumerate(GUEST_RUNS):
        if rig.ok:
            rig.set_input(joy, key)
        res = mr.run(pc, s=GUEST_S, budget=4_000_000)
        tag = f"guest run {k} joy=${joy:02X} key=${key:02X}"
        if not clean(res, tag, f):
            continue
        if res["d"][3]:
            f.append(f"{tag}: the fixture saw no VERTB (d3 = {res['d'][3]})")
            continue
        raw = mr.guest_read(LOG, 6 * FRAMES)
        bad = 0
        for i in range(FRAMES):
            jd, pra, _, pot = struct.unpack_from(">HBBH", raw, 6 * i)
            want = m.frame(joy, key)
            got = {"joy0dat": jd, "pra": pra, "potinp": pot}
            if any(got[x] != want[x] for x in got):
                bad += 1
                if bad <= 3:
                    f.append(f"{tag} frame {i}: got "
                             f"{ {x: hex(v) for x, v in got.items()} }, want "
                             f"{ {x: hex(want[x]) for x in got} }")
        if rig.ok:
            st = rig.state()
            if st["view"] != m.view:
                f.append(f"{tag}: inp_view {st['view']} want {m.view}")
        log(f"  {tag}: {FRAMES} frames, JOY0DAT ${m.joy0dat:04X}, "
            f"{'ok' if not bad else f'{bad} frame(s) differ'}")
    return f


# --------------------------------------------------------------------------
# real: VICE joystick ports through the real CIA1 scan
# --------------------------------------------------------------------------

def leg_real(rig: Rig) -> list[str]:
    """The real CIA1 scan (inp_src = 0).  VICE 3.10 ignores the harness's
    JOYPORT_SET here (probe 2026-10-09: ports 0/1 accepted, $DC00/$DC01
    unchanged with every controlport/joydev setting; ports 2/3 refused), so
    a row held low is made by the CIA itself: DDRB bit r = 1, PRB bit r = 0
    drives row r low exactly as a port-1 stick or a key in a selected column
    would (inp.s never writes DDRB).  No stick, no key: everything idle."""
    rig.need()
    f: list[str] = []
    rig.reset(src=0)
    DDRB, PRB = 0xDC03, 0xDC01
    rig.frame()
    base = rig.state()["joy"]      # a real stick may be held (C64U 2026-10-09:
    if base:                       # port 2 PA0 low at 1 and 64 MHz, even driven)
        log(f"  real: port 2 reads ${base:02X} with nothing driven: a physical "
            "stick input; the legs compare against it")

    def frame(drive: int, prev: int) -> int:
        rig.w(rig.src + 2, bytes([prev]))         # inp_key: the last state
        rig.w(PRB, bytes([0xFF ^ drive]))
        rig.w(DDRB, bytes([drive]))
        rig.frame()
        rig.w(DDRB, b"\0")
        st = rig.state()
        if st["joy"] != base:
            f.append(f"real: rows ${drive:02X} driven low: inp_joy ${st['joy']:02X}, "
                     f"undriven ${base:02X} (rows must never reach the stick read)")
        return st["key"]

    try:
        cases = [  # rows driven low, inp_key before, inp_key wanted after
            (0x00, 0, 0), (0x00, F1 | CBM, 0),          # idle: both read up
            (0x10, 0, 0), (0x10, F1, F1),               # row 4 unreadable: F1 kept
            (0x10, F1 | CBM, F1),                       # ... C= (row 5) still read: up
            (0x20, CBM, CBM), (0x20, F1 | CBM, CBM),    # row 5 unreadable: C= kept
            (0x30, F1 | CBM, F1 | CBM), (0x30, 0, 0),
            (0x01, F1 | CBM, 0), (0x0F, 0, 0), (0x40, F1, 0)]   # other rows: no effect
        # (not row 7: a row driven low reaches port A through a HELD key, and
        # SHIFT LOCK holds column 1 / row 7: on the C64U 2026-10-09 rows $C0
        # read as stick DOWN.  inp.s never drives a row; only a port-1 stick
        # with a held key could: CONTRACTS "Input")
        for drive, prev, want in cases:
            got = frame(drive, prev)
            if got != want:
                f.append(f"real: rows ${drive:02X} low, inp_key ${prev:02X} before: "
                         f"${got:02X} after, want ${want:02X}")
        rig.frame()
        ddra, ddrb = rig.r(0xDC02, 2)
        if (ddra, ddrb) != (0xFF, 0x00):
            f.append(f"real: CIA1 left DDRA ${ddra:02X} DDRB ${ddrb:02X}, want $FF $00")
        st = rig.state()
        if not base and (st["joy"] or st["key"] or st["joy0dat"] or st["pra"] != 0xEB):
            f.append(f"real: nothing held, yet {st}")
    finally:
        rig.w(DDRB, b"\0")
    log(f"  real: {'ok' if not f else 'FAIL'}")
    return f


# --------------------------------------------------------------------------
# noinj: the Ultimate's inject_joystick cannot reach inp.s
# --------------------------------------------------------------------------

def leg_noinj(rig: Rig) -> list[str]:
    rig.need()
    f: list[str] = []
    rig.reset(src=0)
    rig.frame()
    base = rig.state()["joy"]               # the physical stick, if held
    seen = []
    for v in (RIGHT, DOWN | FIRE, 31):
        rig.t.inject_joystick(2, v)
        rig.frame()
        seen.append(rig.state()["joy"])
    if any(x != base for x in seen):
        f.append(f"noinj: inject_joystick reached inp_joy {seen} (undriven "
                 f"${base:02X}; or the physical stick moved during the leg)")
    log(f"  noinj: inp_joy {seen} after 3 injections, undriven ${base:02X} "
        f"({'ok' if not f else 'FAIL'})")
    return f


LEGS = {"api": leg_api, "guest": leg_guest, "real": leg_real, "noinj": leg_noinj}


def run_legs(rig: Rig, legs: list[str]) -> list[str]:
    fails = []
    for name in legs:
        t0 = time.monotonic()
        try:
            fl = LEGS[name](rig)
        except Exception as e:                    # noqa: BLE001 - a leg failure
            fl = [f"{name}: {type(e).__name__}: {e}"]
        log(f"leg {name}: {'PASS' if not fl else 'FAIL'} ({time.monotonic() - t0:.0f} s)")
        for x in fl:
            print(f"    {x}")
        fails += fl
    return fails


def vice(prg: str, legs: list[str]) -> list[str]:
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False, ntsc=True,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        try:
            prepare_target(tg, prg)
            return run_legs(Rig(tg, labels, prg), legs)
        finally:
            mgr.release(tg)


def u64(prg: str, mhz: int, fn, tag: str):
    """One DeviceLock hold (u64_hold: config snapshot, exact restore, reset;
    a SESSION.log line); CPU speed set and the machine rebooted first."""
    from c64_test_harness import Labels
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.bench.u64_session import u64_hold
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    out = os.path.join(EVID, f"u64-{tag}")
    os.makedirs(out, exist_ok=True)
    t0, res, err, hold = time.time(), None, None, {}
    try:
        with u64_hold(out) as hold:
            tg = hold["target"]
            tg.transport.set_speed(mhz)
            reboot(tg.transport.client)
            prepare_target(tg, prg)
            try:
                res = fn(Rig(tg, labels, prg))
            finally:
                tg.transport.set_speed(1)
                reboot(tg.transport.client)
    except BaseException as e:  # noqa: BLE001 -- logged, then re-raised
        err = e
    os.makedirs(EVID, exist_ok=True)
    with open(os.path.join(EVID, "SESSION.log"), "a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t0))} "
                 f"run_inp {tag} host={os.environ.get('U64_HOST', '?')} mhz={mhz} "
                 f"prg={prg} lock_wait_s={hold.get('lock_wait_s', '?')} "
                 f"restored={hold.get('restored')} reset_ok={hold.get('reset_ok')} "
                 f"wall_s={time.time() - t0:.0f}"
                 f"{' error=' + repr(err) if err else ''}\n")
    if err:
        raise err
    if not hold.get("restored"):
        raise RuntimeError("device config NOT restored identically (see config_after.json)")
    return res


# --------------------------------------------------------------------------
# attended: the user moves the real stick (the supervisor runs this)
# --------------------------------------------------------------------------

ASTUB, AFLAGS = 0xC700, 0xC7F0      # attended batch stub, OR-ed flags


def attended_stub(rig: Rig, n: int) -> bytes:
    """php/sei; n times: jsr inp_frame, OR inp_joy/inp_key/inp_view into
    AFLAGS..+2 (inp_view consumed like W13-D), wait for the raster to pass
    line 0 or 256 (every ~10 ms, at any CPU speed); plp/rts."""
    f, j, k, v = rig.frame_addr, rig.src + 1, rig.src + 2, rig.src + 3
    lo, hi = lambda a: a & 0xFF, lambda a: a >> 8
    c = [0x08, 0x78, 0xA2, n,
         0x8A, 0x48, 0x20, lo(f), hi(f)]
    for src, dst in ((j, AFLAGS), (k, AFLAGS + 1), (v, AFLAGS + 2)):
        c += [0xAD, lo(src), hi(src), 0x0D, lo(dst), hi(dst), 0x8D, lo(dst), hi(dst)]
    c += [0xA9, 0x00, 0x8D, lo(v), hi(v),
          0xAD, 0x12, 0xD0, 0xD0, 0xFB,            # raster wraps to 0 ...
          0xAD, 0x12, 0xD0, 0xF0, 0xFB,            # ... and leaves it
          0x68, 0xAA, 0xCA, 0xD0]                  # pla/tax/dex/bne loop
    c += [(4 - (len(c) + 1)) & 0xFF, 0x28, 0x60]   # loop = offset 4 (txa)
    return bytes(c)


def attended(rig: Rig, seconds: float) -> int:
    """The real CIA1 scan (inp_src = 0) at the device's speed, 10 frames
    (~10 ms apart, raster-paced) per host call; prints every change and ends with a
    verdict on what was seen."""
    rig.need()
    rig.reset(src=0)
    stub = attended_stub(rig, 10)
    rig.w(ASTUB, stub)
    seen, last, py, px = set(), None, None, None
    print("=" * 72)
    print("ATTENDED: move the joystick in PORT 2 of the C64U: up, down, left,\n"
          "right; press fire; hold C= and move the stick at the same time;\n"
          f"tap F1 twice.  {seconds:.0f} s.  (Ctrl-C ends early.)")
    print("=" * 72)
    rig.frame()
    idle = rig.state()["joy"]
    if idle:
        print(f"  NOTE: port 2 reads ${idle:02X} before anything is touched "
              "(a held direction: check the stick)")
    t_end = time.monotonic() + seconds
    try:
        while time.monotonic() < t_end:
            rig.w(AFLAGS, b"\0\0\0")
            rig.cia.call(ASTUB)
            fj, fk, fv = rig.r(AFLAGS, 3)
            st = rig.state()
            y, x = st["joy0dat"] >> 8, st["joy0dat"] & 0xFF
            for bit, name in ((UP, "up"), (DOWN, "down"), (LEFT, "left"),
                              (RIGHT, "right"), (FIRE, "left button (fire)")):
                if fj & bit & ~idle:
                    seen.add(name)
            if fk & CBM:
                seen.add("right button (C=)")
                if fj & 0x0F & ~idle:
                    seen.add("right button + move")
            if fv & 1:
                seen.add("F1")
            if py is not None and (y, x) != (py, px):
                seen.add("JOY0DAT moved")
            py, px = y, x
            cur = (fj, fk, fv & 1, st["joy0dat"])
            if cur != last:
                pot = 0xFB00 if fk & CBM else 0xFF00
                print(f"  {time.strftime('%H:%M:%S')} stick ${fj:02X} keys ${fk:02X} "
                      f"JOY0DAT ${st['joy0dat']:04X} (y {y:3d} x {x:3d}) "
                      f"/FIR0 {'LOW' if fj & FIRE else 'high'} POTINP ${pot:04X}"
                      f"{'  F1!' if fv & 1 else ''}", flush=True)
                last = cur
    except KeyboardInterrupt:
        print("  (ended early)")
    want = ["up", "down", "left", "right", "left button (fire)", "right button (C=)",
            "right button + move", "F1", "JOY0DAT moved"]
    missing = [w for w in want if w not in seen]
    print(f"ATTENDED VERDICT: {'PASS' if not missing else 'INCOMPLETE'}: seen "
          f"{sorted(seen)}{'; missing ' + str(missing) if missing else ''}")
    return 0 if not missing else 1


# --------------------------------------------------------------------------
# Mutants (<= 10, each built once)
# --------------------------------------------------------------------------

INP, MACH = "runtime/ops/inp.s", "runtime/ops/mach.s"
MUTANTS = {
    # the frame hook never calls inp_frame
    "nocall": (MACH, [("        jsr inp_frame           ; W13-I: joystick -> mouse, once per hooked frame\n", "")]),
    # the step never caps
    "nocap": (INP, [("        cpy #INP_NMAX\n", "        cpy #$FF\n")]),
    # centring does not reset the acceleration
    "noreset": (INP, [("        bne :+\n        sta inp_n\n", "        bne :+\n        nop\n        nop\n        nop\n")]),
    # the direction bits add where they subtract and vice versa
    "sign": (INP, [("        bcs add                 ; C = 1: + (step - 1) + 1\n",
                    "        bcc add                 ; C = 1: + (step - 1) + 1\n")]),
    # fire releases /FIR0 instead of pulling it low
    "fireinv": (INP, [("        eor #$C0\n", "        eor #$80\n")]),
    # /FIR1 not forced high (pin 7 left as it was)
    "nofir1": (INP, [("        and #$C0\n", "        and #$40\n")]),
    # the right button on POTINP bit 11 instead of 10
    "potbit": (INP, [("        lsr\n        lsr\n        lsr\n        eor #$FF\n",
                      "        lsr\n        lsr\n        eor #$FF\n")]),
    # F1 as a level, not an edge
    "noedge": (INP, [("have:   lda inp_kp              ; F1 press edge -> inp_view bit 0\n        eor #$FF\n        and inp_key\n",
                      "have:   lda inp_key             ; F1 press edge -> inp_view bit 0\n")]),
    # unreadable rows not masked: a port-1 row reads as a key (phantom)
    "noghost": (INP, [("        eor inp_key             ; unreadable rows (idle bit 0) keep the\n        and inp_t               ; last state: ((new ^ old) & idle) ^ old\n        eor inp_key\n", "")]),
    # the override is ignored: the real CIA1 is always scanned
    "nosrc": (INP, [("        lda inp_src\n        bne have\n", "")]),
}
MUTANT_LEGS = {"nocall": ["guest"], "nocap": ["api"], "noreset": ["api"],
               "sign": ["api"], "fireinv": ["api"], "nofir1": ["api"],
               "potbit": ["api"], "noedge": ["api"], "noghost": ["real"],
               "nosrc": ["api"]}


def build_mutant(name: str, out_dir: str) -> str:
    import tests.run_mach as rm
    saved = rm.MUTANTS
    try:
        rm.MUTANTS = MUTANTS
        return rm.build_mutant(name, out_dir)
    finally:
        rm.MUTANTS = saved


def teeth(prg: str, only: list[str] | None) -> int:
    bdir = os.path.relpath(os.path.dirname(os.path.abspath(prg)), ROOT)
    names = only or list(MUTANTS)
    bad = []
    for name in names:
        mprg = build_mutant(name, os.path.join(bdir, "inp-mut", name))
        log(f"=== mutant {name} (legs {','.join(MUTANT_LEGS[name])})")
        try:
            fails = vice(mprg, MUTANT_LEGS[name])
        except Exception as e:                    # noqa: BLE001
            fails = [f"{type(e).__name__}: {e}"]
        log(f"=== mutant {name}: {'KILLED' if fails else 'SURVIVED'}")
        if not fails:
            bad.append(name)
    print(f"teeth: {len(names) - len(bad)} killed, {len(bad)} survived {bad}")
    return 1 if bad else 0


def _sigterm(signum, frame):
    raise KeyboardInterrupt("SIGTERM")      # runs u64_hold's restore path


def main(argv=None) -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs")
    ap.add_argument("--backend", default="vice", choices=("vice", "u64"))
    ap.add_argument("--mhz", type=int, default=64)
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--attended", action="store_true")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    VERBOSE = a.verbose
    # a backgrounded job starts with SIGINT ignored; u64_hold's restore needs ^C
    import signal
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _sigterm)
    t0 = time.monotonic()
    if a.mutants:
        rc = teeth(a.prg, a.only.split(",") if a.only else None)
    elif a.attended:
        rc = u64(a.prg, a.mhz, lambda rig: attended(rig, a.seconds), "attended")
    elif a.backend == "u64":
        legs = a.legs.split(",") if a.legs else U64_LEGS
        fails = u64(a.prg, a.mhz, lambda rig: run_legs(rig, legs), f"{a.mhz}mhz")
        print(f"run_inp (u64 {a.mhz} MHz): {'PASS' if not fails else f'FAIL ({len(fails)})'}")
        rc = 1 if fails else 0
    else:
        legs = a.legs.split(",") if a.legs else ALL_LEGS
        fails = vice(a.prg, legs)
        print(f"run_inp: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
        rc = 1 if fails else 0
    print(f"run_inp: wall-clock {time.monotonic() - t0:.0f} s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
