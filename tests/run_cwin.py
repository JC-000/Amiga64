#!/usr/bin/env python3
"""W12-C: the code-window cache (docs/CONTRACTS.md "Code-window cache").

CWIN_N 256-byte windows in CWIN (RAM under BASIC ROM), an MRU-ordered
directory and the page-low map cwin_map that the write check reads.  One
VICE session per leg group, many cases per session (suite-shape rule).

Legs (VICE unless noted):
  smc     guest writes into cached windows that are NOT the current one
          (word, byte, a long that spills from an uncached page into a
          cached one, two windows with the same page-low in different
          banks, a write into the current page), a ring of more pages than
          windows (evictions), and the directory after an N-page ring
          (every page cached)
  blt     a blitter D row into a cached, non-current window (RED until
          blt.s drops it through code_invpg: supervisor change)
  invall  I1: a raw REC STASH (behind the memory API) rewrites a cached,
          non-current page; code_invall makes the next run see it, and the
          same run without code_invall shows the stale copy (teeth)
  bank    $01: a run entered at $37 banks BASIC out before its first
          window fetch and leaves $36; a second $37 entry after emu_reset
          is correct too (a miss path that forgets the banking fails)
  bytew   cycles per MOVE.B/W/L to memory (the write check's per-size
          cost; run on main and on this tree and compare)
  bench   cycles per page change, timed with the CIA2 timers around one
          emu_run (perf_cycles technique): K-page rings (K <= N: switches,
          K > N: every change refills) against an in-page loop of the same
          instructions.  --backend u64: the same on the C64U/U64E (CIA2
          counts 1 MHz phi2 at any turbo speed: microseconds)
  aros    the AROS boot with a counter build (-D CWIN_COUNT, exception-mode
          loop counts instructions): refills / page changes / write drops
          per instruction at the model's m1/m2/m3 instruction counts, for
          the --sizes given (one build and one VICE boot each)

    python3 -m tests.run_cwin --prg build/X/runner.prg [--legs smc,invall,bench]
    python3 -m tests.run_cwin --prg ... --legs bench --backend u64 --mhz 64
    python3 -m tests.run_cwin --legs aros --sizes 1,4,7 [--out DIR]
    python3 -m tests.run_cwin --mutants [--only a,b]

Exit 0 = every selected leg passed; --mutants: every mutant fails a leg.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import run_program as rp  # noqa: E402

EVID = os.path.join(ROOT, "build", "evidence", "w12-cwin")
RUNSTUB = 0xC680        # emu_reset/(code_invall) + PC/A7 + emu_run
STUB = 0xC700           # CIA2-timed jsr RUNSTUB (tests/heap_cost.stub_code)
DMASTUB = 0xC600        # raw REC STASH of DMABUF to a guest address
DMABUF = 0xC5F0
CYC = 0xC7F0            # the stub copies $DD04-$DD07 here (U64: no I/O reads)
PORTOUT = 0xC7F8        # RUNSTUB copies $01 here after emu_run
BASE = 0x010000
STACK = 0x00F000

JMP = 0x4EF9            # JMP (abs.L)
LEA_A0 = 0x41F9         # LEA (abs.L),A0
JMP_A0 = 0x4ED0         # JMP (A0)
MOVEW_IMM = 0x33FC      # MOVE.W #imm,(abs.L)
MOVEB_IMM = 0x13FC      # MOVE.B #imm,(abs.L)
MOVEL_IMM = 0x23FC      # MOVE.L #imm,(abs.L)
HALT = 0xAFFF
NOP = 0x4E71


def log(msg: str) -> None:
    print(msg, flush=True)


def hi(a: int) -> int:
    return (a >> 16) & 0xFFFF


def lo(a: int) -> int:
    return a & 0xFFFF


def chunks(parts: dict[int, list[int]]) -> list:
    """{guest address: [words]} -> staging chunks (absolute, no hunk)."""
    from host.image import Chunk
    return [Chunk(a, b"".join(struct.pack(">H", w) for w in ws))
            for a, ws in sorted(parts.items())]


# --------------------------------------------------------------------------
# Programs (absolute guest addresses; P = the page that gets patched)
# --------------------------------------------------------------------------

def visit_twice(write: list[int], p: int = BASE + 0x200) -> dict:
    """Visit page p (MOVEQ #-1,D0; JMP (A0)) from page BASE, come back, run
    *write* (which patches p's MOVEQ into MOVEQ #1 while p is a cached,
    non-current window), visit p again, HALT.  Stale window: D0 = -1."""
    r1, r2 = BASE + 0x10, BASE + 0x40
    return {BASE: [LEA_A0, hi(r1), lo(r1), JMP, hi(p), lo(p)],
            r1: write + [LEA_A0, hi(r2), lo(r2), JMP, hi(p), lo(p)],
            r2: [HALT],
            p: [0x70FF, JMP_A0]}


def case_word() -> tuple:
    p = BASE + 0x200
    prog = visit_twice([MOVEW_IMM, 0x7001, hi(p), lo(p)])
    return prog, {"D0": 1, "A0": BASE + 0x40}, BASE + 0x42, 10


def case_byte() -> tuple:
    p = BASE + 0x200
    prog = visit_twice([MOVEB_IMM, 0x0001, hi(p + 1), lo(p + 1)])
    return prog, {"D0": 1, "A0": BASE + 0x40}, BASE + 0x42, 10


def case_spill() -> tuple:
    # a long at p-2 starts in page p-$100 (never cached) and spills its
    # last two bytes onto p (cached, not current): the map bit 6 path
    p = BASE + 0x200
    prog = visit_twice([MOVEL_IMM, 0x0000, 0x7001, hi(p - 2), lo(p - 2)])
    return prog, {"D0": 1, "A0": BASE + 0x40}, BASE + 0x42, 10


def case_twobank() -> tuple:
    """Pages $010300 and $020300 share page-low $03.  Both cached; patch
    the first (count 2 -> 1), then the second must still be seen (bit 7
    must survive the first drop), then visit both again."""
    p, q = BASE + 0x300, 0x020300
    r1, r2, r3, r4 = BASE + 0x10, BASE + 0x20, BASE + 0x40, BASE + 0x50
    return ({BASE: [LEA_A0, hi(r1), lo(r1), JMP, hi(p), lo(p)],
             r1: [LEA_A0, hi(r2), lo(r2), JMP, hi(q), lo(q)],
             r2: [MOVEW_IMM, 0x7001, hi(p), lo(p),            # MOVEQ #1,D0
                  MOVEW_IMM, 0x7601, hi(q), lo(q),            # MOVEQ #1,D3
                  LEA_A0, hi(r3), lo(r3), JMP, hi(p), lo(p)],
             r3: [LEA_A0, hi(r4), lo(r4), JMP, hi(q), lo(q)],
             r4: [HALT],
             p: [0x70FF, JMP_A0],
             q: [0x76FF, JMP_A0]},
            {"D0": 1, "D3": 1, "A0": r4}, r4 + 2, 19)


def case_current() -> tuple:
    """A write into the page being executed, the instruction after it (the
    current window: w_inv), as tests/smc.py midpage, from a page reached
    through the cache (a switch back, not a refill)."""
    p = BASE + 0x400
    r1 = BASE + 0x10
    return ({BASE: [LEA_A0, hi(r1), lo(r1), JMP, hi(p), lo(p)],
             r1: [JMP, hi(p + 4), lo(p + 4)],
             p: [0x70FF, JMP_A0],                       # first visit
             p + 4: [MOVEW_IMM, 0x7001, hi(p + 12), lo(p + 12),
                     0x70FF, HALT]},                    # p + 12: patched
            {"D0": 1, "A0": r1}, p + 16, 8)



def case_blit(last: bool = False) -> tuple:
    """A blitter D row (A -> D, one word, BLTADAT source $7001 at $8000)
    lands on page p, a cached non-current window: blt.s must drop it
    (code_invpg), not only the page of r_pc."""
    p = BASE + 0x200
    r1, r2 = BASE + 0x10, BASE + 0x80
    C = 0xDFF000
    w = [0x33FC, 0x8240, hi(C + 0x096), lo(C + 0x096)]
    for off, v in ((0x040, 0x09F0), (0x042, 0), (0x044, 0xFFFF), (0x046, 0xFFFF),
                   (0x064, 0), (0x066, 0)):
        w += [MOVEW_IMM, v, hi(C + off), lo(C + off)]
    w += [MOVEL_IMM, 0x0000, 0x8000, hi(C + 0x050), lo(C + 0x050)]   # BLTAPT
    d = p - 2 if last else p        # last: a 2-word row from p-2, its
    size = 0x0042 if last else 0x0041   # LAST page is p (first page uncached)
    w += [MOVEL_IMM, hi(d), lo(d), hi(C + 0x054), lo(C + 0x054)]     # BLTDPT
    w += [MOVEW_IMM, size, hi(C + 0x058), lo(C + 0x058)]             # BLTSIZE
    w += [LEA_A0, hi(r2), lo(r2), JMP, hi(p), lo(p)]
    assert r1 + 2 * len(w) <= r2
    return ({BASE: [LEA_A0, hi(r1), lo(r1), JMP, hi(p), lo(p)],
             r1: w, r2: [HALT], 0x8000: [0x0000, 0x7001] if last else [0x7001],
             p: [0x70FF, JMP_A0]},
            {"D0": 1, "A0": r2}, r2 + 2, 19)


def straddle(at: int, words: list[int]) -> dict:
    """Visit page A = $010100, then C = $010400, then B = $010200 (FIFO
    fills them into windows k, k-1, k-2: B is never A's neighbour window),
    then jump to *at* near A's end, where *words* run on across into B."""
    a_vis, c, b_vis = BASE + 0x100, BASE + 0x400, BASE + 0x280
    r1, r2, r3 = BASE + 0x10, BASE + 0x20, BASE + 0x30
    prog = {BASE: [LEA_A0, hi(r1), lo(r1), JMP, hi(a_vis), lo(a_vis)],
            a_vis: [JMP_A0],
            r1: [LEA_A0, hi(r2), lo(r2), JMP, hi(c), lo(c)],
            c: [JMP_A0],
            r2: [LEA_A0, hi(r3), lo(r3), JMP, hi(b_vis), lo(b_vis)],
            b_vis: [JMP_A0],
            r3: [JMP, hi(at), lo(at)]}
    # split *words* at the page end into the A part and the B part
    na = (BASE + 0x200 - at) // 2
    prog[at] = words[:na]
    prog[BASE + 0x200] = words[na:]
    return prog


def case_straddle_ext() -> tuple:
    # MOVE.W #$7001,D0 at $0101FE: the opcode is A's last word, the
    # immediate B's first; then HALT in B
    return (straddle(BASE + 0x1FE, [0x303C, 0x7001, HALT]),
            {"D0": 0x7001, "A0": BASE + 0x30}, BASE + 0x204, 12)


def case_straddle_long() -> tuple:
    # MOVE.L #$12345678,D1 at $0101FC: opcode and the first extension word
    # in A, the second extension word in B
    return (straddle(BASE + 0x1FC, [0x223C, 0x1234, 0x5678, HALT]),
            {"D1": 0x12345678, "A0": BASE + 0x30}, BASE + 0x204, 12)


def case_hintval() -> tuple:
    """Pages $020300 (q) then $010300 (p) share page-low $03: the map hint
    names p's window.  A write into p drops it (count 2 -> 1, the hint
    still names p's window, now invalid); the revisit of p must refill,
    not switch to the dropped window (the hint needs its cw_val check)."""
    p, q = BASE + 0x300, 0x020300
    r1, r2, r3 = BASE + 0x10, BASE + 0x20, BASE + 0x40
    return ({BASE: [LEA_A0, hi(r1), lo(r1), JMP, hi(q), lo(q)],
             r1: [LEA_A0, hi(r2), lo(r2), JMP, hi(p), lo(p)],
             r2: [MOVEW_IMM, 0x7001, hi(p), lo(p), LEA_A0, hi(r3), lo(r3),
                  JMP, hi(p), lo(p)],
             r3: [HALT],
             p: [0x70FF, JMP_A0],
             q: [JMP_A0]},
            {"D0": 1, "A0": r3}, r3 + 2, 13)


def case_carry(z: int) -> tuple:
    """A long write at z-2 spills its last two bytes across a 64 KB bank
    line into page z (cached, not current): the spill page's bank is
    z's, one more than the write's (z = $030000; z = $000000 is the 24-bit
    wrap from $FFFFFE)."""
    w = (z - 2) & 0xFFFFFF
    r1, r2 = BASE + 0x10, BASE + 0x30
    return ({BASE: [LEA_A0, hi(r1), lo(r1), JMP, hi(z), lo(z)],
             r1: [MOVEL_IMM, 0x0000, 0x7001, hi(w), lo(w),
                  LEA_A0, hi(r2), lo(r2), JMP, hi(z), lo(z)],
             r2: [HALT],
             z: [0x70FF, JMP_A0]},
            {"D0": 1, "A0": r2}, r2 + 2, 10)

def ring(k: int, n: int, first: int = BASE + 0x100) -> dict:
    """MOVE.W #n-1,D0; then k pages, page i: ADDQ.L #1,D2; JMP page i+1;
    the last: ADDQ.L #1,D2; DBRA D0,page 0; HALT.  D2 = k*n."""
    pages = [first + i * 0x100 for i in range(k)]
    prog = {BASE: [0x303C, n - 1, JMP, hi(pages[0]), lo(pages[0])]}
    for i, a in enumerate(pages[:-1]):
        prog[a] = [0x5282, JMP, hi(pages[i + 1]), lo(pages[i + 1])]
    last = pages[-1]
    prog[last] = [0x5282, 0x51C8, (pages[0] - (last + 4)) & 0xFFFF, HALT]
    return prog


def ring_flat(k: int, n: int, at: int = 0x018000) -> dict:
    """The same instructions as ring(k, n), all in one page."""
    body = [at + i * 8 for i in range(k)]
    prog = {BASE: [0x303C, n - 1, JMP, hi(body[0]), lo(body[0])]}
    for i, a in enumerate(body[:-1]):
        prog[a] = [0x5282, JMP, hi(body[i + 1]), lo(body[i + 1])]
    last = body[-1]
    prog[last] = [0x5282, 0x51C8, (body[0] - (last + 4)) & 0xFFFF, HALT]
    return prog


def case_evict(nwin: int) -> tuple:
    """A ring of nwin + 2 pages twice (every change refills once the cache
    is full), patching page 0's ADDQ.L #1,D2 into ADDQ.L #2,D2 in between
    -- page 0 is evicted and refilled, so this checks reuse, not drops."""
    k = nwin + 2
    first = BASE + 0x100
    prog = ring(k, 1, first)
    # BASE: run the ring once with D0 = 0 (DBRA falls through to HALT) ...
    # turn that into: ring once, patch, ring again via a second entry
    pages = [first + i * 0x100 for i in range(k)]
    last = pages[-1]
    r1 = BASE + 0x20
    prog[BASE] = [0x303C, 0x0001, JMP, hi(pages[0]), lo(pages[0])]
    # the last page: ADDQ; DBRA D0,patch-stub; HALT
    prog[last] = [0x5282, 0x51C8, (r1 - (last + 4)) & 0xFFFF, HALT]
    prog[r1] = [MOVEW_IMM, 0x5482, hi(pages[0]), lo(pages[0]),    # ADDQ.L #2,D2
                MOVEW_IMM, 0x6002, hi(last + 2), lo(last + 2),    # DBRA -> BRA.S +2
                JMP, hi(pages[0]), lo(pages[0])]
    # second pass: page 0 adds 2, the others 1, then BRA.S over the DBRA
    # displacement to the HALT
    return prog, {"D0": 0, "D2": k + (k + 1)}, last + 8, 4 * k + 6


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

class Rig:
    def __init__(self, target, labels):
        from tests.harness_util import lookup_label
        self.tg, self.t = target, target.transport
        self.labels = labels
        self.lab = lambda n: lookup_label(labels, n)
        self.sess = rp.Session(target=target, labels=labels, timeout=120.0)

    def w(self, a: int, d: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, a, d)

    def r(self, a: int, n: int) -> bytes:
        from c64_test_harness import read_bytes
        return read_bytes(self.t, a, n)

    def call(self, a: int, timeout: float = 120.0) -> None:
        from tests.harness_util import call_runner
        call_runner(self.tg, a, timeout)

    def stage(self, prog: dict) -> None:
        self.sess.stage(chunks(prog))

    def run(self, pc: int, budget: int = 100000) -> rp.RunResult:
        return self.sess.run_program(pc, STACK, budget)

    def directory(self) -> dict:
        n = self.lab("CWIN_N")
        tl, th = self.r(self.lab("cw_tlo"), n), self.r(self.lab("cw_thi"), n)
        val, hand = self.r(self.lab("cw_val"), n), self.r(self.lab("cw_hand"), 1)[0]
        return {"n": n, "val": list(val), "hand": hand,
                "pages": [(th[i] << 16 | tl[i] << 8) if val[i] else None
                          for i in range(n)]}

    def map_errors(self) -> list[str]:
        """cwin_map against the directory (CONTRACTS "Code-window cache"):
        bits 7, 6 and 2-0 exactly as the valid windows say, and an entry
        whose count is 0 has no hint bits either (any nonzero byte sends
        word writes to the slow path).  VICE: $B000 is RAM ($01 = $36)."""
        d = self.directory()
        m = self.r(self.lab("cwin_map"), 256)
        lows = [(p >> 8) & 0xFF for p in d["pages"] if p is not None]
        bad = []
        for i in range(256):
            cnt = lows.count(i)
            want = (0x80 if cnt else 0) | (0x40 if (i + 1) & 0xFF in lows else 0) | cnt
            got = m[i] if cnt else m[i]
            mask = 0xC7
            if (got & mask) != want or (not cnt and got & 0x38):
                bad.append(f"map[${i:02X}] = ${m[i]:02X}, want ${want:02X}"
                           f"{' (hint any)' if cnt else ''}")
        return bad

    # raw REC STASH: DMABUF[0..len) -> guest addr, then mem_rec_dirty (C1)
    def raw_stash(self, addr: int, data: bytes) -> None:
        dirty = self.lab("mem_rec_dirty")
        self.w(DMABUF, data)
        code = bytes([
            0xA9, DMABUF & 0xFF, 0x8D, 0x02, 0xDF,          # C64 lo
            0xA9, DMABUF >> 8, 0x8D, 0x03, 0xDF,            # C64 hi
            0xA9, addr & 0xFF, 0x8D, 0x04, 0xDF,            # REU lo
            0xA9, (addr >> 8) & 0xFF, 0x8D, 0x05, 0xDF,     # REU hi
            0xA9, addr >> 16, 0x8D, 0x06, 0xDF,             # REU bank
            0xA9, len(data), 0x8D, 0x07, 0xDF,              # length lo
            0xA9, 0x00, 0x8D, 0x08, 0xDF,                   # length hi
            0x8D, 0x0A, 0xDF,                               # address control
            0xA9, 0x90, 0x8D, 0x01, 0xDF,                   # STASH, execute
            0x4C, dirty & 0xFF, dirty >> 8])                # jmp mem_rec_dirty
        self.w(DMASTUB, code)
        self.call(DMASTUB)

    def runstub(self, invall: bool, reset: bool, port: int | None = None) -> None:
        """RUNSTUB: [jsr emu_reset] [jsr code_invall] st_halt := 0;
        r_pc/r_a7 := BUF_CASE +0/+4; jsr emu_run; rts."""
        c = bytearray()
        if port is not None:                    # lda #port / sta $01
            c += bytes([0xA9, port, 0x85, 0x01])
        if reset:
            er = self.lab("emu_reset")
            c += bytes([0x20, er & 0xFF, er >> 8])
        if invall:
            ia = self.lab("code_invall")
            c += bytes([0x20, ia & 0xFF, ia >> 8])
        rpc, ra7 = self.lab("r_pc"), self.lab("r_a7")
        eru, sth = self.lab("emu_run"), self.lab("st_halt")
        c += bytes([0xA9, 0x00, 0x85, sth,              # st_halt := 0 (run 2)
                    0xA2, 0x03,
                    0xBD, rp.BUF_CASE & 0xFF, rp.BUF_CASE >> 8, 0x95, rpc,
                    0xBD, (rp.BUF_CASE + 4) & 0xFF, rp.BUF_CASE >> 8, 0x95, ra7,
                    0xCA, 0x10, 0xF3,
                    0x20, eru & 0xFF, eru >> 8,
                    0xA5, 0x01, 0x8D, PORTOUT & 0xFF, PORTOUT >> 8,   # $01 after
                    0x60])
        self.w(RUNSTUB, bytes(c))

    def run_raw(self, pc: int, invall: bool = False, reset: bool = True,
                timed: bool = False, port: int | None = None,
                timeout: float = 120.0) -> int | None:
        self.runstub(invall, reset, port)
        self.w(rp.BUF_CASE, struct.pack("<II", pc, STACK))
        if not timed:
            self.call(RUNSTUB, timeout)
            return None
        from tests.heap_cost import stub_code
        st = bytearray(stub_code(RUNSTUB))
        assert st[-1] == 0x60
        st[-1:] = bytes([0xA2, 0x03, 0xBD, 0x04, 0xDD, 0x9D, CYC & 0xFF, CYC >> 8,
                         0xCA, 0x10, 0xF7, 0x60])  # copy $DD04-07 -> CYC, rts
        self.w(STUB, bytes(st))
        self.call(STUB)
        a = self.r(CYC, 4)
        ta, tb = a[0] | a[1] << 8, a[2] | a[3] << 8
        return (0xFFFF - ta) + 0x10000 * (0xFFFF - tb)

    def regs(self) -> dict:
        b = self.r(self.lab("r_d0"), 70)
        out = {f"D{i}": struct.unpack_from("<I", b, 4 * i)[0] for i in range(8)}
        out.update({f"A{i}": struct.unpack_from("<I", b, 32 + 4 * i)[0] for i in range(8)})
        out["PC"] = struct.unpack("<I", self.r(self.lab("r_pc"), 4))[0]
        h = self.r(self.lab("st_halt"), 2)
        out["halt"], out["reason"] = h[0], h[1]
        return out


def vice(prg: str, fn):
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        try:
            prepare_target(tg, prg)
            return fn(Rig(tg, labels))
        finally:
            mgr.release(tg)


def u64(prg: str, out_dir: str, mhz: int, fn):
    """One DeviceLock hold (u64_hold: config snapshot, exact restore and
    reset after, SESSION.log); CPU speed set first and the machine
    rebooted, as tests/aros_boot.u64_session."""
    from c64_test_harness import Labels
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.bench.u64_session import u64_hold
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    os.makedirs(out_dir, exist_ok=True)
    t0, res, err, hold = time.time(), None, None, {}
    try:
        with u64_hold(out_dir) as hold:
            tg = hold["target"]
            tg.transport.set_speed(mhz)
            reboot(tg.transport.client)
            prepare_target(tg, prg)
            try:
                res = fn(Rig(tg, labels))
            finally:
                tg.transport.set_speed(1)
                reboot(tg.transport.client)
    except BaseException as e:  # noqa: BLE001 -- logged, then re-raised
        err = e
    with open(os.path.join(EVID, "SESSION.log"), "a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t0))} "
                 f"run_cwin host={os.environ.get('U64_HOST', '?')} mhz={mhz} "
                 f"prg={prg} out={out_dir} lock_wait_s={hold.get('lock_wait_s', '?')} "
                 f"restored={hold.get('restored')} reset_ok={hold.get('reset_ok')} "
                 f"wall_s={time.time() - t0:.0f}"
                 f"{' error=' + repr(err) if err else ''}\n")
    if err:
        raise err
    return res


# --------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------

def leg_smc(rig: Rig, rep: dict) -> list[str]:
    f: list[str] = []
    n = rig.lab("CWIN_N")
    cases = {"word": case_word(), "byte": case_byte(), "spill": case_spill(),
             "twobank": case_twobank(), "current": case_current(),
             "evict": case_evict(n),
             "hintval": case_hintval(),
             "carry_bank": case_carry(0x030000),
             "carry_wrap": case_carry(0x000000),
             "straddle_ext": case_straddle_ext(),
             "straddle_long": case_straddle_long()}
    out = rep.setdefault("smc", {})
    for name, (prog, regs, pc, icount) in cases.items():
        rig.stage(prog)
        res = rig.run(BASE, 2000)
        got = {k: res.regs[k] for k in regs}
        ok = (res.halt and res.reason == 0 and not res.timeout and
              got == regs and res.pc == pc and res.icount == icount)
        out[name] = {"ok": bool(ok), "regs": {k: f"{v:08X}" for k, v in got.items()},
                     "pc": f"{res.pc:06X}", "icount": res.icount,
                     "halt": res.halt, "timeout": res.timeout}
        log(f"  smc/{name}: {'ok' if ok else 'FAIL'} "
            f"{ {k: f'{v:08X}' for k, v in got.items()} } pc ${res.pc:06X} "
            f"icount {res.icount}")
        if ok and name.startswith("straddle") and n > 2:
            # both pages cached in windows that are NOT neighbours (window
            # k's byte 256 is not page B's byte 0): the word after A's end
            # must come from B's own window
            d = rig.directory()
            wa = d["pages"].index(BASE + 0x100) if BASE + 0x100 in d["pages"] else None
            wb = d["pages"].index(BASE + 0x200) if BASE + 0x200 in d["pages"] else None
            out[name]["windows"] = {"A": wa, "B": wb}
            if wa is None or wb is None or wb == wa + 1:
                ok = False
                out[name]["ok"] = False
                f.append(f"smc/{name}: A/B windows {wa}/{wb}: not both cached "
                         f"in non-adjacent windows (the test proves nothing)")
            log(f"  smc/{name}: A in window {wa}, B in window {wb}")
        if not ok and not (name.startswith("straddle") and f and f[-1].startswith(f"smc/{name}")):
            f.append(f"smc/{name}: regs {got} (want {regs}), pc ${res.pc:06X} "
                     f"(want ${pc:06X}), icount {res.icount} (want {icount}), "
                     f"halt {res.halt} timeout {res.timeout}")
    # the directory after an n-page ring (+ the entry page = n pages): every
    # page cached and valid, no window filled twice (FIFO hand advances)
    prog = ring(n - 1, 3)
    rig.stage(prog)
    res = rig.run(BASE, 2000)
    d = rig.directory()
    pages = [BASE + 0x100 * i for i in range(0, n)]   # entry page + ring
    have = sorted(p for p in d["pages"] if p is not None)
    ok = res.halt and res.regs["D2"] == 3 * (n - 1) and have == pages
    out["directory"] = {"ok": bool(ok), "dir": {**d, "pages": [None if p is None else f"{p:06X}"
                                                                for p in d["pages"]]}}
    log(f"  smc/directory: {'ok' if ok else 'FAIL'} {out['directory']['dir']}")
    # after an eviction-heavy ring the map must still describe exactly the
    # valid windows (a stale hint would leave evicted page-lows nonzero)
    rig.stage(ring(n + 2, 3))
    rig.run(BASE, 4000)
    me = rig.map_errors()
    out["map"] = {"ok": not me, "errors": me[:8], "n_errors": len(me)}
    log(f"  smc/map: {'ok' if not me else 'FAIL'} {me[:4]}")
    if me:
        f.append(f"smc/map: {len(me)} entries wrong, e.g. {me[:3]}")
    if not ok:
        f.append(f"smc/directory: {out['directory']['dir']} (want pages "
                 f"{[f'{p:06X}' for p in pages]})")
    return f


def leg_invall(rig: Rig, rep: dict) -> list[str]:
    """Run 1 caches page p (D0 = -1).  A raw STASH patches p behind the
    memory API.  Run 2 starts at a second entry WITHOUT emu_reset: with
    code_invall it sees MOVEQ #1 (D0 = 1); the control run (no code_invall)
    must see the stale MOVEQ #-1 -- else the leg proves nothing."""
    f: list[str] = []
    out = rep.setdefault("invall", {})
    p = BASE + 0x200
    e2, h1, h2 = BASE + 0x20, BASE + 0x10, BASE + 0x30
    prog = {BASE: [LEA_A0, hi(h1), lo(h1), JMP, hi(p), lo(p)],
            h1: [HALT],
            e2: [LEA_A0, hi(h2), lo(h2), JMP, hi(p), lo(p)],
            h2: [HALT],
            p: [0x70FF, JMP_A0]}
    for invall in (False, True):
        rig.stage(prog)
        rig.run_raw(BASE, reset=True)
        r1 = rig.regs()
        cached = p in rig.directory()["pages"]
        rig.raw_stash(p, bytes([0x70, 0x01]))
        rig.run_raw(e2, invall=invall, reset=False)
        r2 = rig.regs()
        key = "with_invall" if invall else "control"
        out[key] = {"run1_d0": f"{r1['D0']:08X}", "p_cached_after_run1": cached,
                    "run2_d0": f"{r2['D0']:08X}", "run2_halt": r2["halt"],
                    "run2_pc": f"{r2['PC']:06X}"}
        log(f"  invall/{key}: {out[key]}")
        if r1["D0"] != 0xFFFFFFFF or not cached:
            f.append(f"invall/{key}: run 1 did not leave p cached ({out[key]})")
            continue
        want = 1 if invall else 0xFFFFFFFF
        if r2["D0"] != want or r2["halt"] != 1 or r2["PC"] != h2 + 2:
            f.append(f"invall/{key}: run 2 D0 {r2['D0']:08X} (want {want:08X}), "
                     f"{out[key]}")
    return f


def leg_blt(rig: Rig, rep: dict) -> list[str]:
    """Blitter D rows into a cached, non-current window: a one-word row on
    page p, and a two-word row from p-2 whose LAST page is p (blt_dpage
    must drop the windows of both the first and the last page)."""
    f: list[str] = []
    out = rep.setdefault("blt", {})
    for name, last in (("noncur", False), ("lastpage", True)):
        prog, regs, pc, icount = case_blit(last)
        rig.stage(prog)
        res = rig.run(BASE, 2000)
        got = {k: res.regs[k] for k in regs}
        ok = (res.halt and res.reason == 0 and not res.timeout and got == regs
              and res.pc == pc and res.icount == icount)
        out[name] = {"ok": bool(ok), "regs": {k: f"{v:08X}" for k, v in got.items()},
                     "pc": f"{res.pc:06X}", "icount": res.icount}
        log(f"  blt/{name}: {'ok' if ok else 'FAIL'} {out[name]}")
        if not ok:
            f.append(f"blt/{name}: {out[name]} (want D0=1: stale window after "
                     f"a blit into a cached non-current page)")
    return f

def leg_bank(rig: Rig, rep: dict) -> list[str]:
    """CONTRACTS "Code-window cache", $01: a run entered with BASIC ROM in
    ($37, no window valid after emu_reset) must bank it out before its
    first window fetch and leave $36; and after the caller banks BASIC in
    again (windows still valid), emu_reset makes the next run correct.  A
    miss path that forgets to bank out fetches BASIC ROM bytes."""
    f: list[str] = []
    out = rep.setdefault("bank", {})
    prog = ring(3, 5)
    rig.stage(prog)
    for k in ("first", "again"):
        try:
            rig.run_raw(BASE, reset=True, port=0x37, timeout=15.0)
        except Exception as e:  # noqa: BLE001 -- a hang is this leg's failure
            out[k] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            f.append(f"bank/{k}: the run never returned in 15 s (fetched BASIC "
                     f"ROM bytes as code?): {e}")
            return f
        r = rig.regs()
        port = rig.r(PORTOUT, 1)[0]
        ok = r["halt"] == 1 and r["reason"] == 0 and r["D2"] == 15 and port == 0x36
        out[k] = {"ok": ok, "D2": r["D2"], "halt": r["halt"], "port_after": f"${port:02X}"}
        log(f"  bank/{k}: {out[k]}")
        if not ok:
            f.append(f"bank/{k}: {out[k]} (want D2 15, clean HALT, $01 = $36)")
    return f


BYTEW_N1, BYTEW_N2 = 40, 240


def bytew_prog(data: int, op: int, n: int, evict: bool) -> dict:
    """MOVE.W #n-1,D0; LEA data,A0; loop: op (MOVE.x D1,(A0)); DBRA; HALT.
    evict: first run through pages $0101-$0109 (9 pages > CWIN_N) and back,
    so page-lows $01-$03 were cached and evicted before the loop."""
    loop = BASE + 0x40
    prog = {loop: [0x303C, n - 1, LEA_A0, hi(data), lo(data), op, 0x51C8,
                   0xFFFC, HALT]}
    if not evict:
        prog[BASE] = [JMP, hi(loop), lo(loop)]
        return prog
    pages = [BASE + 0x100 * i for i in range(1, 10)]
    prog[BASE] = [JMP, hi(pages[0]), lo(pages[0])]
    for a, b in zip(pages, pages[1:] + [loop]):
        prog[a] = [JMP, hi(b), lo(b)]
    return prog


def leg_bytew(rig: Rig, rep: dict) -> list[str]:
    """The write check's cost per write size, measured like perf_cycles
    (CIA2-timed emu_run, two-point delta; cycles per instruction of MOVE.x
    D1,(A0) + DBRA).  Data page:
      other_pagelow  $0305xx: no cached page has its low byte (common path)
      evicted        $0301xx after pages $0101-$0109 ran (low $01 was cached,
                     then evicted): must cost the same as other_pagelow --
                     asserted (a stale map entry would send it slow)
      next_cached    $02FFxx: low $FF, and low $00 (the loop's page) cached:
                     bit 6 -- word/long writes take the slow path, no spill
      same_pagelow   $0300xx: low $00 = the loop's page, another bank: the
                     map ignores the bank, the slow path compares tags"""
    f: list[str] = []
    out = rep.setdefault("bytew", {"unit": "CIA2 phi2 cycles per instruction"})
    for case, data, ev in (("other_pagelow", 0x030500, False),
                           ("evicted", 0x030100, True),
                           ("next_cached", 0x02FF00, False),
                           ("same_pagelow", 0x030000, False)):
        for name, op in (("byte", 0x1081), ("word", 0x3081), ("long", 0x2081)):
            c = []
            for it in (BYTEW_N1, BYTEW_N2):
                rig.stage(bytew_prog(data, op, it, ev))
                c.append(rig.run_raw(BASE, timed=True))
            per = (c[1] - c[0]) / (BYTEW_N2 - BYTEW_N1) / 2
            out[f"{case}/{name}"] = round(per, 3)
            log(f"  bytew {case} {name}: {per:.3f} per instruction (MOVE + DBRA)")
            if case == "evicted":
                base = out[f"other_pagelow/{name}"]
                if per > base + 0.05 * base / 100 + 1.0:
                    f.append(f"bytew/evicted {name}: {per:.1f} per instruction "
                             f"against {base:.1f} on a never-cached page-low: "
                             f"an evicted page-low still sends writes slow")
    return f


BENCH_N1, BENCH_N2 = 20, 120


def leg_bench(rig: Rig, rep: dict, ks: list[int] | None = None) -> list[str]:
    """Cycles (VICE: 6502 cycles at 1 MHz; Ultimate: CIA2 phi2 = us) per
    page change: (ring - flat) over a two-point iteration delta."""
    f: list[str] = []
    try:
        n = rig.lab("CWIN_N")
    except KeyError:
        n = 1                   # the single-window runtime (main)
    ks = ks or sorted({2, n, n + 1, n + 2})
    out = rep.setdefault("bench", {"cwin_n": n, "unit": "CIA2 phi2 cycles"})

    def per_iter(prog_fn, k):
        c = []
        for it in (BENCH_N1, BENCH_N2):
            rig.stage(prog_fn(k, it))
            c.append(rig.run_raw(BASE, timed=True))
            r = rig.regs()
            if r["halt"] != 1 or r["reason"] != 0 or r["D2"] != k * it:
                raise RuntimeError(f"bench k={k} n={it}: no clean HALT ({r})")
        return (c[1] - c[0]) / (BENCH_N2 - BENCH_N1)

    # warm-up: the first timed run of a U64E session read back invalid
    # (negative, or 729 us) in 2 of 2 sessions; it is discarded
    rig.stage(ring_flat(2, 2))
    rig.run_raw(BASE, timed=True)
    for k in ks:
        ring_c, flat_c = per_iter(ring, k), per_iter(ring_flat, k)
        per_change = (ring_c - flat_c) / k
        kind = "switch" if k <= n else "refill"
        out[f"k{k}"] = {"kind": kind, "ring_per_iter": round(ring_c, 1),
                        "flat_per_iter": round(flat_c, 1),
                        "per_page_change": round(per_change, 1)}
        log(f"  bench k={k} ({kind}): ring {ring_c:.1f} flat {flat_c:.1f} "
            f"per iteration -> {per_change:.1f} per page change")
    return f


# --------------------------------------------------------------------------
# AROS counter boot
# --------------------------------------------------------------------------

def leg_aros(sizes: list[int], out_dir: str, rep: dict,
             timeout: float = 3600.0) -> list[str]:
    from host import machine
    from tests import aros_boot as AB
    from tests.rec_build import build
    f: list[str] = []
    rom, why = machine.find_rom()
    if rom is None:
        log(f"SKIP aros: {why}")
        rep["aros"] = {"skip": why}
        return f
    res = rep.setdefault("aros", {})
    marks = AB.MODEL_AT
    for n in sizes:
        d = os.path.join(out_dir, f"cnt{n}")
        log(f"[aros] counter build CWIN_N={n} -> {d}")
        prg = build(d, ["CWIN_COUNT", f"CWIN_N={n}"])

        def body(m, n=n):
            r = {"stage": m.stage(rom)}
            nins = m.lab("cw_nins")
            m.start(AB.BOOT_S_VICE)
            t0, at, polls = time.monotonic(), {}, []
            while True:
                b = m.r(nins, 20)
                ins, mis, ref, kil, slw = struct.unpack("<5I", b)
                el = time.monotonic() - t0
                polls.append((ins, mis, ref, kil))
                for k, (ic, _fr) in marks.items():
                    if k not in at and ins >= ic:
                        at[k] = {"instr": ins, "page_changes": mis,
                                 "refills": ref, "write_drops": kil,
                                 "write_slow_paths": slw,
                                 "refills_per_instr": round(ref / ins, 5),
                                 "wall_s": round(el, 1)}
                        log(f"  N={n} {k}: {at[k]}")
                if len(at) == len(marks) or el > timeout:
                    break
                time.sleep(1.0)
            r["at"] = at
            r["serial_head"] = ""
            try:
                n_ser = m.count()
                r["serial_bytes"] = n_ser
            except Exception as e:  # noqa: BLE001
                r["serial_bytes"] = f"? {e}"
            return r
        r = AB.vice_session(prg, body)
        res[f"N{n}"] = r
        if len(r["at"]) != len(marks):
            f.append(f"aros N={n}: reached only {sorted(r['at'])} in {timeout:.0f} s")
    return f


# --------------------------------------------------------------------------
# Mutants (<= 10, each built once; every one must fail a leg)
# --------------------------------------------------------------------------

MUTANTS = {
    "curonly": ("CWIN_MUT_CURONLY", "smc"),     # only the current window dropped
    "nospill": ("CWIN_MUT_NOSPILL", "smc"),     # a long's spill page not checked
    "nocarry": ("CWIN_MUT_NOCARRY", "smc"),     # spill page without the bank carry
    "nomapadd": ("CWIN_MUT_NOMAPADD", "smc"),   # a refill never enters the map
    "stalehint": ("CWIN_MUT_STALEHINT", "smc,bytew"),  # last drop keeps the hint
    "hintnoval": ("CWIN_MUT_HINTNOVAL", "smc"),  # hint taken without cw_val
    "nopatch": ("CWIN_MUT_NOPATCH", "smc"),     # fetch_word's loads not re-pointed
    "nobank": ("CWIN_MUT_NOBANK", "bank"),      # BASIC ROM left in: ROM bytes fetched
    "noinvall": ("CWIN_MUT_NOINVALL", "invall"),  # code_invall = the w_inv stub
    "bltfirst": ("CWIN_MUT_BLTFIRST", "blt"),   # blt_dpage drops the first page only
}


def teeth(only: list[str] | None, out_dir: str) -> int:
    from tests.rec_build import build
    rows, bad = {}, 0
    for name, (define, legs) in MUTANTS.items():
        if only and name not in only:
            continue
        d = os.path.join(out_dir, f"mut-{name}")
        log(f"[mutant {name}] -D {define}")
        try:
            prg = build(d, [define])
        except Exception as e:  # noqa: BLE001
            rows[name] = {"killed": False, "error": f"build: {e}"}
            bad += 1
            continue
        rep: dict = {}
        try:
            fails = run_legs(prg, legs.split(","), rep)
        except Exception as e:  # noqa: BLE001 -- a crash/timeout kills too
            fails = [f"exception: {type(e).__name__}: {e}"]
        rows[name] = {"define": define, "legs": legs, "killed": bool(fails),
                      "by": fails[:3]}
        log(f"[mutant {name}] {'KILLED' if fails else 'SURVIVED'}: {fails[:2]}")
        bad += 0 if fails else 1
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "mutants.json"), "w") as fh:
        json.dump(rows, fh, indent=1)
    print(f"mutants: {sum(r['killed'] for r in rows.values())} killed / "
          f"{len(rows) - sum(r['killed'] for r in rows.values())} survived")
    return 1 if bad else 0


LEGS = {"smc": leg_smc, "invall": leg_invall, "bench": leg_bench,
        "blt": leg_blt, "bank": leg_bank, "bytew": leg_bytew}


def run_legs(prg: str, legs: list[str], rep: dict) -> list[str]:
    fns = LEGS
    sel = [x for x in legs if x in fns]
    if not sel:
        return []

    def body(rig: Rig):
        f: list[str] = []
        for x in sel:
            log(f"[{x}]")
            g = fns[x](rig, rep)
            log(f"[{x}] {'PASS' if not g else 'FAIL'}")
            f += g
        return f
    return vice(prg, body)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.run_cwin",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--prg")
    ap.add_argument("--legs", default="smc,invall,blt,bank,bench,bytew")
    ap.add_argument("--backend", default="vice", choices=("vice", "u64"))
    ap.add_argument("--mhz", type=int, default=64)
    ap.add_argument("--sizes", default="1,4,7")
    ap.add_argument("--ks", default="")
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--out", default=EVID)
    ap.add_argument("--json")
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    if a.mutants:
        rc = teeth(a.only.split(",") if a.only else None,
                   os.path.join(a.out, "mutants"))
        print(f"wall {time.monotonic() - t0:.0f} s")
        return rc
    legs = [x for x in a.legs.split(",") if x]
    rep: dict = {"prg": a.prg, "legs": legs, "backend": a.backend}
    fails: list[str] = []
    if "aros" in legs:
        fails += leg_aros([int(x) for x in a.sizes.split(",")], a.out, rep,
                          a.timeout)
    rest = [x for x in legs if x != "aros"]
    if rest:
        if a.backend == "u64":
            rep["mhz"] = a.mhz
            ks = [int(x) for x in a.ks.split(",")] if a.ks else None

            def body(rig: Rig):
                f: list[str] = []
                for x in rest:
                    log(f"[{x}] on {os.environ.get('U64_HOST', '?')} at {a.mhz} MHz")
                    if x == "bench":
                        f += leg_bench(rig, rep, ks)
                    else:
                        f += LEGS[x](rig, rep)
                return f
            fails += u64(a.prg, os.path.join(a.out, f"u64-{a.mhz}"), a.mhz, body)
        else:
            fails += run_legs(a.prg, rest, rep)
    rep["fails"] = fails
    rep["wall_s"] = round(time.monotonic() - t0, 1)
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w") as fh:
            json.dump(rep, fh, indent=1)
    for x in fails:
        print(f"FAIL {x}")
    print(f"run_cwin: {'PASS' if not fails else f'FAIL ({len(fails)})'} "
          f"(wall {rep['wall_s']} s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
