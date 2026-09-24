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
- No supervisor mode, no interrupts, no exceptions in M1. Trap-like events (TRAP, DIV-by-zero, A-line/F-line, illegal) set `st_halt` with a reason code and return from `emu_step` — hooks for M2 HLE.
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
