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

**Vector sets.** `build/vectors` (golden, 5150) is unchanged and runs with
the flag clear. `python3 -m host.vectors --set super --offline --out
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
