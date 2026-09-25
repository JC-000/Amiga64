# AROS reference oracle

`tests/aros_oracle.py` runs every vbcc program registered in
`tests/run_vbcc.py` twice: on Amiga64, and on a real AmigaOS-compatible
system (the open-source AROS m68k ROM in FS-UAE). It then diffs the two
runs. It is a **second opinion, not a gate**. Our own expected outputs
(`run_vbcc.programs()`) remain the authority. A DIFF is a finding to
triage, and the triage goes in the table below.

## Use

```sh
tools/aros/fetch.sh                                   # once: pinned ROM → ~/.cache/amiga64/aros/
python3 -m tests.aros_oracle --prg build/<pkg>/runner.prg   # Amiga64 side run in VICE
python3 -m tests.aros_oracle --table                        # Amiga64 side = run_vbcc expectations
    [--only hello,fio_rw] [--timeout 120] [--retries 1] [--json FILE] [--keep DIR] [--strict]
AMIGA64_AROS_ORACLE=1 python3 -m pytest -q host/tests/test_aros_oracle_fsuae.py \
    host/tests/test_aros_oracle_fetch.py                     # FS-UAE + fetch.sh legs
```

- Each program's line is one of:
  - `MATCH`;
  - `RETRY-MATCH`: it matched only after a boot-script stall was re-run
    (see "Limits and risks");
  - `DIFF`, followed by every difference line, each tagged with its pinned
    triage category, or `[NEW]` when the line is not pinned;
  - `SKIP` with its reason.

  A DIFF is **triaged** only when its lines are *exactly* the pinned ones
  in `TRIAGE` (`tests/aros_oracle.py`, the table below): no new line and
  no pinned line missing. Otherwise the program is `[UNTRIAGED: n new, m
  missing]`. A triaged program that stops differing is `[TRIAGE STALE]`.
  Only two volatile values are patterns, and they are narrow: AROS's
  ThisTask address (1 to 8 hex digits) and the host's Unix time (9 or 10
  digits).
- The summary line counts MATCH, RETRY-MATCH, DIFF (and how many are
  untriaged), SKIP, and the programs that needed a retry. The JSON report
  carries the same counts, plus per program the diff lines, their
  categories, any missing pinned lines and the retries.
- Exit code: 1 on an untriaged DIFF or a stale triage. With `--strict` it
  is also 1 on any DIFF or RETRY-MATCH. Otherwise 0. The oracle **skips
  cleanly** (prints one `SKIP aros_oracle:` line and exits 0) when it
  cannot run:
  - FS-UAE is absent (`FS_UAE` overrides `/Applications/FS-UAE.app/…/fs-uae`);
  - the cache is absent, or a cached file fails its SHA256;
  - vbcc is absent.
- It is not part of `tests/rec_gate.sh`. The host-only tests
  (`host/tests/test_aros_oracle.py`) are part of `make host-test`. They need
  no emulator: `run_aros` is exercised against a fake FS-UAE (a script that
  plays the boot script). The real FS-UAE legs and the `fetch.sh` harness
  (`tools/aros/test_fetch.sh`, a fake local archive with no AROS bytes) are
  opt-in (`AMIGA64_AROS_ORACLE=1`), so `make host-test` never boots an
  emulator or runs curl.
- `--keep DIR` keeps each AROS run directory: the script, the program's
  output, and RAM: as copied back. It leaves out `C/`, so AROS binaries
  never land under `build/`. `--json` writes the per-program report.

## How a program runs on AROS

For each program, `run_aros()` builds a fresh temporary directory and boots
FS-UAE from it once (`tests/oracle/a500.fs-uae.in`):

- **Machine:** A500 (68000), with the pinned AROS main and extended ROM
  banks.
- **Memory:** 1 MB chip, 512 KB slow and 8 MB Zorro II fast, which is the
  most a 68000 A500 takes. That is enough for `heap_coalesce`'s 7 MB block.
- **Mode:** `headless`, no sound, warp.
- **Disk:** no floppy. The host directory is the bootable `DH0:` (UAE
  filesystem).

Only the manifest's pinned `C/` files are copied to `DH0:C`, and
`find_cache` has verified them. Anything else dropped into the cache's
`C/` is never booted.

The boot script (`tests/oracle/Startup-Sequence.in`) does the following:

1. `FailAt 2147483647`: the largest LONG. AmigaDOS aborts a script when a
   command's RC reaches FailAt, so a lower limit would lose the `$RC` and
   `done.txt` of a program that returns a big code, and the run would look
   like a hang. The only code that still aborts is exactly `$7FFFFFFF`, and
   then the run is reported as a timeout with no `$RC`. Negative codes are
   below any limit. Tested live with 1000, 70000 and -1.
2. `Assign ENV: DH0:env` and `T: DH0:t`. Without `ENV:`, vc.lib's
   `getenv()` → `GetVar` opens an "insert volume ENV:" requester and hangs
   headless.
3. Copy the program's RAMDISK files (`Program.files`) into `RAM:`.
4. `CD RAM:`.
5. Run `DH0:bin/<name> <NIL: >DH0:o/stdout.txt`.
6. `Echo "$RC"`.
7. Copy `RAM:` back to the host.
8. Write `done.txt`.

The host polls for `done.txt`. The emulator is terminated after it appears,
or after `--timeout` seconds (default 120), whichever comes first. A timeout
is itself a DIFF.

Why this environment matches Amiga64's:

- **Current directory and RAM:.** On Amiga64 the RAMDISK is both the
  current directory and `RAM:` (docs/HLE_DOS.md). Running the program in
  `RAM:` gives AROS the same single namespace. A first cut
  (`runs/table1.txt`) ran the program in `DH0:w` without the assigns. There
  `fio_rw` and `fio_remove` were false DIFFs, because `RAM:` was somewhere
  else, and `getenv` timed out (item 2).
- **stdin is `NIL:`.** On Amiga64 stdin is an empty scripted queue, which
  reads as EOF (docs/HLE_CON.md).
- **Binary and memory.** Amiga64 runs the same binary: `run_vbcc.compile_c`
  builds it once and the same bytes go to both sides. Amiga64 also
  pre-fills the heap with `$A5` (`--prg`, as run_vbcc does).
- **Resident commands.** `Echo`, `CD`, `FailAt` and the rest of the shell
  commands are resident in the AROS ROM. `Copy` and `Assign` come from the
  same nightly's ISO (`tools/aros/manifest.txt`).

## What is compared

1. **Did each side exit?** On Amiga64 the program must end in HALT_OK. On
   AROS the run must finish without a timeout and leave an `$RC`.
2. **Exit code:** Amiga64's D0 against AROS's `$RC`, both as u32. This is
   compared unconditionally. A side that did not exit has none, and
   "None" against a number is a difference too.
3. **Screen.** AROS's stdout is rendered by `render_screen()`, a host model
   of `hle_putc` (`runtime/ops/hle.s`), so both sides are compared exactly
   as the C64 screen shows them:
   - 40×25, and the screen scrolls lazily;
   - a newline advances `40 - column` cells, so a full 40-character row
     followed by a newline leaves a blank row;
   - other control bytes are dropped, and bytes ≥ `$80` print as `?`;
   - the default charset has no lower case.

   Every differing row is listed. With `--table`, the rows are compared a
   second time **with case** (`render_text()` against run_vbcc's lines). A
   VICE run yields screen codes only, so with `--prg` case is below the
   comparison's resolution.
4. **Files:** each name in the program's `expect_files` is compared by
   contents, or by absence where the table expects the file not to exist.
   On Amiga64 the data comes from the RAMDISK read back after the run
   (`--prg`) or from the table (`--table`). On AROS it comes from RAM:.

**Teeth.** The host-only tests cover every kind of difference. Each one
must produce a DIFF:
- a wrong or missing line, extra output, a difference on the last row
  only, and a character whose screen code differs even though its text
  rendering does not;
- case;
- the exit code in both directions, including Amiga64 = 0 against AROS ≠ 0;
- a negative `$RC`;
- file contents, a longer file, a file that must not exist;
- a halt on the Amiga64 side, and a timeout on the AROS side.

The screen-model tests check that it agrees with `hle_putc`: the wrap, the
blank row after a full row, the lazy scroll, control bytes.

The triage tests replay each triaged program's recorded AROS output and
require exactly the pinned lines. They also require these to be UNTRIAGED:
- the reviewer's masking cases;
- a vanished pinned line;
- an over-wide volatile value;
- a pinned line that is only a prefix of the actual one.

The SKIP set is pinned to `{tspin}`, so adding a SKIP is an explicit test
change.

Every `find_cache` check (a hash mismatch or a missing file) is tested for
every pinned file.

The fake-FS-UAE tests of `run_aros` cover:
- a timeout, with the emulator killed and no process left;
- a stall after exit, as distinct from a guest hang;
- a missing `rc.txt`;
- exit codes 1000, 70000, 2³¹−2 and −1;
- only pinned commands copied;
- staged files round-tripping through RAM:.

The driver tests pin:
- the exit status and `--strict`;
- the SKIP table: a skipped program is neither compiled nor run;
- the vbcc skip;
- that a stall is retried once and reported, in the summary, the JSON and
  as RETRY-MATCH;
- that a guest hang is not retried.

The opt-in FS-UAE legs run `hello` on AROS and check that it MATCHes, that
the same run against a wrong expectation is a DIFF, and that a binary with
a patched string ("Jello") is a DIFF. They also check that `fio_seek`'s
files make the round trip, that a kept run directory holds no AROS file,
and that exit codes 1000, 70000 and -1 are recorded. The opt-in fetch
harness checks 14 cases of `fetch.sh` failing closed, including an HTTP 404
through `curl -f`.

Evidence is in `build/evidence/w6-oracle/`:
- red: `red/01` (no module) and `red/02` (the verdict logic returns no
  differences);
- green: `green/`;
- first-round mutants: `mut/mutants.txt`;
- `fetch/`;
- the review fix round: `fix1/` (red and green per finding;
  `fix1/green/mutants_fix1.txt`, the reviewer's and the agent's mutants
  ported to the fixed code plus the fix1 mutants; `fix1/green/F8_fetch_mutants.txt`).

## Results

Pinned build: AROS nightly2 **20260921**, amiga-m68k (ROM SHA256s in
`tools/aros/manifest.txt`). FS-UAE 3.2.35 on macOS. Amiga64 at
`wave6/integration` f1ff278 (main b14f204 + the Wave 6 brief). AROS wall-clock is per
program and includes the boot (about 2.5 s). Amiga64 wall-clock is the
VICE warp run of `--prg`.

| Program | `--prg` | `--table` | AROS s | Amiga64 s | AROS `$RC` |
|---|---|---|---:|---:|---:|
| `hello` | MATCH | MATCH | 3.0 | 1.3 | 0 |
| `exit` | MATCH | MATCH | 3.0 | 0.8 | 7 |
| `scroll` | MATCH | MATCH | 3.0 | 3.2 | 0 |
| `alloc` | MATCH | MATCH | 3.2 | 82.6 | 0 |
| `sysbase` | DIFF | DIFF | 2.9 | 1.7 | 0 |
| `getenv` | MATCH | MATCH | 2.9 | 0.8 | 0 |
| `time` | DIFF | DIFF | 4.7 | 1.1 | 0 |
| `system` | DIFF | DIFF | 6.8 | 0.9 | 0 |
| `readitem` | DIFF | DIFF | 6.5 | 0.9 | 0 |
| `putstr` | DIFF | DIFF | 4.6 | 0.7 | 3 |
| `bigstack` | MATCH | MATCH | 4.6 | 0.9 | 0 |
| `leak_open` | DIFF | DIFF | 4.4 | 0.9 | 0 |
| `leak_check` | MATCH | MATCH | 4.0 | 0.8 | 0 |
| `fio_rw` | MATCH | MATCH | 4.4 | 1.7 | 0 |
| `fio_seek` | MATCH | MATCH | 25.6 | 1.6 | 0 |
| `fio_exit` | MATCH | MATCH | 19.0 | 1.3 | 0 |
| `fio_fmt` | MATCH | MATCH | 3.2 | 2.0 | 0 |
| `fio_big` | DIFF | DIFF | 3.2 | 28.3 | 10 |
| `fio_append` | MATCH | MATCH | 3.3 | 1.9 | 0 |
| `fio_remove` | MATCH | MATCH | 3.4 | 1.4 | 0 |
| `fio_reuse` | DIFF | DIFF | 4.6 | 3.7 | 11 |
| `tspin` | SKIP | SKIP | – | – | – |
| `heap_churn` | MATCH | MATCH | 3.2 | 10.1 | 0 |
| `heap_coalesce` | MATCH | MATCH | 3.0 | 1.1 | 0 |
| `heap_pool` | MATCH | MATCH | 4.5 | 29.0 | 0 |
| `heap_realloc` | MATCH | MATCH | 3.3 | 5.4 | 0 |
| `heap_stress1` | DIFF | DIFF | 3.1 | 33.5 | 100 |
| `heap_stress2` | DIFF | DIFF | 3.1 | 51.9 | 100 |

Totals, the same in both modes: **17 MATCH, 10 DIFF (all triaged), 1 SKIP**.
Across the two final runs, the diff lines are identical except for values
that differ from run to run: AROS's ThisTask address in `sysbase` and the
host time in `time`. Evidence is in `build/evidence/w6-oracle/runs/`:
`prg2.txt`/`prg2.json` (Amiga64 in VICE) and `table3.txt`/`table3.json`.
The `--prg` run was made while the full VICE gate ran in parallel. That
load is why the AROS times vary (for example `fio_seek` at 25.6 s, against
about 3.5 s when the machine is idle; `table3`). A whole `--table` pass
takes about 2 minutes. A `--prg` pass takes about 7 to 9 minutes, most of
it the VICE side of `alloc`, `heap_*` and `fio_big`.

### Triage of every DIFF

Categories, per diff line:
- **artifact:** the difference comes from the environment (address layout,
  clock source) or from a white-box fixture. It is not a defect on either
  side.
- **HLE limitation:** Amiga64 knowingly departs from AmigaOS. These are
  documented limits and unimplemented calls; fixtures pin them.
- **AROS divergence:** AROS departs from AmigaOS 3.x, where Amiga64 is right.
- **our bug:** Amiga64 is wrong about AmigaOS semantics, and nobody meant
  it to be.

The exact diff lines and the category of each are pinned in `TRIAGE`
(`tests/aros_oracle.py`). The tool annotates known DIFFs and never
suppresses them. Any line that is not pinned makes the program UNTRIAGED.

| Program | Category (lines) | Evidence and reasoning |
|---|---|---|
| `sysbase` | **mixed**: artifact (ThisTask), **AROS divergence** (4 version rows), **HLE limitation** (MaxLocMem, utility, LibList) | The fixture prints the HLE's own ExecBase and DOSBase. **AROS divergence:** AROS reports exec V51 / dos V50, and `OpenLibrary("dos.library", 41/50)` succeeds. AmigaOS 3.1, the `+aos68k` target, is V40 and returns NULL there, which is what Amiga64 does (docs/HLE.md "why exec says V40"). **HLE limitations:** `MaxLocMem` is a nominal `$200000` (the A500 here has `$100000`), there is no utility.library, and every exec List is empty (`LibList BAD` on AROS). **Artifact:** the ThisTask address is a plain address difference. The rows `AttnFlags=0`, `dos v40 ok` and `dos.fake NULL` match, and they are pinned by being absent from the triage: if any of them changes, it is a NEW line. |
| `time` | artifact | Clock source. The HLE clock is staged at 1978-01-01 (`time=252460800`, docs/HLE.md "Clock"); AROS gets the host's time through UAE. The API-level row `same=1` (two `time()` calls agree) matches. |
| `system` | HLE limitation | The HLE's SystemTagList runs nothing and returns -1 (docs/HLE.md dos table): there is no shell. AROS runs `echo hi` and returns 0. `system(NULL)=1` matches. The child's `hi` comes before the parent's lines because the parent's stdio is fully buffered into the redirected file until exit. |
| `readitem` | HLE limitation (gap) | ReadItem (LVO -810) is not implemented. The fixture pins the halt in the dos sentinel slot (b1-review F1). On AROS it returns ITEM_NOTHING (0) on `NIL:` input: the reference behaviour. |
| `putstr` | HLE limitation (gap) | PutStr (LVO -948) is not implemented, and the halt is pinned. On AROS the exit code is 3 and the output is `before`, `after`, `PutStr works`. PutStr writes through the dos FileHandle buffer (AROS `rom/dos/putstr.c` → `FPuts`; its autodoc says "This routine is buffered"). vc.lib's stdio has its own buffer, so the order depends on when each buffer is flushed, an artifact of the redirect. An implementer of PutStr must decide how it interleaves with stdio. |
| `leak_open` | HLE limitation | 8 file-handle slots (docs/HLE_DOS.md "Handle exhaustion"); AROS opens all 10. See "Limits and risks" on what `leak_check`'s MATCH does and does not show. |
| `fio_big` | artifact | The program needs Amiga64's heap placement: `malloc` must place a buffer across guest `$180000`. On AROS, the first `malloc` returns memory above `$180000` (AROS hands out slow RAM at `$C0xxxx`, as `heap_stress`'s `a=c01118` shows), so the size `want - probe - 64` underflows and that `malloc` fails. The program prints `straddle=0` and exits 10 before any I/O. The reviewer ran a portable variant with the placement removed: on AROS it gave `read 3000 bad=0 eof=1`, rc 0, and a `big.bin` byte-identical to the table's. So the I/O half agrees. |
| `fio_reuse` | HLE limitation | The RAMDISK has 32 directory entries (docs/HLE_DOS.md). AROS RAM: never fills, so the loop stops at its own cap of 41 and `more` opens (exit 11). `rm=0` and `again=open` match, and the files `again`, `f04` and `f31` match. |
| `heap_stress1`, `heap_stress2` | artifact | A white-box test of the HLE allocator. It reads the heap control block at the fixed addresses `$400` and `$100000` (docs/HLE.md "Heap"). On AROS the first structural check fails at once (`FAIL 100 top`, exit 100). |

In total, 10 DIFF programs:
- 4 artifacts: `time`, `fio_big`, `heap_stress1`, `heap_stress2`;
- 5 HLE limitations: `system`, `readitem`, `putstr`, `leak_open`,
  `fio_reuse`;
- 1 mixed: `sysbase`, whose version rows are an **AROS divergence**.

**Our bugs found: none.** No difference shows Amiga64 getting AmigaOS
semantics wrong where it meant to get them right. The oracle found **one
AROS divergence**: AROS answers V51/V50 where AmigaOS 3.1 says V40. The
first report said "no AROS divergence"; that was wrong.

### HLE gaps surfaced by the oracle (backlog)

For each gap, AROS shows the reference behaviour:

1. **ReadItem** (dos -810) is unimplemented. With `NIL:` input AROS returns
   `ITEM_NOTHING` (0) (`readitem`).
2. **PutStr** (dos -948) is unimplemented. AROS returns 0 and buffers
   through the FileHandle (`putstr`).
3. **SystemTagList** (dos -606) has no shell. AROS runs the command and
   returns its RC, 0 for `echo hi` (`system`).
4. **File handles** are capped at 8 slots; AROS has no small fixed cap
   (`leak_open`).
5. **The RAMDISK** holds 32 directory entries in 2 MB; AROS RAM: grows
   (`fio_reuse`).
6. **Libraries and ExecBase lists:**
   - no utility.library (AROS opens V37+);
   - every exec List empty, so a program that walks `LibList` finds
     nothing;
   - `MaxLocMem` is nominal (`sysbase`).

   These are by design for now. They are listed because real programs
   read them.

### SKIP

| Program | Reason |
|---|---|
| `tspin` | timing-dependent: it counts busy-loop spins until the DateStamp tick moves. The count depends on CPU speed against the tick source, not on the API. |

With `--prg`, a program that run_vbcc itself would skip on that build is
also a SKIP, and the reason is shown ("Amiga64 build: W5-H pool wiring
absent", "Amiga64 build: hle_dos.s is the stub"). On `wave6/integration`
there are none.

## Limits and risks

- **Screen resolution.** With `--prg` the comparison cannot see case. The
  only screen is the final one: the last 25 rows, 40 columns wide. A
  difference that scrolled off the top is invisible, as it is on Amiga64
  itself.
- **Buffered stdio only.** AROS output goes to a file, so vc.lib's stdio
  is fully buffered there. On Amiga64 it goes to the console.
  Differences in when output is flushed (see `system`, `putstr`) show up as
  reordering, not as lost output.
- **The pin will rotate away.** SourceForge deletes old nightlies. When
  the pinned URL 404s, `fetch.sh` fails with a message, and until then the
  oracle keeps working from the verified cache. Re-pinning to a newer
  nightly can change AROS behaviour: re-run and re-baseline this table.
- **FS-UAE headless on macOS** is not a documented FS-UAE mode, but the
  `headless` option works in 3.2.35. Every run is bounded by `--timeout`,
  and the emulator is killed at the end. One infrastructure stall was seen
  in about 180 AROS runs made for this package. It happened in
  `runs/prg1.txt`, on a machine loaded with VICE: `heap_realloc` exited (`RC=0` and its output written),
  but the boot script never reached `done.txt` within 120 s. Afterwards,
  30 back-to-back runs on an idle machine (`runs/flake1.txt`) and every
  final run were clean. Mitigation:
  - **Only a stall is retried:** a timeout where the program provably
    exited, because its `$RC` was recorded. Such a run is re-run once
    (`--retries`, default 1).
  - **A guest that never exits is not retried.** No `$RC` means a hang or
    crash, and it is reported as a DIFF at once.
  - **Retries are visible.** A program that matches only on the retry is
    `RETRY-MATCH`, counted in the summary line and the JSON, and
    `--strict` fails it.
  - **The timeout message** says which case it was: "the boot script
    stalled after it" or "the program did not exit".
  - A second stall stays a DIFF.

  The first-cut `getenv` hang was a different thing: the `ENV:` requester
  (fixed above).
- A crashing guest program on AROS shows up as a timeout: the guru or
  requester waits forever headless. It is reported as a DIFF with
  "timed out … (the program did not exit)", never as a MATCH, and it is
  not retried.
- **`leak_check` shows less on AROS than on Amiga64.** Each AROS run is a
  fresh boot, so nothing can leak from `leak_open` into `leak_check` there.
  Its MATCH shows only that `open ok` works on a clean system; it says
  nothing about isolation. On Amiga64 the two share one session, and the
  isolation is what `run_vbcc` tests.
- **`fio_rw`'s `RAM:ram.txt` → absent check is vacuous on AROS.** Host file
  names never contain `:`, so that name is always absent on the AROS side.
  The real `RAM:` alias is checked through `ram.txt` (`ram=via RAM:`
  matches, and the file's contents match).
