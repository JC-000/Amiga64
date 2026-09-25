; W5-H merge-down keeps a valid footer: free A, then B above it (B merges
; down into A: the grown block's footer must be rewritten at B's end), then
; C above B (C finds the grown block through that footer and merges down
; too).  A+B+C is then one 216-byte block that takes a 200-byte request.
        movea.l 4.w,a6
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d2               ; A = H+$10
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d3               ; B = H+$58 (payload poisoned $A5)
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d4               ; C = H+$A0
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a2               ; D = H+$E8 guards the top
        movea.l d2,a1
        moveq   #64,d0
        jsr     -210(a6)            ; free A
        movea.l d3,a1
        moveq   #64,d0
        jsr     -210(a6)            ; free B: merges down into A
        movea.l d4,a1
        moveq   #64,d0
        jsr     -210(a6)            ; free C: merges down via B's end footer
        move.l  #200,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d5               ; = A: A+B+C is one 216-byte block
        move.l  $100000,d6          ; free-list head = 0 (nothing left)
        move.l  $100004,d7          ; 0 rejected
        rts
