#!/usr/bin/env python3
"""REC-access budget test (WS-A, docs/WAVE4_BRIEF.md C5).

Counts REU-controller register accesses (loads + stores to $DF00-$DF0A)
made by the interpreter, using VICE binary-monitor watchpoints (checkpoint
cpu_operation load|store, stop_when_hit=0; the monitor's per-checkpoint
hit counter does the counting, so the measurement is independent of the
code under test -- nothing in the PRG is instrumented).

Two measurement families:

  API  -- per memory-API call, host-driven: m_addr/m_val are poked into ZP
          and mem_read*/mem_write* are jsr()ed directly.  Scenarios cover
          the cold call (right after mem_rec_dirty), a repeat at the same
          address, a new address in the same page, a new page, a new bank,
          a size switch and read-modify-write (read then write at the same
          address -- the fused-RMW case).
  LOOP -- per iteration of small guest loops run through test_run_program
          (two-point delta over two iteration counts, so the one-off
          window refill / cold program cancels): memory-bound MOVE.L (A0),
          streaming MOVE.L (A0)+, RMW ADDQ.L #1,(A0), copy MOVE.L
          (A0)+,(A1)+, BSR/RTS stack traffic, and a register-only ALU loop.

Each scenario has a budget (max accesses); the budgets are the WS-A
commitment (see BUDGETS below for the per-line justification).  Exit 0 =
every scenario within budget.  --json-out writes the raw counts.

Usage: python3 -m tests.rec_budget --prg build/ws-a/runner.prg
           [--json-out FILE] [--no-budget]
VICE only (watchpoints are a VICE-monitor feature).
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys

from host import fixturegen, hunk, image

REC_FIRST, REC_LAST = 0xDF00, 0xDF0A
ZP_M_ADDR, ZP_M_VAL = 0x58, 0x5C
CPU_LOAD, CPU_STORE = 0x01, 0x02

# Budgets (max REC register accesses, loads+stores).  Justification:
# the data path keeps a C64-side shadow of the REC registers it owns and
# issues every transfer with AUTOLOAD, so after each DMA the REC holds
# exactly the last-programmed values (verified on VICE and the U64E by
# tests/bench/rec_probe.py).  The C64-address, length-high and address-
# control registers never change between data calls, so a warm call
# writes only the REU-address/length bytes that differ from the previous
# call, plus the command store.
#   cold  -> full program: 8 register stores + command = 9 (== v0 cost)
#   same  -> command only = 1
#   near  -> REU addr low byte + command = 2
#   page  -> low + high + command = 3
#   bank  -> low + high + bank + command = 4
#   size  -> length + low + command = 3  (read8 then read32 in-page)
#   rmw   -> the write after a same-size read of the same address = 1
API_BUDGET = {"cold": 9, "same": 1, "near": 2, "page": 3, "bank": 4,
              "size": 3, "rmw_write": 1}
# Per loop ITERATION.  stream pays one extra REUHI store per 256 bytes
# (every 64th MOVE.L), copy alternates two pages with different low bytes
# (lo+hi+cmd per call = 6), plus one REUHI store per page crossing.
LOOP_BUDGET = {"mem": 1.0, "stream": 2.05, "rmw": 2.0, "copy": 6.05,
               "stack": 2.0, "alu": 0.0}

DATA_A = 0x008000       # guest scratch (stack region, below the program)
DATA_B = 0x00A010       # low byte differs from DATA_A: copy pays lo+hi


# ------------------------------------------------------------ counting

class RecCounter:
    """Two non-stopping VICE watchpoints over the REC register window."""

    def __init__(self, transport):
        from c64_test_harness.backends.vice_binary import CMD_CHECKPOINT_SET
        self.t = transport
        self.nums = {}
        for name, op in (("load", CPU_LOAD), ("store", CPU_STORE)):
            body = struct.pack("<HHBBBB", REC_FIRST, REC_LAST,
                               0x00,   # stop_when_hit = 0: count only
                               0x01,   # enabled
                               op, 0x00)
            resp = transport._send_and_recv(CMD_CHECKPOINT_SET, body)
            self.nums[name] = struct.unpack_from("<I", resp.body, 0)[0]

    def read(self) -> dict:
        hits = {cp["number"]: cp["hit_count"]
                for cp in self.t.checkpoint_list()}
        return {name: hits[n] for name, n in self.nums.items()}

    def total(self) -> int:
        r = self.read()
        return r["load"] + r["store"]


# ------------------------------------------------------------ API family

def api_measure(target, labels, counter) -> dict:
    from c64_test_harness import write_bytes
    from tests.harness_util import call_runner, lookup_label
    t = target.transport
    lab = {n: lookup_label(labels, n) for n in
           ("mem_read8", "mem_read16", "mem_read32", "mem_write8",
            "mem_write16", "mem_write32", "mem_rec_dirty")}

    def call(name, addr=None, val=None):
        if addr is not None:
            write_bytes(t, ZP_M_ADDR, struct.pack("<I", addr))
        if val is not None:
            write_bytes(t, ZP_M_VAL, struct.pack("<I", val))
        c0 = counter.total()
        call_runner(target, lab[name], 10.0)
        return counter.total() - c0

    out = {}
    for op in ("read", "write"):
        for size in (8, 16, 32):
            fn = f"mem_{op}{size}"
            v = 0x11223344 if op == "write" else None
            row = {}
            call("mem_rec_dirty")
            row["cold"] = call(fn, DATA_A + 0x40, v)
            row["same"] = call(fn, DATA_A + 0x40, v)
            row["near"] = call(fn, DATA_A + 0x48, v)
            row["page"] = call(fn, DATA_A + 0x150, v)
            row["bank"] = call(fn, DATA_A + 0x10258, v)
            out[fn] = row
    # size switch: read8 then read32 in the same page
    call("mem_rec_dirty")
    call("mem_read8", DATA_A + 0x10)
    out["size_switch"] = {"size": call("mem_read32", DATA_A + 0x20)}
    # read-modify-write at the same address, per size
    for size in (8, 16, 32):
        call("mem_rec_dirty")
        call(f"mem_read{size}", DATA_A + 0x80)
        call(f"mem_read{size}", DATA_A + 0x90)     # warm, different addr
        call(f"mem_read{size}", DATA_A + 0x84)
        out[f"rmw{size}"] = {"rmw_write": call(f"mem_write{size}",
                                               DATA_A + 0x84, 0x5A5A5A5A)}
    return out


# ----------------------------------------------------------- LOOP family

def _prog(body_words, n, setup_words=()):
    """MOVE.W #n-1,D0; setup; loop: body; DBRA D0,loop; HALT"""
    b = fixturegen.HunkBuilder()
    loop_len = 2 * len(body_words) + 4
    words = [0x303C, n - 1] + list(setup_words) + list(body_words) + \
        [0x51C8, (-(loop_len - 2)) & 0xFFFF, 0xAFFF]
    b.add_code(words)
    return b.build()


def _lea(areg, addr):
    return [0x41F9 | (areg << 9), addr >> 16, addr & 0xFFFF]


LOOPS = {
    # name: (setup words, body words, instructions per iteration, extra)
    "mem":    (_lea(0, DATA_A), [0x2210], 2),                 # MOVE.L (A0),D1
    "stream": (_lea(0, DATA_A), [0x2218], 2),                 # MOVE.L (A0)+,D1
    "rmw":    (_lea(0, DATA_A), [0x5290], 2),                 # ADDQ.L #1,(A0)
    "copy":   (_lea(0, DATA_A) + _lea(1, DATA_B), [0x22D8], 2),  # MOVE.L (A0)+,(A1)+
    "alu":    ((), [0x5281, 0xD682], 3),                      # ADDQ.L/ADD.L
}


def _stack_prog(n):
    """MOVE.W #n-1,D0; loop: BSR.S sub; DBRA D0,loop; HALT; sub: RTS"""
    b = fixturegen.HunkBuilder()
    b.add_code([0x303C, n - 1,
                0x6108,              # loop: BSR.S +8 -> sub
                0x51C8, 0xFFFC,      #       DBRA D0,loop
                0xAFFF,              #       HALT
                0x4E71,              #       (pad) NOP
                0x4E75])             # sub:  RTS
    return b.build()


def loop_measure(target, labels, counter, n1=40, n2=240) -> dict:
    from c64_test_harness import read_bytes, write_bytes
    from tests.run_program import BUF_CASE, RESULT_LEN, RunResult, Session
    from tests.harness_util import call_runner, lookup_label
    sess = Session(target=target, labels=labels, timeout=120.0)
    run_addr = lookup_label(labels, "test_run_program")

    def run(blob):
        exe = hunk.parse(blob)
        chunks, manifest = image.build_image(exe)
        sess.stage(chunks)
        write_bytes(target.transport, BUF_CASE,
                    struct.pack("<III", manifest["entry"], image.STACK_TOP,
                                0))
        c0 = counter.total()
        call_runner(target, run_addr, 120.0)
        hits = counter.total() - c0
        rr = RunResult.from_bytes(read_bytes(target.transport, 0xC800,
                                             RESULT_LEN))
        assert rr.halt and rr.reason == 0, f"loop did not HALT cleanly: {rr}"
        return hits, rr.icount

    out = {}
    progs = {name: (lambda n, s=s, bw=bw: _prog(bw, n, s))
             for name, (s, bw, _ipi) in LOOPS.items()}
    progs["stack"] = _stack_prog
    ipi = {name: v[2] for name, v in LOOPS.items()}
    ipi["stack"] = 3
    for name, build in progs.items():
        h1, i1 = run(build(n1))
        h2, i2 = run(build(n2))
        per_iter = (h2 - h1) / (n2 - n1)
        out[name] = {"per_iter": per_iter,
                     "per_instr": per_iter / ipi[name],
                     "hits": [h1, h2], "icount": [i1, i2], "n": [n1, n2]}
    return out


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prg", required=True)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--no-budget", action="store_true",
                    help="measure only (baseline runs)")
    args = ap.parse_args(argv)

    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target

    prg = os.path.abspath(args.prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=vice_config) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            counter = RecCounter(target.transport)
            api = api_measure(target, labels, counter)
            loops = loop_measure(target, labels, counter)
        finally:
            mgr.release(target)

    fails = []
    print(f"REC accesses ($DF00-$DF0A loads+stores), prg={args.prg}")
    print("\nper memory-API call:")
    for fn, row in api.items():
        cells = []
        for k, v in row.items():
            b = API_BUDGET[k]
            bad = v > b
            if bad:
                fails.append(f"{fn}.{k}: {v} > {b}")
            cells.append(f"{k}={v}{'!' if bad else ''}(<={b})")
        print(f"  {fn:<12} " + "  ".join(cells))
    print("\nper loop iteration (two-point delta):")
    for name, r in loops.items():
        b = LOOP_BUDGET[name]
        bad = r["per_iter"] > b + 1e-9
        if bad:
            fails.append(f"loop {name}: {r['per_iter']:.3f} > {b}")
        print(f"  {name:<7} {r['per_iter']:7.3f}/iter "
              f"{r['per_instr']:7.3f}/instr  budget {b}"
              f"{'  OVER' if bad else ''}")
    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)),
                    exist_ok=True)
        with open(args.json_out, "w") as f:
            json.dump({"prg": args.prg, "api": api, "loops": loops,
                       "api_budget": API_BUDGET, "loop_budget": LOOP_BUDGET,
                       "fails": fails}, f, indent=1)
    if args.no_budget:
        return 0
    if fails:
        print(f"\nBUDGET FAIL ({len(fails)}):")
        for f in fails:
            print(f"  {f}")
        return 1
    print("\nall scenarios within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
