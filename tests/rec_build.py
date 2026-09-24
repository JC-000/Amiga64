"""Variant builds of the runner PRG with ca65 defines (WS-A test tooling).

The Makefile has no hook for assembler defines, so this mirrors its
`$(BUILD)/runner.prg` recipe (same sources, same order, same cfg) and adds
`--asm-define NAME` per flag.  Used by tests/rec_teeth.py (REC_NOINVAL /
REC_MUT_* mutants) and for the FETCH_DIRECT compile check:

    python3 -m tests.rec_build --out build/ws-a-noinval REC_NOINVAL
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_sources() -> list[str]:
    """The Makefile's RUNTIME_SRC, in make's own order (ask make)."""
    r = subprocess.run(["make", "-s", "--no-print-directory", "-f", "-",
                        "x"], cwd=ROOT, capture_output=True, text=True,
                       check=True,
                       input="include Makefile\nx:\n\t@echo $(RUNTIME_SRC)\n")
    return [os.path.join(ROOT, p) for p in r.stdout.split()]


def build(out_dir: str, defines: list[str] | tuple[str, ...] = ()) -> str:
    """Build runner.prg (+ .lbl/.map) into *out_dir*; return the PRG path."""
    out_dir = os.path.join(ROOT, out_dir) if not os.path.isabs(out_dir) \
        else out_dir
    gen = os.path.join(out_dir, "gen", "dispatch.s")
    os.makedirs(os.path.dirname(gen), exist_ok=True)
    subprocess.run([sys.executable, "host/gen_dispatch.py", "-o", gen],
                   cwd=ROOT, check=True)
    srcs = make_sources()
    prg = os.path.join(out_dir, "runner.prg")
    cmd = ["cl65", "-t", "none", "-C", "runtime/c64.cfg", "-o", prg,
           "-Ln", os.path.join(out_dir, "runner.lbl"),
           "-m", os.path.join(out_dir, "runner.map")]
    for d in defines:
        cmd += ["--asm-define", d]
    cmd += [os.path.relpath(s, ROOT) for s in srcs] + [gen]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"variant build failed: {' '.join(cmd)}\n"
                           f"{r.stdout}\n{r.stderr}")
    return prg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("defines", nargs="*")
    args = ap.parse_args(argv)
    print(build(args.out, args.defines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
