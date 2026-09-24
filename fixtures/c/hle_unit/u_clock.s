; dos DateStamp advances one tick per call and carries into ds_Minute and
; ds_Days (m2-post task 4).  The guest presets the HLE clock at ENV_CLOCK
; to day 5, 23:59, tick 2999: the next tick is day 6, 00:00, tick 0.
        move.l  #5,ENV_CLOCK.w
        move.l  #(1439<<16)|2999,ENV_CLOCK+4.w
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     -12(a7),a7
        move.l  a7,d1
        jsr     -192(a6)            ; DateStamp(sp)
        move.l  (a7),d2             ; 6
        move.l  4(a7),d3            ; 0
        move.l  8(a7),d4            ; 0
        move.l  a7,d1
        jsr     -192(a6)
        move.l  (a7),d5             ; 6
        move.l  4(a7),d6            ; 0
        move.l  8(a7),d7            ; 1
        move.l  #(59<<16)|2999,ENV_CLOCK+4.w
        move.l  a7,d1
        jsr     -192(a6)            ; minute carry only
        move.l  4(a7),a2            ; 60
        move.l  8(a7),a3            ; 0
        move.l  (a7),a4             ; 6
        lea     12(a7),a7
        rts
dosname: dc.b   "dos.library",0
        even
