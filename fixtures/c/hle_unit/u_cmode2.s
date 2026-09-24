; dos ChangeMode: everything but (CHANGE_FH, open file, EXCLUSIVE_LOCK)
; is refused: other modes 209, bad/console handles 211 (m2-post task 2).
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     fname(pc),a0
        move.l  a0,d1
        move.l  #1004,d2
        jsr     -30(a6)             ; Open("cm2", MODE_READWRITE)
        movea.l d0,a2               ; $300
        moveq   #1,d1
        move.l  a2,d2
        moveq   #-2,d3
        jsr     -450(a6)            ; ChangeMode(CHANGE_FH, fh, SHARED_LOCK)
        move.l  d0,d4               ; 0 (not implemented)
        jsr     -132(a6)
        move.l  d0,d5               ; 209
        moveq   #1,d1
        move.l  #$1234,d2
        moveq   #-1,d3
        jsr     -450(a6)            ; ChangeMode(bogus BPTR)
        move.l  d0,d6               ; 0
        jsr     -132(a6)
        move.l  d0,d7               ; 211
        moveq   #1,d1
        move.l  a2,d2
        move.l  #1006,d3
        jsr     -450(a6)            ; ChangeMode(fh, MODE_NEWFILE)
        movea.l d0,a3               ; 0
        jsr     -132(a6)
        movea.l d0,a4               ; 209
        jsr     -60(a6)             ; Output()
        move.l  d0,d2
        moveq   #1,d1
        moveq   #-1,d3
        jsr     -450(a6)            ; ChangeMode(console handle)
        move.l  d0,d2               ; 0
        jsr     -132(a6)
        movea.l d0,a5               ; 211
        move.l  a2,d1
        jsr     -36(a6)             ; Close(fh)
        move.l  d0,d3               ; -1
        rts
dosname: dc.b   "dos.library",0
fname:  dc.b    "cm2",0
        even
