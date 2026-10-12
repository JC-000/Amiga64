# Amiga64

A translation layer for running Amiga 500 programs on the **C64 Ultimate** and **Ultimate 64 Elite**.

## Status

| Milestone | State |
|---|---|
| **M0 — Hunk loader** | ✅ done (`m0-loader-green`) — Amiga Hunk executables parsed, relocated, staged into REU, and run end-to-end |
| **M1 — 68000 interpreter** | ✅ done (`m1-vectors-green`, `hw-validated`) — full user-mode integer set, 5150 golden + 63 hand vectors green in VICE **and on real U64E hardware with zero divergence** (the current golden gate is the 5129-case subset without $BF/$DF cases, see `docs/CONTRACTS.md` "Vector sets") |
| **M2 — AmigaOS HLE** | ✅ done — **vbcc-compiled C programs run to completion**: stdio incl. file I/O (`fopen` r/w/a/r+, `fseek`, `remove`), keyboard input on stdin, `malloc`/`free` on a real heap, pools, custom stacks, `getenv`/`time`/`system` |
| M3 — Chipset shim | ✅ done — **a static Amiga playfield, pixel-exact on the VIC-II**: an HLE C program (`fixtures/c/pf_static.c`) builds a copper list, and the copper (`runtime/ops/cop.s`) plus the display refresh (`runtime/ops/disp.s`, c2v kernels) reproduce it byte-for-byte against the host reference (Wave 10). Built on: supervisor mode & exceptions (Wave 6), the $BF/$DF I/O trap (S3.1), the machine loop/time base (S3.2), interrupt delivery (S3.3) and CIA-A/B (S3.4). Plan: [`docs/S3_PLAN.md`](docs/S3_PLAN.md) |
| S3.6/S3.7 — Blitter + AROS boot | ✅ done — **the open-source AROS ROM boots to its "insert disk" screen** on VICE and on a real U64E (48 MHz, ~90 s), pixel-matched to the boot model: the synchronous OCS blitter (`runtime/ops/blt.s`, FS-UAE-checked), a machine-mode memory profile (`runtime/rc/machine.inc`) and the boot milestones in `tests/aros_boot.py` (Wave 11). Workbench followed in Wave 13 ([`docs/WB_BOOT.md`](docs/WB_BOOT.md)) |
| Wave 12 — visible screen, code cache, REU snapshots | ✅ done — the insert-disk screen is **visible** on hardware (contrast-preserving colours), a 7-window **code cache** (`runtime/core.s`), and **suspend/resume** of the whole machine to the REU or to a file on the Ultimate's storage (UCI `LOAD_REU`/`SAVE_REU`, `runtime/ops/snap.s`, `ureu.s`) |
| Wave 13 — Workbench from floppy | ✅ done — **AROS boots the user's Workbench 3.1 disk to the Workbench screen** in ~130 s on a C64 Ultimate at 64 MHz: an HLE trackdisk serves reads from the ADF staged in the REU (`tests/aros/td.s`), a trimmed Startup-Sequence overlay, a **joystick** in port 2 drives the Amiga mouse (`runtime/ops/inp.s`), the pointer is drawn with VIC sprites (`tests/aros/spr.s`), and **F1** toggles a readable **1:1 view** (interlace aware). A saved Workbench session resumes from SD in ~10 s. End-to-end leg: `tests/aros_wb.py --legs e2e` |
| Wave 14 — 1351 mouse, input polish, top-anchored view | 🟡 landed — the **1351 mouse** driver (120 Hz SID pot sampler, click queue so double-clicks survive, per-frame clamp) works in VICE and on hardware; its pot readings span the full range at 1 MHz and at the C64U's 64 MHz turbo (a ~1/4 compression seen once on 2026-10-11 was a transient device state, not reproduced on 2026-10-12; docs/HARDWARE.md). A Workbench session driven by the 1351 at 64 MHz is still to be confirmed with the user. The fit view now shows the **top** of the screen (title bar, menus, pointer start) |
| M4 — AOT translator | not started |

Measured on a real Ultimate 64 Elite (48 MHz turbo; the C64 Ultimate runs at 64 MHz): **~75–80k 68k instructions/sec** on an ALU mix and **~68k memory-bound** (+25% memory-bound / +43% read-modify-write since the REC-shadow memory API) — roughly a 0.5–1 MHz 68000. Details in [`docs/HARDWARE.md`](docs/HARDWARE.md); model in [`docs/PERF.md`](docs/PERF.md).

Independent check: an **AROS reference oracle** (`tests/aros_oracle.py`, [`docs/ORACLE.md`](docs/ORACLE.md)) runs every vbcc test program in FS-UAE on the AROS ROM (fetched, never committed) and diffs it against Amiga64: 17 MATCH, 10 triaged DIFF, 1 SKIP.

## Goal

Enable (a useful subset of) Amiga 500 software — Motorola 68000 code targeting the OCS chipset and Kickstart-era AmigaOS — to execute on Ultimate series hardware, using the platform's strengths (16 MB REU, fast DMA, the Ultimate command interface, 48 MHz CPU turbo) to bridge the enormous gap between the two machines.

## The gap we have to bridge

| | Amiga 500 | C64 / Ultimate 64 |
|---|---|---|
| CPU | 68000 @ ~7.09 MHz, 16/32-bit | 6510 @ ~1 MHz, 8-bit (U64: up to 48 MHz turbo) |
| RAM | 512 KB – 1 MB | 64 KB (+ up to 16 MB REU) |
| Graphics | OCS: bitplanes, Copper, Blitter, 4096 colors | VIC-II: char/bitmap modes, 16 colors |
| Sound | Paula: 4× 8-bit PCM DMA channels | SID: 3 synth voices (+ sample tricks) |
| OS | Kickstart/AmigaOS in ROM | KERNAL |

The strategy mix (S1 runtime interpretation + S4 OS-call HLE first; S3 chipset shim and S2 ahead-of-time translation later) is laid out in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How it works today

A 68000 interpreter written in ca65 assembly runs on the C64 side: guest memory maps 1:1 onto the REU (24-bit address space), instruction fetch runs through a 256-byte window (one REU DMA per page), data accesses go through a REC-shadow memory API (1–4 REU register accesses per call instead of 9, with 512 KB REU-block straddles split correctly), all CCR flag logic is centralized in sized ALU primitives, and a generated 256-entry dispatch table routes opcodes to per-family handlers.

AmigaOS library calls (`jsr -LVO(A6)`) land on A-line trap words planted in full exec/dos vector tables (fake V40 ExecBase/DOSBase) and are serviced natively in 6502 code (clean-room HLE):

- **exec** — FindTask/Process, AllocMem/FreeMem on a first-fit heap with coalescing and checked frees, pools, AllocVec, StackSwap (`docs/HLE.md`).
- **dos files** — Open/Close/Read/Write/Seek/ChangeMode/DeleteFile/IoErr over a 2 MB REU RAM disk by default, or (opt-in) the Ultimate's own storage via its command interface — hardware-validated (`docs/HLE_DOS.md`).
- **console** — Write to the VIC-II text screen with scrolling; Read from the real keyboard (KERNAL-free matrix scan, cooked line editing, EOF on Ctrl-D) (`docs/HLE_CON.md`).
- Unimplemented library calls halt with a precise name, e.g. `dos.library/ReadItem (LVO -810)`.

Correctness is enforced by golden vectors: sampled [SingleStepTests](https://github.com/SingleStepTests/680x0) cases (Musashi-generated, hardware-accurate) execute instruction-by-instruction on the interpreter and every architectural bit is compared — plus hand-written vectors for ops the upstream suite doesn't cover, and a self-modifying-code suite for the fetch window.

## Repository layout

```
docs/           ARCHITECTURE (strategy+milestones), CONTRACTS (binding ABI),
                RESOURCES (zp/ram/guest ownership), PERF, HARDWARE, HLE,
                HLE_DOS, HLE_CON, M0, WAVE*_BRIEF (per-wave contracts)
host/           Python tools: hunk.py, image.py, fixturegen.py, vectors.py,
                handvec.py, gen_dispatch.py, hle_env.py, hle_dos_env.py,
                rescheck.py (+ tests)
runtime/        6502/ca65: core.s (fetch/dispatch, REU memory API, EA, ALU),
                ops/ (one instruction family per file; hle.s, hle_mem.s,
                hle_con.s, hle_dos.s, dosio_ram.s, dosio_uci.s),
                rc/ (resource-contract declarations), c64.cfg
tests/          C64-side runner (runner.s, run_program.s), VICE/U64 drivers
                (run_vectors, run_program, run_hello, run_vbcc, run_dos,
                run_con, smc, rec_*, bss_boot), bench/, stub/
tools/vbcc/     pinned, hash-verified vbcc toolchain installer (installs
                outside the repo)
fixtures/       Vendored vector samples, bench sweep data, C test programs
                and HLE unit sources (own/free content only)
```

## Development

Toolchain: Python 3.11+, `ca65`/`ld65` (cc65), VICE `x64sc` with 16 MB REU, and the c64-test-harness for driving VICE or a real Ultimate 64 (REST). Interface contracts for all modules live in [`docs/CONTRACTS.md`](docs/CONTRACTS.md); zero-page/RAM/guest-memory ownership is machine-checked (`make rescheck`, see [`docs/RESOURCES.md`](docs/RESOURCES.md)).

```sh
make host-test    # host-side pytest suite
make runtime      # build the interpreter/runner PRG (build/runner.prg)
make vice-test    # full golden + hand vector suites in VICE (~35 s)
make rescheck     # resource-contract checker
sh tests/rec_gate.sh build build/gate            # the full gate, logged per step
tools/vbcc/install.sh                            # vbcc toolchain (outside the repo)
python3 -m tests.run_vbcc --prg build/runner.prg # compiled C programs (M2 exit bar)
python3 -m tests.run_dos  --prg build/runner.prg # dos file I/O (add --backend u64 for hardware)
python3 -m tests.run_con  --prg build/runner.prg # console keyboard input
```

The drivers take `--backend u64` to run on a real Ultimate 64 (through the harness's device lock). Validated on hardware so far: the full 5150-case suite, hand vectors, `run_program`, `run_hello`, `run_vbcc` (Wave 4 regression) and `run_dos` (Wave 5, inside one lock session with the device config snapshotted and restored exactly). `run_con`'s keyboard path has an automated hardware smoke (`tests/kbd_echo.py`, 1 and 48 MHz, 47/47); the attended 48 MHz key test with positive controls is ready but not yet run (`docs/HLE_CON.md`). See `docs/HARDWARE.md`.

## Non-goals (for now)

- Full-speed games or demos that race the beam on OCS.
- Kickstart ROM emulation via proprietary ROM images — we target HLE and freely redistributable test programs. (The open-source AROS ROM is the boot target instead: it reaches its "insert disk" screen since Wave 11 and boots a user-owned Workbench 3.1 disk to the Workbench since Wave 13; see "Third-party material".)

## Third-party material

- **Never in the repo:** proprietary or commercial Amiga material — Kickstart
  ROMs, Workbench disks, commercial software, the Amiga NDK.
- **User-owned proprietary media** (e.g. Workbench floppies from the
  maintainer's own hardware collection) may be used *locally* for private
  testing only: imaged into a cache outside the repo, never committed, never
  redistributed, and never required by the test suite.
- **Open-licensed third-party components** (e.g. vbcc under its own terms,
  AROS under the AROS Public License) are fetched by pinned, hash-verified
  scripts into a non-repo cache (`tools/*/install.sh` pattern) with their
  licence recorded; binaries are not committed. Code that translates or
  modifies open-licensed sources keeps that licence and lives in clearly
  separated files. Reading such sources as a reference carries no
  obligation; HLE code stays clean-room.
- **AROS m68k ROM (reference oracle only).** `tools/aros/fetch.sh` fetches a
  pinned AROS nightly (APL 1.1) by exact URL and SHA256 into
  `~/.cache/amiga64/aros/<hash>/`, together with its licence text and a
  record of where the source is (`tools/aros/README.md`). The ROM is
  never committed, never placed under `build/`, and never required by any
  test. `tests/aros_oracle.py` boots it in FS-UAE to diff our vbcc programs
  against a real AmigaOS-compatible system (`docs/ORACLE.md`), and
  `tests/aros_boot.py` stages it into the REU at test time to boot it on
  Amiga64 itself (skipping cleanly when the cache is absent).
