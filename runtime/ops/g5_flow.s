; ===========================================================================
; runtime/ops/g5_flow.s — G5: control flow + fam_4 miscellany
; Owner: G5.  Contract: docs/CONTRACTS.md, docs/WAVE2_BRIEF.md.
;
; Families owned:
;   fam_4   ($40-$4F): CLR/NEG/NEGX/NOT/TST, EXT/SWAP, LEA/PEA,
;                      JMP/JSR/RTS/RTR/NOP, LINK/UNLK, MOVE from SR,
;                      MOVE to CCR; MOVEM/NBCD forwarded to G6;
;                      privileged/trap ops (MOVE to SR, MOVE USP, TRAP,
;                      TRAPV, RESET, STOP, RTE, TAS, CHK) halt ILLEGAL
;                      (M2 HLE hooks later).
;   fam_bcc ($60-$6F): BRA/BSR/Bcc.
; Cross-ops exported (entered by plain jmp, original opcode in op_lo/op_hi):
;   op_dbcc (used by fam_5/G2), op_exg (used by fam_c/G3).
;
; Clobber discipline: every opcode field is extracted BEFORE fetch_word /
; ea_calc / ea_load (which clobber op_lo/op_hi/t_tmp/m_*).  Fields that
; must survive core calls are latched in the g5_* BSS scratch below.
; ===========================================================================

.include "../macros.inc"

.export fam_4, fam_bcc, op_dbcc, op_exg

.import ea_calc, ea_load, ea_store
.import alu_or, alu_neg, alu_not, alu_subx, alu_ext
.import mem_read16, mem_read32, mem_write32
.import fetch_word, dispatch_next, op_illegal, emu_halt
.import op_movem, op_nbcd               ; G6 (dispatch stubs until it lands)

; --- File-local scratch (survives core calls, unlike t_tmp) ----------------
.segment "BSS"
g5_size:  .res 1                ; latched operand size (0/1/2)
g5_reg:   .res 1                ; latched register offset (reg*4)
g5_cond:  .res 1                ; latched condition code
g5_tmp:   .res 1                ; cond_eval negate flag / pop amount / EXG Ry
g5_tmp2:  .res 1                ; cond_eval N^V scratch / EXG loop counter
g5_base:  .res 4                ; branch base address (PC at extension area)
g5_disp:  .res 2                ; branch displacement (sign info in bit 15)

.segment "CODE"

; ===========================================================================
; fam_4 — secondary decode on op_hi.  Even op_hi: 8-way jump table on
; (op_hi>>1)&7.  Odd op_hi: LEA (opmode 111) / CHK (opmode 110).
; Decode cost: ~25 cycles to reach a leaf handler.
; ===========================================================================
fam_4:
        lda op_hi
        lsr                     ; C = opcode bit 8
        bcs f4_odd
        and #$07                ; ($40,$42,..,$4E) -> 0..7
        tax
        lda f4_evtab_lo,x
        sta f4_jv+1
        lda f4_evtab_hi,x
        sta f4_jv+2
f4_jv:  jmp $FFFF               ; self-modified (PRG is in RAM)

f4_odd:                         ; 0100 rrr1 xx...: LEA / CHK
        lda op_lo
        and #$C0
        cmp #$C0
        beq f4_lea
        cmp #$80
        bne @ill
        lda #HALT_ILLEGAL       ; CHK <ea>,Dn: trap-class op, M2 hook later
        jmp emu_halt
@ill:   jmp op_illegal          ; opmodes 100/101 undefined on 68000

; ---------------------------------------------------------------------------
; LEA <ea>,An — control EA -> An, no flags.
; ---------------------------------------------------------------------------
f4_lea:
        lda op_hi               ; An = opcode bits 11-9
        lsr
        and #$07
        asl
        asl
        sta g5_reg              ; An*4 (latched: ea_calc clobbers op_hi)
        lda op_lo
        and #$3F
        tax
        lda #2
        jsr ea_calc             ; control mode: no inc/dec, t_ea = address
        ldy g5_reg
        lda t_ea
        sta r_a0,y
        lda t_ea+1
        sta r_a0+1,y
        lda t_ea+2
        sta r_a0+2,y
        lda t_ea+3
        sta r_a0+3,y
        jmp dispatch_next

; ---------------------------------------------------------------------------
; f4_easetup — shared front end for the sized single-operand ops:
; size = op_lo bits 7-6 (caller already excluded 11), EA = op_lo bits 5-0.
; Latches g5_size, resolves the EA and loads the operand into t_op1.
; ---------------------------------------------------------------------------
f4_easetup:
        lda op_lo
        lsr
        lsr
        lsr
        lsr
        lsr
        lsr
        sta g5_size
        lda op_lo
        and #$3F
        tax
        lda g5_size
        jsr ea_calc
        lda g5_size
        jmp ea_load             ; tail call: t_op1 = operand (sized)

; ---------------------------------------------------------------------------
; $40: NEGX <ea> (sizes 00-10) / MOVE from SR ($40C0|EA, legal user op on
; the 68000).  NEGX = 0 - dst - X: alu_subx(t_op1=0, t_op2=dst) gives the
; exact 68k flag set (X=C=borrow, V, N; Z sticky-clear).
; ---------------------------------------------------------------------------
f4_negx:
        lda op_lo
        and #$C0
        cmp #$C0
        bne @go
        jmp f4_movesr
@go:    jsr f4_easetup
        ldx #3                  ; t_op2 = operand, t_op1 = 0
@cp:    lda t_op1,x
        sta t_op2,x
        lda #0
        sta t_op1,x
        dex
        bpl @cp
        lda g5_size
        jsr alu_subx
        lda g5_size
        jsr ea_store
        jmp dispatch_next

f4_movesr:                      ; MOVE SR,<ea>: word write of r_sr
        lda op_lo
        and #$3F
        tax
        lda #1
        jsr ea_calc
        lda r_sr                ; LE word: lo = CCR, hi = SR high byte
        sta t_res
        lda r_sr+1
        sta t_res+1
        lda #1
        jsr ea_store            ; flags unaffected
        jmp dispatch_next

; ---------------------------------------------------------------------------
; $42: CLR <ea>.  Write-only architecturally (the 68000's dummy read has no
; visible effect, so we skip it — saves one REU DMA).  NZVC = 0100, X kept.
; $42C0 (MOVE from CCR) is 68010+: illegal here.
; ---------------------------------------------------------------------------
f4_clr:
        lda op_lo
        and #$C0
        cmp #$C0
        bne @go
        jmp op_illegal
@go:    lda op_lo
        lsr
        lsr
        lsr
        lsr
        lsr
        lsr
        sta g5_size
        lda op_lo
        and #$3F
        tax
        lda g5_size
        jsr ea_calc
        lda #0
        sta t_res
        sta t_res+1
        sta t_res+2
        sta t_res+3
        lda g5_size
        jsr ea_store
        lda r_sr
        and #$10                ; X unchanged
        ora #$04                ; N=0 Z=1 V=0 C=0
        sta r_sr
        jmp dispatch_next

; ---------------------------------------------------------------------------
; $44: NEG <ea> / MOVE to CCR ($44C0|EA: word source, low 5 bits -> CCR).
; ---------------------------------------------------------------------------
f4_neg:
        lda op_lo
        and #$C0
        cmp #$C0
        bne @go
        jmp f4_moveccr
@go:    jsr f4_easetup
        lda g5_size
        jsr alu_neg             ; t_res = 0 - t_op1, full 68k NEG flags
        lda g5_size
        jsr ea_store
        jmp dispatch_next

f4_moveccr:                     ; MOVE <ea>,CCR
        lda op_lo
        and #$3F
        tax
        lda #1
        jsr ea_calc
        lda #1
        jsr ea_load
        lda t_op1               ; low byte of the word, bits 7-5 discarded
        and #$1F
        sta r_sr                ; SR high byte untouched
        jmp dispatch_next

; ---------------------------------------------------------------------------
; $46: NOT <ea> / MOVE to SR ($46C0: privileged -> halt).
; ---------------------------------------------------------------------------
f4_not:
        lda op_lo
        and #$C0
        cmp #$C0
        bne @go
        lda #HALT_ILLEGAL       ; MOVE <ea>,SR is privileged
        jmp emu_halt
@go:    jsr f4_easetup
        lda g5_size
        jsr alu_not
        lda g5_size
        jsr ea_store
        jmp dispatch_next

; ---------------------------------------------------------------------------
; $48: NBCD (00) / SWAP+PEA (01) / EXT+MOVEM reg->mem (10,11).
; ---------------------------------------------------------------------------
f4_e48:
        lda op_lo
        and #$C0
        bne @n0
        jmp op_nbcd             ; $4800|EA — G6, opcode untouched
@n0:    cmp #$40
        beq @q1
        lda op_lo               ; bits 10/11: mode 000 = EXT, else MOVEM
        and #$38
        beq f4_ext
        jmp op_movem            ; G6, opcode untouched
@q1:    lda op_lo               ; bits 01: mode 000 = SWAP, else PEA
        and #$38
        beq f4_swap
        jmp f4_pea

; --- EXT.W ($4880|Dn) / EXT.L ($48C0|Dn) -----------------------------------
f4_ext:
        lda op_lo
        and #$07
        asl
        asl
        tay                     ; Dn*4
        lda r_d0,y
        sta t_op1
        lda r_d0+1,y
        sta t_op1+1             ; only used by EXT.L
        lda op_lo
        and #$40                ; bit 6: 0 = EXT.W, 1 = EXT.L
        bne @l
        lda #1                  ; byte -> word (N/Z, V=C=0, X kept in core)
        jsr alu_ext
        lda op_lo               ; alu clobbers Y; op_lo still intact
        and #$07
        asl
        asl
        tay
        lda t_res               ; word store: upper register bytes preserved
        sta r_d0,y
        lda t_res+1
        sta r_d0+1,y
        jmp dispatch_next
@l:     lda #2                  ; word -> long
        jsr alu_ext
        lda op_lo
        and #$07
        asl
        asl
        tay
        lda t_res
        sta r_d0,y
        lda t_res+1
        sta r_d0+1,y
        lda t_res+2
        sta r_d0+2,y
        lda t_res+3
        sta r_d0+3,y
        jmp dispatch_next

; --- SWAP Dn ($4840|Dn): exchange 16-bit halves; N/Z from the 32-bit
; result, V=C=0, X unchanged (alu_or with 0 source builds exactly that). ---
f4_swap:
        lda op_lo
        and #$07
        asl
        asl
        tay
        lda r_d0+2,y            ; t_op1 = swapped value
        sta t_op1
        lda r_d0+3,y
        sta t_op1+1
        lda r_d0,y
        sta t_op1+2
        lda r_d0+1,y
        sta t_op1+3
        lda #0
        sta t_op2
        sta t_op2+1
        sta t_op2+2
        sta t_op2+3
        lda #2
        jsr alu_or              ; t_res = t_op1, flags via core
        lda op_lo
        and #$07
        asl
        asl
        tay
        lda t_res
        sta r_d0,y
        lda t_res+1
        sta r_d0+1,y
        lda t_res+2
        sta r_d0+2,y
        lda t_res+3
        sta r_d0+3,y
        jmp dispatch_next

; --- PEA <ea>: push the control EA's address, no flags. --------------------
f4_pea:
        lda op_lo
        and #$3F
        tax
        lda #2
        jsr ea_calc
        lda t_ea
        sta m_val
        lda t_ea+1
        sta m_val+1
        lda t_ea+2
        sta m_val+2
        lda t_ea+3
        sta m_val+3
        jsr push_long
        jmp dispatch_next

; ---------------------------------------------------------------------------
; $4A: TST <ea> / TAS ($4AC0: M2 hook; also catches $4AFC ILLEGAL).
; TST via alu_or with a zero source: N/Z from operand, V=C=0, X kept.
; ---------------------------------------------------------------------------
f4_tst:
        lda op_lo
        and #$C0
        cmp #$C0
        bne @go
        lda #HALT_ILLEGAL
        jmp emu_halt
@go:    jsr f4_easetup
        lda #0
        sta t_op2
        sta t_op2+1
        sta t_op2+2
        sta t_op2+3
        lda g5_size
        jsr alu_or              ; t_res discarded, only CCR matters
        jmp dispatch_next

; ---------------------------------------------------------------------------
; $4C: MOVEM mem->reg ($4C80/$4CC0 patterns); low half undefined on 68000.
; ---------------------------------------------------------------------------
f4_e4c:
        lda op_lo
        and #$80
        beq @ill
        jmp op_movem            ; G6, opcode untouched
@ill:   jmp op_illegal

; ---------------------------------------------------------------------------
; $4E: TRAP/LINK/UNLK/MOVE USP/RESET/NOP/STOP/RTE/RTS/TRAPV/RTR, JSR, JMP.
; ---------------------------------------------------------------------------
f4_e4e:
        lda op_lo
        and #$C0
        cmp #$C0
        bne @njmp
        jmp f4_jmpea            ; $4EC0-$4EFF JMP <ea>
@njmp:  cmp #$80
        bne @njsr
        jmp f4_jsrea            ; $4E80-$4EBF JSR <ea>
@njsr:  cmp #$40
        bne @ill                ; $4E00-$4E3F undefined
        lda op_lo
        and #$38
        cmp #$10
        bne @nlink
        jmp f4_link             ; $4E50-$4E57
@nlink: cmp #$18
        bne @nunlk
        jmp f4_unlk             ; $4E58-$4E5F
@nunlk: cmp #$30
        beq @spec
        lda #HALT_ILLEGAL       ; TRAP #n ($4E40-$4E4F): M2 hook;
        jmp emu_halt            ; MOVE USP ($4E60-$4E6F): privileged;
                                ; $4E78-$4E7F: 68010+ MOVEC etc.
@spec:  lda op_lo
        and #$07
        cmp #$01
        beq @nop                ; $4E71 NOP
        cmp #$05
        bne @nrts
        jmp f4_rts              ; $4E75
@nrts:  cmp #$07
        bne @nrtr
        jmp f4_rtr              ; $4E77
@nrtr:  lda #HALT_ILLEGAL       ; RESET/STOP/RTE/TRAPV privileged or trap,
        jmp emu_halt            ; $4E74 RTD is 68010+
@nop:   jmp dispatch_next
@ill:   jmp op_illegal

; --- JSR <ea>: push the address AFTER the fully-fetched instruction
; (ea_calc has consumed all extension words by then), PC = EA. -------------
f4_jsrea:
        lda op_lo
        and #$3F
        tax
        lda #2
        jsr ea_calc             ; t_ea = target, PC now past the instruction
        lda r_pc
        sta m_val
        lda r_pc+1
        sta m_val+1
        lda r_pc+2
        sta m_val+2
        lda r_pc+3
        sta m_val+3
        jsr push_long           ; t_ea survives (push uses m_*/t_tmp only)
        jmp f4_setpc

f4_jmpea:                       ; JMP <ea>
        lda op_lo
        and #$3F
        tax
        lda #2
        jsr ea_calc
f4_setpc:
        lda t_ea
        sta r_pc
        lda t_ea+1
        sta r_pc+1
        lda t_ea+2
        sta r_pc+2
        lda #0                  ; PC bits 24-31 always 0 (contract; vectors
        sta r_pc+3              ; are normalized to the 24-bit space)
        jmp dispatch_next

; --- RTS / RTR -------------------------------------------------------------
f4_rts:
        jsr pop_long
        jmp pc_from_mval

f4_rtr:
        jsr pop_word            ; word -> CCR (low 5 bits, X included)
        lda m_val
        and #$1F
        sta r_sr                ; SR high byte untouched
        jsr pop_long
pc_from_mval:
        lda m_val
        sta r_pc
        lda m_val+1
        sta r_pc+1
        lda m_val+2
        sta r_pc+2
        lda #0                  ; mask popped address to 24 bits (contract)
        sta r_pc+3
        jmp dispatch_next

; --- LINK An,#d16: push An, An = SP, SP += sext(d16).  LINK A7 pushes the
; DECREMENTED A7 (Musashi m68k_op_link_16_a7: A7 -= 4 first, then writes
; A7 itself) — verified by the hand-written vector suite. ------------------
f4_link:
        lda op_lo
        and #$07
        asl
        asl
        sta g5_reg
        jsr fetch_word          ; d16 (clobbers op_lo/op_hi)
        lda op_lo
        sta g5_disp
        lda op_hi
        sta g5_disp+1
        ldy g5_reg
        lda r_a0,y              ; m_val = old An
        sta m_val
        lda r_a0+1,y
        sta m_val+1
        lda r_a0+2,y
        sta m_val+2
        lda r_a0+3,y
        sta m_val+3
        cpy #$1C                ; LINK A7: pushed value is the decremented A7
        bne @push
        sec
        lda m_val
        sbc #4
        sta m_val
        lda m_val+1
        sbc #0
        sta m_val+1
        lda m_val+2
        sbc #0
        sta m_val+2
        lda m_val+3
        sbc #0
        sta m_val+3
@push:  jsr push_long           ; A7 -= 4; [A7] = pushed value
        ldy g5_reg              ; An = new SP
        lda r_a7
        sta r_a0,y
        lda r_a7+1
        sta r_a0+1,y
        lda r_a7+2
        sta r_a0+2,y
        lda r_a7+3
        sta r_a0+3,y
        ldy #0                  ; A7 += sext16(d16)
        lda g5_disp+1
        bpl @pos
        dey
@pos:   clc
        lda r_a7
        adc g5_disp
        sta r_a7
        lda r_a7+1
        adc g5_disp+1
        sta r_a7+1
        tya
        adc r_a7+2
        sta r_a7+2
        tya
        adc r_a7+3
        sta r_a7+3
        jmp dispatch_next

; --- UNLK An: SP = An; An = [SP], SP += 4.  For UNLK A7 the popped value
; wins over the increment (Musashi order).  Untested (no vector stem). -----
f4_unlk:
        lda op_lo
        and #$07
        asl
        asl
        sta g5_reg
        tay
        lda r_a0,y              ; A7 = An
        sta r_a7
        lda r_a0+1,y
        sta r_a7+1
        lda r_a0+2,y
        sta r_a7+2
        lda r_a0+3,y
        sta r_a7+3
        jsr pop_long            ; m_val = [A7], A7 += 4
        ldy g5_reg
        lda m_val               ; An = popped
        sta r_a0,y
        lda m_val+1
        sta r_a0+1,y
        lda m_val+2
        sta r_a0+2,y
        lda m_val+3
        sta r_a0+3,y
        jmp dispatch_next

; ===========================================================================
; fam_bcc — BRA (cond 0) / BSR (cond 1) / Bcc.  8-bit displacement in the
; opcode's low byte; $00 selects a 16-bit extension word.  Displacement is
; relative to the address of the extension area = PC right after the opcode
; word — which is exactly r_pc at handler entry.
; Taken branch ~120 cycles + fetch; not-taken ~60.
; ===========================================================================
fam_bcc:
        lda op_hi
        and #$0F
        sta g5_cond
        lda r_pc                ; base = extension-area address
        sta g5_base
        lda r_pc+1
        sta g5_base+1
        lda r_pc+2
        sta g5_base+2
        lda r_pc+3
        sta g5_base+3
        lda op_lo
        beq @w16
        sta g5_disp             ; sext8(d8)
        ldx #0
        cmp #$80
        bcc @p8
        ldx #$FF
@p8:    stx g5_disp+1
        jmp @eval
@w16:   jsr fetch_word          ; PC steps past the extension word here
        lda op_lo
        sta g5_disp
        lda op_hi
        sta g5_disp+1
@eval:  ldx g5_cond
        cpx #$01
        beq @bsr
        jsr cond_eval           ; cond 0 = BRA evaluates true
        bcs br_take
        jmp dispatch_next       ; not taken: PC already correct
@bsr:   lda r_pc                ; return addr = past the full instruction
        sta m_val
        lda r_pc+1
        sta m_val+1
        lda r_pc+2
        sta m_val+2
        lda r_pc+3
        sta m_val+3
        jsr push_long
        ; fall through: BSR always branches
br_take:                        ; r_pc = (g5_base + sext16(g5_disp)) & 24bit
        ldy #0
        lda g5_disp+1
        bpl @pos
        dey
@pos:   clc
        lda g5_base
        adc g5_disp
        sta r_pc
        lda g5_base+1
        adc g5_disp+1
        sta r_pc+1
        tya
        adc g5_base+2
        sta r_pc+2
        lda #0                  ; PC bits 24-31 always 0 (contract)
        sta r_pc+3
        jmp dispatch_next

; ===========================================================================
; op_dbcc — DBcc Dn,d16 (cross-op, entered from fam_5/G2 with the original
; opcode in op_lo/op_hi).  Condition TRUE: fall through.  FALSE: Dn.W -= 1
; (upper word untouched); branch by d16 unless the counter hit $FFFF.
; No flags changed.
; ===========================================================================
op_dbcc:
        lda op_hi
        and #$0F
        sta g5_cond
        lda op_lo
        and #$07
        asl
        asl
        sta g5_reg
        lda r_pc                ; base = displacement-word address
        sta g5_base
        lda r_pc+1
        sta g5_base+1
        lda r_pc+2
        sta g5_base+2
        lda r_pc+3
        sta g5_base+3
        jsr fetch_word          ; d16; PC now past it (fall-through state)
        lda op_lo
        sta g5_disp
        lda op_hi
        sta g5_disp+1
        ldx g5_cond
        jsr cond_eval           ; cond 1 = DBF evaluates false (loop form)
        bcs @done
        ldy g5_reg              ; Dn.W -= 1
        sec
        lda r_d0,y
        sbc #1
        sta r_d0,y
        lda r_d0+1,y
        sbc #0
        sta r_d0+1,y
        and r_d0,y              ; both bytes $FF <=> counter expired
        cmp #$FF
        beq @done
        jmp br_take
@done:  jmp dispatch_next

; ===========================================================================
; op_exg — EXG Rx,Ry (cross-op, entered from fam_c/G3).  Opmodes: 01000
; Dx<->Dy, 01001 Ax<->Ay, 10001 Dx<->Ay.  Full 32-bit swap, no flags.
; ~150 cycles.
; ===========================================================================
op_exg:
        lda op_hi               ; Rx = opcode bits 11-9
        lsr
        and #$07
        asl
        asl
        sta g5_reg              ; Rx*4
        lda op_lo               ; Ry = bits 2-0
        and #$07
        asl
        asl
        sta g5_tmp              ; Ry*4
        lda op_lo
        and #$F8                ; opmode (bits 7-3)
        cmp #$40                ; 01000: Dx,Dy
        beq @go
        cmp #$48                ; 01001: Ax,Ay
        beq @aa
        cmp #$88                ; 10001: Dx,Ay
        beq @da
        jmp op_illegal
@aa:    lda g5_reg
        clc
        adc #$20                ; Rx into the A-register file
        sta g5_reg
@da:    lda g5_tmp
        clc
        adc #$20                ; Ry into the A-register file
        sta g5_tmp
@go:    ldx g5_reg
        ldy g5_tmp
        lda #4
        sta g5_tmp2
@lp:    lda r_d0,x
        pha
        lda r_d0,y
        sta r_d0,x
        pla
        sta r_d0,y
        inx
        iny
        dec g5_tmp2
        bne @lp
        jmp dispatch_next

; ===========================================================================
; cond_eval — evaluate 68k condition code X (0-15) against the CCR.
;   Out: C = 1 condition true, C = 0 false.  Clobbers A, Y, g5_tmp/g5_tmp2;
;   preserves X.  ~45 cycles worst case.
; Pairs: the even condition is computed, the odd one is its negation
; (opcode bit 0): 0 T/F, 2 HI/LS, 4 CC/CS, 6 NE/EQ, 8 VC/VS, A PL/MI,
; C GE/LT, E GT/LE.  CCR bits: X=$10 N=$08 Z=$04 V=$02 C=$01.
; ===========================================================================
cond_eval:
        txa
        and #$01
        sta g5_tmp              ; negate flag
        lda r_sr
        and #$0F
        cpx #$02
        bcc @pT
        cpx #$04
        bcc @pHI
        cpx #$06
        bcc @pCC
        cpx #$08
        bcc @pNE
        cpx #$0A
        bcc @pVC
        cpx #$0C
        bcc @pPL
        cpx #$0E
        bcc @pGE
        tay                     ; GT = !Z & (N==V)
        and #$04
        bne @false
        tya
@pGE:   tay                     ; GE = (N == V)
        and #$08
        lsr
        lsr                     ; N -> bit 1
        sta g5_tmp2
        tya
        and #$02                ; V already in bit 1
        eor g5_tmp2
        beq @true
        bne @false
@pT:    lda #1                  ; T (F via negate)
        bne @fin
@pHI:   and #$05                ; HI = !C & !Z
        beq @true
        bne @false
@pCC:   and #$01                ; CC = !C
        beq @true
        bne @false
@pNE:   and #$04                ; NE = !Z
        beq @true
        bne @false
@pVC:   and #$02                ; VC = !V
        beq @true
        bne @false
@pPL:   and #$08                ; PL = !N
        beq @true
@false: lda #0
        beq @fin
@true:  lda #1
@fin:   eor g5_tmp
        lsr                     ; result -> C
        rts

; ===========================================================================
; Guest stack helpers (A7 = active SP).
; push_long: A7 -= 4, guest[A7] = m_val (caller loads m_val first).
; pop_long/pop_word: m_val = guest[A7], A7 += 4/2.
; Clobber A, m_addr, t_tmp (DMA bounce); preserve X/Y, t_ea, t_op*.
; ~160 cycles each incl. the REU DMA.
; ===========================================================================
push_long:
        sec
        lda r_a7
        sbc #4
        sta r_a7
        sta m_addr
        lda r_a7+1
        sbc #0
        sta r_a7+1
        sta m_addr+1
        lda r_a7+2
        sbc #0
        sta r_a7+2
        sta m_addr+2
        lda r_a7+3
        sbc #0
        sta r_a7+3
        sta m_addr+3
        jmp mem_write32

pop_long:
        lda #4
        bne pop_common
pop_word:
        lda #2
pop_common:
        sta g5_tmp
        lda r_a7
        sta m_addr
        lda r_a7+1
        sta m_addr+1
        lda r_a7+2
        sta m_addr+2
        lda r_a7+3
        sta m_addr+3
        clc                     ; A7 += amount
        lda r_a7
        adc g5_tmp
        sta r_a7
        lda r_a7+1
        adc #0
        sta r_a7+1
        lda r_a7+2
        adc #0
        sta r_a7+2
        lda r_a7+3
        adc #0
        sta r_a7+3
        lda g5_tmp
        cmp #4
        beq @l
        jmp mem_read16
@l:     jmp mem_read32

; ===========================================================================
.segment "RODATA"
f4_evtab_lo:                    ; even op_hi $40-$4E -> leaf decoder
        .byte <f4_negx, <f4_clr, <f4_neg, <f4_not
        .byte <f4_e48,  <f4_tst, <f4_e4c, <f4_e4e
f4_evtab_hi:
        .byte >f4_negx, >f4_clr, >f4_neg, >f4_not
        .byte >f4_e48,  >f4_tst, >f4_e4c, >f4_e4e
