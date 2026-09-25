; W5-K: EOF variants (docs/HLE_CON.md).  Queue "par" Ctrl-\\ ($1C) Ctrl-D
; ($04) "q\r" "tail": EOF on a non-empty line delivers the partial line
; (no newline); Ctrl-D on an empty line is EOF (0); input queued behind an
; EOF is still read; a scripted queue running dry mid-line delivers the
; partial line, then 0.  rd records as in u_con1.
RES     equ     $100000
BUF     equ     $100400
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)            ; OpenLibrary
        movea.l d0,a6
        jsr     -54(a6)             ; Input
        movea.l d0,a3
        jsr     -60(a6)             ; Output
        movea.l d0,a2
        lea     RES,a4
        moveq   #64,d3
        bsr     rd                  ; 3 "par"
        bsr     rd                  ; 0 (Ctrl-D, empty line)
        bsr     rd                  ; 2 "q\n"
        bsr     rd                  ; 4 "tail" (queue dry mid-line)
        bsr     rd                  ; 0
        rts

rd:     lea     BUF,a0
        move.l  #$2E2E2E2E,(a0)
        move.l  #$2E2E2E2E,4(a0)
        move.l  a3,d1
        move.l  a0,d2
        move.l  d3,d4
        move.l  #$55,d0             ; D0 must be overwritten
        jsr     -42(a6)             ; Read
        move.l  d0,(a4)+
        lea     BUF,a0
        move.l  (a0),(a4)+
        move.l  4(a0),(a4)+
        moveq   #0,d5
        cmp.l   #4,d0
        blt.s   .short
        move.l  -4(a0,d0.l),d5
.short: move.l  d5,(a4)+
        moveq   #0,d5
        cmp.l   a3,d1
        bne.s   .bad
        cmp.l   #BUF,d2
        bne.s   .bad
        cmp.l   d4,d3
        bne.s   .bad
        moveq   #1,d5
.bad:   move.l  d5,(a4)+
        rts
dosname: dc.b   "dos.library",0
        even
