# HARDWARE — Ultimate 64 Elite validation + measured timing

Real-silicon validation of the M1 interpreter and M0 pipeline on the
Ultimate 64 Elite, 2026-08-22. Everything in `docs/PERF.md` was an analytic
projection; the numbers here are measured. Two interpreter builds were
measured: **v0** (pre-fetch-window core, the build that ran the full suite)
and **windowed** (core with P1's instruction-stream fetch window, commit
5c842ed). A later section, "REC shadow core", adds the wave-4 measurements
(2026-09-23, **firmware 3.15**; compare only within that section).

## Device + setup

| Field | Value |
|---|---|
| Device | Ultimate 64 Elite, hostname `Ultimate-64-Elite-688004`, unique_id `601A96` |
| Firmware / FPGA / core | 3.14d / 122 / 1.49 |
| Address | `http://10.43.23.81` (REST API v0.1 — the harness docs' `192.168.1.81` is stale) |
| Backend | `--backend u64` via c64-test-harness `UnifiedManager` (automatic `DeviceLock`) |
| REU | REST-configured Enabled / 16 MB for tests |
| CPU speed | 1 MHz for all correctness suites; sweep via `CPU Speed` + `Turbo Control: Manual` |

Config as found and **verified restored** afterwards: CPU Speed `" 1"`,
Turbo Control `"Manual"`, RAM Expansion Unit `Enabled`, REU Size `512 KB`,
Cartridge preset `"REU"`; machine reset to clean BASIC at the end. No
firmware or persistent-storage changes of any kind.

## Backend fixes (tests/*.py — host-side only, no `.s` changes)

1. **The stock U64 `run_subroutine` cannot drive our runner**
   (`tests/harness_util.py`). The harness triggers its trampoline by typing
   `SYS <addr>` into the keyboard buffer, which requires BASIC READY. Our
   runner's `main` parks in a `jmp @idle` busy loop after flagging ready
   ($CFFF=$A5), so injected keystrokes never execute. Fix: a host-installed
   **mailbox dispatcher** in the unused `$CE00` page of buf_result. The
   host writes a 21-byte loop (poll MBOX `$CEF8` → clear → `JSR <target>` →
   DONE `$CEF9`=$FF → repeat), then retargets the idle JMP by rewriting
   **only the operand high byte** (`main+7`). A 1-byte DMA write is atomic
   and the operand low byte is unchanged, so the spinning CPU sees either
   the old or the new jump target — never a torn address. Per call: write
   the JSR operand, write MBOX=1/DONE=0 in one DMA write, poll DONE.
   Install is verified by readback and self-tested against a bare RTS.
   VICE path (`jsr()`) unchanged.
2. **`prepare_target`** (`tests/run_vectors.py`) installs the dispatcher on
   the u64 path after `set_reu(16 MB)` + `run_prg` + ready wait, resolving
   `main` from the ld65 label file, using the public
   `target.transport.client` accessor.
3. **`--limit N`** added to `run_vectors` (cases per batch) for subset runs
   on slow transports. `run_program.py` already had `--backend` and worked
   on hardware unmodified.

Driver notes: REST round-trips (~10–30 ms) dominate per-case cost — the
C64-side work is negligible next to them, so the suites were run at 1 MHz.
IRQs stay enabled until the first `test_run_case` SEIs (never CLIs);
the dispatcher lives in always-RAM `$CExx` and is indifferent to ROM
banking state. VICE regression after the driver edits: M0 demo 48/48,
vector batches and hand suite all green.

## Suite results on hardware (v0 core, 1 MHz)

| Suite | Result | Wall clock |
|---|---|---|
| Harness smoke (3 ADD.b cases) | 3/3 pass | 4.1 s incl. boot |
| M0 demo (`tests.run_program`, minimal+reloc+budget) | 48/48 checks | ~40 s |
| Hand vectors (48) | 48/48 pass | 14.8 s |
| **Full SingleStepTests suite, 103 batches** | **5150/5150 pass, 0 field failures** | 1392.8 s (~0.27 s/case, REST-bound) |
| Hand vectors on the **windowed** core | 48/48 pass | 14.0 s |

**No hardware-vs-VICE behavioral divergence found.** No wedges, no
re-acquisitions, no REU addressing anomalies at 16 MB. (The windowed core
additionally passed the full VICE gates before its hardware runs.)

## Measured timing

Methodology: every number is a two-point delta (same workload at two
iteration counts) so fixed REST/dispatcher overhead cancels. Effective CPU
clock is calibrated per speed step with a known-cycle 6502 loop (so VIC-II
badline stalls are folded into "effective MHz", and c/instr figures are in
real delivered CPU cycles). Benches: `$DF0A` store loop (REC I/O rate);
REU fetches of 4096 bytes via autoload (bulk DMA rate); 68k compute loop
(ADDQ.L/ADD.L/DBRA, 1 word fetch per ALU op + 2 per DBRA); 68k memory loop
(MOVE.L (A0),D1/DBRA, adds a 4-byte guest-data read per iteration).
A LEN=1 DMA bench was also run but its deltas were smaller than REST
timing jitter; those numbers are excluded as unreliable.

### v0 core — full sweep

| Step (MHz) | Effective MHz | ns per REC I/O store | Bulk REU DMA MB/s | 68k ALU IPS (c/instr) | 68k MEM IPS (c/instr) |
|---|---|---|---|---|---|
| 1 | 0.961 | 9 462 (=9-cycle loop, full speed) | 0.96 | 1 298 (740) | 1 310 (734) |
| 2 | 1.897 | 5 209 | 1.92 | 2 527 (751) | 2 721 (697) |
| 4 | 4.027 | 3 102 | 1.87 | 5 125 (786) | 4 937 (816) |
| 8 | 7.695 | 2 149 | 2.54 | 9 597 (802) | 9 132 (843) |
| 12 | 11.542 | 2 101 | 2.83 | 11 843 (975) | 11 400 (1 012) |
| 16 | 15.005 | 2 079 | 2.99 | 19 106 (785) | 13 657 (1 099) |
| 20 | 18.309 | 2 079 | 3.10 | 22 077 (829) | 17 203 (1 064) |
| 24 | 22.922 | 2 078 | 3.17 | 24 134 (950) | 18 402 (1 246) |
| 32 | 30.280 | 1 048 | 3.22 | 27 493 (1 101) | 22 628 (1 338) |
| 40 | 36.800 | 1 046 | 3.18 | 31 324 (1 175) | 27 334 (1 346) |
| 48 | 46.206 | 1 044 | 3.36 | 35 258 (1 311) | 28 072 (1 646) |

(The "ns per store" column is wall time per 9-cycle store loop iteration;
subtract 5 non-store cycles at the effective clock for the store itself.
The 12 MHz ALU/MEM c/instr bump and the 16 MHz ALU dip-back are repeatable
oddities of those steps as measured, not noise — deltas were 1–2 s.)

### Windowed core (commit 5c842ed) — 68k IPS at 1/16/20/48

| Step (MHz) | Effective MHz | 68k ALU IPS (c/instr) | vs v0 | 68k MEM IPS (c/instr) | vs v0 |
|---|---|---|---|---|---|
| 1 | 0.983 | 1 589 (619) | +22% | 1 688 (582) | +29% |
| 16 | 15.680 | 23 853 (657) | +25% | 27 568 (569) | +102% |
| 20 | 19.093 | 32 355 (590) | +47% | 28 971 (659) | +68% |
| 48 | 46.002 | **80 297 (573)** | **+128%** | **57 005 (807)** | **+103%** |

## Findings

**1. CPU clock scales almost linearly to 48.** Effective throughput at the
"48" step is 46.2 MHz (96%); every step delivers 92–100% of nominal. The
1 MHz baseline measures 0.96–0.98 MHz — the deficit is VIC-II badline DMA,
present at all speeds and already folded into the effective-MHz columns.

**2. REC I/O register access is throttled, independent of DMA.** A `$DF0A`
store costs ~9.5 µs at 1 MHz (normal bus speed), but stops scaling almost
immediately: ~1.7–1.9 µs per store across the 8–24 MHz band, improving in
a discrete step to ~0.9–1.0 µs at 32/40/48 (the FPGA evidently switches
wait-state regimes; the improvement is repeatable). Practical rule: **an
I/O register access at turbo costs ~1 µs of wall time (~45 CPU cycles at
48 MHz) regardless of the CPU clock.** The v0 interpreter performs ~10 REC
register accesses per memory-API call, so this — not DMA byte time — is
the dominant hardware ceiling for the pre-window interpreter.

**3. Bulk REU DMA saturates at ~3.3 MB/s.** Per-byte transfer time scales
perfectly from 1 MHz (1.05 µs/B) to 2 MHz (0.52 µs/B), then stops: 4 MHz
delivers the same ~0.53 µs/B as 2 MHz, followed by a slow asymptotic climb
to ~0.30 µs/B (3.36 MB/s) at 48. So for *bulk transfer bandwidth* the knee
is at **~2 MHz**, i.e. DMA behaves like a fixed ~3.3 MB/s engine above it —
NOT 1 byte per turbo CPU cycle. The prior-project expectation (user, from
c64-https and c64-wireguard) of a 16–20 MHz knee does not describe bulk
DMA on this device/firmware; what *does* land in that region is the
combined interpreter-level rolloff (next point). There is no CPU-speed
step between 16 and 20 on fw 3.14d to refine the bracket.

**4. The v0 interpreter's scaling rolloff is gradual, centred where REC
setup dominates.** v0 MEM-bench scaling efficiency (IPS gain ÷ effective
clock gain vs 1 MHz): ~100% at 2–8 MHz, 72% at 12, 67–69% at 16–20, 59% at
24, 55% at 32, 45% at 48. There is no sharp knee; the 50% crossing sits
around 24–32 MHz. Because the interpreter moves few bytes per REC program
(≤4), its ceiling is the ~1–2 µs per REC register access (finding 2), not
the 3.3 MB/s stream rate (finding 3).

**5. The fetch window moves the interpreter back into the CPU-scaled
regime.** Windowed c/instr on the compute-bound loop is essentially flat
(619 → 573) from 1 to 48 MHz — i.e. no hardware penalty growth at all —
where v0 grew 740 → 1 311. At 48 MHz the window doubles both benches
(+128% compute, +103% memory-bound). The prediction that windowing
matters *more* at turbo is confirmed: its VICE-model gain was ~1.25×, its
measured 48 MHz gain is 2.3×.

**6. PERF.md scorecard.**
- *"~500 6502 cycles per 68k instruction (v0)"*: optimistic ~2× at 1 MHz.
  Measured v0: 740 c/instr on a light ALU/DBcc mix that the model prices
  at ~373; 734 on the memory loop (model ~530–580). The per-class cycle
  comments undercount real fetch/dispatch cost.
- *"~2,000 IPS at 1 MHz"*: measured 1 298–1 310 (v0), 1 589–1 688
  (windowed).
- *"~96,000 IPS at 48 MHz"*: v0 measured **35,258** — 2.7× below
  projection, because the uniform-scaling assumption fails (findings 2–3).
  The windowed core reaches **80,297** on the compute-bound mix, close to
  the original projection.
- *"REU DMA and RAM access scale uniformly with the turbo clock"*
  (PERF.md caveat, flagged as the big unknown): **false**. RAM: yes.
  Bulk DMA: capped ~3.3 MB/s. I/O register access: capped ~1 M/s.
- Optimization-lever ranking is confirmed by silicon: windowing (lever 1)
  was worth 2.3× at 48 MHz, ahead of its ~1.5× paper estimate; lever 2
  (fewer REC accesses per data-memory call) is now the top remaining item,
  and at ~1 µs per saved register access at turbo it is worth more on
  hardware than its cycle-count estimate suggests.

**7. Throughput perspective.** Windowed, 48 MHz, compute-bound: 80k IPS ≈
a ~0.8–1 MHz 68000 on light code — Workbench-utility territory, matching
PERF.md's qualitative claim. Memory-heavy code lands at ~57k IPS.

## REC shadow core (wave 4, WS-A), 2026-09-23

This section covers the core with the **REC register shadow**
(`runtime/core.s` memory API; ownership contract C1 in
`docs/WAVE4_BRIEF.md`). The data memory API now stores only the REC
registers whose value changed since its last call, and it issues every
data DMA with autoload. Everything was measured in **one DeviceLock
session** (plus two short teeth-only sessions), using
`tests/bench/u64_session.py`. Raw data is in
`fixtures/bench/sweep_reclean.json`.

**Device state differs from the 2026-08-22 runs above.** The firmware is
now **3.15** (git bce4535e), FPGA 125, core 1.4F. On this firmware
`Cartridge` has no `"REU"` preset; the harness `set_reu` adapts to that.
The session found this config left by another project's lane: Turbo
Control `Manual`, CPU Speed `48`, REU `Disabled` / `2 MB`, Cartridge
`""`, Cartridge Preference `Auto`, Badline Timing `Enabled`, Bus
Operation Mode `Quiet`. That is not the `" 1"` / REU-512 KB state
recorded above. The session snapshotted that config right after acquiring
the lock and restored it exactly before releasing the lock (re-read diff:
empty, in all three sessions). The C64 was reset at the end. No firmware
or persistent-storage changes were made. The harness baseline reset on
entry was disabled with `U64_BASELINE_ON_ENTRY=0`. **Do not compare the
numbers below with the 3.14d tables above.** For example, the calibrated
clock at the "48" step is now 38.4–39.4 MHz, where 3.14d measured 46.2.
Only the same-session base-vs-new deltas below are claims.

### REC semantics on silicon (`tests/bench/rec_probe.py`)

The same 6502 probe was run on VICE and on the U64E. Both gave
byte-identical results:

- **Without autoload** (`$91`), a transfer leaves the C64 address
  register and the REU address register advanced by the transfer
  length, and LEN = `$0001`.
- **With autoload** (`$B1`), after every transfer the registers hold
  exactly the values last *written* by the CPU. A single-register write
  (REU-low, then length-low, then C64-low) is also what the next
  autoloaded transfer reloads.
- The data landed where the register model predicts.

The bank register reads back as `$F8|bank` on both, and ACTRL reads as
`$3F`. Neither matters here, because the core never reads REC registers.
Conclusion: shadowing the registers is valid only together with autoload,
and with autoload it is exact on both targets.

### Correctness on silicon (new core, 1 MHz)

| Suite | Result |
|---|---|
| REC probe | identical to VICE (above) |
| Hand vectors | **48/48** (7.7 s) |
| run_program fixtures (minimal + reloc + budget demos) | **48/48 checks** |
| **Full SingleStepTests suite, 103 batches** | **5150/5150, 0 field failures** (894.8 s) |
| C1 teeth, normal build (refill, reset_case, load_chunk, mailbox) | 4/4 legs pass |
| C1 teeth, `REC_NOINVAL` build | 4/4 legs **fail** (as required) |
| C1 teeth, `REC_MUT_DIRTY` build | load_chunk and mailbox **fail**; refill and reset_case pass (their invalidation is intact) |

The `mailbox` leg is hardware-only. It scribbles over the REC, then calls
`mem_read8` directly through the mailbox dispatcher (the
`run_program.peek_guest` path). It proves that the dispatcher's
`jsr mem_rec_dirty` (in `tests/harness_util.py`) is load-bearing: with
that seam emptied, the call returned the stale bounce-buffer byte.

**No hardware-vs-VICE divergence was found.** The REC state held across
REST DMA, mailbox dispatch and firmware activity between calls. That
state is exactly what C1 invalidates on every host entry, so the silicon
result shows the invalidation is sufficient. It does not show that the
firmware never touches the REC.

### REC register access cost on 3.15

`$DF0A` store cost, calibrated as in finding 2: **4.2 µs at 1 MHz**
(= 4 cycles, full bus speed), **1.72 µs at 16 MHz**, and **0.91 µs at
48 MHz**. This matches the 3.14d finding: about 1 µs per REC access at
turbo, independent of CPU clock.

### 68k IPS, base (windowed, 158e684) vs REC-shadow core

Method: two-point deltas, as described above. Each speed step ran in ABBA
order (base, new, new, base) in one session. Values are the means of the
two samples. The cycles-per-instruction figure (c/instr) uses the
calibrated effective clock.

REC accesses per loop iteration were counted in VICE by
`tests/rec_budget.py` and are exact, not estimated:

| Loop | Base | New |
|---|---|---|
| ALU | 0 | 0 |
| MEM | 9 | 1 |
| STREAM | 9 | ≈2 |
| RMW | 18 | 2 |

| MHz (eff) | ALU base → new | MEM `MOVE.L (A0),D1` | STREAM `MOVE.L (A0)+,D1` | RMW `ADDQ.L #1,(A0)` |
|---|---|---|---|---|
| 1 (0.96) | 1 609 → 1 608 (±0%) | 1 658 → 1 682 (**+1.4%**) | 1 571 → 1 562 (−0.5%, noise) | 1 484 → 1 519 (**+2.3%**) |
| 16 (13.6) | 25 955 → 25 626 (−1.3%) | 22 990 → 26 081 (**+13.4%**) | 21 476 → 23 752 (**+10.6%**) | 18 467 → 22 975 (**+24.4%**) |
| 48 (38.9) | 76 070 → 75 183 (−1.2%) | 54 049 → 71 563 (**+32.4%**) | 52 847 → 61 518 (**+16.4%**) | 40 412 → 57 757 (**+42.9%**) |

c/instr at 48 MHz, base → new: MEM 721 → 544, STREAM 737 → 633, RMW
964 → 674, ALU 512 → 518.

The ALU change stays inside the rep-to-rep spread. The two base samples
themselves differ by 1.3% at 16 MHz and 1.1% at 48 MHz. At 1 MHz, where
the clock is exact, ALU c/instr is identical (596 vs 594).

**Finding 8. The saved time matches the access count.** Dividing the time
saved per instruction by the REC accesses saved per instruction gives
**0.76–1.13 µs per avoided access at 48 MHz**, and 1.28–1.33 µs at
16 MHz. This agrees with the measured `$DF0A` store cost above, so the
gain is exactly lever 2's mechanism. At 1 MHz the accesses cost only
4 cycles each, so the effect is within ±2%. The warm path is cycle-neutral
(~48 cycles of REC setup vs v0's ~59 incl. jsr, plus shadow compares).
At 48 MHz the memory-bound loops are now 544–674 c/instr against 512 for
the pure-ALU loop. What remains is mostly CPU work, plus one command
store and the DMA itself per call.

## Reproduction

- Suites: `U64_HOST=<ip> python3 -m tests.run_vectors --prg build/h1/runner.prg
  --vectors build/vectors --backend u64` (add `--limit N` for subsets);
  `python3 -m tests.run_program --prg build/h1/runner.prg --backend u64`.
- Benches: session scratchpad `u64/bench.py` (+ `sweep_v0.json`,
  `sweep_windowed.json`); hand-assembled 6502 loops were functionally
  verified in VICE before touching hardware.
- REC shadow core (wave 4): `U64_HOST=<ip> U64_BASELINE_ON_ENTRY=0 python3
  -m tests.bench.u64_session --new build/ws-a/runner.prg --base
  build/base/runner.prg --noinval build/ws-a-noinval/runner.prg`. This runs
  every phase (REC probe, hand, full suite, run_program, C1 teeth, ABBA IPS
  bench) under one DeviceLock hold, with config snapshot/restore. The noinval
  PRG comes from `python3 -m tests.rec_build --out build/ws-a-noinval
  REC_NOINVAL`. The base PRG is `make runtime BUILD=build/base` on the
  158e684 sources. `tests/bench/u64bench.py` now takes `--prg` instead of the
  hardcoded `build/h1` path.
