# Wave 2 Brief — Instruction-Group Agents (G1–G6)

Read `docs/CONTRACTS.md` first (binding): ZP map, ABI, dispatch family map (your ownership), cross-op names + calling convention. This brief adds the working knowledge from the core agent plus the build/test protocol. Six agents work in the SAME tree concurrently.

## Non-negotiables

- You own EXACTLY ONE file: `runtime/ops/<yourfile>.s`. Create/edit nothing else anywhere. Never run git. Never `make clean` (shared build dir).
- ca65 syntax. Start with `.include "../macros.inc"` (resolved relative to your file). NEVER `.import` ZP names — macros.inc defines them.
- Comment cycle counts on hot paths; note anything slower than ~1500 cycles/instruction.

## Core conventions (from the core agent — trust these)

- ALU primitives: `t_res = t_op1 OP t_op2`, where `t_op1` is the DESTINATION operand (so `alu_sub`/`alu_cmp` compute dst − src). Size in A (0/1/2 = B/W/L). All CCR handled inside — including sticky-Z ADDX/SUBX and X-preservation on CMP/logic. Use them wherever the 68k op's flags match; hand-roll flags only where the op defines different behavior.
- `ea_calc`: A = size, X = 6-bit mode|reg field. Leaves `t_ea` + internal kind info. `ea_load` fills only the SIZED bytes of `t_op1` (upper bytes unchanged — clear them yourself if you need a full 32-bit value); `ea_store` writes sized bytes only. For An destinations sign-extend yourself and use size L.
- RMW pattern works: `ea_calc` → `ea_load` → alu → `ea_store` (`t_ea` is preserved across the ALU).
- Immediate mode (#imm) resolves as a memory operand pointing into the instruction stream.
- CLOBBER RULE: `fetch_word` AND `ea_calc`/`ea_load` clobber `op_lo`/`op_hi`, `t_tmp`, `m_*`. Extract every opcode field you need FIRST (the last fetched word is mirrored at `t_tmp+0/1`, but that's clobbered too). Stash fields in `t_op1`/`t_op2`/`t_res` bytes or (sparingly) `t_tmp` between calls — document what you use.
- `$7A–$7F` are core-internal — never touch. Loader ZP `$02–$0F` — never touch.
- End every handler with `jmp dispatch_next`. Halt (privileged/unimplemented-by-design): `lda #HALT_ILLEGAL` / `jmp emu_halt`.
- Cross-ops (op_scc, op_movem, …): entered by plain `jmp` with the ORIGINAL opcode word untouched in `op_lo`/`op_hi`; they do their own decode and end with `jmp dispatch_next`. Until an owner lands, gen_dispatch emits exported `jmp op_illegal` stubs, so builds always link.

## Build & test protocol (parallel-safe)

- Vectors are already generated at `build/vectors/` (SHARED, READ-ONLY — never regenerate, never `make vectors`).
- Build into a PRIVATE dir: `make runtime BUILD=build/g<N>` — the dispatch table is auto-regenerated there and picks up your `.export` lines (it scans `runtime/ops/*.s`).
- Test: `python3 -m tests.run_vectors --prg build/g<N>/runner.prg --vectors build/vectors --filter <STEM>` (repeatable per stem; concurrent VICE instances across agents are fine — the harness queues).
- Batches bucket by OPERATION, not by our family map: e.g. `ADD.b.bin` also contains ADDI ($06xx) and ADDQ ($5xxx) cases; `MOVE.*` may contain $4xxx cases owned by others. ACCEPTANCE = every case whose opcode belongs to YOUR ownership passes. Failures on opcodes outside your ownership (including `op_illegal` halts from families that don't exist yet): list them in your report, do NOT fix or wait for them.
- An op you own halting with reason ILLEGAL = your missing decode = your bug.
- Other agents' edits may make a shared-tree build transiently fail on files you don't own — retry a couple of minutes later; only your own file's errors are yours.

## Reference

- 68000 semantics reference: Motorola M68000 PRM behavior as encoded in the vectors — when PRM prose and vectors disagree, THE VECTORS WIN (they're Musashi-generated from real-hardware-validated tests). "Undefined" flag bits must match the vectors.
- Batch stems available: ABCD ADD.[bwl] ADDA.[wl] ADDX.[bwl] AND.[bwl] ASL/ASR/LSL/LSR/ROL/ROR/ROXL/ROXR.[bwl] Bcc BCHG BCLR BSET BTST BSR CLR.[bwl] CMP.[bwl] CMPA.[wl] DBcc DIVS DIVU EOR.[bwl] EXG EXT.[wl] JMP JSR LEA MOVE.[bwlq] MOVEA.[wl] MOVEM.[wl] MULS MULU NBCD NEG.[bwl] NEGX.[bwl] NOP NOT.[bwl] OR.[bwl] PEA RTR RTS SBCD Scc SUB.[bwl] SUBA.[wl] SUBX.[bwl] SWAP TST.[bwl]

## Final report must include

Implemented ops; per-stem owned-case pass counts (e.g. "ADD.b: 41/41 owned green, 9 foreign-op failures listed"); flag semantics you hand-rolled and why; cycle notes; any contract/brief deviation; anything the integrator must know.
