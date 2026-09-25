; W5-H DeletePool walks past free blocks: pool block X1, an AllocMem hole
; H1 (freed), pool block X2 above the hole.  DeletePool must free X2 too,
; so pool header + X1 + H1 + X2 become one 232-byte block (the guard G
; above stays live).
        movea.l 4.w,a6
        moveq   #0,d0
        move.l  #$4000,d1
        move.l  #$4000,d2
        jsr     -696(a6)            ; CreatePool
        movea.l d0,a2               ; P = H+$10 (16-byte block)
        movea.l a2,a0
        moveq   #64,d0
        jsr     -708(a6)
        move.l  d0,d2               ; X1 = H+$20
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d3               ; H1 = H+$68 (AllocMem)
        movea.l a2,a0
        moveq   #64,d0
        jsr     -708(a6)
        move.l  d0,d4               ; X2 = H+$B0
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a3               ; G = H+$F8
        move.l  #$6000D00D,(a3)
        movea.l d3,a1
        moveq   #64,d0
        jsr     -210(a6)            ; free H1: a hole between X1 and X2
        movea.l a2,a0
        jsr     -702(a6)            ; DeletePool(P)
        move.l  #224,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d5               ; = P: 232-byte block, exact fit
        move.l  $100000,d6          ; free-list head = 0
        move.l  $100004,d7          ; 0 rejected
        movea.l (a3),a4             ; $6000D00D: G untouched
        rts
