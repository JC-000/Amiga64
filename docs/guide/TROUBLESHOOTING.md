# Troubleshooting

Start with `amiga64 doctor`. It checks your computer's side, the ROM,
your disk and the device, prints one `OK`/`WARN`/`FAIL` line per check
with a plain sentence, and exits with status 1 on any FAIL
([QUICKSTART.md](QUICKSTART.md) step 3 lists the checks). `amiga64 config`
shows every setting and where it came from. This page explains the problems seen during development and
what the underlying messages mean.

## The device

**The command waits before anything happens.** Every session takes the
device through the harness's device lock. If another program (another
`amiga64`, a test, another project) holds the device, the session waits
for it. Sessions in the development logs waited from 0 s up to about
30 minutes behind another project's work (docs/HARDWARE.md "Wave 5:
run_dos on hardware": 1751 s).

**"the Ultimate at … is busy … try again later".** The device did not
become free within 300 s. Wait for the other program to finish.

**"Ultimate DMA Service is off".** The disk is staged through the
device's DMA service, so `doctor` reports a FAIL when it is off. Turn on
`Network Settings / Ultimate DMA Service`; the C64 Ultimate switches it off
at every power-on, so check it after each one.

**"FTP file service off".** The ADF upload and the device checks use the
device's FTP file service; turn on `FTP File Service` in the network
settings.

**"/Temp" warning.** Each program start leaves a firmware file in the
device's `/Temp` (one per `run`, two with sound; two or three per
`resume`), and the harness deletes the older ones under its lock, keeping
the newest. So 0 or 1 file is normal. Two or more mean the clean-up is
failing; a full `/Temp` can crash a C64 Ultimate, and a power cycle
clears it.

**The device is not reachable.** Check the address (`--host`,
`AMIGA64_HOST`, `U64_HOST`, the config file; [DEVICES.md](DEVICES.md)) and
that the Ultimate's network interface and its REST service are on. If the
device has a network password, set `U64_PASSWORD`.

**The device stopped responding during a session.** Do not open the
Ultimate's on-screen menu while a session runs: on the C64U that wedged
the device until a hard power cycle, and the session could not restore the
settings (docs/HARDWARE.md "Wave 14 hardware findings"). After a power
cycle, check the device settings by hand.

**After a power cycle the C64U behaves differently.** Once, for part of a
day, the C64U ran on firmware defaults instead of its saved settings (SID
sockets disabled, network services disabled, Paddle Override enabled); a
further power cycle loaded the saved settings again. The session restores
only what it changed, so it passes such a state through unchanged (same
section).

**A session ends and the settings look changed.** The session restores
every setting it snapshotted and compares them; its report says
IDENTICAL when everything matched. If it does not, the report lists the
differences. Every session (doctor, run, resume) keeps its log and report
in `~/.local/state/amiga64/holds/<date-time>-<doctor|probe|run|resume>/`
(`config_before.json`, `config_after.json`, `session.log`,
`session.json`); `$XDG_STATE_HOME` or `AMIGA64_STATE_DIR` moves it.

## Storage

**No volume found / the folder cannot be created.** The U64E uses a USB
stick at `/USB1`, the C64U an SD card at `/SD`. On the C64U, firmware 1.1.0
lists an SD card it cannot mount as an empty volume, and `mkdir` fails;
unlocking the card and reformatting it as MBR + FAT32 fixed it (which of
the two did is not known) (docs/HARDWARE.md "Device
storage legs on the U64E", "C64U file legs"). `/Flash` and `/Temp` are
never used.

**The ADF on the device differs from yours** (`doctor`: a FAIL on "ADF on
the device"). The device copy's name comes from your image
(`wb-<12 hex digits of its SHA-256>.adf`), so a mismatch means that file
on the device is damaged or was replaced. amiga64 never overwrites it:
move it away on the device yourself (outside a session) and run again.

## The ROM

The AROS ROM is bundled and checked against pinned SHA-256 hashes every
time (`tools/aros/manifest.txt`). A file that does not match is never
used: `host/machine.find_rom` then tries the developer cache
(`~/.cache/amiga64/aros/`, from `tools/aros/fetch.sh`) and fails if that
does not verify either. Reinstall amiga64 if it reports a mismatch.

## Your ADF

The disk checks and their messages (`host/adf_overlay.py`,
`host/adf_files.py`):

| Message | Meaning |
|---|---|
| `N bytes, not a DD ADF (901120)` | not an 880 KB floppy image (HD, compressed or truncated) |
| `no DOS boot block` | a non-DOS disk (for example a game) |
| `dostype DOS\N: only OFS/FFS (DOS\0-\3) are supported` | a directory-cache or long-name disk |
| `root block 880: bad type or checksum` | a damaged image |
| `root: bitmap not valid` | the disk's block bitmap is marked invalid; files cannot be added |
| `S/Startup-Sequence: not on the disk` | the startup script cannot be replaced: not a Workbench disk |
| `disk full: N blocks wanted, M free` | not enough room on the session copy |
| `…: exists` | a file or drawer with that name is already on the disk (no overwrite) |
| `…: bad name` / `not a Latin-1 name` | 1-30 characters, Latin-1, no `:` or `/` |
| `…: no such drawer` | the target drawer does not exist |

"Untested, may work" from `doctor` only means the image's SHA-256 is not
on the tested list ([DISKS_AND_FILES.md](DISKS_AND_FILES.md)).

## Booting

**It takes a long time.** About 135 s (C64U) to 180 s (U64E) to the Workbench is normal
([DEVICES.md](DEVICES.md)); the screens before it are AROS's.

**The pointer moves on its own, and the boot is slow.** A joystick with a
switch stuck closed moves the mouse every frame. On the C64U in Wave 13
port 2 read Up held, and a boot with the real input reached the
insert-disk screen in 126 s, against 69.3 s for the build before input
existed (docs/HARDWARE.md "Joystick
port 2 and the keyboard matrix"). Unplug the stick, or mask the stuck
line with `--stuck MASK` (Up = `0x01`).

**The 1351 pointer does not move.** The mouse must be switched on for the
session with `--mouse-port 1` or `2` (or `mouse_port` in the config);
the default 0 means joystick only.

## Using Workbench

| Symptom | Why | What to do |
|---|---|---|
| text is unreadable | hires text is halved horizontally in the fitted view | press **F1** for the 1:1 view |
| the bottom of the screen is missing | the fitted view shows the top 200 lines | F1, then move the pointer down: the window follows it |
| colours look wrong | 16 VIC colours; pens are kept distinct, but a pen can change hue | expected |
| a keypress does nothing | only F1 and C= are read; there is no Amiga keyboard | expected |
| the screen does not update after a program draws | CPU drawing is not tracked; the picture refreshes on display-register changes and blits | expected; the next such change redraws it |
| everything is slow | the 68000 runs at well under 1 MHz equivalent | expected |

## Sound

- **VICE is always silent**; sound needs a real Ultimate.
- With sound the session maps the Ultimate's sampler and checks that it
  answers. If that check fails, the session stops with a message naming
  the sampler window (`INFRA: the sampler window does not read $00 $10`);
  run with `--no-audio`.
- A song in progress when you `--save` resumes silent until the player
  starts its next note (PAULA known limit).
- On the C64U the pitch is about 2 cents sharp at the highest notes
  (docs/HARDWARE.md "The C64U's sampler").

## ModPlayer

The player shows a requester and returns to Workbench without taking over
when something is missing (its messages, `fixtures/modwb/modwb.s`):

| Requester text | Meaning |
|---|---|
| "The MOD file did not open" | `ModPlayer.mod` (or the project you opened it with) is not there |
| "The MOD is too small or too big, or chip RAM is short" | the player loads at most 1 MB |
| "The file is not a 4-channel M.K. MOD" | only 4-channel `M.K.` modules play |
| "A CIA timer the player needs is in use." | another program owns a CIA timer the player claims |
| "The MOD player is already running." | a second launch while one plays |

## Saved sessions

`amiga64 resume` refuses a saved session that does not fit the current
setup. The underlying codes (`host/snapfmt.ST_ERR`; CONTRACTS
"Suspend/resume"):

| Code | Meaning (`amiga64`'s own words where it has them) |
|---|---|
| `$E3` | "it was saved by a different amiga64 build" (after an upgrade): run again |
| `$E4` | "it was saved with a different AROS ROM" |
| `$E1` | "it is not an amiga64 snapshot (or an older format)" |
| `$E6` | "there is no saved session on the device (amiga64 run --save makes one)" |
| `$E2`, `$E5` | the file is damaged (a checksum failed) |
| `$E7` | the transfer from the device's storage failed |
| `$E8` | this build cannot resume at all (unstamped; a developer build) |
| `$E9` | layout mismatch (a developer build) |
| `$EA` | no ROM id declared (a tool error) |
| halt `$57` after the resume starts | the disk module differs from the one in the snapshot |

## Reporting a problem

Include the `amiga64 doctor` output, the device model and firmware it
reports, the session's folder under `~/.local/state/amiga64/holds/`,
and what was on the screen. Never attach your ADF or a saved session file:
both contain your Workbench disk.

Sources: `host/adf_overlay.py`, `host/adf_files.py`, `host/machine.py`,
`host/snapfmt.py`, `tests/aros_wb.py` (`map_sampler`, `session`),
`tests/ureu_probe.ensure_adf`, `fixtures/modwb/modwb.s`, docs/HARDWARE.md,
CONTRACTS "Suspend/resume", W17-K's draft refusal messages
(`w17-k/amiga64/session.py` `SNAP_REFUSALS`).
