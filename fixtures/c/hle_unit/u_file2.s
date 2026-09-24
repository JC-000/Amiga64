; C2 seam: Read on a closed handle (fh_Args = $FFFFFFFF, WS-B2's Close
; marker) must be handed to hle_dos_read, not treated as console.
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     -44(a7),a7
        moveq   #-1,d0
        move.l  d0,36(a7)
        move.l  a7,d1
        lsr.l   #2,d1
        move.l  #$23456,d2
        moveq   #9,d3
        jsr     -42(a6)             ; Read
        lea     44(a7),a7
        rts
dosname: dc.b   "dos.library",0
        even
