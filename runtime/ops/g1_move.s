; ===========================================================================
; runtime/ops/g1_move.s — MOVE family (G1)
;
;   fam_move  ($10-$3F): MOVE.B/.W/.L  all EA -> all EA, MOVEA.W/.L
;   fam_moveq ($70-$7F): MOVEQ #imm8,Dn
;
; Encoding (MOVE, 68k bits):
;   15-14 = 00, 13-12 = size (01=B, 11=W, 10=L — non-standard),
;   11-9 = dest reg, 8-6 = dest mode (bit-swapped vs. source),
;   5-0  = source mode|reg.
; Illegal encodings routed to op_illegal: dest mode 111 with reg > 001,
; byte-size MOVEA (dest mode 001), byte-size An source (mode 001).
;
; 68000 operand order honoured: source EA is fully resolved and READ
; (extension words + (An)+/-(An) side effects) BEFORE the destination EA
; is resolved, so mem-to-mem cases sharing an An see the updated register
; on the destination side, and MOVEA/MOVE overwrite of a source-side
; postinc/predec An wins (write happens last).
;
; ZP usage between core calls (clobber rule: op_lo/op_hi/t_tmp/m_* die at
; the first fetch/ea/mem call):
;   t_op2+1 = dest reg (0-7)         t_op2+2 = operand size (0/1/2 = B/W/L)
;   t_op2+3 = dest mode|reg field    t_tmp+0/1 = move_nz scratch (no core
;   calls while live)
; ===========================================================================

.include "../macros.inc"

.export fam_move, fam_moveq
.import ea_calc, ea_load, ea_store
.import dispatch_next, op_illegal

.segment "CODE"

; ---------------------------------------------------------------------------
; fam_move — MOVE.B/.W/.L, MOVEA.W/.L
; Cycle cost (excl. core dispatch/fetch overhead): decode ~70; Dn->Dn
; ~+250; mem->mem worst (both sides d8(An,Xn)) ~+1100 (2 ext-word fetches
; ~150 each, mem read+write ~110 each) — well under the 1500 budget.
; ---------------------------------------------------------------------------
fam_move:
        ; ---- extract every opcode field first ----
        lda op_hi               ; size bits 13-12 -> op_hi bits 5-4
        and #$30
        lsr
        lsr
        lsr
        lsr
        tax                     ; 1=B, 3=W, 2=L (1..3 only: op_hi >= $10)
        lda move_szmap,x
        sta t_op2+2             ; operand size 0/1/2

        lda op_hi
        lsr                     ; C = dest-mode bit 2, A = op_hi >> 1
        and #$07                ; A = dest reg (and/sta keep C)
        sta t_op2+1
        lda #$00
        rol                     ; A = dest-mode bit 2 (C now clear)
        asl
        asl                     ; ... shifted into place (bit 2)
        sta t_op2+3
        lda op_lo               ; dest-mode bits 1-0 = op_lo bits 7-6
        asl
        rol
        rol
        and #$03
        ora t_op2+3             ; A = dest mode 0-7

        cmp #$01
        beq movea               ; dest mode 001 -> MOVEA (no flags)

        cmp #$07                ; dest mode 111: only abs.W/abs.L writable
        bne @dok
        ldy t_op2+1
        cpy #$02
        bcs move_ill            ; reg 2-7 -> illegal encoding
@dok:   asl
        asl
        asl
        ora t_op2+1
        sta t_op2+3             ; dest mode|reg 6-bit field

        lda t_op2+2             ; byte-size An source is illegal
        bne @src
        lda op_lo
        and #$38
        cmp #$08
        beq move_ill

@src:   ; ---- source: resolve + load (ext words fetched here, first) ----
        lda op_lo
        and #$3F
        tax
        lda t_op2+2
        jsr ea_calc             ; (An)+/-(An) side effects applied now
        lda t_op2+2
        jsr ea_load             ; t_op1 = source value (sized bytes)

        ; ---- destination EA (sees post-source An values) ----
        lda t_op2+2
        ldx t_op2+3
        jsr ea_calc

        lda t_op1               ; t_res = value (sized bytes matter;
        sta t_res               ;  ea_store writes sized bytes only)
        lda t_op1+1
        sta t_res+1
        lda t_op1+2
        sta t_res+2
        lda t_op1+3
        sta t_res+3

        lda t_op2+2             ; CCR: N/Z from value, V=C=0, X unchanged
        jsr move_nz

        lda t_op2+2
        jsr ea_store
        jmp dispatch_next

move_ill:
        jmp op_illegal

; ---------------------------------------------------------------------------
; MOVEA.W/.L — dest mode 001. Word source sign-extends to 32 bits; the full
; long lands in An directly (no ea_calc needed: An direct has no extension
; words or side effects). CCR untouched.
; ---------------------------------------------------------------------------
movea:
        lda t_op2+2
        beq move_ill            ; MOVEA.B does not exist

        lda op_lo               ; source: resolve + load
        and #$3F
        tax
        lda t_op2+2
        jsr ea_calc
        lda t_op2+2
        jsr ea_load             ; t_op1 = source (sized)

        lda t_op2+1
        asl
        asl
        tay                     ; Y = dest reg * 4 (offset into A-file)
        lda t_op2+2
        cmp #$02
        beq @l
        ldx #$00                ; word: An = sext16(value)
        lda t_op1+1
        bpl @p
        dex                     ; X = $FF
@p:     lda t_op1
        sta r_a0,y
        lda t_op1+1
        sta r_a0+1,y
        txa
        sta r_a0+2,y
        sta r_a0+3,y
        jmp dispatch_next
@l:     lda t_op1               ; long: An = value
        sta r_a0,y
        lda t_op1+1
        sta r_a0+1,y
        lda t_op1+2
        sta r_a0+2,y
        lda t_op1+3
        sta r_a0+3,y
        jmp dispatch_next

; ---------------------------------------------------------------------------
; move_nz — MOVE CCR: N = msb of sized t_op1, Z = sized t_op1 == 0,
; V = C = 0, X unchanged.  A = size (0/1/2).  Hand-rolled instead of going
; through an ALU primitive so t_op2 stays free as the field stash.
; Clobbers A/X/Y, t_tmp+0/1 (no core calls while they're live).  ~60 cycles.
; ---------------------------------------------------------------------------
move_nz:
        tax
        lda r_sr
        and #$10                ; keep X flag only
        sta t_tmp
        cpx #$01
        beq @w
        bcs @l
        lda t_op1               ; byte
        sta t_tmp+1
        jmp @fin
@w:     lda t_op1               ; word
        ora t_op1+1
        sta t_tmp+1
        lda t_op1+1
        jmp @fin
@l:     lda t_op1               ; long
        ora t_op1+1
        ora t_op1+2
        ora t_op1+3
        sta t_tmp+1
        lda t_op1+3
@fin:   ; A = top sized byte, t_tmp+1 = OR of all sized bytes
        bpl @nn
        lda t_tmp
        ora #$08                ; N
        sta t_tmp
@nn:    lda t_tmp+1
        bne @nz
        lda t_tmp
        ora #$04                ; Z
        sta t_tmp
@nz:    lda t_tmp
        sta r_sr
        rts

; ---------------------------------------------------------------------------
; fam_moveq — MOVEQ #imm8,Dn ($7xxx, 68k bit 8 must be 0; else illegal).
; Dn.L = sext8(imm); N/Z from the long result (= from imm8), V=C=0, X kept.
; No fetches/EA calls, so op_lo stays live throughout.  ~70 cycles.
; ---------------------------------------------------------------------------
fam_moveq:
        lda op_hi
        lsr                     ; C = 68k bit 8
        bcs moveq_ill
        and #$07
        asl
        asl
        tay                     ; Y = Dn * 4
        ldx #$00
        lda op_lo
        bpl @p
        dex                     ; X = $FF extension byte
@p:     sta r_d0,y
        txa
        sta r_d0+1,y
        sta r_d0+2,y
        sta r_d0+3,y
        lda r_sr                ; CCR: keep X, N/Z from imm8, V=C=0
        and #$10
        ldy op_lo
        bne @nz
        ora #$04                ; Z
@nz:    cpy #$80
        bcc @nn
        ora #$08                ; N
@nn:    sta r_sr
        jmp dispatch_next
moveq_ill:
        jmp op_illegal

.segment "RODATA"
move_szmap:                     ; size bits 13-12 (01/11/10) -> 0/1/2 = B/W/L
        .byte $FF, $00, $02, $01
