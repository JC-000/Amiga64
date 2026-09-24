; exec memory pools (V39+): CreatePool/AllocPooled/FreePooled/DeletePool.
        movea.l 4.w,a6
        move.l  #$10000,d0          ; MEMF_CLEAR
        move.l  #$4000,d1           ; puddle
        move.l  #$4000,d2           ; threshold
        jsr     -696(a6)            ; CreatePool
        movea.l d0,a2               ; = HEAP_BASE (8-byte pool header)
        movea.l a2,a0
        moveq   #50,d0
        jsr     -708(a6)            ; AllocPooled(a2, 50)
        move.l  d0,d3               ; = HEAP_BASE+8
        movea.l d0,a0
        move.l  48(a0),d4           ; 0: pool flags carry MEMF_CLEAR
        moveq   #0,d0
        move.l  #$4000,d1
        move.l  #$4000,d2
        jsr     -696(a6)            ; CreatePool(flags 0)
        movea.l d0,a3               ; = HEAP_BASE+64
        movea.l a3,a0
        moveq   #20,d0
        jsr     -708(a6)
        move.l  d0,d5               ; = HEAP_BASE+72
        movea.l d0,a0
        move.l  (a0),d6             ; $A5A5A5A5: flags 0 -> not cleared
        movea.l a3,a0
        movea.l d5,a1
        moveq   #20,d0
        jsr     -714(a6)            ; FreePooled (top -> reclaimed)
        movea.l a3,a0
        moveq   #8,d0
        jsr     -708(a6)
        movea.l d0,a4               ; = HEAP_BASE+72 again
        movea.l a3,a0
        jsr     -702(a6)            ; DeletePool
        movea.l a2,a0
        jsr     -702(a6)            ; DeletePool
        moveq   #0,d0
        move.l  #$100,d1
        move.l  #$200,d2            ; threshold > puddle -> fails
        jsr     -696(a6)
        move.l  d0,d7               ; 0
        rts
