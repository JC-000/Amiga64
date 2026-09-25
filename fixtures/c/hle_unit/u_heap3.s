; W5-H MEMF_CLEAR on a reused dirty block (whole and split), alignment of
; odd sizes, AllocVec's 8n+4 result on reused memory.
        movea.l 4.w,a6
        moveq   #100,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a2               ; A = H+$10 (112-byte block)
        movea.l d0,a0
        moveq   #25,d1
.fill:  move.l  #-1,(a0)+           ; dirty all 104 bytes
        dbra    d1,.fill
        moveq   #8,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d2               ; guard G = H+$80
        movea.l a2,a1
        moveq   #100,d0
        jsr     -210(a6)            ; free A (dirty)
        moveq   #100,d0
        move.l  #$10000,d1          ; MEMF_CLEAR
        jsr     -198(a6)
        movea.l d0,a3               ; = A again, whole block reused
        movea.l d0,a0
        moveq   #0,d3
        moveq   #25,d1
.or1:   or.l    (a0)+,d3            ; OR of all 104 bytes: 0
        dbra    d1,.or1
        movea.l a3,a0
        moveq   #-1,d1
        move.l  d1,(a0)             ; dirty again (the rest is 0 now)
        move.l  d1,36(a0)
        movea.l a3,a1
        moveq   #100,d0
        jsr     -210(a6)
        moveq   #40,d0
        move.l  #$10000,d1
        jsr     -198(a6)
        movea.l d0,a4               ; = A: split (48 of 112), cleared
        movea.l d0,a0
        moveq   #0,d4
        moveq   #9,d1
.or2:   or.l    (a0)+,d4            ; OR of 40 bytes: 0
        dbra    d1,.or2
        moveq   #0,d5               ; OR of (addr & 7) over odd sizes: 0
        moveq   #1,d0
        moveq   #0,d1
        jsr     -198(a6)            ; remainder block (64): 1 -> 16 needed
        or.l    d0,d5
        moveq   #3,d0
        moveq   #0,d1
        jsr     -198(a6)
        or.l    d0,d5
        moveq   #9,d0
        moveq   #0,d1
        jsr     -198(a6)
        or.l    d0,d5
        moveq   #17,d0
        moveq   #0,d1
        jsr     -198(a6)
        or.l    d0,d5
        move.l  #4095,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d6               ; last address, exact
        or.l    d0,d5
        andi.l  #7,d5
        moveq   #10,d0
        moveq   #0,d1
        jsr     -684(a6)            ; AllocVec(10)
        move.l  d0,d7               ; 8n+4
        rts
