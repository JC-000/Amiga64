; dos GetVar leaves its buffer untouched and sets IoErr 205
; (ERROR_OBJECT_NOT_FOUND); Close on the console/NIL: handles is a
; DOSTRUE no-op that leaves IoErr alone (m2-post task 5/6).
        movea.l 4.w,a6
        moveq   #32,d0
        moveq   #0,d1
        jsr     -198(a6)            ; AllocMem(32): $A5-poisoned, not cleared
        movea.l d0,a2               ; = HEAP_BASE
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     var(pc),a0
        move.l  a0,d1
        move.l  a2,d2
        moveq   #32,d3
        moveq   #0,d4
        jsr     -906(a6)            ; GetVar("HOME", buf, 32, 0)
        move.l  d0,d5               ; -1
        move.l  (a2),d2             ; untouched: $A5A5A5A5
        move.l  28(a2),d3           ; untouched: $A5A5A5A5 (last longword)
        jsr     -132(a6)            ; IoErr()
        move.l  d0,d6               ; 205
        jsr     -60(a6)             ; Output()
        move.l  d0,d1
        jsr     -36(a6)             ; Close(Output())
        move.l  d0,d7               ; -1
        jsr     -54(a6)             ; Input()
        move.l  d0,d1
        jsr     -36(a6)             ; Close(Input())
        move.l  d0,a3               ; -1
        move.l  #FH_NIL>>2,d1
        jsr     -36(a6)             ; Close(NIL:)
        move.l  d0,a4               ; -1
        jsr     -132(a6)
        move.l  d0,a5               ; still 205
        movem.l d2-d3,-(a7)
        jsr     -60(a6)             ; the console survives its Close:
        move.l  d0,d1
        lea     ok(pc),a0
        move.l  a0,d2
        moveq   #2,d3
        jsr     -48(a6)             ; Write(Output(), "ok", 2)
        movem.l (a7)+,d2-d3
        rts
dosname: dc.b   "dos.library",0
var:    dc.b    "HOME",0
ok:     dc.b    "ok"
        even
