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
- Audio: Paula channel state machine driving the **U64 sampler** ("Ultimate Audio", `docs/HARDWARE.md` "U64 sampler"): each Paula channel maps to a hardware sample voice playing straight from the REU, where chip RAM already lives, with no CPU per sample. Caveats: no loop-point IRQ (AUDxIRQ on a loop is timed by the 6510), volume max 63, 7 C64-visible voices, REPEAT B == LENGTH hazard. On hardware without the sampler (stock C64, VICE), SID playback is best-effort only. Decision 2026-10-01: sampler, not multi-SID.
- Blitter: implemented as 6502/REU memory ops — measured bulk REU DMA tops out at ~3.3 MB/s on the U64E (see `HARDWARE.md`), still competitive with the real Blitter's ~4 MB/s peak for copies.

### S4: High-level emulation of AmigaOS

- 68k TRAP/JSR to library vectors intercepted and serviced natively: exec memory management, dos.library file I/O (mapped to Ultimate DOS command interface → SD card/USB), console output to a VIC-II text screen.
- Makes "well-behaved" OS programs (CLI tools, text apps) the first realistic milestone, since they never touch the chipset.

## Working hypothesis

Milestone-driven combination: **S1 + S4 first** (interpret a Hunk-format CLI program, HLE its OS calls, print to screen), then S3 for minimal graphics, with S2 as the long-term performance play.

## Milestones

1. **M0 — Loader** ✅ (tag `m0-loader-green`): Hunk parser + REU image/manifest on host; chunks staged through the contract memory API; end-to-end demo with runtime relocation verified in VICE.
2. **M1 — Core interpreter** ✅ (tags `m1-vectors-green`, `hw-validated`): full 68000 user-mode integer set in 6502/ca65, validated against 5150 sampled SingleStepTests golden vectors + 48 hand vectors for uncovered ops — green in VICE **and on a real Ultimate 64 Elite with zero divergence**. Measured speed: `docs/HARDWARE.md` (analytic model: `docs/PERF.md`).
3. **M2 — HLE exec/dos** ✅ done (Waves 4–5): vbcc-compiled C programs run to completion — stdio with file I/O (RAM disk; Ultimate storage opt-in, hardware-validated), keyboard input, a real heap, pools, custom stacks (`docs/HLE.md`, `docs/HLE_DOS.md`, `docs/HLE_CON.md`).
4. **M3 — Chipset shim v0**: single static playfield → VIC-II bitmap; framebuffer conversion on vblank. Wave 6 groundwork: supervisor mode and 68000 exception processing (gated, `docs/CONTRACTS.md`), hot-path page-crossing guards (`docs/PERF.md` (f)), and the S3 plan with a modelled AROS boot to "insert disk" (`docs/S3_PLAN.md`; next increment S3.1).
5. **M4 — AOT translator v0**: translate straight-line 68k basic blocks, falling back to the interpreter at block boundaries.

## Hardware access notes (measured — see `HARDWARE.md`)

- REU: 16 MB on U64 Elite; transfers via $DF00 REC registers. Guest memory maps 1:1 onto REU space; instruction fetch runs through a 256-byte window (one DMA per page), data access per-call.
- Measured on real U64E (fw 3.14d): bulk REU DMA scales with CPU clock only to ~2 MHz, capping at ~3.3 MB/s; REC *register* stores are throttled to ~1 µs each at turbo — register-access count, not byte bandwidth, is the memory-API ceiling. Windowed interpreter reaches ~80k/57k 68k-IPS (ALU/mem mix) at 48 MHz with flat cycles-per-instruction across the speed sweep.
- Ultimate command interface ($DF1C area): file I/O against SD/USB storage — backs the opt-in ULTDOS dos.library backend (hardware-validated 2026-09-24; `docs/HLE_DOS.md`).
- U64 turbo mode: CPU speed enum 1–48 MHz. Stock-C64 + cartridge stays 1 MHz: interpreter stays portable, speed is config.
- Resource ownership (ZP / RAM / guest space) is contract-managed: `runtime/rc/*.inc` + `make rescheck` (`RESOURCES.md`).

## Open questions

- ~~Minimum viable 68k subset for M2 — can we skip supervisor mode entirely?~~ **Answered: yes** — full user-mode integer set suffices; supervisor-only ops halt as HLE hooks.
- ~~REU paging strategy?~~ **Answered by measurement**: page-window instruction fetch landed (2× at 48 MHz); for data, the lever is fewer REC register accesses per call (~1 µs each at turbo), not larger windows.
- Can VIC-II FLI/IFLI modes get us closer to OCS palettes, and is the conversion affordable per-frame?
- Is the U64 Elite's firmware/FPGA extensible enough to host a real 68k soft-core as an "S5"? (Out of scope until the software route is mapped, but worth tracking.)
