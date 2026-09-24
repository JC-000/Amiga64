#!/usr/bin/env python3
"""C1 teeth test: every REC-shadow invalidation must be load-bearing.

The memory API caches REC register state between calls (runtime/core.s,
"REC shadow").  Contract C1 (docs/WAVE4_BRIEF.md) makes everything else
that touches the REC invalidate that shadow.  Each leg below disturbs the
REC the way something outside the memory API would, then checks guest-
visible results; a build with the matching invalidation removed must FAIL
the leg (REC_NOINVAL removes all of them; REC_MUT_* remove one each).

  refill     guest program: a data read, then a fetch-window refill (PC
             crosses into the next code page -- the refill programs the
             REC for a 256-byte DMA into code_win), then a second data
             read in the same page with a different low byte.  Needs the
             refill's own invalidation.            (mutant REC_MUT_REFILL)
  reset_case test_run_case, then a host-side "scribbler" reprograms every
             REC register and runs a DMA (standing in for REST DMA /
             Ultimate firmware / any foreign REC user), then a second
             test_run_case whose init-memory writes happen BEFORE the
             step's first window refill.  Needs emu_reset's invalidation.
                                                    (mutant REC_MUT_RESET)
  load_chunk test_run_case, scribbler, then test_load_chunk stages 16
             bytes; verified with an independent reference DMA reader
             (full REC program, no shadow).  Needs test_load_chunk's
             jsr mem_rec_dirty.                     (mutant REC_MUT_DIRTY)
  mailbox    (u64 only) scribbler, then a DIRECT host call of mem_read8
             through the mailbox dispatcher (run_program.peek_guest path).
             Needs the dispatcher's jsr mem_rec_dirty. (REC_MUT_DIRTY)

Data values are random per run (--seed): the REU is NOT cleared between
legs on hardware (run_prg reloads C64 RAM only), so fixed values could let
a stale-shadow write that went astray "pass" by reading back what an
earlier leg left there.

Every leg runs on a freshly acquired target.  Usage:
    python3 -m tests.rec_teeth --prg build/ws-a/runner.prg [--backend vice|u64]
    python3 -m tests.rec_teeth --prg build/ws-a-noinval/runner.prg \
        --expect-fail refill,reset_case,load_chunk
Exit 0: plain mode -- every leg passed; --expect-fail mode -- every named
leg FAILED (other legs are reported, not asserted).
"""

from __future__ import annotations

import argparse
import os
import random
import struct
import sys

from host import fixturegen, image
from tests import run_program as rp
from tests.case_format import Case, encode_case

SCRIB = 0xC600          # helper code in buf_case, above any record we use
REFRD = 0xC680
REFRD_BUF = 0xC6C0
REFRD_ADDR = 0xC6F0
SCRATCH = 0xC5F0        # C64 target of the scribbler's DMA

DATA = 0x009000         # guest data scratch (stack region, below PROG_BASE)


def _sta(addr):
    return [0x8D, addr & 0xFF, addr >> 8]


def scribbler() -> bytes:
    """Reprogram every REC register the data path shadows, then run a
    3-byte fetch without autoload (registers advance, LEN ends at 1)."""
    code = []
    for reg, val in ((0xDF02, SCRATCH & 0xFF), (0xDF03, SCRATCH >> 8),
                     (0xDF04, 0x00), (0xDF05, 0x00), (0xDF06, 0xFE),
                     (0xDF07, 0x03), (0xDF08, 0x00), (0xDF0A, 0x00),
                     (0xDF01, 0x91)):
        code += [0xA9, val] + _sta(reg)
    return bytes(code + [0x60])


def ref_reader() -> bytes:
    """Independent REU reader: 16 bytes REU[REFRD_ADDR u24] -> REFRD_BUF,
    every register programmed, no autoload."""
    code = [0xA9, REFRD_BUF & 0xFF] + _sta(0xDF02)
    code += [0xA9, REFRD_BUF >> 8] + _sta(0xDF03)
    for i, reg in enumerate((0xDF04, 0xDF05, 0xDF06)):
        code += [0xAD, (REFRD_ADDR + i) & 0xFF, (REFRD_ADDR + i) >> 8]
        code += _sta(reg)
    for reg, val in ((0xDF07, 16), (0xDF08, 0), (0xDF0A, 0), (0xDF01, 0x91)):
        code += [0xA9, val] + _sta(reg)
    return bytes(code + [0x60])


def rand_long(rng, avoid=()) -> bytes:
    """4 random bytes, nonzero, pairwise-distinct bytes, not in *avoid*."""
    while True:
        b = bytes(rng.sample(range(1, 256), 4))
        if b not in avoid:
            return b


class Leg:
    def __init__(self, target, labels, backend, rng=None):
        from c64_test_harness import write_bytes
        self.rng = rng or random.Random()
        self.target, self.labels, self.backend = target, labels, backend
        self.t = target.transport
        write_bytes(self.t, SCRIB, scribbler())
        write_bytes(self.t, REFRD, ref_reader())
        self.problems: list[str] = []

    def call(self, label_or_addr, timeout=30.0):
        from tests.harness_util import call_runner, lookup_label
        addr = label_or_addr if isinstance(label_or_addr, int) else \
            lookup_label(self.labels, label_or_addr)
        call_runner(self.target, addr, timeout)

    def scribble(self):
        self.call(SCRIB)

    def ref_read(self, addr) -> bytes:
        from c64_test_harness import read_bytes, write_bytes
        write_bytes(self.t, REFRD_ADDR, bytes([addr & 0xFF, (addr >> 8) & 0xFF,
                                               addr >> 16]))
        self.call(REFRD)
        return read_bytes(self.t, REFRD_BUF, 16)

    def run_case(self, name, data: bytes):
        """MOVE.L (A0),D1 at $1000 with A0 = DATA+$100 and the 4 data
        bytes as init memory; expects D1 = data (BE) and the bytes back."""
        from c64_test_harness import read_bytes, write_bytes
        from tests.run_vectors import RESULT_HDR_LEN, compare_case
        from tests.harness_util import BUF_CASE, BUF_RESULT, RES_MEM
        a0 = DATA + 0x100
        regs = [0] * 16
        regs[8] = a0
        regs[15] = 0x00F000
        exp = list(regs)
        exp[1] = int.from_bytes(data, "big")
        init = [(0x1000, 0x22), (0x1001, 0x10)] + \
            [(a0 + i, b) for i, b in enumerate(data)]
        c = Case(regs=regs, pc=0x1000, sr=0x0000, init_mem=init,
                 exp_mem=[(a0 + i, b) for i, b in enumerate(data)],
                 exp_regs=exp, exp_pc=0x1002,
                 exp_sr=0x0008 if data[0] & 0x80 else 0x0000)  # never 0
        raw = encode_case(c)
        c.raw = raw
        write_bytes(self.t, BUF_CASE, raw)
        self.call("test_run_case")
        hdr = read_bytes(self.t, BUF_RESULT, RESULT_HDR_LEN)
        mem = read_bytes(self.t, RES_MEM, len(c.exp_mem))
        for f, e, a in compare_case(c, hdr, mem, ccr_only=False):
            self.problems.append(f"{name}: {f} expected {e} got {a}")


# ------------------------------------------------------------------ legs

def leg_refill(leg: Leg):
    nops = (0xFE - 20) // 2
    words = [0x41F9, DATA >> 16, DATA & 0xFFFF,       # LEA DATA,A0
             0x20BC, 0x1111, 0x1111,                  # MOVE.L #$11111111,(A0)
             0x217C, 0x2222, 0x2222, 0x0004]          # MOVE.L #$22222222,4(A0)
    words += [0x4E71] * nops                          # NOP pad to +$FE
    words += [0x2210,                                 # +$FE: MOVE.L (A0),D1
              0x2428, 0x0004,                         # +$100: MOVE.L 4(A0),D2
              0xAFFF]                                 # HALT
    assert 2 * words.index(0x2210, 10) == 0xFE
    b = fixturegen.HunkBuilder()
    b.add_code(words)
    exp = rp.Expectation(regs={"D1": 0x11111111, "D2": 0x22222222,
                               "A0": DATA, "A7": image.STACK_TOP},
                         pc=image.BASE_ADDR + 0x106, icount=3 + nops + 3)
    sess = rp.Session(target=leg.target, labels=leg.labels, timeout=30.0)
    rp.run_fixture(sess, "refill", b.build(), exp, budget=1000)
    leg.problems += sess.failures


def leg_reset_case(leg: Leg):
    a = rand_long(leg.rng)
    leg.run_case("case1", a)
    leg.scribble()
    leg.run_case("case2-after-scribble", rand_long(leg.rng, avoid=(a,)))


def leg_load_chunk(leg: Leg):
    from c64_test_harness import read_bytes, write_bytes
    from tests.harness_util import BUF_CASE, BUF_RESULT
    leg.run_case("case1", rand_long(leg.rng))
    leg.scribble()
    payload = bytes(leg.rng.sample(range(256), 16))
    addr = DATA + 0x200
    write_bytes(leg.t, BUF_CASE, struct.pack("<II", addr, len(payload))
                + payload)
    leg.call("test_load_chunk")
    if read_bytes(leg.t, BUF_RESULT, 1)[0] != 0xFF:
        leg.problems.append("test_load_chunk did not complete")
    got = leg.ref_read(addr)
    if got != payload:
        leg.problems.append(f"staged bytes wrong: expected {payload.hex()} "
                            f"got {got.hex()}")


def leg_mailbox(leg: Leg):
    from c64_test_harness import read_bytes, write_bytes
    data = rand_long(leg.rng)
    leg.run_case("case1", data)
    # case1 leaves the shadow warm (readback mem_read8 of DATA+$103)
    leg.scribble()
    write_bytes(leg.t, rp.ZP_M_ADDR, struct.pack("<I", DATA + 0x101))
    leg.call("mem_read8")
    got = read_bytes(leg.t, rp.ZP_M_VAL, 1)[0]
    if got != data[1]:
        leg.problems.append(f"direct mem_read8 after scribble: expected "
                            f"{data[1]:02X} got {got:02X}")


LEGS = {"refill": leg_refill, "reset_case": leg_reset_case,
        "load_chunk": leg_load_chunk, "mailbox": leg_mailbox}
U64_ONLY = {"mailbox"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prg", required=True)
    ap.add_argument("--backend", default="vice", choices=["vice", "u64"])
    ap.add_argument("--expect-fail", default="",
                    help="comma list of legs that MUST fail (mutant build)")
    ap.add_argument("--legs", default="", help="comma list (default: all)")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target

    prg = os.path.abspath(args.prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    names = [n for n in (args.legs.split(",") if args.legs else LEGS)
             if args.backend == "u64" or n not in U64_ONLY]
    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    print(f"seed {seed}")
    rng = random.Random(seed)
    results = {}
    with UnifiedManager(backend=args.backend, vice_config=vice_config) as mgr:
        for name in names:
            target = mgr.acquire()
            try:
                prepare_target(target, prg)
                leg = Leg(target, labels, args.backend, rng)
                try:
                    LEGS[name](leg)
                except Exception as e:      # a wedge is a failure too
                    leg.problems.append(f"exception: {e!r}")
                results[name] = leg.problems
            finally:
                mgr.release(target)

    print(f"\nREC teeth legs, prg={args.prg} backend={args.backend} "
          f"seed={seed}")
    for name in names:
        probs = results[name]
        print(f"  {name:<11} {'PASS' if not probs else 'FAIL'}")
        for p in probs[:6]:
            print(f"      {p}")
    failed = {n for n in names if results[n]}
    if args.expect_fail:
        want = set(args.expect_fail.split(","))
        missing = want - failed
        if missing:
            print(f"NEGATIVE CONTROL FAILED: leg(s) {sorted(missing)} passed "
                  f"on a build that lacks their invalidation")
            return 1
        print(f"NEGATIVE CONTROL OK: {sorted(want)} failed as required")
        return 0
    if failed:
        print(f"FAIL: {sorted(failed)}")
        return 1
    print("all legs passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
