; ===========================================================================
; runtime/core.s — Amiga64 interpreter core (68000 user-mode subset on 6502)
; Owner: core agent.  Contract: docs/CONTRACTS.md (ZP map, ABI, dispatch).
;
; Conventions
;   - All multi-byte ZP values little-endian; the memory API converts to/from
;     guest big-endian at the REU boundary.
;   - Guest memory = REU, guest address A -> REU offset A (1:1, 24-bit).
;   - t_tmp ($70-$77) is free scratch; the memory API uses $70-$73 as its
;     REU DMA bounce buffer, fetch_word mirrors the fetched word to
;     t_tmp+0(lo)/t_tmp+1(hi) on return.
;   - $7A-$7F are interpreter-internal (ea_kind/ea_reg/ea_size/st_step/
;     w_zacc/w_ptmp) — opaque to group handlers.
;   - ea_calc/ea_load and the extension-word fetches CLOBBER op_lo/op_hi:
;     handlers must extract all opcode fields they need BEFORE fetching
;     extension words or calling ea_calc.
; ===========================================================================

.include "macros.inc"

; --- Contract ABI exports --------------------------------------------------
.export emu_reset, emu_step, emu_run, dispatch_next, fetch_word
.export mem_read8, mem_read16, mem_read32
.export mem_write8, mem_write16, mem_write32
.export ea_calc, ea_load, ea_store
.export alu_add, alu_sub, alu_cmp, alu_and, alu_or, alu_eor
.export alu_not, alu_neg, alu_addx, alu_subx, alu_ext
.export op_aline, op_fline, op_illegal
.export emu_halt                ; convenience: A = HALT_* reason -> halt

; ZP map / constants re-exported so they land in build/runner.lbl for the
; host harness.  Group agents: use runtime/macros.inc, do NOT .import these.
.exportzp r_d0, r_d1, r_d2, r_d3, r_d4, r_d5, r_d6, r_d7
.exportzp r_a0, r_a1, r_a2, r_a3, r_a4, r_a5, r_a6, r_a7
.exportzp r_pc, r_sr, op_lo, op_hi, m_addr, m_val
.exportzp t_ea, t_op1, t_op2, t_res, t_tmp, st_halt, st_reason
.export HALT_OK, HALT_ALINE, HALT_FLINE, HALT_ILLEGAL
.export buf_case, buf_result

; --- Imports ---------------------------------------------------------------
.import dispatch_lo, dispatch_hi        ; generated: build/gen/dispatch.s
.import main                            ; runner entry (tests/runner owns it)

; ===========================================================================
; PRG load address + BASIC SYS stub
; ===========================================================================
.segment "LOADADDR"
        .word $0801

.segment "STARTUP"
basic_stub:                             ; 10 SYS 2061
        .word @next                     ; line link
        .word 10                        ; line number
        .byte $9E, "2061", $00          ; SYS 2061 ($080D)
@next:  .word $0000                     ; end of program
        jmp main                        ; $080D

.segment "CODE"

; ===========================================================================
; emu_reset — clear interpreter state ($10-$7F), st_halt = 0
; ===========================================================================
.proc emu_reset
        lda #0
        ldx #$6F
@lp:    sta $10,x
        dex
        bpl @lp
        rts
.endproc

; ===========================================================================
; Step loop / dispatch
;
; emu_step: execute exactly one guest instruction (returns with st_halt set
;           if it halted/trapped).
; emu_run:  loop until st_halt != 0.
; dispatch_next: handlers jmp here when done retiring an instruction.
;
; Dispatch mechanism: two 256-byte tables (dispatch_lo/dispatch_hi, page-
; aligned in segment DISPATCH) indexed by op_hi, patched into a self-
; modifying "jmp abs".  Cost of the select itself:
;     ldx op_hi (3) + 2x lda abs,x (4+4) + 2x sta abs (4+4) + jmp abs (3)
;   = 22 cycles.  Full per-instruction loop overhead (run mode) excluding
; the handler body: dispatch_next re-check (11) + halt check (5) +
; fetch_word (~150 incl. REU DMA) + select (22) ~= 190 cycles.
; ===========================================================================
emu_step:
        lda #1
        sta st_step
        bne dispatch_go         ; always
emu_run:
        lda #0
        sta st_step
        ; fall through
dispatch_go:
        lda st_halt             ; 3
        bne dispatch_done       ; 2 (not taken)
        jsr fetch_word          ; op_lo/op_hi <- (PC), PC += 2
        ldx op_hi               ; 3
        lda dispatch_lo,x       ; 4 (tables page-aligned: never +1)
        sta jvec+1              ; 4
        lda dispatch_hi,x       ; 4
        sta jvec+2              ; 4
jvec:   jmp $FFFF               ; 3 (self-modified)

dispatch_next:                  ; <- handlers return here
        lda st_halt             ; 3
        bne dispatch_done       ; 2
        lda st_step             ; 3
        beq dispatch_go         ; 3: run mode -> next instruction
dispatch_done:
        rts

; ===========================================================================
; fetch_word — read guest big-endian word at PC into op_lo/op_hi (mirrored
; to t_tmp+0=lo/t_tmp+1=hi), PC += 2.  Handlers use it for extension words.
; Clobbers: A, m_addr, m_val, t_tmp.  ~150 cycles (incl. REU DMA).
; ===========================================================================
.proc fetch_word
        lda r_pc                ; m_addr = PC
        sta m_addr
        lda r_pc+1
        sta m_addr+1
        lda r_pc+2
        sta m_addr+2
        lda r_pc+3
        sta m_addr+3
        clc                     ; PC += 2 (bits 24-31 stay 0)
        lda r_pc
        adc #2
        sta r_pc
        bcc @nc
        inc r_pc+1
        bne @nc
        inc r_pc+2
@nc:    jsr mem_read16
        lda m_val
        sta op_lo
        sta t_tmp
        lda m_val+1
        sta op_hi
        sta t_tmp+1
        rts
.endproc

; ===========================================================================
; Memory API — v0: one REU DMA per call through the REC at $DF00.
; Guest 24-bit address in m_addr (LE, byte 3 ignored), value in m_val (LE).
; Big-endian conversion happens here: the DMA bounce buffer is t_tmp+0..3
; (holds guest byte order), m_val holds LE.
; Requires I/O mapped in at $DF00 (default bank state; core never touches $01).
; Cost per call ~= 95-115 cycles (setup ~70 + DMA 1 cycle/byte + copies).
; ===========================================================================

; A = transfer length (1/2/4). Programs the REC for t_tmp <-> REU[m_addr].
.proc mio_setup
        sta REC_LENLO           ; 4
        lda #0
        sta REC_LENHI           ; 4
        sta REC_C64HI           ; 4  C64 base = $0070 (t_tmp)
        sta REC_ACTRL           ; 4  both addresses increment
        lda #<t_tmp
        sta REC_C64LO           ; 4
        lda m_addr
        sta REC_REULO
        lda m_addr+1
        sta REC_REUHI
        lda m_addr+2
        sta REC_REUBANK
        rts
.endproc

.proc mem_read8
        lda #1
        jsr mio_setup
        lda #REU_CMD_FETCH
        sta REC_COMMAND         ; DMA executes here, CPU stalled
        lda t_tmp
        sta m_val
        rts
.endproc

.proc mem_read16
        lda #2
        jsr mio_setup
        lda #REU_CMD_FETCH
        sta REC_COMMAND
        lda t_tmp               ; guest [addr] = high byte
        sta m_val+1
        lda t_tmp+1             ; guest [addr+1] = low byte
        sta m_val
        rts
.endproc

.proc mem_read32
        lda #4
        jsr mio_setup
        lda #REU_CMD_FETCH
        sta REC_COMMAND
        lda t_tmp
        sta m_val+3
        lda t_tmp+1
        sta m_val+2
        lda t_tmp+2
        sta m_val+1
        lda t_tmp+3
        sta m_val
        rts
.endproc

.proc mem_write8
        lda m_val
        sta t_tmp
        lda #1
        jsr mio_setup
        lda #REU_CMD_STASH
        sta REC_COMMAND
        rts
.endproc

.proc mem_write16
        lda m_val+1
        sta t_tmp               ; high byte first (guest BE)
        lda m_val
        sta t_tmp+1
        lda #2
        jsr mio_setup
        lda #REU_CMD_STASH
        sta REC_COMMAND
        rts
.endproc

.proc mem_write32
        lda m_val+3
        sta t_tmp
        lda m_val+2
        sta t_tmp+1
        lda m_val+1
        sta t_tmp+2
        lda m_val
        sta t_tmp+3
        lda #4
        jsr mio_setup
        lda #REU_CMD_STASH
        sta REC_COMMAND
        rts
.endproc

; ===========================================================================
; Effective-address resolution
;
; ea_calc: A = size (0=B,1=W,2=L), X = 6-bit mode|reg field (bits 5-3 mode,
;          bits 2-0 reg).  Leaves:
;            ea_kind = 0 -> memory operand, guest address in t_ea
;            ea_kind = 1 -> register operand, ea_reg = ZP offset from r_d0
;          Postinc/predec adjust An here (byte on A7 adjusts by 2).
;          May fetch extension words: op_lo/op_hi/t_tmp/m_* clobbered.
;          Illegal mode-7 combos (reg 5-7) jump to op_illegal (no return).
;
; ea_load:  A = size. Reads the operand of the last ea_calc into the sized
;           bytes of t_op1 (upper bytes of t_op1 UNCHANGED for B/W).
;           t_ea/ea_kind/ea_reg preserved -> read-modify-write works:
;           ea_calc, ea_load, alu_xxx, ea_store.
; ea_store: A = size. Writes the sized bytes of t_res to the operand of the
;           last ea_calc (register upper bytes preserved — for An results
;           sign-extend first and store with size L, per 68k semantics).
;
; NOTE for handlers: immediate mode is resolved as a "memory" operand whose
; t_ea points into the instruction stream (PC advanced past the data), so
; ea_load just works; ea_store to #imm is illegal (don't).
; ===========================================================================
ea_calc:
        sta ea_size
        txa
        and #7
        asl
        asl
        sta ea_reg              ; reg*4
        txa
        lsr
        lsr
        lsr                     ; mode 0-7
        tay
        lda ea_modetab_lo,y
        sta ea_jv+1
        lda ea_modetab_hi,y
        sta ea_jv+2
ea_jv:  jmp $FFFF               ; self-modified

ea_m0:                          ; Dn direct
        lda #1
        sta ea_kind
        rts
ea_m1:                          ; An direct: bias offset into the A-reg file
        lda #1
        sta ea_kind
        lda ea_reg
        clc
        adc #$20                ; r_a0 - r_d0
        sta ea_reg
        rts
ea_m2:                          ; (An)
        jmp ea_an_ind
ea_m3:                          ; (An)+
        jsr ea_an_ind           ; t_ea = An (pre-increment value)
        jsr ea_amount           ; A = 1/2/4 (byte on A7 -> 2)
        ldy ea_reg
        clc
        adc r_a0,y              ; An += amount (32-bit)
        sta r_a0,y
        lda r_a0+1,y
        adc #0
        sta r_a0+1,y
        lda r_a0+2,y
        adc #0
        sta r_a0+2,y
        lda r_a0+3,y
        adc #0
        sta r_a0+3,y
        rts
ea_m4:                          ; -(An)
        jsr ea_amount
        sta w_ptmp
        ldy ea_reg
        sec
        lda r_a0,y              ; An -= amount (32-bit)
        sbc w_ptmp
        sta r_a0,y
        lda r_a0+1,y
        sbc #0
        sta r_a0+1,y
        lda r_a0+2,y
        sbc #0
        sta r_a0+2,y
        lda r_a0+3,y
        sbc #0
        sta r_a0+3,y
        jmp ea_an_ind           ; t_ea = updated An
ea_m5:                          ; d16(An)
        jsr ea_an_ind
        jsr fetch_word
        jmp ea_add_sext16
ea_m6:                          ; d8(An,Xn) brief extension word
        jsr ea_an_ind
        jsr fetch_word
        jmp ea_add_brief
ea_m7:                          ; abs.W / abs.L / d16(PC) / d8(PC,Xn) / #imm
        txa                     ; X still = mode|reg field
        and #7
        beq ea_absw
        cmp #1
        beq ea_absl
        cmp #2
        beq ea_pcd16
        cmp #3
        beq ea_pcidx
        cmp #4
        beq ea_imm
        jmp op_illegal

ea_absw:                        ; abs.W, sign-extended to 32 bits
        jsr fetch_word
        lda op_lo
        sta t_ea
        ldy #0
        lda op_hi
        sta t_ea+1
        bpl @pos
        dey
@pos:   sty t_ea+2
        sty t_ea+3
        lda #0
        sta ea_kind
        rts
ea_absl:                        ; abs.L: high word first in the stream
        jsr fetch_word
        lda op_lo
        sta t_ea+2
        lda op_hi
        sta t_ea+3
        jsr fetch_word
        lda op_lo
        sta t_ea
        lda op_hi
        sta t_ea+1
        lda #0
        sta ea_kind
        rts
ea_pcd16:                       ; d16(PC): base = address of extension word
        jsr ea_pc_base
        jsr fetch_word
        jmp ea_add_sext16
ea_pcidx:                       ; d8(PC,Xn)
        jsr ea_pc_base
        jsr fetch_word
        jmp ea_add_brief
ea_imm:                         ; #imm: operand lives in the instruction
        jsr ea_pc_base          ; stream; t_ea points at the datum
        lda ea_size
        beq @b
        cmp #2
        beq @l
        lda #2                  ; word
        jmp ea_pc_add
@b:     inc t_ea                ; byte datum = LOW byte of the ext word
        bne @b1
        inc t_ea+1
        bne @b1
        inc t_ea+2
@b1:    lda #2
        jmp ea_pc_add
@l:     lda #4
        jmp ea_pc_add

; --- EA helpers ------------------------------------------------------------
ea_an_ind:                      ; t_ea = An (ea_reg = reg*4), kind = memory
        ldy ea_reg
        lda r_a0,y
        sta t_ea
        lda r_a0+1,y
        sta t_ea+1
        lda r_a0+2,y
        sta t_ea+2
        lda r_a0+3,y
        sta t_ea+3
        lda #0
        sta ea_kind
        rts

ea_pc_base:                     ; t_ea = PC (current, past opcode/prev ext)
        lda r_pc
        sta t_ea
        lda r_pc+1
        sta t_ea+1
        lda r_pc+2
        sta t_ea+2
        lda r_pc+3
        sta t_ea+3
        lda #0
        sta ea_kind
        rts

ea_pc_add:                      ; r_pc += A (bits 24-31 stay 0)
        clc
        adc r_pc
        sta r_pc
        bcc @d
        inc r_pc+1
        bne @d
        inc r_pc+2
@d:     rts

ea_amount:                      ; A = operand byte count; byte on A7 -> 2
        ldx ea_size
        lda ea_szbytes,x
        cmp #1
        bne @ok
        ldy ea_reg
        cpy #$1C                ; 7*4 = A7
        bne @ok
        lda #2                  ; keep SP even (68k quirk)
@ok:    rts

ea_add_sext16:                  ; t_ea += sext16(op_hi:op_lo)
        ldy #0
        lda op_hi
        bpl @pos
        dey                     ; Y = $FF
@pos:   clc
        lda t_ea
        adc op_lo
        sta t_ea
        lda t_ea+1
        adc op_hi
        sta t_ea+1
        tya
        adc t_ea+2
        sta t_ea+2
        tya
        adc t_ea+3
        sta t_ea+3
        rts

; Brief extension word (68000): bit15 D/A, bits14-12 reg, bit11 W/L,
; bits10-9 scale (ignored on 68000, always *1), bits7-0 d8.
ea_add_brief:                   ; t_ea += sext8(op_lo) + index(op_hi)
        ldy #0
        lda op_lo
        bpl @p
        dey
@p:     clc
        adc t_ea
        sta t_ea
        tya
        adc t_ea+1
        sta t_ea+1
        tya
        adc t_ea+2
        sta t_ea+2
        tya
        adc t_ea+3
        sta t_ea+3
        lda op_hi               ; top 4 bits = register index 0-15
        and #$F0
        lsr
        lsr                     ; index*4 = ZP offset from r_d0
        tax
        lda op_hi
        and #$08                ; W/L bit
        bne @long
        ldy #0                  ; word index: sext16(Xn.w)
        lda r_d0+1,x
        bpl @wp
        dey
@wp:    clc
        lda r_d0,x
        adc t_ea
        sta t_ea
        lda r_d0+1,x
        adc t_ea+1
        sta t_ea+1
        tya
        adc t_ea+2
        sta t_ea+2
        tya
        adc t_ea+3
        sta t_ea+3
        rts
@long:  clc                     ; long index: Xn.l
        lda r_d0,x
        adc t_ea
        sta t_ea
        lda r_d0+1,x
        adc t_ea+1
        sta t_ea+1
        lda r_d0+2,x
        adc t_ea+2
        sta t_ea+2
        lda r_d0+3,x
        adc t_ea+3
        sta t_ea+3
        rts

; --- ea_load ---------------------------------------------------------------
ea_load:
        ldy ea_kind
        bne @reg
        tax                     ; memory operand: m_addr = t_ea
        lda t_ea
        sta m_addr
        lda t_ea+1
        sta m_addr+1
        lda t_ea+2
        sta m_addr+2
        lda t_ea+3
        sta m_addr+3
        txa
        beq @m8
        cmp #2
        beq @m32
        jsr mem_read16
        lda m_val
        sta t_op1
        lda m_val+1
        sta t_op1+1
        rts
@m8:    jsr mem_read8
        lda m_val
        sta t_op1
        rts
@m32:   jsr mem_read32
        lda m_val
        sta t_op1
        lda m_val+1
        sta t_op1+1
        lda m_val+2
        sta t_op1+2
        lda m_val+3
        sta t_op1+3
        rts
@reg:   tax                     ; register operand
        ldy ea_reg
        lda r_d0,y
        sta t_op1
        txa
        beq @rd
        lda r_d0+1,y
        sta t_op1+1
        cpx #2
        bne @rd
        lda r_d0+2,y
        sta t_op1+2
        lda r_d0+3,y
        sta t_op1+3
@rd:    rts

; --- ea_store --------------------------------------------------------------
ea_store:
        ldy ea_kind
        bne @reg
        tax                     ; memory operand: m_addr = t_ea
        lda t_ea
        sta m_addr
        lda t_ea+1
        sta m_addr+1
        lda t_ea+2
        sta m_addr+2
        lda t_ea+3
        sta m_addr+3
        txa
        beq @m8
        cmp #2
        beq @m32
        lda t_res
        sta m_val
        lda t_res+1
        sta m_val+1
        jmp mem_write16
@m8:    lda t_res
        sta m_val
        jmp mem_write8
@m32:   lda t_res
        sta m_val
        lda t_res+1
        sta m_val+1
        lda t_res+2
        sta m_val+2
        lda t_res+3
        sta m_val+3
        jmp mem_write32
@reg:   tax                     ; register operand: sized bytes only,
        ldy ea_reg              ; upper register bytes preserved
        lda t_res
        sta r_d0,y
        txa
        beq @rd
        lda t_res+1
        sta r_d0+1,y
        cpx #2
        bne @rd
        lda t_res+2
        sta r_d0+2,y
        lda t_res+3
        sta r_d0+3,y
@rd:    rts

; ===========================================================================
; Sized ALU primitives.  ALL CCR logic lives here.
;
; Inputs:  t_op1 = destination operand, t_op2 = source operand, A = size
;          (0=B,1=W,2=L).  Result: sized bytes of t_res (upper bytes
;          unchanged for B/W).  Convention: t_res = t_op1 OP t_op2
;          (e.g. alu_sub/alu_cmp compute t_op1 - t_op2 = dst - src).
; CCR ($54) per 68000 PRM: N = msb of sized result, Z = sized result == 0,
;          V = signed overflow, C = carry/borrow; X = copy of C for
;          add/sub/neg/addx/subx; cmp/logic/ext leave X.  ADDX/SUBX only
;          CLEAR Z on a nonzero result (never set it).
; Clobbers: A, X, Y, w_zacc, w_ptmp.  alu_cmp also writes t_res.
; ===========================================================================

; Shared add/sub body: size dispatch (tax/dex — never touches C, so the
; caller's carry-in survives), byte-wise op, Z-accumulation in w_zacc,
; then jmp to the flag builder with A = final sized byte and the 6502 P
; from the final adc/sbc still live.
.macro ALUADDSUB oper, flagtgt
        .local w, l, b
        tax
        dex
        beq w
        dex
        beq l
b:      lda #0
        sta w_zacc
        lda t_op1
        oper t_op2
        sta t_res
        jmp flagtgt
w:      lda t_op1
        oper t_op2
        sta t_res
        sta w_zacc
        lda t_op1+1
        oper t_op2+1
        sta t_res+1
        jmp flagtgt
l:      lda t_op1
        oper t_op2
        sta t_res
        sta w_zacc
        lda t_op1+1
        oper t_op2+1
        sta t_res+1
        ora w_zacc
        sta w_zacc
        lda t_op1+2
        oper t_op2+2
        sta t_res+2
        ora w_zacc
        sta w_zacc
        lda t_op1+3
        oper t_op2+3
        sta t_res+3
        jmp flagtgt
.endmacro

alu_add:
        clc
        ALUADDSUB adc, flags_add
alu_sub:
        sec
        ALUADDSUB sbc, flags_sub
alu_cmp:
        sec
        ALUADDSUB sbc, flags_cmp
alu_addx:                       ; carry-in = X flag
        tay                     ; save size
        lda r_sr
        and #$10
        cmp #$01                ; C = X
        tya                     ; restore size (tya keeps C)
        ALUADDSUB adc, flags_addx
alu_subx:                       ; borrow-in = X flag (6502 sense inverted)
        tay
        lda r_sr
        and #$10
        eor #$10
        cmp #$01                ; C = !X
        tya
        ALUADDSUB sbc, flags_subx

alu_neg:                        ; t_res = 0 - t_op1 (t_op2 unused)
        tax
        dex
        beq @w
        dex
        beq @l
        lda #0                  ; byte
        sta w_zacc
        sec
        lda #0
        sbc t_op1
        sta t_res
        jmp flags_sub
@w:     sec
        lda #0
        sbc t_op1
        sta t_res
        sta w_zacc
        lda #0
        sbc t_op1+1
        sta t_res+1
        jmp flags_sub
@l:     sec
        lda #0
        sbc t_op1
        sta t_res
        sta w_zacc
        lda #0
        sbc t_op1+1
        sta t_res+1
        ora w_zacc
        sta w_zacc
        lda #0
        sbc t_op1+2
        sta t_res+2
        ora w_zacc
        sta w_zacc
        lda #0
        sbc t_op1+3
        sta t_res+3
        jmp flags_sub

; Logic body: like ALUADDSUB but no carry concerns; flags_logic wants
; A = final sized byte, w_zacc = OR of the lower bytes.
.macro ALULOGIC oper
        .local w, b
        cmp #1
        beq w
        bcc b
        lda t_op1
        oper t_op2
        sta t_res
        sta w_zacc
        lda t_op1+1
        oper t_op2+1
        sta t_res+1
        ora w_zacc
        sta w_zacc
        lda t_op1+2
        oper t_op2+2
        sta t_res+2
        ora w_zacc
        sta w_zacc
        lda t_op1+3
        oper t_op2+3
        sta t_res+3
        jmp flags_logic
w:      lda t_op1
        oper t_op2
        sta t_res
        sta w_zacc
        lda t_op1+1
        oper t_op2+1
        sta t_res+1
        jmp flags_logic
b:      lda #0
        sta w_zacc
        lda t_op1
        oper t_op2
        sta t_res
        jmp flags_logic
.endmacro

alu_and:
        ALULOGIC and
alu_or:
        ALULOGIC ora
alu_eor:
        ALULOGIC eor

alu_not:                        ; t_res = ~t_op1 sized (t_op2 unused)
        cmp #1
        beq @w
        bcc @b
        lda t_op1
        eor #$FF
        sta t_res
        sta w_zacc
        lda t_op1+1
        eor #$FF
        sta t_res+1
        ora w_zacc
        sta w_zacc
        lda t_op1+2
        eor #$FF
        sta t_res+2
        ora w_zacc
        sta w_zacc
        lda t_op1+3
        eor #$FF
        sta t_res+3
        jmp flags_logic
@w:     lda t_op1
        eor #$FF
        sta t_res
        sta w_zacc
        lda t_op1+1
        eor #$FF
        sta t_res+1
        jmp flags_logic
@b:     lda #0
        sta w_zacc
        lda t_op1
        eor #$FF
        sta t_res
        jmp flags_logic

; alu_ext — sign extension.  A = RESULT size: 1 = byte->word (EXT.W),
; 2 = word->long (EXT.L).  t_res = sext(t_op1); N/Z from the result,
; V = C = 0, X unchanged.
alu_ext:
        cmp #2
        beq @l
        ldy #0                  ; byte -> word
        lda t_op1
        sta t_res
        bpl @p1
        dey
@p1:    sty t_res+1
        sta w_zacc              ; low byte
        tya                     ; A = extension byte (final byte)
        jmp flags_logic
@l:     lda t_op1               ; word -> long
        sta t_res
        sta w_zacc
        ldy #0
        lda t_op1+1
        sta t_res+1
        bpl @p2
        dey
@p2:    sty t_res+2
        sty t_res+3
        ora w_zacc              ; A = byte1; zacc = b0|b1
        sta w_zacc
        tya                     ; A = extension byte = final byte (sign)
        jmp flags_logic

; ---------------------------------------------------------------------------
; CCR builders.  Entry (add/sub/cmp/addx/subx): A = final sized result byte,
; 6502 P from the final adc/sbc still live, w_zacc = OR of lower result
; bytes.  flags_logic: same but P not needed.
; 68k CCR layout: bit4 X, bit3 N, bit2 Z, bit1 V, bit0 C.
; ---------------------------------------------------------------------------
flags_add:                      ; N Z V C from op, X = C
        php
        ora w_zacc
        sta w_zacc
        pla
        jsr fl_setx
        jmp fl_insert_z
flags_addx:                     ; like flags_add, Z sticky-clear
        php
        ora w_zacc
        sta w_zacc
        pla
        jsr fl_setx
        jmp fl_insert_z_sticky
flags_sub:                      ; 68k C = borrow = !6502C, X = C
        php
        ora w_zacc
        sta w_zacc
        pla
        eor #$01
        jsr fl_setx
        jmp fl_insert_z
flags_subx:
        php
        ora w_zacc
        sta w_zacc
        pla
        eor #$01
        jsr fl_setx
        jmp fl_insert_z_sticky
flags_cmp:                      ; like flags_sub but X unchanged
        php
        ora w_zacc
        sta w_zacc
        pla
        eor #$01
        jsr p_to_nvc
        sta w_ptmp
        lda r_sr
        and #$10
        ora w_ptmp
        sta w_ptmp
        jmp fl_insert_z
flags_logic:                    ; N Z from result, V = C = 0, X unchanged
        tay                     ; Y = final byte
        ora w_zacc
        sta w_zacc
        lda #0
        cpy #$80                ; C = 1 iff final byte has bit7 set
        bcc @nn
        lda #$08
@nn:    sta w_ptmp
        lda r_sr
        and #$10
        ora w_ptmp
        sta w_ptmp
        jmp fl_insert_z

fl_setx:                        ; A = P (C already 68k-sense): w_ptmp = XNVC
        jsr p_to_nvc
        sta w_ptmp
        and #$01
        beq @nx
        lda w_ptmp
        ora #$10
        sta w_ptmp
@nx:    rts

p_to_nvc:                       ; A = 6502 P -> A = 68k N($08)|V($02)|C($01)
        tay
        and #$80
        lsr
        lsr
        lsr
        lsr                     ; N -> $08
        sta w_ptmp
        tya
        and #$40
        beq @nv
        lda #$02
        ora w_ptmp
        sta w_ptmp
@nv:    tya
        and #$01
        ora w_ptmp
        rts

fl_insert_z:                    ; w_ptmp = X/N/V/C; Z from w_zacc; -> CCR
        lda w_zacc
        bne @nz
        lda w_ptmp
        ora #$04
        sta w_ptmp
@nz:    lda w_ptmp
        sta r_sr
        rts
fl_insert_z_sticky:             ; ADDX/SUBX/NEGX: Z cleared, never set
        lda w_zacc
        bne @nz
        lda r_sr                ; result zero -> keep previous Z
        and #$04
        ora w_ptmp
        sta w_ptmp
@nz:    lda w_ptmp
        sta r_sr
        rts

; ===========================================================================
; Halt / trap handlers (core-owned dispatch targets)
; ===========================================================================
emu_halt:                       ; A = HALT_* reason
        sta st_reason
        lda #1
        sta st_halt
        jmp dispatch_next

op_aline:                       ; $AFFF = clean HALT; other $Axxx = trap hook
        lda op_hi
        cmp #$AF
        bne @trap
        lda op_lo
        cmp #$FF
        bne @trap
        lda #HALT_OK
        jmp emu_halt
@trap:  lda #HALT_ALINE
        jmp emu_halt

op_fline:
        lda #HALT_FLINE
        jmp emu_halt

op_illegal:
        lda #HALT_ILLEGAL
        jmp emu_halt

; ===========================================================================

; ===========================================================================
.segment "RODATA"
ea_szbytes:
        .byte 1, 2, 4
ea_modetab_lo:
        .byte <ea_m0, <ea_m1, <ea_m2, <ea_m3
        .byte <ea_m4, <ea_m5, <ea_m6, <ea_m7
ea_modetab_hi:
        .byte >ea_m0, >ea_m1, >ea_m2, >ea_m3
        .byte >ea_m4, >ea_m5, >ea_m6, >ea_m7
