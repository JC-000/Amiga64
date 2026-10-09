#!/usr/bin/env python3
"""VIC converter v0 (runtime/ops/c2v.s) against host/c2v_ref.py, in VICE.

W7-V, docs/C2V.md.  Direct API: host-generated bitplanes and the palette
LUT are staged into guest (REU) memory through the runner's own
test_load_chunk (the memory API, an independent path from the module's
DMA), the argument block is poked into c2v_args, and the entries are
called with jsr.  The layout (C2V_ROWS, c2v_bitmap, c2v_screen) is read
from the PRG's labels.  Per case and kernel:

  * the whole RAM at $E000-$FFFF and the whole RAM under I/O $D000-$DFFF
    are filled with a guard pattern first; afterwards they must equal the
    pattern with exactly the reference bitmap, screen matrix and (for a
    matrix under I/O) the RAM NMI vector -> mach_nmi overlaid: nothing
    else in either block may change (REU DMA never targets $D000-$DFFF);
  * the low nibbles of colour RAM and c2v_a_bgc equal the reference;
  * guards: $B800-$B8FF (free RAM just below the work RAM) and the VIC/CIA2
    display registers ($D011 $D016 $D018 $D020 $D021 $DD00);
  * after every entry: $01 is back at the caller's $37, the I flag is back
    at the caller's (clear: every entry is called with CIA1 IRQ sources
    masked and cli), and m_rok = 0 (C1).
  * one case puts a hires plane, both mc2 planes and the LUT across 64 KB
    REU banks (review F3): the plane pointers' bank carry.
  * NMI leg (matrix under I/O only): a CIA2 timer-A NMI is armed to fire
    inside the $01=$34 screen fill, with the RAM NMI vector first pointed
    at a `jmp *` trap.  The call must return with correct output, the NMI
    must have been taken by mach_nmi, the one NMI handler (W8-L review F1:
    its counter mach_nmis +1) and acknowledged (ICR $00), and a LATER CIA2
    timer-B NMI must still be delivered to the KERNAL (the handler released
    /NMI, review F2).  A control NMI before the fill must be taken by the
    KERNAL, not the handler.

Legs:
  (default)          correctness + NMI, the given PRG must pass
  --variant NAME     build a test-only variant of the given PRG's tree (in
                     the Makefile's link order) and run it: stub | bitorder
                     | lut | row | nmi | nmiack | pickr | hrow | hfillbg (mutants),
                     layout-b (176 lines, matrix at $FC00: must PASS),
                     nofetch (--perf only); --expect-fail inverts the exit
  --teeth            the mutants (W12-V adds pickr, hrow, hfillbg), each must fail
  --perf             VICE cycles per frame of each kernel (CIA2 TA->TB
                     32-bit phi2 count, screen blanked: no badlines)

Usage: python3 -m tests.run_c2v --prg build/w7-c2v/runner.prg [--perf]
VICE only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import struct
import subprocess
import sys
import zlib

from host import c2v_ref as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUF_CASE, BUF_RESULT = 0xC000, 0xC800
TRAMP = 0xC700                  # buf_case tail: records are staged below it
TRAP = 0xC7E0                   # `jmp *`: where an unguarded NMI would land
FLAGTRAMP = 0xC7B0              # cli / jsr entry / php: the I flag after an entry
FLAGOUT = 0xC7EC
PERF_OUT = 0xC7F0
MAX_PAYLOAD = 0x700 - 8         # keep records clear of the trampoline
PORT_RUN = 0x37                 # the runner's $01 (BASIC + KERNAL + I/O)
PORT_RAM = 0x34                 # host view: all RAM (incl. under I/O)
NMIVEC = 0xFFFA

# REU (guest) layout of the test data: one 512 KB block, no line crossed.
HIRES_SRC = [0x100000 + p * 0x4000 for p in range(4)]
MC2_SRC = [0x110000 + p * 0x4000 for p in range(2)]
LUT_REAL, LUT_RAND = 0x118000, 0x119000
# review F3: across 64 KB banks (hires plane 0 mid-row at $120000, mc2 plane
# 0 in row 0 at $130000, mc2 plane 1 ends at $17FFFF, the LUT at $150000)
BANK_HSRC = [0x11E000] + HIRES_SRC[1:]
BANK_MSRC = [0x12FFE8, 0x180000 - 200 * R.LORES_ROW]
LUT_BANK = 0x14F800

VARIANTS = {"stub": ["C2V_STUB=1"], "bitorder": ["C2V_MUT_BITORDER=1"],
            "lut": ["C2V_MUT_LUT=1"], "row": ["C2V_MUT_ROW=1"],
            "nmi": ["C2V_MUT_NMI=1"], "nmiack": ["MACH_MUT_NMIACK=1"],
            # W12-V: the cell pen, the row flush, the matrix byte
            "pickr": ["C2V_MUT_PICKR=1"], "hrow": ["C2V_MUT_HROW=1"],
            "hfillbg": ["C2V_MUT_HFILLBG=1"],
            # W13-D (1:1 view, kind "one"): bg pixel, fg re-pick, pair key, matrix
            "omask": ["C2V_MUT_OMASK=1"], "ofg": ["C2V_MUT_OFG=1"],
            "ofin": ["C2V_MUT_OFIN=1"]}
OTHER_VARIANTS = {"layout-b": ["C2V_ROWS=22", "c2v_screen=0xFC00"],
                  "nofetch": ["C2V_PERF_NOFETCH=1"]}
GUARD_REGS = (0xD011, 0xD016, 0xD018, 0xD020, 0xD021, 0xDD00)
GUARD_MASK = (0x7F, 0xFF, 0xFF, 0x0F, 0x0F, 0xFF)   # $D011 bit 7 = raster bit 8


def makefile_sources(root: str = ROOT) -> list[str]:
    """RUNTIME_SRC exactly as the Makefile links it (its own wildcard and any
    ordering rules, e.g. a module forced last), as absolute paths.  Variant
    builds must use this order, not sorted(glob): review F5."""
    rule = "c2v-print-src:\n\t@echo $(RUNTIME_SRC)\n"
    r = subprocess.run(["make", "-s", "--no-print-directory", "-f", "Makefile",
                        "-f", "-", "c2v-print-src"], input=rule, cwd=root,
                       capture_output=True, text=True, check=True)
    return [os.path.join(root, p) for p in r.stdout.split()]


def build_variant(prg: str, name: str, defines: list[str]) -> str:
    """Rebuild the tree with test-only defines next to *prg*."""
    bdir = os.path.dirname(os.path.abspath(prg))
    disp = os.path.join(bdir, "gen", "dispatch.s")
    if not os.path.exists(disp):
        raise FileNotFoundError(f"{disp} (build the runtime with make first)")
    out = os.path.join(bdir, "c2v-" + name)
    os.makedirs(out, exist_ok=True)
    vprg = os.path.join(out, "runner.prg")
    srcs = makefile_sources()
    defs = [x for d in defines for x in ("--asm-define", d)]
    cmd = ["cl65", "-t", "none", "-C", os.path.join(ROOT, "runtime", "c64.cfg"),
           *defs, "-o", vprg, "-Ln", os.path.join(out, "runner.lbl"),
           "-m", os.path.join(out, "runner.map")] + srcs + [disp]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    return vprg


def read_regs(T) -> bytes:
    from c64_test_harness import read_bytes
    return bytes(read_bytes(T, a, 1)[0] & m for a, m in zip(GUARD_REGS, GUARD_MASK))


# --- test data ---------------------------------------------------------------

def make_cases(lines: int) -> list[dict]:
    """Patterns x palettes.  Each: hires planes, mc2 planes, palette, LUT."""
    rng = random.Random(0xC2F)
    nh, nl = lines * R.HIRES_ROW, lines * R.LORES_ROW

    def rnd(n):
        return bytes(rng.randrange(256) for _ in range(n))

    def lineid(n, width, p):     # every byte names its line, column, plane
        return bytes(((i // width) * 7 + (i % width) * 13 + p * 61) & 0xFF
                     for i in range(n))

    def sparse(n):
        b = bytearray(n)
        for _ in range(n // 64):
            b[rng.randrange(n)] = 1 << rng.randrange(8)
        return bytes(b)

    def pal(n):
        return [rng.randrange(0x10000) for _ in range(n)]   # bits 15-12 junk
    return [
        {"name": "random/real-lut", "hires": [rnd(nh) for _ in range(4)],
         "mc2": [rnd(nl) for _ in range(2)], "pal": pal(16), "lut": "real"},
        {"name": "lineid/rand-lut", "hires": [lineid(nh, 80, p) for p in range(4)],
         "mc2": [lineid(nl, 40, p) for p in range(2)], "pal": pal(16), "lut": "rand"},
        {"name": "sparse/rand-lut", "hires": [sparse(nh) for _ in range(4)],
         "mc2": [sparse(nl) for _ in range(2)],
         "pal": [0x0000, 0x0FFF, 0x0F00, 0x000F] + pal(12), "lut": "rand"},
        {"name": "one-plane/real-lut",
         "hires": [bytes(nh)] * 3 + [rnd(nh)], "mc2": [bytes(nl), rnd(nl)],
         "pal": [0x0000, 0x0FFF, 0x0444, 0x0999] + pal(12), "lut": "real"},
        {"name": "text/real-lut", "hires": text_planes(rng, lines), "px": 0,
         "pal": [0x0000, 0x0111, 0x0222, 0x0024, 0x0AAA, 0x0FFF, 0x0F80, 0x0048]
         + pal(8), "lut": "real",
         "mc2": [rnd(nl) for _ in range(2)]},
        {"name": "three-pen/real-lut", "hires": three_pen_planes(lines), "px": 0,
         "pal": [0x000, 0xF00, 0x0F0, 0x00F, 0xFF0] + pal(11), "lut": "real",
         "mc2": [rnd(nl) for _ in range(2)]},      # W13-D: 3+ pens per cell (lossy, pinned)
        {"name": "bank-cross/bank-lut", "hires": [rnd(nh) for _ in range(4)],
         "mc2": [rnd(nl) for _ in range(2)], "lut": "bank",
         "pal": [0x07FF, 0x0800, 0x07FF, 0x0800] + pal(12),   # both LUT banks
         "hsrc": BANK_HSRC, "msrc": BANK_MSRC},
    ]


def text_planes(rng: random.Random, lines: int) -> list[bytes]:
    """W13-D: hires planes drawn like text: per 8x8 cell of the window
    (and of the rest of each 80-byte row) a bg and an fg pen from a few
    (title-bar-like pairs: bg <> 0 too), random glyph bits, a few cells
    with a third pen."""
    pens = [(0, 1), (0, 2), (2, 1), (3, 5), (0, 3), (1, 2), (7, 0)]
    pl = [bytearray(lines * R.HIRES_ROW) for _ in range(4)]
    for r in range(lines // 8):
        for bx in range(R.HIRES_ROW):
            bg, fg = rng.choice(pens)
            third = bx >= 40 and rng.random() < 0.05     # outside the px-0 window
            for y in range(r * 8, r * 8 + 8):
                bits = rng.randrange(256)
                for x in range(8):
                    pen = fg if bits >> (7 - x) & 1 else bg
                    if third and x == 3:
                        pen = 6
                    for p in range(4):
                        if pen >> p & 1:
                            pl[p][y * R.HIRES_ROW + bx] |= 0x80 >> x
    return [bytes(b) for b in pl]


def three_pen_planes(lines: int) -> list[bytes]:
    """W13-D: every cell drawn in pens 1, 2, 3 (and 4 on odd cells), by column."""
    pl = [bytearray(lines * R.HIRES_ROW) for _ in range(4)]
    for y in range(lines):
        for bx in range(R.HIRES_ROW):
            for x in range(8):
                pen = 1 + (x + y + (bx & 1)) % (3 + (bx & 1))
                for p in range(4):
                    if pen >> p & 1:
                        pl[p][y * R.HIRES_ROW + bx] |= 0x80 >> x
    return [bytes(b) for b in pl]


def window(planes: list[bytes], px: int, lines: int) -> list[bytes]:
    """W13-D: the 1:1 window's rows: bytes px .. px+39 of each 80-byte row."""
    return [b"".join(p[y * R.HIRES_ROW + px:y * R.HIRES_ROW + px + R.ONE_ROW]
                     for y in range(lines)) for p in planes]


def port_tramp(target: int) -> bytes:
    """W13-D: flag_tramp at $01 = $36 (c2v_ones / c2v_ofin: BASIC out, as
    disp calls them); stores P, then $01 as returned, then restores $37."""
    return bytes([0xA9, 0x7F, 0x8D, 0x0D, 0xDC, 0xAD, 0x0D, 0xDC,
                  0xA9, 0x36, 0x85, 0x01,                # lda #$36 / sta $01
                  0x58,
                  0x20, target & 0xFF, target >> 8,
                  0x08, 0x78, 0x68,
                  0x8D, FLAGOUT & 0xFF, FLAGOUT >> 8,
                  0xA5, 0x01,                            # lda $01
                  0x8D, (FLAGOUT + 1) & 0xFF, (FLAGOUT + 1) >> 8,
                  0xA9, PORT_RUN, 0x85, 0x01,            # $01 := $37
                  0x60])


def call_banked(s, entry: str, fails: list[str]) -> None:
    """W13-D: call_checked through port_tramp: $01 must come back $36."""
    from c64_test_harness import read_bytes, write_bytes
    write_bytes(s.T, s.lab["m_rok"], b"\x01")
    write_bytes(s.T, FLAGTRAMP, port_tramp(s.lab[entry]))
    write_bytes(s.T, FLAGOUT, b"\xFF\xFF")
    s.call(FLAGTRAMP)
    rok = read_bytes(s.T, s.lab["m_rok"], 1)[0]
    p, port = read_bytes(s.T, FLAGOUT, 2)
    if p & 0x04:
        fails.append(f"{entry}: I flag set after return (P=${p:02X})")
    if rok != 0:
        fails.append(f"{entry}: m_rok = {rok} after return (C1: must be 0)")
    if port != 0x36:
        fails.append(f"{entry}: $01 = ${port:02X} after return (want $36)")


# --- VICE session --------------------------------------------------------------

class Session:
    def __init__(self, target, labels, timeout=60.0):
        from tests.harness_util import lookup_label
        self.t = target
        self.T = target.transport
        self.timeout = timeout
        self.lab = {k: lookup_label(labels, k) for k in (
            "c2v_init", "c2v_pal_mc2", "c2v_pal_hires", "c2v_mc2",
            "c2v_hires", "c2v_args", "test_load_chunk", "m_rok",
            "c2v_hfill", "c2v_v_col", "c2v_v_cnt")}         # W12-V
        for k in ("c2v_ones", "c2v_ofin"):                  # W13-D (absent: red)
            try:
                self.lab[k] = lookup_label(labels, k)
            except KeyError:
                pass
        self.layout(labels)

    def layout(self, labels) -> None:
        from tests.harness_util import lookup_label
        self.rows = lookup_label(labels, "C2V_ROWS")
        self.bitmap = lookup_label(labels, "c2v_bitmap")
        self.screen = lookup_label(labels, "c2v_screen")
        self.nmi_h = lookup_label(labels, "mach_nmi")      # the NMI handler
        self.nmis_at = lookup_label(labels, "mach_nmis")   # its counter
        self.under_io = 0xD000 <= self.screen <= 0xDFFF

    def call(self, name_or_addr) -> None:
        from tests.harness_util import call_runner
        addr = self.lab.get(name_or_addr, name_or_addr)
        call_runner(self.t, addr, self.timeout)

    def port(self, v: int) -> None:
        self.T.write_memory(0x0001, bytes([v]), override="c2v: host view of RAM under ROM/I/O")

    def read_ram(self, addr: int, n: int) -> bytes:
        """CPU RAM (under BASIC/KERNAL/I/O): $01 = $34 around the read."""
        from c64_test_harness import read_bytes
        self.port(PORT_RAM)
        try:
            return read_bytes(self.T, addr, n)
        finally:
            self.port(PORT_RUN)

    def write_ram(self, addr: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        self.port(PORT_RAM)
        try:
            write_bytes(self.T, addr, data)
        finally:
            self.port(PORT_RUN)

    def stage(self, guest: int, data: bytes) -> None:
        from c64_test_harness import read_bytes, write_bytes
        for pos in range(0, len(data), MAX_PAYLOAD):
            part = data[pos:pos + MAX_PAYLOAD]
            write_bytes(self.T, BUF_CASE,
                        struct.pack("<II", guest + pos, len(part)) + part)
            self.call("test_load_chunk")
            if read_bytes(self.T, BUF_RESULT, 1)[0] != 0xFF:
                raise RuntimeError("test_load_chunk did not complete")


def guard_fill(s: Session, seed: int) -> dict:
    """Fill every output and guard byte with a pattern; return what it wrote.

    The RAM NMI vector points at a `jmp *` trap, so an NMI taken through it
    before the module guards it hangs the call."""
    from c64_test_harness import write_bytes
    rng = random.Random(seed)
    top = bytearray(rng.randrange(256) for _ in range(0x2000))  # $E000-$FFFF
    top[NMIVEC - 0xE000:NMIVEC - 0xE000 + 2] = TRAP.to_bytes(2, "little")
    io = bytes(rng.randrange(256) for _ in range(0x1000))       # RAM $D000-$DFFF
    col = bytes(rng.randrange(16) for _ in range(1000))
    free_b = bytes(rng.randrange(256) for _ in range(0x100))    # $B800-$B8FF
    write_bytes(s.T, TRAP, bytes([0x4C, TRAP & 0xFF, TRAP >> 8]))
    s.write_ram(0xE000, bytes(top))
    s.write_ram(0xD000, io)
    write_bytes(s.T, 0xD800, col)
    s.write_ram(0xB800, free_b)
    s.write_ram(s.lab["c2v_args"] + R.ARG_BGC, bytes([rng.randrange(256)]))
    return {"top": bytes(top), "io": io, "col": col, "free_b": free_b}


def expected_blocks(s: Session, g: dict, ref: dict) -> tuple[bytes, bytes]:
    """The guard pattern with exactly the module's outputs overlaid."""
    top, io = bytearray(g["top"]), bytearray(g["io"])
    for base, data in ((s.bitmap, ref["bitmap"]), (s.screen, ref["screen"])):
        for blk, lo in ((top, 0xE000), (io, 0xD000)):
            if lo <= base < lo + len(blk):
                blk[base - lo:base - lo + len(data)] = data
    if s.under_io:
        top[NMIVEC - 0xE000:NMIVEC - 0xE000 + 2] = s.nmi_h.to_bytes(2, "little")
    return bytes(top), bytes(io)


def diff(name: str, got: bytes, want: bytes, base: int = 0) -> list[str]:
    bad = [i for i in range(len(want)) if got[i] != want[i]]
    if not bad:
        return []
    i = bad[0]
    return [f"{name}: {len(bad)}/{len(want)} byte(s) differ, first at "
            f"${base + i:04X} (got ${got[i]:02X}, want ${want[i]:02X})"]


def case_src(case: dict, kind: str) -> list[int]:
    if kind == "hires":
        return case.get("hsrc", HIRES_SRC)
    return case.get("msrc", MC2_SRC)


def case_lut(case: dict) -> int:
    return {"real": LUT_REAL, "rand": LUT_RAND, "bank": LUT_BANK}[case["lut"]]


def reference(s: Session, kind: str, case: dict, luts: dict) -> dict:
    conv = R.convert_hires if kind == "hires" else R.convert_mc2
    return conv(case[kind], case["pal"], luts[case["lut"]], rows=s.rows)


def flag_tramp(target: int) -> bytes:
    """Mask CIA1 IRQ sources, cli, jsr target, then store P and sei."""
    return bytes([0xA9, 0x7F, 0x8D, 0x0D, 0xDC,          # lda #$7F / sta $DC0D
                  0xAD, 0x0D, 0xDC,                      # lda $DC0D (ack)
                  0x58,                                  # cli
                  0x20, target & 0xFF, target >> 8,      # jsr target
                  0x08, 0x78, 0x68,                      # php / sei / pla
                  0x8D, FLAGOUT & 0xFF, FLAGOUT >> 8,    # sta FLAGOUT
                  0x60])


def call_checked(s: Session, entry, fails: list[str]) -> None:
    from c64_test_harness import read_bytes, write_bytes
    write_bytes(s.T, s.lab["m_rok"], b"\x01")   # a valid-looking shadow
    addr = s.lab.get(entry, entry)
    write_bytes(s.T, FLAGTRAMP, flag_tramp(addr))
    write_bytes(s.T, FLAGOUT, b"\xFF")
    s.call(FLAGTRAMP)
    rok = read_bytes(s.T, s.lab["m_rok"], 1)[0]
    port = read_bytes(s.T, 0x0001, 1)[0]
    p = read_bytes(s.T, FLAGOUT, 1)[0]
    name = entry if isinstance(entry, str) else f"${entry:04X}"
    if p & 0x04:
        fails.append(f"{name}: I flag set after return (P=${p:02X}; the caller had it clear)")
    if rok != 0:
        fails.append(f"{name}: m_rok = {rok} after return (C1: must be 0)")
    if port != PORT_RUN:
        fails.append(f"{name}: $01 = ${port:02X} after return (want ${PORT_RUN:02X})")


def check_outputs(s: Session, g: dict, ref: dict, regs0: bytes) -> list[str]:
    from c64_test_harness import read_bytes
    top, io = expected_blocks(s, g, ref)
    fails = diff("RAM $E000-$FFFF", s.read_ram(0xE000, 0x2000), top, 0xE000)
    fails += diff("RAM under I/O $D000-$DFFF", s.read_ram(0xD000, 0x1000), io, 0xD000)
    colram = bytes(b & 0x0F for b in read_bytes(s.T, 0xD800, 1000))
    fails += diff("colour RAM", colram, ref["colram"], 0xD800)
    bgc = s.read_ram(s.lab["c2v_args"] + R.ARG_BGC, 1)[0]
    if bgc != ref["bgc"]:
        fails.append(f"bgc: got {bgc}, want {ref['bgc']}")
    fails += diff("guard $B800-$B8FF", s.read_ram(0xB800, 0x100), g["free_b"], 0xB800)
    fails += diff("VIC/CIA2 registers", read_regs(s.T), regs0)
    return fails


def hfill_ref(s: Session, ref: dict, col: list[int]) -> dict:
    """W12-V: c2v_hfill after c2v_hires, with c2v_v_col = col."""
    n = s.rows * 40
    return dict(ref, screen=bytes((col[k] << 4) | col[0] for k in ref["pens"])
                + bytes([col[0] * 0x11] * (1000 - n)),
                colram=ref["colram"][:n] + bytes(1000 - n), bgc=col[0])


def one_ref(s, case: dict, luts: dict, col: list[int]) -> dict:
    """W13-D: c2v_ones then c2v_ofin with c2v_v_col = col."""
    px = case.get("px", 13)
    ref = R.convert_one(window(case["hires"], px, s.rows * 8), case["pal"],
                        luts[case["lut"]], rows=s.rows)
    return dict(ref, screen=bytes((col[bg if fg == R.NO_FG else fg] << 4) | col[bg]
                                  for fg, bg in ref["cells"]), bgc=col[0])


def run_one(s, case: dict, luts: dict) -> list[str]:
    """W13-D: the 1:1 kernel and its matrix pass, cell by cell."""
    if "c2v_ones" not in s.lab:
        print(f"  {case['name']:20s} one   FAIL  (no c2v_ones in this PRG)")
        return ["no c2v_ones in this PRG (the 1:1 view is missing)"]
    px = case.get("px", 13)
    src = [a + px for a in case_src(case, "hires")]
    col = random.Random(case["name"] + "/one").sample(range(16), 16)
    ref = one_ref(s, case, luts, col)
    g = guard_fill(s, zlib.crc32(f"{case['name']}/one".encode()))
    ref["colram"] = g["col"]                        # not written by the 1:1 view
    regs0 = read_regs(s.T)
    s.write_ram(s.lab["c2v_args"], R.pack_args(src, case_lut(case), case["pal"]))
    s.write_ram(s.lab["c2v_args"] + 72, R.pack_skip([0] * 4))
    fails: list[str] = []
    call_checked(s, "c2v_init", fails)
    call_banked(s, "c2v_ones", fails)
    cnt = list(s.read_ram(s.lab["c2v_v_cnt"], 16))
    if cnt != ref["cnt"]:
        fails.append(f"c2v_v_cnt {cnt} != {ref['cnt']}")
    s.write_ram(s.lab["c2v_v_col"], bytes(col))
    call_banked(s, "c2v_ofin", fails)
    fails += check_outputs(s, g, ref, regs0)
    # pixel by pixel: every two-pen cell shows its pens' colours exactly
    bmp, scr = s.read_ram(s.bitmap, 8000), s.read_ram(s.screen, 1000)
    win = window(case["hires"], px, s.rows * 8)
    bad = 0
    for cell in range(1000):
        r, cx = divmod(cell, 40)
        pix = [[sum(1 << p for p in range(4) if win[p][y * R.ONE_ROW + cx] & (0x80 >> x))
                for x in range(8)] for y in range(r * 8, r * 8 + 8)]
        if len({v for row in pix for v in row}) > 2:
            continue                    # 3+ pens: best effort
        for y in range(8):
            b = bmp[r * 320 + cx * 8 + y]
            for x in range(8):
                shown = scr[cell] >> 4 if b & (0x80 >> x) else scr[cell] & 15
                bad += shown != col[pix[y][x]]
    if bad:
        fails.append(f"pixel-exact: {bad} pixel(s) of two-pen cells show the wrong colour")
    print(f"  {case['name']:20s} one   {'OK' if not fails else 'FAIL'}"
          f"  (px {px}, {len(set(ref['cells']))} distinct (fg, bg))")
    for f in fails:
        print(f"      {f}")
    return fails


def run_kernel(s: Session, kind: str, case: dict, luts: dict) -> list[str]:
    if kind == "one":                               # W13-D
        return run_one(s, case, luts)
    kk = "hires" if kind == "hfill" else kind       # W12-V: hfill = hires + c2v_hfill
    lut_addr, src = case_lut(case), case_src(case, kk)
    ref = reference(s, kk, case, luts)
    g = guard_fill(s, zlib.crc32(f"{case['name']}/{kind}".encode()))
    regs0 = read_regs(s.T)
    s.write_ram(s.lab["c2v_args"], R.pack_args(src, lut_addr, case["pal"]))
    fails: list[str] = []
    for entry in ("c2v_init", f"c2v_pal_{kk}", f"c2v_{kk}"):
        call_checked(s, entry, fails)
    if kind == "hfill":                 # pen colours: a fixed permutation per case
        col = random.Random(case["name"]).sample(range(16), 16)
        s.write_ram(s.lab["c2v_v_col"], bytes(col))
        call_checked(s, "c2v_hfill", fails)
        ref = hfill_ref(s, ref, col)
    fails += check_outputs(s, g, ref, regs0)
    bm = s.read_ram(s.bitmap, 1000)
    unchanged = bm == expected_blocks(s, g, {"bitmap": b"", "screen": b""})[0][
        s.bitmap - 0xE000:s.bitmap - 0xE000 + 1000]
    print(f"  {case['name']:20s} {kind:5s} {'OK' if not fails else 'FAIL'}"
          f"{'  (bitmap UNCHANGED: guard pattern intact)' if unchanged else ''}")
    for f in fails:
        print(f"      {f}")
    return fails


def nmi_tramp(entry: int, count: int, spin: int = 0) -> bytes:
    """Arm a CIA2 timer-A one-shot NMI *count* cycles out, jsr *entry*, then
    (W12-V) *spin* x ~1280 cycles of delay loop, rts: an NMI armed past the
    entry's end then fires before the call returns, never in a race with the
    monitor's stop."""
    return bytes([
        0xA9, 0x7F, 0x8D, 0x0D, 0xDD,           # lda #$7F / sta $DD0D  (mask all)
        0xAD, 0x0D, 0xDD,                       # lda $DD0D             (ack)
        0xA9, 0x00, 0x8D, 0x0E, 0xDD,           # stop TA
        0xA9, count & 0xFF, 0x8D, 0x04, 0xDD,   # TA latch lo
        0xA9, count >> 8, 0x8D, 0x05, 0xDD,     # TA latch hi
        0xA9, 0x81, 0x8D, 0x0D, 0xDD,           # enable TA -> NMI
        0xA9, 0x19, 0x8D, 0x0E, 0xDD,           # TA: force load, one-shot, start
        0x20, entry & 0xFF, entry >> 8,         # jsr entry
        *([0xA0, spin, 0xA2, 0x00, 0xCA, 0xD0, 0xFD, 0x88, 0xD0, 0xF8]
          if spin else []),                     # ldy #spin / ldx #0 / dex / bne / dey / bne
        0x60])


NMI_ACK = bytes([0xA9, 0x7F, 0x8D, 0x0D, 0xDD, 0xAD, 0x0D, 0xDD, 0x60])
NMI_COUNTS = (1500, 3000, 5000)     # phi2 cycles from arming to the NMI
# W12-V: c2v_hfill maps a page (256 cells) with I/O in, then stores it at
# $01 = $34 (~3.3-3.6k cycles a page, 4 pages in ~41k).  The leg sweeps the
# NMI from arming in HFILL_STEP steps (smaller than a store window, so every
# window gets one wherever the code before it moves: integration review,
# V + C) and requires the NMIs that land at $34 (taken by mach_nmi) to form
# HFILL_WINDOWS separate clusters; every other one is the KERNAL's.
HFILL_STEP = 2000
HFILL_COUNTS = tuple(range(HFILL_STEP, 46000, HFILL_STEP))
HFILL_WINDOWS = 4
HFILL_SPIN = 6                  # ~7.7k cycles after the fill: no NMI outlives the call
NMI_CONTROL = 100                   # fires in the LUT lookups ($01=$36): the
                                    # KERNAL handler must take it
# the reviewer's second NMI (scripts/nmi_latch.py): CIA2 timer B one-shot 200
# cycles, enabled, then ~1280 cycles of spin with $01=$37; rts
ARM_B = bytes([0xA9, 0x00, 0x8D, 0x0F, 0xDD, 0xA9, 200, 0x8D, 0x06, 0xDD,
               0xA9, 0, 0x8D, 0x07, 0xDD, 0xA9, 0x82, 0x8D, 0x0D, 0xDD,
               0xA9, 0x19, 0x8D, 0x0F, 0xDD,
               0xA2, 0x00, 0xCA, 0xD0, 0xFD, 0x60])


def run_nmi(s: Session, case: dict, luts: dict) -> list[str]:
    """NMI fired inside the $01=$34 screen fill (see the module docstring)."""
    from c64_test_harness import read_bytes, write_bytes
    fails: list[str] = []
    if not s.under_io:
        print("  nmi leg: skipped (screen matrix not under I/O: no $01=$34 fill)")
        return fails
    nmis_at = s.nmis_at
    lut = luts[case["lut"]]
    hfill_in: list[int] = []
    for kind in ("mc2", "hires", "hfill"):          # W12-V: c2v_hfill's fill too
        for count in (HFILL_COUNTS if kind == "hfill" else (NMI_CONTROL,) + NMI_COUNTS):
            kk = "hires" if kind == "hfill" else kind
            ref = reference(s, kk, case, luts)
            if kind == "hires":                     # W12-V: c2v_pal_hires runs last
                ref = dict(ref, colram=bytes([ref["bgc"]] * 1000))
            g = guard_fill(s, count)
            regs0 = read_regs(s.T)
            s.write_ram(s.lab["c2v_args"],
                        R.pack_args(case_src(case, kk), case_lut(case), case["pal"]))
            call_checked(s, "c2v_init", fails)
            call_checked(s, f"c2v_{kk}", fails)
            entry = f"c2v_pal_{kind}"
            if kind == "hfill":                     # W12-V: the pens' colours, then the fill
                col = [lut[c & 0xFFF] for c in case["pal"]] + [0] * 16
                s.write_ram(s.lab["c2v_v_col"], bytes(col[:16]))
                ref = hfill_ref(s, ref, col)
                entry = "c2v_hfill"
            n0 = s.read_ram(nmis_at, 1)[0]
            write_bytes(s.T, TRAMP, nmi_tramp(s.lab[entry], count,
                                              HFILL_SPIN if kind == "hfill" else 0))
            tag = f"nmi {kind} @{count}"
            try:
                call_checked(s, TRAMP, fails)
            except Exception as e:          # the trap: the call never returns
                fails.append(f"{tag}: call did not return ({type(e).__name__}): "
                             "the NMI took an unguarded RAM vector")
                print(f"  {tag:30s} FAIL (hung)")
                return fails
            icr = read_bytes(s.T, 0xDD0D, 1)[0]     # monitor read: no side effect
            taken = (s.read_ram(nmis_at, 1)[0] - n0) & 0xFF
            f = check_outputs(s, g, ref, regs0)
            if not f and count != NMI_CONTROL:
                # a later CIA2 NMI must still be delivered (to the KERNAL)
                write_bytes(s.T, TRAMP, ARM_B)
                s.call(TRAMP)
                icr_b = read_bytes(s.T, 0xDD0D, 1)[0]
                if icr_b & 0x82:
                    f.append(f"later TB NMI not delivered (ICR ${icr_b:02X}): "
                             "/NMI still latched low by the in-fill NMI")
            write_bytes(s.T, TRAMP, NMI_ACK)
            s.call(TRAMP)
            want = 0 if count == NMI_CONTROL else 1
            if kind == "hfill":                     # in a $34 window or not:
                if taken == 1:                      # exactly one NMI either way
                    hfill_in.append(count)
                want = 0 if taken == 0 else 1
            if taken != want:
                f.append(f"mach_nmi took {taken} NMI(s), want {want}"
                         + (" (control: must go to the KERNAL)" if not want else ""))
            if icr & 0x81:
                f.append(f"ICR ${icr:02X} after the call: NMI not acknowledged")
            what = ("control: taken and acked by the KERNAL before the fill"
                    if count == NMI_CONTROL else
                    "taken by the KERNAL (I/O in), ICR acked" if kind == "hfill" and not taken
                    else "taken by mach_nmi, ICR acked; later TB NMI delivered")
            print(f"  {tag:30s} {'OK' if not f else 'FAIL'}  ({what})")
            for x in f:
                print(f"      {x}")
            fails += [f"{tag}: {x}" for x in f]
    clusters = sum(1 for i, c in enumerate(hfill_in)
                   if i == 0 or c - hfill_in[i - 1] > HFILL_STEP)
    print(f"  nmi hfill: {len(hfill_in)} of {len(HFILL_COUNTS)} offsets landed at $34 "
          f"({clusters} store windows: {hfill_in})")
    if clusters < HFILL_WINDOWS:
        fails.append(f"nmi hfill: NMIs at $34 in {clusters} store window(s), want "
                     f"{HFILL_WINDOWS} (taken by mach_nmi at {hfill_in})")
    return fails


PERF_T_START = bytes([
    0xAD, 0x11, 0xD0, 0x48,             # lda $D011 / pha
    0x29, 0xEF, 0x8D, 0x11, 0xD0,       # and #$EF / sta $D011   (blank)
    0xAD, 0x12, 0xD0, 0xC9, 0xFA,       # @w: lda $D012 / cmp #$FA
    0xD0, 0xF9,                         # bne @w  (past the badline area)
    0xA9, 0x00, 0x8D, 0x0E, 0xDD, 0x8D, 0x0F, 0xDD,   # stop TA/TB
    0xA9, 0xFF, 0x8D, 0x04, 0xDD, 0x8D, 0x05, 0xDD,
    0x8D, 0x06, 0xDD, 0x8D, 0x07, 0xDD,
    0xA9, 0x51, 0x8D, 0x0F, 0xDD,       # TB: load, count TA underflows, start
    0xA9, 0x11, 0x8D, 0x0E, 0xDD,       # TA: load, continuous, start
])


def perf_tramp(target: int) -> bytes:
    """Blank, start the 32-bit phi2 counter, jsr target, stop, store, unblank."""
    t = bytearray(PERF_T_START)
    t += bytes([0x20, target & 0xFF, target >> 8])           # jsr target
    t += bytes([0xA9, 0x00, 0x8D, 0x0E, 0xDD])               # stop TA
    for i, reg in enumerate((0xDD04, 0xDD05, 0xDD06, 0xDD07)):
        o = PERF_OUT + i
        t += bytes([0xAD, reg & 0xFF, reg >> 8, 0x49, 0xFF,   # lda reg / eor #$FF
                    0x8D, o & 0xFF, o >> 8])                  # sta out
    t += bytes([0x68, 0x8D, 0x11, 0xD0, 0x60])               # pla / sta $D011 / rts
    return bytes(t)


def measure(s: Session, entry: str) -> int:
    from c64_test_harness import read_bytes, write_bytes
    write_bytes(s.T, TRAMP, perf_tramp(s.lab[entry]))
    s.call(TRAMP)
    return int.from_bytes(read_bytes(s.T, PERF_OUT, 4), "little")


def session_run(prg: str, perf: bool, fetch: bool = True) -> tuple[list[str], dict]:
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS, lookup_label
    from tests.run_vectors import prepare_target
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    lines = lookup_label(labels, "C2V_ROWS") * 8
    cases = make_cases(lines)
    lrng = random.Random(17)
    luts = {"real": R.build_lut(),
            "rand": bytes(lrng.randrange(16) for _ in range(R.LUT_LEN))}
    luts["bank"] = luts["rand"]
    assert len(set(luts["rand"])) == 16
    for case in cases:
        for a in case_src(case, "hires"):
            R.check_placement(a, lines * R.HIRES_ROW, "hires plane")
        for a in case_src(case, "mc2"):
            R.check_placement(a, lines * R.LORES_ROW, "mc2 plane")
    assert (BANK_HSRC[0] >> 16) != ((BANK_HSRC[0] + lines * 80 - 1) >> 16)
    assert (BANK_MSRC[0] >> 16) != ((BANK_MSRC[0] + 39) >> 16)
    assert (LUT_BANK >> 16) != ((LUT_BANK + 0x7FF + 1) >> 16)
    fails, perf_out = [], {}
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            s = Session(target, labels)
            print(f"  layout: {s.rows} rows ({lines} lines), bitmap ${s.bitmap:04X}, "
                  f"screen ${s.screen:04X}{' (RAM under I/O)' if s.under_io else ''}")
            s.stage(LUT_REAL, luts["real"])
            s.stage(LUT_RAND, luts["rand"])
            s.stage(LUT_BANK, luts["bank"])
            for case in cases:
                for p, a in enumerate(case_src(case, "hires")):
                    s.stage(a, case["hires"][p])
                for p, a in enumerate(case_src(case, "mc2")):
                    s.stage(a, case["mc2"][p])
                for kind in ("mc2", "hires", "hfill") + (("one",) if s.rows == 25 else ()):
                    fails += [f"{case['name']} {kind}: {f}"
                              for f in run_kernel(s, kind, case, luts)]
            if perf:
                for kind, plan in (("hires", 573_820), ("mc2", 327_620)):
                    cyc = measure(s, f"c2v_{kind}")
                    dma = fetch * lines * (4 * R.HIRES_ROW if kind == "hires"
                                           else 2 * R.LORES_ROW)
                    perf_out[kind] = {
                        "lines": lines, "cycles_frame": cyc, "dma_bytes": dma,
                        "s3plan_200": plan,
                        "vs_plan_pct": round(100 * (cyc * 200 / lines / plan - 1), 2),
                        "cycles_per_out_byte": round(cyc / (lines * 40), 2)}
                    print(f"  perf c2v_{kind}: {cyc} cycles / {lines}-line frame "
                          f"({perf_out[kind]['vs_plan_pct']:+.2f}% vs S3_PLAN "
                          f"{plan} per 200 lines); REU DMA per frame: {dma} B"
                          f"{'' if fetch else ' (nofetch variant: kernel only)'}")
                for kind in ("mc2", "hires"):
                    cyc = measure(s, f"c2v_pal_{kind}")
                    perf_out[f"pal_{kind}"] = {"cycles": cyc}
                    print(f"  perf c2v_pal_{kind}: {cyc} cycles")
            # last: a failing NMI leg can leave the machine hung
            fails += run_nmi(s, cases[-1], luts)   # the planes still staged
        finally:
            mgr.release(target)
    return fails, perf_out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--variant", choices=sorted(VARIANTS) + sorted(OTHER_VARIANTS))
    ap.add_argument("--expect-fail", action="store_true")
    ap.add_argument("--teeth", action="store_true")
    ap.add_argument("--perf", action="store_true")
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    prg = os.path.abspath(a.prg)
    if a.teeth:
        caught = {}
        for name, defines in VARIANTS.items():
            print(f"variant {name} ({' '.join(defines)}):")
            fails, _ = session_run(build_variant(prg, name, defines), False)
            caught[name] = len(fails)
        print("teeth: " + ", ".join(f"{k} {'CAUGHT' if v else 'MISSED'} ({v} failure(s))"
                                    for k, v in caught.items()))
        return 0 if all(caught.values()) else 1
    if a.variant:
        prg = build_variant(prg, a.variant,
                            {**VARIANTS, **OTHER_VARIANTS}[a.variant])
        print(f"variant {a.variant}: {prg}")
    fails, perf = session_run(prg, a.perf, fetch=a.variant != "nofetch")
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump({"prg": prg, "failures": fails, "perf": perf}, f, indent=1)
    print(f"run_c2v: {len(fails)} failure(s)")
    if a.expect_fail:
        print("expect-fail: " + ("FAILED as required" if fails else "PASSED (teeth missing!)"))
        return 0 if fails else 1
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
