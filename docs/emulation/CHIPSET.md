# The chipset: custom registers, copper, blitter and display

The OCS custom chips are emulated by a handful of modules in the runner:
`runtime/ops/chip.s` (decode, shadow, the simple registers), `cop.s` (the
copper and the display-register state), `blt.s` (the blitter), `disp.s`
and `c2v.s` (turning Amiga bitplanes into a VIC-II picture), plus the
pointer-sprite module `tests/aros/spr.s`. Audio is in
[PAULA.md](PAULA.md), interrupts and the CIAs in
[CIA_AND_TIMING.md](CIA_AND_TIMING.md), the mouse and buttons in
[INPUT.md](INPUT.md).

Binding text: CONTRACTS "I/O trap", "Copper and display state", "Display
refresh", "Blitter". Reference models: `host/chipset_ref.py` (registers,
copper, blitter) and `host/c2v_ref.py` (the display decision and
conversion). Specifications of the converter: [docs/C2V.md](../C2V.md).

## Registers

Every access to `$DFF000-$DFF1FF` arrives in `chip.s` through the memory
API's I/O trap ([MEMORY.md](MEMORY.md)).

- **Shadow and log.** `chip_shd` is a 512-byte big-endian image of the
  registers. Every decoded write is logged in a 64-entry ring
  (`chip_log`); a write to a write-only register lands in the shadow, a
  write to a read-only slot does not.
- **Reads.** Read-only registers return their shadow, write-only ones 0,
  with these exceptions:
  - VPOSR/VHPOSR come from the beam model once the machine loop runs: the
    position follows guest time within the current frame (5 colour clocks
    per E tick, 227 per line, 313 lines). VPOSR = `$8000 | V8` (LOF set,
    Agnus ID bits 0).
  - DMACONR = `(DMACON & $07FF) | BZERO` (the blitter's zero flag); BBUSY
    never reads 1.
  - POTINP reflects the right mouse button ([INPUT.md](INPUT.md)).
  - SERDATR reads `$3000` (transmit buffer empty, always ready);
    DENISEID reads `$FFFF`.
  - INTENAR/INTREQR follow the SET/CLR rule
    ([CIA_AND_TIMING.md](CIA_AND_TIMING.md)).
- **Serial output.** A SERDAT write appends its low byte to a 1024-byte
  ring, `chip_ser` (count `chip_sern`), in MAIN so the host can read it
  while the guest runs. This is how the host watches AROS's boot messages.
  There is no serial input.

## The copper

The copper (`cop.s`) is `chipset_ref.Copper` without beam timing
(CONTRACTS "Copper and display state"):

- MOVE to any register goes through the same write path as a CPU write.
  A MOVE to a register below `$80` (below `$40` with COPCON's danger bit)
  stops the copper, COPJMP1/2 jump, `$FFFF,$FFFE` ends the list.
- **WAIT is satisfied at once and SKIP does nothing.** There is no beam
  race: a list's final register state per frame is what counts.
- **When it runs:** once per guest frame from COP1LC (the frame hook), when
  the CPU strobes COPJMP1/2, and when the CPU turns copper DMA on, each
  time only with DMAEN and COPEN set. A run stops after 4000 instructions
  or before a fetch from the I/O banks.
- A copper instruction costs about 545 6502 cycles [MEASURED VICE]; the
  frame hook runs once per service pass and skips the frames it overran, so
  a long list slows the guest but never stops it.
- Not modelled: WAIT/SKIP beam timing, copper DMA cycles, the copper
  stopping when its own DMA goes off mid-list, BPLCON3 and later.

## The blitter

The blitter (`blt.s`, banked code at `$A000`) runs **synchronously, inside
the BLTSIZE write**: when the CPU's write returns, the blit is done
(CONTRACTS "Blitter (S3.6)").

- **Normal mode** follows `Blitter.blit`: channels A/B/C/D, the minterm,
  the first/last word masks, shifts carried across words and rows,
  ascending and descending, signed modulos, inclusive and exclusive fill,
  and data registers for channels with DMA off. Pointers and data
  registers are written back as after a real blit.
- **Line mode is not drawn**: it is counted, raises completion and changes
  nothing else. The AROS boot uses no line blits (the boot model's 76 blits
  are all area blits).
- **Completion** sets INTREQ BLIT at once; DMACONR's BZERO reflects the
  last normal blit. BBUSY is never seen.
- **Memory:** addresses are masked to the 2 MB chip range, one REU DMA per
  channel row (two when a row crosses a 512 KB line), and every D row drops
  the code windows of its pages.
- **Deviations:** when D overlaps A/B/C inside one row, `blt.s` reads the
  whole row before writing it, which differs from a word-serial blitter
  and from FS-UAE. Overlaps across rows (the usual scroll and copy cases)
  are exact. Odd modulos are honoured where FS-UAE ignores bit 0. Both are
  pinned by `tests/chip_oracle.py`, which runs the same blits in FS-UAE.
- Not modelled: blitter DMA cycles and BBUSY, BLTPRI, ECS BLTSIZV/BLTSIZH.

## From bitplanes to the VIC-II

The VIC-II has 16 fixed colours, 320×200 hires or 160×200 multicolour,
and colour per 8×8 (or 4×8) cell. The display path converts the Amiga
playfield into that, as often as the time budget allows.

### What triggers a conversion

`cop.s` keeps every display register (DMACON, BPLCONx, the bitplane
pointers and modulos, the window and fetch registers, COLOR00-31) in a
block at `$B800`, `disp_st`, with a generation byte that moves when a value
changes. After every copper run `disp_frame` checks it (CONTRACTS "Display
refresh", "Policy v0"):

- a changed display field, or a changed **pixel epoch**, converts. The
  epoch is a byte that every blit with D enabled increments, so blitter
  drawing is seen;
- **CPU writes to bitplane memory are not tracked.** A program that draws
  with the CPU alone is not redrawn until a display register changes, a
  blit runs, or something bumps the epoch (the test harness does that with
  `force_conv`);
- inside `mach_run` a conversion starts only when 24 guest frames
  (`DISP_N` × 14188 E ticks) have passed since the previous one ended, so
  conversions cannot eat the guest. At `s` = 3 that is about 2.7 s of real
  time [derived: 24 frames at 9.0 frames/s].

### The modes

| Amiga playfield | C64 picture |
|---|---|
| lores, 2 planes | multicolour 160×200, **exact colours**: each C64 pixel is the left lores pixel of its pair (kernel `c2v_mc2`, row-skip entry `c2v_mc2s`) |
| hires, 1-4 planes, fit view | hires 320×200 **silhouette**: a C64 pixel is set if either hires pixel of its pair is non-zero; one foreground colour per 8×8 cell, the pen of the cell's first set pixel (kernel `c2v_hires`, entry `c2v_hiress`) |
| hires, 1-4 planes, 1:1 view (F1) | hires, one Amiga pixel per C64 pixel, a 320×200 window of the Amiga screen; per cell the top-left pixel's pen as background and the first other pen in raster order as foreground; further pens show in the foreground colour (`c2v_ones`) |
| anything else (lores 1, 3, 4 or 5 planes, HAM, dual playfield, DMA off, an empty window, a short fetch, a row crossing a 512 KB line) | blank: the screen shows the border in COLOR00 once a picture has been shown |

Ignored: BPLCON1 scroll, BPLCON2 priorities, the horizontal window
start/stop, colours 16-31 except the pointer's 17-19.

Two cost figures from VICE (φ2 cycles for a 200-line frame, docs/C2V.md
"Performance"): hires 675,439, multicolour 362,839.

### Vertically: the fit view shows the top

A picture taller than 200 lines shows its **first 200 lines from
DIWSTRT** (W14-D): the title bar, menus and the pointer's start position
stay visible, the bottom lines are cut. In the 1:1 view the window can
pan down to them. With interlace the fit view shows one field.

### Colours

- Each OCS colour `$0RGB` maps to the nearest of the 16 VIC colours in CIE
  L\*a\*b\* (CIE76) against Pepto's PAL VIC palette. The 4096-entry table
  is computed by `host/c2v_ref.py build_lut()`, carried run-length packed in
  the PRG and unpacked into the REU at `$DF0000` by `disp_init`.
- That alone maps every dark OCS colour to black, so AROS's dark pens on
  black vanished. Since W12-V a separation rule gives pens with different
  OCS colours different VIC colours: pen 0 keeps its nearest colour, pens
  in use are taken in order of how many cells use them, and a pen whose
  nearest colour is taken walks a luma ladder to the nearest free one
  (CONTRACTS "Colour choice"). Fidelity is best effort: a pen can end up in
  a different hue.
- In hires, a pen that is never the first set pixel of a cell is not
  separated and does not show (on the AROS insert-disk screen, pen 3).

### Readability

Hires text loses half its horizontal resolution in the fit view: 8-pixel
Topaz glyphs become 4 pixels wide. The user's verdict on AROS's text was
"visible, but too compressed to read"; the 1:1 view (F1) is the
workaround. A guest program that wants readable output can follow
[docs/UI_READABLE.md](../UI_READABLE.md): the MOD player's screen is hires,
3 planes, with every design pixel two hires pixels wide, so the fit view is
pixel-exact.

## The F1 view key and the 1:1 window

F1 toggles between the fit view and the 1:1 view (`inp_view` from the
input scan, consumed by `disp_frame`). The 1:1 window opens at the top,
centred horizontally (`disp_px` = 20, in bytes of the 80-byte hires row),
and the pointer module re-centres it when the pointer comes within 32 C64
pixels of an edge. The toggle and a pan convert at once, past the frame
budget (CONTRACTS "1:1 view (W13-D)", "Pointer sprite").

## The pointer sprite

Only sprite 0, the mouse pointer, is shown. `tests/aros/spr.s` (a module at
`$C600-$C8FF`, armed by the session) reads SPR0PT, POS and CTL each frame
and shows the pointer with three hires VIC sprites, one per colour
combination of the two data words, so colours 17-19 are exact. Its
position is mapped through the same crop as the picture.

Limits (CONTRACTS "Pointer sprite"): at most 21 lines tall; the attach bit
is ignored; sprites 1-7 are not shown; a pointer colour change alone, or an
image edited in place, stays stale until the pointer moves or the screen
converts. The module is full (768 bytes, 0 spare).

## What the guest sees that differs from an A500

- Copper WAITs complete at once; raster effects that depend on beam
  position collapse to the frame's final register state.
- The blitter finishes inside the write; nothing ever waits for it.
- Display DMA does not steal guest cycles, and the display lags guest
  changes by up to the conversion budget.
- Undecoded custom-register addresses read 0, not mirrored registers.

Sources: CONTRACTS sections named at the top; `runtime/ops/chip.s`,
`cop.s`, `blt.s`, `disp.s`, `c2v.s`, `tests/aros/spr.s`; docs/C2V.md;
docs/UI_READABLE.md "What the conversion does".
