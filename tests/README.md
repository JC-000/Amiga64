# tests/ — M1 test execution (runner PRG + VICE driver)

Owner: D-harness. Everything here builds against `docs/CONTRACTS.md`; VICE is
driven through the `c64-test-harness` package (`~/Documents/c64-test-harness`,
see the `c64-test` skill).

## Entry points

| Command | What it proves |
|---|---|
| `python3 -m tests.smoke` (= `make smoke`) | Harness path works: builds a ~20-line PRG from `tests/stub/smoke.s`, boots VICE, `jsr()`s a label, asserts memory. No core involved. |
| `python3 -m tests.stub.e2e` | Full pipeline against the mock core: links the REAL `tests/runner/runner.s` with `tests/stub/stub_core.s` (private `tests/stub/stub.cfg`), generates 3 hand-written mini vector batches, runs them through the real `tests.run_vectors` CLI. |
| `python3 -m tests.run_vectors --prg build/runner.prg --vectors build/vectors` (= `make vice-test`) | The real thing, once `runtime/core.s` exists. |

`run_vectors` options: `--filter SUBSTR` (select batches by filename),
`--max-failures N`, `--jobs N` (parallel VICE instances, one batch each, via
the harness's `run_parallel`), `--timeout SECS` (per case), `--ccr-only`
(compare only CCR bits `$1F` instead of the full SR word), `--labels PATH`
(default: PRG path with `.lbl`), `--backend auto|vice|u64`.

## Per-case protocol (runner.s)

Host writes one case record (format: `tests/case_format.py`, identical to
`host/vectors.py`) to `buf_case` ($C000), then calls `test_run_case`
(address from the ld65 `-Ln` label file, `build/runner.lbl`). The runner:
sets the done flag to 0, `emu_reset`, copies regs/PC/SR (70 contiguous LE
bytes) into the ZP register file ($10–$55), applies init-mem pairs via
`mem_write8`, calls `emu_step` ONCE, dumps regs/PC/SR + `st_halt`/`st_reason`
to `buf_result`, reads each expected-mem address back via `mem_read8`, sets
the done flag, RTS. `test_run_case` does SEI on entry (core may bank ROMs
out during the step). Runner-private state lives in the loader/harness ZP
range $02–$08, which the interpreter never touches.

## buf_result layout ($C800)

| Offset | Size | Contents |
|---|---|---|
| +$00 | 1 | done flag: $00 = busy (set on entry), $FF = complete (host-pollable) |
| +$01 | 1 | `st_halt` copy (0 = instruction retired normally) |
| +$02 | 1 | `st_reason` copy |
| +$03 | 1 | n_mem — number of readback bytes at +$80 |
| +$10 | 70 | final D0–D7, A0–A7 (16 × u32 LE), PC (u32 LE), SR (u16 LE) |
| +$80 | m ≤ 255 | `mem_read8` value per expected-mem addr, case order ($C880–$C97F max) |
| +$7FF | 1 | $CFFF: runner-ready flag, `main` writes $A5 at boot |

## Switching from the stub to the real core

Nothing in `tests/` changes. Once `runtime/core.s` (+ `runtime/ops/*.s`)
exists: `make runtime && make vectors && make vice-test`. The Makefile
wildcard links `tests/runner/runner.s` into `build/runner.prg`;
`tests/stub/` is not picked up by it and is only used by the private builds
above. Division of duties agreed with the core agent: core.s owns LOADADDR
and the STARTUP BASIC stub, which must end with `jmp main` (imported);
runner.s exports `main` and `test_run_case` and imports `emu_reset`,
`emu_step`, `mem_read8`, `mem_write8`.

## Stub core (tests/stub/stub_core.s)

Mock ABI for pre-core testing. Guest memory is a 4 KB plain-RAM window
(guest addr & $0FFF), no REU. `emu_step` semantics (mirrored by
`tests/stub/make_mini_vectors.py`): `D0 += 1; [A0] := D1.b; D2.b := [A1];
PC += 2; CCR ^= $04`. Hand-written cases must init every address that gets
read and keep guest addresses below $1000.

## Integrator notes

- `tests/case_format.py` intentionally duplicates the binary case format of
  `host/vectors.py` (encoder needed for stub cases; tests/ stays
  self-contained). Verified byte-exact interop against
  `host.vectors.pack_case`; `case_len` includes its own 2 bytes in both.
  Keep both in sync with CONTRACTS.md.
- VICE is launched with `-reu -reusize 16384` (16 MB) per contract; the
  driver adds these via `ViceConfig.extra_args`.
- Backend is switchable (`--backend`, or `C64_BACKEND` env with `auto`):
  VICE uses `jsr()`; the U64 path (REST `run_prg` + `set_reu` +
  `run_subroutine` sentinel polling) is wired in
  `tests/run_vectors.prepare_target`/`tests/harness_util.call_runner` but
  has not been tested on hardware.
- On a per-case `test_run_case` timeout the driver treats the instance as
  wedged: sequential mode re-acquires a fresh VICE; `--jobs` mode aborts
  that batch. The failure is reported with field `harness`.
