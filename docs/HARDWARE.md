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

### Test-rig note: the keyboard's LED board hides the first hold after idle

This C64's keyboard carries a **Blingboard** (an RGB LED board). After
about 2 minutes idle, the whole first key hold is invisible at the
keyboard matrix: a key held to a count of 10 produces nothing. A SHIFT tap
first wakes it; after a few seconds idle it is fine. **Verified on stock
BASIC, with none of this project's code running, 2026-09-28.**

Any attended keyboard test on this rig must start each run with a SHIFT
tap (the W6-K driver's checklist and the program's step-1 prompt say so),
and a "no key seen at all" in a run's first step is the rig, not the code
under test (docs/HLE_CON.md, "The Blingboard").

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
| **Full SingleStepTests suite, 103 batches** | **5150/5150 pass, 0 field failures** [^io] | 1392.8 s (~0.27 s/case, REST-bound) |
| Hand vectors on the **windowed** core | 48/48 pass | 14.0 s |

[^io]: Since Wave 7 the golden gate set is the 5129-case subset of this
validated 5150-case set: the 21 cases that touch banks $BF/$DF were
dropped after sampling (the I/O trap, `docs/CONTRACTS.md` "Vector sets").
The same holds for every 5150/5150 row below.

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
That drop is **not** attributed to the firmware alone: this session ran
with the other lane's Badline Timing `Enabled` / Bus Operation Mode
`Quiet` config, and those settings may cost effective clock too. No
isolating measurement was made. Only the same-session base-vs-new deltas
below are claims.

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
16 MHz. That band is roughly **±25%** around the measured `$DF0A`
store cost (0.91 µs at 48, 1.72 µs at 16). The saved per-instruction time
also includes the shadow compare cycles and two-point timing jitter, and
loads/stores/command stores may not all cost exactly one `$DF0A` store.
So this is **consistent with** lever 2's mechanism being the whole gain,
not a precise per-access measurement. At 1 MHz the accesses cost only
4 cycles each, so the effect is within ±2%. The warm path is cycle-neutral
(47 cycles of REC setup vs v0's ~59 incl. jsr, plus shadow compares).
At 48 MHz the memory-bound loops are now 544–674 c/instr against 512 for
the pure-ALU loop. What remains is mostly CPU work, plus one command
store and the DMA itself per call.

### Fix round, 2026-09-24: 512 KB REU-block wrap and bank carry

The adversarial review found a **pre-existing** bug. It was present in
158e684 and in every earlier core. The REC's REU address counter carries
only through **bits 0–18**, so one DMA that runs past `$x7FFFF` continues
at `$x00000` instead of `$(x+8)0000`. A 16- or 32-bit guest access that
straddles a 512 KB line therefore split its bytes across the wrong
addresses, and reads wrapped the same way. That is why a read-back
"verified" the damage.

**Silicon matches VICE.** `tests/bench/rec_probe.py`, part 2
(`rec_probe_x.s`), gave byte-identical results on the U64E (fw 3.15,
REU at 16 MB) and on VICE:

- A raw 4-byte stash to `$07FFFE` lands at `$07FFFE/F` and `$000000/1`.
  `$080000/1` is left untouched. `wraps_512k: True`.
- A 4-byte stash to `$FFFFFE` lands its second half at `$F80000`, not
  `$000000`.
- A 4-byte fetch or stash at `$01FFFE` carries correctly into bank 2
  (inside a 512 KB block).
- With **autoload**, the BANK register reads back as bank 1 afterwards
  (`$F9`, the upper bits read as 1). A following access that rewrites only
  REU-low/high and leaves BANK alone hits bank 1, for both fetch and stash.
- Without autoload, BANK is left at 2.

So the REC shadow's bank handling is sound, and bank-crossing transfers
stay on the fast path.

**Fix** (`runtime/core.s`):

- `mem_read16/32` and `mem_write16/32` first test whether the access
  straddles a 512 KB line (`STRADDLE`, 7 cycles on the common path).
- If it does, `mio_split` moves the bytes one DMA per byte, with the CPU
  doing the 24-bit address increment, so `$FFFFFF` wraps to `$000000` as
  on the 68000.
- `mio_split` invalidates the shadow *before* it touches the REC.
- Every other DMA in the tree cannot straddle:
  - byte API calls;
  - `test_load_chunk` staging, which is per-byte `mem_write8`;
  - the 256-byte page-aligned window refill.
- No host tool writes the REU directly.
- WS-B2's future bulk DOS DMA must split at 512 KB lines itself.

**Red → green** (evidence: `build/evidence/ws-a/fix1/`):

- **Hand vectors:** the new `WRAP.bin` batch has 15 cases, written by
  `host/handvec.py`:
  - 32-bit loads and stores at `$07FFFE`, `$17FFFE` and `$FFFFFE`;
  - copy, read-modify-write and predecrement straddles;
  - word and byte controls;
  - three bank-carry cases, each with a wrong-bank twin.

  9 of the 15 fail on 158e684 and on 5ca5a20 (25 field failures); all 15
  pass after the fix.
- **`tests/rec_wrap.py` (runner level, VICE):**
  - a 17-instruction program checked against an independent raw reader;
  - the WS-B2 reviewer's repro (`move.l #$11223344,$07FFFE`), re-hosted;
  - a 32-byte chunk staged across `$080000`.

  It reports 9 failed checks on 5ca5a20 and 0 after the fix. The staging
  leg passes even on the old core, which confirms that per-byte staging
  cannot straddle.

**On silicon, with the fixed core at 1 MHz:**

| Check | Result |
|---|---|
| Probe (above) | matches VICE |
| `rec_wrap`, new core | **all checks passed** |
| `rec_wrap`, 158e684 core (control) | **fails**, same REU damage as VICE |
| Hand vectors (48 HAND + 15 WRAP) | **63/63** |
| run_program | **48/48 checks** |
| Full golden suite | **5150/5150**, 0 field failures (894.7 s) |
| Teeth, normal build (refill, reset_case, load_chunk, mailbox, foreign) | **5/5 pass** |
| Teeth, `REC_NOINVAL` build | 5/5 fail |
| Teeth, `actrl` source mutant | fails the new `foreign` leg only |

The silicon `REC_NOINVAL` 5/5 failure shows only that the build is broken:
its very first test case already fails, because with every invalidation
removed the first window refill leaves the shadow stale for the rest of the
run. It is **not** per-leg teeth evidence. The per-leg evidence is:

- the VICE mutant matrix (`tests/rec_matrix.py`), where each
  `REC_MUT_*` build and each source mutant kills exactly the legs that
  cover it;
- on silicon, the round-1 `REC_MUT_DIRTY` run (only load_chunk and
  mailbox failed);
- on silicon, the `actrl` run above (only `foreign` failed). Its JSON key
  was mislabelled "noinval" and is now renamed `mutant_actrl`; see
  `build/evidence/ws-a/fix1/hw-actrl-bench/NOTE.txt`.

The `actrl` mutant is a cold path without the ACTRL store. The teeth
scribbler now leaves ACTRL `$80` and LENHI `$01`. The runner-entry legs
cannot catch it, because the window refill rewrites ACTRL itself. The
`foreign` leg can: it follows a foreign DMA with `mem_rec_dirty` and then
a direct `mem_write32`, with no refill in between.

**Cost of the straddle test, same session at "48 MHz"** (ABBA,
5ca5a20 → fixed core, `fixtures/bench/sweep_reclean_fix1_vs_5ca5a20.json`):

| Loop | Change |
|---|---|
| MEM | 70 714 → 69 282 (−2.0%) |
| STREAM | 61 480 → 61 343 (−0.2%) |
| RMW | 58 219 → 57 302 (−1.6%) |
| ALU | +0.6% (noise) |

These deltas are **measured**. The 7-cycle straddle test predicts only
about 0.6–1% on these loops, so part of the loss is something else. Code
layout is a likely contributor, but it was not isolated.

One `eff_mhz` sample in that file is an outlier: base rep 0 reads 43.15 MHz,
while the other three samples read 38.1–38.8. The IPS values are
wall-clock based and unaffected, but the c/instr derived from that sample is
about 12% too high. Use the other rep, or IPS, for that row.

Against the 158e684 windowed base in the main fix session
(`fixtures/bench/sweep_reclean_fix1.json`), the fixed core is:

| Loop | Change |
|---|---|
| MEM | 54 646 → 68 172 (**+24.8%**) |
| STREAM | +14.9% |
| RMW | 40 607 → 58 149 (**+43.2%**) |
| ALU | ±0% |

MEM swings 3–4% between sessions (71.6k in the first session, 68.2k and
69.3k here), so compare only within a session. The main fix session waited
173 s for the lock; the teeth+bench session waited 188 s. The session
config found and restored was again the other lane's (CPU Speed 48, REU
Disabled). The re-read diff was empty.

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

## Wave 4 integrated build on hardware (2026-09-24)

Regression of `wave4/integration` @fac0ce3 (WS-A REC shadow + WS-B1 exec/dos
HLE + WS-B2 file DOS + post-merge round) on the U64E, firmware 3.15, one
DeviceLock hold, 1 MHz for correctness; config snapshotted and restored
exactly (re-read diff empty), C64 reset afterwards.

| Suite | Result |
|---|---|
| Hand vectors (48 HAND + 15 WRAP) | 63/63 |
| Full SingleStepTests suite | 5150/5150, 0 field failures (894.7 s) |
| `run_program` | 48/48 |
| `run_hello` | 13/13 |
| `run_vbcc` (RAMDISK backend) | 299/299 |
| `rec_wrap` | pass |
| `rec_teeth` (refill, reset_case, load_chunk, mailbox, foreign) | 5/5 |

No hardware-vs-VICE divergence. `tests/run_dos.py` has no u64 backend yet
(VICE-only; follow-up). 48 MHz ABBA vs 158e684 in the same session: MEM
54.7k → 68.3k IPS (+24.9%), RMW 40.6k → 58.2k (+43.4%) — within 0.2 pt of
the WS-A fix-round session, so the HLE merges left core speed unchanged.
The ULTDOS (UCI) file backend's first hardware leg is reported separately.

### ULTDOS (UCI) file backend — first hardware leg (2026-09-24)

Passed: `u_main` 56/56, `u_delete` 9/9, throughput legs all ok, FTP read-back
byte-identical. Writes confined to `/USB1/amiga64-test` (absent before,
removed after; `/USB1` top-level listing identical); Command Interface enabled
for the session only; full-config re-read IDENTICAL. Measured: read 61 B/s /
write 114 B/s at 1 MHz, read ~2.7 KB/s / write ~4.8-5.1 KB/s at 48 MHz.
Details and model-vs-silicon notes: `docs/HLE_DOS.md` "Hardware validation".

## Wave 5: run_dos on hardware (W5-T, 2026-09-24)

`tests/run_dos.py --backend u64` ran the HLE DOS file-I/O scenarios on the
U64E, **RAMDISK backend only**, at 1 MHz, in one DeviceLock hold. The build
was `wave5/tools` on base 4ce6031; the `.s` sources are unchanged from the
base.

**Device state has changed since the Wave 4 sections above.** The device
reported fw 3.15, but git **c47155b7**, FPGA 125, core **1.50**. Wave 4
recorded git bce4535e and core 1.4F. So compare with earlier sections only
with that in mind.

**Lock and config:**
- The session waited 1751 s for the lock, behind another project's live
  lane.
- Config as found: Turbo Control `Off`, CPU Speed `" 1"`, REU `Disabled` /
  `2 MB`, Cartridge `""`, Cartridge Preference `Auto`, Badline Timing
  `Enabled`, Bus Operation Mode `Quiet`.
- The session took the full config (every category) and the harness
  `snapshot_state` right after acquiring the lock, and set the CPU speed to
  1 MHz explicitly. No factory baseline was applied on entry.
- At the end it ran `restore_state`, reset the C64 and re-read the full
  config. The diff was **empty (IDENTICAL)**. Nothing was saved to flash.
- There were no storage writes and no UCI traffic.

**Boot residue:** after `run_prg`, `dosio_want_uci` read `000000ff` and
`hle_dos_magic` read `ffffffff`. The driver zeroes both and proves it by
readback. The residue was harmless in this session, but a key or a magic
left over from an earlier session would not be (`docs/HLE_DOS.md`
"Testing"). Upstream, the boot stub now clears BSS (supervisor fix,
`tests/bss_boot.py`), so the zeroing stays only as defence in depth.

| | Checks |
|---|---|
| Hardware, 23 scenarios | **736/736** (about 6 min inside the hold) |
| VICE, same 23 scenarios | 711/711 |
| VICE, full (plus the `selection` leg) | 762/762 |

**How the counts relate:**
- **Hardware:** 711 + 25 = 736. The 25 u64-only checks are 2 zeroing
  readbacks and one "`dosio_backend` = RAMDISK" check after each of the 23
  scenarios.
- **VICE:** 711 + 51 = 762. The 51 are the `selection` leg, which was not
  run in this session. It never touches the device: it boots its own
  fake-UCI build in a local VICE instance, whatever `--backend` says. Since
  fix round 1, `--backend u64` runs it there after the hardware leg, and
  labels its checks `[local VICE]`.
- The final backend check is in both counts. Only its label differs.

**No hardware-vs-VICE divergence.** Every scenario passed on both targets.

Log: `build/evidence/w5-tools/hw/run_dos_u64.log`; config JSON in
`build/evidence/w5-tools/hw/run_dos/`.

Reproduce: `U64_HOST=<ip> python3 -m tests.run_dos --prg
build/<pkg>/runner.prg --backend u64 --out-dir <dir>`.

**Bench tooling fix (VICE only).** `tests/bench/u64_session.py` now boots a
fresh VICE instance for every `Hw.load`. Before, it returned the other
build's labels while VICE kept running the first PRG. That gave false
results on VICE:
- a JAM, with the `REC_NOINVAL` build as base;
- a bogus icount `4294901760`, with the 158e684 build as base.

The old dry run hid this by forcing base = new.

`u64bench.check_loaded` guards against the same class of error on both
backends:
- As first committed (35c1c1e), it compared only 3×8 bytes at three
  labels. Same-size builds fooled it: the `REC_MUT_REFILL` build booted
  and checked as `REC_MUT_RESET` was ACCEPTED, although 214 bytes differ.
- Since fix round 1, it compares the whole PRG image with C64 RAM after
  boot. It masks only the bytes the boot itself rewrites:
  - self-modified operands in the BASIC-stub page's boot code, found by
    scanning that code (wave 5's BSS-clear page operand at `$081E`);
  - on the U64, the dispatcher retarget at `main+7`.
- Measured on VICE right after boot, 35c1c1e had no differing byte, and
  `wave5/integration` had only `$081E`.
- The mutant pair is now REFUSED (evidence in
  `build/evidence/w5-tools/fix1/`).

With the fix, a VICE dry run really runs the base and mutant builds:
- `rec_wrap` fails on 158e684, as it did on silicon;
- the `REC_NOINVAL` teeth legs fail: refill, reset_case, load_chunk and
  foreign. The `boot` leg passes.

### Fix round 1: restore path validated on the U64E (2026-09-25)

The adversarial review of 35c1c1e found that `u64_hold` ran its whole
restore in one `try`. A single failing PUT could skip the per-item
fallback, and the next lock holder would inherit the session's settings.

`restore_config` now runs these steps, and none of them can skip the
rest:
1. `restore_state`, in its own step;
2. two rounds of full re-read, diff and per-item repair. Each step is tried
   twice. An item absent from the snapshot is never written.
3. the C64 reset;
4. a final full re-read and diff.

`restored` is True only for an empty final diff. A `^C` during the restore
is held until the restore has finished and the lock is released, then
re-raised.

A snapshot with an errored or missing category aborts before the body
runs, and the device is left untouched. `Network Password`, the only
STRPASS item, is redacted in the config dumps. `host/tests/test_u64_hold.py`
covers these fault paths (15 cases); 8 mutants of the restore path are
each killed.

**One short validation session.** It used the device only, with no PRG,
no memory writes, no storage writes and no UCI:
- The lock wait was 0 s. The device reported fw 3.15, git c47155b7,
  core 1.50.
- Config as found: another lane's state, Turbo Control `Manual`, CPU Speed
  `48`, REU `Enabled` / `16 MB`, Command Interface `Disabled`. The
  run_dos preflight passed on that snapshot.
- The session set the CPU speed to 48 explicitly and changed `REU Size`
  to `512 KB`.
- The re-read showed exactly one difference, `REU Size`. The speed was
  already 48, so the speed PUT changed nothing. This session did not
  exercise a speed restore.
- After the hold: `restored=True`, `reset_ok=True`, and the full re-read
  diff was **IDENTICAL**.

Log: `build/evidence/w5-tools/hw/validate_hold.log`; JSON in
`build/evidence/w5-tools/hw/validate_hold/`.

**Second validation session (speed restore), 2026-09-25.** The device was
found at Turbo Control `Manual` / CPU Speed `48`, REU `16 MB`. The session
set the speed to 1 MHz explicitly and REU Size to `512 KB`.
- On fw 3.15, `set_speed(1)` switches Turbo Control to `Off` and leaves the
  `CPU Speed` item at `48`. So the dirty re-read showed exactly two
  differences, `Turbo Control` (`Manual` → `Off`) and `REU Size`.
- The script's own post-check expected the item `CPU Speed` to differ, so
  it raised inside the hold. That exercised the body-raises path on
  silicon.
- Result: `restored=True`, `reset_ok=True`, and the full re-read diff was
  **IDENTICAL**.

Log: `build/evidence/w5-tools/hw/validate_hold2.log`; JSON in
`build/evidence/w5-tools/hw/validate_hold2/`.

## CIA clock at turbo (W8-H, 2026-09-30)

Question (S3_PLAN section 5 risk 5): with the U64E at 48 MHz, do the CIAs
count phi2 (~1 MHz) or a turbo-scaled clock?

**Answer: [MEASURED HW] CIA2 counts phi2 at 48 MHz (CIA1 inferred: same
chip clock, not sampled).** Mean ticks per video frame are identical to the
1 MHz control (17095 = 263 lines x 65 cycles), and the means agree within
+-0.01 %. The bench U64E runs the NTSC video standard (KERNAL flag), so the
PAL figure 19656 does not apply. CIA2 does not scale with the turbo clock, so
a time base built on CIA ticks needs no speed correction.

Conditions: U64E `Ultimate-64-Elite-688004`, firmware 3.15 (git c47155b7),
FPGA 125, core 1.50 (`GET /v1/info`, recorded after taking the DeviceLock).
Device found at CPU Speed 48 / Turbo Control Manual (not the " 1" older
sections of this file record); the whole config, all categories, was
snapshotted and read back IDENTICAL after the session. Probe:
`tools/s3probe/cia_rate/` (`cia_rate.s`, driver `cia_rate.py`), uploaded once
with `run_prg_via_sys`, interrupts off. CIA2 timer A (phi2 source, continuous,
latch $FFFF) cascaded into timer B is a 32-bit down counter, sampled with the
CIA2 TOD at raster line 100 at frames 0, 50 ... 250 (the VIC raster runs in
real time, so 50 frames is a fixed span). Three runs per speed, interleaved
1, 48, 1, 48, 1, 48; speed switched by REST while the probe idled.

| Speed | Run | Mean CIA ticks/frame (5 x 50-frame intervals) | TOD tenths over 250 frames (expect 42) | Raster polls/frame |
|---|---|---|---|---|
| 1 MHz | 1 | 17095.0 | 41 | 533 |
| 1 MHz | 2 | 17095.1 | 41 | 535 |
| 1 MHz | 3 | 17095.0 | 41 | 534 |
| 48 MHz | 1 | 17095.0 | 42 | 24061 |
| 48 MHz | 2 | 17095.0 | 41 | 24029 |
| 48 MHz | 3 | 17095.0 | 42 | 24096 |

- [MEASURED HW] At 48 MHz every 50-frame interval reads 17095.0. At 1 MHz a
  few intervals are off by about +-10 ticks per frame (SESSION.log 11:00:41:
  17100.3 and 17089.8, a pair of +-256-tick interval errors). This is
  attributed to a tear in the non-atomic TALO/TAHI read (a TA borrow between
  the two reads; the probe's retry check covers TB only), not to poll
  jitter. It cancels in the run mean.
- [MEASURED HW] The CPU really did run fast: raster polls per frame rose from
  about 534 to about 24060, a factor of 45, so the 48 MHz runs are not a
  speed change that failed to apply. Why the factor is below 48 is not
  measured; this is a sanity check, not a clock measurement.
- [MEASURED HW] TOD advances in real time at both speeds (41-42 tenths per
  250 NTSC frames = 4.18 s), with the 60 Hz divider selected to match the
  KERNAL's NTSC flag. TOD is not turbo-scaled either. A first session
  without the poll counter gave the same table (41/41/41 and 41/41/42).
- [MEASURED VICE] Control for the probe itself: x64sc (NTSC) gives 17095.0
  ticks/frame; its TOD reads 34 tenths (VICE feeds a 50 Hz-derived pin to a
  60 Hz-divider CIA), so VICE's TOD is not a reference for this.
- Check with teeth: the driver run against a deliberately wrong expectation
  (17095 x 48 = 820560 ticks/frame) exits 1 on both the VICE run and the
  saved hardware data (`build/evidence/w8-hw/red/`).
- Scope: CIA2 timers and TOD only; CIA1 was not sampled (same clock tree, not
  measured). Only 1 and 48 MHz were run.

## U64 sampler (Ultimate Audio) (W10-H, 2026-10-01)

Question (WAVE10_BRIEF C4): can the U64E's built-in 8-voice sample player
carry Paula's four sample channels, reading from REU memory, with an
end-of-sample IRQ the 6510 can service?

**Answer: [MEASURED HW] yes for fetch from REU, timing, IRQ and REU
coexistence; the register interface is coarser than Paula's (see "What
Paula needs" below).** The sampler is mapped at `$DF20-$DFFF` by the config
item `C64 and Cartridge Settings / Map Ultimate Audio $DF20-DFFF`
(default Disabled). The item takes effect at once: no C64 reset was needed
(`$DF21` read `$10` about 1 s after the REST write).

Conditions: U64E `Ultimate-64-Elite-688004`, firmware 3.15, **git f2e46946**
(not the c47155b7 of the W8-H and W7 sections: the device's build changed;
the register behaviour below is what this build does), FPGA 125, core 1.50
(`GET /v1/info`, recorded after taking the DeviceLock). NTSC. Config found
at Turbo Manual / CPU Speed 48, REU Enabled 16 MB, Audio Mixer at defaults;
the whole config (all categories) was snapshotted and read back IDENTICAL
after every session. Probe: `tools/s3probe/sampler/` (`sampler.s` is a
script interpreter run by the 6510, `sampler.py` the driver), uploaded once
per session with `run_prg_via_sys`. The host cannot reach the expansion port,
so every `$DFxx` access is made by the 6510; time base = CIA2 timers A+B
(phi2, 32-bit, unscaled by turbo, see "CIA clock at turbo"); KERNAL ROM off,
CIA/VIC interrupt sources off, so only the cartridge IRQ line can enter the
handler. Evidence: `build/evidence/w10-hw/green/` (39/39 checks, exit 0).

### Register map as the C64 sees it

[SOURCE] `fpga/io/sampler/vhdl_source/sampler_regs.vhd`, `sampler_pkg.vhd`,
`slot_server_v4.vhd` (`slot_to_io_bridge`, slot range `$120-$1FF`); every item
marked [MEASURED HW] was exercised by the driver.

- Window `$DF20-$DFFF` = 224 bytes = **7 voices** x 32 bytes at
  `$DF20 + 32*v`. The RTL has 8 voices, but voice 7 would sit at `$E000`:
  [SOURCE] it is not reachable from the C64 (status bit 7 exists, never
  settable from here). Voices 0, 3 and 6 were driven [MEASURED HW].
- **Reads**: any even address in the window returns the IRQ status
  (bit v = voice v); any odd address returns `$10` (version). [MEASURED HW]
  all 112 even bytes `$00` when idle and all 112 odd bytes `$10`, at 1, 8 and
  48 MHz (0 bad bytes of 224 each). With the item Disabled the same window
  reads floating bus (several distinct values, not `$10`). Every other
  register is **write-only**: scribbling `$55` over voices 0 and 3 (control
  excluded) changed no read [MEASURED HW].
- **Writes**, offsets within a voice block (multi-byte fields big-endian):

| Off | Reg | Bits |
|---|---|---|
| +00 | CTRL | b0 enable, b1 repeat, b2 IRQ at end, b5:4 non-zero = 16-bit, b6 interleave |
| +01 | VOL | 6 bits, 0-63 (**64 wraps to 0**: Paula's 64 must be sent as 63) |
| +02 | PAN | 4 bits |
| +04..+07 | START | 26-bit sample-memory address (+04 holds bits 25:24) |
| +09..+0B | LENGTH | 24-bit, in positions (samples) |
| +0E,+0F | RATE | 16-bit |
| +11..+13 | REPEAT A | 24-bit, **relative to START** |
| +15..+17 | REPEAT B | 24-bit, relative to START |
| +1F | CLEAR IRQ | b0 clears this voice; `$FF` clears all voices |

- **REU address**: START = `$1000000 + REU offset` ([SOURCE] REU base
  16 MB). [MEASURED HW] two 4 KB patterns stashed at REU `$0000` and `$1000`
  were heard (UDP audio stream, see below) at the predicted frequencies, so
  a start address in the REU does select REU data. Measured with the REU at
  16 MB (as found); the 512 KB size was not tried.
- **Capability**: not readable from the C64 and not in `/v1/info`. The
  config item exists regardless; the capability shows only as `$DF21 = $10`
  once mapped [MEASURED HW]. ([SOURCE] the firmware maps it only when the FPGA
  capability bit CAPAB_SAMPLER is set.)

### Playback timing [MEASURED HW]

Seven rates (0, 1, 3, 28, 63, 126, 283) x lengths 1024/4096/16384, voice 0,
8-bit, IRQ on, start `$1000000`; time = arm stamp (just after the control
write) to the first poll that sees the status bit.

- **Sample time = (rate + 1) x 160.0 ns + 118 ns** (fit; every rate within
  1 % + 10 ns). That is 6.25 MHz rate ticks plus one fixed 120 ns fetch step:
  rate 0 = 277 ns (3.6 MHz), rate 126 = 20.44 us, rate 283 = 45.56 us.
  [SOURCE] the 160 ns is 8 voice slots x 1/50 MHz, independent of the 66.67
  MHz core clock; the 120 ns is one 8-clock voice slot at 66.67 MHz.
- **The end status rises after `length` samples**: time to end =
  length x sample time with a residual under 27 us (stamp jitter, section
  below), e.g. 83.67 ms (end stamp 84873 us minus the 1201 us arm stamp) for rate 126 x 4096; the law gives 83.71 ms.
- Audio capture agrees: pattern A (period 64 samples, rate 283) played at
  343.0 Hz against 343.0 Hz predicted; pattern B (period 32) at 686.1 Hz
  against 685.9. The prediction uses the *fitted* law (slope and constant
  from the poll timing), so it checks consistency of two instruments, not the
  law itself.
- The constant is checked against an independent expectation (120 ns +-15 ns,
  one 8-clock slot, `sampler2.vhd` `fetch1`), not only against the fit: the
  fit alone would pass any constant. Red: the saved data with the expectation
  set to 400 ns fails, and a reviewer's mutant dataset with +0.4 us per
  sample (fit constant 518 ns) fails (`build/evidence/w10-hw/fix1_red_*.txt`).
- **Speed**: register writes land at 8 and 48 MHz: play time of rate 126 x
  4096 was 83.72 ms at 1, 8 and 48 MHz (within 0.1 %), and the window read
  back clean at both. The UCI-style fence (~38 us between accesses) was not
  needed for this peripheral at the access pattern used.

### What Paula needs, as measured

- **Rate**: Paula's sample time is PERIOD x 281.9 ns (PAL/NTSC colour
  clocks differ by 0.9 %), so `rate + 1 = (PERIOD x 281.9 ns - 118 ns) /
  160 ns`; rounding to an integer is at most 80 ns, 0.25 % at PERIOD 113 (the
  Amiga minimum). [DERIVED]
- **Volume/gain**: 6-bit volume scales linearly: volume 8 gave 0.127 of the
  volume-63 level (8/63 = 0.127). At volume 63 and centre pan an 8-bit
  sample LSB is about 137 stream LSB (DC step 4410 for sample `$20`; RMS
  8777 for a +-64 square). [MEASURED HW, stream is post mixer]
- **Pan**: hard left/right at the voice (pan 0 / 15) [SOURCE]. The stream
  after the Audio Mixer showed pan 0 as L 6638 / R 2139 and pan 15 mirrored:
  the leak is likely the mixer (the mixer was not changed; `Pan Sampler L = Left 3`, `Pan Sampler R = Right
  3` at default), not the voice. A hard-panned Paula would need those two
  mixer items changed, which is a config change this probe did not make.
- **Loops**: [MEASURED HW] with REPEAT B inside the sample and ENABLE+REPEAT
  the voice loops for ever and raises **no status and no IRQ** at the loop
  point (none in 17 sample times); REPEAT B beyond LENGTH makes it end
  normally and raise the status. Paula's per-loop audio interrupt cannot be
  taken from this IRQ; it would have to be timed from the 6510 side
  (sample time x length).
- **A stopped voice holds its last sample as a DC level** until it is
  replayed ([MEASURED HW] DC step 4410 after a 16-sample `$20` play, back
  to 0 after a zero play; [SOURCE] `sample_out` is only reset when a voice
  restarts). Channels that Paula leaves with DMA off need zeros played.
- **Capacity**: 7 usable voices, so four Paula channels fit; 16-bit and
  interleave modes were not measured.

### End-of-sample IRQ [MEASURED HW]

- Reaches the **C64 IRQ line** (not NMI): the 6510 entered the `$FFFE`
  handler with the status bit set, no spurious entry in any run.
- **Level**, held until cleared: with no acknowledge the handler re-entered
  50 times to the test limit, and once more after a SEI/CLI (count 51).
  Reading the status (the handler read `$DF20` every time) does **not**
  acknowledge it.
- **Acknowledge**: write bit 0 = 1 to the voice's own clear register
  (`$DF20 + 32*v + $1F`) -> count 1, status bit gone; write `$FF` to any
  clear register -> count 1 (clears all). Writing 0 does not clear; writing
  1 to another voice's clear register does not clear (count 51). Voice 3
  raised status `$08`, voice 6 `$40`, each acknowledged by its own register.
- CTRL bit 2 off: no status bit, no IRQ.
- **Latency**: the handler's first timer stamp trails the status-poll stamp
  of the same kind of event by +28.6 us on average (n = 10, -39 to +107 us);
  that is inside the method's resolution (poll loop about 14 us per pass,
  7-cycle IRQ entry plus about 40 cycles to the stamp, +-40 us arm-stamp
  jitter from VIC badlines), so no latency beyond the 6510's own is shown.
  The two runs per pair are separate plays, not one event.

### Coexistence with REU DMA [MEASURED HW]

- **Load tested: one voice at rate 126**, about 49 KB/s of sample fetches
  (one byte per 20.4 us). One voice at rate 0 would be about 3.6 MB/s, and
  four Paula channels at typical periods about 0.1-0.2 MB/s; neither was
  tried. At this load **no effect was detected**.
- A 1 KB REU fetch took 1409.7 us alone (n = 8, range 1332-1465 us, a 9.4 %
  spread) and 1420.8 us with a voice playing (mean of 40, max 1454.9): +0.8 %,
  which is inside that spread, so it is noise, not a cost. The driver's
  tolerance is 10 %, just above the spread.
- The voice's play time (end stamp minus arm stamp) was 83672 us alone and
  83710 us with 40 DMAs in flight (arm stamp of the second run not recorded;
  the first's 1201 us is used): +38 us = 0.05 %, inside the +-40 us stamp
  jitter. The driver's tolerance is 0.5 %.
- The fetched block was bit-exact (stash then fetch of a 1 KB pattern during
  playback); every DMA ended inside the playback window.
- An end-of-sample IRQ that became due during a 30-DMA burst was delivered
  once (count 1) and acknowledged normally.
- Not shown: sample under-runs during DMA. The stream was not captured in
  that window; the only audio captures ran without DMA.

### Audio-stream capture (what was and was not measured)

The U64 UDP audio stream (`stream_audio_start`) was captured on the host and
used for frequency, level and DC checks only; no audio quality was judged.
Before the voices were zeroed the stream carried a large constant offset
(-32768, +32767 and -4482 in the three earlier runs, `green_run1..3`): that was
the held last sample of voices used by the IRQ tests, which is the DC-hold
finding above. Zeroing every voice gave an offset of -2.

### Checks with teeth

- The driver on VICE (no sampler there): the interpreter self-test passes,
  the sampler checks FAIL, exit 1 (`build/evidence/w10-hw/red/vice/`).
- The driver on the U64E with deliberately wrong expectations (version `$11`,
  tick 320 ns) failed both of those checks before the real run (`red/hw/`;
  that early driver also failed checks that were its own defects, listed
  below); the saved final data judged
  with the same wrong expectations (and a 0.01 % DMA tolerance) exits 1 with
  4 FAILs (`red/analyze.txt`).
- Defects the first hardware runs caught in the driver itself (the first
  run's log was overwritten; later ones are in `green_run1..4`): a poll
  timeout of 4096 iterations, REPEAT A/B given as absolute rather than
  relative positions, a level-IRQ count that forgot the re-entry after
  SEI/CLI, play times that included register-write time (it differs by
  speed), a latency comparison that mixed arm stamps, and a per-rate slope
  tolerance tighter than the stamp jitter at rate 0.

### Scope and limits

Single session at 1 MHz (8 and 48 MHz for the window and play-time checks),
NTSC, REU 16 MB, one sampler in voice 0 for timing, voices 3 and 6 for IRQ.
Firmware hazard (not measured): `sampler2.vhd:145` tests `position =
repeat_b` before `position = length` (line 149), so with REPEAT B equal to
LENGTH and repeat off, the voice is not shown to finish or raise its IRQ
(the position == repeat_b branch only jumps when enable and repeat are both
set, and otherwise falls through without finishing). Keep REPEAT B different
from LENGTH when relying on the end IRQ.
Wrong-expectation runs against the FINAL driver were done with `--analyze` on
the saved data, not repeated on hardware (the hardware red run used the
earlier driver).
Not measured: 16-bit and interleave modes, voice 7, more than one voice
playing at once, REU smaller than 16 MB, the effect of the mixer's pan items,
sample content beyond frequency, and whether playing during heavy REU DMA
under-runs audibly. Evidence is in `build/evidence/` (ignored by git); the
reproduction is `U64_HOST=10.43.23.81 U64_BASELINE_ON_ENTRY=0 python3
tools/s3probe/sampler/sampler.py --backend u64 --out DIR`.

## Reading a running C64's RAM over REST at 48 MHz (W11-A, 2026-10-06)

Question (W11 review, HIGH-2): the first U64E AROS boots captured the
guest's serial ring (`chip_ser`, 256 B, plus the u16 count `chip_sern`)
with NULs and foreign bytes. Is the U64's host read path corrupting bytes
at turbo, or was the capture racing the running guest?

**Path.** `c64_test_harness.read_bytes` → `Ultimate64Transport.read_memory`
→ `Ultimate64Client.read_mem` = REST `GET /v1/machine:readmem` (a DMA read
of C64 memory while the 6510 keeps running); `read_bytes` splits a read
above 256 bytes into 256-byte requests. No debug or data stream, no UCI,
no SocketDMA is involved in any read (SocketDMA `REUWRITE` is used only to
stage the ROM into the REU, before the boot).

**Experiment** [MEASURED HW] (U64E fw 3.15 git 3a1ff9ff, fpga 126, core
1.50; one DeviceLock hold, config restored identical; host on Wi-Fi;
`build/evidence/w11-aros/u64/ringexp/ringexp.json`). A 6502-only writer
(no 68k) uses the `chip_ser` protocol on the runner's own `chip_ser`: it
stores byte k at slot k & $FF, value (11 + 37 k + (k >> 8)) & $FF (so a
previous lap's byte is distinguishable), then bumps the count, ~370
bytes/s (the boot's serial burst rate). The host polls for 40 s per row
with two protocols and checks every slot whose latest write is final
for that snapshot:

| MHz | protocol | polls | slots checked | NUL | stale (prev. lap) | shifted | other |
|---:|---|---:|---:|---:|---:|---:|---:|
| 48 | count, ring (256 B), count | 291 | 62,936 | 0 | 0 | 0 | 0 |
| 48 | ring + count in one 260-B read (round 0) | 296 | 75,776 | 37 | 7,555 | 0 | 0 |
| 1 | count, ring, count | 281 | 62,131 | 0 | 0 | 0 | 0 |
| 1 | one 260-B read (round 0) | 310 | 79,360 | 31 | 7,410 | 0 | 0 |
| 48 | count, ring, count (again) | 307 | 68,734 | 0 | 0 | 0 | 0 |
| 48 | one 260-B read (again) | 313 | 80,128 | 27 | 7,556 | 0 | 0 |

**Result: the single-read failures are the race.** 0 wrong bytes in
193,801 checked slots read by the bracketed protocol, at 48 MHz and at
1 MHz. The round-0 protocol (the count read last, after the ring) gets
~10% of its slots wrong at BOTH speeds, all NUL (first lap) or
previous-lap bytes: it counts bytes the guest wrote after the ring was
read. No shifted or foreign byte appeared in either protocol.

**Not explained, and a read-path effect is NOT ruled out** (rev-aros
round 2). The s = 1 boot of `u64/boot_48_low.json` read the count FIRST
(one count read, then the ring; the code that wrote that file is the one
that added `serial_matches_model`), yet slot 130 came back as '\n' =
the model's serial byte 386, i.e. that slot's second-lap byte of the
PREVIOUS boot (s = 2, same day, a separate hold). That byte had been
cleared twice before the read: by the runner's BSS clear at `run_prg`
and by `chip_reset` at the start of the boot, ~20 s earlier. A slot the
count already covered therefore read a stale value from before both
clears, during a REU-DMA-heavy AROS boot at 48 MHz. The s = 5 capture
(one byte missing at slot 130) may be the same effect. A second
experiment repeated the ring under REU-DMA load [MEASURED HW, same
device and firmware, `u64/ringexp-reu/`]: the 6502 writer also does 16
REU transfers of 256 bytes per ring byte (bank `$40`), the ring was
pre-filled with `$EE` and cleared by the writer, 48 MHz, 45 s per row:

| REU load | protocol | slots checked | NUL | `$EE` (pre-clear) | stale | other |
|---|---|---:|---:|---:|---:|---:|
| 16 DMAs/byte | count, ring, count | 70,511 | 0 | 0 | 0 | 0 |
| 16 DMAs/byte | one 260-B read | 80,128 | 24 | 0 | 5,693 | 0 |
| none | count, ring, count | 64,997 | 0 | 0 | 0 | 0 |
| none | one 260-B read | 81,408 | 37 | 0 | 8,631 | 0 |
| 16 DMAs/byte | count, ring, count | 79,638 | 0 | 0 | 0 | 0 |
| 16 DMAs/byte | one 260-B read | 89,856 | 17 | 0 | 5,641 | 0 |

Clean again: this load (large REU blocks, a quiet 6510) did not
reproduce it. The AROS boot's load differs (millions of short REU DMAs
from the memory API and the code window, the REC reprogrammed
constantly), and the stale value crossed a `run_prg` and a C64 reset.
So the user's turbo/read-path hypothesis stays OPEN: a stale DMA read of
C64 RAM under that load. The capture now guards against it:
`tests/aros_boot.py` zeroes the ring and its count from the host before
every boot and reads them back as zero (a stale read then shows as a NUL
or as a pre-clear byte), reads count, ring, count, takes only the slots
the bracket proves final, and compares every captured byte with the boot
model, so any wrong byte fails the leg. Every U64E boot since (16, fix
rounds 1 and 2) captured all 416 bytes equal to the model.

**Turbo switches and `reboot(client)`.** The harness README requires
`reboot(client)` when switching turbo speeds between REU-heavy workloads
(stale DMA state). No AROS session rebooted between holds: each hold sets
1 MHz, `run_prg`s the runner, sets 48 MHz, boots (REU-heavy), and the hold's
restore resets the C64. The corrupt captures and the clean ones (s = 2,
and every bracketed run since) followed the same switch pattern, so the
switches do not discriminate them; the experiment above (which switches
48 → 1 → 48 live) read clean. The UCI turbo fence (`turbo_safe`) is not
relevant: nothing here touches `$DF1C-$DF1F` (the runner's U64 mailbox
dispatcher is plain RAM at `$CE00`). Harness-reserved RAM `$C400-$C87D`
(turbo UCI routines, `docs/memory_safety.md`) overlaps the runner's
`buf_case` only above `$C400`; the AROS driver (`$C000-$C181`), the
experiment's writer (`$C200-$C245`) and the boot's P/R blocks
(`$C900-$C96F`) stay clear of it.
