; AllocVec/FreeVec (exec V36+): size longword stored in front of the block
; (block = size+4 bytes, result = block+4, as exec does), MEMF_CLEAR,
; FreeVec of the top block reclaims, FreeVec(NULL) is a no-op.
        movea.l 4.w,a6
        moveq   #100,d0
        move.l  #$10001,d1          ; MEMF_PUBLIC|MEMF_CLEAR
        jsr     -684(a6)            ; AllocVec(100)
        move.l  d0,d2               ; = HEAP_BASE+4
        movea.l d0,a0
        move.l  96(a0),d3           ; 0 (cleared over poison)
        move.l  -4(a0),d4           ; 104 (the stored block size)
        moveq   #8,d0
        moveq   #0,d1
        jsr     -684(a6)            ; AllocVec(8)
        move.l  d0,d5               ; = HEAP_BASE+108 (block 12 -> 16)
        movea.l d0,a1
        jsr     -690(a6)            ; FreeVec(top) -> reclaimed
        suba.l  a1,a1
        jsr     -690(a6)            ; FreeVec(NULL)
        moveq   #8,d0
        moveq   #0,d1
        jsr     -684(a6)
        move.l  d0,d6               ; = HEAP_BASE+108 again
        moveq   #0,d0
        moveq   #0,d1
        jsr     -684(a6)            ; AllocVec(0)
        move.l  d0,d7               ; 0
        rts
