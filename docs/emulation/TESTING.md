# Testing

Almost every behaviour in this project is pinned by a test that was seen
to fail before the code made it pass ("red first"), and every package was
attacked by an independent reviewer before it merged (WAVE4_BRIEF C5,
carried through every later brief). This page says what the layers are
and how to run them.

## The pieces

| Layer | What it checks | Needs |
|---|---|---|
| **host tests** (`host/tests/`, and the host-only tests under `tests/`; pytest) | the Python tools and every host reference model; many 6502 modules on an in-process 6502 + REU model (`test_td.py`, `test_snap_file.py`, …) | Python only |
| **VICE legs** (`tests/*.py --backend vice`; the `aros_*` tools and `run_dos`, `run_inp`, `run_blt`, `run_cwin` default to VICE; `run_program`, `run_hello`, `run_vbcc`, `run_con`, `run_vectors` and `smc` default to `auto`, which follows `C64_BACKEND`) | the real runner PRG in `x64sc` with a 16 MB REU, driven through the harness's binary monitor | VICE, cc65 |
| **hardware legs** (`--backend u64`; `run_paula` uses `--hw u64`) | the same runner on a U64E or C64U over the network | a device, the harness |
| **golden vectors** | one 68000 instruction per case against SingleStepTests | VICE (or a device) |
| **the AROS reference oracle** | vbcc programs on Amiga64 vs on AROS in FS-UAE | FS-UAE, the AROS ROM, vbcc |
| **the boot model** (`tools/s3probe/aros_boot_model.py`) | a host model of an A500 booting the AROS ROM, the reference for serial text and screens | the s3probe venv (`tools/s3probe/setup_venv.sh`), the ROM |
| **mutants** (`--mutants`, per package, at most 10) | that a test has teeth: each mutant build must fail | as the leg |

c64-test-harness is the only way any tool touches VICE or a device: it
owns VICE's lifecycle, the binary monitor, the Ultimate's REST client, the
SocketDMA REU write, device storage, and the cross-process **DeviceLock**.
Its usage is in the harness's own documentation.

## Everyday commands

```sh
make host-test            # pytest
make runtime              # build/runner.prg, .lbl, .map, stamped
make vice-test            # golden + hand vectors in VICE
make rescheck             # the address-claim checker
make vectors && python3 -m host.handvec --out build/vectors-hand   # inputs of the gate
sh tests/rec_gate.sh build build/gate     # the full VICE gate, one log per step
```

`tests/rec_gate.sh` runs, in order: host tests, the runtime build, the
golden vectors (4 VICE instances), the hand vectors, `run_program`,
`run_hello`, rescheck, the `FETCH_DIRECT` variant build, `rec_budget`,
`bss_boot`, `rec_wrap`, `rec_apisweep`, the supervisor vectors and their
leak check, `perf_cycles`, and the REC mutant matrix. Its exit status is
the number of failed steps.

The host suite at the end of Wave 17 (the final state) is **2321 passed,
16 skipped**, run in a clean checkout as
`python3 -m pytest host/tests -q -p no:cacheprovider`; `make host-test`
runs a bare `pytest -q`, whose `pyproject.toml` test path is `host/tests`.
Run it from a clean tree: a stale `build/` left by an older build can
fail tests that read it (seen with `host/tests/test_paula_layout.py`).

Opt-in host tests, skipped unless their variable is `1`:

| Variable | Turns on |
|---|---|
| `AMIGA64_VICE_TESTS` | `amiga64 run --backend vice --minutes 0` from a read-only installed tree (several minutes; needs `x64sc`, `prebuilt/`, and a Workbench 3.1 ADF, see `AMIGA64_TEST_ADF` below) |
| `AMIGA64_NET_TESTS` | the clean-venv install test resolves the pinned harness tag from GitHub instead of copying in the local harness |
| `AMIGA64_CHIP_ORACLE` | the live FS-UAE leg of the chipset reference (needs FS-UAE and vasm) |
| `AMIGA64_AROS_ORACLE` | the AROS oracle's FS-UAE leg and the `tools/aros/fetch.sh` fail-closed checks |

`AMIGA64_TEST_ADF` names your Workbench 3.1 ADF for the tests that need
one (default `~/Amiga/amiga-os-310-workbench.adf`); those tests skip when
it is not there.

`AMIGA64_PREBUILT` picks where the C64-side binaries come from: `1`
always loads `prebuilt/`, `0` always builds from source, unset builds
when the toolchain is there and loads `prebuilt/` otherwise
(`host/prebuilt.py`). `make prebuilt` rebuilds `prebuilt/` and its
`MANIFEST`; `make prebuilt-check` rebuilds into a temporary directory
and fails on any difference. `host/tests/test_prebuilt.py` verifies the
manifest on every run and rebuilds byte for byte when the installed
toolchain matches the one `MANIFEST` records (cc65 2.18, vasm 2.0f,
vlink 0.18a); otherwise that one test skips.

## Golden vectors

`host/vectors.py` samples SingleStepTests 680x0 JSON (pinned commit,
seeded, 50 per opcode by default) into binary batches, dropping cases that
take exceptions or touch the I/O banks. `tests/run_vectors.py` writes each
case into `buf_case`, calls `test_run_case` (one `emu_step`), and compares
every register, the SR and every expected memory byte. Current counts:
5129 golden, 63 hand (`host/handvec.py`), and the supervisor set
(`python3 -m host.vectors --set super`, plus `SUPERHAND`) run by
`tests/run_super_vectors.py` (CONTRACTS "Vector sets").

## Program-mode legs

`tests/run_program.py` (the loader), `run_hello.py`, `run_vbcc.py`
(compiled C programs; vbcc from `tools/vbcc/install.sh`, outside the
repo), `run_dos.py` (files), `run_con.py` (keyboard). The oracle,
`tests/aros_oracle.py`, compares the vbcc programs against AROS in FS-UAE
(docs/ORACLE.md); it is a second opinion, not a gate.

## Machine-mode legs

| Tool | Legs (main) | Proves |
|---|---|---|
| `tests/aros_boot.py` | `m1`, `m2`, `m3`, `ablate`, `lat`, `poll`, `s0`, `wkhook`, `pix` | AROS boots: the serial text equals the boot model's byte for byte up to "romtaginit done"; the insert-disk screen equals one of the model's conversions |
| `tests/aros_resume.py` | `mid`, `m3`, `m3file` | suspend and resume, REU-resident and through a file, with every refusal |
| `tests/aros_wb.py` | `w`, `nohle`, `resume`, `e2e`, `demo-boot`, `demo`, `modwb`, `aud`, `audres`, `cost` | Workbench from the ADF; scripted mouse to the disk icon; the attended demos; ModPlayer launched from Workbench |
| `tests/run_paula.py` | VICE legs and `--hw u64` | Paula's model on VICE, pitch/level/timing on the U64E through the audio stream |
| `tests/run_modwb.py` | `--leg model`, `--leg vice` | the ModPlayer launcher under a small exec stub and a model |

Run times differ by orders of magnitude: a VICE AROS boot to the
insert-disk screen takes about 174 s of warp at `s` = 9 (PERF (l)); a
Workbench leg on a device about 2.5 minutes to the Workbench plus the
lock wait and staging.

## Rules for device legs

These rules held for every tool and every contributor throughout
development (every wave brief, "Operating rules"), and they still apply:

- **Only through the harness**: the DeviceLock and `u64_hold`
  (`tests/bench/u64_session.py`), which snapshots the full device
  configuration on entry and restores it on exit; the hold's final re-read
  must say IDENTICAL.
- **No raw REST, curl or FTP**, and never the Ultimate's on-screen menu
  (opening it during a hold once wedged a C64U until a power cycle,
  docs/HARDWARE.md "Wave 14 hardware findings").
- Never `pkill`/`killall`; stop only processes you started.
- Long runners reinstall `signal.default_int_handler`, give every leg a
  deadline, turn SIGTERM into the restore path, and write DONE markers.
- One holder per device at a time; the DeviceLock queue decides.
- In the test tools the device's address comes from `U64_HOST`
  (the `amiga64` CLI looks at `--host`, then `AMIGA64_HOST`, then
  `U64_HOST`, then its config file). `U64_BASELINE_ON_ENTRY=0` is
  required (`tests/bench/u64_session.py`): it disables the harness's
  factory baseline on entry.

## Device quirks that affect tests

- On the U64E, host reads of a running C64's RAM can race the guest; the
  serial capture brackets every ring read with two count reads
  (`tests/aros_boot.Machine.serial`), and a
  stale-read effect under heavy REU DMA is not ruled out (docs/HARDWARE.md
  "Reading a running C64's RAM over REST at 48 MHz").
- The C64U on WiFi refused the UDP audio stream; on ethernet it works
  (2026-10-13: 1814 packets, none dropped, in the `rbsil` leg;
  `build/evidence/sup16/c64u-hw16-eth/run.log` in the w16-int worktree,
  CONTRACTS "Silence at the end").
- The rig keyboard's LED board hides the first key hold after about two
  minutes idle; attended keyboard tests start with a SHIFT tap
  (docs/HARDWARE.md "Test-rig note").
- The project's Wave 13 joystick has Up stuck closed (`--stuck 0x01`).

## Reference models

| Model | Mirrors |
|---|---|
| `host/chipset_ref.py` | custom registers, copper, blitter, interrupts, CIAs, guest time and beam (`MachTime`) |
| `host/c2v_ref.py` | the display decision, the conversion, the colour rule, the LUT |
| `host/inp_ref.py` | the input scan, ramp, 1351 decode, hysteresis, the left button's edge queue (W14-M) |
| `host/paula_ref.py` | the Paula model, `cfg_r16`, the sampler RATE |
| `host/mod_ref.py` | the MOD replay's Paula writes and `mod_view` per tick |
| `host/snapfmt.py` | the snapshot format and the build ID |
| `tools/s3probe/aros_boot_model.py` | an A500 running the AROS ROM (serial, screens, instruction counts) |
| `tests/chip_oracle.py` | FS-UAE as the reference for blits and chip-register effects |

Where a model and FS-UAE disagreed, the model was corrected only with a
supervisor ruling, recorded in CONTRACTS (for example the BLTBDAT shift,
"Blitter (S3.6)").

## Writing a new test

1. Write the test and run it against the unchanged code: it must fail.
   Keep that output.
2. Implement until it passes.
3. Add a mutant (a deliberately broken build or input) that the test must
   kill, when the test could otherwise pass vacuously.
4. Run the full gate, not a filtered subset.

Sources: Makefile; `tests/rec_gate.sh`; `tests/README.md`; CONTRACTS "Test
vector pipeline", "Vector sets"; docs/ORACLE.md; docs/WAVE4_BRIEF.md C5;
docs/WAVE13-17 briefs "Operating rules"; docs/HARDWARE.md; the leg lists in
each tool's module docstring and `main()`.
