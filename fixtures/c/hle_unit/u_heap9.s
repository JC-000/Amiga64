; W5-H walk guard: a guest-corrupted free list that is a cycle (C -> A -> C)
; must not hang AllocMem.  The first-fit walk gives up after WALK_MAX
; ($38000) nodes and carves the block from the wilderness instead.
        movea.l 4.w,a6
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a2               ; A = H+$10 (24-byte block, hdr H+$08)
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)            ; B = H+$28
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a3               ; C = H+$40 (hdr H+$38)
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)            ; D = H+$58, top = H+$68
        movea.l a2,a1
        moveq   #16,d0
        jsr     -210(a6)            ; free A
        movea.l a3,a1
        moveq   #16,d0
        jsr     -210(a6)            ; free C: list C -> A
        lea     -8(a3),a0
        move.l  a0,-4(a2)           ; corrupt A.next := C: a cycle
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d2               ; = H+$70: from the wilderness
        move.l  $100000,d3          ; head still C's header (H+$38)
        rts
