; Console FileHandles (contract C2): Output/Input BPTRs, IsInteractive,
; Write to NIL:/console/NULL, Read from console in.
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)            ; OpenLibrary
        movea.l d0,a6
        jsr     -60(a6)             ; Output
        movea.l d0,a2               ; = FH_CONOUT>>2
        jsr     -54(a6)             ; Input
        movea.l d0,a3               ; = FH_CONIN>>2
        move.l  a2,d1
        jsr     -216(a6)            ; IsInteractive(Output())
        move.l  d0,d4               ; -1
        move.l  #FH_NIL>>2,d1
        jsr     -216(a6)            ; IsInteractive(NIL:)
        move.l  d0,d5               ; 0
        move.l  #FH_NIL>>2,d1
        lea     msg1(pc),a0
        move.l  a0,d2
        moveq   #5,d3
        jsr     -48(a6)             ; Write(NIL:, "NULL!", 5)
        move.l  d0,d6               ; 5, nothing printed
        move.l  a2,d1
        lea     msg2(pc),a0
        move.l  a0,d2
        moveq   #msg2e-msg2,d3
        jsr     -48(a6)             ; Write(Output(), msg2)
        move.l  d0,d7               ; = length
        move.l  a3,d1
        move.l  a7,d2
        subq.l  #8,d2
        moveq   #4,d3
        jsr     -42(a6)             ; Read(Input(), buf, 4)
        movea.l d0,a4               ; 0 (EOF: no keyboard in v0)
        moveq   #0,d1
        move.l  d2,d2
        moveq   #1,d3
        jsr     -48(a6)             ; Write(NULL, ...)
        movea.l d0,a5               ; -1
        rts
dosname: dc.b   "dos.library",0
msg1:   dc.b    "NULL!"
msg2:   dc.b    "unit ok",10,"line2"
msg2e:
        even
