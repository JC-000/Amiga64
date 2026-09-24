; REC probe, part 2 (WS-A fix round): bank carry under autoload and the
; 512 KB address-counter wrap.  Assembled at $C000 with
; tests/bench/rec_probe_x.cfg; CMDF = fetch command for the bank steps
; ($91 no autoload / $B1 autoload).  Layout (all in buf_case):
;   $C600..      data buffer (fetch targets)
;   $C640+16*k   REC readback after bank step k ($DF02-$DF0A at +2..+10)
;   $C680..      512K-wrap raw readbacks
;   $C700..      source patterns ($A0+i)
.segment "CODE"

C64LO = $DF02
C64HI = $DF03
REULO = $DF04
REUHI = $DF05
BANK  = $DF06
LENLO = $DF07
LENHI = $DF08
ACTRL = $DF0A
CMD   = $DF01

; full program + command (assumes nothing about REC state)
.macro XFER c64, reu, len, cmd
        lda #<(c64)
        sta C64LO
        lda #>(c64)
        sta C64HI
        lda #<(reu)
        sta REULO
        lda #>(reu)
        sta REUHI
        lda #^(reu)
        sta BANK
        lda #<(len)
        sta LENLO
        lda #>(len)
        sta LENHI
        lda #0
        sta ACTRL
        lda #cmd
        sta CMD
.endmacro

.macro READREGS dst
        .local l
        ldx #2
l:      lda $DF00,x
        sta dst,x
        inx
        cpx #$0B
        bne l
.endmacro

        sei
        ldx #$5F                        ; patterns: $C700+i = $A0+i
@p:     txa
        clc
        adc #$A0
        sta $C700,x
        dex
        bpl @p
        lda #$EE
        ldx #$3F
@c:     sta $C600,x
        dex
        bpl @c

        ; ---- bank carry: stage REU without crossing a bank in one transfer
        XFER $C700, $01FFF8, 8, $90     ; $01FFF8.. = A0..A7
        XFER $C708, $020000, 8, $90     ; $020000.. = A8..AF
        XFER $C710, $010000, 4, $90     ; $010000.. = B0..B3
        ; step 0: 4-byte fetch at $01FFFE (crosses bank 1 -> 2) -> $C600
        XFER $C600, $01FFFE, 4, CMDF    ; carry ok: A6 A7 A8 A9
        READREGS $C640
        ; step 1: same-bank access writing only C64LO/REULO/REUHI (BANK
        ; untouched), fetch 4 -> $C604: bank 1 -> B0..B3, bank 2 -> A8..AB
        lda #$04
        sta C64LO
        lda #$00
        sta REULO
        sta REUHI
        lda #CMDF
        sta CMD
        READREGS $C650
        ; step 2: stash-direction crossing: B4..B7 -> $01FFFE..$020001
        XFER $C714, $01FFFE, 4, (CMDF & $FE)
        READREGS $C660
        ; step 3: same-bank fetch as step 1 -> $C608 (C64HI rewritten: the
        ; step-2 source was page $C7)
        lda #$08
        sta C64LO
        lda #$C6
        sta C64HI
        lda #$00
        sta REULO
        sta REUHI
        lda #CMDF
        sta CMD
        READREGS $C670
        ; step 4: raw readback of what step 2 stored: $01FFFE..$020001 -> $C60C
        XFER $C60C, $01FFFE, 2, $91
        XFER $C60E, $020000, 2, $91

        ; ---- 512K wrap (raw, no autoload): sentinels, then a 4-byte stash
        XFER $C720, $080000, 2, $90     ; $080000/1 = C0 C1
        XFER $C722, $000000, 2, $90     ; $000000/1 = C2 C3
        XFER $C724, $07FFFC, 2, $90     ; $07FFFC/D = C4 C5
        XFER $C730, $07FFFE, 4, $90     ; D0 D1 D2 D3 -> $07FFFE (crosses)
        ; byte-wise reads (1-byte transfers never cross): $C680..
        XFER $C680, $07FFFE, 1, $91
        XFER $C681, $07FFFF, 1, $91
        XFER $C682, $080000, 1, $91
        XFER $C683, $080001, 1, $91
        XFER $C684, $000000, 1, $91
        XFER $C685, $000001, 1, $91
        ; the 4-byte fetch across the boundary -> $C688..$C68B
        XFER $C688, $07FFFE, 4, $91
        ; 24-bit top: sentinel $F80000/1 = C8 C9, stash D4..D7 -> $FFFFFE
        XFER $C728, $F80000, 2, $90
        XFER $C72A, $000000, 2, $90     ; $000000/1 = CA CB again
        XFER $C734, $FFFFFE, 4, $90
        XFER $C690, $FFFFFE, 1, $91
        XFER $C691, $FFFFFF, 1, $91
        XFER $C692, $F80000, 1, $91
        XFER $C693, $F80001, 1, $91
        XFER $C694, $000000, 1, $91
        XFER $C695, $000001, 1, $91
        rts
