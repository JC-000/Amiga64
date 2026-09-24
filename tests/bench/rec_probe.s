; REC register behaviour probe.  CMDF = fetch command used for the transfers.
; Results: $C100.. data buffer, $C200+16*step+reg readbacks ($DF02-$DF0A).
.segment "CODE"
        sei
        ; fill $C300..$C3FF with 0..255, stash to REU $012300 (autoload off)
        ldx #0
@f:     txa
        sta $C300,x
        inx
        bne @f
        lda #$00
        sta $DF02
        lda #$C3
        sta $DF03
        lda #$00
        sta $DF04
        lda #$23
        sta $DF05
        lda #$01
        sta $DF06
        lda #$00
        sta $DF07
        lda #$01
        sta $DF08
        lda #$00
        sta $DF0A
        lda #$90
        sta $DF01
        ; clear $C100..$C10F
        lda #$EE
        ldx #15
@c:     sta $C100,x
        dex
        bpl @c
        ; step 1: fetch 4 from $012345 to $C100
        lda #$00
        sta $DF02
        lda #$C1
        sta $DF03
        lda #$45
        sta $DF04
        lda #$23
        sta $DF05
        lda #$01
        sta $DF06
        lda #$04
        sta $DF07
        lda #$00
        sta $DF08
        lda #$00
        sta $DF0A
        lda #CMDF
        sta $DF01
        ldx #2
@r1:    lda $DF00,x
        sta $C200,x
        inx
        cpx #$0B
        bne @r1
        ; step 2: write REULO only, fetch again
        lda #$50
        sta $DF04
        lda #CMDF
        sta $DF01
        ldx #2
@r2:    lda $DF00,x
        sta $C210,x
        inx
        cpx #$0B
        bne @r2
        ; step 3: write nothing, fetch again
        lda #CMDF
        sta $DF01
        ldx #2
@r3:    lda $DF00,x
        sta $C220,x
        inx
        cpx #$0B
        bne @r3
        ; step 4: LENLO=2, REULO=$60, fetch again
        lda #$02
        sta $DF07
        lda #$60
        sta $DF04
        lda #CMDF
        sta $DF01
        ldx #2
@r4:    lda $DF00,x
        sta $C230,x
        inx
        cpx #$0B
        bne @r4
        ; step 5: C64HI = $C1 rewrite + C64LO=$08, stash-class cmd with same autoload bit: write C64 $C108.. (2 bytes) back to REU $012360
        lda #$08
        sta $DF02
        lda #(CMDF & $FE)
        sta $DF01
        ldx #2
@r5:    lda $DF00,x
        sta $C240,x
        inx
        cpx #$0B
        bne @r5
        ; step 6: fetch from REU $012360 len 2 into C64 $C10C (verify stash went where expected)
        lda #$0C
        sta $DF02
        lda #CMDF
        sta $DF01
        ldx #2
@r6:    lda $DF00,x
        sta $C250,x
        inx
        cpx #$0B
        bne @r6
        rts
