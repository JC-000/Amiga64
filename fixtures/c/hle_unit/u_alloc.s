; AllocMem/FreeMem basics: MEMF_CLEAR over a poisoned heap, 8-byte rounding,
; no-clear leaves memory alone, top-block FreeMem reclaims, other FreeMem leaks.
        movea.l 4.w,a6
        move.l  #100,d0
        move.l  #$10001,d1          ; MEMF_PUBLIC|MEMF_CLEAR
        jsr     -198(a6)            ; AllocMem
        move.l  d0,d2               ; = HEAP_BASE
        movea.l d0,a0
        move.l  (a0),d3             ; 0 (cleared over $A5 poison)
        move.l  100(a0),d4          ; 0 (rounded tail 100..103 cleared too)
        moveq   #1,d0
        moveq   #0,d1
        jsr     -198(a6)            ; AllocMem(1, 0)
        move.l  d0,d5               ; = HEAP_BASE+104
        movea.l d0,a0
        movea.l (a0),a3             ; $A5A5A5A5: no MEMF_CLEAR -> untouched
        movea.l d5,a1
        moveq   #1,d0
        jsr     -210(a6)            ; FreeMem(top block) -> reclaimed
        moveq   #8,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d6               ; = HEAP_BASE+104 again
        movea.l d2,a1
        moveq   #100,d0
        jsr     -210(a6)            ; FreeMem(not top) -> leaked, no reuse
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a2               ; = HEAP_BASE+112
        suba.l  a1,a1
        moveq   #0,d0
        jsr     -210(a6)            ; FreeMem(NULL) -> no-op
        moveq   #1,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a4               ; = HEAP_BASE+128 (NULL free changed nothing)
        rts
