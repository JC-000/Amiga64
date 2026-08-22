"""Generate hand-written mini "vector" batches for the stub core.

The expected values are computed with a Python model of the stub core's
emu_step semantics (see tests/stub/stub_core.s):
    D0 += 1; [A0] := D1.b; D2.b := [A1]; PC += 2; CCR ^= $04

Constraints imposed by the stub's 4 KB fake guest RAM (addr & $0FFF):
all referenced guest addresses stay below $1000 and every address that is
read (A1 and all expected-mem addresses) is covered by an init pair or the
step's own write.

Usage:  python3 -m tests.stub.make_mini_vectors [--out build/stub/vectors]
"""

from __future__ import annotations

import argparse
import os
import sys

from tests.case_format import Case, encode_batch
from tests.harness_util import BUILD_STUB_DIR

M32 = 0xFFFFFFFF


def stub_step(regs: list[int], pc: int, sr: int,
              init_mem: list[tuple[int, int]]) -> tuple[list[int], int, int, dict[int, int]]:
    """Python model of stub_core.s emu_step. Returns (regs', pc', sr', mem')."""
    mem = {a & 0xFFF: v for a, v in init_mem}
    r = list(regs)
    r[0] = (r[0] + 1) & M32
    mem[r[8] & 0xFFF] = r[1] & 0xFF        # [A0] := D1.b
    r[2] = (r[2] & 0xFFFFFF00) | mem[r[9] & 0xFFF]  # D2.b := [A1] (must be initialized)
    return r, (pc + 2) & M32, sr ^ 0x0004, mem


def make_case(regs: list[int], pc: int, sr: int,
              init_mem: list[tuple[int, int]],
              check_addrs: list[int]) -> Case:
    exp_regs, exp_pc, exp_sr, mem = stub_step(regs, pc, sr, init_mem)
    exp_mem = [(a, mem[a & 0xFFF]) for a in check_addrs]
    return Case(regs, pc, sr, init_mem, exp_mem, exp_regs, exp_pc, exp_sr)


def mini_cases() -> list[Case]:
    # Distinctive per-register fill to catch any dump/load offset mistakes.
    base = [0xD0D0D000 + i for i in range(8)] + [0xA0A0A000 + i for i in range(8)]

    # Case 0: plain step — write to A0, read an init byte via A1.
    r = list(base)
    r[0], r[1], r[2] = 0x11223344, 0x000000AB, 0xFFFFFF00
    r[8], r[9] = 0x000100, 0x000200        # A0, A1
    r[15] = 0x00F000                       # A7 = contract default stack_top
    c0 = make_case(r, 0x00001000, 0x0000, [(0x000200, 0x5A)],
                   [0x000100, 0x000200])

    # Case 1: D0 wraparound, PC carry across 16 bits, step's own write is
    # read back through A1 (A0 == A1), SR high byte preserved.
    r = list(base)
    r[0], r[1], r[2] = 0xFFFFFFFF, 0x00000077, 0x12345678
    r[8], r[9] = 0x000300, 0x000300
    c1 = make_case(r, 0x0000FFFE, 0x271F, [(0x000300, 0x11)],
                   [0x000300])

    # Case 2: several init and expected-mem pairs, CCR toggles Z off.
    r = list(base)
    r[0], r[1], r[2] = 0x00000000, 0x000000FE, 0x00000000
    r[8], r[9] = 0x000480, 0x000404
    init = [(0x000400, 0x01), (0x000401, 0x02), (0x000404, 0x9C),
            (0x0007FF, 0x55), (0x000000, 0xEE)]
    c2 = make_case(r, 0x00FF0010, 0x0004, init,
                   [0x000480, 0x000400, 0x000401, 0x000404, 0x0007FF, 0x000000])

    return [c0, c1, c2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(BUILD_STUB_DIR, "vectors"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "stub_mini.bin")
    with open(path, "wb") as f:
        f.write(encode_batch(mini_cases()))
    print(f"wrote {path} ({len(mini_cases())} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
