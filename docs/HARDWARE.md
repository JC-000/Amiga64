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
