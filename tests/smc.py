"""Targeted self-modifying-code check for the instruction-stream window.

Two guest programs, run through the real M0 pipeline (fixturegen -> hunk ->
image -> staging -> emu_run) with architectural expectations:

  midpage : MOVE.W #$7001,(target).l overwrites the program's own NEXT
            instruction (originally MOVEQ #-1,D0 = $70FF) inside the cached
            code page, then falls into it.  A stale window executes the old
            MOVEQ #-1 (D0=$FFFFFFFF); correct invalidation gives D0=1.

  boundary: MOVE.W #$0070,($00FFFF).l starts one byte BELOW the code page
            and spills its second byte onto $010000, patching the entry
            word BRA.S(+2) ($6002) into MOVEQ #2,D0 ($7002); the program
            then branches back to the entry.  Exercises the cross-page
            branch of the write-overlap check.  A stale window re-runs the
            original BRA.S and loops forever (budget timeout).

Usage: python3 smc_test.py --prg <runner.prg> [--backend auto|vice|u64]
Exit 0 = all checks passed.
"""

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from host import image  # noqa: E402
from host.fixturegen import OP_HALT, HunkBuilder  # noqa: E402
from tests import run_program as rp  # noqa: E402

OP_MOVE_W_IMM_ABSL = 0x33FC  # MOVE.W #imm,(abs.L)


def smc_midpage():
    b = HunkBuilder()
    code = b.add_code([
        OP_MOVE_W_IMM_ABSL,      # $010000: MOVE.W #$7001,(target).l
        0x7001,                  #   imm = MOVEQ #1,D0
        0x0000, 0x0008,          #   abs.L, RELOC32-patched to base+8
        0x70FF,                  # $010008: target: MOVEQ #-1,D0 (overwritten)
        OP_HALT,                 # $01000A
    ])
    b.add_reloc32(code, code, [4])
    exp = rp.Expectation(regs={"D0": 1, "A7": image.STACK_TOP},
                         pc=image.BASE_ADDR + 12, icount=3)
    return b.build(), exp


def smc_boundary():
    base = image.BASE_ADDR
    below = base - 1             # write covers [base-1, base]
    b = HunkBuilder()
    code = b.add_code([
        0x6002,                  # base+0: entry: BRA.S +2 -> main
        OP_HALT,                 # base+2: reached only via the patched entry
        OP_MOVE_W_IMM_ABSL,      # base+4: main: MOVE.W #$0070,(below).l
        0x0070,                  #   guest BE: [base-1]=$00, [base]=$70
        (below >> 16) & 0xFFFF,  #   -> entry word becomes $7002 = MOVEQ #2,D0
        below & 0xFFFF,
        0x60F2,                  # base+12: BRA.S -14 -> entry (now MOVEQ)
    ])
    exp = rp.Expectation(regs={"D0": 2, "A7": image.STACK_TOP},
                         pc=base + 4, icount=5)
    return b.build(), exp


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--prg", required=True)
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "vice", "u64"])
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--expect-fail", action="store_true",
                    help="negative control: assert the checks FAIL "
                         "(for a WINDOW_NOINVAL build)")
    args = ap.parse_args(argv)

    prg = os.path.abspath(args.prg)
    labels_path = os.path.splitext(prg)[0] + ".lbl"

    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target

    labels = Labels.from_file(labels_path)
    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    jobs = [("smc-midpage", *smc_midpage()),
            ("smc-boundary", *smc_boundary())]
    with UnifiedManager(backend=args.backend,
                        vice_config=vice_config) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            sess = rp.Session(target=target, labels=labels,
                              timeout=args.timeout)
            for name, blob, exp in jobs:
                # budget 100: a stale-window boundary case loops forever
                rp.run_fixture(sess, name, blob, exp, budget=100)
                print()
        finally:
            mgr.release(target)

    n_ok, n_bad = len(sess.checks), len(sess.failures)
    print(f"{n_ok}/{n_ok + n_bad} checks passed")
    if args.expect_fail:
        if n_bad:
            print(f"NEGATIVE CONTROL OK: {n_bad} check(s) failed as expected")
            return 0
        print("NEGATIVE CONTROL FAILED: stale-window build passed?!")
        return 1
    for f in sess.failures:
        print(f"FAIL  {f}")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
