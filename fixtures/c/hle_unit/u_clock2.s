; dos DateStamp recovers from a corrupted HLE clock (m2-post fix1 N1): the
; carries use >=, so a tick >= 3000 or a minute >= 1440 is normalised on
; the next call instead of counting on forever.
        clr.l   ENV_CLOCK.w
        move.l  #(5<<16)|3100,ENV_CLOCK+4.w     ; tick 3100: out of range
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     -12(a7),a7
        move.l  a7,d1
        jsr     -192(a6)            ; DateStamp
        move.l  (a7),d2             ; 0
        move.l  4(a7),d3            ; 6
        move.l  8(a7),d4            ; 0
        move.l  #(1500<<16)|10,ENV_CLOCK+4.w    ; minute 1500: out of range
        move.l  a7,d1
        jsr     -192(a6)
        move.l  (a7),d5             ; 1
        move.l  4(a7),d6            ; 0
        move.l  8(a7),d7            ; 11
        lea     12(a7),a7
        rts
dosname: dc.b   "dos.library",0
        even
