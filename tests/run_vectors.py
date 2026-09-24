"""Host-side test-vector driver: runs binary case batches through the C64 runner.

Usage:
    python3 -m tests.run_vectors --prg build/runner.prg --vectors build/vectors
        [--labels build/runner.lbl] [--filter SUBSTR] [--max-failures N]
        [--jobs N] [--timeout SECS] [--ccr-only] [--backend auto|vice|u64]

For each case in each .bin batch (format: tests/case_format.py, per
docs/CONTRACTS.md): write the case record to buf_case ($C000), invoke
`test_run_case` (address discovered from the ld65 label file), read
buf_result ($C800), compare final regs/PC/SR and expected-memory bytes,
and report failures as (batch, opcode, case index, field, expected, actual).
Exit code is nonzero on any failure.

Backends: VICE (default; REU 16 MB enabled per contract) via
c64-test-harness ViceInstanceManager/UnifiedManager + jsr(). --jobs N runs
batches across N parallel VICE instances via the harness's run_parallel.
The u64 path (run_subroutine + REST PRG boot + set_reu) is wired but
untested on hardware.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from c64_test_harness import Labels, UnifiedManager, ViceConfig, read_bytes, write_bytes

from tests.case_format import REG_NAMES, Case, decode_batch
from tests.harness_util import (
    BUF_CASE, BUF_RESULT, REU_VICE_ARGS, RES_MEM,
    call_runner, lookup_label, wait_ready,
)

RESULT_HDR_LEN = 0x56  # done/halt/reason/n_mem + regs/PC/SR block


@dataclass
class Failure:
    batch: str
    opcode: int | None
    case_idx: int
    fld: str
    expected: str
    actual: str

    def line(self) -> str:
        op = f"{self.opcode:04X}" if self.opcode is not None else "????"
        return (f"FAIL  batch={self.batch} opcode={op} case={self.case_idx} "
                f"field={self.fld} expected={self.expected} actual={self.actual}")


@dataclass
class RunContext:
    timeout: float
    max_failures: int              # 0 = unlimited
    ccr_only: bool
    failures: list[Failure] = field(default_factory=list)
    cases_run: dict[str, int] = field(default_factory=dict)
    cases_failed: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stopped: bool = False

    def add_failures(self, fails: list[Failure]) -> None:
        with self._lock:
            self.failures.extend(fails)
            if self.max_failures and len(self.failures) >= self.max_failures:
                self._stopped = True

    def note_case(self, batch: str, failed: bool) -> None:
        with self._lock:
            self.cases_run[batch] = self.cases_run.get(batch, 0) + 1
            if failed:
                self.cases_failed[batch] = self.cases_failed.get(batch, 0) + 1

    def should_stop(self) -> bool:
        with self._lock:
            return self._stopped


def compare_case(case: Case, hdr: bytes, mem: bytes, ccr_only: bool) -> list[tuple[str, str, str]]:
    """Compare a buf_result dump against the case's expectations."""
    fails: list[tuple[str, str, str]] = []
    if hdr[0] != 0xFF:
        return [("done_flag", "FF", f"{hdr[0]:02X}")]
    if hdr[1] != 0:
        fails.append(("st_halt", "00", f"halt={hdr[1]:02X} reason={hdr[2]:02X}"))
    for i, name in enumerate(REG_NAMES):
        act = int.from_bytes(hdr[0x10 + 4 * i:0x14 + 4 * i], "little")
        if act != case.exp_regs[i]:
            fails.append((name, f"{case.exp_regs[i]:08X}", f"{act:08X}"))
    act_pc = int.from_bytes(hdr[0x50:0x54], "little")
    if act_pc != case.exp_pc:
        fails.append(("PC", f"{case.exp_pc:08X}", f"{act_pc:08X}"))
    act_sr = hdr[0x54] | (hdr[0x55] << 8)
    if ccr_only:
        if (act_sr ^ case.exp_sr) & 0x1F:
            fails.append(("CCR", f"{case.exp_sr & 0x1F:02X}", f"{act_sr & 0x1F:02X}"))
    elif act_sr != case.exp_sr:
        fails.append(("SR", f"{case.exp_sr:04X}", f"{act_sr:04X}"))
    if hdr[3] != len(case.exp_mem):
        fails.append(("n_mem", f"{len(case.exp_mem)}", f"{hdr[3]}"))
    else:
        for j, (addr, val) in enumerate(case.exp_mem):
            if mem[j] != val:
                fails.append((f"mem[{addr:06X}]", f"{val:02X}", f"{mem[j]:02X}"))
    return fails


def prepare_target(target, prg_path: str) -> None:
    """Get a freshly acquired target to the runner's ready state."""
    if target.backend != "vice":
        # Hardware path: REU 16 MB + boot the runner PRG via REST, then
        # install the mailbox dispatcher (harness_util) so call_runner()
        # can invoke runner entry points without BASIC READY.
        from c64_test_harness.backends.ultimate64_helpers import set_reu
        from tests.harness_util import u64_install_dispatcher
        client = target.transport.client
        set_reu(client, True, 16)
        with open(prg_path, "rb") as f:
            client.run_prg(f.read())
        wait_ready(target.transport)
        labels = Labels.from_file(os.path.splitext(prg_path)[0] + ".lbl")
        try:    # C1: dispatcher invalidates the REC shadow before each call
            rec_dirty = lookup_label(labels, "mem_rec_dirty")
        except KeyError:
            rec_dirty = None
        u64_install_dispatcher(target, lookup_label(labels, "main"),
                               rec_dirty_addr=rec_dirty)
        return
    wait_ready(target.transport)


def run_batch(target, entry: int, batch_name: str, cases: list[Case],
              ctx: RunContext) -> tuple[bool, int]:
    """Run one batch on a prepared target.

    Returns (healthy, n_field_failures) — healthy is False if the target
    wedged (test_run_case never returned) and the caller should re-acquire.
    """
    transport = target.transport
    nfails = 0
    for idx, case in enumerate(cases):
        if ctx.should_stop():
            return True, nfails
        write_bytes(transport, BUF_CASE, case.raw)
        try:
            call_runner(target, entry, ctx.timeout)
        except Exception as e:
            ctx.add_failures([Failure(batch_name, case.opcode(), idx, "harness",
                                      "test_run_case returns", repr(e))])
            ctx.note_case(batch_name, failed=True)
            return False, nfails + 1  # machine state unknown; caller re-acquires
        hdr = read_bytes(transport, BUF_RESULT, RESULT_HDR_LEN)
        mem = read_bytes(transport, RES_MEM, len(case.exp_mem)) if case.exp_mem else b""
        diffs = compare_case(case, hdr, mem, ctx.ccr_only)
        if diffs:
            ctx.add_failures([Failure(batch_name, case.opcode(), idx, f, e, a)
                              for f, e, a in diffs])
            nfails += len(diffs)
        ctx.note_case(batch_name, failed=bool(diffs))
    return True, nfails


def run_sequential(mgr: UnifiedManager, prg: str, entry_label: str, labels,
                   batches: list[tuple[str, list[Case]]], ctx: RunContext) -> None:
    entry = lookup_label(labels, entry_label)
    target = mgr.acquire()
    try:
        prepare_target(target, prg)
        for name, cases in batches:
            if ctx.should_stop():
                break
            healthy, _ = run_batch(target, entry, name, cases, ctx)
            if not healthy and not ctx.should_stop():
                print(f"  [{name}] target wedged; re-acquiring a fresh instance",
                      file=sys.stderr)
                mgr.release(target)
                target = mgr.acquire()
                prepare_target(target, prg)
    finally:
        mgr.release(target)


def run_parallel_batches(mgr: UnifiedManager, prg: str, entry_label: str, labels,
                         batches: list[tuple[str, list[Case]]], ctx: RunContext,
                         jobs: int) -> None:
    from c64_test_harness import run_parallel

    entry = lookup_label(labels, entry_label)

    def make_test(name: str, cases: list[Case]):
        def fn(target) -> tuple[bool, str]:
            prepare_target(target, prg)
            healthy, n = run_batch(target, entry, name, cases, ctx)
            if not healthy:
                return False, f"target wedged; {n} field failures (details in report)"
            if n:
                return False, f"{n} field failures (details in report)"
            return True, f"{len(cases)} cases ok"
        return fn

    tests = [(name, make_test(name, cases)) for name, cases in batches]
    result = run_parallel(mgr, tests, max_workers=jobs)
    result.print_summary()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.run_vectors",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True, help="runner PRG (build/runner.prg)")
    ap.add_argument("--vectors", required=True, help="directory of .bin batches")
    ap.add_argument("--labels", help="ld65 -Ln label file (default: PRG path with .lbl)")
    ap.add_argument("--filter", default="", help="only run batches whose filename contains this")
    ap.add_argument("--max-failures", type=int, default=0, metavar="N",
                    help="stop after N field failures (0 = unlimited)")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="run at most N cases per batch (0 = all; for "
                         "representative subset runs on slow backends)")
    ap.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="parallel VICE instances (one batch per instance)")
    ap.add_argument("--timeout", type=float, default=15.0,
                    help="per-case test_run_case timeout in seconds")
    ap.add_argument("--ccr-only", action="store_true",
                    help="compare only CCR bits (mask $1F) instead of the full SR word")
    ap.add_argument("--backend", default="auto", choices=["auto", "vice", "u64"],
                    help="c64-test-harness backend (default: auto = $C64_BACKEND or vice)")
    ap.add_argument("--entry-label", default="test_run_case",
                    help="runner entry label name (default: test_run_case)")
    args = ap.parse_args(argv)

    prg = os.path.abspath(args.prg)
    if not os.path.exists(prg):
        print(f"error: PRG not found: {prg}", file=sys.stderr)
        return 2
    labels_path = os.path.abspath(args.labels) if args.labels else os.path.splitext(prg)[0] + ".lbl"
    if not os.path.exists(labels_path):
        print(f"error: label file not found: {labels_path}", file=sys.stderr)
        return 2
    labels = Labels.from_file(labels_path)

    batch_files = sorted(glob.glob(os.path.join(args.vectors, "*.bin")))
    if args.filter:
        batch_files = [p for p in batch_files if args.filter in os.path.basename(p)]
    if not batch_files:
        print(f"error: no .bin batches in {args.vectors!r} matching {args.filter!r}",
              file=sys.stderr)
        return 2

    batches: list[tuple[str, list[Case]]] = []
    for path in batch_files:
        with open(path, "rb") as f:
            data = f.read()
        try:
            cases = decode_batch(data)
        except ValueError as e:
            print(f"error: bad batch {path}: {e}", file=sys.stderr)
            return 2
        if args.limit:
            cases = cases[:args.limit]
        batches.append((os.path.basename(path), cases))
    total_cases = sum(len(c) for _, c in batches)
    print(f"{len(batches)} batch(es), {total_cases} case(s); "
          f"backend={args.backend}, jobs={args.jobs}")

    ctx = RunContext(timeout=args.timeout, max_failures=args.max_failures,
                     ccr_only=args.ccr_only)
    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    t0 = time.monotonic()
    with UnifiedManager(backend=args.backend, vice_config=vice_config) as mgr:
        if args.jobs > 1:
            run_parallel_batches(mgr, prg, args.entry_label, labels, batches, ctx, args.jobs)
        else:
            run_sequential(mgr, prg, args.entry_label, labels, batches, ctx)
    elapsed = time.monotonic() - t0

    # Report
    for fail in ctx.failures:
        print(fail.line())
    print()
    name_w = max(len(n) for n, _ in batches)
    print(f"{'batch':<{name_w}}  {'cases':>6}  {'run':>6}  {'failed':>6}")
    for name, cases in batches:
        print(f"{name:<{name_w}}  {len(cases):>6}  {ctx.cases_run.get(name, 0):>6}  "
              f"{ctx.cases_failed.get(name, 0):>6}")
    n_failed_cases = sum(ctx.cases_failed.values())
    n_run = sum(ctx.cases_run.values())
    print(f"\n{n_run}/{total_cases} cases run, {n_failed_cases} failed "
          f"({len(ctx.failures)} field failures) in {elapsed:.1f}s")
    if ctx.should_stop():
        print(f"stopped early: --max-failures {args.max_failures} reached")
    return 1 if ctx.failures else 0


if __name__ == "__main__":
    sys.exit(main())
