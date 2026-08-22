# Architecture

Status: initial research phase. Nothing here is settled; this document tracks the strategy space and the decisions as we make them.

## Problem statement

Run a Motorola 68000 Amiga 500 program on Ultimate series C64 hardware. The C64 side has an 8-bit 6510, 64 KB of directly addressable RAM, and (on the Ultimate) up to 16 MB of REU memory reachable only via DMA transfers. The Amiga side assumes a 16/32-bit CPU, ≥512 KB flat RAM, and the OCS chipset.

## Candidate strategies

### S1: Runtime interpretation (68k interpreter in 6502 asm)

- Fetch/decode/execute loop over 68000 opcodes held in REU, with a working-set window paged into C64 RAM.
- Register file (D0–D7/A0–A7, 32-bit each) in zero page.
- Expected throughput: hundreds of 6502 instructions per 68k instruction. At 1 MHz that's ~1–5 k 68k IPS — batch programs only. At 48 MHz (U64 turbo) ~50–250 k IPS — roughly a 0.5 MHz 68000-equivalent for compute-bound code. Slow but genuinely usable for non-realtime software.
- Pro: runs unmodified binaries; correctness is testable instruction-by-instruction.
- Con: never fast enough for games/demos; REU paging cost dominates on branchy code.

### S2: Ahead-of-time translation (host-side 68k → 6502)

- A Python translator consumes an Amiga Hunk executable, lifts 68000 code, and emits 6502 code + data laid out for C64 RAM/REU.
- 32-bit ops become multi-byte runtime-helper calls; the win over S1 is eliminating decode cost and specializing addressing modes.
- Pro: 5–20× over interpretation; output is a normal C64 program.
- Con: self-modifying code, computed jumps, and code/data ambiguity need a fallback interpreter anyway. S1 is a prerequisite.

### S3: Chipset shim (OCS → VIC-II / SID)

- Emulated custom-chip register file at $DFF000 (trapped in S1; compiled to shim calls in S2).
- Display: convert a single 4–5 bitplane playfield to VIC-II multicolor bitmap with palette quantization. Copper lists interpreted per-frame for palette/scroll changes only.
- Audio: Paula channel state machine driving SID sample playback; on stock 1 MHz hardware, reduce to best-effort.
- Blitter: implemented as 6502/REU memory ops — REU DMA does copies at ~1 byte/cycle, which is actually competitive.

### S4: High-level emulation of AmigaOS

- 68k TRAP/JSR to library vectors intercepted and serviced natively: exec memory management, dos.library file I/O (mapped to Ultimate DOS command interface → SD card/USB), console output to a VIC-II text screen.
- Makes "well-behaved" OS programs (CLI tools, text apps) the first realistic milestone, since they never touch the chipset.

## Working hypothesis

Milestone-driven combination: **S1 + S4 first** (interpret a Hunk-format CLI program, HLE its OS calls, print to screen), then S3 for minimal graphics, with S2 as the long-term performance play.

## Milestones

1. **M0 — Loader**: parse Amiga Hunk format on host, produce a REU image + manifest; C64-side loader stages it via Ultimate command interface.
2. **M1 — Core interpreter**: 68000 user-mode integer instruction set in 6502 asm, validated instruction-by-instruction against a reference (Musashi or pym68k) using the c64-test-harness.
3. **M2 — HLE exec/dos**: enough of exec.library + dos.library to run a freshly compiled "Hello, Amiga" (vbcc/gcc-m68k) to completion with console output.
4. **M3 — Chipset shim v0**: single static playfield → VIC-II bitmap; framebuffer conversion on vblank.
5. **M4 — AOT translator v0**: translate straight-line 68k basic blocks, falling back to the interpreter at block boundaries.

## Hardware access notes

- REU: 16 MB on U64 Elite; transfers via $DF00 REC registers. Amiga chip+fast RAM lives here; the 6502 sees it through a paging window.
- Ultimate command interface ($DF1C area): file I/O against SD/USB storage — backs dos.library HLE.
- U64 turbo mode: CPU speed register up to 48 MHz — the single biggest lever for S1 viability. Stock-C64 + cartridge stays 1 MHz: keep the interpreter portable, treat speed as a config.

## Open questions

- Minimum viable 68k subset for M2 — can we skip supervisor mode entirely? (Likely yes for HLE.)
- REU paging strategy: fixed 8 KB window vs. software TLB of several 2 KB pages. Needs measurement.
- Can VIC-II FLI/IFLI modes get us closer to OCS palettes, and is the conversion affordable per-frame?
- Is the U64 Elite's firmware/FPGA extensible enough to host a real 68k soft-core as an "S5"? (Out of scope until the software route is mapped, but worth tracking.)
