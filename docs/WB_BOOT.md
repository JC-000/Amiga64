# Booting Workbench from floppy on the AROS ROM: what it costs

> **Note (Wave 17).** The Amiga64 wall-clock times here are pre-implementation
> [EST] figures (the instruction counts are [MODEL], §1 is FS-UAE). The measured boot on the devices (HLE trackdisk, trimmed script,
> 7-window code cache) is in [docs/HARDWARE.md](HARDWARE.md) "Workbench 3.1
> from the ADF": 130.5 s on a C64U at 64 MHz, 147-155 s on a U64E at 48 MHz.

Wave 6 follow-up (WB-measure). The question: should Amiga64 target
Workbench 1.3 or 3.1 on the open-source AROS m68k ROM, and how long would
either boot take? This is a measurement document. Nothing in `runtime/`
changes. Tags as in `docs/S3_PLAN.md`:

| Tag | Meaning |
|---|---|
| **[MEASURED FS-UAE]** | observed in FS-UAE 3.2.35 with the pinned AROS ROM (ground truth for "does it boot") |
| **[MODEL]** | exact counts in the host boot model (`aros_boot_model.py --adf`), not an A500 trace |
| **[SOURCE]** | read from the AROS sources (`~/.cache/amiga64/aros-src`, `2f881971e9b4`) |
| **[EST]** | derived from tagged inputs; the derivation is given (the rates are S3_PLAN §2.3/§3.5) |

The disks are the user's own images (Amiga Forever editions:
`amiga-os-310-workbench.adf`, `-310-install.adf`, `-134-workbench.adf`). They
are opened read-only where they lie and never copied. The tools take the path
as an argument, and no test needs them. Evidence is in
`build/evidence/wb-measure/` (git-ignored). It holds serial logs, JSON reports
and PNGs of the model's screen, and no disk bytes beyond file names and sizes.

## 0. Conclusions

1. **All three disks boot to a Workbench on AROS**, in FS-UAE and in the
   model, with no alert. The 1.3 disk's 53 BCPL `C:` commands and its
   `LoadWB` work: AROS has a BCPL runtime in `dos.library` and a
   `workbench.task` resident with the KS 1.2/1.3 entry point [SOURCE]. The
   Workbench you get is **AROS's Workbook in both cases**, not the
   Commodore one: 1.3's Workbench and 3.1's `workbench.library` lived in the
   Kickstart ROM, and AROS supplies its own. What a disk contributes is its
   commands, its libraries and prefs, and its script.
2. **Unmodified AROS, instructions from reset to boot idle** [MODEL]:
   3.1 Workbench **31.4 M**, 3.1 Install 13.1 M, 1.3 Workbench 24.4 M. At
   48 MHz that is **13–15 min, 5.4–6.4 min and 10–12 min** [EST]. At 1 MHz
   it is 2.6–6.7 hours. The "insert disk" point (the first disk read) comes at
   2.75 M instructions, about 1 min at 48 MHz.
3. **The biggest single cost is trackdisk's MFM decode.** The
   trackdisk.device task runs 10.5 M of 3.1's 31.4 M instructions. An HLE
   trackdisk (trap BeginIO and copy the sectors from the image: on the C64,
   one REU DMA) cuts 3.1 to **19.8 M (−37%), 8–9.4 min** [MODEL/EST]. It
   also removes 1.0 M blitter words and most guest wait time.
4. **The second is the Startup-Sequence.** With HLE trackdisk, 3.1
   Workbench's script (SetPatch, Version ×3, AddDataTypes, LoadResource,
   IPrefs, 14 Assigns …) costs 13.8 M of 19.8 M. A trimmed local script
   (MakeDir, two Assigns, LoadWB) boots in **6.0 M, 2.3–2.8 min at
   48 MHz**. `LoadWB` alone boots in **5.4 M, 2.1–2.5 min** [MODEL/EST].
5. **The third is the code window, not the AROS code.** The boot changes
   256-byte code page 0.10–0.11 times per instruction. That is twice
   S3_PLAN's pre-disk figure, and at ~90 µs per refill it is 35–40% of the
   48 MHz time. An LRU of **4 code windows removes 58%** of the refills and
   8 windows remove 68% [MODEL]. That is a runtime change, not an AROS
   one, and it helps every workload.
6. **Recommendation: target 3.1, with HLE trackdisk and a trimmed
   Startup-Sequence.** Both disks give the same Workbook. With the same
   treatment, 3.1 boots in fewer instructions than 1.3 (5.4 M against
   6.4 M with `LoadWB` only; 1.3's CON window and BCPL shell cost more). 3.1
   needs no BCPL, which leaves room to drop the BCPL runtime from a
   size-trimmed ROM later. 1.3 has no blocker, so it stays possible.
   With every saving applied (§4.6) a 3.1 boot is **~1.5–2 min at 48 MHz**
   [EST]. A 1 MHz boot stays out of reach (≥ 1 hour).

## 1. Ground truth in FS-UAE [MEASURED FS-UAE]

`tools/s3probe/wb_fsuae.py` boots DF0: with the pinned ROM on an A500
(68000, OCS). It runs headless, in warp or real time. FS-UAE here has no
headless screenshot and no console debugger, and the host denies screen
recording. So "Workbench is up" is detected by the volumes it reads: a
non-bootable `DH1:` (boot priority −128) whose `Disk.info` is a FIFO. Workbench
reads every volume's `Disk.info` when it opens, and the tool records the first
open. The serial port gives the AROS nightly's boot log and any alert.

| Disk | Memory | Mode | Workbench read `Disk.info` at | Alerts on serial |
|---|---|---|---:|---|
| 3.1 Workbench | 1 MB chip + 512 KB slow + 8 MB fast (ORACLE.md) | warp | 8.6 s wall (alone), 72 s (3 parallel) | none |
| 3.1 Workbench | 512 KB chip + 512 KB slow | warp | 60 s wall (3 parallel) | none |
| 3.1 Workbench | 512 KB chip + 512 KB slow | **real time** | **125.4 s** | none |
| 3.1 Install | oracle / plain | warp | 25.7 s / 25.4 s | none |
| 3.1 Install | plain | **real time** | **61.7 s** | none |
| 1.3 Workbench | oracle / plain | warp | 51.5 s / 54.2 s | none |
| 1.3 Workbench | plain | **real time** | **117.3 s** | none |

- Real time means FS-UAE at A500 speed with real floppy timing
  (`floppy_drive_speed = 100`). That is what the user would see on an A500
  running this ROM. The times include ~1–2 s of emulator start-up.
- **1.3 specifically.** Its script runs `StartupII` in the background and
  waits `5 MINS` for it to send `Break 1 C`. Workbench appeared at 117 s
  real time, well under 5 minutes. So the BCPL `Run`, `Execute`, `Wait` and
  `Break` worked, and `LoadWB` (C) reached Workbook. The model shows the same
  in detail (§2.3). The model also shows the one real gap: `C:GetHostVar`,
  an Amiga Forever UAE tool, warns (RC 5) outside UAE.
- **Screenshots** of the booted screens come from the model, which renders
  the same ROM's playfield (`model/*/final.png`, `mark_*.png`). The reviewer
  can compare them with FS-UAE only through the serial log and the probe.
- **A chain boot failed.** The tool's `chain` mode boots a host `DH0:`,
  starts `wbsnap` (a vbcc screen dumper, `tools/s3probe/wbsnap.c`) and then
  executes the floppy's own Startup-Sequence. It produced real FS-UAE
  screenshots of the AROS shell. Once that script ran, though, AROS stopped
  with `Software Failure 8100000C` (memory list corrupted, task `CLI` or
  `input.device`: `fsuae/310-workbench-chain-oracle/serial.txt`). The pure
  floppy boots never did. Not investigated further: the mode is kept for
  screenshots of other experiments and is flagged in its docstring.

## 2. Instruction counts [MODEL]

### 2.1 Method: the floppy in the boot model

`aros_boot_model.py --adf DISK` (S3_PLAN §3.1's machine: flat 16 MB REU
memory, 2 MB chip + 1.75 MB slow detected, trapped $BF/$DF, a synchronous
blitter, a copper once per frame) gains DF0: with a disk
(`tools/s3probe/wb_disk.py`):

- **Drive lines.** CIA-B PRB drives /SEL0, /MTR (latched at select), /STEP,
  DIR and /SIDE. CIA-A PRA returns /RDY (motor on), /TK0, /WPRO (protected)
  and /CHNG (high after the first step with a disk in). The unit-ID read then
  gives DRT_AMIGA for DF0 (`disk_intern_init.c` [SOURCE]).
- **Disk DMA, instant.** The second DSKLEN write with DSKEN|DMAEN writes the
  track under the head into DSKPT: 11 AmigaDOS sectors, MFM-encoded on the
  host from the image (the stream starts after the first sync, as WORDSYNC
  does). Then it raises INTREQ DSKBLK. AROS's own `trackdisk.device`
  decodes it with the CPU and the blitter (`td_decodebuffer_ados`,
  `blitdecodesector`) [SOURCE], so its cost is counted as on an A500. There
  is no rotational or bit-cell timing. The host tests check the encoder
  against a Python port of that decoder, and boot the real ROM from a
  synthetic image through it.
- **HLE trackdisk (`--td-hle`), the smallest hook that removes MFM.** When
  trackdisk.device appears in `SysBase->DeviceList`, its BeginIO (the LVO −30
  `JMP` target) is trapped. A quick `CMD_READ`/`ETD_READ` copies
  `io_Length` bytes of the image at `io_Offset` to `io_Data`, sets
  `io_Actual`/`io_Error` and returns. Everything else takes the normal path
  and is counted. In every boot that was 8 requests (TD_GETGEOMETRY, TD_CHANGESTATE and
  TD_MOTOR twice each, TD_PROTSTATUS, and one non-quick TD_ADDCHANGEINT).
  All 118–295 reads were quick. On Amiga64 the hook is one REU→REU DMA from the staged
  image. The modified AROS would do the same in `td_read` or `BeginIO`.
- **Milestones.** The first disk read. The **boot block**: the first
  instruction whose 64 bytes equal the image's boot code, i.e. the
  `jsr (a0)` in `CallBootBlockCode` (`arch/m68k-all/dosboot/bootcode.c`).
  The **first Startup-Sequence command**: a CLI's `cli_CommandName`,
  debounced, with the previous command's `cli_ReturnCode`. **LoadWB**. The
  **Workbench window**: the first window after LoadWB. **Boot idle**: the
  start of the first 3 virtual seconds after LoadWB with no disk read and
  ≥ 70% STOP. The last is not "fully idle", because Workbook and
  input.device keep the CPU ~20% busy afterwards (§2.4).
- **Attribution.** Instructions are charged per 454-cycle slice to
  `SysBase->ThisTask`, or to `CLIn > command` for a shell. AROS renames a
  running shell to its command, so the CLI number keys it. PCs are mapped to
  ROM modules through the ROM's RomTag ranges. "RAM" means code loaded from
  the disk plus library jump tables. Interrupt code is charged to the
  interrupted task.
- **Unchanged without `--adf`.** The no-disk run still reaches the S3_PLAN
  insert-disk frame at 3,333,413 with 193,692 page changes (`model/nodisk`).

### 2.2 Per phase, unmodified AROS (disk DMA)

| Phase | 3.1 Workbench | 3.1 Install | 1.3 Workbench |
|---|---:|---:|---:|
| (i) reset → first disk read ("insert disk") | 2,753,521 | 2,753,521 | 2,753,521 |
| (ii) → boot block entered | 165,654 | 165,654 | 165,708 |
| (iii) → first Startup-Sequence command | 2,602,878 | 2,455,866 | 3,825,185 |
| Startup-Sequence up to LoadWB | 23,874,626 | 6,054,640 | 15,832,239 |
| LoadWB → Workbench window | 977,823 | 481,457 | 615,502 |
| (iv) → boot idle | 999,523 | 1,145,095 | 1,188,384 |
| **Total** | **31,374,025** | **13,056,233** | **24,380,539** |
| Virtual time to idle (of it STOPped) | 62.7 s (10.2) | 26.0 s (5.7) | 50.0 s (9.2) |
| Track DMAs / blitter words | 176 / 1,040,136 | 67 / 426,196 | 129 / 914,052 |
| 256-byte code-page changes (per instr) | 3,514,498 (0.112) | 1,427,234 (0.109) | 2,834,519 (0.116) |
| Trapped $BF/$DF calls | 274,699 | 110,464 | 220,981 |

(i) is the same for every disk: the ROM's own boot up to dosboot's first
read. Without a disk the same ROM shows "insert disk" at 3.33 M
(S3_PLAN §3.2). With one there is no animation at all.

### 2.3 Major consumers [MODEL]

3.1 Workbench, unmodified, whole boot (per task/command, top):

| Consumer | Instructions | Note |
|---|---:|---|
| trackdisk.device task | 10,464,135 | MFM checksum + blitter decode + CopyMem, per track |
| CLI1 (the shell itself: script parsing, If/Else, argument setup) | 4,687,157 | AROS shell reading an 80-line script from floppy |
| DF0 (afs-handler for the floppy) | 3,399,993 | block cache, directory lookups |
| Exec boot task + pre-ExecBase ROM code | 2,679,517 | ROM checksum 0.56 M, RomTag scan 0.51 M (S3_PLAN §3.2) |
| input.device | 1,304,622 | per-VBL input events over 63 s |
| Workbench (Workbook) | 1,261,505 | the window, icons, title bar |
| LoadResource (the command + its resident segments) | 815,154 | diskfont, iffparse, asl, commodities |
| `C:Version` ×3 | 631,039 | three version checks |
| AddDataTypes | 577,388 | |
| IPrefs (command + its process) | 871,561 | |
| Path, Assign ×14, IF ×n, Copy, Resident, Mount, Execute | 0.22–0.39 M each | |
| SetPatch | 306,996 | the 3.1 SetPatch runs and returns 0 on AROS |

By ROM module (3.1 Workbench, unmodified): exec.library 12.8 M (+1.4 M
after its EndSkip), trackdisk.device 5.6 M, kernel.resource 1.65 M,
afs-handler 1.5 M, RAM 1.4 M, dos.library 1.4 M, timer.device 1.0 M.
Exec's share (41%) is the task switching, signals, lists and CopyMem that
every other consumer calls.

**1.3 check (task 1's question).** In the model, all of the 1.3 script's
commands run in order, and 53 of the disk's `C:` files are BCPL (hunk
classifier `adf.hunk_kind`). The return codes are 0 except `SetPatch` 5,
`GetHostVar` 5 (no UAE host), `Assign AF-Application: EXISTS` 5 (as
intended), `Resident` 1 and `Wait` 10 (interrupted by `Break`, by design).
`LoadWB` starts the process `Workbench` through AROS's `workbench.task`, and
the screen is "Workbook 1.0" with the RAM Disk and Workbench1.3 icons
(`model/134-workbench-dma/final.png`).

### 2.4 After boot

Once idle, AROS runs **105–155 k instructions per virtual second**
[MODEL]. That is about twice the 58 k at the insert-disk screen (S3_PLAN
§1.5). A 41 s run past the Workbench window (`model/310-workbench-min-hle-steady`,
124 k/s) shows where it goes: Workbook 72 k/s (its title bar shows free
memory and is redrawn continuously) and input.device 47 k/s; nothing else
reaches 1.2 k/s. At 48 MHz (~40 k effective IPS with refills) the guest
therefore needs time dilation k ≥ 4 just to keep up. S3_PLAN's k ≈ 11.5
leaves it at ~25% of the CPU [EST].

## 3. Wall clock on Amiga64 [EST]

Per phase and per run: `model/summary.md` (`wb_summary.py`). The formula,
from S3_PLAN's rates, is:

- instructions ÷ 52–68 k IPS (48 MHz, exception mode) or ÷ 1.4–1.5 k IPS
  (1 MHz);
- plus code-page changes × 90 µs (48 MHz) or × 0.40 ms (1 MHz);
- plus blitter words × 3.3 µs or × 73 µs;
- plus trapped calls × 200–400 C64 cycles;
- plus one full VIC refresh per guest second (35 ms / 0.69 s).

Guest STOP time is **excluded**: this assumes idle skip (S3_PLAN §1.5).
Without it, add the STOP seconds × k. With k = 11.5 that is +117 s for 3.1
unmodified, because trackdisk's motor and settle waits are 10 of its 63
virtual seconds. The disk DMA itself is free on the C64 if the host
pre-stages the MFM image in the REU (2.2 MB for 160 tracks) [EST].

| Boot | Instructions | 48 MHz | of it: refills | 1 MHz |
|---|---:|---:|---:|---:|
| (i) reset → first disk read (any disk) | 2.75 M | 54–67 s | 13 s | 32–34 min |
| 3.1 Workbench, unmodified | 31.37 M | **785–928 s (13–15 min)** | 316 s | 6.3–6.7 h |
| 3.1 Install, unmodified | 13.06 M | 323–383 s | 128 s | 2.6–2.8 h |
| 1.3 Workbench, unmodified | 24.38 M | 620–731 s | 255 s | 4.9–5.2 h |
| 3.1 Workbench, HLE trackdisk | 19.83 M | 472–562 s | 178 s | 3.9–4.2 h |
| 3.1 Install, HLE | 8.99 M | 214–255 s | 81 s | 1.8–1.9 h |
| 1.3 Workbench, HLE | 15.88 M | 385–458 s | 150 s | 3.1–3.3 h |
| 3.1 Workbench, trimmed script, unmodified trackdisk | 7.60 M | 184–218 s | 70 s | 1.5–1.6 h |
| **3.1 Workbench, trimmed script + HLE** | **6.01 M** | **139–166 s** | 50 s | 1.2–1.3 h |
| 3.1 Workbench, `LoadWB` only + HLE | 5.42 M | 124–149 s | 44 s | 1.1 h |
| 1.3 Workbench, `LoadWB` only + HLE | 6.38 M | 150–179 s | 55 s | 1.3 h |

Per phase, 3.1 Workbench, unmodified at 48 MHz: (i) 54–67 s; (ii) 4.5–5.2
s; (iii) 67–78 s; the Startup-Sequence 606–715 s; LoadWB → window 24–29 s;
→ idle 30–34 s.

## 4. Candidate modifications and savings

### 4.1 REU-backed trackdisk (AROS change + Amiga64 host op)

Measured by the HLE hook [MODEL]: **−11.5 M instructions (−37%)** on 3.1
Workbench, −4.1 M (−31%) on Install and −8.5 M (−35%) on 1.3. It also
removes −1.0 M blitter words (all of the MFM decode blits), −1.5 M
code-page changes and −29 s of virtual time (the motor, step and settle
waits). At 48 MHz: **−313 to −366 s** on 3.1 Workbench [EST]. The change
fits in `td_read` (arch/m68k-amiga/devs/trackdisk/trackdisk_hw.c): replace
seek + `td_readwritetrack` + `td_decodebuffer` with a request that Amiga64
serves with one REU DMA into `io_Data`. The HLE hook's rules (quick reads
only, everything else native) are a working specification. The track cache
(`tc_*`) then becomes pointless.

### 4.2 Quiet boot (skip the insert-disk animation)

**~0 for a floppy boot** [MODEL]. With a disk in DF0 the first read comes at
2.75 M, before the no-media animation, which starts at 3.33 M only when no
disk is found. It matters only for a boot that waits for a disk: each 0.5 s
animation frame is worth ~30 k instructions plus blits (S3_PLAN §1.5).

### 4.3 Trimmed Startup-Sequence (a local, in-memory copy)

`--startup FILE` replaces `S/Startup-Sequence` in the model's in-memory copy
of the image. Nothing is written, and the tool never saves the patched
image. Two scripts were measured:

- **trim:** `C:MakeDir RAM:T RAM:ENV`, `C:Assign >NIL: ENV: RAM:ENV`,
  `C:Assign >NIL: T: RAM:T`, `C:LoadWB`, `EndCLI >NIL:`;
- **min:** `C:LoadWB`, `EndCLI >NIL:`.

On 3.1 Workbench with HLE, trim takes 6.01 M and min 5.42 M, against 19.83 M
for the full script: **−13.8 M (−70%)**, −333 to −396 s at 48 MHz [EST].
Unmodified trackdisk gives 7.60 M against 31.37 M (−76%). Dropped with it:

- SetPatch: AROS's `setpatch.library` makes it moot;
- the three `Version` checks;
- AddDataTypes, LoadResource, IPrefs (default prefs) and ConClip;
- BindDrivers and Mount;
- 12 of the 14 Assigns;
- the monitor List/Execute;
- the shell's own ~4.7 M of script handling.

What a user loses: prefs from ENVARC:, datatypes, the extra assigns (LOCALE:,
HELP:, KEYMAPS:, …) and a pre-loaded asl/diskfont. On Amiga64 the script
would be a host-side overlay of the staged image, not a modified disk.

### 4.4 ROM boot (phase (i)), AROS changes

2.75 M instructions happen before any disk read. Of these:

- the simulated ROM checksum is 0.56 M (16.9% of the pre-screen boot,
  S3_PLAN §3.2);
- the word-by-word RomTag scan over 1 MB is 0.51 M.

A modified ROM can skip the checksum and use a prebuilt RomTag list. That
saves **~1.07 M, ~16–21 s at 48 MHz** [EST], the same on every boot.

### 4.5 Code-window cache (Amiga64 runtime, not AROS)

The model counts the refills left with an LRU of N 256-byte code windows
[MODEL]:

| Boot | 1 window | 2 | 4 | 8 | 16 |
|---|---:|---:|---:|---:|---:|
| 3.1 WB unmodified | 3,514,498 | 2,170,235 | 1,458,599 | 1,125,197 | 838,720 |
| 3.1 WB HLE | 1,980,395 | 1,483,856 | 978,911 | 732,244 | 554,925 |
| 3.1 WB `LoadWB` only + HLE | 486,535 | 360,668 | 255,973 | 191,020 | 141,012 |

At 90 µs per refill, 4 windows save 185 s on an unmodified 3.1 boot and
8 windows save 214 s. On the recommended configuration they save 21 s and
27 s. The cost is 1 KB–2 KB of C64 RAM and a tag compare on a page change.
That is S3's risk 2, now quantified. The 90 µs refill is still an estimate:
S3.1's single-refill measurement would settle it.

### 4.6 Everything together (3.1 Workbench) [EST]

Start from `LoadWB` only + HLE: 124–149 s. Then:

- ROM checksum and RomTag scan: −16 to −21 s;
- 8 code windows: −27 s.

That leaves **~80–105 s at 48 MHz**, which is about a real A500 booting this
ROM from floppy (FS-UAE: 62–125 s real time). With the trimmed (not
minimal) script, add ~15 s. At 1 MHz the same boot is still ~1 hour.

### 4.7 1.3 on AROS: what would be missing

Nothing blocks booting, as §1 and §2.3 show. What AROS provides for it:

- **BCPL runtime.** `arch/m68k-all/dos/`: `bcpl.S` (2,138 lines: the global
  vector, BCPL call/return, the library stubs), `bcpl_support.c`,
  `bcpl_patches.c`, `bcpl_readargs.c`, `bcpl_putpkt.c`, `bcpl_writef.S`,
  `callglobvec.c`, `callentry.S`. That is ~3.5 k lines in `dos.library`,
  which is 62 KB in the pinned ROM ($F8E748–$F9DBDC) [SOURCE].
- **The ROM workbench task.** `arch/m68k-amiga/wb.tag/wbtag.c` defines
  `workbench.task` with the old jump table and a "KS 1.2/1.3 entry point"
  (mode 2: wait for LoadWB's message, then open workbench.library) [SOURCE].
  It is 194 bytes in ROM, and it hands over to `workbook.resource` (14.5 KB).
- **Private 1.3 entry points.** None was hit on this boot. What remains
  untested: 1.3's `Preferences` (it writes `devs:system-configuration`), and
  the disk's tools, which need input the model does not give.
- **What 1.3 costs relative to 3.1** [MODEL]:
  - more instructions for the same treatment (6.38 M against 5.42 M, with
    `LoadWB` only + HLE);
  - its script opens a CON: window for `Echo` (CON 0.7 M);
  - a second shell and a `Wait` for `StartupII`;
  - the BCPL shell overhead.

  Dropping BCPL from a 3.1-only ROM would save ROM space (~20–30 KB [EST],
  unmeasured), not boot time. The 3.1 Workbench disk holds one BCPL file,
  in `L:`.

## 5. Limits

- The model is not cycle-exact: disk DMA is instant, with no index or
  rotation, and the copper runs once per frame. Its **virtual time** is
  shorter than an A500's, but its **instruction counts** are what the ROM and
  the disk's code execute. S3_PLAN's sensitivity check moved counts by
  ≤ 10% under 0.5–4× CPU/timer ratios.
- The machine is S3's REU machine: 2 MB chip + 1.75 MB slow, no Zorro fast.
  FS-UAE covered 512 KB + 512 KB and the oracle memory. All booted.
- Attribution is per slice (~50–80 instructions), and interrupt code is
  charged to the interrupted task. Module ranges come from RomTags. Code
  after a module's EndSkip is labelled "(after EndSkip)".
- "Boot idle" is a definition (§2.1). Moving the threshold by a second
  changes the totals by ~0.1 M.
- The 48 MHz and 1 MHz rates are S3_PLAN estimates, not new measurements.
- FS-UAE times in warp mode depend on host load. Only the real-time rows are
  timings.

## 6. Reproduce

```sh
E=build/evidence/wb-measure; D="$HOME/Documents/Amiga Disk Images"
PY=~/.cache/amiga64/s3probe-venv/bin/python        # tools/s3probe/setup_venv.sh
python3 tools/s3probe/adf.py "$D/amiga-os-310-workbench.adf"       # names, sizes, hunk kind
# task 1: FS-UAE ground truth (warp; --no-warp for real time)
for d in 310-workbench 310-install 134-workbench; do for m in oracle plain; do
  python3 tools/s3probe/wb_fsuae.py --adf "$D/amiga-os-$d.adf" --mem $m --seconds 90 \
      --out $E/fsuae/$d-floppy-$m; done
  python3 tools/s3probe/wb_fsuae.py --adf "$D/amiga-os-$d.adf" --mem plain --seconds 150 \
      --no-warp --out $E/fsuae/$d-floppy-plain-realtime; done
# task 2: model runs (DMA, HLE, trimmed scripts, no-disk regression)
sh $E/model/run_all.sh        # the script is listed in this section's text below
python3 tools/s3probe/wb_summary.py $E/model/*-dma $E/model/*-hle --json $E/model/summary.json
# host tests (synthetic ADF only; the two model legs skip without ROM cache/venv)
python3 -m pytest -q host/tests/test_wb_disk.py
```

`run_all.sh` runs `aros_boot_model.py --seconds 240 --snap-every 10 --adf
DISK [--td-hle] [--startup trim.txt]` once per configuration in §3, and
without `--adf` for 8 s (`nodisk`). The steady-state run adds `--startup min.txt --td-hle
--stop-idle 40 --seconds 60`. The trim scripts are the texts in §4.3.
Teeth (`build/evidence/wb-measure/teeth/`): each of these mutants fails at
least one test:

- odd/even halves swapped (5 tests fail);
- the clock bit ignoring the previous word;
- the HLE hook without its RTS;
- the step direction inverted.

A track rotated by 5 sectors is not a mutant: AROS decodes it, and only the
test that assumes a fixed position fails.
