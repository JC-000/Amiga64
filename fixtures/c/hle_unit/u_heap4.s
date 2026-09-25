; W5-H DeletePool frees every block of the pool (and the pool header) but
; nothing else; DeletePool(NULL) is a no-op; DeletePool of something that is
; not a pool header is rejected like an invalid free.
        movea.l 4.w,a6
        moveq   #0,d0
        move.l  #$4000,d1
        move.l  #$4000,d2
        jsr     -696(a6)            ; CreatePool
        movea.l d0,a2               ; P = H+$10 (16-byte block)
        movea.l a2,a0
        moveq   #100,d0
        jsr     -708(a6)
        move.l  d0,d2               ; X1 = H+$20
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d3               ; M = H+$90 (AllocMem, not pooled)
        movea.l d0,a0
        move.l  #$CAFEF00D,(a0)
        movea.l a2,a0
        move.l  #200,d0
        jsr     -708(a6)
        move.l  d0,d4               ; X2 = H+$D8
        movea.l a2,a0
        moveq   #24,d0
        jsr     -708(a6)            ; X3 = H+$1A8 (top block)
        movea.l a2,a0
        jsr     -702(a6)            ; DeletePool(P)
        suba.l  a0,a0
        jsr     -702(a6)            ; DeletePool(NULL): no-op
        move.l  #120,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d5               ; = P: header + X1 merged (128 bytes)
        moveq   #8,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d6               ; = X2: top dropped back to M's end
        move.l  $100004,d7          ; 0 rejected so far
        movea.l d3,a0
        jsr     -702(a6)            ; DeletePool(M): not a pool -> rejected
        move.l  $100004,a3          ; = 1
        movea.l d3,a0
        movea.l (a0),a4             ; $CAFEF00D: M untouched
        rts
