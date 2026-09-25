; ---------------------------------------------------------------------------
; tools/s3probe/c2v.s -- W6-D (docs/S3_PLAN.md Q2) planar -> VIC-II bitmap
; conversion kernels, for CYCLE MEASUREMENT ONLY (not runtime code).
;
; Both kernels convert one frame (200 lines) of Amiga bitplane rows that are
; already in C64 RAM (the REU fetch is priced separately from HARDWARE.md's
; measured bulk-DMA rate) into a VIC-II bitmap in char-cell order
; (byte for cell cx, line y at BMP + (y>>3)*320 + cx*8 + (y&7)).
;
;   k_hires  4-plane 640-px hires row -> 320-px hires bitmap row:
;            pixel on iff its Amiga colour index != 0 (OR of the planes),
;            2:1 horizontal by OR-ing adjacent pixel pairs (PAIRHI/PAIRLO).
;   k_mc2    2-plane 320-px lores row -> 160-px multicolour row:
;            MC pixel = (plane1, plane0) of the left lores pixel of each
;            pair, i.e. the Amiga colour index 0-3 itself (exact colours via
;            $D021 / screen RAM / colour RAM, set once per palette change).
;
; Every input row is the same 80 (hires) or 40 (lores) bytes per plane (the
; data does not change the cycle count: no data-dependent branches).
; Timing: CIA2 timer A (phi2 cycles) cascaded into timer B, screen blanked
; so no badline DMA; result (32-bit, LE) at res_cyc.  Harness: jsr() the
; entry, then read res_cyc.  tools/s3probe/c2v_measure.py drives it.
; ---------------------------------------------------------------------------

P0    = $2000                   ; plane rows (page-aligned, 80 bytes used)
P1    = $2100
P2    = $2200
P3    = $2300
BMP   = $4000                   ; 8000-byte bitmap (measurement target)
PAIRHI = $2400                  ; byte -> pair-OR of its 4 pixel pairs, bits 7-4
PAIRLO = $2500                  ; same, bits 3-0

CIA2_TALO = $DD04
CIA2_TAHI = $DD05
CIA2_TBLO = $DD06
CIA2_TBHI = $DD07
CIA2_CRA  = $DD0E
CIA2_CRB  = $DD0F

.segment "ZEROPAGE"
line:   .res 1
.segment "LOADADDR"
        .word $0801
.segment "STARTUP"
        .word @next, 10
        .byte $9E, "2061", 0
@next:  .word 0
main:   sei
        lda #$A5
        sta ready
@idle:  jmp @idle

.segment "CODE"
.export k_hires_entry, k_mc2_entry, make_tables, res_cyc, ready, line_ptr

ready:   .byte 0
res_cyc: .res 4
line_ptr: .word 0

; start / stop the 32-bit phi2 counter
.proc t_start
        lda #0
        sta CIA2_CRA
        sta CIA2_CRB
        lda #$FF
        sta CIA2_TALO
        sta CIA2_TAHI
        sta CIA2_TBLO
        sta CIA2_TBHI
        lda #%01010001          ; TB: force load, count TA underflows, start
        sta CIA2_CRB
        lda #%00010001          ; TA: force load, continuous, start
        sta CIA2_CRA
        rts
.endproc
.proc t_stop
        lda #0
        sta CIA2_CRA            ; stop TA (TB stops counting with it)
        lda CIA2_TALO
        eor #$FF
        sta res_cyc
        lda CIA2_TAHI
        eor #$FF
        sta res_cyc+1
        lda CIA2_TBLO
        eor #$FF
        sta res_cyc+2
        lda CIA2_TBHI
        eor #$FF
        sta res_cyc+3
        rts
.endproc

; PAIRHI[b] bit (7-k) for k=0..3 = b bit(7-2k) | b bit(6-2k); PAIRLO = >>4
.proc make_tables
        ldx #0
@b:     txa
        ldy #4
        lda #0
        sta tmp
        txa
@p:     asl                     ; C = pixel 2k
        bcs @on1
        asl                     ; C = pixel 2k+1
        jmp @one
@on1:   asl
        sec
@one:   rol tmp
        dey
        bne @p
        lda tmp
        sta PAIRLO,x
        asl
        asl
        asl
        asl
        sta PAIRHI,x
        inx
        bne @b
        rts
tmp:    .byte 0
.endproc

; line_ptr = BMP + (line>>3)*320 + (line&7)
.proc set_line
        lda line
        lsr
        lsr
        lsr                     ; row 0-24
        tax
        lda rowlo,x
        sta line_ptr
        lda rowhi,x
        sta line_ptr+1
        lda line
        and #7
        clc
        adc line_ptr
        sta line_ptr
        bcc :+
        inc line_ptr+1
:       rts
.endproc

; --- 4-plane hires 640 -> 320 hires, one frame ------------------------------
.proc k_hires_entry
        lda $D011
        and #%11101111          ; blank: no badlines while measuring
        sta $D011
        jsr t_start
        lda #0
        sta line
@ln:    jsr set_line
        lda line_ptr            ; patch the two output segments
        sta @o1+1
        sta @o2+1
        lda line_ptr+1
        sta @o1+2
        clc
        adc #1
        sta @o2+2
        ldx #0                  ; output offset (cell*8), cells 0-31
        ldy #0                  ; input byte index
@l1:    lda P0,y                ; 4
        ora P1,y                ; 4
        ora P2,y                ; 4
        ora P3,y                ; 4
        sta @h1+1               ; 4
        lda P0+1,y              ; 4
        ora P1+1,y              ; 4
        ora P2+1,y              ; 4
        ora P3+1,y              ; 4
        sta @q1+1               ; 4
@q1:    lda PAIRLO              ; 4  (operand low byte patched)
@h1:    ora PAIRHI              ; 4
@o1:    sta BMP,x               ; 5
        iny                     ; 2
        iny                     ; 2
        txa                     ; 2
        clc                     ; 2
        adc #8                  ; 2
        tax                     ; 2
        bne @l1                 ; 3   x wraps to 0 after 32 cells (y = 64)
        ; x wrapped: y = 64 here, cells 32-39 at page +1
@l2:    lda P0,y
        ora P1,y
        ora P2,y
        ora P3,y
        sta @h2+1
        lda P0+1,y
        ora P1+1,y
        ora P2+1,y
        ora P3+1,y
        sta @q2+1
@q2:    lda PAIRLO
@h2:    ora PAIRHI
@o2:    sta BMP+256,x
        txa
        clc
        adc #8
        tax
        iny
        iny
        cpy #80
        bne @l2
        inc line
        lda line
        cmp #200
        beq @done
        jmp @ln
@done:  jsr t_stop
        rts
.endproc

; --- 2-plane lores 320 -> 160 multicolour, one frame ------------------------
.proc k_mc2_entry
        lda $D011
        and #%11101111
        sta $D011
        jsr t_start
        lda #0
        sta line
@ln:    jsr set_line
        lda line_ptr
        sta @o1+1
        sta @o2+1
        lda line_ptr+1
        sta @o1+2
        clc
        adc #1
        sta @o2+2
        ldx #0
        ldy #0
@l1:    lda P0,y                ; 4
        and #$AA                ; 2   left pixel of each pair
        lsr                     ; 2   -> bit positions 6,4,2,0
        sta @t1+1               ; 4
        lda P1,y                ; 4
        and #$AA                ; 2   -> bit positions 7,5,3,1
@t1:    ora #0                  ; 2   (immediate patched)
@o1:    sta BMP,x               ; 5
        iny                     ; 2
        txa                     ; 2
        clc                     ; 2
        adc #8                  ; 2
        tax                     ; 2
        bne @l1                 ; 3   x wraps after 32 cells (y = 32)
@l2:    lda P0,y
        and #$AA
        lsr
        sta @t2+1
        lda P1,y
        and #$AA
@t2:    ora #0
@o2:    sta BMP+256,x
        txa
        clc
        adc #8
        tax
        iny
        cpy #40
        bne @l2
        inc line
        lda line
        cmp #200
        beq @done
        jmp @ln
@done:  jsr t_stop
        rts
.endproc

.segment "RODATA"
rowlo:
.repeat 25, i
        .byte <(BMP + i*320)
.endrepeat
rowhi:
.repeat 25, i
        .byte >(BMP + i*320)
.endrepeat
