# Audio: Paula on the Ultimate sampler

The Ultimates have a built-in sample player ("Ultimate Audio") that can
play 8-bit samples **straight from the REU**. Guest chip RAM is the REU, so
each of Paula's four DMA channels can be one sampler voice playing the
guest's own sample data, with no copying and no 6510 work per sample. The
code is a machine-mode module, `tests/paula/aud.s`, entered from a small
router in `runtime/ops/chip.s`. The reference model is `host/paula_ref.py`.

Binding text: CONTRACTS "Audio (Paula) (W15-P)". Hardware facts:
docs/HARDWARE.md "U64 sampler (Ultimate Audio)", "The C64U's sampler",
"Paula on the U64 sampler".

**VICE has no sampler**: there the module runs its "dry" backend (the
voice registers are kept in its state page and checked by the tests), and
the guest hears nothing.

## What a guest program can use

- AUDxLCH/AUDxLCL, AUDxLEN, AUDxPER and AUDxVOL (`$0A0-$0DA`, channel x at
  +16x);
- DMACON DMAEN and AUD0EN-AUD3EN;
- INTENA/INTREQ AUD0-AUD3 (bits 7-10, level 4).

AUDxDAT (CPU-driven audio) is counted and ignored; ADKCON modulation is
not modelled. Nothing else is promised (CONTRACTS "What the guest can
use").

## The model

- AUDxLC and AUDxLEN are **latches**. DMA start (DMAEN and AUDxEN both
  set) copies them into the playing block and posts AUDx at once; every
  block end copies them again and posts again. So a latch write takes
  effect at the next block, as on Paula.
- A block lasts 2 × LEN × PER colour clocks (LEN 0 = 65536 words, PER 0
  counts as 1). PER and VOL change the sound at once.
- **Deviation:** a block's end is fixed when the block starts. A PER
  written mid-block changes the pitch at once but moves block ends only
  from the next block on (an exact rescale would cost about 25k cycles per
  PER write). The error is less than one block.
- A colour clock is `cfg_r16` / 65536 E ticks. Deadlines are fixed point
  on a grid from DMA start, so a channel never drifts against guest time.
  One machine-loop event slot (**slot 6**) serves all four channels.
- **Parking.** When the voice loops the block in hardware and the guest
  has not acknowledged the last AUDx interrupt, a block end needs no work;
  the channel is then looked at only every 1/2 to 1 frame (plus up to one
block when blocks are longer than half a frame; `PARK_E` = 7094 E). A replay that
  ignores AUDx (most MOD players) costs little: 3.0% of the guest with four
  channels parked, measured on the U64E (HARDWARE "Paula on the U64
  sampler", leg `ips`).

## Mapping a channel onto a sampler voice

Channel x is voice x (`$DF20 + 32x`; CONTRACTS "The sampler"):

| Sampler field | Value |
|---|---|
| START | `$1000000` + the block's REU offset (= its guest address) |
| LENGTH | 2 × words |
| RATE | (PER × 29563210 − 20761805) >> 24, clamped to 0..`$FFFF`: the nearest 160 ns step to PER × 281.937 ns |
| VOL | min(vol, 63): the sampler's volume is 6 bits and 64 wraps to 0 |
| PAN | 0 (left) for channels 0 and 3, 15 (right) for 1 and 2, the Amiga's sides |
| REPEAT A/B + CTRL bit 1 | set while the latched next block starts inside the voice's span and ends exactly at its end, so the hardware loops Paula's repeat with no 6510 work |

Otherwise the voice is reprogrammed by the 6510 at the block end. DMA off
plays two zero bytes once (at `$DF4000`), because a stopped voice would
otherwise hold its last sample as a DC level.

**Write order (a firmware hazard).** With REPEAT B equal to LENGTH and
repeat off, the sampler is not shown to finish a voice. The module never
creates that state, not even between two register writes: a reprogram
first sets B to `$FFFFFF`, stops the voice with CTRL 0 (CTRL 2 does not
stop a playing voice [MEASURED HW]), writes the other fields, starts it,
then writes B low byte first. The VICE legs run a logging build that checks
this order after every write (CONTRACTS "Write order (M5)").

**Never touched:** the sampler's own end-of-sample IRQ (CTRL bit 2) is
never enabled, because the machine loop's IRQ handler acknowledges CIA #1
only and a level cartridge IRQ would storm. The sampler is written only
when the host has set `cfg_hw` after mapping it, and it is never probed:
on VICE `$DF20-$DFFF` mirrors the REU controller, where a write would start
a DMA.

## Sessions and arming

- **Mapping.** The sampler is visible only when the device setting
  `C64 and Cartridge Settings / Map Ultimate Audio $DF20-DFFF` is Enabled.
  A session with `--audio` sets it inside the device hold (which restores
  it) and then proves the mapping differentially: the 6510's copy of
  `$DF20-$DFFF` must read `$00 $10` in all 112 pairs. Without the proof the
  session stops as INFRA (`tests/aros_wb.map_sampler`).
- **Placement.** Segment A at `$CA00-$CCFF` (entries at +0 write, +7 the
  slot-6 callback, +10 init; configuration at +13), state page
  `$CD00-$CDFF`, segment B at `$0400-$07FF` (the HLE text screen, unused in
  machine mode). The host stages and initialises it **last**, right before
  the guest starts or a resume is launched, because `$CA00-$CCFF` is shared
  with the resume stub.
- **Arming.** `mach_cbv[6]` := A + 7, then `aud_mon` := the module's ID
  byte 0. The router enters the module only while `aud_mon` matches; every
  entry checks segment B's ID too and disarms itself on a mismatch.
- **Resume.** The module is not in the snapshot. After a resume the
  launcher stages and arms it again, and checks that the resumed machine's
  `mach_sl` equals the session's `s` (else the staged `cfg_r16` would be
  wrong). A snapshot taken while a channel plays resumes with that channel
  silent until the guest's next DMA start.
- **The end of a session.** The session's closing reboot (1 MHz, then a
  reboot, before the hold restores the settings) stops any playing voice; this was measured on the U64E and on the C64U over ethernet
  (`hw16 rbsil`; `w16-int/build/evidence/sup16/c64u-hw16-eth/run.log`).

## Timing and tempo

Pitch comes from the sampler and is real time. Block-end interrupts, and
any replay tempo the guest derives from VBL or the CIAs, follow guest time,
which is dilated ([CIA_AND_TIMING.md](CIA_AND_TIMING.md)). So `cfg_r16` is
chosen per profile so that Paula's block grid matches the sampler's
playback: 18897 in the Wave 15 player profile (`s` = 0), 2362 in the
Workbench profile (`s` = 3), 37 on VICE at `s` = 9 (dry backend only). A
program that needs a real-time tempo reads it from the profile block at
`$2DC000` ([CIA_AND_TIMING.md](CIA_AND_TIMING.md)).

## Measured on hardware

| Item | U64E (fw 3.15, 48 MHz) | C64U (fw 1.1.0, 64 MHz) |
|---|---|---|
| pitch at PER 124-1712 | within 0.03% of the sampler law, within 0.11% of Paula (PAL colour clock) | sample time (rate + 1) × 160 ns + 80 ns, against the U64E's + 120 ns; the module uses the 118 ns law, so the C64U plays about 2 cents sharp at PER 113 |
| volume | linear; 64 and 63 equal | no separate measurement recorded |
| four channels at once | four fundamentals present | not checked per channel; the ModPlayer e2e measured the music instrumentally (block counters, stream RMS) |
| AUDx timing at `s` = 0 | 776 handler entries for 776 blocks in 2.997 s of guest time; tone lasted 3.007 s by the stream's clock | not measured |
| ModPlayer from Workbench | music in every launch (aud writes and block ends counted, stream RMS 4501-4605 playing, 10-20 silent) | the same e2e, the same checks |

Sources: HARDWARE "Paula on the U64 sampler" (hw8), "The C64U's sampler
(W16-A)"; `w16-e2e2/build/evidence/w16-e4/PROGRESS.md` "Runs" (both
devices, 2026-10-13). The C64U's stream works since it moved to ethernet;
on WiFi the device refused UDP streaming (HARDWARE "Wave 15 player on the
Ultimates").

## The MOD player and ModPlayer

The guest programs that use all this are in `fixtures/`:

- `fixtures/mod/`: a clean-room ProTracker-style 4-channel replay in 68k
  assembly (`replay.s`, written to match `host/mod_ref.py` exactly; the
  supported effects and their semantics are listed in `mod_ref.SEMANTICS`)
  and the player screen (`fixtures/mod/ui/`, built to the readability rules
  of [docs/UI_READABLE.md](../UI_READABLE.md)). Test songs come from our
  own generator, `host/modgen.py`; no third-party MOD is in the repository.
- `fixtures/modwb/`: **ModPlayer**, a Workbench program
  (`modwb.s`) that loads a MOD: started from its tool icon, the tool's own
  name plus `.mod` next to it (`ModPlayer.mod`); started with a project
  icon, that file; from a shell, `ModPlayer.mod`, claims CIA-B timer A,
  the audio channels and the blitter through the OS, takes the machine
  over, plays with the full-screen UI, and on a left click (mouse button or
  stick fire) or at the song's end restores everything in reverse order and
  returns to Workbench. If a claim fails before the takeover it backs out
  with a requester. It publishes the **app anchor** at `$2DC040`
  (`'MWBA'`, a pointer to its state) for the host to find it.

Known limits of ModPlayer (its header comment): CIA interrupts the OS had
pending across the takeover (a key, a one-shot timer request) are not
re-posted at the restore; a left button held longer than 50 frames after
the exit reaches Workbench. CIA-A timer B is never touched: claiming it,
even restored as found, made later launches fail intermittently (3 of 11
launches with it, 0 of 14 without; `fixtures/modwb/modwb.s` header,
W16-E3).

Sources: CONTRACTS "Audio (Paula)"; `tests/paula/aud.s` header;
`tests/aros_wb.py` (`aud_stage`, `aud_arm`, `map_sampler`, `session`);
`fixtures/modwb/modwb.s` header; `fixtures/mod/replay.s` header;
docs/HARDWARE.md sections named above.
