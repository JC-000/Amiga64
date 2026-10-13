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
- `fetch_word` — read guest word at PC into `op_lo`/`op_hi` (also mirrored to `t_tmp` for extension words per handler need), PC += 2. Handlers use it for extension words. Clobbers A and X. Served from a cache of 256-byte page-aligned instruction windows (W12-C, "Code-window cache" below: a page change switches windows or refills one with one REU DMA); `emu_reset` and any `mem_write*` overlapping ANY cached page invalidate that window. A path that writes guest memory WITHOUT going through `mem_write*` calls `code_invpg` for each page it wrote (e.g. a blitter D row) or `code_invall` (e.g. REU staging or a resume), or `emu_reset`. (`w_inv`, ZP $82, now only marks the current window; setting it no longer covers the other windows.)
- Memory API (guest BE ↔ ZP LE conversion at this boundary, v0 = direct per-call REU DMA; internals may later add windowing/TLB — **only these entry points are stable**):
  - `mem_read8` / `mem_read16` / `mem_read32`: `m_addr` → `m_val`
  - `mem_write8` / `mem_write16` / `mem_write32`: `m_addr`, `m_val` →
  - `mem_rec_dirty`: REC-ownership seam — any code that programs the REC outside the memory API calls it before the next memory-API call (clobbers A). Binding rules: `docs/WAVE4_BRIEF.md` C1.
- EA resolution: `ea_calc` — decodes mode/reg bits (X = 6-bit mode|reg field), leaves address in `t_ea`; `ea_load`/`ea_store` sized variants (size in A: 0=B,1=W,2=L). Covers all 68000 modes incl. d8(An,Xn) brief extension and abs.W/L, PC-relative, immediate.
- Sized ALU primitives (all CCR logic lives HERE, not in group handlers): `alu_add`, `alu_sub`, `alu_cmp`, `alu_and`, `alu_or`, `alu_eor`, `alu_not`, `alu_neg`, `alu_addx`, `alu_subx`, `alu_ext`. Inputs `t_op1`,`t_op2`, size in A; result `t_res`, CCR updated per 68000 PRM semantics (X only where the 68k sets it).
- `dispatch_next` — handlers `jmp` here when done (it is the top of the step loop).

## Code-window cache (W12-C)

`runtime/core.s`, labelled `; W12-C`; measurements in `docs/PERF.md` (m).

- **Layout.** `CWIN_N` = 7 windows (`-D CWIN_N=1..7`), window k at `cwin_base` + k·$100 = $A900 + k·$100 (rc `ram.inc`, c64.cfg area CWIN $A800–$B0FF, RAM under BASIC ROM). `cwin_map` $B000–$B0FF (also the last window's guard byte). The cache's lookup/refill code `cw_look` is segment `CWCODE` at $A800 (asserted to end below `cwin_base`). The directory (`cw_tlo`/`cw_thi` tags = guest address bits 15-8/23-16, `cw_val`, the FIFO hand `cw_hand`, `cw_mok`) is MAIN BSS; ZP `w_pg0`/`w_pg1`/`w_inv` describe the **current** window, the one the four hit-path loads (`dg_w0`/`dg_w1` in `dispatch_go`, `fw_w0`/`fw_w1` in `fetch_word`) point at.
- **Hit path unchanged.** The per-instruction test (PC page = `w_pg`, `w_inv` = 0) and the two loads are the base instructions; a window switch re-points the loads' operand high bytes (self-modified, like `jvec`). Only the miss path changed: it banks BASIC out (below) and jumps to `cw_look`.
- **Miss.** `cw_look`: `cwin_map[page-low]` bit 7 clear → refill. Else the map's hint window, then a scan of the directory: a valid window with PC's page becomes current (no DMA). A refill takes the window at the FIFO hand (`cw_hand`, counting down), drops its old page from the map, issues one 256-byte REU DMA (`mem_rec_dirty` semantics as before: `m_rok` := 0 first) and enters the new page.
- **`cwin_map[p]`.** Bit 7: some valid window has page-low p. Bit 6: some valid window has page-low p+1. Bits 5-3: hint, the window last filled with page-low p (a guess; always checked against the tags). Bits 2-0: how many valid windows have page-low p. Bits 7/6/2-0 describe exactly the valid windows, and an entry with count 0 has no hint bits (invariant; `run_cwin` smc/map).
- **Write invalidation (`WINCHK_*`, every `mem_write*`).** One map read through a self-modified absolute load (the memory API keeps X and Y): byte write → slow path only if bit 7; word/long → only if the byte is nonzero. Common path 13 cycles (was 9 for bytes, 16 for words/longs). Slow-path cases (VICE, extra cycles per write; PERF (n)): page-low p+1 cached (bit 6, a word/long that does not spill) ~+100; page-low p cached in another bank (the map ignores the bank; one window with that page-low: the hint decides) ~+156; a real hit drops the window. A slow path entered with BASIC ROM in (a call from outside a run, e.g. `test_load_chunk` at $37, where the map byte was a ROM byte) banks BASIC out for the check and restores the caller's `$01`: such calls pay the slow path for every write whose ROM byte says "maybe" (host staging only; about half of the staged bytes on the AROS staging). The slow path (`cw_ww`) checks the start page and, for a write that spills past the page end, the next page (24-bit wrap $FFFFxx → $0000xx), by tag; each matching valid window is dropped (`cw_kill`: invalid, map updated; the current one also sets `w_inv`). Clobbers A only, as before.
- **`code_invpg`** (export): A = page bits 15-8, X = bits 23-16: drop every cached window of that page. Clobbers A, X; keeps Y. Reads and updates the map: call it with BASIC out (BCODE callers have it out). Never touches `$01`.
- **`code_invall`** (export, interface I1 for W12-T's resume): invalidate every window; the next fetch refills. Clobbers A, X; keeps Y. Writes the map but never reads it, so it is correct with BASIC ROM in. `emu_reset` calls it (it replaces `w_inv := 1`). The first call since boot clears the whole map (`cw_mok`).
- **Banking ($01).** The windows and the map are RAM under BASIC ROM. The miss path banks BASIC out when it is in (LORAM and HIRAM both set: `dec $01`, `$x7 → $x6`, the `blt_w` idiom) and **leaves it out**; apart from that, only the write-check slow path (`cw_ww`) touches the CPU port: it banks BASIC out for the check and restores the caller's `$01` (rev-cwin round 2). Inside `mach_run` $01 = $35 already. Invariant: while a window is valid and the CPU is inside `emu_run`/`emu_step`/`mem_write*`, BASIC is out — every $01 restorer that runs inside a run (`blt_w`, `c2v`, `disp`, `mach_nmi`) restores a value saved during that run. **Caller rule:** a caller that maps BASIC in again after a run (e.g. `mach_run` returning to a `$37` caller) calls `emu_reset` or `code_invall` before the next `emu_step`/`emu_run` or before relying on `mem_write*` invalidation. Every runner and driver in the tree does (they call `emu_reset` per run). Callers of `emu_run`/`emu_step` from `$37` get `$36` back.
- **Page ends.** Every fetch is one word at an even PC, so the hit path reads `base+X` and `base+X+1` with X <= $FE: inside the window. After the last word of a page, `wrap` bumps the PC page and the next word (an extension word or the next opcode) misses and comes from that page's own window. No window keeps a 257th byte; byte 256 (the next window's byte 0, or `cwin_map[0]` for the last window) is read only at an odd PC, an address error on the 68000. Test: `run_cwin` smc/straddle_ext and straddle_long, with both pages in non-adjacent windows.
- **Writers that bypass `mem_write*`.**
  - blt D rows: `code_invpg` per row (`blt_dpage`).
  - dosio STASH into guest RAM: `dosio_stash_guest` only; it re-stores the first and last byte through `mem_write8` (transfers <= 256 bytes, so at most 2 pages).
  - hle_mem (MEMF_CLEAR) and `test_load_chunk`: `mem_write*`.
  - Host REU staging (SocketDMA, mboot's `aros_fill`): before `emu_reset`.
  - W12-T resume and any UCI REU load into guest memory: `code_invall`.
  - c2v/disp STASH only into the $DF LUT bank (trapped, never executed).
- **Replacement: FIFO.** On the AROS boot's page trace (boot model, to m3) FIFO leaves 1.0% more refills than LRU at N = 7 and costs nothing on a window switch; `build/evidence/w12-cwin/policy.txt`.
- **Counter build** (`-D CWIN_COUNT`, tests only): `cw_nins` (instructions, exception-mode loop), `cw_nmis` (miss-path entries), `cw_nref` (refills), `cw_nkil` (windows dropped by writes), u32 each, consecutive; `cw_nslw` (write-check slow paths, u32, exported).
- **Tests:** `tests/run_cwin.py` (legs smc, invall, blt, bank, bench, bytew, aros; `--mutants`, 10), `tests/smc.py`, `host/tests/test_cwin_hotpath.py`.

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
  and does trap). **Other bypass, closed (W10-V fix round 1, F2):** HLE
  DOS bulk transfers (`hle_dos.s` `dosio_dma1`) program the REC directly,
  so `dosio_xfer_setup` now refuses a Read/Write whose transfer touches
  bank `$BF` or `$DF`: the bytes D2 .. D2+n−1 it would move, n = the
  length the loop moves (Read: min(size − pos, D3), so a Read near EOF is
  judged by what lands; Write: D3), 24-bit, wrapping past `$FFFFFF`
  (n ≥ 16 MB covers everything; a wrap that reaches `$BF` needs n > 12 MB,
  beyond the 2 MB RAMDISK, so it is defensive only): D0 = −1, IoErr = 212
  (ERROR_OBJECT_WRONG_TYPE), no DMA. run_dos stays 762/762. The CPU's `$BF`/`$DF` trap and this
  buffer check together keep the guest out of the REU under the I/O
  banks, where the palette LUT `disp_lut` lives (test: `tests/run_pf.py`
  leg `dosbuf`).
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
  PRA = `(PRA & DDRA) | ($EB & ~DDRA)`. DMACONR: see "Blitter (S3.6)"
  (W11-B; it read its slot, 0, before). Timers, ICR and TOD: S3.3/S3.4.
- **Writes.** Every decoded write is logged. A write to a write-only
  register also lands in the shadow, while a write to a read-only slot is
  ignored there. CIA writes land in `chip_cia`, and a SERDAT write appends
  its low byte to the serial ring.
- **Host-visible state** (labels in `runner.lbl`, LE): `chip_shd` is a
  512 B big-endian register image (index = offset), `chip_cia` 32 B (A
  0–15, B 16–31), and `chip_log` a ring of 64 {u16 id, u16 value} (id =
  offset, CIA-A `$0A00+reg`, CIA-B `$0B00+reg`) with u16 `chip_logn`.
  `chip_ser` is a 1024 B ring (`CHIP_SERSZ`, exported; byte k at slot
  k mod 1024, stored before the count is bumped; 1024 divides 65536, so
  the slot stays continuous across the count's wrap) with u16 `chip_sern`
  (all bytes ever written, mod 65536), and u16 `chip_nopen` counts
  open-bus accesses. The ring stays in MAIN BSS: the host reads it over
  REST/the monitor, and RAM under BASIC (BBSS) is not host-readable on the
  C64 Ultimate. Host readers take the size from `CHIP_SERSZ` (checked
  against `chip_logn - chip_ser`), never a constant. It is cleared by the boot BSS clear and by
  `chip_reset` (host entry, clobbers A/X), but NOT by `emu_reset`: it is
  machine state, like guest RAM. Store-before-count is checked statically
  (`host/tests/test_chip_ser_order.py`: every path from `ser` stores
  exactly once into `chip_ser` before `sinc` bumps the count); no dynamic
  test observes the order (the final state cannot show it).
- **Link order.** `chip.s` linked last when it was added (W7-T), so it
  moved no other module then; since then `cia.s`, `cop.s`, `blt.s`,
  `disp.s` and `mach.s` link after it (Makefile), so growing `chip.s`'s
  CODE or BSS moves them (c64u ser-ring: CODE +43 B, BSS +768 B; modules
  before `chip.s` and BSS before `chip_shd` keep their addresses). core.s
  CODE size is unchanged ($D34).

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
  **Forward progress** (fix round 2, replaces round 1b's `MACH_MINW`
  floor): when a pass leaves an armed slot due now (`mach_wake` chose the
  wait 1 for it), it sets `mach_fs`, and `mach_run` then runs exactly ONE
  guest instruction with `emu_step` (core.s's single-step entry, under
  `sei`, `st_halt` cleared first so the step really runs; `ev_pend` keeps
  any event, which the event path then services) before the next pass. So
  a client that re-arms "now" cannot livelock the guest (`run_irq`
  `progress`: an always-due slot, 802 passes for the 801-instruction loop,
  one instruction each), and there is no time floor, so a periodic client
  re-arming from its own deadline keeps a bounded lateness when its period
  exceeds a pass plus one instruction (`lag`: P = 3000 phi2 at s = 0,
  max 2333 late; the round-1b floor drifted to 50k). 
  **Wave 13 (rev-inp round 4):** the frame hooks (`inp_frame`, W13-D's)
  raised the shortest period a pass can serve at s = 0, screen on (VIC
  badlines take ~8% of φ2), from < 3000 to ≈ 3050-3100 φ2.
  - Measured with the `lag` leg's ring log, 160 re-arms per run
    (`build/evidence/sup-lag/probe_*.log`):
    - merged P = 3000 drifts: slope +7116 to +7906, max 9.4-10.4k;
    - merged P = 3100 is bounded: max 2830-3451;
    - merged P = 3200 and 3400 are bounded: max ≤ 3218.
  - The ring log stores every entry, about 50 cycles per callback more than
    the old log's early exit once full. With it the base PRG (5eb3cd0)
    also drifts slowly at P = 3000 (slope +1051 to +1660); it does not at
    3200.
  - The old 30-re-arm window hid the drift: "2921-3485 over 7 runs" were
    fresh-session samples of a drifting series.
  - At s = 3 (the AROS profile on the Ultimates) one φ2 is 1/8 E tick, so a
    pass costs 8× fewer E ticks per period than at s = 0 (16× at VICE's
    s = 4).
  - **Frame-hook budget** (rev-inp, 2026-10-09): further work on the fhook
    path stays safe up to about 300 extra cycles at idle. A 600-cycle hook
    sits at the P = 3200 edge (slope +74 to +1300), so the lag leg would
    flake; re-derive P before adding that much.
  - The leg now runs at **P = 3200**, 240 re-arms, with the screen forced
    on. It gates the max of the logged first 16 and last 16 entries
    (≤ 4096) and the drift: the mean of the last 10 minus the mean of the
    first 10 must be ≤ 700 φ2.
    - Bounded series measured: max 2378-3391, slope −461 to +389, on base,
      merged and with a 300-cycle hook delay.
    - Drifting series: merged at P = 3000, slope ≥ +11254; mutant
      `hook800` (+800 cycles per frame hook), ≥ +12586.
    - A 600-cycle hook delay sits at the edge: +74 to +1300.
  - The gate runs with the input override (`inp_src` = 1). The real CIA1
    scan (+111 cycles per frame hook) is run once more at the same P and
    recorded, not gated ("Input (joystick -> mouse)"). A halt in that step
  (HALT, STOP, an odd vector) leaves through the normal halt path, and every
  `mach_run` exit rule holds. A delivery does not force a step: a higher
  level pending after a delivery nests before the handler's first
  instruction, as on a 68000 (`raiseev`), and deliveries raise the IPL, so
  at most 6 can follow without an instruction.
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
  delivers — clamps the CIA #1 one-shot to the poll interval
  (`mach_pclamp`). **W11-A (supervisor grant, HIGH-1):** the interval is
  max(`MACH_POLL` = `$0400`, 256 E << s) phi2, at most `$FFFF` (s ≤ 2:
  `$0400` as before; s = 4: `$1000`; s = 6: `$4000`; s ≥ 8: `$FFFF`), i.e.
  a bounded latency in GUEST time (≤ 256 E ticks for s ≥ 2) instead of a
  fixed phi2 time: a fixed 1024-phi2 poll let a level-3 handler running
  with a request pending under its IPL lose ~2/3 of a 1 MHz host to passes,
  outlast the frame and livelock AROS's interrupt dispatcher at s ≤ 6
  (`tests/aros_boot.py` leg `poll`, "Machine profile and AROS boot").
  Effect on run_irq at s = 4 (bounds unchanged, before → after, verbose
  run, `build/evidence/w11-aros/fix1/irq_bounds_*.txt`): `storm` 20
  re-entries in 2 → 1 frames; `kick` misses 52 → 61 of ~1,370 VERTBs,
  lost 0 → 0. A "miss" is a SOFTINT delivery whose stacked PC is not the
  instruction after the SET write, i.e. one that VERTB's racing level 3
  took first, so SOFTINT came only after the higher handler's RTE, by the
  poll; "lost" (a SET never delivered) stays 0.
  With nothing pending it is recognised at the next event, which is
  harmless: nothing can be delivered. So a lower level is never starved by
  a recurring higher one **while the higher source's period exceeds the
  poll interval** (s ≤ 2: 1024 phi2; s = 4: 256 E = 4096 phi2; s = 6: 256 E;
  s ≥ 8: 65535 phi2, ≤ 128 E at s = 9) (`starve`, `kick`), and a handler that never
  acknowledges its request is re-entered right after its RTE, as on a
  68000 (`storm`: 20 re-entries within 2 frames at s = 4). Cost: one pass
  per poll interval while something is pending, `docs/PERF.md` (i).
  **Which IPL drops are seen at once:** a SET write to INTENA/INTREQ and a
  post (`mach_ichk` kick, `runtime/ops/chip.s:528`,
  `runtime/ops/mach.s:559`): next instruction boundary; STOP (the halt path
  `runtime/ops/mach.s:294`–`304` takes the event path, whose pass is
  followed by `mach_dlv`, `runtime/ops/mach.s:276`): at once. RTE, MOVE to
  SR, ANDI/EORI to SR (core.s, no SR-write hook): within one poll interval
  (≤ 256 E ticks at s ≥ 2, ≤ 1024 phi2 at s ≤ 2) plus the host work of the
  recognition itself (the pass and the exception frame, fixed in phi2:
  ~300 E at s = 4 on a 1 MHz host, < 10 E at s = 9 or at 48 MHz), once a
  pass has seen the request pending (a request POSTED by a pass, VERTB or
  a CIA, always is; one SET by the CPU while masked is clamped only from
  the next event on, ≤ one VERTB wait). Pinned by `tests/aros_boot.py`
  leg `lat` (s = 4: 543–563 E against a TEST bound of 640; s = 9:
  127–128 E against 320) and its mutant `poll8` (s = 4 entry 8× too long:
  2031 E). The test bounds are not worst cases: t0 is taken after the guest
  has seen the posted bit, so an arbitrary drop can come somewhat later
  than the measured ~560 E; the leg catches a 2× regression of the s = 4
  interval (~810 E) but not ~1.3×. Exact
  recognition there would need a core.s SR-write hook: backlog.
- **STOP** (exception mode): `emu_run` halts `HALT_STOP`; `mach_run`
  marks the CPU stopped (`mach_stp`), clears `st_halt`, and delivers at
  once if an interrupt is already deliverable. Otherwise, if one can still
  come (`mach_wk`: INTEN and a source enabled above I2–I0 that VERTB posts
  (bit 5), or any source enabled above I2–I0 while a client slot is armed,
  the slot-0 hook is installed or (W11-A, C4.2) the frame hook `mach_fhook`
  is installed, since the copper writes INTREQ in it), it **waits** with
  `cli` for the next event
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

## CIA-A/B (S3.4) (W9-C)

Owner: cia (`runtime/ops/cia.s`; the CIA path of `runtime/ops/chip.s`
routes to it). Design: `docs/S3_PLAN.md` §1.5, §4 "S3.4";
`docs/WAVE9_BRIEF.md` C3. Reference: `host/chipset_ref.CIA` (wired as
`Chipset`), plus `cia_lines`/`cia_line_e`/`cia_tod_kprime`/`CIA.tod_ticks`.
Tests: `tests/run_cia.py` (+ `tests/cia/shim.s`), `host/tests/test_cia.py`.

- **Decode** is unchanged (CONTRACTS "I/O trap"): CIA-A = odd bytes with
  A12 = 0, CIA-B = even bytes with A13 = 0, register = `(m_addr+1) & $0F`,
  every access byte-wise, writes logged (`$0A00`/`$0B00` + reg). chip.s
  hands the byte to `cia_read` (X = register index 0–15 CIA-A, 16–31
  CIA-B → A) / `cia_write` (X = index, A = value); both keep Y, clobber X.
  The `chip_cia` shadow is gone.
- **Semantics** = `chipset_ref.CIA`, byte for byte: ports (CIA-A PRA pins
  `$EB`, CIA-B PRA and both PRB `$FF`, `(PR & DDR) | (pins & ~DDR)`),
  timers A/B (continuous / one-shot, force load = CR bit 4 (never read
  back), a TxHI write loads a stopped timer and loads + starts a one-shot,
  underflow on the tick after 0 then reload, latch 0 underflows every tick,
  TB INMODE 0 = E ticks, 1 = CNT (nothing), 2/3 = TA underflows), ICR (data
  bits 0–4, read = data | IR bit 7 then clear; write bit 7 SET / else CLR
  of the mask), TOD (TODHI read latches until TODLO; TODHI write halts until
  TODLO; CRB bit 7 directs TOD writes to the alarm; the alarm bit is set
  when a tick lands on the alarm), SDR plain, reg 11 reads `$FF`. PB6/PB7
  timer outputs and the serial shift are not modelled (nor in chipset_ref).
- **Time is lazy.** Each chip keeps the guest E time of its last sync
  (`CIA_T`). A register access first syncs the chip to `mach_etime`:
  `eclock(now − CIA_T)` in O(1) (one 32/16 division only when a continuous
  timer underflows more than once), CIA-B's TOD advanced by the lines in
  between. `eclock` is split-invariant (host test), so when syncs happen
  does not change the state. Before `mach_init` (`mach_on` = 0) time is
  frozen at 0; a gap read as negative (a later `mach_init` restarted the
  counter) counts 0 ticks and re-anchors. The VERTB hook syncs both chips
  every 256 frames, so no chip goes 2^31 E ticks unsynced.
- **TOD clocks.** CIA-A: +1 per VERTB (mach slot-0 callback, one call per
  frame: 50 Hz guest time). **Power-up:** `cia_reset` (which stores the
  callbacks, the VERTB hook included) first runs at the guest's first CIA
  access or at `chip_reset`; VERTBs before that are not counted, whereas
  chipset_ref counts from power-up. S3.7's machine-profile init must call
  `chip_reset` (after `mach_init`). CIA-B: PAL lines on one grid anchored at
  `cia_reset`: line k starts at colour clock 227 k = E tick ⌈227 k / 5⌉
  (15625 Hz, 312.5 lines per 14188-E frame); the grid runs while the TOD is
  halted. Neither is ever an event per tick.
- **Events** (mach slots, callbacks in `mach_cbv`, filled by `cia_reset`):
  1 CIA-A TA, 2 CIA-A TB, 3 CIA-B TA, 4 CIA-B TB, 5 CIA-B TOD alarm; slot 0
  is the VERTB hook. A timer slot is armed at `CIA_T + count + 1` (its next
  underflow) only while the timer is started, counts E ticks, and its ICR
  mask bit is set (TA also when TB counts TA underflows with mask TB). Slot
  5 is armed at the alarm's line while CIA-B's TOD runs and mask ALRM is
  set. Only an event that can still raise IR is armed: a timer's own slot
  only while its ICR data bit is clear (the ICR read re-arms it), and TB
  counting TA underflows arms TB's slot at TB's own underflow, `CIA_T +
  (TA+1) + TB·(TAL+1)` (capped at 2^30 E ahead; a one-shot TA gives one
  pulse: `CIA_T + TA + 1` if TB = 0, else never) — never one event per TA
  underflow, so a timer faster than a service pass cannot starve the guest
  (review F1; test `run_cia` `cost`). The seam's `mach_am`/`mach_dl` are the truth: `mach_arm` is called
  only when the slot is not armed at the wanted deadline, `mach_disarm`
  only when it is armed (so `mach_init` disarming every slot, or a firing,
  is seen at once). A callback syncs its chip (which posts) and re-arms.
- **Interrupts** (the seam's level hold). After every sync, ICR access and
  TOD-A tick, each chip keeps its `mach_hold` bit (CIA-A PORTS = bit 3,
  CIA-B EXTER = bit 13) equal to its IR condition (`ICR data & mask ≠ 0`)
  and calls `mach_post` on the rising edge. chip.s ORs `mach_hold` back
  after every guest INTREQ write, so PORTS/EXTER cannot be cleared while
  the ICR asserts (chipset_ref `update()` after every `eclock` and INTREQ
  write); the ICR read-clear releases the hold.
- **Host-visible state** (`runner.lbl`): `cia_st` (2 × `CIA_SZ` bytes, the
  `CIA_*` field layout in cia.s; `CIA_T` = the chip's last sync time),
  `cia_now` (u32, E time of the last sync), `cia_ok` (0 until the first
  access or `chip_reset`). `chip_reset` ends in `cia_reset` (both chips to
  power-up, anchored at now, slots 1–5 disarmed, `mach_hold` bits 3/13
  released, callbacks stored in `mach_cbv` slots 0–5).
- **Test build.** `CIA_HOSTTIME` (an `--asm-define`, used only by
  `tests/run_cia.py --build-shim` with `tests/cia/shim.s`) makes `getnow`
  read the host-written `shim_t` instead of `mach_etime`, so a test can put
  guest time anywhere (the u32 wrap). The runtime build never defines it.
- **Link order.** `cia.s` links after `chip.s` and before `mach.s`
  (Makefile); no module before it moves; `core.s` is unchanged.

## Copper and display state (S3.5) (W10-P)

Owner: cop (`runtime/ops/cop.s`; the display/copper write paths of
`runtime/ops/chip.s` route to it; the frame hook `mach_fhook` in
`runtime/ops/mach.s`). Design: `docs/S3_PLAN.md` §4 "S3.5";
`docs/WAVE10_BRIEF.md` C1–C3 (+ supervisor rulings: `disp_st` at `$B800`
with a `valid` byte, `cop_dhook` in BSS, copper DMA gating). Reference:
`host/chipset_ref.CopperAt`, `DisplayState`, `DisplayChipset` (additive to
`Copper`/`Chipset`). Tests: `tests/run_cop.py` (+ `--mutants`),
`host/tests/test_cop.py`.

- **Display-state block `disp_st`** (rc ram claim `cop $B800-$B8FF`, RAM
  under BASIC ROM; 104 bytes, all fields little-endian; fields may be
  appended up to `$B8FF`, never reordered):

  | Off | Field |
  |---|---|
  | +0 | `gen` u8: +1 (wraps) after a change (rule below) |
  | +1 | `dmacon` u16: DMACON SET/CLR (bit 15) of bits 0–14 |
  | +3 | `bplcon0`, `bplcon1`, `bplcon2` u16 |
  | +9 | `bplpt[6]` u24: BPLxPTH's low byte (bits 23–16) : BPLxPTL, as written |
  | +27 | `bpl1mod`, `bpl2mod` u16 (s16) |
  | +31 | `diwstrt`, `diwstop`, `ddfstrt`, `ddfstop` u16 |
  | +39 | `color[32]` u16, as written (no 12-bit mask) |
  | +103 | `valid` u8: `$A5` once `cop_reset` has run (undefined RAM before) |
  | +104 | `dirty` u8: := 1 by every `gen` bump; cleared only by the reader (W10-V fix round 1, F1: a `gen` that wrapped back to the reader's last value is still a change) |
  | +105 | `pix` u8 (Wave 11, supervisor; `docs/WAVE11_BRIEF.md` C3): the pixel epoch. Writers of guest pixel memory store a new value after writing (blt.s: every D-enabled blit); disp.s treats a change like a changed field. Not part of `gen`. Undefined before the first writer; readers snapshot it at `disp_init` |

  Every display-register write, by the CPU or by the copper (word, long =
  two words, byte = both halves, the I/O trap's decode), updates its field.
  **`gen`** (fix round 1, F2): a CPU write outside a copper run bumps it
  once per write that changes a field's value, after the field. Inside a
  copper run (frame or synchronous) it moves **at most once, at the end of
  the run**, and only if the block differs from its state at the start of
  the run (a snapshot): a list rewriting the same values every frame, or a
  raster-bar list writing COLOR00 several times per run with the same end
  state, costs the reader nothing. cop.s
  never reads `disp_st`: it compares against a private copy (`cop_sh`), so
  it writes under any `$01`. **Readers** bank BASIC out (`$36`, or `$35`
  inside `mach_run`), check `valid`, and copy what they need when `gen`
  differs from their copy, from `cop_dhook` or their own explicit refresh
  (no writer runs concurrently: writes happen in the trap or the service
  pass, never in an IRQ).
- **`cop_reset`** (host entry; clobbers A/X/Y): fields := 0 (both copies),
  `valid` := `$A5`, `gen` bumped, counters and the copper PC := 0,
  `mach_fhook` := `cop_frame`; `cop_dhook` untouched. Runs at the first
  display/copper register write (`cop_ok` = 0), lazily like `cia_reset`.
  `chip_reset` does not call it (it clears `chip_shd`; call `cop_reset`
  after it). Neither `emu_reset` nor `mach_init` touches it.
- **Copper** = `chipset_ref.Copper` non-strict (`CopperAt.run_at`): an
  instruction is two guest words (`mem_read32`). MOVE (w1 bit 0 = 0) of
  register `w1 & $1FE`: below `$80` (below `$40` with COPCON bit 1, CDANG,
  read from `chip_shd`) the copper **stops**; COPJMP1/COPJMP2 jump to
  COP1LC/COP2LC (`chip_shd`, read at the jump); any other register is
  written through `c_wrw`, i.e. exactly as a CPU word write (logged, shadow,
  INTENA/INTREQ SET/CLR with `mach_hold` and `mach_ichk`, `disp_st`).
  `$FFFF,$FFFE` ends the list; else w2 bit 0 = SKIP (counted in
  `cop_nskip`, a no-op), else WAIT (satisfied at once; no beam). A run
  stops after `COP_LIMIT` = 4000 instructions (the model's default limit;
  `cop_n` counts every instruction fetched, the stopping one included), or
  before a fetch from bank `$BF`/`$DF` (`COP_IOBANK`). Pointers are 24-bit
  with bit 0 clear (bits 31–24 ignored); the model masks `$1FFFFE`, equal
  below 2 MB. A copper stopped by the end marker or the danger rule **stays
  on that instruction** (`cop_pc` points at it, as the hardware waits there
  until the next restart); after the limit `cop_pc` is the next one.
  `cop_why` = 1 end, 2 danger, 3 limit, 4 I/O bank.
- **When it runs** ("copper DMA on" = `dmacon` DMAEN (bit 9) and COPEN (bit
  7)). (1) `cop_frame`, the frame hook `mach_fhook` (2 bytes, BSS, LE, high
  byte 0 = none; never cleared by `mach_init`), under the callback rules.
  **Once per service pass** (fix round 1, F1): the pass counts every frame
  boundary passed (`mach_frames`, INTREQ VERTB and slot 0's callback stay
  per frame) and sets `mach_fdue`; after the count loop, if a VERTB was
  posted, it calls `mach_fhook` once, re-reads the counter (`mach_rdc`) and
  counts the frames the hook took, which do **not** call the hook again (the
  copper skips frames it is too slow for); then it arms the one-shot. A pass
  that ran the hook sets `mach_fs`, so exactly one guest instruction runs
  before the next pass (W9's forced step; `mach_nfs` counts forced steps).
  With copper DMA on, `cop_frame` loads the PC from COP1LC and runs; then it
  calls `cop_dhook`, every hook pass. (2) A
  **CPU** COPJMP1/COPJMP2 strobe always loads the PC from COP1LC/COP2LC;
  with copper DMA on, the copper runs synchronously in the trap, then
  `cop_dhook`. (3) A **CPU** DMACON write that turns copper DMA on (off
  before the write, on after) runs the copper synchronously from the loaded
  PC, then `cop_dhook`; with no PC loaded since `cop_reset` (no strobe, no
  frame restart yet) it runs nothing (so COP1LC, COPJMP1, DMACON — the HLE
  fixture's order — runs the list once; DMAEN and COPEN may come in either
  order; COP1LC, DMACON, COPJMP1 runs it once, at the strobe). The copper's
  own DMACON writes never start a run (`cop_busy`), and a copper that turns
  its own DMA off keeps running to its stop (Copper.run). The trap state
  (`chip_a`, `m_addr`, `m_val`, Y) is saved around a synchronous run and the
  hook, so the second word of a long write (`move.l` to `$DFF086`/`$DFF088`)
  still lands; the chip ABI (X/Y kept) holds. `chipset_ref.Chipset` has no
  CPU-strobe semantics (its copper only runs per frame); `DisplayChipset`
  adds (2)/(3) as above.
- **`cop_dhook`** (BSS in cop.s, exported, cleared by the boot BSS clear;
  LE, high byte 0 = none): the refresh driver's vector (W10-V's `disp_init`
  stores `disp_frame`; disp.s imports it). Called by `jmp (cop_dhook)` from
  a `jsr` after every run outside the frame and at the end of every
  `cop_frame`; it may clobber A/X/Y. In the trap it runs inside `emu_run`
  (IRQ possibly enabled under `mach_run`); in the frame hook, in the
  service pass. No call without a run: a strobe with copper DMA off and
  plain register writes call nothing.
- **Cost**: a copper instruction ~545 6502 cycles including its write
  [MEASURED VICE, review W10-P], so a 4000-step run (`COP_LIMIT`, a runaway
  list looping by COPJMP) costs ~2.2 M cycles, ~10 guest frames at s = 4.
  A display-register write +~60 cycles over S3.4 [EST, static]; the gen
  snapshot +~2 × 103 × 10 cycles per run; per frame with the copper off
  ~40 [EST]. **Throughput limit:** a copper list costing more than a guest
  frame slows the guest roughly in proportion (the hook runs once per pass
  and skips the frames it overran), but never stops it: every hook pass is
  followed by at least one guest instruction (`run_cop` `catchup`: 450
  MOVEs/frame cost 2.4 × the guest time of no copper; a runaway loop ~70 ×;
  both reach HALT).
- **Host-visible** (`runner.lbl`): `cop_ok`, `cop_sh` (the private copy),
  `cop_dhook`, `cop_pc` (u24), `cop_n`, `cop_nrun` (runs), `cop_nfr`
  (frame-hook calls), `cop_nskip` (u16 each), `cop_why`; entries
  `cop_reset`, `cop_frame`; `mach_fhook`, `mach_nfs` (u16 forced steps); exports `c_wrw`, `chip_a`,
  `chip_vl`, `chip_vh` (chip.s: the copper's write path).
- **Deliberate deviation (supervisor-approved):** a DMACON turn-on with no
  copper PC loaded since `cop_reset` (no strobe, no frame restart) runs
  nothing. Real hardware would run from an undefined PC; nothing legitimate
  depends on it, and running guest address 0 would only write garbage
  (`run_cop` case `nopc`, mutant `nopcok`).
- **Not modelled:** WAIT/SKIP beam timing (the model's limit), copper DMA
  cycles, the copper stopping when its own DMA goes off mid-list, DMACONR
  (since S3.6: "Blitter (S3.6)"), BPLCON3+, the blitter's response to copper
  writes (S3.6). (`mach_wk` counts `mach_fhook` as a wake source since
  W11-A, C4.2: "Interrupt delivery", STOP.)
- **Link order.** `cop.s` links after `cia.s` and before `mach.s`
  (Makefile); no module before it moves; `core.s` is unchanged.

## Display refresh (S3.5) (W10-V)

Owner: disp (`runtime/ops/disp.s`) and c2v (`runtime/ops/c2v.s`, row skip).
Design: `docs/WAVE10_BRIEF.md` C1/C3, `docs/S3_PLAN.md` §2, §4 "S3.5";
spec: `docs/C2V.md` "Row skip", "Display refresh (S3.5)". Reference:
`host/c2v_ref.disp_decide` / `disp_convert`. Tests: `tests/run_pf.py`
(+ `tests/pf/shim.s`, `fixtures/c/pf_static.c`), `host/tests/test_disp_ref.py`.

- **Inputs.** `disp_st` (C1, W10-P's rc claim, RAM under BASIC ROM),
  always with BASIC banked out. `disp_frame` reads `gen` (+0) and `dirty`
  (+104) first, then copies +1..+70 (DMACON through COLOR15) and `valid`
  (+103), then clears `dirty` (its only write to the block). `valid` ≠ `$A5` (W10-P's `cop_reset`
  never ran) means "no display state": blank, reason 7. `cop_dhook` is a
  cop.s BSS export that disp.s imports (consumer → provider, as cia.s
  imports `mach_cbv`). It is written only by `disp_init`: high byte first
  to 0, then low, then high. `tests/pf/shim.s` (test builds only) adds
  `shim_kick`, a fake `cop_frame` end that calls through `cop_dhook`.
- **Entries** (JSR; clobber A/X/Y; any `$01` that maps I/O in: `$37`,
  `$36`, `$35`; BASIC banked out inside, the caller's `$01` restored; the
  I flag preserved; every REC use ends with `mem_rec_dirty`, and a call
  that uses no REC leaves `m_rok` alone):
  - `disp_init`: `c2v_init`, stashes the LUT at guest `disp_lut`, resets
    the state (`disp_mode` = `$FF`, counters 0, budget open), stores
    `disp_frame` in `cop_dhook`. Call it after `cop_reset`.
  - `disp_frame`: the hook. Valid in the service pass (`$01` = `$35`, IRQ
    enabled: `mach_irq` never touches the REC) and in the trap context of a
    synchronous COPJMP run (inside `emu_run`, or outside `mach_run` in HLE).
    The caller (W10-P) must preserve whatever the trap ABI needs (the chip
    ABI keeps X/Y) around it.
  - `disp_refresh`: converts now from `disp_st`, ignoring `gen`, the
    snapshot compare and the budget.
- **Policy v0:** a change is `gen` ≠ the last value seen **or** `dirty` ≠ 0
  (F1); it converts if the copied fields differ from the last snapshot.
  Inside `mach_run` (`mach_in` ≠ 0) a conversion starts only when
  `DISP_N` × 14188 guest E ticks (24 frames' worth since W12-V; was 20) have passed since the
  previous conversion **ended** (`mach_etime` at its end; F3: a frame count
  was eaten by the catch-up after each long conversion, livelocking the
  guest at s ≤ 1). E going backwards (`mach_init`) opens the budget. A
  deferred change stays unseen (`gen`/`dirty` untouched). Outside
  `mach_run`, convert at once. No dirty tracking of CPU bitplane writes.
  **Pixel epoch (W11-A, C3):** `disp_init` snapshots `disp_st+105`; a
  value different from the last one taken is a change like `gen`'s (same
  budget; deferred, it stays unseen), and converts even when the copied
  fields are equal (the pixels changed). The epoch is taken by every
  decision (`disp_snap`, also `disp_refresh`). Writers: `blt.s` (W11-B),
  by a read-modify-write `inc disp_st+105` with BASIC banked out (fix
  round 1, L5: so the first blit always changes the value `disp_init`
  snapshotted). Accepted residual: exactly 256 bumps (a multiple of 256)
  inside one deferred window alias back to the value last taken and are
  not seen; the next blit heals it.
- **VERTBs during a conversion (F4).** A conversion inside the service
  pass takes many frames (mc2 ≈ 30 at s = 0, ≈ 2 at s = 4). The pass
  counts every one of them (`mach_frames`, slot 0's callback: CIA-A's TOD
  ticks once per frame, tested live at s = 2 and 4), but VERTB is a level
  held in INTREQ, so the guest's level-3 handler runs once for all the
  frames that passed during one pass, as in W10-P's copper catch-up
  ("Machine loop" / "Copper"). A guest that counts VERTB interrupts to
  keep time sees fewer of them while the display converts.
- **Modes:** 2-plane lores → `c2v_mc2s` (exact pixels); 4-plane hires →
  `c2v_hiress` (silhouette pixels, per-cell colours, W12-V); anything
  else, DMA off, an empty DIW, a short DDF or a row crossing a 512 KB
  line → blank (`disp_why` 1–6). Once a picture has been shown, a blank
  sets `$D011` = `$0B` and `$D020` = LUT[COLOR00]. Before that, a blank
  only logs.
- **Colour choice (W12-V).** Reference `host/c2v_ref.separate`; tests
  `host/tests/test_disp_colour.py`, `tests/run_pf.py` steps `mc2-dark` /
  `hires-dark`. The LUT (nearest VIC colour in CIE L\*a\*b\*) alone
  maps every dark OCS colour to black, so the insert-disk screen's pens
  ($111, $222, $024 on $000) were black on black. The rule:
  - **Cell pen (hires).** For each 8×8 cell (16 hires pixels per line),
    on the first of its 8 lines with a set pixel, the colour index of the
    leftmost set pixel (plane bytes 2cx then 2cx+1, bit 7 first); 0 if
    the cell has none. All 200 converted lines count, also those below
    DIWSTOP that `disp_clrlines` clears afterwards. `c2v_hiress` leaves
    the pens in colour RAM (unused by the VIC in hires) and counts cells
    per pen (`c2v_v_cnt`, saturating at 255).
  - **Pens in use:** pen 0 always; mc2: pens 1–3; hires: every cell pen.
    **Order:** pen 0, then by cell count descending, ties by lower pen
    (mc2: 1, 2, 3).
  - **Pass 1:** in order, each pen takes its LUT colour unless a pen with
    a different OCS colour (COLORxx & `$FFF`) already holds it. **Pass
    2:** in order, each pen still without a colour walks the luma ladder
    (`c2v_ref.LADDER` = `disp_lad`: 0 6 9 2 11 4 8 14 12 10 5 15 3 7 13
    1) from its LUT colour's place k: k+1, k−1, k+2, k−2, … (places off
    the ladder skipped), and takes the first colour no pen with a
    different OCS colour holds.
  - **Guarantees:** pen 0 keeps LUT[COLOR00] (the background and border
    are unchanged); pens with equal OCS colours share a colour; pens in
    use with different OCS colours never do (≤ 16 pens, 16 colours: pass
    2 always finds one). In hires "in use" means a **cell pen**: a pen
    that is never the leftmost pixel of a cell's first line with pixels
    (on the insert-disk screen pen 3, $024) is not separated and does not
    show. Colour fidelity is best effort: a pen that loses its nearest
    colour gets the nearest free one in luma, any hue.
  - **Output:** mc2: the four colours as before (`$D021`, matrix nibbles,
    colour RAM). Hires: matrix cell = colour(pen) << 4 | colour(pen 0),
    colour RAM = the cell pen (0 on any blank rows), via `c2v_hfill`
    after `c2v_lookn` and `disp_cols` (the rule).
  - **Matrix checksum (V2):** after every shown conversion `disp_msum`
    reads the 1000 matrix bytes back (`$01` = `%xx100` one page at a
    time, IRQs masked per page; the NMI vector already guarded by the
    fill; a matrix at or above $A000 is always read banked, the NMI vector
    set first) into `disp_mck` (BSS, u16 LE: Fletcher sums mod 255 with
    end-around carry, s1 = Σ b, s2 = Σ s1, `c2v_ref.screen_sum`; fix
    round 1: mod 256 kept only ~8 bits of a palette error, which moves
    high nibbles only). The host reads it where it cannot read the RAM
    under I/O (the Ultimate, m3). Proven: every one-bit error of one pen's
    colour and every pen swap on a frame-345-like screen changes it
    (`host/tests/test_disp_colour.py`); in general it is a 16-bit
    checksum, so 1 wrong palette in ~65k can still collide.
  - **LUT carriage:** `disp_lutp` holds the LUT run-length packed
    (`c2v_ref.rle_lut`: byte = (run − 1) << 4 | colour, 821 bytes, was
    2048 nibble-packed; supervisor-approved); `disp_init` unpacks it, once,
    into the same 4096 REU bytes at `disp_lut` and stops at exactly 4096
    entries (`disp_init` 126,616 φ2 cycles, base 120,006–120,225).
- **Fit-view crop (W14-D).** A picture taller than 200 lines shows its
  first 200 lines from DIWSTRT: `disp_crop` = 0, a crop from the bottom
  (was the centre, (L − 200) / 2). A Workbench title bar, its menus and
  the pointer start stay visible. The bottom lines are the ones hidden now:
  lines 200-255 of a 256-line screen (L − 200 in general). They are reachable
  in the 1:1 view by panning down (`disp_py` up to `disp_ymax` = L − 200;
  run_pf view step `right-bottom` shows lines 56-255). AROS's insert-disk
  text (field line ~150) is inside the fit window. LACE uses the same rule: the fit view
  shows one field, whose first 200 lines are the frame's top. Lines below
  the 200th are not shown. The pointer module maps vertical positions
  through `disp_crop`, so it follows unchanged (fit: VIC Y = 50 + VSTART −
  DIWSTRT V). Reference `c2v_ref.disp_decide`; tests
  `host/tests/test_fit_top.py`; model frames `tests/aros/model_frames.py`
  `CROP_RULE = "top"` (stale JSONs are rebuilt by their loaders).
- **1:1 view (W13-D).** Spec: `docs/C2V.md` "1:1 view (W13-D)", "Hires
  with 1-3 planes (W13-D)".
  - Reference: `c2v_ref.one_cells`, `convert_one`, `disp_decide_view`,
    `disp_convert_view`.
  - Tests: `tests/run_c2v.py` (kind `one`), `tests/run_pf.py` leg `view`,
    `tests/run_spr.py` leg `aros1`, `host/tests/test_c2v_one.py`.
  - **State.** All of it is in disp.s's BSS, so the snapshot carries it
    (I2): `disp_view` (0 fit, 1 1:1), `disp_px` (the window's first byte,
    0-40), `disp_py` (its first line, before clamping), `disp_ymax`, and
    `disp_one` (the last decision is the 1:1 view). `disp_ymax` is
    L − 200, or L − 101 with LACE, clamped to 0-255; every shown decision
    sets it. (Fix round 1: LACE L − 101.)
  - **The toggle.** `inp_view` (I1; until W13-I lands, disp.s's stand-in
    under `DISP_INPV_LOCAL`) bit 0 is an edge.
    - `disp_frame` consumes it first, clearing bit 0 only.
    - It flips `disp_view` and sets `disp_px` = 20 and `disp_py` = 0: the
      window starts at the top, centred horizontally (W14-D; was
      `disp_ymax` / 2).
    - It converts at once, ignoring gen, the snapshot compare and the duty
      budget.
  - **The decision.** For hires (BPU 1-4) with `disp_view` = 1:
    - `disp_one` = 1 and `disp_crop` = min(`disp_py`, `disp_ymax`).
    - With BPLCON0 LACE, each plane's row advance is halved (the two
      fields interleave; an arithmetic shift, so negative modulos work) and
      `disp_lines` = min(2 · `disp_lines`, 200). `disp_ymax` = L − 101, so
      the odd field's last frame line read is ≤ 2L − 1.
    - Rows past the picture (frame line ≥ `disp_lines`) read 0
      (`disp_ozero`, BHI, from `c2v_ones` through `c2v_v_olim`): they add
      nothing to the colour counts and show pen 0, as in the fit view. A
      char row that straddles the picture's end shows the cell's bg below
      it.
    - Known gap: the 512 KB check covers only the current field's rows.
      The other field's rows in between are not checked; a straddling row
      shows wrong pixels in that row only, since the DMA target is fixed.
    - Any other decision leaves the fit view's.
  - **The output.** A hires bitmap with a per-cell bg (the pen of the
    cell's top-left pixel) and fg (the first other pen in raster order).
    The matrix byte is colour(fg) << 4 | colour(bg), with colours by the
    W12-V rule over every cell's bg and fg. Colour RAM is left as it was
    (unused in hires). `disp_mck`, `disp_clrlines` and the VIC setup are
    the fit hires view's.
  - **Cells with 3+ pens are lossy:** the top-left pixel's pen becomes bg,
    and every other pen shows as fg's colour. Pinned by run_c2v case
    `three-pen`.
  - **Frozen part.** The fit view's bitmap, matrix and colour RAM are
    byte-identical to the base for every input it converted before: the
    existing run_c2v/run_pf legs, and the AROS m3 hashes, unchanged.
- **Hires with 1-3 planes (W13-D).** These decisions now convert (they
  were DEPTH), in both views.
  - An absent plane reads `disp_zero` (`$DF4000-$DF40FF` in the REU
    under the trapped `$DF` bank, rc `guest.inc`/`machine.inc`, zeroed by
    `disp_init`) with a row advance of 0.
  - The guest cannot reach it: every guest access to bank `$DF` traps to
    `chip.s`.
- **Pointer sprite (W13-D, D2).** The pointer is a machine-mode module,
  `tests/aros/spr.s`, at `$C600-$C8FF`.
  - **Build and arm.** It is assembled at test time by
    `tests/run_spr.py build_spr(out_dir, labels)` against the runner's
    labels; it is not in the PRG. `run_spr.arm` arms it: `spr_vec` :=
    `$C600` (high byte last) and `spr_id` := `"SPR1"` (`$31525053`; a
    resume compares it, W13-W's loader).
  - **Hook.** `disp_vw` (BHI) calls through `spr_vec` once per frame
    hook when F1 is not pressed and `spr_vec` ≠ 0. The module returns
    C = 1 when it moved the 1:1 window; `disp_frame` then converts at
    once.
  - **When it runs.** Inside `mach_run` only. It shows the pointer while
    `disp_mode` is mc2 or hires, DMACON has DMAEN and SPREN, and VSTOP >
    VSTART. A hidden → hidden frame writes nothing.
  - **Per frame.** SPR0PT is read from `chip_shd` (bit 0 ignored), then
    POS/CTL with one 4-byte REU fetch.
  - **Image and colours** are read again only when SPR0PT, POS or CTL
    changed, or `disp_nconv` (low byte) moved. The image is up to 21
    lines of DATA/DATB; the colours are LUT[COLOR17-19], one byte DMA each
    into `$D027-$D029`.
  - **Sprites and memory.** VIC sprite 0 = DATA & ~DATB (colour 17),
    1 = ~DATA & DATB (18), 2 = DATA & DATB (19). The masks are exclusive,
    so the colours are exact at full width. Blocks are at `disp_sprb`
    (`@under disp $D400-$D7FF`, VIC blocks $50-$52) and the pointers at
    `c2v_screen + $3F8`, written at `$01` = `%xx100` under `sei`.
  - **State** is in `c2v_args` + 180..211 plus the module's own two bytes,
    which are zero when staged. After a resume the module is staged again
    and rebuilds everything from `chip_shd` and guest memory.
  - **Position.** In lores pixels: SX = HSTART + 1 − (2 · (DDFSTRT &
    $FC) + 9), lores (DDFSTRT & $F8) + 17. These are the playfield's first
    fetched pixel, which the fit view shows at C64 x = 0 (HRM: the sprite
    pixel at hcount H shows with playfield hcount H + 1). SY = VSTART −
    DIWSTRT V.
    - Fit view: VIC X = 24 + SX, VIC Y = 50 + SY − `disp_crop`.
    - 1:1 view: VIC X = 24 + 2 · (SX − 4 · `disp_px`) (X-expanded), VIC Y
      = 50 + SY − `disp_crop`. With LACE, 50 + 2 · (SY − `disp_crop`),
      Y-expanded.
    - Off X 0-$1FF or Y 0-255, the pointer is hidden.
  - **Pan (1:1).** When the pointer comes within 32 C64 pixels (16 lores;
    LACE 16 field lines) of a window edge, the window re-centres on it:
    `disp_px` := clamp(SX − 80, 0, 160) / 4 and `disp_py` := clamp(SY − 100,
    LACE 50, 0, `disp_ymax`). C = 1 only if px or the window's line
    changed, so a clamped edge never converts. Hysteresis: after a pan the
    pointer is ≥ 48 C64 px from every edge.
  - **Arming guard (fix round 1).** Before calling through `spr_vec`,
    `disp_vw` (BHI) compares the module's ID at `spr_vec` + 3..6 with
    `spr_id`. On a mismatch, for example a tool stub or a resume that did
    not re-stage the module, it clears `spr_vec`'s high byte (disarmed) and
    returns C = 0. Test: `run_spr` leg spr, case `guard-overwritten`.
  - **Hand-off rule.** Whoever launches a session stages the module at
    `$C600` and arms it after the last stub, `test_load_chunk` or
    runner-helper write that can touch `$C000-$C8FF`, and again after every
    resume. W13-W's loader does this at the merge.
    - Arm order (`run_spr.arm`, fix round 2): `spr_vec` high := 0, then
      `spr_id`, then `spr_vec` low, then `spr_vec` high (this last write
      arms). Then `$D01B`/`$D01C` := 0.
    - **A resume disarms the pointer module.** snap.s's restore clears
      `spr_vec` high after the BSS image (supervisor, at the merge), and the
      launcher re-stages and re-arms it.
    - The ID guard catches a module whose ID bytes were overwritten. A stub
      written inside the module that leaves `$C603-$C606` intact is not
      caught (rev-vis round 2); only the rule above prevents that.
    - On an ID mismatch the guard also hides VIC sprites 0-2 (`$D015` bits
      0-2 := 0).
  - **The module is full:** 768 B, `$C600-$C8FF`, 0 B spare; `sval`/`son`
    are its last two bytes (`$C8FE`/`$C8FF`). Any growth must trim first.
  - **Pan gate (fix round 1).** The window re-centres only while the image
    has a pixel (the OR of its 4 · height bytes). An empty pointer, such as
    AROS's on the insert-disk screen, never moves the window: it keeps F1's
    default (W14-D: the top) or the test-set offset.
  - **Limits:**
    - The height is capped at 21 lines (VIC; LACE 1:1 at 42 frame lines).
    - The attach bit (sprite 1) is ignored.
    - Sprite position is not dilated by BPLCON1 scroll.
    - Sprites 1-7 are not shown.
    - A pan converts at once, past the duty budget (user-paced, like F1).
    - A pointer colour change alone (COLOR17-19, not display fields, so no
      conversion), or an image edited in place at the same SPR0PT, stays
      stale until the pointer moves (POS/CTL change) or the screen converts.
- **C64 resources:** writes the c2v regions (bitmap, matrix under I/O,
  colour RAM, `c2v_stage` during `disp_init`, `c2v_args`), `$D011`,
  `$D016`, `$D018`, `$D020`, `$D021`, and `$DD00` bits 1–0 (bits 7–2 and
  the DDR kept; `mach.s` never writes port A, "Machine loop"). State is in
  disp.s's BSS (whole page), host-visible in `runner.lbl`. W13-D: the
  pointer module (not disp.s) writes VIC `$D000-$D005`, `$D010`, `$D015`,
  `$D017`, `$D01B`-`$D01D`, `$D027-$D029`, the sprite pointers
  `$D3F8-$D3FA` and the blocks `$D400-$D4BF` (`@under disp $D400-$D7FF`).
- **Guest resources:** `disp_lut` `$DF0000-$DF0FFF` (rc `guest.inc`,
  owner disp): the REU bytes under the trapped custom-chip bank. Every
  guest access to bank `$DF` goes to `chip.s` (open bus), never the REU,
  so the table is invisible to the guest. disp.s reads it by REU DMA
  only; `disp_init` writes it. The CPU's `$BF`/`$DF` trap and the HLE DOS
  buffer check ("I/O trap", F2) together keep the guest out: tests
  `lut-trap` (30 CPU stores) and `dosbuf` (Read/Write into `$DF`/`$BF`,
  straddling spans and one covering all of `$BF`).
- **Link order:** `disp.s` after `cia.s`, before `mach.s`. CODE and BSS
  are padded to whole pages, and there is no RODATA or DATA. `core.s` is
  unchanged.

## Blitter (S3.6) (W11-B)

Owner: blt (`runtime/ops/blt.s`; in `runtime/ops/chip.s` the blitter write
paths `$040–$074`, the DMACONR read and `chip_blitirq`, labelled `; W11-B
blt`). Design: `docs/WAVE11_BRIEF.md` C1/C3, `docs/S3_PLAN.md` §3.4, §3.5,
§4 "S3.6". Reference: `host/chipset_ref.Blitter` (normal mode) through
`Chipset` (Paula's DMACONR/BZERO, INTREQ BLIT). Tests: `tests/run_blt.py`
(+ `--mutants`, `--perf`), `host/tests/test_blt.py`, the blit cases of
`tests/chip_oracle.py`.

- **Entry.** Every write to `$040–$074` (CPU word/long/byte, or a copper
  MOVE with COPCON CDANG) is logged and lands in `chip_shd` as before; then
  c_wrw calls `blt_w` (X = offset; Y kept, A/X clobbered). BLTSIZE (`$058`)
  runs the whole blit **synchronously, inside the trap**; BLTBDAT (`$072`)
  loads the B hold register (below). Every other blitter register is read
  from `chip_shd` at BLTSIZE. OCS size: h = bits 15–6 (0 = 1024), w = bits
  5–0 (0 = 64). BLTSIZV/BLTSIZH/BLTCON0L (ECS) only land in the shadow.
- **Normal mode** = `Blitter.blit` except for in-row overlap (below,
  "Not modelled": blt.s reads a whole row of A/B/C before it writes the D
  row; the model is word-serial): channels A/B/C/D = BLTCON0 bits 11–8;
  minterm LF (bit 7 = ABC … bit 0 = ¬A¬B¬C); A masked by AFWM on the first
  and ALWM on the last word of each row in processing order (both when
  w = 1), before its shift; ASH/BSH with the old word carried across words
  **and rows** (0 at the start of each blit); signed modulos added after
  each row (subtracted when descending); descending (BLTCON1 bit 1):
  addresses step down, shifts go left; fill (IFE bit 3, EFE bit 4, both =
  exclusive) per word bits 0–15, carry = FCI (bit 2) at every row start;
  a channel with DMA off uses BLTADAT/BLTCDAT, B its hold register. After
  the blit the four pointers are written back as `(p >> 16) & $1F : p &
  $FFFF` (all four, used or not), BLTADAT/BLTBDAT/BLTCDAT hold the last
  word their channel fetched (raw), and the B hold register = the last
  shifted B.
- **BLTBDAT (CPU write)**: hold := the value shifted **alone** by the
  current BSH: `(v << BSH) & $FFFF` descending, `v >> BSH` ascending; no
  previous B word enters. **Model change (Wave 11, supervisor-approved
  exception to "chipset_ref additive only"):** `Blitter.write_bdat` used to
  shift against the previous B word (`bprev`); FS-UAE shows otherwise
  (oracle cases `bdat_desc_bsh`, `bdat_asc_prev_cpu`, `bdat_after_bdma`;
  `b_stale_hold` pins that a B-off blit after a B-DMA blit uses the last
  shifted B, unchanged). The old cases missed it (previous B always 0, or
  BSH 0). `bprev` stays in the model (written by `blit()`, read only by
  the oracle mutant `bdat_reshifted_per_word`); blt.s keeps no such word.
  The chip-oracle mutant `bdat_write_not_shifted` was retargeted at the new
  line. The AROS boot model is unaffected (its blits use no BSH; same
  instruction and blitter-word counts before and after:
  `build/evidence/w11-blt/bootmodel/`). Tests: `host/tests/test_blt_ref.py`
  (31 cases red on the old model).
- **Line mode** (BLTCON1 bit 0): counted in `blt_nline` (and logged as
  the BLTSIZE write); memory, pointers, data registers, BZERO and the
  pixel epoch untouched; completion as below. Deliberate divergence from
  `Blitter.line` (an unverified approximation). The AROS boot model uses
  no line blit (76 blits, all `CD lf=CA` / `BCD lf=CA`).
- **Completion** (both modes): INTREQ BLIT (bit 6) is set by
  `chip_blitirq`, c_wrw's Paula SET path for `$8040` (mach_hold ORed back,
  then `mach_ichk`: in exception mode a deliverable level-3 interrupt is
  taken at the instruction after the BLTSIZE write). Not logged; the raw
  INTREQ slot is untouched. **DMACONR** = `(DMACON & $07FF) | BZERO`, with
  DMACON = cop.s's field `cop_sh+1` (C1 +1) and BZERO = no 1 bit in any D
  word of the last normal blit (D DMA on or off; after fill when filling);
  BBUSY never reads 1. (This supersedes "DMACONR reads its slot" in "I/O
  trap" and "Copper and display state", which predate S3.6.)
- **Memory.** Channel addresses are guest addresses masked to the model's
  2 MB chip range: `& $1FFFFE` at the start (as `Blitter._ptr`), then
  24-bit arithmetic, and every REC bank store is `& $1F`. A blit therefore
  never reads or writes banks `$BF`/`$DF` (the REU under `$DF0000` holds
  `disp_lut`; test `iobank`). One REU DMA per channel row (two when the row
  crosses a 512 KB REC line; mod 2 MB past `$1FFFFF`, below 0), combined in
  the row buffers `blt_abuf`/`blt_cbuf` (rc `blt $B600-$B7FF`, RAM under
  BASIC ROM; BASIC is banked out for the blit, the caller's `$01`
  restored), one DMA per D row. The REC use ends with `mem_rec_dirty`.
- **Instruction window.** W12-C (supervisor-approved): before each D row
  is stored, `blt_dpage` calls core.s's `code_invpg` for the row's first and
  last page (a row is at most 128 bytes), dropping every cached code window
  of those pages, current or not (tests `smc` here and `run_cwin` leg
  `blt`). m_addr, m_val and t_tmp are untouched (`code_invpg` uses none).
- **Pixel epoch (C3, Addendum 3).** After every normal blit with D on:
  `inc disp_st+105`, a read-modify-write of the byte itself (BASIC is out
  in BCODE), never a private counter (one that happened to equal a
  reader's `disp_init` snapshot would hide the first blit). Tests `pix`
  (+1 per D blit, none for D-off or line blits) and `epoch` (byte preset
  to the old counter's next value: red on the old code,
  `build/evidence/w11-blt/red/epoch-old-counter.txt`).
- **Trap ABI.** m_addr, m_val, t_tmp and chip_a survive a blit (the second
  word of `move.l` to `$DFF056` or `$DFF058` still lands); chip_vl/chip_vh
  are clobbered by the IRQ raise (c_wrw's callers reload them per word).
- **Host-visible** (`runner.lbl`): `blt_n` (u16 normal blits), `blt_nline`
  (u16), `blt_nwd` (u32 words), `blt_bz` (`$20` = BZERO), `blt_bhold` (u16
  LE); entry `blt_reset` (BZERO, hold registers
  and counters := 0; host only; `chip_reset` does not call it).
- **Link order and layout (BCODE).** `blt.s` links after `cop.s`, before
  `disp.s`. Only `blt_w` (the bank stub), `blt_dmaconr` and `blt_reset` are
  in CODE (73 bytes); BSS (MAIN) 133 bytes; BBSS 0 bytes. Everything else
  (dispatch, BLTBDAT, shifter, the blit, window invalidation: 1,998 bytes,
  `$A000-$A7CD`) is in **BCODE**, the shared banked-code segment (with
  `BBSS`, optional banked BSS, not cleared at boot): `runtime/c64.cfg`
  memory area `BHI` = `$A000-$B5FF` (rc `core $A000-$B5FF`), RAM under
  BASIC ROM, 3,634 bytes still free there; MAIN is `fill = yes`, so the PRG
  is one contiguous load from `$0801` (40,911 bytes on disk vs 33,824
  before; loads verified through the harness on VICE and the U64E, BCODE
  read back with BASIC out). **BCODE rules:**
  code there runs only with BASIC ROM banked out and is entered only
  through a MAIN stub that banks BASIC out (`$37` → `$36`; `$36`, `$35`,
  `$34` unchanged) and restores the caller's `$01` on every exit (blt_w:
  one return path; test `bank` calls it with `$37`, `$36`, `$35`; mutant
  `nobank` jams in BASIC ROM). blt.s puts no BSS in BBSS. **IRQ safety:** the I flag is the caller's. A
  machine-loop IRQ/NMI during a blit (`mach_run`: `$01` = `$35`, handlers
  and RAM vectors at `$FFFA` in MAIN/RAM under KERNAL) or a KERNAL IRQ in
  HLE (`$36` keeps KERNAL in) runs code outside BCODE that neither depends
  on BASIC nor touches the REC, and returns into BCODE with `$01` as it
  found it. **What moved** (`build/evidence/w11-blt/mapdiff.txt`; CODE grew
  $6A = chip.s +33 + blt.s MAIN 73): the CODE and BSS of every module
  linked before chip.s are unchanged (core.s's included); chip.s's CODE
  grew in place; cia.s and cop.s CODE +$21; disp.s and mach.s CODE +$6A
  and BSS +$85; the RODATA and DATA of every module (core.s's included)
  +$6A, since those segments follow CODE. `core.s` is byte-identical
  modulo that relocation (link-swap: the PRG linked with the base tree's
  core.s equals the gate PRG, `coreswap.txt`) and `perf_cycles --max-delta
  0` passes on the final build.
- **Not modelled:** blitter DMA cycles and BBUSY, BLTPRI/nasty, ECS
  BLTSIZV/BLTSIZH, line drawing, CPU writes to bitplanes for the pixel
  epoch (C3 backlog). **In-row overlap** (review W11-B M1): when D lies
  inside the same row of A/B/C, blt.s's row-wise fetch-then-store differs
  from the word-serial model and from FS-UAE (which writes D one word
  late): oracle case `p_overlap_asc2` (D two words ahead of A, 1 x 12) —
  model = FS-UAE, blt.s differs in 20 bytes; `p_overlap_asc` (distance 1,
  2 x 8) — blt.s, the model and FS-UAE all differ (the model's own
  mismatch: 30 bytes). Both are chip_oracle LIMIT/MATCH-pinned and
  `run_blt` pins blt.s to a row-wise model. Overlaps across rows (the
  usual scroll/copy cases, modulo-separated) are exact. **Odd modulos**
  (L1): FS-UAE ignores modulo bit 0; the model and blt.s honour it
  (`p_odd_mod`, 12 bytes, documented LIMIT). Backlog: a word-serial path
  for in-row overlap (detectable at setup), FS-UAE's D delay for distance
  1 in the model, modulo bit 0.

## Machine profile and AROS boot (S3.7) (W11-A)

Owner: aros (`runtime/rc/machine.inc`, `tests/aros/mboot.s`,
`host/machine.py`, `tests/aros_boot.py`, `tests/aros/model_frames.py`).
Design: `docs/S3_PLAN.md` §3.2–§3.5, §4 "S3.7"; `docs/WAVE11_BRIEF.md`
C2–C4. Reference: `tools/s3probe/aros_boot_model.py` (the boot model).
Tests: `tests/aros_boot.py` (legs `m1`, `m2`, `m3`, `ablate`, `wkhook`,
`s0`, `pix`, `--mutants`), `host/tests/test_aros_profile.py`.

- **Two guest profiles** (`docs/RESOURCES.md`, `host/rescheck.py` check
  (g)). `runtime/rc/guest.inc` stays the HLE map; `runtime/rc/machine.inc`
  is the machine-mode map of the same 16 MB REU, never included by
  `runtime/macros.inc` (the runtime is profile-agnostic). rescheck checks
  each profile on its own (overlap, coverage, equates inside their
  regions), checks that EVERY guest map reserves the trapped banks
  `$BF0000–$BFFFFF` and `$DF0000–$DFFFFF` (owner `io`, or `disp` for the
  palette LUT the REU holds under `$DF`), cross-checks the profile's host
  mirrors (`host/machine.py`) and judges `tests/aros/*.s` literals against
  the profile. The HLE map's `free` region `$800000–$DEFFFF` became `free
  $800000–$BEFFFF`, `io $BF0000–$BFFFFF`, `free $C00000–$DEFFFF`, and its
  `free $DF1000–$DFFFFF` became `io`.

  | Guest range | Machine profile |
  |---|---|
  | `$000000–$1FFFFF` | chip RAM, 2 MB (AROS sizes it so on the flat REU) |
  | `$200000–$BEFFFF` | free: zero, unmapped (AROS probes it) |
  | `$BF0000–$BFFFFF` | io: CIAs |
  | `$C00000–$DBFFFF` | slow RAM, 1.75 MB |
  | `$DC0000–$DEFFFF` | free: zero (Gayle ID `$DE1000` reads 0) |
  | `$DF0000–$DF0FFF` | disp: the palette LUT (as in guest.inc) |
  | `$DF1000–$DFFFFF` | io: custom chips |
  | `$E00000–$E7FFFF` | `aros-ext.bin` |
  | `$E80000–$F7FFFF` | free: zero (no autoconfig board, no cartridge ROM) |
  | `$F80000–$FFFFFF` | `aros-rom.bin` |

  No write protection (the ROM is never written, S3_PLAN §3.3), no mirrors.
- **Load** (`host/machine.py`). `find_rom` takes the two banks from the W6-O
  cache named by `tools/aros/manifest.txt`, re-verifies every SHA256 and
  fails closed; a missing or bad cache makes the boot legs print `SKIP
  aros_boot …` and exit 0. The ROM never enters the repository or a build
  artifact: it goes host → REU at test time only. Staging order: the WHOLE
  REU is zeroed first (`aros_fill`: REC stashes of one zero byte, C64
  address fixed; the U64E's REU otherwise holds the previous user's
  data), then `aros-ext.bin` at `$E00000`, `aros-rom.bin` at `$F80000`, then
  the overlay longs (below). VICE stages through the runner's
  `test_load_chunk`; the U64E through the harness's SocketDMA `REUWRITE`
  (`socket_dma_reu_write`), falling back to `test_load_chunk`. Every staged
  span (and a zero hole at `$F00000`) is read back on the C64 by checksum
  (`aros_sum` = `host/machine.fletcher`) before the boot.
- **Overlay choice.** The A500's reset overlay (ROM mirrored at 0 until
  CIA-A PRA bit 0 drops) is not modelled. Like the boot model, the loader
  copies the ROM's first 8 bytes (reset SSP, reset PC) to guest 0, and the
  boot entry loads SSP from `$000000` and PC from `$000004`. AROS reads
  those once and turns OVL off (a CIA-A port write, only stored) before it
  uses low memory; the 8 bytes are chip RAM afterwards.
- **Boot entry** (`tests/aros/mboot.s` `aros_boot`). A test-time driver
  assembled against `runner.lbl` (as `tests/irq/drv.s`), loaded at
  `buf_case` and called through the runner's mailbox dispatcher (U64E) or
  a 6-byte trampoline (`jsr aros_boot / jmp *`, VICE `goto`): the gate PRG
  stays byte-identical. Sequence: `sei`; `st_exc` := 1; `emu_reset`; SSP,
  PC from the overlay longs; SR := `$2700` (68000 reset: S, IPL 7); USP :=
  0; `mach_s` := the profile value; `mach_init`; `chip_reset` (ends in
  `cia_reset`: the CIAs at power-up, CIA-A PRA inputs `$EB`, "CIA-A/B");
  `cia_reset` again (the brief's explicit step); `cop_reset` (display
  state, `mach_fhook` := `cop_frame`); `disp_init` (LUT, `cop_dhook`); then
  `mach_run`. It returns only on a real halt (an odd vector, or a STOP
  nothing can wake); its result block records `st_halt`, `st_reason`, PC
  and `mach_frames`. The **host stop condition** is the host's: it watches
  the serial ring and the counters while the guest runs and ends the
  session (VICE: the harness releases the instance; U64E: the DeviceLock
  hold's restore resets the C64).
- **buf_case in machine mode (W13-W).** `$C000-$C8FF` is the machine-mode
  code area, test-time only (never in the PRG): mboot `$C000-$C1FF`
  (asserted: its link size), the td module `$C200-$C5FF`, W13-D's spr
  `$C600-$C8FF`. mach_run is called from mboot and stays inside it, so the
  area stays resident; a resume's fresh load re-stages it
  (`tests/aros_wb.WbMachine.load_driver`). `tests/aros_resume.py`'s REU stub
  moved from `$C700` to `$CC00` (buf_result, unused in machine mode;
  `$CB00` is its data page); `tests/ureu_probe.py`'s stub stays at `$C700`
  (it never runs with a module staged).
- **`mach_s` per host** (measured, `docs/PERF.md` "AROS boot"), chosen
  for SPEED only: **3 on the U64E at 48 MHz** (fix round 2, L8: picked by
  the time to m3, the no-media screen, which is bound by guest time: s = 3
  86.8 / 98.3 s, s = 4 101.0 / 100.6 s, s = 2 106.3 s, s = 5 113.7 s; m2
  alone is fastest at s = 4, 58 s) and **9 on VICE** (m2 in ~120 s of
  warp, m3 in 174 s; ≈ 9k guest instructions per frame after m1).
  `tests/aros_boot.py` `BOOT_S_VICE`/`BOOT_S_U64` hold the values; `--s`
  overrides. Lower s is slower to m2 or does not reach it; that is NOT a
  requirement of AROS (fix round 1, HIGH-1: the boot model with its CPU
  slowed 24× still reaches m2 at ~680 instructions per frame). What we
  know (`docs/PERF.md` (l)): on VICE, s ≤ 6 failed through the
  `MACH_POLL` cost (fixed, below; s = 6 now reaches m2); the U64E reaches m2 at s = 1 (4 of 4 runs on
  the fix-round build, ~105 s; one earlier run did not, not reproduced)
  and not at s = 0 (2 of 2 runs, 400 s), consistent with the model's
  limit (see "C4").
- **Milestones.** m1: serial `ROMInfo: 1MiB ROM detected`; m2: serial
  `romtaginit done`, and the serial log up to it must equal the boot
  model's byte for byte (414 bytes up to the milestone, 416 with its line
  end; every captured byte is compared, so a capture defect fails), with
  no byte lost (the host polls the 1024-byte `chip_ser` ring every 20 ms
  on the U64E and the C64U, 250 ms on VICE, reading only the slots between
  the last seen count and the new one; the host zeroes the ring and its
  count before every boot and reads them back as zero; the ring read is
  bracketed by two `chip_sern` reads and re-read until the bracket is
  consistent, else the leg fails; a count read whose low byte is $FF/$00
  (it may be torn across the carry, off by 256, which a 1024-byte bracket
  no longer rejects by itself) is trusted only if a second read agrees;
  once `mach_run` has returned, one final read of the count (exact: the
  guest no longer writes) and the remaining slots takes the bytes printed
  after the last poll. The 256-byte ring could not serve the C64U, where
  one REST read takes ~112 ms and AROS writes 257 bytes (count 130 -> 387)
  inside one read; 1024 bytes hold the whole 416-byte m2 text. On the
  U64E/C64U each contiguous ring segment is read in ONE readmem GET (the
  transport's `read_memory`; verified up to 1024 B on C64U fw 1.1.0 and
  U64E fw 3.15, `docs/HARDWARE.md`), not `read_bytes`, which splits reads
  above 256 B into 128-byte GETs (VICE keeps `read_bytes`: the monitor
  read halts the 6510). A lossy capture (`lost` > 0) or a capture error
  makes the ablate leg INFRA, never a pass; `docs/HARDWARE.md` has the U64E read-path findings); m3: a conversion on the C64
  (bitmap, matrix, colour RAM, the VIC registers disp.s owns) equal to one
  of the boot model's insert-disk conversions (`tests/aros/model_frames.py`:
  every distinct `c2v_ref.disp_convert` of the model's display state at
  its vblanks, first seen at frame ≥ 345, the logo with its icons). On
  the U64E the hash leaves the screen matrix out (it lies under the I/O
  window and the running guest's `$01` is never touched; the model side
  hashes the same way); on VICE the 6510 is halted for the
  snapshot and `$01` is switched to `$34` and back to the value read.
  W12-V narrowed the U64E hash's blind spot (a right-pixel, wrong-palette
  screen differed only in the matrix): its hash now carries `disp_mck`,
  the 16-bit matrix checksum disp.s keeps in BSS (model key `u64`; a
  wrong palette collides with probability ~2⁻¹⁶, tested exactly for
  one-pen errors and swaps), and VICE checks `disp_mck` against the
  matrix it reads. The U64E/C64U bitmap and colour RAM are read in
  1000-byte GETs and read again if `disp_nconv` changed meanwhile. The
  matched model frame must show ≥ 3 distinct non-background colours
  (`visible`, asserted by `model_frames.py` for every insert-disk
  conversion and by the leg), and the leg renders the frame to
  `build/aros/m3_vice.png` or `m3_u64.png` (both Ultimates write the
  same name). On VICE the PNG is the C64's bitmap and matrix; on the
  Ultimates the matrix is not readable, so the PNG takes the bitmap and
  the cell pens from C64 memory and the pen colours from the model's
  `vic_pens`, and is written only if that matrix's sums equal the C64's
  `disp_mck`. The U64E leg re-hashes on
  every 2 s poll (not only after a new conversion), so one torn read of
  the final, static screen cannot make it time out. m3
  needs the blitter (W11-B): green on the merged tree (VICE s = 9: 174 s,
  guest frame 386; U64E s = 3: 86.8–98.3 s), red without it
  (`build/evidence/w11-aros/red/m3_noblit_v2.log`, the pre-merge PRG, no
  blitter, current hash: conversions match the model's frames 250 and 266
  only, never ≥ 345). With the blitter but `disp.s` ignoring the epoch
  (mutant `pixread`, `red/m3_pixread.log`) it is red as well: frames 250,
  266, 267 (converted on display-register changes), then no conversion
  of the later blit-only changes. Every conversion matched a model frame in
  order (250, 266, 267, 293, 345): the screen needs no CPU-drawn pixels
  beyond what the blits' epoch bumps (`disp_st+105`) make visible. Red: `ablate` (exception mode off: the CPU
  probe's MOVEC halts `HALT_ILLEGAL`, no m1); the base tree reaches m1 and
  (at s = 8 on VICE) m2: m2's enabling step is a working `mach_s` (VICE
  s = 4 does not reach it, above).
- **C4 (machine loop).** C4.2: `mach_wk` counts `mach_fhook`, with copper
  DMA on ("Interrupt delivery", STOP). C4.1: the idle "starvation at s = 0"
  is the arithmetic of a 1 MHz host (a 14188-phi2 frame holds ~23 guest
  instructions; the pass, the VERTB delivery and handler and a copper run
  fill most of it; at 48 MHz ~2%), pinned by leg `s0`. The AROS low-s
  stalls are NOT that arithmetic (fix round 1, HIGH-1):
  - VICE s ≤ 6: while a level-2 request (CIA-A PORTS) is pending under
    IPL 3, every pass clamps the one-shot to `MACH_POLL` = `$0400` phi2
    (~640 cycles per 1024 at 1 MHz), so the guest runs at ~37% for as
    long as it is in its level-3 handler; the handler then outlasts the
    frame, the kernel's level-3 dispatcher finds VERTB set again and never
    returns (no delivery for hundreds of frames), and no task runs. A
    build with `MACH_POLL` = `$4000` reaches m2 at s = 6 (158.7 s, frame
    2821). Fixed (supervisor grant): the poll interval is max(`$0400`,
    256 E << s) phi2, "Interrupt delivery"; leg `poll` (red 24% → green
    100% of the quiet progress), mutant `pollfix`; m2 at VICE s = 6 on the
    fixed build: 162.0 s, frame 2821.
  - **U64E s = 0 (not a defect, rev-aros round 2):** after m1 the boot
    runs at 25–39k guest IPS on the U64E, so s = 0 (14.2 ms frames) gives
    ~350–540 instructions per guest frame: at or below the model's limit
    (k = 32, 551 per frame: no m2 in 40k frames). s = 0 not reaching m2
    (2 of 2 runs, 27k frames) is consistent with that limit. s = 1
    (~680 per frame) sits next to the model's slowest passing case
    (k = 24, 676), which fits its one failure in the first measurement
    (later 6 of 6 passed). Model comparison (`tools/s3probe` boot model,
    CPU slowed k×,
    reviewer's `slowcpu2.py`):

    | k | instr/frame | m1 frame | m2 frame |
    |---:|---:|---:|---:|
    | 1 | 11.7k | 96 | 214 |
    | 16 | 906 | 1,334 | 4,921 |
    | 20 | 761 | 1,691 | 7,477 |
    | 24 | 676 | 1,869 | 12,087 |
    | 32 | 551 | – | none in 40k |
- **Risk (Workbench track).** A CIA timer.device underflow that becomes
  pending while an SR mask holds it (MOVE/ANDI to SR, a running handler) is
  taken up to one poll interval late after the mask drops (128–256 E, plus
  the host's recognition work above); a real 68000 takes it at once. A
  Disable()/Enable() pair through INTENA's INTEN is exact: Enable is a SET
  write, and `mach_ichk` kicks at once.
- **Backlog.** The boot entry is a test-time driver this wave; making it
  part of the product PRG (the banked BCODE area) belongs to the
  Workbench track.

## UCI REU I/O (W12-T)

Owner: snap (`runtime/ops/ureu.s`; W12-T exports in `dosio_uci.s`).
Tests: `host/tests/test_snap_ureu.py` (byte-level, the UCI model of
`test_hle_dos_uci.py` with `$21`/`$22` added test-side), the hardware probe
(`docs/HARDWARE.md`, W12-T).

- **Firmware facts** (1541ultimate `software/filemanager`, read at
  `~/Documents/1541u-315preview` @871ad034; dos.h/dos.cc relayed identical
  in the C64U 1.1.0 build): `dos.h:29-30` `DOS_CMD_LOAD_REU = $21`,
  `DOS_CMD_SAVE_REU = $22`; `dos.cc:254-319`: `[t][cmd][addr u32 LE][len
  u32 LE]`, addr masked to 24 bits, len to 25; no open file → `85,NO FILE
  OPEN`; `addr + len > 16 MB` → len clamped, status `02,REQUEST TRUNCATED`;
  the transfer is `file->read`/`write` at the file's current position;
  the reply is one block `"$%6x BYTES LOADED TO REU $%6x"` /
  `"... SAVED FROM REU ..."` carrying the TRANSFERRED count (a short file
  still says `00,OK`); a FatFS error replaces the status. The firmware's
  `sprintf` is its own `software/system/small_printf.cc` (linked into every
  target, e.g. `target/u64/riscv/ultimate/Makefile:40`): `%x` with a width
  is `_hex` (178-184), exactly `width` digits from `hexchar[] =
  "0123456789ABCDEF"` (11), zero-filled, upper case, truncated to the
  width, so a 256 KB chunk answers `"$040000 BYTES ..."` (fix round 1, H1:
  the first version expected C's space padding and lower case). The firmware
  writes the REU backing memory whatever the C64's REU config; the C64
  sees it only with the REU enabled and large enough (the hold sets it).
- **Entries** (MAIN; I/O in; BASIC banked out around the SCODE part by
  `ureu_bank`, the caller's `$01` restored): `ureu_ld` / `ureu_sv` = open
  `dosio_name` (`FA_READ` / `FA_WRITE|FA_CREATE_ALWAYS`) on DOS target 1,
  one transfer of `ureu_rl` bytes at REU `ureu_ra`, close (always; a save
  is flushed by the close). `dosio_uci_init` runs first (handshake reset,
  both targets closed). SCODE pieces for snap.s: `ureu_open`, `ureu_xfer`
  (A = `$21`/`$22`), `ureu_close`.
- **Chunks**: one command moves at most 256 KB (`UREU_CHUNK_HI`), so each
  reply arrives inside `dosio_uci.s`'s poll budget (65536 fenced polls,
  ~8 s at 64 MHz) and nothing is aborted mid-transfer. The C64 waits for
  every reply: a running guest is paused for the whole transfer.
- **The count is the truth**: every chunk must answer status `00` AND the
  exact text `$` + the chunk length as 6 upper-case hex digits,
  zero-filled (small_printf `%6x`) + space; anything else (a short or long count, a truncation,
  another text) fails with `ureu_err` = 5 (`UREU_E_SHORT`); a status other
  than `00` = 3 (`ureu_us` keeps its first two bytes); no reply = 2 (the
  handshake is aborted); open refused = 1; close refused = 6. On a failure
  `ureu_ra`/`ureu_rl` point at the failing chunk.
- **After a load (A1)**: `ureu_ld` ends EVERY load, failing ones too (a
  partial chunk may have landed), with `code_invall` (W12-C: no code window
  may serve the old bytes) and `disp_st+105` += 1 (the pixel epoch: the
  next frame converts the picture again, I2's rule). Any other caller of
  `ureu_xfer` with `$21` into guest memory carries the same obligation
  (snap.s's resume: its restore runs `code_invall` and the epoch bump).
  `ureu_sv` changes no guest byte and does neither.
- **Scratch**: the 8-byte command payload and the check's shift register
  sit in `dosio_buf + $F0..` (a reply keeps at most 40 bytes at its start).
  ureu is never used while an HLE dos.library file is open (machine mode
  has none).
- **Storage**: C64U `/SD/amiga64/`, U64E `/USB1/amiga64/`; never `/Flash` or
  `/Temp`. Device storage writes wait for the harness owner's decision
  (Wave 12 brief). Verified on the U64E (`tests/ureu_probe.py --adf`, the
  harness storage API c1a2095): LOAD_REU of a 901,120-byte ADF checksummed
  on the C64, SAVE_REU read back over FTP, both at 48 and 1 MHz; and on the
  C64U (`/SD`, harness f384ad0) at 64 and 1 MHz (HARDWARE.md). The file
  legs take `--vol` (default `/USB1`; the C64U: `/SD`). A probe enables the Command
  Interface inside its `u64_hold` (both devices keep it Disabled; the
  hold restores it).

## Suspend/resume (W12-T)

Owner: snap (`runtime/ops/snap.s`, `host/snapfmt.py`, rc `snap` region).
Tests: `tests/aros_resume.py` (legs `mid`, `m3` with the refusals,
`--mutants`), `host/tests/test_snap_fmt.py`, `host/tests/test_snap_file.py`
(the file path at host level: the 6502 code on a REU-controller model and
the UCI model).

- **Trigger**: `snap_arm` (`tests/aros/mboot.s`'s W12-T line, after
  `cop_reset`/`disp_init`, before `mach_run`; 31 φ2 per guest frame with no
  request, PERF "Suspend hook cost") puts `snap_fhook` in `mach_fhook` and keeps the displaced
  hook (`cop_frame`) in `snap_chain`, which it calls first. The frame hook
  runs in `mach_run`'s service pass after `emu_run` returned: an instruction
  boundary, `$01 = $35`. The host sets `snap_req` bit 0; the suspend runs
  at the next frame hook (≤ 1 guest frame). Bit 6 also writes the file
  (`dosio_name`), bit 7 continues the guest afterwards; without bit 7 the
  guest freezes (`sei`, spin): a REU-resident snapshot is valid only while
  nothing runs. `snap_st`: `$10` suspended, `$11` + file,
  `$20` resumed, `$E1-$EA` refused (`host/snapfmt.ST_ERR`).
- **The file path (bit 6)**: pinned at host level (`test_snap_file.py`:
  the spans in order, a resume from the file, the refusals) and verified on
  the U64E (fw 3.15, 48 MHz, `aros_resume` leg `m3file`): suspend at m3 to
  `/USB1/amiga64/snap-test.bin` (5.2 MB, 3.1 s), reset, the whole REU
  zeroed, resume from the file (1.2 s), picture again in 3.7 s; and on the
  C64U (fw 1.1.0, 64 MHz, `--vol /SD`): `/SD/amiga64/snap-test.bin` in
  15.4 s, resume from the file 5.6 s, picture again in 10.7 s.
- **Consumed (M1)**: a successful resume, and a suspend that continues the
  guest (bit 7), overwrite the REU header's first magic byte
  (`snap_kill`): the REU-resident snapshot can then never be resumed over
  guest memory that moved on (a second resume refuses `$E2`: the header no
  longer checks). A file keeps its own copy.
- **Snapshot area** (rc `snap $DF1000-$DF3FFF`, the REU under the trapped
  custom-chip bank, never guest-visible, both profiles): `+$000` header,
  `+$100` zero page, `+$200` the `disp_st` page, `+$300` the MAIN BSS image
  `__BSS_RUN__ .. snap_keep` (stashed, summed and filed as whole pages,
  restored exactly), `+$2E00-$2EFF` the scratch-page save of a resume
  (F1, `SNAP_SAV`; in no file span, carved out of `zero_check`),
  `+$2FE0` the declared ROM id (32 bytes, in no file span). Link asserts:
  image end <= `SNAP_SAV`, `SNAP_SAV` + 256 <= the ROM id. Layout and header: `host/snapfmt.py`.
- **Resume** (`snap_resume` from a FRESH runner load; does not return while
  the guest runs): with bit 6, LOAD_REU span 0 (the header) first. Checks,
  all before anything is restored (a refusal leaves `mach_on` 0): the
  build is stamped (`snap_bid` ≠ 0, else `$E8`); the header checksum
  (`$E2`); the header equals what this build would write, flags and state
  sum aside (magic/version `$E1`, layout `$E9`, build ID `$E3`, ROM id = the one declared
  at `+$2FE0` `$E4`; an all-zero declared ROM id `$EA`); then (file) span 1, the state checksum (`$E5`), then
  the guest ranges. Restore: `disp_init`; `st_exc` from the image, then
  `emu_reset`; zero page; `disp_st`; the BSS image; the CIA #2 counter;
  the pixel epoch `disp_st+105` += 1 (I2); `code_invall` (I1, W12-C: AFTER
  the BSS restore -- the image holds `cw_val`/`cw_tlo`/`cw_thi`/`cw_hand`/
  `cw_mok`, while the windows (CWIN) and `cwin_map` are not in it, so the
  restored tags would otherwise claim windows the fresh load never filled:
  duplicate valid tags and a map that no longer counts them. Checked on
  VICE right after every resume (`aros_resume` `cwin_check`: W12-C's
  directory against its page-low map); mutant `noinvall` fails it); `snap_kill` (M1);
  `mem_rec_dirty`; then `mach_run` (`mach_on` = 1: no `mach_init`).
- **Resume scratch**: before the restore every byte of the image range is
  free, so the loaded header is fetched to `__BSS_RUN__` for the layout
  compare. Since W12-C that page also holds `cw_*`, `m_scmd` and
  dosio_uci.s's state, which the file loads that follow write: only the
  compare uses the copy; its state sum is kept in SBSS (`snap_ss`) first.
  **Refusal guarantee (fix round 3, F1)**: `snap_res` saves that whole page
  to the REU at snapshot `+$2E00` (`SNAP_SAV`, between the BSS image and the
  declared ROM id, in no file span) before anything else, and `snap_fail`
  puts it back on EVERY refusal (`$E1-$EA`), after the file is closed: a
  refused resume leaves the scratch page, and with it W12-C's `cw_val`/
  `cw_hand`/`cw_mok` and dosio_uci.s's state, byte for byte as they were,
  so a fallback boot on the same load (refuse, then `aros_boot`) starts
  from a sane cache. Tests: `test_snap_file` (every file and REU-resident
  refusal), the `m3` leg's refusals on every backend (`cw_val` all 0,
  `cw_hand` < `CWIN_N`, the page unchanged), mutant `noscr`.
- **`mach_s` after a resume (A3)**: `mach_s` (DATA) is not in the image: a
  resumed runner's `mach_s` holds the fresh-load default, not the shift the
  machine runs at. The E-clock shift in force is `mach_sl` (BSS, latched
  by `mach_init`, restored with the image): host tools and code must read
  `mach_sl`, never `mach_s`, to learn the shift of a resumed machine.
- **What `mach_run`'s entry overwrites** (the image's values of these never
  take effect): `mach_port` := the caller's `$01`; `mach_ev` := `ev_pend`,
  `ev_pend` := 0 (a pending event is serviced by the entry pass, as at any
  entry); `mach_stp` := 0, `mach_fs` := 0; `mach_in` := `$81`;
  `mach_svc` resets `mach_insv`/`mach_fran` per pass. The suspend runs
  inside a service pass, so the restored machine re-enters exactly like a
  `mach_run` entry after a halt: the remainder of that pass (re-reading
  the counter, counting passed frames, arming the one-shot) is redone; the
  displaced hook's forced step (`mach_fs`) is dropped.
- **STOP across a suspend** (known deviation): a guest suspended while
  STOPped (`mach_stp` = 1: the suspend can run in mach_run's STOP wait) is
  resumed RUNNING: `mach_run`'s entry clears `mach_stp` (frozen code), so
  the instruction after the STOP executes before the next interrupt, a
  spurious wake-up. AROS's idle loop STOPs again (the m3 suspends happen
  there; the picture and serial continuation pass). A fix needs mach.s
  (re-enter the STOP wait when the image's `mach_stp` was set).
- **REU-resident resume (interim, until device storage is allowed)**:
  the snapshot and the guest's memory stay in the REU across the harness
  reset and the fresh runner load. Proven per run, not assumed
  (`aros_resume` `survive`): the state fletcher the C64 computed at
  suspend (header +96, under the header checksum) equals `aros_sum` of the
  same span after the reset; the REU config is re-set by `prepare_target`
  inside the hold before the boot and before the resume, and read back.
- **Nothing else writes the snapshot region**: the memory API traps every
  guest access to `$DF0000-$DFFFFF` to chip.s, which never addresses the
  REU (no REC programming in chip.s/cia.s/cop.s); the only REU users under
  `$DF` are disp.s (the LUT, `$DF0000-$DF0FFF`, and W13-D's zero row
  `disp_zero`, `$DF4000-$DF40FF`, all zero: `zero_check` still holds) and snap.s (the snapshot
  area, and `SNAP_SAV` `+$2E00-$2EFF`: snap_res's own save of the scratch
  page at every resume entry, F1). Checked after m3 on every backend: the
  REU from the end of the BSS image to `SNAP_SAV`, from `SNAP_SAV` + 256 to
  the declared ROM id (empty today), and `$DF4000-$DFFFFF`, read zero
  (`aros_resume` `zero_check`, `SNAP_SAV` carved out).
- **REU size**: `snap_resume` (and the suspend) require the REU configured
  at 16 MB: the snapshot area, `SNAP_SAV` and the declared ROM id lie at
  `$DF1000-$DF3FFF`; a smaller REU wraps those addresses into guest memory
  (the AROS boot needs 16 MB anyway; `prepare_target` sets it).
- **Builds the hardware legs ran on**: the U64E file legs (ADF probe,
  `m3file`) and the U64E/C64U REU-resident legs of fix round 2 on
  `d371b5987ec1cf7e`; the C64U REU-resident legs of fix round 3
  (`c64u/final7.log`) on `d5b5c8149c639820` (= 6886c98's runner); the
  C64U file legs (ADF probe at 64/1 MHz, `m3file`, volume `/SD`,
  `c64u-sd/`) on `d5b5c8149c639820`.
- **Link order** (L6): snap.s then ureu.s link last (Makefile), so the
  image `__BSS_RUN__ .. snap_keep` holds every other module's BSS and none
  of W12-T's scratch. `snapfmt --stamp` refuses a runner whose BSS does not
  end with ureu.s's (`ureu_bend = __BSS_RUN__ + __BSS_SIZE__`, `snap_keep <
  ureu_ra`); snap.s asserts at link time that its scratch (the loaded header
  at `__BSS_RUN__`) never overlaps `dosio_buf`.
- **Build ID** (I3): `snap_bid` (DATA) = the first 8 bytes of SHA-256 of the
  PRG with those 8 bytes zeroed, stamped by the Makefile after the link
  (`host/snapfmt.py --stamp`). Other links (variant builds) stay 0 and can
  suspend but never resume. **ROM id** (I3): a host-declared TAG, not a
  hash the C64 verifies: 32 bytes in the REU at snapshot `+$2FE0`, written
  by whoever staged the ROM (the tests: SHA-256 of `aros-ext.bin ||
  aros-rom.bin`, `tests/aros_resume.rom_id`). The C64 never hashes the ROM;
  suspend copies the tag into the header, resume compares the header's tag
  with the one declared now and refuses an all-zero tag (`$EA`). (In the REU, not MAIN: 32 bytes of
  DATA tipped the MAIN layout over a page, see the Wave 12 report.)

- **W13-W module ID** (`SNAP_VER` 2): a snapshot taken with the td module
  armed carries `td_vec`/`td_mid` in the image; the resume driver re-stages
  the modules first, and a module whose ID differs is refused AFTER the
  restore, by MAIN code: `td_call` calls nothing and the first frame hook
  halts the machine with st_halt 1, st_reason `TD_HALT_MOD` = `$57`
  (`snap_st` already reads `$20`). Snapshots without td (`td_vec` = 0) are
  unaffected. A `SNAP_VER` 1 file is refused `$E1` (`test_snap_file`
  case "version 1").

### State inventory

Every C64-side byte the guest's progress depends on, and what happens to
it. "Image" = saved and restored byte for byte. Wholesale by design: the
image is every module's BSS, so a BSS byte added later is covered without
an edit here (snap.s/ureu.s link last; only `snap_chain` of theirs lies
before `snap_keep`).

| State | Where | Suspend | Resume | Why |
|---|---|---|---|---|
| 68k registers D0-A7, PC, SR | zp `$10-$55` | image | restored | the guest CPU |
| opcode, memory-API args, EA, ALU operands/result, scratch | zp `$56-$77` | image | restored | dead at an instruction boundary; restoring is harmless |
| `st_halt`/`st_reason` | zp `$78-$79` | image | restored | `mach_run` takes a racing event bit as on any entry |
| interpreter-internal | zp `$7A-$7F` | image | restored | as above |
| fetch window `w_pg0/w_pg1/w_inv`, REC shadow `m_rok..m_rbk` | zp `$80-$87` | image | NOT restored: `emu_reset` sets `w_inv`, clears `m_rok`; `code_invall`, `mem_rec_dirty` | caches: the restored values would describe the fresh load's REC/window |
| `st_exc`, `r_osp`, `st_ipc`, `st_iop`, `st_sp6`, `st_exl` | zp `$88-$94` | image | `st_exc` first (latched by `emu_reset`), then all | supervisor mode, inactive SP, the exception snapshot (`st_sp6` is re-taken per instruction) |
| `ev_pend`, `mach_frames` | zp `$95-$99` | image | restored | pending events, the frame count |
| loader scratch, `$00/$01`, free zp | zp `$00-$0F`, `$9A-$FF` | image (not used) | not restored | runner/harness or hardware, not guest state |
| step-loop patch (`nx_br`, `step_jv`, `run_jv`) | CODE (SMC) | — | rebuilt: `emu_reset` with the image's `st_exc` | the only persistent self-modification (others are per instruction) |
| W12-C code cache: `cw_val`/`cw_tlo`/`cw_thi`/`cw_hand`/`cw_mok` (BSS); the windows (CWIN) and `cwin_map` | BSS / CWIN | BSS part in the image; windows and map not | `code_invall` after the BSS restore drops every window | a cache of guest memory: refilled on demand |
| chip shadow, serial ring + count, log | `chip.s` BSS | image | restored | Paula/INTREQ/INTENA state; the ring continues the text |
| CIA-A/B state (timers, ICR, TOD, ports) | `cia.s` BSS | image | restored | guest CIAs live in E time (event slots) |
| copper state, `cop_dhook`, `cop_sh` | `cop.s` BSS | image (`cop_dhook` re-set by `disp_init`, then overwritten by the image) | restored | the copper |
| display state block | `disp_st` `$B800-$B8FF` | image page | restored; +105 bumped | written by cop/blt, read by disp |
| blitter state | `blt.s` BSS | image | restored | blits are synchronous: no blit is in flight at a frame hook; row buffers `$B600-$B7FF` are scratch |
| display driver (`disp_cur`, budget, epoch) | `disp.s` BSS | image | restored | the next conversion decides as before; the bumped epoch forces one |
| machine loop (deadlines, slots, `mach_on`, `mach_fhook`, `mach_now`, `mach_chi`) | `mach.s` BSS | image (`mach_rdc` first) | restored | event seam and frame timing, absolute in the time base below |
| `snap_chain` | `snap.s` BSS (before `snap_keep`) | image | restored | the chained frame hook |
| HLE / dos / con / mem state | `hle*.s`, `dosio_*.s` BSS | image | restored | inert in machine mode (no HLE traps); restoring keeps them consistent |
| group scratch (`v_*`), `mach_s` | DATA | not saved | fresh load | `v_*` per instruction; `mach_s` is read by `mach_init` only (latched in `mach_sl`): after a resume read `mach_sl` (A3) |
| CIA #2 TA/TB (the time base) | hardware | in the image as `mach_now` | reloaded to `~mach_now` (TA ≥ `$0100`: ≤ 255 φ2 earlier) | every deadline is absolute in this counter |
| CIA #1 TA one-shot, ICR mask, IRQ/NMI vectors, `$01` | hardware / `$FFFA` | — | rebuilt by `mach_run`'s entry (`mach_vec`, `mach_svc` re-arms) | mach_run's own entry contract |
| VIC registers, `$DD00` bank, bitmap, matrix, colour RAM | hardware / `$D000`, `$E000` | — | rebuilt by the forced conversion (I2) | derived from `disp_st` + guest memory |
| c2v pair tables, `c2v_args` | `$B900-$BAFF`, `$BF00` | — | `disp_init` → `c2v_init`; args per conversion | derived |
| LUT | REU `$DF0000` | — | `disp_init` stashes it | derived |
| W13-D zero row `disp_zero` | REU `$DF4000-$DF40FF` | — | `disp_init` stashes it | derived (zeros) |
| W13-D view state `disp_view`/`px`/`py`/`ymax`/`one` | `disp.s` BSS | image | restored | the 1:1 view survives a resume |
| W13-D D2 `spr_vec`, `spr_id` | `disp.s` BSS | image | restored as saved; the module they point at is NOT in the image: `disp_vw` compares the module's ID (`spr_vec` + 3..6) with `spr_id` every frame hook and disarms (`spr_vec` high byte := 0) on a mismatch | the hand-off rule below re-stages and re-arms |
| W13-D D2 pointer module, its state | `$C600-$C8FF`, `c2v_args` + 180..212 | — | re-staged and re-armed by whoever launches the session (hand-off rule); its state is zero when staged and rebuilt from `chip_shd` + guest memory | test-time module, not in the PRG |
| W13-D D2 sprite blocks, pointers | RAM under I/O `$D400-$D4BF`, `$D3F8-$D3FA` | — | rebuilt by the module's first image read | derived |
| guest memory | REU chip RAM, slow RAM + `$DC0000-$DEFFFF`, ROM banks | stays (REU-resident) / file spans 2-5 | stays / LOAD_REU | the guest's RAM. AROS WRITES `$DC0000-$DEFFFF` (unmapped in the profile; not zero after m3 on both Ultimates), so the slow-RAM span runs to `$DEFFFF`. `$200000-$BEFFFF` and `$E80000-$F7FFFF` read zero after m3 (`aros_resume` m3 leg, Ultimate) and are not filed: a resume needs them zero, as the boot does (`host/machine.ZERO_SPANS`) |

## Input (joystick -> mouse) (W13-I)

Owner: inp (`runtime/ops/inp.s`; in `chip.s` the POTINP read, now `jmp
inp_pot` padded to its old size so no module moves, labelled `; W13-I`;
one call line in `mach.s`). Design: `docs/WAVE13_BRIEF.md`
"W13-I". Reference: `host/inp_ref.py` (`Inp.frame` the rule, `scan` the
CIA1 side). Tests: `tests/run_inp.py` (+ `fixtures/inp/inp_log.s`),
`host/tests/test_inp.py`.

- **When.** `inp_frame` runs once per machine-loop service pass that
  posted a VERTB: `mach.s` `fhook` calls it first (`jsr inp_frame ;
  W13-I`), whether or not a frame hook (`mach_fhook`) is installed, with
  `$01` = `$35`, A/X/Y clobbered. A pass that catches up several frames
  runs it once, and VERTBs that pass during the hook are counted, not
  hooked (the existing `fhk`/`skip` rule). So every VERTB the guest sees
  has had exactly one `inp_frame` since the previous one (test `guest`).
  Nothing is on the per-instruction path (`perf_cycles --max-delta 0`).
- **Source.** `inp_src` = 0: the real CIA1 is scanned into `inp_joy` and
  `inp_key`. Otherwise those two bytes are the host's override, the test
  seam for VICE and unattended hardware legs. `inp_joy` bits 0-4 =
  up/down/left/right/fire, 1 = pressed (the harness's `inject_joystick`
  polarity). `inp_key`: `$10` = F1, `$20` = C= (the keys' matrix rows).
- **Scan** (the only CIA1 port reader in machine mode). Port 2 drives the
  keyboard's column lines (port A), so the scan keeps them apart like this:
  1. **Stick.** DDRA = `$00`: nothing is driven, so only port 2 can pull
     PA0-4 low. joy = `~PRA & $1F`. A held key cannot reach it, because no
     row is low. DDRB is never written: it stays `$00` (KERNAL, hle_con).
     The one way through is a row pulled low from outside (a port-1 stick)
     *and* a key held on that row in columns 0-4: the stick then reads that
     column's direction, as on any C64. On the C64U (2026-10-09), rows 6-7
     driven low by a test read as DOWN (column 1), consistent with SHIFT
     LOCK latched (column 1, row 7). Port 2 also read UP held at every
     speed, even with PA0 driven high: something outside the CIA holds it,
     the stick or its cable (`build/evidence/w13-inp/green/c64u_pa_diag.log`).
  2. **Idle.** PRA = `$FF` first, then DDRA = `$FF` (the latch is high
     before the outputs turn on, so the old latch never drives PA). A PRB bit low with no column
     selected is a row held by port 1, or by a key in a column the stick
     pulls. That key is *unknown* this frame and keeps its last state:
     `key = ((new ^ old) & idle) ^ old`. Cases: F1 with the stick up; C=
     while the stick pulls the column of S/F/H/K/F3 and that key is held.
  3. **Keys.** F1 is read on column 0 (PRA `$FE`) row 4, then C= on column
     7 (PRA `$7F`) row 5, with no settle wait between them.
     - MEASURED (rev-inp, C64U, 2026-10-09: `build/evidence/w13-inp/settle/c64u_settle.log`,
       probe `build/evidence/w13-inp/settle/settle.s`):
       rows recover at once at 1, 48 and 64 MHz, and a column 1 → column 7
       read shows no phantom.
     - The U64E is unmeasured.
     - A held F3 (column 0, row 5) reading as C= is checked by the attended
       script's F3/C= step.
  4. **Rest (W14-M).** On exit the scan leaves CIA1 port A selecting the
     mouse port's pots for the SID: PRA bits 7-6 = `01` (port 1) or `10`
     (port 2), written before DDRA = `$C0`. PA7/PA6 are also keyboard
     columns 7/6, so between scans the rest drives column 7 (port 1) or
     column 6 (port 2) low.
     - Every port-1 read (PB) and every row read other than a deliberate
       column scan is made with DDRA = `$00` (step 1). Otherwise SPACE
       (column 7, row 4) would read as the port-1 mouse's left button and
       "1" (column 7, row 0) as its right button.
     - VICE cannot hold a key, so `host/tests/test_inp.py::
       test_port1_mouse_not_clicked_by_column7_keys` models it. It no longer restores the
     caller's DDRA/PRA (W13-I's precaution). Callers that read CIA1 set it
     up themselves (`hle_con.s:401-405`), and no runtime code calls the
     KERNAL. DDRB and PRB are never written. (Leg `real` checks it.)
  - **Cost** (leg `cost`: 6502 cycles per call, VICE, screen off, the
    minimum of 5; CPU speed does not change it):
    - the real scan with nothing held: 277;
    - the override, idle: 166;
    - the override with every bit set: 245;
    - worst case: 356 (the real scan plus all four mouse-loop bits);
    - the real scan costs 111 more than the override.
    rev-inp's independent measurement agrees (277/166/245/356).
  - **Lateness.** `run_irq` `lag` (s = 0, screen on) is gated with the
    override at P = 3200 for drift and max; see "Event seam".
    - The W13 frame hooks moved the servable edge from < 3000 to
      ≈ 3050-3100 φ2. The old P = 3000 numbers ("2921-3485 over 7 runs")
      were fresh-session samples of a drifting series.
    - With the real scan at P = 3200 × 240: max 2779-3106 φ2, slope
      +183 to +292 (3 runs, recorded only).
  - **Coverage gap.** A real key or stick press through CIA1 (the scan's
    positive path) is covered only by the attended script
    (`tests/run_inp.py --attended`):
    - VICE cannot hold a matrix key and ignores JOYPORT_SET;
    - the Ultimate's `inject_joystick` writes the PRA latch only;
    - the C64U's stick reads Up held.
    The unattended legs cover the scan's isolation: rows driven low, the
    undriven reading, the CIA1 restore.
  - The Ultimate's `inject_joystick` writes the PRA *latch* (c64-test-
    harness `backends/ultimate64.py` `inject_joystick`). With DDRA = `$00`
    the latch is not on the pins, so it cannot reach `inp_joy` (leg
    `noinj`). VICE 3.10 ignores the harness's JOYPORT_SET: the probe on
    2026-10-09 saw no change at `$DC00`/`$DC01` with any controlport or
    joydev setting. The real-scan leg therefore holds rows low through
    DDRB/PRB instead.
- **1351 mouse (W14-M).** A Commodore 1351 in proportional mode is the
  Amiga's port-0 mouse.
  - **Facts used:** SID POTX/POTY (`$D419/$D41A`) give the selected port's
    position mod 64 in bits 1-6; bit 0 is noise; the SID samples every 512
    φ2 on its own clock. CIA1 PA7-6 select the pot port. The left button is
    the port's fire line, the right button its UP line.
  - **Ports.** `inp_mport` (host): 0 puts the mouse on port 1 and the stick
    on port 2 (the default); 1 swaps them. Step 1 reads both ports with
    DDRA = DDRB = `$00`: port 1 is `~PRB & $1F` (no column is driven, so no
    key reaches it), port 2 is `~PRA & $1F`.
    - The mouse port's lines go to `inp_mb` (bit 4 left, bit 0 right). The
      other port is the stick (`inp_joy`, with `inp_stuck`).
    - The one electrical path left is the same as on any C64: a port-2 line
      held low (for example the stuck UP, column 0) plus a key in that
      column shorts the key's row. With the mouse on port 1, the column-0
      keys (DEL, RETURN, the cursor keys, F1/F3/F5/F7) then read as port-1
      lines: F1 → row 4 → the left button, DEL → row 0 → the right button.
    - The keys behave as in W13-I: a mouse button held makes its row
      unknown, so F1 keeps its state while the left button is down.
  - **Sampling.** The frame hook runs only once per guest frame, about
    8.7 Hz at s = 3. The 1351 aliases beyond ±31 counts per read, so the
    pots are sampled by **mach event slot 7**:
    - callback `inp_msamp` (MAIN: `ldx #3` into `inp_mod`'s ID guard).
      The host stores `mach_cbv[7]` and the period `inp_mper` (E ticks).
      The launchers use 8192 φ2 = `$2000 >> s`, about 120 Hz in real time
      at any s (≈ 3.7k counts/s).
      - `inp_mper` is u32 in `marm`'s add, but the enable check
        (`inp_mrearm`) reads only its low 16 bits: keep it ≤ `$FFFF`
        (bytes 2-3 zero).
    - each sample re-arms itself (`marm`, LOWCODE, with a fresh
      `mach_etime`). The frame hook also re-arms at now + `inp_mper` after
      every scan, so a sample is always at least one period after the last
      port-select change. The SID needs one full 512-φ2 window. The wait is
      measured in the time base's E ticks (CIA #2 φ2), not CPU loops, so it
      holds at any CPU speed.
      - Leg `msamp` checks it: after a scan, slot 7 is due 0..`inp_mper`
        E ticks ahead, even ~80 ms after the last pass. Mutant `stale`
        (`marm` without `mach_etime`) is due −10287.
      - It also counts the samples: at s = 3 under VICE (1 MHz), 9.1 a
        frame, with each sample's pass delaying the next; the Ultimate at
        64 MHz approaches 13.9.
    - **The override and slot 7 (supervisor's ruling, rev14-mouse
      MEDIUM-4):** with `inp_src` = 1 and `inp_mper` ≠ 0 the slot runs too,
      and decodes the host-written `inp_mpot` (the scripted e2e mouse);
      otherwise `inp_mpot` := the SID's pots. `run_irq`'s rig stays safe
      through `inp_mper` = 0 (leg `guest` asserts `mach_am` bit 7 clear).
  - **Decode** (the input module's segment B; `host/inp_ref.m1351_delta`).
    Per axis, v = pot & `$7E`, d = (v − old) mod 128 as a signed 7-bit
    value / 2 (−32..31); the Y delta is negated (the 1351's Y counts up,
    the Amiga's down).
    - **The per-frame clamp** (rev14-mouse HIGH-1). AROS reads JOY0DAT once
      a guest frame as a signed 8-bit delta, and about 14 samples land in
      one frame at s = 3. So the counts added since the last frame hook
      (`inp_macc`, s8 per axis) saturate at ±127, and only the clamped part
      is added. 14 samples of +31 deliver +127 (`host/tests/test_inp.py`,
      leg `mouse`), not a wrap (+217 had moved the pointer −19 px).
      - The stick's ≤ 32 per frame comes on top, so a full-speed stick and
        mouse together can still exceed 127.
    - The scale is 1:1, one 1351 count per Amiga count.
    - The first sample after `inp_mok` = 0 only sets the reference. The
      module also clears `inp_mok` when `inp_mport` or `inp_src` changed
      since the last frame (`inp_mps`).
  - **Buttons.** Left = (stick fire | mouse left) → /FIR0. Right = C= |
    mouse right → POTINP DATLY (`inp_pot`).
  - **The input module** (`tests/aros/inpm.s`; the supervisor's ranges,
    asserted).
    - **Segments:** A at `$C980-$C9FE` (89 of 127 B: entry, ID, the B check,
      `look`) and B at `$CF00-$CFEF` (219 of 240 B: the decode, the clamp,
      the queue).
    - **$CF00-$CFEF in machine mode** is otherwise unused: mboot uses `$CE00`
      and `$CFFF`, the Ultimate dispatcher `$CE00-$CEFF`, `run_irq`'s drv
      `$CFF0`, and `run_super_vectors`' `$CF00-$CF9F` only in its own HLE
      sessions.
    - **One ID** (SHA-1[:4] of A + B with both ID fields zero) is stamped
      in both. MAIN's `inp_mod` compares `inp_mon` (ID byte 0) with A+3;
      A compares all 4 bytes of B's ID with its own before calling B,
      else it disarms (`inp_mon` := 0).
    - **MAIN** (in `inp.s`): `inp_mod`, `inp_mrearm` (LOWCODE), and BSS
      `inp_mon`, `inp_ml`, `inp_mlast`, `inp_mq`, `inp_md`, `inp_macc` (2),
      `inp_mps`, `inp_mns` (u16 sample count, tests). Both call sites end in
      it: `inp_frame` with X = 0, `inp_msamp` with X = 3.
    - **No module** (`inp_mon` = 0, or no match): the stick and the
      mouse's buttons (`inp_mb`, M1) only. The 1351's position needs the
      module (it is AROS-only; supervisor, fix round 1). Slot 7 keeps
      re-arming without reading anything.
    - **Each call** looks at the mouse lines: `inp_ml` with the override,
      else CIA1 with DDRA = `$00`, then the select back. Every left press
      edge is counted in `inp_mq`, which holds at most 2.
    - **The frame call** delivers one left transition per guest frame:
      - delivered, and (up now or a press pending) → released;
      - not delivered, and a press pending → pressed.
      So a click shorter than a frame is one pressed frame, and two clicks
      inside one frame are P, R, P, R (leg `queue`, `host/inp_ref.Queue`).
      Then `inp_mb` := delivered left | right now, /FIR0 := stick fire |
      delivered left, and `inp_macc` := 0. With the module armed, the
      scripted mouse's lines go in `inp_ml`, not `inp_mb`.
    - **The real CIA1 path** with the module armed (leg `mreal`): port-1
      lines are driven low through DDRB/PRB. The legs check
      `inp_mport` 0/1 routing (the mouse vs the stick), that `inp_stuck`
      masks the stick only, and DDRA = `$C0` after every frame.
      - Keyboard isolation at turbo (PA7 released 4 cycles before the PRB
        read) is VICE-impossible. The attended `kbiso` window covers it.
    - **Resume.** The rule is the same as spr: a resume comes up with the
      module **disarmed**, and the launcher re-arms it.
      - `snap.s`'s restore zeroes `inp_mon` next to its `spr_vec` store
        (supervisor, at the merge). `host/tests/test_snap_file.py` has the
        test; it is an expected failure until the store lands.
      - `tests/aros_wb.arm_inpm(m)` runs after staging and after every
        resume (`resume_workbench`, alongside `arm_spr`). `run_inp` arms it
        the same way (`stage_inpm`).
      - The module's state is MAIN BSS and in the image. `arm_inpm` resets
        the queue to idle.
    - **Limits** (supervisor, accepted):
      - the right button is not queued: it is the line's state now;
      - the stick's ramp is not time-scaled by elapsed guest frames: a
        catch-up pass moves one frame's step. The faster ramp below is used
        instead.
  - **Cost** (leg `cost`, VICE, cycles):
    - the frame hook: 327 (the real scan) / 198 (override, idle) without
      the module; +228 / +204 with it armed. The re-arm, when sampling is
      on, adds +383.
    - a sample: 743 with the module (decode, clamp, queue look, re-arm),
      400 without (the guard and the re-arm only).
    - Per second these scale with the rates: samples at ≈ 120 Hz real time
      on the Ultimate (any s), frames at 69.44 or 72.08 / 2^s Hz (PAL or
      NTSC φ2; 8.7 / 9.0 at s = 3). That is ≈ 89k + 5.3k cycles/s, ≈ 0.15 %
      of 64 MHz. On a 1 MHz VICE at s = 9 a 16-E-tick period would mean
      ~890 samples a frame, so the scripted pots are sampled every 2048 E
      ticks there.
  - **Ramp:** not time-scaled (the limit above). The stick's ramp is
    faster instead (W14-M): step = 1 + n, cap `INP_CAP` = 32 counts per
    frame (was 1 + n/2, cap 16).
    - Measured on VICE (`aros_wb --e2e-ramp`, the pointer read every guest
      frame, the AROS lag excluded: the sprite moves 2 frames after the
      counters; AROS shows 2 counts per lores pixel): 1.75 lores px/frame
      over the first 4 moving frames and 16.0 at the cap.
    - At s = 3 that is 15.2 and 138.9 px/s (PAL φ2) or 15.8 and 144.2 px/s
      (NTSC φ2). 320 lores px take 2.2-2.3 s at the cap.
  - **Known limit: the 1351 at turbo; planned fix** (supervisor: approved
    in principle for the next session).
    - **Measured** (C64U, a genuine 1351 in port 1,
      `build/evidence/w14-mouse/diag/run1.log`, `run_inp --attended
      --mouse-diag`):
      - 1 MHz, no frame scan: POTX `$51-$D3` (120 values), POTY `$4D-$CE`,
        the full range;
      - 64 MHz, with or without the scan and the module: POTX `$60-$80`
        (33 values), POTY `$5C-$7C`.
      The 64 MHz turbo compresses the SID pot readings to ~1/4. The pointer
      stays in a small box (the decode sees each wrap as a reverse move).
      It is the same with the 8580s, UltiSID, and Paddle Override on or off.
      The buttons are right = PB0 and left = PB4 at both speeds.
    - **Planned fix:** drop the CPU to 1 MHz only around each pot read.
      - With Turbo Control in register mode ("C64U Turbo Registers" / "U64
        Turbo Registers"), the 6510 writes the speed index to `$D031` (0 = 1
        MHz … 15 = 64 MHz on the C64U):
        - 1541u-315preview `software/u64/u64_config.cc:313`
          (`speeds_u64ii[]`), `:316-317` (`speed_regs[]`,
          `speedregs_regvalues[]`), `:1599-1610` (`C64_TURBOREGS_EN`);
        - the bench notes `~/Documents/1541u-turbo-drafts/close-61.md:11-17`
          (`$D031` changes the clock 47×, reads back; `$D030` reads `$FF`);
        - `clock-issue-draft.md:72-83` (the C64U name of the mode; writes
          are ignored for ~2 s after a reset, so prove the register live
          first; the CIA #2 time base does not move with `$D031`).
      - With "Manual" (today's holds) `$D031` does nothing.
      - **The sampler:** save `$D031`, write 0, wait W φ2 on the time base
        (CIA #2 or raster lines, never a CPU loop; about 2 SID cycles ≈ 1.1
        ms), read POTX/POTY, restore `$D031`.
      - **Gating:** by a host byte (the speed index to restore; 0 = never
        touch `$D031`), set only by an Ultimate launcher that has set the
        register mode and proven it live. VICE and 1 MHz are unchanged.
      - **Cost:** at 120 Hz ≈ 13 % of the throughput, so sample at ≈ 50 Hz
        (≈ 5.5 %, still 31 × 50 ≈ 1.5k counts/s). W and the rate are
        host-set.
      - **First, a confirming diag D:** the register mode, liveness, then W
        = 0 / 0.6 / 1.1 / 2.2 ms while moving right. If no W restores the
        range, fall back to a turbo-gated mod-32 decode of the window
        (≈ 4× coarser, possibly device-specific).
  - **Closed loops on the pointer** (scripted input, the demo helpers).
    AROS moves the pointer sprite from JOY0DAT a few guest frames after
    the counters change, so a sprite position read right after a move is
    stale. `tests/aros_wb.py E2E.mgoto` therefore moves at most half the
    remaining distance per step, waits 2 frames, and stops only on a
    position that still holds 4 frames later. Its first version stopped on
    a stale position 23 lines past the icon, and the double-click missed.
  - **VICE** emulates a 1351 (`-controlport1device 3`) but takes it only
    from the host mouse. The harness's binary monitor has no mouse or pot
    command (c64-test-harness `backends/vice_binary.py:61-84`), and with
    `sound=False` `$D419/$D41A` read `$FF` (`backends/vice_lifecycle.py:60`,
    `:638-640`). So the scripted legs use the override:
    - `run_inp` `mouse` and `msamp`;
    - `tests/aros_wb.py --legs e2e --e2e-mouse`.
    The real SID path is covered by `--attended --mouse 1`.
- **Stuck mask** (`inp_stuck`, MAIN BSS, 0 after the BSS clear,
  host-set; demo 2026-10-11).
  - The real scan reads the stick as `~(PRA | inp_stuck) & $1F`, so a
    switch stuck closed reads as released. The user's stick has Up stuck:
    `$01`. The override path ignores the mask.
  - The snapshot image carries it, like the other inp bytes.
  - It hides the direction from the mouse only. The line is still held at
    the port, so it still selects its keyboard column. A held column-0 line
    (Up) keeps F1 unreadable (it keeps its last state), as before. The
    other keys are unaffected.
  - Tests:
    - `host/tests/test_inp.py::test_scan_stuck_mask` checks the model;
    - `run_inp` `stuck` (VICE) checks the idle real scan with masks $01
      and $1F, and that the override ignores the mask. VICE cannot hold a
      port-A line, so the masking itself is shown only by the model and on
      the device (`--attended --stuck 0x01`).
- **Rule** (per frame, from `inp_joy`/`inp_key`; `host/inp_ref.Inp`):
  - **Mouse.** dir = joy & `$0F`. n = `inp_n`, set to 0 when dir = 0.
    step = 1 + n, then n += 1 while n < `INP_NMAX` (31). The steps run
    1, 2, 3, ..., `INP_CAP` = 32 counter units per frame (W14-M; W13-I
    had 1 + n/2 up to 16). The centred frame counts, so a tap after centring
    moves 2 counts (1 lores px in AROS).
    - Up subtracts the step from JOY0DAT's Y counter (`chip_shd+$0A`, the
      high byte), down adds it. Left and right do the same on the X
      counter (`chip_shd+$0B`). Each counter is a u8 and wraps, as on the
      Amiga.
    - Opposite bits cancel. Diagonals move both axes. Changing direction
      without centring keeps the speed. Centring resets it.
  - **Left button** = fire. CIA-A PRA pin bit 6 (/FIR0) is low while fire
    is held. Pin bit 7 (/FIR1) is forced high. Pins 0-5 (W13-W's 2-5) are
    kept: read-modify-write of `cia_st+CIA_PIN`, mirrored as `INP_PIN`
    (host test). After a `cia_reset` (pins `$EB`, both released) the pins
    are back within one frame.
  - **Right button** = C= (the Amiga has no C= key). The POTINP read
    (`chip.s` → `inp_pot`) returns `$FB00` while `inp_key` bit 5 is set,
    else `$FF00` as before
    (bit 10, DATLY, low). The POTGO output bits are not modelled.
  - **View key.** On an F1 press edge (down now, up the frame before),
    `inp_view` |= 1. W13-D clears the bits it consumes (I1). No other C64
    key reaches the guest in this wave.
- **Resume (I2).** The snapshot image carries `inp_src`/`inp_joy`/`inp_key`
  as they were at suspend; the resume path itself does not touch inp
  state. The host that launches a resume owns `inp_src`:
  - Real input: write `inp_src` = 0.
  - Scripted input: write `inp_src` = 1 with `inp_joy`/`inp_key` = 0.
  - Timing: snap_resume restores the image and continues mach_run without
    returning, so the write must come after the restore. On the Ultimate
    it is a memory write while the guest runs; VICE writes through the
    monitor.
  - Test boots: `tests/aros_boot.py` sets `inp_src` = 1 with no input right
    after the runner loads (`--real-input` keeps 0), so a test snapshot
    resumes scripted and idle.
  - Who writes it: W13-W's I3 helper (`tests/aros_wb.py`) and the demo
    resume must write `inp_src` = 0 after the resume for the user's stick
    to work.
- **State.** `inp_src`, `inp_joy`, `inp_key`, `inp_view`, `inp_n`,
  `inp_kp`, `inp_stuck`, then W14-M's `inp_mport`, `inp_mper` (4), `inp_mpot` (2),
  `inp_mb`, `inp_mold` (2), `inp_mok` (MAIN BSS, in this order, zero = idle; inp.s links after
  mach.s and before snap.s, so the W12-T snapshot image carries them, I2).
  Scratch: rc zp `inp $9A-$9B` (`inp_t`, `inp_u`). It is live only inside
  one call and not saved or restored: the "free zp" row of the
  suspend/resume inventory now starts at `$9C`.
- **Host-visible** (`runner.lbl`): `inp_frame`, `inp_pot`, `inp_msamp`,
  `inp_mport`, `inp_mper`, `inp_mpot`, `inp_mb`, `inp_mok`, `inp_src`,
  `inp_joy`, `inp_key`, `inp_view`, `inp_n`.
- **Link order.** inp.s links after mach.s and before snap.s (Makefile).
  - chip.s keeps its size. Only mach.s grows (+3 B, the call), so only
    snap.s and ureu.s move. core.s is unchanged.
  - **Layout.** W13-I's CODE growth (inp.s plus mach.s's +3) is padded
    (`inp_pad`) to exactly `$100`, so RODATA, DATA and BSS keep every page
    offset. RODATA tables are read with abs,X on the per-instruction path,
    and some straddle a page (g5_flow's 32-byte table). Any other shift
    changes which entries cross: perf_cycles measured "stack" +1/3 cycle
    per instruction at +207 B, and mem/stream/copy +1/2 at +3 B.
  - **MAIN.** The segments grow by 262 B: inp code 198 B, pad 55 B, mach
    3 B, BSS 6 B. The BSS end moves `$9C0B` → `$9D11`, the same as without
    the pad, because BSS starts on a page.
  - Any later CODE growth shifts RODATA again. That is a layout rule for the
    supervisor (e.g. page-align RODATA in `runtime/c64.cfg`).

| guest memory | REU chip RAM, slow RAM + `$DC0000-$DEFFFF`, ROM banks | stays (REU-resident) / file spans 2-5 | stays / LOAD_REU | the guest's RAM. AROS WRITES `$DC0000-$DEFFFF` (unmapped in the profile; not zero after m3 on both Ultimates), so the slow-RAM span runs to `$DEFFFF`. `$200000-$BEFFFF` (W13-W: `$2DC000-$BEFFFF`; the staged disk below it is in span 2; W16-P: except `prof` `$2DC000-$2DC0FF` in a Workbench session with `--audio`, host data that is non-zero by design, not filed, and rewritten by the launcher before every resume launch) and `$E80000-$F7FFFF` read zero after m3 (`aros_resume` m3 leg, Ultimate) and are not filed: a resume needs them zero, as the boot does (`host/machine.ZERO_SPANS`) |
| W13-W disk image | REU `$200000-$2DBFFF` (`TD_ADF`) | file span 2 (the chip span runs to `$2DBFFF`, `SNAP_VER` 2) / stays (REU-resident) | LOAD_REU / stays | the staged DF0 disk and its overlay travel with the snapshot; zero without a disk |
| W13-W drive and device state (`td_*`) | `td.s` BSS | image | restored | drive lines, the patched slot and its original target; the CIA-A pins live in `cia_st` (image) |

## HLE trackdisk and DF0 (W13-W)

Owner: td (`runtime/ops/td.s` (MAIN: state and guarded entries),
`tests/aros/td.s` (the machine-mode module: all the code), the `; W13-W`
lines in `cia.s`, rc `machine.inc` region `td`, `host/adf_overlay.py`,
`tests/aros_wb.py`).
Design and observations: `build/evidence/w13-wb/PROGRESS.md` (boot model,
pinned ROM, 3.1 Workbench disk). Tests: `host/tests/test_td.py` (td.s on
the in-process 6502 + REC model with the REU's 512 KB address wrap: the
drive lines against the boot model's `Floppy`, the locate/patch -- **L8
guard: names may be in ROM** (`test_fhook_finds_names_in_rom`: AROS's
resident names are in the ROM banks; a guard that refused them never
patched BeginIO on the C64U, fix round 1) while nodes, SysBase and the
I/O banks are refused (`test_fhook_walk_stays_in_ram`) --, every request
class, reads across `$80000`, 10 mutants),
`host/tests/test_adf_overlay.py`, `tests/aros_wb.py` (legs `w`, `nohle`,
`resume`).

- **Split (supervisor budget ruling).** MAIN holds only the cia.s hook,
  `td_call`/`td_fcall` and the state (all BSS, so the W12-T image carries
  it): 94 B (cia.o CODE +12, td.o CODE +50, BSS +32; but DATA now ends past
  `$8BFF`, so the page-aligned BSS moved from `$8C00` to `$8D00`: BSS end
  `$9C0B` -> `$9D2B`, +288 B of the space below `$9FFF`). The code is a machine-mode module, `tests/aros/td.s`,
  assembled at test time against `runner.lbl` (the `-D` symbols it lists
  with `.ifndef`/`.error`) at `TD_ORG` = `$C200` (end <= `$C600`; layout:
  mboot `$C000-$C1FF`, td `$C200-$C5FF`, W13-D's spr `$C600-$C8FF`),
  entry `TD_ORG` = `jmp td_arm`, ID at `TD_ORG+3` (4 bytes: SHA-1 of the
  binary with them zero, stamped by `tests/aros_wb.build_module`). The
  generic loader (`aros_wb.MODULES`) stages every module with the boot
  driver and again on every fresh load before a resume.
- **Off by default.** `td_vec` = 0 (program mode, a boot without a disk,
  every existing suite and snapshot): `td_call` returns C=0 at once, CIA-A
  PRA pins stay `$EB`. The module is armed only when the host set `td_on`
  (a disk is staged) and the boot driver called `TD_ORG` (after
  `cia_reset`, `cop_reset`, `snap_arm`; `tests/aros_wb.py` assembles a
  variant of `mboot.s` with that one call): `td_arm` sets `td_vec`/`td_fvec`
  (the module's PRB and frame-hook entries), `td_mid` := the module ID,
  chains `td_fcall` in front of the frame hook. It also zeroes the drive
  and device state (`td_zero` .. `TD_NZERO`: pins, motor, cylinder,
  change, slot, counters; fix round 1 L5), so a second boot in one session
  starts clean.
- **Module ID (resume).** `td_call` and `td_fcall` compare `TD_ORG+3..6`
  with `td_mid` first. A different module (a resume after a rebuild):
  `td_call` calls nothing, `td_fcall` halts the machine with st_halt 1,
  st_reason `TD_HALT_MOD` (`$57`) -- see "Suspend/resume". Tests:
  `test_td.test_other_module_id_refuses`, mutant `nomid`; VICE leg
  `resume` (a `TD_VARIANT` build).
- **The disk** (rc `td $200000-$2DBFFF`, `TD_ADF`): 901,120 bytes, block n
  at `TD_ADF + 512 n`, right after chip RAM. Staged after the ROM: VICE by
  `test_load_chunk` (image with the overlay applied); the Ultimates by the
  runner's `ureu_ld` from `<vol>/amiga64/amiga-os-310-workbench.adf` (byte
  count checked by ureu.s) plus the overlay's blocks by the harness's REU
  write. Both: `aros_sum` of the 901,120 bytes must equal the host's image.
  No guest instruction touches the region: the boot model's watch over the
  whole boot to Workbench idle saw no access to banks `$20-$B7` (only `$B8`
  Akiko, `$E8` autoconfig, `$F0` reads), and the region is never a RAM,
  board or ROM candidate (AROS sizes chip RAM to `$200000`, finds no
  autoconfig board). It reads zero without a disk.
- **The overlay** (`host/adf_overlay.py`): `S/Startup-Sequence` replaced
  in the file's own data blocks (WB_BOOT §4.3 `trim`, default; `min`;
  `full` = none), byte for byte the boot model's `--startup` image. The
  ADF and every device file stay untouched. Files over 72 blocks follow
  their extension blocks (T_LIST, parent = the header, checksummed; fix
  round 1 L3; on the user's disk Libs/asl.library and amigaguide.datatype).
  Only OFS/FFS (`DOS\0`-`DOS\3`) images: dircache and later dostypes
  are refused (L4). Refused: a block outside
  2..1759, not 512 bytes, a header or OFS data block whose checksum does
  not sum to 0, a header whose data-block table changed.
- **The drive** (`td_prb`, the W13-W hook in `cia.s` `wr_prb`, CIA-B
  only, `A` = the written value): the boot model's `Floppy` on the pins
  `PRB | ~DDRB`. `/SEL0` (bit 3) low selects DF0; `/MTR` (bit 7) is latched
  at the `/SEL0` fall; a `/STEP` (bit 0) rise while selected moves the head
  (DIR bit 1 high: towards cylinder 0, else in, clamped 0..83) and sets
  `/CHNG` high (disk present). CIA-A PRA input pins (`cia_st+CIA_PIN`,
  bits 2-5 only, read-modify-write; W13-I owns 6-7): `$3C` while DF0 is
  deselected, else `/RDY` (5) high iff the motor is off, `/TK0` (4) high iff
  not at cylinder 0, `/WPRO` (3) low (write-protected), `/CHNG` (2) high
  once a step saw the disk. `td_arm` sets `$3C` after `cia_reset`. DDRB
  writes are not hooked: the ROM writes DDRB = `$FF` once, before any
  meaningful PRB value (observed).
- **The device.** `td_fhook` (chained in front of the frame hook) walks
  `SysBase->DeviceList` (`$4`, +350; at most 32 nodes per frame; SysBase,
  every node and every name pointer must lie in chip RAM `$000000-$1FFFFF`
  or slow RAM `$C00000-$DBFFFF` (a name pointer may also point into the
  ROM banks, where AROS keeps its resident names), else the walk stops: it
  never reads the trapped `$BF`/`$DF` banks or the disk region, fix round 1
  L8; the name compare includes the terminator) until
  `trackdisk.device` appears, checks that its BeginIO slot (LVO -30) is
  `JMP abs.l`, keeps the target (`td_orig`) and rewrites the slot through
  `mem_write32`/`mem_write16` (W12-C's write check drops a cached window)
  to `move.b d0,$BFD100` -- a CIA-B PRB write of the same 6 bytes. When
  that write reaches `td_prb` with `st_ipc` = the slot (`td_slot`; the
  exception-mode loop's PC of the current instruction; `$FFxxxx` = none),
  nothing is stored and `td_bio` serves the IORequest at A1 (exec/io.h):
  - quick `CMD_READ` / `ETD_READ`: `io_Length` bytes of the image at
    `io_Offset` copied to `io_Data`, `io_Actual` = `io_Length`,
    `io_Error` 0; then the device's RTS (PC := [A7], A7 += 4);
  - quick `CMD_WRITE`, `TD_FORMAT`, `TD_RAWWRITE` (and their `ETD_`
    forms): `io_Error` 28 (TDERR_WriteProt), `io_Actual` 0, RTS;
  - anything else (non-quick requests, `TD_MOTOR`, `TD_CHANGESTATE`,
    `TD_PROTSTATUS`, `TD_GETGEOMETRY`, `TD_ADDCHANGEINT`, ...): PC :=
    `td_orig`, AROS's own trackdisk.device, which sees the drive above
    (write-protected). Observed with the trim script: 55 quick reads
    served, 7-9 requests passed, no write, no disk DMA.
  - `io_Offset + io_Length` beyond the image (or either >= 16 MB):
    `io_Error` 20 (TDERR_NotSpecified), `io_Actual` 0, nothing copied.
  - **Limit (writes):** only QUICK `CMD_WRITE`/`TD_FORMAT`/`TD_RAWWRITE`
    get TDERR_WriteProt here; non-quick ones go to the native path, and
    nothing is proven about what it does with them (no disk DMA exists;
    the drive lines say write-protected). The brief's "any write request
    returns the write-protect error" holds for quick requests only. None
    was observed (the filesystem sees TD_PROTSTATUS and never writes).
  - **Limit:** a NON-quick `CMD_READ`/`ETD_READ` goes to the native path,
    which needs disk DMA that does not exist: it would never complete. It is
    counted in `td_nqr`; `aros_wb` fails if it is not 0 (none observed in the
    boot to Workbench idle: every read is a DoIO).
  The trap is safe because MOVE stores last and `ea_store` tail-calls
  `mem_write8`: after the store the instruction only retires, so the new
  r_pc/r_a7 take effect as the next instruction (flags: MOVE's, which no
  BeginIO caller relies on).
- **The copy.** REU -> `TD_BUF` (`blt_abuf`, `$B600-$B6FF`, shared with
  blt.s, supervisor-approved: blits run synchronously inside a
  custom-register write, so none is in flight inside the BeginIO trap (a
  CIA write) or the frame hook (a service pass)) -> REU, one chunk
  per destination page piece (`c = min(n, 256 - dst & $FF)`), REC address
  control 0; `code_invpg` for each chunk's page (the bytes bypass
  `mem_write*`); `mem_rec_dirty` at the end (C1). A chunk whose
  destination is bank `$BF` or `$DF` is never moved (the REU under the I/O
  banks, "I/O trap" F2): the request ends with `io_Error` 20 and
  `io_Actual` 0 (the chunks before it were copied). **Chunks** (fix round 1, H1):
  `c = min(n, 256 - (dst & $FF), 256 - (src & $FF))` -- the REC carries the
  REU address through bits 0-18 only, so a transfer running past a 512 KB
  line wraps to the line's start; a chunk inside one 256-byte page on both
  sides never crosses one (512 KB lines are page-aligned), on the image
  side (TD_ADF + `$80000`: blocks 1023/1024, e.g. Libs/asl.library) and on
  the guest side alike. Tested on a REC model with that wrap and a
  non-periodic image (mutant `nosrccap`). **Cost** (VICE, phi2-exact, the
  whole trap from `td_call` to the RTS, `aros_wb` leg `cost`): 512 B
  3775 cycles (59 us at 64 MHz), 1024 B 5736 (90 us); with an unaligned
  `io_Data` 4654 / 7437 (more chunks): ~1.8k fixed (decode, io fields,
  RTS through the memory API) + ~1k per chunk (two DMAs, `code_invpg`).
- **Scratch.** Inside the trap and the frame hook td.s uses the
  interpreter's EA/ALU operands `t_ea..t_res` (`$60-$6F`): dead at an
  instruction boundary and after `ea_store`'s tail call (`t_op1+3` the
  chunk length, `t_op2+3` io_Error, `t_res+3` the walk count). `m_addr`/
  `m_val` are the memory API's. Evidence: the `zpjunk` VICE build (all 16
  bytes set to junk at every trap/hook exit) boots to the same Workbench.
- **Host-visible** (runner.lbl): `td_on` (host: 1 = disk staged), `td_vec`,
  `td_mid`, `td_slot`, `td_orig`, `td_nrd` (u16 reads served), `td_npas`,
  `td_nqr` (u8), `td_cyl`, `td_mot`, `td_chg`, and the block watch: `td_wa` (host: the
  watched block's REU address bits 15-9 with bit 8 clear, bits 23-16),
  `td_whit` (chunks read from it, mod 256).
- **Snapshots.** All state is BSS (W12-T image) or `cia_st`; the disk is
  in file span 2 (`SNAP_VER` 2). A snapshot taken before the slot is
  patched resumes into the walk; after it, into the trap.

## Audio (Paula) (W15-P)

Owner: aud (`tests/paula/aud.s`, a machine-mode module; its hook in
`runtime/ops/chip.s`). Design: `docs/WAVE15_BRIEF.md` W15-P and its
addendum (rulings R1-R4, the fix-round rulings M5/M6). Reference:
`host/paula_ref.py`. Tests: `tests/run_paula.py` (+ `tests/paula/drv.s`;
VICE legs `latch`, `blocks`, `irq`, `reload`, `lencache`, `pervol`, `park`,
`phase`, `bplpt`, `dmaoff`, `nomod`; `--mutants`, `--mutants --review`,
`--perf`; U64E legs with `--hw u64`), `host/tests/test_paula_ref.py`,
`host/tests/test_paula_layout.py`.

- **What the guest can use** (WAVE15 I-3): AUDxLCH/AUDxLCL, AUDxLEN,
  AUDxPER, AUDxVOL (`$0A0-$0DA`, channel x at + 16 x); DMACON DMAEN and
  AUD0EN-AUD3EN; INTENA/INTREQ AUD0-AUD3 (bits 7-10, level 4). AUDxDAT
  (CPU mode) is counted and ignored; ADKCON modulation is not modelled.
  Nothing else is promised.
- **The hook (MAIN, +21 B CODE, +1 B DATA).** `c_wrw`'s cop route (the
  page-0 registers that are not SERDAT, INTENA/INTREQ or the blitter's) is
  unchanged while the module is not armed: everything goes to `cop_w0`,
  which ignores the audio registers. Armed, i.e. `aud_mon` (DATA, 0 at load,
  in the STARTUP->DISPATCH slack) equals the module's ID byte 0 at
  `AUD_ORG+3` (`AUD_ORG` = `$CA00`), the registers `$0A0-$0FE` and DMACON
  (`$096`) enter the module instead: it hands `$0E0+` (BPLxPT) straight back
  to `cop_w0` (`bplpt` leg), and calls `cop_w0` first for DMACON (cop.s
  keeps owning DMACON, so DMACONR shows AUDxEN). **Cost of the router**
  (6502 cycles, rev15-paula's count on the product layout): not armed +10
  to +14 per write on the cop route's registers (the range and DMACON
  tests); armed, BPLxPT ~+25 (the guard and the hand-back); DMACON ~1.35k
  (the module's walk of the four channels, `--perf`). The router's branches do
  not cross a page in the product layout; a later CODE shift that moves one
  across adds 1 cycle per taken branch. MAIN is at its limit: run_pf's
  `trace` variant keeps 4 B before its BSS page, and
  `host/tests/test_paula_layout.py` fails if RODATA leaves `$8D00`.
- **Where it lives (the player profile; W16-P: the Workbench profile too, below).** Segment A `$CA00-$CCFF`
  (entries `+0` write, `+7` the slot-6 callback, `+10` init; config and
  counters at `+13`), segment B `$0400-$07FF` (the text screen and its
  sprite-pointer tail, unused in machine mode: the VIC is on bank 3), state
  page `$CD00-$CDFF`. **Known limits:** segment A and the state page are
  mutually exclusive with `aros_resume`'s stub (`$CB00`/`$CC00`) and with
  run_irq's and run_mach's driver blocks (`$CA00-$CCFF`), which belong to
  other sessions; W16-P: in a Workbench session aud is staged after the
  resume stub's last use ("The Workbench profile" below). Segment B is player
  profile only: machine mode never uses the HLE console screen, so it is
  mutually exclusive with HLE console sessions and with `aros_resume`'s
  READY. wait (which happens before staging); it is staged only after any
  harness READY./screen wait, and `run_paula`'s rig refuses to read the
  region once it is staged (except its own integrity checks, which the U64E
  runner repeats before every leg). **ID guard:** the router enters A only
  when `aud_mon` equals A's ID byte 0; every entry then compares B's 4 ID
  bytes with A's; a stale or missing B disarms the module (`aud_mon` := 0)
  and stops every voice (B's high byte `$FF`, then CTRL 0: they hold their
  last sample as DC until the next init) before any jump into B (`nomod`,
  mutant `nobok`). Limit: A's own code is not checksummed (a corrupted A
  with an intact ID byte would run); only the ID bytes are compared.
- **Arming (host).** Stage A and B; write the config (`+13` u16
  `cfg_r16`, `+15` u8 `cfg_hw`, `+16` u24 `cfg_zero`); `mach_cbv[6]` :=
  `AUD_ORG+7`; call `AUD_ORG+10` (init); `aud_mon` := the ID byte 0.
  Disarm: `aud_mon` := 0, `mach_cbv[6]` := 0.
- **The Workbench profile (W16-P; WAVE16 M0 rulings (1)-(4)).** `tests/aros_wb.py
  --audio` (helpers `aud_stage`, `aud_arm`, leg `aud`):
  - **Placement:** unchanged (A `$CA00-$CCFF`, state page `$CD00-$CDFF`,
    B `$0400-$07FF`). Machine mode never writes the text screen (only
    hle.s/hle_con.s do; the VIC is on bank 3). In a Workbench session A
    shares `buf_result` with aros_resume's REU stub (`$CC00`, data `$CB00`:
    fresh_load's scrub, `declare_rid`, `Snap.reu_read/write`), which only
    runs while the guest does not. **Rule: aud is staged and init'ed last**,
    right before `m.start` or the `snap_resume` launch. Leg `cost` (its stub
    at `$CC80`), `--watch` (`$CDC0`) and `--resume-mutant` (another PRG's
    labels) refuse `--audio`. GuestPeek (VICE e2e reads while the guest
    runs) parks at `$CCF0` and fetches into `$CD00`: it saves that page and
    the park bytes and writes both back before the 6510 runs on (leg `aud`:
    `$CA00-$CDFF` unchanged across 20 peeks, counters aside; mutant
    `peeknorest`).
  - **The KERNAL cursor blink:** B is the text screen. After VICE's
    `fresh_load` (READY., then `goto $080D`: the resume path) BLNSW (`$CC`)
    is 0, and the KERNAL IRQ blinks the cursor at `$04F0` (bit 7) until
    `mach_run`'s `sei`. The ID check cannot see that (B's code is not
    checksummed), so `aud_stage` sets `$CC` := 1 before staging B. Leg `aud`
    reads B 12 times over 1.2 s between staging and the resume launch
    (mutant `blink`: B changed in 3 of 12 reads; probe
    `build/evidence/w16-p/vice/blink_probe.py`).
  - **Boot:** after `arm_spr`/`arm_inpm` and every runner stub: the profile
    block (below), A + B (read back), the config (`cfg_r16` for the boot's
    s, `cfg_hw` 1 only when the hold mapped and proved the sampler, `cfg_zero`
    `$DF4000`), init, `mach_cbv[6]` := A+7, then `aud_mon` := the ID byte 0.
    The guest runs armed from its first instruction (mutants `stagefirst`,
    `nocbv`).
  - **Resume:** `aud_mon` (DATA) is 0 after the fresh load and the state page
    is not in the image. Before the launch (after `aros_fill` and
    `declare_rid`): the block, A + B, config, init. After the guest runs:
    `mach_sl` must equal the session's s (else the staged r16 is wrong:
    refused; `mach_sl` is exported for this, label only, the PRG unchanged;
    leg `aud` checks it equals the boot's s), then
    `mach_cbv[6]`, then `aud_mon` (mutant `noresarm`).
    **Known limit:** a snapshot taken while a channel plays resumes with
    that channel silent and posting no AUDx until the guest's next DMA start
    (the channel state is not in the image); the Workbench demo snapshot (no
    audio) is unaffected. **Not shown (rev16-p L5):** that a Paula write
    reaches the module after a resume: at the idle Workbench AROS writes no
    audio register (`st_nw` 0 after every resume), so the re-arm is shown
    statically (`aud_mon`, `mach_cbv[6]`, A/B intact); W16-X's program is the
    dynamic proof. **Resume without `--audio` (rev16-p L6):** a snapshot
    taken with aud armed carries `mach_cbv[6]` = `$CA07` (and maybe slot 6
    armed); resumed without `--audio` nothing is staged there. The restore
    and the continued run are one `snap_resume` call, so the host cannot
    write between them; instead, before the launch, `_resume` writes `rts`
    at `$CA07` (and `aud_mon` := 0, as after any fresh load), so such a
    callback is a no-op, and once the guest runs it clears `mach_cbv[6]`
    (high byte first). Leg `audres` (VICE): slot 6 pending ~30 frames ahead
    at the suspend, `$CA00-$CA0F` as unknown RAM (a self-jump at `$CA07`;
    zeros were not enough: the BRKs fell into the stale A the boot left in
    RAM), the guest must run
    45 frames past the resume and `mach_cbv[6]` read 0 (mutant `noguard`:
    neither the rts nor the clear). The deadline falls after the host's
    clear, so the leg shows the clear; the rts covers only the window
    between the launch and the clear, which the leg does not hit.
    `$CA07` is free in non-audio sessions (the resume stub uses `$CB00`
    and `$CC00`). Leg `aud` disarms before its suspend, as the demo snapshot
    is taken.
  - **The hold (Ultimates):** `Map Ultimate Audio $DF20-DFFF` := Enabled
    inside the hold (restored by it), the runner reloaded, then the
    **differential sampler proof**, measured on the **U64E only** [MEASURED
    HW, U64E fw 3.15, `build/evidence/w16-p/hw16`; the C64U is not measured
    yet: its hold is pending, so `--audio` on the C64U may stop INFRA until
    then]: the 6510's copy of `$DF20-$DFFF` reads
    `$00 $10` in all 112 pairs with the item Enabled; Disabled, the window
    reads changing bus data (`$DF21` = `$00`; two reads differ), so `$DF21`
    alone is not the proof (on VICE the window is the REC's mirror). Without
    the proof the session stops (INFRA). **Silence at the end:** the
    session's closing `set_speed(1)` + reboot stops a voice that is still
    playing on the **U64E** (hw16 `rbsil`, by the UDP audio stream: a
    hardware-looped tone, 4394 rms, gone within ~0.3 s of the reboot
    request; 34 rms after, noise, no 259 Hz line); no init call is needed
    (none is possible while `mach_run` runs). The C64U (no stream: WiFi) is
    to be checked instrumentally (hw16 `rbwin`: the window idle, playing and
    after the reboot).
  - **Time at s = 3** (both Ultimates): E = 1022727 / 8 = 127841 Hz,
    `cfg_r16` 2362, a guest frame 111 ms real (VERTB 9.0 Hz). `PARK_E` stays
    7094 E (55-111 ms real between parked looks: ~8x fewer per real second
    than at s = 0); a late acknowledge is reposted within one guest frame
    (111 ms real); the seam's lateness bound 4096 >> 3 = 512 E = 4 ms real.
    VICE (s = 9): `cfg_r16` 37 (1.3% rounding: the dry backend only).
    **Not measured:** the C64U's phi2 at 64 MHz (assumed NTSC 1022727).
- **The profile block and the app anchor (W16-P; rc machine.inc `prof
  $2DC000-$2DC0FF`).** A guest program cannot measure real time, so the host
  tells it. Guest `$2DC000-$2DC03F`, right after the staged ADF: plain REU
  memory outside chip and slow RAM (AROS never allocates it; no MemHeader
  may cover it: leg `w`'s guest check), written by the host only while the
  guest is stopped (boot: before `m.start`; resume: before the launch; the
  snapshot file spans end at `$2DBFFF`), never torn. Zero in a session
  without it. Big-endian:

  | off | type | field |
  |---|---|---|
  | +0 | u32 | magic `'A64P'` |
  | +4 | u16 | version 1 |
  | +6 | u16 | size 64 |
  | +8 | u32 | `e_hz_q8`: guest E ticks per real second x 256 (s = 3 NTSC: 32727264) |
  | +12 | u32 | `tick_e`: round(E Hz / 50), the 125-BPM tick (s = 3: 2557); CIA period = tick_e x 125 / BPM |
  | +16 | u32 | `frame_e` 14188 (E per guest frame) |
  | +20 | u32 | `phi2_hz` (1022727 NTSC) |
  | +24 | u8 | s |
  | +25 | u8 | host: 0 VICE, 1 U64E, 2 C64U |
  | +26 | u8 | flags: bit 0 aud armed, bit 1 the sampler is live (audible), bit 2 real time is meaningful (an Ultimate) |
  | +27 | u8 | 0 |
  | +28 | u16 | `cfg_r16` in force |
  | +30 | u16 | CPU MHz (informative) |
  | +32..+59 | | 0 |
  | +60 | u32 | `~`(32-bit sum of the longs +0..+56) |

  A guest takes it only when the magic, version 1, the size and the check
  match; otherwise Amiga timing (tick_e 14188, VBL). Leg `aud` checks the
  config and the block it reads back (GuestPeek) against values computed
  from s and phi2 alone, not through `profile_block` (mutants `r16cfg`,
  `blks0`), and zeroes `prof` before the resume's staging, as the Ultimate's
  file resume leaves it, so the rewrite is proven (mutant `noresblk`). **The app anchor**
  `$2DC040-$2DC04F` (supervisor, binding for W16-X/W16-D): written by the
  guest program, only read by the host: +0 `'MWBA'`, +4 a pointer to the
  app's state struct (mod_view etc.), +8 the guest's sequence/flags long,
  +12 reserved. `$2DC050-$2DC0FF` reserved. **Anchor rules** (rev16-p M4):
  1. the program writes the anchor only when the profile block is valid
     (otherwise `$2DC040` may be anything: a real Amiga's memory);
  2. publish +4, +8, +12 first and `'MWBA'` last; withdraw (on exit, before
     the struct is freed) by clearing `'MWBA'` first: the host may read
     between any two guest writes, so the magic gates the rest;
  3. the pointer is never 0, is even, and the whole struct lies in chip RAM
     (`$000000-$1FFFFF`) or slow RAM (`$C00000-$DBFFFF`);
  4. the snapshot file spans end at `$2DBFFF`: a file resume loses the
     anchor (the launcher rewrites only the block), so an app that relies on
     it publishes it again after a resume.
  Host side: `host/profile_block.py` (`build`, `parse`, `parse_anchor(raw,
  size)`: a pointer that is 0, odd, or whose *size*-byte struct leaves its
  RAM span is not to be followed), pytest `host/tests/test_profile_block.py`.
- **Model (Amiga HRM, audio).** AUDxLC/AUDxLEN are latches (LC bits 20-1,
  so samples anywhere in 2 MB of chip RAM, `latch` plays one at `$180000`).
  DMA start (DMAEN and AUDxEN both set) copies them into the playing block
  and posts AUDx at once; every block end copies them again and posts
  again; so a latch write takes effect at the next block (a block end that
  falls between an LC and its LEN write reloads the half-written pair, as
  on Paula). A block lasts 2 x LEN x PER colour clocks (LEN 0 = 65536 words,
  PER 0 counts as 1), capped at 2^30 E. PER and VOL act on the voice (the
  sound) at once. DMA off stops the channel. **Deviation (cost):** a
  block's end is fixed when it starts, from the PER of that moment; a PER
  written mid-block moves block ends from the next block on (an exact
  rescale cost ~25k cycles per PER write). Error bound: the block end (the
  AUDx post, and the 6510 reprogram of a block that does not loop in
  hardware) is off by the rest of that block x |1 - new/old|, less than one
  block; the pitch itself is exact at once.
- **Time.** A colour clock is `cfg_r16` / 65536 E ticks: Amiga-nominal
  13107 (E = CCK / 5); **the U64E player profile (s = 0, NTSC phi2)
  18897** (R3; `host/paula_ref.r16_for`). Deadlines are fixed point
  (E x 65536, 6 bytes) on a grid from DMA start, so a channel never drifts;
  the grid is Paula time (2 x LEN x PER colour clocks), which differs from
  the sampler's playback of the same block by the RATE rounding (at most
  half a 160 ns step per sample: +0.059% at PER 428, `--hw` `irq`). The
  block length is cached per channel, keyed on the playing block's LEN and
  dropped by a PER write (`lencache`). One mach event slot, **slot 6**,
  serves all four channels, armed at the earliest due tick,
  ceil(deadline). A firing services every due channel's block and its next
  blocks while they are due, up to 16 per pass; then the grid restarts at
  now + one block (`st_nres` counts it: only blocks shorter than the host
  can serve), so AUDx posts coalesce in INTREQ for blocks shorter than a
  pass, as they would for a CPU too slow to acknowledge them. Lateness: the
  seam's (≤ 4096 >> s E) plus at most one pass servicing the other channels
  (VICE: the last post of every channel within (4096 + 12288) >> s E of its
  due tick, `blocks`, which checks lateness on each channel's last post
  only, the per-channel maximum having gone for space (rev15-paula L4); U64E s = 0: at most 138-149 E, measured with the
  round-0 module's lateness log, `hw3`/`hw5`, before that log was dropped
  for space).
- **Parking (phase kept, M6).** A block end needs no work when the voice
  loops the block in hardware (nothing to reload) and the channel's INTREQ
  AUDx bit is still pending: another post would change nothing. Such a
  channel's deadline advances by 2^k blocks >= `PARK_E` = 7094 E (half a
  frame), so it stays on the block grid and is looked at again every 1/2 to
  1 frame (plus up to a block for blocks longer than half a frame); once the
  guest has acknowledged, that look ends the block (posts AUDx). So a replay
  that ignores AUDx costs one look per channel per 1/2 to 1 frame even on
  the 1-word null loop after a one-shot sample (U64E: 3.0% of the guest with
  four such channels, `--hw` `ips`), a guest that acknowledges every block
  keeps the exact grid (`irq`), and a late acknowledge gets its next post
  within one frame (`park`). A loop change that drops the hardware loop (an
  instrument change without a note on a parked channel) pulls the deadline
  back to the first grid point after the write, so the 6510 reprograms at
  the hardware's loop end (`phase`: within the seam bound). **Known limit
  (rev15-paula L5):** the pull steps back one block at a time, at most 2^k
  <= 2 x `PARK_E` / block + 1 iterations of ~200 cycles: 128 for a 1-word
  loop at PER 113 (the shortest MOD period; ~25k cycles, ~0.6 ms at 48
  MHz), but ~24k for LEN 1 at PER 0..2 (~5M cycles, ~0.1 s), an invalid
  period (Paula's DMA needs >= 124) that a guest would have to program on
  purpose.
- **The sampler (U64 Ultimate Audio, HARDWARE "U64 sampler").** Channel x =
  voice x. START = `$1000000` + the REU offset of the block (guest chip RAM
  is the REU, flat); LENGTH = 2 x words (always even); RATE = (PER x
  29563210 - 20761805) >> 24, clamped to 0..$FFFF (the nearest 160 ns step
  to PER x 281.937 ns); VOL = min(vol, 63) (64 wraps to 0 there); PAN 0 for
  channels 0/3, 15 for 1/2. **Hardware loop:** REPEAT A/B (relative to
  START) + CTRL bit 1 only while the latched block starts inside the
  voice's span and ends exactly where it ends (so B = LENGTH whenever
  looping); the voice then plays Paula's next blocks with no 6510 work.
  Otherwise the voice is reprogrammed at the block end, counted per
  channel. DMA off plays the two zero bytes at `cfg_zero` (W13-D's
  `disp_zero`, REU `$DF4000`, read-only) once, so the voice does not hold
  its last sample as DC.
- **Write order (M5): repeat is never off with REPEAT B == LENGTH, not even
  between two writes** (the firmware hazard, HARDWARE "U64 sampler"):
  - a reprogram: B := `$FFFFFF` (never LENGTH, which is even; a voice with B
    beyond its LENGTH ends there normally), CTRL := 0 (a real stop: CTRL 2
    does not stop a playing voice [MEASURED HW]), every other field, CTRL :=
    1 or 3 (starts at START), then B := its value, low byte first (from
    `$FFFFFF` every step stays >= `$FF0000`);
  - a loop change while playing: B := `$FFFFFF`, A, CTRL (no restart
    [MEASURED HW]), B low byte first; an unchanged loop rewrites A only.
  - Proof: the VICE legs run the module's **log build** (`-D AUD_LOG`: every
    sampler store is `jsr aud_log` into `tests/paula/drv.s`, same size), which
    mirrors the registers, keeps a command ring and checks the state after
    every write: no "repeat off with B == LENGTH", and no write to START or
    LENGTH (+$04..+$0B) while the mirror's CTRL bit 0 is set (rewriting the
    span of a voice that was not stopped lets it run on past a shorter
    LENGTH: the hazard the U64E showed, HARDWARE "Paula on the U64
    sampler"); every leg asserts both and that the mirror equals the
    module's voice image (`logcheck`); `nomod` asserts the stale-B stop
    (CTRL 0, B's high byte `$FF`) on every voice. Mutants `m5order` (a
    reprogram without the B step), `nostop` (without CTRL 0), `bokstop` (no
    stale-B stop) and `m5loop` (a loop change without the B step) are
    killed.
  - Torn fields while a voice plays: a live RATE (2 bytes) or a changed
    loop's A (3 bytes) is written byte by byte; for the few microseconds in
    between the voice may play one sample at a mixed rate, or jump to a
    mixed A if it reaches B in that window. B itself never takes an earlier
    value while repeat is on (each step stays at or beyond LENGTH).
  - **Known limit (rev15-paula L2, kept):** an A change of a loop that stays
    a loop is written directly (high byte first), not through B =
    `$FFFFFF`; a torn A can momentarily exceed the new A (even LENGTH, if
    the high byte grows first) if the voice hits B inside the write. The B
    detour was not taken because a short loop reaching LENGTH while B is
    beyond it would END instead of looping (a 2-sample null loop is ~0.25 ms
    long, comparable to the write window), leaving a silent parked channel:
    worse than the torn A. Trigger: a loop that stays a loop gets a new A
    whose high byte differs from the old one (loops of 64 KB or more), and
    the voice reaches B during the 3-byte write. Consequence: one jump to a
    wrong A, possibly past LENGTH, so the voice plays sample memory beyond
    the block until the next B or program. Contiguous A changes are rare in
    replays (a new instrument's loop is normally a downgrade, reprogrammed at
    the block end).
  - **Known limit (rev15-paula L3):** an upgrade (a one-shot block turned
    into a hardware loop by its latches) has a window of ~10 sampler writes
    with B = `$FFFFFF` and repeat still off. Trigger: the voice reaches
    LENGTH inside that window (the latches are written just as the block
    ends). Consequence: the voice finishes, but the image says CTRL 3, so
    the module never reprograms it and the channel parks silent until the
    next retrigger (DMA off/on). Silence, not noise; rare.
- **Rules.** The sampler's own end IRQ (CTRL bit 2) is **never** set:
  `mach_irq` acknowledges CIA #1 only, so a level cartridge IRQ would
  storm. The sampler is written **only when the host has set `cfg_hw`**
  inside a U64E hold that maps it (`Map Ultimate Audio $DF20-DFFF`, restored
  by the hold; the U64E runner silences every voice before the restore); it
  is **never probed**: on VICE `$DF20-$DFFF` mirrors the REU's REC, where
  `$DF21` (the command register, `$10` at power-up) would read like the
  sampler's version byte and a write would start a DMA (the log build sets
  `cfg_hw` = 1 on VICE because its stores go to the logger, never `$DF20`).
- **Host-visible state** (the state page; layouts in `aud.s`'s header): per
  channel a 32-byte voice image in the sampler's register layout (+0..+17;
  +18..+1E the cached block length) and a 32-byte channel record (latches,
  the playing block, the deadline, blocks started, programs, the E of the
  latest post; +29..+31 are scratch); counters `st_nw`, `st_nfire`,
  `st_ndat`, `st_now` (E of the latest DMACON, downgrade or firing),
  `st_nres`, `st_npark`.
- **Cost** (P3, VICE phi2-exact, `tests/run_paula.py --perf`): evidence
  `build/evidence/w15-paula/final_perf.json` (the numbers are in the W15-P
  report).
