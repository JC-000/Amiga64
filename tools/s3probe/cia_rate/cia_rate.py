#!/usr/bin/env python3
"""W8-H: do the C64 CIAs count phi2 or a turbo-scaled clock? (S3_PLAN risk 5)

Builds cia_rate.s, uploads it ONCE, then for each requested CPU speed runs the
probe (CIA2 timer A->B free-running, sampled at raster line 100 every 50
frames, plus CIA2 TOD) and prints CIA ticks per frame.  Expected if the CIAs
count phi2: 19656 per PAL frame (312 lines x 63 cycles), 17095 per NTSC frame
(263 x 65).  The verdict per run is phi2 / SCALED / other, checked against the
expectation (--expect-ticks overrides it for every speed: the red leg uses a
deliberately wrong one and must exit 1).

  python3 cia_rate.py --backend u64 --runs 3 --speeds 1,48 --out DIR
  python3 cia_rate.py --backend vice --runs 1 --speeds 1 --out DIR
  python3 cia_rate.py --analyze DIR/runs.json [--expect-ticks N]   # offline

Hardware sessions run under tests.bench.u64_session.u64_hold: one DeviceLock
hold, full config snapshot before, restore (and diff) on every exit path.
Exit 0 when every run meets the expectation, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)

STEP, NSAMP = 50, 6
PAL_TICKS, NTSC_TICKS = 19656, 17095
PAL_HZ, NTSC_HZ = 985248 / PAL_TICKS, 1022727 / NTSC_TICKS
TOL = 0.01                       # 1 % of the expectation


def build(out: str) -> tuple[str, dict]:
    os.makedirs(out, exist_ok=True)
    prg, lbl = os.path.join(out, "cia_rate.prg"), os.path.join(out, "cia_rate.lbl")
    subprocess.run(["cl65", "-t", "none", "-C", "cia_rate.cfg", "-o", prg,
                    "-Ln", lbl, "cia_rate.s"], check=True, cwd=HERE)
    labels = {}
    for line in open(lbl):
        _, a, n = line.split()
        labels[n.lstrip(".")] = int(a, 16)
    return prg, labels


def bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 15)


def decode(recs: bytes) -> dict:
    """recs: NSAMP x 8 bytes -> per-interval ticks and TOD deltas."""
    vals, tods = [], []
    for i in range(NSAMP):
        r = recs[i * 8:i * 8 + 8]
        vals.append(((r[0] << 8 | r[1]) << 16) | (r[3] << 8 | r[2]))
        tods.append(((bcd(r[4] & 0x1F) * 60 + bcd(r[5])) * 60 + bcd(r[6])) * 10
                    + (r[7] & 15))
    ticks = [(vals[i] - vals[i + 1]) & 0xFFFFFFFF for i in range(NSAMP - 1)]
    return {"ticks_per_interval": ticks,
            "ticks_per_frame": [t / STEP for t in ticks],
            "mean_ticks_per_frame": sum(ticks) / (STEP * len(ticks)),
            "span_ticks": (vals[0] - vals[-1]) & 0xFFFFFFFF,
            "tod_tenths": tods, "tod_span_tenths": tods[-1] - tods[0]}


def verdict(run: dict, expect: float | None) -> tuple[str, bool]:
    """(label, meets_expectation).  expect None = the phi2 value for the
    video standard the run recorded."""
    pal = run["pal"]
    phi2 = PAL_TICKS if pal else NTSC_TICKS
    want = expect if expect is not None else phi2
    got = run["decoded"]["mean_ticks_per_frame"]
    ok = abs(got - want) <= TOL * want
    ratio = got / phi2
    label = ("phi2" if abs(ratio - 1) <= TOL else
             f"SCALED x{ratio:.3f} of phi2" if ratio > 1.5 else f"other x{ratio:.3f}")
    return label, ok


def report(runs: list[dict], expect: float | None, log) -> bool:
    all_ok = True
    log(f"{'speed':>5} {'run':>3} {'std':>4} {'ticks/frame (5 x 50-frame intervals)':<50} "
        f"{'mean':>9} {'TOD span':>9} {'polls/fr':>8}  verdict")
    for r in runs:
        d = r["decoded"]
        label, ok = verdict(r, expect)
        all_ok &= ok
        per = " ".join(f"{t:8.1f}" for t in d["ticks_per_frame"])
        hz = PAL_HZ if r["pal"] else NTSC_HZ
        log(f"{r['mhz']:>5} {r['run']:>3} {'PAL' if r['pal'] else 'NTSC':>4} {per:<50} "
            f"{d['mean_ticks_per_frame']:9.1f} {d['tod_span_tenths']:>4}/{STEP*(NSAMP-1)/hz*10:.0f}t "
            f"{r.get('polls', 0) / (STEP * (NSAMP - 1)):8.0f}  {label} {'OK' if ok else 'FAIL (expected ' + format(expect if expect is not None else (PAL_TICKS if r['pal'] else NTSC_TICKS), '.0f') + ')'}")
    return all_ok


def measure(t, labels, speeds, nruns, set_speed, log, tod50=None):
    from c64_test_harness import read_bytes, wait_for_memory, write_bytes
    tr = t.transport
    if wait_for_memory(tr, labels["ready"], 0xC3, timeout=30.0) is None:
        raise TimeoutError("probe never became ready")
    pal = read_bytes(tr, 0x02A6, 1)[0] != 0          # KERNAL PAL flag
    log(f"video standard per KERNAL $02A6: {'PAL' if pal else 'NTSC'}")
    runs = []
    for run in range(1, nruns + 1):
        for mhz in speeds:                            # interleaved 1,48,1,48,...
            set_speed(mhz)
            write_bytes(tr, labels["tod50"], bytes([tod50 if tod50 is not None else 1 if pal else 0]))
            write_bytes(tr, labels["done"], bytes([0]))
            write_bytes(tr, labels["go"], bytes([1]))
            if wait_for_memory(tr, labels["done"], 0xA5, timeout=60.0) is None:
                raise TimeoutError(f"run {run} at {mhz} MHz never finished")
            recs = read_bytes(tr, labels["recs"], 8 * NSAMP)
            polls = int.from_bytes(read_bytes(tr, labels["polls"], 4), "little")
            r = {"polls": polls, "mhz": mhz, "run": run, "pal": pal, "recs": recs.hex(),
                 "decoded": decode(recs)}
            runs.append(r)
            log(f"run {run} @ {mhz} MHz: raw {recs.hex()} -> mean "
                f"{r['decoded']['mean_ticks_per_frame']:.1f} ticks/frame, "
                f"TOD span {r['decoded']['tod_span_tenths']} tenths, "
                f"{polls / (STEP * (NSAMP - 1)):.0f} raster polls/frame")
    return runs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", choices=["u64", "vice"], default="u64")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--speeds", default="1,48")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "evidence", "w8-hw"))
    ap.add_argument("--tod50", type=int, choices=[0, 1], default=None,
                    help="force the CIA2 TOD 50/60 Hz bit (default: from KERNAL PAL flag)")
    ap.add_argument("--expect-ticks", type=float, default=None)
    ap.add_argument("--analyze", help="re-evaluate a saved runs.json, no device")
    a = ap.parse_args(argv)
    a.out = os.path.abspath(a.out)
    os.makedirs(a.out, exist_ok=True)
    logf = open(os.path.join(a.out, f"driver_{a.backend}.log"), "a")

    def log(msg):
        line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    if a.analyze:
        runs = json.load(open(a.analyze))["runs"]
        for r in runs:
            r["decoded"] = decode(bytes.fromhex(r["recs"]))
        return 0 if report(runs, a.expect_ticks, log) else 1

    prg, labels = build(a.out)
    speeds = [int(s) for s in a.speeds.split(",")]
    from c64_test_harness import UnifiedManager, ViceConfig
    if a.backend == "vice":
        cfg = ViceConfig(prg_path=prg, warp=False, sound=False)
        with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
            t = mgr.acquire()
            try:
                runs = measure(t, labels, [1], a.runs, lambda m: None, log, a.tod50)
            finally:
                mgr.release(t)
    else:
        from c64_test_harness import run_prg_via_sys
        import tests.bench.u64_session as us
        us.log = log                                   # u64_hold logs into ours
        with us.u64_hold(a.out) as hold:
            t = hold["target"]
            info = hold["info"]
            log(f"GET /v1/info: {json.dumps(info, sort_keys=True)}")
            t.transport.set_speed(1)
            log("uploading probe via run_prg_via_sys at 1 MHz")
            run_prg_via_sys(t, open(prg, "rb").read())

            def set_speed(m):
                t.transport.set_speed(m)
                time.sleep(0.5)
                log(f"CPU speed set to {m} MHz")
            runs = measure(t, labels, speeds, a.runs, set_speed, log, a.tod50)
        log(f"config restored={hold.get('restored')} reset_ok={hold.get('reset_ok')}")
    json.dump({"runs": runs, "expect_ticks": a.expect_ticks},
              open(os.path.join(a.out, f"runs_{a.backend}.json"), "w"), indent=1)
    return 0 if report(runs, a.expect_ticks, log) else 1


if __name__ == "__main__":
    sys.exit(main())
