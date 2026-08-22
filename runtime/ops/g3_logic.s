; ===========================================================================
; runtime/ops/g3_logic.s — G3: logic and bit-op families.
; Owner: G3.  Contract: docs/CONTRACTS.md; conventions: docs/WAVE2_BRIEF.md.
;
; Families:
;   fam_0 ($00-$0F): ORI/ANDI/SUBI/ADDI/EORI/CMPI #imm,<ea> (all sizes),
;                    ORI/ANDI/EORI #imm,CCR (byte) / #imm,SR (privileged ->
;                    HALT_ILLEGAL), BTST/BCHG/BCLR/BSET static ($08xx, bit
;                    number in extension word) and dynamic (bit 8 set, bit
;                    number in Dn), MOVEP (dynamic pattern, EA mode 001) ->
;                    op_illegal stub.
;   fam_8 ($80-$8F): OR <ea>,Dn / OR Dn,<ea>; DIVU/DIVS -> op_divu/op_divs
;                    (G6); SBCD -> op_sbcd (G6).
;   fam_c ($C0-$CF): AND both directions; MULU/MULS -> op_mulu/op_muls (G6);
;                    ABCD -> op_abcd (G6); EXG -> op_exg (G5).
; Cross-op exports (jmp-entered, original opcode word in op_lo/op_hi):
;   op_scc: Scc <ea> (all 16 conditions, byte EA, no flags changed).
;   op_eor: EOR Dn,<ea> (jmp-entered from fam_b/G2).
;
; Field-stash discipline (CLOBBER RULE): fetch_word / ea_calc / ea_load
; clobber op_lo/op_hi, t_tmp, m_*.  All opcode fields are extracted into the
; g3_* BSS bytes below BEFORE any of those calls.
; ===========================================================================

.include "../macros.inc"

.export fam_0, fam_8, fam_c
.export op_scc, op_eor

.import dispatch_next, fetch_word
.import ea_calc, ea_load, ea_store
.import alu_add, alu_sub, alu_cmp, alu_and, alu_or, alu_eor
.import op_illegal, emu_halt
.import op_divu, op_divs, op_sbcd          ; G6 (stubs until it lands)
.import op_mulu, op_muls, op_abcd          ; G6
.import op_exg                             ; G5

; --- Private field stash (survives core calls and the ALU) -----------------
.segment "BSS"
g3_op:    .res 1        ; fam_0 immediate-op index = op_hi>>1
g3_size:  .res 1        ; size 0/1/2, also opmode low 2 bits in fam_8/fam_c
g3_ea:    .res 1        ; 6-bit mode|reg EA field
g3_reg:   .res 1        ; Dn field from opcode bits 11-9
g3_bop:   .res 1        ; bit-op selector: 0=BTST 1=BCHG 2=BCLR 3=BSET
g3_bit:   .res 1        ; bit number (low byte; masked mod 32/8 at use)
g3_mask:  .res 1        ; 1 << (bit number mod 8)
g3_t:     .res 1        ; local scratch (w_zacc/w_ptmp are core-internal)

.segment "CODE"

; --- ALU trampoline --------------------------------------------------------
; Family entries patch the jmp operand (self-modifying, PRG is in RAM), then
; the shared bodies `jsr alu_call` with size in A.  Avoids the jmp-indirect
; page-boundary erratum and keeps one shared OR/AND/EOR/imm body.
alu_call:
        jmp $FFFF               ; operand = alu_call+1/+2, patched per entry

.macro SETALU tgt
        lda #<tgt
        sta alu_call+1
        lda #>tgt
        sta alu_call+2
.endmacro

; ===========================================================================
; fam_0 — $00-$0F
; Decode on op_hi bit0 (opcode bit 8) first: 1 = dynamic bit op / MOVEP,
; 0 = immediate ops ($08 = static bit ops, $0E = MOVES/68010 -> illegal).
; ===========================================================================
fam_0:
        lda op_hi
        lsr a                   ; C = opcode bit 8, A = op index 0-7
        bcs @dyn
        sta g3_op               ; 0=ORI 1=ANDI 2=SUBI 3=ADDI 4=bitop
        cmp #4                  ;   5=EORI 6=CMPI 7=$0E illegal
        beq @stat
        bne @imm                ; always
@dyn:   jmp f0_dynamic
@stat:  jmp f0_static
@imm:   cmp #7
        beq f0_ill
        lda op_lo
        and #$3F
        sta g3_ea
        lda op_lo               ; size = op_lo bits 7-6
        asl a
        rol a
        rol a
        and #$03
        sta g3_size
        cmp #3
        beq f0_ill              ; size %11: no immediate op
        ; EA field %111100 (#imm as destination) selects the CCR/SR forms
        ; of ORI/ANDI/EORI; SUBI/ADDI/CMPI have no such form (and never
        ; appear with it in the vectors) -> fall through to the normal path.
        lda g3_ea
        cmp #$3C
        bne f0_imm_go
        lda g3_op
        cmp #2
        beq f0_imm_go           ; SUBI
        cmp #3
        beq f0_imm_go           ; ADDI
        cmp #6
        beq f0_imm_go           ; CMPI
        lda g3_size
        beq f0_ccr              ; byte -> #imm,CCR
        cmp #1
        beq f0_sr               ; word -> #imm,SR: privileged
f0_ill:
        jmp op_illegal

f0_sr:                          ; ORI/ANDI/EORI #imm,SR: privileged in user
        lda #HALT_ILLEGAL       ; mode -> halt (M2 exception hook)
        jmp emu_halt

; --- ORI/ANDI/EORI #imm,CCR ------------------------------------------------
; Immediate is a word in the stream; only its low byte applies.  Result
; masked to the 5 architectural CCR bits (upper CCR bits always read 0).
f0_ccr:
        jsr fetch_word          ; op_lo = immediate low byte
        lda g3_op               ; 0=ORI 1=ANDI else EORI
        beq @or
        cmp #1
        beq @and
        lda op_lo               ; EORI
        eor r_sr
        jmp @fin
@or:    lda op_lo
        ora r_sr
        jmp @fin
@and:   lda op_lo
        and r_sr
@fin:   and #$1F
        sta r_sr
        jmp dispatch_next

; --- ORI/ANDI/SUBI/ADDI/EORI/CMPI #imm,<ea> --------------------------------
; 68k operand order: immediate word(s) precede the destination's extension
; words, so resolve #imm (EA %111100) first — ea_calc/ea_load do the fetch
; and the byte-in-low-byte-of-word rule for us.  ~600-900 cycles, REU-DMA
; dominated (2 x ea_calc + 2 x ea_load + alu + ea_store).
f0_imm_go:
        ldx g3_op
        lda f0_alutab_lo,x
        sta alu_call+1
        lda f0_alutab_hi,x
        sta alu_call+2
        lda g3_size
        ldx #$3C                ; #imm
        jsr ea_calc
        lda g3_size
        jsr ea_load             ; t_op1 = immediate (sized bytes)
        lda t_op1               ; move to t_op2 (source slot); upper bytes
        sta t_op2               ; may be stale for B/W — ALU reads sized only
        lda t_op1+1
        sta t_op2+1
        lda t_op1+2
        sta t_op2+2
        lda t_op1+3
        sta t_op2+3
        lda g3_size
        ldx g3_ea
        jsr ea_calc
        lda g3_size
        jsr ea_load             ; t_op1 = destination operand
        lda g3_size
        jsr alu_call            ; CCR per ALU (SUBI/ADDI set X, CMPI keeps X)
        lda g3_op
        cmp #6
        beq @nost               ; CMPI: no writeback
        lda g3_size
        jsr ea_store
@nost:  jmp dispatch_next

; --- Bit ops ---------------------------------------------------------------
; Static form ($08xx): bit number = low byte of the extension word.
f0_static:
        lda op_lo
        and #$3F
        sta g3_ea
        lda op_lo
        asl a
        rol a
        rol a
        and #$03
        sta g3_bop
        jsr fetch_word
        lda op_lo               ; ext word low byte = bit number
        sta g3_bit
        jmp bitop_common

; Dynamic form (opcode bit 8 set): bit number in Dn (bits 11-9); EA mode
; 001 in this pattern is MOVEP -> contract stub (halt ILLEGAL).
f0_dynamic:                     ; A = Dn index (from the lsr at fam_0)
        asl a
        asl a
        tay
        lda r_d0,y              ; Dn low byte (mod 32/8 needs no more)
        sta g3_bit
        lda op_lo
        and #$38
        cmp #$08
        beq f0_movep
        lda op_lo
        and #$3F
        sta g3_ea
        lda op_lo
        asl a
        rol a
        rol a
        and #$03
        sta g3_bop
        jmp bitop_common
f0_movep:
        jmp op_illegal          ; MOVEP: contract stub

; Common bit-op body.  In: g3_ea, g3_bop, g3_bit.
; Dn target: long operand, bit number mod 32, done in the register file
; directly.  Memory target: byte operand, bit number mod 8, RMW via ea_*.
; Z = tested bit BEFORE the operation; N/V/C/X unchanged (set_z).
; Dn form ~120 cycles; memory form ~350-500 (EA + REU DMA).
bitop_common:
        lda g3_ea
        and #$38
        beq bit_reg
        lda g3_bit              ; memory: mask = 1 << (bit mod 8)
        and #$07
        tax
        lda bmask,x
        sta g3_mask
        lda #0
        ldx g3_ea
        jsr ea_calc
        lda #0
        jsr ea_load             ; t_op1 = byte operand
        lda t_op1
        and g3_mask
        jsr set_z
        ldx g3_bop
        beq bit_done            ; BTST: test only
        dex
        beq @chg
        dex
        beq @clr
        lda t_op1               ; BSET
        ora g3_mask
        jmp @st
@chg:   lda t_op1               ; BCHG
        eor g3_mask
        jmp @st
@clr:   lda g3_mask             ; BCLR
        eor #$FF
        and t_op1
@st:    sta t_res
        lda #0
        jsr ea_store            ; t_ea preserved across set_z -> RMW ok
bit_done:
        jmp dispatch_next

bit_reg:                        ; Dn target: bit number mod 32
        lda g3_bit
        and #$07
        tax
        lda bmask,x
        sta g3_mask
        lda g3_bit
        and #$18                ; byte index = (bit mod 32) >> 3
        lsr a
        lsr a
        lsr a
        sta g3_t
        lda g3_ea
        and #$07                ; target Dn = EA reg field (mode was 000)
        asl a
        asl a                   ; reg*4 (bits 2-4) | byte index (bits 0-1)
        ora g3_t
        tay
        lda r_d0,y
        and g3_mask
        jsr set_z               ; preserves Y
        ldx g3_bop
        beq bit_done
        dex
        beq @chg
        dex
        beq @clr
        lda r_d0,y              ; BSET
        ora g3_mask
        jmp @w
@chg:   lda r_d0,y              ; BCHG
        eor g3_mask
        jmp @w
@clr:   lda g3_mask             ; BCLR
        eor #$FF
        and r_d0,y
@w:     sta r_d0,y
        jmp dispatch_next

; Z := (A == 0), all other CCR bits (N/V/C and X) unchanged.
; Clobbers A only (and #$FB does not touch carry).
set_z:
        cmp #1                  ; C = tested bit set
        lda r_sr
        and #$FB
        bcs @nz
        ora #$04
@nz:    sta r_sr
        rts

; ===========================================================================
; fam_8 — $80-$8F: OR / DIVU / DIVS / SBCD
; opmode (bits 8-6): 0ss = OR <ea>,Dn; 011 = DIVU; 1ss = OR Dn,<ea>;
; 111 = DIVS; opmode 100 with EA mode 000/001 = SBCD (bits 8-4 = %10000).
; Cross-ops are jmp-entered with op_lo/op_hi untouched (reads only above).
; ===========================================================================
fam_8:
        SETALU alu_or
        lda op_lo
        asl a
        rol a
        rol a
        and #$03
        sta g3_size             ; opmode low 2 bits = size for OR forms
        lda op_hi
        lsr a                   ; C = opmode bit 2 (direction)
        and #$07
        sta g3_reg
        bcs @dnea
        lda g3_size
        cmp #3
        beq @divu
        jmp logic_ea_dn         ; OR <ea>,Dn
@divu:  jmp op_divu
@dnea:  lda g3_size
        cmp #3
        beq @divs
        cmp #0
        bne @doit               ; opmode 101/110: OR.w/.l Dn,<ea>
        lda op_lo
        and #$38
        cmp #$10
        bcs @doit               ; mode >= 2: OR.b Dn,<ea>
        jmp op_sbcd             ; mode 000/001: SBCD
@divs:  jmp op_divs
@doit:  jmp logic_dn_ea

; ===========================================================================
; fam_c — $C0-$CF: AND / MULU / MULS / ABCD / EXG
; opmode 011 = MULU, 111 = MULS; opmode 100 mode<2 = ABCD; opmode 101
; mode<2 = EXG Dx,Dy / Ax,Ay; opmode 110 mode 001 = EXG Dx,Ay (mode 000
; there is no valid encoding -> op_illegal).
; ===========================================================================
fam_c:
        SETALU alu_and
        lda op_lo
        asl a
        rol a
        rol a
        and #$03
        sta g3_size
        lda op_hi
        lsr a                   ; C = opmode bit 2
        and #$07
        sta g3_reg
        bcs @hi
        lda g3_size
        cmp #3
        beq @mulu
        jmp logic_ea_dn         ; AND <ea>,Dn
@mulu:  jmp op_mulu
@hi:    lda g3_size
        cmp #3
        beq @muls
        lda op_lo               ; A = EA mode 0-7
        and #$38
        lsr a
        lsr a
        lsr a
        ldy g3_size
        beq @om100
        dey
        beq @om101
        cmp #1                  ; opmode 110 (AND.l Dn,<ea>)
        beq @exg                ; mode 001: EXG Dx,Ay
        cmp #0
        beq @ill                ; %110 000: not a valid encoding
        jmp logic_dn_ea
@om100: cmp #2                  ; opmode 100 (AND.b Dn,<ea>)
        bcs @go
        jmp op_abcd             ; mode 000/001: ABCD
@om101: cmp #2                  ; opmode 101 (AND.w Dn,<ea>)
        bcs @go
@exg:   jmp op_exg              ; mode 000/001: EXG Dx,Dy / Ax,Ay
@muls:  jmp op_muls
@ill:   jmp op_illegal
@go:    jmp logic_dn_ea

; ===========================================================================
; Shared OR/AND/EOR bodies.  In: alu_call patched, g3_size, g3_reg,
; op_lo still intact (EA field).  OR/AND are commutative, so operand slot
; order does not matter for result or flags (N/Z from result, V=C=0,
; X unchanged — all inside the ALU).
; ===========================================================================

; <ea>,Dn: t_op1 = <ea> operand, t_op2 = Dn, sized result back into Dn
; (sized bytes only; upper register bytes preserved).  ~400-600 cycles.
logic_ea_dn:
        lda op_lo
        and #$3F
        tax
        lda g3_size
        jsr ea_calc
        lda g3_size
        jsr ea_load             ; t_op1 = source operand
        ldx g3_reg
        getreg2                 ; t_op2 = Dn
        lda g3_size
        jsr alu_call
        lda g3_reg              ; write sized t_res into Dn
        asl a
        asl a
        tay
        ldx g3_size
        lda t_res
        sta r_d0,y
        dex
        bmi @d
        lda t_res+1
        sta r_d0+1,y
        dex
        bmi @d
        lda t_res+2
        sta r_d0+2,y
        lda t_res+3
        sta r_d0+3,y
@d:     jmp dispatch_next

; Dn,<ea>: classic RMW — ea_calc / ea_load / alu / ea_store.  ~500-700 cyc.
logic_dn_ea:
        lda op_lo
        and #$3F
        tax
        lda g3_size
        jsr ea_calc
        lda g3_size
        jsr ea_load             ; t_op1 = destination operand
        ldx g3_reg
        getreg2                 ; t_op2 = Dn
        lda g3_size
        jsr alu_call
        lda g3_size
        jsr ea_store
        jmp dispatch_next

; ===========================================================================
; op_eor — EOR Dn,<ea> (cross-op, jmp-entered from fam_b/G2 with the
; original opcode word in op_lo/op_hi).  Opmode 1ss, ss = size.
; ===========================================================================
op_eor:
        SETALU alu_eor
        lda op_lo
        asl a
        rol a
        rol a
        and #$03
        sta g3_size
        lda op_hi
        lsr a
        and #$07
        sta g3_reg
        jmp logic_dn_ea

; ===========================================================================
; op_scc — Scc <ea> (cross-op, jmp-entered from fam_5/G2).
; cond = opcode bits 11-8; writes $FF (true) / $00 (false) as a byte to the
; EA; no flags changed.  Dn destination writes the low byte only (ea_store
; size B); -(A7)/(A7)+ byte quirk handled inside ea_calc.  ~300-450 cycles.
; ===========================================================================
op_scc:
        lda op_lo
        and #$3F
        sta g3_ea
        lda op_hi
        and #$0F
        jsr cond_eval           ; A = $FF/$00
        sta t_res               ; ea_calc does not touch t_res
        lda #0
        ldx g3_ea
        jsr ea_calc
        lda #0
        jsr ea_store
        jmp dispatch_next

; ---------------------------------------------------------------------------
; cond_eval — 68000 condition evaluation.  In: A = cond 0-15.
; Out: A = $FF (true) / $00 (false).  Clobbers A/X/Y, g3_t.
;
; Conditions come in even/odd pairs where odd = NOT even (T/F, HI/LS, CC/CS,
; NE/EQ, VC/VS, PL/MI, GE/LT, GT/LE).  For each pair compute
;   expr: 0:0  1:C|Z  2:C  3:Z  4:V  5:N  6:N^V  7:(N^V)|Z
; then result = expr for odd cond, !expr for even cond — exactly the PRM
; truth tables (GE/LT/GT/LE via N xor V).  CCR: X=$10 N=$08 Z=$04 V=$02 C=$01.
; ---------------------------------------------------------------------------
cond_eval:
        tay                     ; Y = cond (parity needed at the end)
        lsr a
        tax                     ; X = pair 0-7
        beq @e0
        cpx #6
        bcs @gele
        lda r_sr                ; pairs 1-5: simple mask test
        and cmask-1,x
        jmp @fin
@e0:    lda #0                  ; pair 0 (T/F): expr = 0
        beq @fin                ; always
@gele:  lda r_sr                ; N^V: shift N (bit3) down onto V (bit1)
        lsr a
        lsr a
        eor r_sr
        and #$02                ; nonzero iff N != V
        cpx #7
        bne @fin
        sta g3_t                ; pair 7 (GT/LE): expr |= Z
        lda r_sr
        and #$04
        ora g3_t
@fin:   cmp #1                  ; C = expr true (A != 0)
        bcs @exprt
        tya                     ; expr false
        lsr a                   ; C = cond odd
        bcs @false              ; odd: result = expr = false
@true:  lda #$FF
        rts
@exprt: tya                     ; expr true
        lsr a
        bcs @true               ; odd: result = expr = true
@false: lda #$00
        rts

; ===========================================================================
.segment "RODATA"
bmask:                          ; 1 << n
        .byte $01, $02, $04, $08, $10, $20, $40, $80
cmask:                          ; cond_eval pair masks, index 1-5 (C|Z, C,
        .byte $05, $01, $04, $02, $08                 ; Z, V, N)
f0_alutab_lo:                   ; fam_0 immediate-op ALU, index = op_hi>>1
        .byte <alu_or, <alu_and, <alu_sub, <alu_add
        .byte <op_illegal, <alu_eor, <alu_cmp, <op_illegal
f0_alutab_hi:                   ; (indices 4/7 routed away before use)
        .byte >alu_or, >alu_and, >alu_sub, >alu_add
        .byte >op_illegal, >alu_eor, >alu_cmp, >op_illegal
