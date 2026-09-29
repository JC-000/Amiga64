#!/usr/bin/env python3
"""Count REC bank-register switches per memory-API call (W6-D, S3 plan Q1).

The proposed custom-chip trap (docs/S3_PLAN.md, "Trap mechanism") costs
cycles ONLY on memory-API calls whose guest bank differs from the REC
shadow's bank (m_rbk), i.e. exactly on the calls that execute the
`sta m_rbk / sta REC_REUBANK` pair (6 warm MIO_PROG copies + mio_full).
This probe counts, in VICE and without instrumenting the PRG:

  * calls  -- exec hits on the six mem_read*/mem_write* entry points
  * bank   -- exec hits on every `85 87 8D 06 DF` site (sta m_rbk;
              sta REC_REUBANK), found by scanning the PRG bytes
  * instr  -- guest instructions retired (the runner's icount)

for real vbcc programs from tests/run_vbcc.py (its table, not a copy).
VICE monitor checkpoints (stop_when_hit = 0) do the counting, as in
tests/rec_budget.py.  Counts are exact; converting them to cycles uses the
static per-path costs in docs/S3_PLAN.md (estimate, not a measurement).

Usage: python3 tools/s3probe/bank_probe.py --prg build/w6-s3plan/rt/runner.prg
           [--only hello,alloc] [--json-out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

M_RBK = 0x87            # runtime/rc/zp.inc: $80-$87 window + REC shadow, m_rbk last
REC_REUBANK = 0xDF06
SITE = bytes([0x85, M_RBK, 0x8D, REC_REUBANK & 0xFF, REC_REUBANK >> 8])
API = ("mem_read8", "mem_read16", "mem_read32",
       "mem_write8", "mem_write16", "mem_write32")
CPU_EXEC = 0x04


def bank_sites(prg: str) -> list[int]:
    data = open(prg, "rb").read()
    load = data[0] | data[1] << 8
    return [load + m.start() - 2 for m in re.finditer(re.escape(SITE), data)]


class ExecCounter:
    """Count-only VICE exec checkpoints on single addresses."""

    def __init__(self, transport, addrs: dict[str, int]):
        from c64_test_harness.backends.vice_binary import CMD_CHECKPOINT_SET
        self.t = transport
        self.nums = {}
        for name, a in addrs.items():
            body = struct.pack("<HHBBBB", a, a, 0x00, 0x01, CPU_EXEC, 0x00)
            resp = transport._send_and_recv(CMD_CHECKPOINT_SET, body)
            self.nums[name] = struct.unpack_from("<I", resp.body, 0)[0]

    def read(self) -> dict[str, int]:
        hits = {cp["number"]: cp["hit_count"] for cp in self.t.checkpoint_list()}
        return {n: hits[k] for n, k in self.nums.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--only", default="hello,scroll,alloc,sysbase,exit")
    ap.add_argument("--budget", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "w6-s3plan", "vbcc"))
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)

    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests import run_vbcc
    from tests.harness_util import REU_VICE_ARGS, lookup_label
    from tests.run_program import Session
    from tests.run_vectors import prepare_target

    prg = os.path.abspath(a.prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    sites = bank_sites(prg)
    if len(sites) != 7:
        print(f"warning: expected 7 bank-store sites, found {len(sites)}")
    table = run_vbcc.programs()
    names = [n for n in a.only.split(",") if n]
    os.makedirs(a.out, exist_ok=True)
    blobs = {n: run_vbcc.compile_c(n, a.out, table[n].src, table[n].defines)
             for n in names}
    run_vbcc.POOL_WIRED = run_vbcc.pool_wiring(prg, labels)

    addrs = {f"site_{s:04x}": s for s in sites}
    addrs.update({n: lookup_label(labels, n) for n in API})
    results = {}
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            sess = Session(target=target, labels=labels, timeout=1800.0)
            cnt = ExecCounter(target.transport, addrs)
            for n in names:
                prog = table[n]
                if prog.needs_dos and run_vbcc.hle_dos_is_stub(sess):
                    continue
                # snapshot right before the guest runs: staging (per-byte
                # mem_write8) and hle_dos_init are not part of the program
                snap = {}
                orig = sess.run_program

                def run_program(*args, _o=orig, **kw):
                    snap.update(cnt.read())
                    return _o(*args, **kw)
                sess.run_program = run_program
                res, _m, why = run_vbcc.stage_and_run(
                    sess, n, blobs[n], prog.budget or a.budget, poison=False,
                    files=prog.files)
                sess.run_program = orig
                c1 = cnt.read()
                d = {k: c1[k] - snap[k] for k in c1}
                calls = sum(d[k] for k in API)
                bank = sum(v for k, v in d.items() if k.startswith("site_"))
                # HLE (6502-side library code) also calls the API directly;
                # those calls are counted too (they would pay the check).
                results[n] = {"instr": res.icount, "calls": calls,
                              "bank_switch": bank, "halt": why,
                              "per_api": {k: d[k] for k in API},
                              "per_site": {k: v for k, v in d.items()
                                           if k.startswith("site_")}}
                print(f"[{n}] instr={res.icount} calls={calls} "
                      f"bank_switch={bank} ({100.0 * bank / max(calls, 1):.1f}% of calls,"
                      f" {bank / max(res.icount, 1):.3f}/instr) halt={why}")
        finally:
            mgr.release(target)
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump({"sites": [hex(s) for s in sites], "programs": results}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
