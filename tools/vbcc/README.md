# vbcc m68k-amigaos toolchain (test-time only)

`tools/vbcc/install.sh` installs the vbcc cross toolchain used by
`tests/run_vbcc.py` (the M2 exit bar: vbcc-compiled programs running in
the interpreter). Nothing it installs lives in the repository.

## What and where

| Component | Version | Source (official distribution) | SHA256 |
|---|---|---|---|
| vbcc compiler (`vbccm68k`, `vc`) | 0.9h patch 3 | `http://phoenix.owl.de/tags/vbcc0_9hP3.tar.gz` | `cc5571f36ff63158f31b2757c5374b636e073451e147f05575307d56572d9029` |
| vasm, m68k CPU / Motorola syntax (`vasmm68k_mot`, `vobjdump`) | 2.0f | `http://phoenix.owl.de/tags/vasm2_0f.tar.gz` | `c84b2de1cbb87831795fe64a85c5d9a7002a766e3a7c30b0a2d7d5e99d878f49` |
| vlink | 0.18a | `http://phoenix.owl.de/tags/vlink0_18a.tar.gz` | `8d151cdd30a4feb575a364e68810c2bc300fe1a7c074dbbee6fd1175a6c5bfae` |
| m68k-amigaos target (`startup.o`, `vc.lib`, headers) | 2022-05-22 (0.9h P3) | `http://phoenix.owl.de/vbcc/2022-05-22/vbcc_target_m68k-amigaos.lha` | `ec734d7115359cdb5d1c70349284ecbd5712ef47e8bc28d4150117c6f8c73289` |
| Unix config files (`config/aos68k`, …) | 2022-05-22 | `http://phoenix.owl.de/vbcc/2022-05-22/vbcc_unix_config.tar.gz` | `c643a095439aaf0cbcf7521920c0ff5d1cb07a4fc6fe8cc59e69ae1dbb329497` |

All URLs are the ones linked from the vbcc/vasm/vlink home pages
(`sun.hasenbraten.de/vbcc`, `/vasm`, `/vlink`, maintained by the authors,
Volker Barthelmann and Frank Wille); the target archive was also verified
byte-identical to `phoenix.owl.de/vbcc/current/` on 2026-09-23.

- Install prefix: `~/.local/opt/vbcc` (override `VBCC_PREFIX`), with
  `bin/`, `config/`, `targets/m68k-amigaos/`.
- Download/build cache: `~/.local/opt/vbcc-dl` (override `VBCC_DL`).
- Every archive is SHA256-verified **before** anything is extracted; a
  mismatch aborts. Re-running is idempotent (cached, verified archives and
  already-built tools are kept; it re-runs the final smoke compile).
- Needs: `curl`, `shasum`, `make`, a host C compiler, and `lha`
  (`brew install lhasa`) for the target archive.
- No Amiga NDK, Kickstart ROM or other commercial material is used: the
  test programs use only ISO C stdio/stdlib from vc.lib.

Use:

```sh
tools/vbcc/install.sh
export VBCC=~/.local/opt/vbcc PATH=$VBCC/bin:$PATH
python3 -m tests.run_vbcc --prg build/<pkg>/runner.prg
```

`tests/run_vbcc.py` finds the toolchain via `$VBCC` or the default
prefix and compiles with vbcc's **stock** `vc +aos68k -O2` — exactly what
users get. Its vlink line uses `-Rshort`, which emits
`HUNK_RELOC32SHORT` (`$3F7` in a load file); `host/hunk.py` parses it.

## Licence note

vbcc is copyrighted by Volker Barthelmann and is **free for
non-commercial use only** (see the `vbcc` documentation / archive
headers); vasm and vlink have similar terms, and the target archive
(startup code, vc.lib) is part of the same distribution. Therefore:

- never commit vbcc/vasm/vlink binaries, their libraries or headers, or
  any file derived from their configs;
- never commit compiled test programs — they link vbcc's `startup.o` and
  `vc.lib`. They are built into `build/vbcc/` (git-ignored) at test time;
- only our own C sources (`fixtures/c/*.c`) and vasm sources
  (`fixtures/c/hle_unit/*.s`) are in the repository.
