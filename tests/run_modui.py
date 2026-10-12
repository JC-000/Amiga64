#!/usr/bin/env python3
"""W15-U: the MOD player's UI kit and screen on VICE (docs/UI_READABLE.md).

The UI runs alone from a host-written mod_view (WAVE15_BRIEF I-2), built
from fixtures/mod/ui/ui.s and the test main fixtures/mod/ui/test/ui_test.s
(vasm, flat at $010000).  The reference is host/ui_preview.py: the same
screen drawn by the host model (player_static + player_frame + Ring).  A
second, independent reference is ``expect_fields``: the text each field
must show, written from I-1 alone, read back from the guest's planes by
``decode`` (rev15-ui, fix round 1).

Legs:
  kit   outside mach_run (test_run_program, instruction-counted).  First
        t_preset (DMACON SET AUD0-3EN, INTENA SET PORTS|AUD0: what the
        replay owns), then ui_init, then the song and stress sequences and
        the special views, one ui_frame per step (the view written to
        $000400 before each).  Every run's custom-register writes (the
        chip log) against the UI's allowlist; per call the 68k
        instruction count; the kicks (blt_n) against the cadence.  At each
        checkpoint (the view held for TRAIL_EVERY + 1 cycles): the guest's
        planes against the model byte for byte; decode(planes) against
        expect_fields(view); R1-R7 on the guest's planes with the guest's
        own BPLCON0, DIW, DDF, modulos and palette (cop_sh); the palette
        against the approved one.  Then DMACONR / INTENAR: the preset bits
        unchanged.  Budget (supervisor): average <= 400 on the song, worst
        <= 1500 on every call.
  live  under mach_run (s = --s, default 2): t_preset, then the test main
        runs a staged script, one ui_frame per VERTB, then idles until the
        last kick is converted.  The planes against the model and decode;
        the C64's bitmap, matrix, colour RAM and VIC registers against
        c2v_ref.disp_convert of them; disp_mode HIRES; R3 on the C64's
        real pixels; the kicks (blt_n) within the cadence bound; DMACON's
        AUD bits and INTENA unchanged.  R7: disp_view := 1 (the 1:1 view,
        px 20) and disp_refresh: the C64 against disp_convert_view.  PNGs
        of the C64's screen (read back from VICE) in the evidence dir.

    python3 -m tests.run_modui --prg build/runner.prg [--legs kit,live]
    python3 -m tests.run_modui --prg ... --mutants [--only pal,iface]
    python3 -m tests.run_modui --prg ... --variant B     # trail variant B
    python3 -m tests.run_modui --font-vs-rom              # host only -> fix1/font_vs_rom.txt

Not gated by these legs: custom-register and CIA READS (the chip log
records writes only).  host/tests/test_ui_static.py checks ui.s's
operands statically instead: no CIA, INTENA/INTREQ or Paula address.

Exit 0 = every selected leg passed (--mutants: every mutant failed the
check it is meant to fail, not merely a crash).  The command line and a
DONE marker (build/evidence/w15-ui/run_modui.DONE) go to the evidence dir.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import time

from host import c2v_ref as R
from host import ui_preview as U

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(ROOT, "build", "evidence", "w15-ui")
VBCC = os.environ.get("VBCC", os.path.expanduser("~/.local/opt/vbcc"))
UIDIR = os.path.join(ROOT, "fixtures", "mod", "ui")
MAIN = os.path.join(UIDIR, "test", "ui_test.s")
UI_S = os.path.join(UIDIR, "ui.s")

BASE = 0x010000
T_INIT, T_FRAME, T_EMPTY, T_LIVE, T_PRESET, T_REGS = (BASE + 4 * i for i in range(6))
SCRIPT, VIEW = 0x030000, 0x000400
AVG_MAX, WORST_MAX = 400, 1500          # supervisor ruling (time base)
DMA_STUB, DMA_BUF = 0xC100, 0xC000      # as tests/run_mach.py
PRESET_DMA, PRESET_INT = 0x000F, 0x0088  # t_preset: AUD0-3EN; PORTS|AUD0
UI_DMA_SET = {0x8240, 0x8380}            # ui_init's DMACON writes (SET only)

# Every custom register the UI may write (chip log ids): display set-up,
# palette 0-7, copper, its MOVEs (BPL1PT-BPL3PT), DMACON (values above),
# and the blitter registers of the clear and the kick.
UI_ALLOW = ({0x100, 0x102, 0x104, 0x108, 0x10A, 0x08E, 0x090, 0x092, 0x094,
             0x080, 0x082, 0x088, 0x096, 0x040, 0x042, 0x054, 0x056, 0x058, 0x066}
            | set(range(0x180, 0x190, 2)) | set(range(0x0E0, 0x0EC, 2)))

# name -> (vasm defines, ui.s source patches, the failure text that kills it)
MUTANTS = {
    "pal": (["UI_MUT_PAL"], [], "palette"),
    "plane": (["UI_MUT_PLANE"], [], "plane bytes differ"),
    "nokick": (["UI_MUT_NOKICK"], [], "live: bitmap"),
    "font1": (["UI_MUT_FONT1"], [], "plane bytes differ"),
    # rev15-ui's, fix round 1 (source patches)
    "iface": ([], [("_ui_init:\n        movem.l d2-d7/a2-a6,-(sp)\n        lea     CUSTOM,a6\n",
                    "_ui_init:\n        movem.l d2-d7/a2-a6,-(sp)\n        lea     CUSTOM,a6\n"
                    "        move.w  #$000f,DMACON(a6)\n        move.w  #$4000,$09a(a6)\n")],
              "interface"),
    "kickall": ([], [("        moveq   #0,d6\n        bsr     capture\n",
                      "        moveq   #0,d6\n        bsr     capture\n        bsr     kick\n")],
                "kick rate"),
    "tall": ([], [("move.w  #$f4c1,DIWSTOP(a6)", "move.w  #$2cc1,DIWSTOP(a6)")], "R6"),
    "nonul": ([], [(".nl     move.b  (a1)+,d0\n        beq.s   .nz",
                    ".nl     move.b  (a1)+,d0\n        nop")], "decode"),
    "bpm0": ([], [("        cmp.b   #' ',(a2)\n        beq.s   .t2",
                   "        cmp.b   #' ',(a2)\n        nop")], "decode"),
    "time": ([], [("        cmp.l   #300000,d0", "        cmp.l   #3276800,d0")], "decode"),
}
VARIANT_DEFS = {"A": [], "B": ["TRAIL_N=7", "TRAIL_PITCH=1", "TRAIL_DIM=1"]}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(out_dir: str, defines: list[str], patches=()) -> list[int]:
    """The test image as big-endian words (flat, org $010000).  Patches
    apply to a copy of ui.s in out_dir, found first on the include path."""
    os.makedirs(out_dir, exist_ok=True)
    local = os.path.join(out_dir, "ui.s")
    if patches:
        src = open(UI_S).read()
        for old, new in patches:
            if src.count(old) != 1:
                raise RuntimeError(f"mutant patch does not match once: {old[:40]!r}")
            src = src.replace(old, new)
        open(local, "w").write(src)
    elif os.path.exists(local):
        os.remove(local)
    if "UI_MUT_FONT1" in defines:
        thin = U._thin_font(U.load_font())
        open(os.path.join(out_dir, "font_thin.i"), "w").write(U.font_inc(thin))
    out = os.path.join(out_dir, "ui_test.bin")
    cmd = [os.path.join(VBCC, "bin", "vasmm68k_mot"), "-quiet", "-Fbin",
           "-I", out_dir, "-I", UIDIR, "-L", os.path.join(out_dir, "ui_test.lst")]
    cmd += [f"-D{d}" for d in defines] + ["-o", out, MAIN]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"vasm failed: {r.stdout}{r.stderr}")
    b = open(out, "rb").read()
    if len(b) & 1:
        b += b"\0"
    return list(struct.unpack(f">{len(b) // 2}H", b))


# --------------------------------------------------------------------------
# Views, the model and the independent decode
# --------------------------------------------------------------------------

def song(n: int, seed: int = 7, speed: int = 6, p_note: float = 0.4) -> list[dict]:
    """n ui_frame calls at 72 Hz of a 4-channel song at the given speed,
    125 BPM (50 ticks/s): rows, note triggers (vu := volume), vu decay (-2
    per tick), positions every 64 rows, the elapsed time per tick."""
    rnd = random.Random(seed)
    v = U.mod_view_sample()
    v.update(pos=0, row=0, pattern=0, time=0, flags=1)
    for c in v["ch"]:
        c.update(inst=0, vol=0, note=0xFF, fx=0, param=0, vu=0)
    out, ticks, tick_i = [], 0.0, 0
    for _ in range(n):
        ticks += 50 / 72
        while tick_i < int(ticks):
            tick_i += 1
            v["time"] = tick_i
            for c in v["ch"]:
                c["vu"] = max(0, c["vu"] - 2)
            if tick_i % speed == 0:                 # a new row
                v["row"] = (v["row"] + 1) & 63
                if v["row"] == 0:
                    v["pos"] = (v["pos"] + 1) % 12
                    v["pattern"] = (v["pattern"] * 5 + 3) % 32
                for c in v["ch"]:
                    if rnd.random() < p_note:
                        c.update(note=rnd.randrange(36), inst=rnd.randrange(1, 32),
                                 vol=rnd.randrange(16, 65),
                                 fx=rnd.choice([0, 0, 0xC, 0xA, 4, 0xE, 0xF]
                                               if p_note < 1 else [0xC, 0xA, 4, 0xE]),
                                 param=rnd.randrange(256))
                        c["vu"] = c["vol"]
                    else:
                        c.update(note=0xFF, fx=0, param=0)
        out.append(copy.deepcopy(v))
    return out


def extreme() -> dict:
    v = U.mod_view_sample()
    v.update(name=bytes([1, 0x7E, 0x60, 0x80, 0xFF]) + b"x" * 15, songlen=255, pos=255,
             pattern=255, row=255, speed=255, bpm=255, flags=255, time=0xFFFFFFFF)
    for c in v["ch"]:
        c.update(inst=255, vol=255, note=200, fx=255, param=255, vu=255)
    return v


def special() -> list[tuple[str, dict]]:
    """M1 (rev15-ui): bytes after a NUL in the name, BPM 5 and 105, elapsed
    between 300000 and 3276799 (and an effect above F)."""
    a = U.mod_view_sample()
    a.update(name=b"ab\0JUNK-AFTER-NUL", bpm=5, time=1_000_000, row=40)
    a["ch"][0].update(fx=0x1F, param=0x44)
    a["ch"][1].update(fx=16, param=0)
    b = U.mod_view_sample()
    b.update(name=b"\0" + b"Z" * 19, bpm=105, time=3_276_799, row=41)
    c = U.mod_view_sample()
    c.update(bpm=100, time=300_000, row=42)
    return [("nul-bpm5", a), ("bpm105", b), ("bpm100", c)]


def model(view: dict, ring: U.Ring, variant: str) -> U.Screen:
    s = U.Screen()
    U.player_static(s, variant)
    U.player_frame(s, view, ring, variant)
    return s


NOTE = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")


def _shown(b: int) -> str:
    if b < 32:
        return " "
    if 97 <= b <= 122:
        return chr(b - 32)
    if 32 <= b <= 95 or b in (0x80, 0x81, 0x82):
        return chr(b)
    return "?"


def expect_fields(v: dict) -> dict:
    """(col, row) -> the text a field must show, from I-1 and
    docs/UI_READABLE.md alone (not from ui_preview's drawing code)."""
    e = {}
    nm = bytes(v["name"][:20]).split(b"\0")[0]
    e[(6, 2)] = "".join(_shown(b) for b in nm).ljust(20)
    c2 = lambda x, hi=99: "%02d" % min(x, hi)              # noqa: E731
    e[(5, 4)], e[(8, 4)] = c2(v["pos"]), c2(v["songlen"])
    e[(16, 4)], e[(24, 4)] = c2(v["pattern"]), c2(v["row"])
    secs = min(v["time"] // 50, 99 * 60 + 59)
    e[(34, 4)], e[(37, 4)] = "%02d" % (secs // 60), "%02d" % (secs % 60)
    e[(5, 6)], e[(16, 6)] = c2(v["speed"]), "%3d" % v["bpm"]
    e[(24, 6)] = "CIA" if v["flags"] & 4 else "VBL"
    e[(29, 6)] = "LOOP" if v["flags"] & 2 else "    "
    e[(36, 0)] = chr(0x80) if v["flags"] & 1 else chr(0x81)
    for k, c in enumerate(v["ch"]):
        r = 10 + 2 * k
        e[(4, r)] = "--" if c["inst"] == 0 else c2(c["inst"])
        n = c["note"]
        e[(8, r)] = "---" if n > 35 else "%s%d" % (NOTE[n % 12], n // 12 + 1)
        e[(13, r)] = c2(c["vol"], 64)
        fx, p = c["fx"], c["param"]
        e[(17, r)] = "---" if (fx, p) == (0, 0) else \
            ("?" if fx > 15 else "%X" % fx) + "%02X" % p
    return e


def decode(planes: bytes, font: dict) -> list[list[str]]:
    """The guest planes back to glyphs, cell by cell (inverse cells by their
    complement; '\\0' where a cell is no glyph: bars, rules)."""
    rev = {}
    for code, rows in font.items():
        rev.setdefault(tuple(rows), code)
    grid = []
    for r in range(U.ROWS):
        line = []
        for cx in range(U.COLS):
            rows = []
            for y in range(8):
                o = (r * 8 + y) * U.STRIDE + 2 * cx
                w = 0
                for p in range(U.PLANES):
                    w |= planes[o + p * U.ROWB] << 8 | planes[o + p * U.ROWB + 1]
                rows.append(sum((w >> (15 - 2 * k) & 1) << (7 - k) for k in range(8)))
            t = tuple(rows)
            inv = tuple(x ^ 0xFF for x in rows)
            line.append(chr(rev[t]) if t in rev else chr(rev[inv]) if inv in rev else "\0")
        grid.append(line)
    return grid


def decode_check(planes: bytes, v: dict, tag: str) -> list[str]:
    g = decode(planes, U.load_font())
    f = []
    for (c, r), t in expect_fields(v).items():
        got = "".join(g[r][c:c + len(t)])
        if got != t:
            f.append(f"{tag}: decode ({c},{r}) shows {got!r}, I-1 says {t!r}")
    return f


# --------------------------------------------------------------------------
# The rig (tests/run_mach.Rig) plus guest writes, the log, the registers
# --------------------------------------------------------------------------

def guest_write(rig, a: int, data: bytes) -> None:
    """Guest RAM by raw REC stash from DMA_BUF (then mem_rec_dirty)."""
    rd = rig.lab("mem_rec_dirty")
    while data:
        k = min(len(data), 256)
        rig.w(DMA_BUF, data[:k])
        c = bytearray()
        for reg, v in ((0x02, DMA_BUF & 0xFF), (0x03, DMA_BUF >> 8),
                       (0x04, a & 0xFF), (0x05, (a >> 8) & 0xFF),
                       (0x06, a >> 16), (0x07, k & 0xFF), (0x08, k >> 8),
                       (0x0A, 0), (0x01, 0x90)):
            c += bytes([0xA9, v, 0x8D, reg, 0xDF])
        c += bytes([0x20, rd & 0xFF, rd >> 8, 0x60])
        rig.w(DMA_STUB, bytes(c))
        rig.call(DMA_STUB)
        a, data = a + k, data[k:]


def reset_display(rig) -> None:
    rig.call(rig.lab("chip_reset"))
    rig.call(rig.lab("cop_reset"))
    rig.call(rig.lab("disp_init"))


def u16(rig, name: str) -> int:
    return struct.unpack("<H", rig.r(rig.lab(name), 2))[0]


def run1(rig, pc: int, budget: int = 400_000):
    res = rig.sess.run_program(pc, BASE + 0x8000, budget)
    if res.halt != 1 or res.reason != 0 or res.timeout:
        raise RuntimeError(f"run ${pc:06X}: halt {res.halt} reason {res.reason} "
                           f"timeout {res.timeout} icount {res.icount}")
    return res


def regs(rig) -> tuple[int, int]:
    """(DMACONR, INTENAR) as the guest reads them."""
    res = run1(rig, T_REGS)
    return res.regs["D0"] & 0xFFFF, res.regs["D1"] & 0xFFFF


def log_since(rig, n0: int) -> tuple[list[tuple[int, int]], bool]:
    """The chip log entries written since chip_logn was n0 (id, value);
    False if the ring (64) overflowed."""
    n1 = u16(rig, "chip_logn")
    k = (n1 - n0) & 0xFFFF
    raw = rig.r(rig.lab("chip_log"), 256)
    out = []
    for i in range(min(k, 64)):
        j = (n0 + (k - min(k, 64)) + i) & 63
        out.append(struct.unpack_from("<HH", raw, 4 * j))
    return out, k <= 64


def iface_check(entries, complete: bool, tag: str) -> list[str]:
    """H1: the UI writes only its allowlist; DMACON only SET of its bits."""
    f = []
    if not complete:
        f.append(f"{tag}: interface: more than 64 custom writes in one run (log ring)")
    for rid, v in entries:
        if rid >= 0x0A00:
            f.append(f"{tag}: interface: CIA-{'AB'[(rid >> 8) - 0x0A]} write reg "
                     f"{rid & 0xFF:X} = ${v:04X}")
        elif rid not in UI_ALLOW:
            f.append(f"{tag}: interface: write to ${rid:03X} = ${v:04X} (not the UI's)")
        elif rid == 0x096 and v not in UI_DMA_SET:
            f.append(f"{tag}: interface: DMACON ${v:04X} (only SET of DMAEN/BPLEN/COPEN/BLTEN)")
    return f[:6]


def preset_check(rig, tag: str) -> list[str]:
    dma, ints = regs(rig)
    f = []
    if dma & PRESET_DMA != PRESET_DMA:
        f.append(f"{tag}: interface: DMACONR ${dma:04X}: the AUD0-3EN bits were cleared")
    if ints & 0x7FFF != PRESET_INT:
        f.append(f"{tag}: interface: INTENAR ${ints:04X}, the program set ${PRESET_INT:04X}")
    return f


def guest_state(rig) -> dict:
    """The display registers the guest left (cop.s's private copy, C1)."""
    return R.unpack_disp_st(rig.r(rig.lab("cop_sh"), R.DISP_ST_LEN))


def planes_of(rig) -> bytes:
    return rig.guest_read(U.UI_PLANES, U.H * U.STRIDE)


def compare_planes(got: bytes, s: U.Screen, tag: str) -> list[str]:
    want = bytes(s.mem)
    if got == want:
        return []
    bad = [i for i in range(len(want)) if got[i] != want[i]]
    i = bad[0]
    y, o = divmod(i, U.STRIDE)
    p, x = divmod(o, U.ROWB)
    return [f"{tag}: {len(bad)} plane bytes differ from the model; first: line {y} "
            f"plane {p} byte {x} (cell {x // 2},{y // 8}) got ${got[i]:02X} want ${want[i]:02X}"]


def guest_screen(got: bytes, s: U.Screen, st: dict) -> U.Screen:
    """The guest's planes and display registers with the model's pen map
    (the layout), for R1-R7 (M2: BPLCON0, DIW and palette from the guest)."""
    g = U.Screen()
    g.pen, g.kind = s.pen, s.kind
    g.mem = bytearray(got)
    g.palette = [c & 0xFFF for c in st["color"][:8]]
    g.st_override = st
    return g


def state_checks(st: dict, tag: str) -> list[str]:
    f = []
    if [c & 0xFFF for c in st["color"][:8]] != list(U.PALETTE):
        f.append(f"{tag}: palette {[hex(c) for c in st['color'][:8]]} is not the "
                 f"approved one")
    if st["bplpt"][:3] != [U.UI_PLANES + p * U.ROWB for p in range(3)]:
        f.append(f"{tag}: BPLxPT {[hex(a) for a in st['bplpt'][:3]]}")
    return f


# --------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------

def leg_kit(rig, words, variant: str, n: int, out: dict) -> list[str]:
    f: list[str] = []
    (pc,) = rig.stage(words)
    assert pc == BASE
    reset_display(rig)
    run1(rig, T_PRESET)
    n0 = u16(rig, "chip_logn")
    out["init_icount"] = run1(rig, T_INIT, 2_000_000).icount
    f += iface_check(*log_since(rig, n0), "ui_init")
    guest_write(rig, VIEW, U.pack_mod_view(U.mod_view_sample()))
    base = run1(rig, T_EMPTY).icount - 1        # the call frame, t_rts excluded
    ring = U.Ring()
    counts: list[int] = []
    last = None
    bad_iface: list[str] = []

    def call(v: dict) -> None:
        nonlocal last
        b = U.pack_mod_view(v)
        if b != last:
            guest_write(rig, VIEW, b)
            last = b
        n1 = u16(rig, "chip_logn")
        counts.append(run1(rig, T_FRAME).icount - base)
        if not bad_iface:
            bad_iface.extend(iface_check(*log_since(rig, n1), f"ui_frame call {len(counts)}"))
        ring.capture(v)

    def checkpoint(v: dict, tag: str) -> None:
        # settled: a whole trail refresh lies inside the hold wherever the
        # cycle stands ((TRAIL_EVERY + 1) cycles)
        for _ in range((U.TRAIL_EVERY + 1) * U.CADENCE):
            call(v)
        got = planes_of(rig)
        s = model(v, ring, variant)
        st = guest_state(rig)
        e = compare_planes(got, s, tag)
        e += decode_check(got, v, tag)
        e += state_checks(st, tag)
        e += [f"{tag}: {x}" for x in U.check_all(guest_screen(got, s, st))]
        f.extend(e)
        log(f"  kit {tag}: {'OK' if not e else 'FAIL'}")

    seq = song(n)
    checkpoint(seq[0], "first")
    t0, k0 = len(counts), u16(rig, "blt_n")
    for v in seq:
        call(v)
    run_counts = counts[t0:]
    kicks = (u16(rig, "blt_n") - k0) & 0xFFFF
    bound = len(seq) // U.CADENCE + 1
    out["song_kicks"] = kicks
    if kicks > bound:
        f.append(f"kit: kick rate: {kicks} kicks in {len(seq)} calls, cadence bound {bound}")
    checkpoint(seq[-1], "song")
    stress = song(n // 2, seed=3, speed=1, p_note=1.0)
    t1 = len(counts)
    for v in stress:
        call(v)
    stress_counts = counts[t1:]
    checkpoint(stress[-1], "stress")
    for name, v in special():
        checkpoint(v, name)
    checkpoint(extreme(), "extreme")
    f += bad_iface
    f += preset_check(rig, "kit")
    out["calls"] = len(counts)
    out["song_avg"] = sum(run_counts) / len(run_counts)
    out["song_worst"] = max(run_counts)
    out["stress_avg"] = sum(stress_counts) / len(stress_counts)
    out["stress_worst"] = max(stress_counts)
    out["all_worst"] = max(counts)
    log(f"  kit: ui_init {out['init_icount']} instr; song ({len(run_counts)} calls): "
        f"avg {out['song_avg']:.0f}, worst {out['song_worst']}; stress ({len(stress_counts)} "
        f"calls, speed 1, every channel every row): avg {out['stress_avg']:.0f}, worst "
        f"{out['stress_worst']}; worst of all {len(counts)} calls {out['all_worst']}; "
        f"kicks {kicks} in {len(seq)} song calls")
    if out["song_avg"] > AVG_MAX:
        f.append(f"kit: average {out['song_avg']:.0f} > {AVG_MAX} instructions per call")
    if out["all_worst"] > WORST_MAX:
        f.append(f"kit: worst {out['all_worst']} > {WORST_MAX} instructions per call")
    return f


def c64_out(rig) -> dict:
    from c64_test_harness import read_bytes
    from tests import run_c2v as C
    cs = C.Session(rig.tg, rig.labels)
    vic = {a: read_bytes(rig.t, a, 1)[0] for a in (0xD011, 0xD016, 0xD018, 0xD020, 0xD021)}
    return {"bitmap": cs.read_ram(cs.bitmap, 8000), "screen": cs.read_ram(cs.screen, 1000),
            "colram": bytes(b & 0x0F for b in read_bytes(rig.t, 0xD800, 1000)),
            "vic": vic, "cs": cs}


def compare_c64(got: dict, want: dict, tag: str) -> list[str]:
    from tests import run_c2v as C
    from tests.run_pf import VIC_MASK
    cs = got["cs"]
    f = [f"{tag}: {x}" for x in C.diff("bitmap", got["bitmap"], want["bitmap"], cs.bitmap)]
    f += [f"{tag}: {x}" for x in C.diff("matrix", got["screen"], want["screen"], cs.screen)]
    if want.get("colram") is not None:
        f += [f"{tag}: {x}" for x in C.diff("colour RAM", got["colram"], want["colram"], 0xD800)]
    for a, v in want["vic"].items():
        if got["vic"][a] & VIC_MASK[a] != v & VIC_MASK[a]:
            f.append(f"{tag}: VIC ${a:04X} = ${got['vic'][a]:02X}, want ${v:02X}")
    return f


def leg_live(rig, words, variant: str, s: int, n: int, out: dict,
             png_tag: str | None) -> list[str]:
    from tests.run_mach import F_INIT, clean
    f: list[str] = []
    (pc,) = rig.stage(words)
    assert pc == BASE
    reset_display(rig)
    run1(rig, T_PRESET)
    seq = song(n, seed=11)
    idle = (U.TRAIL_EVERY + 3) * U.CADENCE + rig.lab("DISP_N") * 2 + 40
    blob = struct.pack(">II", len(seq), idle)
    for v in seq:
        blob += struct.pack(">HH", 1, 0) + U.pack_mod_view(v)
    guest_write(rig, SCRIPT, blob)
    k0 = u16(rig, "blt_n")
    res = rig.run(T_LIVE, s=s, flags=F_INIT)
    if not clean(res, f"live s={s}", f):
        return f
    calls = len(seq) + idle
    kicks = (u16(rig, "blt_n") - k0) & 0xFFFF
    bound = calls // U.CADENCE + 3                 # + the clear and ui_init's kick
    ring = U.Ring()
    for v in seq:
        ring.capture(v)
    sm = model(seq[-1], ring, variant)
    got = planes_of(rig)
    f += compare_planes(got, sm, "live planes")
    f += decode_check(got, seq[-1], "live")
    f += state_checks(guest_state(rig), "live")
    nconv = u16(rig, "disp_nconv")
    mode, why = rig.r(rig.lab("disp_mode"), 1)[0], rig.r(rig.lab("disp_why"), 1)[0]
    out.update(live_frames=res["frames"], live_nconv=nconv, live_kicks=kicks,
               live_calls=calls)
    log(f"  live s={s}: frames {res['frames']} calls {calls} kicks {kicks} (bound {bound}) "
        f"conversions {nconv} mode {mode} why {why}")
    if kicks > bound:
        f.append(f"live: kick rate: {kicks} kicks in {calls} calls, cadence bound {bound}")
    if mode != R.MODE_HIRES or why:
        f.append(f"live: disp_mode {mode} why {why} (want HIRES)")
    want = R.disp_convert(sm.disp_st(), sm.read, U.lut())
    g = c64_out(rig)
    f += compare_c64(g, want, "live")
    actual = {**{k: g[k] for k in ("bitmap", "screen", "colram", "vic")},
              "decide": {"mode": R.MODE_HIRES}, "vic_pens": want["vic_pens"]}
    img = U.vic_pixels(actual)
    d = sm.design()
    bad = sum(1 for y in range(U.H) for x in range(U.W)
              if img[y][x] != want["vic_pens"][d[y][2 * x]])
    if bad:
        f.append(f"live: R3: the C64 shows {bad} pixels unlike the design")
    if png_tag:
        U.vic_image(actual).save(os.path.join(EVID, "u1", f"live_{png_tag}.png"))
    f += preset_check(rig, "live")
    # R7 on VICE: the 1:1 view (F1's window, px 20, the top), converted now
    rig.w(rig.lab("disp_px"), bytes([R.PX_CENTRE, 0]))
    rig.w(rig.lab("disp_view"), b"\x01")
    rig.call(rig.lab("disp_refresh"))
    want1 = R.disp_convert_view(sm.disp_st(), sm.read, U.lut(), view=1, px=R.PX_CENTRE)
    g1 = c64_out(rig)
    want1 = {**want1, "colram": None}
    f += compare_c64(g1, want1, "live R7 (1:1)")
    if png_tag:
        a1 = {**{k: g1[k] for k in ("bitmap", "screen", "colram", "vic")},
              "decide": {"mode": R.MODE_HIRES}}
        U.vic_image(a1).save(os.path.join(EVID, "u1", f"live_{png_tag}_1to1.png"))
    rig.w(rig.lab("disp_view"), b"\x00")
    return f


# --------------------------------------------------------------------------
# --font-vs-rom (B1, evidence only: the ROM is read from the local VICE
# install and never stored or copied)
# --------------------------------------------------------------------------

ROM_CANDIDATES = [
    os.path.expanduser("~/.local/opt/vice-3.10-ethernet/share/vice/C64/chargen-901225-01.bin"),
    os.path.expanduser("~/opt/vice-eth/vice-3.10-sdl2-eth/share/vice/C64/chargen-901225-01.bin"),
    "/opt/homebrew/share/vice/C64/chargen-901225-01.bin",
]
ROM_MIN = 8
# space, and shapes the cell forces (justified in PROGRESS.md)
ROM_EXEMPT = {ord(" "): "empty", ord("-"): "one short bar", ord("."): "one dot"}


def _shift(rows, dx, dy):
    out = [0] * 8
    for y in range(8):
        sy = y - dy
        if 0 <= sy < 8:
            v = rows[sy]
            out[y] = ((v >> dx) if dx >= 0 else (v << -dx)) & 0xFF
    return out


def font_vs_rom(path: str | None = None) -> tuple[int, str]:
    path = path or next((p for p in ROM_CANDIDATES if os.path.exists(p)), None)
    if not path or not os.path.exists(path):
        tried = [path] if path else ROM_CANDIDATES
        return 0, ("font-vs-rom: SKIPPED - NO C64 CHARACTER ROM FOUND; F4 NOT CHECKED. "
                   "Tried: " + ", ".join(tried))
    rom = open(path, "rb").read()
    font = U.load_font()
    lines, bad = [f"font8.txt vs {os.path.basename(path)} (upper-case set), "
                  f"min over +-1 px shifts; need >= {ROM_MIN}"], []
    for c in range(32, 96):
        sc = c - 64 if c >= 64 else c
        rg = list(rom[8 * sc:8 * sc + 8])
        d = min(sum(bin(a ^ b).count("1") for a, b in zip(font[c], _shift(rg, dx, dy)))
                for dx in (-1, 0, 1) for dy in (-1, 0, 1))
        tag = ""
        if c in ROM_EXEMPT:
            tag = f"  exempt: {ROM_EXEMPT[c]}"
        elif d < ROM_MIN:
            tag = "  FAIL"
            bad.append(chr(c))
        lines.append(f"{chr(c)!r:5} {d:3d}{tag}")
    lines.append(f"result: {'PASS' if not bad else 'FAIL ' + ''.join(bad)}")
    return (1 if bad else 0), "\n".join(lines)


# --------------------------------------------------------------------------

def session(prg: str, fn):
    from tests import run_mach as M
    return M.session(prg, fn)


def run(prg: str, legs: list[str], defines: list[str], patches, variant: str, s: int,
        n_kit: int, n_live: int, tag: str, out: dict) -> list[str]:
    words = build(os.path.join(ROOT, "build", "w15ui", tag),
                  defines + VARIANT_DEFS[variant], patches)
    os.makedirs(os.path.join(EVID, "u1"), exist_ok=True)

    def body(rig):
        from c64_test_harness import Labels
        rig.labels = Labels.from_file(os.path.splitext(os.path.abspath(prg))[0] + ".lbl")
        f: list[str] = []
        for leg in legs:
            t0 = time.monotonic()
            try:
                if leg == "kit":
                    fl = leg_kit(rig, words, variant, n_kit, out)
                else:
                    fl = leg_live(rig, words, variant, s, n_live, out, tag)
            except Exception as e:                       # noqa: BLE001
                fl = [f"{leg}: CRASH {type(e).__name__}: {e}"]
            log(f"leg {leg} [{tag}]: {'PASS' if not fl else 'FAIL'} "
                f"({time.monotonic() - t0:.0f} s)")
            for x in fl:
                print(f"    {x}", flush=True)
            f += fl
        return f
    return session(prg, body)


def _sigterm(signum, frame):
    raise SystemExit(f"signal {signum}")


def main(argv=None) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _sigterm)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg")
    ap.add_argument("--legs", default="kit,live")
    ap.add_argument("--variant", default="A", choices=sorted(VARIANT_DEFS))
    ap.add_argument("-D", dest="defs", action="append", default=[],
                    help="extra vasm define for the kit (e.g. TRAIL_EVERY=1)")
    ap.add_argument("--s", type=int, default=2)
    ap.add_argument("--kit-calls", type=int, default=240)
    ap.add_argument("--live-calls", type=int, default=60)
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--font-vs-rom", action="store_true")
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    os.makedirs(EVID, exist_ok=True)
    with open(os.path.join(EVID, "commands.txt"), "a") as fh:     # L5
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  python3 -m tests.run_modui "
                 f"{' '.join(argv if argv is not None else sys.argv[1:])}\n")
    if a.font_vs_rom:
        rc, text = font_vs_rom()
        print(text)
        os.makedirs(os.path.join(EVID, "fix1"), exist_ok=True)
        with open(os.path.join(EVID, "fix1", "font_vs_rom.txt"), "w") as fh:
            fh.write(text + "\n")
        return rc
    if not a.prg:
        ap.error("--prg is required")
    done = os.path.join(EVID, "run_modui.DONE")
    if os.path.exists(done):
        os.remove(done)
    legs = a.legs.split(",")
    result: dict = {"variant": a.variant, "s": a.s, "defs": a.defs}
    if a.mutants:
        names = a.only.split(",") if a.only else list(MUTANTS)
        survived = []
        for m in names:
            defs, patches, expect = MUTANTS[m]
            o: dict = {}
            fl = run(a.prg, legs, defs, patches, a.variant, a.s, 36, 60, f"mut-{m}", o)
            killed = any(expect in x and "CRASH" not in x for x in fl)
            log(f"mutant {m}: {'KILLED' if killed else 'SURVIVED'} "
                f"(needs a failure with {expect!r}; {len(fl)} failure line(s))")
            result[f"mut_{m}"] = {"killed": killed, "expect": expect, "fails": fl[:8]}
            if not killed:
                survived.append(m)
        rc = 1 if survived else 0
    else:
        fl = run(a.prg, legs, a.defs, (), a.variant, a.s, a.kit_calls, a.live_calls,
                 f"green-{a.variant}" + "".join(f"-{d}" for d in a.defs), result)
        result["fails"] = fl
        rc = 1 if fl else 0
    result["rc"] = rc
    if a.json_out:
        with open(a.json_out, "w") as fh:
            json.dump(result, fh, indent=1, default=str)
    with open(done, "w") as fh:
        fh.write(f"rc={rc} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    log(f"run_modui: {'PASS' if rc == 0 else 'FAIL'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
