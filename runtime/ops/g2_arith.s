; ===========================================================================
; runtime/ops/g2_arith.s — G2: binary arithmetic families.
;   fam_5   ($50-$5F): ADDQ/SUBQ; routes Scc -> op_scc, DBcc -> op_dbcc
;   fam_sub ($90-$9F): SUB/SUBA/SUBX
;   fam_b   ($B0-$BF): CMP/CMPA/CMPM; routes EOR -> op_eor
;   fam_add ($D0-$DF): ADD/ADDA/ADDX
; Owner: G2.  Contract: docs/CONTRACTS.md; conventions: docs/WAVE2_BRIEF.md.
;
; Scratch discipline (clobber rule): all opcode fields are extracted into
; the g2_* BSS bytes BEFORE the first core call; core routines never touch
; them.  The source operand rides across the destination's ea_calc/ea_load
; in t_op2 (core preserves t_op2).
; ===========================================================================

.include "../macros.inc"

.export fam_5, fam_sub, fam_b, fam_add

.import ea_calc, ea_load, ea_store
.import alu_add, alu_sub, alu_cmp, alu_addx, alu_subx
.import dispatch_next
.import op_scc, op_dbcc, op_eor         ; cross-ops (G3/G5), stubbed until they land

.segment "BSS"
g2_dir:         .res 1                  ; 0 = add-direction, 1 = sub-direction
g2_reg:         .res 1                  ; opcode bits 11-9 (Dn/An number)
g2_ea:          .res 1                  ; opcode bits 5-0 (mode|reg)
g2_size:        .res 1                  ; 0/1/2 = B/W/L (opmode bits 1-0)
g2_opmode:      .res 1                  ; opcode bits 8-6

.segment "CODE"

; ===========================================================================
; Helpers
; ===========================================================================

; Copy t_op1 -> t_op2, all 4 bytes (the ALU only reads the sized ones, so
; stale upper bytes for B/W are harmless).  ~26 cycles.
src_to_op2:
        lda t_op1
        sta t_op2
        lda t_op1+1
        sta t_op2+1
        lda t_op1+2
        sta t_op2+2
        lda t_op1+3
        sta t_op2+3
        rts

; Sign-extend the word in t_op1 to 32 bits (ea_load left bytes 2-3 stale).
sext_w_op1:
        ldy #0
        lda t_op1+1
        bpl sxw1
        dey
sxw1:   sty t_op1+2
        sty t_op1+3
        rts

; An += / -= t_op2, full 32 bits, CCR untouched (ADDA/SUBA/ADDQ-An/SUBQ-An
; semantics).  Y = ZP offset of the register from r_d0 ($20 + reg*4).
; lda/sta r_d0,y assemble as abs,y (no zp,y for lda/sta).  ~60 cycles.
areg_add:
        clc
        lda r_d0,y
        adc t_op2
        sta r_d0,y
        lda r_d0+1,y
        adc t_op2+1
        sta r_d0+1,y
        lda r_d0+2,y
        adc t_op2+2
        sta r_d0+2,y
        lda r_d0+3,y
        adc t_op2+3
        sta r_d0+3,y
        rts

areg_sub:
        sec
        lda r_d0,y
        sbc t_op2
        sta r_d0,y
        lda r_d0+1,y
        sbc t_op2+1
        sta r_d0+1,y
        lda r_d0+2,y
        sbc t_op2+2
        sta r_d0+2,y
        lda r_d0+3,y
        sbc t_op2+3
        sta r_d0+3,y
        rts

; Direction trampolines: A = size on entry, g2_dir picks the primitive.
; jsr as_alu / as_alux, the primitive's rts returns to our caller.
as_alu:
        ldy g2_dir
        bne as_alu1
        jmp alu_add
as_alu1:
        jmp alu_sub

as_alux:
        ldy g2_dir
        bne as_alux1
        jmp alu_addx
as_alux1:
        jmp alu_subx

; ===========================================================================
; fam_5 ($50-$5F) — 0101 ddd D ss eeeeee
;   ss = 00/01/10: ADDQ (D=0) / SUBQ (D=1) #d,<ea>, d = 1-8 (field 0 = 8).
;     To An: ALWAYS full 32-bit regardless of size, CCR untouched.
;     Else: normal sized RMW, full CCR (incl. X) via alu_add/alu_sub.
;   ss = 11: Scc <ea>, except mode 001 = DBcc Dn,disp — both cross-ops.
; Decode ~80 cycles; Dn form ~330 total/instr, memory .l form ~900.
; ===========================================================================
fam_5:
        lda op_lo
        and #$C0
        cmp #$C0
        beq f5_sccdb
        ; --- ADDQ/SUBQ: extract every field before any core call ---
        lda op_hi
        lsr
        and #7
        bne f5_d
        lda #8                  ; data field 0 encodes 8
f5_d:   sta t_op2               ; t_op2 = zero-extended immediate 1-8
        lda #0
        sta t_op2+1
        sta t_op2+2
        sta t_op2+3
        lda op_hi
        and #1
        sta g2_dir              ; opcode bit 8: 0 = ADDQ, 1 = SUBQ
        lda op_lo
        lsr
        lsr
        lsr
        lsr
        lsr
        lsr
        sta g2_size
        lda op_lo
        and #$3F
        sta g2_ea
        and #$38
        cmp #$08
        beq f5_an               ; An destination: special semantics
        ; --- general alterable EA: RMW, full CCR ---
        lda g2_size
        ldx g2_ea
        jsr ea_calc
        lda g2_size
        jsr ea_load             ; t_op1 = dst
        lda g2_size
        jsr as_alu
        lda g2_size
        jsr ea_store
        jmp dispatch_next
        ; --- An destination: 32-bit always, no flags ---
f5_an:  lda g2_ea
        and #7
        asl
        asl
        ora #$20                ; ZP offset of An from r_d0
        tay
        lda g2_dir
        bne f5_ans
        jsr areg_add
        jmp dispatch_next
f5_ans: jsr areg_sub
        jmp dispatch_next
        ; --- size field 11: DBcc when mode = 001, else Scc ---
f5_sccdb:
        lda op_lo
        and #$38
        cmp #$08
        beq f5_db
        jmp op_scc              ; opcode word untouched (cross-op convention)
f5_db:  jmp op_dbcc

; ===========================================================================
; fam_sub ($90-$9F) / fam_add ($D0-$DF) — 1x01 rrr ooo eeeeee
;   opmode 000-010: Dn = Dn op <ea>            (any readable EA incl. #imm)
;   opmode 011/111: ADDA/SUBA <ea>,An          (.W sign-extends source to
;                                               32; full 32-bit; NO flags)
;   opmode 100-110: <ea> = <ea> op Dn          (memory-alterable), EXCEPT
;       mode 000 -> ADDX/SUBX Dy,Dx
;       mode 001 -> ADDX/SUBX -(Ay),-(Ax)      (source decremented + read
;                                               first, then destination)
;   X-flag carry-in and sticky Z live in core alu_addx/alu_subx.
; Decode ~90 cycles; reg/reg .l ~450 total/instr, -(Ay),-(Ax) .l ~1150
; (two EA cycles + two REU reads + write) — worst case, still < 1500.
; ===========================================================================
fam_sub:
        lda #1
        sta g2_dir
        bne as_common           ; always (A = 1)
fam_add:
        lda #0
        sta g2_dir
as_common:
        ; extract every field before any core call
        lda op_hi
        lsr
        and #7
        sta g2_reg
        lda op_lo
        and #$3F
        sta g2_ea
        lda op_lo
        lsr
        lsr
        lsr
        lsr
        lsr
        lsr
        sta g2_size             ; opmode bits 1-0 = size for non-A forms
        lda op_hi
        and #1
        asl
        asl
        ora g2_size
        sta g2_opmode
        cmp #3
        bne as_n3
        jmp as_adda_w
as_n3:  cmp #7
        bne as_n7
        jmp as_adda_l
as_n7:  cmp #4
        bcs as_dn2ea
        ; --- opmode 0-2: Dn = Dn op <ea> ---
        lda g2_size
        ldx g2_ea
        jsr ea_calc
        lda g2_size
        jsr ea_load             ; t_op1 = src
        jsr src_to_op2
        lda g2_size
        ldx g2_reg
        jsr ea_calc             ; dst Dn via EA mode 000
        lda g2_size
        jsr ea_load             ; t_op1 = Dn
        lda g2_size
        jsr as_alu
        lda g2_size
        jsr ea_store            ; sized bytes only: upper Dn bytes kept
        jmp dispatch_next
        ; --- opmode 4-6: <ea> = <ea> op Dn, or ADDX/SUBX ---
as_dn2ea:
        lda g2_ea
        and #$38
        beq as_xreg             ; mode 000 -> ADDX/SUBX Dy,Dx
        cmp #$08
        beq as_xmem             ; mode 001 -> ADDX/SUBX -(Ay),-(Ax)
        ldx g2_reg
        getreg2                 ; t_op2 = Dn (source)
        lda g2_size
        ldx g2_ea
        jsr ea_calc
        lda g2_size
        jsr ea_load             ; t_op1 = dst
        lda g2_size
        jsr as_alu
        lda g2_size
        jsr ea_store
        jmp dispatch_next
        ; --- ADDX/SUBX Dy,Dx ---
as_xreg:
        lda g2_ea
        and #7
        tax
        getreg2                 ; t_op2 = Dy (source)
        lda g2_size
        ldx g2_reg
        jsr ea_calc             ; dst Dx via EA mode 000
        lda g2_size
        jsr ea_load
        lda g2_size
        jsr as_alux
        lda g2_size
        jsr ea_store
        jmp dispatch_next
        ; --- ADDX/SUBX -(Ay),-(Ax): Ay side first (68k order) ---
as_xmem:
        lda g2_ea
        and #7
        ora #$20                ; mode 100 | y  =>  -(Ay)
        tax
        lda g2_size
        jsr ea_calc
        lda g2_size
        jsr ea_load             ; t_op1 = src
        jsr src_to_op2
        lda g2_reg
        ora #$20                ; mode 100 | x  =>  -(Ax)
        tax
        lda g2_size
        jsr ea_calc
        lda g2_size
        jsr ea_load             ; t_op1 = dst
        lda g2_size
        jsr as_alux
        lda g2_size
        jsr ea_store
        jmp dispatch_next
        ; --- ADDA/SUBA <ea>,An ---
as_adda_w:
        lda #1
        ldx g2_ea
        jsr ea_calc
        lda #1
        jsr ea_load
        jsr sext_w_op1          ; .W source sign-extends to 32
        jmp as_adda
as_adda_l:
        lda #2
        ldx g2_ea
        jsr ea_calc
        lda #2
        jsr ea_load
as_adda:
        jsr src_to_op2
        lda g2_reg
        asl
        asl
        ora #$20                ; ZP offset of An from r_d0
        tay
        lda g2_dir
        bne as_addas
        jsr areg_add            ; no flags, per ADDA/SUBA semantics
        jmp dispatch_next
as_addas:
        jsr areg_sub
        jmp dispatch_next

; ===========================================================================
; fam_b ($B0-$BF) — 1011 rrr ooo eeeeee
;   opmode 000-010: CMP <ea>,Dn      (flags = Dn - <ea>; X unchanged — core)
;   opmode 011/111: CMPA.W/.L <ea>,An (.W source sign-extended, full 32 cmp)
;   opmode 100-110: mode 001 -> CMPM (Ay)+,(Ax)+ (source read first),
;                   else EOR Dn,<ea> -> op_eor (G3 cross-op)
; CMP Dn form ~330 cycles/instr, CMPM .l ~1000.
; ===========================================================================
fam_b:
        lda op_hi
        lsr
        and #7
        sta g2_reg
        lda op_lo
        and #$3F
        sta g2_ea
        lda op_lo
        lsr
        lsr
        lsr
        lsr
        lsr
        lsr
        sta g2_size
        lda op_hi
        and #1
        bne fb_hi
        ; --- opmode 0-3 ---
        lda g2_size
        cmp #3
        beq fb_cmpa_w
        ; CMP <ea>,Dn
        lda g2_size
        ldx g2_ea
        jsr ea_calc
        lda g2_size
        jsr ea_load             ; t_op1 = src
        jsr src_to_op2
        ldx g2_reg
        getreg                  ; t_op1 = Dn (dst)
        lda g2_size
        jsr alu_cmp             ; flags only; core preserves X flag
        jmp dispatch_next
        ; --- opmode 4-7 ---
fb_hi:  lda g2_size
        cmp #3
        beq fb_cmpa_l
        lda g2_ea
        and #$38
        cmp #$08
        beq fb_cmpm
        jmp op_eor              ; opcode word untouched (cross-op convention)
        ; --- CMPA <ea>,An ---
fb_cmpa_w:
        lda #1
        ldx g2_ea
        jsr ea_calc
        lda #1
        jsr ea_load
        jsr sext_w_op1
        jmp fb_cmpa
fb_cmpa_l:
        lda #2
        ldx g2_ea
        jsr ea_calc
        lda #2
        jsr ea_load
fb_cmpa:
        jsr src_to_op2
        lda g2_reg
        ora #8
        tax                     ; register index 8-15 = An
        getreg                  ; t_op1 = An, full 32
        lda #2
        jsr alu_cmp             ; always a long compare
        jmp dispatch_next
        ; --- CMPM (Ay)+,(Ax)+: source side first (68k order) ---
fb_cmpm:
        lda g2_ea
        and #7
        ora #$18                ; mode 011 | y  =>  (Ay)+
        tax
        lda g2_size
        jsr ea_calc
        lda g2_size
        jsr ea_load             ; t_op1 = src
        jsr src_to_op2
        lda g2_reg
        ora #$18                ; mode 011 | x  =>  (Ax)+
        tax
        lda g2_size
        jsr ea_calc
        lda g2_size
        jsr ea_load             ; t_op1 = dst
        lda g2_size
        jsr alu_cmp
        jmp dispatch_next
