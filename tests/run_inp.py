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
  cost    VICE: 6502 cycles of one inp_frame (CIA1 timer B, screen off,
          min of 5): the real scan, the override idle and with every input
          bit; the worst case.
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
        --mouse 1 [--stuck 0x01]        # W14-M: the 1351 windows (CUE lines)
    U64_HOST=10.53.21.158 python3 -m tests.run_inp --prg ... --attended \
        [--seconds 60] [--mhz 64]       # the user moves the real stick, then
                                        # holds F3 alone, then C= alone (5 s each)
    ... --backend u64 --allow-held 0x01 # a hardware port with a stuck direction:
                                        # WARN line + "warn" in --json, not a fail

Every leg that reads the real port first takes the undriven reading: it
must be $00 (bits --allow-held names may be held: a WARN line, and "warn"
in --json).  The scan's positive path (a real key or stick press through
CIA1) is covered only by --attended: VICE cannot hold a matrix key and
ignores JOYPORT_SET, and the Ultimate's inject_joystick writes the PRA
latch only.

Exit 0 = every selected leg passed (--mutants: every mutant killed;
--attended: every input was seen and the F3/C= check passed).
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
EVID = os.path.join(ROOT, "build", "evidence", "w14-mouse")
VBCC = os.environ.get("VBCC", os.path.expanduser("~/.local/opt/vbcc"))
FIXTURE = os.path.join(ROOT, "fixtures", "inp", "inp_log.s")

FSTUB = 0xC900            # php / sei / jsr inp_frame / plp / rts ($C900-$C96F: past the
MSTUB = 0xC908            # mach driver at $C200-$C7C1, before inp's module at $C980)
JOY0DAT, POTINP, CIAA_PRA = 0xDFF00A, 0xDFF016, 0xBFE001
JOY0 = 0x0A               # JOY0DAT in chip_shd (inp.s INP_JOY0)
LOG = 0x20000             # fixtures/inp/inp_log.s LOG
FRAMES = 16               # fixtures/inp/inp_log.s FRAMES
GUEST_S = 2               # mach_s for the guest runs (18 frames/s on NTSC phi2)
ALL_LEGS = ["api", "guest", "real", "cost", "stuck", "mouse", "msamp", "queue", "mreal"]
U64_LEGS = ["api", "guest", "real", "noinj"]
VERBOSE = False
STUCK = 0                 # --stuck: inp_stuck written by every reset (attended/demo)
ALLOW_HELD = 0            # --allow-held: stick bits a hardware port may read held
WARNS: list[str] = []     # the WARN lines of this run (summary / --json "warn")


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
        BSS block (inp_src .. inp_kp, 6 bytes) to idle; inp_stuck := *stuck*."""
        self.cia.call(self.lab("chip_reset"))
        if self.ok:
            assert self.lab("inp_n") == self.src + 4, "inp BSS layout"
            self.w(self.src, bytes([src, 0, 0, 0, 0, 0]))
            try:
                self.w(self.lab("inp_stuck"), bytes([STUCK]))
            except KeyError:
                pass                        # a PRG before inp_stuck
            if self.mouse:                  # W14-M: mport .. mok (11 bytes)
                assert self.lab("inp_mok") == self.lab("inp_mport") + 10, "inp BSS"
                self.w(self.lab("inp_mport"), bytes(11))
                try:                        # W14-M M2: mon .. md (7 bytes): no module
                    self.w(self.lab("inp_mon"), bytes(7))
                except KeyError:
                    pass

    @property
    def mouse(self) -> bool:
        try:
            self.lab("inp_msamp")
            return True
        except KeyError:
            return False

    def msamp(self) -> None:
        """Call inp_msamp (the 1351 sampler) once, like a slot firing."""
        a = self.lab("inp_msamp")
        self.w(MSTUB, bytes([0x08, 0x78, 0x20, a & 0xFF, a >> 8, 0x28, 0x60]))
        self.cia.call(MSTUB)

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
    # pins 0-5 must be kept (2-5 are W13-W's /CHNG, /WPRO, /TK0, /RDY) and
    # /FIR1 forced high: each script runs from two complementary pin patterns
    for name, sc, pin in (("fixed", script_fixed(), 0x2A),
                          ("fixed", script_fixed(), 0x15),
                          ("random", script_random(13, nrand), 0x15),
                          ("random", script_random(13, nrand), 0x2A)):
        name = f"{name}/pins${pin:02X}"
        rig.reset(src=1)
        rig.w(rig.pin, bytes([pin]))
        m = Inp(pin=pin)
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


def fixture_words(frames: int | None = None) -> list[int]:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "inp_log.bin")
        dfr = [f"-DFRAMES={frames}"] if frames else []
        r = subprocess.run([os.path.join(VBCC, "bin", "vasmm68k_mot"), "-quiet", *dfr,
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
    if rig.mouse:                       # W14-M: the override with inp_mper = 0
        am = rig.r(rig.lab("mach_am"), 1)[0]          # (run_irq's rig) arms nothing
        if am & 0x80:
            f.append(f"guest: mach_am ${am:02X}: slot 7 armed with inp_mper = 0")
    return f


def undriven(rig: Rig, leg: str, f: list[str]) -> int:
    """inp_joy with nothing driven (inp_src = 0).  Must be $00, except for
    bits --allow-held names (the C64U's port 2, 2026-10-09: Up held at every
    speed, even with PA0 driven high); a held bit is a loud WARN either way."""
    rig.frame()
    base = rig.state()["joy"]
    if base:
        w = (f"WARN: port 2 reads ${base:02X} with nothing driven ({leg}): a "
             "direction or fire is held at the port (stuck stick?)")
        print(w, flush=True)
        WARNS.append(w)
    if base & ~ALLOW_HELD:
        f.append(f"{leg}: port 2 reads ${base:02X} with nothing driven; only "
                 f"--allow-held ${ALLOW_HELD:02X} may be (a stuck stick, or the "
                 "stick read is broken: e.g. its polarity)")
    return base


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
    base = undriven(rig, "real", f)

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
        # W14-M: CIA1 port A rests selecting the mouse port's pots for the
        # SID: DDRA $C0, PA7-6 = 01 (port 1, inp_mport 0) or 10 (port 2)
        for mport, sel in ((0, 0x40), (1, 0x80)):
            if not rig.mouse and mport:
                break
            if rig.mouse:
                rig.w(rig.lab("inp_mport"), bytes([mport]))
            rig.frame()
            pra, _, ddra, ddrb = rig.r(0xDC00, 4)
            if (pra & 0xC0, ddra, ddrb) != (sel, 0xC0, 0x00):
                f.append(f"real: inp_mport {mport}: CIA1 left PRA ${pra:02X} DDRA "
                         f"${ddra:02X} DDRB ${ddrb:02X}, want PA7-6 ${sel:02X}, "
                         "DDRA $C0, DDRB $00 (the pot select at rest)")
        if rig.mouse:
            rig.w(rig.lab("inp_mport"), b"\0")
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
    base = undriven(rig, "noinj", f)
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


# --------------------------------------------------------------------------
# cost: 6502 cycles per inp_frame (CIA1 timer B, phi2 = one cycle in VICE)
# --------------------------------------------------------------------------

CSTUB, COUT = 0xC910, 0xC960        # (as FSTUB: outside the mach driver)


def cycles(rig: Rig, target: int) -> int:
    """php/sei; TB := $FFFF one-shot, started; jsr target; TB stopped;
    TB -> COUT; plp.  Returns $FFFF - TB (fixed overhead included)."""
    lo, hi = target & 0xFF, target >> 8
    rig.w(CSTUB, bytes([0x08, 0x78, 0xA9, 0x00, 0x8D, 0x0F, 0xDC,
                        0xA9, 0xFF, 0x8D, 0x06, 0xDC, 0x8D, 0x07, 0xDC,
                        0xA9, 0x19, 0x8D, 0x0F, 0xDC,          # load, one-shot, go
                        0x20, lo, hi,
                        0xA9, 0x00, 0x8D, 0x0F, 0xDC,          # stop
                        0xAD, 0x06, 0xDC, 0x8D, COUT & 0xFF, COUT >> 8,
                        0xAD, 0x07, 0xDC, 0x8D, (COUT + 1) & 0xFF, COUT >> 8,
                        0x28, 0x60]))
    rig.cia.call(CSTUB)
    tb = struct.unpack("<H", rig.r(COUT, 2))[0]
    return 0xFFFF - tb


def leg_cost(rig: Rig) -> list[str]:
    """Cycles of one inp_frame: the real scan with nothing held, the
    override idle and with every input bit (all four direction bits: the
    mouse loop's worst case; an F1 edge).  The worst case is the real scan
    plus the input-dependent part: real0 + (ovr_all - ovr0)."""
    rig.need()
    d011 = rig.r(0xD011, 1)[0]
    rig.w(0xD011, bytes([d011 & ~0x10 & 0x7F]))  # screen off: no badline steals
    try:
        rig.w(0xC7F8, b"\x60")                  # an rts: the stub's overhead
        best = lambda: min(cycles(rig, rig.frame_addr) for _ in range(5))
        over = min(cycles(rig, 0xC7F8) for _ in range(5))
        rig.reset(src=0)
        real0 = best() - over
        rig.reset(src=1)
        ovr0 = best() - over
        rig.set_input(0x1F, F1 | CBM)
        ovr_all = min(cycles(rig, rig.frame_addr) for _ in range(5)) - over
        msamp = rearm = None
        extra = ""
        if rig.mouse:                           # W14-M: the 1351 sampler
            rig.reset(src=1)
            rig.w(rig.lab("inp_mper"), struct.pack("<I", 2048))
            rig.w(rig.lab("inp_mpot"), bytes([0x40, 0x40]))
            cycles(rig, rig.lab("inp_msamp"))   # the reference
            rig.w(rig.lab("inp_mpot"), bytes([0x7E, 0x02]))   # both axes move
            msamp = min(cycles(rig, rig.lab("inp_msamp")) for _ in range(5)) - over
            rig.reset(src=0)
            rig.w(rig.lab("inp_mper"), struct.pack("<I", 2048))
            rearm = best() - over - real0
            rig.w(rig.lab("inp_mper"), bytes(4))
            try:                                # W14-M M2: the module armed
                rig.lab("inp_mon")
                rig.reset(src=1)
                stage_inpm(rig)
                mframe = best() - over - ovr0
                rig.w(rig.lab("inp_mpot"), bytes([0x40, 0x40]))
                cycles(rig, rig.lab("inp_msamp"))
                msamp2 = min(cycles(rig, rig.lab("inp_msamp")) for _ in range(5)) - over
                rig.reset(src=0)
                stage_inpm(rig)
                mframer = best() - over - real0
                extra = (f"; M2 module armed: frame +{mframe} (override) / +{mframer} "
                         f"(real), sample {msamp2}")
            except KeyError:
                extra = ""
    finally:
        rig.w(0xD011, bytes([d011 & 0x7F]))
    worst = real0 + ovr_all - ovr0
    log(f"  cost (screen off, min of 5): inp_frame real scan {real0}, override "
        f"idle {ovr0}, override all bits {ovr_all} cycles; worst case {worst} "
        f"cycles; the real scan adds {real0 - ovr0} (stub overhead {over})"
        + (f"; W14-M: inp_msamp {msamp} cycles per sample (with its re-arm), the "
           f"frame hook's re-arm +{rearm}" if msamp is not None else "") + extra)
    rig.reset(src=1)
    return [] if 0 < worst < 1000 else [f"cost: implausible {worst} cycles"]


def leg_mouse(rig: Rig) -> list[str]:
    """W14-M: the 1351 decode (tests/aros/inpm.s, staged and armed) with the
    override (inp_src = 1; VICE's 1351 takes the host mouse only, CONTRACTS
    "Input"): inp_msamp called by the host like a slot firing, a frame every
    4 samples; scripted pots (POTY, POTX) with bit-0/bit-7 noise, the mod-64
    wrap, the +-31 limit; JOY0DAT against host/inp_ref after every sample.
    The clamp (rev14-mouse HIGH-1): 14 samples of +31 counts inside one
    frame deliver +127 (X) / -127 (Y), not a wrap.  Then the buttons (lines
    in inp_ml, each held 2 frames): left -> /FIR0, right -> POTINP DATLY,
    OR-ed with the stick's fire and C=."""
    rig.need()
    if not rig.mouse:
        return ["mouse: this PRG has no inp_msamp (no W14-M)"]
    f: list[str] = []
    rig.reset(src=1)
    stage_inpm(rig)
    pot, ml = rig.lab("inp_mpot"), rig.lab("inp_ml")
    m = Inp()
    rng = random.Random(1351)
    y = x = 0x40
    seq = [(y, x)]
    for i in range(120):                       # a square, then random moves
        dy, dx = ((0, 6), (6, 0), (0, -6), (-6, 0))[i // 30]
        y, x = (y + 2 * dy) & 0x7E, (x + 2 * dx) & 0x7E
        seq.append((y, x))
    for _ in range(80):
        y = (y + 2 * rng.randint(-31, 31)) & 0x7E
        x = (x + 2 * rng.randint(-31, 31)) & 0x7E
        seq.append((y, x))

    def sample(i, ny, nx):
        rig.w(pot, bytes([ny, nx]))
        rig.msamp()
        want = m.msamp(ny, nx)["joy0dat"]
        got = rig.state()["joy0dat"]
        if got != want:
            f.append(f"mouse sample {i}: pots ${ny:02X} ${nx:02X}: JOY0DAT "
                     f"${got:04X}, want ${want:04X}")
        return got

    for i, (py, px) in enumerate(seq):
        if i % 4 == 0:
            rig.frame()
            m.frame(0, 0)
        ny, nx = py | rng.randint(0, 1), px | rng.randint(0, 1)   # bit-0 noise
        if rng.random() < 0.2:
            ny |= 0x80                          # bit 7 (not a 1351 bit)
        sample(i, ny, nx)
        if len(f) > 5:
            return f
    # the clamp: 14 x +31 counts in one frame (X right, Y up = Amiga -)
    rig.frame()
    m.frame(0, 0)
    j0 = rig.state()["joy0dat"]
    for k in range(14):
        y, x = (y + 62) & 0x7E, (x + 62) & 0x7E
        j1 = sample(f"clamp{k}", y, x)
    dy = ((j1 >> 8) - (j0 >> 8)) & 0xFF
    dx = ((j1 & 0xFF) - (j0 & 0xFF)) & 0xFF
    log(f"  mouse clamp: 14 x +31 counts in one frame -> dx {dx} (want 127), "
        f"dy {dy - 256 if dy > 127 else dy} (want -127)")
    if (dx, dy) != (127, 0x81):
        f.append(f"mouse clamp: one frame's 14 x +31 counts gave dx {dx} dy {dy}, "
                 "want +127 / -127 (AROS reads s8 per frame: no wrap)")
    # a port change takes a new reference (rev14-mouse LOW h): a jump of
    # +20 counts across the change must not move the pointer
    rig.frame()
    m.frame(0, 0)
    j0 = rig.state()["joy0dat"]
    rig.w(rig.lab("inp_mport"), b"\x01")
    rig.frame()                                 # the module sees the change
    rig.w(pot, bytes([y, (x + 40) & 0x7E]))
    rig.msamp()
    rig.w(rig.lab("inp_mport"), b"\x00")
    rig.frame()
    rig.w(pot, bytes([y, (x + 80) & 0x7E]))
    rig.msamp()
    j1 = rig.state()["joy0dat"]
    log(f"  mouse port change: JOY0DAT ${j0:04X} -> ${j1:04X} (want unchanged)")
    if j1 != j0:
        f.append(f"mouse: across a port change JOY0DAT ${j0:04X} -> ${j1:04X}: the "
                 "first sample after a change must be a new reference")
    m.mold = [y & 0x7E, (x + 80) & 0x7E]
    for joy, key, b in ((0, 0, FIRE), (0, 0, UP), (0, 0, FIRE | UP),
                        (FIRE, 0, UP), (0, CBM, FIRE), (0, 0, 0)):
        rig.set_input(joy, key)
        rig.w(ml, bytes([b]))
        rig.frame()
        rig.frame()                             # the queue delivers within 2 frames
        m.frame(joy, key, mb=b)
        want = m.frame(joy, key, mb=b)
        g = rig.api_read()
        if (g["pra"], g["potinp"]) != (want["pra"], want["potinp"]):
            f.append(f"mouse buttons joy ${joy:02X} key ${key:02X} lines ${b:02X}: "
                     f"PRA ${g['pra']:02X} POTINP ${g['potinp']:04X}, want "
                     f"${want['pra']:02X} ${want['potinp']:04X}")
    rig.reset(src=1)
    log(f"  mouse: {len(seq)} samples + the clamp, end JOY0DAT ${m.joy0dat:04X}, "
        f"{'ok' if not f else 'FAIL'}")
    return f


def leg_msamp(rig: Rig) -> list[str]:
    """W14-M: the sampler live under mach_run (the production path: slot 7,
    its re-arms, the module armed), s = 3 as on the C64U.
    Real source (inp_src = 0): the frame hook arms slot 7 (inp_mper =
    8192 phi2 = $2000 >> s, mach_cbv[7] = inp_msamp, as the launchers set
    them); the slot fires; the module reads the SID (VICE, sound off: $FF
    $FF): inp_mok = 1, JOY0DAT still; the samples per guest frame
    (inp_mns): at most 14188 E / 1024 E = 13.9 (+10%), at least half of it
    (VICE at 1 MHz: ~9, each sample's pass delays the next; a sampler that
    re-arms only from the frame hook takes 1).  Override: a POTX +20 / POTY -10 count
    step written before the run is integrated exactly ($0A14).  Red: no
    slot, inp_mok stays 0."""
    rig.need()
    if not rig.mouse:
        return ["msamp: this PRG has no inp_msamp (no W14-M)"]
    from tests.run_mach import clean
    f: list[str] = []
    s = 3
    per = 0x2000 >> s
    mr = rig.cia.mach_rig()
    (pc,) = mr.stage(fixture_words())
    rig._stub = False
    rig.cia._tramp = None
    rig.reset(src=0)
    stage_inpm(rig)
    a = rig.lab("inp_msamp")
    rig.w(rig.lab("mach_cbv") + 14, bytes([a & 0xFF, a >> 8]))
    rig.w(rig.lab("inp_mper"), struct.pack("<I", per))
    n0 = struct.unpack("<H", rig.r(rig.lab("inp_mns"), 2))[0]
    res = mr.run(pc, s=s, budget=8_000_000)
    clean(res, "msamp", f)
    ns = (struct.unpack("<H", rig.r(rig.lab("inp_mns"), 2))[0] - n0) & 0xFFFF
    want = FRAMES * 14188 / per
    mok = rig.r(rig.lab("inp_mok"), 1)[0]
    mpot = rig.r(rig.lab("inp_mpot"), 2)
    jd = rig.state()["joy0dat"]
    log(f"  msamp (s={s}): {ns} samples in {FRAMES} frames ({ns / FRAMES:.1f} a frame, "
        f"want ~{want / FRAMES:.1f}), inp_mok {mok}, inp_mpot {mpot.hex()}, JOY0DAT ${jd:04X}")
    # the re-arm counts from NOW (the scan's end), not a stale E: ~80 ms of
    # 6502 time after the run (mach_et untouched), then a frame must arm
    # slot 7 at E now + inp_mper
    rig.w(CSTUB, bytes([0x08, 0x78, 0xA0, 0x40, 0xA2, 0x00, 0xCA, 0xD0, 0xFD, 0x88,
                        0xD0, 0xF8, 0x28, 0x60]))   # 64 x 256 x 5 cycles
    rig.cia.call(CSTUB)
    rig.frame()
    et = rig.lab("mach_et")
    e_stub = rig.lab("mach_etime")
    rig.w(CSTUB, bytes([0x08, 0x78, 0x20, e_stub & 0xFF, e_stub >> 8, 0x28, 0x60]))
    rig.cia.call(CSTUB)
    now = struct.unpack("<I", rig.r(et, 4))[0]
    dl = struct.unpack("<I", rig.r(rig.lab("mach_dl") + 4 * 7, 4))[0]
    ahead = (dl - now + (1 << 31)) % (1 << 32) - (1 << 31)
    log(f"  msamp: after a frame scan slot 7 is due {ahead} E ticks ahead (want 0..{per}: "
        "one period from the scan, the SID's 512 phi2 settle)")
    if not 0 <= ahead <= per:
        f.append(f"msamp: slot 7 due {ahead} E ticks from now after a scan, want 0..{per} "
                 "(marm must take E now, not a stale mach_et)")
    if mok != 1 or mpot != b"\xff\xff" or jd != 0:
        f.append(f"msamp: inp_mok {mok}, inp_mpot {mpot.hex()}, JOY0DAT ${jd:04X} "
                 "(want 1, ffff, $0000: the slot fired and read the SID)")
    # at 1 MHz (VICE) each sample's pass delays the next (~9 a frame); the
    # Ultimate at 64 MHz comes close to 13.9.  A sampler that re-arms only
    # from the frame hook takes 1 a frame, a stale deadline far more.
    if not 0.5 * want <= ns <= 1.1 * want:
        f.append(f"msamp: {ns} samples in {FRAMES} frames, want {0.5 * want:.0f}-"
                 f"{1.1 * want:.0f} (8192 phi2 a sample: the sampler re-arms itself)")
    rig.reset(src=1)                            # the override: reference, then a step
    stage_inpm(rig)
    rig.w(rig.lab("mach_cbv") + 14, bytes([a & 0xFF, a >> 8]))
    rig.w(rig.lab("inp_mper"), struct.pack("<I", per))
    rig.w(rig.lab("inp_mpot"), bytes([0x40, 0x40]))
    rig.frame()                                  # (the source changed: the module
    rig.msamp()                                  # takes a new reference) the reference
    rig.w(rig.lab("inp_mpot"), bytes([0x40 - 20, 0x40 + 40]))   # POTY -10, POTX +20
    res = mr.run(pc, s=s, budget=8_000_000)
    clean(res, "msamp override", f)
    jd = rig.state()["joy0dat"]
    log(f"  msamp override: JOY0DAT ${jd:04X} (want $0A14)")
    if jd != 0x0A14:
        f.append(f"msamp override: JOY0DAT ${jd:04X}, want $0A14 (the slot did not "
                 "integrate the host's pot step)")
    rig.w(rig.lab("inp_mper"), bytes(4))
    rig.reset(src=1)
    return f


def stage_inpm(rig: Rig, defines: tuple = ()) -> bytes:
    """W14-M: build tests/aros/inpm.s (A at $C980, B at $CF00) for this PRG,
    stage both, arm it (tests/aros_wb.arm_inpm).  Returns A."""
    from tests import aros_wb as WB
    d = os.path.join(os.path.dirname(os.path.abspath(rig.prg)), "inpm-mod")
    lbl = os.path.splitext(rig.prg)[0] + ".lbl"
    src = None
    if QMUT[0]:                                  # a module mutant (--qmutants)
        text = open(os.path.join(ROOT, "tests", "aros", "inpm.s")).read()
        a, b = QMUTANTS[QMUT[0]]
        assert text.count(a) == 1, f"qmutant {QMUT[0]}: anchor"
        os.makedirs(d, exist_ok=True)
        src = os.path.join(d, f"inpm_{QMUT[0]}.s")
        open(src, "w").write(text.replace(a, b))
    ma, mb = WB.build_inpm2(lbl, d, src, defines)
    rig.w(WB.INPM_A[0], ma)
    rig.w(WB.INPM_B[0], mb)
    WB.arm_inpm(type("M", (), {"w": staticmethod(rig.w), "lab": staticmethod(rig.lab)}), ma)
    return ma


QMUT = [None]
QMUTANTS = {    # tests/aros/inpm.s mutants (python3 -m tests.run_inp --qmutants)
    # no queue: the delivered left button is the lines now (stretch lost)
    "qnone": (":       lda inp_md              ; the left button: one transition a frame\n        beq rel\n",
              ":       tya\n        and #$10\n        bpl set\n"),
    # a pending press does not force the release frame (P,P instead of P,R,P)
    "qnorel": ("        lda inp_mq              ; delivered: release when up now or a press\n        bne up                  ; is pending\n",
               "        lda inp_mq              ; delivered: release when up now or a press\n        nop\n        nop\n"),
    # the sampler does not count edges (only the frame looks)
    "qnosamp": ("bbody:  jsr look\n", "bbody:  nop\n        nop\n        nop\n"),
    # the 1351's Y not negated (the pointer moves the wrong way)
    "msign": ("        eor #$FF                ; Y: negate (C = 0)\n        adc #1\n", ""),
    # bit 0 (noise) kept in the position
    "mnoise": ("lp:     lda inp_mpot,x\n        and #$7E\n", "lp:     lda inp_mpot,x\n        and #$7F\n"),
    # the first sample is a move, not the reference
    "mref": ("        lda inp_mok\n        beq nx                  ; reference only\n", ""),
    # no clamp: every sample's delta added (rev14-mouse HIGH-1: AROS wraps)
    "noclamp": ("        bvc :+\n        lda inp_t\n        bmi neg\n        lda #$7F\n        bne :+\nneg:    lda #$81\n:       cmp #$80\n        bne :+\n        lda #$81\n:       tay",
                "        lda inp_t\n        clc\n        adc inp_macc,x\n        tay"),
    # the module leaves PA7-6 released (DDRA $00): the pot select floats
    "qnorest": (":       ldy #$C0\n        sty CIA1_DDRA           ; the pot select at rest again\n",
                ":       ldy #$00\n        sty CIA1_DDRA           ; the pot select at rest again\n"),
    # the module reads the wrong port for the mouse lines
    "qport": ("        lda CIA1_PRB            ; port 1\n        ldy inp_mport\n        beq :+\n        lda CIA1_PRA            ; port 2\n",
              "        lda CIA1_PRA            ; port 1\n        ldy inp_mport\n        beq :+\n        lda CIA1_PRB            ; port 2\n"),
    # a port/source change keeps the old reference (a jump)
    "qnomps": ("        sta inp_mps\n        lda #0\n        sta inp_mok\n", "        sta inp_mps\n"),
}
QMUTANT_LEGS = {"qnone": ["queue"], "qnorel": ["queue"], "qnosamp": ["queue"],
                "msign": ["mouse"], "mnoise": ["mouse"], "mref": ["mouse"],
                "noclamp": ["mouse"], "qnorest": ["mreal"], "qport": ["mreal"],
                "qnomps": ["mouse"]}


def leg_queue(rig: Rig) -> list[str]:
    """W14-M M2: the button edge queue (tests/aros/inpm.s) with the
    override (inp_ml): press edges seen by the sampler are delivered one
    left transition per frame.  Two clicks inside one frame -> P,R,P,R; a
    click shorter than a frame -> one pressed frame; a held button stays;
    the right button is the lines now (not queued); both at once; each
    against host/inp_ref.Queue.
    Then no module: an unstaged module (inp_mon set, $C980 cleared) is
    disarmed by the guard and the buttons behave as M1 (immediate)."""
    rig.need()
    if not rig.mouse:
        return ["queue: no inp_msamp (no W14-M)"]
    try:
        rig.lab("inp_mon")
    except KeyError:
        return ["queue: no inp_mon (no M2 stub)"]
    from host.inp_ref import Queue
    f: list[str] = []
    rig.reset(src=1)
    rig.w(rig.lab("inp_mpot"), bytes([0x40, 0x40]))
    mod = stage_inpm(rig)
    ml = rig.lab("inp_ml")
    cases = [("double", [FIRE, 0, FIRE, 0], [0] * 5),
             ("short", [FIRE, 0], [0] * 3),
             ("held", [FIRE], [FIRE] * 3 + [0, 0]),
             ("dblheld", [FIRE, 0, FIRE], [FIRE] * 4 + [0]),     # 2nd click still down
             ("right", [UP, 0, UP, 0], [0, UP, UP, 0]),        # not queued: now
             ("both", [FIRE | UP, 0, FIRE | UP, 0], [UP, UP, 0, 0, 0])]
    for name, looks, frames in cases:
        q = Queue()
        rig.w(rig.lab("inp_mlast"), bytes(4))
        rig.w(rig.lab("inp_mb"), b"\0")
        for lines in looks:                     # samples between two frames
            rig.w(ml, bytes([lines]))
            rig.msamp()
            q.look(lines)
        got, want = [], []
        for lines in frames:
            rig.w(ml, bytes([lines]))
            rig.frame()
            want.append(q.frame(lines))
            g = rig.api_read()
            got.append((0 if g["pra"] & 0x40 else FIRE) | (UP if g["potinp"] == 0xFB00 else 0))
        log(f"  queue/{name}: delivered {['%02X' % x for x in got]}")
        if got != want:
            f.append(f"queue/{name}: delivered {got}, want {want} (host/inp_ref.Queue)")
    # no module staged: the guard disarms, the buttons are M1's
    rig.w(0xC980, bytes(len(mod)))
    rig.w(rig.lab("inp_mb"), bytes([FIRE]))
    rig.frame()
    g = rig.api_read()
    mon = rig.r(rig.lab("inp_mon"), 1)[0]
    if mon or g["pra"] & 0x40:
        f.append(f"queue/nomodule: inp_mon ${mon:02X} after a frame, PRA ${g['pra']:02X} "
                 "(want 0: disarmed, and the M1 button through inp_mb)")
    rig.reset(src=1)
    return f


def leg_mreal(rig: Rig) -> list[str]:
    """W14-M: the real CIA1 path with the module armed (inp_src = 0).  Port 1's
    lines are driven low by the CIA itself (DDRB/PRB, as leg real: VICE
    cannot hold a stick or a key), port 2's cannot be (DDRA = $00 releases
    them).  Routing: inp_mport 0 -> port 1 is the mouse (PB4 = left: /FIR0
    after the queue; PB0 = right: POTINP), not the stick; inp_mport 1 ->
    port 1 is the stick (PB4 = fire in inp_joy), not the mouse.  inp_stuck
    masks the stick only, never the mouse's lines.  After every frame CIA1
    DDRA = $C0 (the module put the pot select back)."""
    rig.need()
    if not rig.mouse:
        return ["mreal: no W14-M"]
    f: list[str] = []
    DDRB, PRB = 0xDC03, 0xDC01
    mbl = rig.lab("inp_mb")

    def run(mport, stuck, drive, frames=3):
        rig.reset(src=0)
        rig.w(rig.lab("inp_mport"), bytes([mport]))
        rig.w(rig.lab("inp_stuck"), bytes([stuck]))
        stage_inpm(rig)
        out = []
        try:
            for _ in range(frames):
                rig.w(PRB, bytes([0xFF ^ drive]))
                rig.w(DDRB, bytes([drive]))
                rig.frame()
                rig.w(DDRB, b"\0")
                g = rig.api_read()
                ddra = rig.r(0xDC02, 1)[0]
                out.append({"joy": rig.state()["joy"], "mb": rig.r(mbl, 1)[0],
                            "fir0": not g["pra"] & 0x40, "rmb": g["potinp"] == 0xFB00,
                            "ddra": ddra})
        finally:
            rig.w(DDRB, b"\0")
            rig.w(rig.lab("inp_stuck"), b"\0")
        return out

    for mport, stuck, drive, want in (
            (0, 0, 0x10, {"joy": 0, "fir0": True, "rmb": False}),      # mouse left
            (0, 0, 0x01, {"joy": 0, "fir0": False, "rmb": True}),      # mouse right
            (0, 0x11, 0x11, {"joy": 0, "fir0": True, "rmb": True}),   # stuck: stick only
            (1, 0, 0x10, {"joy": FIRE, "fir0": True, "rmb": False}),   # port 1 = the stick
            (1, 0, 0x01, {"joy": UP, "fir0": False, "rmb": False}),
            (1, 0x10, 0x10, {"joy": 0, "fir0": False, "rmb": False})): # stuck stick fire
        st = run(mport, stuck, drive)[-1]
        bad = [k for k, v in want.items() if st[k] != v]
        if st["ddra"] != 0xC0:
            bad.append("ddra")
        log(f"  mreal mport {mport} stuck ${stuck:02X} PB low ${drive:02X}: {st}"
            f"{'' if not bad else ' FAIL ' + str(bad)}")
        if bad:
            f.append(f"mreal mport {mport} stuck ${stuck:02X} drive ${drive:02X}: {st}, "
                     f"want {want} and DDRA $C0")
    rig.reset(src=1)
    return f


def leg_stuck(rig: Rig) -> list[str]:
    """inp_stuck (W13-I demo, 2026-10-11).  VICE cannot hold a port-A line
    (it ignores JOYPORT_SET; DDRB/PRB only drives rows, and the stick read
    has DDRA = $00), so the masking itself is shown by host/inp_ref
    (test_scan_stuck_mask) and on the device (--attended --stuck $01).
    Here: the real scan with every mask still reads idle and moves nothing;
    the override ignores the mask (UP and DOWN with inp_stuck = $1F move);
    a reset leaves inp_stuck as the host set it."""
    global STUCK
    rig.need()
    f: list[str] = []
    a = rig.lab("inp_stuck")
    saved = STUCK
    try:
        for m in (0x01, 0x1F):
            STUCK = m
            rig.reset(src=0)
            for _ in range(3):
                rig.frame()
            st = rig.state()
            if st["joy"] or st["joy0dat"] or rig.r(a, 1)[0] != m:
                f.append(f"stuck ${m:02X}: real scan idle gave {st}, inp_stuck "
                         f"${rig.r(a, 1)[0]:02X}")
        STUCK = 0x1F
        rig.reset(src=1)
        m = Inp()
        for joy in (UP, UP, DOWN, DOWN | FIRE):
            rig.set_input(joy, 0)
            rig.frame()
            want = m.frame(joy, 0)
            got = rig.state()
            if (got["joy0dat"], got["pra"]) != (want["joy0dat"], want["pra"]):
                f.append(f"stuck $1F, override joy ${joy:02X}: got JOY0DAT "
                         f"${got['joy0dat']:04X} PRA ${got['pra']:02X}, want "
                         f"${want['joy0dat']:04X} ${want['pra']:02X} (the override "
                         "must ignore inp_stuck)")
    finally:
        STUCK = saved
        rig.reset(src=1)
    log(f"  stuck: {'ok' if not f else 'FAIL'}")
    return f


LEGS = {"api": leg_api, "guest": leg_guest, "real": leg_real, "noinj": leg_noinj, "cost": leg_cost, "stuck": leg_stuck,
        "mouse": leg_mouse, "msamp": leg_msamp, "queue": leg_queue, "mreal": leg_mreal}


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


def mouse_stub(rig: Rig, n: int) -> bytes:
    """php/sei; n times (raster-paced, ~10 ms): jsr inp_frame; jsr inp_msamp;
    OR inp_joy, inp_key, inp_view (consumed), inp_mb into AFLAGS+0..3; count
    the mouse buttons' press edges (left = bit 4 at AFLAGS+4, right = bit 0
    at AFLAGS+5; the last inp_mb at AFLAGS+6); plp/rts."""
    lo, hi = lambda a: a & 0xFF, lambda a: a >> 8
    f, ms = rig.frame_addr, rig.lab("inp_msamp")
    j, k, v, mb = rig.src + 1, rig.src + 2, rig.src + 3, rig.lab("inp_mb")
    c = [0x08, 0x78, 0xA2, n]
    top = len(c)
    c += [0x8A, 0x48, 0x20, lo(f), hi(f), 0x20, lo(ms), hi(ms)]
    for src, dst in ((j, AFLAGS), (k, AFLAGS + 1), (v, AFLAGS + 2), (mb, AFLAGS + 3)):
        c += [0xAD, lo(src), hi(src), 0x0D, lo(dst), hi(dst), 0x8D, lo(dst), hi(dst)]
    c += [0xA9, 0x00, 0x8D, lo(v), hi(v)]
    p6 = AFLAGS + 6
    c += [0xAD, lo(p6), hi(p6), 0x49, 0xFF, 0x2D, lo(mb), hi(mb)]   # new presses
    c += [0xAE, lo(mb), hi(mb), 0x8E, lo(p6), hi(p6)]                # prev := mb
    c += [0x4A, 0x90, 0x03, 0xEE, lo(AFLAGS + 5), hi(AFLAGS + 5)]    # bit 0: right
    c += [0x29, 0x08, 0xF0, 0x03, 0xEE, lo(AFLAGS + 4), hi(AFLAGS + 4)]  # bit 4: left
    c += [0xAD, 0x12, 0xD0, 0xD0, 0xFB, 0xAD, 0x12, 0xD0, 0xF0, 0xFB,
          0x68, 0xAA, 0xCA, 0xD0]
    c += [(top - (len(c) + 1)) & 0xFF, 0x28, 0x60]
    assert len(c) < 0xF0
    return bytes(c)


MOUSE_WINDOWS = [   # (seconds, cue, check)
    (20, "MOUSE: move it in a SQUARE, slowly: right, then down, then left, then up", "square"),
    (15, "MOUSE: LEFT button: click it 3 times", "left3"),
    (15, "MOUSE: RIGHT button: click it 3 times", "right3"),
    (15, "MOUSE: hold BOTH buttons together for a second, then release", "both"),
    (15, "KEYBOARD: hold SPACE for 5 s, then the 1 key for 5 s (expect NO mouse click)", "kbiso"),
    (15, "KEYBOARD: hold C= alone (the right button through the keyboard)", "cbm"),
    (20, "JOYSTICK (the other port): up, down, left, right, then fire", "stick"),
]
ATT_S = 3                       # mach_s, as the AROS boot on the Ultimate


def square_order(deltas: list) -> list:
    """The per-frame (dx, dy) as direction runs (>= 3 frames): R, D, L, U."""
    runs, cur, n = [], None, 0
    for dx, dy in deltas:
        d = None
        if abs(dx) >= abs(dy) and abs(dx) >= 2:
            d = "R" if dx > 0 else "L"
        elif abs(dy) >= 2:
            d = "D" if dy > 0 else "U"
        if d == cur:
            n += 1
        else:
            if cur and n >= 3 and (not runs or runs[-1] != cur):
                runs.append(cur)
            cur, n = d, 1
    if cur and n >= 3 and (not runs or runs[-1] != cur):
        runs.append(cur)
    return runs


def attended_mouse(rig: Rig, mport: int) -> int:
    """W14-M: the 1351 through the PRODUCTION path (rev14-mouse 5): the
    68k fixture runs under mach_run (s = 3) for each window, so slot 7's
    sampler, its re-arms, the frame hook and the input module (armed) do
    the work; the fixture logs JOY0DAT, CIA-A PRA and POTINP every guest
    frame, the host evaluates the log after the window.  Seven windows,
    each with a timestamped CUE and GO line (the supervisor relays them),
    an END line and a verdict.  "square" checks the direction ORDER (right,
    down, left, up as frame-delta runs); "kbiso" (rev14-mouse MEDIUM-3):
    SPACE and 1 held with the mouse on port 1 must give no click."""
    rig.need()
    if not rig.mouse:
        print("ATTENDED MOUSE: this PRG has no inp_msamp")
        return 1
    from tests.run_mach import clean
    mr = rig.cia.mach_rig()
    secs_max = max(w[0] for w in MOUSE_WINDOWS)
    fps = 72.08 / 2 ** ATT_S
    nfr = int(secs_max * fps) + 2
    (pc,) = mr.stage(fixture_words(nfr))
    rig._stub = False
    rig.cia._tramp = None
    rig.reset(src=0)
    rig.w(rig.lab("inp_mport"), bytes([mport]))
    stage_inpm(rig)
    a = rig.lab("inp_msamp")
    rig.w(rig.lab("mach_cbv") + 14, bytes([a & 0xFF, a >> 8]))
    rig.w(rig.lab("inp_mper"), struct.pack("<I", 0x2000 >> ATT_S))
    idle = undriven(rig, "attended-mouse", [])
    print("=" * 72)
    print(f"ATTENDED MOUSE: the 1351 in PORT {1 + mport}, the joystick in port "
          f"{2 - mport}.  {len(MOUSE_WINDOWS)} windows of ~{nfr / fps:.0f} s each; "
          "each starts with a CUE line, then GO.")
    print("=" * 72, flush=True)
    results = {}
    for secs, cue, chk in MOUSE_WINDOWS:
        print(f"CUE {time.strftime('%H:%M:%S')} (~{nfr / fps:.0f} s, starts in 5 s): {cue}",
              flush=True)
        time.sleep(5.0)
        print(f"GO  {time.strftime('%H:%M:%S')}: {cue}", flush=True)
        n0 = struct.unpack("<H", rig.r(rig.lab("inp_mns"), 2))[0]
        res = mr.run(pc, s=ATT_S, budget=50_000_000)
        ok_run = clean(res, chk, [])
        raw = mr.guest_read(LOG, 6 * nfr)
        ns = (struct.unpack("<H", rig.r(rig.lab("inp_mns"), 2))[0] - n0) & 0xFFFF
        rows = [struct.unpack_from(">HBBH", raw, 6 * k) for k in range(nfr)]
        deltas, prev = [], rows[0][0]
        for jd, _p, _z, _pot in rows[1:]:
            dy = ((jd >> 8) - (prev >> 8) + 128) % 256 - 128
            dx = ((jd & 0xFF) - (prev & 0xFF) + 128) % 256 - 128
            deltas.append((dx, dy))
            prev = jd
        left = [not r[1] & 0x40 for r in rows]
        right = [r[3] == 0xFB00 for r in rows]
        lp = sum(1 for k in range(1, nfr) if left[k] and not left[k - 1])
        rp = sum(1 for k in range(1, nfr) if right[k] and not right[k - 1])
        both = any(l and r for l, r in zip(left, right))
        order = square_order(deltas)
        sq = "".join(order)
        joy = rig.state()["joy"]
        ok = {"square": "RDLU" in sq * 2,
              "left3": lp >= 3, "right3": rp >= 3, "both": both,
              "kbiso": lp == 0 and rp == 0,
              "cbm": rp >= 1,
              "stick": any(abs(dx) + abs(dy) for dx, dy in deltas)}[chk]
        results[chk] = ok and ok_run
        print(f"END {time.strftime('%H:%M:%S')}: {chk}: {'OK' if ok else 'NOT SEEN'} "
              f"(direction runs {sq or '-'}; left presses {lp}, right presses {rp}, "
              f"both {both}; 1351 samples {ns} in {nfr} frames; stick ${joy:02X}"
              f"{'' if ok_run else '; the guest run did not end cleanly'})", flush=True)
    rig.w(rig.lab("inp_mper"), bytes(4))
    missing = [k for k, v in results.items() if not v]
    print(f"ATTENDED MOUSE VERDICT: {'PASS' if not missing else 'INCOMPLETE'}"
          f"{': missing ' + str(missing) if missing else ''}")
    return 0 if not missing else 1


# --------------------------------------------------------------------------
# --mouse-diag: the raw 1351 lines and pots on the device (supervisor,
# 2026-10-11: the real 1351's pots stay in a ~32-unit window at 64 MHz)
# --------------------------------------------------------------------------

DIAG_ORG, DIAG_PRM, DIAG_DATA, DIAG_N = 0xC500, 0xC600, 0xC000, 300   # data $C000-$C4AF
DIAG_SRC = """
.ifndef inp_frame
.error "-D inp_frame"
.endif
.ifndef inp_msamp
.error "-D inp_msamp"
.endif
PRM  = $C600        ; +0 mode (0 = no scan: the select rests; 1 = inp_frame each
                    ; sample; 2 = inp_frame + inp_msamp, the module armed)
                    ; +1 u16 samples, +3 VIC frames between samples
CNT  = $C604
PTR  = $FB
DATA = $C000
.segment "CODE"
        php
        sei
        lda #<DATA
        sta PTR
        lda #>DATA
        sta PTR+1
        lda PRM+1
        sta CNT
        lda PRM+2
        sta CNT+1
        lda PRM
        bne loop
        lda #$40            ; no scan: PA7-6 = 01 (port 1's pots), latch first
        sta $DC00
        lda #$C0
        sta $DC02
        lda #0
        sta $DC03
loop:   lda PRM
        beq :+
        jsr inp_frame       ; the production scan (and its rest select)
        lda PRM
        cmp #2
        bne :+
        jsr inp_msamp       ; + the module's sample (its port-line look)
:       ldy #0
        lda $DC01           ; port 1 lines (column 7 driven low by the rest)
        sta (PTR),y
        iny
        lda $DC00
        sta (PTR),y
        iny
        lda $D419           ; POTX
        sta (PTR),y
        iny
        lda $D41A           ; POTY
        sta (PTR),y
        clc
        lda PTR
        adc #4
        sta PTR
        bcc :+
        inc PTR+1
:       ldx PRM+3
w:      lda $D012           ; one VIC frame: raster line $30 of the top half
        cmp #$30
        bne w
        lda $D011
        bmi w
w2:     lda $D012
        cmp #$30
        beq w2
        dex
        bne w
        lda CNT
        bne :+
        dec CNT+1
:       dec CNT
        lda CNT
        ora CNT+1
        bne loop
        plp
        rts
"""

DIAG_PLAN = [   # (config, MHz, mode, [(seconds, cue, tag)])
    ("A: 1 MHz, no frame scan", 1, 0, [
        (10, "MOUSE: do NOT touch the mouse (idle)", "idle"),
        (30, "MOUSE: press and HOLD the RIGHT button (keep holding)", "right"),
        (30, "MOUSE: press and HOLD the LEFT button (keep holding)", "left"),
        (30, "MOUSE: move SLOWLY to the RIGHT, then back left, several times", "x"),
        (30, "MOUSE: move SLOWLY DOWN, then back up, several times", "y")]),
    ("B: 64 MHz, no frame scan", 64, 0, [
        (30, "MOUSE: move SLOWLY to the RIGHT, then back left, several times", "x"),
        (30, "MOUSE: move SLOWLY DOWN, then back up, several times", "y")]),
    ("C: 64 MHz, the production frame scan + the module", 64, 2, [
        (30, "MOUSE: move SLOWLY to the RIGHT, then back left, several times", "x"),
        (30, "MOUSE: press and HOLD the RIGHT button (keep holding)", "right")]),
]


def diag_stub(rig: Rig) -> bytes:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "diag.s")
        open(src, "w").write(DIAG_SRC)
        cfg = os.path.join(d, "diag.cfg")
        open(cfg, "w").write("MEMORY { M: file = %%O, start = $%04X, size = $100; }\n"
                             "SEGMENTS { CODE: load = M, type = rw; }\n" % DIAG_ORG)
        out = os.path.join(d, "diag.bin")
        for cmd in (["ca65", "-D", f"inp_frame=${rig.frame_addr:04X}",
                     "-D", f"inp_msamp=${rig.lab('inp_msamp'):04X}", "-o", src + ".o", src],
                    ["ld65", "-C", cfg, "-o", out, src + ".o"]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(r.stdout + r.stderr)
        return open(out, "rb").read()


def mouse_diag(rig: Rig) -> int:
    """Raw 1351 diagnostics: every ~2 VIC frames (25/30 Hz) the C64 itself
    records $DC01 (port 1 lines), $DC00, POTX, POTY into RAM; the host reads
    them after each 10-s chunk and prints the table with times.  Windows of
    10-30 s, each announced 10 s ahead (CUE) with a GO line; per window a
    summary (lines that changed vs idle; each pot's min/max/distinct values
    and its wraps); a verdict at the end.  Configurations: A 1 MHz without
    the frame scan (the pot select rests on port 1 the whole time), B 64 MHz
    without it, C 64 MHz with inp_frame (the production key scan) and
    inp_msamp (the input module armed: its port-line look) per sample."""
    rig.need()
    rig.reset(src=0)
    rig.w(rig.lab("inp_mport"), b"\0")
    rig.w(rig.lab("inp_mper"), bytes(4))
    stub = diag_stub(rig)
    rig.w(DIAG_ORG, stub)
    nf = 2
    print("=" * 72)
    print("MOUSE DIAG: the 1351 in PORT 1.  Each window is announced 10 s ahead (CUE),")
    print("starts at GO and ends at END.  Columns: t(s) DC01 DC00 POTX POTY")
    print("=" * 72, flush=True)
    summary, idle01 = [], None
    for cname, mhz, mode, windows in DIAG_PLAN:
        rig.tg.transport.set_speed(mhz)
        time.sleep(1.0)
        if mode == 2:                   # the input module, armed (inp_mper = 0:
            stage_inpm(rig)             # the host-paced samples only)
            rig.w(DIAG_ORG, stub)
        print(f"CONFIG {cname}", flush=True)
        for secs, cue, tag in windows:
            print(f"CUE {time.strftime('%H:%M:%S')} [{cname}] in 10 s, for {secs} s: {cue}",
                  flush=True)
            time.sleep(10.0)
            print(f"GO  {time.strftime('%H:%M:%S')} [{cname}] {cue}", flush=True)
            rows, t0 = [], time.monotonic()
            while time.monotonic() - t0 < secs:
                n = DIAG_N
                rig.w(DIAG_PRM, bytes([mode, n & 0xFF, n >> 8, nf]))
                ts = time.monotonic()
                rig.cia.call(DIAG_ORG, timeout=120)
                te = time.monotonic()
                raw = rig.r(DIAG_DATA, 4 * n)
                for k in range(n):
                    rows.append((round(ts - t0 + (te - ts) * k / n, 2), *raw[4 * k:4 * k + 4]))
            for t, b1, b0, px, py in rows:
                print(f"  {tag:5} {t:6.2f}  ${b1:02X} ${b0:02X}  ${px:02X} ${py:02X}")
            def stats(col):
                vals = [r[col] for r in rows]
                wraps = sum(1 for a, b in zip(vals, vals[1:]) if abs(a - b) > 0x40)
                return {"min": min(vals), "max": max(vals), "distinct": len(set(vals)),
                        "wraps": wraps}
            b1s = {r[1] for r in rows}
            low = 0
            for v in b1s:
                low |= ~v & 0xFF
            if tag == "idle":
                idle01 = low
            sx, sy = stats(3), stats(4)
            line = (f"END {time.strftime('%H:%M:%S')} [{cname}] {tag}: {len(rows)} samples; "
                    f"DC01 values {sorted('$%02X' % v for v in b1s)} (bits seen low ${low:02X}"
                    f"{'' if idle01 is None else f', new vs idle ${low & ~idle01 & 0xFF:02X}'}); "
                    f"POTX ${sx['min']:02X}-${sx['max']:02X} ({sx['distinct']} values, "
                    f"{sx['wraps']} jumps > $40); POTY ${sy['min']:02X}-${sy['max']:02X} "
                    f"({sy['distinct']} values, {sy['wraps']} jumps)")
            print(line, flush=True)
            summary.append((cname, tag, low, sx, sy))
    rig.tg.transport.set_speed(1)
    rig.w(rig.lab("inp_mon"), b"\0")
    print("MOUSE DIAG VERDICT:")
    for cname, tag, low, sx, sy in summary:
        what = {"right": "RIGHT button", "left": "LEFT button"}.get(tag)
        if what:
            nb = low & ~(idle01 or 0) & 0xFF
            print(f"  {cname}: {what} held -> DC01 bits low ${nb:02X}"
                  f" ({'PB' + ','.join(str(b) for b in range(8) if nb >> b & 1) if nb else 'no line'})"
                  f"; pots X ${sx['min']:02X}-${sx['max']:02X}, Y ${sy['min']:02X}-${sy['max']:02X}")
        elif tag in ("x", "y"):
            st = sx if tag == "x" else sy
            print(f"  {cname}: move {tag.upper()} -> POT{tag.upper()} ${st['min']:02X}-"
                  f"${st['max']:02X}, {st['distinct']} values, {st['wraps']} jumps "
                  f"(a 1351 covers ~$00-$7F mod 64 counts)")
    return 0


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
    idle = undriven(rig, "attended", [])
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
    # The turbo phantom check: F3 (column 0, row 5) must never read as C=
    # (column 7, row 5); then C= alone must.  rev-inp measured instant row
    # recovery on the C64U; this is the key-held confirmation.
    def window(what: str, secs: float = 5.0) -> int:
        print(f"  >>> in 3 s: HOLD {what} ALONE for {secs:.0f} s (nothing else)", flush=True)
        time.sleep(3.0)
        print(f"  >>> HOLD {what} NOW", flush=True)
        cbm, t1 = 0, time.monotonic() + secs
        while time.monotonic() < t1:
            rig.w(AFLAGS, b"\0\0\0")
            rig.cia.call(ASTUB)
            cbm += bool(rig.r(AFLAGS + 1, 1)[0] & CBM)
        print(f"  >>> release {what}: C= seen in {cbm} batch(es)", flush=True)
        return cbm
    phantom = None
    try:
        f3, cb = window("F3"), window("C=")
        phantom = "FAIL: F3 read as C= (turbo phantom)" if f3 else \
            ("INCOMPLETE: C= never seen in its window" if not cb else "PASS")
        print(f"F3/C= CHECK: {phantom}")
    except KeyboardInterrupt:
        print("  (F3/C= check ended early)")
    want = ["up", "down", "left", "right", "left button (fire)", "right button (C=)",
            "right button + move", "F1", "JOY0DAT moved"]
    excused = [n for b, n in ((UP, "up"), (DOWN, "down"), (LEFT, "left"),
                              (RIGHT, "right"), (FIRE, "left button (fire)"))
               if STUCK & b]
    if excused:
        print(f"  (--stuck ${STUCK:02X}: not required: {excused})")
    missing = [w for w in want if w not in seen and w not in excused]
    print(f"ATTENDED VERDICT: {'PASS' if not missing else 'INCOMPLETE'}: seen "
          f"{sorted(seen)}{'; missing ' + str(missing) if missing else ''}; "
          f"F3/C= {phantom or 'not run'}")
    return 0 if not missing and phantom == "PASS" else 1


# --------------------------------------------------------------------------
# Mutants (<= 10, each built once)
# --------------------------------------------------------------------------

INP, MACH = "runtime/ops/inp.s", "runtime/ops/mach.s"
MUTANTS = {
    # the frame hook never calls inp_frame
    "nocall": (MACH, [("        jsr inp_frame           ; W13-I: joystick -> mouse, once per hooked frame\n", "")]),
    # the step never caps
    "nocap": (INP, [("        cpy #INP_NMAX\n", "        cpy #$FF\n")]),
    # the direction bits add where they subtract and vice versa
    "sign": (INP, [("        bcs add                 ; C = 1: + (step - 1) + 1\n",
                    "        bcc add                 ; C = 1: + (step - 1) + 1\n")]),
    # fire releases /FIR0 instead of pulling it low
    "fireinv": (INP, [("        eor #$C0\n", "        eor #$80\n")]),
    # the pin write leaks into bit 4 (W13-W's /TK0) (rev-inp)
    "pinleak4": (INP, [("        and #$C0\n", "        and #$D0\n")]),
    # the stick read without its polarity flip (rev-inp R1)
    "stickpol": (INP, [("        lda CIA1_PRA            ; port 2\n        eor #$FF\n",
                        "        lda CIA1_PRA            ; port 2\n")]),
    # unreadable rows not masked: a port-1 row reads as a key (phantom)
    "noghost": (INP, [("        eor inp_key             ; unreadable rows (idle bit 0) keep the\n        and inp_t               ; last state: ((new ^ old) & idle) ^ old\n        eor inp_key\n", "")]),
    # the override is ignored: the real CIA1 is always scanned
    "nosrc": (INP, [("        lda inp_src\n        bne have\n", "")]),
    # W14-M (rev14-mouse): the port swap never happens (the mouse always port 1)
    "noswap": (INP, [("        ldx inp_mport           ; W14-M: the mouse on port 2: swap\n        beq :+\n",
                      "        ldx inp_mport           ; W14-M: the mouse on port 2: swap\n        bne :+\n        beq :+\n")]),
    # W14-M (rev14-mouse): the sampler re-arms only from the frame hook
    "nosamparm": (INP, [("        txa\n        bne marm\n        lda inp_mper\n        ora inp_mper+1\n        bne marm\n        rts\n",
                         "        txa\n        bne :+\n        lda inp_mper\n        ora inp_mper+1\n        bne marm\n:       rts\n")]),
}
# One-off checks (--only), outside the <= 10 set
EXTRA_MUTANTS = {
    # centring does not reset the acceleration
    "noreset": (INP, [("        bne :+\n        sta inp_n\n", "        bne :+\n        nop\n        nop\n        nop\n")]),
    # /FIR1 not forced high (pin 7 left as it was)
    "nofir1": (INP, [("        and #$C0\n", "        and #$40\n")]),
    # the pin write leaks into bit 2 (W13-W's /CHNG) (rev-inp)
    "pinleak2": (INP, [("        and #$C0\n", "        and #$C4\n")]),
    # the right button on POTINP bit 11 instead of 10
    "potbit": (INP, [("        lsr\n        lsr\n        lsr\n        sta inp_t\n",
                      "        lsr\n        lsr\n        sta inp_t\n")]),
    # F1 as a level, not an edge
    "noedge": (INP, [("have:   lda inp_kp              ; F1 press edge -> inp_view bit 0\n        eor #$FF\n        and inp_key\n",
                      "have:   lda inp_key             ; F1 press edge -> inp_view bit 0\n")]),
    # W14-M: the pots rest on port 2 when the mouse is on port 1
    "mselect": (INP, [("        lda #$40                ; W14-M: rest with PA7-6 selecting the\n",
                       "        lda #$80                ; W14-M: rest with PA7-6 selecting the\n")]),
    # W14-M (rev14-mouse): marm without mach_etime (a stale deadline)
    "stale": (INP, [("        jsr mach_etime\n        clc\n", "        clc\n")]),
    # W14-M (rev14-mouse): the stuck mask on the mouse's lines, not the stick's
    "stuckmouse": (INP, [(":       sty inp_mb              ; the mouse port's lines (buttons)\n        ora inp_stuck           ; the stick's port; stuck lines read as\n        eor inp_stuck           ; released\n        sta inp_joy\n",
                          ":       sta inp_joy\n        tya\n        ora inp_stuck\n        eor inp_stuck\n        sta inp_mb\n        lda inp_joy\n        sta inp_joy\n")]),
    # W14-M (rev14-mouse): the ID guard compares the wrong byte
    "idbyte": (INP, [("        cmp INPM_ORG+3\n", "        cmp INPM_ORG+4\n")]),
    # W14-M (rev14-mouse): the rest select written without DDRA
    "restfloat": (INP, [("        lda #$C0\n        sta CIA1_DDRA\nhave:", "        lda #$00\n        sta CIA1_DDRA\nhave:")]),
    # W14-M (rev14-mouse): port 1 read before PA7-6 are released (VICE cannot
    # hold a key: SURVIVES by design, the attended keyboard window covers it)
    "kbiso": (INP, [("        lda #0                  ; 1. both ports, nothing driven (DDRB is\n        sta CIA1_DDRA           ; $00 throughout: nothing here sets it)\n        lda CIA1_PRB            ; port 1 (rows: no column driven, so no key)\n",
                     "        lda CIA1_PRB            ; port 1 (rows: no column driven, so no key)\n        pha\n        lda #0\n        sta CIA1_DDRA\n        pla\n")]),
}
EXTRA_LEGS = {"noreset": ["api"], "nofir1": ["api"], "pinleak2": ["api"],
              "potbit": ["api", "mouse"], "noedge": ["api"], "mselect": ["real"],
              "stale": ["msamp"], "stuckmouse": ["mreal"], "idbyte": ["queue"],
              "restfloat": ["real"], "kbiso": ["mreal", "real"]}
MUTANT_LEGS = {"nocall": ["guest"], "nocap": ["api"], "pinleak4": ["api"],
               "sign": ["api"], "fireinv": ["api"], "stickpol": ["real"],
               "noghost": ["real"], "nosrc": ["api"],
               "noswap": ["mreal"], "nosamparm": ["msamp"]}


def build_mutant(name: str, out_dir: str) -> str:
    import tests.run_mach as rm
    saved = rm.MUTANTS
    try:
        rm.MUTANTS = {**MUTANTS, **EXTRA_MUTANTS}
        return rm.build_mutant(name, out_dir)
    finally:
        rm.MUTANTS = saved


def teeth(prg: str, only: list[str] | None) -> int:
    bdir = os.path.relpath(os.path.dirname(os.path.abspath(prg)), ROOT)
    names = only or list(MUTANTS)
    bad = []
    for name in names:
        mprg = build_mutant(name, os.path.join(bdir, "inp-mut", name))
        legs = {**MUTANT_LEGS, **EXTRA_LEGS}[name]
        log(f"=== mutant {name} (legs {','.join(legs)})")
        try:
            fails = vice(mprg, legs)
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
    ap.add_argument("--qmutants", action="store_true", help="W14-M: the inpm.s module mutants")
    ap.add_argument("--attended", action="store_true")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--allow-held", default="0",
                    help="stick bits port 2 may read held with nothing driven "
                         "(hardware only, e.g. $01 for the C64U's stuck Up): a WARN")
    ap.add_argument("--stuck", default="0",
                    help="inp_stuck for --attended / hardware legs: stick bits the "
                         "real scan ignores (e.g. $01: Up stuck closed)")
    ap.add_argument("--mouse", type=int, choices=(1, 2), default=None,
                    help="with --attended: the 1351 windows, mouse in this port")
    ap.add_argument("--mouse-diag", action="store_true",
                    help="with --attended: the raw 1351 lines/pots (3 configurations)")
    ap.add_argument("--json", help="write {legs, fails, warn} here")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    VERBOSE = a.verbose
    global ALLOW_HELD, STUCK
    STUCK = int(a.stuck.lstrip("$"), 16) if a.stuck.startswith("$") else int(a.stuck, 0)
    ALLOW_HELD = int(a.allow_held.lstrip("$"), 16) if a.allow_held.startswith("$") \
        else int(a.allow_held, 0)
    if ALLOW_HELD and a.backend != "u64" and not a.attended:
        ap.error("--allow-held is for a hardware port only")
    # a backgrounded job starts with SIGINT ignored; u64_hold's restore needs ^C
    import signal
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _sigterm)
    t0 = time.monotonic()
    if a.qmutants:                      # W14-M M2: tests/aros/inpm.s mutants
        bad = []
        for name in QMUTANTS:
            QMUT[0] = name
            fails = vice(a.prg, QMUTANT_LEGS[name])
            log(f"=== qmutant {name}: {'KILLED' if fails else 'SURVIVED'}")
            if not fails:
                bad.append(name)
        QMUT[0] = None
        print(f"qmutants: {len(QMUTANTS) - len(bad)} killed, {len(bad)} survived {bad}")
        rc = 1 if bad else 0
    elif a.mutants:
        rc = teeth(a.prg, a.only.split(",") if a.only else None)
    elif a.attended:
        legs, fails = ["attended"], []
        if a.mouse_diag:
            rc = u64(a.prg, a.mhz, mouse_diag, "mouse-diag")
        elif a.mouse is not None:
            rc = u64(a.prg, a.mhz, lambda rig: attended_mouse(rig, a.mouse - 1),
                     "attended-mouse")
        else:
            rc = u64(a.prg, a.mhz, lambda rig: attended(rig, a.seconds), "attended")
    elif a.backend == "u64":
        legs = a.legs.split(",") if a.legs else U64_LEGS
        fails = u64(a.prg, a.mhz, lambda rig: run_legs(rig, legs), f"{a.mhz}mhz")
        print(f"run_inp (u64 {a.mhz} MHz): {'PASS' if not fails else f'FAIL ({len(fails)})'}"
              f"{f'  warn: {len(WARNS)}' if WARNS else ''}")
        rc = 1 if fails else 0
    else:
        legs = a.legs.split(",") if a.legs else ALL_LEGS
        fails = vice(a.prg, legs)
        print(f"run_inp: {'PASS' if not fails else f'FAIL ({len(fails)})'}"
              f"{f'  warn: {len(WARNS)}' if WARNS else ''}")
        rc = 1 if fails else 0
    if a.json and not a.mutants:
        import json
        with open(a.json, "w") as fh:
            json.dump({"legs": legs if not a.attended else ["attended"],
                       "fails": fails if not a.attended else [], "rc": rc,
                       "warn": WARNS, "allow_held": ALLOW_HELD}, fh, indent=1)
    print(f"run_inp: wall-clock {time.monotonic() - t0:.0f} s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
