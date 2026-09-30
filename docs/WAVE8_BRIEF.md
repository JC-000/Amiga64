# Wave 8 Brief — S3.2 machine loop, time base, events

Supervisor-authored, **binding**. `docs/WAVE4_BRIEF.md` C1–C5 still apply,
in particular **C5 (red → green, adversarial)** and its "Local model" and
"Hardware" sections. Wave 5–7 contracts (`docs/WAVE5_BRIEF.md`,
`docs/WAVE6_BRIEF.md`, `docs/WAVE7_BRIEF.md`, `docs/CONTRACTS.md`) still
hold for the files they cover. The design is `docs/S3_PLAN.md` §1.5 and §4
"S3.2". The converter handover is `docs/C2V.md` "Handover to S3.2". Where
this brief is silent, S3_PLAN governs. Changing a contract needs supervisor
sign-off: report it, don't work around it.

Base: branch `wave8/integration` (= main 130dac6 + this brief). Each package
works ONLY in its supervisor-created worktree (the absolute path is in your
launch prompt). **Never run mutating git.** Evidence goes to
`build/evidence/<pkg>/{red,green,mutants,...}` in your worktree, with a
running `build/evidence/<pkg>/PROGRESS.md` (one line per milestone; the
supervisor reads it if your message never arrives). Session scratch goes
only in `<scratchpad>/<pkg>/`, because the scratchpad is shared.
`build/` is per-worktree: run `make vectors` and
`python3 -m host.handvec --out build/vectors-hand` in your worktree before
the gate. Long jobs run in the background with a DONE marker that you poll.

**Context economy.** Read only what your package needs: this brief, the
S3_PLAN and C2V sections cited above, `.serena/memories/task_completion.md`
(the done-gate), and the source you change. Don't read other waves' briefs
end to end.

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W8-L** | `w8-loop` | new `runtime/ops/mach.s`; `runtime/ops/chip.s` (beam reads, the INTREQ VERTB bit only); `runtime/ops/c2v.s` (the NMI-vector handover only); **new** claims in `runtime/rc/zp.inc`/`ram.inc` labelled `; W8-L mach`; **sign-off given** to replace the existing `c2v $FFFA-$FFFB` interim line and the `free $FFFC-$FFFF` line with `s3 $FFFA-$FFFF` (S3_PLAN §1.5 wording); the registration of `mach.s` in `Makefile`/`host/rescheck.py`/`tests.run_c2v.makefile_sources()` (those lines only); `host/chipset_ref.py` (**additive only**: a beam/time-base reference class and its tests); new `tests/run_mach.py`, new `tests/mach/` (guest fixtures), new `tests/runner/mach_*.s` if needed, new `host/tests/test_mach*.py`; `docs/CONTRACTS.md` (new section "Machine loop (S3.2)"); `docs/C2V.md` (the handover section only); `docs/PERF.md` (new section) |
| **W8-H** | `w8-hw` | new `tools/s3probe/cia_rate/` (probe program + driver), `docs/HARDWARE.md` (new section "CIA clock at turbo"). The **only** Wave 8 agent that touches the U64E. |

Frozen (supervisor only): `runtime/core.s`, `runtime/ops/hle*.s`,
`runtime/ops/dosio_*.s`, `runtime/ops/g*.s`, `host/image.py`,
`host/gen_dispatch.py`, `runtime/macros.inc`, `runtime/rc/guest.inc`,
`tests/rec_gate.sh`, `tests/runner/runner.s`, `tests/runner/run_program.s`,
every other existing claim line, all brief files. **S3.2 needs no `core.s`
change** (S3_PLAN §1.5: the event path costs 0 cycles per instruction). If
you believe it does, stop and report.

## C1 — `mach_run` (W8-L)

- `mach_run` wraps `emu_run`: same entry state and the same exit contract
  (`st_halt`/`st_reason` as `emu_run` leaves them), so a caller can swap one
  for the other. `mach_init` sets up the time base once per guest boot.
- On entry: `$01`=$35 (KERNAL out, I/O in), the RAM vectors at $FFFA–$FFFF
  are installed, the CIA #1 timer A IRQ is unmasked, `cli`. On every exit:
  CIA #1 ICR masked and acknowledged, `sei`, `$01` restored to the caller's
  value. The vectors may stay in RAM (they are harmless with the KERNAL in).
- Race-free clear exactly as S3_PLAN §1.5: clear bit 7 of `st_halt` under
  `sei`, test `st_halt & $7F`; a real halt goes back to the caller with
  `ev_pend` kept for the next `mach_run`.
- **Events in S3.2: VERTB only.** The CIA #1 one-shot is armed for the next
  guest frame boundary. Servicing a VERTB: advance the frame counter
  `mach_frames` (u32, claimed), set INTREQ bit 5 in the chip shadow, re-arm.
  **No interrupt delivery** (that is S3.3), no CIA-A/B guest timers (S3.4),
  no copper or refresh (S3.5).
- **STOP:** `HALT_STOP` passes back to the caller unchanged. Idle skip waits
  for S3.3, which can wake a stopped CPU.
- The handler touches only A/X/Y (saved), `ev_pend`, `st_halt` and CIA
  registers. It never touches the REC, the REU or guest state.
- NMI: satisfy every bullet in C2V.md "Handover to S3.2" (valid with
  `$01`=$34, saves A and `$01`, maps I/O in, acknowledges the CIA2 ICR,
  `rti`). Choose one of C2V's two options, update C2V.md and S3_PLAN §1.5's
  wording in your report (you don't edit S3_PLAN; the supervisor does).
  `tests.run_c2v` stays green.
- Existing drivers keep `sei` and never call `mach_run`; the Wave 5–7
  counts must not move.

## C2 — time base and beam (W8-L)

- The CIA #2 TA→TB cascade runs free as a 32-bit φ2 counter. Its port A
  (VIC bank) and its NMI mask are untouched; check this against c2v's use
  of CIA #2.
- The guest E-clock is the counter >> `mach_s` (u8, claimed, runtime
  setting, default 4). One guest frame is **14188 E ticks** (the AROS
  TOD-vs-timer-A probe then reads 50 Hz). In VICE, guest frames per
  second ≈ 985248 / 14188 / 2^s = 69.4 / 2^s.
- VPOSR/VHPOSR are derived from the position within the current frame:
  5 colour clocks per E tick, 227 colour clocks per line, so 313 lines
  (V 0–312; the last one is partial). VPOSR keeps the stub's LOF/Agnus-ID
  bits and adds V8 in bit 0. Pin the exact formula in `host/chipset_ref.py`
  (new class) and CONTRACTS, and test the 6502 against it.
- Before `mach_init` has run, the beam registers return today's stub
  values, so `tests.run_chip`'s 8 legs stay green unchanged.
- The beam reads sit on the trapped cold path, so they may cost more than
  the hot path. Measure and document the cycles per VHPOSR read in PERF.md.

## C3 — tests (W8-L): the S3_PLAN §4 legs, plus these

- **Red** (on the base build, before any fix): the guest loop that polls
  VHPOSR for a change times out; a frame count stays 0.
- **Green:** the poll loop terminates; VPOSR/VHPOSR are monotonic within a
  frame and wrap at the frame boundary; frames/s = 69.4/2^s ±10% for
  s ∈ {0, 2, 4} in VICE; INTREQ bit 5 is set after a frame.
- **Teeth**, each needing a mutant that turns a leg red: handler not
  installed; `st_halt` compared with `= $80` instead of `& $7F` (a halt
  racing an event, `st_halt` = $81, must still return the halt); the IRQ
  left enabled on exit (a following `emu_run` in the same test must see no
  event); `$01` not restored; the NMI handler not acknowledging CIA2 (a
  forced CIA2 NMI source must not latch /NMI); a wrong `mach_s` shift.
- **Perf gate:** `rec_budget` bit-identical; `core.s` byte-identical
  (`cmp` of the linked `core.o`/segment, or the equivalent); the full gate
  plus `run_dos`/`run_vbcc`/`run_con`/`smc`/`run_chip`/`run_c2v`/
  `chip_oracle` at the exact Wave 7 counts.

## C4 — W8-H: CIA clock at turbo (S3_PLAN §5 risk 5)

Answer one question on the U64E: at 48 MHz (and at 1 MHz as the control),
do the C64 CIAs count φ2 (~0.985 MHz), or does their rate scale with the
turbo clock? Method: a free-running CIA #2 timer sampled at raster-line
matches N frames apart (the VIC raster runs in real time at 50 Hz). Also
report the TOD. Expect 19656 ticks per PAL frame if the CIAs count φ2.
Protocol: `/v1/info` before any run; the harness DeviceLock; snapshot and
restore the config (`u64_hold`); a session log at
`build/evidence/w8-hw/SESSION.log` written as you go. No keyboard input is
needed. The Blingboard gotcha doesn't apply. Report with [MEASURED HW]
tags and the firmware and core versions.

## Merge order and review

W8-H (independent) → W8-L. After each report, a fresh **adversarial
reviewer** re-runs the red and green legs, writes its **own** mutants, and
tries to break the C1–C3 claims (IRQ races, `$01` state on every exit path,
an REU DMA and the c2v fill interacting with the IRQ, a counter wrap,
`mach_s` = 0 and 8). Fix rounds follow until the review is MERGE. The
supervisor re-runs the full gate on the merged tree before each merge.

## Report

What you built; red → green evidence paths; mutation results; the exact
full-gate counts; contract deviations (there should be none); open risks;
where you used the local model; the exact list of changed files relative
to `wave8/integration`. Keep it under 60 lines. The evidence stays on disk.
