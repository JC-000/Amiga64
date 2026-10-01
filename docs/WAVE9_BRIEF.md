# Wave 9 Brief — S3.3 interrupt delivery ∥ S3.4 CIA-A/B

Supervisor-authored, **binding**. `docs/WAVE4_BRIEF.md` C1–C5 still apply,
in particular **C5 (red → green, adversarial)** and its "Local model"
section. `docs/CONTRACTS.md` (especially "Machine loop (S3.2)" and "I/O trap
($BF/$DF)") holds for the files it covers. The design is `docs/S3_PLAN.md`
§1.5 and §4 "S3.3" / "S3.4". The reference semantics are
`host/chipset_ref.py` (`Paula`, `CIA`, `Chipset`; checked against FS-UAE by
`tests.chip_oracle`). Where this brief is silent, S3_PLAN governs, then
chipset_ref. Changing a contract needs supervisor sign-off: report it, don't
work around it.

Base: branch `wave9/integration` (= main e64f595 + this brief). Each package
works ONLY in its supervisor-created worktree. **Never run mutating git.**
Evidence goes to `build/evidence/<pkg>/{red,green,mutants,...}` with a
running `PROGRESS.md` (one line per milestone; the supervisor reads it when
a message doesn't arrive). Scratch goes only in `<scratchpad>/<pkg>/`. Run
`make vectors` and `python3 -m host.handvec --out build/vectors-hand` in your
worktree before any gate. Long jobs run in the background with a DONE marker
that you poll yourself. Don't wait on completion notices.

**Context economy.** Read this brief, the S3_PLAN sections cited, the
CONTRACTS sections cited, `.serena/memories/task_completion.md`, and the
source you change. Use grep and line ranges; don't read whole unrelated
files or other waves' briefs.

**Clean room.** No AROS source, ROM bytes or disassembly in the repo, and
for this wave **don't read AROS sources at all**. The S3.4 calibration probe
is our own program written from the CIA/Paula hardware behaviour (C3).

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W9-I** | `w9-irq` | `runtime/ops/mach.s`; in `runtime/ops/chip.s` the INTENA/INTREQ/INTENAR/INTREQR paths only; **new** claims in `runtime/rc/*.inc` labelled `; W9-I irq`; new `tests/run_irq.py`, new `tests/irq/`, new `host/tests/test_irq*.py`; `tests/run_mach.py` + `tests/mach/` (only to keep them green as the seam changes); `host/chipset_ref.py` (**additive only**); `docs/CONTRACTS.md` sections "Machine loop (S3.2)" (amend) and new "Event seam (S3.3/S3.4)" + "Interrupt delivery (S3.3)"; `docs/PERF.md` (new section) |
| **W9-C** | `w9-cia` | new `runtime/ops/cia.s`; in `runtime/ops/chip.s` the CIA path only (route bank $BF to `cia.s`, remove the CIA stub code and the `chip_cia` shadow if it becomes unused); **new** claims labelled `; W9-C cia`; registration of `cia.s` in `Makefile`/`host/rescheck.py`/`tests.run_c2v.makefile_sources()` (those lines only; link order: `cia.s` before `mach.s`, `chip.s` stays last); new `tests/run_cia.py`, new `tests/cia/`, new `host/tests/test_cia*.py`; `tests/run_chip.py` (only the CIA legs that this change makes obsolete, each replaced by an equivalent in run_cia); `host/chipset_ref.py` (**additive only**); `docs/CONTRACTS.md` new section "CIA-A/B (S3.4)" |

Frozen (supervisor only): `runtime/core.s`, `runtime/ops/hle*.s`,
`dosio_*.s`, `g*.s`, `c2v.s`, `host/image.py`, `host/gen_dispatch.py`,
`runtime/macros.inc`, `runtime/rc/guest.inc`, `tests/rec_gate.sh`, the
runners, every existing claim line, all brief files. **If S3.3 needs a
`core.s` change** (for example an `exc_raise` entry that is usable from
`mach_run` outside `emu_run`), stop and report the exact minimal change
with its perf evidence. Don't work around it.

Both packages edit `chip.s` and `host/chipset_ref.py`, in disjoint regions.
Keep your edits contiguous and labelled. The supervisor resolves the merge.

## C1 — the event seam (W9-I builds it; W9-C programs against it)

All in `mach.s`, exported, documented in CONTRACTS "Event seam":

- **Guest time:** `mach_etime` → `mach_et` (u32 LE) = the guest E-clock
  now (counter >> s). This is the one time base for every guest clock.
- **Slots:** `MACH_NSLOT = 8`. Slot 0 is VERTB (mach-internal). Slots
  1–7 belong to `cia.s` (W9-C assigns them, e.g. A-TA, A-TB, B-TA, B-TB,
  A-TOD alarm, B-TOD alarm).
- **Arm:** `mach_arm` (A = slot, `mach_at` = absolute E time, u32 LE).
  `mach_disarm` (A = slot). Both are callable from the trap context
  (inside `emu_run`, IRQ enabled) and from callbacks. W9-I makes them
  race-free against `mach_irq` and reprograms the CIA #1 one-shot when the
  new deadline is the earliest. A deadline in the past fires at the next
  service pass.
- **Callbacks:** a RAM table `mach_cbv` (2 bytes per slot) that a client
  fills at its own init (`cia_init` stores its handlers). There's no link
  dependency of `mach.s` on `cia.s`. A callback runs in the service pass
  (never in the IRQ), with `$01` = $35, may call `mach_arm`/`mach_post`,
  must not call `emu_*`, and returns with RTS (A/X/Y clobbered).
- **VERTB hook:** `mach_cbv` slot 0's callback runs after every VERTB, so
  CIA-A's TOD (which counts vsyncs on an Amiga) can tick there. Default:
  none.
- **Post:** `mach_post` (A = INTREQ bit 0–14) sets that INTREQ bit. Delivery
  happens at the next service pass. `cia.s` posts PORTS (bit 3) for CIA-A
  and EXTER (bit 13) for CIA-B, with the level/edge semantics of
  `chipset_ref` (follow its rule for re-raising while the ICR stays set).
- **Phase A:** W9-I ships the seam FIRST, tested with a fake event source,
  and reports "seam ready". The supervisor merges it into
  `wave9/integration` and then into W9-C's branch. Until then W9-C codes
  against a shim of exactly this API in its own `tests/cia/` (the shim
  must never be linked into `runtime/`).

## C2 — S3.3 interrupt delivery (W9-I, phase B)

- INTENA/INTREQ: the full SET/CLR rule, the master enable bit (INTEN, bit
  14), INTENAR/INTREQR reads, and INTREQ bit 5 now set via `mach_post`.
- **Delivery** (exception mode only, `st_exc` ≠ 0): after each service
  pass, if `INTEN` and `INTENA & INTREQ` give a level above SR's I2–I0,
  raise autovector 24+level (stacked SR, stacked PC = `r_pc`, S = 1,
  T = 0, I2–I0 := level) and re-enter `emu_run`. Use the existing
  exception-frame code (see the core.s rule above). The level map is
  chipset_ref's.
- **Recognition latency:** a CPU write to INTENA/INTREQ, or an IPL drop by
  MOVE to SR/RTE, may act at the next event (S3_PLAN §1.5). If you instead
  force an early pass through `st_halt` from `chip.s`, show that it costs 0
  on the hot path.
- **STOP:** on `HALT_STOP`, `mach_run` waits for the next event (or
  fast-forwards guest time to it, your choice; report it), delivers if
  possible and resumes. A STOP with nothing armed and nothing pending
  returns `HALT_STOP` to the caller (no infinite wait).
- **Red:** S3_PLAN §4 S3.3 (a level-3 handler counting VERTB stays at 0).
  **Green:** counter > 0, stacked SR/PC and the IPL checked in the handler.
  **Teeth:** IPL = 7 → 0 deliveries; a handler that doesn't acknowledge
  INTREQ → the re-entry storm is detected; hand vectors pin the frame; STOP
  wakes on VERTB; a mutant per rule (level map, IPL compare `>` vs `>=`,
  INTEN ignored, SET/CLR inverted).

## C3 — S3.4 CIA-A/B (W9-C)

- Timers A/B: continuous and one-shot, force load, start on the high-byte
  write in one-shot mode, TB counting TA underflows (CRB INMODE), PB6/PB7
  outputs only as far as chipset_ref models them. ICR: masks (SET/CLR),
  read-clear, the IR bit. TOD: latch on the high read, halt on the high
  write, alarm (CRB bit 7). CIA-A TOD counts VERTB (the slot-0 hook);
  CIA-B TOD counts lines (lazily from `mach_etime`, never one event per
  line). CIA-A PRA inputs $EB as in S3.1.
- Timer counts are computed **lazily** from `mach_etime` on read; only
  underflows and alarms are armed as events. No per-tick work.
- **Red/green:** direct-API sweeps (as `rec_apisweep`) of `cia_read`/
  `cia_write` sequences diffed against `chipset_ref.CIA`; then guest legs
  through the $BF trap. **Calibration probe** (clean room): a guest program
  that measures CIA-A timer-A ticks between two CIA-A TOD ticks and reports
  the TOD frequency. It must report 50 Hz (14188 ticks per tick).
  Red: 0, or a hang.
- **Teeth:** one mutant per rule (one-shot stop, force load, high-byte
  start, ICR read-clear, mask SET/CLR, TB INMODE, TOD latch, TOD alarm).
- `tests.run_chip` stays green. Any CIA leg made obsolete moves to run_cia
  with an equivalent check (list each one in your report).

## C4 — gates (both)

`core.s` byte-identical modulo relocation (link-swap proof);
`perf_cycles --base build/<base> --max-delta 0`; rec_budget identical; the
full gate (`mem:task_completion`, exact counts: golden 5129, hand 63, super
678, host-test 1168 + your new tests, dos 762, vbcc 433, con 280, smc 42,
run_chip 8 legs, run_c2v + `--teeth` 6/6, chip_oracle 69 MATCH + 1 LIMIT,
run_mach + `--mutants` all killed) plus your new suite with `--mutants`.
VICE only; no U64E in this wave.

## Merge order and review

W9-I phase A (seam) → integration → into W9-C. Then W9-I phase B and W9-C
proceed in parallel. After each package's final report, a **fresh
adversarial reviewer** attacks it (re-runs red and green, writes its own
mutants, attacks races: `mach_arm` from the trap racing `mach_irq`,
delivery during STOP, nested interrupts at a higher level, an ICR read
racing an underflow). Fix rounds follow until the review is MERGE. The
supervisor re-runs the full gate on the merged tree before each merge.
Order: W9-I → W9-C.

## Report

What you built; red → green evidence paths; mutation results; exact gate
counts; contract deviations (none expected); open risks; local-model use;
the changed files vs your base. Under 60 lines, sent as a message. Put a
copy in `build/evidence/<pkg>/REPORT.md` if the harness allows.
