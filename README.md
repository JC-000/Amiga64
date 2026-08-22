# Amiga64

A translation layer for running Amiga 500 programs on the **C64 Ultimate** and **Ultimate 64 Elite**.

## Goal

Enable (a useful subset of) Amiga 500 software — Motorola 68000 code targeting the OCS chipset and Kickstart 1.3 — to execute on Ultimate series hardware, using the platform's strengths (large expansion RAM/REU, fast DMA, the Ultimate command interface, and on the U64 Elite the FPGA itself) to bridge the enormous gap between the two machines.

## The gap we have to bridge

| | Amiga 500 | C64 / Ultimate 64 |
|---|---|---|
| CPU | 68000 @ ~7.09 MHz, 16/32-bit | 6510 @ ~1 MHz, 8-bit (U64: up to 48 MHz turbo) |
| RAM | 512 KB – 1 MB | 64 KB (+ up to 16 MB REU) |
| Graphics | OCS: bitplanes, Copper, Blitter, 4096 colors | VIC-II: char/bitmap modes, 16 colors |
| Sound | Paula: 4× 8-bit PCM DMA channels | SID: 3 synth voices (+ sample tricks) |
| OS | Kickstart/AmigaOS in ROM | KERNAL |

There is no way to run Amiga software at full fidelity on a stock C64. The project is about finding the *practical* translation strategies:

1. **68000 interpreter on the 6502** — a software CPU emulator using REU memory as Amiga RAM, with the U64's turbo mode (48 MHz) making interpretation plausible for non-realtime programs.
2. **Ahead-of-time / static translation** — translate 68000 code to 6502 code offline on a host machine, producing a native C64 program plus a runtime that emulates the Amiga environment (chipset registers, exec.library calls).
3. **Chipset shim** — map a constrained subset of OCS behavior (one playfield, no Copper tricks) onto VIC-II, and Paula sample playback onto SID volume-register / mahoney-style sample techniques.
4. **HLE of AmigaOS** — intercept exec/dos/graphics library calls at the trap level instead of emulating the ROM, so well-behaved OS programs need far less machinery than hardware-hitting games.

Which mix of these wins is an open research question — that's what this repo is for.

## Target hardware

- **Ultimate 64 Elite** (FPGA C64 mainboard): CPU turbo up to 48 MHz, 16 MB REU, Ultimate DOS/command interface.
- **C64 Ultimate / Ultimate-II+ cartridge** on a stock C64: REU + command interface, but 1 MHz 6510 only — the constrained target.

## Repository layout

```
docs/         Research notes and architecture decisions
  ARCHITECTURE.md   Overall approach and strategy comparison
host/         Host-side tools (Python): hunk loader, 68k → 6502 translator, asset converters
runtime/      6502-side runtime (assembly): 68k interpreter core, chipset shims, REU paging
tests/        Test programs and harnesses (c64-test-harness against VICE / real U64)
fixtures/     Small Amiga test binaries (own code only — no copyrighted ROMs or software)
```

## Non-goals (for now)

- Full-speed games or demos that race the beam on OCS.
- Kickstart ROM emulation via copyrighted ROM images — we target HLE and freely redistributable test programs.

## Development

Host tooling is Python 3.11+. 6502 code assembles with `ca65`/`ld65` (cc65 suite). Testing uses VICE for fast iteration and real Ultimate 64 hardware for validation. Interface contracts for all work packages live in `docs/CONTRACTS.md`.
