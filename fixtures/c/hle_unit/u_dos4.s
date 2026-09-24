; dos GetVar / DateStamp / SystemTagList (b1-review R1/R2).
        movea.l 4.w,a6
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)            ; AllocMem(16): poisoned, not cleared
        movea.l d0,a2               ; = HEAP_BASE
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     var(pc),a0
        move.l  a0,d1
        move.l  a2,d2
        moveq   #16,d3
        move.l  #$400,d4            ; GVF_GLOBAL_ONLY? any flags
        moveq   #5,d0               ; D0 must be overwritten
        jsr     -906(a6)            ; GetVar("HOME", buf, 16, flags)
        move.l  d0,d5               ; -1 (not found)
        move.l  a2,d1
        moveq   #7,d0
        jsr     -192(a6)            ; DateStamp(a2)
        move.l  d0,d6               ; = a2
        move.l  (a2),d2             ; ds_Days   0 (over $A5 poison)
        move.l  4(a2),d3            ; ds_Minute 0
        move.l  8(a2),d4            ; ds_Tick   0
        move.l  12(a2),a4           ; untouched: $A5A5A5A5
        lea     cmd(pc),a0
        move.l  a0,d1
        moveq   #0,d2
        moveq   #3,d0
        jsr     -606(a6)            ; SystemTagList("echo hi", NULL)
        move.l  d0,d7               ; -1
        rts
dosname: dc.b   "dos.library",0
var:    dc.b    "HOME",0
cmd:    dc.b    "echo hi",0
        even
