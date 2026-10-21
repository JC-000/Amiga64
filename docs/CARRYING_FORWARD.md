# Carrying Amiga64 forward

Amiga64 was built in a series of development waves. Wave 17 was the last
one: it produced the `amiga64` command, the prebuilt binaries, the bundled
AROS ROM and the documentation, and version 0.17.0. The project is
complete as what it set out to be, a proof that Amiga 500 software can run
on a C64 with an Ultimate board. This page is for anyone who wants to
continue the work. It covers:

1. [a map of the code and the docs](#a-map-of-the-code-and-the-docs);
2. [building and testing](#building-and-testing);
3. [the ground rules that kept the project clean](#ground-rules);
4. [what was planned but not built](#what-was-planned-but-not-built),
   with where the design or the evidence lives;
5. [how to contribute](#how-to-contribute).

Some docs cite evidence as `<name>/build/evidence/...`. Those are local
run logs from the development machine that were never committed (README
"Documentation"). Evidence you can rerun lives in `tests/` and
`host/tests/`.

## A map of the code and the docs

### The directories

| Path | What it holds |
|---|---|
| `runtime/` | the 6502 side, in ca65 syntax: `core.s` (the 68000 interpreter's core: fetch, dispatch, effective addresses, the memory API), `ops/*.s` (instruction groups `g1_move` to `g6_muldiv`; the chipset `chip`, `cia`, `cop`, `blt`, `c2v`, `disp`, `mach`; input `inp`; the trackdisk `td`; snapshots `snap` and `ureu`; the program-mode HLE `hle*`, `dosio_*`), `rc/*.inc` (every claimed address: `zp`, `ram`, `guest`, `machine`), `c64.cfg` (the ld65 memory map), `macros.inc` |
| `tests/runner/` | `runner.s` and `run_program.s`, the test and program entries linked into `build/runner.prg` |
| `tests/aros/`, `tests/paula/`, `tests/modwb/`, `tests/mod/`, `tests/irq/`, `tests/mach/`, `tests/cia/`, `tests/pf/`, `tests/mturbo/` | the machine-mode modules assembled at run time against `runner.lbl` (`mboot.s`, `td.s`, `spr.s`, `inpm.s`, `aud.s`, the ModPlayer stand-in) and the test drivers and shims |
| `tests/*.py` | the VICE and device legs (`aros_boot`, `aros_wb`, `aros_resume`, `aros_modwb`, `run_*`), the vector runner, the gate (`rec_gate.sh`), `mk_prebuilt.py`; `tests/bench/u64_session.py` holds `u64_hold`, the device hold every device leg uses |
| `host/` | Python on the host: the Hunk loader and image builder, the vector pipeline, the reference models (`chipset_ref`, `c2v_ref`, `inp_ref`, `paula_ref`, `mod_ref`), the snapshot format (`snapfmt`), the ADF tools (`adf_overlay`, `adf_files`), `modgen`, `rescheck`, `prebuilt` |
| `host/tests/` | the pytest suite (`make host-test`) |
| `amiga64/` | the user-facing CLI: `cli.py`, `config.py`, `device.py`, `adf.py`, `session.py`, `kit.py`; it calls the `tests/` and `host/` code in-process. `setup.py` ships that tree inside the wheel as `amiga64/_tree/` |
| `fixtures/` | guest-side sources and data: `c/` (vbcc test programs), `mod/` (the 68k MOD replay and its UI), `modwb/` (ModPlayer, the Workbench launcher), `inp/`, `vectors/` (SingleStepTests samples), `bench/` (recorded sweeps) |
| `prebuilt/` | the shipped C64-side binaries and their `MANIFEST` (written by `make prebuilt`, never by hand) |
| `third_party/aros/` | the AROS m68k ROM banks, unmodified, with `LICENSE` (APL 1.1), `ACKNOWLEDGEMENTS` and `SOURCE.txt` |
| `tools/aros/` | `manifest.txt`: the SHA-256 of every bundled AROS file in `third_party/aros/` (nothing is downloaded) |
| `tools/vbcc/` | `install.sh`: the pinned vbcc, vasm and vlink, installed outside the repository |
| `tools/s3probe/` | the Wave 6 boot model (`aros_boot_model.py`, an A500 model running the AROS ROM) and the FS-UAE probes |

### What to read, in order

1. [README](../README.md), then [docs/guide/QUICKSTART.md](guide/QUICKSTART.md):
   run it once if you have a device.
2. [docs/emulation/OVERVIEW.md](emulation/OVERVIEW.md): the two modes
   (program mode and machine mode) and what lives where.
3. [docs/emulation/LIMITS.md](emulation/LIMITS.md): what does not work, and
   its "Open items".
4. The area you want to work on: [CPU](emulation/CPU.md),
   [MEMORY](emulation/MEMORY.md), [CHIPSET](emulation/CHIPSET.md),
   [PAULA](emulation/PAULA.md), [CIA_AND_TIMING](emulation/CIA_AND_TIMING.md),
   [INPUT](emulation/INPUT.md), [STORAGE](emulation/STORAGE.md),
   [HLE](emulation/HLE.md).
5. [docs/emulation/TESTING.md](emulation/TESTING.md) and
   [docs/RESOURCES.md](RESOURCES.md) (address claims and `make rescheck`).
6. [docs/CONTRACTS.md](CONTRACTS.md), as a reference: the binding
   interfaces, section by section. Where any other doc disagrees with it,
   CONTRACTS and the code win.
7. [docs/HARDWARE.md](HARDWARE.md) for every number measured on the
   devices, and [docs/PERF.md](PERF.md) for the cycle accounting.
8. The `docs/WAVE*_BRIEF.md` files when you want to know why something is
   the way it is. They are history: binding for their wave, kept unchanged.

## Building and testing

### Toolchain

| Tool | Version | Used for | From |
|---|---|---|---|
| Python | 3.11+ | everything on the host | |
| cc65 (`ca65`, `ld65`, `cl65`) | **2.18** | the runner and the 6502 modules | `prebuilt/MANIFEST` `#: tool` lines; CONTRACTS "Toolchain" |
| vasm (`vasmm68k_mot`) | **2.0f** | the 68k guest programs (ModPlayer, the MOD replay, test stubs) | `tools/vbcc/install.sh`, `prebuilt/MANIFEST` |
| vlink | **0.18a** | linking them | the same |
| vbcc | 0.9h patch 3 | the C test programs (`fixtures/c/`), not the shipped binaries | `tools/vbcc/README.md` |
| VICE `x64sc` | with a 16 MB REU | the VICE legs | |
| c64-test-harness | master (v0.17.0 pins **v0.13.1**) | every VICE and device access | `pyproject.toml` |
| FS-UAE | | only the optional oracles | docs/ORACLE.md |

`tools/vbcc/install.sh` installs vbcc, vasm and vlink under
`~/.local/opt/vbcc` (override `VBCC_PREFIX`), pinned by SHA-256. The
guest builds find them through `VBCC` (`fixtures/modwb/Makefile`).

### Everyday commands

```sh
python3 -m pytest host/tests -q -p no:cacheprovider   # the host suite (= make host-test)
make runtime                                          # build/runner.prg, .lbl, .map, stamped
make vice-test                                        # golden + hand vectors in VICE
make rescheck                                         # the address-claim checker
make prebuilt-check                                   # prebuilt/ still matches the sources
sh tests/rec_gate.sh build build/gate                 # the full VICE gate, one log per step
```

The host suite at the end of Wave 17 is **2321 passed, 16 skipped** in a
clean checkout. A stale `build/` can fail tests that read it, so run from
a clean tree. The opt-in tests (`AMIGA64_VICE_TESTS=1`,
`AMIGA64_NET_TESTS=1`, `AMIGA64_CHIP_ORACLE=1`, `AMIGA64_AROS_ORACLE=1`)
are listed in [TESTING](emulation/TESTING.md#everyday-commands).

### VICE tests

The VICE legs run the real `build/runner.prg` in `x64sc` with a 16 MB REU,
through the harness's binary monitor. The `aros_*` tools default to VICE;
other tools take `--backend vice` or follow `C64_BACKEND`
([TESTING](emulation/TESTING.md#the-pieces)). They are slow: a VICE AROS
boot to the insert-disk screen takes about 174 s of warp, and a Workbench
boot several minutes. VICE has no sampler and says nothing about speed: it
proves correctness only (PERF "(e) Caveats").

### Device tests

Device legs run the same runner on a U64E or a C64U. These rules held for
the whole project; keep them:

- **Only through c64-test-harness.** Every hold takes the harness's
  cross-process **DeviceLock** and runs inside `u64_hold`
  (`tests/bench/u64_session.py`). It snapshots every device setting on
  entry and restores it on exit, then re-reads the configuration: the
  hold's report must say **IDENTICAL**. No raw REST, curl or FTP.
- `U64_BASELINE_ON_ENTRY=0` is required: it stops the harness from
  applying its factory baseline on entry. The test tools take the
  device's address from `U64_HOST`.
- **Never open the Ultimate's on-screen menu during a hold.** It once
  wedged a C64U until a power cycle (docs/HARDWARE.md "Wave 14 hardware
  findings").
- **Never `pkill` or `killall`.** Stop only the processes you started.
  Long runners reinstall `signal.default_int_handler`, give every leg a
  deadline, turn SIGTERM into the restore path, and write DONE markers.
- **Do not switch the Ultimate DMA Service (socket DMA) on from a tool.**
  It is a network service the device's owner controls. `amiga64 doctor`
  only reports it (`amiga64/cli.py`); the user turns it on.
- **Keep `/Temp` clean on C64U firmware 1.1.0.** Every program start leaves
  a firmware file in `/Temp`, which is a RAM disk; a full `/Temp` can
  crash a C64U. The U64E's 3.15 firmware collects them itself; on the C64U
  the harness sweeps them under its lock and keeps the newest
  (`U64_TEMP_GC_KEEP`). Never write to `/Temp` or `/Flash` yourself, and
  check `amiga64 doctor`'s `/Temp` line after a series of runs
  ([DEVICES](guide/DEVICES.md#files-the-session-writes-on-the-device)).
- One holder per device at a time; the DeviceLock queue decides. Tools
  queue behind each other only when they lock under the same key
  ([DEVICES](guide/DEVICES.md#other-harness-tools-on-the-same-device)).
- Speed claims come from devices only, never from VICE warp.

### The prebuilt binaries

Users run `prebuilt/` so they need no cc65 or vasm. `host/prebuilt.py`
loads it when `AMIGA64_PREBUILT=1` or a tool is missing, and builds from
source when `AMIGA64_PREBUILT=0` or the toolchain is present. Every file is
checked against `prebuilt/MANIFEST` on load: the SHA-256 of each file, the
runner's build ID, every module ID, and the toolchain versions that made
them. A prebuilt module is valid only with the prebuilt runner, because the
modules are assembled against `runner.lbl`.

To refresh them after a change to the 6502 or 68k sources:

1. Install the pinned toolchain (cc65 2.18, vasm 2.0f, vlink 0.18a). Another
   version writes other bytes and other `#: tool` lines.
2. `make prebuilt` (`tests/mk_prebuilt.py`): it builds every artifact with
   `AMIGA64_PREBUILT=0`, from the fixed directory `build/prebuilt-src`, so
   the map file is byte-identical, and writes `MANIFEST`.
3. `make prebuilt-check`: it rebuilds into a temporary directory and fails
   on any difference. `host/tests/test_prebuilt.py` rebuilds byte for byte
   when the installed toolchain matches the manifest (otherwise that test
   skips) and verifies the manifest on every run.
4. Commit `prebuilt/` with the source change. A new runner has a new build
   ID, so saved sessions from the old build are refused on resume, as
   designed.

## Ground rules

These are the habits that kept the project clean and publishable. They are
advice now, not orders, but breaking them is how a project like this gets
into trouble.

- **Stay clean-room.** No AROS source and no Amiga NDK, ever, in the
  repository or in your reasoning while you write code. Since Wave 10 the
  briefs have forbidden reading the AROS sources at all (the Wave 6
  planning documents, S3_PLAN and WB_BOOT, still cite AROS source files
  from the analysis done before that rule). Work from public
  documentation: hardware references, the documented library offsets and
  structures, the Hunk, ADF and `.info` formats. Cite what you used.
- **The AROS ROM is a binary.** It is bundled only as the unmodified
  pinned files in `third_party/aros/`, under the AROS Public License 1.1,
  with its licence, acknowledgements and source notice next to it. Patch
  it only at run time in the REU, as the trackdisk module does; do not
  commit a modified ROM or a disassembly of it.
- **Never bundle Commodore or Amiga, Inc. material.** No Kickstart ROMs, no
  Workbench disks, no commercial Amiga software, no NDK. Users supply
  their own Workbench ADF; it is copied only to their own Ultimate, and
  only when they confirm. Test music is generated by `host/modgen.py`.
- **Keep device identifiers out of commits.** No real IP addresses, MAC
  addresses, device unique IDs, host names or home paths in code, docs,
  logs or fixtures. Use the RFC 5737 documentation addresses (192.0.2.x,
  198.51.100.x) in examples, and take real addresses from the environment
  (`U64_HOST`, `AMIGA64_HOST`) or the config file.
- **Simplify; don't chase compatibility.** The project's direction is to
  pursue simplifications, not compatibility edge cases: a trimmed
  Startup-Sequence, a patched trackdisk instead of disk DMA, a copper
  without beam timing. Choose the change that makes the next real program
  work, and record what you knowingly left out in
  [LIMITS](emulation/LIMITS.md).
- **Red first, then green, then an adversary.** Write the test, see it
  fail, make it pass, add a mutant the test must kill, and have someone
  else try to break the change ([TESTING](emulation/TESTING.md#writing-a-new-test)).
- **Measure before you optimise.** Every speed claim in the docs is tagged
  [MODEL], [EST] or measured; keep that distinction.

## What was planned but not built

Every item here was planned, proposed or deferred somewhere in the
repository and is not in the code at the end of Wave 17. Each says what is
known and where the design or the evidence lives. Sizes are given only
where the repository itself estimates them; most items have no estimate.
"No write-up" means the idea was noted during development but the
repository holds no design for it.

### Emulation core

- **M4: the ahead-of-time 68k→6502 translator.** Not started. The plan
  ("S2") is a Python translator that lifts a Hunk executable's 68000 code
  into 6502 code, with the interpreter as the fallback for self-modifying
  code, computed jumps and code/data ambiguity; M4 v0 would translate
  straight-line basic blocks and fall back at block boundaries. The
  research-phase estimate is **5-20× over interpretation**. Where:
  docs/ARCHITECTURE.md "S2: Ahead-of-time translation", "Milestones" item
  5; README "Status".
- **Interpreter speed levers left open.** docs/PERF.md "(d) Top
  optimization levers":
  - trimming the zero-page copies around `ea_load`/`ea_store`/`m_val`
    (not estimated);
  - a fused read-modify-write entry, which would save only the 41-cycle
    compare path;
  - shift-count clamps for LSL/LSR/ASL, **about 1-2% on the typical mix**,
    large in shift-heavy code. Only ASR short-cuts a count above the
    operand width today (`runtime/ops/g4_shift.s`, `asr_big`).
- **Faster AROS boot by skipping the ROM checksum and the RomTag scan.**
  Not built. About 1.07 M instructions, **about 16-21 s at 48 MHz** [EST],
  on every boot. The plan assumed a modified ROM; since the bundled ROM must
  stay unmodified, it would have to be a run-time patch. Where:
  docs/WB_BOOT.md §4.4 and §4.6.
- **MAIN is full.** The runner's main area has almost no room left (Wave 16:
  about 59 B of CODE margin; Wave 17 added no MAIN bytes), and the Wave 14
  addendum already recorded that MAIN needs restructuring. New
  6502 code went into machine-mode modules instead. Any sizeable new
  feature in the core needs that restructure first. Where:
  docs/WAVE14_BRIEF.md "Addendum: state at landing" (MAIN layout limit);
  docs/WAVE16_BRIEF.md "Operating rules".
- **A layout-independent placement for the td vectors.** A follow-up from
  Wave 14: the `FETCH_DIRECT` variant build moved `td_fvec` onto a page end,
  and the vector asserts are now product-layout only. Where:
  docs/WAVE14_BRIEF.md addendum, "Variant layout".
- **CPU gaps, by design.** TAS and MOVEP halt; address and bus errors, the
  trace bit and 68010+ instructions are not modelled. Where:
  [LIMITS "CPU"](emulation/LIMITS.md#cpu).
- **A 68k soft core in the Ultimate's FPGA ("S5").** Only an open
  question: "out of scope until the software route is mapped". Where:
  docs/ARCHITECTURE.md "Open questions".

### Display

- **Readable text in the fitted view.** The user's verdict on AROS's text
  was "visible, but too compressed to read": in the fit view each C64 pixel
  is the OR of two hires pixels, so 8-pixel Topaz glyphs become 4 pixels
  wide. F1's 1:1 view is the workaround and is pixel-exact for two-colour
  text (Wave 13). Three ideas were noted:
  - **hires rendering for two-colour text cells** in the fit view: no
    write-up. The 1:1 view's per-cell two-pen rule is the starting point
    (docs/WAVE13_BRIEF.md "W13-D", [CHIPSET](emulation/CHIPSET.md)).
  - **interlace on the insert-disk screen:** the fit view shows one field
    of an interlaced screen, so every second line of Topaz is lost; the
    1:1 view shows both. Combining the fields in the fit view has no
    write-up. Where: CHIPSET "Vertically: the fit view shows the top";
    CONTRACTS "Fit-view crop"; docs/WAVE13_BRIEF.md addendum ("Scope
    added").
  - **colour choice weighted by pen coverage:** no write-up. Today a hires
    cell's foreground is the pen of its first set pixel, so a pen that is
    never first does not show (pen 3 on the insert-disk screen). Where:
    CHIPSET "Colours"; CONTRACTS "Colour choice".

  Related: the 1:1 view cannot pan to the insert-disk text, because AROS's
  pointer there is empty and an empty pointer never moves the window
  (docs/WAVE14_BRIEF.md addendum; CONTRACTS "Pan gate"). Guest programs
  can already be readable by following [docs/UI_READABLE.md](UI_READABLE.md).
- **16-colour hires fidelity ("v1").** The fit view is a silhouette with one
  foreground per cell; v1 would treat the planes per palette entry. The
  plan's estimate: **v1 costs 2-4×** the conversion [EST]. Where:
  docs/S3_PLAN.md §2.1 "Modes" (the "any depth 3-5" row) and §5 "Risks",
  item 4.
- **VIC-II FLI/IFLI modes for closer OCS colours.** An open question,
  never explored. Where: docs/ARCHITECTURE.md "Open questions".
- **Tracking CPU-drawn pixels.** The picture is reconverted only on a
  display-register change, a blit or an epoch bump; CPU writes to
  bitplanes are not seen. Backlog since Wave 10; Wave 11 said not to add a
  write hook to `core.s` for it; Wave 15 left it open for a proposal. Where:
  docs/WAVE11_BRIEF.md C3; docs/WAVE15_BRIEF.md addendum ("W15-U"); CONTRACTS
  "Policy v0".
- **ModPlayer in a Workbench window ("option B").** Wave 16 chose the
  full-screen takeover; an OS-friendly window was set aside because it
  "needs readable hires text in the converter". Where:
  docs/WAVE16_BRIEF.md "Goal".
- **Display modes and chips not modelled.** HAM, dual playfield, EHB,
  lores with 1 or 3-6 planes, sprites 1-7, copper beam timing, blitter
  line drawing; the blitter's D-overlap and odd-modulo divergences from
  FS-UAE are pinned as accepted v0 limits with "model and blt.s fixes"
  in the backlog. Where: [LIMITS "Chipset and display"](emulation/LIMITS.md#chipset-and-display);
  docs/WAVE11_BRIEF.md "Addendum 4".

### Audio

- **A per-device sampler RATE constant for the C64U.** The C64U's sample
  time is (rate + 1) × 160 ns + 80 ns, against the U64E's + 120 ns, but the
  module picks RATE from the U64E's law, so the C64U plays sharp: +0.016%
  at PER 856, **+0.12% at PER 113 (about 2 cents)**. Measured, not applied;
  the host-side expectation is already per device (`host/paula_ref.sample_c`).
  Where: docs/HARDWARE.md "The C64U's sampler"; LIMITS "Open items" 6.
- **An exact real-time E clock for the player profile.** The Wave 15
  player profile runs with E = 1.0227 MHz as a known deviation; "an exact
  real-time E in mach.s (option a)" is in the backlog. Where:
  docs/WAVE15_BRIEF.md "Addendum: supervisor rulings" (time base).
- **Paula features not modelled:** AUDxDAT (CPU audio), ADKCON modulation,
  a channel that was playing at a suspend resumes silent, rare loop-change
  limits. Where: [LIMITS "Audio"](emulation/LIMITS.md#audio).

### Input, disk and the OS

- **An Amiga keyboard.** Wave 13 deferred it and kept C= free as the right
  button so that a keyboard map could use the rest; it was never built. Only F1
  (view) and C= (right button) reach the guest in machine mode, so you
  cannot type into AROS. Where: docs/WAVE13_BRIEF.md "W13-I", I3 (view keys);
  [INPUT](emulation/INPUT.md); LIMITS "Input".
- **Workbench usability at this speed.** In the final demo (C64U, 64 MHz,
  1351 mouse, joystick, ModPlayer) everything worked, but the disk window
  redraws from scratch a few times while it fills and a double-click
  during a redraw is lost; double-clicks may also need to be slower than
  usual (not pinned down). No change was made; the guide tells users what
  to expect. Where: LIMITS "Open items" 3; QUICKSTART step 6.
- **A second Amiga port** (a joystick as an Amiga joystick, or a second
  mouse): none. Where: LIMITS "Input".
- **Disk: writes, more drives, disk change, a live file channel.** The drive
  is write-protected, DF0 only, no disk change while running, and there is
  no live path between the host's files and a running Workbench (Wave 17
  ruling: "No new host-to-guest channel this wave"). Real disk DMA is not
  emulated; the trackdisk device is patched instead (leg `nohle` is the red
  control). Where: [STORAGE](emulation/STORAGE.md);
  [DISKS_AND_FILES](guide/DISKS_AND_FILES.md); docs/WAVE17_BRIEF.md
  rulings; LIMITS "Disk and files".
- **Other Workbench versions.** Workbench 3.1 Install and 1.3 booted in the
  boot model and in FS-UAE on the same ROM, never on Amiga64; §4.7 lists
  what 1.3 would miss. Where: docs/WB_BOOT.md §0, §4.7.
- **Program-mode HLE gaps.** ReadItem, PutStr, SystemTagList (no shell),
  the 8-handle cap, the 32-entry RAM disk, no utility.library, empty exec
  lists (docs/ORACLE.md "HLE gaps surfaced by the oracle (backlog)"); a
  real clock, GetVar, more libraries (docs/HLE.md "Still missing (next)";
  its console keyboard item was built later, docs/HLE_CON.md); a runner-level HLE init
  hook, Rename, Lock/Examine (docs/HLE_DOS.md "Limitations / open items").
  These matter only for programs run without AROS.

### Devices and the harness

- **A program start that leaves nothing in `/Temp`.** Tried and parked:
  the check before `SYS` cannot verify a body at or above `$A000`, which
  reads back as BASIC ROM (c64-test-harness #532), and a check after the
  start fails because the runner rewrites itself. A fix to #532 would
  unblock it. Until then the harness sweeps `/Temp` on the C64U. Where:
  LIMITS "Open items" 1; `host/tests/test_amiga64_budget.py`.
- **Networks that filter ICMP** (c64-test-harness #536): the harness pings
  before it takes the lock, so a reachable device can be reported
  unreachable. Where: LIMITS "Open items" 4.
- **Split device-lock keys:** tools that reach one device under different
  keys do not queue behind each other. Where: LIMITS "Open items" 5;
  [DEVICES](guide/DEVICES.md#other-harness-tools-on-the-same-device).
- **PAL machines.** Untested; everything was measured on NTSC. Derived
  only: guest time about 4% slower and the player's tempo about 4% off.
  Where: LIMITS "Open items" 2; DEVICES "Supported devices".
- **Unmeasured and untested:** the absolute φ2 frequency of either device,
  the keyboard's row recovery at turbo on the U64E, non-quick trackdisk
  writes, firmware versions and Ultimate models other than the two project
  devices. Where: LIMITS "Untested or unmeasured", "Open items" 8.
- **A standalone version that needs no computer.** The user's Wave 17
  decision was "a host CLI first"; the standalone version was scoped for
  later and never designed. No write-up. Where: docs/WAVE17_BRIEF.md "The
  user's decisions".

### Tooling and hosts

- **Windows hosts:** not supported, a documented non-goal (Wave 17 ruling).
  **Linux hosts:** expected to work, never tested; all development ran on
  macOS. Where: LIMITS "Scope", "Untested or unmeasured".
- **A missing staged file is reported without saying it is staged** ("no
  such file or directory"; `amiga64 adf rm` removes it). A review nit that
  was not fixed. Where: LIMITS "Open items" 7.


### Settled: do not redo

These looked open in older documents and are done:

- **The full AROS ROM boot.** The unmodified ROM boots to its insert-disk
  screen (Wave 11) and, with the user's Workbench 3.1 disk, to the
  Workbench (Wave 13), with supervisor mode, exceptions, interrupts and
  the CIAs (S3). What is left on that road is in the lists above (disk
  DMA, the keyboard).
- **The 1351 clamp margin** (Wave 14 follow-up): the per-window travel
  budget is 63 counts since W16-I (CONTRACTS "The per-window travel
  budget").
- **The 1351 at turbo:** the 2026-10-11 pot compression was not
  reproduced on 2026-10-12 (docs/HARDWARE.md, CONTRACTS "Input").
- **Starvation at s = 0:** explained as the arithmetic of a 1 MHz host and
  pinned by leg `s0` (CONTRACTS "C4 (machine loop)"; PERF (l)).
- **The code-window cache** recommended by WB_BOOT §4.5: built in Wave 12
  (seven windows).

## How to contribute

- **Issues and pull requests** go to
  [JC-000/Amiga64](https://github.com/JC-000/Amiga64). Attach
  `amiga64 doctor`'s output and the session folder under
  `~/.local/state/amiga64/holds/` to a bug report, never an ADF or a saved
  session file ([TROUBLESHOOTING](guide/TROUBLESHOOTING.md#reporting-a-problem)).
- **Tests come with every change.** A pull request should carry a test that
  failed before the change, keep `python3 -m pytest host/tests` green, and
  say which VICE or device legs were run and with what result. A change to
  the 6502 or 68k sources also refreshes `prebuilt/` and passes
  `make prebuilt-check`. Device results should name the device model and
  firmware, never its address or ID.
- **Interfaces change in the open.** A change to a binding interface
  updates [CONTRACTS](CONTRACTS.md), the code and the tests in the same pull
  request; a new fixed address goes into `runtime/rc/*.inc` and passes
  `make rescheck`. Record a deliberate deviation or a new limit in
  [LIMITS](emulation/LIMITS.md).
- **The harness is a separate project.**
  [c64-test-harness](https://github.com/JC-000/c64-test-harness) owns VICE,
  the device clients, the DeviceLock and device storage. Amiga64 tracks its
  master (the v0.17.0 tag pins v0.13.1, a known-good environment). Report
  what Amiga64 needs from it there; do not patch it locally.
- **Licence.** Amiga64's own code and documentation are MIT
  ([LICENSE](../LICENSE), [NOTICE](../NOTICE)); contributions are taken
  under the same licence. The AROS ROM in `third_party/aros/` stays under
  the AROS Public License 1.1, and the CPU test vectors keep their
  upstream status (`fixtures/vectors/UPSTREAM_LICENSE`).

Sources: the documents and sections named in each item; `Makefile`;
`tests/mk_prebuilt.py`; `host/prebuilt.py`; `prebuilt/MANIFEST`;
`tools/vbcc/install.sh`; `pyproject.toml`; `setup.py`;
`tests/bench/u64_session.py`; `amiga64/device.py`; docs/WAVE17_BRIEF.md.
