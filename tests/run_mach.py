#!/usr/bin/env python3
"""S3.2 machine loop, time base and VERTB: guest and driver legs (VICE).

docs/S3_PLAN.md §1.5 and §4 "S3.2", docs/WAVE8_BRIEF.md C1-C3, contract
docs/CONTRACTS.md "Machine loop (S3.2)".  Every leg stages a small guest
program (host/fixturegen hunk, loaded through test_load_chunk), then runs it
through tests/mach/drv.s -- a driver assembled here at test time against the
runner's labels and loaded into the buf_case tail -- which calls mach_init /
mach_run and records the exit state.  A PRG without mach_run (the base
build: the red legs) runs the same guests through test_run_program with an
instruction budget instead.

Legs:
  stub    before any mach_init: VPOSR/VHPOSR read the S3.1 stub values
  poll    a guest polls VHPOSR for a change (red: times out), s = 4, 0, 8
  vertb   INTREQ bit 5 cleared by the guest is set again after a frame
  beam    VPOSR/VHPOSR sampled by move.l: every sample is a position of the
          host/chipset_ref.MachTime formula, monotonic within a frame, and
          the wraps match mach_frames; s = 0, 4, 8
  fps     guest frames per second (CIA1 TOD) = 985248 / 14188 / 2^s +-10%
          for s = 0, 2, 4; the guest's VERTB count = mach_frames
  race    st_halt = $81 on entry (a halt racing an event): the halt comes
          back ($01, reason kept) and ev_pend stays set
  exit    after mach_run: $01 restored ($37 and $36 callers), I = 1, and a
          following plain emu_run with IRQs enabled sees no event
  nmi     CIA2 timer-A NMIs during mach_run are acknowledged (all counted)
  nmi34   the same with $01 = $34 (all RAM): the handler maps I/O in
  stop    STOP #$2700 (exception mode) after a VERTB: HALT_STOP comes back
          unchanged (st_halt $01, reason 4)

    python3 -m tests.run_mach --prg build/X/runner.prg [--legs a,b]
    python3 -m tests.run_mach --prg ... --mutants [--only m1,m2]
    python3 -m tests.run_mach --prg ... --perf [--json-out F]

Exit 0 = every selected leg passed (--mutants: every mutant failed a leg).
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile

from host import chipset_ref, fixturegen, hunk, image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRV_ORG = 0xC400          # buf_case tail: written after the guest is staged
P, R = 0xCA00, 0xCB00     # drv.s parameter / result blocks (buf_result)
DMA_STUB, DMA_BUF = 0xC300, 0xC000
F_INIT, F_FOLLOW, F_PRESET, F_NMI, F_TOD = 0x01, 0x02, 0x04, 0x08, 0x10
F_SUPER = 0x20
HALT_STOP = 0x04
BASE = image.BASE_ADDR    # first hunk: absolute guest addresses work
SAMPLES = BASE + 0x10000  # beam samples (guest RAM)
HALT = 0xAFFF
VPOSR, VHPOSR, INTREQR, INTREQ = 0xDFF004, 0xDFF006, 0xDFF01E, 0xDFF09C
DRV_SYMS = ("emu_reset", "emu_run", "mem_read16", "mem_rec_dirty",
            "mach_init", "mach_run", "mach_vec", "mach_s", "mach_nmis")
VERBOSE = False


# --------------------------------------------------------------------------
# A tiny 68000 assembler for the guest fixtures (only what the legs use)
# --------------------------------------------------------------------------

class G:
    def __init__(self):
        self.w: list[int] = []
        self.labels: dict[str, int] = {}
        self.fix: list[tuple[int, str, str]] = []

    def __call__(self, *words: int) -> "G":
        self.w += [x & 0xFFFF for x in words]
        return self

    def label(self, name: str) -> "G":
        self.labels[name] = len(self.w)
        return self

    def abs(self, a: int) -> list[int]:
        return [a >> 16, a & 0xFFFF]

    def bcc(self, op: int, name: str) -> "G":        # Bcc.W
        self.fix.append((len(self.w) + 1, name, "w"))
        return self(op, 0)

    def dbra(self, d: int, name: str) -> "G":
        self.fix.append((len(self.w) + 1, name, "w"))
        return self(0x51C8 | d, 0)

    def words(self) -> list[int]:
        for at, name, _ in self.fix:
            self.w[at] = (2 * (self.labels[name] - at)) & 0xFFFF
        return self.w


BNE, BEQ, BRA = 0x6600, 0x6700, 0x6000


def mv_abs_d(g: G, size: str, a: int, d: int) -> G:     # move.<size> abs.l,Dd
    return g({"w": 0x3039, "l": 0x2039}[size] | d << 9, *g.abs(a))


def mv_imm_abs_w(g: G, v: int, a: int) -> G:            # move.w #v,abs.l
    return g(0x33FC, v, *g.abs(a))


def mv_imm_d_w(g: G, v: int, d: int) -> G:              # move.w #v,Dd
    return g(0x303C | d << 9, v)


def moveq(g: G, v: int, d: int) -> G:
    return g(0x7000 | d << 9 | (v & 0xFF))


def prog_poll(n: int = 20000) -> list[int]:
    """d0 = 1 when VHPOSR changes within n polls, else 0."""
    g = G()
    mv_abs_d(g, "w", VHPOSR, 1)
    mv_imm_d_w(g, n - 1, 3)
    g.label("lp")
    mv_abs_d(g, "w", VHPOSR, 2)
    g(0xB441)                                   # cmp.w d1,d2
    g.bcc(BNE, "hit")
    g.dbra(3, "lp")
    moveq(g, 0, 0)
    g(HALT)
    g.label("hit")
    moveq(g, 1, 0)
    g(HALT)
    return g.words()


def wait_vertb(g: G, tag: str, bound: int = 20000) -> G:
    """Clear INTREQ bit 5, poll INTREQR until it is set (at most *bound*
    polls: d5 := 1 and HALT on a timeout, which the callers' counts show)."""
    mv_imm_abs_w(g, 0x0020, INTREQ)
    mv_imm_d_w(g, bound - 1, 4)
    g.label(tag)
    mv_abs_d(g, "w", INTREQR, 1)
    g(0x0241, 0x0020)                           # andi.w #$20,d1
    g.bcc(BNE, tag + "_ok")
    g.dbra(4, tag)
    moveq(g, 1, 5)
    g(HALT)
    return g.label(tag + "_ok")


def prog_vertb() -> list[int]:
    """d2 = INTREQR right after clearing VERTB (bit 5 must be 0), then wait
    for one VERTB: d0 = 1, d5 = 0 on success."""
    g = G()
    moveq(g, 0, 5)
    moveq(g, 0, 0)
    mv_imm_abs_w(g, 0x0020, INTREQ)
    mv_abs_d(g, "w", INTREQR, 2)
    wait_vertb(g, "w")
    moveq(g, 1, 0)
    g(HALT)
    return g.words()


def prog_count(n: int) -> list[int]:
    """Count n VERTBs: d0 = count, d5 = 1 on a timeout."""
    g = G()
    moveq(g, 0, 0)
    moveq(g, 0, 5)
    mv_imm_d_w(g, n, 2)
    g.label("top")
    wait_vertb(g, "w")
    g(0x5240)                                   # addq.w #1,d0
    g(0xB042)                                   # cmp.w d2,d0
    g.bcc(BNE, "top")
    g(HALT)
    return g.words()


def prog_samples(n: int) -> list[int]:
    """n samples of move.l VPOSR,(a0)+ at SAMPLES."""
    g = G()
    g(0x41F9, *g.abs(SAMPLES))                  # lea SAMPLES,a0
    mv_imm_d_w(g, n - 1, 3)
    g.label("lp")
    g(0x20F9, *g.abs(VPOSR))                    # move.l VPOSR,(a0)+
    g.dbra(3, "lp")
    g(HALT)
    return g.words()


def prog_stub() -> list[int]:
    g = G()
    mv_abs_d(g, "w", VPOSR, 0)
    mv_abs_d(g, "w", VHPOSR, 1)
    g(HALT)
    return g.words()


def prog_busy(n: int) -> list[int]:
    """A register-only loop of n iterations (~n x 250 phi2), then HALT."""
    g = G()
    mv_imm_d_w(g, n - 1, 3)
    g.label("lp")
    g(0x5281)                                   # addq.l #1,d1
    g.dbra(3, "lp")
    g(HALT)
    return g.words()


def build_image(*progs: list[int]) -> tuple[list, list[int]]:
    """One hunk holding the programs back to back; their entry addresses."""
    words, entries = [], []
    for p in progs:
        entries.append(BASE + 2 * len(words))
        words += p
    b = fixturegen.HunkBuilder()
    b.add_code(words)
    chunks, man = image.build_image(hunk.parse(b.build()))
    assert man["entry"] == BASE, man["entry"]
    return chunks, entries


# --------------------------------------------------------------------------
# Driver build and the VICE rig
# --------------------------------------------------------------------------

def build_driver(labels_path: str, out_dir: str) -> bytes:
    from c64_test_harness import Labels
    from tests.harness_util import lookup_label
    labels = Labels.from_file(labels_path)
    defs = []
    for n in DRV_SYMS:
        defs += ["-D", f"{n}=${lookup_label(labels, n):04X}"]
    os.makedirs(out_dir, exist_ok=True)
    obj, out = os.path.join(out_dir, "drv.o"), os.path.join(out_dir, "drv.bin")
    cfg = os.path.join(out_dir, "drv.cfg")
    with open(cfg, "w") as f:
        f.write("MEMORY { M: file = %%O, start = $%04X, size = $%04X; }\n"
                "SEGMENTS { CODE: load = M, type = rw; }\n"
                % (DRV_ORG, 0x400))
    for cmd in (["ca65", "-g", *defs, "-o", obj, os.path.join(ROOT, "tests", "mach", "drv.s")],
                ["ld65", "-C", cfg, "-o", out, "-Ln", os.path.join(out_dir, "drv.lbl"), obj]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return open(out, "rb").read()


def drv_labels(out_dir: str) -> dict[str, int]:
    lab = {}
    for line in open(os.path.join(out_dir, "drv.lbl")):
        _, a, n = line.split()
        lab[n.lstrip(".")] = int(a, 16)
    return lab


class Rig:
    def __init__(self, target, labels, prg: str):
        from tests.harness_util import lookup_label
        from tests.run_program import Session
        self.tg, self.t = target, target.transport
        self.lab = lambda n: lookup_label(labels, n)
        self.sess = Session(target=target, labels=labels, timeout=300.0)
        try:
            self.lab("mach_run")
            self.mach = True
        except KeyError:
            self.mach = False
        if self.mach:
            d = os.path.join(os.path.dirname(os.path.abspath(prg)), "mach-drv")
            self.drv = build_driver(os.path.splitext(prg)[0] + ".lbl", d)
            self.dl = drv_labels(d)

    def call(self, addr: int, timeout: float = 300.0) -> None:
        from tests.harness_util import call_runner
        call_runner(self.tg, addr, timeout)

    def w(self, addr: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, addr, data)

    def r(self, addr: int, n: int) -> bytes:
        from c64_test_harness import read_bytes
        return read_bytes(self.t, addr, n)

    def stage(self, *progs: list[int]) -> list[int]:
        chunks, entries = build_image(*progs)
        self.sess.stage(chunks)
        if self.mach:
            self.w(DRV_ORG, self.drv)
        return entries

    def guest_read(self, a: int, n: int) -> bytes:
        """Guest RAM by raw REC fetch into DMA_BUF (then mem_rec_dirty)."""
        out = bytearray()
        rd = self.lab("mem_rec_dirty")
        while n:
            k = min(n, 256)
            c = bytearray()
            for reg, v in ((0x02, DMA_BUF & 0xFF), (0x03, DMA_BUF >> 8),
                           (0x04, a & 0xFF), (0x05, (a >> 8) & 0xFF),
                           (0x06, a >> 16), (0x07, k & 0xFF), (0x08, k >> 8),
                           (0x0A, 0), (0x01, 0x91)):
                c += bytes([0xA9, v, 0x8D, reg, 0xDF])
            c += bytes([0x20, rd & 0xFF, rd >> 8, 0x60])
            self.w(DMA_STUB, bytes(c))
            self.call(DMA_STUB)
            out += self.r(DMA_BUF, k)
            a, n = a + k, n - k
        return bytes(out)

    def run(self, pc: int, s: int = 4, flags: int = F_INIT, port: int = 0x37,
            pc2: int = 0, preset: tuple[int, int] = (0, 0),
            budget: int = 3_000_000) -> dict:
        """One guest run.  mach PRG: drv_run; base PRG: test_run_program."""
        if not self.mach:
            res = self.sess.run_program(pc, BASE + 0x8000, budget)
            return {"halt": res.halt, "reason": res.reason, "timeout": res.timeout,
                    "d": [res.regs[f"D{i}"] for i in range(4)], "frames": 0,
                    "base": True}
        blk = struct.pack("<IIBBBIBB", pc, BASE + 0x8000, s, flags, port, pc2,
                          *preset)
        self.w(P, blk)
        self.call(self.dl["drv_run"])
        r = self.r(R, 48)
        if r[0] != 0xFF:
            raise RuntimeError("drv_run did not complete")
        tod = lambda o: (r[o + 3] & 0x7F, r[o + 2], r[o + 1], r[o])
        return {"halt": r[1], "reason": r[2], "port": r[3], "p": r[4],
                "ev": r[5], "frames": struct.unpack_from("<I", r, 6)[0],
                "halt2": r[10], "ev2": r[11],
                "frames2": struct.unpack_from("<I", r, 12)[0],
                "tod0": tod(16), "tod1": tod(20), "nmi0": r[24], "nmi1": r[25],
                "d": list(struct.unpack_from("<4I", r, 32)), "timeout": 0,
                "base": False}


def bcd(x: int) -> int:
    return (x >> 4) * 10 + (x & 0xF)


def tod_tenths(t) -> int:
    h, m, s, t10 = t
    return ((bcd(h) * 60 + bcd(m)) * 60 + bcd(s)) * 10 + (t10 & 0xF)


def clean(res: dict, tag: str, fails: list[str]) -> bool:
    if res["timeout"] or res["halt"] != 1 or res["reason"] != 0:
        fails.append(f"{tag}: no clean HALT (halt=${res['halt']:02X} "
                     f"reason={res['reason']} timeout={res['timeout']})")
        return False
    return True


# --------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------

def leg_stub(rig: Rig) -> list[str]:
    f: list[str] = []
    (pc,) = rig.stage(prog_stub())
    res = rig.sess.run_program(pc, BASE + 0x8000, 100)
    d = [res.regs["D0"] & 0xFFFF, res.regs["D1"] & 0xFFFF]
    if d != [chipset_ref.VPOSR_STUB, 0]:
        f.append(f"stub: VPOSR/VHPOSR before mach_init = ${d[0]:04X}/${d[1]:04X}, "
                 "want $8000/$0000")
    return f


def leg_poll(rig: Rig) -> list[str]:
    f: list[str] = []
    (pc,) = rig.stage(prog_poll())
    for s in (4, 0, 8):
        res = rig.run(pc, s=s)
        if clean(res, f"poll s={s}", f) and res["d"][0] != 1:
            f.append(f"poll s={s}: VHPOSR never changed (d0={res['d'][0]})")
        if res["base"]:
            break
    return f


def leg_vertb(rig: Rig) -> list[str]:
    f: list[str] = []
    (pc,) = rig.stage(prog_vertb())
    res = rig.run(pc, s=2)
    if clean(res, "vertb", f):
        d0, d2 = res["d"][0], res["d"][2] & 0xFFFF
        if d0 != 1:
            f.append(f"vertb: INTREQ bit 5 never set after a frame (d0={d0})")
        if d2 & 0x20:
            f.append(f"vertb: INTREQR ${d2:04X} right after the clear")
        if not res["base"] and res["frames"] < 1:
            f.append("vertb: mach_frames = 0")
    return f


def check_samples(raw: bytes, s: int, frames: int, tag: str) -> list[str]:
    f: list[str] = []
    wraps, last = 0, None
    for i in range(0, len(raw), 4):
        vposr, vhposr = struct.unpack_from(">HH", raw, i)
        if vposr & 0xFFFE != chipset_ref.VPOSR_STUB:
            f.append(f"{tag}: sample {i // 4} VPOSR ${vposr:04X}: LOF/ID bits lost")
            continue
        v, h = (vposr & 1) << 8 | vhposr >> 8, vhposr & 0xFF
        if not chipset_ref.MachTime.is_beam(v, h):
            f.append(f"{tag}: sample {i // 4} V={v} H={h} is no beam position")
            continue
        cc = v * 227 + h
        if last is not None and cc < last:
            wraps += 1
        last = cc
    if not f and not wraps <= frames <= wraps + 1:
        f.append(f"{tag}: {wraps} wraps in the samples, mach_frames = {frames}")
    return f[:10]


def leg_beam(rig: Rig) -> list[str]:
    f: list[str] = []
    n = 400
    (pc,) = rig.stage(prog_samples(n))
    for s in (0, 4, 8):
        res = rig.run(pc, s=s)
        if not clean(res, f"beam s={s}", f):
            continue
        raw = rig.guest_read(SAMPLES, 4 * n)
        f += check_samples(raw, s, res["frames"], f"beam s={s}")
        vs = {struct.unpack_from(">H", raw, i + 2)[0] for i in range(0, 4 * n, 4)}
        if len(vs) < n // 4:
            f.append(f"beam s={s}: only {len(vs)} distinct VHPOSR values")
        if res["base"]:
            break
        if s == 0 and res["frames"] < 2:
            f.append(f"beam s=0: only {res['frames']} frame(s) over {n} samples")
        if VERBOSE:
            print(f"    beam s={s}: frames={res['frames']} distinct={len(vs)}")
    return f


FPS_N = {0: 420, 2: 104, 4: 26}


def leg_fps(rig: Rig) -> list[str]:
    f: list[str] = []
    for s, n in FPS_N.items():
        (pc,) = rig.stage(prog_count(n))
        res = rig.run(pc, s=s, flags=F_INIT | F_TOD)
        if not clean(res, f"fps s={s}", f):
            continue
        if res["base"]:
            f.append("fps: base build (no machine loop)")
            break
        if res["d"][0] & 0xFFFF != n:
            f.append(f"fps s={s}: guest counted {res['d'][0] & 0xFFFF} VERTBs, want {n}")
        if res["frames"] != n:
            f.append(f"fps s={s}: mach_frames = {res['frames']}, guest saw {n}")
        dt = (tod_tenths(res["tod1"]) - tod_tenths(res["tod0"])) / 10
        want = chipset_ref.MachTime(s).fps()
        got = res["frames"] / dt if dt > 0 else 0.0
        print(f"    fps s={s}: {res['frames']} frames in {dt:.1f} s (TOD) = "
              f"{got:.3f}/s, want {want:.3f} +-10%")
        if not 0.9 * want <= got <= 1.1 * want:
            f.append(f"fps s={s}: {got:.3f} frames/s, want {want:.3f} +-10%")
    return f


def leg_race(rig: Rig) -> list[str]:
    f: list[str] = []
    g = G()
    moveq(g, 0x55, 0)
    g(HALT)
    (pc,) = rig.stage(g.words())
    if not rig.mach:
        return ["race: base build (no mach_run)"]
    res = rig.run(pc, flags=F_INIT | F_PRESET, preset=(0x81, 0x01))
    if res["halt"] != 0x01:
        f.append(f"race: st_halt ${res['halt']:02X} on return, want $01 "
                 "(the halt, bit 7 cleared)")
    if res["reason"] != 0:
        f.append(f"race: st_reason {res['reason']} changed")
    if res["d"][0] != 0:
        f.append(f"race: the guest ran on past the halt (d0=${res['d'][0]:X})")
    if not res["ev"] & 1:
        f.append(f"race: ev_pend ${res['ev']:02X}: the pending event was lost")
    return f


def leg_exit(rig: Rig) -> list[str]:
    f: list[str] = []
    g = G()
    moveq(g, 0, 5)
    wait_vertb(g, "w")
    moveq(g, 1, 0)
    g(HALT)
    pc, pc2 = rig.stage(g.words(), prog_busy(4000))
    if not rig.mach:
        return ["exit: base build (no mach_run)"]
    for port in (0x37, 0x36):
        res = rig.run(pc, s=2, flags=F_INIT | F_FOLLOW, port=port, pc2=pc2)
        tag = f"exit ${port:02X}"
        if not clean(res, tag, f):
            continue
        if res["d"][0] != 1:
            f.append(f"{tag}: the guest saw no VERTB")
        if res["port"] != port:
            f.append(f"{tag}: $01 = ${res['port']:02X} after mach_run")
        if not res["p"] & 0x04:
            f.append(f"{tag}: I flag clear after mach_run (P=${res['p']:02X})")
        if res["halt2"] != 0x01:
            f.append(f"{tag}: the following emu_run returned st_halt "
                     f"${res['halt2']:02X}: an event reached it")
        if res["ev2"] != res["ev"] or res["frames2"] != res["frames"]:
            f.append(f"{tag}: ev_pend ${res['ev']:02X}->${res['ev2']:02X}, "
                     f"frames {res['frames']}->{res['frames2']} outside mach_run")
    return f


def leg_nmi(rig: Rig) -> list[str]:
    f: list[str] = []
    (pc,) = rig.stage(prog_count(3))
    if not rig.mach:
        return ["nmi: base build (no mach_run)"]
    # caller $01 = $35: an NMI before mach_run installs mach_nmi lands in
    # drv_null (RAM), not in the KERNAL's handler, which would mask CIA2
    res = rig.run(pc, s=4, flags=F_INIT | F_NMI | F_TOD, port=0x35)
    if clean(res, "nmi", f):
        dt = (tod_tenths(res["tod1"]) - tod_tenths(res["tod0"])) / 10
        n = (res["nmi1"] - res["nmi0"]) & 0xFF
        want = int(dt * chipset_ref.C64_PAL_PHI2 / 65536)
        print(f"    nmi: {n} NMIs in {dt:.1f} s (~{want} timer-A underflows)")
        if n < max(3, want // 2):
            f.append(f"nmi: {n} CIA2 NMIs taken in {dt:.1f} s, want ~{want}: "
                     "/NMI left latched")
        if res["d"][0] & 0xFFFF != 3:
            f.append(f"nmi: guest counted {res['d'][0] & 0xFFFF} VERTBs, want 3")
    return f


def leg_nmi34(rig: Rig) -> list[str]:
    if not rig.mach:
        return ["nmi34: base build (no mach_run)"]
    rig.stage(prog_stub())
    rig.w(P + 8, bytes([4]))
    rig.call(rig.dl["drv_nmi34"])
    r = rig.r(R, 32)
    n = (r[25] - r[24]) & 0xFF
    print(f"    nmi34: {n} NMIs under $01=$34 (~13 timer-A underflows)")
    if r[0] != 0xFF or n < 10:
        return [f"nmi34: {n} CIA2 NMIs taken under $01=$34, want ~13"]
    return []


def leg_stop(rig: Rig) -> list[str]:
    f: list[str] = []
    g = G()
    moveq(g, 0, 5)
    wait_vertb(g, "w")
    moveq(g, 1, 0)
    g(0x4E72, 0x2700)                           # stop #$2700
    moveq(g, 2, 0)                              # never reached
    g(HALT)
    (pc,) = rig.stage(g.words())
    if not rig.mach:
        return ["stop: base build (no mach_run)"]
    res = rig.run(pc, s=2, flags=F_INIT | F_SUPER)
    if res["halt"] != 1 or res["reason"] != HALT_STOP:
        f.append(f"stop: st_halt ${res['halt']:02X} reason {res['reason']}, "
                 f"want $01 / HALT_STOP ({HALT_STOP})")
    if res["d"][0] != 1:
        f.append(f"stop: d0={res['d'][0]} (1 = stopped after one VERTB)")
    return f


LEGS = {"stub": leg_stub, "poll": leg_poll, "vertb": leg_vertb,
        "beam": leg_beam, "fps": leg_fps, "race": leg_race, "exit": leg_exit,
        "nmi": leg_nmi, "nmi34": leg_nmi34, "stop": leg_stop}


def session(prg: str, fn) -> object:
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    # PAL (the harness defaults to NTSC): the guest is a PAL A500 and the
    # frames/s legs are PAL numbers (phi2 985248 Hz, 50 Hz TOD)
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False, ntsc=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        try:
            prepare_target(tg, prg)
            return fn(Rig(tg, labels, prg))
        finally:
            mgr.release(tg)


def run_all(prg: str, legs: list[str]) -> list[str]:
    def body(rig: Rig) -> list[str]:
        fails: list[str] = []
        for name in legs:
            try:
                f = LEGS[name](rig)
            except Exception as e:           # a hung/crashed leg is a failure
                f = [f"{name}: {type(e).__name__}: {e}"]
                print(f"[{name}] FAIL")
                for x in f:
                    print(f"    {x}")
                fails += f
                break                        # the machine state is unknown
            print(f"[{name}] {'PASS' if not f else 'FAIL'}")
            for x in f[:40]:
                print(f"    {x}")
            fails += f
        return fails
    return session(prg, body)


# --------------------------------------------------------------------------
# Teeth: source mutants of runtime/ops/mach.s (docs/WAVE8_BRIEF.md C3)
# --------------------------------------------------------------------------

MUTANTS = {
    # the IRQ vector is never written: no event ever arrives
    "noirq": ("runtime/ops/mach.s",
              [("        lda #<mach_irq\n        sta mach_virq\n"
                "        lda #>mach_irq\n        sta mach_virq+1\n", "")]),
    # st_halt compared with = $80 instead of & $7F: $81 comes back as $81
    "eq80": ("runtime/ops/mach.s",
             [("        lda st_halt\n        and #$7F\n        sta st_halt\n"
               "        bne exit\n",
               "        lda st_halt\n        cmp #$80\n        bne exit\n"
               "        lda #0\n        sta st_halt\n")]),
    # the exit leaves the CIA1 timer-A IRQ unmasked and unacknowledged
    "irqexit": ("runtime/ops/mach.s",
                [("exit:   lda #$7F                ; mask every CIA1 source\n"
                  "        sta CIA1_ICR\n"
                  "        lda CIA1_ICR            ; and acknowledge\n",
                  "exit:\n")]),
    # the caller's $01 is not restored
    "port": ("runtime/ops/mach.s",
             [("        lda mach_port\n        sta CPU_PORT\n        rts\n",
               "        rts\n")]),
    # the NMI handler does not acknowledge CIA2
    "nmiack": ("runtime/ops/mach.s",
               [("        lda CIA2_ICR            ; acknowledge: releases /NMI\n", "")]),
    # the NMI handler does not map I/O in (acknowledges RAM under $34)
    "nmimap": ("runtime/ops/mach.s",
               [("        and #$F8                ; NMI: I/O in\n"
                 "        ora #$05\n        sta CPU_PORT\n", "")]),
    # the frame length is shifted once too often (wrong mach_s shift)
    "shift": ("runtime/ops/mach.s",
              [("        ldx mach_sl             ; frame length = 14188 << s\n",
                "        ldx mach_sl             ; frame length = 14188 << s\n"
                "        inx                     ; MUTANT\n")]),
    # the beam uses s - 1 (positions run past the frame)
    "beamshift": ("runtime/ops/mach.s",
                  [("        ldx mach_sl             ; beam: t >> s\n",
                    "        ldx mach_sl             ; beam: t >> s\n"
                    "        dex                     ; MUTANT\n")]),
    # no sei after emu_run: the exit leaves I = 0 (and the RMW races)
    "nosei": ("runtime/ops/mach.s",
              [("        sei                     ; the RMW below must not race"
                " mach_irq's ora\n", "")]),
}


def build_mutant(name: str, out_dir: str) -> str:
    from tests.rec_build import make_sources
    out_dir = os.path.join(ROOT, out_dir)
    gen = os.path.join(out_dir, "gen", "dispatch.s")
    os.makedirs(os.path.dirname(gen), exist_ok=True)
    subprocess.run([sys.executable, "host/gen_dispatch.py", "-o", gen],
                   cwd=ROOT, check=True)
    rel, subs = MUTANTS[name]
    src = os.path.join(ROOT, rel)
    text = open(src).read()
    for a, b in subs:
        if text.count(a) != 1:
            raise RuntimeError(f"mutant {name}: anchor found {text.count(a)}x")
        text = text.replace(a, b)
    mpath = os.path.join(out_dir, f"mut_{name}_{os.path.basename(rel)}")
    with open(mpath, "w") as f:
        f.write(text)
    srcs = [mpath if s == src else s for s in make_sources()]
    prg = os.path.join(out_dir, "runner.prg")
    cmd = ["cl65", "-t", "none", "-C", "runtime/c64.cfg", "-o", prg,
           "-Ln", os.path.join(out_dir, "runner.lbl"),
           "-m", os.path.join(out_dir, "runner.map"),
           "--asm-include-dir", os.path.dirname(rel)]
    cmd += [os.path.relpath(s, ROOT) for s in srcs] + [gen]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"mutant build failed: {r.stdout}\n{r.stderr}")
    return prg


MUTANT_LEGS = {"noirq": ["vertb"], "eq80": ["race"], "irqexit": ["exit"],
               "port": ["exit"], "nmiack": ["nmi"], "nmimap": ["nmi34"],
               "shift": ["fps"], "beamshift": ["beam"], "nosei": ["exit"]}


def teeth(prg: str, only: list[str] | None) -> int:
    bdir = os.path.relpath(os.path.dirname(os.path.abspath(prg)), ROOT)
    bad = []
    for name in only or list(MUTANTS):
        mprg = build_mutant(name, os.path.join(bdir, "mach-mut", name))
        print(f"=== mutant {name} (legs {','.join(MUTANT_LEGS[name])})")
        try:
            fails = run_all(mprg, MUTANT_LEGS[name])
        except Exception as e:
            fails = [f"{type(e).__name__}: {e}"]
        verdict = "KILLED" if fails else "SURVIVED"
        print(f"=== mutant {name}: {verdict}")
        if not fails:
            bad.append(name)
    print(f"teeth: {len(MUTANTS if not only else only) - len(bad)} killed, "
          f"{len(bad)} survived {bad}")
    return 1 if bad else 0


# --------------------------------------------------------------------------
# Perf: cycles per beam read (docs/PERF.md "Machine loop (S3.2)")
# --------------------------------------------------------------------------

def perf(prg: str, json_out: str | None) -> int:
    def body(rig: Rig) -> dict:
        def timed(addr: int | None, s: int = 4, init: bool = False) -> int:
            blk = struct.pack("<IIBBB", 0, 0, s, F_INIT if init else 0, 0x37)
            rig.w(P, blk + b"\0" * 6
                  + (addr or 0).to_bytes(3, "little") + bytes([addr is not None]))
            rig.call(rig.dl["drv_time"])
            r = rig.r(R, 28)
            return 0xFFFF - struct.unpack_from("<H", r, 26)[0]

        rig.stage(prog_stub())
        out: dict = {}
        empty = min(timed(None) for _ in range(3))
        out["empty_window"] = empty
        out["vhposr_stub"] = [timed(VHPOSR) - empty for _ in range(3)]
        out["intreqr"] = [timed(INTREQR) - empty for _ in range(3)]
        for s in (0, 4, 8, 16):
            timed(None, s=s, init=True)
            out[f"vhposr_s{s}"] = [timed(VHPOSR, s=s) - empty for _ in range(5)]
            out[f"vposr_s{s}"] = [timed(VPOSR, s=s) - empty for _ in range(3)]
        return out

    res = session(prg, body)
    for k, v in res.items():
        print(f"  {k:14s} {v}")
    if json_out:
        with open(json_out, "w") as f:
            json.dump(res, f, indent=1)
    return 0


def main(argv=None) -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default=",".join(LEGS))
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--perf", action="store_true")
    ap.add_argument("--json-out")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    VERBOSE = a.verbose
    if a.mutants:
        return teeth(a.prg, a.only.split(",") if a.only else None)
    if a.perf:
        return perf(a.prg, a.json_out)
    fails = run_all(a.prg, a.legs.split(","))
    print(f"run_mach: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
