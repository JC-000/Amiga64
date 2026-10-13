# Wave 17 Brief — "I just want to run it": the host CLI, the release kit and the emulation docs

Supervisor-authored, **binding**.
- `docs/WAVE4_BRIEF.md` C1–C5 apply. **C5: red → green, adversarial.**
- The operating rules of `docs/WAVE13_BRIEF.md` through `docs/WAVE16_BRIEF.md` (including their addenda) carry over unless this brief says otherwise.
- `docs/CONTRACTS.md` holds for every file it covers. Changing a contract needs supervisor sign-off.

**The user's decisions (2026-10-13), after Wave 16 merged (main de5ab0e):**
- **Run mode: a host CLI first.** One installable command drives the Ultimate over the network, as our tools do today. The standalone version that needs no computer is scoped for a later wave.
- **Workbench images:** the user is prompted for them. The default is a relative path (`./adf/`), or the user gives one. *"We include or automatically fetch as much as possible."*
- **AROS ROM: bundled in the repo**, the exact tested build, *"to keep things simple for 'I just want to run it'"*. It carries the APL 1.1 licence text and a source-location notice.
- **Licence: MIT** for our code. Third-party parts keep their own licences.
- **Prebuilt binaries ship**, so users don't need cc65 or vasm.
- **c64-test-harness:** pinned to a tagged release. The supervisor has asked the harness project for one.
- **Docs:** thorough and accurate documentation of the emulation, so others can carry the project forward. They include instructions for, and the current support state of, Amiga disk images (ADF) and copying files into the emulated environment.
- **Scope:** this is a proof that it's possible. Pursue simplifications, not compatibility edge cases.

**Goal.** An outsider with a C64 Ultimate or an Ultimate 64 Elite, a Mac or Linux host and their own Workbench 3.1 ADF can do this:
1. Run `pip install <repo>@<tag>` (or a clone plus `pip install .`). No cc65, vasm, VICE or AROS download is needed.
2. Run `amiga64 doctor`. It checks the device, the firmware, the REU, the storage volume, the ROM and the ADF, and says plainly what is wrong.
3. Run `amiga64 run`. It is prompted for the device address and the ADF the first time, then boots Workbench on the Ultimate with the ModPlayer icon on the disk.
4. Play with Workbench on the joystick or the 1351, double-click ModPlayer to hear Paula, and click to return to Workbench.
5. Read docs that explain how all of it works and what doesn't.

**Base:** `wave17/integration` = main de5ab0e + this brief. Every PR targets **main** and is not stacked.

**Fresh agents.** Read the Wave 16 handoffs first:
- `build/evidence/w16-*/PROGRESS.md` (HANDOFF sections) in the `w16-*` worktrees;
- `build/evidence/w16-e4/` in `w16-e2e2`, which has the confirmed modwb hardware commands.

Worktrees: `.claude/worktrees/w17-<pkg>`, branch `wave17/<pkg>`, created by the supervisor. Evidence goes in `build/evidence/w17-<pkg>/` (PROGRESS.md, SESSION.log). Scratch: `<scratchpad>/w17-<pkg>/`. Report to "main". Agents never run mutating git.

## Operating rules
- **Devices only through c64-test-harness:** DeviceLock and `u64_hold`, with the config IDENTICAL afterwards.
  - Never pkill or killall; stop only PIDs you started.
  - No raw REST, curl or FTP. Never use the Ultimate menu.
- **Long runners:** reinstall `signal.default_int_handler`; give every leg a deadline; SIGTERM leads to the restore; write DONE markers you poll.
- **Devices: both are free for this wave (user 2026-10-13).**
  - **U64E** at 10.43.23.81: NTSC, 48 MHz, wired, volume /USB1.
  - **C64U** at 10.43.23.26 (uid 5D2518, fw 1.1.0): NTSC, 64 MHz, **ethernet** (the UDP audio stream works), volume /SD.
  - Tell the supervisor before each hold. Only one agent holds a given device at a time, and the DeviceLock queue decides.
- **Clean room, as before.**
  - No AROS source and no Commodore NDK/ROM sources.
  - The bundled AROS ROM is the pinned **binary** only (tools/aros/manifest.txt hashes), with its LICENSE and ACKNOWLEDGEMENTS plus our SOURCE notice. No disassembly of it goes in the repo.
  - **The user's Workbench ADFs are never bundled, committed or uploaded anywhere** except to the user's own Ultimate volume, and only on the user's command.
- Builds are serial. MAIN is full: this wave adds **no 6502 MAIN bytes**. Any byte needs a ruling.

## Packages

| Pkg | Owns |
|---|---|
| **W17-B** prebuilt path | A `make prebuilt` target and a `prebuilt/` tree: `runner.prg`/`.lbl`/`.map`, every module the Workbench session assembles at run time (mboot/td_arm, td, spr, inpm, inpm2, aud) and `modwb`. Also `prebuilt/MANIFEST` (SHA-256, build ID, module IDs, toolchain versions). Refactors `tests/aros_wb.py` `build_module`/the driver build, `tests/run_paula.py`, `tests/aros_boot.py` and `tests/aros_modwb.py` so they **load the prebuilt binary when the toolchain is absent or `AMIGA64_PREBUILT=1`**, with the ID check unchanged. Labelled `; W17-B` / `# W17-B`. Adds a host test that rebuilds from source and compares with `prebuilt/` byte for byte (skipped without the toolchain). |
| **W17-K** the CLI and release kit | A new package `amiga64/` (cli, config, device, adf, rom) with the `amiga64` console script. pyproject: name, version 0.17.0, deps (the harness tag; numpy optional), package data. `third_party/aros/` (rom, ext, LICENSE, ACKNOWLEDGEMENTS, SOURCE.txt). The MIT `LICENSE` and `NOTICE`. `tools/aros/fetch.sh` stays for developers but the CLI never needs it. CLI commands: `doctor`, `run`, `resume` (the saved session), `adf add` (files in), `adf ls`. |
| **W17-D** the emulation docs | `docs/guide/` (new): QUICKSTART, DEVICES, DISKS_AND_FILES, and TROUBLESHOOTING. `docs/emulation/` (new): OVERVIEW/architecture, CPU (68000 interpreter, fetch window, code cache, exceptions), MEMORY (REU map, machine profile, chip/fast/ROM, rc regions), HLE (exec/dos/console, superseded parts flagged), CHIPSET (copper, blitter, c2v display, sprites, F1 view), PAULA, CIA_AND_TIMING (time base, profile block, s/R), INPUT (joystick, 1351), STORAGE (td/ADF, overlay, REU snapshots), TESTING (harness, VICE/hardware legs, vectors, oracle, mutants), and LIMITS. README rewritten for an outsider. Stale docs (ARCHITECTURE "research phase", PERF, S3_PLAN, M0, the HLE trio) are marked historical with a pointer to the new docs, not deleted. |
| **rev17-docs** docs accuracy (reviewer) | Read-only. It checks every factual claim in W17-D's docs (addresses, sizes, timings, commands, limits) against the code and the evidence, and reports per file. It also runs QUICKSTART **verbatim** in a clean venv against one device. |

Reviewers per package as usual (rev17-b, rev17-k). rev17-docs is the docs package's reviewer.

## Rulings in force (supervisor, 2026-10-13)
- **The CLI's `run` is the attended path.** It is `demo-boot` with the modwb disk: the ModPlayer, its icon and a generated MOD are on the overlay, real input comes from the joystick or 1351, and there is no model screen check (no s3probe venv). The scripted `modwb` leg stays a test.
  - If demo-boot can't take `--disk modwb` today, W17-K adds that, labelled, in `tests/aros_wb.py`, coordinated with W17-B. Otherwise the CLI calls the existing functions in-process. **No subprocess fan-out to test scripts.**
- **The device address** comes from, in order: `--host`, `AMIGA64_HOST`, `U64_HOST`, then the config file `~/.config/amiga64/config.toml`, which the first run prompts for and writes.
  - The device kind (U64E/C64U), the MHz (48/64) and the volume (/USB1, /SD) are detected through the harness and can be overridden with flags.
  - A password, if one is set, comes from `U64_PASSWORD` or the config file, never from argv.
- **ADF:** the default directory is `./adf/`. Precedence: `--adf PATH`, then the config, then `./adf/*.adf`, then a prompt.
  - The image must be DD (901,120 B) and DOS\0–\3 without dircache, the existing `adf_overlay` checks. A known-good SHA-256 list (the tested Workbench 3.1 image) gives "tested"; any other passing image gives "untested, may work".
  - The CLI uploads it to `<vol>/amiga64/` itself (the `ensure_adf` pattern: no overwrite, then a SHA check), after a one-line confirmation.
  - The user's file is never modified.
- **Files in (`adf add`):** host files and drawers go onto the **REU copy for this session** via `DiskEdit.add_file`/`mkdir`, or into a new local output ADF (`--out`). The user's original is never modified.
  - Limits are reported, not worked around: free blocks, name rules, no overwrite, and the deletion policy (only `amigaguide.datatype` as today).
  - No new host-to-guest channel this wave. The docs describe the existing paths (overlay, REU staging, the HLE dos RAM disk/UCI storage for non-Workbench programs) and their limits.
- **ROM:** `third_party/aros/` holds the pinned files, byte-identical to the manifest hashes. `host/machine.find_rom` prefers the bundle and falls back to the cache. The README and NOTICE state the APL 1.1 terms and the source location.
- **Harness pin:** `c64-test-harness @ git+https://github.com/JC-000/c64-test-harness@v0.13.0` (= 4f51020; no runtime deps, Python ≥ 3.10). The tag is pending push by the harness project: until the supervisor confirms it, pin `@4f51020` and leave a TODO. A stale `0.12.4` dist-info in local envs is an old install, not a harness bug. Anything the CLI needs that the harness lacks is reported to the supervisor, never patched locally.
- **Python ≥ 3.11, macOS and Linux.** Windows is a documented non-goal for now.
- **Snapshots:** `resume` uses `<vol>/amiga64/wb-snap.bin` and refuses a different build ID or ROM ID with a plain message. `run --save` writes it.

## Design questions each M0 note must answer (before code)

**W17-B:**
- (1) The full list of run-time assembled artifacts (every `build_module`/cl65/vasm call reached by demo-boot, modwb, resume and aud), and which of them depend on `runner.lbl` values.
- (2) Is the build reproducible (same bytes from a clean tree)? Name the toolchain versions to pin in MANIFEST.
- (3) How to keep `prebuilt/` from going stale: a host test, a `make prebuilt-check`, or both. What fails when the source changes and nobody rebuilds?
- (4) Size of `prebuilt/` in git.

**W17-K:**
- (1) The in-process call chain from `amiga64 run` to `leg_demo_boot` (or a refactored entry point): what moves out of `tests/` into the `amiga64/` package and what stays a test. Keep the `tests/` CLIs working.
- (2) Device detection through the harness (kind, MHz, volume, firmware, REU), and what `doctor` checks and prints.
- (3) The packaging route: setuptools with package data for `prebuilt/` and `third_party/aros/`, and how `pip install git+…` finds them.
- (4) The first-run prompts and the config file schema.
- (5) The `adf add` UX and the error messages for each limit.
- (6) The test strategy: host tests for config, adf and the prompts (fake device); a VICE leg (`amiga64 run --backend vice`, short); and one hardware run per device for acceptance.

**W17-D:**
- (1) An outline per file with the sources (code paths, CONTRACTS sections, evidence) each section is drawn from.
- (2) Which existing docs are superseded, and how they are marked.
- (3) The support-state tables: ADF (formats, read-only vs overlay, user-supplied WB disks, what is tested) and files-in (each path, its limits).

## Process
1. M0 notes to the supervisor (short message, details in PROGRESS.md). Rulings go into a brief addendum.
2. W17-B and W17-D start at once. W17-K starts at once on config, adf, rom and packaging, and switches to the prebuilt path when W17-B lands.
3. Red first; reviewers per package; fix rounds until MERGE.
4. The supervisor merges in the order B, K, D, then runs the gate.
5. **Acceptance:**
   - A fresh clone and a clean venv, with no cc65, vasm or VICE on PATH, and no `~/.cache/amiga64`.
   - `amiga64 doctor` and `amiga64 run` on the **U64E** and on the **C64U**.
   - The docs reviewer's QUICKSTART run.
   - The attended demo with the user, last.
6. One PR to main for the wave.
