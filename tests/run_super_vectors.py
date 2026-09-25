"""Supervisor-mode / exception vector driver (W6-S, docs/CONTRACTS.md
"Supervisor mode & exceptions").

Usage:
    python3 -m host.vectors --set super --offline --out build/vectors-super
    python3 -m tests.run_super_vectors --prg build/<b>/runner.prg \\
        --vectors build/vectors-super [--filter SUBSTR] [--jobs N]

Runs the extended super batches (host/vectors.py "Super batch format")
through the UNCHANGED runner entry ``test_run_case``.  The two pieces of
state the standard case record cannot carry live in the interpreter's zero
page, which ``emu_reset`` deliberately leaves alone, so the host sets them
over the monitor before each call:

* ``st_exc``  -- the exception-delivery flag (case flags bit 0);
* ``r_osp``   -- the inactive stack pointer (USP while S=1, else SSP).

Two one-off checks run first on every fresh target: ``st_exc`` must read 0
straight after boot (the boot stub clears it; BASIC's CHRGET lives there
before), and the RESET hook must fire -- the driver points
``emu_rst_hook``'s jmp at a counter stub, runs SUPERHAND's "RESET (S)"
case and requires exactly one call.

With the flag clear the driver also plants a sentinel in ``st_ipc`` and
requires it untouched afterwards: proof that ``emu_reset`` latched the
plain step loop (the exception-mode loop rewrites ``st_ipc`` every
instruction), i.e. that the flag really gates the hot path.

After the call it reads ``r_osp`` back and compares it, plus the usual
registers/PC/full SR/memory (tests/run_vectors.compare_case), and the
expected halt state: a case may expect a halt (STOP, the address-error
reason, or -- with the flag clear -- today's HALT_* reasons), which
run_vectors would report as a failure.  ``st_exc`` is cleared again at the
end of every batch.  The golden suite (tests/run_vectors.py on
build/vectors) never touches either byte.
"""

from __future__ import annotations

import argparse
import glob
import os
import struct
import sys
import time

from c64_test_harness import (Labels, UnifiedManager, ViceConfig,
                              read_bytes, write_bytes)

from host.vectors import (FLAG_EXC, HALT_ALINE, HALT_ILLEGAL, HALT_OK,
                          RUN_SSP, RUN_USP,
                          decode_super_batch, pack_super_norms,
                          super_hand_cases, super_run_programs)
from tests.case_format import REG_NAMES, Case
from tests.harness_util import (BUF_CASE, BUF_RESULT, REU_VICE_ARGS, RES_MEM,
                                call_runner, lookup_label)
from tests.run_vectors import (RESULT_HDR_LEN, Failure, RunContext,
                               compare_case, prepare_target)


def _case(n: dict) -> Case:
    return Case(n["regs"], n["pc"], n["sr"], n["init_mem"], n["exp_mem"],
                n["exp_regs"], n["exp_pc"], n["exp_sr"], raw=n["raw"])


IPC_SENTINEL = 0xA55A5AA5


def compare_super(n: dict, hdr: bytes, mem: bytes, osp: int, exc: int,
                  ipc: int | None = None) -> list[tuple[str, str, str]]:
    diffs = [d for d in compare_case(_case(n), hdr, mem, ccr_only=False)
             if d[0] != "st_halt"]
    if hdr[0] == 0xFF:
        if n["exp_halt"]:
            if hdr[1] == 0 or hdr[2] != n["exp_reason"]:
                diffs.append(("st_halt", f"halt reason={n['exp_reason']:02X}",
                              f"halt={hdr[1]:02X} reason={hdr[2]:02X}"))
        elif hdr[1] != 0:
            diffs.append(("st_halt", "00",
                          f"halt={hdr[1]:02X} reason={hdr[2]:02X}"))
        if osp != n["exp_osp"]:
            diffs.append(("OSP", f"{n['exp_osp']:08X}", f"{osp:08X}"))
        if exc != (n["flags"] & FLAG_EXC):
            diffs.append(("st_exc", f"{n['flags'] & FLAG_EXC:02X}",
                          f"{exc:02X}"))
        if ipc is not None and ipc != IPC_SENTINEL:
            diffs.append(("st_ipc (plain loop latched?)",
                          f"{IPC_SENTINEL:08X}", f"{ipc:08X}"))
    return diffs


HOOK_STUB = 0xCF80      # buf_result tail, unused by the runner
HOOK_COUNT = 0xCF90
RUN_STUB = 0xCF00       # emu_run stub + its 8-byte PC/A7 block (buf_result tail)
RUN_ARGS = 0xCF40


def target_checks(target, addrs: dict, ctx: RunContext) -> None:
    """Boot-state and RESET-hook checks on a freshly prepared target."""
    tr = target.transport
    fails = []
    boot = read_bytes(tr, addrs["st_exc"], 1)[0]
    if boot != 0:
        fails.append(("boot st_exc", "00", f"{boot:02X}"))
    if "st_exl" in addrs:                   # latched copy, cleared by the stub
        boot = read_bytes(tr, addrs["st_exl"], 1)[0]
        if boot != 0:
            fails.append(("boot st_exl", "00", f"{boot:02X}"))
    hook = addrs.get("emu_rst_hook")
    if hook is not None:
        (n,) = [c for c in super_hand_cases() if c["name"] == "RESET (S)"]
        n = decode_super_batch(pack_super_norms([n]))[0]
        old = read_bytes(tr, hook + 1, 2)
        write_bytes(tr, HOOK_STUB, bytes([0xEE, HOOK_COUNT & 0xFF,
                                          HOOK_COUNT >> 8, 0x60]))  # inc; rts
        write_bytes(tr, HOOK_COUNT, b"\x00")
        write_bytes(tr, hook + 1, HOOK_STUB.to_bytes(2, "little"))
        try:
            write_bytes(tr, BUF_CASE, n["raw"])
            write_bytes(tr, addrs["st_exc"], b"\x01")
            write_bytes(tr, addrs["r_osp"], n["osp"].to_bytes(4, "little"))
            call_runner(target, addrs["entry"], ctx.timeout)
            calls = read_bytes(tr, HOOK_COUNT, 1)[0]
            if calls != 1:
                fails.append(("RESET hook calls", "1", str(calls)))
        finally:
            write_bytes(tr, hook + 1, old)
            write_bytes(tr, addrs["st_exc"], b"\x00")
    fails += _midrun_check(target, addrs, ctx)
    if fails:
        ctx.add_failures([Failure("target-checks", None, 0, f, e, a)
                          for f, e, a in fails])


def _midrun_check(target, addrs: dict, ctx: RunContext) -> list:
    """Review F2: st_exc written AFTER emu_reset must change nothing.  A
    flag-clear case latches the plain loop; then st_exc := 1 and the same
    opcode is stepped again with a bare emu_step.  It must return with
    today's halt, registers/SR untouched, and without resuming a stale
    6502 frame (RES_DONE sentinel: test_run_case's tail must not run).
    TRAP #0 goes through G5's decode, A-line through emu_halt: a live
    st_exc read raises vector 10 there, or unwinds to a stale st_sp6."""
    tr = target.transport
    fails = []
    for cname, reason_exp in (("flag clear: TRAP #0 (S)", HALT_ILLEGAL),
                              ("flag clear: A-line $A000 (S)", HALT_ALINE)):
        (n,) = [c for c in super_hand_cases() if c["name"] == cname]
        n = decode_super_batch(pack_super_norms([n]))[0]
        label = f"mid-run st_exc flip [{cname[12:]}]"
        try:
            write_bytes(tr, BUF_CASE, n["raw"])
            write_bytes(tr, addrs["st_exc"], b"\x00")
            call_runner(target, addrs["entry"], ctx.timeout)   # latch 0
            write_bytes(tr, addrs["st_exc"], b"\x01")           # the flip
            write_bytes(tr, addrs["r_pc"], n["pc"].to_bytes(4, "little"))
            write_bytes(tr, addrs["st_halt"], b"\x00\x00")
            write_bytes(tr, BUF_RESULT, b"\x5A")                # RES_DONE
            try:
                call_runner(target, addrs["emu_step"], 10.0)
            except Exception as e:  # noqa: BLE001 -- the wedge IS the failure
                fails.append((label, "emu_step returns", repr(e)[:80]))
                continue
            halt, reason = read_bytes(tr, addrs["st_halt"], 2)
            z = read_bytes(tr, addrs["r_d0"], 70)
            pc = int.from_bytes(z[64:68], "little")
            sr = z[68] | z[69] << 8
            a7 = int.from_bytes(z[60:64], "little")
            got = (halt, reason, pc, sr, a7, read_bytes(tr, BUF_RESULT, 1)[0])
            want = (1, reason_exp, n["pc"] + 2, n["sr"], n["regs"][15], 0x5A)
            if got != want:
                fails.append((label, "halt/reason/pc/sr/a7/RES_DONE=" +
                              "/".join(f"{v:X}" for v in want),
                              "/".join(f"{v:X}" for v in got)))
        finally:
            write_bytes(tr, addrs["st_exc"], b"\x00")
    return fails


def program_leg(target, addrs: dict, labels, ctx: RunContext) -> None:
    """Review F1: multi-instruction exception-mode programs, each run
    through test_run_program (emu_step path) AND emu_reset + emu_run (the
    product path that only the latch-patched run_jv/nx_br reach)."""
    from tests.run_program import RESULT_LEN, RunResult, Session
    from host.image import Chunk
    tr = target.transport
    sess = Session(target=target, labels=labels, timeout=ctx.timeout)
    rpc, ra7 = addrs["r_pc"], addrs["r_a7"]
    er, eru = addrs["emu_reset"], addrs["emu_run"]
    stub = bytes([0x78,                                     # sei
                  0x20, er & 0xFF, er >> 8,                 # jsr emu_reset
                  0xA2, 0x03,                               # ldx #3
                  0xBD, RUN_ARGS & 0xFF, RUN_ARGS >> 8,     # lda RUN_ARGS,x
                  0x95, rpc,                                # sta r_pc,x
                  0xBD, (RUN_ARGS + 4) & 0xFF, RUN_ARGS >> 8,
                  0x95, ra7,                                # sta r_a7,x
                  0xCA, 0x10, 0xF3,                         # dex / bpl -13
                  0x20, eru & 0xFF, eru >> 8,               # jsr emu_run
                  0x60])                                    # rts
    for prog in super_run_programs():
        chunks = [Chunk(a, bytes([v])) for a, v in sorted(prog["mem"].items())]
        for mode in ("emu_step", "emu_run"):
            name = f"program[{mode}]: {prog['name']}"
            try:
                sess.stage(chunks)
                write_bytes(tr, addrs["st_exc"], b"\x01")
                write_bytes(tr, addrs["r_osp"], RUN_SSP.to_bytes(4, "little"))
                if mode == "emu_step":
                    write_bytes(tr, BUF_CASE, struct.pack(
                        "<III", prog["pc"], RUN_USP, 1000))
                    call_runner(target, addrs["test_run_program"], ctx.timeout)
                    rr = RunResult.from_bytes(read_bytes(tr, BUF_RESULT,
                                                         RESULT_LEN))
                    regs = [rr.regs[k] for k in REG_NAMES]
                    got_pc, got_sr = rr.pc, rr.sr
                    halt, reason, icount = rr.halt, rr.reason, rr.icount
                else:
                    write_bytes(tr, RUN_STUB, stub)
                    write_bytes(tr, RUN_ARGS, struct.pack(
                        "<II", prog["pc"], RUN_USP))
                    call_runner(target, RUN_STUB, ctx.timeout)
                    z = read_bytes(tr, addrs["r_d0"], 70)
                    regs = [int.from_bytes(z[4 * i:4 * i + 4], "little")
                            for i in range(16)]
                    got_pc = int.from_bytes(z[64:68], "little")
                    got_sr = z[68] | z[69] << 8
                    halt, reason = read_bytes(tr, addrs["st_halt"], 2)
                    icount = None
                osp = int.from_bytes(read_bytes(tr, addrs["r_osp"], 4),
                                     "little")
                diffs = []
                exp_regs = {f"D{i}": prog["exp"].get(f"d{i}", 0)
                            for i in range(8)}
                exp_regs.update({f"A{i}": prog["exp"].get(f"a{i}", 0)
                                 for i in range(8)})
                exp_regs["A7"] = prog["exp"]["a7"]
                for k, v in zip(REG_NAMES, regs):
                    if v != exp_regs[k]:
                        diffs.append((k, f"{exp_regs[k]:08X}", f"{v:08X}"))
                for fld, e, a in (("PC", prog["exp_pc"], got_pc),
                                  ("SR", prog["exp_sr"], got_sr),
                                  ("OSP", prog["exp_osp"], osp)):
                    if e != a:
                        diffs.append((fld, f"{e:08X}", f"{a:08X}"))
                if (halt, reason) != (1, HALT_OK):
                    diffs.append(("halt", "halt=1 reason=00",
                                  f"halt={halt} reason={reason:02X}"))
                if icount is not None and icount != prog["icount"]:
                    diffs.append(("icount", str(prog["icount"]), str(icount)))
                for a, v in sorted(prog["exp_mem"].items()):
                    got = sess.peek_guest(a)
                    if got != v:
                        diffs.append((f"mem[{a:06X}]", f"{v:02X}", f"{got:02X}"))
            except Exception as e:  # noqa: BLE001 -- wedge / harness error
                diffs = [("harness", "program completes", repr(e)[:100])]
            finally:
                try:
                    write_bytes(tr, addrs["st_exc"], b"\x00")
                except Exception:  # noqa: BLE001
                    pass
            if diffs:
                ctx.add_failures([Failure(name, None, 0, f, e, a)
                                  for f, e, a in diffs])
            ctx.note_case(name, failed=bool(diffs))


RESTORE = True   # --no-restore (TEST-ONLY) clears it: the leak test's red leg


def run_batch(target, addrs: dict, name: str, cases: list[dict],
              ctx: RunContext, poke_exc: bool = True) -> tuple[bool, int]:
    """Run super cases.  poke_exc=False (leak check) leaves st_exc alone:
    only flag-clear cases may be run that way, and they must then see
    today's behaviour purely from the state the previous batches left."""
    tr = target.transport
    nf = 0
    try:
        for idx, n in enumerate(cases):
            if ctx.should_stop():
                return True, nf
            opcode = _case(n).opcode()
            write_bytes(tr, BUF_CASE, n["raw"])
            if poke_exc:
                write_bytes(tr, addrs["st_exc"],
                            bytes([n["flags"] & FLAG_EXC]))
            write_bytes(tr, addrs["r_osp"], n["osp"].to_bytes(4, "little"))
            clear = not n["flags"] & FLAG_EXC
            if clear:
                write_bytes(tr, addrs["st_ipc"],
                            IPC_SENTINEL.to_bytes(4, "little"))
            try:
                call_runner(target, addrs["entry"], ctx.timeout)
            except Exception as e:  # noqa: BLE001 -- wedge: report + reacquire
                ctx.add_failures([Failure(name, opcode, idx, "harness",
                                          "test_run_case returns", repr(e))])
                ctx.note_case(name, failed=True)
                return False, nf + 1
            hdr = read_bytes(tr, BUF_RESULT, RESULT_HDR_LEN)
            mem = (read_bytes(tr, RES_MEM, len(n["exp_mem"]))
                   if n["exp_mem"] else b"")
            osp = int.from_bytes(read_bytes(tr, addrs["r_osp"], 4), "little")
            exc = read_bytes(tr, addrs["st_exc"], 1)[0]
            ipc = (int.from_bytes(read_bytes(tr, addrs["st_ipc"], 4), "little")
                   if clear else None)
            diffs = compare_super(n, hdr, mem, osp, exc, ipc)
            if diffs:
                ctx.add_failures([Failure(name, opcode, idx, f, e, a)
                                  for f, e, a in diffs])
                nf += len(diffs)
            ctx.note_case(name, failed=bool(diffs))
    finally:
        # Invariant: every batch leaves st_exc = 0, so a later
        # test_run_case / test_run_program in the SAME session (no re-boot)
        # sees today's flag-clear behaviour.  Checked by --leak-check.
        if RESTORE:
            try:
                write_bytes(tr, addrs["st_exc"], b"\x00")
            except Exception:  # noqa: BLE001 -- best effort, wedged target
                pass
    return True, nf


def leak_check(target, addrs: dict, golden: str | None,
               ctx: RunContext) -> None:
    """Same session, no re-boot, after the super batches: st_exc must read
    0, SUPERHAND's flag-clear cases (today's halts) must pass WITHOUT the
    driver touching st_exc, and optional golden batches must pass through
    the plain run_vectors path."""
    from tests.case_format import decode_batch
    from tests.run_vectors import run_batch as golden_batch
    exc = read_bytes(target.transport, addrs["st_exc"], 1)[0]
    if exc:
        ctx.add_failures([Failure("leak-check", None, 0, "st_exc after "
                                  "super batches", "00", f"{exc:02X}")])
    clear = [n for n in decode_super_batch(pack_super_norms(
        [c for c in super_hand_cases() if not c["flags"] & FLAG_EXC]))]
    run_batch(target, addrs, "leak-check:SUPERHAND-flag-clear", clear, ctx,
              poke_exc=False)
    if golden:
        for f in sorted(glob.glob(os.path.join(golden, "*.bin"))):
            with open(f, "rb") as fh:
                golden_batch(target, addrs["entry"],
                             "leak-check:" + os.path.basename(f),
                             decode_batch(fh.read()), ctx)


def run_sequential(mgr, prg: str, addrs: dict, batches: list, ctx: RunContext,
                   leak: str | None | bool = False) -> None:
    """All batches on one target, in order; a wedged target is replaced by
    a fresh one, which first gets the one-off target checks (boot state,
    RESET hook, mid-run flip) like any fresh target (review addendum).
    ``leak``: False = no leak check, else the golden dir (or None) for
    leak_check on the final target."""
    target = mgr.acquire()
    try:
        prepare_target(target, prg)
        for name, cases in batches:
            ok, _ = run_batch(target, addrs, name, cases, ctx)
            if not ok:
                mgr.release(target)
                target = mgr.acquire()
                prepare_target(target, prg)
                if "emu_run" in addrs:          # full label set (not base)
                    target_checks(target, addrs, ctx)
        if leak is not False:
            leak_check(target, addrs, leak, ctx)
    finally:
        mgr.release(target)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.run_super_vectors",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--vectors", default="build/vectors-super")
    ap.add_argument("--labels")
    ap.add_argument("--filter", default="")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--max-failures", type=int, default=0)
    ap.add_argument("--backend", default="vice", choices=["vice"])
    ap.add_argument("--leak-check", action="store_true",
                    help="after the super batches, in the SAME VICE session: "
                         "st_exc must be 0 and the flag-clear cases (plus "
                         "--golden batches) must behave as today; forces "
                         "--jobs 1")
    ap.add_argument("--golden", default=None,
                    help="golden batch dir for --leak-check (build/vectors)")
    ap.add_argument("--no-restore", action="store_true",
                    help=argparse.SUPPRESS)  # TEST-ONLY: leak-check red leg
    ap.add_argument("--allow-variant", action="store_true",
                    help=argparse.SUPPRESS)  # mutant/variant PRGs only
    ap.add_argument("--no-programs", action="store_true",
                    help="skip the one-off legs (boot/RESET-hook/mid-run "
                         "checks and the multi-instruction program leg)")
    args = ap.parse_args(argv)
    global RESTORE
    RESTORE = not args.no_restore
    if args.leak_check:
        args.jobs = 1

    prg = os.path.abspath(args.prg)
    lbl = (os.path.abspath(args.labels) if args.labels
           else os.path.splitext(prg)[0] + ".lbl")
    labels = Labels.from_file(lbl)
    # Containment of the BRNC escape hatch (docs/PERF.md (f)): a gate PRG
    # is built with the hot-path page-cross guards ENFORCING.
    try:
        enforced = lookup_label(labels, "emu_brnc_enforced")
    except KeyError:
        enforced = None                     # pre-guard build (e.g. base)
    if enforced == 0 and not args.allow_variant:
        print(f"error: {prg} was built with BRNC_WARN_ONLY or a variant "
              f"define (emu_brnc_enforced = 0): not a gate build",
              file=sys.stderr)
        return 2
    try:
        addrs = {"entry": lookup_label(labels, "test_run_case"),
                 "st_exc": lookup_label(labels, "st_exc"),
                 "r_osp": lookup_label(labels, "r_osp"),
                 "st_ipc": lookup_label(labels, "st_ipc"),
                 "emu_rst_hook": lookup_label(labels, "emu_rst_hook")}
        for k in ("r_pc", "r_a7", "r_d0", "st_halt", "emu_step", "emu_reset",
                  "emu_run", "test_run_program"):
            addrs[k] = lookup_label(labels, k)
        try:
            addrs["st_exl"] = lookup_label(labels, "st_exl")
        except KeyError:
            pass                            # pre-F2 build: no latched copy
    except KeyError as e:
        # The pre-W6-S base has no st_exc/r_osp: poke the documented
        # addresses anyway so the red leg runs (and fails on semantics).
        print(f"warning: label missing ({e}); using rc/zp.inc defaults "
              f"st_exc=$88 r_osp=$89", file=sys.stderr)
        addrs = {"entry": lookup_label(labels, "test_run_case"),
                 "st_exc": 0x88, "r_osp": 0x89, "st_ipc": 0x8D}

    files = sorted(glob.glob(os.path.join(args.vectors, "*.bin")))
    files = [f for f in files if args.filter in os.path.basename(f)]
    if not files:
        print(f"error: no batches in {args.vectors!r} matching "
              f"{args.filter!r}", file=sys.stderr)
        return 2
    batches = []
    for f in files:
        with open(f, "rb") as fh:
            batches.append((os.path.basename(f), decode_super_batch(fh.read())))
    total = sum(len(c) for _, c in batches)
    print(f"{len(batches)} batch(es), {total} case(s); jobs={args.jobs}")

    ctx = RunContext(timeout=args.timeout, max_failures=args.max_failures,
                     ccr_only=False)
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    t0 = time.monotonic()
    with UnifiedManager(backend=args.backend, vice_config=cfg) as mgr:
        if not args.no_programs and "emu_run" in addrs:
            # one-off legs on a fresh target: boot state, RESET hook,
            # mid-run flag flip (F2), multi-instruction programs (F1)
            target = mgr.acquire()
            try:
                prepare_target(target, prg)
                target_checks(target, addrs, ctx)
                program_leg(target, addrs, labels, ctx)
            finally:
                mgr.release(target)
        if args.jobs > 1:
            from c64_test_harness import run_parallel

            def make(name, cases):
                def fn(target):
                    prepare_target(target, prg)
                    ok, n = run_batch(target, addrs, name, cases, ctx)
                    return (ok and not n), f"{n} field failures"
                return fn
            res = run_parallel(mgr, [(nm, make(nm, cs)) for nm, cs in batches],
                               max_workers=args.jobs)
            res.print_summary()
        else:
            run_sequential(mgr, prg, addrs, batches, ctx,
                           leak=args.golden if args.leak_check else False)
    dt = time.monotonic() - t0

    for f in ctx.failures:
        print(f.line())
    print()
    w = max(len(nm) for nm, _ in batches)
    print(f"{'batch':<{w}}  {'cases':>6}  {'run':>6}  {'failed':>6}")
    for name, cases in batches:
        print(f"{name:<{w}}  {len(cases):>6}  {ctx.cases_run.get(name, 0):>6}"
              f"  {ctx.cases_failed.get(name, 0):>6}")
    extra = sorted(k for k in ctx.cases_run
                   if k.startswith(("leak-check", "program[")))
    for name in extra:
        print(f"{name:<{w}}  {'':>6}  {ctx.cases_run[name]:>6}"
              f"  {ctx.cases_failed.get(name, 0):>6}")
    nfail = sum(ctx.cases_failed.values())
    nrun = sum(v for k, v in ctx.cases_run.items()
               if not k.startswith(("leak-check", "program[")))
    progs = [k for k in extra if k.startswith("program[")]
    if progs:
        print(f"programs: {len(progs)} run, "
              f"{sum(ctx.cases_failed.get(k, 0) for k in progs)} failed")
    extra = [k for k in extra if k.startswith("leak-check")]
    if extra:
        print(f"leak check: {sum(ctx.cases_run[k] for k in extra)} case(s) "
              f"in the same session, "
              f"{sum(ctx.cases_failed.get(k, 0) for k in extra)} failed")
    print(f"\n{nrun}/{total} cases run, {nfail} failed "
          f"({len(ctx.failures)} field failures) in {dt:.1f}s")
    return 1 if ctx.failures or nrun != total else 0


if __name__ == "__main__":
    sys.exit(main())
