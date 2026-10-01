# Interface Contracts (M0/M1)

Fixed interfaces for parallel work packages. **Changing anything here requires supervisor sign-off** — every agent builds against this document. 0-based, hex addresses unless noted.

## Toolchain

- Assembler: **ca65** (cc65 V2.18+), linker **ld65**. All runtime sources in ca65 syntax (`.proc`, `.segment`, `.import`/`.export`), extension `.s`.
- Host: Python 3.11+, stdlib + pytest only. Package root `host/`, tests in `host/tests/`.
- Emulation: VICE `x64sc`, REU enabled, 16 MB (`-reu -reusize 16384`). Test orchestration via c64-test-harness (`~/Documents/c64-test-harness`; load the `c64-test` skill for usage).
- Generated files land in `build/` (git-ignored). Never commit generated code.

## Guest model

- Guest = 68000 **user-mode integer** subset. 24-bit address space, big-endian.
- Guest memory = REU: guest address `A` maps 1:1 to REU offset `A` (16 MB covers the full 24-bit space). Guest memory bytes are stored exactly as the 68k sees them (big-endian words/longs).
- Supervisor mode and exceptions exist behind ONE flag, `st_exc` (see "Supervisor mode & exceptions" below; supersedes the M1 "no supervisor mode, no exceptions" rule). With `st_exc` clear — the default; the image/program runner never sets it — trap-like events (TRAP, TRAPV, CHK, DIV-by-zero, A-line/F-line, illegal, every privileged op) set `st_halt` with today's reason code and return from `emu_step`, exactly as in M1/M2. No interrupts yet.
- Pseudo-op `$AFFF` = HALT (A-line, reason `HALT_OK`): clean program termination for M0 runs.

## Zero-page register file (interpreter owns $10–$7F)

**Authoritative map: `runtime/rc/zp.inc`** (resource contract, see
`docs/RESOURCES.md`; machine-checked by `make rescheck`). Summary: $02–$0F
loader/harness scratch (never touched by the interpreter); $10–$7F the
interpreter register file — `r_d0..r_d7`, `r_a0..r_a7` (A7 = active SP),
`r_pc`, `r_sr`, `op_lo`/`op_hi`, `m_addr`, `m_val`, `t_ea`,
`t_op1`/`t_op2`, `t_res`, `t_tmp` scratch, `st_halt`/`st_reason`, and the
core-internal $7A–$7F; $80–$82 the fetch-window state (ZEROPAGE segment).
All multi-byte ZP values **little-endian** (LSB first); the memory API
converts to/from guest big-endian at the boundary. CCR layout in `r_sr`:
bit4 X, 3 N, 2 Z, 1 V, 0 C (native 68k); $55 = SR high byte (stored, not
interpreted).

Register accessor convention: register number × 4 = ZP offset from `r_d0` ($10). Core exports macros `getreg`/`setreg` (X = register index 0–15, copies 4 bytes to/from `t_op1`/`t_res`).

## C64 memory map (interpreter build)

**Authoritative map: `runtime/rc/ram.inc`** (C64 side) and
**`runtime/rc/guest.inc`** (guest/REU side) — resource contract, see
`docs/RESOURCES.md`. Summary: runner PRG at $0801–$9FFF (DISPATCH tables
page-aligned at $0900–$0AFF); HLE console screen $0400–$07E7; `buf_case`
$C000–$C7FF; `buf_result` $C800–$CFFF with the $CE00 page reserved as the
hardware-harness mailbox; REC registers $DF00–$DF0A; Ultimate command
interface $DF1C+ (M0 loader only). Guest side: 68k vector page, HLE env
$000400–$000FFF, stack top $00F000, program load base $010000.

- KERNAL/BASIC ROMs may be banked out during interpretation; interpreter must not call KERNAL.
- Linker config: `runtime/c64.cfg` (owned by core agent, others consume).

## Interpreter ABI (exported by `runtime/core.s`)

- `emu_reset` — clear state, `st_halt`=0.
- `emu_step` — execute exactly one guest instruction (fetch, dispatch, retire). Returns with `st_halt` set if the instruction halted/trapped.
- `emu_run` — loop `emu_step` until `st_halt` ≠ 0.
- `fetch_word` — read guest word at PC into `op_lo`/`op_hi` (also mirrored to `t_tmp` for extension words per handler need), PC += 2. Handlers use it for extension words. Clobbers A and X. Served from a 256-byte page-aligned instruction window (one REU DMA per page refill); `emu_reset` and any `mem_write*` overlapping the cached page invalidate it. Any future path that writes guest memory WITHOUT going through `mem_write*` (e.g. direct REU staging mid-run) must set the window-invalid flag (`w_inv`, ZP $82) or call `emu_reset` first.
- Memory API (guest BE ↔ ZP LE conversion at this boundary, v0 = direct per-call REU DMA; internals may later add windowing/TLB — **only these entry points are stable**):
  - `mem_read8` / `mem_read16` / `mem_read32`: `m_addr` → `m_val`
  - `mem_write8` / `mem_write16` / `mem_write32`: `m_addr`, `m_val` →
  - `mem_rec_dirty`: REC-ownership seam — any code that programs the REC outside the memory API calls it before the next memory-API call (clobbers A). Binding rules: `docs/WAVE4_BRIEF.md` C1.
- EA resolution: `ea_calc` — decodes mode/reg bits (X = 6-bit mode|reg field), leaves address in `t_ea`; `ea_load`/`ea_store` sized variants (size in A: 0=B,1=W,2=L). Covers all 68000 modes incl. d8(An,Xn) brief extension and abs.W/L, PC-relative, immediate.
- Sized ALU primitives (all CCR logic lives HERE, not in group handlers): `alu_add`, `alu_sub`, `alu_cmp`, `alu_and`, `alu_or`, `alu_eor`, `alu_not`, `alu_neg`, `alu_addx`, `alu_subx`, `alu_ext`. Inputs `t_op1`,`t_op2`, size in A; result `t_res`, CCR updated per 68000 PRM semantics (X only where the 68k sets it).
- `dispatch_next` — handlers `jmp` here when done (it is the top of the step loop).

## Dispatch

- Primary: 256-entry jump table on `op_hi`, **generated** by `host/gen_dispatch.py` from the static family map below into `build/gen/dispatch.s`. No one hand-edits the table.
- Handler entry state: opcode in `op_lo`/`op_hi`, PC already past the opcode word.
- Family map (owner in parentheses; secondary decode inside the family file; cross-group ops are `.import`ed):

| op_hi | Handler | Owner |
|---|---|---|
| $00–$0F | `fam_0` — ORI/ANDI/SUBI/ADDI/EORI/CMPI, BTST/BCHG/BCLR/BSET, MOVEP(stub→halt) | G3 |
| $10–$3F | `fam_move` — MOVE.B/.L/.W, MOVEA | G1 |
| $40–$4F | `fam_4` — CLR/NEG/NEGX/NOT/TST, EXT/SWAP, LEA/PEA, JSR/JMP/RTS/RTR/NOP, MOVEM (`.import op_movem` from G6), TAS/CHK stub→halt | G5 |
| $50–$5F | `fam_5` — ADDQ/SUBQ, Scc (`.import op_scc` G3), DBcc (`.import op_dbcc` G5) | G2 |
| $60–$6F | `fam_bcc` — BRA/BSR/Bcc | G5 |
| $70–$7F | `fam_moveq` | G1 |
| $80–$8F | `fam_8` — OR; DIVU/DIVS + SBCD `.import` G6 | G3 |
| $90–$9F | `fam_sub` — SUB/SUBA/SUBX | G2 |
| $A0–$AF | `op_aline` — HALT ($AFFF) / trap hook | core |
| $B0–$BF | `fam_b` — CMP/CMPA/CMPM; EOR `.import` G3 | G2 |
| $C0–$CF | `fam_c` — AND; MULU/MULS + ABCD `.import` G6; EXG `.import` G5 | G3 |
| $D0–$DF | `fam_add` — ADD/ADDA/ADDX | G2 |
| $E0–$EF | `fam_shift` — ASL/ASR/LSL/LSR/ROL/ROR/ROXL/ROXR | G4 |
| $F0–$FF | `op_fline` — halt reason FLINE | core |

- Cross-op calling convention: a family decoder that recognizes a cross-group pattern does a plain `jmp op_x` with the ORIGINAL opcode word untouched in `op_lo`/`op_hi` (and its `t_tmp` mirror) and nothing pre-decoded; the cross-op owns its full field extraction and ends with `jmp dispatch_next` (same entry state as a family handler).
- Cross-group op label names are pinned: `op_scc`, `op_dbcc` (fam_5), `op_eor` (fam_b), `op_exg`, `op_mulu`, `op_muls`, `op_abcd` (fam_c), `op_divu`, `op_divs`, `op_sbcd` (fam_8), `op_movem`, `op_nbcd` (fam_4). Until the owning group exports one, gen_dispatch.py emits an exported `jmp op_illegal` stub for it so importers always link.
- Unimplemented secondary decodes jump to `op_illegal` (halt reason ILLEGAL). The suite must never silently skip — an unimplemented op fails its vectors.
- File ownership: `runtime/ops/g1_move.s`, `g2_arith.s`, `g3_logic.s`, `g4_shift.s`, `g5_flow.s`, `g6_muldiv.s`. One agent per file; no edits outside your file (+ your test list).

## Supervisor mode & exceptions (W6-S)

Owner: core (`runtime/core.s`) + G5 (`runtime/ops/g5_flow.s`). Authority:
the SingleStepTests vectors (`python3 -m host.vectors --set super`, run by
`tests/run_super_vectors.py`); where upstream has no cases the hand set
`SUPERHAND` (host/vectors.py) is authored from the PRM and cross-checked by
the reference model in `host/tests/test_super_model.py`.

**State.**
- `r_sr` ($54) keeps the CCR exactly where it was; `r_sr+1` ($55) is now the
  interpreted SR system byte: T (bit 15, stored, tracing not modelled), S
  (bit 13), I2–I0 (bits 10–8, stored; no interrupts yet). Every SR write
  masks to `$A71F` (bits a 68000 implements).
- `r_a7` is the ACTIVE stack pointer (unchanged ABI); `r_osp` ($89–$8C, LE)
  holds the inactive one: the USP while S = 1, the SSP while S = 0. Every
  S transition swaps the two (`sp_swap`).
- `st_exc` ($88): nonzero = exception delivery on. It is embedder
  configuration: the boot stub zeroes it, `r_osp` and `st_exl`; `emu_reset`
  does NOT clear `st_exc`/`r_osp` ($88–$94 lie outside the $10–$7F block).
- `emu_reset` LATCHES `st_exc`: it patches the three step-loop entries
  (`emu_step`, `emu_run`, `dispatch_next`'s run-mode branch) to the plain
  loop or the exception loop, and copies the flag to `st_exl` ($94). Every
  runtime test of the flag (`emu_halt`, `op_illegal`, the G5 decoders) reads
  `st_exl`, never live `st_exc`, so a write to `st_exc` between two resets
  has NO effect until the next `emu_reset` (the driver's mid-run check pins
  this: a flip after a flag-clear reset still halts `HALT_ILLEGAL`).
- Exception mode runs a second step loop (`dispatch_gox`) that snapshots,
  per instruction, the start PC `st_ipc`, the opcode `st_iop` and the 6502
  SP `st_sp6` ($8D–$93). **Invariant:** these three are valid ONLY in
  exception mode — `dispatch_gox` writes all of them at the start of every
  instruction, before any reader (`exc_group1`, `exc_halt`, `op_illegal`,
  `exc_raise`) can run, and `dispatch_gox` runs only after `emu_reset`
  latched a nonzero flag. They are therefore never cleared (boot or reset)
  and never read with the flag clear; proof: the driver plants a sentinel in
  `st_ipc` before every flag-clear case and requires it untouched.
- Cost with the flag clear: the fetch/dispatch loop, `dispatch_next`, the
  memory API, EA resolution and the ALU/CCR primitives execute the same
  instructions as before (`host/tests/test_super_hotpath.py`); `emu_step`'s
  `bne` became a patchable `jmp` (same 3 cycles), `emu_run` pays one `jmp`
  per call, `emu_reset` ~35 cycles, and halting paths (`emu_halt`,
  `op_illegal`, the G5 system-op decodes) +5 cycles (`ldx st_exl`/`bne`).
  Measured: docs/PERF.md (f).

**Flag clear (compatibility, non-negotiable).** Bit for bit today's
behaviour: every op below halts `HALT_ILLEGAL` (A/F-line `HALT_ALINE` /
`HALT_FLINE`, `$AFFF` `HALT_OK`) with PC past the opcode word. Pinned by the
flag-clear half of `SUPERHAND` and by `run_dos`/`run_vbcc`/`run_con`/`smc`.

**Flag set.**

| Op | Behaviour |
|---|---|
| ANDI/ORI/EORI #imm,SR | S=1: SR op= imm (masked; may clear S → swap). S=0: privilege violation |
| MOVE <ea>,SR | same, word source (EA side effects only when privileged) |
| MOVE SR,<ea> | user-legal on the 68000 (unchanged G5 code, now writes the real system byte) |
| MOVE An,USP / MOVE USP,An | privileged; USP = `r_osp` |
| RTE | privileged; SR := (A7), PC := 2(A7), A7 += 6, then the new SR applies (swap after the pop). Odd PC: halt `HALT_ADDRESS` |
| TRAP #n | vector 32+n |
| TRAPV | V set: vector 7 |
| CHK <ea>,Dn (word) | Dn < 0: N:=1, vector 6; Dn > src (signed): N:=0, vector 6; in range: N kept. Z/V/C := 0, X kept, always |
| STOP #imm | privileged; SR := imm (may swap), PC past imm, halt `HALT_STOP` ($04) |
| RESET | privileged; no-op + `jsr emu_rst_hook` (a `jmp` whose operand an embedder may patch; default does nothing) |
| DIVU/DIVS by zero | NZVC := 0 (X kept), vector 5 |
| encodings the decoders reject: `$4AFC`, 68010+ ops (MOVEC, RTD, …), undefined `$4E00`–`$4E3F`, EA modes that `ea_calc` rejects (mode 7 reg 5–7), An sources of MOVE to SR / CHK | vector 4 |
| A-line not claimed by the HLE | vector 10 (`$AFFF` stays `HALT_OK`; HLE `$A1xx` indices still run the HLE) |
| F-line | vector 11 |
| TAS, MOVEP (legal, unimplemented) | still halt `HALT_ILLEGAL` |

**Known gap (frozen G-modules, backlog):** the G1–G6 decoders accept some
encodings that are illegal on a 68000 because of the addressing mode; in
exception mode these EXECUTE instead of raising vector 4 (flag clear: they
execute today as well). Found by the W6-S review (`illea.py`): LEA (A0)+,A1
`$43D8`, PEA (A0)+ `$4858`, JMP (A0)+ `$4ED8`, JSR -(A0) `$4EA0`, MOVEM.W
D0,(A0)+ `$4898`, MOVEM.W -(A0),D0 `$4CA0`, CMPI.W #,d16(PC) `$0C7A`,
ADDI.W #,#imm `$067C`, CMPI.W #,#imm `$0C7C` (W6-S re-review), SUBQ.W #1,d16(PC)
`$537A`. Upstream contains only
legal encodings, so no vector covers them; tighten EA legality in the owning
groups before AROS depends on it.

**Exception processing (68000 group 1/2).** Stacked SR = SR before the
exception (after the op's own flag updates: CHK N, DIV NZVC); then S := 1,
T := 0 (A7 switches to the SSP when coming from user mode); A7 -= 6; frame =
SR word at (A7), PC long at 2(A7); PC := long at vector × 4 (VBR fixed at 0).
Stacked PC: the NEXT instruction for TRAP, TRAPV, CHK; the FAULTING
instruction (`st_ipc`) for illegal, privilege violation, A-line, F-line and
divide-by-zero (the latter per the upstream vectors, which differ from the
PRM's "next instruction"). An odd vector halts `HALT_ADDRESS` ($05) after
the frame is written: address and bus errors are a reason code only, never
an exception. The 6502 stack is unwound to `st_sp6` first, so an exception
raised inside a nested `jsr` (e.g. `op_illegal` from `ea_calc`) never
resumes the half-done handler.

**Not modelled (reason code or nothing):** address and bus errors are only
`HALT_ADDRESS` on an odd vector or odd RTE target. An ODD SSP while a frame
is built (or popped by RTE) is not detected — the frame is written
misaligned without a halt (a 68000 would take an address error / double
fault). Tracing (T) is stored, never acted on. Interrupts: none yet (S3).

**Environment notes.** $88–$94 overlap BASIC's CHRGET code ($73–$8A: $88
reads $E9 after boot — the boot stub clears it, and the driver asserts
`st_exc` = 0 on every fresh target) and the KERNAL's STATUS ($90) / STKEY
($91): the KERNAL IRQ writes $91 every jiffy, which is harmless only because
every runner entry runs under SEI. S3's run loop (`mach_run`) must keep the
KERNAL IRQ off in exception mode, or `st_iop` can be clobbered between the
snapshot and the decode.

**Frozen callers.** G3 (`#imm,SR`) and G6 (divide by zero) still `jmp
emu_halt` with `HALT_ILLEGAL`; in exception mode `emu_halt` re-decodes
`st_iop` and executes/raises instead. `op_illegal` distinguishes MOVEP
(G3's stub) from true illegal encodings the same way.

**Vector sets.** `build/vectors` (golden, **5129**) runs with the flag
clear. **io-bank exclusion (Wave 7, W7-T):** banks $BF/$DF are chip
registers since the I/O trap, so `host/vectors.py` (`exclude_io_bank`)
drops, AFTER stride sampling and without re-sampling, every case whose
initial/final RAM or prefetch touches them (MANIFEST reason
`dropped_after_sampling: io-bank`). 21 golden cases go (5150 -> 5129; every
other case is bit-identical to the hardware-validated 5150) and one super
case (MOVEfromSR case 26: 679 -> 678). The hand vectors (63) and
SUPERHAND contain none (`host/tests/test_chip_vectors.py`). `python3 -m host.vectors --set super --offline --out
build/vectors-super` builds ANDItoSR, ORItoSR, EORItoSR, MOVEtoSR,
MOVEfromSR, MOVEtoUSP, MOVEfromUSP, RTE, RESET, TRAP, TRAPV, CHK (50 each,
same pin, same seed) plus `DIVU.exc` (the single upstream divide-by-zero
case) and `SUPERHAND` (hand cases: privilege violations — checked before any EA side
effect and with T=1 —, user-mode-origin exceptions, illegal/A-line/F-line,
group-1 PCs and handlers above $FFFF, STOP, odd vectors, and the flag-clear
halts). The driver also runs multi-instruction exception-mode programs
(`super_run_programs`) through BOTH `test_run_program` (emu_step) and
`emu_reset`+`emu_run` (the product path), plus one-off checks (boot state,
RESET hook, mid-run flag flip). Exclusions: address-error cases only (listed per stem in
`fixtures/vectors/MANIFEST.json`). Super batch = standard record + 12-byte
extension (inactive SP in/out, flag, expected halt/reason); the driver
pokes `st_exc`/`r_osp` around the unchanged `test_run_case`.

## Test vector pipeline

- Source: **SingleStepTests 680x0** JSON (github.com/SingleStepTests/680x0, pinned commit recorded in `host/vectors.py`; vendor the sampled subset + upstream license into `fixtures/vectors/`).
- `host/vectors.py` samples N cases/opcode (default 50, seeded, deterministic), **filtering out**: cases taking any exception (address error, privilege, divide, illegal — detect via exception-vector fetches in the JSON transaction list or final-SR/SSP changes), trace-bit cases, and instructions outside the M1 subset. Only architectural state is kept (regs + RAM byte pairs); prefetch/bus detail is dropped. Retained initial SR is normalized to user mode with the same CCR.
- Binary batch format (LE unless noted), one file per opcode-page or group, ≤ 2048-byte cases fit `buf_case` one at a time:
  - Batch header: `u16 case_count`
  - Per case: `u16 case_len` (includes the 2-byte length field itself; next case at offset + case_len), 16 × `u32` D0–D7/A0–A7, `u32 PC`, `u16 SR`, `u8 n_init_mem`, n × {`u24 addr`, `u8 val`}, `u8 n_exp_mem`, m × {`u24 addr`, `u8 val`}, 16 × `u32` expected regs, `u32` expected PC, `u16` expected SR
- Execution: for each case the host (via harness) writes the case into `buf_case`, calls `test_run_case` (runner entry), which: loads regs/SR/PC, writes init mem pairs via `mem_write8`, calls `emu_step` once, dumps regs/SR/PC + reads expected-mem addresses via `mem_read8` into `buf_result`. Host compares and reports (opcode, case index, field diff).
- The runner (`tests/` side) exports `test_run_case` at a fixed label in the PRG; harness discovers addresses via the ld65 label/map file (`build/runner.lbl`).

## M0 loader

- `host/hunk.py`: parse HUNK_HEADER/CODE/DATA/BSS/RELOC32/END(/DEBUG,SYMBOL skipped). Output: segment list with sizes/memory flags.
- `host/image.py`: lay out segments in guest space (first segment at $010000; 4-byte alignment; BSS zero-filled), apply RELOC32, emit:
  - `image.bin` — flat REU image (sparse-friendly: emit as {offset,len,data} chunks)
  - `manifest.json` — `{"entry": u32, "segments": [{"guest_addr", "size", "kind"}], "stack_top": u32}` (stack_top default $00F000, A7 init)
- `host/fixturegen.py`: build minimal valid Hunk executables from a tiny assembler-free builder (hand-encoded 68k words) — no external m68k toolchain.
- C64 loader (Wave 3): stage image chunks into REU (VICE: preload via harness/REU image file; hardware: Ultimate DOS), init regs (PC=entry, A7=stack_top), `emu_run`, expose result via `buf_result` convention.

## Verification commands (Makefile targets)

- `make host-test` — pytest `host/tests/`
- `make runtime` — ca65/ld65 build of runner PRG + label file into `build/`
- `make vectors` — generate binary batches into `build/vectors/`
- `make vice-test` — run vector batches through VICE via harness (warp allowed: correctness-only; **VICE warp ≠ U64E 48 MHz** — performance claims only via cycle accounting or real hardware)
- `make smoke` — trivial PRG in VICE proving the harness path works

## I/O trap ($BF/$DF) (W7-T, S3.1)

Owner: core (`runtime/core.s` memory API) + chip (`runtime/ops/chip.s`).
Design: `docs/S3_PLAN.md` §1.2–1.3, §3.4. Tests: `tests/run_chip.py`,
`host/tests/test_chip_hotpath.py`, `host/tests/test_chip_ref.py`.

- **Guest model.** Banks `$DF` (custom chips) and `$BF` (CIAs) are no
  longer RAM. Every `mem_read*`/`mem_write*` access whose bank is `$DF` or
  `$BF` goes to `chip.s`, and no REU DMA is issued for it. A 16/32-bit
  access that straddles into or out of an I/O bank is split into bytes, and
  each byte goes to its own bank (REU or chip). The instruction window
  (`fetch_word`) is NOT checked: executing from `$BF`/`$DF` reads the REU
  (in a FETCH_DIRECT variant build, instruction fetch goes through the API
  and does trap). **Other bypass:** HLE DOS bulk transfers
  (`hle_dos.s` `dosio_dma1`) program the REC directly, so a guest DOS
  buffer placed in `$BF`/`$DF` would move its bulk bytes to or from the REU
  underneath, while the first/last-byte fix-ups go through `mem_write8` and
  hit the chip stub. This is acceptable for now: the HLE memory map
  (`rc/guest.inc`) never puts program, heap or RAMDISK memory in those
  banks, and a program that passes an I/O address as a DOS buffer is
  already broken on an Amiga. It becomes a risk once the S3.7 machine
  profile exists; the S3.7 guard is to reject such buffers in `hle_dos`.
- **Where the test sits.** Only where the REC bank is about to be
  programmed: MIO_PROG's bank-changed branch (`cmp #$DF / beq trap / cmp
  #$BF / beq trap`, +8 cycles, never taken for RAM), the cold path
  (`mio_full`, tested before any REC store; the caller's `jmp hiok` then
  takes the bank-changed branch), STRADDLE's slow branch (bank carry
  `$BE→$BF`, `$DE→$DF` → `mio_split`), and `mio_split` per byte (before the
  bank store and the command). Same-bank calls execute exactly the base
  instructions (+0).
- **Invariant.** `m_rbk` never holds `$BF` or `$DF`. An I/O access
  therefore always arrives at the bank compare as a change and can never
  take the same-bank fast path. A trapped warm call may already have
  stored the length and low/high address bytes. These stay consistent with
  the shadow, so the next call is still correct. A trapped cold call
  touches no REC register and leaves `m_rok` = 0, `m_rbk` = 0.
- **Chip ABI** = the memory API's: `m_addr` in, `m_val` LE in/out (a read
  writes only the bytes its size covers), X/Y preserved, clobbers A, `rts`
  to the API's caller. Entries `chip_r8/r16/r32/w8/w16/w32`, which are
  `jmp`'d from the API entries, plus `chip_split` (one byte, from
  `mio_split`).
- **Decode.** Custom registers are `$DFF000–$DFF1FF`, register = `m_addr &
  $1FE`. A word access is one register and a long is two. A byte write
  lands on both halves; a byte read returns the addressed half. CIA-A is
  the odd bytes with A12 = 0 and CIA-B the even bytes with A13 = 0,
  register = `(m_addr+1) & $0F`, always byte-wise. Everything else in the
  two banks is open bus: reads 0, writes ignored, counted in `chip_nopen`.
  This deliberately differs from an A500 and from the boot model, which
  mirror the custom registers and return $FF on undecoded CIA reads
  (S3_PLAN §3.4 L7).
- **Values (S3.1 stub).** Reads return fixed values: VPOSR `$8000`,
  POTINP `$FF00`, SERDATR `$3000`, DENISEID `$FFFF`. The other read-only
  slots (`$000–$01E`) return their shadow word, and write-only registers
  (`$020–$1FE`) return 0. CIA registers return their shadow, except CIA-A
  PRA = `(PRA & DDRA) | ($EB & ~DDRA)`. Not modelled yet: DMACONR (it
  reads its slot, 0), timers, ICR and TOD (S3.3/S3.4).
- **Writes.** Every decoded write is logged. A write to a write-only
  register also lands in the shadow, while a write to a read-only slot is
  ignored there. CIA writes land in `chip_cia`, and a SERDAT write appends
  its low byte to the serial ring.
- **Host-visible state** (labels in `runner.lbl`, LE): `chip_shd` is a
  512 B big-endian register image (index = offset), `chip_cia` 32 B (A
  0–15, B 16–31), and `chip_log` a ring of 64 {u16 id, u16 value} (id =
  offset, CIA-A `$0A00+reg`, CIA-B `$0B00+reg`) with u16 `chip_logn`.
  `chip_ser` is a 256 B ring with u16 `chip_sern`, and u16 `chip_nopen`
  counts open-bus accesses. It is cleared by the boot BSS clear and by
  `chip_reset` (host entry, clobbers A/X), but NOT by `emu_reset`: it is
  machine state, like guest RAM.
- **Link order.** `chip.s` links last (Makefile), so adding it moves no
  other module. core.s CODE size is unchanged ($D34).

## Machine loop (S3.2) (W8-L)

Owner: s3 (`runtime/ops/mach.s`; beam reads and the INTREQ VERTB bit in
`runtime/ops/chip.s`). Design: `docs/S3_PLAN.md` §1.5, §4 "S3.2";
`docs/WAVE8_BRIEF.md` C1–C3. Reference: `host/chipset_ref.MachTime`.
Tests: `tests/run_mach.py` (+ `tests/mach/drv.s`), `host/tests/test_mach.py`.

- **`mach_run`** is `emu_run`'s drop-in: the same entry state (after
  `emu_reset`, regs loaded, `st_halt` = 0) and the same exit contract
  (`st_halt`/`st_reason` as `emu_run` leaves them). It returns only on a
  real halt; events are serviced inside. It calls `mach_init` itself if
  that never ran. Clobbers A/X/Y.
- **Inside `mach_run`:** `$01` = `$35` (I/O in, BASIC and KERNAL out); the
  RAM vectors are installed (NMI → `mach_nmi`, IRQ → `mach_irq`, re-written
  on every entry); CIA #1 timer A, one-shot, is the only unmasked IRQ
  source; `cli` around `emu_run` and the service pass. **On every exit:**
  CIA #1 ICR masked (`$7F`) and acknowledged, **I forced to 1** (whatever
  the caller had: `mach_run` does not restore the caller's I flag), `$01`
  = the caller's value. The RAM vectors stay (harmless with the KERNAL
  in). **The KERNAL's jiffy IRQ is not restored**: CIA #1 timer A is left
  a stopped (or running-out) one-shot with every ICR source masked, so a
  caller that returns to the KERNAL (BASIC, keyboard scan, `$A0–$A2`) must
  re-init CIA #1 itself (timer A continuous, ICR `$81`). Existing
  drivers (`runner.s`, `run_program.s`, HLE) keep `sei`, never call
  `mach_run`, and see no event.
- **Vector invariant.** Whenever `$01` exposes the RAM vectors,
  `$FFFA–$FFFF` holds a valid handler. RAM vectors are written only while
  the ROM vectors are live, or rewritten with identical bytes. Sanctioned
  writers: `mach_vec` (called by `mach_run` under `sei` before it changes
  `$01`, i.e. with the caller's `$01`: ROM live, or identical bytes when a
  caller already runs at `$35` after an earlier `mach_run`) and `c2v_fill`
  (stores `mach_nmi`: identical bytes inside `mach_run`, ROM live outside).
  A caller that is already at a `$01` exposing the RAM vectors (`$35`/`$34`)
  when it enters `mach_run` must already have `$FFFA` = `mach_nmi`, the only
  sanctioned NMI handler: `mach_vec` rewrites `$FFFA/$FFFB` byte by byte,
  and an NMI between the two stores would take a torn vector from any other
  handler, even a valid one (review round 2: 6/120 torn at `$35`). Any IRQ
  handler is fine there, because `mach_vec` writes `$FFFE` under `sei`. Tests: run_mach
  `win` (NMI swept over the prologue, poisoned vector), `c2vnmi` (NMI swept
  over c2v's store and fill at `$35`).
- **Race-free clear.** After `emu_run` returns, under `sei`: `st_halt &=
  $7F`; nonzero → a real halt (S3.3: `HALT_STOP` in exception mode stops
  the CPU instead, see "Interrupt delivery (S3.3)") → exit with `ev_pend` untouched (a halt
  racing an event, `st_halt` = `$81`, returns `$01`, `ev_pend` set). Zero
  → `ev_pend` is copied to `mach_ev` and cleared, `cli`, service,
  re-enter `emu_run`. The next `mach_run` takes a kept `ev_pend` on entry
  the same way (under `sei`, before its catch-up pass), so it never goes
  stale. Tested live (`race2`: IRQ/halt races at s = 0, ≥ 1 genuine race,
  `ev_pend` set only when the boundary really passed, no stray IRQ).
- **IRQ handler** (`mach_irq`, ~40 cycles with the 6502's IRQ entry):
  saves A, reads the CIA #1 ICR (acknowledge); if an enabled source fired,
  `ev_pend |= 1` and `st_halt |= $80`. Touches nothing else: never the
  REC, the REU or guest state. It is valid only with I/O mapped in, which
  holds inside `mach_run` (the c2v `$01` = `$34` fill runs under `sei`).
- **NMI handler** (`mach_nmi`), **the only one** (c2v's fill uses it too;
  review W8-L F1): valid under any `$01` (RAM below `$A000`); saves A and
  `$01`, maps I/O in (`$01 & $F8 | $05`), reads the CIA #2 ICR (never
  leaves /NMI latched), `inc mach_nmis` (u8, wraps: every NMI taken through
  the RAM vector), restores, `rti`. RESTORE and CIA #2 NMIs are dropped.
  RESET (`$FFFC`) is claimed but never written: a reset maps the KERNAL in.
- **Time base.** CIA #2 timer A (φ2, continuous, latch `$FFFF`) cascades
  into timer B (counts A's underflows, latch `$FFFF`): C = `~(TB:TA)`, a
  free-running 32-bit φ2 counter (wraps after 2^32 φ2, ~73 min). `mach_rdc`
  reads a consistent snapshot (TA high byte read around its low byte, TB
  around TA, repeat on a change). CIA #2 port A (VIC bank), DDRs and ICR
  mask are never written; CRA bits 7–6 and CRB bit 7 are kept. Nothing
  else may reprogram the CIA #2 timers after `mach_init` (the runtime does
  not; test tools such as `perf_cycles` and `run_c2v`'s NMI leg do, and
  need a fresh `mach_init`). The CIA #2
  ICR data bits (TA/TB underflows) stay masked; `mach_nmi` reading the ICR
  does not disturb the counter.
- **Guest time.** E = C >> s, s = `mach_s`: a u8 in the PRG's DATA segment
  (label `mach_s` in `runner.lbl`, value 4 at load; not an rc claim, since
  an rc address cannot carry a load-time default), clamped to 16 and
  latched by `mach_init`. A driver overrides it by storing the new shift at
  the `mach_s` address (`runner.lbl`) before `mach_init`, from 6502 code
  (`lda #s / sta mach_s`) or from the host; a change after `mach_init`
  takes effect at the next `mach_init`. One guest frame = 14188 E ticks
  = 14188 << s φ2. The runtime has no φ2 constant: frames/s = φ2 / 14188 /
  2^s. PAL (985248 Hz, VICE `-pal`): 69.44 / 2^s (4.340 at s = 4). NTSC
  (1022727 Hz, the harness's default VICE and **the bench U64E**, whose
  CIAs count φ2 unscaled at 48 MHz [MEASURED HW, W8-H]): **72.08 / 2^s**
  (4.505 at s = 4).
- **Events (S3.2: VERTB only).** `mach_svc`, on entry and after each
  event: for every frame boundary C has passed, `mach_frames` += 1, INTREQR
  bit 5 set (`chip_shd+$1F`), next boundary += frame; then CIA #1 timer A,
  one-shot, := min(next − C, `$FFFF`) (a frame longer than 65535 φ2 takes
  several wake-ups). Frames are counted (one pass, ~85 cycles, per missed
  frame) as long as the absence since the last service pass is below 2^31
  φ2 (~36 min). **Aliasing:** after a service pass next − C is in (0,
  frame], so next − C > frame (unsigned) can only mean C − next wrapped past
  2^31 (a longer absence). The pass then posts one VERTB and restarts the
  frame at C (next = C + frame): the lost frames are not counted, but time
  never stalls, so there is no re-entry bound (the round-0 ~36 min bound is
  gone; test `catch`: 2^31 + 1 frame of absence). S3.3 (W9-I) adds the
  client slots, the VERTB hook and delivery after each pass ("Event seam",
  "Interrupt delivery"); no CIA-A/B guest timers (S3.4), no copper/refresh
  (S3.5).
- **Beam** (`mach_beam`, from `chip.s` VPOSR/VHPOSR reads once `mach_on`):
  t = C − frame start (φ2 into the frame; t ≥ frame length, i.e. the
  boundary passed but not serviced yet, reads the last tick), e = t >> s,
  cc = 5 e, V = cc / 227 (0–312; line 312 has H 0–115), H = cc mod 227.
  **VPOSR = `$8000 | V8`** (LOF and Agnus ID of the S3.1 stub),
  **VHPOSR = `(V & $FF) << 8 | H`**. A long read (`move.l VPOSR`) latches
  one beam for both words. Before `mach_init` (`mach_on` = 0) both return
  the S3.1 values (VPOSR `$8000`, VHPOSR its shadow word).
- **INTREQ VERTB** (S3.2; **superseded by S3.3**, W9-I): INTENA/INTREQ
  writes now apply the full SET/CLR rule to every bit, `mach_on` or not,
  and VERTB is posted through `mach_post` (see "Interrupt delivery
  (S3.3)").
- **Claims.** rc zp s3 `$95–$99`: `ev_pend` (bit 0 = CIA #1 timer A; S3.3
  adds bit 1 = a trap-context kick, "Event seam"),
  `mach_frames` (u32 LE); none is written by the KERNAL IRQ. rc ram s3
  `$FFFA–$FFFF`: `mach_vnmi`, `mach_vrst`, `mach_virq`. CIA #1/#2 register
  equates in the rc sys block. Linker state in `runner.lbl`: `mach_s`
  (DATA, 4 at load), `mach_on`, `mach_nmis`, `mach_bv`/`mach_bh`,
  `mach_next`/`mach_now` and the entry `mach_rdc` (test visibility).
- **Link order.** `mach.s` links after `chip.s` (Makefile), so no other
  module's code moves; `core.s` is unchanged.

## Event seam (S3.3/S3.4) (W9-I)

Owner: s3 (`runtime/ops/mach.s`). Design: `docs/WAVE9_BRIEF.md` C1,
`docs/S3_PLAN.md` §1.5. Tests: `tests/run_irq.py` legs `etime`, `etlive`,
`arm`, `periodic`, `trap`, `race`, `post`, `out` (+ `tests/irq/drv.s`, a
fake event source) and `--mutants`. Clients: `cia.s` (S3.4); `mach.s` has
no link dependency on any client.

- **Guest time.** `mach_etime` (JSR; clobbers A, X; keeps Y) → `mach_et`
  (u32 LE) = the low 32 bits of `(chi:C) >> s`, where C is the S3.2 phi2
  counter and `mach_chi` (u16) counts its wraps (`mach_rdc` compares C's top
  byte with the previous read's: lower = a wrap). It is THE time base of
  every guest clock. It wraps at 2^32 E ticks, so compare E times by signed
  difference. A wrap is counted only if C is read at least once per 2^31
  phi2 (~36 min): `mach_run` reads it at every pass (at least every 65536
  phi2); a caller that stays away from `mach_run` longer loses the wrap
  (as S3.2's frame count does). `mach_init` restarts E at 0 (`chi` := 0).
- **Slots.** `MACH_NSLOT` = 8 (exported). Slot 0 is VERTB (mach-internal,
  in phi2, as S3.2); slots 1–7 belong to the client (`cia.s` assigns them).
  State: `mach_dl` (u32 deadline per slot, 4 bytes × slot), `mach_am`
  (armed mask, bit n = slot n).
- **`mach_arm`** (A = slot 1–7, `mach_at` = absolute E deadline u32 LE;
  clobbers A, X; keeps Y): arms or re-arms (replaces the deadline). Slot 0
  and A ≥ 8 are ignored. **`mach_disarm`** (A = slot; clobbers A, X):
  disarms, a no-op when not armed. Both are callable from the trap context
  (inside `emu_run` under `mach_run`, IRQ enabled), from callbacks, and
  outside `mach_run` (no kick; the next `mach_run`'s entry pass sees it).
  `mach_init` disarms every slot (callbacks are kept): init the time base
  before the client arms.
- **Firing.** Every service pass (`mach_svc`: the entry pass of
  `mach_run`, and the pass after each event or kick) first counts VERTBs,
  then takes ONE E snapshot and fires every armed slot 1..7 (in slot order)
  whose deadline is ≤ the snapshot: the slot is disarmed, then its callback
  runs. Each slot fires at most once per pass, so a callback that re-arms
  in the past is fired by the next pass, not this one (no livelock). A
  deadline never fires early. The comparison uses the pass's private copy
  of that snapshot (`mach_es`): a callback's own `mach_etime` moves
  `mach_et`, not the firing decision (fix round 1, W9-C review). Then the
  pass programs CIA #1 timer A
  (one-shot) for the earliest of the next frame boundary and every armed
  deadline: wait = `(d − et) << s` − the phi2 already spent in tick `et`
  (the first phi2 of tick d is `d << s`), at least 1 (a deadline at or
  before the snapshot gives 1), at most
  `$FFFF` (a farther one is reached through the VERTB wake-ups).
  **Forward progress** (fix round 1, W9-C review): when the pass fired a
  client slot, or leaves an interrupt to deliver, the wait is at least
  `MACH_MINW` = `$0800` phi2 (`mach_wmin`), so the guest runs before the
  next pass even if a client re-arms "now" (`run_irq` leg `progress`: an
  always-due slot, ~180 firings in ~830 k phi2 at s = 0, the guest loop
  finishes; round 0 livelocked). A firing may therefore come up to
  `MACH_MINW` later than its deadline when another slot fired just before;
  the VERTB count is unaffected (it counts boundaries). Latency
  [MEASURED VICE, `run_irq` `arm`/`periodic`/`trap`]: a firing comes at
  most `4096 >> s` E ticks late (an IRQ, the instruction in flight and one
  pass).
- **Trap-context kick.** `mach_arm` called while `mach_run` runs the
  guest (`mach_in` ≠ 0) and no pass runs (`mach_insv` = 0) **kicks**
  (`mach_post` and a SET write to INTENA/INTREQ kick only when they make an
  interrupt deliverable, `mach_ichk`, see "Interrupt delivery"): under `php`/`sei`, `ev_pend |= 2` (bit 1, `MACH_EV_KICK`)
  and `st_halt |= $80` — exactly what `mach_irq` does — so `emu_run`
  returns at the next instruction boundary and the pass re-programs the
  one-shot. Race-free against `mach_irq` by construction: the IRQ handler
  touches only `ev_pend`, `st_halt` and the CIA #1 ICR, the slot state is
  never touched in the IRQ, and both read-modify-writes run under `sei`.
  A kick costs one pass (~400 cycles [EST, static]) per arm. Outside
  `mach_run`, `mach_in` = 0, so a client's direct-API use (tests) never
  sets `st_halt`.
- **Register convention (supervisor R4).** Every seam routine clobbers
  A/X/Y unless stated here; the exceptions are `mach_etime`, `mach_arm`,
  `mach_post` (keep Y). Callbacks may clobber A/X/Y.
- **`mach_cbv` lifetime (R1).** Zero at boot (BSS); never cleared by
  `mach_init` or `mach_run`'s lazy `mach_init`, so a client fills it once at
  its own init.
- **E is a true u32 (R3)** at every s (tested at s = 0, 4, 8, 9, 16 across a
  C wrap, `run_irq` leg `etime`).
- **Callbacks.** `mach_cbv`: 2 bytes per slot (LE address; high byte 0 =
  none), RAM, filled by the client at its own init. A callback runs in the
  service pass, never in the IRQ, with `$01` = `$35`; it may call
  `mach_etime`, `mach_arm`, `mach_disarm`, `mach_post` (no kick inside a
  pass), must not call `emu_*` or `mach_run`, and returns with `rts`
  (A/X/Y clobbered). It must not use `mach_t`/`mach_c` (beam scratch).
- **VERTB hook.** Slot 0's callback runs after every VERTB the pass posts
  (catch-up passes call it once per frame), so CIA-A's TOD can count
  vsyncs. Default none.
- **`mach_post`** (A = INTREQ bit 0–14; clobbers A, X; keeps Y): sets that
  bit in INTREQR (`chip_shd+$1E`, big-endian) at once (a guest INTREQR read
  sees it). Delivery (S3.3) happens at the next pass; from the trap context
  a post that makes an interrupt deliverable kicks (`mach_ichk`), so it is
  taken at the next instruction boundary. VERTB uses the same path (inside
  the pass).
- **`mach_hold`** (u16 LE, RAM): the INTREQ bits a **level** source still
  asserts. Every INTREQ write (`chip.s`) ORs it back after the SET/CLR, so
  a guest CLR of a held bit leaves it set — `chipset_ref.Chipset.
  custom_write`'s `ciaa.update()`/`ciab.update()` re-raise. The client keeps
  bit 3 (CIA-A) / bit 13 (CIA-B) of `mach_hold` equal to its ICR `IR`
  condition (data & mask ≠ 0) and `mach_post`s on the rising edge.
- **Host-visible** (`runner.lbl`): `mach_et`, `mach_at`, `mach_dl`,
  `mach_am`, `mach_cbv`, `mach_hold`, `mach_chi`, `mach_in`, `mach_insv`;
  entries `mach_etime`, `mach_arm`, `mach_disarm`, `mach_post`,
  `mach_kick`. All BSS (no rc claim): cleared at boot, not by `emu_reset`.

## Interrupt delivery (S3.3) (W9-I)

Owner: s3 (`runtime/ops/mach.s`; the INTENA/INTREQ paths of
`runtime/ops/chip.s`). Design: `docs/WAVE9_BRIEF.md` C2, `docs/S3_PLAN.md`
§1.5 and §4 "S3.3". Reference: `host/chipset_ref.Paula` (`setclr`,
`level`, `IRQ_LEVELS`). Tests: `tests/run_irq.py` legs `setclr`,
`deliver`, `masked`, `levels`, `nest`, `storm`, `stop`, `oddvec` and
`--mutants`; `host/tests/test_irq.py` (the level tables vs the model).

- **INTENA/INTREQ** (`chip.s`, every write, `mach_on` or not): the SET/CLR
  rule (bit 15 set: OR in bits 0–14, else clear them) applied to INTENAR
  (`chip_shd+$1C`) / INTREQR (`$1E`); bit 15 reads 0. A byte write lands
  on both halves (OCS), so it is a SET/CLR of `v:v`. After an INTREQ write
  `mach_hold` is ORed back (level sources). The raw word still lands in the
  write-only slot (`$09A`/`$09C`) and the log. Reads: INTENAR/INTREQR
  return the shadow (S3.1 read-only rule). This replaces S3.2's
  VERTB-only, `mach_on`-gated INTREQ rule; VERTB is posted with
  `mach_post` (bit 5).
- **IPL** (`mach_lvl`): 0 unless INTEN (INTENA bit 14); else the level of
  the highest source in `INTENA & INTREQ & $3FFF` by
  `chipset_ref.IRQ_LEVELS` (13 → 6; 12, 11 → 5; 10–7 → 4; 6–4 → 3; 3 → 2;
  2–0 → 1).
- **Delivery** (`mach_dlv`, exception mode only: `st_exl` ≠ 0): after the
  entry pass and after every service pass, if IPL > SR's I2–I0, raise
  autovector 24 + IPL: stacked SR = SR, stacked PC = `r_pc` (the next
  instruction), S := 1, T := 0 (A7 switches to the SSP from user mode),
  frame at SSP − 6, PC := (vector × 4); then I2–I0 := IPL. Through core.s's
  `exc_group2`/`exc_raise` unchanged (no core.s change): `mach_raise`
  stores the 6502 SP in `st_sp6` (the depth `emu_run`'s step loop runs at,
  one `jsr` below `mach_run`) and jumps to `exc_group2`; `st_halt` is preset
  to `$40` so `dispatch_next` returns right after the frame instead of
  running the handler. Back in `mach_dlv` (under `sei`): `st_halt & $7F` =
  `$40` → keep only bit 7 (an event that came meanwhile) and go on; = 1 →
  an odd vector halted `HALT_ADDRESS` (frame written): `mach_run` exits with
  it. One interrupt per pass; a higher level arriving in a handler nests
  (I2–I0 = the running level masks equal and lower ones: the compare is
  `>`). `mach_nirq` (u32) counts deliveries.
- **Recognition latency.** A pass happens at every event and kick. A SET
  write to INTENA/INTREQ, or a post from the trap context, calls
  `mach_ichk`: when it makes an interrupt deliverable now (exception mode,
  IPL > I2–I0) it kicks, so the interrupt is taken at the next instruction
  boundary, as on a 68000 (`levels` pins the stacked PC = the instruction
  after the write). It costs the trap path only (`chip.s`, never the hot
  path); CLR writes never kick. **An IPL drop** by MOVE to SR / RTE / ANDI
  to SR (core.s, frozen, no SR-write hook) **is recognised within
  `MACH_POLL` phi2 while an interrupt is pending** (fix round 1, review
  F2): every pass in exception mode that finds `mach_lvl` ≠ 0 — pending and
  enabled, whether masked by I2–I0 or queued behind the one this pass
  delivers — clamps the CIA #1 one-shot to `MACH_POLL` = `$0400` phi2
  (`mach_pclamp`). With nothing pending it is recognised at the next event,
  which is harmless: nothing can be delivered. So a lower level is never
  starved by a recurring higher one (`starve`, `kick`), and a handler that
  never acknowledges its request is re-entered right after its RTE, as on a
  68000 (`storm`: 20 re-entries within 2 frames at s = 4). Cost: one pass
  per `MACH_POLL` phi2 while something is pending, `docs/PERF.md` (i).
  Exact recognition at the RTE/MOVE-to-SR instruction would need a core.s
  SR-write hook: backlog, not Wave 9.
- **STOP** (exception mode): `emu_run` halts `HALT_STOP`; `mach_run`
  marks the CPU stopped (`mach_stp`), clears `st_halt`, and delivers at
  once if an interrupt is already deliverable. Otherwise, if one can still
  come (`mach_wk`: INTEN and a source enabled above I2–I0 that VERTB posts
  (bit 5), or any source enabled above I2–I0 while a client slot is armed or
  the slot-0 hook is installed), it **waits** with `cli` for the next event
  (no fast-forward: guest time runs at the dilated rate while stopped) and
  services it; a delivery resumes the CPU (stacked PC = the instruction
  after STOP). If nothing can wake it (IPL 7, nothing enabled above the
  IPL, or only client sources with nothing armed and no hook), `mach_run`
  returns `HALT_STOP` (`st_halt` = 1, `st_reason` = 4) at once, as in S3.2.
  To resume, the caller must clear `st_halt` (`mach_run`'s entry
  precondition is `st_halt` = 0) and call `mach_run` again: execution goes
  on after the STOP. **Unbounded STOP (review F4, intended):** while any
  client slot is armed or the slot-0 hook is installed (S3.4's `cia_init`
  always installs it), a STOP with an enabled source above I2–I0 returns
  only through a delivered interrupt; `mach_run` does not give the host
  control back meanwhile. That is what a 68000 does: STOP ends only on an
  interrupt (or reset). On entry the halt path goes through the event path
  (fix round 1, review F1): a one-shot that fired during the STOP or
  between `halt_now` and the `sei` is serviced, never dropped
  (`stoplost`).
- **Plain mode** (`st_exl` = 0): nothing is delivered and `HALT_STOP`
  cannot occur; the INTENA/INTREQ registers behave as above.

