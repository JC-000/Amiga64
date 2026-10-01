; ---------------------------------------------------------------------------
; runtime/rc/zp.inc — zero-page resource contract.  Owner: rc (RC-contracts).
;
; SINGLE SOURCE OF TRUTH for every C64 zero-page address this project
; claims.  Consumed via runtime/macros.inc (runtime/*.s, runtime/ops/*.s)
; or included directly (tests/runner/*.s).  Machine-checked by
; host/rescheck.py (`make rescheck`) — grammar in docs/RESOURCES.md.
;
; Every equate is .ifndef-guarded, so a variant build may relocate a slot
; with `ca65 -D name=value`.  Values passed through make/shell MUST be
; $-free (`-D r_d0=0x40`, not `-D r_d0=$40`): the shell eats `$4` and the
; assemble silently succeeds with the slot at 0 (see docs/RESOURCES.md).
;
; Region grammar (parsed by host/rescheck.py):
;   ; @space <name> $<start>-$<end>
;   ; @region <owner> $<start>-$<end> <role...>
; Equate lines below a @region line belong to it and must lie inside its
; range.  Owner tags: docs/RESOURCES.md prefix registry.
; ---------------------------------------------------------------------------

; @space zp $00-$FF

; @region sys $00-$01 6510 CPU port (DDR/data) - hardware, never claimable
; (no equates: core never touches $01; default bank state assumed)
.ifndef CPU_PORT
CPU_PORT  = $01         ; W7-V c2v: 6510 data port; c2v.s banks BASIC out ($37 -> $36) per call, $34 under sei for the under-I/O screen fill; always restored
.endif

; @region run $02-$0F loader/harness scratch - two overlaid banks, entries never run concurrently
; runner.s bank (test_run_case):
.ifndef cp
cp        = $02         ; 2-byte walking pointer into buf_case
.endif
.ifndef cnt
cnt       = $04         ; init-pair loop counter
.endif
.ifndef nexp
nexp      = $05         ; number of expected-mem pairs
.endif
.ifndef expp
expp      = $06         ; 2-byte saved pointer to expected-mem pairs
.endif
.ifndef rbidx
rbidx     = $08         ; readback index
.endif
; run_program.s bank (test_load_chunk / test_run_program) — overlays the
; runner.s bank byte-for-byte by design (documented in run_program.s):
.ifndef srcp
srcp      = $02         ; 2-byte walking pointer into the record payload
.endif
.ifndef lcnt
lcnt      = $04         ; 2-byte remaining payload byte count
.endif
.ifndef gaddr
gaddr     = $06         ; 4-byte guest-address cursor (load entry)
.endif
.ifndef icount
icount    = $06         ; 4-byte instruction counter (run entry, overlays gaddr)
.endif
.ifndef nobud
nobud     = $0A         ; 1: budget == 0 (unlimited)
.endif

; --- Interpreter contract register file ($10-$7F, docs/CONTRACTS.md) -------
; All multi-byte values little-endian (LSB first).

; @region core $10-$2F data registers D0-D7, 4 bytes each
.ifndef r_d0
r_d0      = $10
.endif
.ifndef r_d1
r_d1      = $14
.endif
.ifndef r_d2
r_d2      = $18
.endif
.ifndef r_d3
r_d3      = $1C
.endif
.ifndef r_d4
r_d4      = $20
.endif
.ifndef r_d5
r_d5      = $24
.endif
.ifndef r_d6
r_d6      = $28
.endif
.ifndef r_d7
r_d7      = $2C
.endif

; @region core $30-$4F address registers A0-A7, 4 bytes each; A7 = active SP
.ifndef r_a0
r_a0      = $30
.endif
.ifndef r_a1
r_a1      = $34
.endif
.ifndef r_a2
r_a2      = $38
.endif
.ifndef r_a3
r_a3      = $3C
.endif
.ifndef r_a4
r_a4      = $40
.endif
.ifndef r_a5
r_a5      = $44
.endif
.ifndef r_a6
r_a6      = $48
.endif
.ifndef r_a7
r_a7      = $4C
.endif

; @region core $50-$53 program counter, 4 bytes (bits 24-31 always 0)
.ifndef r_pc
r_pc      = $50
.endif

; @region core $54-$55 status register: $54 = CCR (X N Z V C), $55 = SR hi
.ifndef r_sr
r_sr      = $54
.endif

; @region core $56-$57 current opcode word (op_hi = 68k bits 15-8)
.ifndef op_lo
op_lo     = $56
.endif
.ifndef op_hi
op_hi     = $57
.endif

; @region core $58-$5B memory-API guest address argument (LE)
.ifndef m_addr
m_addr    = $58
.endif

; @region core $5C-$5F memory-API value in/out (LE)
.ifndef m_val
m_val     = $5C
.endif

; @region core $60-$63 resolved effective address (LE)
.ifndef t_ea
t_ea      = $60
.endif

; @region core $64-$6B ALU operands: t_op1 = destination, t_op2 = source (LE)
.ifndef t_op1
t_op1     = $64
.endif
.ifndef t_op2
t_op2     = $68
.endif

; @region core $6C-$6F ALU result (LE)
.ifndef t_res
t_res     = $6C
.endif

; @region core $70-$77 free scratch - any core routine may clobber (memory API bounce $70-$73, fetch_word mirror $70-$71)
.ifndef t_tmp
t_tmp     = $70
.endif

; @region core $78-$79 interpreter halt state: st_halt, st_reason
.ifndef st_halt
st_halt   = $78
.endif
.ifndef st_reason
st_reason = $79
.endif

; @region core $7A-$7F interpreter-internal state - opaque to group handlers
.ifndef ea_kind
ea_kind   = $7A         ; 0 = memory operand (t_ea valid), 1 = register
.endif
.ifndef ea_reg
ea_reg    = $7B         ; ZP offset of register from r_d0 (reg*4, +$20 An)
.endif
.ifndef ea_size
ea_size   = $7C         ; size latched by ea_calc
.endif
.ifndef st_step
st_step   = $7D         ; nonzero = emu_step (stop after 1 instruction)
.endif
.ifndef w_zacc
w_zacc    = $7E         ; ALU zero-accumulator scratch
.endif
.ifndef w_ptmp
w_ptmp    = $7F         ; ALU/EA flag scratch
.endif

; @region core $80-$87 fetch-window state w_pg0/w_pg1/w_inv + REC shadow m_rok/m_rlen/m_rlo/m_rhi/m_rbk - linker-assigned (ZEROPAGE segment, core.s)
; (no equates: allocated with .res in segment ZEROPAGE; c64.cfg ZP memory
;  area starts at $0080, so these land at $80-$87 in declaration order —
;  see build/runner.map.  REC shadow: WS-A, docs/WAVE4_BRIEF.md C1/C4)

; @region core $88-$94 supervisor mode & exceptions (W6-S, docs/CONTRACTS.md): flag, inactive SP, exception-mode instruction snapshot, latched flag
; Outside the $10-$7F block on purpose: emu_reset does NOT clear these.
; st_exc and r_osp belong to the embedder (the boot stub zeroes them; the
; super vector driver pokes them before test_run_case); st_ipc/st_iop/
; st_sp6 are rewritten at the start of every instruction in exception mode;
; st_exl is written only by emu_reset (the latched copy of st_exc).
; core.s asserts the linker's ZEROPAGE .res pool ends below st_exc.
.ifndef st_exc
st_exc    = $88         ; nonzero = deliver exceptions (latched by emu_reset)
.endif
.ifndef r_osp
r_osp     = $89         ; 4 bytes LE: inactive SP (USP while S=1, else SSP)
.endif
.ifndef st_ipc
st_ipc    = $8D         ; 4 bytes LE: PC of the current instruction (exc mode)
.endif
.ifndef st_iop
st_iop    = $91         ; 2 bytes: its opcode word, lo/hi like op_lo/op_hi
.endif
.ifndef st_sp6
st_sp6    = $93         ; 6502 SP at instruction start (exception unwind)
.endif
.ifndef st_exl
st_exl    = $94         ; st_exc as LATCHED by emu_reset: every runtime test reads this
.endif

; W8-L mach: S3.2 machine loop (runtime/ops/mach.s, docs/CONTRACTS.md "Machine loop (S3.2)").
; Machine state, not interpreter state: emu_reset does not touch it; mach_init
; clears it.  None of $95-$99 is written by the KERNAL IRQ (it keeps $A0-$A2,
; $91, $C5/$CB/$C6, $F5-$F6), so the values survive between host calls.
; @region s3 $95-$99 machine loop: pending-event flags (IRQ handler) and the guest frame counter
.ifndef ev_pend
ev_pend     = $95       ; W8-L mach: pending events, bit 0 = CIA1 timer A (set by mach_irq, taken by mach_run)
.endif
.ifndef mach_frames
mach_frames = $96       ; W8-L mach: u32 LE guest frames (VERTBs) since mach_init
.endif

; @region free $9A-$FF UNALLOCATED - $9A-$F8 is still inside the ld65 ZEROPAGE pool (c64.cfg: start $0080 size $0079) but NOT usable by .res past $87 (core.s asserts); claim via this file + rescheck green

; --- Contract invariants (assemble-time; keep true under -D relocation) ----
.assert r_a0 = r_d0 + $20, error, "getreg/setreg do reg*4 indexing from r_d0 - A regs must follow D regs contiguously"
.assert r_pc = r_d0 + $40, error, "runner dumps/loads regs+PC+SR as 70 contiguous bytes from r_d0"
.assert r_sr = r_pc + 4,   error, "SR must directly follow PC (70-byte contiguous reg dump)"
.assert st_reason = st_halt + 1, error, "st_reason must directly follow st_halt (runner copies both)"
.assert st_halt = t_tmp + 8, error, "emu_reset clears $10-$7F as one block ending at w_ptmp"
