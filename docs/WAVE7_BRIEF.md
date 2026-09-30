# Wave 7 Brief — chipset shim, first increments (S3.0, S3.1, S3.5 converter)

Supervisor-authored, **binding**. Read `docs/WAVE4_BRIEF.md` first: its
contracts C1–C5 all still apply, in particular **C5 (red → green,
adversarial)** and its "Local model" rules. Wave 5 and Wave 6 contracts
(`docs/WAVE5_BRIEF.md`, `docs/WAVE6_BRIEF.md`, `docs/CONTRACTS.md`) still
hold for the files they cover. The design is `docs/S3_PLAN.md`; its §4
increments are the specification, and where this brief is silent, S3_PLAN
governs. Changing a contract needs supervisor sign-off: report it, don't
work around it.

Base: branch `wave7/integration` (= main 66649e8 + this brief). Each package
works ONLY in its supervisor-created worktree (the absolute path is in your
launch prompt). Edit only files you own, plus new files under your own
prefix. **Never run mutating git.** Other packages' files are read-only.
Evidence goes to `build/evidence/<pkg>/{red,green,...}` in your worktree,
with a running `build/evidence/<pkg>/PROGRESS.md`.

The harness is c64-test-harness **≥ 0.13.0**. After `goto()` on VICE, poll
with `wait_for_memory` (never a bare `read_bytes` loop; see #514).

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W7-M** | `w7-models` | new `host/chipset_ref.py`, new `host/tests/test_chipset_ref*.py`, new `tests/chip_oracle.py`, new `tests/chip_oracle/` (our own m68k test programs + FS-UAE configs), `tools/s3probe/aros_boot_model.py` (only to import the promoted classes; see C1) |
| **W7-T** | `w7-trap` | `runtime/core.s` (the MIO hook, cold path, STRADDLE bank+1 route, `mio_split`), new `runtime/ops/chip.s`, **new** claims in `runtime/rc/zp.inc` and `runtime/rc/ram.inc` (label them `; W7-T chip`), the registration of `chip.s` in `host/rescheck.py` and `Makefile` (those lines only), new `tests/run_chip.py`, new `host/tests/test_chip*.py`, `docs/CONTRACTS.md` (new section "I/O trap ($BF/$DF)"), `docs/PERF.md` (new section) |
| **W7-V** | `w7-c2v` | new `runtime/ops/c2v.s`, **new** claims in `runtime/rc/zp.inc`/`ram.inc` (label them `; W7-V c2v`), the registration of `c2v.s` in `host/rescheck.py` and `Makefile` (those lines only), new `host/c2v_ref.py`, new `host/tests/test_c2v*.py`, new `tests/run_c2v.py`, new `docs/C2V.md` |

Frozen (supervisor only): `runtime/ops/hle*.s`, `runtime/ops/dosio_*.s`,
`runtime/ops/g*.s`, `host/image.py`, `host/gen_dispatch.py`,
`runtime/macros.inc`, `runtime/rc/guest.inc`, `tests/rec_gate.sh`, every
existing claim line in `runtime/rc/*.inc`, all brief files. If you need a
one-line change there, describe it exactly in your report and test it on a
scratch copy under `build/`. W7-T and W7-V both add rc claims and
registration lines: the supervisor resolves those merges, so keep your
additions in contiguous, labelled blocks.

**Third-party material:** no AROS ROM bytes, disassembly or Workbench-disk
content in the repo, ever (README "Third-party material"). The AROS ROM
comes only from the W6-O cache (`tools/aros/fetch.sh`). Tests that need it
skip cleanly when it is absent.

## C1 — S3.0 host reference models (W7-M)

S3_PLAN §4 "S3.0". Promote CIA, Paula (INTENA/INTREQ/DMACON), blitter
(normal mode + fill; line mode may be logged only) and copper (MOVE/WAIT/
SKIP) from `tools/s3probe/aros_boot_model.py` into `host/chipset_ref.py`
with pytest. Afterwards `aros_boot_model.py` imports them, and its results
must be **bit-identical** to before. Record the before/after instruction
counts to "insert disk" and a Workbench phase count from docs/WB_BOOT.md
(using a synthetic ADF in tests; the user's disks only for the recorded
comparison, read in place, never copied).
- Oracle: `tests/chip_oracle.py` runs our own small m68k programs
  (blitter copy, cookie-cut $CA, clear, descending shifted copy, area fill;
  CIA timer one-shot/continuous/TB-counts-TA, ICR read-clear) in FS-UAE
  (A500 OCS, the W6-O setup) and diffs memory dumps against the model.
- Red: an injected wrong minterm, and an injected one-shot rule error, each
  fail the diff. Green: every oracle case matches. Mutants: at least one
  per blitter channel/shift/mask rule and per CIA timer/ICR rule.
- This package is **the reference for W7-T's and later packages' tests**:
  keep the API small and documented at the top of `host/chipset_ref.py`.

## C2 — S3.1 trap mechanism, stub chip file, serial capture (W7-T)

S3_PLAN §4 "S3.1" and §1.2–1.3, exactly. Invasive; merges last.
- The hook sits only on the bank-changed branch of `MIO_PROG` and the cold
  path. **Invariant: `m_rbk` never holds $BF or $DF.** A trapped access
  issues no REU DMA.
- `runtime/ops/chip.s`: writes land in a 256-word shadow plus a log; reads
  return the §3.4 fixed values (SERDATR $3000, POTINP $FF00, DENISEID $FFFF,
  VPOSR $8000; CIA-A PRA $EB), 0 for write-only registers, otherwise the
  shadow. SERDAT appends to a C64-side ring that the host reads.
- Red/green, teeth and the four mutants: as S3_PLAN lists them (cold path,
  split long at $DFFFFE, `$BEFFFE`/`$DEFFFE` bank carry, byte access, CIA
  odd/even; mutants: no cold check, no split check, no bank+1 route, `m_rbk`
  stored before the trap).
- **Perf gate (hard):** `rec_budget` bit-identical; `tools/s3probe/
  bank_probe.py` counts unchanged; `tests/perf_cycles.py --max-delta 0` on
  the non-I/O loops; exactly +8 cycles per bank switch on a bank-alternating
  loop (VICE); BRNC-enforcing build with no new hot-path page crossings
  (fix with HOTALIGN, never BRNC_WARN_ONLY).
- **Hardware (you are the only U64E owner in Wave 7):** one unattended ABBA
  session at 48 MHz (base vs trap build), plus **the cost of one code-window
  refill at 48 MHz** (a loop that jumps between two pages every iteration:
  the number S3_PLAN §3.5 assumes as ~90 µs). Before running: `/v1/info`
  must report fw 3.15 git `c47155b7`; take the harness DeviceLock; snapshot
  and restore the config (`u64_hold`); record fw/FPGA/core versions in the
  evidence. Report the numbers in `docs/PERF.md` with [MEASURED HW] tags.
- Use the W7-M models only if they have merged. Otherwise the §3.4 fixed
  values are the whole spec for S3.1.

## C3 — S3.5 converter v0, standalone (W7-V)

The **converter half** of S3_PLAN §4 "S3.5" only: no copper, no display
registers, and no guest-visible behaviour change. Promote the two kernels
from `tools/s3probe/c2v.s` (2-plane lores → multicolour bitmap with exact
colours; 4-plane hires → hires silhouette v0) into `runtime/ops/c2v.s` as a
callable module. The planar source is in the REU and the output is a
VIC-II bitmap + screen RAM + colour RAM. Add the 4096-entry OCS-RGB →
VIC palette LUT built on the host (`host/c2v_ref.py`), plus the host
reference conversion.
- Placement: find the bitmap/screen/colour placement and the VIC bank that
  fit the current runtime memory map (`runtime/rc/ram.inc`,
  `runtime/c64.cfg`) without moving any existing claim. If none fits,
  **stop and report** with the options and their costs. Don't move
  anything.
- Red: `tests/run_c2v.py` (VICE, direct-API `jsr` into the module on a
  host-generated planar buffer in the REU) shows the bitmap/screen/colour
  unchanged before the module exists (or with the call stubbed). Green:
  byte-for-byte equal to `host/c2v_ref.py`, for several patterns and
  palettes, both kernels. Teeth: a bit-order mutant, a palette-LUT
  off-by-one, a row-advance mutant.
- Perf: VICE cycles per frame for each kernel, compared with S3_PLAN's
  573,820 / 327,620 [MEASURED VICE]. Explain any difference over 5%.
- The runtime must not grow on the interpreter's hot path: link-map diff and
  `perf_cycles --max-delta 0`.

## Merge order and review

W7-M → W7-V → W7-T (last, invasive). After each report, a fresh
**adversarial reviewer** attacks the package: it re-runs the red and green
legs, writes its own mutants, and tries to break the contract claims. Fix
rounds follow until the review is MERGE. The supervisor then re-runs the
full gate (`mem:task_completion`) on the merged tree before each merge.
Next wave (not now): S3.2 machine loop/time base, then S3.3/S3.4 against the
W7-M models.

## Report

What you built; red → green evidence paths; mutation results; full-gate
exact counts (code packages: `sh tests/rec_gate.sh build/<pkg>
build/<pkg>-gate` plus `run_dos`, `run_vbcc`, `run_con` and `smc`, and
your own suites); contract deviations (there should be none); open risks;
where you used the local model; the exact list of changed files relative
to `wave7/integration`.
