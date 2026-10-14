# Quickstart: Workbench on your Ultimate

This gets AROS and your own Workbench 3.1 disk running on a C64 Ultimate
or an Ultimate 64 Elite, driven from a Mac or Linux computer on the same
network. Most of the first run's time is the boot.

> The repository is private until it is published; the install line
> below is the one to use then.

## What you need

| | Requirement | Notes |
|---|---|---|
| Device | a **C64 Ultimate** or an **Ultimate 64 Elite** | tested: U64E firmware 3.15, C64U firmware 1.1.0, both NTSC ([DEVICES.md](DEVICES.md)) |
| Storage on the device | a USB stick (U64E, `/USB1`) or an SD card (C64U, `/SD`) | about 1 MB for the disk image, about 6 MB more for a saved session |
| Network | the device and your computer on one network | the U64E was tested wired; the C64U on WiFi and on ethernet |
| Computer | macOS or Linux, **Python 3.11 or newer** | Windows is not supported yet |
| Workbench | your own **Workbench 3.1 ADF** (an 880 KB floppy image) | not included and never uploaded anywhere but your own device ([DISKS_AND_FILES.md](DISKS_AND_FILES.md)) |
| Input | a joystick in port 2, and/or a Commodore 1351 mouse | the 1351 must be switched on for the session, see step 5 ([DEVICES.md](DEVICES.md)) |
| Sound | the C64's audio output connected | the music player uses the Ultimate's sampler |

You do **not** need cc65, vasm, VICE or an AROS download: the C64-side
programs ship prebuilt, and the AROS ROM is bundled (its licence is in
`third_party/aros/`).

## 1. Install

```sh
python3 -m venv ~/amiga64-venv && . ~/amiga64-venv/bin/activate
pip install "git+https://github.com/JC-000/Amiga64@v0.17.0"
```

or from a clone:

```sh
git clone https://github.com/JC-000/Amiga64 && cd Amiga64
pip install .
```

## 2. Put your Workbench disk where it is found

Copy your ADF into a folder called `adf` in the directory you run the
command from:

```sh
mkdir -p adf && cp "/path/to/your/Workbench 3.1.adf" adf/
```

Or pass it each time with `--adf PATH`, or answer the prompt on the first
run (the answer is saved). The file is only read, never changed.

## 3. Check everything

```sh
amiga64 doctor
```

`doctor` prints one `OK`, `WARN` or `FAIL` line per check and exits with
status 1 if anything failed. Without the device (`--offline`) it checks
Python, c64-test-harness, the prebuilt binaries, the AROS ROM, your ADF
(where it was found, its DOS type, tested or untested) and the session
disk (whether ModPlayer and your staged files fit). With the device (one
short session; the C64 is reset when it is released) it also checks the
model and firmware (U64E 3.15 and C64U 1.1.0 are the tested ones), the
speed, the 16 MB REU, the Command Interface, the audio sampler, the
device's **DMA service** and **FTP service** (both must be on: a FAIL
otherwise; the C64U switches its DMA service off at every power-on),
leftover firmware files in `/Temp` (0 or 1 is normal, 2 or more is a
WARN), the storage
volume, your ADF and a saved session on the device, and that the device
settings came back as they were. The first
time it asks for your device's address (an IP address or host name) and
saves it in `~/.config/amiga64/config.toml`. You can also give it with
`--host ADDRESS` or the environment variable `AMIGA64_HOST`. Prompts appear
only when you run it in a terminal; otherwise a missing setting is an
error that names the option to use.

If your device has a network password, put it in the environment variable
`U64_PASSWORD` or in the config file; it is never taken from the command
line.

## 4. Run it

```sh
amiga64 run
```

What happens:

1. The first time, it asks once before copying your ADF to
   `<volume>/amiga64/wb-<12 hex digits>.adf` on the device ("Copy … (880 KB)
   to … on …? [Y/n]"; `--yes` skips the question). The digits come from
   the image's SHA-256, and it never overwrites a file there. If the
   device already holds the same image (for example as
   `amiga-os-310-workbench.adf`), it reuses that file.
2. It takes the device for the session: it waits up to 300 s if another
   program is using it (then it stops: the device "is busy … try again later"), records every device setting, switches on the 16 MB REU, the
   turbo speed (48 MHz on the U64E, 64 MHz on the C64U) and the settings
   the session needs, and reboots the C64.
3. It loads the emulator, copies the AROS ROM and your disk into the REU,
   and boots. You see AROS's start-up screens on the C64.
4. **The Workbench appears after about 2-3 minutes.** Measured with
   `amiga64 run`: 135 s on a C64U at 64 MHz, 180 s on a U64E at 48 MHz.
   It then prints how to use it.
5. It then hands the joystick (and the mouse) to you and keeps running
   until you press **Ctrl-C** in the terminal, or for `--minutes N`.
   Sound is on when the device has the sampler setting; `--no-audio`
   turns it off.
6. When it ends it slows the C64 back to 1 MHz, reboots it, puts every
   device setting back as it found it, and prints where its log and report
   are (`~/.local/state/amiga64/holds/<date-time>-run/`, or under
   `$XDG_STATE_HOME` or `AMIGA64_STATE_DIR`).

## 5. Use Workbench

| You do | Amiga gets |
|---|---|
| joystick in port 2 | mouse movement: slow at first, faster the longer you hold |
| fire button | left mouse button: click, double-click, drag |
| hold **C=** | right mouse button: hold C= and move to use the menus |
| a 1351 mouse, with `--mouse-port 1` (or 2, or `mouse_port` in the config) | mouse movement, left and right buttons; the joystick then uses the other port |
| **F1** | switch between the fitted view and a 1:1 view |

- The screen is converted to the C64's 16 colours. In the normal (fitted)
  view hires text is squeezed to half width and hard to read; press **F1**
  for the **1:1 view**, which shows a 320×200 window of the Amiga screen
  at full detail and follows the pointer. F1 again goes back.
- The fitted view shows the top 200 lines of the Amiga screen.
- No other keys reach the Amiga: you cannot type into AROS.
- Everything is slow: the emulated 68000 runs at well under 1 MHz
  equivalent, against the real Amiga's 7 MHz.

## 6. Play some music

The session disk has three extra files in its root: **ModPlayer**, its
icon, and **ModPlayer.mod**, a generated test song.

1. Double-click the Workbench disk icon to open its window.
2. Double-click the **ModPlayer** icon.
3. The player takes over the whole screen and plays the song through the
   Ultimate's sampler, Amiga Paula style, four channels.
4. Click (fire or the left mouse button), or wait for the song's end, and
   Workbench comes back as it was.

To make room for the player, `Classes/DataTypes/amigaguide.datatype` is
removed from the **session copy** of the disk. Your ADF is not changed.

## 7. Save and resume a session

```sh
amiga64 run --save     # at the end, save the running machine to the device
amiga64 resume         # later: continue exactly there
```

`--save` writes `<volume>/amiga64/wb-snap.bin` (about 6 MB) on the device
when the session ends (Ctrl-C or `--minutes`; a SIGTERM never saves).
`resume` brings that machine back: the resume itself took 1.23 s on a
U64E and 6.45 s on a C64U with `amiga64`, and the earlier test legs saw
the same Workbench picture back 3.8 s (U64E) and 9.4-9.8 s (C64U) after
the launch. A saved session belongs to the exact build of `amiga64` and
the ROM that made it: after an upgrade `resume` refuses it ("it was saved
by a different amiga64 build"), and you run again.

## 8. Add your own files

```sh
amiga64 adf add MyProgram MyDrawer/      # check and stage them
amiga64 adf ls                           # what is staged, and the disk
amiga64 run                              # boots with them; says what it added
```

Files and folders go onto the session's copy of the disk, never into your
ADF. There is room for about 21 KB on the session copy of the tested
disk. `adf add --out NEW.adf` writes a new ADF (your disk plus the files)
instead, but a stock Workbench 3.1 disk has only about 1.5 KB free.
`adf rm DISKPATH` and `adf clear` change the staged list. See [DISKS_AND_FILES.md](DISKS_AND_FILES.md) for what fits and what is
supported.

## If something goes wrong

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md). The short version: run
`amiga64 doctor`, and never open the Ultimate's on-screen menu while a
session runs.

How it works inside: [docs/emulation/OVERVIEW.md](../emulation/OVERVIEW.md).

Sources: docs/WAVE17_BRIEF.md (goal, rulings), W17-K's M0 design
(`w17-k/build/evidence/w17-k/PROGRESS.md`), `tests/aros_wb.py`
(`leg_demo_boot`, `session`, `make_disk`), CONTRACTS "Input", docs/HARDWARE.md
"Workbench 3.1 from the ADF", `host/adf_files.py` (`modwb_image`).
