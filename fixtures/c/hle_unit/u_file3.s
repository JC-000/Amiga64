; C2 seam: a kind with an upper byte set and a low byte below $10
; (fh_Args = $00000100) is a file kind too and must be handed to
; hle_dos_write, never treated as console/NIL.
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     -44(a7),a7
        move.l  #$100,36(a7)        ; fh_Args = $00000100
        move.l  a7,d1
        lsr.l   #2,d1
        lea     msg(pc),a0
        move.l  a0,d2
        moveq   #3,d3
        jsr     -48(a6)             ; Write
        lea     44(a7),a7
        rts
dosname: dc.b   "dos.library",0
msg:    dc.b    "BAD"
        even
