#!/usr/bin/env python3
"""REC (REU controller) register-state probe: what do $DF02-$DF08 hold
after a transfer, with and without autoload?  Runs tests/bench/rec_probe.s
(assembled at $C000 = buf_case) on VICE or the U64E through the normal
runner + call_runner path, for fetch command $91 (no autoload) and $B1
(autoload).  The REC-shadow design in runtime/core.s depends on the answer
(docs/HARDWARE.md "REC shadow").

Usage: python3 -m tests.bench.rec_probe --prg build/ws-a/runner.prg
           [--backend vice|u64] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

REGS = ["C64LO", "C64HI", "REULO", "REUHI", "BANK", "LENLO", "LENHI",
        "IMASK", "ACTRL"]
STEPS = [
    "1 full program C64=$C100 REU=$012345 LEN=4, fetch",
    "2 write REULO=$50 only, fetch",
    "3 no writes, fetch",
    "4 write LENLO=2 REULO=$60, fetch",
    "5 write C64LO=$08, stash",
    "6 write C64LO=$0C, fetch",
]


def assemble(cmd: int, outdir: str) -> bytes:
    out = os.path.join(outdir, f"rec_probe_{cmd:02x}.bin")
    subprocess.run(["cl65", "-t", "none", "-C",
                    os.path.join(HERE, "rec_probe.cfg"),
                    f"--asm-define", f"CMDF=0x{cmd:02x}", "-o", out,
                    os.path.join(HERE, "rec_probe.s")], check=True)
    with open(out, "rb") as f:
        return f.read()


def probe(target, codes: dict) -> dict:
    """Run the assembled probes on a prepared target; print + return."""
    from c64_test_harness import read_bytes, write_bytes
    from tests.harness_util import call_runner
    result = {}
    for cmd, code in codes.items():
        write_bytes(target.transport, 0xC000, code)
        write_bytes(target.transport, 0xC200, bytes(0x60))
        call_runner(target, 0xC000, 30.0)
        regs = read_bytes(target.transport, 0xC200, 0x60)
        data = read_bytes(target.transport, 0xC100, 16)
        steps = []
        print(f"\n=== fetch command ${cmd:02X} "
              f"({'autoload' if cmd & 0x20 else 'no autoload'}) ===")
        for i, name in enumerate(STEPS):
            row = regs[16 * i + 2:16 * i + 11]
            d = dict(zip(REGS, row))
            steps.append({"step": name, "regs": d})
            c64 = d["C64HI"] << 8 | d["C64LO"]
            reu = d["BANK"] << 16 | d["REUHI"] << 8 | d["REULO"]
            ln = d["LENHI"] << 8 | d["LENLO"]
            print(f"  after {name:<48} C64=${c64:04X} "
                  f"REU=${reu:06X} LEN=${ln:04X} "
                  f"ACTRL=${d['ACTRL']:02X}")
        print(f"  data $C100-$C10F: {data.hex(' ')}")
        result[f"{cmd:02X}"] = {"steps": steps, "data": data.hex()}
    return result


def assemble_x(cmd: int, outdir: str) -> bytes:
    out = os.path.join(outdir, f"rec_probe_x_{cmd:02x}.bin")
    subprocess.run(["cl65", "-t", "none", "-C",
                    os.path.join(HERE, "rec_probe_x.cfg"),
                    "--asm-define", f"CMDF=0x{cmd:02x}", "-o", out,
                    os.path.join(HERE, "rec_probe_x.s")], check=True)
    obj = os.path.join(HERE, "rec_probe_x.o")
    if os.path.exists(obj):
        os.remove(obj)
    with open(out, "rb") as f:
        return f.read()


XSTEPS = ["0 fetch 4 @ $01FFFE (bank 1->2)",
          "1 same-bank fetch, BANK not rewritten",
          "2 stash 4 @ $01FFFE (bank 1->2)",
          "3 same-bank fetch, BANK not rewritten"]


def probe_x(target, codes: dict) -> dict:
    """Bank-carry-under-autoload + 512 KB wrap probe (rec_probe_x.s)."""
    from c64_test_harness import read_bytes, write_bytes
    from tests.harness_util import call_runner
    result = {}
    for cmd, code in codes.items():
        write_bytes(target.transport, 0xC000, code)
        write_bytes(target.transport, 0xC600, bytes(0x100))
        call_runner(target, 0xC000, 30.0)
        buf = read_bytes(target.transport, 0xC600, 0x100)
        auto = bool(cmd & 0x20)
        print(f"\n=== rec_probe_x, bank steps with ${cmd:02X} "
              f"({'autoload' if auto else 'no autoload'}) ===")
        steps = []
        for i, name in enumerate(XSTEPS):
            d = dict(zip(REGS, buf[0x40 + 16 * i + 2:0x40 + 16 * i + 11]))
            reu = d["BANK"] << 16 | d["REUHI"] << 8 | d["REULO"]
            steps.append({"step": name, "regs": d})
            print(f"  after {name:<40} REU=${reu:06X} "
                  f"BANK=${d['BANK']:02X} LEN=${d['LENHI'] << 8 | d['LENLO']:04X}")
        data = buf[0:0x10]
        wrap = buf[0x80:0x96]
        v = {
            "carry_fetch_ok": data[0:4] == bytes([0xA6, 0xA7, 0xA8, 0xA9]),
            "step1_hit_bank1": data[4:8] == bytes([0xB0, 0xB1, 0xB2, 0xB3]),
            "step3_hit_bank1": data[8:12] == bytes([0xB0, 0xB1, 0xB2, 0xB3]),
            "carry_stash_ok": data[12:16] == bytes([0xB4, 0xB5, 0xB6, 0xB7]),
            "bank_reg_after_step0": steps[0]["regs"]["BANK"],
            "bank_reg_after_step2": steps[2]["regs"]["BANK"],
        }
        w = {
            "07FFFE..080001_bytewise": wrap[0:6].hex(),
            "fetch4_07FFFE": wrap[8:12].hex(),
            "top_FFFFFE_bytewise": wrap[0x10:0x16].hex(),
        }
        # correct 24-bit carry would give: $07FFFE.. D0 D1, $080000.. D2 D3,
        # $000000.. C2 C3 (untouched); a 19-bit counter wrap gives
        # $080000.. C0 C1 (untouched), $000000.. D2 D3.
        w["wraps_512k"] = (wrap[2:4] == bytes([0xC0, 0xC1])
                           and wrap[4:6] == bytes([0xD2, 0xD3]))
        w["carries_512k"] = (wrap[2:4] == bytes([0xD2, 0xD3])
                             and wrap[4:6] == bytes([0xC2, 0xC3]))
        w["top_wraps_to_F80000"] = (wrap[0x12:0x14] == bytes([0xD6, 0xD7])
                                    and wrap[0x14:0x16] == bytes([0xCA, 0xCB]))
        print(f"  data $C600-$C60F: {data.hex(' ')}")
        print(f"  verdict: {v}")
        print(f"  512K: {w}")
        result[f"{cmd:02X}"] = {"steps": steps, "data": data.hex(),
                                "bank": v, "wrap": w}
    return result


def assemble_all(prg: str) -> dict:
    outdir = os.path.join(os.path.dirname(os.path.abspath(prg)), "rec_probe")
    os.makedirs(outdir, exist_ok=True)
    return {c: assemble(c, outdir) for c in (0x91, 0xB1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prg", required=True)
    ap.add_argument("--backend", default="vice", choices=["vice", "u64"])
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    from c64_test_harness import UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target

    prg = os.path.abspath(args.prg)
    codes = assemble_all(prg)

    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend=args.backend, vice_config=vice_config) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            result = probe(target, codes)
            outdir = os.path.join(os.path.dirname(prg), "rec_probe")
            result["x"] = probe_x(target, {c: assemble_x(c, outdir)
                                           for c in (0x91, 0xB1)})
        finally:
            mgr.release(target)
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"backend": args.backend, "result": result}, f,
                      indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
