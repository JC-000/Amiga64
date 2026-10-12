#!/usr/bin/env python3
"""W15-M: the MOD player program (fixtures/mod/) against host/mod_ref.py.

docs/WAVE15_BRIEF.md "W15-M" M2, interface I-1, the M0 rulings (addendum).
The program is built at test time (fixtures/mod/Makefile, vasm + vbcc +
vlink, outputs in build/mod*/) with MOD_TRACE: every Paula write the replay
issues and one 80-byte record per tick (the shadow mod_view, the frame
count, the beam) go to a trace in guest slow RAM ($C00000, fixtures/mod/
mod.i).  Each test song (host/modgen.py) is staged with the program and
played to its end; then

  * the trace's writes, tick by tick, equal mod_ref's (the init writes, each
    tick's writes including its two DMA stages, the end writes);
  * every tick's mod_view equals mod_ref's, byte for byte, and the view
    published at $000400 equals the last tick's;
  * the real write path, independently of the trace: the model leg compares
    every custom-chip write it saw; the VICE leg compares chip_log's tail
    (the last 64 decoded writes) and the AUD image in chip_shd;
  * timing: VBL ticks count frames (tick k at frame k + c); CIA ticks are
    spaced tick_e * 125 / BPM E ticks (from the beam and the frame count);
  * the status block: done, no fault, no trace overflow, the ignored-effect
    counts equal mod_ref's.

Legs:
  model   the same binary on Musashi (machine68k 0.4.1, the s3probe venv:
          ~/.cache/amiga64/s3probe-venv/bin/python) with host/chipset_ref's
          CIAs and Paula; exact 68k instruction counts per tick (product
          build, no trace) -> --cost.  Host-side oracle only (M0 ruling 7).
  vice    VICE through c64-test-harness: the runner PRG, tests/aros/mboot.s
          aros_boot (exception mode, mach_s = --s), one session for all songs.

    ~/.cache/amiga64/s3probe-venv/bin/python -m tests.run_mod --leg model [--cost]
    python3 -m tests.run_mod --leg vice --prg build/X/runner.prg [--s 6]
    ... --mutants [--only a,b]   (each mutant must fail the leg)
    ... --variant stub           (red: the replay does nothing)
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from host import mod_ref, modgen  # noqa: E402

FIX = os.path.join(ROOT, "fixtures", "mod")
EVID = os.path.join(ROOT, "build", "evidence", "w15-mod")

# fixtures/mod/mod.i (host/tests/test_mod_prog.py checks the mirror)
VIEW, VIEW_LEN = 0x000400, 72
PARAM, STATUS = 0x000480, 0x0004C0
IMAGE = 0x010000
MOD_AT = 0x080000
TR_W, TR_WEND, TR_T, TR_TEND, TR_REC = 0xC00000, 0xC80000, 0xC80000, 0xDC0000, 80
PF_STOPEND, PF_TRACE, PF_REALTIME = 1, 2, 4
ST_DONE, ST_ERROR, ST_FAULT = 2, 3, 4
IGN_KEYS = ("E0", "E3", "E8", "EF", "8", "F00")
MARK_TICK, MARK_END = 0xFFFF, 0xFFFE
FRAME_E = 14188

VARIANTS = {"stub": ["MOD_STUB"],
            # diagnostic (s = 0 study, round 2: does not starve once tr_time is bounded)
            "emitarm": ["MOD_MUT_EMITARM"]}    # the stage one-shot armed inside the tick
MUTANTS = {
    # rev15-mod's review mutants (fix round 1): each must die
    "r_tremlo": ["MOD_MUT_R_TREMLO"],       # tremolo not clamped below 0
    "r_portadn": ["MOD_MUT_R_PORTADN"],     # tone portamento down overshoots its target
    "r_norestart": ["MOD_MUT_R_NORESTART"], # the restart byte ignored (restarts at 0)
    "r_e6noreset": ["MOD_MUT_R_E6NORESET"], # E6 loop marks survive a new position
    "r_eebrk": ["MOD_MUT_R_EEBRK"],         # a D on the row cancels EE
    "r_offnn": ["MOD_MUT_R_OFFNN"],         # 9xx without a note keeps no memory
    # W15-M's own (retired for the six above: arp, vib, porta, bd, e6, ee;
    # earlier volclamp, dhex; their semantics stay pinned by mod_ref's
    # tests, every byte-exact trace and the 150-song differential fuzz)
    "ciaper": ["MOD_MUT_CIAPER"],       # CIA period from tick_e * 128 / BPM
    "repfirst": ["MOD_MUT_REPFIRST"],   # repeat LC/LEN before the DMACON set
    "trtime": ["MOD_MUT_TRTIME"],       # tr_time's unbounded retry: the s = 0 livelock (gate)
    "fullipl": ["MOD_MUT_FULLIPL"],     # the replay at the handler's IPL: VERTB pending under it
    # rev15-mod round 2 (supervisor request; 12 > the brief's 10: waiver asked)
    "e10": ["MOD_MUT_E10"],             # E1x/E2x act on a channel with no note
    "odd": ["MOD_MUT_ODD"],             # an odd usable length not rounded down
}
VICE_ONLY = {"trtime"}                  # killed by the s = 0 gate case only (no 1 MHz host in the model)
MODEL_ONLY = {"fullipl",                # killed by the slow-host model check (VICE s = 0 completes it)
              "e10", "odd"}             # killed by malformed cases (model only)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(defs: list[str], out: str, ui_src: str | None = None) -> tuple[bytes, dict]:
    """make -C fixtures/mod; returns (image, symbols from the vlink map)."""
    os.makedirs(out, exist_ok=True)
    cmd = ["make", "-s", "-C", FIX, f"OUT={out}", f"DEFS={' '.join(defs)}"]
    if ui_src:
        cmd.append(f"UI_SRC={ui_src}")
    r = subprocess.run(cmd + ["clean"], capture_output=True, text=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"build {defs} failed:\n{r.stdout}{r.stderr}")
    img = open(os.path.join(out, "modplay.bin"), "rb").read()
    syms = {}
    uiname = os.path.basename(ui_src or "ui_stub.c")
    for line in open(os.path.join(out, "modplay.map")):
        m = re.match(r"\s+0x([0-9a-f]+) (\w+): (?:global|local) reloc", line)
        if m:
            syms[m.group(2)] = int(m.group(1), 16)
        m = re.match(r"\s+(\S+):\s+CODE ([0-9a-f]+)\(([0-9a-f]+)\)", line)
        if m and m.group(1) == uiname:      # the UI object's code: its writes are the UI's
            lo = int(m.group(2), 16)
            syms["__ui_lo"], syms["__ui_hi"] = lo, lo + int(m.group(3), 16)
    if syms.get("_start") != IMAGE:
        raise RuntimeError(f"_start at {syms.get('_start')}, want ${IMAGE:06X}")
    if syms["__BSS_END"] > 0x040000:
        raise RuntimeError("image bss reaches $040000")
    return img, syms


# --------------------------------------------------------------------------
# A test case and its staging
# --------------------------------------------------------------------------

class Case:
    def __init__(self, name: str, raw: bytes, flags: int = PF_STOPEND | PF_TRACE,
                 tick_e: int = 0, max_ticks: int = 0, max_frames: int = 0,
                 start: int = 0, uidiv: int = 0, s: int | None = None,
                 timing: bool = True, wall: float = 0.0, e_per_cyc: tuple = (1, 10),
                 model_only: bool = False, modat: int = MOD_AT, plen: int | None = None):
        self.name, self.raw, self.flags = name, raw, flags
        self.tick_e, self.max_ticks, self.max_frames, self.start = (
            tick_e, max_ticks, max_frames, start)
        self.uidiv = uidiv
        self.s, self.timing, self.wall = s, timing, wall      # s: None = the leg's
        self.e_per_cyc, self.model_only = e_per_cyc, model_only
        self.modat, self.plen = modat, len(raw) if plen is None else plen
        self.err_expect = 0
        n = max_ticks or 100_000
        try:
            self.ticks, self.rep = mod_ref.play(raw[:self.plen], n, base=modat,
                                                stop_at_end=bool(flags & PF_STOPEND),
                                                start_pos=start)
        except mod_ref.ModError as e:        # the program must refuse it the same way
            self.ticks, self.rep, self.err_expect = [], None, e.code
        if not self.max_frames:              # a safety net: never an endless run
            self.max_frames = 4 * len(self.ticks) + 64

    def param(self) -> bytes:
        p = bytearray(32)
        p[0:4] = b"MPAR"
        struct.pack_into(">IIHHIB", p, 4, self.modat, self.plen, self.flags,
                         self.tick_e, self.max_ticks, self.start)
        p[21] = self.uidiv
        struct.pack_into(">I", p, 24, self.max_frames)
        return bytes(p)

    def chunks(self, img: bytes) -> list[tuple[int, bytes]]:
        reset = struct.pack(">II", IMAGE, IMAGE)        # SSP = $010000, PC = _start
        return [(0, reset), (PARAM, self.param()), (STATUS, bytes(64)),
                (VIEW, bytes(VIEW_LEN)), (IMAGE, img), (self.modat, self.raw)]


CASE_FILTER: list[str] = []            # --case NAME[,NAME]
STOP_FIRST = [False]
WALL = [0.0]                            # --wall: fixed per-case wall deadline (s)                    # --mutants: a mutant is killed by its first failing case


def cases(quick: bool = False) -> list[Case]:
    s = modgen.songs()
    out = [Case(n, s[n]) for n in ("basic", "arp", "slides", "toneporta", "vibrato",
                                   "tremolo", "offset", "volume", "flow", "retrig",
                                   "tempo")]
    # the real-time tempo (PF_REALTIME): every tick from CIA-B, U64E s = 0 tick_e
    out.append(Case("rt_tempo", s["tempo"], PF_STOPEND | PF_TRACE | PF_REALTIME,
                    tick_e=20455))
    # looping without the stop flag, cut by max_ticks; restart position
    out.append(Case("demo_loop", s["demo"], PF_TRACE, max_ticks=1700))
    # rev15-mod fix round 1 (H1): the edge rows, the restart position looping
    out.append(Case("edge", s["edge"]))
    out.append(Case("restart_loop", s["restart"], PF_TRACE, max_ticks=120))
    # UI_DIV = 3 (supervisor, M2: the UI budget's safety valve)
    out.append(Case("basic_uidiv3", s["basic"], uidiv=3))
    # THE s = 0 GATE (supervisor, round 2): the ruled player profile on a
    # 1 MHz host.  Content checked in full; timing not (a frame holds about
    # 23 guest instructions there, PERF (l)'s measurement, not a bound:
    # timestamps tear, ticks run late and catch up).
    # Bound from the measurement (build/evidence/w15-mod/s0/): the song
    # completes in ~17 s of wall / ~16.5k guest frames; budget 120 s and
    # 60 frames per tick (it needs ~43).
    out.append(Case("basic_s0", s["basic"], s=0, timing=False,
                    max_frames=60 * 384, wall=S0_WALL_S))
    # the model as a slow host (supervisor ruling, round 2 (a)): 2 E per CPU
    # cycle (20x the real E rate) so a traced tick spans frames, as on a
    # 1 MHz host; checks that no interrupt stays pending under the IPL for
    # more than MASKED_MAX_INSTR instructions (mutant fullipl)
    out.append(Case("basic_slow", s["basic"], timing=False, max_frames=60 * 384,
                    e_per_cyc=(2, 1), model_only=True))
    if CASE_FILTER:
        out = [c for c in out if c.name in CASE_FILTER]
    elif quick:
        out = out[:2]
    return out


# --------------------------------------------------------------------------
# The trace and the checks
# --------------------------------------------------------------------------

def parse_trace(stream: bytes) -> tuple[list[tuple[int, int]], list[list[tuple[int, int]]], list]:
    """(init writes, writes per tick (index 0 = tick 1), end writes)."""
    init, ticks, end, cur, ended = [], [], [], None, False
    for k in range(0, len(stream) - 3, 4):
        reg, val = struct.unpack_from(">HH", stream, k)
        if reg == MARK_TICK:
            if val != (len(ticks) + 1) & 0xFFFF:
                raise ValueError(f"trace marker {val} after {len(ticks)} ticks")
            cur = []
            ticks.append(cur)
        elif reg == MARK_END:
            ended = True
        elif ended:
            end.append((reg, val))
        elif cur is None:
            init.append((reg, val))
        else:
            cur.append((reg, val))
    return init, ticks, (end if ended else None)


def wname(w: tuple[int, int]) -> str:
    from host.chipset_ref import CUSTOM_NAMES
    return f"{CUSTOM_NAMES.get(w[0], hex(w[0]))}=${w[1]:04X}"


def check_case(c: Case, st: bytes, stream: bytes, recs: bytes, view: bytes,
               writes_seen: list | None, timing: bool = True) -> list[str]:
    f: list[str] = []
    tag = c.name
    state, err, fault = st[4], st[5], struct.unpack_from(">H", st, 6)[0]
    nt = struct.unpack_from(">I", st, 8)[0]
    if st[0:4] != b"MSTA":
        return [f"{tag}: no status block"]
    if c.err_expect:                        # M1: refused, with mod_ref's code
        if state != ST_ERROR or err != c.err_expect or fault:
            return [f"{tag}: state {state} err {err} fault {fault}, want the "
                    f"refusal err {c.err_expect}"]
        return []
    if state != ST_DONE or err or fault:
        f.append(f"{tag}: state {state} err {err} fault vector {fault}")
    if st[29]:
        f.append(f"{tag}: trace overflow")
    if st[30]:
        f.append(f"{tag}: the frame limit ended the run")
    want = c.ticks
    if nt != len(want):
        f.append(f"{tag}: {nt} ticks, mod_ref {len(want)}")
    ign = struct.unpack_from(">6H", st, 16)
    wign = tuple(c.rep.ignored.get(k, 0) for k in IGN_KEYS)
    if ign != wign:
        f.append(f"{tag}: ignored counts {ign}, mod_ref {wign}")
    try:
        init, tw, end = parse_trace(stream)
    except ValueError as e:
        return f + [f"{tag}: {e}"]
    if init != mod_ref.init_writes():
        f.append(f"{tag}: init writes {[wname(w) for w in init]}")
    if end != mod_ref.END_WRITES:
        f.append(f"{tag}: end writes {end and [wname(w) for w in end]}")
    bad = 0
    for i, t in enumerate(want):
        got = tw[i] if i < len(tw) else None
        if got != t.writes:
            bad += 1
            if bad <= 3:
                f.append(f"{tag} tick {t.n}: writes {got and [wname(w) for w in got]}"
                         f" != mod_ref {[wname(w) for w in t.writes]}")
    if bad > 3:
        f.append(f"{tag}: {bad} ticks with wrong writes")
    if len(tw) != len(want):
        f.append(f"{tag}: {len(tw)} traced ticks, mod_ref {len(want)}")
    nrec = len(recs) // TR_REC
    if nrec != len(want):
        f.append(f"{tag}: {nrec} tick records, mod_ref {len(want)}")
    bad = 0
    for i in range(min(nrec, len(want))):
        v = recs[i * TR_REC:i * TR_REC + VIEW_LEN]
        if v != want[i].view:
            bad += 1
            if bad <= 3:
                f.append(f"{tag} tick {i + 1}: view {view_diff(v, want[i].view)}")
    if bad > 3:
        f.append(f"{tag}: {bad} ticks with a wrong view")
    if want and view != c.rep.final_view(want[-1].view):
        f.append(f"{tag}: published view {view_diff(view, c.rep.final_view(want[-1].view))}")
    # the real write path
    flat = init + [w for t in tw for w in t] + (end or [])
    if writes_seen is not None and writes_seen != flat:
        k = next((i for i, (a, b) in enumerate(zip(writes_seen, flat)) if a != b),
                 min(len(writes_seen), len(flat)))
        f.append(f"{tag}: the custom writes seen differ from the trace at #{k} "
                 f"({len(writes_seen)} vs {len(flat)})")
    nt_ = sum(torn(recs, i) for i in range(nrec))
    c.torn = nt_
    if timing and c.timing:
        if nt_:
            f.append(f"{tag}: {nt_} torn tick timestamps (none expected at s >= 4)")
        else:
            f += check_timing(c, recs, min(nrec, len(want)))
    return f


def view_diff(got: bytes, want: bytes) -> str:
    names = ["magic"] * 4 + ["tick"] * 4 + ["name"] * 20 + [
        "len", "pos", "pattern", "row", "speed", "bpm", "flags", "pad"]
    for c in range(4):
        names += [f"ch{c}.period"] * 2 + [f"ch{c}.{n}" for n in
                                         ("inst", "vol", "note", "eff", "par", "vu")]
    names += ["elapsed"] * 4
    d = [f"{names[i]}@{i}: {got[i]:02X}!={want[i]:02X}"
         for i in range(min(len(got), len(want))) if got[i] != want[i]]
    return ", ".join(d[:6]) or f"length {len(got)}"


def torn(recs: bytes, i: int) -> bool:
    """tr_time gave up (3 reads, a frame passed between each): bit 31."""
    return bool(struct.unpack_from(">I", recs, i * TR_REC + VIEW_LEN)[0] >> 31)


def rec_time(recs: bytes, i: int) -> int:
    """Guest E time of tick record i (taken at the tick's start): CIA-A TOD
    (+1 per frame) * 14188 + the beam's E (cc = 227 V + H, E = cc / 5)."""
    tod, vp = struct.unpack_from(">II", recs, i * TR_REC + VIEW_LEN)
    tod &= 0x7FFFFFFF
    v = ((vp >> 16) & 1) << 8 | (vp >> 8) & 0xFF
    return tod * FRAME_E + (227 * v + (vp & 0xFF)) // 5


def predict_cia(c: Case, a: int) -> list[int]:
    """Underflow-exact tick times relative to tick a (the first CIA tick):
    CIA-B TA reloads from the latch programmed after the previous tick (a
    running timer's latch write applies at the next reload); a tick is
    NSPLIT underflows."""
    want = c.ticks
    tick_e = c.tick_e or FRAME_E

    def per(i):                         # the period programmed after tick i
        cia = i + 1 < len(want) and want[i + 1].cia
        return tick_e * 125 // (want[i].bpm if cia else 125)

    rt = bool(c.flags & PF_REALTIME)
    p0 = per(a - 1) if a > 0 else tick_e
    count = mod_ref.cia_split(p0)[1]         # the countdown loaded at tick a
    t, out = 0, [0]
    for k in range(a + 1, len(want)):
        n, latch = mod_ref.cia_split(per(k - 1))
        for _ in range(n):
            t += count + 1
            count = latch
        out.append(t)
    assert rt or a > 0
    return out


def check_timing(c: Case, recs: bytes, n: int) -> list[str]:
    """Tick times against the ideal grid, per tempo segment: VBL ticks one
    per frame (tick i due at frame f0 + i: a late tick is caught up, so the
    grid holds), CIA ticks at their underflow-exact prediction from the
    first CIA tick.  Lateness L = measured - predicted, normalised so the
    least late tick of the segment has L = 0.  A tick starts late when the
    previous one ran long (catch-up: on a 1 MHz host one guest instruction
    is ~10 E at s = 6), so L is bounded loosely (LATE_BOUND_E); the tempo
    must not drift: the median L of the last 20 ticks minus that of the 20
    from mid-song (after any start-up catch-up) is within DRIFT_BOUND_E."""
    f = []
    want = c.ticks[:n]
    rt = bool(c.flags & PF_REALTIME)
    a = 0 if rt else next((i for i, t in enumerate(want) if t.cia), len(want))
    segs = []
    if a > 1:
        segs.append(("VBL", 0, a, [i * FRAME_E for i in range(a)]))
    if a < len(want):
        segs.append(("CIA", a, len(want), predict_cia(c, a)))
    c.timing_worst = c.timing_drift = 0
    for name, lo_i, hi_i, pred in segs:
        late = [rec_time(recs, i) - pred[i - lo_i] for i in range(lo_i, hi_i)]
        lo = min(late)
        late = [x - lo for x in late]
        k = min(20, len(late))
        med = lambda xs: sorted(xs)[len(xs) // 2]
        mid = len(late) // 2                # after a start-up catch-up converged
        drift = med(late[-k:]) - med(late[mid:mid + k])
        c.timing_worst = max(c.timing_worst, max(late))
        c.timing_drift = drift if abs(drift) > abs(c.timing_drift) else c.timing_drift
        if max(late) > LATE_BOUND_E:
            f.append(f"{c.name}: a {name} tick {max(late)} E late (bound {LATE_BOUND_E})")
        if abs(drift) > DRIFT_BOUND_E:
            f.append(f"{c.name}: the {name} tempo drifts {drift} E over {len(late)} "
                     f"ticks (bound {DRIFT_BOUND_E})")
    return f


S0_WALL_S = 120.0                       # the s = 0 gate's wall budget
MASKED_MAX_INSTR = 32                   # a bound; measured max 14 (stub UI) / 23 (W15-U's ui.s)
LATE_BOUND_E = 3 * FRAME_E              # catch-up on a slow host (VICE s = 6)
DRIFT_BOUND_E = 2048                    # the tempo: no drift between the ends


# --------------------------------------------------------------------------
# Leg "model": Musashi + chipset_ref
# --------------------------------------------------------------------------

class Model:
    """The program on machine68k with chipset_ref's Paula and CIAs.  E =
    CPU / 10; a frame is 14188 E (our emulator's, CONTRACTS "Machine
    loop"); VERTB at every frame boundary."""

    SLICE = 200                    # CPU cycles per slice (20 E)

    def __init__(self, img: bytes, syms: dict, case: Case):
        import machine68k as m68
        from host.chipset_ref import Chipset
        self.m68 = m68
        self.mach = m68.Machine(m68.CPUType.M68000, 16384)
        self.cpu, self.mem = self.mach.cpu, self.mach.mem
        for a, d in case.chunks(img):
            self.mem.w_block(a, d)
        self.cs = Chipset(self.mem)
        self.cs.update_irq = self._irq
        self.ipl = 0
        self.writes: list[tuple[int, int]] = []  # Paula/DMACON writes by the program
        self.ctl: list[tuple[int, int]] = []     # DMACON/INTENA/INTREQ writes by the program
        self.ui_ctl: list[tuple[int, int]] = []  # every custom write by the UI's code
        self.ui_range = (syms.get("__ui_lo", 0), syms.get("__ui_hi", 0))
        self.cur_pc = 0
        self.icount, self.halted, self.e, self.frames = 0, False, 0, 0
        self.syms = syms
        self.tick_cost: list[int] = []     # instructions inside do_tick, per tick
        self.stage_cost: list[int] = []
        self._dt = None                    # (return address, sp, icount at entry)
        self.ui_calls = 0
        self.epc = case.e_per_cyc
        self.masked_run = self.masked_max = 0     # instructions with an IRQ pending under the IPL
        self._st = None
        self.mem.set_special_range_read_funcs(0xDF0000, 1, self.c_r8, self.c_r16, self.c_r32)
        self.mem.set_special_range_write_funcs(0xDF0000, 1, self.c_w8, self.c_w16, self.c_w32)
        self.mem.set_special_range_read_funcs(0xBF0000, 1, self.a_r8, self.a_r16, self.a_r32)
        # W15-U's area $040000-$07FFFF: RAM for the UI's code only (any other
        # writer is reported: the replay must never write there)
        self.uiarea = bytearray(0x40000)
        self.uiarea_bad: list[tuple[int, int]] = []
        self.mem.set_special_range_read_funcs(0x040000, 4, self.u_r8, self.u_r16, self.u_r32)
        self.mem.set_special_range_write_funcs(0x040000, 4, self.u_w8, self.u_w16, self.u_w32)
        self.mem.set_special_range_write_funcs(0xBF0000, 1, self.a_w8, self.a_w16, self.a_w32)
        self.cpu.set_instr_hook_callback(self.on_instr)
        self.cpu.pulse_reset()

    def _irq(self):
        lvl = self.cs.paula.level()
        if lvl != self.ipl:
            self.ipl = lvl
            self.cpu.set_irq(lvl)

    def on_instr(self, pc):
        self.icount += 1
        self.cur_pc = pc & 0xFFFFFF
        lvl = self.ipl
        if lvl and lvl <= (self.cpu.r_sr() >> 8) & 7:
            self.masked_run += 1
            if self.masked_run > self.masked_max:
                self.masked_max = self.masked_run
        else:
            self.masked_run = 0
        pc &= 0xFFFFFF
        if self.mem.r16(pc) == 0xAFFF:
            if not self.halted:             # Musashi still takes the A-line
                self.halted = True          # trap after this hook: keep the
                self.status = self.read(STATUS, 64)   # status as HALT left it
            self.mach.abort_execute()
            return
        sp = self.cpu.r_sp()
        if self._dt and pc == self._dt[0] and sp == self._dt[1]:
            self.tick_cost.append(self.icount - self._dt[2])
            self._dt = None
        if self._st and pc == self._st[0] and sp == self._st[1]:
            self.stage_cost.append(self.icount - self._st[2])
            self._st = None
        if pc == self.syms.get("_ui_frame"):
            self.ui_calls += 1
        if pc == self.syms.get("do_tick"):
            self._dt = (self.mem.r32(sp), sp + 4, self.icount)
        elif pc == self.syms.get("stage_step"):
            self._st = (self.mem.r32(sp), sp + 4, self.icount)

    # custom chips: word writes logged (Paula's audio registers and DMACON)
    def c_r16(self, a):
        return self.beam_read(a & 0x1FE) if a & 0xFFFF >= 0xF000 else 0

    def beam_read(self, r):
        e = self.e % FRAME_E
        cc = 5 * e
        v, h = cc // 227, cc % 227
        if r == 0x004:
            return 0x8000 | (v >> 8)
        if r == 0x006:
            return ((v & 0xFF) << 8) | h
        return self.cs.custom_read(r)

    def c_r8(self, a):
        w = self.c_r16(a & ~1)
        return w & 0xFF if a & 1 else w >> 8

    def c_r32(self, a):
        return (self.c_r16(a) << 16) | self.c_r16(a + 2)

    def c_w16(self, a, v):
        r = a & 0x1FE
        if self.ui_range[0] <= self.cur_pc < self.ui_range[1]:
            self.ui_ctl.append((r, v))      # the UI's (I-2): checked by ui_ownership
        else:
            if r == 0x096 or 0x0A0 <= r <= 0x0DF:
                self.writes.append((r, v))
            if r in (0x096, 0x09A, 0x09C):
                self.ctl.append((r, v))
        self.cs.custom_write(r, v)

    def c_w8(self, a, v):
        self.c_w16(a & ~1, (v << 8) | v)

    def c_w32(self, a, v):
        self.c_w16(a, v >> 16)
        self.c_w16(a + 2, v & 0xFFFF)

    def u_r8(self, a):
        return self.uiarea[a - 0x040000]

    def u_r16(self, a):
        return (self.u_r8(a) << 8) | self.u_r8(a + 1)

    def u_r32(self, a):
        return (self.u_r16(a) << 16) | self.u_r16(a + 2)

    def u_w8(self, a, v):
        if not self.ui_range[0] <= self.cur_pc < self.ui_range[1] and len(self.uiarea_bad) < 4:
            self.uiarea_bad.append((a, self.cur_pc))
        self.uiarea[a - 0x040000] = v & 0xFF

    def u_w16(self, a, v):
        self.u_w8(a, v >> 8)
        self.u_w8(a + 1, v & 0xFF)

    def u_w32(self, a, v):
        self.u_w16(a, v >> 16)
        self.u_w16(a + 2, v & 0xFFFF)

    def a_r8(self, a):
        sel = self.cs.cia_select(a)
        if sel is None:
            return 0xFF
        return (self.cs.ciaa if sel[0] == "A" else self.cs.ciab).read(sel[1])

    def a_w8(self, a, v):
        sel = self.cs.cia_select(a)
        if sel is not None:
            (self.cs.ciaa if sel[0] == "A" else self.cs.ciab).write(sel[1], v)

    def a_r16(self, a):
        return (self.a_r8(a) << 8) | self.a_r8(a + 1)

    def a_r32(self, a):
        return (self.a_r16(a) << 16) | self.a_r16(a + 2)

    def a_w16(self, a, v):
        self.a_w8(a, v >> 8)
        self.a_w8(a + 1, v & 0xFF)

    def a_w32(self, a, v):
        self.a_w16(a, v >> 16)
        self.a_w16(a + 2, v & 0xFFFF)

    def run(self, max_e: int) -> None:
        rem = 0
        while not self.halted and self.e < max_e:
            res = self.mach.execute(self.SLICE)
            ran = res.cycles if res.cycles > 0 else self.SLICE
            n, rem = divmod(rem + ran * self.epc[0], self.epc[1])
            for _ in range(2):              # split at a frame boundary
                to_fr = FRAME_E - self.e % FRAME_E
                k = min(n, to_fr)
                self.cs.ciaa.eclock(k)
                self.cs.ciab.eclock(k)
                self.e += k
                n -= k
                if self.e % FRAME_E == 0 and k:
                    self.frames += 1
                    self.cs.ciaa.tod_tick(1)        # CIA-A TOD: +1 per VERTB
                    self.cs.request(5)
                if not n:
                    break

    def read(self, a: int, n: int) -> bytes:
        return bytes(self.mem.r_block(a, n))


INT_MINE = 0x2028                       # EXTER VERTB PORTS (fixtures/mod/replay.s)


def ownership(ctl: list[tuple[int, int]]) -> list[str]:
    """W15-U ruling (I-2): DMACON writes touch only AUD0-3EN; after
    mod_init's clean slate (one INTENA and one INTREQ $7FFF, before
    ui_init) INTENA/INTREQ writes touch only W15-M's sources (and SET
    INTEN)."""
    f = []
    seen = 0
    for r, v in ctl:
        if r == 0x096 and v & 0x7FF0:
            f.append(f"DMACON write ${v:04X} touches bits beyond AUD0-3EN")
        elif r in (0x09A, 0x09C):
            if seen < 2 and v == 0x7FFF:
                seen += 1
                continue
            extra = v & 0x3FFF & ~INT_MINE
            if extra or (v & 0x4000 and not v & 0x8000):
                f.append(f"{'INTENA' if r == 0x09A else 'INTREQ'} write ${v:04X} "
                         f"touches bits W15-M does not own")
    return f[:4]


def ui_ownership(ctl: list[tuple[int, int]]) -> list[str]:
    """I-2: the UI never touches Paula (AUDx registers, AUD0-3EN) nor the
    interrupt enables/requests."""
    f = []
    for r, v in ctl:
        if 0x0A0 <= r <= 0x0DF or (r == 0x096 and v & 0x000F) or r in (0x09A, 0x09C):
            f.append(f"the UI wrote ${v:04X} to ${0xDFF000 + r:06X}")
    return f[:4]


def allowed_writes(syms: dict) -> list[tuple[int, int]]:
    """Chip RAM the program may change: vectors, view, param, status,
    stack ($000000-$00FFFF), the image incl. bss.  W15-U's $040000-$07FFFF
    is a trapped range in the model: only the UI's code may write it."""
    return [(0, IMAGE), (IMAGE, syms["__BSS_END"])]   # $040000-$07FFFF: trapped (UI only)


def memory_diff(before: bytes, after: bytes, allowed) -> list[str]:
    f, i, n = [], 0, len(before)
    while i < n:
        if before[i:i + 4096] == after[i:i + 4096]:
            i += 4096
            continue
        for k in range(i, min(n, i + 4096)):
            if before[k] != after[k] and not any(lo <= k < hi for lo, hi in allowed):
                return [f"chip RAM ${k:06X} changed (outside the program's regions)"]
        i += 4096
    return f


def run_model(img: bytes, syms: dict, c: Case) -> tuple[list[str], "Model"]:
    m = Model(img, syms, c)
    before = bytes(m.mem.r_block(0, 0x200000))
    m.run(max_e=(c.max_frames + 2) * FRAME_E)
    st = m.status if m.halted else m.read(STATUS, 64)
    twp, ttp = struct.unpack_from(">II", st, 32)
    stream = m.read(TR_W, max(0, twp - TR_W)) if twp else b""
    recs = m.read(TR_T, max(0, ttp - TR_T)) if ttp else b""
    f = [] if m.halted else [f"{c.name}: no HALT within {c.max_frames + 2} frames"]
    f += check_case(c, st, stream, recs, m.read(VIEW, VIEW_LEN), m.writes)
    f += [f"{c.name}: {x}" for x in ownership(m.ctl)]
    f += [f"{c.name}: {x}" for x in ui_ownership(m.ui_ctl)]
    f += [f"{c.name}: ${a:06X} (W15-U's area) written by non-UI code at ${pc:06X}"
          for a, pc in m.uiarea_bad]
    f += [f"{c.name}: {x}" for x in memory_diff(before, bytes(m.mem.r_block(0, 0x200000)),
                                                allowed_writes(syms))]
    if m.masked_max > MASKED_MAX_INSTR:
        f.append(f"{c.name}: an interrupt stayed pending under the IPL for "
                 f"{m.masked_max} instructions (bound {MASKED_MAX_INSTR})")
    c.masked_max = m.masked_max
    # ui_frame once per UI_DIV frames (+1: the run may end mid-period)
    fr = struct.unpack_from(">I", st, 12)[0]
    div = max(1, c.uidiv)
    if c.timing and not c.err_expect and not fr // div - 1 <= m.ui_calls <= fr // div + 1:
        f.append(f"{c.name}: ui_frame called {m.ui_calls} times in {fr} frames, "
                 f"UI_DIV {div}")
    return f, m


def fuzz_cases(n: int = 150, seed: int = 1512) -> list[Case]:
    """Seeded random valid M.K. songs (the differential fuzz of rev15-mod's
    review, its generator): every effect and parameter range, 1-3 patterns,
    random orders and restart bytes, 30% with PF_REALTIME."""
    import random
    rnd = random.Random(seed)

    def cell():
        if rnd.random() < 0.35:
            return ""
        note = rnd.choice(["---"] + [modgen.note_name(i) for i in range(36)] * 2)
        ins = rnd.choice([".."] * 3 + [f"{i:02X}" for i in range(1, 7)])
        e = rnd.randrange(16)
        if e == 0xE:
            par = rnd.randrange(16) << 4 | rnd.randrange(16)
        elif e == 0xF:
            par = rnd.choice([rnd.randrange(1, 12), rnd.randrange(32, 256), 0])
        elif e == 0xB:
            par = rnd.randrange(6)
        elif e == 0xD:
            par = rnd.choice([0, 0x10, 0x32, 0x63, 0x99, 0x07])
        else:
            par = rnd.randrange(256)
        if rnd.random() < 0.25:
            return f"{note} {ins}"
        return f"{note} {ins} {e:X}{par:02X}"

    out = []
    for k in range(n):
        npat = rnd.randrange(1, 4)
        pats = [modgen.pattern({r: [cell() for _ in range(4)] for r in range(64)
                                if rnd.random() < 0.4}) for _ in range(npat)]
        order = [rnd.randrange(npat) for _ in range(rnd.randrange(1, 5))]
        if max(order) + 1 < npat:
            order[0] = npat - 1
        raw = modgen.Song("f", pats, order, restart=rnd.randrange(3)).build()
        rt = rnd.random() < 0.3
        out.append(Case(f"fz{k}", raw, PF_STOPEND | PF_TRACE | (PF_REALTIME if rt else 0),
                        tick_e=20455 if rt else 0, max_ticks=1500))
    return out


def malformed_cases() -> list[Case]:
    """rev15-mod's malformed inputs (tools/malformed.py): refused with
    mod_ref's error code, or clamped and played exactly as mod_ref."""
    base = modgen.Song("t", [modgen.pattern({0: ["C-2 01", "C-2 04"]})], [0]).build()
    out = []

    def add(name, raw, **kw):
        out.append(Case(name, bytes(raw), timing=False, model_only=True, **kw))

    raw = bytearray(base)
    raw[1084] |= 0xF0
    raw[1086] = (raw[1086] & 0x0F) | 0xF0
    add("m_inst255", raw)
    raw = bytearray(base)
    raw[1084] |= 0x20
    add("m_inst33", raw)
    add("m_truncated_samples", base[:1084 + 1024])
    raw = bytearray(base)
    for i in range(31):
        struct.pack_into(">H", raw, 20 + 30 * i + 22, 0xFFFF)
        struct.pack_into(">HH", raw, 20 + 30 * i + 26, 0, 1)
    raw[1084:1084 + 1024] = modgen.pattern({0: ["C-2 1F", "C-2 1E", "C-2 10", "C-2 01"]})
    add("m_huge_samples", raw[:1084 + 1024 + 64])
    raw = bytearray(base)
    struct.pack_into(">HH", raw, 20 + 26, 0x7000, 0x1000)
    add("m_loop_past_end", raw)
    raw = bytearray(base)
    struct.pack_into(">HH", raw, 20 + 26, 4, 100)
    add("m_loop_cut", raw)
    for nm, k, v in (("m_songlen0", 950, 0), ("m_songlen129", 950, 129)):
        raw = bytearray(base)
        raw[k] = v
        add(nm, raw)
    raw = bytearray(base)
    raw[1080:1084] = b"M!K!"
    add("m_no_mk", raw)
    add("m_tiny", base[:600])
    raw = bytearray(base)
    raw[953] = 40
    add("m_order_unplayed_40", raw)
    add("m_vib_no_note", modgen.Song("t", [modgen.pattern(
        {0: ["--- 01 4F8"], 1: ["--- .. 4F8"], 2: ["--- .. 4F8"]})], [0]).build())
    add("m_e9_no_note", modgen.Song("t", [modgen.pattern({0: ["--- 01 E92"]})], [0]).build())
    add("m_porta2_no_note", modgen.Song("t", [modgen.pattern(
        {0: ["--- 01 205"], 1: ["--- 01 C40"]})], [0]).build())
    add("m_odd_modaddr", base, modat=MOD_AT + 1)
    add("m_end_of_area", base, modat=0x200000 - 1084 - 1024 - 40)   # 40 sample bytes fit
    add("m_jump_far", modgen.Song("t", [modgen.pattern({1: ["--- .. BFF"]})], [0],
                                  restart=200).build())
    # rev15-mod round 2: E1x/E2x on a channel with no note (REV2_E10) ...
    add("m_e1e2_no_note", modgen.Song("t", [modgen.pattern(
        {0: ["--- 01 E13", "--- 01 E25"], 2: ["--- .. 0FF", "--- .. 4F8"]})], [0]).build())
    # ... and an odd P_LEN cutting a looped sample (REV2_ODD): sample 1 cut to
    # 2k + 1 = 17 bytes, its loop at 2k - 2 = 14: the even length 16 leaves a
    # 2-byte loop, i.e. none
    raw = bytearray(base)
    struct.pack_into(">HH", raw, 20 + 26, 7, 16)
    add("m_odd_cut_loop", raw[:1084 + 1024 + 17])
    add("m_period_extremes", modgen.Song("t", [modgen.pattern(
        {0: ["C-1 01 E58", "B-3 01 E57"], 1: ["--- .. 4FF", "--- .. 4FF"]})], [0]).build())
    return out


FUZZ = [150]                            # --fuzz N (0: none); model leg only


def leg_model(defs: list[str], cost: bool, quick: bool, ui_src: str | None = None) -> list[str]:
    out = os.path.join(ROOT, "build", "mod-model" + ("-" + "-".join(defs) if defs else "")
                       + ("-ui" if ui_src else ""))
    img, syms = build(["MOD_TRACE", *defs], out, ui_src)
    fails = []
    extra = [] if quick else malformed_cases() + fuzz_cases(FUZZ[0])
    if CASE_FILTER:
        extra = [c for c in extra if c.name in CASE_FILTER]
    for c in cases(quick) + extra:
        if fails and STOP_FIRST[0]:
            break
        t0 = time.monotonic()
        f, m = run_model(img, syms, c)
        if c.name.startswith("fz") and not f:
            continue                        # the fuzz: failures only (a summary below)
        log(f"model {c.name}: {len(c.ticks)} ticks, {m.icount} instr, masked max "
            f"{getattr(c, 'masked_max', '-')} instr, "
            f"{time.monotonic() - t0:.1f}s, timing worst "
            f"{getattr(c, 'timing_worst', '-')} E (drift {getattr(c, 'timing_drift', '-')}): "
            f"{'ok' if not f else 'FAIL'}")
        for x in f[:8]:
            print("    " + x)
        fails += f
    if extra:
        nf = sum(1 for c in extra if c.name.startswith("fz"))
        log(f"model: {len(extra) - nf} malformed cases, {nf} fuzz songs (seed 1512)")
    if cost:
        fails += leg_cost(defs)
    return fails


def leg_cost(defs: list[str]) -> list[str]:
    """Exact 68k instructions per tick, the product build (no trace)."""
    import json
    out = os.path.join(ROOT, "build", "mod-cost")
    img, syms = build(list(defs), out)
    rows = {}
    for c in cases():
        c.flags &= ~PF_TRACE
        m = Model(img, syms, c)
        m.run(max_e=(c.max_frames + 2) * FRAME_E)
        tc = m.tick_cost
        row = [tc[i] for i in range(len(tc)) if c.ticks[i].writes and any(
            w[0] == mod_ref.DMACON and not w[1] & 0x8000 for w in c.ticks[i].writes)]
        rows[c.name] = {"ticks": len(tc), "mean": round(sum(tc) / max(1, len(tc)), 1),
                        "median": sorted(tc)[len(tc) // 2] if tc else 0,
                        "max": max(tc, default=0),
                        "trigger_ticks_max": max(row, default=0),
                        "stage_max": max(m.stage_cost, default=0),
                        "total_instr": m.icount}
        log(f"cost {c.name}: {rows[c.name]}")
    os.makedirs(EVID, exist_ok=True)
    with open(os.path.join(EVID, "cost_model.json"), "w") as fp:
        json.dump(rows, fp, indent=1)
    return []


# --------------------------------------------------------------------------
# Leg "vice": the runner PRG in VICE, tests/aros/mboot.s aros_boot
# --------------------------------------------------------------------------

CHIP_FILL_BANKS = 0x20                  # guest $000000-$1FFFFF zeroed before each song


def chip_tail(m, lab) -> tuple[list[tuple[int, int]], dict[int, int]]:
    """chip_log's last entries (Paula's registers only, oldest first) and
    chip_shd's AUD/DMACON words."""
    log_ = m.r(lab("chip_log"), 256)
    n = struct.unpack("<H", m.r(lab("chip_logn"), 2))[0]
    k = min(n, 64)
    ents = []
    for j in range(n - k, n):
        i = (j % 64) * 4
        rid, val = struct.unpack_from("<HH", log_, i)
        if rid == 0x096 or 0x0A0 <= rid <= 0x0DF:
            ents.append((rid, val))
    shd = m.r(lab("chip_shd"), 0x100)
    regs = {r: struct.unpack_from(">H", shd, r)[0] for r in [0x096] +
            [0x0A0 + 16 * c + 2 * j for c in range(4) for j in range(5)]}
    return ents, regs


def real_path(c: Case, flat: list, ents: list, regs: dict) -> list[str]:
    f = []
    if not ents:
        f.append(f"{c.name}: no Paula write in chip_log's tail")
    elif flat[-len(ents):] != ents:
        f.append(f"{c.name}: chip_log tail {[wname(w) for w in ents[-6:]]} != "
                 f"trace {[wname(w) for w in flat[-6:]]}")
    last = {}
    for r, v in flat:
        last[r] = v
    for r, v in last.items():
        if regs.get(r) != v:
            f.append(f"{c.name}: chip_shd {wname((r, regs.get(r, -1)))} != last write "
                     f"{wname((r, v))}")
    return f


def leg_vice(prg: str, defs: list[str], s: int, quick: bool) -> list[str]:
    from tests import aros_boot as AB
    from tests.run_dos import Guest
    from tests.run_program import Chunk, Session
    from types import SimpleNamespace
    if not prg:
        raise SystemExit("--prg is required for --leg vice")
    out = os.path.join(ROOT, "build", "mod-vice" + ("-" + "-".join(defs) if defs else ""))
    img, syms = build(["MOD_TRACE", *defs], out)
    fails: list[str] = []

    def body(m):
        sess = Session(target=m.tg, labels=m.labels, timeout=300.0)
        g = Guest(SimpleNamespace(target=m.tg, timeout=600.0), m.labels)
        for c in cases(quick):
            if c.model_only:
                continue
            t0 = time.monotonic()
            m.load_driver()
            m.w(AB.P, bytes([0, CHIP_FILL_BANKS]))
            m.call("aros_fill")
            sess.stage([Chunk(a, d) for a, d in c.chunks(img)])
            m.load_driver()                     # staging used buf_case
            nirq0 = struct.unpack("<I", m.r(m.lab("mach_nirq"), 4))[0]
            nfs0 = struct.unpack("<H", m.r(m.lab("mach_nfs"), 2))[0]
            cs = s if c.s is None else c.s
            m.start(cs)
            deadline = time.monotonic() + (WALL[0] or c.wall or
                                           120 + 0.6 * c.max_frames * 2 ** (cs - 6))
            while True:
                r = m.r(AB.R, 0x10)
                if r[0] == 0xFF:
                    break
                if time.monotonic() > deadline:
                    # the measurement at the deadline (s = 0 starvation, M2)
                    st = g.read(STATUS, 64)
                    mf = struct.unpack("<I", m.r(0x96, 4))[0]        # zp mach_frames
                    nirq = (struct.unpack("<I", m.r(m.lab("mach_nirq"), 4))[0]
                            - nirq0) & 0xFFFFFFFF
                    nfs = (struct.unpack("<H", m.r(m.lab("mach_nfs"), 2))[0]
                           - nfs0) & 0xFFFF
                    tk, gf = struct.unpack_from(">II", st, 8)
                    msg = (f"{c.name}: no halt by the deadline ({time.monotonic() - t0:.0f}s "
                           f"wall, s = {cs}): guest ticks {tk} of {len(c.ticks)}, "
                           f"VERTBs handled {gf}, mach_frames {mf}, deliveries {nirq}, "
                           f"forced steps {nfs}")
                    log(msg)
                    fails.append(msg)
                    return              # the guest still runs: end the session
                time.sleep(1.0)
            halt, reason = r[1], r[2]
            nirq = (struct.unpack("<I", m.r(m.lab("mach_nirq"), 4))[0] - nirq0) & 0xFFFFFFFF
            nfs = (struct.unpack("<H", m.r(m.lab("mach_nfs"), 2))[0] - nfs0) & 0xFFFF
            c.counters = {"frames": None, "nirq": nirq, "nfs": nfs,
                          "wall_s": round(time.monotonic() - t0, 1)}
            frames = struct.unpack_from("<I", r, 12)[0]
            st = g.read(STATUS, 64)
            twp, ttp = struct.unpack_from(">II", st, 32)
            stream = g.read(TR_W, twp - TR_W) if twp > TR_W else b""
            recs = g.read(TR_T, ttp - TR_T) if ttp > TR_T else b""
            view = g.read(VIEW, VIEW_LEN)
            f = []
            if halt != 1 or reason != 0:
                f.append(f"{c.name}: mach_run returned st_halt {halt} reason {reason} "
                         f"(want HALT_OK)")
            f += check_case(c, st, stream, recs, view, None)
            try:
                init, tw, end = parse_trace(stream)
                flat = init + [w for t in tw for w in t] + (end or [])
                ents, regs = chip_tail(m, m.lab)
                f += real_path(c, flat, ents, regs)
            except ValueError:
                pass
            l6, fl, mc = struct.unpack_from(">HHB", st, 42)[0], \
                struct.unpack_from(">H", st, 40)[0], st[28]
            log(f"vice {c.name} (s = {cs}): {len(c.ticks)} ticks, guest frames {frames}, "
                f"torn {getattr(c, 'torn', 0)}, "
                f"{time.monotonic() - t0:.0f}s, timing worst "
                f"{getattr(c, 'timing_worst', '-')} E (drift {getattr(c, 'timing_drift', '-')}), level-6 entries {l6}, "
                f"stage flushes {fl}, max catch-up {mc}, deliveries {nirq}, forced "
                f"steps {nfs}: {'ok' if not f else 'FAIL'}")
            for x in f[:8]:
                print("    " + x)
            fails.extend(f)
            if f and STOP_FIRST[0]:
                return

    def on_term(*_):
        raise KeyboardInterrupt("SIGTERM")
    signal.signal(signal.SIGTERM, on_term)
    AB.vice_session(prg, body)
    return fails


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--leg", choices=("model", "vice"), default="model")
    ap.add_argument("--prg")
    ap.add_argument("--s", type=int, default=6)
    ap.add_argument("--cost", action="store_true")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--ui", help="model: link this UI source (e.g. the merged fixtures/mod/ui/ui.s)")
    ap.add_argument("--fuzz", type=int, default=150, help="model: fuzz songs (0: none)")
    ap.add_argument("--case", help="only these cases (comma-separated)")
    ap.add_argument("--wall", type=float, default=0.0,
                    help="per-case wall deadline in s (a measurement run)")
    ap.add_argument("--variant", choices=sorted(VARIANTS))
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--expect-fail", action="store_true")
    a = ap.parse_args(argv)
    if a.case:
        CASE_FILTER[:] = a.case.split(",")
    WALL[0] = a.wall
    FUZZ[0] = a.fuzz

    def leg(defs):
        if a.leg == "model":
            return leg_model(defs, a.cost and not defs, a.quick, a.ui)
        return leg_vice(a.prg, defs, a.s, a.quick)

    if a.mutants:
        STOP_FIRST[0] = True
        names = a.only.split(",") if a.only else list(MUTANTS)
        skip = VICE_ONLY if a.leg == "model" else MODEL_ONLY
        names = [n for n in names if n not in skip]
        print(f"{a.leg} leg: {sorted(skip)} not applicable here (killed by the other leg)")
        survived = []
        for n in names:
            log(f"mutant {n}")
            f = leg(MUTANTS[n])
            log(f"mutant {n}: {'KILLED' if f else 'SURVIVED'} ({len(f)} failures)")
            if not f:
                survived.append(n)
        print(f"mutants: {len(names) - len(survived)}/{len(names)} killed"
              + (f"; SURVIVED {survived}" if survived else ""))
        return 1 if survived else 0
    f = leg(VARIANTS[a.variant] if a.variant else [])
    print(f"run_mod {a.leg}{' ' + a.variant if a.variant else ''}: "
          f"{'PASS' if not f else f'FAIL ({len(f)})'}")
    if a.expect_fail:
        return 0 if f else 1
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
