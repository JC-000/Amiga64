# Amiga64

**Amiga 500 software on a Commodore 64**, with the help of an Ultimate
board: a C64 Ultimate or an Ultimate 64 Elite.

Amiga64 is a 68000 interpreter and an Amiga chipset emulation written in
6502 assembly. It boots the open-source AROS Kickstart replacement (bundled)
and your own Workbench 3.1 floppy image to the Workbench screen, with the
pointer on a joystick or a 1351 mouse and Paula's sound on the Ultimate's
built-in sampler. A host program on your Mac (or Linux computer) sets up
each session over the network.

It is a proof that this is possible, not a fast or complete Amiga: the
emulated 68000 runs at about 1 MHz equivalent at best against the A500's
7 MHz, the display is converted to the VIC-II's 16 colours, and Workbench
takes about 2-4 minutes to boot (`amiga64 run`: about 135 s on a C64U,
about 180 s on a U64E).

The project was built in a series of development waves, the last of them
Wave 17, and is complete as a proof of possibility: version 0.17.0 is its
final planned release, and it is now open for anyone to carry forward.
Start with
**[docs/CARRYING_FORWARD.md](docs/CARRYING_FORWARD.md)**: a map of the
code, how to build and test it, the ground rules, and everything that was
planned but not built.

## Try it

You need a C64 Ultimate or an Ultimate 64 Elite on your network with a USB
stick (U64E) or SD card (C64U), Python 3.11+ and git on macOS (Linux is
expected to work but was never tested) with access to github.com (pip
installs amiga64 and its c64-test-harness dependency from GitHub), and
your own Workbench 3.1 ADF.

```sh
pip install "git+https://github.com/JC-000/Amiga64@v0.17.0"
mkdir -p adf && cp /path/to/your/workbench-3.1.adf adf/
amiga64 doctor
amiga64 run
```

`doctor` asks for your device's address the first time (in a terminal)
and saves it. Then double-click the Workbench disk (Workbench3.1), find
**ModPlayer** just to the right of the Expansion drawer, double-click it,
and listen.
Workbench may redraw the disk window a few times while it fills, and a
double-click during a redraw is lost: double-click between redraws. The
full walkthrough is [docs/guide/QUICKSTART.md](docs/guide/QUICKSTART.md).

## Documentation

| For | Read |
|---|---|
| using it | [QUICKSTART](docs/guide/QUICKSTART.md), [DEVICES](docs/guide/DEVICES.md), [DISKS_AND_FILES](docs/guide/DISKS_AND_FILES.md), [TROUBLESHOOTING](docs/guide/TROUBLESHOOTING.md) |
| how it works | [docs/emulation/OVERVIEW.md](docs/emulation/OVERVIEW.md), then [CPU](docs/emulation/CPU.md), [MEMORY](docs/emulation/MEMORY.md), [CHIPSET](docs/emulation/CHIPSET.md), [PAULA](docs/emulation/PAULA.md), [CIA_AND_TIMING](docs/emulation/CIA_AND_TIMING.md), [INPUT](docs/emulation/INPUT.md), [STORAGE](docs/emulation/STORAGE.md), [HLE](docs/emulation/HLE.md) |
| what does not work, and what is still open | [docs/emulation/LIMITS.md](docs/emulation/LIMITS.md), its [Open items](docs/emulation/LIMITS.md#open-items-carrying-it-forward) |
| **carrying it forward** | **[docs/CARRYING_FORWARD.md](docs/CARRYING_FORWARD.md)**: the code map, building and testing, the ground rules, what was planned but not built, how to contribute |
| developing it | [docs/emulation/TESTING.md](docs/emulation/TESTING.md), [docs/CONTRACTS.md](docs/CONTRACTS.md) (the binding interfaces), [docs/RESOURCES.md](docs/RESOURCES.md) (address claims) |
| measurements | [docs/HARDWARE.md](docs/HARDWARE.md) (real devices), [docs/C2V.md](docs/C2V.md), [docs/WB_BOOT.md](docs/WB_BOOT.md), [docs/ORACLE.md](docs/ORACLE.md) |
| history | the `docs/WAVE*_BRIEF.md` files: one brief per development wave, binding for that wave's work and kept unchanged as a record (their worktrees, agents, device addresses and plans belong to the development setup, not to you); older design documents are marked "Historical" |

Some docs cite evidence as `<name>/build/evidence/...` (for example
`w17-k/build/evidence/w17-k/PROGRESS.md`). Those are the development
machine's local run logs from that work's checkout. They were never
committed, so treat such a citation as a record of where a number came
from, not as a file you can open. Evidence you can rerun lives in
`tests/` and `host/tests/`.

## Status

| Milestone | State |
|---|---|
| M0: Hunk loader | done: Amiga Hunk executables parsed, relocated, staged into the REU and run |
| M1: 68000 interpreter | done: the user-mode integer set (except TAS and MOVEP), validated against SingleStepTests vectors: the current 5129-case golden set and 63 hand vectors in VICE, and the 5150-case set it was cut from plus 48 hand vectors on a real U64E |
| M2: AmigaOS HLE | done: vbcc-compiled C programs run without an OS, with stdio, files and keyboard input |
| S3: chipset | done: supervisor mode and exceptions, interrupts, CIA-A/B, copper, a synchronous blitter, the planar-to-VIC display |
| AROS boot | done: the AROS ROM boots to its insert-disk screen (Wave 11) |
| Workbench | done: the user's Workbench 3.1 disk boots to the Workbench in about 130 s on a C64U at 64 MHz (Wave 13); suspend and resume through a file |
| Input | done: joystick and 1351 mouse as the Amiga mouse; F1 for a 1:1 view (Waves 13-16) |
| Audio | done: Paula's four channels on the Ultimate sampler, a clean-room MOD player with a readable UI (Wave 15), launched from a Workbench icon (Wave 16) |
| Release kit | done: the `amiga64` command, prebuilt binaries, the bundled AROS ROM, these docs (Wave 17, v0.17.0) |
| M4: ahead-of-time translator | not built: designed only ([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) S2, M4); see [CARRYING_FORWARD](docs/CARRYING_FORWARD.md#what-was-planned-but-not-built) |

Wave 17 was the last wave. What is still open is listed, with its
evidence, in [CARRYING_FORWARD](docs/CARRYING_FORWARD.md#what-was-planned-but-not-built)
and in LIMITS [Open items](docs/emulation/LIMITS.md#open-items-carrying-it-forward).

## How it works, briefly

- The **68000** is interpreted in 6502 code. Its registers live in the
  C64's zero page; its memory is the Ultimate's 16 MB REU, guest address =
  REU offset. Code is fetched through a cache of seven 256-byte windows in
  C64 RAM ([CPU](docs/emulation/CPU.md), [MEMORY](docs/emulation/MEMORY.md)).
- The **custom chips** are trapped at their addresses: a copper without
  beam timing, a blitter that finishes inside the register write, and a
  converter that turns Amiga bitplanes into a VIC-II picture
  ([CHIPSET](docs/emulation/CHIPSET.md)).
- **Time** comes from the C64's CIA #2 counting φ2, slowed by a power of
  two (`s`) so the interpreter keeps up; interrupts and CIA timers run on
  it ([CIA_AND_TIMING](docs/emulation/CIA_AND_TIMING.md)).
- The **floppy** is an ADF in the REU, served by patching AROS's
  `trackdisk.device`; the user's file is never modified
  ([STORAGE](docs/emulation/STORAGE.md)).
- **Paula** channels play straight from the REU on the Ultimate's sampler
  ([PAULA](docs/emulation/PAULA.md)).
- Programs run without AROS get **HLE** exec/dos/console calls answered in
  6502 code ([HLE](docs/emulation/HLE.md)).

## Development

Toolchain for building from source: Python 3.11+, cc65 2.18 (`ca65`/`ld65`),
VICE `x64sc` with a 16 MB REU for the tests, vbcc 0.9h with vasm 2.0f and
vlink 0.18a for the guest programs (`tools/vbcc/install.sh`, installed
outside the repository), and
[c64-test-harness](https://github.com/JC-000/c64-test-harness) v0.13.1 for
driving VICE or a device. Users of the prebuilt release need none of the
build tools.

```sh
make host-test                          # pytest (host/tests)
make runtime                            # build/runner.prg (+ .lbl, .map)
make vice-test                          # golden + hand vectors in VICE
make rescheck                           # address-claim checker
make prebuilt-check                     # prebuilt/ still matches the sources
sh tests/rec_gate.sh build build/gate   # the full VICE gate
```

Every change was tested red-first and attacked by an independent reviewer
before it merged, and contributions are expected to keep that up. The
device rules (only through the harness's device lock, every setting
restored) are in [TESTING](docs/emulation/TESTING.md); the full workflow
is in [CARRYING_FORWARD](docs/CARRYING_FORWARD.md#building-and-testing).

## Licence and third-party material

- **Amiga64's own code and documentation: MIT** ([LICENSE](LICENSE),
  [NOTICE](NOTICE)).
- **The AROS m68k ROM** (`third_party/aros/aros-rom.bin`, `aros-ext.bin`,
  AROS nightly2 20260921, unmodified) is under the **AROS Public License
  1.1**, not MIT. Its licence text, its acknowledgements and a note on
  where its source code is published are next to it
  (`third_party/aros/LICENSE`, `ACKNOWLEDGEMENTS`, `SOURCE.txt`). The AROS
  source is at <https://github.com/aros-development-team/AROS>; the
  20260921 nightly's SourceForge folder has been removed, so `SOURCE.txt`
  names that tree with the build date and the archive's SHA-256.
- **CPU test vectors** in `fixtures/vectors/` are filtered samples of
  [SingleStepTests 680x0](https://github.com/SingleStepTests/680x0); see
  `fixtures/vectors/UPSTREAM_LICENSE`.
- **Never in the repository:** Kickstart ROMs, Workbench disks, commercial
  Amiga software, the Amiga NDK. You supply your own Workbench ADF; the
  `amiga64` tool copies it only to your own Ultimate's storage, and only
  when you confirm (or pass `--yes`). Test music is generated by our own `host/modgen.py`.
- The HLE and the guest programs (the MOD replay, its UI, ModPlayer) are
  clean-room work from public documentation. No AROS source, Commodore
  source or NDK material is in the repository; AROS is used only as the
  unmodified binary ROM above.

Hosts: macOS was used for all development and testing. Linux is expected
to work but is untested; Windows is not supported.
