; W5-H coalescing: free A, C, then B between them merges all three (upper
; and lower neighbour); double frees of A, B and C are all rejected after
; the merge; freeing the top block D then merges everything into the
; wilderness (top drops back to the first block).  Also the 8-byte slack
; case: a 200-byte request takes the whole 216-byte merged block and must
; be freed with its own size.
        movea.l 4.w,a6
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d2               ; A = H+$10
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d3               ; B = H+$58
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d4               ; C = H+$A0
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a2               ; D = H+$E8 (top block)
        movea.l d2,a1
        moveq   #64,d0
        jsr     -210(a6)            ; free A
        movea.l d4,a1
        moveq   #64,d0
        jsr     -210(a6)            ; free C
        movea.l d3,a1
        moveq   #64,d0
        jsr     -210(a6)            ; free B: A+B+C = one 216-byte block
        movea.l d3,a1
        moveq   #64,d0
        jsr     -210(a6)            ; double free B (tag scrubbed)
        movea.l d4,a1
        moveq   #64,d0
        jsr     -210(a6)            ; double free C (stale free header)
        movea.l d2,a1
        moveq   #64,d0
        jsr     -210(a6)            ; double free A (merged block, free)
        move.l  $100004,d5          ; = 3
        move.l  #200,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d6               ; = A: 208 needed, 216 free, no split
        movea.l d6,a1
        move.l  #200,d0
        jsr     -210(a6)            ; frees the slack block with its size
        movea.l a2,a1
        moveq   #64,d0
        jsr     -210(a6)            ; free D: merges into the wilderness
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d7               ; = H+$10: from the wilderness again
        move.l  $100000,a3          ; free-list head = 0
        move.l  $100004,a4          ; still 3 rejected
        rts
