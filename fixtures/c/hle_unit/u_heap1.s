; W5-H invalid frees: each is rejected, counted in HEAP_CTL+4 and leaves the
; heap alone (the block stays allocated, nothing is handed out twice).
; NULL is a silent no-op.  A = H+$10, B = H+$58 (64 -> 72-byte blocks).
        movea.l 4.w,a6
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d2               ; A = H+$10
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d3               ; B = H+$58
        suba.l  a1,a1
        moveq   #64,d0
        jsr     -210(a6)            ; NULL: no-op, not counted
        movea.l d2,a1
        moveq   #32,d0
        jsr     -210(a6)            ; 1: size too small
        movea.l d2,a1
        move.l  #128,d0
        jsr     -210(a6)            ; 2: size too large
        movea.l d2,a1
        moveq   #0,d0
        jsr     -210(a6)            ; 3: size 0
        movea.l d2,a1
        addq.l  #8,a1
        moveq   #56,d0
        jsr     -210(a6)            ; 4: interior pointer (poisoned payload)
        movea.l d2,a1
        addq.l  #4,a1
        moveq   #64,d0
        jsr     -210(a6)            ; 5: misaligned
        movea.w #$F000,a1
        moveq   #64,d0
        jsr     -210(a6)            ; 6: below the heap (guest stack)
        movea.l #$900000,a1
        moveq   #64,d0
        jsr     -210(a6)            ; 7: above the heap
        movea.l #$100000,a1
        moveq   #64,d0
        jsr     -210(a6)            ; 8: the control block itself
        movea.l d3,a1
        adda.l  #$1000,a1
        moveq   #64,d0
        jsr     -210(a6)            ; 9: inside the heap, above top
        movea.l d2,a1
        move.l  #$7FFFFFF8,d0
        jsr     -210(a6)            ; 10: absurd size
        move.l  $100004,d4          ; = 10 rejected
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d5               ; = H+$A0: A and B still live
        movea.l d2,a1
        moveq   #64,d0
        jsr     -210(a6)            ; valid free of A
        movea.l d2,a1
        moveq   #64,d0
        jsr     -210(a6)            ; 11: double free
        move.l  $100004,d6          ; = 11
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d7               ; = A (reused once)
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a2               ; = H+$E8 (not A a second time)
        move.l  $100000,a3          ; free-list head = 0
        rts
