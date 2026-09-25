# Amiga64

A translation layer for running Amiga 500 programs on the **C64 Ultimate** and **Ultimate 64 Elite**.

## Status

| Milestone | State |
|---|---|
| **M0 — Hunk loader** | ✅ done (`m0-loader-green`) — Amiga Hunk executables parsed, relocated, staged into REU, and run end-to-end |
| **M1 — 68000 interpreter** | ✅ done (`m1-vectors-green`, `hw-validated`) — full user-mode integer set, 5150 golden + 48 hand vectors green in VICE **and on real U64E hardware with zero divergence** |
| **M2 — AmigaOS HLE** | 🔶 v0 — exec/dos calls serviced natively; a hello-world Hunk program prints on the C64 screen; compiler-built binaries pending (AllocMem next) |
| M3 — Chipset shim | not started |
| M4 — AOT translator | not started |

Measured on a real Ultimate 64 Elite (48 MHz turbo): **~80k 68k instructions/sec** on an ALU mix, ~57k memory-bound — roughly a 0.5–1 MHz 68000. Details in [`docs/HARDWARE.md`](docs/HARDWARE.md); model in [`docs/PERF.md`](docs/PERF.md).

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

A 68000 interpreter written in ca65 assembly runs on the C64 side: guest memory maps 1:1 onto the REU (24-bit address space), instruction fetch runs through a 256-byte window (one REU DMA per page), all CCR flag logic is centralized in sized ALU primitives, and a generated 256-entry dispatch table routes opcodes to per-family handlers. AmigaOS library calls (`jsr -LVO(A6)`) land on A-line trap words planted in a fake ExecBase/DOSBase environment and are serviced natively — dos.library `Write()` currently prints to the VIC-II text screen.

Correctness is enforced by golden vectors: sampled [SingleStepTests](https://github.com/SingleStepTests/680x0) cases (Musashi-generated, hardware-accurate) execute instruction-by-instruction on the interpreter and every architectural bit is compared — plus hand-written vectors for ops the upstream suite doesn't cover, and a self-modifying-code suite for the fetch window.

## Repository layout

```
docs/           ARCHITECTURE (strategy+milestones), CONTRACTS (binding ABI),
                RESOURCES (zp/ram/guest ownership), PERF, HARDWARE, HLE, M0
host/           Python tools: hunk.py, image.py, fixturegen.py, vectors.py,
                handvec.py, gen_dispatch.py, hle_env.py, rescheck.py (+ tests)
runtime/        6502/ca65: core.s (fetch/dispatch, REU memory API, EA, ALU),
                ops/ (one instruction family per file, + hle.s),
                rc/ (resource-contract declarations), c64.cfg
tests/          C64-side runner (runner.s, run_program.s), VICE/U64 drivers
                (run_vectors, run_program, run_hello, smc), bench/, stub/
fixtures/       Vendored vector samples, bench sweep data (own/free content only)
```

## Development

Toolchain: Python 3.11+, `ca65`/`ld65` (cc65), VICE `x64sc` with 16 MB REU, and the c64-test-harness for driving VICE or a real Ultimate 64 (REST). Interface contracts for all modules live in [`docs/CONTRACTS.md`](docs/CONTRACTS.md); zero-page/RAM/guest-memory ownership is machine-checked (`make rescheck`, see [`docs/RESOURCES.md`](docs/RESOURCES.md)).

```sh
make host-test    # host-side pytest suite
make runtime      # build the interpreter/runner PRG (build/runner.prg)
make vice-test    # full golden + hand vector suites in VICE (~35 s)
make rescheck     # resource-contract checker
python3 -m tests.run_hello --prg build/runner.prg   # the M2 hello demo
```

The full 5150-case suite also runs on real Ultimate 64 hardware: `python3 -m tests.run_vectors --backend u64 ...` (see `docs/HARDWARE.md`).

## Non-goals (for now)

- Full-speed games or demos that race the beam on OCS.
- Kickstart ROM emulation via proprietary ROM images — we target HLE and freely redistributable test programs. (Booting the open-source AROS ROM is kept open as a future option once a chipset shim and supervisor mode exist; see "Third-party material".)

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
