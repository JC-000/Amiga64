"""Exact 6502 cycle accounting of the interpreter's hot path in VICE.

Runs the loop programs of tests/rec_budget.py (mem/stream/rmw/copy/alu/stack)
in two modes and times each whole call with the CIA2 timers
(tests/heap_cost.py technique: timer A counts phi2, B counts A's underflows
-> an exact 32-bit cycle count in VICE).  The screen is switched off (DEN=0)
first so VIC-II badline DMA cannot steal cycles at a random phase.  A
two-point delta over N1/N2 iterations cancels setup, window refills and the
final halt, leaving cycles per loop iteration and per guest instruction.

Modes (both measured, run first):
  run   emu_reset + emu_run from a small stub: the PRODUCT path (one emu_run
        per program, no per-instruction runner code) -- the headline number;
  step  test_run_program: the program runner's emu_step-per-instruction loop
        (tests/runner/run_program.s), i.e. what run_dos/run_vbcc/run_con time.

    python3 -m tests.perf_cycles --prg build/X/runner.prg [--exc]
        [--base build/Y/runner.prg] [--max-delta 0] [--json-out F]

--exc      set st_exc before every run (exception-mode loop, docs/CONTRACTS.md)
--base     measure a second PRG and print per-instruction deltas (X - Y)
--max-delta D   exit 1 if any loop's delta exceeds D cycles/instruction
VICE only.  Method and reference numbers: docs/PERF.md (f).
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys

from host import fixturegen, hunk, image

STUB = 0xC700          # buf_case tail: loop images are staged far below it
RUNSTUB = 0xC680       # emu_reset + PC/A7 load + emu_run (run mode)
N1, N2 = 40, 240


def measure(prg: str, exc: bool, allow_variant: bool = False,
            mode: str = "run") -> dict:
    from c64_test_harness import (Labels, UnifiedManager, ViceConfig,
                                  read_bytes, write_bytes)
    from tests.harness_util import REU_VICE_ARGS, call_runner, lookup_label
    from tests.heap_cost import stub_code
    from tests.rec_budget import LOOPS, _prog, _stack_prog
    from tests.run_program import BUF_CASE, RESULT_LEN, RunResult, Session
    from tests.run_vectors import prepare_target

    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    try:
        if lookup_label(labels, "emu_brnc_enforced") == 0 and not allow_variant:
            raise SystemExit(f"{prg}: built with BRNC_WARN_ONLY or a variant "
                             f"define (emu_brnc_enforced = 0): numbers would "
                             f"not describe the gate build")
    except KeyError:
        pass                                    # pre-guard build (e.g. base)
    try:
        st_exc = lookup_label(labels, "st_exc")
    except KeyError:
        if exc:
            raise SystemExit(f"{prg}: no st_exc label (pre-W6-S build)")
        st_exc = None
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    out: dict = {}
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        try:
            prepare_target(tg, prg)
            t = tg.transport
            write_bytes(t, 0xD011, bytes([0x0B]))          # DEN = 0
            if read_bytes(t, 0xD011, 1)[0] & 0x10:
                raise RuntimeError("could not blank the screen")
            sess = Session(target=tg, labels=labels, timeout=120.0)
            if mode == "step":
                run_addr = lookup_label(labels, "test_run_program")
            else:
                er = lookup_label(labels, "emu_reset")
                eru = lookup_label(labels, "emu_run")
                rpc = lookup_label(labels, "r_pc")
                ra7 = lookup_label(labels, "r_a7")
                write_bytes(t, RUNSTUB, bytes([
                    0x20, er & 0xFF, er >> 8,               # jsr emu_reset
                    0xA2, 0x03,                             # ldx #3
                    0xBD, BUF_CASE & 0xFF, BUF_CASE >> 8,   # lda PC,x
                    0x95, rpc,                              # sta r_pc,x
                    0xBD, (BUF_CASE + 4) & 0xFF, BUF_CASE >> 8,
                    0x95, ra7,                              # sta r_a7,x
                    0xCA, 0x10, 0xF3,                       # dex / bpl
                    0x20, eru & 0xFF, eru >> 8,             # jsr emu_run
                    0x60]))
                run_addr = RUNSTUB
                halt_l = lookup_label(labels, "st_halt")

            def run(blob: bytes) -> tuple[int, int]:
                exe = hunk.parse(blob)
                chunks, man = image.build_image(exe)
                sess.stage(chunks)
                write_bytes(t, BUF_CASE, struct.pack(
                    "<III", man["entry"], image.STACK_TOP, 0))
                write_bytes(t, STUB, stub_code(run_addr))
                if st_exc is not None:
                    write_bytes(t, st_exc, bytes([1 if exc else 0]))
                try:
                    call_runner(tg, STUB, 120.0)
                finally:
                    if st_exc is not None:
                        write_bytes(t, st_exc, b"\x00")
                a = read_bytes(t, 0xDD04, 4)
                ta, tb = a[0] | a[1] << 8, a[2] | a[3] << 8
                cyc = (0xFFFF - ta) + 0x10000 * (0xFFFF - tb)
                if mode == "step":
                    rr = RunResult.from_bytes(read_bytes(t, 0xC800, RESULT_LEN))
                    if not (rr.halt and rr.reason == 0):
                        raise RuntimeError(f"loop did not HALT cleanly: {rr}")
                    return cyc, rr.icount
                h, r = read_bytes(t, halt_l, 2)
                if not (h and r == 0):
                    raise RuntimeError(f"loop did not HALT cleanly: {h} {r}")
                return cyc, None

            progs = {n: (lambda k, s=s, bw=bw: _prog(bw, k, s))
                     for n, (s, bw, _i) in LOOPS.items()}
            progs["stack"] = _stack_prog
            ipi = {n: v[2] for n, v in LOOPS.items()}
            ipi["stack"] = 3
            for name, build in progs.items():
                c1, i1 = run(build(N1))
                c2, i2 = run(build(N2))
                per = (c2 - c1) / (N2 - N1)
                out[name] = {"cyc_per_iter": per,
                             "cyc_per_instr": per / ipi[name],
                             "cyc": [c1, c2], "icount": [i1, i2]}
            b = fixturegen.HunkBuilder()
            b.add_code([0xAFFF])
            out["halt_only_program"] = {"cyc": run(b.build())[0]}
        finally:
            mgr.release(tg)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.perf_cycles",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--exc", action="store_true")
    ap.add_argument("--base", default=None)
    ap.add_argument("--max-delta", type=float, default=None)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--modes", default="run,step",
                    help="comma list of run (emu_run, product path) and/or "
                         "step (test_run_program); default both, run first")
    ap.add_argument("--allow-variant", action="store_true",
                    help=argparse.SUPPRESS)     # A/B of variant builds only
    args = ap.parse_args(argv)
    res = {"prg": args.prg, "exc": args.exc, "modes": {}}
    fails = []
    for mode in args.modes.split(","):
        r = {"loops": measure(args.prg, args.exc, args.allow_variant, mode)}
        if args.base:
            r["base"] = measure(args.base, False, True, mode)
        res["modes"][mode] = r
        print(f"[{mode}] 6502 cycles per guest instruction ({N1}/{N2}-"
              f"iteration delta, screen off), prg={args.prg}"
              f"{' [st_exc=1]' if args.exc else ''}")
        for name, lp in r["loops"].items():
            if "cyc_per_instr" not in lp:
                continue
            line = f"  {name:7} {lp['cyc_per_instr']:9.3f}"
            if args.base:
                b = r["base"][name]["cyc_per_instr"]
                d = lp["cyc_per_instr"] - b
                line += f"   base {b:9.3f}   delta {d:+.3f}"
                if args.max_delta is not None and d > args.max_delta + 1e-9:
                    fails.append(f"[{mode}] {name}: {d:+.3f} > {args.max_delta}")
            print(line)
        once = r["loops"]["halt_only_program"]["cyc"]
        print(f"  one-off per call (halt-only program): {once}"
              + (f"   base {r['base']['halt_only_program']['cyc']}"
                 if args.base else ""))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(res, f, indent=1)
    if fails:
        print("DELTA FAIL: " + "; ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
