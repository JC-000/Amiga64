#!/usr/bin/env python3
"""Display refresh driver (runtime/ops/disp.s) against host/c2v_ref.py, in VICE.

W10-V, docs/WAVE10_BRIEF.md C3, docs/C2V.md "Display refresh (S3.5)".

Leg ``shim`` (direct API, a host-filled display-state block): the PRG is
rebuilt with tests/pf/shim.s (before W10-P: the C1 block ``disp_st`` and
the hook vector ``cop_dhook``; with W10-P's rc claims only ``shim_kick``),
unless the given PRG already exports ``shim_kick``.  The host stages guest
memory through the runner's test_load_chunk, writes ``disp_st`` and calls ``shim_kick`` (a fake
``cop_frame`` end: it calls through ``cop_dhook``) or ``disp_refresh``.
A scripted sequence of steps (``STEPS``) covers every policy rule and mode:
first-call blank with nothing shown, mc2, equal fields, gen gate, modulo +
wide DDF + PAL crop + bit-0 pointers, hires with a short DIW, a negative
modulo, the blank reasons, the 512 KB line, the duty budget, disp_refresh.
After every step:

  * a shown picture: the whole RAM at $E000-$FFFF and under I/O
    $D000-$DFFF equals the guard pattern with exactly the reference bitmap,
    matrix and RAM NMI vector overlaid; colour RAM, c2v_a_bgc and the VIC
    registers ($D011 $D016 $D018 $D020 $D021, $DD00 bits 1-0) equal
    ``c2v_ref.disp_convert``;
  * no conversion: both RAM blocks keep the guard pattern; the VIC
    registers are unchanged, or for a blank after a shown picture $D011 =
    display off and $D020 = LUT[COLOR00];
  * disp_mode, disp_why, disp_nconv, disp_nblank as the model says;
  * every entry returns with $01 = $37, the I flag as the caller had it
    (clear) and m_rok = 0 after any REC use (C1), unchanged without one.

disp_init's LUT stash is read back from the REU (4096 bytes = build_lut()).

Legs ``--teeth``: the mutants (C3: kernel bit order, palette off by one,
stale gen, plane-pointer order, modulo ignored; plus the 512 KB check, the
DIW line clear and the budget), each must fail the shim leg.  ``--variant
stub`` (every disp entry rts) is the red leg.

Usage: python3 -m tests.run_pf --prg build/<name>/runner.prg
           [--leg shim] [--teeth] [--variant NAME] [--expect-fail]
VICE only.
"""

from __future__ import annotations

import argparse
import os
import random
import struct
import subprocess
import sys
import zlib

from host import c2v_ref as R
from tests import run_c2v as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(ROOT, "tests", "pf", "shim.s")

VIC_REGS = (0xD011, 0xD016, 0xD018, 0xD020, 0xD021, 0xDD00)
VIC_MASK = {0xD011: 0x7F, 0xD016: 0x1F, 0xD018: 0xFE, 0xD020: 0x0F,
            0xD021: 0x0F, 0xDD00: 0x03}
MACH_FRAMES = 0x96               # rc zp mach_frames (u32 LE)

VARIANTS = {
    "stub": ["DISP_STUB=1"],
    "bitorder": ["C2V_MUT_BITORDER=1"],
    "pal": ["DISP_MUT_PAL=1"],
    "gen": ["DISP_MUT_GEN=1"],
    "ppord": ["DISP_MUT_PPORD=1"],
    "mod": ["DISP_MUT_MOD=1"],
    "straddle": ["DISP_MUT_STRADDLE=1"],
    "clr": ["DISP_MUT_CLR=1"],
    "budget": ["DISP_MUT_BUDGET=1"],
    "bank": ["DISP_MUT_BANK=1"],         # caught once disp_st is under BASIC ROM (W10-P)
    # red for the LUT-trap leg: the LUT in guest-visible REU (the first proposal)
    "lutram": ["disp_lut=0x800000"],
}
NOT_TEETH = ("stub", "lutram")
TEETH = [k for k in VARIANTS if k not in NOT_TEETH]


def build(prg: str, name: str, defines: list[str], shim: bool) -> str:
    """Rebuild the tree (Makefile link order) next to *prg*, optionally
    with tests/pf/shim.s, plus test-only defines."""
    bdir = os.path.dirname(os.path.abspath(prg))
    disp = os.path.join(bdir, "gen", "dispatch.s")
    if not os.path.exists(disp):
        raise FileNotFoundError(f"{disp} (build the runtime with make first)")
    out = os.path.join(bdir, "pf-" + name)
    os.makedirs(out, exist_ok=True)
    vprg = os.path.join(out, "runner.prg")
    defs = list(defines)
    srcs = C.makefile_sources() + ([SHIM] if shim else [])
    cmd = ["cl65", "-t", "none", "-C", os.path.join(ROOT, "runtime", "c64.cfg"),
           *[x for d in defs for x in ("--asm-define", d)],
           "-o", vprg, "-Ln", os.path.join(out, "runner.lbl"),
           "-m", os.path.join(out, "runner.map")] + srcs + [disp]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    return vprg


def has_label(prg: str, name: str) -> bool:
    with open(os.path.splitext(prg)[0] + ".lbl") as f:
        return any(line.split()[-1].lstrip(".") == name for line in f)


# --- guest memory model ----------------------------------------------------------

class Mem:
    """What the host staged in guest memory (the reference reads this)."""

    def __init__(self):
        self.chunks: dict[int, bytearray] = {}

    def put(self, addr: int, data: bytes) -> None:
        self.chunks[addr] = bytearray(data)

    def read(self, a: int, n: int) -> bytes:
        for base, b in self.chunks.items():
            if base <= a and a + n <= base + len(b):
                return bytes(b[a - base:a - base + n])
        raise KeyError(f"reference read ${a:06X}+{n} outside the staged memory")


def rnd(seed: int, n: int) -> bytes:
    r = random.Random(seed)
    return bytes(r.randrange(256) for _ in range(n))


def pal(seed: int) -> list[int]:
    r = random.Random(seed)
    return [r.randrange(0x10000) for _ in range(32)]    # bits 15-12 junk


STD = {"dmacon": 0x0380, "bplcon0": 0x2200, "diwstrt": 0x2C81,
       "diwstop": 0xF4C1, "ddfstrt": 0x0038, "ddfstop": 0x00D0}

# regions staged once (base, length, seed)
REGIONS = [(0x100000, 0x4000, 1), (0x110000, 0x10000, 2),
           (0x120000, 0x10000, 3), (0x130000, 0x8000, 4)]


def steps() -> list[dict]:
    """The scripted sequence.  Each: name, st (the C1 fields; gen given),
    action (kick | refresh), optional pre (host writes before it: 'restage'
    a region with a seed, 'frames' = mach_frames relative to disp_lastf,
    'mach_in'), and what the model expects ('conv': True/False)."""
    mc2 = dict(STD, bplpt=[0x100000, 0x102000], color=pal(10))
    s = []
    s.append({"name": "no-state", "st": dict(mc2, gen=0x40, valid=0x00),
              "conv": False})                  # cop_reset never ran
    s.append({"name": "off-none", "st": dict(STD, dmacon=0x0200, gen=1,
                                               color=pal(9)),
              "conv": False})
    s.append({"name": "mc2-std", "st": dict(mc2, gen=2), "conv": True})
    s.append({"name": "same-fields", "st": dict(mc2, gen=3), "conv": False,
              "pre": {"restage": (0x100000, 0x4000, 11)}})
    s.append({"name": "gen-gate", "st": dict(mc2, gen=3, color=pal(12)),
              "conv": False})
    s.append({"name": "mc2-mod-crop", "conv": True, "st": dict(
        STD, gen=4, bplpt=[0x110001, 0x118000], bpl1mod=40, bpl2mod=9,
        ddfstrt=0x30, diwstrt=0x2C81, diwstop=0x2CC1, color=pal(13))})
    s.append({"name": "hires-short", "conv": True, "st": dict(
        STD, gen=5, bplcon0=0xC200, bplpt=[0x120000 + p * 80 for p in range(4)],
        bpl1mod=240, bpl2mod=240, ddfstrt=0x3C, ddfstop=0xD4,
        diwstrt=0x2C81, diwstop=0xC2C1, color=pal(14))})
    s.append({"name": "mc2-negmod", "conv": True, "st": dict(
        STD, gen=6, bplpt=[0x132000, 0x134000], bpl1mod=-80 & 0xFFFF,
        bpl2mod=0, color=pal(15))})
    s.append({"name": "blank-depth3", "conv": False, "st": dict(
        STD, gen=7, bplcon0=0x3200, bplpt=[0x100000, 0x102000],
        color=pal(16))})
    s.append({"name": "blank-ham", "conv": False, "st": dict(
        STD, gen=8, bplcon0=0x6A00, color=pal(17))})
    s.append({"name": "blank-straddle", "conv": False, "st": dict(
        STD, gen=9, bplpt=[0x17FFE8, 0x170000], color=pal(18))})
    s.append({"name": "blank-diw", "conv": False, "st": dict(   # $90 < $F4
        STD, gen=10, bplpt=[0x100000, 0x102000], diwstrt=0xF481,
        diwstop=0x90C1, color=pal(19))})
    s.append({"name": "blank-ddf", "conv": False, "st": dict(
        mc2, gen=11, ddfstop=0xC8, color=pal(20))})
    s.append({"name": "budget-defer", "conv": False, "st": dict(
        mc2, gen=12, color=pal(21)),
        "pre": {"mach_in": 1, "frames": R_DISP_N - 1}})
    s.append({"name": "budget-due", "conv": True, "st": dict(
        mc2, gen=12, color=pal(21)),
        "pre": {"mach_in": 1, "frames": R_DISP_N}})
    s.append({"name": "refresh", "conv": True, "action": "refresh",
              "st": dict(mc2, gen=12, color=pal(21)),
              "pre": {"mach_in": 0, "restage": (0x100000, 0x4000, 22)}})
    return s


R_DISP_N = 16      # replaced from the PRG's DISP_N label at run time


# --- VICE ------------------------------------------------------------------------

class Pf(C.Session):
    def __init__(self, target, labels):
        super().__init__(target, labels)
        from tests.harness_util import lookup_label
        for k in ("disp_init", "disp_frame", "disp_refresh", "disp_st",
                  "cop_dhook", "shim_kick", "disp_mode", "disp_why",
                  "disp_nconv", "disp_nblank", "disp_lastf", "mach_in",
                  "mem_rec_dirty", "DISP_N", "mem_write8", "mem_write16",
                  "mem_write32", "chip_nopen", "disp_lut"):
            self.lab[k] = lookup_label(labels, k)
        self.timeout = 600.0

    def rd(self, a: int, n: int) -> bytes:
        from c64_test_harness import read_bytes
        return bytes(read_bytes(self.T, a, n))

    def wr(self, a: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.T, a, data)

    def vic(self) -> dict:
        return {a: self.rd(a, 1)[0] & VIC_MASK[a] for a in VIC_REGS}

    def guest_read(self, addr: int, n: int) -> bytes:
        from types import SimpleNamespace
        from tests.run_dos import Guest
        sess = SimpleNamespace(target=self.t, timeout=self.timeout)
        return Guest(sess, _Lab(self.lab)).read(addr, n)


class _Lab:
    def __init__(self, lab):
        self.lab = lab

    def address(self, name):
        return self.lab.get(name.lstrip("."))


def state(s: Pf) -> dict:
    nconv = struct.unpack("<H", s.rd(s.lab["disp_nconv"], 2))[0]
    nblank = struct.unpack("<H", s.rd(s.lab["disp_nblank"], 2))[0]
    return {"mode": s.rd(s.lab["disp_mode"], 1)[0],
            "why": s.rd(s.lab["disp_why"], 1)[0], "nconv": nconv,
            "nblank": nblank}


PERF_CASES = [
    ("refresh mc2 (200 lines)", "disp_refresh",
     dict(STD, bplpt=[0x100000, 0x102000], color=pal(30))),
    ("refresh hires4 (200 lines)", "disp_refresh",
     dict(STD, bplcon0=0xC200, bplpt=[0x120000 + p * 80 for p in range(4)],
          bpl1mod=240, bpl2mod=240, ddfstrt=0x3C, ddfstop=0xD4, color=pal(31))),
    ("frame, gen unchanged", "shim_kick", None),
    ("frame, gen changed, fields equal", "shim_kick", "bump"),
    ("refresh, blank (DMA off)", "disp_refresh",
     dict(STD, dmacon=0, bplpt=[0x100000, 0x102000], color=pal(32))),
]


def run_perf(s: "Pf") -> dict:
    """phi2 cycles per entry, screen blanked (run_c2v's CIA2 TA->TB count)."""
    out, gen, st = {}, 0x80, None
    for name, entry, case in PERF_CASES:
        if isinstance(case, dict):
            st = dict(case, gen=gen)
        elif case == "bump":
            st = dict(st, gen=gen)
        gen += 1
        s.write_ram(s.lab["disp_st"], R.pack_disp_st(st))
        cyc = C.measure(s, entry)
        out[name] = cyc
        print(f"  perf {name:34s} {cyc:9d} cycles")
    return out


def run_shim(prg: str, perf: bool = False) -> list[str]:
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    global R_DISP_N
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    lut = R.build_lut()
    fails: list[str] = []
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            s = Pf(target, labels)
            R_DISP_N = s.lab["DISP_N"]
            mem = Mem()
            for base, n, seed in REGIONS:
                data = rnd(seed, n)
                s.stage(base, data)
                mem.put(base, data)
            # disp_init
            f: list[str] = []
            C.call_checked(s, "disp_init", f)
            got = s.guest_read(s.lab["disp_lut"], R.LUT_LEN)
            if got != lut:
                bad = sum(a != b for a, b in zip(got, lut))
                f.append(f"LUT in the REU: {bad}/4096 bytes differ from build_lut()")
            hook = struct.unpack("<H", s.read_ram(s.lab["cop_dhook"], 2))[0]
            if hook != s.lab["disp_frame"]:
                f.append(f"cop_dhook = ${hook:04X}, want disp_frame "
                         f"${s.lab['disp_frame']:04X}")
            st0 = state(s)
            if st0 != {"mode": R.MODE_NONE, "why": 0, "nconv": 0, "nblank": 0}:
                f.append(f"state after init {st0}")
            print(f"  {'init':16s} {'OK' if not f else 'FAIL'}")
            for x in f:
                print(f"      {x}")
            fails += [f"init: {x}" for x in f]
            f = lut_guarded(s, lut)
            fails += [f"lut-trap: {x}" for x in f]
            shown = False
            for step in steps():
                f, shown = run_step(s, step, mem, lut, shown)
                fails += [f"{step['name']}: {x}" for x in f]
            if perf:
                run_perf(s)
        finally:
            mgr.release(target)
    return fails


# guest writes into the LUT's REU bytes, through the interpreter's memory API
# (the path every guest store takes): bank $DF traps to chip.s (open bus)
LUT_WRITES = ([("mem_write8", 0, 1), ("mem_write8", 0xFFF, 1),
               ("mem_write16", 0x800, 2), ("mem_write32", 0xFFC, 4),
               ("mem_write32", -2, 4),            # straddles into the LUT
               ("mem_write16", 0xFFF, 2)]         # straddles out of it
              + [("mem_write32", 4 * random.Random(7).randrange(1024), 4)
                 for _ in range(24)])             # (offsets from disp_lut)


def lut_guarded(s: Pf, lut: bytes) -> list[str]:
    """Guest stores to $DF0000-$DF0FFF must leave the stashed LUT intact."""
    from tests.run_program import ZP_M_ADDR, ZP_M_VAL
    f = []
    n0 = struct.unpack("<H", s.rd(s.lab["chip_nopen"], 2))[0]
    base = s.lab["disp_lut"]
    for entry, off, size in LUT_WRITES:
        s.wr(ZP_M_ADDR, struct.pack("<I", base + off))
        s.wr(ZP_M_VAL, b"\xFF" * 4)
        s.call(entry)
    n1 = struct.unpack("<H", s.rd(s.lab["chip_nopen"], 2))[0]
    got = s.guest_read(base, R.LUT_LEN)
    if got != lut:
        bad = sum(a != b for a, b in zip(got, lut))
        f.append(f"{bad}/4096 LUT bytes at ${base:06X} changed by guest stores")
    # each store reaches bank $DF, i.e. chip.s's open bus, at least once
    if (n1 - n0) & 0xFFFF < len(LUT_WRITES):
        f.append(f"chip_nopen +{(n1 - n0) & 0xFFFF}, want >= {len(LUT_WRITES)} "
                 "(the stores did not trap?)")
    print(f"  {'lut-trap':16s} {'OK' if not f else 'FAIL'}  ({len(LUT_WRITES)} guest stores)")
    for x in f:
        print(f"      {x}")
    return f


def run_step(s: Pf, step: dict, mem: Mem, lut: bytes, shown: bool):
    pre = step.get("pre", {})
    if "restage" in pre:
        base, n, seed = pre["restage"]
        data = rnd(seed, n)
        s.stage(base, data)
        mem.put(base, data)
    if "mach_in" in pre:
        s.wr(s.lab["mach_in"], bytes([pre["mach_in"]]))
    if "frames" in pre:
        last = struct.unpack("<I", s.rd(s.lab["disp_lastf"], 4))[0]
        s.wr(MACH_FRAMES, struct.pack("<I", (last + pre["frames"]) & 0xFFFFFFFF))
    st = dict(step["st"])
    before = state(s)
    vic0 = s.vic()
    g = C.guard_fill(s, zlib.crc32(step["name"].encode()))
    s.write_ram(s.lab["disp_st"], R.pack_disp_st(st))   # RAM, whatever maps it
    f: list[str] = []
    entry = "disp_refresh" if step.get("action") == "refresh" else "shim_kick"
    C.call_checked(s, entry, f)
    decided = step["name"] not in ("same-fields", "gen-gate", "budget-defer")
    d0 = R.disp_decide({**{k: 0 for k in ("bplcon1", "bplcon2", "bpl1mod",
                                          "bpl2mod")}, **st,
                        "bplpt": list(st.get("bplpt", [])) + [0] * 6})
    rec_used = step["conv"] or (decided and shown and d0["mode"] == R.MODE_BLANK)
    if not rec_used:
        # call_checked presets m_rok = 1 and demands 0; with no REC use the
        # shadow must be left alone (C1 asks only for REC users to dirty it)
        rok = [x for x in f if "m_rok = 1" in x]
        f = [x for x in f if x not in rok]
        if not rok:
            f.append("m_rok changed although the REC was not used")
    full = {**{k: 0 for k in ("bplcon1", "bplcon2", "bpl1mod", "bpl2mod")},
            **st}
    full["bplpt"] = list(st.get("bplpt", [])) + [0] * 6
    full["color"] = list(st.get("color", [])) + [0] * 32
    want = R.disp_convert(full, mem.read, lut) if step["conv"] else None
    d = R.disp_decide(full)
    after = state(s)
    if step["conv"]:
        if d["mode"] == R.MODE_BLANK:
            f.append(f"TEST BUG: the model blanks (why {d['why']})")
        # the VIC registers change on purpose: checked against the model below
        f += C.check_outputs(s, g, want, C.read_regs(s.T))
        vic = s.vic()
        exp = dict(want["vic"])
        exp[0xDD00] = 0
        for a, v in exp.items():
            if vic[a] != v & VIC_MASK[a]:
                f.append(f"VIC ${a:04X} = ${vic[a]:02X}, want ${v & VIC_MASK[a]:02X}")
        want_st = {"mode": d["mode"], "why": 0, "nconv": before["nconv"] + 1,
                   "nblank": before["nblank"]}
        shown = True
    else:
        # nothing converted: both RAM blocks keep the guard pattern
        top, io = C.expected_blocks(s, g, {"bitmap": b"", "screen": b""})
        if s.under_io:      # the NMI vector is the guard's trap: untouched
            top = bytearray(top)
            top[C.NMIVEC - 0xE000:C.NMIVEC - 0xE000 + 2] = g["top"][
                C.NMIVEC - 0xE000:C.NMIVEC - 0xE000 + 2]
            top = bytes(top)
        f += C.diff("RAM $E000-$FFFF", s.read_ram(0xE000, 0x2000), top, 0xE000)
        f += C.diff("RAM under I/O", s.read_ram(0xD000, 0x1000), io, 0xD000)
        vic = s.vic()
        exp = dict(vic0)
        if decided and shown:
            exp[0xD011] = R.VIC_D011_BLANK & VIC_MASK[0xD011]
            exp[0xD020] = lut[full["color"][0] & 0xFFF]
        for a in VIC_REGS:
            if vic[a] != exp[a]:
                f.append(f"VIC ${a:04X} = ${vic[a]:02X}, want ${exp[a]:02X}")
        if decided:
            mode = (R.MODE_BLANK if shown else R.MODE_NONE)
            want_st = {"mode": mode, "why": d["why"], "nconv": before["nconv"],
                       "nblank": before["nblank"] + 1}
        else:
            want_st = before
    if after != want_st:
        f.append(f"state {after}, want {want_st}")
    print(f"  {step['name']:16s} {'OK' if not f else 'FAIL'}  "
          f"(mode {after['mode']:#04x} why {after['why']} "
          f"nconv {after['nconv']} nblank {after['nblank']})")
    for x in f:
        print(f"      {x}")
    return f, shown


# --- leg "fixture": fixtures/c/pf_static.c through the real copper ----------------

FIX_FINAL_PAL = [0x0000, 0x0FFF, 0x0F00, 0x000F]
FIX_W, FIX_H, FIX_S0, FIX_S1 = 40, 200, 80, 48


def fixture_planes() -> tuple[bytes, bytes]:
    """pf_static.c's two buffers (rows incl. the junk modulo bytes)."""
    p0, p1 = bytearray(FIX_S0 * FIX_H), bytearray(FIX_S1 * FIX_H)
    for y in range(FIX_H):
        for x in range(FIX_W):
            p0[y * FIX_S0 + x] = ((x * 7 + y * 13) & 0xFF) ^ ((y >> 2) & 0xFF)
            p1[y * FIX_S1 + x] = (x * 11 + y * 5 + 0x5A) & 0xFF
        for x in range(FIX_W, FIX_S0):
            p0[y * FIX_S0 + x] = (0xC3 ^ x ^ y) & 0xFF
        for x in range(FIX_W, FIX_S1):
            p1[y * FIX_S1 + x] = (0x3C ^ x ^ y) & 0xFF
    return bytes(p0), bytes(p1)


def fixture_st(a0: int, a1: int) -> dict:
    """The display state pf_static.c's final copper run leaves."""
    return {"dmacon": 0x0380, "bplcon0": 0x2200, "bplcon1": 0, "bplcon2": 0,
            "bplpt": [a0, a1, 0, 0, 0, 0], "bpl1mod": FIX_S0 - FIX_W,
            "bpl2mod": FIX_S1 - FIX_W, "diwstrt": 0x2C81, "diwstop": 0xF4C1,
            "ddfstrt": 0x38, "ddfstop": 0xD0,
            "color": FIX_FINAL_PAL + [0] * 28}


def run_fixture(prg: str, budget: int = 0) -> list[str]:
    import re
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests import run_vbcc as V
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_program import Session as PSession
    from tests.run_vectors import prepare_target
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    out = os.path.join(ROOT, "build", "vbcc")
    os.makedirs(out, exist_ok=True)
    blob = V.compile_c("pf_static", out)
    lut = R.build_lut()
    fails: list[str] = []
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            c = C.Session(target, labels)
            ps = PSession(target=target, labels=labels, timeout=1800.0)
            g = C.guard_fill(c, 0x9F5)
            from c64_test_harness import read_bytes
            vic0 = {a: read_bytes(c.T, a, 1)[0] & VIC_MASK[a] for a in VIC_REGS}
            inits = [n for n in ("cop_reset", "disp_init") if V.has_label(ps, n)]
            print(f"  init entries: {', '.join(inits) or 'none (base PRG)'}")
            for n in inits:
                ps._call(n)
            res, manifest, why = V.stage_and_run(ps, "pf_static", blob, budget, True)
            ok = res.halt != 0 and res.timeout == 0 and res.reason == 0
            if not ok:
                fails.append(f"pf_static did not halt HALT_OK: {why}")
            if res.regs["D0"] != 0:
                fails.append(f"exit code D0=${res.regs['D0']:08X}, want 0")
            scr = read_bytes(c.T, V.SCREEN, 1000)
            rows = [V.screen_to_text(scr[r * 40:(r + 1) * 40]) for r in range(25)]
            text = "\n".join(rows)
            m = re.search(r"P0=([0-9a-f]{6}) P1=([0-9a-f]{6})", text, re.I)
            if rows[0].upper() != "PF STATIC" or not m:
                fails.append(f"screen rows {rows[:4]}")
                print(f"      {fails[-1]}")
                return fails
            a0, a1 = int(m.group(1), 16), int(m.group(2), 16)
            print(f"  planes P0=${a0:06X} P1=${a1:06X}")
            sess = c
            want0, want1 = fixture_planes()
            from types import SimpleNamespace
            from tests.run_dos import Guest
            gr = Guest(SimpleNamespace(target=target, timeout=600.0), labels)
            got0, got1 = gr.read(a0, len(want0)), gr.read(a1, len(want1))
            if (got0, got1) != (want0, want1):
                fails.append("guest planes differ from pf_static.c's pattern")
            mem = Mem()
            mem.put(a0, got0)
            mem.put(a1, got1)
            st = fixture_st(a0, a1)
            want = R.disp_convert(st, mem.read, lut)
            if want["decide"]["mode"] != R.MODE_MC2:
                fails.append(f"TEST BUG: model decides {want['decide']}")
            f = C.check_outputs(sess, g, want, C.read_regs(c.T))
            bm = sess.read_ram(sess.bitmap, 8000)
            if bm == g["top"][sess.bitmap - 0xE000:sess.bitmap - 0xE000 + 8000]:
                f.append("bitmap UNCHANGED (guard pattern intact): nothing converted")
            vic = {a: read_bytes(c.T, a, 1)[0] & VIC_MASK[a] for a in VIC_REGS}
            exp = {**want["vic"], 0xDD00: 0}
            for a, v in exp.items():
                if vic[a] != v & VIC_MASK[a]:
                    f.append(f"VIC ${a:04X} = ${vic[a]:02X}, want ${v & VIC_MASK[a]:02X}"
                             f" (before the run ${vic0[a]:02X})")
            if V.has_label(ps, "disp_lut"):     # the program stored over it
                from tests.harness_util import lookup_label
                la = lookup_label(labels, "disp_lut")
                if gr.read(la, R.LUT_LEN) != lut:
                    f.append(f"LUT at ${la:06X} changed by pf_static's stores to bank $DF")
            if V.has_label(ps, "disp_nconv"):
                from tests.harness_util import lookup_label
                n = struct.unpack("<H", bytes(read_bytes(
                    c.T, lookup_label(labels, "disp_nconv"), 2)))[0]
                if n != 2:
                    f.append(f"disp_nconv = {n}, want 2 (decoy, then final)")
            fails += f
        finally:
            mgr.release(target)
    for x in fails:
        print(f"      {x}")
    print(f"  pf_static {'OK' if not fails else 'FAIL'}")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--variant", choices=sorted(VARIANTS))
    ap.add_argument("--teeth", action="store_true")
    ap.add_argument("--expect-fail", action="store_true")
    ap.add_argument("--perf", action="store_true",
                    help="shim leg: also time disp_refresh / disp_frame")
    ap.add_argument("--leg", choices=["shim", "fixture", "all"], default="all",
                    help="default all: the shim leg, then pf_static (the "
                    "fixture leg needs W10-P's copper: cop_reset)")
    a = ap.parse_args(argv)
    prg = os.path.abspath(a.prg)
    legs = ["shim", "fixture"] if a.leg == "all" else [a.leg]

    def run_legs(p: str) -> list[str]:
        fails = []
        if "shim" in legs:
            sp = p if has_label(p, "shim_kick") else build(p, "shim", [], True)
            print(f"run_pf shim leg: {sp}")
            fails += run_shim(sp, a.perf)
        if "fixture" in legs:
            print(f"run_pf fixture leg: {p}")
            fails += [f"pf_static: {x}" for x in run_fixture(p)]
        return fails

    if a.teeth:
        caught = {}
        for name in TEETH:
            print(f"variant {name} ({' '.join(VARIANTS[name])}):")
            caught[name] = len(run_legs(build(prg, name, VARIANTS[name], True)))
        print("teeth: " + ", ".join(f"{k} {'CAUGHT' if v else 'MISSED'} ({v})"
                                    for k, v in caught.items()))
        return 0 if all(caught.values()) else 1
    if a.variant:
        prg = build(prg, a.variant, VARIANTS[a.variant], True)
    fails = run_legs(prg)
    print(f"run_pf: {len(fails)} failure(s)")
    if a.expect_fail:
        print("expect-fail: " + ("FAILED as required" if fails else "PASSED (teeth missing!)"))
        return 0 if fails else 1
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
