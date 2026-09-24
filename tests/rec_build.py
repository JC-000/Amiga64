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


# Source mutants of runtime/core.s (reviewer-style: one store or branch
# removed).  Each must be killed by a teeth leg (tests/rec_matrix.py).
MUTANTS = {
    # mio_full forgets the address-control store (scribbler leaves $80)
    "actrl": ("        sta REC_ACTRL           ; both addresses increment\n"
              "        lda #<t_tmp\n", "        lda #<t_tmp\n"),
    # mio_full forgets the length-high store (scribbler leaves $01)
    "lenhi": ("        sta REC_LENHI\n        sta REC_C64HI", "        sta REC_C64HI"),
    # mio_full forgets the C64-address-high store (scribbler leaves $C5)
    "c64hi": ("        sta REC_C64HI           ; C64 base = $0070 (t_tmp)\n",
              ""),
}


def build(out_dir: str, defines: list[str] | tuple[str, ...] = (),
          mutant: str | None = None) -> str:
    """Build runner.prg (+ .lbl/.map) into *out_dir*; return the PRG path.
    *mutant*: name in MUTANTS -- runtime/core.s is copied into *out_dir*
    with that one edit and assembled in its place."""
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
    if mutant:
        a, b = MUTANTS[mutant]
        core = os.path.join(ROOT, "runtime", "core.s")
        text = open(core).read()
        if text.count(a) != 1:
            raise RuntimeError(f"mutant {mutant}: anchor found "
                               f"{text.count(a)} times")
        mpath = os.path.join(out_dir, f"core_mut_{mutant}.s")
        with open(mpath, "w") as f:
            f.write(text.replace(a, b))
        srcs = [mpath if s == core else s for s in srcs]
        cmd += ["--asm-include-dir", "runtime"]
    cmd += [os.path.relpath(s, ROOT) for s in srcs] + [gen]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"variant build failed: {' '.join(cmd)}\n"
                           f"{r.stdout}\n{r.stderr}")
    return prg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--mutant", choices=sorted(MUTANTS))
    ap.add_argument("defines", nargs="*")
    args = ap.parse_args(argv)
    print(build(args.out, args.defines, args.mutant))
    return 0


if __name__ == "__main__":
    sys.exit(main())
