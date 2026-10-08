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

## (f) Supervisor mode & exceptions (W6-S): cycle accounting

**Criterion (C4, supervisor-amended 2026-09-25).** With `st_exc` clear:
(i) the hot-path instruction streams are unchanged; (ii) REC access counts
are identical; (iii) no loop gets slower, measured on the product path
(`emu_run`) and on the program runner's step path; (iv) every residual delta
is explained branch by branch.

**Method.** `tests/perf_cycles.py` runs `tests/rec_budget.py`'s six loops
(mem/stream/rmw/copy/alu/stack) with the screen off (no badline DMA) and
times each call with the CIA2 timers (exact phi2 count in VICE); a
40/240-iteration two-point delta leaves cycles per guest instruction.
`--modes run` = `emu_reset` + `emu_run` from a stub (the PRODUCT path, one
call per program); `--modes step` = `test_run_program` (emu_step per
instruction; what run_dos/run_vbcc/run_con execute). `--base` measures a
second PRG, `--max-delta D` fails above D. The PRG must be a gate build
(`emu_brnc_enforced` = 1, below).

**Numbers (cycles per guest instruction, flag clear; base = main b14f204).**

| loop | base run | **W6-S run** | base step | W6-S step | W6-S step + run_program.diff |
|---|---|---|---|---|---|
| mem MOVE.L (A0),D1 | 526.555 | **-1.000** | 564.572 | +0.000 | -1.000 |
| stream MOVE.L (A0)+,D1 | 571.505 | **-1.000** | 609.523 | +0.000 | -1.000 |
| rmw ADDQ.L #1,(A0) | 593.000 | **-1.000** | 631.018 | +0.000 | -1.000 |
| copy MOVE.L (A0)+,(A1)+ | 694.960 | **-1.000** | 732.977 | +0.000 | -1.000 |
| alu ADDQ.L/ADD.L | 548.333 | **+0.000** | 586.357 | +1.000 | +0.000 |
| stack BSR/RTS | 383.000 | **-0.667** | 421.023 | +0.333 | -0.667 |

One-off per call: `emu_run` path 1623 -> 1666 cycles (the `emu_reset`
latch + the halt test), step path 2968 -> 2941. Flag SET (exception-mode
loop, `--exc`): +55 to +56 cycles per instruction. REC accesses
(`tests/rec_budget.py --json-out`): API and loop counts identical to base.
Evidence: `build/evidence/w6-super/fix1/green/perf_cycles*.txt`.

**(i) Instruction streams.** `host/tests/test_super_hotpath.py` fingerprints
the normalized instruction lines of fetch/dispatch + `dispatch_next`,
`fetch_word`, the memory API, EA resolution and the ALU/CCR primitives
against a snapshot of the base source (a `BRNC op, t` guard reads as the
plain branch, `HOTALIGN` fill is dropped), pins the two audited step-loop
entries (emu_step `bne` -> patchable `jmp`, same 3 cycles; emu_run + one
`jmp`), and self-tests that one inserted NOP changes each region's
fingerprint. The assembled PRGs agree at opcode level too (37 + 105 + 302 +
337 + 497 instructions).

**(iii)/(iv) Page crossings: guarded, not pinned.** A taken 6502 branch whose
target is in another page costs +1 cycle; on the hot path that is per guest
instruction, and ANY code growth reshuffles which branches cross (the base
itself paid for several). Instead of pinning a layout, every conditional
branch on the core.s hot path is written `BRNC op, target`: it assembles to
exactly `op target` plus a LINK-time `.assert` that the taken target is in
the branch's page, so a crossing FAILS the production build. Crossings are
resolved with `HOTALIGN n` (never-executed `$EA` `.align` fill; absolute
because core.s's CODE starts page-aligned, asserted at `core_code0`) before
mem_read16, mem_write8, mem_write32, ea_store, alu_cmp, alu_neg and
alu_eor (+211 bytes). Test-only variant builds (FETCH_DIRECT, REC_*,
WINDOW_NOINVAL, `BRNC_WARN_ONLY`) only warn; the exported
`emu_brnc_enforced` (1 = enforcing) is checked by `tests/run_super_vectors.py`
and `tests/perf_cycles.py`, which refuse a non-gate PRG.
`host/tests/test_super_brnc.py` is the teeth: fill inserted so that
mem_read16's first guarded branch ends a page must fail the normal link and
link with `BRNC_WARN_ONLY`. Guarded sites (74 guarded branch sites in runtime/core.s):

- dispatch_go: bne dispatch_done, bne dg_miss, bne dg_miss, beq dg_wrap
- dg_wrap: bne dispatch_sel
- dispatch_next: bne dispatch_done
- nx_br: beq dispatch_go
- fetch_word: bcc @nc, bne @nc, bne miss, bne miss
- serve: beq wrap
- wrap: bne wdone
- macro WINCHK_B: bne done, bne done
- macro WINCHK_W: beq chkhi, bcc done
- cross: bne done, bne done, beq kill
- chkhi: bne done
- macro MIO_PROG: bne warm
- warm: beq lenok
- lenok: beq look
- look: beq hiok
- hiok: beq done
- macro STRADDLE: bcc no, bne no, beq slow
- loop: bne loop
- ea_m7: beq ea_absw, beq ea_absl, beq ea_pcd16, beq ea_pcidx, beq ea_imm
- ea_absw: bpl @pos
- ea_imm: beq @b, beq @l, bne @b1, bne @b1
- ea_pc_add: bcc @d, bne @d
- ea_amount: bne @ok, bne @ok
- ea_add_sext16: bpl @pos
- ea_add_brief: bpl @p, bne @long, bpl @wp
- ea_load: bne @reg, beq @m8, beq @m32, beq @rd, bne @rd
- ea_store: bne @reg, beq @m8, beq @m32, beq @rd, bne @rd
- macro ALUADDSUB: beq w, beq l
- alu_neg: beq @w, beq @l
- macro ALULOGIC: beq w, bcc b
- alu_not: beq @w, bcc @b
- alu_ext: beq @l, bpl @p1, bpl @p2
- flags_logic: bcc @nn
- fl_setx: beq @nx
- p_to_nvc: beq @nv
- fl_insert_z: bne @nz
- fl_insert_z_sticky: bne @nz

Residue, attributed with `build/evidence/w6-super/perf/attrib.py` (count-only
VICE exec checkpoints on every branch whose crossing differs between the
two links; the sum of taken x crossing change equals the measured delta
exactly): the step-mode +1.000 (alu) / +0.333 (stack) is the program
runner's per-instruction `bne @step` in `tests/runner/run_program.s`
crossing a page (+1/instr), offset by core's `mem_read32` STRADDLE branch and
g2's `fam_5`+$7D no longer crossing (-1 each), plus g2 `fam_add`+$12C now
crossing (+1 per ADD; frozen file). The proposed runner guard
(`build/evidence/w6-super/perf/runner_guard/run_program.diff`: RUN_NOCROSS
on `bne @step`/`bne @loop` + `.align 64` before the loop) removes the
dominant term: measured column above. The product path has no runner loop
and is at or below base on every loop.

An earlier "layout control" (base padded only at the ends of core.s and
g5_flow.s) is withdrawn: W6-S inserts code INSIDE core.s and g5_flow.s, so
that control did not reproduce W6-S's core-internal layout (review F4).

## (g) S3.1 I/O trap (W7-T): cycle accounting and 48 MHz measurements

**Static cost** (`runtime/core.s`, lines tagged `; W7-T`; contract
`docs/CONTRACTS.md` "I/O trap"). Same-bank call: **+0**, the instruction
stream up to MIO_PROG's `beq done` is the base one. Bank change: **+8**
(`cmp #$DF / beq / cmp #$BF / beq`, both not taken). Cold call
(`mio_full`): +23 for RAM (`lda m_addr+2` + 8 + `lda m_rlen` + the
`jmp hiok` compare, 9), and 0 REC accesses when the call traps. STRADDLE:
+11 only on its slow branch ($xxFFFD–$xxFFFF: `lda zp` + 4 × 2). `mio_split`: +8 per byte.
REC accesses are unchanged everywhere. core.s CODE stays $D34 bytes (the
growth fits the HOTALIGN fills), and `chip.s` links last, so no other
module moves. BSS moves by whole pages ($300) only.
`host/tests/test_chip_hotpath.py` strips the tagged lines and gets back the
base memory-API fingerprint (246, 55bef079f4cb52cf).

**VICE [MEASURED VICE]** (`build/evidence/w7-trap/perf/`, base = wave7/integration 2d61eaa):

| Check | Result |
|---|---|
| `tests/perf_cycles.py --max-delta 0`, run + step, six loops | delta **+0.000** on every loop; one-off per call identical (1666 / 2942) |
| `tests/rec_budget.py` | API and loop counts identical (JSON equal) |
| `tools/s3probe/bank_probe.py` (hello, scroll, alloc, sysbase, exit) | instr / calls / bank switches identical (e.g. alloc 921,683 / 428,457 / 131,140); 7 bank-store sites |
| bank-alternating loop `MOVE.L (A0),D1 / MOVE.L (A1),D2`, banks $00/$02 (`tests/run_chip.py --perf`) | +16.000 cycles/iter at 2.000 switches/iter = **+8.000 per switch**; same-bank control +0.000 |
| one code-window refill (JMP abs.L + DBRA between two pages vs in-page control) | 336.9 cycles (1 MHz model; PERF.md's estimate ~380) |
| cold call (a MOVE.L (A0),D1 as the first data call after each refill, `run_chip --perf` `cold_cross`) | **+23.000 per cold call**; in-page control +0.000 (fix round 1; also the reviewer's `coldperf.py`) |

**Per instruction on real programs [MEASURED VICE counts × static cost].**
`bank_probe` separates cold calls (the `mio_full` site) from warm bank
switches (the six MIO_PROG sites). Hook cost = (23 × cold + 8 × warm) /
instructions:

| vbcc program | instr | cold | warm switches | hook cyc/instr | S3_PLAN §1.2 estimate |
|---|---:|---:|---:|---:|---:|
| hello | 2,841 | 230 | 398 | **2.98** | 1.8 |
| scroll | 28,452 | 2,103 | 2,280 | **2.34** | 1.2 |
| alloc | 921,683 | 48,346 | 82,794 | **1.93** | 1.1 |
| sysbase | 11,259 | 905 | 1,277 | **2.76** | 1.6 |
| exit | 1,101 | 95 | 294 | **4.12** | 2.8 |

That is **1.9–4.1 cycles/instruction**, not the plan's 1.1–2.8. The plan
charged +8 to cold calls too, but the cold test lives in `mio_full` and
costs +23. Against the measured 518–674 cycles/instruction at 48 MHz this
is 0.3–0.8%. `perf_cycles` cannot see it, because its loops never refill and the
two-point delta cancels the one cold call per run.

**U64E at 48 MHz [MEASURED HW]** (2026-09-29, `tests/run_chip.py --hw u64`,
`build/evidence/w7-trap/hw/`). One DeviceLock hold (0 s wait), fw 3.15 git
c47155b7, FPGA 125, core 1.50. Config found at Turbo Manual / 48, REU 16 MB.
The full config was snapshotted and restored (re-read IDENTICAL), and
nothing was written to flash. ABBA order base, trap, trap, base; wall-clock
two-point deltas (200 vs 24,000 iterations). Calibrated effective clock
41.6–48.3 MHz across the four loads (the last load read high; the cycle
conversions below use each core's mean).

| Loop | base IPS | trap IPS | change |
|---|---|---|---|
| ALU ADDQ/ADD | 75,120 | 75,624 | +0.7% |
| MEM MOVE.L (A0) | 68,910 | 68,550 | −0.5% |
| STREAM MOVE.L (A0)+ | 62,395 | 62,327 | −0.1% |
| RMW ADDQ.L #1,(A0) | 56,616 | 57,643 | +1.8% |
| two reads, same bank | 58,486 | 57,752 | −1.3% |
| two reads, alternating banks | 59,379 | 58,613 | −1.3% |
| `MOVE.W $DFF004,D1` (base: REU; trap: chip_r16) | 65,678 | 67,421 | +2.7% |

Rep-to-rep spread is ±1.5% (e.g. alternating 57.8k–59.4k). Every change is
inside it. The expected +8 cycles × 2 switches (~0.4 µs of 51 µs/iter, 0.7%)
is below the resolution of this wall-clock method. The exact figure is the
VICE +8.000 above. A trapped custom-register read is at least as fast as a
same-bank REU read.

**Code-window refill at 48 MHz [MEASURED HW]:** the loop that jumps between
two pages every iteration costs 199.3 µs/iter, against 19.4 µs for the
in-page control. That is **one refill = 89.9 µs (base) / 90.0 µs (trap)**,
≈ 3,800–4,000 cycles at the measured clock. It confirms the ~90 µs that
`docs/S3_PLAN.md` §3.5 assumed for the AROS boot estimate (~17 s of refills
over 193,692 page changes). The trap does not change it: `fetch_word` is
unchecked.

## (h) S3.2 machine loop (W8-L): event path and beam reads

**Static cost** (`runtime/ops/mach.s`; contract `docs/CONTRACTS.md`
"Machine loop (S3.2)"). The interpreter's per-instruction cost is
**+0**: `core.s` is byte-identical (the PRG linked with the base tree's
`core.o` equals the new one, `build/evidence/w8-loop/green/core_identical.txt`),
`mach.s` links after `chip.s`, and the only core bytes that moved are
absolute operands (BSS +$400, page-aligned; the chip entries +10; RODATA
+$36D, where neither `ea_*` table crosses a page in either build).
`tests.perf_cycles --base <wave8/integration> --max-delta 0` passes. An event
costs [EST, static] one IRQ (~40 cycles: entry, `lda CIA1_ICR`, two `ora`,
`rti`), the return from `emu_run` at the next instruction boundary, and a
service pass (counter read + compare + aliasing check + re-arm, ~220
cycles; ~85 more per missed frame on a catch-up). With frames longer than
65535 φ2 (s ≥ 3) the one-shot wakes the loop every 65536 φ2 on the way:
< 0.5% of the time at 1 MHz. Guest frames/s = φ2 / 14188 / 2^s: 69.4 / 2^s
on PAL, **72.1 / 2^s on the bench U64E** (NTSC; its CIAs count φ2 unscaled
at 48 MHz [MEASURED HW, W8-H]) [MEASURED VICE: 70.00 / 4.333 PAL and
72.41 / 4.561 NTSC at s = 0 / 4, `tests/run_mach.py` legs fps / fps_ntsc].

**Beam reads [MEASURED VICE]** (`python3 -m tests.run_mach --perf`, PAL,
screen off; cycles of one `mem_read16` API call including its jsr/rts,
CIA1 timer B; `build/evidence/w8-loop/perf/beam_read.json`):

| read | s = 0 | s = 4 | s = 8 | s = 16 |
|---|---|---|---|---|
| VHPOSR (live) | 1050 | 1155 | 1268 | 1500 |
| VPOSR (live) | 1042 | 1155 | 1264 | 1492 |
| VHPOSR before `mach_init` (stub) | 225 | | | |
| INTREQR (a plain trapped register) | 215 | | | |

So a live beam read costs **~940 cycles more** than a plain trapped read
at s = 4 (+27 per shift step; a counter re-read adds ~40 on rare CIA
wraps). The parts: the consistent 32-bit CIA read (~70), frame offset and
clamp (~100), t >> s (27 s), 5·e (~70), and divmod by 227 as 16
shift-subtract steps (~450). A `move.l VPOSR` computes the beam once, not
twice (latched for the second word). The path is cold (only `$DFF004–7`
reads). If a guest turns out to poll the beam hard (AROS's VHPOSR PAL
probe polls a few hundred times at boot), the division is the lever: a
313-entry line table would cut ~400 cycles.

## (i) S3.3 event seam and interrupt delivery (W9-I)

**Hot path: +0.** `core.s` is unchanged and byte-identical modulo
relocation (link-swap: the PRG linked with the base tree's `core.o` equals
the PRG linked with its own, `build/evidence/W9-I/green/coreswap.txt`);
`chip.s` and `mach.s` still link last; `tests.perf_cycles --base <wave9
base> --max-delta 0` passes (`build/evidence/W9-I/green/perf.txt`). The
seam and delivery live entirely in the service pass and the I/O trap path.
No per-instruction work: the IPL is evaluated only at a pass (each event,
each kick), never per instruction.

**Static costs [EST, static, 6502 cycles]:**

| path | cost |
|---|---|
| service pass, nothing armed (vs S3.2) | +~110: `mach_insv`, the VERTB post through `mach_pset`, the empty slot-0 hook, `mach_slots`/`mach_wake` early-outs, `mach_w` copy |
| `mach_etime` | ~70 (`mach_rdc`) + ~38 per shift step (s = 4: ~220) |
| a pass with k armed slots | + 2 × `mach_etime` + ~45 k (due test) + ~70–110 k (wait) + each callback |
| `mach_arm` from the trap context | ~70 + the kick (~35 under `sei`), then one `emu_run` exit and a pass |
| INTENA/INTREQ write (trap path) | +~45 (SET/CLR into the read register, hold OR); a SET adds `mach_ichk` ~60, plus a pass only when it makes an interrupt deliverable |
| delivery (`mach_lvl` ~40–60, `mach_dlv`, `exc_raise`) | ~150 + the frame: one `mem_write32`, one `mem_write16`, one `mem_read32` through the memory API |
| STOP | the CPU idles in a 7-cycle `ev_pend` spin with `cli` until the next event; no fast-forward |

**Latency [MEASURED VICE, `tests/run_irq.py`]:** a slot fires at most
4096 phi2 after its deadline (legs `arm`, `periodic` at s = 4 and 0,
`trap`, `race`; bound `SLACK_PHI2`), never early. A SET write to
INTENA/INTREQ that makes an interrupt deliverable is taken at the next
instruction boundary (leg `levels`: stacked PC = the next instruction for
all 14 sources). An IPL drop by MOVE to SR/RTE is seen within
`MACH_POLL` phi2 while an interrupt is pending (`storm`, `starve`,
`kick`), else at the next event (nothing to deliver then).

**`MACH_POLL` poll cost (fix round 1, review F2) [MEASURED VICE, PAL,
`python3 -m tests.run_irq --perf`, `build/evidence/W9-I/fix1/green/
pollcost.json`]:** the same register-only guest loop at s = 0, IPL 7,
with nothing pending vs VERTB enabled and pending but masked (a pass
every `MACH_POLL` = `$0400` phi2): 23.12 M vs 37.66 M phi2, i.e. **+63%
wall time at 1 MHz; 39% of the time goes to passes**, ~640 cycles per
pass (IRQ, `emu_run` exit/re-entry, `mach_svc` with `mach_pclamp`,
`mach_dlv`'s level test) [EST from share = c / (1024 + c)]. At 48 MHz the
CIAs still count phi2 at ~1 MHz, so the pass rate stays ~980/s and the
share is **~1.3%** [EST]. The poll runs only in exception mode and only
while `mach_lvl` ≠ 0: AROS's Disable() clears INTEN, so `mach_lvl` = 0 and
nothing polls; the case is a request pending under a higher running level
(a handler) or under an SR mask. `MACH_POLL` is one named constant in
`mach.s`; at 1 MHz a larger value trades latency for throughput. **W11-A (supervisor grant, HIGH-1):** the
interval is now max(`MACH_POLL`, 256 E << s) phi2, at most `$FFFF`: the
same `$0400` at s ≤ 2 (the numbers above), `$1000` at s = 4, `$4000` at
s = 6, so the passes' share at 1 MHz falls with s (~13% at s = 4, ~4% at
s = 6) and the latency stays ≤ 256 guest E ticks. Exact
recognition at the SR write would need a core.s SR-write hook (backlog).

**Forward progress: one forced guest instruction (fix round 2).** A pass
that leaves a client slot due now is followed by one `emu_step` before
the next pass (no time floor; round 1b's `MACH_MINW` floor made periodic
clients drift and is gone). Cost: one single-step call (~+15 cycles over
the instruction itself, `emu_step` vs `dispatch_go`) per such pass, and
only when a client is at or behind its deadline. Measured [VICE, s = 0,
`run_irq` legs `progress`/`lag`]: an always-due slot gives 802 passes for
the 801-instruction loop, ~2,830 phi2 per pass+instruction at 1 MHz (a
callback that logs and re-arms included); a period of 3000 phi2 keeps its
lateness bounded (max 2,333); 1500 phi2 is below that service time on a
1 MHz 6502, so its lateness grows ~1,430 phi2 per period (round 0, which
guaranteed no instruction, ~800; round 1b ~3,150). At 48 MHz the same
pass is ~60 phi2, so client periods down to ~100 phi2 stay bounded [EST].

## (j) S3.5 display refresh (W10-V)

`runtime/ops/disp.s` (`docs/C2V.md` "Display refresh (S3.5)"). Measured with
`python3 -m tests.run_pf --prg … --leg shim --perf`: CIA2 TA→TB 32-bit φ2
count, screen blanked (no badlines), VICE x64sc PAL [MEASURED VICE];
evidence `build/evidence/w10-disp/perf/`.

| Entry | φ2 cycles |
|---|---|
| `disp_frame`, `gen` unchanged (the per-frame cost while static) | 55 |
| `disp_frame`, `gen` changed, fields equal (copper rewrote the same values) | 2,185 |
| `disp_refresh`, blank (DMA off, border lookup) | 2,265 |
| `disp_refresh`, mc2, 200 lines | 422,505 |
| `disp_refresh`, hires4, 200 lines | 770,377 |

A full refresh = palette (~23k) + kernel (mc2 365,991 / hires 683,591, as
`run_c2v --perf` measures on this build) + the decision (~34k mc2 / ~64k
hires). The decision is dominated by the 512 KB line walk: every fetched row
of every plane, ~50 cycles per row. That is 8% of a hires refresh; a
two-endpoint fast path is possible (backlog).

**Kernel change (row skip).** On the same VICE, `run_c2v --perf` gives
mc2 365,991 vs 362,679 on the base (+3,312, +0.9%) and hires 683,591 vs
673,880 (+9,711, +1.4%). The added cost is +4 cycles per plane row
(+1,600 / +3,200 per frame). The rest is the kernels' placement: the
module grew from 4 to 5 pages, so the loops' page-crossing reads moved,
inside the ±1.5% band `docs/C2V.md` "Performance" documents. The
interpreter is unaffected: `perf_cycles --max-delta 0`.

**Duty budget `DISP_N` = 20 frames of guest E-time, from a conversion's
END** (fix round 1, F3). Inside `mach_run`, a conversion starts only when
N × 14188 E ticks have passed since the previous conversion ended
(`mach_etime` at its end). The round-0 budget counted `mach_frames` from a
conversion's start. A conversion longer than N frames (mc2 ≈ 30 frames at
s = 0) used the whole budget up itself, so the next one could start at once,
and the guest livelocked at s ≤ 1 with an edit every frame (review
`pf_live_s0edit`: no HALT in 900 s; now `tests/run_pf.py` leg `live`).
Both quantities are φ2 on the same CIA clock, so the worst-case duty
(`gen` changing every frame) is conv / (conv + N × 14188 × 2^s),
independent of PAL/NTSC, and the guest always keeps the rest:

| s | frame (φ2) | mc2 duty, N = 20 | hires4 duty, N = 20 |
|---|---|---|---|
| 4 (default) | 227,008 | 8.5% | **14.5%** |
| 3 | 113,504 | 15.7% | 25.3% |
| 2 | 56,752 | 27.1% | 40.4% |
| 1 | 28,376 | 42.7% | 57.6% |
| 0 | 14,188 | 59.8% | 73.1% |

**Superseded (Wave 12):** W12-V's colour work made hires conversion dearer, and DISP_N is now 24. The current figures are in (m); the table and text here describe the Wave 10 code.

N = 20 keeps hires4 under S3_PLAN §2.3's 20% duty at the default s = 4,
with margin for badlines (+2–4% with the display on) and for layout (±1.5%).
At s ≤ 3 a display that changes every frame exceeds 20%, but the display
never takes more than 73% of the guest's time (hires4 at s = 0). That bounds
the display's share only: at s = 0 the machine loop itself already starves
the guest with no display at all (W10-V review: one guest loop iteration in
2,131 frames with the hook off), so s = 0 is not a usable setting either way. Measured live
(`run_pf` leg `live`, mc2, an edit every delivered VERTB): s = 4 loses
5.2% of guest progress to the display, against 7% on the round-0
frame-counted budget (review), and s = 0 reaches HALT. On the U64E at
48 MHz the CIAs count φ2 at 1 MHz [MEASURED HW, W8-H], so the same
conversion is ~40× shorter in guest time: ≤ 0.5%.

## (k) Blitter (W11-B, S3.6)

**Hot path: +0.** `core.s` is unchanged and byte-identical modulo
relocation (link-swap, `build/evidence/w11-blt/coreswap.txt`); every module
linked before `chip.s` and all BSS before `blt.s` keep their addresses
(`build/evidence/w11-blt/mapdiff.txt`); `tests.perf_cycles --base <wave11
base> --max-delta 0` passes. The blitter costs only the trap path of a
`$040-$074` write: +~25 cycles to route it to `blt_w`, and a DMACONR read
+~20.

**Measured [MEASURED VICE, 1 MHz PAL; `tests.run_blt --perf`]:** `blt_w`
timed directly (CIA2 phi2 count; two points, 1x1 and 20x40 words; best of
3), so the per-word figure excludes the trap entry and the fixed figure is
per blit (setup, the bank switch, the writeback, the IRQ raise):

| shape | phi2 / word | fixed phi2 / blit | note |
|---|---|---|---|
| CD `$CA` (A, B constant: the boot's shape) | 108 | ~2,600 | fast `$CA` kernel, one C fetch + one D store per row |
| BCD `$CA` | 120 | ~2,850 | |
| AD `$F0` copy | 87 | ~2,800 | fast `$F0` kernel |
| D `$00` clear | 46 | ~2,600 | fast clear kernel |
| ABCD `$CA`, ASH = BSH = 4 | 812 | ~3,700 | bit-serial shifter (~20 phi2/bit/word per shifted channel) |
| AD `$F0` + inclusive fill | 919 | ~2,950 | bit-serial fill (~100 phi2/byte) |

**U64E [MEASURED HW, fw 3.15, 2026-10-06; `tests.run_blt --perf --backend
u64`, `build/evidence/w11-blt/perf-u64.json`, `SESSION-2-perf-and-legs.log`]:**
the same stub. The CIA2 timers count NTSC phi2 at every CPU speed (17095
ticks/frame, 1 tick = 0.978 µs; docs/HARDWARE.md "CIA clock at turbo"), so
the 48 MHz column is ticks x 0.978:

| shape | 1 MHz: phi2/word, fixed phi2 | 48 MHz: µs/word (words/s), fixed µs/blit |
|---|---|---|
| CD `$CA` | 110.6, 2,668 | **4.26** (235k), 70 |
| BCD `$CA` | 123.2, 2,981 | 5.12 (195k), 84 |
| AD `$F0` | 88.2, 2,615 | 3.59 (279k), 71 |
| ABCD `$CA` shifted | 812.9, 3,785 | 20.8 (48k), 109 |
| D clear | 45.8, 2,503 | 1.65 (606k), 59 |
| AD + fill | 904.3, 3,016 | 20.6 (49k), 78 |

48 MHz is only ~25x the 1 MHz rate on the DMA-bound shapes (CD `$CA`:
110.6 / 4.35 ticks), not ~39x: each row programs the REC twice (C fetch, D
stash; ~9 register stores each at ~1 µs on the U64E, docs/HARDWARE.md
finding 2), ~18 µs per row, i.e. ~0.9 µs/word on these 20-word rows.

**vs S3_PLAN §3.5.** The plan assumed ~3.3 µs/word at 48 MHz with row-wise
DMA. The boot's blits are all `CD`/`BCD` `$CA` with no shift (boot model:
292 blits, 496,088 words in its first 40 guest seconds; 76 blits, ~324k
words to "insert disk"). At 1 MHz they cost ~108-120 phi2/word, i.e.
~324k x 0.115 ms ≈ 37 s of the ~40 min 1 MHz boot. At 48 MHz the boot's
shape costs **4.26-5.12 µs/word, above the plan's ~3.3 µs/word**: ~324k
words to "insert disk" ≈ 1.5 s instead of ≈ 1 s, and the current boot
model's 496k words (first 40 guest seconds, 292 blits) ≈ 2.3 s instead of
≈ 1.6 s: +0.5-0.7 s on the 70-95 s estimate, plus ~80 µs per blit (< 0.03 s). The REC register writes per row dominate; a
lever is to keep the REC registers that do not change between rows (C64
address, length, address control) and only rewrite the REU address. Shifted and filled blits use the generic bit-serial paths
(8-10x slower; fine for v0, supervisor ruling); the boot uses neither, Workbench will (text, BOBs): the
levers are a table shifter (2 x 256 B per shift) and a 512-byte fill
table.

## (l) S3.7 AROS boot (W11-A)

`tests/aros_boot.py`, contract "Machine profile and AROS boot (S3.7)".
Pinned ROM AROS-20260921 (`tools/aros/manifest.txt`). Wall time from
`aros_boot` (after staging) to the serial line; "frame" = `mach_frames`
then; "IPS" = the boot model's instruction count at that line / the wall
time (model: m1 1,256,189 instr / frame 95, m2 2,640,815 / frame 212) —
an estimate, since dilated guest time changes the count a little.

**U64E, 48 MHz** [MEASURED HW, fw 3.15 git 3a1ff9ff, fpga 126, core 1.50;
one DeviceLock hold per row, config restored identical; NTSC: frames/s =
72.08 / 2^s; `build/evidence/w11-aros/u64/`]:

| s | m1 | m2 | m1 IPS | m2 IPS | frame at m2 (model 212) |
|---:|---:|---:|---:|---:|---:|
| 0 | 19.9 s | not in 420 s | 63k | – | (30,248 frames at 420 s) |
| 1 | 19.9 s | not in 420 s; fix-round build 4 of 4: 104–106 s | 63k | 25k | 3,793–3,821 |
| 2 | 19.9 s | 67.8 s | 63k | 39k | 1,222 |
| 3 | 20.3 s | 63.4 s | 62k | 42k | 570 |
| **4** | **20.4 s** | **58.1 s** | **62k** | **45k** | **261** |
| 5 | 20.8 s | 58.0 s | 60k | 46k | 130 |

Fix round 1 build (scaled poll, bracketed capture; `u64/fix1-w3/`, 416/416
serial bytes equal to the model in every run): s = 4 m1 20.1 s / m2 57.6 s
(frame 259); s = 3 20.0 / 59.9 s (540); s = 2 19.9 / 67.6 s (1,219); s = 1
19.9 / 106.1 s (3,834). s = 4 stays the fastest: `mach_s` stays 4.

**Milestone 3, the no-media screen** (merged tree with the blitter, fix
round 2; `build/evidence/w11-aros/m3/`; wall time from the boot's start):

| s (U64E 48 MHz) | m2 | m3 | guest frame at m3 (model 345) |
|---:|---:|---:|---:|
| 2 | 67.8 s | 106.3 s | 1,917 |
| **3** | 62.4 s | **86.8 s / 98.3 s** | 785 / 887 |
| 4 | 57.8–58.0 s | 100.6 s / 101.0 s | 453 / 455 |
| 5 | 57.8 s | 113.7 s | 256 |

S3_PLAN §3.5 estimated 70–95 s at 48 MHz [EST]; s = 3 lands at 87–98 s
(the m3 instant is quantised by the conversion budget, DISP_N frames, and
a 2 s poll, so runs vary ±6 s). The phase after m2 waits on guest time
(the model idles 59–89% of frames 250–345), so a smaller s wins there
while a larger one wins up to m2: s = 3 is the best total, `mach_s` = 3
on the U64E. VICE s = 9: m3 at 174 s of warp (frame 386).

m1 is CPU-bound (the ROM checksum and ROMtag scan, 32% of the boot) and
independent of s: ~62k IPS, inside S3_PLAN §3.5's 52–68k [EST]. Staging:
zero fill of 16 MB 4.3–4.7 s, SocketDMA REU write of 1 MB 3.5–4.2 s,
checksum readback 2.6–2.9 s. S3_PLAN §3.5 estimated 70–95 s to the
insert-disk screen; at 45k IPS the model's 3.43 M instructions take ~76 s,
plus the blits (W11-B), inside that range [EST].

**VICE x64sc, PAL, warp** [MEASURED VICE; correctness vehicle, no
performance claim; `build/evidence/w11-aros/explore/`]: m1 ~50 s at every
s. m2: s = 4 not in 400 s (27,856 frames), s = 6 not in 900 s (16,013), s = 7
180 s (frame 1,605), s = 8 129 s (564), **s = 9 119 s (259)**, s = 10 117 s
(129); serial matches the model in every passing run.

**Low s (C4.1; fix round 1, HIGH-1 — the round-0 "AROS needs ≳ 3k
instructions per frame" was wrong and is withdrawn).** One guest frame is
14188 << s phi2 and the CIAs count phi2 at ~1 MHz on both hosts, so a frame
holds ~IPS × 0.0144 × 2^s raw guest instructions: 1 MHz ~23 × 2^s, 48 MHz
~890 × 2^s. The boot model with its CPU slowed k× still reaches m2 down to
~680 instructions per frame (k = 24; CONTRACTS S3.7 "C4" has the table).
Measured on our machine: VICE s ≤ 6 stalls through the `MACH_POLL` cost
(a level-2 request pending under IPL 3 makes every pass a 1024-phi2 poll,
~37% guest speed at 1 MHz, so the level-3 handler outlasts the frame and
the kernel's dispatcher never returns; `MACH_POLL` = `$4000` reaches m2 at
s = 6 in 158.7 s, frame 2821; fixed by the scaled poll interval below and
in (i): m2 at s = 6 in 162.0 s, frame 2821); the U64E reaches m2 at s = 1 (4 of 4 runs,
104–106 s, frame ~3,800; one earlier run did not) and not at s = 0 (2 of
2, 400 s): consistent with the model's limit, since after m1 the U64E
runs 25–39k IPS, ~350–540 instructions per s = 0 frame, at or below the
model's k = 32 (551 per frame, no m2). Post-m1
rates at the chosen s (model instructions m1→m2 / frames): U64E s = 4
≈ 8.1k per frame, VICE s = 9 ≈ 9.3k. The idle "machine loop starves the
guest at s = 0" backlog item, by contrast, is the arithmetic of a 1 MHz
host, measured by leg `s0` (main-loop instructions per real second, a
VERTB handler that only acknowledges, VICE PAL):

| s | 6 | 4 | 3 | 2 | 1 | 0 |
|---|---:|---:|---:|---:|---:|---:|
| copper off | 1,628 | 1,447 | 1,371 | 1,291 | 1,104 | 741 |
| 4 copper MOVEs | 1,628 | 1,395 | 1,302 | 1,140 | 810 | 153 |
| 28 copper MOVEs | 1,628 | 1,302 | 1,132 | 805 | 146 | 129 |

At s = 0 the fixed per-frame work (the pass and its IRQ, the VERTB delivery
and handler with their window refills: ~7.6k cycles; a 4-MOVE copper run
with its gen snapshot: ~5k) fills most of the 14188-cycle frame; every
case still reaches HALT (no livelock). At 48 MHz the same work is ~2% of a
frame. The boot profile's s is chosen per host for speed (U64E 4, VICE 9).

## (m) Visible display: per-cell colours and the colour choice (W12-V)

CONTRACTS "Display refresh (S3.5)", "Colour choice (W12-V)". VICE x64sc PAL,
the CIA2 TA→TB count of (j) [MEASURED VICE]; base = `wave12/integration`
(e9c8cdd / 0bc9c15) built in a clean copy; evidence
`build/evidence/w12-vis/perf/` and `tests/run_pf.py --leg cols --perf`
(fix round 1: the worst cases were found by review rev-vis).

| Entry | base | W12-V |
|---|---:|---:|
| `c2v_hires`, empty planes | – | 729,587 |
| `c2v_hires`, run_c2v's random case | 683,591 | 932,225 |
| `c2v_hires`, **worst**: one set pixel per cell line at bit 0 of the right byte | – | **1,030,057** |
| `c2v_mc2` | 365,991 | 364,591 |
| `disp_msum` (mod 255) / `c2v_hfill` / `disp_cols` worst (16 pens, one LUT colour) | – | 37,270 / 41,324 / 26,409 (each incl. ~25 tramp) |
| `disp_refresh`, hires4, random | 770,351 | 1,091,729 |
| `disp_refresh`, hires4, **worst** (the random refresh + the kernel's worst − random) | – | **1,189,561** |
| `disp_refresh`, mc2 | 422,481 | 461,488 |
| `disp_frame`, gen unchanged / fields equal; refresh blank | 100 / 2,205; 2,251 | 100 / 2,205; 2,336 |

Where the hires cost goes: +3 cycles per empty cell line, +9 per cell line
whose cell already has a pen; `c2v_pick` once per cell with pixels (up to
~150 cycles: the right byte and bit 0 are its slow path); `c2v_hrow` ~1,000
per char row; per conversion `c2v_lookn` for 16 pens, `disp_cols`,
`c2v_hfill` and `disp_msum`. The insert-disk screen has 81–132 cells with
pixels (the boot model's frames 345–397), far from the worst case.

**Duty budget: `DISP_N` 20 → 24** (fix round 1). (j)'s formula at s = 4,
hires4 changing every frame, worst case: 1,189,561 / (1,189,561 + 24 ×
14188 × 16) = **17.9%** before badlines (S3_PLAN §2.3: ≤ 20%); N = 20 would
give 20.7%. With the display on, badlines take 25 × 40 of a PAL frame's
19,656 cycles (5.1%), so the conversion runs ~5.4% longer: **18.7%**. mc2
worst: 7.8%. At 48/64 MHz the conversion is ≤ 0.6% of guest time. The
interpreter is untouched (`perf_cycles --max-delta 0`).

MAIN: the run-length LUT (821 B instead of 2,048; supervisor-approved) pays
for the new code: c2v CODE +1 page ($500 → $600), disp CODE −4 pages
($E00 → $A00), disp BSS unchanged (1 page): MAIN −768 B (BSS end $9DEE →
$9AEE). The LUT is unpacked once, in `disp_init`: 126,616 φ2 cycles against
120,006–120,225 on the base (+5.4%, once per boot; `perf/disp_init_cycles.txt`).

## (n) Code-window cache (W12-C)

Contract: CONTRACTS.md "Code-window cache". Evidence:
`build/evidence/w12-cwin/`. N = 7 windows (CWIN area, $A900–$AFFF), FIFO
replacement, page-low map at $B000.

**Per-instruction paths** [MEASURED VICE, `perf_cycles --base` main,
`--max-delta 0`]: every row ≤ 0 in both modes (run: mem −6.16, stream 0,
rmw −1.50, copy −1.50, alu −0.33, stack −1.00 cycles per instruction;
step: the same). The fetch hit path is unchanged; the word/long write
check went from 16 to 13 cycles, the byte write check from 9 to 13 (+4).
The −6/−0.3 on rows without writes are layout (page-cross) effects.
One-off per run: +277 cycles (`emu_reset` → `code_invall`).

**Write check per size** [MEASURED, `run_cwin` leg bytew: MOVE.B/W/L
D1,(A0) + DBRA, cycles per instruction (VICE) / µs per instruction (C64U
64 MHz); data page: "other" = no cached page has its low byte; "evicted"
= its low byte was cached and evicted (asserted equal to "other");
"p+1 cached" = the next page-low is cached (bit 6); "alias" = the cached
code page's low byte in another bank (the map ignores the bank)]:

| | byte | word | long |
|---|---:|---:|---:|
| main, VICE | 521.2 | 541.6 | 566.2 |
| W12-C, VICE, other | 522.8 | 540.6 | 564.6 |
| W12-C, VICE, evicted | 523.4 | 540.6 | 563.7 |
| W12-C, VICE, p+1 cached | 523.2 | 591.5 | 615.5 |
| W12-C, VICE, alias | 601.4 | 618.6 | 642.8 |
| W12-C, C64U, other / alias | 9.04 / 9.39 | 8.65 / 10.86 | 9.57 / 11.47 |

So a slow-path write costs ~+102 cycles (p+1 cached, word/long) or ~+156
(alias, any size) on VICE. On the AROS boot (counter build, VICE) the
slow path runs ~42k times to m3 (~0.012 per instruction). A first
version left the hint bits in a map entry whose count dropped to 0, which
sent every later word/long write to that page-low down the slow path
(rev-cwin: 397k of 509k write checks m1→m3, more 6502 cycles than the
cache saved); fixed (`cw_drop` keeps only bit 6), guarded by `run_cwin`
smc/map, bytew `evicted` and mutant `stalehint`. A slow path entered with
BASIC ROM in (host staging through `test_load_chunk` at $37, where the map
byte read was a ROM byte) banks BASIC out for the check and restores the
caller's $01 (tests rely on $37 staying), so staging at $37 pays ~+150
cycles for about half of its byte writes (~504k on the 1 MB AROS staging
through test_load_chunk, ~75 s of 1 MHz time; VICE and the C64U's
test_load_chunk fallback only; the Ultimate stages by SocketDMA).

**Page change** [MEASURED; `run_cwin` leg bench: a K-page ring against
the same instructions in one page, two-point delta]:

| | VICE (cycles) | C64U 64 MHz (µs) | U64E 48 MHz (µs) |
|---|---:|---:|---:|
| main: refill (every page change) | 358 | 75.0–77.1 | 91.6 |
| W12-C: switch to a cached window (K ≤ 7) | 129 | 1.5–1.9 | 2.4–2.9 |
| W12-C: refill (K ≥ 8) | 589 | 78.8 | 96.4–97.0 |

This settles WB_BOOT §4.5's 90 µs estimate: a refill costs ~76 µs on the
C64U at 64 MHz, almost all of it the REC/DMA, not CPU cycles. The cache's
own work is ~2–3 µs per refill and ~1.7 µs per switch (U64E 48 MHz: a
refill ~92 µs, the cache's work ~5 µs per refill and ~2.4 µs per switch).
On the U64E the first timed run of a session read back invalid (negative
or 729 µs, twice); the leg now discards a warm-up run.

**Refills on the AROS boot** [MEASURED VICE, counter build `-D CWIN_COUNT`,
s = 9; at the boot model's instruction counts; model = the boot model's
page trace replayed through LRU/FIFO, `policy.txt`]:

| to m3 (3.43 M instr) | page changes | refills | per instr | vs N = 1 | model (LRU / FIFO) |
|---|---:|---:|---:|---:|---:|
| N = 1 | 209,351 | 209,351 | 0.0610 | – | 207,007 |
| N = 4 | 210,276 | 124,569 | 0.0363 | −40.5% | 113,699 / 122,076 |
| N = 7 | 211,234 | 84,138 | 0.0245 | −59.8% | 89,717 / 90,631 |

The measured N = 7 cut (−59.8%) is close to the model's −56.2% (FIFO)
and inside the brief's 58–68% (§4.5, which measured the WB boots). N = 8
(model LRU −59.7%) does not fit: CWIN holds the cache's banked code
(CWCODE, $A800) and 7 windows.

**AROS boot wall-clock** [MEASURED HW, `aros_boot --legs m1,m2,m3`,
s = 3, one hold per run, config IDENTICAL after each;
`build/evidence/w12-cwin/hw/`, `hw2/`, `hw3/`]:

| | m1 | m2 | m3 |
|---|---:|---:|---:|
| C64U 64 MHz, main (3 runs) | 15.3 / 15.3 / 15.4 s | 45.2 / 45.3 / 45.3 s | 76.1 / 76.7 / 76.4 s |
| C64U 64 MHz, W12-C before the stale-hint fix (4 runs) | 14.8–15.0 s | 38.5–39.6 s | 59.7 / 70.3 / 60.1 / 81.4 s |
| C64U 64 MHz, W12-C final (4 runs) | 15.3 / 14.8 / 14.9 / 14.9 s | 38.8 / 38.6 / 38.6 / 38.7 s | 70.2 / 69.5 / 69.8 / 81.8 s |
| U64E 48 MHz, main (2 runs) | 20.2 / 20.0 s | 59.6 / 60.2 s | 95.5 / 100.6 s |
| U64E 48 MHz, W12-C final (3 runs) | 19.4 / 19.4 / 19.8 s | 57.1 / 51.3 / 53.3 s | 90.6 / 84.3 / 86.7 s |

C64U: m2 −6.6 s (−15%; the refill arithmetic predicts −6.4 s) in every
run. m3 is the first display conversion whose hash equals a model frame;
conversions come ~100 guest frames (~10 s at s = 3) apart, so m3 is
quantised: three final runs at 69.5–70.2 s (guest frame 626–633), one at
81.8 s (one conversion later), against main's steady 76.1–76.7 s. U64E:
m2 −6.0 s (59.88 → 53.93 s) and m3 −10.9 s (98.05 → 87.2 s) on average (rev-cwin round 2, recomputed from the table's runs); the U64E m2 spread (51.3–57.1 s) is wider than the C64U's, so m2 is "steady" on the C64U only. (hw3 = the final code except the
slow path's $01 restore; hw4 = the final tree.)

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
