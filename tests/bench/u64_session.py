#!/usr/bin/env python3
"""WS-A hardware session on the U64E: ONE DeviceLock hold for everything.

    U64_HOST=192.0.2.81 U64_BASELINE_ON_ENTRY=0 python3 -m tests.bench.u64_session \
        --new build/ws-a/runner.prg --base build/base/runner.prg \
        --noinval build/ws-a-noinval/runner.prg

Right after the lock is acquired the device config is snapshotted
(harness snapshot_state: turbo control, CPU speed, REU, cartridge,
cartridge preference, badline timing, bus mode, SID addressing) and the
device info recorded; in `finally` the snapshot is written back with
restore_state (memory-only config, no flash save), the C64 is reset, the
config re-read and compared.  CPU speed is set explicitly before every
phase.  U64_BASELINE_ON_ENTRY=0 is required (no factory reset on entry).

Phases (--phases, default all, in this order):
  probe    tests/bench/rec_probe.s + rec_probe_x.s on silicon (autoload
           semantics, bank carry under autoload, 512 KB counter wrap)
  wrap     tests/rec_wrap.py on the new core (+ the base core as a red
           control with --wrap-base)
  hand     hand vectors at 1 MHz, new core (HAND + WRAP batches)
  full     full SingleStepTests suite at 1 MHz, new core
  program  run_program fixtures (minimal, reloc + budget demos), new core
  teeth    C1 teeth legs (tests/rec_teeth.py) on the new core (all must
           pass) and on the --noinval core (REC_NOINVAL: all must fail;
           a mutant build: the legs that cover it must fail)
  bench    68k IPS, base vs new, ABBA order per speed (--speeds), plus a
           calibrated $DF0A store-cost measurement per speed
Output: --out-dir (logs + JSON per phase) and --bench-json.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import glob
import json
import os
import signal
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)


class SnapshotError(RuntimeError):
    """The config snapshot is incomplete: nothing may run on the device."""


def full_config(client) -> dict:
    """{category: {item: value}} for every category (bodyless GETs only).
    The REST answer nests the items under the category name.  A category
    that answers with errors or without its items raises SnapshotError."""
    out = {}
    for cat in client.list_configs():
        raw = client.get_config_category(cat)
        items = raw.get(cat)
        if raw.get("errors") or not isinstance(items, dict):
            raise SnapshotError(f"config category {cat!r}: "
                                f"{raw.get('errors') or 'no items'}")
        out[cat] = items
    return out


def config_diff(a: dict, b: dict) -> dict:
    """{category: {item: (a, b)}} for every item that differs."""
    diff = {}
    for cat in sorted(set(a) | set(b)):
        ca, cb = a.get(cat) or {}, b.get(cat) or {}
        d = {k: (ca.get(k), cb.get(k)) for k in sorted(set(ca) | set(cb))
             if ca.get(k) != cb.get(k)}
        if d:
            diff[cat] = d
    return diff


def secret(item: str) -> bool:
    """STRPASS items (fw 3.15: only "Network Password",
    network_config.cc:18) never go into a dump or the log."""
    return "password" in item.lower()


def redacted(obj):
    """*obj* (a full config, or a config_diff) with secret values masked."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if secret(str(k)) and not isinstance(v, dict)
                    else redacted(v)) for k, v in obj.items()}
    return obj


class _Steps:
    """Run restore steps so that no single failure (or ^C) skips the
    rest: every step is tried *attempts* times; a KeyboardInterrupt or
    SystemExit counts as a failed attempt and is re-raised once the whole
    restore has finished."""

    def __init__(self):
        self.interrupt = None

    def run(self, what: str, fn, attempts: int = 2):
        """-> (True, result) or (False, None)."""
        for n in range(1, attempts + 1):
            try:
                return True, fn()
            except BaseException as e:           # noqa: BLE001 - see class
                if not isinstance(e, Exception) and self.interrupt is None:
                    self.interrupt = e
                log(f"  {what} failed (attempt {n}/{attempts}): {e!r}")
        return False, None


def restore_config(client, before: dict, snap: dict, steps: _Steps) -> dict:
    """Put the device back to *before*.  -> {"restored", "reset_ok",
    "after", "diff"}; restored is True only for an empty final diff."""
    from c64_test_harness.backends.ultimate64_helpers import (
        U64StateSnapshot, restore_state)
    steps.run("restore_state", lambda: restore_state(
        client, U64StateSnapshot(**snap)), attempts=1)
    for rnd in (1, 2):                          # re-read + per-item repair
        ok, diff = steps.run(f"re-read {rnd}",
                             lambda: config_diff(before, full_config(client)))
        if not ok:
            continue
        if not diff:
            break
        for cat, items in diff.items():
            for item, (was, cur) in items.items():
                show = "<redacted>" if secret(item) else f"{was!r}"
                if was is None:
                    log(f"  not restoring {cat} / {item}: absent from the "
                        f"snapshot (now {'<redacted>' if secret(item) else repr(cur)})")
                    continue
                if steps.run(f"set {cat} / {item} = {show}",
                             lambda: client.set_config_item(cat, item, was))[0]:
                    log(f"  restored {cat} / {item} = {show}")
    reset_ok = steps.run("C64 reset", client.reset, attempts=1)[0]
    steps.run("settle", lambda: time.sleep(3.0), attempts=1)

    def final():
        after = full_config(client)
        return after, config_diff(before, after)
    ok, r = steps.run("final re-read", final)
    after, diff = r if ok else (None, None)
    return {"restored": diff == {}, "reset_ok": reset_ok, "after": after,
            "diff": diff}


def _defer_sigint(steps: _Steps):
    """Install a SIGINT handler that only records the ^C (into
    steps.interrupt); -> the previous handler, or None when signals
    cannot be handled here (not the main thread)."""
    def handler(signum, frame):
        if steps.interrupt is None:
            steps.interrupt = KeyboardInterrupt()
        log("^C received: deferred until the restore is done and the lock "
            "released")
    try:
        return signal.signal(signal.SIGINT, handler)
    except ValueError:
        return None


@contextlib.contextmanager
def u64_hold(out_dir: str, lock_timeout: float = 6 * 3600.0):
    """ONE DeviceLock hold with exact config snapshot/restore.

    Right after the lock is acquired: device info, the harness
    snapshot_state and the FULL config (every category) are recorded in
    out_dir/config_before.json (passwords redacted).  Any failure there
    (including a category answering with errors) aborts before the body
    runs, with the device untouched.  On exit, also on error or ^C:
    restore_config (restore_state; re-read + per-item repair, each step
    retried; C64 reset; final re-read and diff into config_after.json),
    then the lock is released and a ^C is re-raised.  During the whole
    restore SIGINT is deferred (a handler records it; the previous
    handler is put back after the lock is released).  Every device call,
    the re-read/diff rounds, the settle sleep and the config_after.json
    report run as _Steps steps, so a KeyboardInterrupt raised inside one
    of them cannot skip the rest either (only the per-item log lines run
    outside a step; a real ^C there is deferred by the handler).  No factory baseline
    on entry, no flash save.  Yields a dict: target, info, before,
    lock_wait_s; after the block, "restored" is True only for an empty
    final diff and "reset_ok" says whether the C64 reset went through."""
    from c64_test_harness import UnifiedManager
    from c64_test_harness.backends.ultimate64_helpers import snapshot_state
    hold = {"restored": False, "reset_ok": False}
    t_req = time.monotonic()
    log("requesting the device (DeviceLock) ...")
    with UnifiedManager(backend="u64", lock_timeout=lock_timeout,
                        baseline_on_entry=False) as mgr:
        target = mgr.acquire()
        snap = None
        steps = _Steps()
        try:
            hold["lock_wait_s"] = waited = time.monotonic() - t_req
            client = target.transport.client
            before = full_config(client)
            snap_ = dataclasses.asdict(snapshot_state(client))
            info = client.get_info()
            snap = snap_
            hold.update(target=target, info=info, before=before)
            log(f"acquired after {waited:.0f}s wait; {info.get('product')} "
                f"fw {info.get('firmware_version')} fpga "
                f"{info.get('fpga_version')} core {info.get('core_version')}")
            log(f"config before: {snap}")
            dump(os.path.join(out_dir, "config_before.json"),
                 {"info": info, "snapshot_state": snap,
                  "full": redacted(before), "lock_wait_s": waited,
                  "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
            yield hold
        finally:
            prev = _defer_sigint(steps)
            try:
                if snap is None:
                    log("no complete snapshot was taken: nothing ran, "
                        "nothing to restore")
                else:
                    log("restoring config ...")
                    r = restore_config(client, before, snap, steps)
                    hold.update(restored=r["restored"],
                                reset_ok=r["reset_ok"])

                    def report():
                        dump(os.path.join(out_dir, "config_after.json"),
                             {"full": redacted(r["after"]),
                              "diff_vs_before": redacted(r["diff"]),
                              "restored": r["restored"],
                              "reset_ok": r["reset_ok"],
                              "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
                        d = r["diff"]
                        log("config after (full, all categories): " +
                            ("IDENTICAL" if d == {} else "RE-READ FAILED"
                             if d is None else f"DIFFERS {redacted(d)}"))
                    steps.run("write config_after.json", report, attempts=1)
            except BaseException as e:          # noqa: BLE001 - never skip
                log(f"RESTORE FAILED: {e!r}\n{traceback.format_exc()}")
                if not isinstance(e, Exception) and steps.interrupt is None:
                    steps.interrupt = e
            finally:
                try:
                    mgr.release(target)
                finally:
                    if prev is not None:
                        signal.signal(signal.SIGINT, prev)
            if steps.interrupt is not None:
                raise steps.interrupt


class Hw:
    def __init__(self, target, out_dir, vice=None):
        """*vice*: a u64bench.ViceBoot for the dry run (target unused)."""
        self.target = target
        self.vice = vice
        self.out = out_dir
        self.cur_prg = None

    @property
    def t(self):
        return self.target.transport

    @property
    def backend(self) -> str:
        return "vice" if self.vice is not None else self.target.backend

    def speed(self, mhz: int) -> None:
        if self.vice is not None:              # dry-run backend: 1 MHz only
            return
        self.t.set_speed(mhz)
        time.sleep(0.5)

    def load(self, prg: str):
        """Boot *prg* at 1 MHz (run_prg + dispatcher), return its labels.
        On VICE every load is a fresh instance of *prg*, so a base or
        mutant build really runs (and its own labels match it)."""
        from c64_test_harness import Labels
        from tests.bench.u64bench import check_loaded
        from tests.run_vectors import prepare_target
        labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
        if self.vice is not None:
            self.target = self.vice.load(prg)
        else:
            self.speed(1)
            prepare_target(self.target, prg)
        check_loaded(self.target, prg, labels)
        self.cur_prg = prg
        return labels


# ------------------------------------------------------------------ phases

def ph_probe(hw: Hw, a) -> dict:
    from tests.bench.rec_probe import assemble_all, assemble_x, probe, probe_x
    hw.load(a.new)
    out = probe(hw.target, assemble_all(a.new))
    xdir = os.path.join(os.path.dirname(a.new), "rec_probe")
    out["x"] = probe_x(hw.target, {c: assemble_x(c, xdir)
                                   for c in (0x91, 0xB1)})
    return out


def ph_wrap(hw: Hw, a) -> dict:
    """tests/rec_wrap.py on silicon: 512K straddles, bank carry, the B2
    reviewer's repro and a chunk staged across $080000."""
    from tests.rec_wrap import run, run_b2
    out = {}
    for tag, prg in (("new", a.new),) + ((("base", a.base),)
                                           if a.wrap_base else ()):
        labels = hw.load(prg)
        probs = run(hw.target, labels, "u64")
        labels = hw.load(prg)
        probs += run_b2(hw.target, labels, "u64")
        out[tag] = probs
        log(f"  rec_wrap {tag}: {'all checks passed' if not probs else probs}")
    return out


def _vectors(hw: Hw, prg: str, vec_dir: str) -> dict:
    from tests.case_format import decode_batch
    from tests.harness_util import lookup_label
    from tests.run_vectors import RunContext, run_batch
    labels = hw.load(prg)
    entry = lookup_label(labels, "test_run_case")
    ctx = RunContext(timeout=15.0, max_failures=0, ccr_only=False)
    files = sorted(glob.glob(os.path.join(vec_dir, "*.bin")))
    total = 0
    t0 = time.monotonic()
    for path in files:
        with open(path, "rb") as f:
            cases = decode_batch(f.read())
        total += len(cases)
        name = os.path.basename(path)
        healthy, _ = run_batch(hw.target, entry, name, cases, ctx)
        if not healthy:
            log(f"  [{name}] wedged; re-booting the runner")
            hw.load(prg)
    run = sum(ctx.cases_run.values())
    failed = sum(ctx.cases_failed.values())
    res = {"batches": len(files), "cases": total, "run": run,
           "failed": failed, "field_failures": len(ctx.failures),
           "failures": [f.line() for f in ctx.failures[:200]],
           "seconds": time.monotonic() - t0}
    log(f"  {run}/{total} cases run, {failed} failed "
        f"({len(ctx.failures)} field failures) in {res['seconds']:.1f}s")
    return res


def ph_hand(hw: Hw, a) -> dict:
    return _vectors(hw, a.new, a.hand)


def ph_full(hw: Hw, a) -> dict:
    return _vectors(hw, a.new, a.vectors)


def ph_program(hw: Hw, a) -> dict:
    from tests import run_program as rp
    labels = hw.load(a.new)
    sess = rp.Session(target=hw.target, labels=labels, timeout=60.0)
    for name, fn in rp.FIXTURES.items():
        blob, exp = fn()
        rp.run_fixture(sess, name, blob, exp, 10_000)
        rp.run_budget_demo(sess, name, exp)
    res = {"checks": len(sess.checks), "failures": sess.failures}
    log(f"  run_program: {len(sess.checks)}/"
        f"{len(sess.checks) + len(sess.failures)} checks passed")
    return res


def ph_teeth(hw: Hw, a) -> dict:
    import random
    from tests.rec_teeth import LEGS, Leg
    seed = random.randrange(1 << 30)
    rng = random.Random(seed)
    log(f"  teeth seed {seed}")
    out = {"seed": seed}
    # label the second row by the build it really is (REC_NOINVAL or a
    # mutant passed via --noinval), e.g. "ws-a-noinval", "ws-a-src-actrl"
    mtag = os.path.basename(os.path.dirname(a.noinval))
    out["mutant_build"] = os.path.relpath(a.noinval, ROOT)
    for tag, prg in (("normal", a.new), (mtag, a.noinval)):
        rows = {}
        for name, fn in LEGS.items():
            if name == "mailbox" and hw.backend == "vice":
                continue                        # no mailbox on VICE
            if name == "boot" and hw.backend != "vice":
                continue                        # mailbox dirties first
            labels = hw.load(prg)
            leg = Leg(hw.target, labels, "u64", rng)
            try:
                fn(leg)
            except Exception as e:
                leg.problems.append(f"exception: {e!r}")
            rows[name] = leg.problems
            log(f"  teeth {tag:<14} {name:<11} "
                f"{'PASS' if not leg.problems else 'FAIL'} "
                f"{leg.problems[:2] if leg.problems else ''}")
        out[tag] = rows
    out["normal_all_pass"] = not any(out["normal"].values())
    out["mutant_failed_legs"] = sorted(k for k, v in out[mtag].items()
                                       if v)
    return out


def ph_bench(hw: Hw, a) -> dict:
    from tests.bench import u64bench as ub
    from tests.run_program import Session
    rows = []
    loops = [("alu", ub.hunk_alu68k, ub.alu68k_icount),
             ("mem", ub.hunk_mem68k, ub.mem68k_icount),
             ("stream", ub.hunk_stream68k, ub.mem68k_icount),
             ("rmw", ub.hunk_rmw68k, ub.mem68k_icount)]
    for mhz in [int(s) for s in a.speeds.split(",")]:
        for i, (tag, prg) in enumerate((("base", a.base), ("new", a.new),
                                        ("new", a.new), ("base", a.base))):
            labels = hw.load(prg)
            hw.speed(mhz)
            row = {"mhz": mhz, "core": tag, "rep": (0, 0, 1, 1)[i],
                   "prg": os.path.relpath(prg, ROOT)}
            oc2 = min(255, 2 + 6 * mhz)
            r = ub.two_point(hw.target, ub.ALU_ADDR, ub.code_alu, 2, oc2, 600.0)
            eff = r["dc"] * ub.ALU_CYCLES_PER_OC / r["dt"] / 1e6
            row["eff_mhz"] = eff
            if i == 0:
                r = ub.two_point(hw.target, ub.IO_ADDR, ub.code_io, 2, oc2,
                                 900.0)
                dstores = r["dc"] * ub.IO_STORES_PER_OC
                ns_iter = r["dt"] / dstores * 1e9
                # loop iteration = sta(4) + inx(2) + bne(3); the 5 non-store
                # cycles run at the calibrated clock
                row["df0a_ns_per_iter"] = ns_iter
                row["df0a_ns_per_store"] = ns_iter - 5e3 / eff
            sess = Session(target=hw.target, labels=labels, timeout=900.0)
            n2 = min(32000, max(2200, 500 * mhz))
            for name, build, icf in loops:
                r = ub.bench_68k(sess, build, icf, 200, n2)
                row[f"{name}_ips"] = r["ips"]
                row[f"{name}_cpi"] = eff * 1e6 / r["ips"]
                row[f"{name}_dt"] = r["dt"]
            log(f"  {mhz:>2} MHz {tag:<4} eff {eff:6.3f}  " +
                "  ".join(f"{n} {row[n + '_ips']:7.0f}" for n, _, _ in loops)
                + (f"  $DF0A {row['df0a_ns_per_store']:.0f} ns/store"
                   if "df0a_ns_per_store" in row else ""))
            rows.append(row)
    hw.speed(1)
    return {"rows": rows}


def vice_dry_run(a) -> int:
    """Exercise the phase code on VICE (logic check before device time).
    VICE cannot swap PRGs inside one instance, so every Hw.load boots a
    fresh instance of that PRG: --base and --noinval really run."""
    from tests.bench.u64bench import ViceBoot
    rc = 0
    vice = ViceBoot()
    try:
        hw = Hw(None, a.out_dir, vice=vice)
        for ph in a.phases.split(","):
            log(f"=== (vice dry run) phase {ph} ===")
            try:
                r = PHASES[ph](hw, a)
                dump(os.path.join(a.out_dir, f"phase_{ph}.json"), r)
            except Exception as e:
                rc = 1
                log(f"phase {ph} ERROR: {e!r}\n{traceback.format_exc()}")
    finally:
        vice.close()
    return rc


PHASES = {"probe": ph_probe, "wrap": ph_wrap, "hand": ph_hand,
          "full": ph_full,
          "program": ph_program, "teeth": ph_teeth, "bench": ph_bench}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", default="build/ws-a/runner.prg")
    ap.add_argument("--base", default="build/base/runner.prg")
    ap.add_argument("--noinval", default="build/ws-a-noinval/runner.prg")
    ap.add_argument("--vectors", default="build/vectors")
    ap.add_argument("--hand", default="build/vectors-hand")
    ap.add_argument("--phases", default=",".join(PHASES))
    ap.add_argument("--speeds", default="1,16,48")
    ap.add_argument("--wrap-base", action="store_true",
                    help="wrap phase: also run the base core (red control)")
    ap.add_argument("--backend", default="u64", choices=["u64", "vice"],
                    help="vice = dry run of the session logic (no config, "
                         "1 MHz only, warp; no performance meaning)")
    ap.add_argument("--out-dir", default="build/evidence/ws-a/hw")
    ap.add_argument("--bench-json", default="fixtures/bench/sweep_reclean.json")
    a = ap.parse_args(argv)
    for k in ("new", "base", "noinval", "vectors", "hand", "out_dir",
              "bench_json"):
        setattr(a, k, os.path.join(ROOT, getattr(a, k)))
    if a.backend == "u64" and os.environ.get("U64_BASELINE_ON_ENTRY") != "0":
        print("refusing: set U64_BASELINE_ON_ENTRY=0", file=sys.stderr)
        return 2

    from c64_test_harness import UnifiedManager, ViceConfig
    from c64_test_harness.backends.ultimate64_helpers import (
        U64StateSnapshot, restore_state, snapshot_state)
    from tests.harness_util import REU_VICE_ARGS
    if a.backend == "vice":
        return vice_dry_run(a)

    results = {}
    rc = 0
    t_req = time.monotonic()
    log("requesting the device (DeviceLock) ...")
    with UnifiedManager(backend="u64", lock_timeout=6 * 3600.0) as mgr:
        target = mgr.acquire()
        waited = time.monotonic() - t_req
        client = target.transport.client
        info = client.get_info()
        before = dataclasses.asdict(snapshot_state(client))
        log(f"acquired after {waited:.0f}s wait; {info.get('product')} "
            f"fw {info.get('firmware_version')} fpga {info.get('fpga_version')} "
            f"core {info.get('core_version')}")
        log(f"config before: {before}")
        dump(os.path.join(a.out_dir, "config_before.json"),
             {"info": info, "state": before, "lock_wait_s": waited,
              "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
        hw = Hw(target, a.out_dir)
        try:
            for ph in a.phases.split(","):
                log(f"=== phase {ph} ===")
                t0 = time.monotonic()
                try:
                    results[ph] = PHASES[ph](hw, a)
                except Exception as e:
                    rc = 1
                    results[ph] = {"error": repr(e),
                                   "traceback": traceback.format_exc()}
                    log(f"phase {ph} ERROR: {e!r}")
                results[ph + "_seconds"] = time.monotonic() - t0
                dump(os.path.join(a.out_dir, f"phase_{ph}.json"), results[ph])
                if ph == "bench" and "rows" in results[ph]:
                    dump(a.bench_json, {
                        "device": info, "config_before": before,
                        "date": time.strftime("%Y-%m-%d"),
                        "method": "two-point deltas (n=200 vs n2 iterations); "
                                  "eff_mhz from a known-cycle 6502 loop; ABBA "
                                  "order base,new,new,base per speed; same "
                                  "lock session",
                        "rows": results[ph]["rows"]})
        finally:
            log("restoring config ...")
            try:
                restore_state(client, U64StateSnapshot(**before))
                client.reset()
                time.sleep(3.0)
                after = dataclasses.asdict(snapshot_state(client))
                diff = {k: (before[k], after[k]) for k in before
                        if before[k] != after[k]}
                dump(os.path.join(a.out_dir, "config_after.json"),
                     {"state": after, "diff_vs_before": diff,
                      "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
                log(f"config after: {'IDENTICAL' if not diff else diff}")
                if diff:
                    rc = 1
            except Exception as e:
                rc = 1
                log(f"RESTORE FAILED: {e!r}")
            mgr.release(target)
    dump(os.path.join(a.out_dir, "session.json"),
         {k: v for k, v in results.items()})
    return rc


if __name__ == "__main__":
    sys.exit(main())
