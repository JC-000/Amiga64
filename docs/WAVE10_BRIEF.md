# Wave 10 Brief — S3.5: copper + display → VIC (M3), and the U64 sampler probe

Supervisor-authored, **binding**. `docs/WAVE4_BRIEF.md` C1–C5 still apply
(**C5 red → green, adversarial**; "Local model": budget ≥ 20k tokens, never
an authority). `docs/CONTRACTS.md` holds for the files it covers, in
particular "Machine loop (S3.2)", "Event seam (S3.3/S3.4)", "Interrupt
delivery (S3.3)", "CIA-A/B (S3.4)" and "I/O trap ($BF/$DF)". The design is
`docs/S3_PLAN.md` §2 and §4 "S3.5"; the converter is `docs/C2V.md`. The
reference semantics are `host/chipset_ref.py` (`Copper`, `Chipset`) and
`host/c2v_ref.py`. Where this brief is silent, S3_PLAN governs. Changing a
contract needs supervisor sign-off: report it, don't work around it.

Base: `wave10/integration` (= main 29d750d + this brief). Work ONLY in your
supervisor-created worktree. **Never run mutating git.** Evidence goes to
`build/evidence/<pkg>/{red,green,mutants,...}` with a running `PROGRESS.md`.
Scratch goes only in `<scratchpad>/<pkg>/`. Run `make vectors` and
`python3 -m host.handvec --out build/vectors-hand` before any gate.

## Operating rules (all agents; from Waves 7–9)

- **Devices only through c64-test-harness.** VICE, the U64E and the C64U are
  started, driven and stopped only through the harness. **Never `pkill`,
  `killall`, or launch `x64sc` yourself.** To stop your own stuck run,
  use the harness's teardown or the exact PID the harness gave you.
- **Builds are serial.** cl65 objects land in `runtime/*.o` in the source
  tree, so never run two builds (including `--mutants`, `--teeth` or
  `--build-shim` runners) at once in one worktree.
- **Poll your own DONE markers.** Run long jobs in the background with a
  marker file, and poll it and the output timestamps yourself. Completion
  notices are sometimes lost. Keep PROGRESS.md current.
- **Context economy.** Read this brief, the cited sections, the
  done-gate (`.serena/memories/task_completion.md` in the main checkout)
  and the source you change. Use grep and line ranges.
- **Clean room.** No AROS source, ROM bytes or disassembly; don't read
  AROS sources.

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W10-P** | `w10-cop` | new `runtime/ops/cop.s` (the copper); in `runtime/ops/chip.s` the display/copper register write paths only (COP1LC/COP2LC/COPJMP1/COPJMP2, BPLCON0–2, BPLxPT, BPL1MOD/BPL2MOD, DIWSTRT/DIWSTOP, DDFSTRT/DDFSTOP, COLOR00–31, and DMACON's side effects for COPEN/BPLEN/DMAEN); in `runtime/ops/mach.s` only the **frame hook** (C2); **new** claims labelled `; W10-P cop`, including the display-state block (C1); registration of `cop.s` in `Makefile`/`host/rescheck.py`/`tests.run_c2v.makefile_sources()` (those lines only; link order: before `mach.s`); new `tests/run_cop.py`, `tests/cop/`, `host/tests/test_cop*.py`; `host/chipset_ref.py` (**additive only**); CONTRACTS new section "Copper and display state (S3.5)" |
| **W10-V** | `w10-disp` | new `runtime/ops/disp.s` (the refresh driver); `runtime/ops/c2v.s` (only as the driver needs; the kernels stay bit-identical unless a finding demands otherwise); **new** claims labelled `; W10-V disp`; registration of `disp.s` (as above; link order: after `c2v.s`, before `mach.s`); new `fixtures/c/pf_static.c`; new `tests/run_pf.py`, `tests/pf/`, `host/tests/test_disp*.py`; `host/c2v_ref.py` (**additive only**); `docs/C2V.md` (new sections only); CONTRACTS new section "Display refresh (S3.5)"; `docs/PERF.md` (new section) |
| **W10-H** | `w10-hw` | new `tools/s3probe/sampler/`; `docs/HARDWARE.md` new section "U64 sampler (Ultimate Audio)". The **only** agent on the U64E this wave. |

Frozen (supervisor only): `runtime/core.s`, `cia.s`, `hle*.s`, `dosio_*.s`,
`g*.s`, the rest of `mach.s` and `chip.s`, `host/image.py`,
`host/gen_dispatch.py`, `runtime/macros.inc`, `runtime/rc/guest.inc`,
`tests/rec_gate.sh`, the runners, every existing claim line, all brief
files, `docs/ARCHITECTURE.md` (the supervisor updates the audio line).

## C1 — the display-state block (W10-P writes, W10-V reads)

A claimed RAM block `disp_st`, little-endian, with this layout (W10-P
may append fields but must not reorder; document it in CONTRACTS):

| Off | Field | Meaning |
|---|---|---|
| +0 | `gen` u8 | incremented on every change of any field below (wraps) |
| +1 | `dmacon` u16 | the DMACON shadow (DMAEN/BPLEN/COPEN gate the display) |
| +3 | `bplcon0` u16, `bplcon1` u16, `bplcon2` u16 | |
| +9 | `bplpt[6]` 6 × u24 | plane pointers (guest addresses) as of the last copper run or CPU write |
| +27 | `bpl1mod` s16, `bpl2mod` s16 | |
| +31 | `diwstrt`, `diwstop`, `ddfstrt`, `ddfstop` u16 | |
| +39 | `color[32]` 32 × u16 | |

W10-V reads it only through `gen`: it copies the fields it needs when `gen`
changed. The writer bumps `gen` after the fields are written. Before W10-P
lands, W10-V tests against a host-filled block (a shim in its own
`tests/pf/`, never linked into `runtime/`).

## C2 — the frame hook and the copper (W10-P)

- `mach.s` gets ONE extra per-frame vector, `mach_fhook` (2 bytes, high byte
  0 = none). It is called right after slot 0's VERTB callback (the CIA's),
  once per VERTB, in the service pass, under the same rules as callbacks.
  `cop.s` registers `cop_frame` there at `cop_reset`. Nothing else in
  `mach.s` changes. `cop.s` also owns the `cop_dhook` vector (C3).
- Copper semantics = `chipset_ref.Copper`: MOVE executes (the danger rule:
  registers below COPCON's threshold are refused as the model does), WAIT
  is satisfied at once (once per frame, beam timing not modelled), SKIP is
  counted and treated as a no-op (the non-strict model), and the list ends
  at `$FFFF,$FFFE` or a step limit. `cop_frame` restarts from COP1LC
  every frame when COPEN and DMAEN are set.
- A COPJMP1/COPJMP2 strobe from the CPU runs the copper **synchronously in
  the trap** (so HLE runners that never call `mach_run` still see the
  display state), bounded by the same step limit.
- Every display-register write, by the CPU or the copper, updates `disp_st`
  and bumps `gen`.
- **Red:** a guest builds a copper list that sets COLOR00/BPLCON0/BPL1PT,
  strobes COPJMP1, and reads back through `disp_st`; on base nothing
  changes. **Teeth:** a mutant per rule (WAIT not skipped, the danger
  threshold, the end-of-list marker, COPJMP2 using COP1LC, gen not bumped,
  the frame hook not called).

## C3 — the refresh driver and the fixture (W10-V)

- **The link (decided):** W10-P owns a RAM vector `cop_dhook` (2 bytes,
  high byte 0 = none). `cop_frame` calls it at its end every frame, and a
  synchronous COPJMP run calls it after the copper stops. W10-V's
  `disp_init` stores `disp_frame` there. Neither package links against the
  other. W10-P tests the vector with a fake target.
- `disp_frame`: when `gen` changed, decide the mode
  (2-plane lores → the exact multicolour kernel; 4-plane hires → the v0
  silhouette kernel; anything else → blank + log), build the palette LUT
  from `color[]`, and convert with the existing c2v kernels (REU fetch of
  the planes, per-line row advance, the screen-fill protocol of C2V.md).
- **Refresh policy v0:** convert on a `gen` change, at most one full
  conversion per N frames (a duty budget; pick N and justify it in PERF),
  with no dirty-tracking of bitplane writes (backlog). An HLE run with no
  `mach_run` converts once at a COPJMP strobe or an explicit
  `disp_refresh` call.
- **Fixture** `fixtures/c/pf_static.c` (vbcc, HLE) per S3_PLAN §4 S3.5.
  **Red:** the VIC bitmap, screen RAM and `$D800` stay empty (or the stub
  pattern). **Green:** byte-for-byte equal to `host/c2v_ref.py`'s
  conversion of the guest's planes and palette, in VICE. **Teeth:** a
  kernel bit-order mutant, a palette-LUT off-by-one, a stale `gen`, a wrong
  plane-pointer order, and a modulo ignored.

## C4 — W10-H: the U64 sampler (Ultimate Audio)

The firmware (`~/Documents/1541ultimate`: `software/io/audio/sampler.h`,
`fpga/io/sampler/`) has an 8-voice sample player mapped at C64
`$DF20–$DFFF` when config "Map Ultimate Audio $DF20-DFFF" is enabled
(default off). Per voice: control (enable/repeat/IRQ/8- or 16-bit), volume,
pan, start (26-bit memory address; the REU lives at `$1000000`), length,
repeat A/B and rate. Read the firmware to establish the exact C64 register
layout. Don't trust this summary.

On the U64E: `/v1/info` first; take the harness DeviceLock; snapshot the
config and restore it at the end (even on failure); write
`build/evidence/w10-hw/SESSION.log` as you go. Establish: (1) the
capability bit and the register map as the C64 sees it; (2) a known 8-bit
sample staged in REU memory plays from its REU address (a start address
pointing into the REU, check the end-of-sample status/IRQ bit, and the
counts); (3) the end-of-sample IRQ reaching the C64 IRQ line and how it is
acknowledged; (4) whether REU DMA and the sampler coexist. Audio quality
is not measured (no capture device); assert register/IRQ behaviour and
timing only. Red/green: the driver must detect a wrong expectation first.
Report with [MEASURED HW] tags and the firmware and core versions.

## C5 — gates (code packages)

`core.s` byte-identical modulo relocation (a real link-swap proof);
`perf_cycles --base <base prg> --max-delta 0`; rec_budget identical; the
full gate at exact counts (golden 5129, hand 63, super 678, host-test 1261
+ your new tests, dos 762, vbcc 433 (+ pf_static if it joins run_vbcc),
con 280, smc 42, run_chip 8, run_c2v + `--teeth` 6/6, chip_oracle 69+1,
run_mach + `--mutants` 18/18, run_irq + `--mutants` 29/29, run_cia live +
host + both `--mutants`), plus your new suite with `--mutants`. VICE only.

## Merge order and review

W10-H (independent) whenever done. W10-P first, then W10-V (it consumes
`disp_st` and the frame hook). After each package's final report, a
**fresh adversarial reviewer** attacks it; fix rounds follow until it says
MERGE; the supervisor runs a serial full gate on the merged tree before
each merge.

## Report

What you built; red → green evidence paths; mutation results; exact gate
counts; contract deviations (none expected); open risks; local-model use;
changed files vs your base. Under 60 lines, as a message.
