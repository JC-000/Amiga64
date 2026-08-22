# HARDWARE — Ultimate 64 Elite validation + measured timing

Real-silicon validation of the M1 interpreter and M0 pipeline on the
Ultimate 64 Elite, 2026-08-22. Everything in `docs/PERF.md` was an analytic
projection; the numbers here are measured. Two interpreter builds were
measured: **v0** (pre-fetch-window core, the build that ran the full suite)
and **windowed** (core with P1's instruction-stream fetch window, commit
5c842ed).

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

## Reproduction

- Suites: `U64_HOST=<ip> python3 -m tests.run_vectors --prg build/h1/runner.prg
  --vectors build/vectors --backend u64` (add `--limit N` for subsets);
  `python3 -m tests.run_program --prg build/h1/runner.prg --backend u64`.
- Benches: session scratchpad `u64/bench.py` (+ `sweep_v0.json`,
  `sweep_windowed.json`); hand-assembled 6502 loops were functionally
  verified in VICE before touching hardware.
