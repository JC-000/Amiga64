# Amiga64 emulation: overview

Amiga64 runs Amiga 500 software on a Commodore 64 with an Ultimate
cartridge-class FPGA board: the **C64 Ultimate** (C64U) or the **Ultimate 64
Elite** (U64E). Everything that executes on the C64 side is 6502 code
written in ca65 assembly. The Amiga's memory lives in the Ultimate's 16 MB
REU. A Python host program drives the device over the network through the
[c64-test-harness](https://github.com/JC-000/c64-test-harness) package.

This directory explains how the emulation works, so that someone new can
carry it forward. It is written from the code and from
[`docs/CONTRACTS.md`](../CONTRACTS.md). CONTRACTS.md is the binding
interface document: when this text and CONTRACTS.md disagree, CONTRACTS.md
and the code are right, and this text is a bug.

| File | What it covers |
|---|---|
| [CPU.md](CPU.md) | the 68000 interpreter: register file, dispatch, fetch window and code cache, exceptions, speed |
| [MEMORY.md](MEMORY.md) | the REU as guest memory, the memory API, the I/O trap, both guest maps, the C64 RAM map |
| [HLE.md](HLE.md) | high-level emulation of exec/dos/console for programs run without an OS |
| [CHIPSET.md](CHIPSET.md) | custom registers, copper, blitter, the planar-to-VIC display, the pointer sprite |
| [PAULA.md](PAULA.md) | audio: the Paula model and its mapping onto the Ultimate's sampler |
| [CIA_AND_TIMING.md](CIA_AND_TIMING.md) | the time base, guest time, event slots, interrupts, CIA-A/B, the profile block |
| [INPUT.md](INPUT.md) | joystick and 1351 mouse to Amiga mouse, buttons, the F1 view key |
| [STORAGE.md](STORAGE.md) | the HLE trackdisk, ADF staging, the overlay, UCI REU I/O, snapshots |
| [TESTING.md](TESTING.md) | how everything is tested: vectors, VICE and hardware legs, the oracle, mutants |
| [LIMITS.md](LIMITS.md) | one list of what is not modelled, known deviations and what is untested |

For using the emulator rather than developing it, see
[`docs/guide/`](../guide/QUICKSTART.md).

## The two modes

The same runner PRG (`build/runner.prg`) serves two quite different kinds of
session.

| | Program mode | Machine mode |
|---|---|---|
| What runs | one Amiga Hunk executable (for example a vbcc-compiled C program) | the AROS Kickstart-replacement ROM, then Workbench from the user's ADF |
| Operating system | none: exec, dos and console calls are serviced natively in 6502 code (HLE) | AROS, running as guest code |
| Guest memory map | `runtime/rc/guest.inc` | `runtime/rc/machine.inc` |
| CPU loop | `emu_step`/`emu_run`, exceptions off (`st_exc` = 0) | `mach_run`, exceptions on (`st_exc` = 1) |
| Chipset | trapped registers; the copper and display run when the program uses them | everything in [CHIPSET.md](CHIPSET.md), plus interrupts, CIAs and guest time |
| Entry | `test_run_program` (`tests/runner/run_program.s`) | `aros_boot` in the test-time driver `tests/aros/mboot.s` |
| Typical tools | `tests/run_program.py`, `run_vbcc.py`, `run_dos.py`, `run_con.py` | `tests/aros_boot.py`, `aros_wb.py`, `aros_resume.py`, `aros_modwb.py` |

Program mode came first (milestones M0-M2). Machine mode came with the
chipset work (supervisor mode in Wave 6, S3 in Waves 7-11) and is what the Workbench demo uses. The
interpreter core is the same in both; the differences are which guest map
the host stages and which entry it calls.

Sources: CONTRACTS "Guest model", "M0 loader", "Machine profile and AROS
boot (S3.7)"; `tests/runner/run_program.s`; `tests/aros/mboot.s`.

## What lives where

**On the C64** (details in [MEMORY.md](MEMORY.md)):

- **The runner PRG** loads at `$0801` as one contiguous image: the main
  area to `$9FFF`, then the banked areas under the BASIC ROM from `$A000`,
  ending inside `$B100-$B5FF` (`runtime/c64.cfg`). It holds the interpreter core (`runtime/core.s`), the
  instruction families (`runtime/ops/g1_move.s` … `g6_muldiv.s`), the HLE
  (`hle*.s`, `dosio_*.s`), the chipset (`chip.s`, `cia.s`, `cop.s`,
  `blt.s`, `disp.s`, `c2v.s`), the machine loop (`mach.s`), input
  (`inp.s`), the trackdisk state (`td.s`), and suspend/resume (`snap.s`,
  `ureu.s`). It is stamped with a build ID after the link
  (`host/snapfmt.py --stamp`, Makefile).
- **Machine-mode modules** are small 6502 binaries assembled at session
  time against the runner's label file and copied into C64 RAM, mostly
  the `$C000-$CFFF` buffers (the audio module's segment B uses the unused
  text screen at `$0400`): the boot driver `tests/aros/mboot.s`, the trackdisk code
  `tests/aros/td.s`, the pointer sprite `tests/aros/spr.s`, the 1351 module
  `tests/aros/inpm.s` (two segments) and the audio module
  `tests/paula/aud.s`. They exist because the PRG's main area is full
  (CONTRACTS "Machine profile", "buf_case in machine mode"; Wave 15 and 16
  briefs, "MAIN is full"). The td, pointer, 1351 and audio modules carry
  an ID that the runner checks before it calls in, so a stale or
  overwritten module is refused rather than run (the boot driver is called
  only by the host and has none).

**In the REU** (details in [MEMORY.md](MEMORY.md)): guest RAM and ROM, a
copy of the disk image, and some private data that the guest can never
address because it lies under the trapped `$DF` bank (the palette table,
the snapshot area, a zero row).

**On the host:** Python in `host/` (format tools and reference models) and
`tests/` (the session drivers). The Wave 17 `amiga64` command line tool is
the user-facing entry; see [`docs/guide/`](../guide/QUICKSTART.md).

## A Workbench session, step by step

This is what `tests/aros_wb.py` does for a Workbench boot (legs `w`,
`demo-boot`, `modwb`), in order. Function names are from that file and
`tests/aros_boot.py` unless marked.

1. **Take the device.** `session()` opens a `u64_hold`
   (`tests/bench/u64_session.py`): the harness DeviceLock, then a snapshot
   of the full device configuration. It sets the CPU speed asked for (48 MHz
   on the U64E and 64 MHz on the C64U in the Workbench runs of
   docs/HARDWARE.md), reboots the C64, and `prepare_target`
   (`tests/run_vectors.py`) sets the REU to 16 MB, loads the runner PRG
   and installs a small mailbox dispatcher at `$CE00` through which the
   host calls runner entries (docs/HARDWARE.md "Backend fixes"). On VICE
   the session is an `x64sc` with a 16 MB REU instead.
2. **Device settings for the session.** The Ultimate's Command Interface is
   switched on inside the hold (`enable_ci`), because the disk image is
   loaded with Ultimate DOS commands. With `--audio` the sampler is mapped
   too (`map_sampler`). The hold restores every setting at the end.
3. **Stage the ROM.** `Machine.stage`: the whole 16 MB REU is zeroed
   (`aros_fill`), then `aros-ext.bin` goes to `$E00000` and `aros-rom.bin`
   to `$F80000`, and the ROM's first 8 bytes (reset SSP and PC) to guest 0
   (`host/machine.stage_plan`). On the Ultimates the bytes go through the
   harness's SocketDMA REU write, with the runner's `test_load_chunk` as a
   slow fallback. Every staged span is checksummed on the C64 (`aros_sum`)
   against the host's copy.
4. **Stage the disk.** `stage_disk`: on the Ultimates the runner loads the
   ADF from the device's own storage into the REU at `$200000` with a UCI
   `LOAD_REU` command (`ureu_ld`), and the host then writes the overlay
   blocks (the trimmed Startup-Sequence and any added files) with the REU
   write. VICE gets the whole patched image through `test_load_chunk`. The
   901,120 bytes are checksummed again, then `td_on` := 1.
5. **Load the modules.** The boot driver is `mboot.s` with one extra line
   (`jsr TD_ORG` after `snap_arm`, generated by `wb_driver`), and the
   modules of `MODULES` follow (td, spr, inpm A and B). The pointer and the
   1351 module are armed (`arm_spr`, `arm_inpm`). With `--audio` the audio
   module and the profile block come last (`aud_stage`, `aud_arm`).
6. **Boot.** `Machine.start` zeroes the serial ring and calls `aros_boot`.
   The driver sets exception mode, resets the core, loads SSP/PC from guest
   0/4, sets SR = `$2700`, stores the time shift `mach_s`, then runs
   `mach_init`, `chip_reset`, `cia_reset`, `cop_reset`, `disp_init`,
   `snap_arm`, `td_arm` and finally `mach_run`, which does not return while
   the guest runs (CONTRACTS "Boot entry").
7. **AROS boots.** The ROM sizes memory, prints to the serial port (the host
   reads the text from a ring buffer; the test legs compare it byte for
   byte with the boot model's text when the model is available), and asks
   for a disk. The td module patches
   `trackdisk.device`'s BeginIO so that reads are copied straight from the
   REU image ([STORAGE.md](STORAGE.md)).
8. **Workbench.** The host follows the boot by watching which disk blocks
   are read: block 0 (`w1`), the Startup-Sequence's first data block
   (`w2`), and the first data block of `Disk.info` (`wbi`), which means
   Workbench has opened its window.
9. **Hand over.** For the attended demo (`leg_demo_boot`) the host then sets
   the input source to the real ports (`inp_src` = 0), re-arms the pointer,
   optionally starts the 1351 sampler, and keeps the hold open for
   `--hold-s` seconds.
10. **Give the device back.** `session()` sets 1 MHz and reboots the C64
    (which also silences the sampler); then the hold restores the
    configuration it found and resets the C64.

Measured times to the Workbench (the `wbi` milestone, from the runner start,
trimmed Startup-Sequence): **130.5 s** on the C64U at 64 MHz and
**147.1-154.8 s** on the U64E at 48 MHz (docs/HARDWARE.md "Workbench 3.1
from the ADF"). A saved session resumes from a file in 9.4-9.8 s (C64U) or
about 3.8 s (U64E) to the same picture (same section).
With the Wave 17 `amiga64 run` (the same path with the ModPlayer disk and
real input) the Workbench was up after 135 s on the C64U and 180 s on the
U64E, and `amiga64 resume` took 1.23 s and 6.45 s (W17-K's device runs,
`w17-k/build/evidence/w17-k/PROGRESS.md` HANDOFF).

Sources: `tests/aros_wb.py` (`session`, `enable_ci`, `map_sampler`,
`stage_disk`, `wb_driver`, `MODULES`, `boot_workbench`, `leg_demo_boot`),
`tests/aros_boot.py` (`Machine.stage`, `Machine.start`), `tests/aros/mboot.s`,
`host/machine.py`, CONTRACTS "Machine profile and AROS boot (S3.7)" and
"HLE trackdisk and DF0", docs/HARDWARE.md.

## Guest time, in one paragraph

The guest does not run in Amiga real time. A free-running counter built
from CIA #2's timers counts the C64's φ2 clock, which stays at about 1 MHz
whatever the turbo setting. Guest time is that count shifted right by
`s` (`mach_s`), and one guest video frame is 14188 guest ticks. So a
larger `s` makes guest time run slower relative to the real world, which
gives the slow interpreter more instructions per guest frame. Workbench
sessions on the Ultimates use `s` = 3 (about 9 guest frames per real
second on NTSC φ2); the MOD player profile of Wave 15 used `s` = 0. The
host can tell a guest program the real-time rate through a profile block.
Everything about this is in [CIA_AND_TIMING.md](CIA_AND_TIMING.md).

## Source map

| Area | Code | Host reference / tools | Tests (main ones) |
|---|---|---|---|
| CPU | `runtime/core.s`, `runtime/ops/g1-g6_*.s`, `build/gen/dispatch.s` (generated by `host/gen_dispatch.py`) | `host/vectors.py`, `host/handvec.py` | `tests/run_vectors.py`, `run_super_vectors.py`, `smc.py`, `run_cwin.py` |
| Memory API, REC | `runtime/core.s` | `host/rescheck.py` (address claims) | `tests/rec_*.py` |
| HLE | `runtime/ops/hle.s`, `hle_mem.s`, `hle_con.s`, `hle_dos.s`, `dosio_ram.s`, `dosio_uci.s` | `host/hle_env.py`, `host/hle_dos_env.py`, `host/hunk.py`, `host/image.py` | `tests/run_hello.py`, `run_vbcc.py`, `run_dos.py`, `run_con.py`, `aros_oracle.py` |
| Custom chips | `runtime/ops/chip.s`, `cop.s`, `blt.s` | `host/chipset_ref.py` | `tests/run_chip.py`, `run_cop.py`, `run_blt.py`, `chip_oracle.py` |
| Display | `runtime/ops/disp.s`, `c2v.s`, `tests/aros/spr.s` | `host/c2v_ref.py` | `tests/run_c2v.py`, `run_pf.py`, `run_spr.py` |
| Time, CIAs, IRQs | `runtime/ops/mach.s`, `cia.s` | `host/chipset_ref.py` | `tests/run_mach.py`, `run_irq.py`, `run_cia.py` |
| Input | `runtime/ops/inp.s`, `tests/aros/inpm.s` | `host/inp_ref.py` | `tests/run_inp.py` |
| Audio | `tests/paula/aud.s`, hook in `chip.s` | `host/paula_ref.py`, `host/profile_block.py` | `tests/run_paula.py` |
| Disk | `runtime/ops/td.s`, `tests/aros/td.s` | `host/adf_overlay.py`, `host/adf_files.py`, `host/wbicon.py` | `host/tests/test_td.py`, `tests/aros_wb.py` |
| Snapshots, UCI | `runtime/ops/snap.s`, `ureu.s`, `dosio_uci.s` | `host/snapfmt.py` | `tests/aros_resume.py`, `ureu_probe.py` |
| AROS boot | `tests/aros/mboot.s` | `host/machine.py`, `tools/s3probe/` (boot model), `tests/aros/model_frames.py` | `tests/aros_boot.py` |
| Guest programs | `fixtures/c/` (vbcc C), `fixtures/mod/` (MOD replay and UI), `fixtures/modwb/` (the Workbench launcher) | `host/modgen.py`, `host/mod_ref.py`, `host/ui_preview.py` | `tests/run_mod.py`, `run_modui.py`, `run_player.py`, `run_modwb.py`, `aros_modwb.py` |

Link order matters: the Makefile links `chip.s`, `cia.s`, `cop.s`, `blt.s`,
`disp.s`, `mach.s`, `inp.s`, `td.s`, `snap.s`, `ureu.s` last, in that
order, so that adding to a later module never moves the earlier ones and
so that the snapshot image (`snap.s`) ends with every other module's BSS.
The reasons are recorded in the Makefile comments and in each CONTRACTS
section's "Link order" item.

## History in one table

| Milestone / wave | What it added |
|---|---|
| M0 | Hunk loader: parse, relocate, stage into the REU, run |
| M1 | the 68000 user-mode integer set, validated against SingleStepTests vectors in VICE and on a U64E |
| M2 (Waves 4-5) | HLE exec/dos/console: vbcc C programs run, with file I/O and keyboard input |
| Wave 6 | supervisor mode and exceptions; the AROS reference oracle |
| Waves 7-10 (S3.1-S3.5) | I/O trap, machine loop and time base, interrupts, CIA-A/B, copper, the planar-to-VIC display |
| Wave 11 (S3.6-S3.7) | synchronous blitter, machine profile: AROS boots to its insert-disk screen |
| Wave 12 | a visible screen on hardware, the 7-window code cache, REU snapshots and file suspend/resume |
| Wave 13 | Workbench 3.1 from the user's ADF (HLE trackdisk), joystick mouse, pointer sprite, F1 1:1 view |
| Wave 14 | the 1351 mouse, a faster stick ramp, the top-anchored fit view |
| Wave 15 | Paula on the Ultimate sampler, a clean-room MOD replay and a readable player UI |
| Wave 16 | the MOD player launched from a Workbench icon, the profile block, 1351 idle hysteresis |
| Wave 17 | the `amiga64` host CLI, prebuilt binaries, the bundled AROS ROM, these docs |

Sources: README.md status table, the WAVE*_BRIEF.md files, git history.
