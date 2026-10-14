# Wave 14 Brief — 1351 mouse ∥ input polish ∥ top-anchored fit view

Supervisor-authored, **binding**.
- `docs/WAVE4_BRIEF.md` C1–C5 still apply. **C5: red → green, adversarial.**
- `docs/WAVE13_BRIEF.md` operating rules, memory rules and the machine-mode module rules (addendum) carry over unchanged unless this brief says otherwise.
- `docs/CONTRACTS.md` holds for the files it covers ("Input", the display sections, "Machine profile and AROS boot").
- Changing a contract needs supervisor sign-off. Report it; don't work around it.

**The user's direction (2026-10-11), after driving Workbench on the C64U with a joystick:**
- *"It works. I saw the red cursor. The top menu and start position for the cursor are off the screen so it was not obvious where the cursor was or that it was moving at first."*
- Holding C= showed readable menus, but a double-click on the disk icon got no reaction.
- *"Make the 1351 mouse driver a priority for the next wave, as I will be able to borrow one today."*

**Base:** `wave14/integration`, which is wave13/integration (PR #13, not yet merged) plus the inp_stuck mask (wave13/mask, 9084088) and this brief.
- Work ONLY in your supervisor-created worktree, and never run mutating git.
- Evidence goes in `build/evidence/<pkg>/` with PROGRESS.md and SESSION.log.
- Scratch goes in `<scratchpad>/<pkg>/`.

## Operating rules
- **Devices only through c64-test-harness:** DeviceLock and `u64_hold`, with the config IDENTICAL afterwards.
  - Never pkill or killall.
  - No raw curl, FTP or REST.
- **Hardware.** The C64U is at 198.51.100.158, 64 MHz; the U64E at 192.0.2.81 is the cross-check.
- **The user is often at the C64U.**
  - **Ask the supervisor before any C64U hold.**
  - Attended legs are run by the supervisor, who relays cues in chat. On-screen prompts are not seen; give long windows (≥ 15 s) and put every cue in the log.
- **Physical devices on the C64U:**
  - a joystick in port 2 whose UP switch is stuck closed (inp_stuck $01 hides it);
  - a borrowed 1351 mouse, coming today, port to be agreed with the user (port 1 by default).
- **Suite shape.**
  - Batch runs, ≤ 10 mutants per package, host tests in-process.
  - VICE runs in background mode with DONE markers you poll.
  - Builds are serial.
- **Clean room.**
  - Do not open ~/.cache/amiga64/aros-src/.
  - Public C64 and Amiga hardware facts are fine (SID POT registers, the 1351 proportional protocol, CIA1, JOY0DAT); cite them.
- **Timing budget.** The frame-hook path stays within the CONTRACTS budget: about +300 cycles idle over today's inp_frame. run_irq lag (P=3200, slope gate) must stay green, and perf_cycles --max-delta 0 must hold.

## Memory budget
- **MAIN:** BSS ends at $9F32, leaving ~205 B to $9FFF.
  - W14-M ≤ 160 B.
  - W14-D ≤ 32 B.
  - Prefer a machine-mode module for any input code that runs only under AROS: buf_case has room at $C900+ only around the mboot P/R block, so ask the supervisor for a range first.
- **Under I/O:** W13-I's unused $D800-$D8FF is W14-M's, if it needs data there.

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W14-M** 1351 mouse and input polish (PRIORITY) | `w14-mouse` | runtime/ops/inp.s, host/inp_ref.py, tests/run_inp.py, host/tests/test_inp*.py, fixtures/inp/; the mach.s call line; CONTRACTS "Input"; HARDWARE notes; tests/aros_wb.py's demo and e2e input helpers (labelled) |
| **W14-D** top-anchored fit view | `w14-vis` | the fit-view crop decision in disp.s/c2v and host/c2v_ref.py (labelled `; W14-D`); tests/aros/model_frames.py regeneration; the expectations in run_pf, run_c2v and run_spr; tests/aros_boot.py's m3 model loader; CONTRACTS display text |

## W14-M — the 1351 mouse first, then input polish

**M1: the 1351 driver (priority).** A Commodore 1351 in proportional mode drives the Amiga port-0 mouse (JOY0DAT) and buttons. Public facts:
- **Position:** SID POTX/POTY ($D419/$D41A) carry it, modulo 64 in bits 1-6; bit 0 is noise.
- **Port select:** CIA1 PRA bits 7-6 select which port's pots the SID reads (01 = port 1, 10 = port 2). The SID needs one sample period after a switch.
- **Buttons:** left = the port's fire line, right = the port's UP line.

**Requirements:**
- **Port:** the mouse port is selectable (port 1 default, port 2 option), set by a host byte `inp_mport`. The joystick and the mouse can both work at once, the joystick on the other port.
- **Delta decoding:** decode deltas from successive pot values (signed mod 64 with the bit-0 noise removed), as the 1351 protocol needs. Add them to JOY0DAT. Scaling is documented (1351 counts → Amiga counts).
- **Buttons:** left → /FIR0, right → POTINP DATLY. C= stays the right button too, OR-ed.
- **Turbo:** the SID's pot sampling runs on its own clock. Prove that the reads are valid at 64 MHz: wait for a full sample period after a port select, measured in raster lines or a CIA timer, not CPU loops.
- **Port 1 and the keyboard:** port 1 lines share the keyboard rows (PB). The scan must keep keys and the mouse buttons apart, as W13-I did for port 2. Say how.
- **Tests:**
  - a host reference (inp_ref) with the 1351 decode, the wrap and the noise;
  - VICE has a 1351 emulation; check whether the harness can drive it and cite the file. If not, use a host override for the pot values (like inp_src) for the scripted legs;
  - an **attended leg** for the supervisor with long windows: move the mouse in a square, then left click, right click, both.
- **E2E:** with the override, a scripted mouse path drives the pointer to the disk icon and double-clicks, using aros_wb's e2e machinery.

**M2: input polish (after M1 is green):**
- **Button latching:** a press or release between two inp passes must reach the guest. Latch the edges and deliver each for at least one guest frame, so quick taps and double-clicks are never lost. The user's double-click failed on the C64U.
- **Time-scaled acceleration:** the joystick's ramp uses elapsed guest frames, not hook runs, so a catch-up pass counts. Raise the speed: target the full screen width in ≤ 3 s at the cap on the C64U at 64 MHz. Measure and report px/s at the start and at the cap.
- **inp_stuck:** keep it, and cover it in the attended leg.

## W14-D — the fit view keeps the top of the screen

- **Crop rule:** when the picture is taller than 200 lines, crop from the bottom, not the middle. The first 200 lines from DIW start stay visible, so a Workbench title bar, its menus and the pointer start position are on screen. Do it for both LACE and non-LACE fit; say whether LACE needs its own rule.
  - The 1:1 view's default window starts at the top too.
- **Fit output changes:** the existing fit-view output changes **only** in which lines are shown. List every changed expectation. Regenerate the model frames and the m3 and w3 sets, and show that aros_boot m3 and aros_wb w3 pass on VICE again.
- **Pointer mapping:** the W13-D sprite mapping follows the new crop, with the hot spot still within ±1 px.
- **Show the user:** a PNG of the Workbench fit view from C64 memory (VICE), with the title bar visible.

## Done-gate per package
- The task_completion gate and run_inp, run_spr and run_irq ×3. perf_cycles --max-delta 0 and rescheck.
- The AROS legs on VICE: aros_boot m1-m3; aros_resume; aros_wb w, resume and e2e.
- Your package's mutants and red → green.
- One C64U run of your package leg, **after asking**.

The supervisor merges W14-M then W14-D, re-runs the gate, has a fresh adversarial reviewer per package, and runs the attended mouse demo with the user.

## Addendum: state at landing (2026-10-11, supervisor)

- **W14-D** (top-anchored fit view): rev14-vis said MERGE. Follow-ups from that review:
  - the 1:1 view can't reach the insert-disk text: F1 opens at the top, and an empty pointer never pans;
  - the Ultimate w3 hash drops disp_mck now that the title rows are masked;
  - the 1:1 straddle check in c2v_ref uses the fit window;
  - stale "centred" comments.
- **W14-M** (1351 + polish): rev14-mouse's round-1 verdict was FIX. Fix round 1 is done: the per-frame clamp; the decode moved into the module, now two segments $C980-$C9FE and $CF00-$CFEF under one ID; new teeth; the production-path attended leg; the diag mode. **Round 2 of rev14-mouse's review is still open.**
  - Known limit: the 1351 at turbo (docs/HARDWARE.md).
  - Supervisor at the merge: snap.s zeroes inp_mon on resume, as it does spr_vec.
- **MAIN layout limit:** CODE must end below $8D00 and LOWCODE below $0900; BSS ends at $9F47. A future wave needs a MAIN restructure.
- **Variant layout:** the FETCH_DIRECT link-check variant shifts core BSS, which put td_fvec on a page end ("jmp (abs)" straddle). The td vector asserts are now product-layout only (`.ifndef FETCH_DIRECT`); a layout-independent vector placement is a follow-up.
- **rev14-mouse round 2 (c09f941): FIX, landed as a known limit.**
  - The ±127 per-frame clamp resets at the frame hook, but AROS reads JOY0DAT later, in its VBL handler. A late sample can push one AROS delta to 158 counts, which wraps: an occasional ~50 px backward jerk on a sustained very fast flick. VICE showed 1 in ~15 saturated frames.
  - Planned fix for the next session: clamp at ±63, or reset the budget on the guest's JOY0DAT read.
  - Irrelevant on the C64U until the 1351-at-turbo fix lands.
  - Also noted:
    - the stale comment in arm_inpm;
    - a select toggle ~10 cycles before each SID read;
    - the production frame hook (~+690 cycles with the module armed) is not covered by the lag gate.
