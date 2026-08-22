#!/usr/bin/env python3
"""Hardware timing benches for the Amiga64 M1 interpreter on the U64E.

Per CPU-speed step (default sweep 1..48 MHz):
  1. 6502 ALU loop         — real effective CPU clock (known cycle count)
  2. 6502 IO-store loop    — is REC register access ($DF0A) wait-stated?
  3. 6502 REU-DMA loop     — per-transfer time at LEN=4096 and LEN=1
                             -> DMA bytes/sec + per-transfer engine overhead
  4. 68k ALU/DBcc loop     — interpreter IPS + 6502-cycles/instr
  5. 68k memory loop       — same, REU-data-bound (MOVE.L (A0),D1)

All wall-clock numbers are two-point deltas so fixed REST/dispatch overhead
cancels. Run: U64_HOST=... python3 bench.py [--backend vice] [--speeds ...]
"""

import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, "/Users/someone/Documents/Amiga64")

from c64_test_harness import Labels, UnifiedManager, ViceConfig, read_bytes, write_bytes
from tests.harness_util import REU_VICE_ARGS, call_runner, lookup_label
from tests.run_program import Session, RESULT_LEN, RunResult, BUF_CASE
from tests.run_vectors import prepare_target
from host import fixturegen, hunk, image

PRG = "/Users/someone/Documents/Amiga64/build/h1/runner.prg"
LBL = "/Users/someone/Documents/Amiga64/build/h1/runner.lbl"

ALU_ADDR = 0xC000   # buf_case is free while no case is staged
IO_ADDR = 0xC040
DMA_ADDR = 0xC080
DMA_BUF = 0x4000    # runner code ends at $2B5D; $4000 page is free

# ---------------------------------------------------------------- 6502 code

def code_alu(oc: int) -> bytes:
    """sei; 3-level loop; rts.  cycles = oc*329_226 + const."""
    return bytes([
        0x78,             # sei
        0xA9, oc,         # lda #oc
        0x85, 0x02,       # sta $02
        0xA0, 0x00,       # o: ldy #0
        0xA2, 0x00,       # m: ldx #0
        0xE8,             # i: inx
        0xD0, 0xFD,       #    bne i
        0x88,             #    dey
        0xD0, 0xF8,       #    bne m
        0xC6, 0x02,       #    dec $02
        0xD0, 0xF2,       #    bne o
        0x60,             # rts
    ])

ALU_CYCLES_PER_OC = 329_226  # ldy2 + 256*(ldx2+256*5-1+dey2+bne3) + dec5+bne3


def code_io(oc: int) -> bytes:
    """Same shape, inner op = sta $DF0A (REC control reg, no execute).

    cycles = oc*(2 + 256*(2 + 256*9 - 1) + 8) at nominal speed; slower
    if IO access is wait-stated at turbo.
    inner: sta4 + dey2 + bne3 = 9/iter.
    """
    return bytes([
        0x78,                   # sei
        0xA5, 0x01, 0x48,       # lda $01 / pha
        0xA9, 0x35, 0x85, 0x01, # lda #$35 / sta $01  (IO in)
        0xA9, oc,               # lda #oc
        0x85, 0x02,             # sta $02
        0xA0, 0x00,             # o: ldy #0
        0xA2, 0x00,             # m: ldx #0
        0x8D, 0x0A, 0xDF,       # i: sta $DF0A  (A = leftover, harmless bits)
        0xE8,                   #    inx
        0xD0, 0xFA,             #    bne i
        0x88,                   #    dey
        0xD0, 0xF5,             #    bne m
        0xC6, 0x02,             #    dec $02
        0xD0, 0xEF,             #    bne o
        0x68, 0x85, 0x01,       # pla / sta $01
        0x60,                   # rts
    ])

# inner iter: sta4 + inx2 + bne3 = 9; mid: ldx2 + 256*9-1 + dey2+bne3 = 2310
IO_CYCLES_PER_OC = 2 + 256 * 2310 + 5 + 3    # = 591_370
IO_STORES_PER_OC = 256 * 256


def code_dma(oc: int, xc: int, length: int) -> bytes:
    """REU fetch (REU->C64) of *length* bytes, xc*oc times, autoload."""
    lo, hi = length & 0xFF, length >> 8
    return bytes([
        0x78,                   # sei
        0xA5, 0x01, 0x48,       # lda $01 / pha
        0xA9, 0x35, 0x85, 0x01, # lda #$35 / sta $01
        0xA9, 0x00, 0x8D, 0x02, 0xDF,           # c64 addr = DMA_BUF
        0xA9, DMA_BUF >> 8, 0x8D, 0x03, 0xDF,
        0xA9, 0x00, 0x8D, 0x04, 0xDF,           # reu addr 0
        0xA9, 0x00, 0x8D, 0x05, 0xDF,
        0xA9, 0x00, 0x8D, 0x06, 0xDF,
        0xA9, lo, 0x8D, 0x07, 0xDF,             # length
        0xA9, hi, 0x8D, 0x08, 0xDF,
        0xA9, 0x00, 0x8D, 0x0A, 0xDF,           # control: inc both
        0xA9, oc,               # lda #oc
        0x85, 0x02,             # sta $02
        0xA2, xc,               # o: ldx #xc
        0xA9, 0xB1,             # l: lda #$B1 (exec|autoload|imm|fetch)
        0x8D, 0x01, 0xDF,       #    sta $DF01   <- CPU stalled during DMA
        0xCA,                   #    dex
        0xD0, 0xF8,             #    bne l
        0xC6, 0x02,             #    dec $02
        0xD0, 0xF2,             #    bne o
        0x68, 0x85, 0x01,       # pla / sta $01
        0x60,                   # rts
    ])

DMA_LOOP_CYCLES = 11  # lda#2 + sta4 + dex2 + bne3, excluding DMA stall

# ----------------------------------------------------------------- 68k code

def hunk_alu68k(n: int) -> bytes:
    b = fixturegen.HunkBuilder()
    b.add_code([
        0x303C, n - 1,        # MOVE.W #n-1,D0
        0x5281,               # loop: ADDQ.L #1,D1
        0xD682,               #       ADD.L  D2,D3
        0x51C8, 0xFFFA,       #       DBRA   D0,loop
        0xAFFF,               # HALT
    ])
    return b.build()

def alu68k_icount(n: int) -> int:
    return 2 + 3 * n

def hunk_mem68k(n: int) -> bytes:
    b = fixturegen.HunkBuilder()
    b.add_code([
        0x303C, n - 1,              # MOVE.W #n-1,D0
        0x41F9, 0x0000, 0x8000,     # LEA $8000.L,A0
        0x2210,                     # loop: MOVE.L (A0),D1
        0x51C8, 0xFFFC,             #       DBRA D0,loop
        0xAFFF,                     # HALT
    ])
    return b.build()

def mem68k_icount(n: int) -> int:
    return 3 + 2 * n

# ------------------------------------------------------------------ driver

def timed_call(target, addr: int, timeout: float) -> float:
    t0 = time.monotonic()
    call_runner(target, addr, timeout)
    return time.monotonic() - t0


def run_prog(sess: Session, entry: int, budget: int) -> tuple[float, RunResult]:
    write_bytes(sess.target.transport, BUF_CASE,
                struct.pack("<III", entry, image.STACK_TOP, budget))
    addr = lookup_label(sess.labels, "test_run_program")
    dt = timed_call(sess.target, addr, sess.timeout)
    hdr = read_bytes(sess.target.transport, 0xC800, RESULT_LEN)
    assert hdr[0] == 0xFF, "test_run_program did not complete"
    return dt, RunResult.from_bytes(hdr)


def bench_68k(sess: Session, build, icount_fn, n1: int, n2: int) -> dict:
    res = {}
    for n in (n1, n2):
        blob = build(n)
        exe = hunk.parse(blob)
        chunks, manifest = image.build_image(exe)
        sess.stage(chunks)
        dt, rr = run_prog(sess, manifest["entry"], icount_fn(n) + 10)
        assert rr.halt and rr.reason == 0x00, f"bad halt {rr.halt}/{rr.reason}"
        assert rr.icount == icount_fn(n), f"icount {rr.icount} != {icount_fn(n)}"
        res[n] = (dt, rr.icount)
    (t1, i1), (t2, i2) = res[n1], res[n2]
    return {"ips": (i2 - i1) / (t2 - t1), "t1": t1, "t2": t2,
            "di": i2 - i1, "dt": t2 - t1}


def two_point(target, addr: int, codegen, c1: int, c2: int,
              timeout: float) -> dict:
    ts = []
    for c in (c1, c2):
        write_bytes(target.transport, addr, codegen(c))
        ts.append(timed_call(target, addr, timeout))
    return {"t1": ts[0], "t2": ts[1], "dt": ts[1] - ts[0], "dc": c2 - c1}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="u64", choices=["vice", "u64"])
    ap.add_argument("--speeds", default="1,2,4,8,12,16,20,24,32,40,48")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--only-68k", action="store_true",
                    help="skip 6502 IO/DMA benches (keep ALU clock calibration)")
    args = ap.parse_args()

    labels = Labels.from_file(LBL)
    vice_config = ViceConfig(prg_path=PRG, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    results = []
    with UnifiedManager(backend=args.backend, vice_config=vice_config,
                        lock_timeout=1800.0) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, PRG)
            if args.backend == "vice":
                sess = Session(target=target, labels=labels, timeout=120.0)
                r = bench_68k(sess, hunk_alu68k, alu68k_icount, 200, 2200)
                print(f"[vice] 68k ALU bench functional, ips(warp)={r['ips']:.0f}")
                r = bench_68k(sess, hunk_mem68k, mem68k_icount, 200, 2200)
                print(f"[vice] 68k MEM bench functional, ips(warp)={r['ips']:.0f}")
                return 0

            sess = Session(target=target, labels=labels, timeout=600.0)
            for mhz in [int(s) for s in args.speeds.split(",")]:
                row = {"mhz": mhz}
                print(f"\n===== CPU speed {mhz} MHz =====", flush=True)
                target.transport.set_speed(mhz)
                time.sleep(0.5)

                # 1. 6502 ALU calibration (~2s delta)
                oc2 = min(255, 2 + 6 * mhz)
                r = two_point(target, ALU_ADDR, code_alu, 2, oc2, 600.0)
                eff_mhz = r["dc"] * ALU_CYCLES_PER_OC / r["dt"] / 1e6
                row["eff_mhz"] = eff_mhz
                print(f"6502 ALU: eff {eff_mhz:.3f} MHz "
                      f"(dt={r['dt']:.3f}s over {r['dc']*ALU_CYCLES_PER_OC} cyc)",
                      flush=True)

                if args.only_68k:
                    n2 = min(32000, max(2200, 500 * mhz))
                    r = bench_68k(sess, hunk_alu68k, alu68k_icount, 200, n2)
                    row["alu68k_ips"] = r["ips"]
                    row["alu68k_cpi"] = eff_mhz * 1e6 / r["ips"]
                    print(f"68k ALU: {r['ips']:.0f} IPS, "
                          f"{row['alu68k_cpi']:.0f} c/instr "
                          f"(dt={r['dt']:.2f}s/{r['di']} instr)", flush=True)
                    r = bench_68k(sess, hunk_mem68k, mem68k_icount, 200, n2)
                    row["mem68k_ips"] = r["ips"]
                    row["mem68k_cpi"] = eff_mhz * 1e6 / r["ips"]
                    print(f"68k MEM: {r['ips']:.0f} IPS, "
                          f"{row['mem68k_cpi']:.0f} c/instr "
                          f"(dt={r['dt']:.2f}s/{r['di']} instr)", flush=True)
                    results.append(row)
                    continue

                # 2. 6502 IO-store rate ($DF0A)
                oc2 = min(255, 2 + 6 * mhz)
                r = two_point(target, IO_ADDR, code_io, 2, oc2, 600.0)
                dstores = r["dc"] * IO_STORES_PER_OC
                dcyc = r["dc"] * IO_CYCLES_PER_OC
                eff_io_mhz = dcyc / r["dt"] / 1e6
                ns_store = r["dt"] / dstores * 1e9
                row["eff_io_mhz"] = eff_io_mhz
                print(f"6502 IO : eff {eff_io_mhz:.3f} MHz "
                      f"({ns_store:.0f} ns per $DF0A store; "
                      f"{'throttled' if eff_io_mhz < 0.85*eff_mhz else 'full speed'})",
                      flush=True)

                # 3. DMA at LEN=4096 (probe then refine to ~2.5s delta)
                r = two_point(target, DMA_ADDR,
                              lambda oc: code_dma(oc, 100, 4096), 1, 3, 600.0)
                per = r["dt"] / (r["dc"] * 100)
                span = max(2, min(200, int(2.5 / (per * 100))))
                r = two_point(target, DMA_ADDR,
                              lambda oc: code_dma(oc, 100, 4096),
                              1, 1 + span, 600.0)
                per4k = r["dt"] / (r["dc"] * 100)
                stall4k = per4k - DMA_LOOP_CYCLES / (eff_mhz * 1e6)
                bps = 4096 / stall4k
                row["dma_per4k_us"] = per4k * 1e6
                row["dma_bps"] = bps
                print(f"6502 DMA4096: {per4k*1e6:.1f} us/transfer -> "
                      f"{bps/1e6:.2f} MB/s "
                      f"({bps/(eff_mhz*1e6):.2f} B/CPU-cycle)", flush=True)

                # 4. DMA at LEN=1 (per-transfer engine overhead)
                r = two_point(target, DMA_ADDR,
                              lambda oc: code_dma(oc, 200, 1), 1, 3, 600.0)
                per = r["dt"] / (r["dc"] * 200)
                span = max(2, min(250, int(1.5 / (per * 200))))
                r = two_point(target, DMA_ADDR,
                              lambda oc: code_dma(oc, 200, 1),
                              1, 1 + span, 600.0)
                per1 = r["dt"] / (r["dc"] * 200)
                row["dma_per1_us"] = per1 * 1e6
                print(f"6502 DMA1  : {per1*1e6:.2f} us/transfer "
                      f"(per-byte {((per4k-per1)/4095)*1e9:.1f} ns)", flush=True)

                # 5. 68k ALU
                n2 = min(32000, max(2200, 500 * mhz))
                r = bench_68k(sess, hunk_alu68k, alu68k_icount, 200, n2)
                row["alu68k_ips"] = r["ips"]
                row["alu68k_cpi"] = eff_mhz * 1e6 / r["ips"]
                print(f"68k ALU: {r['ips']:.0f} IPS, "
                      f"{row['alu68k_cpi']:.0f} c/instr "
                      f"(dt={r['dt']:.2f}s/{r['di']} instr)", flush=True)

                # 6. 68k MEM
                r = bench_68k(sess, hunk_mem68k, mem68k_icount, 200, n2)
                row["mem68k_ips"] = r["ips"]
                row["mem68k_cpi"] = eff_mhz * 1e6 / r["ips"]
                print(f"68k MEM: {r['ips']:.0f} IPS, "
                      f"{row['mem68k_cpi']:.0f} c/instr "
                      f"(dt={r['dt']:.2f}s/{r['di']} instr)", flush=True)

                results.append(row)

            target.transport.set_speed(1)
            print("\nspeed restored:", target.transport.get_speed(), "MHz")
        finally:
            try:
                target.transport.set_speed(1)
            except Exception as e:
                print(f"WARNING: speed restore failed: {e}")
            mgr.release(target)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
