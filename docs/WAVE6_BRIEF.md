# Wave 6 Brief — the open queue (keyboard, AROS oracle, harness quirk, supervisor mode, S3 plan)

Supervisor-authored, **binding**. Read `docs/WAVE4_BRIEF.md` first: its
contracts C1–C5 all still apply, in particular **C5 (red → green,
adversarial)** and its "Local model" rules. Wave 5 contracts
(`docs/WAVE5_BRIEF.md` C1–C3) still hold for the files they cover. Changing
a contract needs supervisor sign-off: report it, don't work around it.

Base: branch `wave6/integration` (= main b14f204 + this brief). Each package
works ONLY in its supervisor-created worktree (the absolute path is in your
launch prompt). Edit only files you own, plus new files under your own
prefix. **Never run mutating git.** Other packages' files are read-only.
Evidence goes to `build/evidence/<pkg>/{red,green,...}` in your worktree.

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W6-K** | `w6-kbd` | new `tests/kbd_echo.py`, new `fixtures/c/kbd_*.c` and/or `tests/kbd/*.s`, new `host/tests/test_kbd_*.py`, `docs/HLE_CON.md` (new "Hardware validation" section only) |
| **W6-O** | `w6-oracle` | new `tools/aros/` (fetch script, pinned manifest, LICENSE note), new `tests/aros_oracle.py`, new `tests/oracle/` (FS-UAE config templates, startup scripts), new `host/tests/test_aros_oracle*.py`, new `docs/ORACLE.md`, README "Third-party material" (append the AROS entry only) |
| **W6-Q** | `w6-quirk` | new `build/evidence/w6-quirk/**` only (repro scripts + upstream issue draft). No edits to the harness repo or to Amiga64 sources. |
| **W6-S** | `w6-super` | `runtime/core.s`, `runtime/ops/g5_*.s` (flow/system ops), new `runtime/ops/g7_system.s`, `host/gen_dispatch.py`, `host/vectors.py`, `fixtures/vectors/` (new stems via the pinned fetch only), `runtime/rc/zp.inc` (claims inside $88–$9F only), `docs/CONTRACTS.md` ("Supervisor mode & exceptions" section: new, plus the one M1 bullet it supersedes), `docs/PERF.md` (new section), new `host/tests/test_super*.py` |
| **W6-D** | `w6-s3plan` | new `docs/S3_PLAN.md`, new `tools/s3probe/**` (analysis scripts), new `build/evidence/w6-s3plan/**` |

Frozen (supervisor only): `runtime/ops/hle*.s`, `runtime/ops/dosio_*.s`,
`runtime/ops/g1..g4,g6_*.s`, `host/rescheck.py`, `host/image.py`,
`Makefile`, `runtime/macros.inc`, `runtime/rc/ram.inc`,
`runtime/rc/guest.inc`, `tests/rec_gate.sh`, all brief files. If you need a
one-line change there, describe it exactly in your report and test it on a
scratch copy under `build/`.

## C1 — Keyboard hardware validation (W6-K)

Goal: close the "48 MHz column-settle UNVALIDATED" item in `mem:core`.
Build a tiny program (vbcc C through the HLE console, preferred; or a
runner-level asm probe if it has to be) that echoes, for each key event,
the ASCII that `hle_con.s` delivers **and** the raw column/row it scanned,
so a human can see a wrong character at once. `tests/kbd_echo.py` runs it
with `--backend vice` (automated: inject keys via the VICE keyboard buffer
or matrix, assert the echo) and `--backend u64` (manual: load and run,
print on-screen instructions, stream the echoed output back to the host
terminal, snapshot/restore config via `u64_hold`, explicit CPU speed with
`--mhz 1|48`). The manual script checklist (in `docs/HLE_CON.md`): hold Q
(wrong result would be `^`), a 3-key rollover, RSHIFT and C=, joystick port
1 held while typing (must be masked).

Hardware (C3 of Wave 5 applies): **W6-K is the only package that touches
the U64E.** Allowed: ONE automated smoke run at 1 MHz and 48 MHz, with no
human, to prove load/run/stream/restore. Use the DeviceLock and write a
session log to `build/evidence/w6-kbd/hw/`. The human session itself is
run by the supervisor with the user. Re-record the fw/FPGA/core versions at
the start of the session (the device was reflashed on 2026-09-25).

## C2 — AROS reference oracle (W6-O)

- The AROS m68k ROM (APL 1.1) is **fetched, never committed**:
  `tools/aros/fetch.sh` downloads a pinned nightly by exact URL and SHA-256
  into `~/.cache/amiga64/aros/<hash>/` and records the licence text and
  source location there. It fails closed on a hash mismatch. Nothing
  under `build/` or the repo holds ROM bytes.
- `tests/aros_oracle.py --prg <runner.prg>`: for each `fixtures/c` vbcc
  program registered in `tests/run_vbcc.py` (import its table, don't copy
  it), run the same Hunk binary in FS-UAE (`/Applications/FS-UAE.app`,
  A500 model, AROS ROM, headless or with a hidden window, with a host
  directory mounted as a drive, output redirected to a file there), then
  diff that output against Amiga64's output for the same program. It
  reports a per-program MATCH/DIFF/SKIP. SKIP needs a written reason:
  interactive, timing-dependent or ULTDOS programs.
- The oracle must have teeth: a test injects a known wrong expected output
  (or a mutated binary) and asserts that the oracle reports DIFF. Red means
  the teeth test fails before the diff logic exists.
- The oracle is **never** part of `rec_gate.sh` and never required by
  tests: it skips cleanly (exit 0, a SKIP line) when FS-UAE or the cache is
  absent. Our own expected outputs stay the authority for the gate. A DIFF
  is a finding to triage in `docs/ORACLE.md`: our bug, an AROS divergence,
  or a test artifact.
- Wall-clock: record per-program runtime. FS-UAE runs must time out.

## C3 — Harness quirk (W6-Q)

Reproduce the report: a VICE binary-monitor memory read right after
`execute.goto()` can leave the CPU halted (seen in `tests/bss_boot.py`,
worked around with a 1 s settle). Deliver a minimal standalone repro (no
Amiga64 runtime needed if possible), how often it happens over N ≥ 200
trials with and without the settle, the harness and VICE versions, a
root-cause hypothesis backed by the harness source
(`~/Documents/c64-test-harness`, read-only), and a draft upstream issue as
`build/evidence/w6-quirk/ISSUE.md`. **Do not file it, push or comment
anywhere**: the supervisor asks the user first. VICE only; no U64E.

## C4 — Supervisor mode & exceptions (W6-S)

First step of queue item 4 (S3 plus supervisor mode, towards an AROS ROM
boot). Scope: the SR system byte (S, T0 read-only/ignored, I2–I0), USP/SSP
banking, `MOVE to/from SR`, `ANDI/ORI/EORI to SR`, `MOVE USP`, `RTE`,
`TRAP #n`, `TRAPV`, `CHK`, `STOP` (halts with a new reason), `RESET`
(no-op plus a hook), and exception processing for privilege violation,
TRAP, TRAPV, CHK, divide-by-zero, illegal, A-line and F-line, all through a
guest vector table at VBR = 0 (68000: fixed). No interrupts yet (that's
S3), and no address/bus error (reason code only).

- **Vectors are the authority.** Extend `host/vectors.py` to import the
  upstream stems for these ops, and the exception cases it currently drops,
  from the SAME pinned commit, offline into `fixtures/vectors/`. The FULL
  suite must stay green, and the new stems must reach a documented pass
  count. Every case you exclude is listed with a reason; aim for zero
  exclusions beyond address-error cases.
- **Compatibility gate (non-negotiable).** HLE programs must behave
  exactly as today. Exception delivery is gated by one flag, `st_exc`
  (claim it in `zp.inc` $88–$9F). With the flag clear (the default, which
  the image/runner never sets), TRAP, A-line, F-line, illegal and privilege
  cases halt with today's `HALT_*` reasons, bit for bit. The vector runner
  sets the flag. `run_dos` 762, `run_vbcc` 433, `run_con` 280, `smc` 42
  and the hand vectors 63 are unchanged.
- **Perf.** The user-mode hot path (fetch/dispatch, and the CCR paths in
  the sized ALU primitives) must not get slower. Prove it with
  `tests/rec_budget.py` and a cycle-accounting delta in `docs/PERF.md`. Keep
  the system byte out of the hot path: CCR stays where it is.
- Work in your worktree only; this is an invasive refactor of frozen
  files that other packages build against. Because of that, W6-S merges
  **last**.

## C5 — S3 chipset plan (W6-D)

A design document, not code in `runtime/`. `docs/S3_PLAN.md` must answer,
with evidence:

1. **The minimum OCS surface for a first static playfield** (M3 in
   `docs/ARCHITECTURE.md`): which custom registers, Copper, which DMA, and
   how guest writes to $DFF000–$DFFFFF are trapped without slowing the
   memory-API hot path. Give a concrete mechanism and its cycle cost
   against `docs/PERF.md` numbers.
2. **The mapping to the VIC-II**: bitplanes to multicolour/hires bitmap,
   palette reduction, vblank conversion budget at 1 and 48 MHz. Measure or
   estimate from HARDWARE.md numbers.
3. **The AROS boot surface**: what the AROS m68k ROM touches before
   "insert disk" (chip registers, CIAs, interrupts, memory probing,
   supervisor ops). Gather it by static analysis of the AROS sources and,
   once W6-O's cache exists (`~/.cache/amiga64/aros/`), optionally by
   tracing an FS-UAE boot. Give a feasibility and boot-time estimate.
4. **An increment plan** after W6-S (interrupts, CIA timers, the chipset
   trap mechanism, a playfield demo fixture), each step with its own
   red/green test idea.

No proprietary material. Read the AROS sources online or fetch them into
the cache, never into the repo. Keep the doc under about 600 lines.

## Merge order and review

W6-Q and W6-D (docs/evidence) → W6-K → W6-O → W6-S (last, invasive).
After each report, a fresh **adversarial reviewer** attacks the package:
it re-runs the red and green legs, writes its own mutants, and tries to
break the contract claims. The supervisor then re-runs the full gate
(`mem:task_completion`) on the merged tree before each merge.

## Report

What you built; red → green evidence paths; mutation results; full-gate
exact counts (code packages: `sh tests/rec_gate.sh build/<pkg>
build/<pkg>-gate` plus `run_dos`, `run_vbcc`, `run_con` and `smc`, and
your own suites); contract deviations (there should be none); open risks;
where you used the local model; the exact list of changed files relative
to `wave6/integration`. Long-running work: keep a progress log at
`build/evidence/<pkg>/PROGRESS.md` so the supervisor can check it directly.
