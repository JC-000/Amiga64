#!/usr/bin/env python3
"""Run the C1 teeth matrix (tests/rec_teeth.py) over the normal build and
every invalidation mutant, building the mutants first (tests/rec_build.py).
Writes one log per row under --evidence and prints a summary table.

    python3 -m tests.rec_matrix [--backend vice|u64] [--evidence DIR]

Rows (build flags -> legs that must FAIL):
    normal          (none)          -> none; every leg must pass
    REC_NOINVAL     all invalidation -> refill, reset_case, load_chunk, foreign (+mailbox on u64)
    REC_MUT_REFILL  refill only     -> refill
    REC_MUT_RESET   emu_reset only  -> reset_case
    REC_MUT_DIRTY   mem_rec_dirty   -> load_chunk, foreign (+mailbox on u64)
    src-actrl/lenhi/c64hi  mio_full minus one cold-path store -> foreign
    src-bootrok     boot stub leaves m_rok set -> boot
  plus the direct-API sweep (tests/rec_apisweep.py): normal build passes,
  the src-s-pred-odd / src-s-nostraddle2 / src-s-noy mutants must fail.
Exit 0 iff every row behaves as required.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from tests.rec_build import ROOT, build

ROWS = [
    ("normal", [], None),
    ("noinval", ["REC_NOINVAL"], ["refill", "reset_case", "load_chunk",
                                  "mailbox", "foreign"]),
    ("mut-refill", ["REC_MUT_REFILL"], ["refill"]),
    ("mut-reset", ["REC_MUT_RESET"], ["reset_case"]),
    ("mut-dirty", ["REC_MUT_DIRTY"], ["load_chunk", "mailbox", "foreign"]),
    # source mutants of the cold path (tests/rec_build.py MUTANTS): the
    # scribbler leaves ACTRL/LENHI/C64HI hostile, so mio_full must write them
    ("src-actrl", "actrl", ["foreign"]),
    ("src-lenhi", "lenhi", ["foreign"]),
    ("src-c64hi", "c64hi", ["foreign"]),
    ("src-bootrok", "bootrok", ["boot"]),
]

# Direct-API sweep rows (VICE only): (name, mutant or None, must fail?)
SWEEP_ROWS = [
    ("sweep-normal", None, False),
    ("sweep-s-pred-odd", "s-pred-odd", True),
    ("sweep-s-nostraddle2", "s-nostraddle2", True),
    ("sweep-s-noy", "s-noy", True),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="vice", choices=["vice", "u64"])
    ap.add_argument("--evidence", default="build/evidence/ws-a/green")
    ap.add_argument("--rows", default="", help="comma list of row names")
    args = ap.parse_args(argv)
    ev = os.path.join(ROOT, args.evidence)
    os.makedirs(ev, exist_ok=True)
    rows = [r for r in ROWS if not args.rows or r[0] in args.rows.split(",")]
    summary = []
    ok_all = True
    for name, flags, must_fail in rows:
        out = "build/ws-a" if not flags else f"build/ws-a-{name}"
        if isinstance(flags, str):                  # source mutant
            prg = build(out, (), mutant=flags)
            flags = [f"mutant:{flags}"]
        else:
            prg = build(out, flags)
        cmd = [sys.executable, "-m", "tests.rec_teeth", "--prg", prg,
               "--backend", args.backend]
        if must_fail:
            legs = [l for l in must_fail
                    if (args.backend == "u64" or l != "mailbox")
                    and (args.backend == "vice" or l != "boot")]
            cmd += ["--expect-fail", ",".join(legs)]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        log = os.path.join(ev, f"rec_teeth_{args.backend}_{name}.txt")
        text = r.stdout + r.stderr
        tail = text[text.find("REC teeth legs"):] if "REC teeth legs" in text \
            else text[-3000:]
        with open(log, "w") as f:
            f.write("$ " + " ".join(os.path.relpath(c, ROOT)
                                    if c.startswith(ROOT) else c
                                    for c in cmd) + "\n")
            f.write(tail + f"\nexit={r.returncode}\n")
        ok = r.returncode == 0
        ok_all &= ok
        summary.append((name, " ".join(flags) or "-", ok, tail))
    if args.backend == "vice":
        for name, mut, must_fail in SWEEP_ROWS:
            if args.rows and name not in args.rows.split(","):
                continue
            out = "build/ws-a" if not mut else f"build/ws-a-src-{mut}"
            prg = build(out, (), mutant=mut)
            cmd = [sys.executable, "-m", "tests.rec_apisweep", "--prg", prg]
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            text = r.stdout + r.stderr
            ok = (r.returncode != 0) if must_fail else (r.returncode == 0)
            ok_all &= ok
            with open(os.path.join(ev, f"rec_apisweep_{name}.txt"), "w") as f:
                f.write("$ " + " ".join(os.path.relpath(c, ROOT)
                                        if c.startswith(ROOT) else c
                                        for c in cmd) + "\n")
                f.write(text[-6000:] + f"\nexit={r.returncode} (must "
                        f"{'FAIL' if must_fail else 'pass'})\n")
            last = [l for l in text.splitlines() if " cases, " in l]
            summary.append((name, f"mutant:{mut}" if mut else "-", ok,
                            "\n" + (last[-1] if last else text[-300:])))
    print(f"\nC1 teeth matrix ({args.backend})")
    for name, flags, ok, tail in summary:
        print(f"== {name:<11} flags={flags:<15} {'OK' if ok else 'NOT OK'}")
        for line in tail.splitlines()[1:]:
            if line.strip():
                print("   " + line)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
