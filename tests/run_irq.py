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
from tests.run_mach import (BASE, HALT, BNE, BEQ, G, build_image, moveq,
                            mv_abs_d, mv_imm_abs_w, mv_imm_d_w)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRV_ORG = 0xC200
P, R, LOG = 0xCA00, 0xCB00, 0xCC00
F_INIT, F_SUPER, F_HOOK, F_RST, F_IEN = 0x01, 0x02, 0x04, 0x08, 0x10
INTENAR, INTREQR = 0xDFF01C, 0xDFF01E
INTENA, INTREQ = 0xDFF09A, 0xDFF09C
RESET = 0x4E70
SLACK_PHI2 = 4096           # a pass + IRQ entry + the instruction in flight
M32 = 0xFFFFFFFF
DRV_SYMS = ("emu_reset", "emu_run", "emu_rst_hook", "mach_init", "mach_run",
            "mach_s", "mach_nmi", "mach_now", "mach_chi", "mach_etime",
            "mach_et", "mach_at", "mach_arm", "mach_disarm", "mach_post",
            "mach_cbv", "mach_hold", "mach_am", "chip_shd")
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
        if self.seam:
            self.w(DRV_ORG, self.drv)
        return entries

    def dcall(self, name: str, timeout: float = 300.0) -> bytes:
        self.w(R, b"\0")
        self.call(self.dl[name], timeout)
        r = self.r(R, 128)
        if r[0] != 0xFF:
            raise RuntimeError(f"{name} did not complete")
        return r

    def run(self, pc: int, s: int = 4, flags: int = F_INIT, sr: int = 0x27,
            slots: dict[int, Slot] | None = None, disarm: int = 0,
            rst: tuple[int, int] = (0, 0), ien: int = 0,
            port: int = 0x37) -> dict:
        blk = struct.pack("<IIBBBBIBBHI", pc, BASE + 0x8000, s, flags, port,
                          rst[0], rst[1] & M32, sr, disarm, ien,
                          BASE + 0x7000)
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
                "frames0": u32(75), "osp": u32(79), "log": log}


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
        for s in (0, 4, 9, 16):
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


LEGS = {"setclr": leg_setclr, "etime": leg_etime, "etlive": leg_etlive,
        "arm": leg_arm, "periodic": leg_periodic, "trap": leg_trap,
        "race": leg_race, "post": leg_post, "out": leg_out}


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
}
MUTANT_LEGS = {"noslot": ["arm"], "nokick": ["trap"], "kickout": ["out"],
               "disarm": ["arm"], "wrap": ["etime"], "eshift": ["etime"],
               "hook": ["periodic"], "posthi": ["post"], "nodue": ["arm"],
               "once": ["arm"], "hold": ["post"], "setclr": ["setclr"]}


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


def main(argv=None) -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default=",".join(LEGS))
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    VERBOSE = a.verbose
    if a.mutants:
        return teeth(a.prg, a.only.split(",") if a.only else None)
    fails = run_all(a.prg, a.legs.split(","))
    print(f"run_irq: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
