; OpenLibrary: exact name match and the version check (lib_Version 40).
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #40,d0
        jsr     -552(a6)            ; OpenLibrary("dos.library", 40)
        move.l  d0,d2               ; = DOS_BASE
        lea     dosname(pc),a1
        moveq   #41,d0
        jsr     -552(a6)            ; version 41 > 40
        move.l  d0,d3               ; 0
        lea     dosname(pc),a1
        move.l  #$10028,d0          ; high word set: > 40
        jsr     -552(a6)
        move.l  d0,d4               ; 0
        lea     fake(pc),a1
        moveq   #0,d0
        jsr     -552(a6)            ; "dos.fake": a prefix is not a match
        move.l  d0,d5               ; 0
        lea     longer(pc),a1
        moveq   #0,d0
        jsr     -552(a6)            ; "dos.libraryX"
        move.l  d0,d6               ; 0
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)            ; version 0 = any
        move.l  d0,d7               ; = DOS_BASE
        movea.l 4.w,a0
        move.w  20(a0),a2           ; exec lib_Version (sign-extended) = 40
        movea.l d2,a0
        move.w  20(a0),a3           ; dos lib_Version = 40
        rts
dosname: dc.b   "dos.library",0
fake:   dc.b    "dos.fake",0
longer: dc.b    "dos.libraryX",0
        even
