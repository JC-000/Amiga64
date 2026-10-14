# Wave 12 Brief — visible display ∥ UCI REU I/O + suspend/resume ∥ code-window cache

Supervisor-authored, **binding**.
- `docs/WAVE4_BRIEF.md` C1–C5 still apply. **C5: red → green, adversarial.**
- `docs/CONTRACTS.md` holds for the files it covers.
- Design references:
  - `docs/WB_BOOT.md`: §4.5 code windows, §0 conclusions.
  - `docs/S3_PLAN.md`.
  - The user's direction (2026-10-08) behind W12-V and W12-T is quoted in each package.
- Changing a contract needs supervisor sign-off. Report it; don't work around it.

**Base:** `wave12/integration` (main 464b41c + this brief).
- Work ONLY in your supervisor-created worktree. **Never run mutating git** (the supervisor commits).
- Evidence goes in `build/evidence/<pkg>/{red,green,mutants,...}`, with a running `PROGRESS.md`.
- Scratch goes only in `<scratchpad>/<pkg>/`.
- Run `make vectors` and `python3 -m host.handvec --out build/vectors-hand` before any gate.

## Operating rules (all agents)

- **Devices only through c64-test-harness.**
  - VICE, the U64E (192.0.2.81) and the C64U (198.51.100.158) are started, driven and stopped only through the harness: its lifecycle, its DeviceLock, and `tests/bench/u64_session.u64_hold` (config snapshot and exact restore).
  - **Never `pkill` or `killall`. No raw `curl`, FTP or REST to a device, not even a read-only GET.**
  - Stop your own stuck run with the harness teardown or the exact PID the harness gave you.
- **Hardware is queued, not scheduled.** Slow legs run whenever you need them, through the DeviceLock.
  - **Default to the C64U at 64 MHz** (`U64_HOST=198.51.100.158 --mhz 64`).
  - Use the U64E at 48 MHz as the cross-check before you report done.
  - The user is remote, so nobody watches the screens. Another session, `c64-test-harness-c2`, also uses both devices.
  - Every hold appends to `build/evidence/<pkg>/SESSION.log`, and the config must read back IDENTICAL.
- **C64U facts** (memory, `docs/HARDWARE.md`):
  - ~112 ms per REST read: 53 ms RTT, the same at every turbo speed.
  - `read_bytes` splits reads over 256 B into 128 B GETs. `transport.read_memory` reads up to 1024 B in one GET (verified).
  - fw 1.1.0: no Temp-folder fix, the DMA service is off after a power-cycle, and REST writemem is slow at ≥ 16 KiB.
- **Suite shape (user rule).**
  - No suite made of many discrete shell commands or subprocesses. Batch: one build, one emulator session, many cases.
  - Host tests run in-process with pytest.
  - **≤ 10 mutants per package**, each built once.
  - Runners finish in minutes; state the wall-clock in your report.
- **Long jobs:** use your tool's background mode with a DONE marker, never `( ... ) &`: a non-interactive shell's `&` ignores SIGINT.
- **Builds are serial** within a worktree: cl65 objects land in `runtime/*.o`.
- **Clean room.**
  - No AROS source, ROM bytes or disassembly in the repo or in your reasoning.
  - **Do not open `~/.cache/amiga64/aros-src/`.**
  - The ROM and the ADFs are black boxes loaded at test time and never committed. Observing behaviour is fine: serial text, registers, the boot model's traces.
  - The Ultimate firmware (GPL, `~/Documents/1541u-*` checkouts, c64-test-harness docs) MAY be read for interface facts. Cite the file and line.
- **Context economy.** Read this brief, the cited sections, the done-gate (`.serena/memories/task_completion.md`) and the source you change. Use grep and line ranges.

## Memory budget (C64 RAM; `make rescheck` enforces ownership)

- **MAIN:** BSS ends at $9DEE, leaving **529 B** to $9FFF. **W12-V ≤ 64 B, W12-T ≤ 192 B, W12-C ≤ 128 B** of MAIN growth (CODE+DATA+BSS), measured from `runner.map`.
- **BHI** ($A000-$B5FF; RAM under BASIC; BCODE in use to $A7CD): **W12-C owns $A800-$B0FF** (8 windows × 257 B fits). **W12-T owns $B100-$B5FF.**
  - BHI is not host-readable on the Ultimate.
  - It is accessible to the guest only while BASIC is banked out. **Prove** the `$01` value inside `mach_run`/the interpreter before relying on it.
- New REU regions: claim them in `runtime/rc/guest.inc` / `machine.inc`, labelled with your package.

## Packages and file ownership

| Pkg | Agent | Owns |
|---|---|---|
| **W12-V** visible display | `w12-vis` | The colour choice of the conversion: in `host/c2v_ref.py` and the c2v kernels / `runtime/ops/disp.s`, only the code that picks LUT colours and cell colours, labelled `; W12-V`. A host-readable matrix checksum (new BSS bytes in `disp.s`). `tests/aros/model_frames.py` (regenerate). In `tests/aros_boot.py` only `c64_hash`/`leg_m3` and the model loader. `tests/run_c2v.py` / `tests/run_pf.py` expectations. CONTRACTS: the display sections' colour text. A new PERF note. |
| **W12-T** UCI REU I/O + suspend/resume | `w12-reu` | New `runtime/ops/ureu.s` (UCI DOS REU load/save; may reuse `dosio_uci.s` primitives by import, or add exports there labelled `; W12-T`). New `runtime/ops/snap.s` (suspend/resume). Their Makefile/rescheck registration lines. New `tests/aros_resume.py`, `tests/ureu*.py`, `host/tests/test_snap*.py`, `host/snapfmt.py`. In `host/machine.py`, additive only. CONTRACTS: new sections "UCI REU I/O" and "Suspend/resume". HARDWARE: probe results. |
| **W12-C** code-window cache | `w12-cwin` | In `runtime/core.s` only the code-window refill path, `code_win` storage/selection and `w_inv`, labelled `; W12-C`. The BHI window area. A new runner leg or `tests/run_cwin.py`. Host counters. CONTRACTS: the core fetch / code-window text. PERF: a new section. |

**Frozen (supervisor only):**
- everything else in `core.s`;
- `chip.s`, `cia.s`, `cop.s`, `blt.s`, `mach.s`, `hle*.s`, `g*.s`;
- `dosio_*.s` except W12-T's labelled exports;
- `host/image.py`, `host/gen_dispatch.py`, `runtime/macros.inc`;
- every existing claim line in `ram.inc`/`zp.inc`/`guest.inc`;
- `tests/rec_gate.sh`, the existing runners (beyond the named functions), all brief files, `docs/ARCHITECTURE.md`.

## Interfaces between packages

- **I1 (C → T):** `core.s` exports `code_invall`: invalidate every cached window, so the next fetch refills. W12-C provides it; until it lands, W12-T calls a stub that sets `w_inv`. snap.s's resume calls it, and calls nothing else of W12-C.
- **I2 (V → T):** resume restores `disp_st` and forces one conversion (bump the pixel epoch `disp_st+105` the CONTRACTS way). That rebuilds the screen with whatever colour logic is in the build. V and T don't touch each other's files.
- **I3:** one suspend format version number, in `host/snapfmt.py` and `snap.s`. The header holds the runner build ID (hash of the PRG, embedded at build time) and the ROM SHA-256. A mismatch refuses to resume, and that is tested.

## W12-V — the insert-disk screen must be visible

User: *"It's pretty hard to respond to insert floppy if you can't see it."*

Finding (supervisor, 2026-10-08):
- The insert-disk picture (eyes, logo, "waiting for bootable media" text, icons) is drawn in OCS pens 1–3 ($111, $222, $024) on pen 0 $000.
- The nearest-colour LUT maps all four to C64 colour 0. In hires mode every cell becomes black on black, so the C64U showed black.
- m3 passed on hardware because the U64 hash leaves the screen matrix out (`nomatrix`), and the matrix carries all hires colours.

Required:
- **V1:** contrast-preserving colour choice. Pens that differ in the OCS palette must map to C64 colours a viewer can tell apart, also when the whole palette is dark. Pen 0 stays the background.
  - The rule is yours (e.g. luminance-ranked mapping of the pens in use, or a contrast stretch), but it is deterministic, documented in CONTRACTS, and identical in `c2v_ref.py` and the 6502 code.
  - Existing c2v/pf vectors may change only where the new rule says so. List every changed expectation and why.
- **V2:** visibility is checked on hardware. disp keeps a host-readable checksum (or a colour histogram) of the screen matrix in MAIN BSS, updated at each conversion. The U64 m3 hash includes it, so a wrong palette fails m3 on the Ultimate.
- **V3:** m3 on VICE, the C64U (64 MHz) and the U64E (48 MHz), with the regenerated model. The model's insert-disk frame has ≥ 3 distinct non-background colours in use. Assert it.
- Put a PNG of the converted frame (rendered from the C64 memory you read) in your evidence. It is generated output, not ROM bytes, and stays in `build/`.
- **Perf:** report conversion cost before and after (DISP budget, PERF (l)).

## W12-T — load the REU through UCI, then suspend and resume

User: *"You can save the state of the REU and for something with a long boot time … boot to the REU memory state is almost instant."*

**T1 — probe and loader:**
- **UCI DOS REU commands** (relayed from `c64-test-harness-c2`, source-read, unmeasured; verify and cite `software/filemanager/dos.h`/`dos.cc` yourself):
  - Target DOS1 $01 (or DOS2 $02). Sequence: OPEN_FILE $02 → **LOAD_REU $21 / SAVE_REU $22** → CLOSE_FILE $03. Without an open file the status is "file not open".
  - Payload after target/command: addr 4 B LE (24-bit), len 4 B LE (25-bit). The transfer starts at the file's current position.
  - addr+len past 16 MB is clamped with status "truncated". The data reply is the text "$xxxxxx BYTES LOADED TO REU $xxxxxx": **use its count, not status OK** (a short file gives OK and a smaller count).
  - The firmware writes REU backing memory regardless of config. The guest sees it only with the REU Enabled and REU Size ≥ what you use: set it inside the hold, and u64_hold restores it.
  - Cartridge Preference must not be External (#359).
  - dos.h/dos.cc are identical across the C64U 1.1.0 and U64E 3.15 builds.
- **Storage:** C64U `/SD/amiga64/`, U64E `/USB1/amiga64/`. Never `/Flash` or `/Temp`.
- Measure on the C64U (fw 1.1.0, 64 and 1 MHz) and the U64E (fw 3.15, 48 MHz): throughput, correctness by checksum, and the effect on a running guest at turbo.
- **ADF upload:** no harness API exists. The harness owner is deciding between adding one and doing a one-off upload. **Do not write device storage, by any means, until the supervisor relays the answer.**
  - The ADF is `~/Documents/Amiga Disk Images/amiga-os-310-workbench.adf`. It is never committed, and its content is a black box.
  - Until the upload is available, develop the 6502 path against VICE's fake UCI (`tests/run_dos.py build_fake_uci`). There is no harness-placed file to probe with (/Temp is a swept RAM disk, /Flash is off limits), so the device LOAD/SAVE probe waits for the storage decision (supervisor correction, 2026-10-08).
- **Deliverable:** `ureu.s` loads a named file into a REU range and saves a REU range to a file. The AROS ROM can then be staged by the C64 from storage as an alternative to host staging. Host staging stays for VICE.

**T2 — suspend/resume:**
- **Suspend** happens at an instruction boundary in `mach_run`. Trigger: a host-set request byte for tests. A key combination is optional and documented.
  - The C64-side machine state goes into a claimed REU area: the 68k registers and the state of `core`, `chip`, `cia`, `cop`, `blt`, `mach` and `disp_st`. Every BSS/zp byte the guest's progress depends on must be listed and justified.
  - Then `ureu` saves the used REU range plus a header (I3).
- **Resume:** load the file into the REU and restore the state. Then `code_invall` (I1), a forced conversion (I2), and `mach_run` continues.
- **Milestone:** the C64U at 64 MHz boots AROS to m3 and suspends. A reset follows (harness reset; the config is restored by u64_hold). Resume runs from a fresh runner load; the guest continues and m3's screen hash matches again within **≤ 15 s** of starting the resume.
  - Serial output after the resume continues the boot model's text.
  - Cross-check on the U64E.
- **Red:**
  - a resume with a corrupted header byte or a wrong build ID refuses;
  - a resume with one state block left out (a mutant) fails the continuation check.

## W12-C — code-window cache

- Replace the single `code_win` with an LRU (or clock) of **N windows (4 or 8, measured)** in BHI $A800-$B0FF.
- The tag check runs only on a page change. **The per-instruction fetch path must not get slower:** `perf_cycles --max-delta 0` on every row. A page change may cost more, but report it.
- Invalidation correctness:
  - guest writes into a cached page (SMC);
  - REU/blitter writes;
  - `w_inv` users;
  - the new `code_invall` (I1).
  
  The existing SMC suite (42) must pass, and you add cases for a write into a non-current cached window.
- **Measure:**
  - refills per instruction on the AROS boot to m3 (VICE, counter build), against `WB_BOOT.md` §4.5's model figures;
  - m1/m2/m3 wall-clock on the C64U at 64 MHz, before and after (main's figures: 15.3/45.3/76.9 s).

## Done-gate per package

All of these pass and are recorded in `build/evidence/<pkg>/GATES.txt`:
- the task_completion gate: host-test, vectors/golden, hand, super, dos, vbcc, con, smc, chip_oracle, run_chip/c2v/pf/blt/mach/irq/cia/cop and their mutants;
- `perf_cycles --max-delta 0`, `make rescheck`;
- link-swap / mapdiff;
- the AROS legs on VICE (m1–m3, ablate, machine-loop legs) and on the C64U, plus your package's own legs, mutants, and red → green.

Then report to the supervisor. The supervisor merges into `wave12/integration` in the order **C, V, T** (T last: it touches the most state), re-runs the combined gate, and has an adversarial reviewer per package.
