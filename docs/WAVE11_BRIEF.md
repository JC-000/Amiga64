# Wave 11 Brief — S3.6 blitter v0 ∥ S3.7 AROS machine profile + boot milestones

Supervisor-authored, **binding**. `docs/WAVE4_BRIEF.md` C1–C5 still apply
(**C5 red → green, adversarial**; "Local model": budget ≥ 20k tokens, never
an authority). `docs/CONTRACTS.md` holds for the files it covers, in
particular "I/O trap ($BF/$DF)", "Machine loop (S3.2)", "Event seam",
"Interrupt delivery (S3.3)", "CIA-A/B (S3.4)", "Copper and display state
(S3.5)" and "Display refresh (S3.5)". The design is `docs/S3_PLAN.md` §3.4,
§3.5 and §4 "S3.6"/"S3.7". The reference semantics are `host/chipset_ref.py`
(`Blitter`, `Chipset`, `CIA`) and the boot model
`tools/s3probe/aros_boot_model.py`. Where this brief is silent, S3_PLAN
governs. Changing a contract needs supervisor sign-off: report it, don't
work around it.

Base: `wave11/integration` (= main 7ecc2cd + this brief). Work ONLY in your
supervisor-created worktree. **Never run mutating git.** Evidence goes to
`build/evidence/<pkg>/{red,green,mutants,...}` with a running `PROGRESS.md`.
Scratch goes only in `<scratchpad>/<pkg>/`. Run `make vectors` and
`python3 -m host.handvec --out build/vectors-hand` before any gate.

## Operating rules (all agents; from Waves 7–10)

- **Devices only through c64-test-harness.** VICE, the U64E (10.43.23.81)
  and the C64U are started, driven and stopped only through the harness.
  **Never `pkill`, `killall`, or launch `x64sc` yourself.** To stop your own
  stuck run, use the harness's teardown or the exact PID the harness gave
  you (record it at launch). Other sessions use these devices too.
- **The U64E is shared through the harness queue (DeviceLock), not owned.**
  Slow legs are NOT "nightly only": queue them through the harness whenever
  you need them. `/v1/info` first; take the DeviceLock; snapshot the config
  and restore it at the end (even on failure); write
  `build/evidence/<pkg>/SESSION.log` as you go. Honour holds announced by
  other sessions (c64-https). Blingboard: tap SHIFT before any key test.
- **Suite shape (user rule).** No test suite may consist of large numbers
  of discrete shell commands or subprocesses (per-case cl65/x64sc/python
  invocations). Batch: one build, one emulator session, many cases inside
  it; host tests in-process with pytest. Mutant runs build each mutant
  once and run all its legs in one session; keep **≤ 10 mutants per
  package**. A runner must finish in minutes, not hours; state its
  wall-clock in your report.
- **Builds are serial.** cl65 objects land in `runtime/*.o` in the source
  tree, so never run two builds (including `--mutants`, `--teeth` or
  `--build-shim` runners) at once in one worktree.
- **Poll your own DONE markers.** Run long jobs in the background with a
  marker file, and poll it and the output timestamps yourself. Completion
  notices are sometimes lost. Keep PROGRESS.md current.
- **Context economy.** Read this brief, the cited sections, the done-gate
  (`.serena/memories/task_completion.md` in the main checkout) and the
  source you change. Use grep and line ranges.
- **Clean room.** No AROS source, ROM bytes or disassembly in the repo or
  in your reasoning; don't read AROS sources. The ROM is a black box loaded
  from `~/.cache/amiga64/aros/` (tools/aros/fetch.sh) at test time only.
  Observing its behaviour (serial output, registers it touches, the boot
  model's traces) is fine.

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W11-B** | `w11-blt` | new `runtime/ops/blt.s`; in `runtime/ops/chip.s` only: the blitter register write paths ($040–$074, BLTSIZE $058 starts a blit), the DMACONR ($002) read value, and the INTREQ BLIT raise (via the existing SET path + `mach_ichk`); **new** claims labelled `; W11-B blt`; registration of `blt.s` in `Makefile`/`host/rescheck.py`/`tests.run_c2v.makefile_sources()` (those lines only; link order: after `cop.s`, before `disp.s`); new `tests/run_blt.py`, `tests/blt/`, `host/tests/test_blt*.py`; blit cases added to the FS-UAE oracle (`tests/chip_oracle*`, additive); `host/chipset_ref.py` (**additive only**); CONTRACTS new section "Blitter (S3.6)"; `docs/PERF.md` new section "Blitter" |
| **W11-A** | `w11-aros` | the machine-mode profile: new `runtime/rc/machine.inc` (+ `rescheck` per profile), and in `runtime/rc/guest.inc` only the shrink of the HLE `free` regions to exclude $BF0000–$BFFFFF and $DF0000–$DFFFFF; a new machine-mode boot entry (new `runtime/ops/mboot.s` or a new runner under `tests/runner/`, your choice, documented); in `runtime/ops/mach.s` only the two backlog fixes below (C4), labelled `; W11-A`; in `runtime/ops/disp.s` only the pixel-epoch read (C3); new `tests/aros_boot.py`, `tests/aros/`, `host/tests/test_aros*.py`, `host/machine.py` (or similar host loader); CONTRACTS new section "Machine profile and AROS boot (S3.7)"; `docs/PERF.md` new section "AROS boot" |

Frozen (supervisor only): `runtime/core.s`, `cia.s`, `cop.s`, `c2v.s`,
`hle*.s`, `dosio_*.s`, `g*.s`, the rest of `mach.s`, `chip.s` and `disp.s`,
`host/image.py`, `host/gen_dispatch.py`, `runtime/macros.inc`,
`runtime/rc/ram.inc` and `zp.inc` except NEW claims labelled with your
package, `tests/rec_gate.sh`, the existing runners, every existing claim
line, all brief files, `docs/ARCHITECTURE.md`.

## C1 — blitter semantics (W11-B)

- `blt.s` = `chipset_ref.Blitter`, synchronous: a BLTSIZE write runs the
  whole blit inside the trap. Normal mode: channels A/B/C/D (BLTCON0
  bits 11–8), minterm LF, ASH/BSH shifts, AFWM/ALWM, the four modulos,
  descending mode (BLTCON1 bit 1), inclusive/exclusive fill with FCI
  (BLTCON1 bits 3/4/2), BLTxDAT used when a channel's DMA is off, the
  BLTBDAT pre-shift rule (`write_bdat`), BZERO. Size: OCS BLTSIZE
  (h = bits 15–6, 0 = 1024; w = bits 5–0, 0 = 64).
- **Line mode** (BLTCON1 bit 0): logged and counted (`blt_nline`), memory
  untouched, completion as for a normal blit. Report how often the boot
  model uses it.
- Completion: INTREQ BLIT (bit 6) is set (as a Paula SET write, so
  `mach_hold`/`mach_ichk` rules apply). DMACONR returns
  `(DMACON & $07FF) | BZERO`, BBUSY never set (S3_PLAN §3.4 table; DMACON
  comes from cop.s's shadow — read it, don't duplicate it).
- **Memory.** Every channel address is a guest chip address masked to the
  chip-RAM range the model uses; a blit can never read or write REU banks
  $BF/$DF (host-private data lives there: the c2v LUT at $DF0000). Prove
  it with a leg. Use row-wise REU DMA (fetch a row of A/B/C, compute,
  store a row of D), not per-word calls, per S3_PLAN §3.5.
- **Pixel epoch (C3):** after every blit with D enabled (normal mode),
  store an incremented private counter to `disp_st+105`.
- **Red:** a guest blit fixture (copy, cookie-cut `$CA` with shifts and
  masks, clear, descending shifted copy, a fill) leaves the destination
  unchanged on base. **Green:** the destination equals the model and the
  FS-UAE oracle dump, case by case. **Teeth (≤ 10 mutants):** minterm bit
  order, descending addressing, fill carry/FCI, AFWM/ALWM swap, a modulo
  ignored, the B pre-shift, BZERO, the chip-address mask.
- **Perf:** words/s on VICE (1 MHz) and on the U64E at 48 MHz (queue it),
  against the §3.5 assumption of ~3.3 µs/word; per-blit fixed cost too.

## C2 — machine profile and boot (W11-A)

- **Profile.** `runtime/rc/machine.inc`: guest chip RAM from $000000 (size
  per the boot model), the ROM at $F80000–$FFFFFF and the extension at
  $E00000–$E7FFFF (`aros-rom.bin`, `aros-ext.bin`; check the cache's
  manifest/hash), `io` at $BF and $DF, no HLE tables or heap. `rescheck`
  checks each profile on its own.
- **Load.** A host loader stages the ROM files from the cache into the REU
  at test time (VICE: harness memory/REU writes; U64E: harness DMA/UCI),
  never into the repo or a committed artifact. Skips cleanly (exit 0,
  SKIP line) when the cache is absent, like `tests/aros_oracle.py`.
- **Boot entry.** `st_exc` = 1; reset: SSP/PC from the ROM's reset longs as
  the boot model does (document the overlay choice); init calls
  `chip_reset` AND `cop_reset` AND `cia_reset`, `mach_init`, `disp_init`;
  CIA-A PRA inputs read $EB; then `mach_run` until a host stop condition.
  Choose `mach_s` by measurement (C4) and document it.
- **Milestones** (`tests/aros_boot.py`; runs on VICE warp and on the U64E,
  queued via the harness; part of the wave's gate, run on demand):
  1. serial `ROMInfo: 1MiB ROM detected` (chip_ser ring);
  2. serial `romtaginit done`;
  3. the no-media ("insert disk") screen: the VIC conversion hashed against
     the model's frame converted by the same kernel on the host
     (`host/c2v_ref.py`). It needs the blitter: develop 1–2 now; the
     supervisor merges W11-B into your worktree when it lands, then you
     finish 3.
  **Red:** each milestone fails before its enabling step (record which).
  Record wall-clock to each milestone on both targets and compare with
  S3_PLAN §3.5 (≈ 70–95 s at 48 MHz [EST]).

## C3 — pixel epoch (shared, supervisor-defined)

`disp_st+105` (u8, appended to the C1 block of CONTRACTS "Copper and
display state") = the pixel epoch. Writers of guest pixel memory store a
new value there after writing (blt.s: every D-enabled blit). `disp.s`
(W11-A) treats a changed epoch like a changed field: a conversion is due,
subject to the same DISP_N budget inside `mach_run`; outside `mach_run` it
converts at once. CPU writes to bitplanes are still untracked (backlog);
if milestone 3 needs them, report it — don't add a write hook to core.s.

## C4 — machine-loop backlog fixes (W11-A)

1. **Starvation at s = 0**: the machine loop starves the guest. Fix it or
   prove AROS needs s ≥ 1, with numbers; either way pick `mach_s` for the
   boot profile by measurement (guest instructions per real second, the
   frame rate the ROM sees).
2. **`mach_wk` ignores `mach_fhook`**: the copper/display hook must run on
   the wake/idle path too. Red first.
Both keep run_mach/run_irq/run_cia/run_cop/run_pf exactly green.

## C5 — gates (both packages)

`core.s` byte-identical modulo relocation (a real link-swap proof);
`perf_cycles --base <base prg> --max-delta 0`; rec_budget identical; the
full gate at exact counts (golden 5129, hand 63, super 678, host-test 1358
+ your new tests, dos 762, vbcc 433, con 280, smc 42, run_chip 8, run_c2v +
`--teeth` 6/6, chip_oracle 69+1 (+ new blit cases), run_mach + `--mutants`
18/18, run_irq + `--mutants` 29/29, run_cia live + host + both
`--mutants`, run_cop + `--mutants` 27/27, run_pf + `--teeth` 11/11), plus
your new suite with its mutants. VICE for the gate; the U64E legs as above.

## Merge order and review

W11-B first, then the supervisor merges `wave11/integration` into W11-A's
branch for milestone 3, then W11-A. After each package's final report, a
**fresh adversarial reviewer** attacks it; fix rounds follow until it says
MERGE; the supervisor runs its own serial full gate on the merged tree
before each merge and before the PR. The user merges the PR.

## Report

What you built; red → green evidence paths; mutation results; exact gate
counts; runner wall-clocks; contract deviations (none expected); open
risks; local-model use; changed files vs your base. Under 60 lines, as a
message.

## Addendum 1 (2026-10-06): banked code area BCODE

The base runner leaves 1430 B free in MAIN, and blt.s needs about 2.1 KB.
Supervisor decision (W11-B's option 1): a new ld65 memory area `BHI` at
$A000–$B5FF, under BASIC ROM, with segments `BCODE` (and optional `BBSS`), and MAIN `fill = yes`
so the PRG stays contiguous. ram.inc: `core $A000-$B5FF` BCODE plus W11-B's
`blt $B600-$B7FF` row buffers. W11-B owns this one c64.cfg/ram.inc edit.
BCODE is shared: code there runs only with BASIC banked out and is entered
through a MAIN stub that restores the caller's $01. W11-A puts its bulk
boot code there after the blitter merges. Existing module addresses must
not move (map diff, link-swap proof, perf_cycles delta 0).

## Addendum 2 (2026-10-06): BLTBDAT model fix

FS-UAE (3 new oracle DIFFs) shows a CPU BLTBDAT write is shifted on its
own: `(v << BSH) & $FFFF` descending, `v >> BSH` ascending, never combined
with the previous B word. Supervisor sign-off for a one-off non-additive
edit to `host/chipset_ref.Blitter.write_bdat` and the chip_oracle mutant
`bdat_write_not_shifted`; the boot model's counts must be re-shown
unchanged. chip_oracle's exact counts change accordingly.
