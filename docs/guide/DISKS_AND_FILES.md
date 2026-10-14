# Disks and files

## Your Workbench disk

Amiga64 boots **AROS**, the open-source AmigaOS replacement, from the ROM
bundled with it, and then boots **your own Workbench 3.1 floppy image**
(ADF) in drive DF0. AROS supplies its own Workbench program ("Workbook"):
what your disk contributes is its commands, libraries, preferences and
startup script (docs/WB_BOOT.md §0). Your ADF is never bundled, committed
or uploaded anywhere except to your own Ultimate's storage, and only when
you confirm or pass `--yes` (WAVE17 ruling).

**Where it is looked for:** `--adf PATH`, then the path saved in
`~/.config/amiga64/config.toml`, then the single `./adf/*.adf` (with
several there, a numbered choice), then a prompt (only on a terminal).
`amiga64 config` shows which one is in use and why.

**Your file is only read.** Every change the session makes (the trimmed
startup script, the music player, your added files) is made on a copy in
memory and then in the REU. Nothing writes back to your ADF, and the
emulated drive is write-protected for the Amiga too.

### ADF support state

| Item | State | Source |
|---|---|---|
| Image size | **DD only**: exactly 901,120 bytes (80 cylinders × 2 heads × 11 sectors × 512) | `host/adf_overlay.Adf` |
| HD images (1,802,240 bytes), compressed images (ADZ, DMS), hard-disk images (HDF) | refused / not supported | the same check |
| File system | `DOS\0`-`DOS\3`: OFS and FFS, with or without international mode | `Adf`: "only OFS/FFS (DOS\0-\3) are supported" |
| `DOS\4` and later (directory cache, long names) | refused | the same |
| Non-DOS disks (games with their own boot block) | refused: the boot block must start with `DOS` | the same |
| A broken disk | refused: root block 880 type and checksum are checked; to add files the bitmap must be valid; every change is checked with a full `fsck` | `Adf`, `adf_files.DiskEdit`, `fsck` |
| The guest writing to the disk | refused: the drive is write-protected, quick writes return `TDERR_WriteProt` | [docs/emulation/STORAGE.md](../emulation/STORAGE.md) |
| Startup-Sequence | replaced on the copy by a **trimmed script** by default: `MakeDir RAM:T RAM:ENV`, two `Assign`s, `LoadWB`, `EndCLI`. The disk's own script is far slower (372 s against 130.5 s to the Workbench on the C64U) | `host/adf_overlay.SCRIPTS`, docs/HARDWARE.md |
| What the trimmed boot loses | prefs from `ENVARC:`, datatypes, most assigns, `SetPatch`, `IPrefs` | docs/WB_BOOT.md §4.3 |
| `amigaguide.datatype` | removed **from the session copy** to make room for ModPlayer; never from your file. A disk without `Classes/DataTypes/amigaguide.datatype` is refused for a session ("… use a Workbench 3.1 disk"); `adf add --out` does not need it | `host/adf_files.MODWB_DROP`, `amiga64/adf.check` |
| Drives | DF0 only; no disk change while running | STORAGE.md |
| **Tested** | the user's own Workbench 3.1 ADF (an Amiga Forever edition), booted to the Workbench on a U64E at 48 MHz, a C64U at 64 MHz and VICE | docs/HARDWARE.md "Workbench 3.1 from the ADF" (devices); `w16-d/build/evidence/w16-d/PROGRESS.md` (VICE, boot to the Workbench in 306 s of warp) |
| Other Workbench 3.1 images | **untested, may work**: `doctor` and `run` say "tested" only for images whose SHA-256 is on the tested list | WAVE17 ruling |
| Workbench 3.1 Install, Workbench 1.3 | booted to a Workbench in the boot model and in FS-UAE on the same AROS ROM, **never on Amiga64** | docs/WB_BOOT.md §0, §4.7 |
| Other disks (tools, games) | untested; anything that needs disk DMA, custom track formats or a non-DOS boot block cannot work | STORAGE.md |

### How the disk gets to the device

The Ultimate loads the disk into the REU itself, from its own storage, in
about 0.4 s (U64E) or 1.3 s (C64U). So the first `amiga64 run` copies your
ADF to the device as `<volume>/amiga64/wb-<12 hex digits>.adf`, after
asking once; the 12 digits are the start of the image's SHA-256, so
different images get different names. If the device already holds the
same image (for example as `amiga-os-310-workbench.adf`, the name the test
tools use) it is reused and nothing is uploaded. It never overwrites,
with one exception: if `wb-<12 hex digits>.adf` is already there with
other bytes (an interrupted copy), `doctor` warns and `run` asks before it
replaces that one file ("Replace it? [y/N]", or `--replace-adf`;
`amiga64/device.py` `ensure_adf`). The changed blocks (script, player,
your files) are then written into the REU copy over the network.

## Getting files into the emulated Amiga

There is no live connection between your computer's files and a running
Workbench. Files go onto the disk **before** the boot, or, for the
developers' program mode, into the emulator's own file stores.

### Files-in support state

| Path | What it is | Limits | State |
|---|---|---|---|
| **`amiga64 adf add PATH... [--to DRAWER] [--as NAME]`** | host files and folders (folders become drawers, recursively; dotfiles are skipped and listed) are checked against the limits at once and staged; every later `run` adds them to the **session copy** of the disk and prints what it applied. `adf rm DISKPATH` and `adf clear` change the staged list | the free blocks of the session disk after the player's files: 42 blocks, about 21 KB, on the tested disk; names 1-30 Latin-1 characters without `:` or `/`; an existing name is refused (no overwrite); the target drawer must exist; symbolic links and special files are refused; your ADF is never changed | new in Wave 17; the disk editor under it (`host/adf_files.DiskEdit`) is tested on synthetic OFS and FFS images (`host/tests/test_adf_files.py`, plus a check on the user's disk when it is present) and was used for the ModPlayer files on the real disk |
| **`amiga64 adf add --out NEW.adf`** | your disk plus the files, written to a **new local ADF** you can keep (without ModPlayer and without the script overlay) | as above, but the free blocks are your original disk's: 3 blocks, about 1.5 KB, on a stock Workbench 3.1 disk; `NEW.adf` must not exist and its folder must exist | new in Wave 17 |
| `amiga64 adf ls [DRAWER] [--original]` | lists the session disk (or your original): names, sizes, drawers, `(staged)` marks, then the free blocks and bytes | read-only | new in Wave 17 |
| **Deleting** | only `Classes/DataTypes/amigaguide.datatype`, only from the session copy, only to make room for ModPlayer | amiga64 never deletes your files to make room | ruled (WAVE16 addendum, WAVE17 rulings) |
| Saving from inside the Amiga | not possible: the drive is write-protected | | by design |
| `RAM:` inside AROS | works as on an Amiga (the trimmed script creates `RAM:T` and `RAM:ENV`), but starts empty and is lost when the session ends unless you `run --save` | the guest's RAM | AROS behaviour |
| A saved session (`run --save`) | carries the disk copy and RAM as they were | resumes only on the same build and ROM | the Workbench snapshot was tested on both devices (Wave 13); `run --save` with the ModPlayer disk and added files is new in Wave 17 |
| Program mode: the HLE RAM disk | for developers: a Hunk program run without AROS gets a 2 MB RAM disk the host can fill before the run and read after (`host/hle_dos_env.ramdisk_chunks`) | 32 files, names ≤ 31 characters, 8 open handles; not visible to AROS or Workbench | tested in VICE and on the U64E |
| Program mode: ULTDOS | for developers: such a program's files on the Ultimate's storage, through the Command Interface (opt-in key `"UCI!"`) | 2 open files, about 2.7-5 KB/s at 48 MHz; not visible to AROS | validated on the U64E |
| Program mode: the program itself | `tests/run_program.py --hunk FILE` stages any Hunk executable at `$010000` and runs it with the HLE | the HLE's library subset ([docs/emulation/HLE.md](../emulation/HLE.md)) | tested |
| Serial, network, drag and drop into a running Workbench | **none** | | not planned |

What the limits mean in practice:

- A file takes one header block plus its data: 488 bytes per block on an
  OFS disk, 512 on FFS. The header holds the first 72 data-block
  pointers; each further 72 need one extension block
  (`host/adf_files.DiskEdit.add_file`). A full 880 KB floppy has room for
  only a little; `adf ls` shows the free blocks.
- New blocks are taken from block 881 upwards, then below 880.
- Added files get a fixed date, 2026-10-12 (`adf_files.DATE`), and
  protection 0, which on AmigaDOS means read, write, execute and delete
  all allowed (`DiskEdit.add_file`).

Sources: `amiga64/adf.py` (`check`, `add`, `write_out`), `amiga64/device.py`
(`adf_candidates`, `ensure_adf`), `host/adf_overlay.py`, `host/adf_files.py`,
`tests/aros_wb.py` (`stage_disk`, `make_disk`), CONTRACTS "HLE
trackdisk and DF0", docs/HLE_DOS.md, docs/WB_BOOT.md, docs/HARDWARE.md,
docs/WAVE16_BRIEF.md addendum, docs/WAVE17_BRIEF.md rulings, W17-K's M0
(`w17-k/build/evidence/w17-k/PROGRESS.md` (5)).
