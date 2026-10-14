# Time, events, interrupts and the CIAs

Machine mode runs the guest inside `mach_run` (`runtime/ops/mach.s`), the
drop-in replacement for `emu_run` that adds guest time, events and
interrupt delivery. The guest CIAs are `runtime/ops/cia.s`. Binding text:
CONTRACTS "Machine loop (S3.2)", "Event seam (S3.3/S3.4)", "Interrupt
delivery (S3.3)", "CIA-A/B (S3.4)" and, for the profile block, "Audio
(Paula)". Reference model: `host/chipset_ref.py` (`MachTime`, `Paula`,
`CIA`).

## The time base

The C64's CIA #2 timer A (counting φ2, latch `$FFFF`) cascades into timer
B (counting A's underflows). Read together they form a free-running 32-bit
φ2 counter C (`mach_rdc` reads a consistent snapshot). Nothing else may
reprogram CIA #2's timers after `mach_init`.

On the U64E at 48 MHz the CIAs count φ2 unscaled: 17095 ticks per NTSC
video frame at both 1 and 48 MHz [MEASURED HW] (docs/HARDWARE.md "CIA clock
at turbo"). The time base therefore needs no correction for the turbo
speed. The same probe on the C64U (2026-10-13, ethernet) read 17095.0
ticks per frame at 64 MHz and 17094.0-17095.1 at 1 MHz
(`build/evidence/sup16/c64u-hw16-eth/run.log` in the `w16-int` worktree;
CONTRACTS "Time at s = 3"). Both Ultimates in this project run NTSC, nominal
φ2 = 1022727 Hz (docs/HARDWARE.md "Wave 15 player on the Ultimates").

## Guest time: E = C >> s

Guest time E is C shifted right by `s`:

- `mach_s` (a byte in the PRG's DATA, 4 at load) is latched by `mach_init`
  into `mach_sl`. A driver stores the wanted value before `mach_init`.
  After a resume, read `mach_sl`, not `mach_s` (CONTRACTS "Suspend/resume",
  A3).
- **One guest frame is 14188 E ticks** (about one A500 PAL frame of
  709 kHz E-clock ticks [derived]), so `frames/s = φ2 / 14188 / 2^s`: 72.08 / 2^s on NTSC φ2,
  69.44 / 2^s on PAL φ2.
- `mach_etime` gives the 32-bit E time (it also counts the 32-bit C wraps,
  which needs a read at least every ~36 min; `mach_run` reads it at every
  service pass).

**Which `s` is used:**

| Session | `s` | Guest frames per real second | Chosen for | Source |
|---|---:|---:|---|---|
| AROS / Workbench on the U64E and C64U | 3 | 9.0 | the fastest boot to the insert-disk screen on the U64E (87-98 s at `s` = 3; 101 s at 4, 106 s at 2) | `tests/aros_boot.py` `BOOT_S_U64`, PERF (l) |
| AROS on VICE | 9 | 0.14 (PAL: `tests/aros_boot.py` and `aros_wb.py` start VICE with `ntsc=False`) | reaching m2 in reasonable warp time | `BOOT_S_VICE`, PERF (l) |
| the Wave 15 MOD player profile, U64E | 0 | 72.08 | real-time tempo with CIA-B paced replay | WAVE15 brief addendum |

At `s` = 3 guest time runs at about 9.0 / 50 = 0.18 of an A500's
[derived]. AROS does not need a high `s`: the boot model reaches its
"romtaginit done" milestone down to about 680 guest instructions per frame,
and the U64E reached it at `s` = 1 and not at `s` = 0 (CONTRACTS "C4",
"`mach_s` per host").

## The service pass and event slots

`mach_run` uses CIA #1 timer A as a one-shot that wakes it for the next
event. Its IRQ handler only sets a pending bit and makes `emu_run` return
at the next instruction boundary; all work happens in the **service pass**
(`mach_svc`), with `$01` = `$35`:

1. Count every frame boundary passed: `mach_frames` += 1 and INTREQ VERTB
   (bit 5) posted per frame.
2. Run the frame hook once if a VERTB was posted: input (`inp_frame`),
   then `mach_fhook` (the copper and display, the suspend check, the
   trackdisk device walk). Frames that pass during the hook are counted but
   not hooked again.
3. Fire every armed event slot whose deadline has passed, in slot order,
   each at most once per pass.
4. Program the one-shot for the earliest of the next frame boundary and
   every armed deadline (at most `$FFFF` φ2 ahead; shorter while an
   interrupt is pending, see "Latency" below).

After the pass, `mach_run` delivers an interrupt if one is deliverable
(`mach_dlv`), then runs the guest again (`runtime/ops/mach.s` `mach_run`,
`mach_svc`).

| Slot | Owner | Use |
|---:|---|---|
| 0 | `mach.s` | VERTB; its callback is the VERTB hook (CIA-A TOD counts it) |
| 1 | `cia.s` | CIA-A timer A |
| 2 | `cia.s` | CIA-A timer B |
| 3 | `cia.s` | CIA-B timer A |
| 4 | `cia.s` | CIA-B timer B |
| 5 | `cia.s` | CIA-B TOD alarm |
| 6 | `tests/paula/aud.s` | Paula block ends, all four channels |
| 7 | `inp.s` / the 1351 module | the 1351 pot sampler (about 120 Hz real time) |

A pass that leaves a slot due "now" forces exactly one guest instruction
before the next pass, so a client that keeps re-arming in the past cannot
livelock the guest. A firing is at most `4096 >> s` E ticks late
[MEASURED VICE] (CONTRACTS "Event seam").

## Interrupt delivery

- INTENA and INTREQ follow the OCS SET/CLR rule on every write
  (`chip.s`). Level sources (CIA-A on bit 3, CIA-B on bit 13) are held in
  `mach_hold` and ORed back after each INTREQ write, so the guest cannot
  clear them while the CIA still asserts.
- **IPL** = 0 unless INTEN (INTENA bit 14); otherwise the level of the
  highest source in `INTENA & INTREQ & $3FFF`: bit 13 → 6; 12, 11 → 5;
  10-7 → 4; 6-4 → 3; 3 → 2; 2-0 → 1.
- In exception mode only, after the entry pass and after every service
  pass, if IPL > SR's I2-I0 the CPU takes autovector 24 + IPL (stacked PC =
  the next instruction) and I2-I0 := IPL. One interrupt per pass; a higher
  one nests.
- **Latency.** A SET write to INTENA/INTREQ or a post that makes an
  interrupt deliverable "kicks", so it is taken at the next instruction
  boundary, as on a 68000. A drop of the CPU's mask by RTE, MOVE to SR or
  ANDI/EORI to SR has no hook in the core; it is seen within one poll
  interval, max(`$0400`, 256 E << s) φ2 capped at `$FFFF`, while something is pending
  (measured 543-563 E at `s` = 4, 127-128 E at `s` = 9; CONTRACTS
  "Recognition latency"). A real 68000 takes it at once.
- **STOP** waits for the next event when an interrupt can still come, and
  returns `HALT_STOP` only when nothing could ever wake the CPU. Guest time
  keeps running at the dilated rate while stopped (no fast-forward).

## The guest CIAs

`cia.s` models both 8520s byte for byte like `chipset_ref.CIA`
(CONTRACTS "CIA-A/B"): ports (CIA-A PRA input pins `$EB`, the others
`$FF`, `(PR & DDR) | (pins & ~DDR)`), timers A and B (continuous and
one-shot, force load, TB counting TA underflows), the ICR, TOD with latch
and alarm, and a plain SDR.

- **Time is lazy.** A chip is brought up to date (`eclock`) only when it
  is accessed, from its last sync time, in O(1); an event slot is armed
  only for an underflow or alarm that can still raise the ICR. A timer
  faster than a service pass therefore cannot starve the guest.
- **TOD:** CIA-A counts VERTBs (one per guest frame); CIA-B counts PAL
  lines on a grid of 227 colour clocks per line (15625 Hz, 312.5 lines per
  14188-E frame).
- **Not modelled:** the PB6/PB7 timer outputs and the serial shift
  register. Port pins that the emulator drives itself are the trackdisk's
  drive lines on CIA-A PRA bits 2-5 and the left mouse button on bit 6
  (bit 7 is held high; the right button is read through POTINP)
  ([STORAGE.md](STORAGE.md), [INPUT.md](INPUT.md)).

## Telling a guest program the real time: the profile block

A guest program cannot measure real time, so for a Workbench session with
`--audio` the host writes a 64-byte block at guest `$2DC000` while the
guest is stopped (`host/profile_block.py`; CONTRACTS "The profile block and
the app anchor"). Big-endian:

| Off | Field |
|---|---|
| +0 | `'A64P'` |
| +4 / +6 | version 1, size 64 |
| +8 | `e_hz_q8`: guest E ticks per real second × 256 (`s` = 3, NTSC: 32727264) |
| +12 | `tick_e`: round(E Hz / 50), the 125 BPM tick (`s` = 3: 2557) |
| +16 | `frame_e`: 14188 |
| +20 | φ2 Hz (1022727) |
| +24 / +25 / +26 | `s`; host (0 VICE, 1 U64E, 2 C64U); flags (bit 0 audio armed, bit 1 sampler live, bit 2 real time meaningful) |
| +28 / +30 | `cfg_r16` in force; CPU MHz |
| +60 | `~` (32-bit sum of the longs +0 .. +56) |

A program uses the block only when the magic, version, size and check all
match, and otherwise falls back to Amiga timing (tick 14188 E, VBL). The
Wave 16 ModPlayer takes its tempo from it. The app anchor at `$2DC040` is
the opposite direction: a guest program publishes a pointer to its state
there for the host ([PAULA.md](PAULA.md)).

## Costs worth knowing

- A copper instruction costs about 545 6502 cycles [MEASURED VICE]; a
  copper list longer than a guest frame slows the guest roughly in
  proportion but never stops it (CONTRACTS "Copper", "Cost").
- The frame hooks stay safe with up to about 300 extra cycles at idle; a
  600-cycle hook sits at the edge where the `run_irq` `lag` leg (a
  periodic client at P = 3200 φ2, `s` = 0) would flake (CONTRACTS
  "Frame-hook budget").
- The suspend check costs 31 φ2 cycles per guest frame (PERF "Suspend hook
  cost").

## Limits

- An interrupt that becomes pending while the CPU's mask holds it is
  taken up to one poll interval late after the mask drops (above).
- A suspend taken while the CPU is STOPped resumes it running: one
  spurious wake-up (CONTRACTS "STOP across a suspend").
- VERTB is a level in INTREQ: when a display conversion takes several
  guest frames, the guest's level-3 handler runs once for all of them, so a
  guest that counts VERTB interrupts sees fewer frames while the screen
  converts (CONTRACTS "VERTBs during a conversion").
- Guest time is not Amiga time. Anything that measures real elapsed time
  with the CIAs or VBL sees dilated time unless it reads the profile
  block.

Sources: CONTRACTS sections named at the top; `runtime/ops/mach.s`,
`cia.s`; `tests/aros_boot.py` (`BOOT_S_*`); `host/profile_block.py`;
docs/HARDWARE.md "CIA clock at turbo", "Wave 15 player on the Ultimates";
docs/PERF.md (l) and "Suspend hook cost".
