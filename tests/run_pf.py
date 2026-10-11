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
    "dirty": ["DISP_MUT_DIRTY=1"],       # F1: the dirty byte ignored
    "efrom": ["DISP_MUT_EFROM=1", "DISP_TRACE=1"],  # F3: the budget measured from the start
    # W11-A (supervisor grant, efrom): the real build with the budget trace
    "trace": ["DISP_TRACE=1"],
    # red for the LUT-trap leg: the LUT in guest-visible REU (the first proposal)
    "lutram": ["disp_lut=0x800000"],
    # W12-V: the colour choice, the matrix checksum, the run-length LUT
    "w12ord": ["DISP_MUT_W12ORD=1"], "w12onepass": ["DISP_MUT_W12ONEPASS=1"],
    "w12ocs": ["DISP_MUT_W12OCS=1"], "w12up": ["DISP_MUT_W12UP=1"],
    "w12mck": ["DISP_MUT_W12MCK=1"], "w12rle": ["DISP_MUT_W12RLE=1"],
    "w12run": ["DISP_MUT_W12RUN=1"],
    # W13-D: the 1:1 view (leg view)
    "w13px": ["DISP_MUT_W13PX=1"],
    "w13edge": ["DISP_MUT_W13EDGE=1"], "w13lsr": ["DISP_MUT_W13LSR=1"],
    "w13bpu5": ["DISP_MUT_W13BPU5=1"],
    "w14crop": ["DISP_MUT_W14CROP=1"],   # W14-D: the centre crop back
}
NOT_TEETH = ("stub", "lutram", "trace")
TEETH = [k for k in VARIANTS if k not in NOT_TEETH]
ALL_LEGS = ["shim", "fixture", "wrap", "dosbuf", "live", "gap", "cols", "view"]
# the legs each mutant runs (--teeth): the ones meant to catch it
MUTANT_LEGS = {"bitorder": ["shim", "fixture"], "pal": ["shim", "fixture"],
               "gen": ["shim"], "ppord": ["shim", "fixture"],
               "mod": ["shim", "fixture"], "straddle": ["shim"], "clr": ["shim"],
               "budget": ["shim"], "bank": ["shim", "fixture"],
               "dirty": ["shim", "wrap"],
               "efrom": ["gap"],
               "w12ord": ["shim"], "w12onepass": ["shim"], "w12ocs": ["shim"],
               "w12up": ["shim"], "w12mck": ["shim"], "w12rle": ["shim"],
               "w12run": ["shim"],            # W11-A (supervisor grant, efrom)
               "w13px": ["view"], "w13edge": ["view"],
               "w13lsr": ["view"], "w13bpu5": ["view"],
               "w14crop": ["shim", "view"]}


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
    a region with a seed, 'ebud' = the E-time budget reference: 'now'
    (just converted: defer), 'due' (a full budget ago) or 'future' (E
    restarted),
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
    # F1: gen equal to the last one seen (it wrapped), but the dirty byte set
    s.append({"name": "dirty-gen-same", "st": dict(mc2, gen=3, dirty=1,
                                                   color=pal(23)), "conv": True})
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
        mc2, gen=12, color=pal(21)), "pre": {"mach_in": 1, "ebud": "now"}})
    s.append({"name": "budget-due", "conv": True, "st": dict(
        mc2, gen=12, color=pal(21)), "pre": {"mach_in": 1, "ebud": "due"}})
    s.append({"name": "budget-restart", "conv": True, "st": dict(
        mc2, gen=13, color=pal(24)), "pre": {"mach_in": 1, "ebud": "future"}})
    s.append({"name": "refresh", "conv": True, "action": "refresh",
              "st": dict(mc2, gen=12, color=pal(21)),
              "pre": {"mach_in": 0, "restage": (0x100000, 0x4000, 22)}})
    # W12-V: dark palettes the nearest-colour LUT maps to one colour; the
    # hires one with every pen in use, equal OCS pens (1, 15) and junk bits
    s.append({"name": "mc2-dark", "conv": True,
              "st": dict(mc2, gen=14, color=[0x000, 0x111, 0x222, 0x024] + [0] * 28)})
    s.append({"name": "hires-dark", "conv": True, "st": dict(
        STD, gen=15, bplcon0=0xC200, bplpt=[0x120000 + p * 80 for p in range(4)],
        bpl1mod=240, bpl2mod=240, ddfstrt=0x3C, ddfstop=0xD4,
        color=[0x000, 0x111, 0x222, 0x024, 0x042, 0x057, 0x555, 0xAAA, 0x0AA,
               0x547, 0xCCC, 0xBEF, 0xFFF, 0x07F, 0xA7F, 0xF111] + [0] * 16)})
    return s


R_DISP_N = 20      # replaced from the PRG's DISP_N label at run time
FRAME_E = 14188    # E ticks per guest frame


# --- VICE ------------------------------------------------------------------------

def rc_equate(name: str) -> int:
    """An rc RAM equate (runtime/rc/ram.inc), e.g. W10-P's disp_st."""
    import re
    with open(os.path.join(ROOT, "runtime", "rc", "ram.inc")) as f:
        m = re.search(rf"^{name}\s*=\s*\$([0-9A-Fa-f]+)", f.read(), re.M)
    if not m:
        raise KeyError(f"{name} not in runtime/rc/ram.inc")
    return int(m.group(1), 16)


class Pf(C.Session):
    def __init__(self, target, labels):
        super().__init__(target, labels)
        from tests.harness_util import lookup_label
        self.lab["disp_st"] = rc_equate("disp_st")
        for k in ("disp_init", "disp_frame", "disp_refresh",
                  "cop_dhook", "shim_kick", "disp_mode", "disp_why",
                  "disp_nconv", "disp_nblank", "disp_laste", "disp_bud",
                  "mach_in", "mach_s", "mach_init", "mach_etime", "mach_et",
                  "mem_rec_dirty", "DISP_N", "mem_write8", "mem_write16",
                  "mem_write32", "chip_nopen", "disp_lut", "disp_mck"):
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


PIX_OFF = 105      # W11-A (supervisor grant, WAVE11 C3): disp_st+105, the pixel epoch


def check_outputs(s, g: dict, want: dict, st_block: bytes | None) -> list[str]:
    """run_c2v's check_outputs, with its $B800-$B8FF guard made exact: that
    page is now W10-P's disp_st claim.  The block must hold st_block (the
    bytes the host wrote; None: the copper's, not checked here) and the
    spare tail after it must keep the guard pattern.
    # W11-A (supervisor grant, WAVE11 C3): +105 is the pixel epoch, a block
    # field that disp.s only reads: it must hold the step's epoch (g["pix"],
    # restored by run_step after the guard fill; else the guard byte), and
    # the guard check covers +106..$FF exactly as before."""
    f = [x for x in C.check_outputs(s, g, want, C.read_regs(s.T))
         if not x.startswith("guard $B800-$B8FF")]
    base = rc_equate("disp_st")
    page = s.read_ram(0xB800, 0x100)
    exp = bytearray(g["free_b"])
    n = R.DISP_ST_LEN
    if st_block is not None:
        exp[base - 0xB800:base - 0xB800 + n] = st_block
    else:
        exp[base - 0xB800:base - 0xB800 + n] = page[base - 0xB800:base - 0xB800 + n]
    if "pix" in g:                      # W11-A (supervisor grant, WAVE11 C3)
        exp[base - 0xB800 + PIX_OFF] = g["pix"]
    f += C.diff("$B800-$B8FF (disp_st + spare)", page, bytes(exp), 0xB800)
    return f


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
            # the budget steps read guest E: a time base at s = 16 (1 tick
            # per 65536 phi2) keeps it still across host round trips
            s.wr(s.lab["mach_s"], b"\x10")
            s.call(s.lab["mach_init"])
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
    if "ebud" in pre:
        # E at s = 16 (set before these steps) moves 1 tick per 65536 phi2,
        # so "now" stays within the budget across the host round trips
        s.call(s.lab["mach_etime"])
        now = struct.unpack("<I", s.rd(s.lab["mach_et"], 4))[0]
        ebud = R_DISP_N * FRAME_E
        ref = {"now": now, "due": now - ebud, "future": now + 1000}[pre["ebud"]]
        s.wr(s.lab["disp_laste"], struct.pack("<I", ref & 0xFFFFFFFF))
        s.wr(s.lab["disp_bud"], b"\1")
    st = dict(step["st"])
    before = state(s)
    vic0 = s.vic()
    pix = s.read_ram(s.lab["disp_st"] + PIX_OFF, 1)     # W11-A (supervisor grant,
    g = C.guard_fill(s, zlib.crc32(step["name"].encode()))
    s.write_ram(s.lab["disp_st"] + PIX_OFF, pix)        # WAVE11 C3): the epoch
    g["pix"] = pix[0]                                   # moves only on purpose
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
        f += check_outputs(s, g, want, R.pack_disp_st({**st, "dirty": 0}))
        vic = s.vic()
        exp = dict(want["vic"])
        exp[0xDD00] = 0
        for a, v in exp.items():
            if vic[a] != v & VIC_MASK[a]:
                f.append(f"VIC ${a:04X} = ${vic[a]:02X}, want ${v & VIC_MASK[a]:02X}")
        mck = int.from_bytes(s.read_ram(s.lab["disp_mck"], 2), "little")   # W12-V
        if mck != want["mck"]:
            f.append(f"disp_mck ${mck:04X}, want ${want['mck']:04X}")
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
            f = check_outputs(sess, g, want, None)
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


# --- legs from the W10-V review (fix round 1) -------------------------------------

CUSTOM = 0xDFF000


def wrap_prog(la: int, lb: int, k: int) -> list[int]:
    """F1 (review pf_wrap): list A shows a playfield with COLOR00 = $000
    (COP1LC, COPJMP1, DMACON on: one run, one conversion); then K CPU
    writes of distinct COLOR00 values ending at $F00 (gen += K), then
    COP2LC := B (just the end marker) and COPJMP2: the run changes nothing,
    gen does not move, the hook runs.  K = 256 or 512 brings gen back to
    the value the driver saw: only the dirty byte tells it."""
    from tests.run_mach import G, HALT, mv_imm_abs_w
    g = G()
    g(0x23FC, la >> 16, la & 0xFFFF, *g.abs(CUSTOM + 0x080))   # COP1LC
    mv_imm_abs_w(g, 0, CUSTOM + 0x088)                        # COPJMP1
    mv_imm_abs_w(g, 0x8380, CUSTOM + 0x096)                   # DMACON on: runs A
    g(0x23FC, lb >> 16, lb & 0xFFFF, *g.abs(CUSTOM + 0x084))   # COP2LC
    g(0x323C, 0xF00 - (k - 1))                                # move.w #first,d1
    g(0x343C, k - 2)                                          # move.w #k-2,d2
    g.label("lp")
    g(0x33C1, *g.abs(CUSTOM + 0x180))                         # move.w d1,COLOR00
    g(0x5241)                                                 # addq.w #1,d1
    g.dbra(2, "lp")
    mv_imm_abs_w(g, 0x0F00, CUSTOM + 0x180)
    mv_imm_abs_w(g, 0, CUSTOM + 0x08A)                        # COPJMP2: runs B
    g(HALT)
    return g.words()


def leg_wrap(prg: str) -> list[str]:
    """F1: after K = 255 / 256 / 512 net COLOR00 changes and a no-op copper
    run, $D021 shows LUT[COLOR00] and the driver converted again (red on
    498ab52: K = 256 and 512 stay stale)."""
    from tests import run_mach as M
    from tests.run_mach import BASE
    lut = R.build_lut()

    def body(rig):
        f = []
        for k in (255, 256, 512):
            rig.call(rig.lab("chip_reset"))
            rig.call(rig.lab("cop_reset"))
            rig.call(rig.lab("disp_init"))
            a0, a1 = 0x100000, 0x102000
            la_w = [0x0E0, a0 >> 16, 0x0E2, a0 & 0xFFFF, 0x0E4, a1 >> 16,
                    0x0E6, a1 & 0xFFFF, 0x108, 0, 0x10A, 0, 0x08E, 0x2C81,
                    0x090, 0xF4C1, 0x092, 0x38, 0x094, 0xD0, 0x180, 0x000,
                    0x182, 0xFFF, 0x100, 0x2200, 0xFFFF, 0xFFFE]
            lb_w = [0xFFFF, 0xFFFE]
            n0 = len(wrap_prog(0, 0, k))
            la = BASE + 2 * n0
            lb = la + 2 * len(la_w)
            pc, x, y = rig.stage(wrap_prog(la, lb, k), la_w, lb_w)
            assert (x, y) == (la, lb)
            res = rig.sess.run_program(pc, BASE + 0x8000, 3_000_000)
            nconv = struct.unpack("<H", rig.r(rig.lab("disp_nconv"), 2))[0]
            d021 = rig.r(0xD021, 1)[0] & 0x0F
            want = lut[0xF00]
            ok = res.halt == 1 and res.reason == 0 and d021 == want and nconv == 2
            print(f"  wrap K={k:3d}: halt {res.halt}/{res.reason} nconv {nconv} "
                  f"$D021={d021} want LUT[$F00]={want} -> {'OK' if ok else 'STALE'}")
            if not ok:
                f.append(f"K={k}: $D021={d021} (want {want}), nconv {nconv} (want 2)")
        return f
    return [f"wrap: {x}" for x in M.session(prg, body)]


DOSBUF_FILES = {"lut.bin": bytes([0x77]) * 0x10200}


def leg_dosbuf(prg: str) -> list[str]:
    """F2: Read/Write with a buffer touching bank $BF/$DF fail (-1, IoErr
    212) and move nothing: the LUT stays intact; normal buffers work (red
    on 498ab52: the Read lands 4096 x $77 in the LUT)."""
    import re
    from c64_test_harness import Labels, UnifiedManager, ViceConfig, read_bytes
    from tests import run_vbcc as V
    from tests.harness_util import REU_VICE_ARGS, lookup_label
    from tests.run_program import Session as PSession
    from tests.run_vectors import prepare_target
    from tests.run_dos import Guest
    from types import SimpleNamespace
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    out = os.path.join(ROOT, "build", "vbcc")
    os.makedirs(out, exist_ok=True)
    blob = V.compile_c("pf_dosbuf", out)
    lut = R.build_lut()
    want = ["R DF0000 -1 212", "R DEFF00 -1 212", "R BFE000 -1 212",
            "R BEFFF0 -1 212", r"R [0-9A-F]{6} 16 0",
            "W DF0800 -1 212", "W BEFFF0 -1 212", r"W [0-9A-F]{6} 8 0", "BUF 77"]
    f: list[str] = []
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        t = mgr.acquire()
        try:
            prepare_target(t, prg)
            ps = PSession(target=t, labels=labels, timeout=600.0)
            ps._call("disp_init")
            gr = Guest(SimpleNamespace(target=t, timeout=600.0), labels)
            la = lookup_label(labels, "disp_lut")
            if gr.read(la, 4096) != lut:
                f.append("LUT not stashed before the run")
            res, _m, why = V.stage_and_run(ps, "pf_dosbuf", blob, 0, True,
                                           files=DOSBUF_FILES)
            if not (res.halt and not res.timeout and res.reason == 0
                    and res.regs["D0"] == 0):
                f.append(f"no clean exit 0: {why} D0=${res.regs['D0']:08X}")
            scr = bytes(read_bytes(t.transport, V.SCREEN, 1000))
            rows = [V.screen_to_text(scr[r * 40:(r + 1) * 40]).upper() for r in range(25)]
            for i, w in enumerate(want):
                ok = re.fullmatch(w, rows[i]) is not None
                print(f"  {'  ' if ok else '!!'} {rows[i]:24s} want {w}")
                if not ok:
                    f.append(f"row {i}: {rows[i]!r}, want {w!r}")
            got = gr.read(la, 4096)
            bad = sum(a != b for a, b in zip(got, lut))
            print(f"  LUT after the run: {bad}/4096 bytes changed")
            if bad:
                f.append(f"LUT: {bad}/4096 bytes changed by DOS DMA")
        finally:
            mgr.release(t)
    print(f"  dosbuf {'OK' if not f else 'FAIL'}")
    return [f"dosbuf: {x}" for x in f]


# live: the display under mach_run (review pf_live), with the REAL CIA
# callbacks (chip_reset -> cia_reset), so CIA-A's TOD counts the VERTBs
LIVE_FINAL = 0x0F0F
LIVE_PAL0 = [0x0000, 0x0F00, 0x00F0, 0x000F]
LIVE_CONV_PHI2 = 380_000    # a lower bound on one mc2 refresh (PERF (j): 422,505)
LIVE_NV = 60                # handler runs before the final colour (the review's)
LIVE_TO = float(os.environ.get("PF_LIVE_TO", "900"))  # s: one guest run (a livelock = a failure)
# (s, edit, rig): "mach" = tests/mach/drv.s with the real CIA callbacks
# (the TOD check); "irq" = tests/irq/drv.s, the review's rig (no CIA: its
# fake slot callbacks), where the round-0 frame budget livelocks at s = 0
LIVE_CASES = [(4, False, "mach"), (4, True, "mach"), (2, True, "mach"),
              (0, True, "irq")]


def live_list(a0: int, a1: int) -> tuple[list[int], int]:
    w = [0x0E0, a0 >> 16, 0x0E2, a0 & 0xFFFF, 0x0E4, a1 >> 16, 0x0E6, a1 & 0xFFFF,
         0x108, 0, 0x10A, 0, 0x08E, 0x2C81, 0x090, 0xF4C1, 0x092, 0x38, 0x094, 0xD0]
    col1 = len(w) + 2 + 1                      # value word of the COLOR01 MOVE
    for i, c in enumerate(LIVE_PAL0):
        w += [0x180 + 2 * i, c]
    return w + [0x100, 0x2200, 0xFFFF, 0xFFFE], col1


def live_prog(lst: int, colw: int, nv: int, n_after: int, edit: bool):
    """Exception mode: a level-3 VERTB handler counts (and with EDIT bumps
    COLOR01 in the list, a display change every delivered frame); the main
    program counts in d0 until NV handler runs, writes the final colour,
    waits n_after more, reads CIA-A's TOD into d2, the count into d3, HALT."""
    from tests import run_irq as I
    from tests.run_mach import G, HALT, mv_imm_abs_w
    flag = I.DATA + 4
    g = G()
    I.vec(g, 27, "h3")
    I.clr_l(g, I.CNT)
    I.mv_l_imm_abs(g, 0 if edit else 1, flag)
    g(0x7000)                                   # moveq #0,d0
    mv_imm_abs_w(g, 0x7FFF, I.INTENA)
    mv_imm_abs_w(g, 0x7FFF, I.INTREQ)
    I.mv_l_imm_abs(g, lst, CUSTOM + 0x080)      # COP1LC
    mv_imm_abs_w(g, 0, CUSTOM + 0x088)          # COPJMP1 (load)
    mv_imm_abs_w(g, 0x8380, CUSTOM + 0x096)     # DMACON: copper + planes on
    mv_imm_abs_w(g, 0xC020, I.INTENA)
    I.move_sr(g, 0x2000)
    g.label("spin")
    g(0x5280)                                   # addq.l #1,d0
    g(0x0CB9, nv >> 16, nv & 0xFFFF, *g.abs(I.CNT))
    g.bcc(0x6D00, "spin")                       # blt.w
    I.mv_l_imm_abs(g, 1, flag)
    mv_imm_abs_w(g, LIVE_FINAL, colw)
    g(0x2239, *g.abs(I.CNT))                    # move.l CNT,d1
    g(0x0681, n_after >> 16, n_after & 0xFFFF)  # addi.l #n,d1
    g.label("w2")
    g(0xB2B9, *g.abs(I.CNT))                    # cmp.l CNT,d1
    g.bcc(0x6E00, "w2")                         # bgt.w
    g(0x7400)                                   # moveq #0,d2
    g(0x1439, *g.abs(0xBFEA01))                 # TOD hi (latches)
    g(0xE18A)                                   # lsl.l #8,d2
    g(0x1439, *g.abs(0xBFE901))
    g(0xE18A)
    g(0x1439, *g.abs(0xBFE801))                 # lo (unlatches)
    g(0x2639, *g.abs(I.CNT))                    # move.l CNT,d3
    g(HALT)
    g.label("h3")
    I.addq_l_abs(g, I.CNT)
    g(0x4AB9, *g.abs(flag))                     # tst.l flag
    g.bcc(0x6600, "noed")                       # bne.w
    g(0x5279, *g.abs(colw))                     # addq.w #1,colw
    g.label("noed")
    mv_imm_abs_w(g, 0x0020, I.INTREQ)
    g(0x4E73)                                   # rte
    return g


def leg_live(prg: str, cases=None, nohook: bool = False) -> list[str]:
    cases = cases or LIVE_CASES
    f: list[str] = []
    for kind in ("mach", "irq"):
        sel = [(s, e) for s, e, k in cases if k == kind]
        if sel:
            f += live_rig(prg, sel, kind, nohook)
    return f


def live_rig(prg: str, cases, kind: str, nohook: bool) -> list[str]:
    """F3 + the TOD check: the display under the live mach_run.  s = 4
    static / edit-every-frame and s = 0 edit-every-frame must HALT (red on
    498ab52: s = 0 edit livelocks, the frame budget eaten by the catch-up);
    the picture is byte-exact; deliveries = handler runs; CIA-A's TOD (the
    real CIA installed) = mach_frames; with editing, conversions stay
    within the E-time budget (<= frames / DISP_N + 2)."""
    from c64_test_harness import read_bytes
    from tests import run_irq as I
    from tests import run_mach as M
    from tests.run_mach import BASE, F_INIT, F_SUPER, clean
    lut = R.build_lut()

    def body(rig):
        orig = rig.call
        rig.call = lambda addr, timeout=LIVE_TO: orig(addr, timeout)
        if kind == "irq":
            od = rig.dcall
            rig.dcall = lambda name, timeout=LIVE_TO: od(name, timeout)
        f: list[str] = []
        n_dn = rig.lab("DISP_N")
        progress = {}
        for s, edit in cases:
            tag = f"live s={s} {'edit' if edit else 'static'}"
            rig.call(rig.lab("chip_reset"))         # also cia_reset (callbacks)
            rig.call(rig.lab("cop_reset"))
            rig.call(rig.lab("disp_init"))
            if nohook:                              # diagnostics: no display
                rig.w(rig.lab("cop_dhook"), b"\0\0")
            pl0, pl1 = rnd(100 + s, 8000), rnd(101 + s, 8000)
            n0 = len(I.link(live_prog(0, 0, LIVE_NV, n_dn + 4, edit), BASE))
            lw, col1 = live_list(0, 0)
            lst = BASE + 2 * n0
            a0 = lst + 2 * len(lw)
            a1 = a0 + 8000
            lw, col1 = live_list(a0, a1)
            colw = lst + 2 * col1
            words = I.link(live_prog(lst, colw, LIVE_NV, n_dn + 4, edit), BASE)
            assert len(words) == n0
            pc, l2, b0, b1 = rig.stage(words, lw, list(struct.unpack(">4000H", pl0)),
                                       list(struct.unpack(">4000H", pl1)))
            assert (l2, b0, b1) == (lst, a0, a1)
            nirq0 = struct.unpack("<I", rig.r(rig.lab("mach_nirq"), 4))[0]
            try:
                if kind == "irq":
                    res = rig.run(pc, s=s, flags=I.F_INIT | I.F_SUPER, sr=0x27)
                    res["timeout"] = 0
                else:
                    res = rig.run(pc, s=s, flags=F_INIT | F_SUPER)
            except Exception as e:
                f.append(f"{tag}: no HALT within {LIVE_TO:.0f} s ({type(e).__name__})")
                print(f"  {tag}: NO HALT ({type(e).__name__})")
                break                               # the machine state is unknown
            if not clean(res, tag, f):
                continue
            d0, d1, d2, d3 = res["d"]
            frames = res["frames"]
            nirq = (struct.unpack("<I", rig.r(rig.lab("mach_nirq"), 4))[0] - nirq0) & 0xFFFFFFFF
            nconv = struct.unpack("<H", rig.r(rig.lab("disp_nconv"), 2))[0]
            why, mode = rig.r(rig.lab("disp_why"), 1)[0], rig.r(rig.lab("disp_mode"), 1)[0]
            progress[(s, edit)] = d0 / max(frames, 1)
            print(f"  {tag}: frames {frames} handler runs {d3} deliveries {nirq} "
                  f"TOD {d2} nconv {nconv} progress/frame {d0 / max(frames, 1):.1f}")
            if nirq != d3:
                f.append(f"{tag}: deliveries {nirq} != handler runs {d3}")
            # one TOD tick per VERTB.  Exact where the guest runs several
            # instructions per frame; at s = 0 the loop itself starves the
            # guest (it gets ~1 instruction per many frames even with no
            # display: review pf_live_nohook, here nohook=True), so frames
            # pass between its TOD read and the HALT: only TOD <= frames
            if kind == "mach" and ((s >= 2 and not frames - 1 <= d2 <= frames)
                                   or d2 > frames):
                f.append(f"{tag}: CIA-A TOD {d2}, mach_frames {frames} (one tick per VERTB)")
            if why != 0 or mode != R.MODE_MC2:
                f.append(f"{tag}: disp_why {why} mode {mode}")
            # the budget runs from a conversion's END: each conversion takes
            # its own length (c frames, at least LIVE_CONV_PHI2) plus N
            # frames, so nconv <= frames / (N + c) + 2 (the start-anchored
            # mutant efrom: back-to-back conversions, ~frames / c)
            c_fr = LIVE_CONV_PHI2 // (FRAME_E << s)
            bound = frames // (n_dn + c_fr) + 2
            if edit and nconv > bound:
                f.append(f"{tag}: {nconv} conversions in {frames} frames, budget "
                         f"<= {bound} (N {n_dn} + {c_fr} frames per conversion)")
            pal = list(LIVE_PAL0)
            pal[1] = LIVE_FINAL
            st = {"dmacon": 0x0380, "bplcon0": 0x2200, "bplcon1": 0, "bplcon2": 0,
                  "bplpt": [a0, a1, 0, 0, 0, 0], "bpl1mod": 0, "bpl2mod": 0,
                  "diwstrt": 0x2C81, "diwstop": 0xF4C1, "ddfstrt": 0x38,
                  "ddfstop": 0xD0, "color": pal + [0] * 28}
            mem = Mem()
            mem.put(a0, pl0)
            mem.put(a1, pl1)
            want = R.disp_convert(st, mem.read, lut)
            cs = C.Session(rig.tg, rig.labels if hasattr(rig, "labels") else LABELS[0])
            f += [f"{tag}: {x}" for x in C.diff("bitmap", cs.read_ram(cs.bitmap, 8000),
                                                 want["bitmap"], cs.bitmap)]
            f += [f"{tag}: {x}" for x in C.diff("matrix", cs.read_ram(cs.screen, 1000),
                                                 want["screen"], cs.screen)]
            cr_ = bytes(b & 0x0F for b in read_bytes(rig.t, 0xD800, 1000))
            f += [f"{tag}: {x}" for x in C.diff("colour RAM", cr_, want["colram"], 0xD800)]
            for a_, v in {**want["vic"], 0xDD00: 0}.items():
                got = read_bytes(rig.t, a_, 1)[0] & VIC_MASK[a_]
                if got != v & VIC_MASK[a_]:
                    f.append(f"{tag}: VIC ${a_:04X} = ${got:02X}, want ${v & VIC_MASK[a_]:02X}")
            if rig.guest_read(rig.lab("disp_lut"), 4096) != lut:
                f.append(f"{tag}: LUT changed")
        for s in sorted({s for s, _ in cases}):
            if (s, False) in progress and (s, True) in progress:
                lost = 1 - progress[(s, True)] / max(progress[(s, False)], 1e-9)
                print(f"  live s={s}: guest progress lost to the display {100 * lost:.1f}%")
        return f
    from c64_test_harness import Labels
    LABELS[0] = Labels.from_file(os.path.splitext(os.path.abspath(prg))[0] + ".lbl")
    return (I.session if kind == "irq" else M.session)(prg, body)


LABELS = [None]

# --- leg "gap": the duty budget, measured directly ---------------------------
# W11-A (supervisor grant, efrom).  The old efrom teeth inferred the rule
# from an AVERAGE conversion count in the live s = 0 leg, whose edit rate is
# starved there, so it swung with code layout.  This leg checks the rule
# itself on a DISP_TRACE build: inside mach_run the gap from a conversion's
# END to the next conversion's START is never below DISP_N x 14188 E (the
# MINIMUM over the run), with a display change every delivered VERTB at
# s = GAP_S (an edit rate far above the budget's, so the budget, not the
# guest, bounds the conversions).

GAP_S = 2
GAP_NV = 160                # handler runs (edits) before the final colour


def leg_gap(prg: str) -> list[str]:
    from tests import run_irq as I
    from tests import run_mach as M
    from tests.run_mach import BASE, F_INIT, F_SUPER, clean

    def ram(rig, fn):
        """Wave 13 (supervisor; rev-vis round 3): the trace bytes live in BHI
        (RAM under BASIC ROM), so the host reads and writes them with $01 =
        $34 around the access; a plain read at $37 returns BASIC ROM."""
        rig.t.write_memory(0x0001, bytes([0x34]), override="run_pf gap: RAM under BASIC")
        try:
            return fn()
        finally:
            rig.t.write_memory(0x0001, bytes([0x37]), override="run_pf gap: runner port")

    def body(rig):
        f: list[str] = []
        n_dn = rig.lab("DISP_N")
        rig.call(rig.lab("chip_reset"))
        rig.call(rig.lab("cop_reset"))
        rig.call(rig.lab("disp_init"))
        ram(rig, lambda: (rig.w(rig.lab("disp_gmin"), b"\xFF" * 4),
                          rig.w(rig.lab("disp_tval"), b"\0"),
                          rig.w(rig.lab("disp_ngap"), b"\0\0")))
        pl0, pl1 = rnd(200, 8000), rnd(201, 8000)
        n0 = len(I.link(live_prog(0, 0, GAP_NV, n_dn + 4, True), BASE))
        lw, col1 = live_list(0, 0)
        lst = BASE + 2 * n0
        a0 = lst + 2 * len(lw)
        a1 = a0 + 8000
        lw, col1 = live_list(a0, a1)
        words = I.link(live_prog(lst, lst + 2 * col1, GAP_NV, n_dn + 4, True), BASE)
        rig.stage(words, lw, list(struct.unpack(">4000H", pl0)),
                  list(struct.unpack(">4000H", pl1)))
        res = rig.run(BASE, s=GAP_S, flags=F_INIT | F_SUPER)
        if not clean(res, "gap", f):
            return f
        gmin = struct.unpack("<I", ram(rig, lambda: rig.r(rig.lab("disp_gmin"), 4)))[0]
        ngap = struct.unpack("<H", ram(rig, lambda: rig.r(rig.lab("disp_ngap"), 2)))[0]
        nconv = struct.unpack("<H", rig.r(rig.lab("disp_nconv"), 2))[0]
        need = n_dn * FRAME_E
        print(f"  gap: frames {res['frames']} conversions {nconv} gaps {ngap} "
              f"least END->START {gmin} E, budget {need} E "
              f"({gmin / need:.2f} x the budget)")
        if ngap < 3:
            f.append(f"only {ngap} gaps measured (the leg needs >= 3)")
        if gmin < need:
            f.append(f"a conversion started {gmin} E after the previous one "
                     f"ended: the budget is {need} E (DISP_N {n_dn} frames)")
        return f
    return M.session(prg, body)


# --- leg "cols" (W12-V fix round 1): disp_cols vs c2v_ref.separate, fuzzed ------

COLS_N = 1500
COLS_TRAMP = 0xC780


def _bank_tramp(target: int, x: int) -> bytes:
    """lda $01 / pha / lda #$36 / sta $01 / ldx #x / jsr target / pla / sta $01 / rts"""
    return bytes([0xA5, 0x01, 0x48, 0xA9, 0x36, 0x85, 0x01, 0xA2, x,
                  0x20, target & 0xFF, target >> 8, 0x68, 0x85, 0x01, 0x60])


def leg_cols(prg: str, perf: bool = False) -> list[str]:
    """rev-vis's twin fuzz, one VICE session: COLS_N disp_cols calls (random,
    dark, one-LUT-colour, few-values, two-class and near-equal palettes; every
    count shape incl. all-255 ties; random LUT 1 case in 5) against
    c2v_ref.separate, plus injectivity over the pens in use and pens not in
    use left at their LUT colour; 200 mc2-shape calls (X = 3: pens 4-15
    untouched); c2v_hires's pens and counts on adversarial layouts.  --perf:
    the cycles of those layouts, disp_msum and c2v_hfill."""
    from c64_test_harness import (Labels, UnifiedManager, ViceConfig, read_bytes,
                                  write_bytes)
    from tests.harness_util import REU_VICE_ARGS, lookup_label
    from tests.run_vectors import prepare_target
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    lab = {k: lookup_label(labels, k) for k in ("disp_cols", "disp_msum", "c2v_args",
                                                "c2v_v_col", "c2v_v_cnt", "c2v_hfill")}
    real = R.build_lut()
    rnd = random.Random(0x5EED)
    rlut = bytes(rnd.randrange(16) for _ in range(4096))
    luts = {C.LUT_REAL: real, C.LUT_RAND: rlut}
    pre: dict[int, list[int]] = {}
    for c in range(4096):
        pre.setdefault(real[c], []).append(c)
    fails: list[str] = []
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False, extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            s = C.Session(target, labels)
            s.stage(C.LUT_REAL, real)
            s.stage(C.LUT_RAND, rlut)
            C.call_checked(s, "c2v_init", fails)

            def run_cols(pal, cnt, lut_addr, x=15):
                s.write_ram(lab["c2v_args"] + 12, lut_addr.to_bytes(3, "little"))
                s.write_ram(lab["c2v_args"] + 16, b"".join(c.to_bytes(2, "little") for c in pal))
                s.write_ram(lab["c2v_v_cnt"], bytes(cnt))
                write_bytes(s.T, COLS_TRAMP, _bank_tramp(lab["disp_cols"], x))
                s.call(COLS_TRAMP)
                return list(s.read_ram(lab["c2v_v_col"], 16))

            def gen(i):
                kind, lut_addr = i % 6, (C.LUT_RAND if i % 5 == 4 else C.LUT_REAL)
                if kind == 0:
                    pal = [rnd.randrange(0x10000) for _ in range(16)]
                elif kind == 1:
                    pal = [rnd.randrange(0x10000) & 0xF333 for _ in range(16)]
                elif kind == 2:                   # 16 distinct OCS, one LUT colour
                    v = rnd.choice([k for k in pre if len(pre[k]) >= 16])
                    pal = [c | (rnd.randrange(16) << 12) for c in rnd.sample(pre[v], 16)]
                    lut_addr = C.LUT_REAL
                elif kind == 3:
                    base = [rnd.randrange(4096) for _ in range(3)]
                    pal = [rnd.choice(base) | (rnd.randrange(16) << 12) for _ in range(16)]
                elif kind == 4:
                    a, b = rnd.sample([k for k in pre if len(pre[k]) >= 8], 2)
                    pal = [rnd.choice(pre[rnd.choice((a, b))]) for _ in range(16)]
                    lut_addr = C.LUT_REAL
                else:
                    g = rnd.randrange(16)
                    pal = [((g + rnd.randrange(-1, 2)) % 16) * 0x111 for _ in range(16)]
                mode = rnd.randrange(4)
                if mode == 0:
                    cnt = [rnd.choice([0, 1, 2, 3, 254, 255]) for _ in range(16)]
                elif mode == 1:
                    cnt = [255] * 16
                elif mode == 2:
                    cnt = [rnd.choice([0, 1]) for _ in range(16)]
                else:
                    cnt = [rnd.randrange(256) for _ in range(16)]
                cnt[0] = rnd.randrange(256)       # pen 0's count must not matter
                return pal, cnt, lut_addr, luts[lut_addr]

            nbad = 0
            for i in range(COLS_N):
                pal, cnt, lut_addr, lut = gen(i)
                got = run_cols(pal, cnt, lut_addr)
                want = R.separate(pal, lut, cnt)
                use = [0] + [p for p in range(1, 16) if cnt[p]]
                bad = [p for p in use if got[p] != want[p]]
                bad += [p for p in range(16) if p not in use and got[p] != lut[pal[p] & 0xFFF]]
                inj = all((got[a] == got[b]) == ((pal[a] & 0xFFF) == (pal[b] & 0xFFF))
                          for a in use for b in use)
                if bad or not inj:
                    nbad += 1
                    if nbad <= 5:
                        fails.append(f"case {i}: pal {[hex(c) for c in pal]} cnt {cnt}: "
                                     f"got {got} want {want} (bad pens {bad}, injective {inj})")
            print(f"  disp_cols fuzz: {COLS_N} cases, {nbad} mismatch(es)")
            for i in range(200):
                pal = [rnd.randrange(0x10000) & (0xF333 if i % 2 else 0xFFFF) for _ in range(16)]
                cnt = [0, 1, 1, 1] + [0] * 12
                s.write_ram(lab["c2v_v_col"], bytes(range(16)))
                got = run_cols(pal, cnt, C.LUT_REAL, x=3)
                want = R.separate(pal, real, cnt)
                if got[:4] != want[:4] or got[4:] != list(range(4, 16)):
                    fails.append(f"mc2 case {i}: got {got} want {want[:4]}")
            print("  mc2 shape: 200 cases")
            lines = s.rows * 8
            nh = lines * R.HIRES_ROW
            z = bytes(nh)
            cases = {
                "allpen1": [bytes([0xFF]) * nh, z, z, z],
                "random": [bytes(rnd.randrange(256) for _ in range(nh)) for _ in range(4)],
                "right-bit0": [bytes([0, 1] * (nh // 2)), z, z, z],
                "right-bit0-p15": [bytes([0, 1] * (nh // 2))] * 4,
                "left-bit7": [bytes([0x80, 0] * (nh // 2)), z, z, z],
                "last-line-bit0": [bytes(([0] * 80) * 7 + [0, 1] * 40) * (lines // 8), z, z, z],
                "empty": [z] * 4,
            }
            pal = [0x000, 0xFFF, 0xF00, 0x0F0] + [0x111 * k for k in range(4, 16)]
            for name, planes in cases.items():
                for p in range(4):
                    s.stage(C.HIRES_SRC[p], planes[p])
                s.write_ram(lab["c2v_args"], R.pack_args(C.HIRES_SRC, C.LUT_REAL, pal))
                C.call_checked(s, "c2v_hires", fails)
                cnt = list(s.read_ram(lab["c2v_v_cnt"], 16))
                ref = R.convert_hires(planes, pal, real, rows=s.rows)
                colram = bytes(b & 15 for b in read_bytes(s.T, 0xD800, 1000))
                if cnt != ref["cnt"]:
                    fails.append(f"hires {name}: cnt {cnt} want {ref['cnt']}")
                if colram[:s.rows * 40] != bytes(ref["pens"]):
                    fails.append(f"hires {name}: colour RAM pens differ")
                if perf:
                    print(f"  perf c2v_hires {name:16s} {C.measure(s, 'c2v_hires'):9d} cycles")
            if perf:
                for name in ("disp_msum", "c2v_hfill"):
                    write_bytes(s.T, COLS_TRAMP, _bank_tramp(lab[name], 0))
                    write_bytes(s.T, C.TRAMP, C.perf_tramp(COLS_TRAMP))
                    s.call(C.TRAMP)
                    print(f"  perf {name:24s} {int.from_bytes(read_bytes(s.T, C.PERF_OUT, 4), 'little'):9d}"
                          " cycles (incl. ~25 tramp)")
                v = max(pre, key=lambda k: len(pre[k]))
                s.write_ram(lab["c2v_args"] + 16, b"".join(c.to_bytes(2, "little")
                                                           for c in rnd.sample(pre[v], 16)))
                s.write_ram(lab["c2v_v_cnt"], bytes([0] + list(range(1, 16))))
                write_bytes(s.T, COLS_TRAMP, _bank_tramp(lab["disp_cols"], 15))
                write_bytes(s.T, C.TRAMP, C.perf_tramp(COLS_TRAMP))
                s.call(C.TRAMP)
                print(f"  perf disp_cols worst (16 pens, one LUT colour) "
                      f"{int.from_bytes(read_bytes(s.T, C.PERF_OUT, 4), 'little')} cycles")
        finally:
            mgr.release(target)
    return fails


# --- leg "view" (W13-D): the 1:1 view through disp_frame / disp_refresh ---------

VIEW_REGION = (0x140000, 0x20000, 5)            # 256 interleaved hires lines
WB_BASE = 0x160000                              # W13-D: a 2-plane hires Workbench-like screen
WB_PAL = [0xAAA, 0x000, 0xFFF, 0x68B]           # 3.1's Workbench colours (W13-W, boot model)


def wb_planes(seed: int = 13) -> list[bytes]:
    """W13-D: 640x256, 2 planes (80-byte rows, BPLxMOD 0): a title bar
    (lines 0-10: pen 2 with pen-1 Topaz-like glyphs), then pen-0 background
    with pen-1 glyphs and a few pen-3 cells."""
    rng = random.Random(seed)
    pl = [bytearray(256 * 80) for _ in range(2)]
    for r in range(32):
        for bx in range(80):
            bg, fg = (2, 1) if r < 2 else ((0, 3) if rng.random() < 0.1 else (0, 1))
            for y in range(r * 8, r * 8 + 8):
                bits = rng.randrange(256) if y % 8 not in (0, 7) else 0
                for x in range(8):
                    pen = fg if bits >> (7 - x) & 1 else bg
                    for p in range(2):
                        if pen >> p & 1:
                            pl[p][y * 80 + bx] |= 0x80 >> x
    return [bytes(b) for b in pl]


LACE_BASE = 0x180000                            # W13-D: a 640x512 interlaced 2-plane frame


def lace_planes(seed: int = 21) -> list[bytes]:
    """W13-D: a 640x512 2-plane frame image (frame line k at +80k), drawn
    as 8-frame-line Topaz-like text: pen 1 on pen 0, a title band of pen 1
    on pen 2.  Each field alone shows every other glyph line."""
    rng = random.Random(seed)
    pl = [bytearray(512 * 80) for _ in range(2)]
    for r in range(64):
        for bx in range(80):
            bg = 2 if r < 3 else 0
            for y in range(r * 8, r * 8 + 8):
                bits = rng.randrange(256) if y % 8 else 0
                for x in range(8):
                    pen = 1 if bits >> (7 - x) & 1 else bg
                    for p in range(2):
                        if pen >> p & 1:
                            pl[p][y * 80 + bx] |= 0x80 >> x
    return [bytes(b) for b in pl]


LACE_ST = dict(STD, bplcon0=0xA204, bplpt=[LACE_BASE, LACE_BASE + 0xA000],
               bpl1mod=80, bpl2mod=80, ddfstrt=0x3C, ddfstop=0xD4,
               diwstrt=0x2C81, diwstop=0x2CC1, color=[0xAAA, 0x000, 0xFFF, 0x68B] + [0] * 28)


WB_ST = dict(STD, bplcon0=0xA200, bplpt=[WB_BASE, WB_BASE + 0x5000], bpl1mod=0, bpl2mod=0,
             ddfstrt=0x3C, ddfstop=0xD4, diwstrt=0x2C81, diwstop=0x2CC1,
             color=WB_PAL + [0x123] * 28)
VIEW_HIRES = dict(STD, bplcon0=0xC200, bplpt=[0x140000 + p * 80 for p in range(4)],
                  bpl1mod=240, bpl2mod=240, ddfstrt=0x3C, ddfstop=0xD4,
                  diwstrt=0x2C81, diwstop=0x2CC1,          # 256 lines (PAL)
                  color=[0x000, 0x111, 0x222, 0x024, 0x0AAA, 0xFFF, 0xF00, 0x0F0,
                         0x00F, 0xFF0, 0x0FF, 0xF0F, 0x888, 0x444, 0xCCC, 0x123] + [0] * 16)


def view_steps() -> list[dict]:
    """(name, action, host writes before, the model's view (view, px, py) or
    None for no conversion).  action: kick (disp_frame through shim_kick) or
    refresh."""
    st = dict(VIEW_HIRES, gen=0x21)
    mc2 = dict(STD, gen=0x24, bplpt=[0x100000, 0x102000], color=pal(40))
    return [
        {"name": "fit", "st": st, "act": "refresh", "view": (0, 20, 0)},
        {"name": "f1-to-1:1", "st": st, "act": "kick", "pre": {"inp_view": 0x81},
         "view": (1, 20, 0)},                   # px 20 centred, W14-D: the top line
        {"name": "same-no-conv", "st": st, "act": "kick", "pre": {"inp_view": 0x80},
         "view": None},
        {"name": "test-offset", "st": st, "act": "refresh",
         "pre": {"disp_px": 0, "disp_py": 3}, "view": (1, 0, 3)},
        {"name": "right-bottom", "st": st, "act": "refresh",
         "pre": {"disp_px": 40, "disp_py": 200}, "view": (1, 40, 56)},   # clamped
        {"name": "new-gen-1:1", "st": dict(st, gen=0x22, color=pal(41)), "act": "kick",
         "pre": {"disp_px": 7, "disp_py": 9}, "view": (1, 7, 9)},
        {"name": "f1-back-fit", "st": dict(st, gen=0x22, color=pal(41)), "act": "kick",
         "pre": {"inp_view": 1}, "view": (0, 20, 9)},
        {"name": "mc2-in-1:1", "st": mc2, "act": "kick",
         "pre": {"disp_view": 1}, "view": (1, 20, 0)},  # lores: the fit view
        {"name": "dark-1:1", "st": dict(st, gen=0x25), "act": "kick",
         "pre": {"disp_view": 1, "disp_px": 20, "disp_py": 28}, "view": (1, 20, 28)},
        # W13-D: hires BPU 1-3 convert (were BLANK, why DEPTH); the Workbench's 2 planes
        {"name": "wb2-fit", "st": dict(WB_ST, gen=0x26), "act": "kick",
         "pre": {"disp_view": 0}, "view": (0, 20, 0), "exact": True},
        {"name": "wb2-1:1-title", "st": dict(WB_ST, gen=0x27), "act": "refresh",
         "pre": {"disp_view": 1, "disp_px": 3, "disp_py": 0}, "view": (1, 3, 0),
         "exact": True},
        {"name": "wb1-1:1", "st": dict(WB_ST, gen=0x28, bplcon0=0x9200), "act": "kick",
         "pre": {"disp_view": 1, "disp_px": 40, "disp_py": 56}, "view": (1, 40, 56),
         "exact": True},
        {"name": "wb3-fit", "st": dict(WB_ST, gen=0x29, bplcon0=0xB200,
                                       bplpt=[WB_BASE, WB_BASE + 0x5000, 0x140000]),
         "act": "kick", "pre": {"disp_view": 0}, "view": (0, 20, 0)},
        # W13-D: LACE in the 1:1 view (both fields); the fit view keeps one field
        {"name": "lace-fit", "st": dict(LACE_ST, gen=0x2A), "act": "kick",
         "pre": {"disp_view": 0}, "view": (0, 20, 0)},
        {"name": "lace-f1-centre", "st": dict(LACE_ST, gen=0x2A), "act": "kick",
         "pre": {"inp_view": 1}, "view": (1, 20, 0), "exact": True},    # W14-D: the top
        {"name": "lace-odd-top", "st": dict(LACE_ST, gen=0x2B,
                                            bplpt=[LACE_BASE + 80, LACE_BASE + 0xA050]),
         "act": "refresh", "pre": {"disp_px": 0, "disp_py": 0}, "view": (1, 0, 0),
         "exact": True},
        {"name": "nolace-after", "st": dict(WB_ST, gen=0x2C), "act": "kick",
         "pre": {"disp_py": 40}, "view": (1, 0, 40), "exact": True},
        # fix round 1: the odd field at the bottom clamp (the last frame line
        # read <= 2L - 1); a LACE screen with a negative modulo (advance -80
        # per frame line: the arithmetic shift); rev-vis case 165 (L = 180:
        # rows past the picture read 0); hires BPU 5 stays BLANK (DEPTH)
        {"name": "lace-odd-bottom", "st": dict(LACE_ST, gen=0x2D,
                                               bplpt=[LACE_BASE + 80, LACE_BASE + 0xA050]),
         "act": "refresh", "pre": {"disp_px": 40, "disp_py": 255}, "view": (1, 40, 255)},
        {"name": "lace-negmod", "st": dict(LACE_ST, gen=0x2E, bpl1mod=-240 & 0xFFFF,
                                           bpl2mod=-240 & 0xFFFF,
                                           bplpt=[LACE_BASE + 511 * 80,
                                                  LACE_BASE + 0xA000 + 511 * 80]),
         "act": "refresh", "pre": {"disp_px": 8, "disp_py": 20}, "view": (1, 8, 20),
         "exact": True},
        {"name": "short-180", "st": dict(VIEW_HIRES, gen=0x2F, diwstop=0xE0C1),
         "act": "refresh", "pre": {"disp_px": 5, "disp_py": 0}, "view": (1, 5, 0)},
        {"name": "bpu5-blank", "st": dict(VIEW_HIRES, gen=0x30, bplcon0=0xD200),
         "act": "kick", "pre": {}, "view": None, "blank": R.WHY_DEPTH},
    ]


def leg_view(prg: str, perf: bool = False) -> list[str]:
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS, lookup_label
    from tests.run_vectors import prepare_target
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
            try:
                for k in ("inp_view", "disp_view", "disp_px", "disp_py", "disp_one",
                          "disp_ymax", "disp_crop"):
                    s.lab[k] = lookup_label(labels, k)
            except KeyError as e:
                print(f"  view: FAIL ({e}: no 1:1 view in this PRG)")
                return [str(e)]
            mem = Mem()
            for base, n, seed in REGIONS + [VIEW_REGION]:
                data = rnd(seed, n)
                s.stage(base, data)
                mem.put(base, data)
            wb = b"".join(wb_planes())                  # W13-D
            s.stage(WB_BASE, wb)
            mem.put(WB_BASE, wb)
            lc = b"".join(lace_planes())
            s.stage(LACE_BASE, lc)
            mem.put(LACE_BASE, lc)
            f: list[str] = []
            C.call_checked(s, "disp_init", f)
            fails += [f"init: {x}" for x in f]
            for step in view_steps():
                f = view_step(s, step, mem, lut)
                fails += [f"{step['name']}: {x}" for x in f]
            if perf:                     # PERF (o): fit vs 1:1, the same picture
                st = dict(VIEW_HIRES, gen=0x30)
                s.write_ram(s.lab["disp_st"], R.pack_disp_st(st))
                for name, view in (("fit hires4", 0), ("1:1 centred", 1),
                                   ("fit hires4 (again)", 0)):
                    s.wr(s.lab["disp_view"], bytes([view]))
                    s.wr(s.lab["disp_px"], bytes([R.PX_CENTRE]))
                    s.wr(s.lab["disp_py"], bytes([28]))
                    cyc = C.measure(s, "disp_refresh")
                    print(f"  perf disp_refresh {name:20s} {cyc:9d} cycles")
        finally:
            mgr.release(target)
    return fails


def pixel_exact(s: Pf, st: dict, mem: Mem, d: dict, px: int) -> list[str]:
    """W13-D: every cell of the 1:1 window drawn in at most two pens shows
    each pixel in its pen's colour (read from the C64: bitmap, matrix)."""
    bmp = s.read_ram(s.bitmap, 8000)
    scr = s.read_ram(s.screen, 1000)
    vic = R.separate(st["color"][:16], R.build_lut(), R.one_cells(
        [R.disp_rows(mem.read, (d["src"][p] + px) & 0xFFFFFF, R.ONE_ROW,
                     d["width"] + d["skip"][p] - R.ONE_ROW) for p in range(d["planes"])]
        + [bytes(200 * R.ONE_ROW)] * (4 - d["planes"]))[2])
    rows = [R.disp_rows(mem.read, (d["src"][p] + px) & 0xFFFFFF, R.ONE_ROW,
                        d["width"] + d["skip"][p] - R.ONE_ROW) for p in range(d["planes"])]
    bad = cells = 0
    for c in range(1000):
        r, cx = divmod(c, 40)
        pix = [[sum(1 << p for p in range(d["planes"])
                    if rows[p][y * R.ONE_ROW + cx] & (0x80 >> x)) for x in range(8)]
               for y in range(r * 8, r * 8 + 8)]
        if len({v for row in pix for v in row}) > 2:
            continue
        cells += 1
        for y in range(8):
            b = bmp[r * 320 + cx * 8 + y]
            for x in range(8):
                shown = scr[c] >> 4 if b & (0x80 >> x) else scr[c] & 15
                bad += shown != vic[pix[y][x]]
    return [f"pixel-exact: {bad} pixel(s) wrong in {cells} two-pen cells"] if bad else []


def view_step(s: Pf, step: dict, mem: Mem, lut: bytes) -> list[str]:
    for k, v in step.get("pre", {}).items():
        s.wr(s.lab[k], bytes([v]))
    st = dict(step["st"])
    full = {**{k: 0 for k in ("bplcon1", "bplcon2", "bpl1mod", "bpl2mod")}, **st}
    full["bplpt"] = list(st.get("bplpt", [])) + [0] * 6
    full["color"] = list(st.get("color", [])) + [0] * 32
    before = state(s)
    pix = s.read_ram(s.lab["disp_st"] + PIX_OFF, 1)
    g = C.guard_fill(s, zlib.crc32(("view/" + step["name"]).encode()))
    s.write_ram(s.lab["disp_st"] + PIX_OFF, pix)
    g["pix"] = pix[0]
    s.write_ram(s.lab["disp_st"], R.pack_disp_st(st))
    f: list[str] = []
    C.call_checked(s, "disp_refresh" if step["act"] == "refresh" else "shim_kick", f)
    after = state(s)
    iv = s.rd(s.lab["inp_view"], 1)[0]
    if iv & 1:
        f.append(f"inp_view = ${iv:02X}: bit 0 not consumed")
    want_iv = step.get("pre", {}).get("inp_view", iv) & 0xFE
    if "inp_view" in step.get("pre", {}) and iv != want_iv:
        f.append(f"inp_view = ${iv:02X}, want ${want_iv:02X} (only bit 0 is D's)")
    if step.get("blank"):
        if (after["mode"], after["why"]) != (R.MODE_BLANK, step["blank"]):
            f.append(f"state {after}: want BLANK, why {step['blank']}")
        f = [x for x in f if "m_rok" not in x]
    elif step["view"] is None:
        top, io = C.expected_blocks(s, g, {"bitmap": b"", "screen": b""})
        top = bytearray(top)
        top[C.NMIVEC - 0xE000:C.NMIVEC - 0xE000 + 2] = g["top"][
            C.NMIVEC - 0xE000:C.NMIVEC - 0xE000 + 2]
        f = [x for x in f if "m_rok = 1" not in x]
        f += C.diff("RAM $E000-$FFFF", s.read_ram(0xE000, 0x2000), bytes(top), 0xE000)
        f += C.diff("RAM under I/O", s.read_ram(0xD000, 0x1000), io, 0xD000)
        if after != before:
            f.append(f"state {after}, want {before} (no conversion)")
    else:
        view, px, py = step["view"]
        want = R.disp_convert_view(full, mem.read, lut, view, px, py)
        if want["colram"] is None:      # the 1:1 view leaves colour RAM alone
            want["colram"] = g["col"]
        f += check_outputs(s, g, want, R.pack_disp_st({**st, "dirty": 0}))
        vic = s.vic()
        exp = dict(want["vic"])
        exp[0xDD00] = 0
        for a, v in exp.items():
            if vic[a] != v & VIC_MASK[a]:
                f.append(f"VIC ${a:04X} = ${vic[a]:02X}, want ${v & VIC_MASK[a]:02X}")
        mck = int.from_bytes(s.read_ram(s.lab["disp_mck"], 2), "little")
        if mck != want["mck"]:
            f.append(f"disp_mck ${mck:04X}, want ${want['mck']:04X}")
        d = want["decide"]
        got = {k: s.rd(s.lab[k], 1)[0] for k in ("disp_view", "disp_one", "disp_crop",
                                                 "disp_ymax")}
        exp_s = {"disp_view": view, "disp_one": int(d["one"]),
                 "disp_crop": d["crop"], "disp_ymax": d["ymax"]}
        if got != exp_s:
            f.append(f"view state {got}, want {exp_s}")
        if d["one"] and s.rd(s.lab["disp_px"], 1)[0] != px:
            f.append(f"disp_px {s.rd(s.lab['disp_px'], 1)[0]}, want {px}")
        if after["nconv"] != before["nconv"] + 1:
            f.append(f"nconv {after['nconv']}, want {before['nconv'] + 1}")
        if step.get("exact") and d["one"]:
            f += pixel_exact(s, full, mem, d, px)
    print(f"  {step['name']:16s} {'OK' if not f else 'FAIL'}")
    for x in f:
        print(f"      {x}")
    return f


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--variant", choices=sorted(VARIANTS))
    ap.add_argument("--teeth", action="store_true")
    ap.add_argument("--expect-fail", action="store_true")
    ap.add_argument("--perf", action="store_true",
                    help="shim leg: also time disp_refresh / disp_frame")
    ap.add_argument("--leg", default="all",
                    help="comma list of " + ", ".join(ALL_LEGS) + " (live:s0 = "
                    "only the s = 0 edit case); default all")
    a = ap.parse_args(argv)
    prg = os.path.abspath(a.prg)
    legs = ALL_LEGS if a.leg == "all" else a.leg.split(",")

    def run_legs(p: str, legs: list[str] = legs) -> list[str]:
        fails = []
        if "shim" in legs:
            sp = p if has_label(p, "shim_kick") else build(p, "shim", [], True)
            print(f"run_pf shim leg: {sp}")
            fails += run_shim(sp, a.perf)
        if "fixture" in legs:
            print(f"run_pf fixture leg: {p}")
            fails += [f"pf_static: {x}" for x in run_fixture(p)]
        if "wrap" in legs:
            print(f"run_pf wrap leg: {p}")
            fails += leg_wrap(p)
        if "dosbuf" in legs:
            print(f"run_pf dosbuf leg: {p}")
            fails += leg_dosbuf(p)
        if "gap" in legs:                # W11-A (supervisor grant, efrom)
            tp = p if has_label(p, "disp_gmin") else build(p, "trace", VARIANTS["trace"], True)
            print(f"run_pf gap leg: {tp}")
            fails += [f"gap: {x}" for x in leg_gap(tp)]
        if "cols" in legs:               # W12-V (fix round 1, rev-vis's fuzz)
            print(f"run_pf cols leg: {p}")
            fails += [f"cols: {x}" for x in leg_cols(p, a.perf)]
        if "view" in legs:               # W13-D: the 1:1 view
            sp = p if has_label(p, "shim_kick") else build(p, "shim", [], True)
            print(f"run_pf view leg: {sp}")
            fails += [f"view: {x}" for x in leg_view(sp, a.perf)]
        if "live" in legs or "live:s0" in legs:
            print(f"run_pf live leg: {p}")
            cases = ([(0, True, "irq")] if "live:s0" in legs and "live" not in legs
                     else None)
            fails += [f"live: {x}" for x in leg_live(p, cases)]
        return fails

    if a.teeth:
        caught = {}
        for name in TEETH:
            print(f"variant {name} ({' '.join(VARIANTS[name])}), legs "
                  f"{','.join(MUTANT_LEGS[name])}:")
            caught[name] = len(run_legs(build(prg, name, VARIANTS[name], True),
                                        MUTANT_LEGS[name]))
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
