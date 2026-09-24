; Console without a prior Output() call (cursor state comes from the env),
; plus negative-length Write.
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        move.l  #FH_CONOUT>>2,d1
        lea     msg(pc),a0
        move.l  a0,d2
        moveq   #msge-msg,d3
        jsr     -48(a6)             ; Write without Output()
        move.l  d0,d4               ; = length
        move.l  #FH_CONOUT>>2,d1
        moveq   #-1,d3
        jsr     -48(a6)             ; Write(len -1)
        move.l  d0,d5               ; -1, nothing printed
        move.l  #FH_CONIN>>2,d1
        lea     msg2(pc),a0
        move.l  a0,d2
        moveq   #3,d3
        jsr     -48(a6)             ; Write to the console-in handle -> console
        move.l  d0,d6               ; 3
        rts
dosname: dc.b   "dos.library",0
msg:    dc.b    "no output call",10
msge:
msg2:   dc.b    "in!"
        even
