#!/usr/bin/env python3
"""W15-P Paula audio: the aud module on VICE against host/paula_ref.py.

docs/WAVE15_BRIEF.md W15-P (P1) and its addendum; contract docs/CONTRACTS.md
"Audio (Paula)".  Every leg stages a small guest program (hand-assembled with
tests/run_mach.G), the audio module tests/paula/aud.s (segment A at $CA00,
segment B at $0400) and the driver tests/paula/drv.s, then runs the guest in
exception mode under mach_run.  VICE has no sampler, so the module keeps
cfg_hw = 0: the voice image in its state page (the sampler's own register
layout) is the "dry" backend the legs compare.

Legs:
  latch   LC/LEN/PER/VOL then DMA on (4 channels), then new latches: the
          playing blocks are the first latches, the voices are programmed
          from them, AUD0-3 posted at DMA start, the deadlines are DMA start
          + one block (state page = paula_ref, field by field)
  blocks  short blocks, 4 channels at different periods: every channel's
          deadline is t0 + n blocks exactly, with one t0 for all four (one
          DMACON), every block end within the seam's latency, no overdue
          channel at the end (the one-slot min-deadline multiplex)
  irq     AUD0 enabled at level 4: the handler counts and acknowledges;
          deliveries = blocks started (+-1 in flight)
  reload  a mid-block latch write acts at the next block: non-contiguous
          (reprogrammed, then looped), contiguous (looped in hardware at
          once, never reprogrammed), the one-shot -> 1-word null loop
  pervol  PER/VOL mid-block act on the voice at once (RATE, VOL capped at
          63); the block keeps its end (no rescale), the next takes the PER
  park    a 1-word loop nobody acknowledges parks (one look per frame, the
          guest keeps its speed); one acknowledge -> AUD0 posted again
  dmaoff  DMA off per channel and by DMAEN: silence (2 bytes at disp_zero),
          slot 6 disarmed; a restart re-latches and posts again
  nomod   no module armed / a wrong ID / segment B stale (its ID) or missing
          (zeros): audio writes change no module state (B mismatch disarms
          aud_mon, mutant nobok);
          DMACONR still shows the AUDxEN bits (cop.s keeps DMACON)

    python3 -m tests.run_paula --prg build/runner.prg [--legs a,b]
    python3 -m tests.run_paula --prg ... --mutants [--only m1,m2]

Exit 0 = every selected leg passed (--mutants: every mutant failed a leg).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import subprocess
import sys

from host import paula_ref as PR
from tests import run_mach
from tests.run_mach import BASE, HALT, G, build_image, mv_imm_abs_w, mv_abs_d
from tests.run_irq import mv_l_imm_abs, move_sr, addq_l_abs, vec, link

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRV_ORG = 0xC200
P, R = 0xC800, 0xC820
AUD_A, AUD_A_END = 0xCA00, 0xCD00
AUD_B, AUD_B_END = 0x0400, 0x0800
VC, CH = 0xCD00, 0xCD80
DISP_ZERO = 0xDF4000           # W13-D disp_zero (CONTRACTS "Display refresh")
CUSTOM = 0xDFF000
DMACON, DMACONR = CUSTOM + 0x096, CUSTOM + 0x002
INTENA, INTREQ, INTREQR = CUSTOM + 0x09A, CUSTOM + 0x09C, CUSTOM + 0x01E
SSP0 = BASE + 0x8000
CNT = BASE + 0xB000            # guest counters (irq leg)
SAMPLE = 0x080000              # where W15-M stages the MOD (sample data)
SLACK_PHI2 = 4096              # the seam's latency bound (run_irq SLACK_PHI2)
FIRE_PHI2 = 12288              # + one pass servicing four channels' block ends
                               # on a 1 MHz host (measured 350-480 E at s = 4:
                               # 5.6-7.7k phi2; build/evidence/w15-paula/p1_e.log)
DRV_SYMS = ("emu_reset", "mach_init", "mach_run", "mach_s", "mach_nmi",
            "mach_etime", "mach_et", "mach_cbv", "mach_nirq", "chip_reset",
            "cop_reset", "chip_shd", "aud_mon", "AUD_ORG")
VERBOSE = False


def aud(ch: int, r: int) -> int:
    return CUSTOM + 0xA0 + 16 * ch + r


# --------------------------------------------------------------------------
# Builds
# --------------------------------------------------------------------------

def _defs(labels_path: str, names) -> list[str]:
    from c64_test_harness import Labels
    from tests.harness_util import lookup_label
    labels = Labels.from_file(labels_path)
    out = []
    for n in names:
        out += ["-D", f"{n}=${lookup_label(labels, n):04X}"]
    return out


def _asm(src: str, defs: list[str], cfg_text: str, base: str) -> None:
    with open(base + ".cfg", "w") as f:
        f.write(cfg_text)
    for cmd in (["ca65", "-g", *defs, "-o", base + ".o", src],
                ["ld65", "-C", base + ".cfg", "-Ln", base + ".lbl", base + ".o"]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}{r.stderr}")


def build_aud(labels_path: str, out_dir: str, src: str | None = None,
              defines: tuple = ()):
    """The module in two segments; one ID (SHA-1[:4] of A + B with both ID
    fields zero) at A+3 and B+0.  Returns (a, b, labels).  *defines*:
    extra -D (the log build: AUD_LOG=<drv's aud_log>)."""
    from host import prebuilt                                   # W17-B
    why = " ".join(([src] if src else []) + list(defines))      # W17-B
    if prebuilt.use(("ca65", "ld65"), "aud", why and f"aud {why}", labels_path):   # W17-B
        a, b = prebuilt.module("aud", labels_path)              # W17-B
        return a, b, prebuilt.labels("aud")                     # W17-B
    from tests.aros_wb import module_syms
    src = src or os.path.join(ROOT, "tests", "paula", "aud.s")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, "aud")
    cfg = ("MEMORY { A: file = \"%s\", start = $%04X, size = $%04X;\n"
           "         B: file = \"%s\", start = $%04X, size = $%04X; }\n"
           "SEGMENTS { CODE: load = A, type = rw; CODEB: load = B, type = rw; }\n"
           % (base + ".a.bin", AUD_A, AUD_A_END - AUD_A,
              base + ".b.bin", AUD_B, AUD_B_END - AUD_B))
    defs = _defs(labels_path, module_syms(open(src).read()))
    for d in defines:
        defs += ["-D", d]
    _asm(src, defs, cfg, base)
    a = bytearray(open(base + ".a.bin", "rb").read())
    b = bytearray(open(base + ".b.bin", "rb").read())
    if a[3:7] != bytes(4) or b[0:4] != bytes(4):
        raise RuntimeError(f"{src}: no zero ID fields at A+3 / B+0")
    mid = hashlib.sha1(bytes(a) + bytes(b)).digest()[:4]
    if mid[0] == 0:
        raise RuntimeError("aud: ID byte 0 is 0 (cannot arm): change the source")
    a[3:7] = b[0:4] = mid
    lab = {}
    for line in open(base + ".lbl"):
        _, adr, n = line.split()
        lab[n.lstrip(".")] = int(adr, 16)
    return bytes(a), bytes(b), lab


def build_drv(labels_path: str, out_dir: str) -> tuple[bytes, dict]:
    from host import prebuilt                                   # W17-B: a test driver,
    prebuilt.use(("ca65", "ld65"), None, "run_paula's driver tests/paula/drv.s")   # W17-B: never shipped
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, "drv")
    cfg = ("MEMORY { M: file = \"%s\", start = $%04X, size = $%04X; }\n"
           "SEGMENTS { CODE: load = M, type = rw; }\n"
           % (base + ".bin", DRV_ORG, 0xC800 - DRV_ORG))
    _asm(os.path.join(ROOT, "tests", "paula", "drv.s"),
         _defs(labels_path, DRV_SYMS), cfg, base)
    lab = {}
    for line in open(base + ".lbl"):
        _, adr, n = line.split()
        lab[n.lstrip(".")] = int(adr, 16)
    return open(base + ".bin", "rb").read(), lab


# --------------------------------------------------------------------------
# Module state
# --------------------------------------------------------------------------

def be(b: bytes) -> int:
    return int.from_bytes(b, "big")


def le(b: bytes) -> int:
    return int.from_bytes(b, "little")


def parse(page: bytes) -> list[dict]:
    """The state page -> one dict per channel."""
    out = []
    for x in range(4):
        v, c = page[32 * x:32 * x + 32], page[128 + 32 * x:128 + 32 * x + 32]
        ctl = v[0]
        out.append(dict(
            ctl=ctl, vvol=v[1], pan=v[2], start=be(v[4:8]), length=be(v[9:12]),
            rate=be(v[14:16]), ra=be(v[17:20]), rb=be(v[21:24]),
            zero=bytes(v[3:4] + v[8:9] + v[12:14] + v[16:17] + v[20:21]),
            dcache=le(v[24:30]), dvalid=v[30],
            lc=le(c[0:3]), len=le(c[3:5]), per=le(c[5:7]), vol=c[7], on=c[8],
            clc=le(c[9:12]), clen=le(c[12:14]),
            dl=le(c[16:20]) << 16 | le(c[14:16]), nb=le(c[20:22]),
            lt=le(c[22:24]), np=c[24], le=le(c[25:29])))
    return out


def ref_voice(v: PR.Voice) -> dict:
    """The ref's voice as the module's image would hold it."""
    rep = v.repeat
    return dict(ctl=(1 | (2 if rep else 0)) if v.programs else 0,
                start=v.start, length=v.length, rate=v.rate, vvol=v.vol,
                ra=rep[0] if rep else 0, rb=rep[1] if rep else 0)


def cmp_chan(tag: str, m: dict, c: PR.Chan, v: PR.Voice, f: list[str],
             dl: bool = True, voice: bool = True) -> None:
    want = dict(lc=c.lc, len=c.len, per=c.per, vol=c.vol & 0xFF, on=int(c.on),
                clc=c.cur_lc, clen=c.cur_len, nb=c.nblk & 0xFFFF,
                np=v.programs & 0xFF)
    if dl:
        want["dl"] = c.dl & (1 << 48) - 1
    if voice:
        want.update(ref_voice(v))
        if v.programs:
            want["pan"] = v.pan
    for k, w in want.items():
        if m[k] != w:
            f.append(f"{tag}: {k} = {m[k]:#x}, ref {w:#x}")
    if m["zero"] != bytes(len(m["zero"])):
        f.append(f"{tag}: the image's unused bytes are not 0: {m['zero'].hex()}")


# --------------------------------------------------------------------------
# The VICE rig
# --------------------------------------------------------------------------

LOG_BUILD = True               # VICE: the module's log build (M5's sampler log)


class Hang(Exception):
    """drv_run did not come back: the guest (and the module under it) never
    halted.  A named check failure, not an infrastructure exception."""


class Rig:
    log = LOG_BUILD

    def __init__(self, target, labels, prg: str, src: str | None = None):
        from tests.harness_util import lookup_label
        from tests.run_program import Session
        self.tg, self.t = target, target.transport
        self.labels, self.prg = labels, prg
        self.lab = lambda n: lookup_label(labels, n)
        self.sess = Session(target=target, labels=labels, timeout=300.0)
        d = os.path.join(os.path.dirname(os.path.abspath(prg)), "paula")
        lbl = os.path.splitext(prg)[0] + ".lbl"
        try:
            self.lab("aud_mon")
            self.has_hook = True
        except KeyError:            # the base PRG (red): no router
            self.has_hook = False
        if self.has_hook:
            self.drv, self.dlab = build_drv(lbl, d)
            defs = (f"AUD_LOG=${self.dlab['aud_log']:04X}",) if self.log else ()
            self.a, self.b, self.alab = build_aud(lbl, d, src, defs)
        self.r16 = PR.R16_AMIGA
        self.hazards: list[str] = []
        try:                        # W13-I: idle scripted input (no CIA1 scan)
            self.w(self.lab("inp_src"), bytes([1, 0, 0]))
        except KeyError:
            pass

    def call(self, addr: int, timeout: float = 300.0) -> None:
        from tests.harness_util import call_runner
        call_runner(self.tg, addr, timeout)

    guest_read = run_mach.Rig.guest_read

    def w(self, addr: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, addr, data)

    def r(self, addr: int, n: int, seg_b: bool = False) -> bytes:
        """Segment B lives in the text screen: once it is staged, nothing
        but the module checks (seg_b=True) may read there (supervisor)."""
        from c64_test_harness import read_bytes
        if getattr(self, "installed", False) and not seg_b and \
                addr < AUD_B_END and addr + n > AUD_B:
            raise RuntimeError(f"read of ${addr:04X}+{n} overlaps segment B "
                               "(the text screen) after staging")
        return read_bytes(self.t, addr, n)

    def install(self) -> None:
        """The driver and both module segments (staging can overwrite
        buf_case, so every stage re-installs them)."""
        self.w(DRV_ORG, self.drv)
        self.w(AUD_A, self.a)
        self.w(AUD_B, self.b)
        self.installed = True

    def stage(self, prog: list[int]) -> int:
        chunks, (pc,) = build_image(prog)
        self.sess.stage(chunks)
        self.install()
        return pc

    def cfg(self, r16: int | None = None) -> None:
        """VICE: the log build writes its "sampler" into the driver's logger
        (RAM), so cfg_hw = 1 is safe there (never $DF20: the REC mirror)."""
        self.r16 = r16 or PR.R16_AMIGA
        self.w(AUD_A + 13, struct.pack("<HB", self.r16, 1 if self.log else 0)
               + DISP_ZERO.to_bytes(3, "little"))

    def run(self, pc: int, s: int = 4, arm: int | None = None,
            init: bool = True) -> dict:
        mid = self.a[3] if arm is None else arm
        self.w(P, struct.pack("<IIBBBB", pc, SSP0, s, mid, 0x37, int(init)))
        self.w(R, b"\0")
        if self.log:
            self.w(0xC400, bytes(128))
            self.w(self.dlab["lg_n"], bytes(8))
        try:
            self.call(self.dlab["drv_run"])
        except Exception as e:                  # the harness's own TimeoutError class
            if "Timeout" not in type(e).__name__:
                raise
            raise Hang(f"drv_run did not return (the guest never halted): {e}") from e
        if self.log:
            n, bad, first, live = struct.unpack("<HHHH", self.r(self.dlab["lg_n"], 8))
            self.last_log = (n, bad, first)
            if bad:
                self.hazards.append(f"{bad} sampler state(s) with repeat off and "
                                    f"REPEAT B == LENGTH (first at command {first} of {n})")
            if live:
                self.hazards.append(f"{live} START/LENGTH write(s) to a voice not stopped "
                                    "(CTRL bit 0 set): it can run on past a shorter LENGTH")
        r = self.r(R, 60)
        if r[0] != 0xFF:
            raise RuntimeError("drv_run did not complete")
        u32 = lambda o: struct.unpack_from("<I", r, o)[0]
        return {"halt": r[1], "reason": r[2], "frames": u32(4), "e1": u32(8),
                "nirq": u32(12), "d": list(struct.unpack_from("<8I", r, 16)),
                "pc": u32(48), "intreqr": be(r[52:54]), "intenar": be(r[54:56]),
                "e0": u32(56)}

    def state(self) -> tuple[list[dict], dict]:
        page = self.r(VC, 256)
        hdr = self.r(AUD_A + 13, 18)
        st = dict(r16=le(hdr[0:2]), hw=hdr[2], zero=le(hdr[3:6]),
                  nw=le(hdr[6:8]), nfire=le(hdr[8:10]), ndat=hdr[10],
                  now=le(hdr[11:15]), nres=hdr[15], npark=le(hdr[16:18]))
        return parse(page), st

    def intact(self, f: list[str], tag: str) -> None:
        """Both segments still hold what was staged (B is screen RAM)."""
        if self.r(AUD_B, len(self.b), seg_b=True) != self.b:   # B: no scratch
            f.append(f"{tag}: segment B ($0400) changed under the run")
        a = bytearray(self.r(AUD_A, len(self.a)))
        a[13:31] = self.a[13:31]                # config + counters
        lo = self.alab["tA"] - AUD_A            # the scratch (tA first): anything
        a[lo:] = self.a[lo:]
        if bytes(a) != self.a:
            f.append(f"{tag}: segment A code changed under the run")


def logcheck(rig: "Rig", f: list[str], tag: str) -> None:
    """M5 over every run of a leg (the log build): no hazard state, and the
    logged sampler registers equal the module's voice image (+0..+$17) for
    every voice programmed since init."""
    if not getattr(rig, "log", False) or not rig.has_hook:
        return
    for h in rig.hazards:
        f.append(f"{tag}: M5: {h}")
    rig.hazards = []
    mirror = rig.r(0xC400, 128)
    page = rig.r(VC, 256)
    for x in range(4):
        if page[128 + 32 * x + 24] == 0:        # never programmed since init
            continue
        a, b = mirror[32 * x:32 * x + 24], page[32 * x:32 * x + 24]
        if a != b:
            f.append(f"{tag}: voice {x}: the sampler got {a.hex()}, the image is {b.hex()}")


def clean(res: dict, tag: str, f: list[str]) -> bool:
    if res["halt"] != 1 or res["reason"] != 0:
        f.append(f"{tag}: halt {res['halt']:#x} reason {res['reason']:#x}, "
                 f"PC {res['pc']:#x}")
        return False
    return True


# --------------------------------------------------------------------------
# Guest programs
# --------------------------------------------------------------------------

def setch(g: G, ch: int, lc: int, ln: int, per: int, vol: int) -> G:
    mv_l_imm_abs(g, lc, aud(ch, PR.AUD_LCH))
    mv_imm_abs_w(g, ln, aud(ch, PR.AUD_LEN))
    mv_imm_abs_w(g, per, aud(ch, PR.AUD_PER))
    return mv_imm_abs_w(g, vol, aud(ch, PR.AUD_VOL))


def busy(g: G, n: int, tag: str, ack: bool = False) -> G:
    """n x 64K-ish iterations: dbra d3 inside dbra d4; *ack*: each one
    acknowledges AUD0-3 (INTREQ CLR $0780), so no channel parks."""
    g(0x383C, n - 1)                            # move.w #n-1,d4
    g.label(tag + "o")
    g(0x363C, 0xFFFF)                           # move.w #$FFFF,d3
    g.label(tag)
    if ack:
        mv_imm_abs_w(g, 0x0780, INTREQ)
    g.dbra(3, tag)
    return g.dbra(4, tag + "o")


def ref_writes(p: PR.Paula, now: int, writes: list[tuple[int, int]],
               dmacon: int = 0) -> int:
    """Replay (address, word) writes at E time *now*; returns DMACON."""
    for a, v in writes:
        off = a - CUSTOM
        if off == 0x096:
            dmacon = (dmacon | v & 0x7FFF) if v & 0x8000 else dmacon & ~v
            p.write(now, off, dmacon)
        elif off in (PR.AUD_BASE + 16 * x + r for x in range(4) for r in (0,)):
            p.write(now, off, v >> 16)
            p.write(now, off + 2, v & 0xFFFF)
        else:
            p.write(now, off, v)
    return dmacon


def emit(g: G, writes: list[tuple[int, int]]) -> G:
    for a, v in writes:
        if (a - CUSTOM - 0xA0) % 16 == 0 and 0xA0 <= a - CUSTOM < 0xE0:
            mv_l_imm_abs(g, v, a)               # AUDxLC as one long
        else:
            mv_imm_abs_w(g, v, a)
    return g


def chw(ch: int, lc: int, ln: int, per: int, vol: int) -> list[tuple[int, int]]:
    return [(aud(ch, 0), lc), (aud(ch, 4), ln), (aud(ch, 6), per), (aud(ch, 8), vol)]


# --------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------

def need_hook(rig: Rig, tag: str) -> list[str]:
    return [] if rig.has_hook else [f"{tag}: the PRG has no audio router (aud_mon)"]


def leg_latch(rig: Rig) -> list[str]:
    f = need_hook(rig, "latch")
    if f:
        return f
    vols = (10, 64, 0x7F, 0x41)
    lens = (0x1000, 0x1001, 0x1002, 0)          # ch3: LEN 0 = 65536 words
    lcs = (SAMPLE, SAMPLE + 0x1000, 0x180000, SAMPLE + 0x3000)   # ch2: LC bit 20
    first = sum((chw(x, lcs[x], lens[x], 0x1000 + 7 * x, vols[x])
                 for x in range(4)), [])
    first += [(DMACON, 0x7FFF), (DMACON, 0x820F)]
    second = sum((chw(x, SAMPLE + 0x8001 + 0x100 * x, 0x20 + x, 0x1000 + 7 * x,
                      vols[x]) for x in range(4)), [])
    g = G()
    emit(g, first)
    emit(g, second)
    mv_abs_d(g, "w", INTREQR, 0)
    mv_abs_d(g, "w", DMACONR, 1)
    g(HALT)
    for r16 in (PR.R16_AMIGA, PR.R16_U64_S0):
        tag = f"latch r16={r16}"
        pc = rig.stage(g.words())
        rig.cfg(r16)
        res = rig.run(pc, s=4)
        if not clean(res, tag, f):
            continue
        m, st = rig.state()
        p = PR.Paula(r16=r16, zero_reu=DISP_ZERO)
        t0 = m[0]["le"]                         # DMA start: AUD0-3's post
        dm = ref_writes(p, t0, first)
        ref_writes(p, st["now"], second, dm)    # (PER rewritten: a rescale)
        for x in range(4):
            cmp_chan(f"{tag} ch{x}", m[x], p.ch[x], p.vc[x], f)
            if m[x]["le"] != t0:
                f.append(f"{tag} ch{x}: post E {m[x]['le']}, ch0's {t0}")
        if not t0 <= st["now"]:
            f.append(f"{tag}: st_now {st['now']} before DMA start {t0}")
        if res["d"][0] & 0x0780 != 0x0780:
            f.append(f"{tag}: INTREQR {res['d'][0]:#06x}: AUD0-3 not posted")
        if res["d"][1] & 0x020F != 0x020F:
            f.append(f"{tag}: DMACONR {res['d'][1]:#06x}")
        if st["nw"] != len(first) + len(second) + 4 + 4:   # LC = 2 word writes
            f.append(f"{tag}: st_nw {st['nw']}")
        rig.intact(f, tag)
    return f


def leg_blocks(rig: Rig) -> list[str]:
    f = need_hook(rig, "blocks")
    if f:
        return f
    s = 4
    pers = (250, 333, 401, 517)
    lens = (80, 72, 56, 48)     # ~8000-12000 E: a 1 MHz host keeps up
    w = sum((chw(x, SAMPLE + 0x400 * x, lens[x], pers[x], 64) for x in range(4)), [])
    w += [(DMACON, 0x820F)]
    g = G()
    emit(g, w)
    busy(g, 1, "b", ack=True)
    g(HALT)
    pc = rig.stage(g.words())
    for r16 in (PR.R16_AMIGA, PR.R16_U64_S0):
        tag = f"blocks r16={r16}"
        rig.cfg(r16)
        res = rig.run(pc, s=s)
        if not clean(res, tag, f):
            continue
        m, st = rig.state()
        bound = (SLACK_PHI2 + FIRE_PHI2) >> s
        t0s, lates = set(), []
        for x in range(4):
            p = PR.Paula(r16=r16)
            c = p.ch[x]
            c.cur_len, c.per = lens[x], pers[x]
            dur = p.dur(c)
            mx = m[x]
            base_ = mx["dl"] - mx["nb"] * dur
            if base_ % 65536:
                f.append(f"{tag} ch{x}: dl - n * block is not a whole E tick")
            t0s.add(base_ // 65536)
            if mx["nb"] < 3:
                f.append(f"{tag} ch{x}: only {mx['nb']} blocks")
            # lateness: the LAST post only (the per-channel maximum went for
            # space in fix round 1; rev15-paula L4)
            prev_due = PR.Paula.due(mx["dl"] - dur)
            lates.append(mx["le"] - prev_due)
            if not 0 <= mx["le"] - prev_due <= bound:
                f.append(f"{tag} ch{x}: last post at {mx['le']}, due {prev_due}")
            if res["e1"] - PR.Paula.due(mx["dl"]) > bound:
                f.append(f"{tag} ch{x}: overdue at the end")
            if mx["np"] != 1 or mx["ctl"] != 3:
                f.append(f"{tag} ch{x}: np {mx['np']} ctl {mx['ctl']} (same block: "
                         "one program, hardware loop)")
        if len(t0s) != 1:
            f.append(f"{tag}: channels disagree on DMA start: {sorted(t0s)}")
        if st["nres"] or st["npark"]:
            f.append(f"{tag}: {st['nres']} grid restarts, {st['npark']} parks "
                     "(blocks long enough to keep up; every post acknowledged)")
        if st["nfire"] < sum(mx["nb"] - 1 for mx in m) // 4:
            f.append(f"{tag}: st_nfire {st['nfire']}")
        if VERBOSE:
            print(f"  {tag}: blocks {[mx['nb'] for mx in m]} last post late {lates}"
                  f" fires {st['nfire']} restarts {st['nres']} bound {bound}")
        rig.intact(f, tag)
    return f


def leg_irq(rig: Rig) -> list[str]:
    f = need_hook(rig, "irq")
    if f:
        return f
    g = G()
    vec(g, 28, "h4")                            # level 4 autovector
    g(0x42B9, *g.abs(CNT))                      # clr.l CNT
    emit(g, chw(0, SAMPLE, 12, 300, 40))
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, 0xC080, INTENA)             # INTEN + AUD0
    mv_imm_abs_w(g, 0x8201, DMACON)
    move_sr(g, 0x2000)
    busy(g, 1, "b")
    move_sr(g, 0x2700)
    mv_abs_d(g, "w", INTREQR, 0)
    g(HALT)
    g.label("h4")
    addq_l_abs(g, CNT)
    mv_imm_abs_w(g, 0x0080, INTREQ)
    g(0x4E73)                                   # rte
    pc = rig.stage(link(g, BASE))
    rig.cfg(PR.R16_AMIGA)
    res = rig.run(pc, s=4)
    if not clean(res, "irq", f):
        return f
    m, st = rig.state()
    cnt = be(rig.guest_read(CNT, 4))
    nb, pend = m[0]["nb"], bool(res["d"][0] & 0x80)
    if cnt + pend != nb:
        f.append(f"irq: handler ran {cnt}x (+{int(pend)} pending) for {nb} blocks")
    if res["nirq"] != cnt:
        f.append(f"irq: mach_nirq delta {res['nirq']} != handler count {cnt}")
    if nb < 5:
        f.append(f"irq: only {nb} blocks")
    if VERBOSE:
        print(f"  irq: blocks {nb} handler {cnt} pending {pend} "
              f"fires {st['nfire']} frames {res['frames']}")
    return f


def leg_reload(rig: Rig) -> list[str]:
    f = need_hook(rig, "reload")
    if f:
        return f
    a0, a1, a2, a3 = SAMPLE, SAMPLE + 0x1000, SAMPLE + 0x2000, SAMPLE + 0x4000
    # PER 1200: 7680 E first blocks, so every latch write lands inside the
    # first block (a block end between an LC and its LEN write reloads the
    # half-written pair, as on Paula: real, but the ref writes in zero time)
    first = (chw(0, a0, 16, 1200, 64) + chw(1, a1, 16, 1200, 64)
             + chw(2, a2, 16, 1200, 64) + chw(3, a3, 16, 1200, 64) + [(DMACON, 0x820F)])
    second = [(aud(0, 0), SAMPLE + 0x3000), (aud(0, 4), 8),       # elsewhere
              (aud(1, 0), a1 + 8), (aud(1, 4), 12),               # the tail
              (aud(2, 0), 0), (aud(2, 4), 1),                      # null loop
              (aud(3, 0), a3 + 8), (aud(3, 4), 11)]   # ends 2 B short of the span

    g = G()
    emit(g, first)
    emit(g, second)
    busy(g, 1, "b", ack=True)
    g(HALT)
    pc = rig.stage(g.words())
    rig.cfg(PR.R16_AMIGA)
    res = rig.run(pc, s=4)
    if not clean(res, "reload", f):
        return f
    m, st = rig.state()
    for x in range(4):
        p = PR.Paula(r16=PR.R16_AMIGA, zero_reu=DISP_ZERO)
        ref_writes(p, 0, second, ref_writes(p, 0, first))
        if x == 1 and p.vc[1].repeat != (8, 32):
            f.append(f"reload: (ref) ch1 repeat {p.vc[1].repeat}")
        while p.ch[x].nblk < m[x]["nb"]:        # the ref's blocks to the count
            p.fire(p.due(p.ch[x].dl))
        cmp_chan(f"reload ch{x}", m[x], p.ch[x], p.vc[x], f, dl=False)
        if m[x]["nb"] < 3:
            f.append(f"reload ch{x}: only {m[x]['nb']} blocks")
    want_np = (2, 1, 2, 2)
    for x in range(4):
        if m[x]["np"] != want_np[x]:
            f.append(f"reload ch{x}: {m[x]['np']} programs, want {want_np[x]}")
    rig.intact(f, "reload")
    return f


def leg_pervol(rig: Rig) -> list[str]:
    """PER and VOL mid-block: the voice's RATE and VOL change at once; the
    block keeps its end (W15-P deviation: no rescale); the next block takes
    the new PER (the cache is invalidated)."""
    f = need_hook(rig, "pervol")
    if f:
        return f
    for new in (0x200, 0x900, 0):
        tag = f"pervol {new:#x}"
        w0 = chw(0, SAMPLE, 0x1000, 0x400, 10) + [(DMACON, 0x8201)]
        g = G()
        emit(g, w0)
        g(0x4E71, 0x4E71)                       # nop nop
        mv_imm_abs_w(g, new, aud(0, 6))
        mv_imm_abs_w(g, 64, aud(0, 8))
        g(HALT)
        pc = rig.stage(g.words())
        rig.cfg(PR.R16_AMIGA)
        res = rig.run(pc, s=4)
        if not clean(res, tag, f):
            continue
        m, st = rig.state()
        p = PR.Paula(r16=PR.R16_AMIGA, zero_reu=DISP_ZERO)
        t0 = m[0]["le"]
        ref_writes(p, t0, w0)
        p.write(t0 + 1, 0xA6, new)
        p.write(t0 + 1, 0xA8, 64)
        cmp_chan(tag, m[0], p.ch[0], p.vc[0], f)
        if m[0]["dvalid"]:
            f.append(f"{tag}: the block-length cache is still valid after PER")
    return f



def clock_t0(m: dict, ln: int, per: int, r16: int) -> int:
    """DMA start (E * 65536) from a clock channel that never ended its
    first block (LEN ln, PER per): dl - one block."""
    p = PR.Paula(r16=r16)
    p.ch[0].cur_len, p.ch[0].per = ln, per
    return m["dl"] - p.dur(p.ch[0])


def leg_lencache(rig: Rig) -> list[str]:
    """A LEN latch write mid-block invalidates the cached block length: the
    second block (and the ones after) last 2 x 32 x PER, the first 2 x 16 x
    PER; the deadline equals the ref's exactly (t0 from a clock channel)."""
    f = need_hook(rig, "lencache")
    if f:
        return f
    # PER 2000: 12800 E first block, so the LEN write lands inside it (the
    # setup writes take ~2400 E at s = 4 on VICE)
    w0 = (chw(0, SAMPLE, 16, 2000, 64) + chw(1, SAMPLE, 0xFFFF, 0xFFFF, 0)
          + [(DMACON, 0x8203)])
    w1 = [(aud(0, 4), 32)]
    g = G()
    emit(g, w0)
    emit(g, w1)
    busy(g, 1, "b", ack=True)
    g(HALT)
    pc = rig.stage(g.words())
    rig.cfg(PR.R16_AMIGA)
    res = rig.run(pc, s=4)
    if not clean(res, "lencache", f):
        return f
    m, st = rig.state()
    t0 = clock_t0(m[1], 0xFFFF, 0xFFFF, PR.R16_AMIGA)
    if t0 % 65536:
        f.append("lencache: the clock channel's start is not a whole E tick")
    p = PR.Paula(r16=PR.R16_AMIGA, zero_reu=DISP_ZERO)
    dm = ref_writes(p, t0 // 65536, w0)
    ref_writes(p, t0 // 65536, w1, dm)
    while p.ch[0].nblk < m[0]["nb"]:
        p.fire(p.due(p.ch[0].dl))
    cmp_chan("lencache ch0", m[0], p.ch[0], p.vc[0], f)
    if m[0]["nb"] < 3:
        f.append(f"lencache: only {m[0]['nb']} blocks")
    return f


def disp_block(rig: Rig) -> bytes:
    """disp_st (cop.s's display state, RAM under BASIC ROM: $01 = $34
    around the read, as tests/run_cop.py)."""
    t = rig.t
    port = rig.r(1, 1)
    t.write_memory(0x0001, bytes([0x34]), override="paula: RAM under ROM")
    try:
        return rig.r(0xB800, 64)
    finally:
        t.write_memory(0x0001, port, override="paula: RAM under ROM")


def leg_bplpt(rig: Rig) -> list[str]:
    """BPL1PT-BPL4PT ($0E0-$0EE) written with the module armed reach cop.s
    unchanged (W15-U's copper list writes them every frame): disp_st equals
    the unarmed run's, and carries the pointers."""
    f = need_hook(rig, "bplpt")
    if f:
        return f
    g = G()
    for k, v in enumerate((0x012344, 0x056788, 0x09ABCC, 0x0DEF00)):
        mv_l_imm_abs(g, v, CUSTOM + 0xE0 + 4 * k)
    mv_imm_abs_w(g, 0x8201, DMACON)             # an audio write in between
    mv_l_imm_abs(g, 0x112230, CUSTOM + 0xE0)
    g(HALT)
    pc = rig.stage(g.words())
    rig.cfg(PR.R16_AMIGA)
    blocks, nw = {}, {}
    for tag, arm in (("armed", None), ("off", 0)):
        res = rig.run(pc, s=4, arm=arm)
        if not clean(res, f"bplpt {tag}", f):
            return f
        blocks[tag] = disp_block(rig)[1:]       # (+0: cop's generation count)
        nw[tag] = rig.state()[1]["nw"]
    if blocks["armed"] != blocks["off"]:
        d = [i + 1 for i in range(63) if blocks["armed"][i] != blocks["off"][i]]
        f.append(f"bplpt: disp_st differs armed vs not at bytes {d[:12]}")
    if blocks["off"][8:11] == bytes(3):
        f.append("bplpt: (test) BPL1PT did not reach disp_st at all")
    st = {"nw": nw["armed"]}
    if st["nw"] != 1:
        f.append(f"bplpt: the module counted {st['nw']} writes (the DMACON only)")
    return f


def leg_phase(rig: Rig) -> list[str]:
    """M6: a parked looping channel gets a new instrument without a note
    (LC/LEN only, not contiguous: the hardware loop is dropped).  The switch
    (the 6510's reprogram) comes at the first grid point of the old block
    after the write, within the seam bound.  ch1: a clock (t0); ch2: started
    right after the write (its post marks the write's E)."""
    f = need_hook(rig, "phase")
    if f:
        return f
    s = 4
    ln0, per0 = 2, 300              # 240 E blocks: a parked deadline is 32 ahead
    w0 = (chw(0, SAMPLE, ln0, per0, 64) + chw(1, SAMPLE, 0xFFFF, 0xFFFF, 0)
          + chw(2, SAMPLE, 0xFFFF, 0xFFFF, 0) + [(DMACON, 0x8203)])
    g = G()
    emit(g, w0)
    busy(g, 1, "a")                             # never acknowledged: ch0 parks
    # the new instrument: LC only, in the same 64 KB (only the LCL write
    # changes the latches: one downgrade, one switch), the same LEN
    emit(g, [(aud(0, 0), SAMPLE + 0x2000)])
    mv_imm_abs_w(g, 0x8204, DMACON)             # ch2 starts: marks the write
    busy(g, 1, "b")
    g(HALT)
    pc = rig.stage(g.words())
    rig.cfg(PR.R16_AMIGA)
    res = rig.run(pc, s=s)
    if not clean(res, "phase", f):
        return f
    m, st = rig.state()
    p = PR.Paula(r16=PR.R16_AMIGA)
    c = p.ch[0]
    c.cur_len, c.per = ln0, per0
    blk = p.dur(c)
    blk_new = blk
    t0 = clock_t0(m[1], 0xFFFF, 0xFFFF, PR.R16_AMIGA)
    tw = m[2]["le"] * 65536                     # ch2's post: just after the write
    # the switch: ch0's last post; its block start ts is on the old grid
    i = (m[0]["le"] * 65536 - t0) // blk         # the grid point at or before the post
    ts = t0 + i * blk
    bound = (SLACK_PHI2 + FIRE_PHI2) >> s
    if st["npark"] < 1:
        f.append("phase: ch0 never parked before the change")
    if m[0]["np"] != 2 or m[0]["clc"] != SAMPLE + 0x2000 or m[0]["clen"] != ln0:
        f.append(f"phase: ch0 not switched: np {m[0]['np']} lc {m[0]['clc']:#x} "
                 f"len {m[0]['clen']:#x}")
    if (m[0]["dl"] - ts) % blk_new:
        f.append("phase: after the switch the deadline is not on the new block's grid "
                 "from the switch")
    if not 0 <= m[0]["le"] - PR.Paula.due(ts) <= bound:
        f.append(f"phase: the switch was posted at {m[0]['le']}, its grid point is "
                 f"{PR.Paula.due(ts)} (bound {bound}): not on the old grid")
    lead = 3000                                 # E: the write's own processing + 2 instructions
    if not (tw - lead * 65536 < ts <= tw + blk):
        f.append(f"phase: the switch at {ts / 65536:.1f} E is not the first grid point "
                 f"after the write ({tw / 65536:.1f} E; one block = {blk / 65536:.1f} E)")
    if VERBOSE:
        print(f"  phase: parks {st['npark']}, write ~{tw / 65536:.0f}, switch "
              f"{ts / 65536:.1f} (grid {i}), post {m[0]['le']}, block {blk / 65536:.1f}")
    return f


def leg_park(rig: Rig) -> list[str]:
    """A 1-word loop (the null loop after a one-shot sample) nobody
    acknowledges: the channel parks (one look per PARK_E, the guest keeps
    its speed); after one acknowledge AUD0 is posted again within a frame."""
    f = need_hook(rig, "park")
    if f:
        return f
    g = G()
    emit(g, chw(0, 0, 1, 428, 0))               # LEN 1 at chip 0: 2 samples
    mv_imm_abs_w(g, 0x7FFF, INTREQ)
    mv_imm_abs_w(g, 0x8201, DMACON)
    busy(g, 1, "a")
    mv_abs_d(g, "w", INTREQR, 0)                # pending (never acknowledged)
    mv_imm_abs_w(g, 0x0080, INTREQ)             # acknowledge AUD0 once
    mv_abs_d(g, "w", INTREQR, 1)
    busy(g, 1, "b")                             # >> one frame
    mv_abs_d(g, "w", INTREQR, 2)                # posted again
    g(HALT)
    pc = rig.stage(g.words())
    rig.cfg(PR.R16_AMIGA)
    res = rig.run(pc, s=4)
    if not clean(res, "park", f):
        return f
    m, st = rig.state()
    quiet = rig.run(pc, s=4, arm=0)             # the same guest, no module
    share = (quiet["e1"] - quiet["e0"]) / max(res["e1"] - res["e0"], 1)
    d = res["d"]
    if not d[0] & 0x80:
        f.append(f"park: AUD0 not pending before the acknowledge: {d[0]:#06x}")
    if not d[2] & 0x80:
        f.append(f"park: AUD0 not posted again after the acknowledge: {d[2]:#06x}")
    if st["npark"] < 2:
        f.append(f"park: {st['npark']} parks")
    if share < 0.9:
        f.append(f"park: the guest ran at {share:.2f} of its speed (a 1-word loop "
                 "must cost one look per frame)")
    if m[0]["nb"] > 8:
        f.append(f"park: {m[0]['nb']} blocks serviced (want the first, one after "
                 "the acknowledge, and a few before parking)")
    # the look interval: the last look (st_now: fire's clock) to the next
    # deadline: 2^k blocks >= PARK_E (half a frame), so < one frame + a block
    nxt = PR.Paula.due(m[0]["dl"])
    gap = nxt - st["now"]
    q = PR.Paula(r16=PR.R16_AMIGA)
    q.ch[0].cur_len, q.ch[0].per = 1, 428
    blk = -(-q.dur(q.ch[0]) // 65536)
    if not 7094 - 1100 <= gap <= 14188 + blk:
        f.append(f"park: the next look comes {gap} E after the last (want 2^k blocks "
                 f">= 7094 E, at most one frame + a block = {14188 + blk})")

    if VERBOSE:
        print(f"  park: nb {m[0]['nb']} parks {st['npark']} share {share:.3f} "
              f"INTREQR {d[0]:#06x} {d[1]:#06x} {d[2]:#06x}")
    return f


def leg_dmaoff(rig: Rig) -> list[str]:
    f = need_hook(rig, "dmaoff")
    if f:
        return f
    w0 = sum((chw(x, SAMPLE + 0x100 * x, 0x800, 0x300, 30) for x in range(4)), [])
    steps = [w0 + [(DMACON, 0x820F)], [(DMACON, 0x0002)], [(DMACON, 0x0200)],
             [(aud(3, 0), SAMPLE + 0x5000), (DMACON, 0x8200)]]
    g = G()
    for st_ in steps:
        emit(g, st_)
    g(HALT)
    pc = rig.stage(g.words())
    rig.cfg(PR.R16_AMIGA)
    res = rig.run(pc, s=4)
    if not clean(res, "dmaoff", f):
        return f
    m, st = rig.state()
    p = PR.Paula(r16=PR.R16_AMIGA, zero_reu=DISP_ZERO)
    t1 = st["now"]
    dm = 0
    for i, st_ in enumerate(steps):
        # the times: the first start = ch1's post (it never restarts), the
        # restart = st_now (the last DMACON)
        dm = ref_writes(p, m[1]["le"] if i == 0 else t1, st_, dm)
    for x in range(4):
        cmp_chan(f"dmaoff ch{x}", m[x], p.ch[x], p.vc[x], f)
    if m[1]["on"] or m[1]["start"] != PR.REU_BASE + DISP_ZERO or m[1]["length"] != 2:
        f.append(f"dmaoff: ch1 not silenced: {m[1]}")
    if m[0]["nb"] != 2 or m[0]["clc"] != SAMPLE:
        f.append(f"dmaoff: ch0 restart: nb {m[0]['nb']} lc {m[0]['clc']:#x}")
    if m[3]["clc"] != SAMPLE + 0x5000:
        f.append(f"dmaoff: ch3 restart did not re-latch: {m[3]['clc']:#x}")
    am = rig.r(rig.lab("mach_am"), 1)[0]
    if am & 0x40 and not any(mx["on"] for mx in m):
        f.append("dmaoff: slot 6 armed with nothing playing")
    return f


def leg_nomod(rig: Rig) -> list[str]:
    f: list[str] = []
    w = chw(0, SAMPLE, 0x100, 0x200, 20) + [(DMACON, 0x8201)]
    g = G()
    emit(g, w)
    mv_abs_d(g, "w", DMACONR, 1)
    g(HALT)
    if not rig.has_hook:
        return ["nomod: the PRG has no audio router (aud_mon)"]
    # B corrupt: a stale B (its ID differs); B missing: never staged (zeros).
    # Both must disarm (aud_mon := 0) before any jump into B.
    cases = [("off", 0, None), ("wrong ID", rig.a[3] ^ 0x5A or 1, None),
             ("B corrupt", None, bytes([rig.b[0] ^ 0xFF])),
             ("B missing", None, bytes(len(rig.b)))]
    for tag, arm, bdata in cases:
        pc = rig.stage(g.words())
        rig.cfg(PR.R16_AMIGA)
        if bdata is not None:
            rig.w(AUD_B, bdata)
        res = rig.run(pc, s=4, arm=arm)
        if not clean(res, f"nomod {tag}", f):
            continue
        m, st = rig.state()
        if st["nw"] or any(mx["on"] or mx["nb"] or mx["lc"] for mx in m):
            f.append(f"nomod {tag}: the module ran: nw {st['nw']}, ch0 {m[0]}")
        if res["d"][1] & 0x0201 != 0x0201:
            f.append(f"nomod {tag}: DMACONR {res['d'][1]:#06x}")
        if bdata is not None and rig.r(rig.lab("aud_mon"), 1)[0]:
            f.append(f"nomod {tag}: aud_mon still armed")
        if bdata is not None and rig.log:     # the stale-B stop reached every voice
            mir = rig.r(0xC400, 128)
            for x in range(4):
                if mir[32 * x] != 0 or mir[32 * x + 0x15] != 0xFF:
                    f.append(f"nomod {tag}: voice {x} not stopped (CTRL {mir[32 * x]:#04x}, "
                             f"B high {mir[32 * x + 0x15]:#04x}; want 0 and $FF)")
            for h in rig.hazards:
                f.append(f"nomod {tag}: {h}")
        rig.hazards = []
    return f


LEGS = {"latch": leg_latch, "blocks": leg_blocks, "irq": leg_irq,
        "reload": leg_reload, "lencache": leg_lencache, "pervol": leg_pervol,
        "park": leg_park, "phase": leg_phase, "bplpt": leg_bplpt,
        "dmaoff": leg_dmaoff, "nomod": leg_nomod}


# --------------------------------------------------------------------------
# Session, mutants, main
# --------------------------------------------------------------------------

def session(prg: str, fn, src: str | None = None):
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
            return fn(Rig(tg, labels, prg, src))
        finally:
            mgr.release(tg)


def run_all(prg: str, legs: list[str], src: str | None = None) -> list[str]:
    def body(rig: Rig) -> list[str]:
        fails: list[str] = []
        for name in legs:
            try:
                f = LEGS[name](rig)
                if name != "nomod":
                    logcheck(rig, f, name)
            except Hang as e:
                f = [f"{name}: HANG {e}"]
                fails += f
                print(f"[{name}] FAIL")
                print(f"  {f[0]}")
                break                           # the C64 is stuck in mach_run
            except Exception as e:
                f = [f"{name}: EXC {type(e).__name__}: {e}"]
            rig.hazards = []
            print(f"[{name}] {'PASS' if not f else 'FAIL'}")
            for line in f:
                print(f"  {line}")
            fails += f
        return fails
    return session(prg, body, src)


# Module mutants (text replacements in tests/paula/aud.s) and the legs that
# must catch each.
MUTANTS = {
    "nolatch": ([("lda CH+C_LC,y           ; LC (3), LEN (2) -> the playing block",
                  "lda CH+C_CLC,y          ; mutant")], ["latch"]),
    # (len0 and nopark retired to REVIEW_MUTANTS for nostop and bokstop)
    "nostop": ([("        lda #0                  ; a real stop (CTRL 2 does not stop a\n        SPUT                    ; playing voice [MEASURED HW])",
                 "        lda #0\n        nop\n        nop\n        nop")], ["reload"]),
    "bokstop": ([("        lda cfg_hw              ; stop every voice", "        lda #0                  ; stop every voice")],
                ["nomod"]),
    # (vol64 retired for nobok, supervisor's ruling; the VOL cap stays checked
    # by leg latch against the ref and by the U64E leg vol)
    "nobok": ([("        cmp bid,x\n        bne :+", "        cmp bid,x\n        nop\n        nop")],
              ["nomod"]),
    "rateoff": ([("RATE_C   = 20761805", "RATE_C   = 20761805 - 16777216")], ["latch"]),
    "dmaen": ([("and #$02                ; DMAEN (bit 9)", "ora #$02                ; mutant")],
              ["dmaoff"]),
    "nosilence": ([("        sta CH+C_ON,y\n        jsr silence",
                    "        sta CH+C_ON,y\n        nop\n        nop\n        nop")], ["dmaoff"]),
    "parkall": ([("        lda tb\n:       lsr a                   ; C = this channel's bit",
                  "        lda #$FF\n:       lsr a                   ; mutant")], ["irq"]),
    # (noirq retired for m5order: leg latch's INTREQR check kills it too)
    # the reprogram without "B beyond LENGTH" first: CTRL 0 with B == LENGTH
    "m5order": ([("        jsr rbff                ; B := $FFFFFF: never LENGTH (always even)",
                  "        nop\n        nop\n        nop")], ["dmaoff"]),
    "rearmfirst": ([("        tax\n        bpl rnx", "        tax\n        jmp rnx")],
                   ["blocks"]),
}


# rev15-paula's mutants (round 1) and the M5 hwloop ordering: run with
# --mutants --review; each must be killed by a named check.
REVIEW_MUTANTS = {
    "len0": ([("        lda #1\n:       sta tN+2", "        lda #0\n:       sta tN+2")],
             ["latch"]),
    "nopark": ([("fb:     jsr parkq\n        bcs fpk", "fb:     clc\n        nop\n        nop\n        bcs fpk")],
               ["park"]),
    "e0hand": ([("write:  cpx #$E0", "write:  cpx #$F0")], ["bplpt"]),
    "lc1mb": ([("lch:    and #$1F", "lch:    and #$0F")], ["latch"]),
    # a new LEN no longer drops the cached block length (block's check)
    "leninval": ([(":       lda #0\n        sta VC+V_DV,y\n:       sty sy",
                   ":       lda #0\n        nop\n        nop\n        nop\n:       sty sy")], ["lencache"]),
    "park4b": ([("PARK_E = 7094 ", "PARK_E = 28376 ")], ["park"]),
    "loopend": ([("        lda tN\n        cmp VC+V_LN+2,y\n        bne lno",
                  "        lda tN\n        cmp VC+V_LN+2,y\n        nop\n        nop")], ["reload"]),
    "nohwloop": ([("relp:   lda CH+C_ON,y           ; a playing voice's loop follows the latches\n        beq out",
                   "relp:   jmp out\n        beq out")], ["reload"]),
    # a loop change without "B beyond LENGTH" first: CTRL 1 with B == LENGTH
    "m5loop": ([("        jsr rbff                ; changed: B beyond LENGTH, A, CTRL (no",
                 "        nop\n        nop\n        nop")], ["reload"]),
    "nopull": ([("        jsr pull                ; hardware's loop end (M6)\n        jmp rearm",
                 "        rts\n        nop\n        nop\n        nop\n        nop\n        nop")],
               ["phase"]),
    "parknow": ([(":       jsr pkstep\n        jsr dlrdt\n        jsr dladd",
                  ":       jsr pkstep\n        jsr dlnow\n        nop\n        nop\n        nop")], ["phase"]),
}


def mutant_src(name: str, table: dict) -> str:
    subs, _ = table[name]
    src = os.path.join(ROOT, "tests", "paula", "aud.s")
    text = open(src).read()
    for a, b in subs:
        if text.count(a) != 1:
            raise RuntimeError(f"mutant {name}: anchor found {text.count(a)}x")
        text = text.replace(a, b)
    # the mutant keeps the original's directory for its .include paths
    path = os.path.join(ROOT, "tests", "paula", f"_mut_{name}.s")
    with open(path, "w") as f:
        f.write(text)
    return path


def teeth(prg: str, only: list[str] | None, table: dict | None = None) -> int:
    """Every mutant must fail a NAMED check: a leg that only raised (an
    infrastructure failure, marked "EXC") is not a kill (rev15-paula L2)."""
    table = table or MUTANTS
    names = only or list(table)
    bad = []
    for name in names:
        src = mutant_src(name, table)
        try:
            print(f"=== mutant {name} (legs {','.join(table[name][1])})")
            try:
                fails = run_all(prg, table[name][1], src)
            except Exception as e:
                fails = [f"session: EXC {type(e).__name__}: {e}"]
        finally:
            os.remove(src)
        named = [x for x in fails if ": EXC " not in x]
        verdict = "KILLED" if named else ("NOT KILLED (exceptions only)" if fails else "SURVIVED")
        print(f"=== mutant {name}: {verdict}")
        if not named:
            bad.append(name)
    print(f"teeth: {len(names) - len(bad)} killed, {len(bad)} not {bad}")
    return 1 if bad else 0


# --------------------------------------------------------------------------
# P3: the cost report (VICE, s = 0: one E tick = one phi2 = one 6502 cycle)
# --------------------------------------------------------------------------

PERF_N = 2000


def perf_loop(pre: list[tuple[int, int]], op: list[int], n: int = PERF_N) -> list[int]:
    """pre writes, then n x op (one instruction) in a dbra loop, HALT."""
    g = G()
    emit(g, pre)
    g(0x363C, n - 1)                            # move.w #n-1,d3
    g.label("l")
    g(*op)
    g.dbra(3, "l")
    g(HALT)
    return g.words()


def perf(prg: str, json_out: str | None) -> int:
    import json

    def body(rig: Rig) -> dict:
        out: dict = {}
        on1 = chw(0, SAMPLE, 0xFFFF, 0xFFFF, 40) + [(DMACON, 0x8201)]
        on4 = sum((chw(x, SAMPLE + 0x100 * x, 0xFFFF, 0xFFFF, 40) for x in range(4)),
                  []) + [(DMACON, 0x820F)]

        def de(prog, arm=None, s=0, r16=PR.R16_U64_S0):
            pc = rig.stage(prog)
            rig.cfg(r16)
            ts = []
            for _ in range(3):
                res = rig.run(pc, s=s, arm=arm)
                assert res["halt"] == 1 and res["reason"] == 0, res
                ts.append((res["e1"] - res["e0"]) & 0xFFFFFFFF)
            return min(ts), rig.state()

        a_off = 0
        ops = {
            "vol (on)": (on1, [0x33C1, *G().abs(aud(0, 8))]),      # move.w d1,AUD0VOL
            "per (on)": (on1, [0x33C2, *G().abs(aud(0, 6))]),
            "lcl (on, loop)": (on1, [0x33C4, *G().abs(aud(0, 2))]),
            "len (on, loop)": (on1, [0x33C5, *G().abs(aud(0, 4))]),
            "vol (off)": ([], [0x33C1, *G().abs(aud(0, 8))]),
            "dmacon (no change, 4 on)": (on4, [0x33FC, 0x820F, *G().abs(DMACON)]),
            "dat": ([], [0x33C1, *G().abs(aud(0, 10))]),
        }
        for name, (pre, op) in ops.items():
            prog = perf_loop(pre, op)
            t_on, _ = de(prog)
            t_off, _ = de(prog, arm=a_off)
            out[name] = round((t_on - t_off) / PERF_N, 1)
        # one DMACON starting four channels (one-off)
        g1 = perf_loop([], [0x4E71], 1)
        g4 = perf_loop(on4, [0x4E71], 1)
        t1, _ = de(g4)
        t0, _ = de(g4, arm=a_off)
        out["dmacon start x4 + 16 setup writes (one-off)"] = t1 - t0
        # idle: armed, nothing playing, a busy loop
        busyp = perf_loop([], [0x5281], 20000)          # addq.l #1,d1
        ti, _ = de(busyp)
        tn, _ = de(busyp, arm=a_off)
        out["idle per 20000 instr (armed - off)"] = ti - tn
        # block ends: four channels looping in hardware, blocks of LEN words;
        # the guest acknowledges AUD0-3 every instruction (no parking)
        ack = [0x33FC, 0x0780, *G().abs(INTREQ)]
        for ln, per in ((256, 428), (32, 428)):
            pre = sum((chw(x, SAMPLE + 0x1000 * x, ln, per, 40) for x in range(4)),
                      []) + [(DMACON, 0x820F)]
            prog = perf_loop(pre, ack, 20000)
            tb_, (m, st) = de(prog)
            tq, _ = de(prog, arm=a_off)
            nb = sum(mx["nb"] - 1 for mx in m)
            out[f"block end LEN {ln} PER {per}, acknowledged: cycles each"] = \
                round((tb_ - tq) / max(nb, 1), 1)
            out[f"block end LEN {ln} PER {per}, acknowledged: blocks, restarts, parks, guest share"] = [
                nb, st["nres"], st["npark"], round(tq / tb_, 3)]
        # parked: nobody acknowledges; the cost is one look per channel per
        # 1/2 to 1 frame (2000 instructions: st_npark, a u16, must not wrap)
        for ln, per in ((256, 428), (1, 428)):
            pre = sum((chw(x, SAMPLE + 0x1000 * x, ln, per, 40) for x in range(4)),
                      []) + [(DMACON, 0x820F)]
            prog = perf_loop(pre, [0x5281], 2000)
            tb_, (m, st) = de(prog)
            tq, _ = de(prog, arm=a_off)
            looks = st["npark"] + sum(mx["nb"] - 1 for mx in m)
            assert looks < 60000, "st_npark may have wrapped"
            out[f"parked LEN {ln} PER {per}: cycles per look"] = round((tb_ - tq) / max(looks, 1), 1)
            out[f"parked LEN {ln} PER {per}: looks, guest share"] = [looks, round(tq / tb_, 3)]
        # s = 0 vs s = 4 guest rate, nothing playing (the old starvation item)
        for s in (0, 4):
            t, _ = de(busyp, arm=a_off, s=s)
            out[f"busy 20000 instr, s = {s}: E ticks"] = t
        return out

    out = session(prg, body)
    print(json.dumps(out, indent=1))
    if json_out:
        with open(json_out, "w") as f:
            json.dump(out, f, indent=1)
    return 0



# --------------------------------------------------------------------------
# P2: the U64E (sampler mapped, s = 0, r16 = 18897, the UDP audio stream)
# --------------------------------------------------------------------------

MAP_ITEM = "Map Ultimate Audio $DF20-DFFF"
ONSET_TOL = 0.02               # s: 20 ms windows, 5 ms hop
PITCH_TOL = 0.0005             # measured <= 0.0001 (hw0b, hw1); one RATE step
                               # is 0.13% at PER 428, 0.5% at PER 124
HW_S = 0                       # the player profile (R3)
VBL_HZ = 1022727 / 14188       # VERTB at s = 0 on the NTSC U64E: 72.08 Hz
VERTB = 0x0020
HW_LOG: list[str] = []
HW_LAW_C = [PR.SAMPLE_C_LAW]   # the sampler's constant (ns), set from /v1/info by hw_main


def hlog(msg: str) -> None:
    import time
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    HW_LOG.append(line)
    print(line, flush=True)


def wait_frames(g: G, n: int, tag: str) -> G:
    """Poll INTREQR VERTB n times (acknowledging each): n guest frames."""
    g(0x3A3C, n - 1)                            # move.w #n-1,d5
    g.label(tag)
    mv_abs_d(g, "w", INTREQR, 6)
    g(0x0246, VERTB)                            # andi.w #$20,d6
    g.bcc(0x6700, tag)                          # beq tag
    mv_imm_abs_w(g, VERTB, INTREQ)
    return g.dbra(5, tag)


def square(period: int, n: int, amp: int = 64) -> bytes:
    """n bytes of a signed square wave, *period* samples per cycle."""
    h = period // 2
    return bytes(((amp if (i % period) < h else -amp) & 0xFF) for i in range(n))


def hw_guest(steps, data: bytes, irq: bool = False):
    """steps: [(writes, frames)]; a write's value may be a callable of the
    data's guest address.  Ends with DMACON off, two frames, HALT.  *irq*:
    a level-4 handler counts AUDx and acknowledges (count at CNT)."""
    def build(daddr: int) -> G:
        g = G()
        if irq:
            vec(g, 28, "h4")
            g(0x42B9, *g.abs(CNT))              # clr.l CNT
        mv_imm_abs_w(g, 0x7FFF, INTREQ)
        if irq:
            move_sr(g, 0x2000)                  # the driver starts at IPL 7
        for i, (writes, frames) in enumerate(steps):
            emit(g, [(a, v(daddr) if callable(v) else v) for a, v in writes])
            if frames:
                wait_frames(g, frames, f"w{i}")
        if irq:
            move_sr(g, 0x2700)
        mv_imm_abs_w(g, 0x000F, DMACON)
        wait_frames(g, 2, "wz")
        g(HALT)
        if irq:
            g.label("h4")
            addq_l_abs(g, CNT)
            mv_imm_abs_w(g, 0x0780, INTREQ)
            g(0x4E73)                           # rte
        return g
    n = len(link(build(0), BASE))
    daddr = BASE + 2 * n
    daddr += -daddr % 16
    words = link(build(daddr), BASE)
    pad = (daddr - BASE) // 2 - len(words)
    dw = [int.from_bytes(data[i:i + 2].ljust(2, b"\0"), "big")
          for i in range(0, len(data), 2)]
    return words + [0] * pad + dw, daddr


def peak(x, rate: float) -> dict:
    import numpy as np
    x = np.asarray(x, dtype=np.float64)
    mean = float(x.mean()) if len(x) else 0.0
    x = x - mean
    rms = float(np.sqrt((x ** 2).mean())) if len(x) else 0.0
    if rms == 0.0:
        return {"rms": 0.0, "f": 0.0, "mean": mean}
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    k = int(sp.argmax())
    if 0 < k < len(sp) - 1:
        a, b, c = (np.log(sp[k + d] + 1e-9) for d in (-1, 0, 1))
        k = k + 0.5 * (a - c) / (a - 2 * b + c)
    return {"rms": rms, "f": float(k * rate / len(x)), "mean": mean}


class HwRig(Rig):
    """Rig on the U64E: the product module (no log build), the sampler
    backend on (cfg_hw = 1), s = 0."""
    log = False

    def cfg(self, r16: int | None = None, hw: int = 1, zero: int = DISP_ZERO) -> None:
        self.r16 = r16 or PR.R16_U64_S0
        self.w(AUD_A + 13, struct.pack("<HB", self.r16, hw) + zero.to_bytes(3, "little"))

    def capture(self, tag: str, pc: int, out_dir: str, **kw) -> tuple[dict, dict]:
        """drv_run with the UDP audio stream captured around it.  -> (the
        run's result, {"l","r","mono": numpy arrays, "rate", ...})."""
        import socket
        import time
        import wave
        import numpy as np
        from c64_test_harness.backends.u64_audio_capture import (
            AudioCapture, U64_NTSC_AUDIO_RATE_HZ)
        client = self.t.client
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((os.environ["U64_HOST"], 80))      # the device address (required)
        ip = s.getsockname()[0]
        s.close()
        cap = AudioCapture(port=0, sample_rate=U64_NTSC_AUDIO_RATE_HZ)
        cap.start()
        wav = os.path.join(out_dir, f"audio_{tag}.wav")
        t0 = time.monotonic()
        try:
            client.stream_audio_start(f"{ip}:{cap.port}")
            time.sleep(0.3)
            t1 = time.monotonic()
            res = self.run(pc, s=HW_S, **kw)
            t2 = time.monotonic()
            time.sleep(0.3)
        finally:
            try:
                client.stream_audio_stop()
            except Exception as e:                       # noqa: BLE001
                hlog(f"stream_audio_stop failed: {e!r}")
            info = cap.stop(wav_path=wav)
        with wave.open(wav, "rb") as w:
            fr = w.getframerate()
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(
                -1, w.getnchannels()).astype(np.float64)
        res["wall"] = t2 - t1
        return res, {"l": raw[:, 0], "r": raw[:, 1], "mono": raw.mean(axis=1),
                     "rate": float(fr), "packets": info.packets_received,
                     "dropped": info.packets_dropped, "lead": t1 - t0}


def seg(a, lo: float, hi: float):
    return a[int(len(a) * lo):int(len(a) * hi)]


def find_tone(x, rate: float, thr: float = 500.0, win: float = 0.01,
              hold: float = 0.0) -> tuple[int, int]:
    """First and last sample index where the *win*-second RMS exceeds thr.
    *hold* > 0: the first is where it stays above thr for *hold* seconds (a
    DC step, as the C64U's stream takes before a song, is one or two
    windows; W16-A)."""
    import numpy as np
    w = int(rate * win)
    n = len(x) // w
    r = [float(np.std(x[i * w:(i + 1) * w])) for i in range(n)]
    on = [i for i, v in enumerate(r) if v > thr]
    if not on:
        return 0, 0
    k = max(1, round(hold / win))
    first = next((i for i in on if all(v > thr for v in r[i:i + k]) and i + k <= n), None)
    if first is None:
        return 0, 0
    return first * w, (on[-1] + 1) * w


def onset(x, rate: float, a: int, b: int, f_new: float, f_old: list[float],
          win: float = 0.02) -> float | None:
    """Seconds after sample a when the 20 ms windows' peak first sits
    closer to f_new than to every frequency in f_old (hop 5 ms)."""
    w, hop = int(rate * win), int(rate * 0.005)
    for i in range(a, b - w, hop):
        pk = peak(x[i:i + w], rate)["f"]
        if all(abs(pk - f_new) < abs(pk - fo) for fo in f_old):
            return (i + w / 2 - a) / rate
    return None


def tone_stats(au: dict, tag: str) -> dict:
    """Peak frequency and levels in the middle 60% of the tone."""
    a, b = find_tone(au["mono"], au["rate"])
    if b - a < au["rate"] * 0.2:
        return {"tone_s": (b - a) / au["rate"], "f": 0.0, "rms": 0.0, "rms_l": 0.0,
                "rms_r": 0.0, "mean": 0.0}
    m = a + (b - a) // 5, b - (b - a) // 5
    pk = peak(au["mono"][m[0]:m[1]], au["rate"])
    import numpy as np
    pk.update(tone_s=(b - a) / au["rate"],
              rms_l=float(np.std(au["l"][m[0]:m[1]])),
              rms_r=float(np.std(au["r"][m[0]:m[1]])))
    return pk


def hw_ns(per: int) -> float:
    """The sample time the device plays for PER (its RATE by the module's
    law, its own constant: HW_LAW_C)."""
    return PR.sample_ns(PR.sampler_rate(per), HW_LAW_C[0])


def f_expect(per: int, period: int, law_c: float | None = None) -> tuple[float, float]:
    """(the sampler's frequency for RATE(per), Paula's) of a waveform of
    *period* samples; *law_c*: the sample time constant (default HW_LAW_C,
    the device of this session)."""
    c = HW_LAW_C[0] if law_c is None else law_c
    return (1e9 / (period * PR.sample_ns(PR.sampler_rate(per), c)),
            PR.PAL_CCK / (period * per))


def hw_pitch(rig: HwRig, out: str, rep: dict) -> list[str]:
    f: list[str] = []
    data = square(32, 512)
    for per in (124, 214, 428, 856, 1712):
        steps = [(chw(0, lambda d: d, 256, per, 64) + [(DMACON, 0x8201)], 108)]
        prog, _ = hw_guest(steps, data)
        pc = rig.stage(prog)
        rig.cfg()
        res, au = rig.capture(f"pitch_{per}", pc, out)
        ts = tone_stats(au, "pitch")
        fs, fp = f_expect(per, 32)
        rep[f"pitch_{per}"] = dict(ts, f_sampler=fs, f_paula=fp, wall=res["wall"],
                                   dropped=au["dropped"], halt=res["halt"])
        hlog(f"pitch PER {per}: {ts['f']:.2f} Hz (sampler {fs:.2f}, Paula {fp:.2f}), "
             f"tone {ts['tone_s']:.2f} s, rms {ts['rms']:.0f}, dropped {au['dropped']}")
        if res["halt"] != 1 or res["reason"]:
            f.append(f"pitch {per}: halt {res['halt']} reason {res['reason']}")
        if abs(ts["f"] / fs - 1) > PITCH_TOL:
            f.append(f"pitch {per}: {ts['f']:.2f} Hz, the sampler law says {fs:.2f}")
        if abs(ts["f"] / fp - 1) > 0.005:
            f.append(f"pitch {per}: {ts['f']:.2f} Hz, Paula {fp:.2f} (> 0.5%)")
        want_s = 108 / VBL_HZ
        if abs(ts["tone_s"] - want_s) > 0.1:
            f.append(f"pitch {per}: tone {ts['tone_s']:.2f} s, 108 frames = {want_s:.2f} s")
    return f


def hw_vol(rig: HwRig, out: str, rep: dict) -> list[str]:
    f: list[str] = []
    data = square(32, 512)
    lv = {}
    for vol in (64, 63, 32, 16, 0):
        steps = [(chw(0, lambda d: d, 256, 428, 64) + [(DMACON, 0x8201)], 20),
                 ([(aud(0, 8), vol)], 72)]
        prog, _ = hw_guest(steps, data)
        pc = rig.stage(prog)
        rig.cfg()
        res, au = rig.capture(f"vol_{vol}", pc, out)
        r0, t = au["rate"], au["lead"]          # frames 20-92: the VOL step
        x = au["mono"][int(r0 * (t + 20 / VBL_HZ + 0.15)):int(r0 * (t + 92 / VBL_HZ - 0.1))]
        pk = peak(x, r0)
        lv[vol] = pk["rms"]
        rep[f"vol_{vol}"] = pk
        hlog(f"vol {vol}: rms {pk['rms']:.0f}")
    ref = lv[64]
    for vol, want in ((63, 1.0), (32, 32 / 63), (16, 16 / 63)):
        if ref == 0 or abs(lv[vol] / ref - want) > 0.05:
            f.append(f"vol {vol}: level {lv[vol]:.0f} / {ref:.0f} (VOL 64 -> 63), want {want:.3f}")
    if ref and lv[0] / ref > 0.02:
        f.append(f"vol 0: level {lv[0]:.0f}, not silent")
    return f


def hw_off(rig: HwRig, out: str, rep: dict, red: bool = False) -> list[str]:
    """DMA off -> the zero play: no DC left.  The tone ends on a +64 half
    cycle (a DC of +64 x ~137 stream LSB if the voice held it)."""
    f: list[str] = []
    data = bytes([64]) * 256
    steps = [(chw(0, lambda d: d, 128, 428, 64) + [(DMACON, 0x8201)], 36),
             ([(DMACON, 0x0001)], 72)]
    prog, daddr = hw_guest(steps, data)
    pc = rig.stage(prog)
    rig.cfg(zero=daddr if red else DISP_ZERO)   # red: the "zeros" are the $40s
    res, au = rig.capture("red_off" if red else "off", pc, out)
    import numpy as np
    x, r0 = au["mono"], au["rate"]
    t = au["lead"]                              # the run starts here (+ staging-free)
    win = lambda a, b: float(np.mean(x[int(r0 * (t + a)):int(r0 * (t + b))]))
    base = float(np.mean(x[:int(r0 * 0.2)]))    # before the run
    on = win(0.15, 0.40)                        # frames 0-36: the DC sample
    tail = win(36 / VBL_HZ + 0.25, 108 / VBL_HZ - 0.05)   # after DMA off
    rep["off"] = {"base": base, "on": on, "after_off": tail}
    hlog(f"off: mean before {base:.0f}, playing {on:.0f}, after DMA off {tail:.0f}")
    if abs(on - base) < 2000:
        f.append(f"off: the DC sample did not play ({on:.0f} vs {base:.0f})")
    if abs(tail - base) > 300:
        f.append(f"off: DC {tail - base:.0f} left after DMA off (the voice holds its sample)")
    return f


def hw_pan(rig: HwRig, out: str, rep: dict) -> list[str]:
    f: list[str] = []
    data = square(32, 512)
    for x in range(4):
        steps = [(chw(x, lambda d: d, 256, 428, 64) + [(DMACON, 0x8200 | 1 << x)], 72)]
        prog, _ = hw_guest(steps, data)
        pc = rig.stage(prog)
        rig.cfg()
        res, au = rig.capture(f"pan_{x}", pc, out)
        ts = tone_stats(au, "pan")
        rep[f"pan_{x}"] = ts
        left = ts["rms_l"] > ts["rms_r"]
        hlog(f"pan ch{x}: L {ts['rms_l']:.0f} R {ts['rms_r']:.0f} f {ts['f']:.1f}")
        if left != (x in (0, 3)):
            f.append(f"pan ch{x}: L {ts['rms_l']:.0f} R {ts['rms_r']:.0f} "
                     f"(Amiga: channels 0/3 left, 1/2 right)")
    return f


def hw_four(rig: HwRig, out: str, rep: dict) -> list[str]:
    """Four channels at once, four periods: four peaks in the spectrum."""
    import numpy as np
    f: list[str] = []
    data = square(32, 512)
    pers = (300, 400, 500, 640)
    w = sum((chw(x, lambda d: d, 256, pers[x], 40) for x in range(4)), [])
    steps = [(w + [(DMACON, 0x820F)], 144)]
    prog, _ = hw_guest(steps, data)
    pc = rig.stage(prog)
    rig.cfg()
    res, au = rig.capture("four", pc, out)
    a, b = find_tone(au["mono"], au["rate"])
    x = au["mono"][a + (b - a) // 5:b - (b - a) // 5]
    x = x - x.mean()
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    fr = np.fft.rfftfreq(len(x), 1 / au["rate"])
    got = []
    for per in pers:
        fs, _ = f_expect(per, 32)
        k = (fr > fs * 0.995) & (fr < fs * 1.005)
        near = float(sp[k].max()) if k.any() else 0.0
        got.append(near / float(sp.max()))
    rep["four"] = {"rel_peaks": got, "pers": pers}
    hlog(f"four: relative peak heights {[f'{v:.2f}' for v in got]}")
    for per, v in zip(pers, got):
        if v < 0.3:
            f.append(f"four: PER {per}'s fundamental at {v:.2f} of the largest peak")
    return f


def hw_irq(rig: HwRig, out: str, rep: dict) -> list[str]:
    """AUD0 at level 4, 32-sample blocks at PER 428: the handler's count
    against the sampler's real time (the run lasts F VERTB frames: the time
    base) and against the module's grid."""
    f: list[str] = []
    data = square(32, 64)
    frames = 216
    steps = [([(INTENA, 0xC080)] + chw(0, lambda d: d, 16, 428, 40)
              + [(DMACON, 0x8201)], frames)]
    prog, _ = hw_guest(steps, data, irq=True)
    pc = rig.stage(prog)
    rig.cfg()
    res, au = rig.capture("irq", pc, out)
    ts = tone_stats(au, "irq")
    m, st = rig.state()
    cnt = be(rig.guest_read(CNT, 4))
    blk = 32 * hw_ns(428) * 1e-9
    t_guest = frames / VBL_HZ
    rep["irq"] = {"count": cnt, "nb": m[0]["nb"], "nirq": res["nirq"], "wall": res["wall"],
                  "tone_s": ts["tone_s"], "e_s": ((res["e1"] - res["e0"]) & 0xFFFFFFFF) / 1022727,
                  "t_frames": t_guest, "block_s": blk,
                  "npark": st["npark"]}
    hlog(f"irq: handler {cnt}, blocks {m[0]['nb']}, wall {res['wall']:.3f} s, "
         f"{frames} frames = {t_guest:.3f} s, block {blk * 1e3:.3f} ms -> "
         f"want ~{t_guest / blk:.0f}")
    hlog(f"irq: the tone lasted {ts['tone_s']:.3f} s by the stream's clock "
         f"({frames} frames by the time base: {t_guest:.3f} s); host wall around "
         f"the call {res['wall']:.3f} s")
    if abs(ts["tone_s"] - t_guest) > 0.03:
        f.append(f"irq: {frames} VERTB frames lasted {ts['tone_s']:.3f} s of audio, "
                 f"the time base says {t_guest:.3f} s")
    if abs(m[0]["nb"] / (t_guest / blk) - 1) > 0.02:
        f.append(f"irq: {m[0]['nb']} blocks in {t_guest:.3f} s, the sampler's "
                 f"{t_guest / blk:.0f}")
    if abs(cnt - m[0]["nb"]) > 2:
        f.append(f"irq: handler {cnt} vs blocks {m[0]['nb']}")
    return f


def hw_ips(rig: HwRig, out: str, rep: dict) -> list[str]:
    """R3 caution: guest speed at s = 0 on the U64E at 48 MHz, idle and
    with four channels parked on null loops (no acknowledge)."""
    f: list[str] = []
    n = 60000
    for tag, pre in (("idle", []),
                     ("4 null loops", sum((chw(x, 0, 1, 428, 0) for x in range(4)), [])
                      + [(DMACON, 0x820F)])):
        g = G()
        emit(g, pre)
        g(0x2A3C, 0, n - 1)                     # move.l #n-1,d5
        g.label("l")
        g(0x5281)                               # addq.l #1,d1
        g(0x5385)                               # subq.l #1,d5
        g.bcc(0x6A00, "l")                      # bpl l
        g(HALT)
        pc = rig.stage(g.words())
        rig.cfg()
        res = rig.run(pc, s=HW_S)
        dt = ((res["e1"] - res["e0"]) & 0xFFFFFFFF) / 1022727
        ips = 3 * n / dt
        rep[f"ips_{tag}"] = {"s": dt, "ips": ips, "frames": res["frames"]}
        hlog(f"ips {tag}: {3 * n} instructions in {dt:.3f} s = {ips:.0f} IPS "
             f"({ips / VBL_HZ:.0f} per 72 Hz frame)")
    a, b = rep["ips_idle"]["ips"], rep["ips_4 null loops"]["ips"]
    if b / a < 0.95:
        f.append(f"ips: four parked null loops cost {100 * (1 - b / a):.1f}% of the guest")
    return f


def hw_live(rig: HwRig, out: str, rep: dict) -> list[str]:
    """Unknown 1 (measured): a PER written while the voice loops changes the
    pitch without a restart -> two tones, back to back, no gap."""
    import numpy as np
    data = square(32, 512)
    steps = [(chw(0, lambda d: d, 256, 428, 64) + [(DMACON, 0x8201)], 72),
             ([(aud(0, 6), 214)], 72)]
    prog, _ = hw_guest(steps, data)
    pc = rig.stage(prog)
    rig.cfg()
    res, au = rig.capture("live_rate", pc, out)
    a, b = find_tone(au["mono"], au["rate"])
    h = (a + b) // 2
    q = (b - a) // 8
    f1 = peak(au["mono"][a + q:h - q], au["rate"])["f"]
    f2 = peak(au["mono"][h + q:b - q], au["rate"])["f"]
    w = int(au["rate"] / 200)
    gaps = sum(1 for i in range(a, b - w, w) if np.std(au["mono"][i:i + w]) < 300)
    rep["live_rate"] = {"f1": f1, "f2": f2, "gaps_5ms": gaps,
                        "f_428": f_expect(428, 32)[0], "f_214": f_expect(214, 32)[0]}
    hlog(f"live RATE: {f1:.1f} Hz then {f2:.1f} Hz (want {f_expect(428, 32)[0]:.1f} "
         f"/ {f_expect(214, 32)[0]:.1f}), {gaps} silent 5 ms windows")
    f = []
    for got, per in ((f1, 428), (f2, 214)):
        if abs(got / f_expect(per, 32)[0] - 1) > PITCH_TOL:
            f.append(f"live: {got:.2f} Hz, want {f_expect(per, 32)[0]:.2f} (PER {per})")
    if gaps > 2:
        f.append(f"live: {gaps} silent 5 ms windows in the tone (a gap at the PER write)")
    return f


def hw_sign(rig: HwRig, out: str, rep: dict) -> list[str]:
    """Unknown 2 (measured): sample signedness: a looped block of $7F, then
    one of $80; their DC levels against silence."""
    import numpy as np
    data = bytes([0x7F]) * 64 + bytes([0x80]) * 64
    steps = [(chw(0, lambda d: d, 32, 428, 64) + [(DMACON, 0x8201)], 36),
             ([(aud(0, 0), lambda d: d + 64)], 36)]
    prog, _ = hw_guest(steps, data)
    pc = rig.stage(prog)
    rig.cfg()
    res, au = rig.capture("sign", pc, out)
    x, r0, t = au["mono"], au["rate"], au["lead"]
    base = float(np.mean(x[:int(r0 * 0.2)]))
    win = lambda a, b: float(np.mean(x[int(r0 * (t + a)):int(r0 * (t + b))]))
    m1 = win(0.15, 36 / VBL_HZ - 0.05) - base
    m2 = win(36 / VBL_HZ + 0.15, 72 / VBL_HZ - 0.05) - base
    rep["sign"] = {"dc_7f": m1, "dc_80": m2}
    hlog(f"sign: $7F -> DC {m1:.0f}, $80 -> DC {m2:.0f} (signed: + then -)")
    if not (m1 > 2000 and m2 < -2000):
        return [f"sign: $7F -> {m1:.0f}, $80 -> {m2:.0f}: not a signed 8-bit pair"]
    return []


def hw_oneshot(rig: HwRig, out: str, rep: dict) -> list[str]:
    """A one-shot sample (512 samples at PER 428: 61.8 ms by the sampler's
    law), then the latches name a 1-word null loop of zeros elsewhere: the
    6510 reprograms the voice at the block end it times from guest E (r16):
    the tone must last one block, then silence with no DC."""
    import numpy as np
    f: list[str] = []
    data = square(32, 512) + bytes(16)
    steps = [(chw(0, lambda d: d, 256, 428, 64) + [(DMACON, 0x8201)]
              + [(aud(0, 0), lambda d: d + 512), (aud(0, 4), 1)], 72)]
    prog, _ = hw_guest(steps, data)
    pc = rig.stage(prog)
    rig.cfg()
    res, au = rig.capture("oneshot", pc, out)
    m, st = rig.state()
    a, b = find_tone(au["mono"], au["rate"], thr=500.0, win=0.002)
    dur = (b - a) / au["rate"]
    want = 512 * hw_ns(428) * 1e-9
    x, r0 = au["mono"], au["rate"]
    tail = float(np.mean(x[b + int(r0 * 0.1):b + int(r0 * 0.5)])) if b else 0.0
    tail5 = float(np.mean(x[b:b + int(r0 * 0.005)])) if b else 0.0   # the first 5 ms
    base = float(np.mean(x[:int(r0 * 0.2)]))
    rep["oneshot"] = {"tone_s": dur, "want_s": want, "tail_dc": tail - base,
                      "tail5_dc": tail5 - base,
                      "np": m[0]["np"], "nb": m[0]["nb"], "npark": st["npark"]}
    hlog(f"oneshot: tone {dur * 1e3:.1f} ms (one block {want * 1e3:.1f} ms, 2 ms "
         f"windows), DC after {tail - base:.0f}, programs {m[0]['np']}")
    if abs(dur - want) > 0.008:
        f.append(f"oneshot: tone {dur * 1e3:.1f} ms, one block is {want * 1e3:.1f} ms")
    if abs(tail - base) > 300:
        f.append(f"oneshot: DC {tail - base:.0f} after the null loop took over")
    if abs(tail5 - base) > 300:
        f.append(f"oneshot: DC {tail5 - base:.0f} in the first 5 ms after the tone "
                 "(the reprogram came late: the voice held its last sample)")
    if m[0]["np"] != 3:
        f.append(f"oneshot: {m[0]['np']} programs (want 3: the start, the null "
                 "loop at the block end, the silence at the final DMA off)")
    return f


def hw_ctrl(rig: HwRig, out: str, rep: dict) -> list[str]:
    """Unknown 3 (measured): does rewriting CTRL (1 -> 3: the loop upgrade,
    hwloop) restart a playing voice?  The block: 4096 samples of period 32
    (0.49 s) then 256 of period 16; 10 frames in, the latches name the
    tail (a contiguous loop).  No restart: the period-16 tone starts at
    0.49 s; a restart at the write (0.139 s) would move it to ~0.63 s."""
    import numpy as np
    data = square(32, 4096) + square(16, 256)
    steps = [(chw(0, lambda d: d, 2176, 428, 64) + [(DMACON, 0x8201)], 10),
             ([(aud(0, 0), lambda d: d + 4096), (aud(0, 4), 128)], 62)]
    prog, _ = hw_guest(steps, data)
    pc = rig.stage(prog)
    rig.cfg()
    res, au = rig.capture("ctrl", pc, out)
    m, st = rig.state()
    x, r0 = au["mono"], au["rate"]
    a, b = find_tone(x, r0)
    st_ns = hw_ns(428)
    f32, f16 = 1e9 / (32 * st_ns), 1e9 / (16 * st_ns)
    w = int(r0 * 0.02)
    sw = None
    for i in range(a, b - w, w // 2):
        pk = peak(x[i:i + w], r0)["f"]
        if abs(pk - f16) < abs(pk - f32):
            sw = (i - a) / r0
            break
    want = 4096 * st_ns * 1e-9
    rep["ctrl"] = {"switch_s": sw, "no_restart_s": want, "restart_s": 10 / VBL_HZ + want,
                   "ctl": m[0]["ctl"], "np": m[0]["np"], "ra": m[0]["ra"], "rb": m[0]["rb"]}
    hlog(f"ctrl: the period-16 tone starts at {sw} s after the tone's start "
         f"(no restart: {want:.3f} s; a restart at the CTRL write: "
         f"{10 / VBL_HZ + want:.3f} s); ctl {m[0]['ctl']} np {m[0]['np']}")
    if sw is None or abs(sw - want) > ONSET_TOL:
        return [f"ctrl: the tail began at {sw} s, no restart predicts {want:.3f} s "
                f"(+-{ONSET_TOL}); a restart {10 / VBL_HZ + want:.3f} s"]
    return []


def hw_liveper(rig: HwRig, out: str, rep: dict) -> list[str]:
    """H1: does a live RATE write restart a playing voice?  The block: 4096
    samples of period 32, then a 256-sample tail of period 16 (the hardware
    loop).  Mid-block, PER 428 -> 214.  The write's moment is measured in
    the audio (the 259 -> 517 Hz step); the tail (1035 Hz) then begins
    after the rest of the 4096 at the new rate (no restart) or after all
    4096 at the new rate (a restart): 65 ms apart."""
    data = square(32, 4096) + square(16, 256)
    steps = [(chw(0, lambda d: d, 2176, 428, 64) + [(DMACON, 0x8201)]
              + [(aud(0, 0), lambda d: d + 4096), (aud(0, 4), 128)], 10),
             ([(aud(0, 6), 214)], 62)]
    prog, _ = hw_guest(steps, data)
    pc = rig.stage(prog)
    rig.cfg()
    res, au = rig.capture("liveper", pc, out)
    x, r0 = au["mono"], au["rate"]
    a, b = find_tone(x, r0)
    s428, s214 = (hw_ns(p_) * 1e-9 for p_ in (428, 214))
    fa428, fa214, fb214 = 1 / (32 * s428), 1 / (32 * s214), 1 / (16 * s214)
    tw = onset(x, r0, a, b, fa214, [fa428])
    tb = onset(x, r0, a, b, fb214, [fa428, fa214])
    if tw is None or tb is None:
        rep["liveper"] = {"tw": tw, "tb": tb}
        return [f"liveper: no PER step ({tw}) or no tail ({tb}) found"]
    nw = tw / s428                              # samples played at the write
    no_restart = tw + (4096 - nw) * s214
    restart = tw + 4096 * s214
    rep["liveper"] = {"tw": tw, "tail": tb, "no_restart": no_restart, "restart": restart}
    hlog(f"liveper: PER write at {tw:.3f} s, tail at {tb:.3f} s (no restart "
         f"{no_restart:.3f}, restart {restart:.3f})")
    if abs(tb - no_restart) > ONSET_TOL:
        return [f"liveper: tail at {tb:.3f} s, no restart predicts {no_restart:.3f} "
                f"(+-{ONSET_TOL}), a restart {restart:.3f}"]
    return []


def hw_livevol(rig: HwRig, out: str, rep: dict) -> list[str]:
    """H1 (VOL): VOL 64 -> 32 mid-block; the tail (period 16) must begin
    4096 samples after the start (no restart), not 4096 after the write."""
    import numpy as np
    data = square(32, 4096) + square(16, 256)
    steps = [(chw(0, lambda d: d, 2176, 428, 64) + [(DMACON, 0x8201)]
              + [(aud(0, 0), lambda d: d + 4096), (aud(0, 4), 128)], 10),
             ([(aud(0, 8), 32)], 62)]
    prog, _ = hw_guest(steps, data)
    pc = rig.stage(prog)
    rig.cfg()
    res, au = rig.capture("livevol", pc, out)
    x, r0 = au["mono"], au["rate"]
    a, b = find_tone(x, r0)
    s428 = hw_ns(428) * 1e-9
    tb = onset(x, r0, a, b, 1 / (16 * s428), [1 / (32 * s428)])
    w = int(r0 * 0.01)                          # the VOL step: the level halves
    lv = [float(np.std(x[i:i + w])) for i in range(a, b - w, w)]
    tw = next((k * 0.01 for k in range(1, len(lv)) if lv[k] < 0.7 * lv[0]), None)
    want = 4096 * s428
    rep["livevol"] = {"tw": tw, "tail": tb, "no_restart": want,
                      "restart": (tw or 0) + want}
    hlog(f"livevol: VOL write at {tw} s, tail at {tb} s (no restart {want:.3f}, "
         f"restart {(tw or 0) + want:.3f})")
    if tb is None or tw is None or abs(tb - want) > ONSET_TOL:
        return [f"livevol: tail at {tb} s, no restart predicts {want:.3f} (+-{ONSET_TOL})"]
    return []


def hw_red_rate(rig: HwRig, out: str, rep: dict) -> list[str]:
    """Red: the module with RATE one step high (mutant rateoff) must fail
    the pitch check at PER 428 and 124 on the hardware."""
    from c64_test_harness import Labels
    src = mutant_src("rateoff", MUTANTS)
    try:
        bad = HwRig(rig.tg, rig.labels, rig.prg, src)
    finally:
        os.remove(src)
    bad.install()
    bad.cfg()
    bad.call(AUD_A + 10)
    sub: dict = {}
    data = square(32, 512)
    inner = []
    for per in (428, 124):
        steps = [(chw(0, lambda d: d, 256, per, 64) + [(DMACON, 0x8201)], 72)]
        prog, _ = hw_guest(steps, data)
        pc = bad.stage(prog)
        bad.cfg()
        res, au = bad.capture(f"red_rate_{per}", pc, out)
        ts = tone_stats(au, "red")
        fs, fp = f_expect(per, 32)
        sub[per] = dict(ts, f_sampler=fs)
        hlog(f"red RATE+1, PER {per}: {ts['f']:.2f} Hz (the law for the right RATE {fs:.2f})")
        if abs(ts["f"] / fs - 1) > PITCH_TOL:
            inner.append(per)
    rep["red_rate"] = sub
    rig.install()                               # the good module back
    rig.cfg()
    rig.call(AUD_A + 10)
    return [] if inner == [428, 124] else [f"red_rate: the mutant passed the pitch "
                                            f"check at {[p for p in (428, 124) if p not in inner]}"]


def hw_red_off(rig: HwRig, out: str, rep: dict) -> list[str]:
    """Red: cfg_zero pointed at non-zero data must leave DC after DMA off
    (the off leg's check fails)."""
    sub: dict = {}
    inner = hw_off(rig, out, sub, red=True)
    rep["red_off"] = sub
    rig.cfg()
    rig.call(AUD_A + 10)
    return [] if any("left after DMA off" in x for x in inner) else \
        [f"red_off: no DC with a non-zero 'zero' source: {sub}"]


HW_LEGS = {"oneshot": hw_oneshot, "ctrl": hw_ctrl, "liveper": hw_liveper,
           "livevol": hw_livevol, "pitch": hw_pitch, "vol": hw_vol, "off": hw_off, "pan": hw_pan,
           "four": hw_four, "irq": hw_irq, "ips": hw_ips, "live": hw_live,
           "sign": hw_sign, "red_rate": hw_red_rate, "red_off": hw_red_off}


def run_hw_legs(rig: "HwRig", out: str, legs: list[str], rep: dict,
                fails: list[str]) -> None:
    import time
    for name in legs:
        t0 = time.monotonic()
        lr: dict = {}
        gb = rig.r(AUD_B, len(rig.b), seg_b=True)
        ga = rig.r(AUD_A, rig.alab["tA"] - AUD_A)
        da = [AUD_A + i for i in range(len(ga)) if ga[i] != rig.a[i] and not 13 <= i < 31]
        db = [AUD_B + i for i in range(len(gb)) if gb[i] != rig.b[i]]
        if da or db:
            hlog(f"before {name}: module changed: A {[hex(v) for v in da[:16]]} "
                 f"B {[hex(v) for v in db[:16]]} (B bytes {gb[db[0] - AUD_B:db[0] - AUD_B + 8].hex() if db else ''})")
            fails.append(f"{name}: the staged module changed before the leg "
                         f"(A {len(da)} B {len(db)} bytes)")
            rig.install()
        try:
            f = HW_LEGS[name](rig, out, lr)
        except Exception as e:      # noqa: BLE001
            import traceback
            f = [f"{name}: EXC {type(e).__name__}: {e}"]
            hlog(traceback.format_exc())
        lr["fails"] = f
        lr["seconds"] = time.monotonic() - t0
        rep["legs"][name] = lr
        hlog(f"[{name}] {'PASS' if not f else 'FAIL'}")
        for line in f:
            hlog(f"  {line}")
        fails += f


def hw_main(prg: str, out: str, legs: list[str]) -> int:
    """One DeviceLock hold: 48 MHz, reboot, the runner, the sampler mapped
    (inside the hold: the hold's restore puts the item back), the REU size
    checked, then the legs.  Config IDENTICAL afterwards or exit 3."""
    import json
    import signal
    import time
    from c64_test_harness import Labels
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.bench import u64_session as us
    from tests.run_vectors import prepare_target
    signal.signal(signal.SIGINT, signal.default_int_handler)

    def term(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGALRM, term)         # a deadline (perl alarm) restores too
    os.makedirs(out, exist_ok=True)
    us.log = hlog
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    rep: dict = {"legs": {}}
    fails: list[str] = []
    with us.u64_hold(out) as hold:
        tg = hold["target"]
        client = tg.transport.client
        cats = {c: client.get_config_category(c).get(c, {}) for c in client.list_configs()}
        where = [c for c, items in cats.items() if MAP_ITEM in items]
        reu = [(i.get("RAM Expansion Unit"), i.get("REU Size")) for i in cats.values()
               if "RAM Expansion Unit" in i]
        speed = [i.get("CPU Speed") for i in cats.values() if "CPU Speed" in i]
        hlog(f"found: {MAP_ITEM!r} in {where}, REU {reu}, CPU speed {speed}")
        rep.update(map_cat=where, reu=reu, speed_found=speed, info=hold["info"])
        HW_LAW_C[0] = PR.sample_c(hold["info"].get("product"))
        rep["sampler_c_ns"] = HW_LAW_C[0]
        hlog(f"sampler law: (rate + 1) x 160 + {HW_LAW_C[0]:g} ns "
             f"({hold['info'].get('product')!r})")
        if not where or not reu or reu[0][0] != "Enabled" or "16" not in str(reu[0][1]):
            fails.append(f"INFRA: needs the sampler item and a 16 MB REU: {where} {reu}")
        else:
            tg.transport.set_speed(48)
            reboot(client)
            client.set_config_item(where[0], MAP_ITEM, "Enabled")
            time.sleep(1.0)
            prepare_target(tg, prg)
            hlog("runner booted at 48 MHz, sampler mapped")
            rig = HwRig(tg, labels, prg)
            # the mapping, read by the 6510 ($DF21 = $10: the sampler's version)
            rig.w(0xC100, bytes([0xAD, 0x21, 0xDF, 0x8D, 0xF0, 0xC1, 0x60]))
            rig.call(0xC100)
            ver = rig.r(0xC1F0, 1)[0]
            rep["df21"] = ver
            hlog(f"$DF21 = ${ver:02X}")
            if ver != 0x10:
                fails.append(f"INFRA: $DF21 = ${ver:02X}: the sampler is not mapped")
            else:
                rig.install()
                rig.cfg()
                if rig.r(AUD_B, len(rig.b), seg_b=True) != rig.b or \
                        rig.r(AUD_A, 13) != rig.a[:13]:
                    raise RuntimeError("the module did not read back as staged")
                rig.call(AUD_A + 10)            # init: every voice plays zeros
                try:
                    run_hw_legs(rig, out, legs, rep, fails)
                finally:                        # every voice silent before the restore
                    try:
                        rig.install()
                        rig.cfg(hw=1)
                        rig.call(AUD_A + 10, timeout=30.0)
                        hlog("voices silenced (init) before the restore")
                    except Exception as e:      # noqa: BLE001
                        hlog(f"silencing before the restore failed: {e!r}")
    rep["restored"] = hold.get("restored")
    rep["fails"] = fails
    with open(os.path.join(out, "report.json"), "w") as fh:
        json.dump(rep, fh, indent=1, default=str)
    with open(os.path.join(out, "SESSION.log"), "a") as fh:
        fh.write("\n".join(HW_LOG) + "\n")
    hlog(f"run_paula hw: {'PASS' if not fails else f'FAIL ({len(fails)})'}; "
         f"config restored: {hold.get('restored')}")
    if not hold.get("restored"):
        return 3
    return 1 if fails else 0


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
    ap.add_argument("--hw", choices=["u64"])
    ap.add_argument("--review", action="store_true",
                    help="with --mutants: rev15-paula's mutant set")
    ap.add_argument("--out", default="build/evidence/w15-paula/hw")
    a = ap.parse_args(argv)
    VERBOSE = a.verbose
    if a.hw:
        legs = a.legs.split(",") if a.legs != ",".join(LEGS) else list(HW_LEGS)
        return hw_main(a.prg, a.out, legs)
    if a.perf:
        return perf(a.prg, a.json_out)
    if a.mutants:
        return teeth(a.prg, a.only.split(",") if a.only else None,
                     REVIEW_MUTANTS if a.review else MUTANTS)
    fails = run_all(a.prg, a.legs.split(","))
    print(f"run_paula: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
