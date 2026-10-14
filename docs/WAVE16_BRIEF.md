# Wave 16 Brief — the MOD player launched from Workbench

Supervisor-authored, **binding**.
- `docs/WAVE4_BRIEF.md` C1–C5 apply. **C5: red → green, adversarial.**
- The operating rules of `docs/WAVE13_BRIEF.md`, `docs/WAVE14_BRIEF.md` and `docs/WAVE15_BRIEF.md` (including its Addendum) carry over unless this brief says otherwise.
- `docs/CONTRACTS.md` holds for every file it covers. Changing a contract needs supervisor sign-off.

**The user's direction (2026-10-12), after hearing the Wave 15 player on the C64U:**
> *"I was expecting something that would run in a workbench window, is that feasible?"*, then, choosing option A: *"Lets go for A - launch our existing full screen UI from within workbench".*

**Goal.**
1. The user boots Workbench 3.1 from the ADF on the C64U or U64E.
2. They open a disk, double-click a **MOD player icon**, and our Wave 15 player takes over the screen: the full-screen readable UI plus Paula on the Ultimate sampler.
3. It plays a MOD file that it loaded from the disk.
4. On a click (or at the song's end) it hands the machine back, and **Workbench returns intact**.

This is classic Amiga "system takeover" practice; it is not an OS-friendly window (that is option B, which needs readable hires text in the converter, a later wave).

**Base:** `wave16/integration` = wave15/integration 2ac0785 + this brief. The remaining Wave 15 items (W15-P's 0-byte walk-exit fix, W15-M's `run_player` rig fix and U64E pitch run) are merged in when they land; rebase notices come from the supervisor.

**Fresh agents.** Every agent in this wave is new. Read the Wave 15 handoffs first:
- `build/evidence/w15-paula/PROGRESS.md` (HANDOFF section) in worktree `w15-paula`;
- `build/evidence/w15-mod/PROGRESS.md` and `build/evidence/w15-m3/PROGRESS.md` in worktrees `w15-mod` and `w15-m3`;
- `build/evidence/w15-ui/PROGRESS.md` in `w15-ui`.

Worktrees: `.claude/worktrees/w16-<pkg>`, branch `wave16/<pkg>`. Evidence goes in `build/evidence/w16-<pkg>/` (PROGRESS.md, SESSION.log). Scratch: `<scratchpad>/w16-<pkg>/`. Report to "main".

**Context discipline.** Keep messages to the supervisor short. Keep the details in PROGRESS.md, and write a HANDOFF section when your package is done.

## Operating rules (unchanged in spirit)
- Devices only through c64-test-harness: DeviceLock and `u64_hold`, with the config IDENTICAL afterwards.
  - Never pkill or killall. No raw REST.
  - Never use the Ultimate menu.
  - Long runners: SIGTERM leads to the restore; deadlines; DONE markers you poll.
- **Devices:**
  - **C64U** at 192.0.2.158 (uid def456): NTSC, 64 MHz, the sampler present ($DF21 = $10), WiFi, so **no UDP audio stream**.
  - **U64E** at 192.0.2.81: NTSC, 48 MHz, wired, so the audio stream works.
  - Ask the supervisor before any hold. The supervisor runs attended legs with the user.
- **Clean room.**
  - No AROS source (`~/.cache/amiga64/aros-src/`), no Commodore NDK or ROM sources, no MOD replay sources.
  - AmigaOS library call offsets (LVOs), structure offsets and the hunk, ADF and .info formats come from public documentation; cite them in PROGRESS.md.
  - The user's Workbench ADF is LOCAL ONLY. Overlays are applied to the REU copy, never committed (`host/adf_overlay.py` pattern).
- Builds are serial. ≤ 10 mutants per package. VICE runs in background mode.
- **MAIN is full.** CODE has ~59 B of margin, and the run_pf trace variant has 4 B. New 6502 code goes in machine-mode modules. Any MAIN byte needs a ruling.

## Packages

| Pkg | Owns |
|---|---|
| **W16-P** Paula in the Workbench profile | `tests/paula/*` placement and arming changes (labelled `; W16-P`), the arming hook in the Workbench launchers (`tests/aros_wb.py` demo/e2e helpers, labelled), ram.inc/rescheck proposals, CONTRACTS "Audio (Paula)" updates |
| **W16-X** the AmigaOS launcher program | `fixtures/modwb/` (new): the hunk executable that takes over, runs the Wave 15 replay and UI, and restores; its build; host tests for it; `tests/run_modwb.py` (VICE legs without Workbench, e.g. under a minimal exec stub or the AROS boot) |
| **W16-D** disk and Workbench end-to-end | the ADF overlay that adds the player, its `.info` icon and a generated MOD (`host/adf_overlay.py` extension or `host/adf_files.py`, new); the e2e leg (boot Workbench, open the disk, double-click the icon with the scripted 1351/stick path, check takeover, music state and return); the attended demo command |
| **W16-I** 1351 polish (small) | `runtime/ops/inp.s` module segment, `host/inp_ref.py`, `tests/run_inp.py`: idle-jitter hysteresis and the Wave 14 clamp-margin follow-up |

## Design questions each M0 note must answer (before code)

**W16-P:**
- (1) Where the `aud` module lives so it coexists with the Workbench machine profile: mboot $C000-$C1FF, td $C200-$C5FF, spr $C600-$C8FF, inpm $C980-$C9FE and its segment B $CF00, the aros_resume stub $CB00/$CC00, the mailbox $CE00, and aud A $CA00-$CDFF and B $0400-$07FF today. Does AROS machine mode touch $0400-$07FF?
- (2) Arming during the Workbench boot (demo-boot) and, if feasible, across suspend/resume.
- (3) The R factor and PARK_E at the Workbench profile's s (3 on the U64E/C64U today).
- (4) How the guest program learns the real-time tick_e for the current profile. The program can't measure real time itself. Propose a host-written, guest-readable profile block, a reserved chip-RAM location or a module register. The supervisor rules, and W16-X consumes it.

**W16-X:**
- (1) The takeover/restore sequence as an AmigaOS program: Forbid/Disable, saving INTENA/DMACON/vectors/COP1LC (or the View), the audio channels (audio.device allocation, or the documented takeover), the blitter (OwnBlitter/WaitBlit), the restore order, and the return to Workbench.
- (2) Reusing the Wave 15 replay.s and ui.s unchanged where possible. The guest map changes under the OS: the program is loaded by DOS, chip RAM is allocated (AllocMem MEMF_CHIP) for the UI planes and copper and for the MOD. mod_view and params can't sit at fixed $000400/$000480 under exec, so propose the relocation (a pointer in a register or a struct) and its impact on W15's I-1 and ui.s.
- (3) Loading the MOD (dos.library Open/Read, size, a chip copy).
- (4) The exit conditions: LMB/fire, song end, an optional key.
- (5) The real-time tempo from W16-P's profile block.
- (6) The test strategy: VICE legs under the real AROS boot, or under a small exec stub, plus the model leg where useful.

**W16-D:**
- (1) The ADF write path: adding files and a drawer (OFS/FFS, bitmap, checksums) from public format docs, reusing `host/adf_overlay.py`.
- (2) The `.info` DiskObject format, plus a minimal readable icon.
- (3) The e2e script: which Workbench window or icon positions (deterministic after the trimmed boot?), the double-click via the scripted mouse (W14 `--e2e-mouse` path), and the takeover/return detection (copper list, display hash, mod_view state).
- (4) Run times on VICE versus hardware.

**W16-I:**
- (1) Hysteresis that removes the ±1 idle wiggle (the 1351 at rest alternates POTX $C9/$CA, POTY $AB/$AC) without losing slow movement.
- (2) The clamp-margin fix (WAVE14_BRIEF addendum; the rev14-mouse flick_probe.py check).
- (3) Module bytes and cost.

## Process
1. M0 notes to the supervisor (short message, details in PROGRESS.md). Rulings go into a brief addendum.
2. Red first; reviewers per package; fix rounds until MERGE.
3. The supervisor merges in the order P, I, X, D, gates, then runs the attended Workbench demo with the user.

## Addendum: supervisor rulings (2026-10-12)
- **Profile block (W16-P):**
  - Guest $2DC000-$2DC03F, rc region `prof` $2DC000-$2DC0FF, right after the ADF staging ($200000-$2DBFFF).
  - 64 B big-endian: 'A64P', ver, size, e_hz_q8, tick_e, frame_e, phi2_hz, s, host, flags, r16, MHz, and a check at +60.
  - The host writes it only while the guest is stopped. If the check fails, the guest falls back to Amiga timing.
- **App anchor (W16-X writes, host reads):**
  - $2DC040-$2DC04F: +0 'MWBA', +4 a state-struct pointer, +8 seq/flags.
  - It is the single way tests and e2e find the player (not tc_UserData, not a scan).
- **aud in Workbench sessions (W16-P):**
  - The same addresses and bytes. It is staged and init'ed last, before m.start or the snap_resume launch.
  - GuestPeek saves and restores PEEK_BUF.
  - leg cost and --watch refuse --audio.
  - A mid-play snapshot resumes silent (a known limit).
  - The sampler proof is differential (MAP item off vs on).
  - Until the reboot-silence check is done, the player ends with DMA off.
- **W16-X:**
  - The OS-claim takeover/restore. It backs out cleanly if a claim fails.
  - The MOD_HUNK/UI_HUNK switches, with the default W15 builds byte-identical.
  - P_STAGEE from the profile E rate.
  - The exit on LMB/fire or song end.
  - Leg S (exec stub) is the gate; leg A (real AROS) runs on demand.
- **W16-D:**
  - amigaguide.datatype is deleted from the REU copy only, with an asserted no-read.
  - The CONTRACTS overlay text is widened to cover adding and deleting files.
- **W16-I:** the direction-following 1-count deadband, ±63 saturation, and 0 MAIN bytes.
