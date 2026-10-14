# Troubleshooting

Start with `amiga64 doctor`. It checks your computer's side, the ROM,
your disk and the device, prints one `OK`/`WARN`/`FAIL` line per check
with a plain sentence, and exits with status 1 on any FAIL
([QUICKSTART.md](QUICKSTART.md) step 3 lists the checks). `amiga64 config`
shows every setting and where it came from. This page explains the
problems seen during development and what the messages mean.

**"amiga64: unexpected error: … (details: …/last-error.txt)".** Anything
amiga64 does not have a plain message for is reported on one line, and
the full traceback is written to `last-error.txt` in the state directory
(`~/.local/state/amiga64/`, or `$XDG_STATE_HOME/amiga64/` or
`AMIGA64_STATE_DIR`). Attach that file when you report the problem. A
Ctrl-C prints "amiga64: interrupted" and exits with status 130.

## The device

**The command waits before anything happens.** Every session takes the
device through the harness's device lock. If another program (another
`amiga64`, a test, another project) holds the device, the session waits
for it. Sessions in the development logs waited from 0 s up to about
30 minutes behind another project's work (docs/HARDWARE.md "Wave 5:
run_dos on hardware": 1751 s).

**"the Ultimate at … stayed busy (…); try again later".** The wait
behind another program is extended for as long as that program keeps
making progress; this message means the holder stopped making progress
for 300 s, or the device changed hands too often while amiga64 waited
([DEVICES.md](DEVICES.md#what-a-session-changes-on-the-device-and-puts-back)).
Wait for the other program to finish, or find out why it hangs.

**Another tool reset or used the device during a session.** Tools queue
behind each other only when they lock the device under the same key. Look
at `doctor`'s `device lock` line: an address key means that a tool which
reaches the device by another address does not wait
([DEVICES.md](DEVICES.md#other-harness-tools-on-the-same-device)).

**"Ultimate DMA Service is off".** The disk is staged through the
device's DMA service, so `doctor` reports a FAIL when it is off. Turn on
`Network Settings / Ultimate DMA Service`; the C64 Ultimate switches it off
at every power-on, so check it after each one.

**"… has its FTP File Service off".** The ADF upload and the device
checks use the device's FTP file service; turn on `Network Settings / FTP
File Service`. `doctor` then stops at that single `FAIL device` line. If
the setting is not found at all, the `FTP service` line is a WARN,
"unknown".

**"/Temp" warning.** Each program start creates a firmware file in the
device's `/Temp` (one per `run`, two with sound; two or three per
`resume`). The U64E's 3.15 firmware deletes them itself; on the C64U
(1.1.0) the harness deletes the older ones under its lock, keeping the
newest. So 0 or 1 file is normal. Two or more mean the clean-up is
failing; a full `/Temp` can crash a C64 Ultimate, and a power cycle
clears it.

**The device is not reachable.** Check the address (`--host`,
`AMIGA64_HOST`, `U64_HOST`, the config file; [DEVICES.md](DEVICES.md)) and
that the Ultimate's network interface and its REST service are on. If the
device has a network password, set `U64_PASSWORD`. The harness checks
that the device is alive with `ping` first: amiga64 adds `/sbin` and
`/usr/sbin` to `PATH` so that `ping` is found, but on a network that
filters ICMP a reachable device can still be reported unreachable
(c64-test-harness issue #536; [Open items](../emulation/LIMITS.md#open-items-carrying-it-forward)).

**"N MHz (…): this device offers …; give --mhz with one of them".** The
session speed (`--mhz`, the config's `mhz`, or the default 48 MHz on a
U64E and 64 MHz on a C64U) is not one of the device's CPU Speed choices.
This also happens with `--kind C64U` on a U64E. A config value that is
not a number is refused as "mhz '…' in …: not a number" (the same for
`mouse_port`).

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
differences. Every device hold keeps its report in
`~/.local/state/amiga64/holds/<date-time>-<doctor|probe|run|resume>/`
(`config_before.json`, `config_after.json`); the `run` and `resume`
folders also hold the session's `session.log` and `session.json` (the
`probe` folder is the short check before a `run` or `resume`).
`$XDG_STATE_HOME` or `AMIGA64_STATE_DIR` moves it. `session.json`'s
`ended_by` is `ctrl-c`, `time` (`--minutes`) or `interrupted` (a Ctrl-C
before the hand-over, or a SIGTERM; `amiga64` then exits with status 130).

## Storage

**No volume found / the folder cannot be created.** The U64E uses a USB
stick at `/USB1`, the C64U an SD card at `/SD`. On the C64U, firmware 1.1.0
lists an SD card it cannot mount as an empty volume, and `mkdir` fails;
unlocking the card and reformatting it as MBR + FAT32 fixed it (which of
the two did is not known) (docs/HARDWARE.md "Device
storage legs on the U64E", "C64U file legs"). `/Flash` and `/Temp` are
never used.

**"… is not a whole copy of your ADF (an interrupted copy?)"** (`doctor`:
a WARN on "ADF on the device"). The device copy's name comes from your
image (`wb-<12 hex digits of its SHA-256>.adf`), so other bytes under
that name mean an earlier copy was cut off (or the file was damaged).
`amiga64 run` asks "Replace it? [y/N]"; answer `y`, or pass
`--replace-adf` (needed without a terminal). Only that file is ever
replaced; a file under another name, such as
`amiga-os-310-workbench.adf`, is never touched.

## The ROM

The AROS ROM is bundled and checked against pinned SHA-256 hashes every
time (`tools/aros/manifest.txt`). A file that does not match is never
used: `host/machine.find_rom` then tries the developer cache
(`~/.cache/amiga64/aros/`) and fails if that does not verify either.

- In an installed amiga64 the message is "broken install: the bundled
  AROS ROM: … ; reinstall amiga64". Reinstall it with pip.
- In a git checkout the advice is "restore it: git checkout --
  third_party/aros" (or, for the cache, run `tools/aros/fetch.sh`).

`run` and `resume` print it as "amiga64: the AROS ROM: …".

## Your ADF

The disk checks (`amiga64/adf.py` `check`; `doctor` shows them on its
`Workbench ADF` line, `run` stops on them before it contacts the device):

| Message | Meaning |
|---|---|
| "… is N bytes (an HD disk?); only DD disk images of 901,120 bytes are supported" | not an 880 KB floppy image (HD, compressed or truncated) |
| "… has no DOS boot block: not an AmigaDOS disk" | a non-DOS disk (for example a game) |
| "… is DOS\N (a directory cache disk): only OFS and FFS disks … are supported" | a directory-cache or long-name disk |
| "…: the file system is inconsistent (…); repair it on an Amiga or an emulator first" | a damaged image (the underlying reason from `host/adf_overlay.py` / `host/adf_files.py` is in the brackets) |
| "… is not a bootable Workbench disk: S/Startup-Sequence (…)" | the startup script cannot be replaced |
| "…: this disk lacks Classes/DataTypes/amigaguide.datatype, which the ModPlayer overlay replaces; use a Workbench 3.1 disk" | the session needs that file's room for ModPlayer; `adf add --out` does not need it |

`amiga64 adf add` refuses, one message per limit (`amiga64/adf.py`):

| Message | Meaning |
|---|---|
| "Not enough room: X needs N blocks, the disk has M free (… bytes). amiga64 never deletes your files to make room" | the files do not fit |
| "X already exists on the disk; amiga64 never overwrites a file there. Use --as NAME" | no overwrite |
| "…: the name '…' is N characters; AmigaDOS names are 1 to at most 30 characters. Use --as NAME" (also: blank, a leading or trailing space, a control character, `:` or `/`, not Latin-1) | AmigaDOS name rules |
| "--to X: no such drawer on the disk" / "--to X: not a drawer" | the target drawer must exist |
| "… is a symbolic link: give the file it points to" / "…: only plain files and directories can be added" | links and special files are refused |
| "…: no such file or directory" | the host path is missing (also for a staged file that was removed from your computer: `amiga64 adf rm` unstages it) |
| "--as names one file: give one PATH with it" | `--as` with several paths |
| "X already exists; --out writes a new file and never replaces one" | `--out` must name a new file |
| "--out X: no such directory D" / "--out X: cannot write it (…)" | the folder for `--out` is missing or not writable |

"Untested, may work" from `doctor` only means the image's SHA-256 is not
on the tested list ([DISKS_AND_FILES.md](DISKS_AND_FILES.md)).

## Booting

**It takes a long time.** About 2-4 minutes to the Workbench is normal
(measured: about 135 s on a C64U, about 180 s on a U64E;
[DEVICES.md](DEVICES.md)); the screens before it are AROS's.

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
| everything is slow | the 68000 runs at about 1 MHz equivalent at best | expected |

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

**I can't find it, or double-clicking does nothing.** ModPlayer is inside
the Workbench 3.1 disk: open the disk first. Its icon sits just to the
right of the Expansion drawer, and its label is hard to read. While the
window fills, Workbench redraws it from scratch a few times, and a redraw
in the middle of a double-click loses it. Double-click between redraws,
and a little more slowly if clicks still do not register.

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

Sources: `amiga64/cli.py`, `adf.py`, `device.py`, `session.py`
(`SNAP_REFUSALS`), `config.py`; `host/adf_overlay.py`, `host/adf_files.py`,
`host/machine.py` (`find_rom`), `host/snapfmt.py`, `tests/aros_wb.py`
(`map_sampler`, `session`), `fixtures/modwb/modwb.s`, docs/HARDWARE.md,
CONTRACTS "Suspend/resume"; W17-K2's message list
(`w17-k2/build/evidence/w17-k2/PROGRESS.md`).
