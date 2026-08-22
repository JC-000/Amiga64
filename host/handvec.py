"""Hand-written test vectors for ops with zero upstream coverage.

The SingleStepTests suite has no files for the CCR-immediate forms
(ORI/ANDI/EORI #imm,CCR — $003C/$023C/$0A3C), MOVE from SR ($40C0|ea),
MOVE to CCR ($44C0|ea), LINK ($4E50+n) or UNLK ($4E58+n).  This module
hand-authors cases for them from M68000 PRM / Musashi semantics and
compiles them into one binary batch in the contract case format
(encoders reused from host/vectors.py).

Run:

    make runtime BUILD=build/w3v
    python3 -m host.handvec                       # -> build/vectors-hand/HAND.bin
    python3 -m tests.run_vectors --prg build/w3v/runner.prg \
        --vectors build/vectors-hand

Semantics encoded here (the authorities are the PRM and Musashi; where
they could be misread, Musashi's order wins, matching the golden suite):

* ORI/ANDI/EORI #imm,CCR: word immediate, only its low byte applies, the
  result is masked to the 5 architectural CCR bits (X included — these
  ops DO set/clear X).  PC += 4.
* MOVE SR,<ea>: writes a WORD (user-legal on the 68000).  The stored SR
  high byte is 0 after vector normalization, so the written word is
  $00xx with xx = CCR.  Flags unaffected.  Dn destination keeps the
  upper word of Dn.
* MOVE <ea>,CCR: word source, only bits 4-0 land in the CCR.
* LINK An,#d16: SP -= 4; [SP] = An; An = SP; SP += sext(d16).  For
  An = A7 the pushed value is the DECREMENTED A7 (Musashi:
  ``REG_A[7] -= 4; write_32(REG_A[7], REG_A[7])``).
* UNLK An: SP = An; An = [SP]; SP += 4.  For An = A7 the popped value
  wins (Musashi: ``REG_A[7] = read_32(REG_A[7])`` — no +4 survives).
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import Any, Sequence

from host.vectors import pack_case

BATCH_STEM = "HAND"
PC = 0x001000

# Distinctive, fixed register file; cases override only what they use.
BASE_REGS = {
    "d0": 0x00000000, "d1": 0x11111111, "d2": 0x22222222, "d3": 0x33333333,
    "d4": 0x44444444, "d5": 0x55555555, "d6": 0x66666666, "d7": 0x77777777,
    "a0": 0x00002000, "a1": 0x00002100, "a2": 0x00002200, "a3": 0x00002300,
    "a4": 0x00002400, "a5": 0x00002500, "a6": 0x00003000, "a7": 0x0000F000,
}
REG_ORDER = tuple(f"d{i}" for i in range(8)) + tuple(f"a{i}" for i in range(8))


def be32(addr: int, val: int) -> dict[int, int]:
    """Four big-endian memory bytes for a 32-bit value at ``addr``."""
    return {addr + i: (val >> (24 - 8 * i)) & 0xFF for i in range(4)}


def case(name: str, words: Sequence[int], *, sr: int,
         regs: dict[str, int] | None = None,
         exp: dict[str, int] | None = None,
         exp_sr: int | None = None, exp_pc: int | None = None,
         mem: dict[int, int] | None = None,
         wmem: dict[int, int] | None = None) -> dict[str, Any]:
    """Build one normalized case dict (host/vectors.py shape).

    ``words`` are materialized big-endian at PC as init memory (opcode +
    extension words).  ``mem`` adds further init bytes; ``wmem`` are the
    bytes the instruction writes (overlaid onto init memory for the
    expected view, so every untouched init byte is asserted unchanged —
    same convention as the golden pipeline).  ``exp`` gives final
    register values for registers that change; ``exp_pc`` defaults to
    PC past all ``words``; ``exp_sr`` defaults to unchanged.
    """
    init = dict(BASE_REGS)
    init.update(regs or {})
    final = dict(init)
    final.update(exp or {})

    init_mem: dict[int, int] = {}
    for i, w in enumerate(words):
        init_mem[PC + 2 * i] = (w >> 8) & 0xFF
        init_mem[PC + 2 * i + 1] = w & 0xFF
    init_mem.update(mem or {})
    exp_mem = dict(init_mem)
    exp_mem.update(wmem or {})

    return {
        "name": name,
        "regs": [init[r] & 0xFFFFFFFF for r in REG_ORDER],
        "pc": PC,
        "sr": sr,
        "init_mem": sorted(init_mem.items()),
        "exp_regs": [final[r] & 0xFFFFFFFF for r in REG_ORDER],
        "exp_pc": PC + 2 * len(words) if exp_pc is None else exp_pc,
        "exp_sr": sr if exp_sr is None else exp_sr,
        "exp_mem": sorted(exp_mem.items()),
    }


# --- ORI/ANDI/EORI #imm,CCR -------------------------------------------------
# CCR' = (CCR op imm.b) & $1F.  X participates like any other bit.

def ccr_immediate_cases() -> list[dict[str, Any]]:
    return [
        # ORI #imm,CCR ($003C)
        case("ORI-ccr-zero", [0x003C, 0x0000], sr=0x00, exp_sr=0x00),
        case("ORI-ccr-setall", [0x003C, 0x001F], sr=0x00, exp_sr=0x1F),
        case("ORI-ccr-merge", [0x003C, 0x000A], sr=0x15, exp_sr=0x1F),
        # imm bits 7-5 must not stick: (0 | $E5) & $1F = $05
        case("ORI-ccr-maskhi", [0x003C, 0x00E5], sr=0x00, exp_sr=0x05),
        case("ORI-ccr-keepx", [0x003C, 0x0004], sr=0x10, exp_sr=0x14),
        case("ORI-ccr-noop-full", [0x003C, 0x0000], sr=0x1F, exp_sr=0x1F),
        # immediate high byte is ignored
        case("ORI-ccr-hibyte", [0x003C, 0xA505], sr=0x0A, exp_sr=0x0F),

        # ANDI #imm,CCR ($023C)
        case("ANDI-ccr-clearall", [0x023C, 0x0000], sr=0x1F, exp_sr=0x00),
        case("ANDI-ccr-keepall", [0x023C, 0x001F], sr=0x1F, exp_sr=0x1F),
        case("ANDI-ccr-xonly", [0x023C, 0x0010], sr=0x1F, exp_sr=0x10),
        case("ANDI-ccr-dropx", [0x023C, 0x000F], sr=0x15, exp_sr=0x05),
        case("ANDI-ccr-ff", [0x023C, 0x00FF], sr=0x0A, exp_sr=0x0A),
        case("ANDI-ccr-zero-ccr", [0x023C, 0x001F], sr=0x00, exp_sr=0x00),
        case("ANDI-ccr-hibyte", [0x023C, 0xA50E], sr=0x1F, exp_sr=0x0E),

        # EORI #imm,CCR ($0A3C)
        case("EORI-ccr-setall", [0x0A3C, 0x001F], sr=0x00, exp_sr=0x1F),
        case("EORI-ccr-clearall", [0x0A3C, 0x001F], sr=0x1F, exp_sr=0x00),
        case("EORI-ccr-x-off", [0x0A3C, 0x0010], sr=0x15, exp_sr=0x05),
        case("EORI-ccr-x-on", [0x0A3C, 0x0010], sr=0x05, exp_sr=0x15),
        # ($0C ^ $FF) & $1F = $13
        case("EORI-ccr-ff", [0x0A3C, 0x00FF], sr=0x0C, exp_sr=0x13),
        case("EORI-ccr-zero", [0x0A3C, 0x0000], sr=0x0A, exp_sr=0x0A),
        case("EORI-ccr-hibyte", [0x0A3C, 0xFF09], sr=0x12, exp_sr=0x1B),
    ]


# --- MOVE SR,<ea> ($40C0|ea) ------------------------------------------------
# Word write of $00:CCR; flags unaffected.  Data-alterable destinations.

def move_from_sr_cases() -> list[dict[str, Any]]:
    stale = {0: 0xAA, 1: 0xBB}  # preset destination bytes so writes show

    def dst(addr: int) -> dict[int, int]:
        return {addr + k: v for k, v in stale.items()}

    return [
        # Dn: low word replaced, upper word kept
        case("MOVEfSR-d1", [0x40C1], sr=0x1F, exp={"d1": 0x1111001F}),
        case("MOVEfSR-d0-clr", [0x40C0], sr=0x00,
             regs={"d0": 0xFFFFFFFF}, exp={"d0": 0xFFFF0000}),
        case("MOVEfSR-ind", [0x40D0], sr=0x15, mem=dst(0x2000),
             wmem={0x2000: 0x00, 0x2001: 0x15}),
        case("MOVEfSR-postinc", [0x40D9], sr=0x0A, mem=dst(0x2100),
             wmem={0x2100: 0x00, 0x2101: 0x0A}, exp={"a1": 0x00002102}),
        case("MOVEfSR-predec", [0x40E2], sr=0x11, mem=dst(0x21FE),
             wmem={0x21FE: 0x00, 0x21FF: 0x11}, exp={"a2": 0x000021FE}),
        case("MOVEfSR-d16an", [0x40EB, 0x0010], sr=0x04, mem=dst(0x2310),
             wmem={0x2310: 0x00, 0x2311: 0x04}),
        case("MOVEfSR-absw", [0x40F8, 0x2400], sr=0x1B, mem=dst(0x2400),
             wmem={0x2400: 0x00, 0x2401: 0x1B}),
    ]


# --- MOVE <ea>,CCR ($44C0|ea) -----------------------------------------------
# Word source; CCR = source & $1F; other bits of the source ignored.

def move_to_ccr_cases() -> list[dict[str, Any]]:
    return [
        case("MOVEtCCR-d2-ffff", [0x44C2], sr=0x00,
             regs={"d2": 0x2222FFFF}, exp_sr=0x1F),
        # D3 low word $3333 & $1F = $13
        case("MOVEtCCR-d3", [0x44C3], sr=0x0C, exp_sr=0x13),
        case("MOVEtCCR-imm", [0x44FC, 0x0015], sr=0x00, exp_sr=0x15),
        # every bit above bit 4 set, none may land
        case("MOVEtCCR-imm-mask", [0x44FC, 0xFFE0], sr=0x1F, exp_sr=0x00),
        # (A0): word $A5F1 & $1F = $11
        case("MOVEtCCR-ind", [0x44D0], sr=0x00,
             mem={0x2000: 0xA5, 0x2001: 0xF1}, exp_sr=0x11),
        case("MOVEtCCR-postinc", [0x44D9], sr=0x1F,
             mem={0x2100: 0x00, 0x2101: 0x04}, exp_sr=0x04,
             exp={"a1": 0x00002102}),
        # -16(A3) = $22F0
        case("MOVEtCCR-d16an-neg", [0x44EB, 0xFFF0], sr=0x01,
             mem={0x22F0: 0x00, 0x22F1: 0x1E}, exp_sr=0x1E),
    ]


# --- LINK An,#d16 ($4E50+n) -------------------------------------------------
# SP -= 4; [SP] = An; An = SP; SP += sext(d16).  Base A7 = $F000, so every
# push lands at $EFFC (preset to $CC so the write is observable).

def link_cases() -> list[dict[str, Any]]:
    frame = {a: 0xCC for a in range(0xEFFC, 0xF000)}
    return [
        case("LINK-a0-neg16", [0x4E50, 0xFFF0], sr=0x05, mem=frame,
             wmem=be32(0xEFFC, 0x00002000),
             exp={"a0": 0x0000EFFC, "a7": 0x0000EFEC}),
        case("LINK-a6-zero", [0x4E56, 0x0000], sr=0x00, mem=frame,
             wmem=be32(0xEFFC, 0x00003000),
             exp={"a6": 0x0000EFFC, "a7": 0x0000EFFC}),
        case("LINK-a1-pos8", [0x4E51, 0x0008], sr=0x1F, mem=frame,
             wmem=be32(0xEFFC, 0x00002100),
             exp={"a1": 0x0000EFFC, "a7": 0x0000F004}),
        # d16 = $8000 (-32768): $EFFC + $FFFF8000 = $00006FFC (32-bit wrap)
        case("LINK-a2-min-disp", [0x4E52, 0x8000], sr=0x10, mem=frame,
             wmem=be32(0xEFFC, 0x00002200),
             exp={"a2": 0x0000EFFC, "a7": 0x00006FFC}),
        # LINK A7: the pushed long is the DECREMENTED A7 ($EFFC), not $F000
        case("LINK-a7-neg8", [0x4E57, 0xFFF8], sr=0x00, mem=frame,
             wmem=be32(0xEFFC, 0x0000EFFC),
             exp={"a7": 0x0000EFF4}),
        # full 32-bit An value goes to the stack
        case("LINK-a5-full32", [0x4E55, 0xFFFC], sr=0x0A, mem=frame,
             regs={"a5": 0xDEADBEEF}, wmem=be32(0xEFFC, 0xDEADBEEF),
             exp={"a5": 0x0000EFFC, "a7": 0x0000EFF8}),
        # d16 = $7FFF (max positive): $EFFC + $7FFF = $00016FFB
        case("LINK-a4-max-disp", [0x4E54, 0x7FFF], sr=0x15, mem=frame,
             wmem=be32(0xEFFC, 0x00002400),
             exp={"a4": 0x0000EFFC, "a7": 0x00016FFB}),
    ]


# --- UNLK An ($4E58+n) ------------------------------------------------------
# SP = An; An = [SP]; SP += 4.  Memory is only read (asserted unchanged).

def unlk_cases() -> list[dict[str, Any]]:
    return [
        case("UNLK-a0", [0x4E58], sr=0x05, regs={"a0": 0x0000EF00},
             mem=be32(0xEF00, 0x00001234),
             exp={"a0": 0x00001234, "a7": 0x0000EF04}),
        case("UNLK-a6-full32", [0x4E5E], sr=0x00, regs={"a6": 0x0000E000},
             mem=be32(0xE000, 0xDEADBEEF),
             exp={"a6": 0xDEADBEEF, "a7": 0x0000E004}),
        # classic frame teardown: saved-FP chain
        case("UNLK-a3-frame", [0x4E5B], sr=0x1F, regs={"a3": 0x0000EFF8},
             mem=be32(0xEFF8, 0x0000EFFC),
             exp={"a3": 0x0000EFFC, "a7": 0x0000EFFC}),
        # UNLK A7: A7 = [A7]; the popped value wins, no +4 on top of it
        case("UNLK-a7", [0x4E5F], sr=0x0A, regs={"a7": 0x0000EF80},
             mem=be32(0xEF80, 0x00001248),
             exp={"a7": 0x00001248}),
        case("UNLK-a7-plus4-trap", [0x4E5F], sr=0x10,
             regs={"a7": 0x0000EF40}, mem=be32(0xEF40, 0x00004000),
             exp={"a7": 0x00004000}),
        case("UNLK-a1", [0x4E59], sr=0x00,
             mem=be32(0x2100, 0x01020304),
             exp={"a1": 0x01020304, "a7": 0x00002104}),
    ]


def all_cases() -> list[dict[str, Any]]:
    return (ccr_immediate_cases() + move_from_sr_cases()
            + move_to_ccr_cases() + link_cases() + unlk_cases())


def build_batch() -> bytes:
    cases = all_cases()
    return struct.pack("<H", len(cases)) + b"".join(
        pack_case(c) for c in cases)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m host.handvec",
        description="Compile hand-written vectors for uncovered ops.")
    ap.add_argument("--out", type=Path, default=Path("build/vectors-hand"),
                    help="output directory (default: build/vectors-hand)")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    blob = build_batch()
    path = args.out / f"{BATCH_STEM}.bin"
    path.write_bytes(blob)
    print(f"wrote {len(all_cases())} case(s) to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
