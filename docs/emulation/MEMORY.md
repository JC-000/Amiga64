# Memory

## Guest memory is the REU

The guest's whole 24-bit address space is the Ultimate's 16 MB REU: guest
address `A` is REU offset `A`, and the bytes are stored exactly as the 68000
sees them, big-endian (CONTRACTS "Guest model"). Apart from the open bus in
the two I/O banks (below) there are no mirrors, no bus holes and no write
protection in either guest map; anything the host
did not stage reads zero because the host zeroes the REU first in machine
mode (`host/machine.py` `ZERO_SPANS`).

Every session therefore needs the REU **enabled at 16 MB**. The session
code sets it inside the device hold (`prepare_target` in
`tests/run_vectors.py`). The suspend/resume code requires it too: its own
data lives at `$DF1000-$DF3FFF`, which a smaller REU would wrap into guest
memory. There is no run-time check for that; the session sets the size (CONTRACTS "Suspend/resume", "REU size").

## The memory API

All data accesses go through six entry points in `runtime/core.s`:
`mem_read8/16/32` and `mem_write8/16/32` (`m_addr` in, `m_val` in or out,
little-endian on the C64 side). Each call is one REU DMA through the REU
controller ("REC", registers `$DF00-$DF0A`), with three refinements:

- **REC shadow.** The API remembers the REC registers it last programmed
  and issues every DMA with *autoload*, so a following call stores only the
  registers whose values differ (CONTRACTS "Interpreter ABI"; WAVE4_BRIEF
  C1). On silicon this was worth +16% to +43% on memory-bound loops at
  48 MHz (docs/HARDWARE.md "REC shadow core").
- **The seam rule (C1).** Anything else that programs the REC (the code
  window refill, the blitter, the display converter, the trackdisk copy,
  UCI transfers, host staging) must call `mem_rec_dirty` before the next
  API call, and every host-reachable entry invalidates first. A test build
  with the invalidation removed (`REC_NOINVAL`) fails its dedicated legs.
- **512 KB lines.** The REC's address counter carries only through bits
  0-18, so one DMA that runs past `$x7FFFF` continues at `$x00000`. A word
  or long access that straddles a 512 KB line is therefore split into
  single-byte DMAs (`mio_split`), with the CPU doing the 24-bit increment
  (docs/HARDWARE.md "Fix round, 2026-09-24"). Every other bulk mover in the
  tree keeps its transfers inside a line: the window refill is one aligned
  page, the trackdisk copy splits at page ends on both sides, and the
  display refuses to fetch a row that crosses a line.

## The I/O trap: banks `$BF` and `$DF`

Two guest banks are not memory (CONTRACTS "I/O trap"):

- **`$DFF000-$DFF1FF`, the custom chips.** Register = `m_addr & $1FE`. A word
  access is one register, a long is two, a byte write lands on both halves
  and a byte read returns the addressed half.
- **`$BF`, the CIAs.** CIA-A is the odd bytes with A12 = 0, CIA-B the even
  bytes with A13 = 0; register = address bits 11-8 (`m_addr+1 & $0F`, where `m_addr+1` is the
  byte holding bits 15-8), always byte-wise.
- **Everything else in the two banks is open bus**: reads 0, writes are
  ignored and counted in `chip_nopen`. This deliberately differs from an
  A500, which mirrors the custom registers and returns `$FF` on undecoded
  CIA reads.

The test costs nothing on the common path: it sits only on the branch taken
when the guest bank changes, and a trapped access never loads the shadow's
bank, so an I/O access always arrives as a bank change. A 16- or 32-bit
access straddling into or out of an I/O bank is split into bytes, each to
its own side.

Two consequences matter for the rest of the design:

1. **The REU under `$DF` is private.** The guest can never read or write
   it, so the runtime keeps data there: the palette table `disp_lut`
   (`$DF0000-$DF0FFF`), the snapshot area (`$DF1000-$DF3FFF`) and a zero row
   for the display (`$DF4000-$DF40FF`). Bulk movers that bypass the CPU
   check for the I/O banks themselves: HLE DOS refuses Read/Write buffers
   there (IoErr 212), the blitter masks its addresses to 2 MB, the trackdisk
   refuses destinations there, and the copper stops before fetching from
   them.
2. **Instruction fetch is not trapped.** Executing from `$BF`/`$DF` would
   read the REU. No guest is known to do that.

Tests: `tests/run_chip.py`, `host/tests/test_chip_hotpath.py`,
`tests/run_pf.py` legs `lut-trap` and `dosbuf`.

## The two guest maps

Both maps describe the same 16 MB REU, at different times. They are
declared in `runtime/rc/guest.inc` (program mode, the HLE) and
`runtime/rc/machine.inc` (machine mode, AROS), and checked by
`host/rescheck.py` (`make rescheck`): no overlaps, full coverage, every
equate inside its region, and both maps must reserve the two I/O banks.

### Program mode (`guest.inc`)

| Guest range | Use |
|---|---|
| `$000000-$0003FF` | 68k vector page; AbsExecBase pointer at `$000004` |
| `$000400-$000FFF` | HLE environment: state, clock, CLI launch stub, arguments, exit word, CLI and Process structures, console FileHandles, the dos FileHandle pool (`$000C00-$000FFF`) |
| `$001000-$00FFFF` | program stack, A7 starts at `$00F000` |
| `$010000-$0EFFFF` | program load region (first Hunk segment at `$010000`) |
| `$0F0000-$0FFFFF` | fake ExecBase (`$0F0400`) and DOSBase (`$0F0C00`) with their full vector tables |
| `$100000-$7FFFFF` | HLE heap (AllocMem pool, 7 MB) |
| `$800000-$BEFFFF` | free |
| `$BF0000-$BFFFFF` | I/O: CIAs |
| `$C00000-$DEFFFF` | free |
| `$DF0000-$DF0FFF` | palette LUT (private) |
| `$DF1000-$DF3FFF` | snapshot area (private) |
| `$DF4000-$DF40FF` | display zero row (private) |
| `$DF4100-$DFFFFF` | I/O: custom chips |
| `$E00000-$FFFFFF` | HLE DOS RAM disk, 2 MB |

### Machine mode (`machine.inc`)

| Guest range | Use |
|---|---|
| `$000000-$1FFFFF` | chip RAM, 2 MB (AROS sizes it so on the flat REU); the reset SSP/PC copied to `$000000-$000007` |
| `$200000-$2DBFFF` | the staged DF0 disk image, 901,120 bytes (`TD_ADF`): read only by the trackdisk module, never by guest code |
| `$2DC000-$2DC0FF` | the profile block (`$2DC000`, 64 B, host-written) and the app anchor (`$2DC040`, 16 B, guest-written); the rest reserved |
| `$2DC100-$BEFFFF` | unmapped, zero (AROS probes it and finds nothing) |
| `$BF0000-$BFFFFF` | I/O: CIAs |
| `$C00000-$DBFFFF` | slow RAM, 1.75 MB (SysBase lands here) |
| `$DC0000-$DEFFFF` | unmapped, zero at boot (no RTC; Gayle ID at `$DE1000` reads 0). AROS writes here after boot, so snapshots save it |
| `$DF0000-$DF40FF` | private: LUT, snapshot area, zero row (as above) |
| `$DF4100-$DFFFFF` | I/O: custom chips |
| `$E00000-$E7FFFF` | `aros-ext.bin`, 512 KB |
| `$E80000-$F7FFFF` | unmapped, zero (no autoconfig board, no cartridge ROM) |
| `$F80000-$FFFFFF` | `aros-rom.bin`, 512 KB |

The A500's reset overlay (ROM mirrored at 0 until CIA-A PRA bit 0 drops) is
not modelled: the loader copies the ROM's first 8 bytes to guest 0, and
AROS reads them once before it turns the overlay off (CONTRACTS "Overlay
choice"). Nothing protects the ROM banks from writes; AROS does not write
them. The profile block and anchor are described in
[CIA_AND_TIMING.md](CIA_AND_TIMING.md) and [PAULA.md](PAULA.md).

There is **no Zorro fast RAM**. At Workbench idle the title bar reads
"Chip: 1987k, Fast: 1530k" (docs/HARDWARE.md "Workbench 3.1 from the
ADF"). Those are free amounts; since the slow RAM is the only memory
outside chip RAM, it is what the "Fast" figure counts.

## The C64 side

The authoritative map is `runtime/rc/ram.inc` (and `zp.inc` for page
zero); the link layout is `runtime/c64.cfg`.

| C64 range | Use |
|---|---|
| `$0002-$000F` | loader/harness scratch |
| `$0010-$009B` | the interpreter's register file and core state, machine loop, input scratch ([CPU.md](CPU.md)) |
| `$0100-$03FF` | 6502 stack, KERNAL/BASIC workspace |
| `$0400-$07E7` | HLE console text screen (program mode). In machine mode, audio module segment B uses `$0400-$07FF` |
| `$0801-$9FFF` | **MAIN**: the runner PRG (BASIC stub, DATA, LOWCODE, the dispatch tables at `$0900-$0AFF`, CODE, RODATA, BSS) |
| `$A000-$A7FF` | **BHI**, banked code under the BASIC ROM: the blitter (`BCODE`) |
| `$A800-$B0FF` | **CWIN**: the code cache's banked code (`$A800`), 7 windows (`$A900-$AFFF`), its map (`$B000`) |
| `$B100-$B5FF` | **SNAPHI**: suspend/resume code and BSS |
| `$B600-$B7FF` | blitter row buffers (also the trackdisk's bounce buffer) |
| `$B800-$B8FF` | `disp_st`, the display-register state block |
| `$B900-$BFFF` | converter tables, staged plane rows, argument block |
| `$C000-$C7FF` | `buf_case`: test cases in program mode; in machine mode the module area (`$C000-$C8FF`): mboot `$C000-$C1FF`, td `$C200-$C5FF`, spr `$C600-$C8FF` (its last page lies in `buf_result`) |
| `$C800-$CFFF` | `buf_result`: results in program mode. Machine mode: the boot parameter/result blocks at `$C900`/`$C940`, 1351 module A `$C980-$C9FE`, audio module A `$CA00-$CCFF` and its state page `$CD00-$CDFF`, the hardware mailbox `$CE00-$CEFF`, 1351 module B `$CF00-$CFEF`, the ready flag `$CFFF` |
| `$D000-$D3FF` (RAM under I/O) | the VIC screen matrix and sprite pointers |
| `$D400-$D7FF` (RAM under I/O) | the pointer's sprite blocks |
| `$DF00-$DF0A` | REU controller registers |
| `$DF1C-$DF1F` | Ultimate Command Interface |
| `$DF20-$DFFF` | the Ultimate sampler, when mapped |
| `$E000-$FF3F` (RAM under KERNAL) | the VIC bitmap (bank 3) |
| `$FFFA-$FFFF` (RAM under KERNAL) | the machine loop's NMI and IRQ vectors |

Rules that keep this working:

- **Banked code** (`BHI`, `CWIN`, `SNAPHI`) runs only with the BASIC ROM
  banked out. The blitter and the snapshot code are entered through a
  MAIN stub that banks BASIC out and restores the caller's `$01`; the code
  cache's miss path banks BASIC out and leaves it out (a caller from `$37`
  gets `$36` back), and only its write check restores `$01` (CONTRACTS
  "Blitter (S3.6)", "Code-window cache" Banking, "UCI REU I/O").
- **Host-readable state stays in MAIN.** RAM under the BASIC ROM is not
  readable from the host on the C64U, so anything the host polls while the
  guest runs (the serial ring, counters) is in MAIN BSS (CONTRACTS "I/O
  trap", `chip_ser`).
- **Inside `mach_run`, `$01` = `$35`** (I/O in, BASIC and KERNAL out); the
  runtime never calls the KERNAL (CONTRACTS "Machine loop").
- **MAIN is full.** Waves 15-17 rule out new MAIN bytes without a
  supervisor ruling; at Wave 16 CODE had about 59 bytes of margin (WAVE16
  brief, "Operating rules"). New machine-mode code has gone into
  modules in the `$C000-$CFFF` buffers (and the audio module's segment B
  at `$0400`), each placement ruled by the supervisor.
- **Modules are staged after the last thing that writes their area**
  (test stubs, `test_load_chunk`, runner helpers) and again after every
  resume, and they are armed last (CONTRACTS "Hand-off rule"; the audio
  module's rule in "Audio (Paula)").

Claiming new space means adding a region to the right `rc/*.inc` file and
keeping `make rescheck` green; docs/RESOURCES.md has the grammar.

Sources: CONTRACTS "Guest model", "C64 memory map", "Interpreter ABI", "I/O
trap", "Machine profile and AROS boot (S3.7)", "Display refresh",
"Suspend/resume"; `runtime/rc/{zp,ram,guest,machine}.inc`;
`runtime/c64.cfg`; docs/RESOURCES.md; docs/HARDWARE.md (REC sections).
