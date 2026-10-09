# Wave 13 Brief — Workbench from floppy ∥ joystick pointer ∥ readable view

Supervisor-authored, **binding**.
- `docs/WAVE4_BRIEF.md` C1–C5 still apply. **C5: red → green, adversarial.**
- `docs/CONTRACTS.md` holds for the files it covers.
- Design references:
  - `docs/WB_BOOT.md`: §0 conclusions, §2.1 the floppy in the boot model and the HLE trackdisk rules, §4.1 and §4.3.
  - `docs/WAVE12_BRIEF.md` (UCI REU I/O, suspend/resume) and CONTRACTS "UCI REU I/O" / "Suspend/resume".
  - The user's direction (2026-10-02 to 2026-10-09), quoted below.
- Changing a contract needs supervisor sign-off. Report it; don't work around it.

User direction:
- *"Get Workbench on the screen; sound comes later."*
- On the Wave 12 insert-disk screen: *"It loads and is visible … the insert floppy text is rendered too compressed to be readable but it is recognizable as text."*
- *"I have connected a joystick as well in case we want to be able to move the cursor in workbench. I don't have a commodore mouse here but we can test that in a couple days."* The joystick is on the **C64U, port 2**.

**Goal of the wave:** on the C64U at 64 MHz, AROS boots the user's Workbench 3.1 disk to the Workbench screen. The joystick moves the Amiga pointer and clicks. A 1:1 view makes the text readable. A suspended Workbench resumes in seconds.

**Base:** `wave13/integration` (main c349020 + this brief).
- Work ONLY in your supervisor-created worktree. **Never run mutating git** (the supervisor commits).
- Evidence goes in `build/evidence/<pkg>/{red,green,mutants,...}`, with a running `PROGRESS.md`.
- Scratch goes only in `<scratchpad>/<pkg>/`.
- Run `make vectors` and `python3 -m host.handvec --out build/vectors-hand` before any gate.
- When the supervisor asks you to stop, answer **"TREE FROZEN"** with your gate summary, then make no further edits until told.

## Operating rules (all agents)

- **Devices only through c64-test-harness.**
  - VICE, the U64E (10.43.23.81) and the C64U (10.53.21.158) are started, driven and stopped only through the harness: its lifecycle, its DeviceLock, and `tests/bench/u64_session.u64_hold` (config snapshot and exact restore).
  - **Never `pkill` or `killall`. No raw `curl`, FTP or REST to a device, not even a read-only GET.**
  - Stop your own stuck run with the harness teardown or the exact PID the harness gave you.
  - Long device runners reinstall `signal.default_int_handler` in `main()`, have per-leg deadlines, and run their config-restore path on SIGTERM.
- **Hardware is queued, not scheduled.** Slow legs run whenever you need them, through the DeviceLock.
  - **Default to the C64U at 64 MHz** (`U64_HOST=10.53.21.158 --mhz 64`, storage `/SD/amiga64/`).
  - Use the U64E at 48 MHz (`/USB1/amiga64/`) as the cross-check before you report done.
  - Another session, `c64-test-harness-c2`, also uses both devices.
  - Every hold appends to `build/evidence/<pkg>/SESSION.log`, and the config must read back IDENTICAL.
- **Device storage.** Write only under `<vol>/amiga64/`. Never `/Flash` or `/Temp`. Use the harness storage API.
  - The Workbench ADF is already on both devices (`/SD/amiga64/amiga-os-310-workbench.adf`, `/USB1/amiga64/…`). Read it; never overwrite or delete it.
  - Delete your own test files when a leg ends. A file left on purpose (e.g. a snapshot for the demo) is named in your report.
- **The user is often at the machine but is not a test fixture.** Every leg must pass unattended. An *attended* leg (the user moves the joystick) is optional. Prepare it as a script the supervisor runs with the user; don't block on it.
- **Suite shape (user rule).**
  - No suite made of many discrete shell commands or subprocesses. Batch: one build, one emulator session, many cases.
  - Host tests run in-process with pytest.
  - **≤ 10 mutants per package**, each built once.
  - Runners finish in minutes, except the named slow legs; state the wall-clock in your report.
- **Long jobs:** use your tool's background mode with a DONE marker, never `( ... ) &`. Poll your own DONE markers; completion notices get lost.
- **Builds are serial** within a worktree: cl65 objects land in `runtime/*.o`.
- **Clean room.**
  - No AROS source, ROM bytes or disassembly in the repo or in your reasoning.
  - **Do not open `~/.cache/amiga64/aros-src/`.**
  - The ROM and the ADFs are black boxes loaded at test time and never committed. No disk bytes in the repo or in evidence beyond names, sizes, block numbers and checksums.
  - Observing behaviour is fine: serial text, registers, the boot model's traces, the existing `tools/s3probe` tools.
  - Public Amiga interface facts (the NDK: exec structures, IORequest field offsets, trackdisk command numbers, custom-chip and CIA register maps, the OFS/FFS on-disk format) are fine. Cite what you rely on.
  - The Ultimate firmware (GPL, `~/Documents/1541u-*`) and the c64-test-harness docs may be read for interface facts. Cite the file and line.
- **Context economy.** Read this brief, the cited sections, the done-gate (`.serena/memories/task_completion.md`) and the source you change. Use grep and line ranges.

## Memory budget (C64 RAM; `make rescheck` enforces ownership)

- **MAIN:** BSS ends at $9C0B (main c349020), leaving **1012 B** to $9FFF. **W13-W ≤ 448 B, W13-D ≤ 288 B, W13-I ≤ 192 B** of MAIN growth (CODE+DATA+BSS), measured from `runner.map`. Keep the per-instruction path untouched (`perf_cycles --max-delta 0`).
- **BHI** ($A000-$A7FF, BCODE in use to $A777): the remaining **$A778-$A7FF (136 B) belongs to W13-D**. CWIN and SNAPHI are full; don't touch them.
- **Data under I/O** (RAM at $D400-$DFFF under the I/O block; data only; CPU access with `$01=$34` under `sei`, never REU DMA, like c2v's `@under` region at $D000-$D3FF). Claim with `@under` lines in `ram.inc`:
  - **W13-D $D400-$D7FF**: sprite blocks (VIC bank 3: block = (addr−$C000)/64) and view tables.
  - **W13-I $D800-$D8FF.**
  - **W13-W $D900-$DBFF.**
  - $DC00-$DFFF stays free.
- **REU (guest space):** new regions are claimed in `runtime/rc/machine.inc` (and `guest.inc` if both profiles see them), labelled with your package. Every REU byte is a guest address. **Prove** that the guest never reads or writes a range you claim: a counter build or a watch over the AROS boot to the Workbench, or a memory-API mapping that makes it unmapped. AROS sizes memory and probes autoconfig at $E80000; a staged image must never look like RAM, a board or a ROM to it.

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W13-W** Workbench boot | `w13-wb` | New `runtime/ops/td.s` (HLE trackdisk and the DF0 drive). In `cia.s`, only a labelled `; W13-W` hook in the CIA-B PRB write path (drive select, motor, step, side) and CIA-A pin bits **2-5** (/CHNG, /WPRO, /TK0, /RDY). The ADF REU region (claim). New `host/adf_overlay.py`, `tests/aros_wb.py`, `host/tests/test_adf*.py`, `host/tests/test_td*.py`. In `tests/aros/model_frames.py`, an additive Workbench frame set. In `snap.s`/`host/snapfmt.py`/`tests/aros_resume.py`, the additions a Workbench snapshot needs (labelled `; W13-W`; format version bump if the layout changes). In `host/machine.py`, additive only. CONTRACTS: a new section "HLE trackdisk and DF0". HARDWARE: results. |
| **W13-I** joystick pointer | `w13-inp` | New `runtime/ops/inp.s`: the only code that reads CIA1 (C64 keyboard matrix and joystick port 2) in machine mode. It writes the Amiga mouse counters (`chip_shd` JOY0DAT) and CIA-A pin bits **6-7** (/FIR0, /FIR1). In `chip.s`, only the POTINP read (`potinp:`), labelled `; W13-I`. Its call site in the machine loop's frame hook (one labelled line in `mach.s`). New `tests/run_inp.py`, a vbcc/asm fixture under `fixtures/`, `host/tests/test_inp*.py`. CONTRACTS: a new section "Input (joystick → mouse)". |
| **W13-D** readable view + pointer sprite | `w13-vis` | In `disp.s`/c2v and `host/c2v_ref.py`: a new **1:1 view** mode, labelled `; W13-D`. The Amiga sprite-0 → VIC sprite path (new `runtime/ops/spr.s` or a labelled part of `disp.s`). The BHI tail and the $D400-$D7FF claim. `tests/run_c2v.py` / `tests/run_pf.py` additions (new cases only). New `tests/run_spr.py`. CONTRACTS: the display sections' new text. PERF: a new section. |

**Frozen (supervisor only):**
- `core.s`; `cop.s`, `blt.s`, `hle*.s`, `g*.s`, `ureu.s`, `dosio_*.s`;
- `chip.s` and `cia.s` beyond the labelled parts above; `mach.s` beyond W13-I's one call line (and W13-D's, if it needs one; ask);
- in `disp.s`/c2v, everything that makes the **existing fit view's** output: its bitmap, matrix and colour bytes must stay byte-identical, so the existing model frames and m3 hashes stay valid;
- `host/image.py`, `host/gen_dispatch.py`, `runtime/macros.inc`;
- every existing claim line in `ram.inc`/`zp.inc`/`guest.inc`/`machine.inc`;
- `tests/rec_gate.sh`, `tests/aros_boot.py`, the existing runners (beyond the named additions), all brief files, `docs/ARCHITECTURE.md`.

A change to `core.s` (for example a trap opcode that machine mode doesn't have today) needs supervisor approval *before* you write it. Ask with the reason, the bytes it adds and a `perf_cycles` plan.

## Interfaces between packages

- **I1 (I → D):** `inp.s` exports `inp_view`, a BSS byte of view-control *edges*. Bit 0 means "toggle fit/1:1" (the **F1** key). D clears the bits it consumes. Until W13-I lands, D tests with a host-written byte.
- **I2 (I, D, W → snapshot):** every new state byte lives in MAIN BSS, so the W12-T snapshot carries it. State outside MAIN BSS (the `@under` areas, sprite blocks) must be rebuildable after a resume: D rebuilds the sprite and view from `chip_shd` and guest memory at the forced conversion. List anything else for W13-W.
- **I3 (W → D, I):** `tests/aros_wb.py` exposes a helper that boots to the Workbench (or resumes a Workbench snapshot) in one emulator session. D's and I's end-to-end legs run on the integration branch after W13-W is merged (the supervisor's combined gate). Until then, D and I test on their own fixtures and the insert-disk screen.
- **CIA-A input pins** (`cia_st+CIA_PIN`, reset to $EB by `cia_reset`): W13-W owns bits 2-5 and W13-I bits 6-7. Each writes only its own bits (read-modify-write), and re-applies them after any `cia_reset`.

## W13-W — boot Workbench 3.1 from the ADF

**W1: the disk.** The ADF (880 KB, the user's) is staged into a claimed REU range. In VICE the host writes it. On the Ultimate the C64 loads it with `ureu` `LOAD_REU` from device storage, and the byte count is checked.
- The disk is **write-protected**. No write path exists in this wave. Any write request returns the write-protect error.
- **Startup-Sequence overlay.** `host/adf_overlay.py` computes a block overlay from the ADF the host reads locally: a replacement `S/Startup-Sequence` (WB_BOOT §4.3's **trim** script by default; **min** and **full** = no overlay as options). The overlay is a handful of 512-byte blocks applied to the REU copy after staging, never to a file on any device and never to the ADF.
  - It parses OFS/FFS from the public format: root block, hashing, file header, data blocks and checksums. Host tests use synthetic images only.
  - On the Ultimate the overlay blocks are written with the harness memory API after `LOAD_REU`.

**W2: HLE trackdisk.** Serve trackdisk.device requests from the staged image, with no MFM and no disk DMA (WB_BOOT §2.1, §4.1).
- The DF0 drive must look right to the ROM's own init: the drive ID and the disk-in, /CHNG, /TK0, /RDY and /WPRO behaviour.
- How you intercept the device is your design. Options include trapping `BeginIO` once the device appears in the device list, and emulating the drive lines and disk DMA. Find which requests the ROM actually sends by observation (the boot model, VICE counters), and serve at least every quick `CMD_READ`/`ETD_READ`.
- Document the mechanism, the requests served and what happens to the rest in CONTRACTS.
- Per read, the cost is one REU→REU copy (the C64's REU can't copy REU→REU directly: say how you do it and measure it).

**W3: milestones** in `tests/aros_wb.py` (VICE plus the C64U; it skips without the ROM or the ADF):
- **w1:** the first disk request served (the boot block, block 0-1);
- **w2:** a read of the Startup-Sequence file's blocks (the host knows their numbers from the overlay);
- **w3:** **the Workbench screen.** The screen hash equals a boot-model frame from `aros_boot_model.py --adf … --td-hle --startup <same script>`, with ≥ 3 visible colours. If the title bar's free-memory text keeps changing, define a mask or a frame set and justify it.
- **Wall clock:** report every milestone on the C64U at 64 MHz. The target for **trim** is the Workbench in **≤ 240 s** from the runner start (estimate 2-3 min). Run **full** once on the C64U and report it (it may take ~10 min).
- U64E cross-check at 48 MHz.

**W4: instant Workbench.** Suspend at the Workbench to `<vol>/amiga64/wb-snap.bin`, reset, and resume from the file.
- The Workbench screen is back within **≤ 20 s** of starting the resume on the C64U.
- The resumed guest must still read the disk: the snapshot includes the staged ADF and overlay, or the resume re-stages them. Prove it: after the resume, a disk read is served correctly.
- Leave `wb-snap.bin` for the user's demo, built from the final tree. Name its build ID.

**Red:**
- the Workbench milestone fails with the HLE off;
- a read of a wrong block (a mutant offset) fails w2/w3;
- a write request succeeds (a mutant);
- an overlay with a bad block checksum is refused by the host tool.

## W13-I — the joystick moves the Amiga pointer

Hardware: a standard C64 joystick in **port 2** (CIA1 port A, $DC00: up/down/left/right/fire = bits 0-4, active low). Port 2 shares its lines with the keyboard column outputs. Scan safely and say how.

- **I1: the mouse.** Once per guest frame (the machine loop's frame hook), `inp.s` reads the joystick and adds a delta to the Amiga port-0 mouse counters, JOY0DAT ($DFF00A: Y counter high byte, X counter low byte, each 8-bit and wrapping).
  - Holding a direction accelerates: 1 counter step per frame at first, rising to a documented cap.
  - The rule is deterministic and identical in a host reference (`host/inp_ref.py`).
- **I2: the buttons.**
  - Fire = **left button**: CIA-A PRA /FIR0, bit 6, low while pressed.
  - **Right button = the C= key** (the Amiga has no C= key, so it stays free for a later keyboard map): POTINP ($DFF016) bit 10 (DATLY) low while held.
  - Workbench menus need the right button held while moving: test that both work at once.
- **I3: view keys.** **F1** sets `inp_view` bit 0 (an edge on press). No other C64 key reaches the guest in this wave (an Amiga keyboard is the next wave).
- **Test source.** A host-written override (`inp_src`/`inp_joy`/`inp_key`, like `hle_con_src`) feeds scripted input for VICE and for unattended hardware legs. The real CIA1 is read when the override is off.
- **Tests:**
  - `tests/run_inp.py` with a 68k fixture that reads JOY0DAT, CIA-A PRA and POTINP each frame and logs them, and the host reference;
  - the counter wrap, the acceleration, both buttons, and port-2/keyboard isolation (a key held while the stick moves);
  - the C64U run with the override (unattended);
  - an **attended script** for the supervisor: the user moves the stick and the log shows the counters move and the buttons change.
- **Note.** The harness has `inject_joystick` (U64 transport: writes $DC00). Check whether it can reach your code at all, since the runner drives $DC00 too. Cite the harness file. Don't rely on it without proof.

## W13-D — a readable 1:1 view and the pointer

**D1: the 1:1 view.** A second display mode next to today's fit view.
- One Amiga hires pixel becomes one C64 hires pixel, and one Amiga line one C64 line. The window is 320×200 of the Amiga screen (half its width, 200 of its lines). Per 8×8 cell: two colours, chosen by the W12-V rule applied to that cell's pixels.
  - Text drawn in two colours (Topaz on the title bar, the insert-disk text) must come out **pixel-exact**.
  - A host reference in `c2v_ref.py`, and cases in `run_c2v.py` that compare cells pixel by pixel.
- **F1** (`inp_view` bit 0) toggles fit/1:1. The fit view's output stays byte-identical (frozen above).
- **Panning:** in 1:1 the window follows the Amiga pointer. When the pointer comes within 32 C64 pixels of an edge, the window moves by a documented step and re-converts. Without a pointer (the insert-disk screen), it starts centred on the screen's middle, and a test can set the offset.
- Report the 1:1 conversion cost against the fit view's (PERF (l)), and the time from a pan to a finished picture at 64 MHz.

**D2: the pointer as a VIC sprite.** AROS draws the pointer with Amiga sprite 0 (SPR0PT; the POS/CTL words at the start of its data give the position, height and attach bit).
- Each machine-loop frame, a cheap path reads the 4 control bytes through SPR0PT (`chip_shd`, as written by the copper or the CPU) and moves a VIC sprite. The image (16 px, 2 planes, 3 colours) is re-read only when SPR0PT or the control words change.
  - DMACON SPREN off, or a pointer above the display, hides the sprite.
- The mapping from Amiga position to VIC position is exact in both views, in lores pixels:
  - the pointer's hot spot lands within ±1 C64 pixel of where the Amiga would show it;
  - fit view: the vertical scale is the fit view's.
- Colours come from the Amiga sprite palette (colours 17-19) through the LUT. Any VIC sprite trick (multicolour, two overlaid sprites) is fine. Document it.
- **Tests:** `tests/run_spr.py` with a fixture that writes sprite data and moves it: positions in both views, hiding, an image change, and a host reference.
- **Cost:** the per-frame path's cycles, which must not delay `mach_run` noticeably (report cycles per frame; the conversion budget doesn't change).

## Done-gate per package

All of these pass and are recorded in `build/evidence/<pkg>/GATES.txt`:
- the task_completion gate: host-test, vectors/golden, hand, super, dos, vbcc, con, smc, chip_oracle, run_chip/c2v/pf/blt/mach/irq/cia/cop/cwin and their mutants;
- `perf_cycles --max-delta 0`, `make rescheck`;
- link-swap / mapdiff;
- the AROS legs on VICE (`aros_boot` m1–m3, ablate, machine-loop legs; `aros_resume` mid/m3) and on the C64U (m1–m3);
- your package's own legs, mutants, and red → green.

Then report to the supervisor. The supervisor:
1. merges into `wave13/integration` in the order **I, D, W** (W last: it touches the most state);
2. re-runs the combined gate, including the end-to-end legs: boot to the Workbench, move the pointer with scripted input, open the Workbench disk's window by a double-click, toggle the 1:1 view;
3. has a fresh adversarial reviewer for each package;
4. runs the attended demo with the user on the C64U.
