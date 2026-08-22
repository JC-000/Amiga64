# PERF — Analytic performance projection (M1 interpreter)

Engineering estimate from the cycle comments in `runtime/core.s` and
`runtime/ops/*.s`. Nothing here is measured on hardware. **VICE warp is not a
48 MHz model** — warp only removes real-time pacing from a 1 MHz machine model
and validates correctness, never speed. See Caveats.

> **Revision note:** numbers below reflect the **instruction-stream window**
> (lever d.1, now implemented in `runtime/core.s`). The pre-window figures
> (fetch ~150, overhead ~190, weighted mean ~500) are historical; they are
> quoted inline as "(was …)" and survive in this file's git history. A
> `FETCH_DIRECT` build flag restores the old fetch path for A/B runs.

## Cost model (6502 cycles)

Instruction fetch goes through a 256-byte page-aligned C64-RAM window of the
guest code page, refilled by ONE 256-byte REU DMA on a miss:

- `fetch_word` **hit**: 57 cycles in-routine (+6 jsr), ~51 when inlined in
  the dispatch loop (the per-opcode fetch is inlined). (was ~150/word)
- `fetch_word` **miss** (PC left the cached page, or window invalidated):
  ~380 cycles (~60 REC setup + ~258 DMA + serve). Straight-line code misses
  once per 128 words (~+3 cycles/instr amortized); in-page loops never miss;
  a branch/call/return that leaves the page pays one refill.
- Fixed per-instruction overhead (core): inlined fetch hit ~51 + dispatch
  select 22 + loop re-check 16 ≈ **~90 cycles before any handler runs**
  (was ~190). Every extension word costs another `fetch_word` (~63)
  unless folded into `ea_calc`/`ea_load` (immediate operands resolve
  through the memory API, ~95–115/call, unchanged).
- Coherence tax: `mem_write8/16/32` test the write against the cached page
  and invalidate on overlap (self-modifying code, stack pushes near code):
  +9 cycles per byte write, +16 per word/long write on the non-overlap path.
- Data reads/writes are otherwise unchanged: REU DMA setup dominates,
  ~70 cycles of REC register setup + 1 cycle/byte + ZP copies per call.

## (a) Representative instruction classes — estimated total 6502 cycles/instr

| Class | Example | Est. cycles | Source |
|---|---|---|---|
| reg-reg ALU | ADD.L D1,D2; CMP; AND | ~230 (was ~330) | g2 handler ~140 + 90 |
| MOVE reg→reg / MOVEQ | MOVE.L D0,D1 | ~200–230 (was ~300–330) | g1 decode ~70 + copy ~60 + 90 |
| reg←mem ALU (.L) | ADD.L (A0),D1 | ~500–600 (was ~600–700) | EA ~150 + read ~110 + alu + 90 |
| mem RMW (.L) | ADD.L D1,(A0); NOT | ~820 (was ~900) | −100 fetch, +16 write check |
| MOVE mem→mem (.L) | MOVE.L (A0),(A1) | ~660–720 (was ~750–800) | 2×EA + read + write + 90 |
| Branch not taken | Bcc.s | ~150 (was ~250) | g5 ~60 + 90 |
| Branch taken | Bcc.s / DBcc | ~210 (+~63 if .w ext) (was ~310 +150) | g5 ~120 + 90; +refill if it leaves the page |
| JSR / RTS | JSR (A0) | ~420–470 (was ~500–550) | push/pop_long ~160 + EA + 90 |
| Shift, immediate count ≤8 | LSL.W #4,Dn | ~500–1000 (was ~600–1100) | g4; 38–58/bit (+19 ASL) |
| Shift, register count | LSL.L D0,Dn (count 63) | up to ~4200 | g4 worst ROXL.L ~3.9k + ~350 |
| MULU/MULS | MULU D0,D1 | ~1100–1400 (was ~1200–1500) | g6 ~1000–1300 + 90 |
| DIVU/DIVS | DIVU D0,D1 | ~1200–1550 (was ~1300–1650) | g6 ~1100–1450 + 90 |
| MOVEM.L, n regs | MOVEM.L d0-a6,-(SP) | ~160 + ~260–280/reg (16 regs ~4500) | g6; +16/reg write check on stores |

## (b) Weighted "typical integer code" mix

Assumed mix (classic 68k compiler output; no FP, light MUL/DIV):

| Class | Weight | Cycles | Contribution |
|---|---|---|---|
| MOVE (reg/imm/simple-EA mix) | 35% | ~350 | 123 |
| ALU reg-reg | 20% | ~230 | 46 |
| ALU with memory operand | 15% | ~640 | 96 |
| Branch/DBcc (60% taken) | 20% | ~190 | 38 |
| JSR/RTS/LEA/PEA | 5% | ~420 | 21 |
| MUL/DIV/shift/MOVEM | 5% | ~1400 | 70 |

Sum ≈ 394; add ~10 for amortized window refills (1 per 128 straight-line
words, plus one per out-of-page transfer — in-page loops pay none).

**Weighted mean ≈ ~405 ≈ ~400 6502 cycles per 68k instruction** (was ~500;
the window is worth ~1.25× on this mix). Sensitivity: a MOVE/branch-heavy
mix lands ~310; a memory/EA-heavy mix ~550. Pathology: a hot loop spanning
a 256-byte page boundary refills twice per iteration (window pages are
aligned, not PC-centered) — rare, bounded, and detectable by profiling.

## (c) Projected 68k throughput

| Host | Clock | Projected 68k IPS | vs real 7.09 MHz 68000 (~0.7–1.4 MIPS) |
|---|---|---|---|
| Stock C64 + cartridge | ~1 MHz | ~2,500 IPS (2.5 KIPS) (was ~2,000) | ~280–560× slower |
| Ultimate 64 Elite turbo | 48 MHz | ~120,000 IPS (~0.12 MIPS) (was ~96,000) | ~6–12× slower |

Equivalently: at 48 MHz the interpreter behaves like a ~0.6–1.2 MHz 68000 —
enough for Workbench-era utility code to be usable, not for A500-class games.
At 1 MHz it is a correctness/demo vehicle only. (A real 68000 averages ~8–12
of its own cycles/instruction; we spend ~400 host cycles/instruction.)

## (d) Top optimization levers, ranked by expected payoff

1. ~~**Instruction-stream windowing for `fetch_word`**~~ **DONE** (~1.25×
   measured against the cost model; the original ~1.5× estimate assumed a
   ~27-cycle hit, the real hit is ~51–63 because of the mandatory window
   bounds/validity test, the contract's t_tmp mirror, and PC page-cross
   handling). `FETCH_DIRECT` builds keep the old path for A/B comparison.
2. **ALU/EA path + data memory-API trims** (~1.1–1.2×) — now the top open
   lever. ~95–115 cycles/call is mostly REC setup and ZP copies; a leaner
   setup path, fused `ea_load`+`ea_store` REC programming for RMW, and
   skipping dead upper-byte copies for B/W sizes save ~40–70 cycles on the
   ~40% of instructions that touch guest data memory. Note: skipping
   "unchanged" REC registers between calls was evaluated during the window
   work and deliberately NOT done — it makes every memory call depend on
   REC state surviving outside the interpreter (loader staging, Ultimate
   firmware on real hardware), which is a correctness risk until the
   hardware backend pins down REC ownership.
3. **Shift-count clamps for LSL/LSR/ASL** (tail latency; ~1–2% on the typical
   mix, large in shift-heavy code). Counts ≥ operand width zero-fill (C/X from
   the last shifted bit) — equivalence proven by G4's vectors — clamping the
   worst register-count cases from ~3.9k+350 to a few hundred cycles. ROL/ROR/
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
  unvalidated; if DMA does not scale, memory-bound classes get slower — but
  the window has already removed most per-instruction DMA traffic (one
  256-byte burst per page instead of one 2-byte DMA per word), so the
  interpreter is now much less sensitive to this assumption than before.
- The refill amortization (+~10 on the mix) assumes in-page loops and one
  out-of-page transfer every ~30–50 instructions; call-heavy code with
  call sites and callees in different pages pays more (~380/refill).
- Real-hardware validation on the U64E is pending; treat every figure here as
  provisional until a cycle-counted hardware run exists.
