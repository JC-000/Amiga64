#!/usr/bin/env python3
"""W15: why the 1351's SID pot readings compress at the C64U's 64 MHz turbo,
and whether dropping the CPU to 1 MHz via $D031 around each pot read is a
sound workaround.  Test-only (tests/mturbo/mturbo.s, its own area at $C800).

Measured 2026-10-11 (build/evidence/w14-mouse/diag/run1.log): while the
mouse moves, POTX/POTY span $51-$D3 / $4D-$CE at 1 MHz but $60-$80 /
$5C-$7C (33 values, centre moved) at 64 MHz (Turbo Control = Manual).

Legs (unattended first, the mouse in port 1 lying untouched):
  M4  device info, the U64 settings that touch the pots / turbo, any USB
      view the harness has; the SID sockets must read Enabled and 8580
      (a firmware-defaults state was seen once): otherwise no pot leg runs.
      USB: the user confirmed 2026-10-12 nothing is plugged in (recorded).
  M5  read timing in phi2 ticks (CIA1 timer A): per read of $D419, $D41A,
      $D012, $DC01, $DE00 (cartridge I/O1) and RAM, the empty loop
      subtracted; Turbo Control Off, Manual 64 and, with the registers
      mode, every $D031 index (RAM, $D419) plus the switch latency (the
      speed right after a $D031 write).  Also the wait arithmetic (WCHK).
  M3s still mouse: the 1 MHz reference and the turbo value over many
      samples, then trials: at index 15, $D031 := 0, wait k phi2, read,
      back to 15 (k = 0 .. 3072); per k the distribution against both.
      Controls: Turbo Control Off and Manual 64 via config.
Attended (the supervisor relays the CUE lines; nothing shows on the C64):
  M1  the user moves the mouse; $D031 swept 0..15 then 15..0 in 3-s
      sub-windows; per index the POTX/POTY range and distinct values: is
      the window the same at every turbo speed (a mode) or does it scale?
  M3m the user moves the mouse; the workaround trials interleaved with
      a reference entry that keeps the CPU at 1 MHz between reads.
  M7  (--only m7: M4 + this) config path vs register path: one continuous
      left-right movement while Turbo Control switches between Manual /
      CPU Speed 64 (the run1 path, no $D031 write at all) and C64U Turbo
      Registers with $D031 = 15; (e) Manual 64 with $D031 = 15 written
      anyway; (f, opt-in) the registers mode with CPU Speed config 64.  Pots
      read at full speed, no drop; a window is COMPRESSED when POTX spans
      under 64 values.

    U64_HOST=198.51.100.158 python3 -m tests.run_mturbo --prg build/runner.prg \
        --backend u64 --unattended-only        # M4, M5, M3s (mouse untouched)
    U64_HOST=198.51.100.158 python3 -m tests.run_mturbo --prg build/runner.prg \
        --backend u64 [--skip-unattended]      # + the attended M1, M3m
    U64_HOST=198.51.100.158 python3 -m tests.run_mturbo --prg build/runner.prg \
        --backend u64 --only m7 [--m7-windows abcdef]   # M4 + the attended M7
    python3 -m tests.run_mturbo --prg build/runner.prg   # VICE dry run (fast)
    python3 -m tests.run_mturbo --from-log build/evidence/w14-mouse/diag/run1.log

Output: the full log (tee) and summary.json in build/evidence/w15-mdiag/
(--tag names the run), the u64_hold config snapshots in a subdirectory,
a SESSION.log line, and DONE-<tag>.  Exit 0 = every leg ran (the verdict is
data, not a pass/fail).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(ROOT, "build", "evidence", "w15-mdiag")
SRC = os.path.join(ROOT, "tests", "mturbo", "mturbo.s")

ORG, PRM, TBL, OUT, DATA, DATA_N = 0xC800, 0xCC00, 0xCC80, 0xCD00, 0xC000, 0x800
TIME, SAMPLE, TRIALS, WCHK = 1, 2, 3, 4
U64 = "U64 Specific Settings"
RAM = 0xCD80                    # a quiet RAM byte for the RAM reads
TARGETS = {"null": 0, "ram": RAM, "D419": 0xD419, "D41A": 0xD41A, "D012": 0xD012,
           "DC01": 0xDC01, "DE00": 0xDE00}
OUTER = 255                     # TIME loops: 133 cycles each, 33915 ticks at 1 MHz
STILL_KS = [0, 256, 512, 768, 1024, 1536, 2048, 3072]
MOVE_KS = [0, 512, 1024, 1536, 2048]
T_BETWEEN = 20000               # phi2 ticks at the "between" speed before each trial
M3M_ROUNDS = 30                 # M3m: rounds of the 6 entries per call (~4 s)
M1_SPACE, M1_SUB = 4096, 3.0    # M1: a sample every 4096 phi2, 3-s sub-windows
CUE_S = 12.0                    # each attended window announced this far ahead
SCALE = 1.0                     # --fast (VICE): windows and cues shrink
NOWRITE = 0x80                  # SAMPLE idx / exit: leave $D031 alone
M7_PLAN = {     # window: (Turbo Control, regs CPU Speed, $D031 written, seconds, what)
    "a": ("manual64", None, NOWRITE, 30, "Manual / CPU Speed 64 (config path, as run1)"),
    "b": ("regs", None, 15, 30, "C64U Turbo Registers, $D031 = 15"),
    "c": ("manual64", None, NOWRITE, 30, "Manual / CPU Speed 64 again"),
    "d": ("regs", None, 15, 30, "C64U Turbo Registers, $D031 = 15 again"),
    "e": ("manual64", None, 15, 15, "Manual 64, the stub writes $D031 = 15 anyway"),
    "f": ("regs", "top", 15, 15, "Turbo Registers with CPU Speed config 64, $D031 = 15"),
}
M7_DEFAULT = "abcde"
M7_SWITCH_S = 4.0               # a Turbo Control change: set, read back, 2 s settle, probe
COMPRESSED_W = 64               # a moving 1351 spans ~$51-$D3 (131 wide); run1's
                                # compressed window was 33 wide
NOMOTION_D = 8                  # fewer distinct values: the mouse did not move
USB_FACT = ("user-confirmed 2026-10-12: nothing is plugged into the C64U's USB ports "
            "(the harness has no USB view); the 1351 is in port 1")

ATTENDED = [    # (tag, seconds, what the user does)
    ("M1-X", 2 * 16 * M1_SUB, "move the mouse SLOWLY LEFT-RIGHT, continuously, the whole window"),
    ("M1-Y", 2 * 16 * M1_SUB, "move the mouse SLOWLY DOWN-UP, continuously, the whole window"),
    ("M3m-X", 45.0, "move the mouse SLOWLY LEFT-RIGHT, continuously, the whole window"),
    ("M3m-Y", 45.0, "move the mouse SLOWLY DOWN-UP, continuously, the whole window"),
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Tee:
    def __init__(self, path: str):
        self.fh, self.out = open(path, "a"), sys.stdout

    def write(self, s):
        self.out.write(s)
        self.fh.write(s)

    def flush(self):
        self.out.flush()
        self.fh.flush()


class Deadline:
    """A per-leg wall-clock budget, checked between device calls."""

    def __init__(self, leg: str, seconds: float):
        self.leg, self.t1 = leg, time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self.t1:
            raise TimeoutError(f"leg {self.leg}: over its deadline")


def build_stub() -> bytes:
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "mturbo.cfg")
        open(cfg, "w").write("MEMORY { M: file = %%O, start = $%04X, size = $400; }\n"
                             "SEGMENTS { CODE: load = M, type = rw; }\n" % ORG)
        obj, out = os.path.join(d, "mturbo.o"), os.path.join(d, "mturbo.bin")
        for cmd in (["ca65", "-o", obj, SRC], ["ld65", "-C", cfg, "-o", out, obj]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(r.stdout + r.stderr)
        return open(out, "rb").read()


# --------------------------------------------------------------------------
# statistics (also run on run1.log by --from-log)
# --------------------------------------------------------------------------

def pot_stats(vals: list[int]) -> dict:
    if not vals:
        return {"n": 0}
    hist: dict[int, int] = {}
    for v in vals:
        hist[v] = hist.get(v, 0) + 1
    top = sorted(hist.items(), key=lambda kv: -kv[1])
    return {"n": len(vals), "min": min(vals), "max": max(vals),
            "width": max(vals) - min(vals) + 1, "distinct": len(hist),
            "mode": top[0][0], "mean": round(statistics.fmean(vals), 2),
            "jumps": sum(1 for a, b in zip(vals, vals[1:]) if abs(a - b) > 0x40),
            "top": [[v, c] for v, c in top[:6]]}


def fmt(st: dict) -> str:
    if not st.get("n"):
        return "no samples"
    return (f"${st['min']:02X}-${st['max']:02X} ({st['distinct']:3d} values, mode "
            f"${st['mode']:02X}, mean {st['mean']:6.2f})")


def squeezed(st: dict) -> bool:
    """A moving-mouse window narrower than COMPRESSED_W: run1's 64 MHz case."""
    return bool(st.get("n")) and st["width"] < COMPRESSED_W


def motion(st: dict) -> str:
    """COMPRESSED / full range, or NO MOTION (a still 1351 reads 1-2 values)."""
    if not st.get("n") or st["distinct"] < NOMOTION_D:
        return "NO MOTION"
    return "COMPRESSED" if squeezed(st) else "full range"


def near(vals: list[int], centre: int, tol: int = 2) -> float:
    return sum(1 for v in vals if abs(v - centre) <= tol) / len(vals) if vals else 0.0


def spearman(a: list[float], b: list[float]) -> float:
    def ranks(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    ra, rb = ranks(a), ranks(b)
    if len(a) < 3 or len(set(ra)) < 2 or len(set(rb)) < 2:
        return 0.0
    return statistics.correlation(ra, rb)


def classify_m1(per_idx: dict, rates: dict, axis: str) -> dict:
    """per_idx {index: stats of the axis that moved}; rates {index: delivered
    multiple of phi2} (M5).  MODE: every turbo index shows the same narrowed
    window; RATIO: the window narrows with the rate; NONE: no narrowing."""
    idx = sorted(i for i in per_idx if per_idx[i].get("n"))
    if 0 not in idx or len(idx) < 3:
        return {"axis": axis, "class": "INCOMPLETE"}
    w0 = per_idx[0]["distinct"]
    turbo = [i for i in idx if i > 0]
    ws = [per_idx[i]["distinct"] for i in turbo]
    med = statistics.median(ws)
    narrowed = [i for i in turbo if per_idx[i]["distinct"] < 0.7 * w0]
    rho = spearman([rates.get(i, i) for i in idx], [per_idx[i]["distinct"] for i in idx])
    same = all(abs(w - med) <= 0.2 * med for w in ws)
    if not narrowed:
        cls = "NONE"
    elif same and med < 0.7 * w0:
        cls = "MODE"
    elif rho < -0.8:
        cls = "RATIO"
    else:
        cls = "MIXED"
    return {"axis": axis, "class": cls, "distinct_1mhz": w0, "median_turbo": med,
            "first_narrowed_index": narrowed[0] if narrowed else None,
            "spearman_rate_vs_distinct": round(rho, 3),
            "distinct_by_index": {i: per_idx[i]["distinct"] for i in idx}}


def from_log(path: str) -> int:
    """Run the window statistics and the M1 classifier on a --mouse-diag log
    (run1.log): config A = 1 MHz, B/C = 64 MHz (Manual)."""
    cfg, rows = None, {}
    for line in open(path):
        m = re.match(r"CONFIG (\w):", line)
        if m:
            cfg = m.group(1)
            continue
        m = re.match(r"\s+(\w+)\s+[\d.]+\s+\$([0-9A-F]{2}) \$([0-9A-F]{2})\s+"
                     r"\$([0-9A-F]{2}) \$([0-9A-F]{2})\s*$", line)
        if m and cfg:
            rows.setdefault((cfg, m.group(1)), []).append(
                (int(m.group(4), 16), int(m.group(5), 16)))
    out = {}
    for (c, tag), rs in sorted(rows.items()):
        sx, sy = pot_stats([r[0] for r in rs]), pot_stats([r[1] for r in rs])
        out[f"{c}/{tag}"] = {"x": sx, "y": sy}
        flag = ""
        if tag in ("x", "y"):
            flag = "  -> " + motion(sx if tag == "x" else sy)
        print(f"  {c}/{tag:5} n={len(rs):4d}  POTX {fmt(sx)}  POTY {fmt(sy)}{flag}")
    for axis, col in (("x", 0), ("y", 1)):
        per = {}
        if (("A", axis)) in rows:
            per[0] = pot_stats([r[col] for r in rows[("A", axis)]])
        if (("B", axis)) in rows:
            per[15] = pot_stats([r[col] for r in rows[("B", axis)]])
            per[14] = per[15]           # two-point log: the classifier needs 3
        c = classify_m1(per, {0: 1.0, 14: 40.0, 15: 47.0}, axis)
        print(f"  classify ({axis}, two points only, index 14 := 15): {c}")
        out[f"classify_{axis}"] = c
    return 0 if out else 1


# --------------------------------------------------------------------------
# the device side
# --------------------------------------------------------------------------

class Diag:
    def __init__(self, target, client=None):
        self.tg, self.t, self.client = target, target.transport, client
        self.vice = target.backend == "vice"
        self.mode = "Off"           # the Turbo Control this runner last set
        self.stub = build_stub()
        self.w(ORG, self.stub)
        log(f"stub: {len(self.stub)} bytes at ${ORG:04X}")

    def w(self, addr: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, addr, data)

    def r(self, addr: int, n: int) -> bytes:
        from c64_test_harness import read_bytes
        return read_bytes(self.t, addr, n)

    def run(self, mode: int, idx: int = 0, n: int = 0, reps: int = 0, addr: int = 0,
            sw: int = 0, space: int = 0, m: int = 0, k: int = 0, exit_: int = 0,
            timeout: float = 60.0):
        from tests.harness_util import call_runner
        self.w(PRM, bytes([mode, idx, n & 0xFF, n >> 8, reps, addr & 0xFF, addr >> 8, sw,
                           space & 0xFF, space >> 8, m, k & 0xFF, k >> 8, exit_]))
        call_runner(self.tg, ORG, timeout)

    # -- TIME / WCHK -------------------------------------------------------
    def stopwatch(self, mode: int, reps: int, **kw) -> list[int]:
        self.run(mode, reps=reps, **kw)
        raw = self.r(OUT, 3 * reps)
        ticks = []
        for i in range(reps):
            lo, hi, uf = raw[3 * i:3 * i + 3]
            if uf:
                raise RuntimeError("stopwatch overflow (> 65535 phi2 ticks)")
            ticks.append(0xFFFF - (hi << 8 | lo))
        return ticks

    def ticks(self, addr: int, idx: int, outer: int = OUTER, reps: int = 5,
              sw: int | None = None) -> int:
        return min(self.stopwatch(TIME, reps, idx=idx, n=outer, addr=addr,
                                  sw=0 if sw is None else 0x80 | sw))

    def per_read(self, idx: int, names: list[str], outer: int = OUTER) -> dict:
        """{name: phi2 ticks per read} (the empty loop subtracted)."""
        t0 = self.ticks(0, idx, outer)
        return {n: round((self.ticks(TARGETS[n], idx, outer) - t0) / (outer * 32), 4)
                for n in names if n != "null"}

    def wchk(self, idx: int, ks: list[int]) -> dict:
        base = min(self.stopwatch(WCHK, 3, idx=idx, k=0))
        return {k: min(self.stopwatch(WCHK, 3, idx=idx, k=k)) - base for k in ks}

    # -- SAMPLE / TRIALS ---------------------------------------------------
    def sample(self, idx: int, n: int, space: int, exit_: int = 0) -> list[tuple[int, int]]:
        """idx / exit_ with bit 7 set (NOWRITE): the stub leaves $D031 alone."""
        assert 2 * n <= DATA_N
        t0 = time.monotonic()
        self.run(SAMPLE, idx=idx, n=n, space=space, exit_=exit_,
                 timeout=30.0 + n * space / 1e6 * 3)
        t1 = time.monotonic()
        raw = self.r(DATA, 2 * n)
        self.timing = (t1 - t0, time.monotonic() - t1)     # (call s, read s)
        return [(raw[2 * i], raw[2 * i + 1]) for i in range(n)]

    def trials(self, entries: list[tuple[int, int, int]], rounds: int,
               between: int = T_BETWEEN) -> list[tuple]:
        """entries: (k, idx between, idx read).  -> [(entry, preX, preY, X, Y)]"""
        m = len(entries)
        assert 4 * m * rounds <= DATA_N and 4 * m <= 0x80
        self.w(TBL, b"".join(bytes([k & 0xFF, k >> 8, b, rd]) for k, b, rd in entries))
        est = rounds * sum(between + k for k, _, _ in entries) / 1e6
        self.run(TRIALS, n=rounds, m=m, space=between, timeout=30.0 + 3 * est)
        raw = self.r(DATA, 4 * m * rounds)
        return [(i % m, *raw[4 * i:4 * i + 4]) for i in range(m * rounds)]

    # -- Turbo Control -----------------------------------------------------
    def set_turbo(self, what: str, regs_speed: str | None = None) -> None:
        """'off', 'manual64' or 'regs' (the device's "... Turbo Registers",
        CPU Speed at the table's first entry so a reverting reset task lands
        slow, as the bench gist does; *regs_speed* "top": its last entry).
        manual64 is set_turbo_mhz(64): the transport.set_speed(64) path
        run_inp's --mouse-diag used (run1.log)."""
        if self.vice:
            self.mode = what
            return
        from c64_test_harness.backends.ultimate64_helpers import set_turbo_mhz
        if what == "off":
            set_turbo_mhz(self.client, None)
            want = "Off"
        elif what == "manual64":
            set_turbo_mhz(self.client, 64)
            want = "Manual"
        else:
            vals = [str(v) for v in self.client.get_config_item(U64, "Turbo Control")
                    .get("values", [])]
            want = next((v for v in vals if "Turbo Registers" in v), None)
            if want is None:
                raise RuntimeError(f"no '... Turbo Registers' mode in {vals}")
            speeds = [str(v) for v in self.client.get_config_item(U64, "CPU Speed")
                      .get("values", [])]
            self.speeds = speeds
            sp = speeds[-1] if regs_speed == "top" else speeds[0]
            self.client.set_config_items(U64, {"Turbo Control": want, "CPU Speed": sp})
        got = self.client.get_config_item(U64, "Turbo Control").get("current")
        if str(got).strip() != want:
            raise RuntimeError(f"Turbo Control reads {got!r}, not {want!r}")
        self.mode = what
        log(f"Turbo Control = {want!r}"
            f"{', CPU Speed ' + repr(sp) if what == 'regs' else ''}")
        time.sleep(2.0)

    def live(self, budget: float = 25.0) -> dict:
        """Prove $D031 is honoured (index 0 slow AND index 15 fast, RAM loop),
        asking again for up to *budget* s (after a reset the core ignores
        $D031 for a few seconds: the bench gist)."""
        t1, n = time.monotonic() + (0 if self.vice else budget), 0
        while True:
            n += 1
            slow = self.ticks(RAM, 0, outer=64, reps=1)
            fast = self.ticks(RAM, 15, outer=64, reps=1)
            ratio = slow / max(fast, 1)
            if ratio > 8 or time.monotonic() > t1:
                break
            time.sleep(0.5)
        res = {"live": ratio > 8, "probes": n, "slow_ticks": slow, "fast_ticks": fast,
               "ratio": round(ratio, 2)}
        log(f"$D031 liveness: {res}")
        return res


# --------------------------------------------------------------------------
# legs
# --------------------------------------------------------------------------

def leg_m4(dg: Diag, hold: dict) -> dict:
    """Device info and the settings that touch the pots; -> {"sid_ok": ...}."""
    if dg.vice:
        print("M4: VICE (no device info, no config)")
        return {"sid_ok": True, "vice": True}
    from c64_test_harness.backends.ultimate64_helpers import (
        get_detected_sid_types, get_sid_socket_enabled)
    c = dg.client
    info, before = hold.get("info", {}), hold.get("before", {})
    print(f"M4 /v1/info: {info}")
    keys = ("paddle", "mouse", "usb", "joystick", "turbo", "cpu speed", "sid", "badline")
    pick = {cat: {k: v for k, v in items.items()
                  if any(s in (cat + " " + k).lower() for s in keys)}
            for cat, items in before.items()}
    for cat, items in pick.items():
        for k, v in items.items():
            print(f"M4 config {cat} / {k} = {v!r}")
    usb = USB_FACT
    print(f"M4 USB: {usb}")
    try:
        drives = c.list_drives()
    except Exception as e:              # noqa: BLE001 - recorded
        drives = repr(e)
    en, det = get_sid_socket_enabled(c), get_detected_sid_types(c)
    sid_ok = en == {1: True, 2: True} and all(v == "8580" for v in det.values()) \
        and len(det) == 2
    print(f"M4 SID sockets enabled {en}, detected {det}: "
          f"{'OK' if sid_ok else 'NOT Enabled/8580: no SID or input leg runs'}")
    return {"info": info, "config": pick, "usb": usb, "drives": drives,
            "sid_enabled": en, "sid_detected": det, "sid_ok": sid_ok}


def leg_m5(dg: Diag) -> dict:
    """Read timing in phi2 ticks per read, every Turbo Control mode."""
    names = list(TARGETS)
    res: dict = {}
    dl = Deadline("M5", 300)
    ks = [256, 1024, 3072]
    for what in ("off", "manual64", "regs"):
        dg.set_turbo(what)
        r: dict = {}
        if what == "regs":
            r["live"] = dg.live()
            r["index"] = {}
            for i in range(16):
                dl.check()
                r["index"][i] = pr = dg.per_read(i, ["ram", "D419"])
                # relative to index 0: badline DMA stretches the 4-cycle read
                rate = round(r["index"][0]["ram"] / pr["ram"], 2) if pr["ram"] > 0 else None
                pr["rate"] = rate
                print(f"M5 regs idx {i:2d}: RAM {pr['ram']:.4f}  D419 {pr['D419']:.4f} "
                      f"ticks/read  (delivered x{rate})", flush=True)
            for i in (0, 15):
                dl.check()
                r[f"all_{i}"] = pr = dg.per_read(i, names)
                print(f"M5 regs idx {i:2d} all: {pr}", flush=True)
            # the switch latency: 4 loops (532 cycles) right after a $D031 write
            for frm, to in ((15, 0), (0, 15)):
                t_sw = dg.ticks(RAM, frm, outer=4, sw=to)
                t_st = dg.ticks(RAM, to, outer=4)
                t_fr = dg.ticks(RAM, frm, outer=4)
                r[f"switch_{frm}_{to}"] = s = {"after_write": t_sw, "steady_to": t_st,
                                               "steady_from": t_fr}
                print(f"M5 switch {frm}->{to}: 4 loops after the write {t_sw} ticks; "
                      f"steady at {to}: {t_st}, at {frm}: {t_fr}", flush=True)
            r["wchk_15"] = dg.wchk(15, ks)
            print(f"M5 wait check at idx 15 (measured - k=0): {r['wchk_15']}")
        else:
            r["all"] = pr = dg.per_read(15, names)  # $D031 := 15 must be ignored here
            print(f"M5 {what} (with $D031 := 15 written): {pr}", flush=True)
            if what == "off":
                r["wchk"] = dg.wchk(0, ks)
                print(f"M5 wait check at 1 MHz (measured - k=0): {r['wchk']}")
        res[what] = r
        dl.check()
    return res


def still_block(dg: Diag, label: str, ref_idx: int, turbo_idx: int, rounds_calls: int,
                ks: list[int]) -> dict:
    n = 1000
    out: dict = {"ref": dg.sample(ref_idx, n, 2048)}
    out["turbo"] = dg.sample(turbo_idx, n, 2048)
    ents = [(k, turbo_idx, ref_idx) for k in ks]
    rounds = DATA_N // (4 * len(ents))
    tr = []
    for _ in range(rounds_calls):
        tr += dg.trials(ents, rounds)
    rx, ry = pot_stats([p[0] for p in out["ref"]]), pot_stats([p[1] for p in out["ref"]])
    tx, ty = pot_stats([p[0] for p in out["turbo"]]), pot_stats([p[1] for p in out["turbo"]])
    print(f"M3s {label} 1 MHz ref (idx {ref_idx}): POTX {fmt(rx)}  POTY {fmt(ry)}")
    print(f"M3s {label} turbo    (idx {turbo_idx}): POTX {fmt(tx)}  POTY {fmt(ty)}")
    summ = {"ref": {"x": rx, "y": ry}, "turbo": {"x": tx, "y": ty}, "k": {}}
    for e, k in enumerate(ks):
        rows = [t for t in tr if t[0] == e]
        xs, ys = [t[3] for t in rows], [t[4] for t in rows]
        px, py = [t[1] for t in rows], [t[2] for t in rows]
        sx, sy = pot_stats(xs), pot_stats(ys)
        d = {"x": sx, "y": sy, "pre_x": pot_stats(px), "pre_y": pot_stats(py),
             "near_ref_x": round(near(xs, rx["mode"]), 3),
             "near_ref_y": round(near(ys, ry["mode"]), 3),
             "near_turbo_x": round(near(xs, tx["mode"]), 3),
             "near_turbo_y": round(near(ys, ty["mode"]), 3)}
        summ["k"][k] = d
        print(f"M3s {label} k={k:4d}: POTX {fmt(sx)} ref {d['near_ref_x']:.0%} turbo "
              f"{d['near_turbo_x']:.0%} | POTY {fmt(sy)} ref {d['near_ref_y']:.0%} turbo "
              f"{d['near_turbo_y']:.0%}   (pre X {fmt(d['pre_x'])})", flush=True)
    return summ


def leg_m3s(dg: Diag) -> dict:
    res = {}
    dl = Deadline("M3s", 300)
    dg.set_turbo("off")
    res["off"] = still_block(dg, "Turbo Off", 0, 15, 1, STILL_KS)
    dl.check()
    dg.set_turbo("manual64")
    t = dg.sample(15, 1000, 2048)
    res["manual64"] = {"x": pot_stats([p[0] for p in t]), "y": pot_stats([p[1] for p in t])}
    print(f"M3s Manual 64: POTX {fmt(res['manual64']['x'])}  POTY {fmt(res['manual64']['y'])}")
    dl.check()
    dg.set_turbo("regs")
    res["live"] = dg.live()
    res["regs"] = still_block(dg, "regs", 0, 15, 2, STILL_KS)
    return res


def cue(tag: str, secs: float, what: str) -> None:
    print(f"CUE {time.strftime('%H:%M:%S')} [{tag}] in {CUE_S * SCALE:.0f} s, for "
          f"{secs:.0f} s: {what}", flush=True)
    time.sleep(CUE_S * SCALE)
    print(f"GO  {time.strftime('%H:%M:%S')} [{tag}] {what}", flush=True)


def leg_m1(dg: Diag, axis: str, rates: dict) -> dict:
    tag, secs, what = next(a for a in ATTENDED if a[0] == f"M1-{axis.upper()}")
    col = 0 if axis == "x" else 1
    n = int(M1_SUB * SCALE * 1.0227e6 / M1_SPACE)
    cue(tag, secs * SCALE, what)
    per: dict = {i: [] for i in range(16)}
    other: dict = {i: [] for i in range(16)}
    dl = Deadline(tag, secs * SCALE * 3 + 60)
    for p, order in enumerate((range(16), range(15, -1, -1))):
        for i in order:
            dl.check()
            s = dg.sample(i, n, M1_SPACE)
            per[i] += [v[col] for v in s]
            other[i] += [v[1 - col] for v in s]
            st = pot_stats([v[col] for v in s])
            print(f"  {tag} pass {p + 1} idx {i:2d}: POT{axis.upper()} {fmt(st)}", flush=True)
    print(f"END {time.strftime('%H:%M:%S')} [{tag}] stop moving", flush=True)
    stats = {i: pot_stats(v) for i, v in per.items()}
    for i in range(16):
        print(f"  {tag} idx {i:2d} (x{rates.get(i, '?')}): POT{axis.upper()} {fmt(stats[i])}"
              f"  other axis {fmt(pot_stats(other[i]))}")
    c = classify_m1(stats, rates, axis)
    print(f"  {tag} classification: {c['class']}  {c}", flush=True)
    return {"stats": stats, "other": {i: pot_stats(v) for i, v in other.items()},
            "classify": c}


def leg_m3m(dg: Diag, axis: str) -> dict:
    tag, secs, what = next(a for a in ATTENDED if a[0] == f"M3m-{axis.upper()}")
    col = 3 if axis == "x" else 4
    ents = [(k, 15, 0) for k in MOVE_KS] + [(2048, 0, 0)]   # the last: stays at 1 MHz
    names = [f"k={k}" for k in MOVE_KS] + ["ref 1MHz"]
    rounds = max(1, int(M3M_ROUNDS * min(1.0, SCALE * 4)))   # ~4 s per call
    cue(tag, secs * SCALE, what)
    t1, tr = time.monotonic() + secs * SCALE, []
    dl = Deadline(tag, secs * SCALE * 3 + 60)
    while time.monotonic() < t1:
        dl.check()
        got = dg.trials(ents, rounds)
        tr += got
        print(f"  {tag} chunk: {len(got)} trials", flush=True)
    print(f"END {time.strftime('%H:%M:%S')} [{tag}] stop moving", flush=True)
    out = {}
    for e, nm in enumerate(names):
        rows = [t for t in tr if t[0] == e]
        out[nm] = {"read": pot_stats([t[col] for t in rows]),
                   "other": pot_stats([t[7 - col] for t in rows]),
                   "pre": pot_stats([t[col - 2] for t in rows])}
        print(f"  {tag} {nm:9}: POT{axis.upper()} {fmt(out[nm]['read'])}   "
              f"(pre-drop read {fmt(out[nm]['pre'])})", flush=True)
    return out


def leg_m7(dg: Diag, windows: str) -> dict:
    """Config path vs register path: one continuous left-right movement,
    windows switching Turbo Control between them; pots read at full speed,
    no drop; $D031 held for the whole window (NOWRITE at exit)."""
    n = int(M1_SUB * SCALE * 1.0227e6 / M1_SPACE)
    total = sum(M7_PLAN[w][3] for w in windows) * SCALE + len(windows) * M7_SWITCH_S
    cue("M7", total, "move the mouse SLOWLY LEFT-RIGHT, continuously, until END "
        "(the speed changes underneath; keep going)")
    out: dict = {}
    dl = Deadline("M7", total * 2 + 60)
    for w in windows:
        what, rsp, idx, secs, desc = M7_PLAN[w]
        dg.set_turbo(what, regs_speed=rsp)
        live = dg.live(budget=10.0) if what == "regs" else None
        speed = dg.per_read(idx if idx != NOWRITE else 15, ["ram"], outer=64)["ram"]
        print(f"  M7 ({w}) {desc}: RAM {speed:.4f} ticks/read"
              f"{'' if live is None else ', $D031 live ' + str(live['live'])}", flush=True)
        xs, ys, calls, t1 = [], [], [], time.monotonic() + secs * SCALE
        per_call = M1_SUB * SCALE
        while not calls or time.monotonic() + per_call < t1:    # no overrun
            dl.check()
            got = dg.sample(idx, n, M1_SPACE, exit_=NOWRITE)
            per_call = max(per_call, sum(dg.timing))
            xs += [v[0] for v in got]
            ys += [v[1] for v in got]
            calls.append([round(t, 2) for t in dg.timing])
            print(f"  M7 ({w}) call {dg.timing[0]:.2f} s + read {dg.timing[1]:.2f} s: "
                  f"POTX {fmt(pot_stats([v[0] for v in got]))}", flush=True)
        sx, sy = pot_stats(xs), pot_stats(ys)
        out[w] = {"desc": desc, "turbo": what, "regs_speed": rsp, "d031": idx,
                  "ram_ticks": speed, "live": live, "x": sx, "y": sy, "calls": calls,
                  "state": motion(sx), "compressed": motion(sx) == "COMPRESSED"}
        print(f"WINDOW M7 ({w}) {desc}: POTX {fmt(sx)} width {sx.get('width')} -> "
              f"{out[w]['state']};  POTY {fmt(sy)}", flush=True)
    print(f"END {time.strftime('%H:%M:%S')} [M7] stop moving", flush=True)
    return out


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------

def verdict(s: dict) -> list[str]:
    v = []
    m5 = s.get("M5")
    if m5 and "regs" in m5:
        a15 = m5["regs"].get("all_15", {})
        if "live" in m5["regs"] and not m5["regs"]["live"]["live"]:
            a15 = {}
            v.append("M5: $D031 NEVER WENT LIVE: the index legs measured one speed only")
        if a15:
            d, ram, io1 = a15["D419"], a15["ram"], a15["DE00"]
            how = ("held to the phi2 bus (goes out to the SID side)" if d >= 0.75 else
                   "served at CPU speed (from the core, not the bus)" if d <= 2 * ram + 0.01
                   else "between CPU speed and the bus")
            v.append(f"M5: at index 15 a $D419 read costs {d:.3f} phi2 ticks (RAM {ram:.3f}, "
                     f"$DE00 {io1:.3f}, $D012 {a15['D012']:.3f}, $DC01 {a15['DC01']:.3f}): {how}")
    for name in ("off", "regs"):
        b = s.get("M3s", {}).get(name)
        if not b:
            continue
        ok = [k for k, d in b["k"].items() if min(d["near_ref_x"], d["near_ref_y"]) >= 0.95]
        same = abs(b["ref"]["x"]["mode"] - b["turbo"]["x"]["mode"]) <= 2 and \
            abs(b["ref"]["y"]["mode"] - b["turbo"]["y"]["mode"]) <= 2
        v.append(f"M3s {name}: 1 MHz ref X ${b['ref']['x']['mode']:02X} Y "
                 f"${b['ref']['y']['mode']:02X}, turbo X ${b['turbo']['x']['mode']:02X} Y "
                 f"${b['turbo']['y']['mode']:02X}"
                 + (" (the same: a still mouse does not discriminate)" if same else "")
                 + f"; k with >= 95% of reads at the 1 MHz value: {sorted(ok) or 'none'}")
    m64 = s.get("M3s", {}).get("manual64")
    if m64:
        v.append(f"M3s Manual 64 still: X ${m64['x']['mode']:02X} Y ${m64['y']['mode']:02X}")
    for ax in ("x", "y"):
        m1 = s.get(f"M1-{ax}")
        if m1:
            c = m1["classify"]
            v.append(f"M1 {ax.upper()}: {c['class']} (distinct at 1 MHz {c.get('distinct_1mhz')},"
                     f" median at turbo {c.get('median_turbo')}, first narrowed index "
                     f"{c.get('first_narrowed_index')}, rho {c.get('spearman_rate_vs_distinct')})")
        m3 = s.get(f"M3m-{ax}")
        if m3:
            ref = m3["ref 1MHz"]["read"]
            good = [nm for nm, d in m3.items() if nm != "ref 1MHz" and d["read"].get("n")
                    and d["read"]["distinct"] >= 0.9 * ref.get("distinct", 1)
                    and d["read"]["width"] >= 0.9 * ref.get("width", 1)]
            v.append(f"M3m {ax.upper()}: ref (1 MHz between) {fmt(ref)}; "
                     + "; ".join(f"{nm} {d['read'].get('distinct')} values"
                                 for nm, d in m3.items() if nm != "ref 1MHz")
                     + f"; >= 90% of the reference range and values: {good or 'none'}")
    m7 = s.get("M7")
    if m7:
        for w, d in m7.items():
            v.append(f"M7 ({w}) {d['desc']}: POTX {fmt(d['x'])} width {d['x'].get('width')}"
                     f" -> {d['state']}")
        moved = {w: d for w, d in m7.items() if d["state"] != "NO MOTION"}
        man = [d["compressed"] for w, d in moved.items() if w in "ac"]
        reg = [d["compressed"] for w, d in moved.items() if w in "bd"]
        if len(moved) < len(m7):
            v.append(f"M7: NO MOTION in {sorted(set(m7) - set(moved))}: excluded")
        if not (man and reg):
            v.append("M7: INCONCLUSIVE: no moving window on one of the paths")
        else:
            v.append("M7: " + (
                "CONFIRMED: the Manual config path compresses, the register path does not"
                if all(man) and not any(reg) else
                "REJECTED: neither path compresses (run1's case not reproduced)"
                if not any(man) and not any(reg) else
                "REJECTED: both paths compress" if all(man) and all(reg) else
                "MIXED: the repeats disagree; see the windows"))
    return v


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------

def m7_schedule(windows: str) -> str:
    t = ["=" * 72, "1351 TURBO DIAG M7 (config path vs register path): the mouse in PORT 1",
         "of the C64U.  Nothing shows on the C64 screen; follow CUE / GO / END only.",
         "Leave the mouse alone until GO (M4 runs first, ~10 s).",
         f"CUE {CUE_S:.0f} s ahead, then ONE continuous window: move the mouse SLOWLY",
         "LEFT-RIGHT without stopping until END.  Underneath, the runner switches:"]
    tot = 0.0
    for w in windows:
        secs, desc = M7_PLAN[w][3], M7_PLAN[w][4]
        t.append(f"  ({w}) {secs:3d} s  {desc}")
        tot += secs + M7_SWITCH_S
    t.append(f"Moving about {tot:.0f} s (windows + ~{M7_SWITCH_S:.0f} s per switch); "
             f"with the cue about {(tot + CUE_S) / 60:.1f} min.")
    t.append("=" * 72)
    return "\n".join(t)


def schedule_text(skip_unattended: bool, attended: bool) -> str:
    t = ["=" * 72, "1351 TURBO DIAG: the mouse in PORT 1 of the C64U.  Nothing shows on",
         "the C64 screen; follow the CUE / GO / END lines only."]
    if not skip_unattended:
        t.append("FIRST (unattended, ~2-3 min): M4, M5, M3-still.  The mouse lies STILL,")
        t.append("nobody touches it until CUE 1.")
    if attended:
        tot = 0.0
        t.append("THEN the attended windows, each announced %.0f s ahead (CUE), starting at" % CUE_S)
        t.append("GO and ending at END (stop moving at END; rest until the next CUE):")
        for n, (tag, secs, what) in enumerate(ATTENDED, 1):
            t.append(f"  {n}. {tag:6} {secs:4.0f} s: {what}")
            tot += secs + CUE_S + 10
        t.append(f"Attended total about {tot / 60:.1f} min (windows, cues and the host gaps).")
        t.append("M1: the CPU speed steps through all 16 turbo indices (3 s each, two")
        t.append("passes); keep moving steadily through it.  M3m: the drop-to-1-MHz trials.")
    t.append("=" * 72)
    return "\n".join(t)


def run_all(dg: Diag, hold: dict, a) -> dict:
    s: dict = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "backend": dg.tg.backend}
    s["M4"] = m4 = leg_m4(dg, hold)
    if not m4["sid_ok"]:
        print("STOP: the SID sockets are not Enabled/8580; nothing else runs")
        return s
    if a.only == "m7":
        print("ATTENDED PART STARTS", flush=True)
        s["M7"] = leg_m7(dg, a.m7_windows)
        _save(s, a)
        print("ATTENDED PART DONE: the user can let go of the mouse", flush=True)
        return s
    if not a.skip_unattended:
        s["M5"] = leg_m5(dg)
        _save(s, a)
        s["M3s"] = leg_m3s(dg)
        _save(s, a)
        open(os.path.join(EVID, f"DONE-{a.tag}-unattended"), "w").write(s["time"] + "\n")
    rates = {}
    if "M5" in s and "regs" in s["M5"]:
        rates = {int(i): d["rate"] for i, d in s["M5"]["regs"]["index"].items()}
    if not a.unattended_only:
        dg.set_turbo("regs")
        s["live_attended"] = dg.live()
        if not rates:
            ram = {i: dg.per_read(i, ["ram"], outer=64)["ram"] for i in range(16)}
            rates = {i: round(ram[0] / v, 2) if v > 0 else None for i, v in ram.items()}
        print("ATTENDED PART STARTS", flush=True)
        for ax in ("x", "y"):
            s[f"M1-{ax}"] = leg_m1(dg, ax, rates)
            _save(s, a)
        for ax in ("x", "y"):
            s[f"M3m-{ax}"] = leg_m3m(dg, ax)
            _save(s, a)
        print("ATTENDED PART DONE: the user can let go of the mouse", flush=True)
    return s


def _save(s: dict, a) -> None:
    with open(os.path.join(EVID, f"{a.tag}-summary.json"), "w") as fh:
        json.dump(s, fh, indent=1, default=str)


def session(a) -> int:
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(a.prg)
    print(m7_schedule(a.m7_windows) if a.only == "m7" else
          schedule_text(a.skip_unattended, not a.unattended_only), flush=True)
    t0, res, err, hold = time.time(), None, None, {}
    if a.backend == "vice":
        from c64_test_harness import UnifiedManager, ViceConfig
        from tests.harness_util import REU_VICE_ARGS
        cfg = ViceConfig(prg_path=prg, warp=True, sound=False, ntsc=True,
                         extra_args=list(REU_VICE_ARGS))
        with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
            tg = mgr.acquire()
            try:
                prepare_target(tg, prg)
                res = run_all(Diag(tg), {}, a)
            finally:
                mgr.release(tg)
    else:
        from c64_test_harness.backends.ultimate64_helpers import reboot
        from tests.bench.u64_session import u64_hold
        try:
            with u64_hold(os.path.join(EVID, f"u64-{a.tag}")) as hold:
                tg = hold["target"]
                tg.transport.set_speed(1)
                reboot(tg.transport.client)
                prepare_target(tg, prg)
                try:
                    res = run_all(Diag(tg, tg.transport.client), hold, a)
                finally:
                    tg.transport.set_speed(1)
        except BaseException as e:  # noqa: BLE001 -- logged, then re-raised
            err = e
        with open(os.path.join(EVID, "SESSION.log"), "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t0))} "
                     f"run_mturbo {a.tag} host={os.environ.get('U64_HOST', '?')} prg={prg} "
                     f"lock_wait_s={hold.get('lock_wait_s', '?')} "
                     f"restored={hold.get('restored')} reset_ok={hold.get('reset_ok')} "
                     f"wall_s={time.time() - t0:.0f}{' error=' + repr(err) if err else ''}\n")
        if err:
            raise err
        if not hold.get("restored"):
            raise RuntimeError("device config NOT restored identically (see config_after.json)")
    res["verdict"] = verdict(res)
    _save(res, a)
    print("VERDICT:")
    for line in res["verdict"]:
        print(f"  {line}")
    return 0


def _sigterm(signum, frame):
    raise KeyboardInterrupt("SIGTERM")      # runs u64_hold's restore path


def main(argv=None) -> int:
    global SCALE, CUE_S
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", default="build/runner.prg")
    ap.add_argument("--backend", default="vice", choices=("vice", "u64"))
    ap.add_argument("--unattended-only", action="store_true")
    ap.add_argument("--skip-unattended", action="store_true",
                    help="M4 + the attended legs only (M5's rates re-measured quickly)")
    ap.add_argument("--only", choices=("m7",),
                    help="m7: M4, then only the attended M7 (config vs register path)")
    ap.add_argument("--m7-windows", default=M7_DEFAULT,
                    help=f"M7 windows in order, from {''.join(M7_PLAN)} (default {M7_DEFAULT})")
    ap.add_argument("--fast", action="store_true", help="short windows and cues (VICE dry run)")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--from-log", help="run the statistics on a --mouse-diag log and exit")
    a = ap.parse_args(argv)
    if a.from_log:
        return from_log(a.from_log)
    if any(w not in M7_PLAN for w in a.m7_windows):
        ap.error(f"--m7-windows: letters from {''.join(M7_PLAN)}")
    if a.backend == "vice":
        a.fast = True
    if a.fast:
        SCALE, CUE_S = 0.1, 1.0
    a.tag = a.tag or ("vice-m7" if a.backend == "vice" and a.only == "m7" else
                      "m7" if a.only == "m7" else
                      "vice" if a.backend == "vice" else
                      "unattended" if a.unattended_only else "full")
    import signal
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _sigterm)
    os.makedirs(EVID, exist_ok=True)
    sys.stdout = Tee(os.path.join(EVID, f"{a.tag}.log"))
    t0 = time.monotonic()
    rc = 1
    try:
        rc = session(a)
    finally:
        print(f"run_mturbo: wall-clock {time.monotonic() - t0:.0f} s, rc {rc}")
        open(os.path.join(EVID, f"DONE-{a.tag}"), "w").write(f"rc {rc}\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
