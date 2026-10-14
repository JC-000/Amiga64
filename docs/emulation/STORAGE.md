# Storage: the disk, the Ultimate's files and snapshots

Three mechanisms move bulk data between the Ultimate's storage, the REU
and the guest:

1. **The HLE trackdisk** serves AROS's floppy reads from an ADF image kept
   in the REU (`runtime/ops/td.s` state, `tests/aros/td.s` code).
2. **UCI REU I/O** loads a file from the Ultimate's storage straight into
   the REU, or saves an REU range to a file (`runtime/ops/ureu.s`, using the
   Ultimate Command Interface at `$DF1C`).
3. **Snapshots** suspend the whole machine into the REU and optionally to
   a file, and resume it later (`runtime/ops/snap.s`, `host/snapfmt.py`).

How a user gets a disk and files in is in
[docs/guide/DISKS_AND_FILES.md](../guide/DISKS_AND_FILES.md). The HLE
dos.library of program mode (RAM disk, ULTDOS) is in [HLE.md](HLE.md).

## The HLE trackdisk (DF0)

Binding text: CONTRACTS "HLE trackdisk and DF0 (W13-W)".

Real AROS reads a floppy through `trackdisk.device`, which decodes MFM
from disk DMA. The boot model measured that decode at 10.5 M of an
unmodified Workbench 3.1 boot's 31.4 M instructions (docs/WB_BOOT.md §0),
and the emulator has no disk DMA at all. So reads are served by the host
side of the emulator instead:

- **The image** sits in the REU at `$200000-$2DBFFF` (`TD_ADF`, block n at
  `TD_ADF + 512n`), right after chip RAM. No guest instruction touches that
  region; AROS never treats it as RAM.
- **The patch.** Once per frame the td module's frame hook walks
  `SysBase->DeviceList` (at most 32 nodes per frame, only through chip and
  slow RAM, with names allowed in the ROM banks) until it finds
  `trackdisk.device`. It checks that the device's BeginIO slot (LVO −30) is
  a `JMP abs.l`, keeps the target, and rewrites the slot to
  `move.b d0,$BFD100`: a CIA-B port B write of the same six bytes.
- **The trap.** When that CIA write arrives from the patched slot, nothing
  is written to the CIA; the td module serves the IORequest at A1:
  - quick `CMD_READ` / `ETD_READ`: `io_Length` bytes of the image at
    `io_Offset` are copied to `io_Data`, `io_Actual` = `io_Length`,
    `io_Error` = 0, and the device returns;
  - quick `CMD_WRITE`, `TD_FORMAT`, `TD_RAWWRITE` (and their `ETD_` forms):
    `io_Error` = 28 (TDERR_WriteProt);
  - an offset or length beyond the image, or either value at 16 MB or
    more: `io_Error` = 20, nothing copied; a destination chunk in bank
    `$BF` or `$DF` also ends the request with 20 (the chunks before it
    were copied);
  - anything else (non-quick requests, motor, change state, protection
    status, geometry, change interrupts): passed to AROS's own trackdisk
    code, which sees a write-protected drive DF0 with a disk present.
- **The copy** goes REU → a 256-byte C64 buffer → REU, in chunks that never
  cross a 256-byte page on either side (so never a 512 KB line), invalidates
  the code windows of each destination page, and refuses destinations in
  the I/O banks. Cost: 3775 cycles for a 512-byte read, measured on VICE
  (59 µs at 64 MHz, CONTRACTS "Cost").
- **The drive lines.** `td_prb`, hooked into CIA-B port B writes, models
  DF0's select, motor, step and direction lines, and drives CIA-A PRA's
  /RDY, /TK0, /WPRO (always low: write-protected) and /CHNG pins.

What was observed on the Workbench boot with the trimmed script: 55 quick
reads served, 7-9 requests passed to AROS, no write, no disk DMA
(CONTRACTS "The device").

**Limits:**

- A **non-quick read** would go to AROS's native path, which needs disk
  DMA and would never complete. None was observed (every read is a
  `DoIO`); `td_nqr` counts them and `aros_wb` fails if it is not 0.
- Only **quick** writes are refused with TDERR_WriteProt. Non-quick writes
  go to the native path, and nothing is proven about what it does with them;
  none was observed (the filesystem sees the write protection and does not
  write).
- **DF0 only.** No other drive, no disk change while running.
- The module is armed only when the host staged a disk (`td_on` = 1);
  otherwise every hook returns at once and the CIA-A pins stay `$EB`.

Tests: `host/tests/test_td.py` (the module on an in-process 6502 and REC
model, including the 512 KB wrap and 10 mutants), `tests/aros_wb.py` legs
`w`, `nohle` (red: never patched) and `resume`.

## Staging the ADF

`tests/aros_wb.stage_disk` (CONTRACTS "The disk"):

| Backend | How the 901,120 bytes get into the REU |
|---|---|
| VICE | the patched image (overlay applied) through the runner's `test_load_chunk` |
| U64E / C64U | the runner's `ureu_ld`: one UCI `LOAD_REU` of `<vol>/amiga64/<name>.adf` from the device's own storage; then the overlay's changed blocks with the harness's REU write |

Both end with a C64-side checksum of the whole image (`aros_sum`) against
the host's image. Measured LOAD_REU times for the ADF: 0.40 s on the U64E
at 48 MHz (`/USB1`), 1.26 s on the C64U at 64 MHz (`/SD`) (docs/HARDWARE.md
"Device storage legs").

The file must already be on the device. `tests/aros_wb.stage_disk` loads
`<vol>/amiga64/<disk.adf_name>`; the test tools use the default name
`amiga-os-310-workbench.adf` (`tests/aros_wb.ADF_NAME`), uploaded once by
`tests/ureu_probe.py`'s `ensure_adf`: upload without overwrite, and if a
file is already there, compare its SHA-256 with the local one and stop on
a difference. The Wave 17 CLI has its own `ensure_adf`
(`amiga64/device.py`) with the same rules. It first looks for the user's
image on the device under `wb-<the first 12 hex digits of its
SHA-256>.adf` and then under the tests' name, and reuses either one when
its SHA-256 matches (`adf_candidates`). Otherwise it uploads the image
as `wb-<12 hex digits>.adf` (supervisor ruling). It overwrites only a
file under that name whose bytes differ (an interrupted copy, say), and
only after asking
(`run --replace-adf`)
([docs/guide/DISKS_AND_FILES.md](../guide/DISKS_AND_FILES.md)).

## The overlay: changes on the REU copy only

The user's ADF file is never written. Everything that changes the disk is
an **overlay**: a list of 512-byte blocks applied to the in-memory copy and
then to the REU copy (`host/adf_overlay.py`, `host/adf_files.py`;
CONTRACTS "The overlay").

- **Startup-Sequence.** `make_overlay` rewrites `S/Startup-Sequence` in the
  file's own data blocks. Scripts: `trim` (the default: MakeDir RAM:T
  RAM:ENV, two Assigns, LoadWB, EndCLI), `min` (LoadWB, EndCLI), `full`
  (no overlay: the disk's own script). With the full Workbench 3.1 script
  the boot to Workbench took 372 s against 130.5 s with `trim` on the C64U
  (docs/HARDWARE.md "Workbench 3.1 from the ADF"). What `trim` drops:
  docs/WB_BOOT.md §4.3.
- **Files and drawers.** `DiskEdit` (`host/adf_files.py`) adds files
  (`add_file`), drawers (`mkdir`) and deletes (`delete`) on the copy:
  blocks are taken from the bitmap lowest-first from block 881 upwards,
  then below 880; headers, extension blocks and data blocks (OFS with
  their 24-byte headers, FFS raw) are written and checksummed; the new
  header is linked into its hash chain. An existing name is refused, and
  only an empty drawer can be deleted.
- **Checking.** Every overlay is validated before staging: block numbers
  2-1759 and 512 bytes; for a file overlay, `fsck` of the whole applied
  image (every header, chain, data and extension block, no block used
  twice, the bitmap exactly the complement of the used set).
- **Formats.** Only DD images (901,120 bytes) with `DOS\0`-`DOS\3` (OFS or
  FFS, international or not). `DOS\4` and later (directory cache, long
  names) and anything else are refused. Name hashing and case folding
  follow the international rule on intl disks.
- **The ModPlayer disk** (`--disk modwb`): `Classes/DataTypes/amigaguide.datatype`
  is deleted from the copy to make room (102 blocks; "the trim boot never
  reads it", `host/adf_files.py`, and the `modwb` leg of
  `tests/aros_modwb.py` fails if its drawer's header block is read), then
  `ModPlayer`, `ModPlayer.info` and `ModPlayer.mod` are added to the root
  (`modwb_image`).

## UCI REU I/O

`ureu.s` sends the Ultimate DOS commands `LOAD_REU` (`$21`) and
`SAVE_REU` (`$22`): open a file on DOS target 1, transfer between the file
and an REU range, close (CONTRACTS "UCI REU I/O"):

- at most 256 KB per command, so each reply comes inside the poll budget;
  a running guest is paused for the whole transfer;
- **the reply's byte count is the truth**: every chunk must answer status
  `00` and exactly the expected count text (the firmware's `%6x`: six
  upper-case hex digits). `ureu_err`: 5 = a wrong or short count,
  3 = a status other than `00`, 2 = no reply, 1 = open refused, 6 = close
  refused;
- after every load, the code windows are dropped and the pixel epoch is
  bumped, so no stale code or picture survives;
- the Command Interface must be enabled; both devices keep it disabled in
  their saved settings, so sessions enable it inside the device hold,
  which restores it;
- volumes: `/USB1` on the U64E and `/SD` on the C64U by default; the CLI
  takes the first other writable volume when the default is missing, and
  `--vol` picks one (`amiga64/device.choose_volume`). Never `/Flash` or
  `/Temp` (the tools refuse them).

## Snapshots: suspend and resume

Binding text: CONTRACTS "Suspend/resume (W12-T)" with its state inventory.

**Suspend.** The host sets a request bit; at the next frame hook (an
instruction boundary, within one guest frame) `snap.s` copies to the
snapshot area in the REU under the trapped `$DF` bank (`$DF1000-$DF3FFF`):
a header, the zero page, the display-state page and the whole MAIN BSS
image (every module's state, since `snap.s`/`ureu.s` link last). Guest
memory needs no copy: it is already in the REU. With the file bit the
spans are also written to a file with `SAVE_REU`: the header, the state,
then chip RAM with the disk (`$000000-$2DBFFF`), slow RAM to `$DEFFFF`,
and both ROM banks (`host/snapfmt.RANGES`), about 6.1 MB. Without the
continue bit the guest freezes; with it the guest runs on.

**Resume** (`snap_resume`, on a freshly loaded runner) checks everything
before it restores anything, and refuses with a code in `snap_st`
(`host/snapfmt.ST_ERR`):

| Code | Refused because |
|---|---|
| `$E1` | magic or version (e.g. a version-1 file) |
| `$E2` | header checksum |
| `$E3` | build ID: the snapshot was taken by a different runner build |
| `$E4` | ROM id: a different ROM was declared |
| `$E5` | state checksum |
| `$E6` / `$E7` | the file did not open / a transfer failed |
| `$E8` | this runner is unstamped (build ID 0) |
| `$E9` | layout (BSS address, sizes, spans) |
| `$EA` | no ROM id declared |

- The **build ID** is the first 8 bytes of SHA-256 of the PRG with those
  bytes zeroed, stamped by the Makefile after the link. Any rebuild that
  changes a byte gives a new ID, so a snapshot only resumes on the exact
  runner that took it.
- The **ROM id** is a 32-byte tag the host declares before the boot and
  before the resume (the tools use SHA-256 of `aros-ext.bin || aros-rom.bin`,
  `tests/aros_resume.rom_id`). The C64 compares tags; it never hashes the
  ROM.
- The **td module ID** is in the image too. A resume onto a different td
  module build halts the machine at the first frame hook with `st_reason`
  `$57` (`TD_HALT_MOD`), measured on both devices (docs/HARDWARE.md
  "Workbench 3.1 from the ADF").
- A refused resume leaves the fresh runner's state untouched (a scratch
  page is saved and put back), so the host can fall back to a fresh boot
  on the same load.

**What a launcher must do after a resume** (the modules are not in the
image): re-stage td, spr and both inpm segments (`WbMachine.load_driver` on the fresh
load), re-arm the pointer and the 1351 module (they come up disarmed),
write `inp_src` = 0 for real input, and with audio, stage the audio module
last and re-arm it once the guest runs (`tests/aros_wb.resume_workbench`,
`_resume`). A REU-resident snapshot (no file) is consumed by a successful
resume, and by a suspend that lets the guest continue, so it can never be
resumed over guest memory that moved on; a file keeps its own copy.

**Measured** (docs/HARDWARE.md "Workbench 3.1 from the ADF", file
`wb-snap-test.bin`): suspend with the file 19.1-19.8 s on the C64U (SD
writes), 3.4 s on the U64E; resume including LOAD_REU
6.55-6.64 s on the C64U and 1.34-1.36 s on the U64E; the same Workbench
picture again 9.4-9.8 s (C64U) and 3.8 s (U64E) after the launch.

**Limits:**

- A guest suspended while STOPped resumes running (one spurious wake-up;
  AROS's idle loop STOPs again).
- A channel playing at the suspend resumes silent until the guest's next
  DMA start ([PAULA.md](PAULA.md)).
- The snapshot needs the REU at 16 MB.
- The app anchor at `$2DC040` is outside the file spans; a program that
  relies on it publishes it again after a resume.

Sources: CONTRACTS "HLE trackdisk and DF0", "UCI REU I/O", "Suspend/resume";
`runtime/ops/td.s`, `tests/aros/td.s`, `runtime/ops/ureu.s`, `snap.s`;
`host/adf_overlay.py`, `host/adf_files.py`, `host/snapfmt.py`;
`tests/aros_wb.py` (`stage_disk`, `make_disk`, `_suspend`, `_resume`,
`resume_workbench`), `tests/ureu_probe.ensure_adf`; docs/HARDWARE.md
sections named above; docs/WB_BOOT.md.
