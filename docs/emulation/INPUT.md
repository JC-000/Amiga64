# Input: joystick and 1351 to the Amiga mouse

The Amiga's port-0 mouse is fed from the C64's control ports. A joystick
moves the pointer with an accelerating ramp; a Commodore 1351 mouse in
proportional mode moves it one count for one count. The code is
`runtime/ops/inp.s` (in the PRG) and the 1351 module `tests/aros/inpm.s`
(two segments, `$C980-$C9FE` and `$CF00-$CFEF`). The reference model is
`host/inp_ref.py`. Binding text: CONTRACTS "Input (joystick -> mouse)
(W13-I)", including its "1351 mouse (W14-M)" part.

## What reaches the guest

| C64 | Amiga |
|---|---|
| joystick in port 2: directions | mouse movement (JOY0DAT counters) |
| joystick fire | left mouse button (CIA-A PRA /FIR0) |
| 1351 movement | mouse movement, 1:1 |
| 1351 left button | left mouse button |
| 1351 right button | right mouse button (POTINP DATLY) |
| C= key | right mouse button (the Amiga has no C= key) |
| F1 | not sent to the guest: toggles the fit / 1:1 view ([CHIPSET.md](CHIPSET.md)) |

**No other C64 key reaches the guest** in machine mode (CONTRACTS "View
key"). There is no Amiga keyboard emulation; typing into AROS is not
possible. (Program mode has its own keyboard input for console reads,
[HLE.md](HLE.md).)

## When input is read

`inp_frame` runs once per service pass that posted a VERTB, at the start
of the frame hook ([CIA_AND_TIMING.md](CIA_AND_TIMING.md)); a pass that
catches up several frames runs it once, so every VERTB the guest sees has
had exactly one input update since the previous one. Nothing is on the
per-instruction path. Cost per frame hook (VICE, 6502 cycles; CONTRACTS
"Cost"): 327 for the real scan without the 1351 module, +228 with the
module armed, +383 for the sampler's re-arm; each 1351 sample costs
626-793 more, about 120 times per real second.

## The CIA1 scan

Port 2 shares CIA1 port A with the keyboard's column lines, so the scan
keeps them apart (CONTRACTS "Scan"):

1. **Stick:** with nothing driven (DDRA = `$00`), port 2's lines are the
   only thing that can pull PA0-4 low, so a held key cannot look like a
   direction. The one way through is the usual C64 one: a port-1 device
   pulling a row low while a key on that row is held.
2. **Idle check:** with every column high, a row that still reads low is
   held from outside; a key in such a row is "unknown" this frame and
   keeps its last state.
3. **Keys:** F1 (column 0, row 4) and C= (column 7, row 5).
4. **Rest:** the scan leaves PA7-6 selecting the mouse port's pots for the
   SID.

The harness's `inject_joystick` writes the CIA's port latch, which this
undriven read never sees, and VICE ignores the harness's joystick command.
So unattended tests drive input through an **override** (`inp_src` = 1,
host-written `inp_joy`/`inp_key`), and the real scan's positive path is
covered only by attended runs (`tests/run_inp.py --attended`).

## The joystick ramp

Per frame, with a direction held: step = 1 + n counts, n += 1 up to 31, so
1, 2, 3, … up to **32 counts per frame**; centring the stick resets the
ramp, changing direction without centring keeps it; diagonals move both
axes. AROS shows 2 counts per lores pixel. Measured on VICE: 1.75 lores
pixels per frame over the first 4 moving frames and 16.0 at the cap, so at
`s` = 3 on NTSC φ2, 15.8 and 144.2 pixels per real second; crossing 320
lores pixels at the cap takes 2.2-2.3 s (CONTRACTS "Ramp").

The ramp is per guest frame, not per elapsed time: a service pass that
catches up several frames moves one frame's step.

## The 1351 mouse

- **Ports.** `inp_mport` = 0 (the default) puts the mouse on port 1 and the
  stick on port 2; 1 swaps them. The demo legs take `--mouse-port 1|2`.
- **Sampling.** The SID's POTX/POTY give the position mod 64 and alias
  beyond ±31 counts per read, and a guest frame at `s` = 3 is 111 ms, so
  the pots are sampled by **event slot 7** about 120 times per real second
  (period 8192 φ2 = `$2000 >> s` E ticks), not once per frame. The
  launcher switches the sampler on (`tests/aros_wb.demo_mouse`: the port,
  `mach_cbv[7]` := `inp_msamp`, `inp_mper`); without it the 1351's
  position is not read.
- **Decode.** Per axis, d = (v − old) mod 128 as a signed 7-bit value / 2;
  Y is negated (the 1351 counts up, the Amiga down). Scale 1:1.
- **Idle hysteresis (W16-I).** A 1351 at rest can flicker between two
  adjacent values (measured on the user's mouse on the C64U: POTX
  `$C9/$CA`, POTY `$AB/$AC`). A one-count step *against* the direction of
  the last move is held, so the pointer does not wiggle; slow moves keep
  every count; a lone one-count reversal stays held (one count of
  backlash, half a lores pixel).
- **Travel budget.** AROS reads the mouse once per guest frame as a signed
  8-bit delta, and about 14 samples land in one frame at `s` = 3. So the
  travel added per frame is capped at 63 counts per axis; one AROS read
  then never sees more than 126, never a wrap. The pointer moves at most
  about 31 lores pixels per guest frame, about 570 counts per second at
  `s` = 3; only a very fast flick reaches that.
- **Buttons** go through a small queue so that a click shorter than a
  frame becomes one pressed frame and a double-click inside one frame
  becomes press, release, press, release (leg `queue`). The right button
  is not queued: it is the line's state now.
- **Without the module** (`inp_mon` = 0) only the stick and the mouse's
  buttons work; the 1351's position needs the module.

The 2026-10-11 pot compression seen at 64 MHz on the C64U was not
reproduced on 2026-10-12 in any turbo mode; it is recorded as a transient
device state and no workaround is in the code (docs/HARDWARE.md "Wave 14
hardware findings").

## A stuck switch

`inp_stuck` (a mask set by the host) makes the real scan read those stick
lines as released. The project's Wave 13 joystick has Up stuck closed, so
the demos used `--stuck 0x01`. The line is still held at the port, so it
still selects its keyboard column: with Up held, F1 cannot be read (it
keeps its last state). The override path ignores the mask.

## Resume

The snapshot carries `inp_src`, `inp_joy`, `inp_key` and the module's
state, but a resume comes up with the 1351 module **disarmed** (snap.s
zeroes `inp_mon`), and the launcher re-arms it (`tests/aros_wb.arm_inpm`).
Test snapshots are taken with the scripted, idle input (`inp_src` = 1), so
**whoever resumes for a person must write `inp_src` = 0** after the resume,
or the real ports are ignored (CONTRACTS "Resume (I2)").

## Limits

- The ramp and the 1351 budget are per guest frame, so the pointer's
  real-time speed depends on `s` [derived: guest frames per real second
  are 72.08 / 2^s on NTSC φ2].
- A skipped AROS VBL (three sampling windows in one read) can still wrap
  the 8-bit delta.
- The pointer on screen lags the counters by a few guest frames (AROS
  moves its sprite after the counters change), which matters for scripted
  closed loops (`tests/aros_wb.py E2E.mgoto`).
- Row recovery after a column read was measured on the C64U at 1, 48 and
  64 MHz; the U64E is unmeasured (CONTRACTS "Keys"). Keyboard isolation
  from the 1351 at turbo cannot be tested in VICE and is covered only by
  an attended window (CONTRACTS "The real CIA1 path").

Sources: CONTRACTS "Input (joystick -> mouse) (W13-I)"; `runtime/ops/inp.s`;
`tests/aros/inpm.s`; `host/inp_ref.py`; `tests/aros_wb.py` (`demo_mouse`,
`arm_inpm`, `leg_demo_boot`); docs/HARDWARE.md "Joystick port 2 and the
keyboard matrix", "Wave 14 hardware findings".
