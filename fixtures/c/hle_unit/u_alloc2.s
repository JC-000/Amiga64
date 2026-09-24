; AllocMem limits: size 0, absurd size, exact exhaustion, reclaim of all.
        movea.l 4.w,a6
        move.l  #$7FFFFFF8,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d2               ; 0 (absurd size)
        moveq   #0,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d3               ; 0 (size 0)
        move.l  #$700000,d0         ; exactly the whole heap
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d4               ; = HEAP_BASE
        moveq   #1,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d5               ; 0 (exhausted)
        movea.l d4,a1
        move.l  #$700000,d0
        jsr     -210(a6)            ; free everything (top block)
        moveq   #1,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d6               ; = HEAP_BASE again
        move.l  #$6FFFF9,d0         ; rounds to $700000 > $6FFFF8 left
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d7               ; 0
        rts
