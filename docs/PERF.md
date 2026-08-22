# PERF — Analytic performance projection (M1 interpreter)

Engineering estimate from the cycle comments in `runtime/core.s` and
`runtime/ops/*.s`. Nothing here is measured on hardware. **VICE warp is not a
48 MHz model** — warp only removes real-time pacing from a 1 MHz machine model
and validates correctness, never speed. See Caveats.

## Cost model (6502 cycles)

Fixed per-instruction overhead (core): `fetch_word` ~150 (incl. REU DMA) +
dispatch select 22 + loop ≈ **~190 cycles before any handler runs**. Every
extension word costs another `fetch_word` (~150) unless folded into
`ea_calc`/`ea_load` (immediate operands resolve through the memory API,
~95–115/call). REU DMA setup dominates all memory traffic: ~70 cycles of REC
register setup + 1 cycle/byte + ZP copies per call.

## (a) Representative instruction classes — estimated total 6502 cycles/instr

| Class | Example | Est. cycles | Source |
|---|---|---|---|
| reg-reg ALU | ADD.L D1,D2; CMP; AND | ~330 | g2 "Dn form ~330 total/instr" |
| MOVE reg→reg / MOVEQ | MOVE.L D0,D1 | ~300–330 | g1 decode ~70 + copy ~60 + 190 |
| reg←mem ALU (.L) | ADD.L (A0),D1 | ~600–700 | EA ~150 + read ~110 + alu + 190 |
| mem RMW (.L) | ADD.L D1,(A0); NOT | ~900 | g2 "memory .l form ~900" |
| MOVE mem→mem (.L) | MOVE.L (A0),(A1) | ~750–800 | 2×EA + read + write + 190 |
| Branch not taken | Bcc.s | ~250 | g5 ~60 + 190 |
| Branch taken | Bcc.s / DBcc | ~310 (+~150 if .w ext) | g5 ~120 + 190 |
| JSR / RTS | JSR (A0) | ~500–550 | push/pop_long ~160 + EA + 190 |
| Shift, immediate count ≤8 | LSL.W #4,Dn | ~600–1100 | g4; 38–58/bit (+19 ASL) |
| Shift, register count | LSL.L D0,Dn (count 63) | up to ~4300 | g4 worst ROXL.L ~3.9k + ~450 |
| MULU/MULS | MULU D0,D1 | ~1200–1500 | g6 ~1000–1300 + 190 |
| DIVU/DIVS | DIVU D0,D1 | ~1300–1650 | g6 ~1100–1450 + 190 |
| MOVEM.L, n regs | MOVEM.L d0-a6,-(SP) | ~350 + ~260/reg (16 regs ~4300) | g6 |

## (b) Weighted "typical integer code" mix

Assumed mix (classic 68k compiler output; no FP, light MUL/DIV):

| Class | Weight | Cycles | Contribution |
|---|---|---|---|
| MOVE (reg/imm/simple-EA mix) | 35% | ~450 | 158 |
| ALU reg-reg | 20% | ~330 | 66 |
| ALU with memory operand | 15% | ~750 | 113 |
| Branch/DBcc (60% taken) | 20% | ~290 | 58 |
| JSR/RTS/LEA/PEA | 5% | ~500 | 25 |
| MUL/DIV/shift/MOVEM | 5% | ~1500 | 75 |

**Weighted mean ≈ 495 ≈ ~500 6502 cycles per 68k instruction.** Sensitivity:
a MOVE/branch-heavy mix lands ~400; a memory/EA-heavy mix ~650.

## (c) Projected 68k throughput

| Host | Clock | Projected 68k IPS | vs real 7.09 MHz 68000 (~0.7–1.4 MIPS) |
|---|---|---|---|
| Stock C64 + cartridge | ~1 MHz | ~2,000 IPS (2 KIPS) | ~350–700× slower |
| Ultimate 64 Elite turbo | 48 MHz | ~96,000 IPS (~0.1 MIPS) | ~7–15× slower |

Equivalently: at 48 MHz the interpreter behaves like a ~0.5–1 MHz 68000 —
enough for Workbench-era utility code to be usable, not for A500-class games.
At 1 MHz it is a correctness/demo vehicle only. (A real 68000 averages ~8–12
of its own cycles/instruction; we spend ~500 host cycles/instruction.)

## (d) Top optimization levers, ranked by expected payoff

1. **Instruction-stream windowing for `fetch_word`** (~1.5× overall). Cache a
   small C64-RAM window of the instruction stream, refilled per page and on
   branches; `fetch_word` drops from ~150 to ~25–30. At ~1.4 word fetches per
   average instruction that removes ~170 cycles from the ~500 mix. Memory-API
   entry points are contract-stable, so this is a core-internal change.
2. **ALU/EA path + data memory-API trims** (~1.1–1.2×). ~95–115 cycles/call is
   mostly REC setup and ZP copies; a leaner setup path, fused
   `ea_load`+`ea_store` REC programming for RMW, and skipping dead upper-byte
   copies for B/W sizes save ~40–70 cycles on the ~40% of instructions that
   touch guest data memory.
3. **Shift-count clamps for LSL/LSR/ASL** (tail latency; ~1–2% on the typical
   mix, large in shift-heavy code). Counts ≥ operand width zero-fill (C/X from
   the last shifted bit) — equivalence proven by G4's vectors — clamping the
   worst register-count cases from ~3.9k+450 to a few hundred cycles. ROL/ROR/
   ROX* still need modulo iteration.

Dispatch itself (22 cycles) is already negligible; do not micro-optimize it.

## (e) Caveats

- Every number above comes from **hand-written cycle comments**, not
  measurement. Expect ±20–30% per class; the mix weights are assumptions.
- **VICE warp ≠ 48 MHz.** Warp validates correctness at maximum host speed on
  a 1 MHz machine model; it models neither the U64E turbo clock nor REU DMA
  timing at that clock. No performance claim may cite a warp run.
- The 48 MHz projection assumes REU DMA and RAM access scale uniformly with
  the turbo clock on the U64E (REC setup ~70 cycles, 1 cycle/byte). That is
  unvalidated; if DMA does not scale, memory-bound classes get slower and the
  windowing lever (d.1) becomes even more dominant.
- Real-hardware validation on the U64E is pending; treat every figure here as
  provisional until a cycle-counted hardware run exists.
