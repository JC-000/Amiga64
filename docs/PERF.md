# PERF — Analytic performance projection (M1 interpreter)

> **Measured reality now exists: see `docs/HARDWARE.md`** (real U64E, fw 3.14d,
> raw data in `fixtures/bench/`). Headlines vs. this doc's estimates: 1 MHz
> cycles/instr came out ~2× worse than the cycle-comment model; the 48 MHz
> uniform-scaling assumption is false (bulk REU DMA caps at ~3.3 MB/s past
> 2 MHz, and REC **register I/O is throttled to ~1 µs/store** at turbo — the
> real v0 ceiling). Measured 48 MHz IPS: v0 ~35k/28k (ALU/mem), windowed core
> ~80k/57k (window gain 2.28×/2.03× at 48 MHz; windowed cycles/instr is flat
> across the sweep). Lever 2, fewer REC accesses per memory call, is now done
> as the **REC shadow** (wave 4). Measured on firmware 3.15, same session, vs
> the windowed core at "48 MHz": memory-bound loops **+16% to +43% IPS**, the
> ALU loop unchanged; at 1 MHz within ±2%. See (d) and `docs/HARDWARE.md`
> "REC shadow core".

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
- Data reads/writes: ~70 cycles of REC register setup + 1 cycle/byte + ZP
  copies per call in v0 and the windowed core. With the REC shadow (lever
  d.2), a warm in-page call costs 47 cycles of compare/setup with
  2 REC accesses instead of 9 (47 cycles + 7 for the 512 KB-straddle test
  on 16/32-bit calls). On hardware the access count, not the cycle count,
  is what matters (see (d) 2).

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
2. **Data memory-API REC trims.** The REC register shadow is **DONE** (wave
   4, WS-A). Measured on hardware, not estimated: every REC register access
   costs ~0.9–1.7 µs at turbo (see `docs/HARDWARE.md`, finding 2 and the
   3.15 re-measurement), and v0 and the windowed core paid 9 accesses per
   call. The memory API now keeps a C64-side shadow of the REC registers it
   owns and issues every data DMA with **autoload**. Silicon and VICE both
   reload exactly the last-written values; see `tests/bench/rec_probe.py`. A
   warm call therefore stores only the REU-address and length bytes that
   changed, plus the command. Accesses per call, counted in VICE by
   `tests/rec_budget.py`:

   | Case | Accesses |
   |---|---|
   | cold | 9 |
   | same address | 1 |
   | same page | 2 |
   | new page | 3 |
   | new bank | 4 |
   | size switch | 3 |
   | store half of an RMW | 1 |

   The v0 and windowed cores paid 9 in every case. Per loop iteration:
   MOVE.L (A0) goes from 9 to 1, MOVE.L (A0)+ from 9 to 2.015, ADDQ.L #1,(A0)
   from 18 to 2, MOVE.L (A0)+,(A1)+ from 18 to 6, and BSR/RTS from 18 to 2.

   Measured IPS at "48 MHz" (38.9 MHz effective on firmware 3.15), windowed
   base → shadow core:

   | Loop | Base IPS | Shadow IPS | Change |
   |---|---|---|---|
   | MEM | 54.0k | 71.6k | +32% |
   | STREAM | 52.8k | 61.5k | +16% |
   | RMW | 40.4k | 57.8k | +43% |
   | ALU | 76.1k | 75.2k | within the ±1.3% rep spread |

   At 16 MHz the gains are +13% (MEM), +11% (STREAM) and +24% (RMW). At
   1 MHz they are within ±2%, because the warm REC setup is 47 cycles vs
   v0's ~59 including jsr, and the accesses themselves are cheap at bus
   speed. The saved time per avoided access (0.8–1.1 µs at 48 MHz) matches
   the measured store cost, so the model is confirmed.

   Correctness: the shadow depends on REC state surviving between calls.
   That was the reason this lever was deferred before, and contract C1
   handles it:
   - everything else that touches the REC calls `mem_rec_dirty`: the
     fetch-window refill inline, `emu_reset`, `test_load_chunk`, and the U64
     mailbox dispatcher before every call;
   - the `REC_NOINVAL` / `REC_MUT_*` builds and the cold-path source mutants
     (`tests/rec_build.py` MUTANTS) prove, in VICE and on silicon, that each
     of those invalidations and every `mio_full` store is load-bearing
     (`tests/rec_teeth.py`, `tests/rec_matrix.py`);
   - the boot stub also clears the flag (ZP comes up with garbage). That
     clear **protects a direct memory-API call made before the first
     runner entry**, for example a host `jsr` into `mem_read8` right after
     boot on VICE. The runner entries invalidate on their own, but a
     direct call has nothing else in front of it. At power-on the REC
     length register is `$FFFF`, so a stale "valid" shadow issues a 64 KB
     DMA over C64 RAM and VICE hangs. The `boot` teeth leg
     (`tests/rec_teeth.py`, VICE only) catches the `bootrok` mutant this
     way. On the U64 the mailbox dispatcher dirties the shadow before
     every call, so the leg does not apply there.

   512 KB straddles (fix round, 2026-09-24). The REC's address counter
   carries only through bits 0–18, on VICE and on the U64E alike. A 16- or
   32-bit access straddling `$x7FFFF` → `$x80000` therefore needs splitting.
   This was a pre-existing bug, not something the shadow introduced. The
   API now tests for the straddle, which costs 7 cycles on the common path,
   and moves straddling accesses byte by byte (`mio_split`). The odd-address
   straddle paths are reachable only from HLE and direct API calls, because
   odd guest word/long accesses are address errors. They are pinned by
   `tests/rec_apisweep.py`: 128 direct-API cases, with a split-path check
   through `m_rok` and a check that X/Y are preserved.

   **Measured** at "48 MHz", same session, fixed core against the unfixed
   shadow core: MEM −2.0%, RMW −1.6%, STREAM −0.2%. The 7-cycle test alone
   predicts only about 0.6–1% on these loops (1 or 2 calls per 2
   instructions, against about 550–700 cycles per instruction). The rest is
   **not** attributed to the test; code layout (page-crossing branches and
   the moved entry points) is a likely contributor, but it was not isolated.
   Against the windowed base, the fixed core is still +25% MEM, +15% STREAM
   and +43% RMW. See `docs/HARDWARE.md`, "Fix round".

   Not done, and still open for the ALU/EA side of this lever:
   - trimming the ZP copies around `ea_load` / `ea_store` / `m_val`;
   - a dedicated fused RMW entry. The shadow already reduces the store half
     of an RMW to the command store (1 access); a fused entry would save
     only the 41-cycle compare path.
   - DMA straight into `m_val` for byte ops. Rejected: a second C64 target
     address costs a C64-low store (~1 µs at turbo) on every byte↔word/long
     switch, to save a 6-cycle copy.

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
