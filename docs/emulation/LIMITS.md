# Limits, deviations and what is untested

Amiga64 is a proof that running Amiga software this way is possible. The
user's direction for the project (2026-10-13) is to pursue
simplifications, not compatibility edge cases. This page collects, in one
place, what that means: what is not modelled, where the emulation knowingly
differs from an A500, and what has not been tested. Each item names where
it is documented in detail.

## Scope

- **Supported targets:** a C64 Ultimate or an Ultimate 64 Elite with the
  REU at 16 MB, driven from a macOS or Linux host. Windows is a documented
  non-goal for now (WAVE17 brief). VICE is a test vehicle, not a way to use
  the emulator: it has no sampler and says nothing about speed.
- **Software:** AROS (bundled) as the Kickstart replacement, booting the
  user's own Workbench 3.1 ADF; Hunk executables in program mode; the
  project's own MOD player. Commercial games and demos that race the beam
  are out of scope (the pre-Wave-17 README's "Non-goals").
- **Speed:** well under a 1 MHz 68000 ([CPU.md](CPU.md)). Guest time is
  dilated ([CIA_AND_TIMING.md](CIA_AND_TIMING.md)).

## CPU

| Item | State | Where |
|---|---|---|
| TAS, MOVEP | halt `HALT_ILLEGAL` | CONTRACTS "Flag set" |
| address and bus errors | only `HALT_ADDRESS` on an odd vector or RTE target | CONTRACTS "Not modelled" |
| odd supervisor stack while a frame is built | not detected | same |
| trace (T bit) | stored, never acted on | same |
| some illegal-by-addressing-mode encodings | execute instead of raising vector 4 | CONTRACTS "Known gap" |
| IPL drop by RTE / MOVE to SR / ANDI to SR | seen within one poll interval, not at once | CONTRACTS "Recognition latency" |
| 68010+ instructions | vector 4 (illegal) | CONTRACTS "Flag set" |

## Memory

| Item | State | Where |
|---|---|---|
| the A500 reset overlay | not modelled (the reset longs are copied to 0) | CONTRACTS "Overlay choice" |
| undecoded custom/CIA addresses | read 0 (an A500 mirrors / returns `$FF`) | CONTRACTS "Decode" |
| instruction fetch from `$BF`/`$DF` | reads the REU, not the chips | CONTRACTS "I/O trap" |
| ROM write protection | none | CONTRACTS "Machine profile" |
| Zorro fast RAM, autoconfig | none: 2 MB chip + 1.75 MB slow | [MEMORY.md](MEMORY.md) |

## Chipset and display

| Item | State | Where |
|---|---|---|
| copper WAIT/SKIP beam timing | WAIT completes at once, SKIP does nothing | CONTRACTS "Copper", "Not modelled" |
| copper and blitter DMA cycles, BBUSY | not modelled | same, "Blitter" |
| blitter line mode | counted, draws nothing | CONTRACTS "Line mode" |
| blitter D overlapping A/B/C within one row | row-wise, differs from FS-UAE | CONTRACTS "Not modelled" (blitter) |
| display modes | lores 2 planes exact; hires 1-4 planes as a silhouette or the 1:1 view; everything else blank (HAM, dual playfield, lores 1/3/4/5 planes) | [CHIPSET.md](CHIPSET.md) |
| BPLCON1 scroll, BPLCON2, horizontal window, colours 16-31 | ignored (except pointer colours 17-19; `disp_frame` copies DMACON through COLOR15) | docs/C2V.md "Decision" (written before 4-plane hires, it says colours 4-31), CONTRACTS "Display refresh" Inputs |
| CPU-drawn pixels | not tracked: redrawn only on a display-register change, a blit, or an epoch bump | CONTRACTS "Policy v0" |
| colours | nearest of 16 VIC colours, then separated; hue may change | CONTRACTS "Colour choice" |
| hires text in the fit view | half width: "visible, but too compressed to read" | docs/UI_READABLE.md |
| lines below the 200th | not shown in the fit view (pan in 1:1) | CONTRACTS "Fit-view crop" |
| sprites 1-7 | not shown | CONTRACTS "Pointer sprite" |
| the attach bit of sprite 1 | ignored | same |
| pointer height | cut at 21 lines (42 frame lines in the interlaced 1:1 view) | same |
| a row crossing a 512 KB REU line | that picture blanks; in the 1:1 interlaced view the other field is not checked | CONTRACTS "Display refresh" |

## Audio

| Item | State | Where |
|---|---|---|
| AUDxDAT (CPU audio), ADKCON modulation | ignored | CONTRACTS "What the guest can use" |
| PER written mid-block | pitch at once; block ends from the next block | CONTRACTS "Model" |
| loop changes of 64 KB+ loops, upgrades at the block end | rare torn-A jump or a silent parked channel | CONTRACTS "Known limit (rev15-paula L2/L3)" |
| C64U pitch | about 2 cents sharp at PER 113 (per-device RATE constant not applied) | docs/HARDWARE.md "The C64U's sampler" |
| a channel playing at a suspend | resumes silent until the next DMA start | CONTRACTS "Known limit" (Paula) |
| VICE | silent | [PAULA.md](PAULA.md) |

## Time and interrupts

| Item | State | Where |
|---|---|---|
| guest time | dilated: at `s` = 3 about 0.18 of A500 real time | [CIA_AND_TIMING.md](CIA_AND_TIMING.md) |
| VERTB counting during a long conversion | fewer level-3 interrupts than frames | CONTRACTS "VERTBs during a conversion" |
| CIA PB6/PB7 timer outputs, serial shift register | not modelled | CONTRACTS "CIA-A/B" |
| STOP across a suspend | resumes running (one spurious wake-up) | CONTRACTS "STOP across a suspend" |
| serial input | none (output only, to the host's ring) | [CHIPSET.md](CHIPSET.md) |

## Input

| Item | State | Where |
|---|---|---|
| Amiga keyboard | none: only F1 (view) and C= (right button) are read in machine mode | CONTRACTS "View key" |
| second mouse / joystick port 1 as an Amiga joystick | none: one Amiga mouse | [INPUT.md](INPUT.md) |
| stick ramp, 1351 budget | per guest frame, so real-time speed depends on `s` | CONTRACTS "Limits" (input) |
| a skipped AROS VBL | the 8-bit mouse delta can wrap | CONTRACTS "Limits" (1351) |
| keyboard row recovery at turbo on the U64E | unmeasured | CONTRACTS "Keys" |

## Disk and files

| Item | State | Where |
|---|---|---|
| drives | DF0 only, no disk change while running | [STORAGE.md](STORAGE.md) |
| writes from the guest | refused (write-protected) for quick requests; non-quick writes not proven | CONTRACTS "Limit (writes)" |
| non-quick reads | would never complete (none observed) | CONTRACTS "Limit" |
| ADF formats | DD OFS/FFS `DOS\0`-`DOS\3` only | CONTRACTS "The overlay" |
| changing the running disk from the host | no live path; changes are made before the boot | [docs/guide/DISKS_AND_FILES.md](../guide/DISKS_AND_FILES.md) |
| program-mode files | RAM disk 32 files / 2 MB, 8 handles; ULTDOS 2 open files | docs/HLE_DOS.md |

## Snapshots

| Item | State | Where |
|---|---|---|
| portability | resumes only on the exact runner build (build ID), the same declared ROM, the same td module | [STORAGE.md](STORAGE.md) |
| REU size | 16 MB required | CONTRACTS "REU size" |
| the app anchor | not in the file; republished by the program | CONTRACTS "Anchor rules" |

## Untested or unmeasured

- The **φ2 frequency** of either device as an absolute value: it is taken
  as NTSC 1022727 Hz; what was measured is that the CIAs count the same
  ticks per frame at 1 MHz and at turbo ([CIA_AND_TIMING.md](CIA_AND_TIMING.md)).
- **Other Workbench versions and other ADFs.** Only the user's Workbench
  3.1 disk (Amiga Forever edition) was booted on the devices. The boot
  model and FS-UAE booted Workbench 3.1 Install and Workbench 1.3 to a
  Workbench too (docs/WB_BOOT.md §0), but never on Amiga64.
- **The 1351 driving a whole Workbench session at 64 MHz** with the user
  is recorded as still to be confirmed (the pre-Wave-17 README's Wave 14
  row); this package
  found no later record of that attended run. The ModPlayer e2e drives
  Workbench with the scripted mouse.
- **Hardware other than the two project devices**: one U64E (fw 3.15) and
  one C64U (fw 1.1.0). Other firmware versions are untested.
- A **Linux host**: no recorded session names one.

Sources: the documents and CONTRACTS sections named in each row; README
"Non-goals" (git `a9fd1cc:README.md`); docs/WAVE17_BRIEF.md.
