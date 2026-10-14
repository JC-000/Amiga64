# Devices

Amiga64 runs on two members of the Ultimate family. The emulator itself is
6502 code on the C64 side; your computer stages the session over the
network and then just keeps the session open.

## Supported devices

| | Ultimate 64 Elite (U64E) | C64 Ultimate (C64U) |
|---|---|---|
| Tested firmware | 3.15 (the recorded FPGA/core builds under it varied: FPGA 125-126, core 1.4F-1.50); the first M1 runs used 3.14d | 1.1.0 (FPGA 122, core 1.49) |
| Video standard tested | NTSC | NTSC |
| CPU speed used | 48 MHz | 64 MHz |
| Storage volume | `/USB1` (a USB stick) | `/SD` (a microSD card) |
| Network tested | wired | WiFi through Wave 16 (the recorded boot and resume times), ethernet since 2026-10-13; on WiFi the device refused the UDP audio stream that the test tools use |
| Sampler ("Ultimate Audio") | present | present |
| Workbench up with `amiga64 run` | 180 s | 135 s |
| Workbench boot, test leg `w` (trimmed script) | 147-155 s | 130.5 s |
| `amiga64 resume`, the resume itself | 1.23 s | 6.45 s |
| Resume, test leg: the same picture back after the launch | about 3.8 s | 9.4-9.8 s |
| Services that must be on | Ultimate DMA Service, FTP File Service | the same; the C64U switches its DMA service off at every power-on |

Sources: docs/HARDWARE.md ("Device + setup", "REC shadow core", "CIA clock
at turbo", "Workbench 3.1 from the ADF", "Wave 15 player on the
Ultimates", "The C64U's sampler"); the brief's device list
(docs/WAVE17_BRIEF.md). Other firmware versions, PAL machines and other
Ultimate models are **untested**. On a PAL machine guest time runs about
4% slower, and the music player's tempo block assumes NTSC timing
(`host/profile_block.build` defaults to the NTSC φ2), so its tempo would be
about 4% off [derived: 1022727 / 985248 Hz].

## The address

The device is found by its IP address or host name, in this order:

1. `--host ADDRESS` on the command line;
2. the environment variable `AMIGA64_HOST`;
3. the environment variable `U64_HOST` (what the test tools use);
4. `~/.config/amiga64/config.toml` (or `$XDG_CONFIG_HOME/amiga64/`, or the
   file named by `AMIGA64_CONFIG`), which the first run writes after asking
   you. It is created with mode 0600.

`amiga64 config` shows every setting and where it came from.

A network password, if your device has one, comes from `U64_PASSWORD` or
the config file, never from the command line.

The kind of device (from its product name), its CPU speed (48 MHz on the
U64E, 64 MHz on the C64U) and its storage volume are detected, and can be
set with `--kind U64E|C64U`, `--mhz N` and `--vol /VOLUME`, or in the
config file's `[device]` section (`kind`, `mhz`, `volume`). The option
wins over the config, the config over the detection. The speed must be
one of the device's own CPU Speed choices: otherwise the check refuses it
before the session starts ("N MHz (--mhz): this device offers 1, 2, …
MHz; give --mhz with one of them"). A config `mhz` or `mouse_port` that
is not a number is an error naming the file ("mhz 'x' in …: not a
number"). Without `--vol`, the session uses `/USB1` (U64E) or `/SD`
(C64U), or the device's first other writable volume when that one is
missing.

## What a session changes on the device, and puts back

Every session (`doctor`'s device check, `run`, `resume`) takes the device
through c64-test-harness's **device lock**, so it waits while another
program is using the device. It waits for as long as that program keeps
making progress. It gives up when the holder has stopped making progress
for 300 s, or at most 300 s after the device has changed hands more than 3 times
during the wait: "the Ultimate at … stayed busy (another program held it
and stopped making progress for 300 s, or it changed hands too often);
try again later". Then (`tests/aros_wb.session`, `u64_hold`):

| Step | Why |
|---|---|
| a snapshot of **every** device setting | to restore it exactly |
| CPU speed set (48 or 64 MHz), C64 rebooted | the emulator needs turbo |
| REU **Enabled, 16 MB** | guest memory is the REU |
| the runner loaded, a small dispatcher installed at `$CE00` | the computer calls into the C64 through it |
| **Command Interface** Enabled | the disk image and snapshots are loaded with Ultimate DOS commands; both devices keep it disabled normally |
| **Map Ultimate Audio $DF20-DFFF** Enabled (with sound) | the sampler is the Amiga's sound; the session proves the mapping before it uses it |
| at the end: 1 MHz, reboot | the reboot also stops any sound still playing |
| every setting restored, read back, compared | the hold reports IDENTICAL when everything is as it was; the report is kept in `~/.local/state/amiga64/holds/` |

Nothing is ever written to the device's flash, and the firmware is never
touched.

**Don't open the Ultimate's on-screen menu while a session runs.** On the
C64U this once wedged the device: its REST interface timed out, the
settings could not be restored, and only a power cycle recovered it
(docs/HARDWARE.md "Wave 14 hardware findings").

## Files the session writes on the device

All in one folder, `<volume>/amiga64/` (amiga64 never writes to
`/Flash` or `/Temp` itself):

| File | Written by | Notes |
|---|---|---|
| your ADF, as `wb-<first 12 hex digits of its SHA-256>.adf` | `run`, the first time, after asking | if the same image is already there (also as `amiga-os-310-workbench.adf`) it is reused. Never overwritten, except an interrupted copy under this name, and only when you answer yes or pass `--replace-adf` |
| `wb-snap.bin` | `run --save` | the saved session, about 6 MB |

The C64U's SD card must be a card the firmware can use: in one case a
card listed as empty and refused `mkdir` until the user unlocked it and
reformatted it as MBR + FAT32 (which of the two fixed it is not known) (docs/HARDWARE.md "Device storage legs on the U64E", its
"C64U file legs" part).

The firmware itself also leaves a file in `/Temp` each time a program is
started on the C64: one per `run` (two with sound) and two or three per
`resume`. The harness deletes the older ones under its lock and keeps the
newest, so `doctor` treats 0 or 1 as normal and warns at 2 or more (a
full `/Temp` can crash a C64 Ultimate; a power cycle clears it).

## Other harness tools on the same device

If you also run other c64-test-harness tools (tests, other projects)
against the same Ultimate, they queue behind each other only when they
lock the device under the same key. The harness locks it as
`uid-<id>` only when its alias map lists the address you use:
`~/.config/c64-test-harness/devices.toml`, or the file named by
`$C64_DEVICE_ALIASES_FILE`. Otherwise it locks the raw address, and a
tool that reaches the same device by another address (its WiFi and its
ethernet address, or a host name) does not wait for amiga64, and the
other way round. `amiga64 doctor` prints the key in use on its `device
lock` line: "uid-… (shared with other harness tools via devices.toml)" or
"<address> (address key; tools using another address for the same device
won't queue behind it)". amiga64 itself never writes the alias map.

## Controllers

| Port | Default | Notes |
|---|---|---|
| port 2 | joystick: the Amiga mouse with an accelerating ramp, fire = left button | |
| port 1 | a Commodore 1351 in proportional mode, when switched on for the session | `--mouse-port 1` (or 2) switches the 1351 on in that port; the joystick then uses the other one. `0` (the default) is joystick only |
| keyboard | **C=** = right mouse button, **F1** = view toggle | no other key reaches the Amiga |

A stick with a switch stuck closed can be masked with `--stuck MASK`
(the project's own stick has Up stuck: `--stuck 0x01`).

After about two minutes idle the project's keyboard, which carries an LED
board, misses the first key hold; that is that keyboard, not the emulator
(docs/HARDWARE.md "Test-rig note").

## Speed

The emulator runs the 68000 at about 1 MHz equivalent at best (measured
on the U64E: about 0.8-1 MHz on light code; on the AROS boot about 75-80%
of that up to its first milestone, then a third to a half; the C64U was
not benchmarked the same way); the device's turbo speed matters less than you might expect,
because the REU controller's register accesses stop getting faster with the CPU
clock: about 1.7-1.9 µs each at 8-24 MHz and 0.9-1.0 µs at 32-48 MHz on
the U64E (docs/HARDWARE.md "Findings"). Details:
[docs/emulation/CPU.md](../emulation/CPU.md).

Sources: `amiga64/device.py` (`probe`, `check_speed`, `choose_volume`,
`ensure_adf`, `lock_key`, `explain`), `amiga64/cli.py` (`doctor_device`),
`tests/aros_wb.py` (`session`, `enable_ci`, `map_sampler`,
`leg_demo_boot`, `demo_mouse`), `tests/bench/u64_session.py`,
c64-test-harness `backends/device_lock.py` (the wait) and
`backends/device_aliases.py` (the alias map), docs/WAVE17_BRIEF.md
rulings, docs/HARDWARE.md sections named above.
