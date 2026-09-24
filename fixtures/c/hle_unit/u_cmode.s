; dos ChangeMode(CHANGE_FH, fh, EXCLUSIVE_LOCK) — what vc.lib's fopen "a"
; and "r+" call — under the RAMDISK sharing rules (m2-post task 2).
; FileHandle BPTRs: slot 0 = $300, slot 1 = $30B.
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        movea.l d0,a6
        lea     fname(pc),a0
        move.l  a0,d1
        move.l  #1004,d2
        jsr     -30(a6)             ; Open("cm", MODE_READWRITE)
        movea.l d0,a2               ; $300
        moveq   #1,d1
        move.l  a2,d2
        moveq   #-1,d3
        jsr     -450(a6)            ; ChangeMode(CHANGE_FH, fh, EXCLUSIVE)
        move.l  d0,d2               ; -1
        lea     fname(pc),a0
        move.l  a0,d1
        move.l  d2,-(a7)
        move.l  #1005,d2
        jsr     -30(a6)             ; Open("cm", MODE_OLDFILE): exclusive
        move.l  (a7)+,d2
        move.l  d0,d3               ; 0
        jsr     -132(a6)
        move.l  d0,d4               ; 202
        moveq   #0,d1
        move.l  d2,-(a7)
        move.l  a2,d2
        move.l  d3,-(a7)
        moveq   #-1,d3
        jsr     -450(a6)            ; ChangeMode(CHANGE_LOCK, ...): 209
        move.l  (a7)+,d3
        move.l  (a7)+,d2
        move.l  d0,d5               ; 0
        jsr     -132(a6)
        move.l  d0,d6               ; 209
        move.l  a2,d1
        jsr     -36(a6)             ; Close(fh)
        lea     fname(pc),a0
        move.l  a0,d1
        move.l  d2,-(a7)
        move.l  #1005,d2
        jsr     -30(a6)             ; Open("cm", OLD) -> slot 0 again
        movea.l d0,a2
        jsr     -30(a6)             ; Open("cm", OLD) -> slot 1 (shares)
        movea.l d0,a3               ; $30B
        move.l  (a7)+,d2
        moveq   #1,d1
        move.l  d2,-(a7)
        move.l  a2,d2
        move.l  d3,-(a7)
        moveq   #-1,d3
        jsr     -450(a6)            ; ChangeMode(fh0): fh1 shares -> 202
        move.l  (a7)+,d3
        move.l  (a7)+,d2
        move.l  d0,d7               ; 0
        jsr     -132(a6)
        movea.l d0,a4               ; 202
        move.l  a3,d1
        jsr     -36(a6)             ; Close(fh1)
        moveq   #1,d1
        move.l  d2,-(a7)
        move.l  a2,d2
        move.l  d3,-(a7)
        moveq   #-1,d3
        jsr     -450(a6)            ; ChangeMode(fh0): now alone -> -1
        move.l  (a7)+,d3
        move.l  (a7)+,d2
        movea.l d0,a5               ; -1
        move.l  a2,d1
        jsr     -36(a6)
        rts
dosname: dc.b   "dos.library",0
fname:  dc.b    "cm",0
        even
