; C2 seam: Write on a FileHandle with fh_Args >= $10 must be handed to
; hle_dos_write (WS-B2) with the call state untouched.
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     -44(a7),a7          ; FileHandle on the stack ($00EFD0)
        move.l  #$10,36(a7)         ; fh_Args = FH_KIND_FILE0
        move.l  a7,d1
        lsr.l   #2,d1
        move.l  #$12345,d2
        move.l  #$777,d3
        jsr     -48(a6)             ; Write
        lea     44(a7),a7
        rts
dosname: dc.b   "dos.library",0
        even
