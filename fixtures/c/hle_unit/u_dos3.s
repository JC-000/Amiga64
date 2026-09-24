; Read on NIL: returns 0 (EOF), Read on console out returns 0, Read on a
; NULL handle returns -1.
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        move.l  #FH_NIL>>2,d1
        move.l  a7,d2
        subq.l  #8,d2
        moveq   #4,d3
        move.l  #$55,d0             ; D0 must be overwritten
        jsr     -42(a6)             ; Read(NIL:, buf, 4)
        move.l  d0,d4               ; 0
        move.l  #FH_CONOUT>>2,d1
        jsr     -42(a6)             ; Read(console out)
        move.l  d0,d5               ; 0
        moveq   #0,d1
        jsr     -42(a6)             ; Read(NULL)
        move.l  d0,d6               ; -1
        rts
dosname: dc.b   "dos.library",0
        even
