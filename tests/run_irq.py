#!/usr/bin/env python3
"""S3.3 event seam and interrupt delivery: guest and driver legs (VICE).

docs/WAVE9_BRIEF.md C1/C2, docs/S3_PLAN.md §1.5 and §4 "S3.3", contracts
docs/CONTRACTS.md "Event seam (S3.3/S3.4)" and "Interrupt delivery (S3.3)".
Every leg stages a small guest program (as tests/run_mach.py does), then
runs it through tests/irq/drv.s -- a driver assembled here at test time
against the runner's labels -- whose callbacks are a fake event source for
the seam (slots 1-7, the slot-0 VERTB hook, a RESET-instruction hook that
arms from inside emu_run).  A PRG without the seam (the base build) fails
the driver legs as red; the guest-only legs run there through
test_run_program.

Legs (phase A, the seam):
  setclr   INTENA/INTREQ SET/CLR into INTENAR/INTREQR (guest only, against
           host/chipset_ref.Paula), byte writes on both halves
  etime    mach_etime = low 32 bits of (chi:C) >> s, C wraps counted
           (stopped counter at chosen values), s = 0, 4, 9, 16
  etlive   live samples: the same formula, strictly increasing
  arm      slots armed before the run: each fires once, never early, at
           most SLACK late; a past deadline fires at the entry pass; a far
           one (> frame) fires on time; disarm before the run and from a
           callback; the log is in time order
  periodic a callback re-arming from its own deadline: every firing on
           time, no drift; the slot-0 hook runs once per VERTB; s = 4, 0
  trap     RESET arms slot 7 from inside emu_run (trap context): each
           firing on time (the arm kicks a pass that reprograms the
           one-shot)
  race     s = 0: thousands of trap arms racing mach_irq and the passes:
           fires + replaced + pending = arms, never early
  post     mach_post from callbacks (bit 3, bit 13) seen in INTREQR by the
           guest; mach_hold re-raises bit 3 after a guest CLR until the
           holder drops it
  out      mach_post/mach_arm outside mach_run do not touch st_halt/ev_pend

    python3 -m tests.run_irq --prg build/X/runner.prg [--legs a,b]
    python3 -m tests.run_irq --prg ... --mutants [--only m1,m2]

Exit 0 = every selected leg passed (--mutants: every mutant failed a leg).
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys

from host import chipset_ref
from tests import run_mach
from tests.run_mach import (BASE, HALT, BNE, BEQ, G, build_image, moveq,
                            mv_abs_d, mv_imm_abs_w, mv_imm_d_w)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRV_ORG = 0xC200
P, R, LOG = 0xCA00, 0xCB00, 0xCC00
F_INIT, F_SUPER, F_HOOK, F_RST, F_IEN = 0x01, 0x02, 0x04, 0x08, 0x10
F_PRESET = 0x20
INTENAR, INTREQR = 0xDFF01C, 0xDFF01E
INTENA, INTREQ = 0xDFF09A, 0xDFF09C
RESET = 0x4E70
SLACK_PHI2 = 4096           # a pass + IRQ entry + the instruction in flight
# Wave 13 (supervisor): leg lag only.  Its pass now also runs the W13 frame
# hooks (inp_frame <= 356 cycles, disp_vw ~25), which can push the client
# behind one more busy-loop guest instruction (~760 phi2 at s = 0): on the
# merged I+D+W build the max lateness is bimodal 3485-3548 / 4248-4307
# (base 2332-2754, W13-I alone 2921-3485).  One instruction of headroom.
LAG_SLACK_PHI2 = SLACK_PHI2 + 1024
M32 = 0xFFFFFFFF
DRV_SYMS = ("emu_reset", "emu_run", "emu_rst_hook", "mach_init", "mach_run",
            "mach_s", "mach_nmi", "mach_now", "mach_chi", "mach_etime",
            "mach_et", "mach_at", "mach_arm", "mach_disarm", "mach_post",
            "mach_cbv", "mach_hold", "mach_am", "chip_shd")
# Phase-B symbols a phase-A (seam only) PRG lacks: the driver then reads a
# zero-filled scratch word instead (the red legs need the driver to build)
DRV_OPT = ("mach_nirq", "mach_stp")
DRV_DUMMY = 0xCFF0
VERBOSE = False


# --------------------------------------------------------------------------
# Guest programs
# --------------------------------------------------------------------------

def prog_busy(n: int) -> list[int]:
    g = G()
    mv_imm_d_w(g, n - 1, 3)
    g.label("lp")
    g(0x5281)                                   # addq.l #1,d1
    g.dbra(3, "lp")
    g(HALT)
    return g.words()


def busy(g: G, n: int, tag: str) -> G:
    mv_imm_d_w(g, n - 1, 3)
    g.label(tag)
    g(0x5281)
    return g.dbra(3, tag)


def prog_trap(k: int, gap: int) -> list[int]:
    """k x {RESET; busy(gap)}, then HALT."""
    g = G()
    mv_imm_d_w(g, k - 1, 4)
    g.label("top")
    g(RESET)
    busy(g, gap, "b")
    g.dbra(4, "top")
    g(HALT)
    return g.words()


def wait_bit(g: G, bit: int, tag: str, bound: int = 30000) -> G:
    """Poll INTREQR until *bit* is set (d5 := 1 and HALT on a timeout)."""
    mv_imm_d_w(g, bound - 1, 4)
    g.label(tag)
    mv_abs_d(g, "w", INTREQR, 6)
    g(0x0246, 1 << bit)                         # andi.w #bit,d6
    g.bcc(BNE, tag + "_ok")
    g.dbra(4, tag)
    moveq(g, 1, 5)
    g(HALT)
    return g.label(tag + "_ok")


def prog_post() -> list[int]:
    g = G()
    moveq(g, 0, 5)
    moveq(g, 0, 0)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)             # no request left by a prior leg
    wait_bit(g, 3, "w3")
    mv_abs_d(g, "w", INTREQR, 1)                # bit 3 posted
    mv_imm_abs_w(g, 0x0008, INTREQ)             # CLR bit 3: held
    mv_abs_d(g, "w", INTREQR, 2)
    wait_bit(g, 13, "w13")
    moveq(g, 1, 0)
    mv_imm_abs_w(g, 0x2008, INTREQ)             # CLR 3 + 13: no longer held
    mv_abs_d(g, "w", INTREQR, 3)
    g(HALT)
    return g.words()


SETCLR_OPS = [("w", INTENA, 0x7FFF), ("w", INTREQ, 0x7FFF),
              ("w", INTENA, 0xC020), ("r", INTENAR, 0),
              ("w", INTENA, 0x0020), ("r", INTENAR, 1),
              ("w", INTREQ, 0xA008), ("r", INTREQR, 2),
              ("w", INTREQ, 0x0008), ("r", INTREQR, 3),
              ("w", INTENA, 0xFFFF), ("r", INTENAR, 4),
              ("b", INTENA + 1, 0x20), ("r", INTENAR, 5),
              ("b", INTREQ, 0xA0), ("r", INTREQR, 6),
              ("w", INTENA, 0x7FFF), ("r", INTENAR, 7)]


def prog_setclr() -> list[int]:
    g = G()
    for kind, a, v in SETCLR_OPS:
        if kind == "w":
            mv_imm_abs_w(g, v, a)
        elif kind == "b":
            g(0x13FC, v, *g.abs(a))             # move.b #v,abs.l
        else:
            mv_abs_d(g, "w", a, v)
    g(HALT)
    return g.words()


def setclr_want() -> list[int]:
    """The D0-D7 low words host/chipset_ref.Chipset gives for SETCLR_OPS."""
    c = chipset_ref.Chipset(chipset_ref.Ram(0x100))
    d = [0] * 8
    for kind, a, v in SETCLR_OPS:
        off = a & 0x1FE
        if kind == "w":
            c.custom_write(off, v)
        elif kind == "b":                       # OCS: a byte lands on both halves
            c.custom_write(off, v << 8 | v)
        else:
            d[v] = c.custom_read(off)
    return d


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
    for n in DRV_OPT:
        try:
            a = lookup_label(labels, n)
        except KeyError:
            a = DRV_DUMMY
        defs += ["-D", f"{n}=${a:04X}"]
    os.makedirs(out_dir, exist_ok=True)
    obj, out = os.path.join(out_dir, "drv.o"), os.path.join(out_dir, "drv.bin")
    cfg = os.path.join(out_dir, "drv.cfg")
    with open(cfg, "w") as f:
        f.write("MEMORY { M: file = %%O, start = $%04X, size = $%04X; }\n"
                "SEGMENTS { CODE: load = M, type = rw; }\n"
                % (DRV_ORG, 0xC800 - DRV_ORG))
    for cmd in (["ca65", "-g", *defs, "-o", obj, os.path.join(ROOT, "tests", "irq", "drv.s")],
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


class Slot:
    def __init__(self, rel: int, post: int = 0xFF, period: int = 0,
                 rearms: int = 0, disarm: int = 0, hold: int | None = None,
                 start: bool = True):
        self.rel, self.post, self.period, self.start = rel, post, period, start
        self.rearms, self.disarm, self.hold = rearms, disarm, hold

    def pack(self) -> bytes:
        return struct.pack("<BBHiBBBH3x", self.start, self.post, self.period,
                           self.rel, self.rearms, self.disarm,
                           0 if self.hold is None else 1, self.hold or 0)


class Rig:
    def __init__(self, target, labels, prg: str):
        from tests.harness_util import lookup_label
        from tests.run_program import Session
        self.tg, self.t = target, target.transport
        self.lab = lambda n: lookup_label(labels, n)
        self.sess = Session(target=target, labels=labels, timeout=300.0)
        try:
            self.lab("mach_arm")
            self.seam = True
        except KeyError:
            self.seam = False
        if self.seam:
            d = os.path.join(os.path.dirname(os.path.abspath(prg)), "irq-drv")
            self.drv = build_driver(os.path.splitext(prg)[0] + ".lbl", d)
            self.dl = drv_labels(d)
            self.w(DRV_ORG, self.drv)        # a leg may dcall before any stage
        self.inp(1)                     # W13-I: idle scripted input (no CIA1 scan)

    def inp(self, src: int) -> None:    # W13-I: inp_src (1 = override, idle)
        try:                            # W13-I
            self.w(self.lab("inp_src"), bytes([src, 0, 0]))   # W13-I
        except KeyError:                # W13-I: a PRG without W13-I
            pass                        # W13-I

    def call(self, addr: int, timeout: float = 300.0) -> None:
        from tests.harness_util import call_runner
        call_runner(self.tg, addr, timeout)

    guest_read = run_mach.Rig.guest_read

    def w(self, addr: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, addr, data)

    def r(self, addr: int, n: int) -> bytes:
        from c64_test_harness import read_bytes
        return read_bytes(self.t, addr, n)

    def stage(self, *progs: list[int]) -> list[int]:
        chunks, entries = build_image(*progs)
        self.sess.stage(chunks)
        if self.seam:
            self.w(DRV_ORG, self.drv)
        return entries

    def dcall(self, name: str, timeout: float = 120.0) -> bytes:
        self.w(R, b"\0")
        self.call(self.dl[name], timeout)
        r = self.r(R, 128)
        if r[0] != 0xFF:
            raise RuntimeError(f"{name} did not complete")
        return r

    def run(self, pc: int, s: int = 4, flags: int = F_INIT, sr: int = 0x27,
            slots: dict[int, Slot] | None = None, disarm: int = 0,
            rst: tuple[int, int] = (0, 0), ien: int = 0, ireq: int = 0,
            preset: tuple[int, int] = (0, 0), port: int = 0x37) -> dict:
        blk = struct.pack("<IIBBBBIBBHIBBH", pc, BASE + 0x8000, s, flags, port,
                          rst[0], rst[1] & M32, sr, disarm, ien,
                          BASE + 0x7000, *preset, ireq)
        self.w(P, blk.ljust(0x40, b"\0"))
        tab = bytearray(0x80)
        for k, sl in (slots or {}).items():
            tab[16 * k:16 * k + 16] = sl.pack()
        self.w(P + 0x40, bytes(tab))
        r = self.dcall("drv_run")
        u32 = lambda o: struct.unpack_from("<I", r, o)[0]
        n = struct.unpack_from("<H", r, 62)[0]
        raw = self.r(LOG, 8 * min(n, 32)) if n else b""
        log = []
        for i in range(0, len(raw), 8):
            k, e = raw[i], struct.unpack_from("<I", raw, i + 1)[0]
            late = int.from_bytes(raw[i + 5:i + 8], "little")
            if late & 0x800000:
                late -= 1 << 24
            log.append((k, e, late))
        return {"halt": r[1], "reason": r[2], "ev": r[3], "frames": u32(4),
                "e0": u32(8), "fires": list(struct.unpack_from("<8H", r, 12)),
                "arms": struct.unpack_from("<H", r, 28)[0],
                "repl": struct.unpack_from("<H", r, 30)[0],
                "d": list(struct.unpack_from("<4I", r, 32)),
                "intreqr": struct.unpack_from(">H", r, 48)[0],
                "intenar": struct.unpack_from(">H", r, 50)[0],
                "sr": struct.unpack_from("<H", r, 52)[0], "pc": u32(54),
                "a7": u32(58), "logn": n, "e1": u32(64), "am": r[68],
                "hold": struct.unpack_from("<H", r, 69)[0], "vtb": u32(71),
                "frames0": u32(75), "osp": u32(79), "nirq": u32(83),
                "stp": r[87], "log": log, "base": False}

    def run_any(self, pc: int, budget: int = 400_000, **kw) -> dict:
        """drv_run on a seam PRG; test_run_program (plain mode: no
        exception delivery, the red build) otherwise."""
        if self.seam:
            return self.run(pc, **kw)
        res = self.sess.run_program(pc, BASE + 0x8000, budget)
        return {"halt": res.halt, "reason": res.reason, "frames": 0,
                "nirq": 0, "d": [res.regs[f"D{i}"] for i in range(4)],
                "base": True}


def clean(res: dict, tag: str, fails: list[str]) -> bool:
    if res["halt"] != 1 or res["reason"] != 0:
        fails.append(f"{tag}: no clean HALT (halt=${res['halt']:02X} "
                     f"reason={res['reason']})")
        return False
    return True


def need_seam(rig: Rig, tag: str) -> list[str]:
    return [] if rig.seam else [f"{tag}: no event seam in this PRG (mach_arm missing)"]


def check_fires(res: dict, s: int, tag: str, f: list[str]) -> None:
    """Every logged firing: never early, at most SLACK late; log in E order."""
    slack = SLACK_PHI2 >> s
    last = None
    for k, e, late in res["log"]:
        if last is not None and ((e - last) & M32) >= 1 << 31:
            f.append(f"{tag}: log out of time order (E {last} then {e})")
        last = e
        if k & 0x80:
            continue
        if late < 0:
            f.append(f"{tag}: slot {k} fired {-late} E ticks EARLY")
        elif late > slack:
            f.append(f"{tag}: slot {k} fired {late} E ticks late (slack {slack})")


# --------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------

def leg_setclr(rig: Rig) -> list[str]:
    f: list[str] = []
    (pc,) = rig.stage(prog_setclr())
    res = rig.sess.run_program(pc, BASE + 0x8000, 200)
    got = [res.regs[f"D{i}"] & 0xFFFF for i in range(8)]
    want = setclr_want()
    if res.halt != 1 or res.reason != 0:
        f.append(f"setclr: no clean HALT ({res.halt}/{res.reason})")
    for i, (g_, w_) in enumerate(zip(got, want)):
        if g_ != w_:
            f.append(f"setclr: d{i} = ${g_:04X}, chipset_ref ${w_:04X}")
    return f


def leg_etime(rig: Rig) -> list[str]:
    f = need_seam(rig, "etime")
    if f:
        return f
    for cs, chis in (((0xFFFFFF00, 0x00000100, 0x00001000), (0, 1, 1)),
                     ((0x12345678, 0x80000000, 0xFFFFFFFF), (0, 0, 0)),
                     ((0x7FFFFFF0, 0x00000010, 0x00000020), (0, 1, 1))):
        for s in (0, 4, 8, 9, 16):
            blk = struct.pack("<IIBxxxI", cs[0], cs[1], s, cs[2])
            rig.w(P, blk)
            r = rig.dcall("drv_et")
            for i, (c, chi) in enumerate(zip(cs, chis)):
                e, gchi = struct.unpack_from("<IH", r, 4 + 8 * i)
                want = ((chi << 32 | c) >> s) & M32
                if gchi != chi or e != want:
                    f.append(f"etime s={s} C=${c:08X}: E=${e:08X} chi={gchi}, "
                             f"want ${want:08X} chi={chi}")
    return f


def leg_etlive(rig: Rig) -> list[str]:
    f = need_seam(rig, "etlive")
    if f:
        return f
    for s in (0, 4):
        rig.w(P, struct.pack("<IIBxxxH", 0, 0, s, 4))
        rig.dcall("drv_etl")
        raw = rig.r(LOG, 16 * 12)
        last = None
        for i in range(16):
            e, now, chi = struct.unpack_from("<IIH", raw, 12 * i)
            if e != ((chi << 32 | now) >> s) & M32:
                f.append(f"etlive s={s} #{i}: E={e} from C={now} chi={chi}")
            if last is not None and not 0 < ((e - last) & M32) < 1 << 31:
                f.append(f"etlive s={s} #{i}: E not increasing ({last} -> {e})")
            last = e
    return f


def leg_arm(rig: Rig) -> list[str]:
    f = need_seam(rig, "arm")
    if f:
        return f
    (pc,) = rig.stage(prog_busy(2600))
    slots = {1: Slot(1000), 2: Slot(2500), 3: Slot(-5), 4: Slot(20000),
             5: Slot(1500, disarm=6), 6: Slot(3000), 7: Slot(800)}
    res = rig.run(pc, s=4, slots=slots, disarm=7)
    if clean(res, "arm", f):
        want = [0, 1, 1, 1, 1, 1, 0, 0]
        if res["fires"] != want:
            f.append(f"arm: fires {res['fires']}, want {want}")
        if (res["e1"] - res["e0"]) & M32 < 20000 + (SLACK_PHI2 >> 4):
            f.append("arm: the guest ended before the last deadline (busy too short)")
        if res["am"]:
            f.append(f"arm: slots still armed at the end (mask ${res['am']:02X})")
        order = [k for k, _, _ in res["log"]]
        if order != [3, 1, 5, 2, 4]:
            f.append(f"arm: firing order {order}, want [3, 1, 5, 2, 4]")
        check_fires(res, 4, "arm", f)
    return f


def leg_periodic(rig: Rig) -> list[str]:
    f = need_seam(rig, "periodic")
    if f:
        return f
    (pc,) = rig.stage(prog_busy(2600))
    for s, scale in ((4, 1), (0, 16)):
        n = 30
        res = rig.run(pc, s=s, flags=F_INIT | F_HOOK,
                      slots={2: Slot(300 * scale, period=400 * scale, rearms=n)})
        tag = f"periodic s={s}"
        if not clean(res, tag, f):
            continue
        if res["fires"][2] != n + 1:
            f.append(f"{tag}: {res['fires'][2]} firings, want {n + 1}")
        es = [e for k, e, _ in res["log"] if k == 2]
        dls = [(res["e0"] + (300 + 400 * i) * scale) & M32 for i in range(len(es))]
        lates = [(e - d) & M32 for e, d in zip(es, dls)]
        if any(x >= 1 << 31 or x > SLACK_PHI2 >> s for x in lates):
            f.append(f"{tag}: firing vs E0 + 300 + 400 k off: {lates[:8]}...")
        check_fires(res, s, tag, f)
        if res["vtb"] != res["frames"] - res["frames0"] or res["vtb"] == 0:
            f.append(f"{tag}: VERTB hook ran {res['vtb']}x for "
                     f"{res['frames'] - res['frames0']} frames")
    return f


def leg_trap(rig: Rig) -> list[str]:
    f = need_seam(rig, "trap")
    if f:
        return f
    k = 8
    (pc,) = rig.stage(prog_trap(k, 120))
    res = rig.run(pc, s=4, flags=F_INIT | F_SUPER | F_RST, rst=(7, 600),
                  slots={7: Slot(0, start=False)})
    if clean(res, "trap", f):
        if res["arms"] != k or res["fires"][7] != k or res["repl"]:
            f.append(f"trap: {res['arms']} arms, {res['fires'][7]} firings, "
                     f"{res['repl']} replaced; want {k}/{k}/0")
        check_fires(res, 4, "trap", f)
    return f


def leg_race(rig: Rig) -> list[str]:
    f = need_seam(rig, "race")
    if f:
        return f
    k = 3000
    (pc,) = rig.stage(prog_trap(k, 2))
    for d in (150, 700, 5000):
        res = rig.run(pc, s=0, flags=F_INIT | F_SUPER | F_RST, rst=(7, d),
                      slots={7: Slot(0, start=False)})
        tag = f"race d={d}"
        if not clean(res, tag, f):
            continue
        pend = 1 if res["am"] & 0x80 else 0
        if res["arms"] != k or res["fires"][7] + res["repl"] + pend != k:
            f.append(f"{tag}: arms {res['arms']}, fires {res['fires'][7]} + "
                     f"replaced {res['repl']} + pending {pend} != {k}")
        if res["fires"][7] == 0:
            f.append(f"{tag}: nothing fired")
        if d == 5000 and not res["repl"]:
            f.append(f"{tag}: no arm replaced a pending one (the leg lost its teeth)")
        check_fires(res, 0, tag, f)
        if VERBOSE:
            print(f"    {tag}: fires {res['fires'][7]} replaced {res['repl']}")
    return f


def leg_post(rig: Rig) -> list[str]:
    f = need_seam(rig, "post")
    if f:
        return f
    (pc,) = rig.stage(prog_post())
    res = rig.run(pc, s=4, slots={3: Slot(1000, post=3, hold=0x0008),
                                 4: Slot(3000, post=13, hold=0)})
    if clean(res, "post", f):
        d0, d1, d2, d3 = (x & 0xFFFF for x in res["d"])
        if d0 != 1:
            f.append("post: INTREQR bit 13 never seen (mach_post of a high bit)")
        if not d1 & 0x0008:
            f.append(f"post: INTREQR ${d1:04X} after the bit-3 post")
        if not d2 & 0x0008:
            f.append(f"post: held bit 3 cleared by the guest (INTREQR ${d2:04X})")
        if d3 & 0x2008:
            f.append(f"post: INTREQR ${d3:04X} after CLR with the hold dropped")
        if res["fires"][3:5] != [1, 1]:
            f.append(f"post: fires {res['fires']}")
    return f


def leg_out(rig: Rig) -> list[str]:
    f = need_seam(rig, "out")
    if f:
        return f
    r = rig.dcall("drv_out")
    if r[1] or r[2]:
        f.append(f"out: post/arm outside mach_run left st_halt ${r[1]:02X} "
                 f"ev_pend ${r[2]:02X}")
    if not r[3] & 0x04:
        f.append("out: the post outside mach_run did not set INTREQR bit 2")
    return f


# --------------------------------------------------------------------------
# Phase B: interrupt delivery (S3.3)
# --------------------------------------------------------------------------

DATA = BASE + 0x9000        # guest counters
CNT, HSR, SSR, SPC, HSP = DATA, DATA + 4, DATA + 8, DATA + 12, DATA + 16
DEPTH, MAXD, CNT6, SSR6, HSR6 = DATA + 20, DATA + 24, DATA + 28, DATA + 32, DATA + 36
LVL = DATA + 40
REC = BASE + 0xA000         # per-delivery records (levels leg)
OUTS = BASE + 0xB000        # main-loop samples (levels leg)
SSP0, USP0 = BASE + 0x8000, BASE + 0x7000
LEVEL_OF = dict((b, l) for b, l in chipset_ref.IRQ_LEVELS)


def mv_l_imm_abs(g: G, v: int, a: int) -> G:            # move.l #v,abs.l
    return g(0x23FC, v >> 16, v & 0xFFFF, *g.abs(a))


def clr_l(g: G, a: int) -> G:
    return g(0x42B9, *g.abs(a))


def move_sr(g: G, v: int) -> G:                         # move.w #v,sr
    return g(0x46FC, v)


def addq_l_abs(g: G, a: int) -> G:                      # addq.l #1,abs.l
    return g(0x52B9, *g.abs(a))


def subq_l_abs(g: G, a: int) -> G:                      # subq.l #1,abs.l
    return g(0x53B9, *g.abs(a))


def vec(g: G, n: int, name: str) -> G:
    """move.l #<label>,n*4 (patched after layout)."""
    g.fix_abs = getattr(g, "fix_abs", [])
    g.fix_abs.append((len(g.w) + 1, name))
    return g(0x23FC, 0, 0, *g.abs(4 * n))


def link(g: G, base: int) -> list[int]:
    w = g.words()
    for at, name in getattr(g, "fix_abs", []):
        a = base + 2 * g.labels[name]
        w[at], w[at + 1] = a >> 16, a & 0xFFFF
    return w


def prog_deliver(sr0: int, ien: int = 0xC020, n: int = 5, ack: bool = True,
                 bound: int = 20000, ack_after: int = 0) -> tuple[G, int]:
    """S3_PLAN §4 S3.3: a level-3 handler counts VERTBs.  Main: vector 27,
    INTENA = ien, IPL dropped to sr0, spin until CNT = n (d5 = 1 and HALT
    after *bound* passes).  The handler records SR, the stacked SR/PC and
    its A7 (the frame address)."""
    g = G()
    vec(g, 27, "h3")
    for a in (CNT, HSR, SSR, SPC, HSP):
        clr_l(g, a)
    moveq(g, 0, 5)
    moveq(g, 0, 0)
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, ien, INTENA)
    move_sr(g, sr0)
    g.label("spin")
    g(0x0CB9, 0, n, *g.abs(CNT))                # cmpi.l #n,CNT
    g.bcc(0x6C00, "done")                       # bge.w
    g(0x5280)                                   # addq.l #1,d0
    g(0x0C80, bound >> 16, bound & 0xFFFF)      # cmpi.l #bound,d0
    g.bcc(0x6D00, "spin")                       # blt.w
    moveq(g, 1, 5)
    g.label("done")
    g.label("spin_end")
    g(HALT)
    g.label("h3")
    addq_l_abs(g, CNT)
    g(0x40F9, *g.abs(HSR))                      # move.w sr,HSR
    g(0x33D7, *g.abs(SSR))                      # move.w (a7),SSR
    g(0x23EF, 2, *g.abs(SPC))                   # move.l 2(a7),SPC
    g(0x23CF, *g.abs(HSP))                      # move.l a7,HSP
    if ack_after:                               # acknowledge from entry n on
        g(0x0CB9, ack_after >> 16, ack_after & 0xFFFF, *g.abs(CNT))
        g.bcc(0x6D00, "noack")                  # blt.w
    if ack:
        mv_imm_abs_w(g, 0x0020, INTREQ)
    g.label("noack")
    g(0x4E73)                                   # rte
    return g, 0


def run_deliver(rig: Rig, g: G, s: int = 2, sr: int = 0x27,
                **kw) -> tuple[dict, dict]:
    (pc,) = rig.stage(link(g, BASE))
    res = rig.run_any(pc, s=s, flags=F_INIT | F_SUPER, sr=sr, **kw)
    raw = rig.guest_read(DATA, 44)
    mem = dict(zip(("cnt", "hsr", "ssr", "spc", "hsp", "depth", "maxd",
                    "cnt6", "ssr6", "hsr6", "lvl"),
                   struct.unpack(">IHxxHxxIIIIIHxxHxxI", raw)))
    mem["spin"] = pc + 2 * g.labels["spin"]
    mem["spin_end"] = pc + 2 * g.labels["spin_end"]
    return res, mem


def leg_deliver(rig: Rig) -> list[str]:
    """Red: the counter stays 0.  Green: counter = n, frame pinned."""
    f: list[str] = []
    for sr0, tag in ((0x2000, "super"), (0x0000, "user")):
        n = 5
        g, _ = prog_deliver(sr0, n=n)
        res, m = run_deliver(rig, g)
        t = f"deliver {tag}"
        if res["base"]:
            f.append(f"{t}: no machine loop: counter {m['cnt']} (halt "
                     f"{res['halt']}/{res['reason']})")
            continue
        if not clean(res, t, f):
            continue
        if m["cnt"] != n:
            f.append(f"{t}: handler count {m['cnt']}, want {n}")
            continue
        if res["nirq"] != n:
            f.append(f"{t}: {res['nirq']} deliveries for {n} handler runs")
        if m["ssr"] & 0xFF00 != sr0 & 0xFF00:
            f.append(f"{t}: stacked SR ${m['ssr']:04X}, want ${sr0 >> 8:02X}xx")
        if not m["spin"] <= m["spc"] < m["spin_end"]:
            f.append(f"{t}: stacked PC ${m['spc']:06X} outside the spin loop "
                     f"${m['spin']:06X}-${m['spin_end']:06X}")
        if m["hsr"] & 0xFF00 != 0x2300:
            f.append(f"{t}: SR in the handler ${m['hsr']:04X}, want $23xx "
                     "(S = 1, T = 0, I = 3)")
        if m["hsp"] != SSP0 - 6:
            f.append(f"{t}: frame at ${m['hsp']:06X}, want SSP - 6 = "
                     f"${SSP0 - 6:06X}")
        a7 = USP0 if sr0 == 0 else SSP0
        if res["a7"] != a7 or res["sr"] & 0xFF00 != sr0 & 0xFF00:
            f.append(f"{t}: after RTE A7 ${res['a7']:06X} SR ${res['sr']:04X}")
        if res["frames"] < n:
            f.append(f"{t}: {res['frames']} frames for {n} VERTBs")
    return f


def leg_masked(rig: Rig) -> list[str]:
    """IPL 7 -> 0 deliveries; INTEN clear -> 0 deliveries (VERTB runs)."""
    f = need_seam(rig, "masked")
    if f:
        return f
    for sr0, ien, tag in ((0x2700, 0xC020, "ipl7"), (0x2000, 0x8020, "noinTEN"),
                          (0x2300, 0xC020, "ipl3")):
        g, _ = prog_deliver(sr0, ien=ien, n=1, bound=1000)
        res, m = run_deliver(rig, g)
        t = f"masked {tag}"
        if clean(res, t, f):
            if m["cnt"] or res["nirq"]:
                f.append(f"{t}: {m['cnt']} handler runs / {res['nirq']} deliveries, want 0")
            if res["frames"] < 2:
                f.append(f"{t}: only {res['frames']} frames: the leg proves nothing")
    return f


def leg_storm(rig: Rig) -> list[str]:
    """A handler that does not acknowledge its request is re-entered right
    after its RTE, as on a 68000 (the request stays asserted): with the
    MACH_POLL poll the IPL drop is seen within MACH_POLL phi2, so N entries
    come within a few frames (round 0: 5 at s = 4, now 2).  This handler acknowledges from entry N
    on, so the storm ends; one that acknowledges at once runs once per
    VERTB."""
    f = need_seam(rig, "storm")
    if f:
        return f
    n = 20
    for ack in (True, False):
        g, _ = (prog_deliver(0x2000, n=6) if ack else
                prog_deliver(0x2000, n=n, ack_after=n))
        res, m = run_deliver(rig, g, s=4)
        t = f"storm ack={ack}"
        if not clean(res, t, f):
            continue
        fr = res["frames"] - res["frames0"]
        if VERBOSE:
            print(f"    {t}: {res['nirq']} deliveries, {fr} frames")
        if ack and not res["nirq"] <= fr + 1:
            f.append(f"{t}: {res['nirq']} deliveries for {fr} frames (re-entry "
                     "without a new request)")
        if not ack:
            if m["cnt"] < n or not n <= res["nirq"] <= n + 1:
                f.append(f"{t}: {m['cnt']} entries / {res['nirq']} deliveries, "
                         f"want {n} (+1 for a VERTB after the acknowledge)")
            if fr > 3:                          # round 0 (no poll): 5
                f.append(f"{t}: {n} re-entries took {fr} frames: the IPL drop "
                         "at RTE waited for VERTB (no MACH_POLL poll)")
    return f


# --- review w9-irq-review legs (fix round 1; programs from its stress.py) --

CNT2, MISS, LOST = DATA + 28, DATA + 32, DATA + 36


def prog_stoplost(nvb: int, bound: int) -> G:
    """Loop: IPL 7, post SOFT (level 1), STOP #$2000 (the STOP's halt path
    delivers SOFT at once).  VERTB (level 3) counts frames into CNT.  Ends
    when CNT = nvb (d5 = 0) or after *bound* STOPs (d5 = 1: VERTB stopped:
    an event taken during STOP was lost and nothing re-armed the one-shot)."""
    g = G()
    vec(g, 27, "hv")
    vec(g, 25, "hs")
    clr_l(g, CNT)
    clr_l(g, CNT2)
    moveq(g, 0, 5)
    moveq(g, 0, 0)
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, 0xC024, INTENA)             # INTEN | VERTB | SOFT
    g.label("top")
    move_sr(g, 0x2700)
    mv_imm_abs_w(g, 0x8004, INTREQ)             # SOFT pending (masked)
    g(0x4E72, 0x2000)                           # stop #$2000
    g(0x5280)                                   # addq.l #1,d0
    g(0x0CB9, nvb >> 16, nvb & 0xFFFF, *g.abs(CNT))
    g.bcc(0x6C00, "done")                       # bge.w
    g(0x0C80, bound >> 16, bound & 0xFFFF)
    g.bcc(0x6D00, "top")                        # blt.w
    moveq(g, 1, 5)
    g.label("done")
    g(0x2239, *g.abs(CNT))                      # move.l CNT,d1
    g(0x2439, *g.abs(CNT2))                     # move.l CNT2,d2
    g(HALT)
    g.label("hv")
    addq_l_abs(g, CNT)
    mv_imm_abs_w(g, 0x0020, INTREQ)
    g(0x4E73)
    g.label("hs")
    addq_l_abs(g, CNT2)
    mv_imm_abs_w(g, 0x0004, INTREQ)
    g(0x4E73)
    return g


def leg_stoplost(rig: Rig) -> list[str]:
    """Review F1: STOPs back to back at s = 0 (frame 14188 phi2), so the
    one-shot often fires during a STOP: VERTB must keep coming."""
    f = need_seam(rig, "stoplost")
    if f:
        return f
    nvb, bound = 150, 6000
    (pc,) = rig.stage(link(prog_stoplost(nvb, bound), BASE))
    res = rig.run(pc, s=0, flags=F_INIT | F_SUPER, sr=0x27)
    d = res["d"]
    if VERBOSE:
        print(f"    stoplost: stops {d[0]} vertb {d[1]} soft {d[2]} "
              f"frames {res['frames'] - res['frames0']}")
    if not clean(res, "stoplost", f):
        return f
    if d[1] < nvb:
        f.append(f"stoplost: VERTB stopped after {d[1]} frames; {d[0]} STOPs "
                 "ran with no timer event (an event taken at STOP was lost)")
    if d[2] != d[0]:
        f.append(f"stoplost: {d[2]} SOFT deliveries for {d[0]} STOPs")
    return f


def prog_starve(nvb: int, src: int) -> G:
    """IPL 7: VERTB and a level-1/2 source (INTREQ bit 2 SOFT / 3 PORTS)
    pending and enabled, then MOVE #$2000,SR.  A 68000 takes VERTB, then the
    low level right after the VERTB handler's RTE.  Spin until nvb VERTBs;
    d2 = the low level's count (must be 1)."""
    g = G()
    vec(g, 27, "hv")
    vec(g, 24 + (1 if src == 2 else 2), "hs")
    clr_l(g, CNT)
    clr_l(g, CNT2)
    moveq(g, 0, 5)
    moveq(g, 0, 0)
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, 0xC020 | (1 << src), INTENA)
    mv_imm_abs_w(g, 0x8020 | (1 << src), INTREQ)
    move_sr(g, 0x2000)
    g.label("spin")
    g(0x5280)
    g(0x0CB9, nvb >> 16, nvb & 0xFFFF, *g.abs(CNT))
    g.bcc(0x6D00, "spin")                       # blt.w
    g(0x2239, *g.abs(CNT))
    g(0x2439, *g.abs(CNT2))
    g(HALT)
    g.label("hv")
    addq_l_abs(g, CNT)
    mv_imm_abs_w(g, 0x0020, INTREQ)
    g(0x4E73)
    g.label("hs")
    addq_l_abs(g, CNT2)
    mv_imm_abs_w(g, 1 << src, INTREQ)
    g(0x4E73)
    return g


def leg_starve(rig: Rig) -> list[str]:
    """Review F2: a pending lower level is not starved by a recurring VERTB."""
    f = need_seam(rig, "starve")
    if f:
        return f
    for src in (2, 3):
        (pc,) = rig.stage(link(prog_starve(30, src), BASE))
        res = rig.run(pc, s=2, flags=F_INIT | F_SUPER, sr=0x27)
        if not clean(res, f"starve bit {src}", f):
            continue
        d = res["d"]
        if d[2] != 1:
            f.append(f"starve: INTREQ bit {src} (enabled, pending, IPL 0) "
                     f"delivered {d[2]}x during {d[1]} VERTBs; want 1")
    return f


def prog_kick(k: int, polls: int = 1000) -> G:
    """k x {SET SOFT at IPL 0 (a trap-context kick), wait for its handler,
    an LFSR-random busy gap}; VERTB (level 3) races it.  LOST counts SETs
    not delivered within *polls* polls, MISS deliveries whose stacked PC is
    not the instruction after the write."""
    g = G()
    vec(g, 27, "hv")
    vec(g, 25, "hs")
    for a in (CNT, CNT2, MISS, LOST):
        clr_l(g, a)
    moveq(g, 0, 5)
    moveq(g, 0, 0)
    moveq(g, 0x5A, 7)
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, 0xC024, INTENA)
    move_sr(g, 0x2000)
    g.label("top")
    clr_l(g, SPC)
    g(0x2239, *g.abs(CNT2))                     # move.l CNT2,d1
    mv_imm_abs_w(g, 0x8004, INTREQ)             # the kick
    g.label("after")
    g(0x383C, polls - 1)                        # move.w #polls-1,d4
    g.label("wt")
    g(0xB2B9, *g.abs(CNT2))                     # cmp.l CNT2,d1
    g.bcc(0x6600, "got")
    g.dbra(4, "wt")
    addq_l_abs(g, LOST)
    g.bcc(0x6000, "gap")
    g.label("got")
    g.fix_abs = getattr(g, "fix_abs", [])
    g.fix_abs.append((len(g.w) + 1, "after"))
    g(0x0CB9, 0, 0, *g.abs(SPC))                # cmpi.l #after,SPC
    g.bcc(0x6700, "gap")
    addq_l_abs(g, MISS)
    g.label("gap")
    g(0x2607)                                   # move.l d7,d3
    g(0x0243, 0x007F)                           # andi.w #$7F,d3
    g.label("bz")
    g.dbra(3, "bz")
    g(0xDE47)                                   # add.w d7,d7
    g.bcc(0x6400, "nx")                         # bcc.w
    g(0x0A47, 0x002D)                           # eori.w #$2D,d7
    g.label("nx")
    g(0x5280)                                   # addq.l #1,d0
    g(0x0C80, k >> 16, k & 0xFFFF)
    g.bcc(0x6D00, "top")
    g(0x2239, *g.abs(CNT))
    g(0x2439, *g.abs(CNT2))
    g(0x2639, *g.abs(MISS))                     # move.l MISS,d3
    g(HALT)
    g.label("hv")
    addq_l_abs(g, CNT)
    mv_imm_abs_w(g, 0x0020, INTREQ)
    g(0x4E73)
    g.label("hs")
    addq_l_abs(g, CNT2)
    g(0x23EF, 2, *g.abs(SPC))
    mv_imm_abs_w(g, 0x0004, INTREQ)
    g(0x4E73)
    return g


def leg_kick(rig: Rig) -> list[str]:
    """Review F2/F3: 200 kicked SETs racing VERTB at s = 0: every one is
    delivered, VERTB keeps being handled."""
    f = need_seam(rig, "kick")
    if f:
        return f
    k = 200
    (pc,) = rig.stage(link(prog_kick(k), BASE))
    res = rig.run(pc, s=0, flags=F_INIT | F_SUPER, sr=0x27)
    d = res["d"]
    lost = struct.unpack(">I", rig.guest_read(LOST, 4))[0]
    fr = res["frames"] - res["frames0"]
    if VERBOSE:
        print(f"    kick: soft {d[2]} lost {lost} miss {d[3]} vertb {d[1]} frames {fr}")
    if not clean(res, "kick", f):
        return f
    if lost or d[2] != k:
        f.append(f"kick: {lost} SETs not delivered in time, {d[2]} of {k} handled")
    if d[1] < fr - 1:
        f.append(f"kick: {d[1]} VERTBs handled of {fr} frames")
    return f


def prog_ustop(lvl_bit: int, vecn: int) -> G:
    """STOP #$0000 (to user mode, IPL 0); the handler records SR, the
    stacked SR/PC and A7; after the RTE the user code halts."""
    g = G()
    vec(g, vecn, "h")
    for a in (CNT, HSR, SSR, SPC, HSP):
        clr_l(g, a)
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, 0xC000 | (1 << lvl_bit), INTENA)
    g(0x4E72, 0x0000)                           # stop #$0000
    g.label("after")
    g(0x2039, *g.abs(CNT))
    g(HALT)
    g.label("h")
    addq_l_abs(g, CNT)
    g(0x40F9, *g.abs(HSR))
    g(0x33D7, *g.abs(SSR))
    g(0x23EF, 2, *g.abs(SPC))
    g(0x23CF, *g.abs(HSP))
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    g(0x4E73)
    return g


def leg_ustop(rig: Rig) -> list[str]:
    """STOP into user mode, woken by VERTB (level 3) and by a client post
    (bit 13, level 6): the frame goes on the SSP, A7 is the USP after RTE."""
    f = need_seam(rig, "ustop")
    if f:
        return f
    for tag, bit, vecn, slots in (("vertb", 5, 27, None),
                                  ("exter", 13, 30, {2: Slot(3000, post=13)})):
        g = prog_ustop(bit, vecn)
        (pc,) = rig.stage(link(g, BASE))
        res = rig.run(pc, s=2, flags=F_INIT | F_SUPER, sr=0x27, slots=slots)
        cnt, hsr, ssr, spc, hsp = struct.unpack(">IHxxHxxII", rig.guest_read(DATA, 20))
        after = pc + 2 * g.labels["after"]
        lvl = 3 if bit == 5 else 6
        want = (1, 0x2000 | lvl << 8, 0x0000, after, SSP0 - 6)
        if (res["halt"], res["reason"]) != (1, 0) or (cnt, hsr, ssr, spc, hsp) != want \
                or res["a7"] != USP0 or res["osp"] != SSP0 or res["sr"] & 0x2000:
            f.append(f"ustop {tag}: (count, SR, stacked SR, PC, A7) = ({cnt}, "
                     f"${hsr:04X}, ${ssr:04X}, ${spc:06X}, ${hsp:06X}), A7 "
                     f"${res['a7']:06X} osp ${res['osp']:06X}; want {want}")
    return f


def prog_levels() -> tuple[G, list[int]]:
    """For each INTREQ bit b: INTENA = INTEN | b, IPL 0, CPU SETs INTREQ b;
    the handler of the level it raises records its frame; the main loop's
    next instruction samples LVL (the kick: delivered at the boundary)."""
    g = G()
    for lv in range(1, 7):
        vec(g, 24 + lv, f"h{lv}")
    g(0x41F9, *g.abs(OUTS))                     # lea OUTS,a0
    g(0x43F9, *g.abs(REC))                      # lea REC,a1
    nexts = []
    for b in range(14):
        mv_imm_abs_w(g, 0x7FFF, INTENA)
        mv_imm_abs_w(g, 0x7FFF, INTREQ)
        clr_l(g, LVL)
        mv_imm_abs_w(g, 0xC000 | 1 << b, INTENA)
        move_sr(g, 0x2000)                      # IPL 0, CCR 0
        mv_imm_abs_w(g, 0x8000 | 1 << b, INTREQ)    # N = 1: CCR $08
        g.label(f"n{b}")
        nexts.append(f"n{b}")
        g(0x20F9, *g.abs(LVL))                  # move.l LVL,(a0)+
        move_sr(g, 0x2700)
    g(HALT)
    for lv in range(1, 7):
        g.label(f"h{lv}")
        g(0x40D9)                               # move.w sr,(a1)+
        g(0x32D7)                               # move.w (a7),(a1)+
        g(0x22EF, 2)                            # move.l 2(a7),(a1)+
        g(0x22CF)                               # move.l a7,(a1)+
        mv_l_imm_abs(g, lv, LVL)
        mv_imm_abs_w(g, 0x7FFF, INTREQ)
        g(0x4E73)
    return g, nexts


def leg_levels(rig: Rig) -> list[str]:
    """Hand vectors pin the frame of every level: stacked SR ($2008), the
    stacked PC (the next instruction: delivered at the boundary after the
    SET), SR in the handler ($2008 | level << 8), the frame at SSP - 6, and
    the level of every INTREQ bit = host/chipset_ref.IRQ_LEVELS."""
    f = need_seam(rig, "levels")
    if f:
        return f
    g, nexts = prog_levels()
    (pc,) = rig.stage(link(g, BASE))
    res = rig.run(pc, s=4, flags=F_INIT | F_SUPER, sr=0x27)
    if not clean(res, "levels", f):
        return f
    outs = struct.unpack(">14I", rig.guest_read(OUTS, 56))
    recs = rig.guest_read(REC, 12 * 14)
    if res["nirq"] != 14:
        f.append(f"levels: {res['nirq']} deliveries, want 14")
    for b in range(14):
        lv = LEVEL_OF[b]
        hsr, ssr, spc, hsp = struct.unpack_from(">HHII", recs, 12 * b)
        want_pc = pc + 2 * g.labels[nexts[b]]
        got = (outs[b], hsr, ssr, spc, hsp)
        want = (lv, 0x2008 | lv << 8, 0x2008, want_pc, SSP0 - 6)
        if got != want:
            f.append(f"levels bit {b}: (level, SR, stacked SR, stacked PC, A7) = "
                     f"({got[0]}, ${hsr:04X}, ${ssr:04X}, ${spc:06X}, ${hsp:06X}), "
                     f"want ({lv}, ${want[1]:04X}, $2008, ${want_pc:06X}, "
                     f"${SSP0 - 6:06X})")
    return f


def prog_nest(n: int = 2, busy_n: int = 600) -> G:
    """Level-3 VERTB handler: depth count, SETs EXTER (level 6, enabled)
    from inside (nested at once), a long busy part (so passes run while it
    is in service with VERTB still pending), ack at the END.  The level-6
    handler records its stacked SR and SR and acknowledges EXTER."""
    g = G()
    vec(g, 27, "h3")
    vec(g, 30, "h6")
    for a in (CNT, DEPTH, MAXD, CNT6, SSR6, HSR6):
        clr_l(g, a)
    moveq(g, 0, 5)
    moveq(g, 0, 0)
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, 0xE020, INTENA)             # INTEN | EXTER | VERTB
    move_sr(g, 0x2000)
    g.label("spin")
    g(0x0CB9, 0, n, *g.abs(CNT))
    g.bcc(0x6C00, "done")
    g(0x5280)
    g(0x0C80, 0x0006, 0x0000)                   # bound 393216
    g.bcc(0x6D00, "spin")
    moveq(g, 1, 5)
    g.label("done")
    g.label("spin_end")
    g(HALT)
    g.label("h3")
    addq_l_abs(g, DEPTH)
    g(0x2E39, *g.abs(DEPTH))                    # move.l DEPTH,d7
    g(0xBEB9, *g.abs(MAXD))                     # cmp.l MAXD,d7
    g.bcc(0x6F00, "nomax")                      # ble.w
    g(0x23C7, *g.abs(MAXD))                     # move.l d7,MAXD
    g.label("nomax")
    mv_imm_abs_w(g, 0xA000, INTREQ)             # SET EXTER: level 6 now
    busy(g, busy_n, "hb")
    addq_l_abs(g, CNT)
    subq_l_abs(g, DEPTH)
    mv_imm_abs_w(g, 0x0020, INTREQ)             # ack VERTB last
    g(0x4E73)
    g.label("h6")
    addq_l_abs(g, CNT6)
    g(0x33D7, *g.abs(SSR6))                     # move.w (a7),SSR6
    g(0x40F9, *g.abs(HSR6))                     # move.w sr,HSR6
    mv_imm_abs_w(g, 0x2000, INTREQ)
    g(0x4E73)
    return g


def leg_nest(rig: Rig) -> list[str]:
    """A level-6 request inside a level-3 handler nests at once; level 3
    never re-enters itself while in service (IPL compare is >, not >=)."""
    f = need_seam(rig, "nest")
    if f:
        return f
    g = prog_nest()
    res, m = run_deliver(rig, g, s=4)
    if clean(res, "nest", f):
        if m["cnt"] < 2 or m["cnt6"] != m["cnt"]:
            f.append(f"nest: level-3 runs {m['cnt']}, level-6 runs {m['cnt6']}, "
                     "want >= 2, one level-6 run per level-3 run")
        if m["maxd"] != 1:
            f.append(f"nest: level-3 handler re-entered (max depth {m['maxd']})")
        if m["ssr6"] & 0x0700 != 0x0300 or m["hsr6"] & 0xFF00 != 0x2600:
            f.append(f"nest: level-6 stacked SR ${m['ssr6']:04X} (want I = 3), "
                     f"SR ${m['hsr6']:04X} (want $26xx)")
        if res["frames"] - res["frames0"] < 3:
            f.append("nest: too few frames for the level-3 handler to be "
                     "re-requested in service (the leg proves nothing)")
    return f


def prog_stop(pre: list, stop_sr: int, n_after: bool = True) -> G:
    g = G()
    vec(g, 27, "h3")
    vec(g, 26, "h2")
    clr_l(g, CNT)
    clr_l(g, SPC)
    moveq(g, 0, 0)
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    for v, a in pre:
        mv_imm_abs_w(g, v, a)
    g(0x4E72, stop_sr)                          # stop #sr
    g.label("after")
    g(0x2039, *g.abs(CNT))                      # move.l CNT,d0
    g(HALT)
    for h in ("h3", "h2"):
        g.label(h)
        addq_l_abs(g, CNT)
        g(0x23EF, 2, *g.abs(SPC))
        mv_imm_abs_w(g, 0x7FFF, INTREQ)
        mv_imm_abs_w(g, 0x7FFF, INTENA)         # one wake-up only
        g(0x4E73)
    return g


def leg_stop(rig: Rig) -> list[str]:
    """STOP waits for an interrupt (VERTB, a client's post), takes one that
    is already pending at once, and returns HALT_STOP when nothing can wake
    it (IPL 7, nothing enabled)."""
    f = need_seam(rig, "stop")
    if f:
        return f
    # client: the deadline lies well after the STOP (20000 E at s = 2), so
    # the CPU really waits on the slot (at 3000 E it fired before the STOP)
    cases = [("vertb", [(0xC020, INTENA)], 0x2000, None, True),
             ("pending", [(0xC020, INTENA), (0x8020, INTREQ)], 0x2000, None, True),
             ("client", [(0xC008, INTENA)], 0x2000, {3: Slot(20000, post=3)}, True),
             ("ipl7", [(0xC020, INTENA)], 0x2700, None, False),
             ("noenable", [], 0x2000, None, False),
             ("noclient", [(0xC008, INTENA)], 0x2000, None, False)]
    for tag, pre, ssr, slots, wakes in cases:
        g = prog_stop(pre, ssr)
        (pc,) = rig.stage(link(g, BASE))
        res = rig.run(pc, s=2, flags=F_INIT | F_SUPER, sr=0x27, slots=slots)
        t = f"stop {tag}"
        cnt, spc = struct.unpack(">II", rig.guest_read(CNT, 4) + rig.guest_read(SPC, 4))
        if wakes:
            if not clean(res, t, f):
                continue
            after = pc + 2 * g.labels["after"]
            if cnt != 1 or res["d"][0] != 1 or spc != after:
                f.append(f"{t}: handler runs {cnt} (d0 {res['d'][0]}), stacked PC "
                         f"${spc:06X}, want 1 and ${after:06X} (after the STOP)")
            if res["stp"]:
                f.append(f"{t}: still marked stopped")
        else:
            if res["halt"] != 1 or res["reason"] != 4 or cnt:
                f.append(f"{t}: halt ${res['halt']:02X} reason {res['reason']}, "
                         f"{cnt} handler runs; want HALT_STOP ($01/4), 0")
            elif res["frames"] - res["frames0"] > 2:
                f.append(f"{t}: HALT_STOP came after {res['frames'] - res['frames0']} "
                         "frames (it waited)")
    return f


def leg_oddvec(rig: Rig) -> list[str]:
    """An odd autovector halts HALT_ADDRESS (exc_raise's rule) out of
    mach_run, I = 1, the frame written."""
    f = need_seam(rig, "oddvec")
    if f:
        return f
    g, _ = prog_deliver(0x2000, n=1, bound=200000)
    words = link(g, BASE)
    h3 = BASE + 2 * g.labels["h3"]
    at = words.index(0x23FC)                    # the vector store: make it odd
    words[at + 2] = (h3 | 1) & 0xFFFF
    (pc,) = rig.stage(words)
    res = rig.run(pc, s=2, flags=F_INIT | F_SUPER, sr=0x27)
    if res["halt"] != 1 or res["reason"] != 5:
        f.append(f"oddvec: halt ${res['halt']:02X} reason {res['reason']}, "
                 "want $01/5 (HALT_ADDRESS)")
    if res["a7"] != SSP0 - 6:
        f.append(f"oddvec: A7 ${res['a7']:06X}, want the frame at ${SSP0 - 6:06X}")
    return f


def leg_haltrace(rig: Rig) -> list[str]:
    """A real halt pending on entry (st_halt = $81, as a halt that raced an
    event leaves it) with an interrupt deliverable: the halt comes back
    ($01), no frame is pushed, nothing is delivered."""
    f = need_seam(rig, "haltrace")
    if f:
        return f
    (pc,) = rig.stage([HALT])
    res = rig.run(pc, s=2, flags=F_INIT | F_SUPER | F_IEN | F_PRESET, sr=0x20,
                  ien=0xC020, ireq=0x0020, preset=(0x81, 1))
    if res["halt"] != 1 or res["nirq"] or res["a7"] != SSP0 or res["pc"] != pc:
        f.append(f"haltrace: halt ${res['halt']:02X}, {res['nirq']} deliveries, "
                 f"A7 ${res['a7']:06X}, PC ${res['pc']:06X}; want $01, 0, "
                 f"${SSP0:06X}, ${pc:06X}")
    return f




def prog_raiseev() -> G:
    """An event that arrives DURING a raise must be serviced before the
    handler's first instruction.  The SSP is $DFF0A2, so the level-3
    frame's SR word lands on INTREQ ($DFF09C); the stacked SR is $A008
    (T = 1, stored only; S = 1; N from the MOVE), i.e. an INTREQ SET of
    EXTER, which kicks from inside exc_raise (st_halt bit 7).  Correct:
    the pass runs at once and level 6 nests before h3's first instruction
    (d1 = 0, d0 = 6); an implementation that drops that bit runs h3's
    first instruction (d1 = 3) before a later pass nests level 6.  h6
    HALTs (the frames are in chip space: no RTE)."""
    g = G()
    vec(g, 27, "h3")
    vec(g, 30, "h6")
    moveq(g, 0, 0)
    moveq(g, 0, 1)
    mv_imm_abs_w(g, 0x7FFF, INTENA)
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, 0xE020, INTENA)             # INTEN | EXTER | VERTB
    g(0x2E7C, 0x00DF, 0xF0A2)                   # movea.l #$DFF0A2,a7
    move_sr(g, 0xA000)                          # T (stored), S, IPL 0
    mv_imm_abs_w(g, 0x8020, INTREQ)             # VERTB: kick -> level 3
    g(HALT)
    g.label("h3")
    moveq(g, 3, 1)
    g.label("h3spin")
    g.bcc(0x6000, "h3spin")                     # bra.w: wait to be nested
    g.label("h6")
    moveq(g, 6, 0)
    g(HALT)
    return g


def leg_raiseev(rig: Rig) -> list[str]:
    """Review F3 (dropb7): mach_dlv keeps an event that came during the
    raise (st_halt bit 7)."""
    f = need_seam(rig, "raiseev")
    if f:
        return f
    (pc,) = rig.stage(link(prog_raiseev(), BASE))
    res = rig.run(pc, s=4, flags=F_INIT | F_SUPER, sr=0x27)
    if not clean(res, "raiseev", f):
        return f
    if res["d"][0] != 6 or res["d"][1] != 0 or res["nirq"] != 2:
        f.append(f"raiseev: d0 = {res['d'][0]}, d1 = {res['d'][1]}, "
                 f"{res['nirq']} deliveries; want 6, 0, 2 (the event raised "
                 "inside the frame write was serviced only after the "
                 "level-3 handler started)")
    return f


def leg_raiseslot(rig: Rig) -> list[str]:
    """Review L1 (fix round 3): raiseev with an always-due client slot.  The
    slot makes the pass set mach_fs (a forced step); the delivery in the
    same pass must cancel it, or the step runs h3's first instruction
    before level 6 nests (d1 = 3)."""
    f = need_seam(rig, "raiseslot")
    if f:
        return f
    (pc,) = rig.stage(link(prog_raiseev(), BASE))
    res = rig.run(pc, s=4, flags=F_INIT | F_SUPER, sr=0x27,
                  slots={4: Slot(0, period=1, rearms=0xFF)})
    if not clean(res, "raiseslot", f):
        return f
    if res["d"][0] != 6 or res["d"][1] != 0:
        f.append(f"raiseslot: d0 = {res['d'][0]}, d1 = {res['d'][1]}; want 6, 0 "
                 "(the forced step ran the level-3 handler before level 6 nested)")
    return f


def leg_progress(rig: Rig) -> list[str]:
    """W9-C review / fix round 2: a client whose callback re-arms its slot
    in the past every time (always due) must not livelock the guest: a
    pass that leaves a slot due now is followed by one emu_step, so the
    guest runs one instruction per pass and its loop finishes."""
    f = need_seam(rig, "progress")
    if f:
        return f
    (pc,) = rig.stage(prog_busy(400))
    res = rig.run(pc, s=0, slots={4: Slot(0, period=1, rearms=0xFF)})
    if not clean(res, "progress", f):
        return f
    span = (res["e1"] - res["e0"]) & M32
    fires = res["fires"][4]
    if VERBOSE:
        print(f"    progress: {fires} firings in {span} phi2")
    if fires < 10:
        f.append(f"progress: only {fires} firings (the client never re-fired)")
    if fires > 2 * 801 + 2:
        f.append(f"progress: {fires} firings for 801 guest instructions: "
                 "passes ran without a guest instruction between them")
    return f


def leg_lag(rig: Rig) -> list[str]:
    """Review N1 (fix round 2): a periodic client re-arming from its own
    deadline (CIA timers' no-drift pattern) at s = 0.  P = 3000 phi2 keeps
    a bounded lateness (<= LAG_SLACK_PHI2).  P = 1500 is below what a 1 MHz
    6502 can serve (a pass with a callback plus the guaranteed guest
    instruction, ~2.9k phi2), so no scheme keeps it bounded; its lateness
    may grow by at most GROW phi2 per period (round 0 ~800 with no
    progress guarantee, this ~1450, MACH_MINW ~3150)."""
    f = need_seam(rig, "lag")
    if f:
        return f
    grow = 1600
    for p in (3000, 1500):
        (pc,) = rig.stage(prog_busy(3000))
        res = rig.run(pc, s=0, slots={4: Slot(1000, period=p, rearms=30)})
        lates = [l for k, e, l in res["log"] if k == 4]
        if VERBOSE:
            print(f"    lag P={p}: fires {res['fires'][4]} late first/last/max "
                  f"{lates[:1]}/{lates[-1:]}/{max(lates) if lates else None}")
        if not clean(res, f"lag P={p}", f):
            continue
        if res["fires"][4] != 31 or len(lates) < 31:
            f.append(f"lag P={p}: {res['fires'][4]} firings, {len(lates)} logged")
            continue
        if p == 3000:                                       # W13-I: every value reported
            print(f"    lag P=3000 (override, gated): max lateness {max(lates)} phi2")  # W13-I
        if p == 3000 and max(lates) > LAG_SLACK_PHI2:
            f.append(f"lag P=3000: max lateness {max(lates)} phi2 > {LAG_SLACK_PHI2} "
                     "(the lateness grows: the client drifts)")
        if p == 1500 and (lates[-1] - lates[0]) / 30 > grow:
            f.append(f"lag P=1500: lateness grows {(lates[-1] - lates[0]) / 30:.0f} "
                     f"phi2 per period (> {grow})")
    # W13-I: the bound is gated with the idle override (rig default); once
    # more with the real CIA1 scan (inp_src = 0, +111 cycles per frame hook),
    # recorded only (docs/CONTRACTS.md "Input")
    try:                                                    # W13-I
        rig.lab("inp_src")                                  # W13-I
        rig.inp(0)                                          # W13-I
        (pc,) = rig.stage(prog_busy(3000))                  # W13-I
        res = rig.run(pc, s=0, slots={4: Slot(1000, period=3000, rearms=30)})  # W13-I
        lates = [l for k, e, l in res["log"] if k == 4]     # W13-I
        print(f"    lag P=3000 real input (W13-I, not gated): max lateness "   # W13-I
              f"{max(lates) if lates else None} phi2 (bound {LAG_SLACK_PHI2} gated "
              "with the override)")                         # W13-I
    except KeyError:                                        # W13-I
        pass                                                # W13-I
    finally:                                                # W13-I
        rig.inp(1)                                          # W13-I
    return f


LEGS = {"setclr": leg_setclr, "etime": leg_etime, "etlive": leg_etlive,
        "arm": leg_arm, "periodic": leg_periodic, "trap": leg_trap,
        "race": leg_race, "post": leg_post, "out": leg_out,
        "deliver": leg_deliver, "masked": leg_masked, "levels": leg_levels,
        "nest": leg_nest, "storm": leg_storm, "stop": leg_stop,
        "oddvec": leg_oddvec, "haltrace": leg_haltrace,
        "stoplost": leg_stoplost, "starve": leg_starve, "kick": leg_kick,
        "ustop": leg_ustop, "progress": leg_progress, "raiseev": leg_raiseev,
        "lag": leg_lag, "raiseslot": leg_raiseslot}


def session(prg: str, fn) -> object:
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
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
# Teeth: source mutants (docs/WAVE9_BRIEF.md C1/C2)
# --------------------------------------------------------------------------

MACH, CHIP = "runtime/ops/mach.s", "runtime/ops/chip.s"
MUTANTS = {
    # the one-shot ignores the slot deadlines (VERTB / $FFFF wake-ups only)
    "noslot": (MACH, [("        jsr mach_wake           ; mach_w := min(VERTB, armed slots)\n", "")]),
    # a trap-context arm does not kick a pass
    "nokick": (MACH, [("        lda mach_in\n        beq done\n        php\n",
                       "        jmp done\n        php\n")]),
    # the kick ignores mach_in (fires outside mach_run)
    "kickout": (MACH, [("        lda mach_in\n        beq done\n        php\n",
                        "        php\n")]),
    # mach_disarm does nothing
    "disarm": (MACH, [("        eor #$FF\n        and mach_am\n        sta mach_am\n"
                       "done:   rts\n.endproc\n\n; mach_post",
                       "done:   rts\n.endproc\n\n; mach_post")]),
    # counter wraps are not counted (E jumps back at the wrap)
    "wrap": (MACH, [("        bcs :+\n        inc mach_chi\n        bne :+\n"
                     "        inc mach_chi+1\n:       rts\n", ":       rts\n")]),
    # the E shift is one short
    "eshift": (MACH, [("        ldx mach_sl\n        beq done\nsh:     lsr mach_eh+1\n",
                       "        ldx mach_sl\n        dex\n        beq done\n"
                       "sh:     lsr mach_eh+1\n")]),
    # the slot-0 VERTB hook is never called
    "hook": (MACH, [("        ldx #0                  ; slot 0's hook (CIA-A TOD), if any\n"
                     "        jmp mach_cb\n", "        rts\n")]),
    # a post of bits 8-14 lands in the low byte
    "posthi": (MACH, [("        ora chip_shd+MACH_INTREQR\n        sta chip_shd+MACH_INTREQR\n",
                       "        ora chip_shd+MACH_INTREQR+1\n        sta chip_shd+MACH_INTREQR+1\n")]),
    # every armed slot fires at the first pass (no due test)
    "nodue": (MACH, [("        sbc mach_dl+3,y\n        bmi next\n        lda mach_bit,x\n",
                      "        sbc mach_dl+3,y\n        lda mach_bit,x\n")]),
    # a slot is not disarmed before its callback (fires every pass)
    "once": (MACH, [("        bmi next\n        lda mach_bit,x\n        eor #$FF\n"
                     "        and mach_am\n        sta mach_am\n        jsr mach_cb\n",
                     "        bmi next\n        jsr mach_cb\n")]),
    # INTREQ writes ignore mach_hold
    "hold": (CHIP, [("        lda mach_hold           ; a level source still asserting re-raises\n"
                     "        ora chip_shd+CHIP_INTREQR+1\n",
                     "        lda #0\n        ora chip_shd+CHIP_INTREQR+1\n")]),
    # SET/CLR inverted (bit 15 clear sets)
    "setclr": (CHIP, [("        bit chip_vh\n        bmi set\n        lda chip_vl             ; CLR",
                       "        bit chip_vh\n        bpl set\n        lda chip_vl             ; CLR")]),
    # --- phase B: delivery (docs/WAVE9_BRIEF.md C2 teeth) ---
    # level map: VERTB/COPER/BLIT bits 4-6 -> level 4 instead of 3
    "levelmap": (MACH, [("mach_lvlo: .byte $07, $08, $70, $80\n",
                         "mach_lvlo: .byte $07, $08, $00, $F0\n")]),
    # IPL compare >= instead of > (a level re-enters its own handler)
    "iplge": (MACH, [("        cmp mach_lv\n        bcs none                ; I2-I0 >= level: masked (Paula levels are\n",
                      "        cmp mach_lv\n        beq go\n        bcs none\ngo:\n")]),
    # INTEN (the master enable) ignored
    "inten": (MACH, [("        lda chip_shd+MACH_INTENAR\n        and #$40                ; INTEN, the master enable\n"
                      "        beq done\n", "")]),
    # I2-I0 not raised to the level after the frame
    "noipl": (MACH, [("        lda r_sr+1              ; I2-I0 := level (S = 1, T = 0 already)\n"
                      "        and #$F8\n        ora mach_lv\n        sta r_sr+1\n", "")]),
    # autovector base off by one
    "vecbase": (MACH, [("MACH_AUTOVEC  = 24", "MACH_AUTOVEC  = 25")]),
    # stacked PC = the interrupted instruction's start (group-1 rule)
    "group1": (MACH, [("        jmp exc_group2\n", "        jmp exc_group1\n"),
                      (".import exc_group2\n", ".import exc_group2, exc_group1\n")]),
    # a SET write to INTENA/INTREQ does not check for a deliverable level
    "noichk": (CHIP, [("        bpl done                ; act at the next instruction boundary\n"
                       "        jmp mach_ichk\n", "        bpl done\n        rts\n")]),
    # STOP is a plain halt again (the S3.2 behaviour)
    "nostop": (MACH, [("        cmp #MACH_STOPCODE\n        bne exit\n", "        jmp exit\n")]),
    # a stopped CPU never gives up (IPL 7 / nothing enabled waits forever)
    "nowk": (MACH, [("        beq no                  ; nothing enabled above the IPL\n",
                     "        jmp yes\n")]),
    # a client slot cannot wake a stopped CPU
    "wkslot": (MACH, [("        lda mach_am\n        bne yes\n", "")]),
    # delivery ignores a real halt pending on entry
    "haltguard": (MACH, [("        lda st_halt             ; a real halt pending (st_halt = $81 on\n"
                          "        and #$7F                ; entry): emu_run returns it, no frame\n"
                          "        bne none\n", "")]),
    # --- fix round 1 (review w9-irq-review) ---
    # F1: the STOP halt path delivers without taking ev_pend (round 0)
    "stopev": (MACH, [("        jmp event               ; take ev_pend (the one-shot may have fired\n",
                       "        jmp deliv\n")]),
    # F2: no poll while an interrupt is pending (round 0)
    "nopoll": (MACH, [("        jsr mach_pclamp         ; pending interrupt: poll every MACH_POLL\n", "")]),
    # F3 (reviewer's): mach_dlv drops an event that came during the raise
    "dropb7": (MACH, [("        and #$80                ; keep an event that came meanwhile\n",
                       "        lda #0\n")]),
    # reviewer's: the STOP wake ignores a VERTB enabled above the IPL
    "wkvb": (MACH, [("        and #1 << MACH_VERTB_BIT\n        bne yes\n", "")]),
    # fix round 2: no forced guest instruction when a slot is due now
    # fix round 3: a delivery does not cancel the forced step
    "fsclear": (MACH, [("        lda #0                  ; no forced step after a delivery: a higher\n"
                        "        sta mach_fs             ; level must nest before the handler runs\n", "")]),
    "nostep": (MACH, [("loop:   lda mach_fs             ; W9-I: a slot is due now (fix round 2)\n"
                       "        bne step\n", "loop:\n")]),
}
MUTANT_LEGS = {"noslot": ["arm"], "nokick": ["trap"], "kickout": ["out"],
               "disarm": ["arm"], "wrap": ["etime"], "eshift": ["etime"],
               "hook": ["periodic"], "posthi": ["post"], "nodue": ["arm"],
               "once": ["arm"], "hold": ["post"], "setclr": ["setclr"],
               "levelmap": ["levels"], "iplge": ["nest"], "inten": ["masked"],
               "noipl": ["levels"], "vecbase": ["levels"], "group1": ["levels"],
               "noichk": ["levels"], "nostop": ["stop"], "nowk": ["stop"],
               "wkslot": ["stop"], "haltguard": ["haltrace"],
               "stopev": ["stoplost"], "nopoll": ["starve", "kick", "storm"],
               "dropb7": ["stoplost", "kick", "raiseev"], "wkvb": ["stop"],
               "nostep": ["progress"], "fsclear": ["raiseslot"]}


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


def teeth(prg: str, only: list[str] | None) -> int:
    bdir = os.path.relpath(os.path.dirname(os.path.abspath(prg)), ROOT)
    names = only or list(MUTANTS)
    bad = []
    for name in names:
        mprg = build_mutant(name, os.path.join(bdir, "irq-mut", name))
        print(f"=== mutant {name} (legs {','.join(MUTANT_LEGS[name])})")
        try:
            fails = run_all(mprg, MUTANT_LEGS[name])
        except Exception as e:
            fails = [f"{type(e).__name__}: {e}"]
        verdict = "KILLED" if fails else "SURVIVED"
        print(f"=== mutant {name}: {verdict}")
        if not fails:
            bad.append(name)
    print(f"teeth: {len(names) - len(bad)} killed, {len(bad)} survived {bad}")
    return 1 if bad else 0


def perf(prg: str, json_out: str | None) -> int:
    """Cost of the MACH_POLL poll: the same register-only guest loop at
    s = 0 (E = phi2) with nothing pending, then with VERTB enabled and
    pending but masked by IPL 7 (the AROS Disable()-with-a-request case
    when INTEN stays set): phi2 elapsed, passes, phi2 per pass."""
    import json

    def body(rig: Rig) -> dict:
        (pc,) = rig.stage(prog_busy(20000))
        out = {}
        for tag, ien, ireq in (("idle", 0x0000, 0x0000),
                               ("pending", 0xC020, 0x0020)):
            ts = []
            for _ in range(3):
                res = rig.run(pc, s=0, flags=F_INIT | F_SUPER | F_IEN,
                              sr=0x27, ien=ien, ireq=ireq)
                assert res["halt"] == 1 and res["reason"] == 0, res
                ts.append((res["e1"] - res["e0"]) & M32)
            out[tag] = min(ts)
        d = out["pending"] - out["idle"]
        share = d / out["pending"]              # time in passes, 1 MHz
        # period = MACH_POLL + c (+ the instruction in flight, ignored):
        # share = c / (MACH_POLL + c)  ->  c = share * MACH_POLL / (1 - share)
        c = share * 0x0400 / (1 - share)
        out.update(delta=d, slowdown_1mhz=d / out["idle"], share_1mhz=share,
                   pass_cycles_est=round(c),
                   share_48mhz_est=(1e6 / 0x0400) * c / 48e6)
        return out
    out = session(prg, body)
    print(json.dumps(out, indent=1))
    if json_out:
        with open(json_out, "w") as f:
            json.dump(out, f, indent=1)
    return 0


def main(argv=None) -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default=",".join(LEGS))
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--perf", action="store_true")
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    if a.perf:
        return perf(a.prg, a.json_out)
    VERBOSE = a.verbose
    if a.mutants:
        return teeth(a.prg, a.only.split(",") if a.only else None)
    fails = run_all(a.prg, a.legs.split(","))
    print(f"run_irq: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
