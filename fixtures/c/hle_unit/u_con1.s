; W5-K console input (docs/HLE_CON.md): line discipline through dos Read on
; the console handles, fed by the host's scripted queue (tests/run_con.py
; stages "hello world\r" EOF "z\rout\r").  Each rd stores 5 longs at RES:
; D0, buf[0..3], buf[4..7], the last 4 delivered bytes (0 if D0 < 4) and a
; flag that D1-D3 came back unchanged.  Covers: remainder kept across
; Reads, EOF key on an empty line -> 0 with input still queued behind it,
; D3 = 0 / negative / >= 16 MB, Read on console OUT (kind 0), and the
; queue running dry in scripted mode -> 0.
RES     equ     $100000
BUF     equ     $100400
        movea.l 4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)            ; OpenLibrary
        movea.l d0,a6
        jsr     -54(a6)             ; Input
        movea.l d0,a3
        jsr     -60(a6)             ; Output
        movea.l d0,a2
        lea     RES,a4
        moveq   #5,d3
        bsr     rd                  ; 5 "hello"
        bsr     rd                  ; 5 " worl"
        bsr     rd                  ; 2 "d\n"
        bsr     rd                  ; 0 (EOF key)
        moveq   #0,d3
        bsr     rd                  ; 0 (D3 = 0: nothing consumed)
        moveq   #-1,d3
        bsr     rd                  ; -1
        move.l  #$01000000,d3
        bsr     rd                  ; -1
        moveq   #5,d3
        bsr     rd                  ; 2 "z\n"
        exg     a2,a3
        moveq   #8,d3
        bsr     rd                  ; 4 "out\n" via Output()
        exg     a2,a3
        bsr     rd                  ; 0 (queue dry)
        rts

rd:     lea     BUF,a0
        move.l  #$2E2E2E2E,(a0)
        move.l  #$2E2E2E2E,4(a0)
        move.l  a3,d1
        move.l  a0,d2
        move.l  d3,d4
        move.l  #$55,d0             ; D0 must be overwritten
        jsr     -42(a6)             ; Read
        move.l  d0,(a4)+
        lea     BUF,a0
        move.l  (a0),(a4)+
        move.l  4(a0),(a4)+
        moveq   #0,d5
        cmp.l   #4,d0
        blt.s   .short
        move.l  -4(a0,d0.l),d5
.short: move.l  d5,(a4)+
        moveq   #0,d5
        cmp.l   a3,d1
        bne.s   .bad
        cmp.l   #BUF,d2
        bne.s   .bad
        cmp.l   d4,d3
        bne.s   .bad
        moveq   #1,d5
.bad:   move.l  d5,(a4)+
        rts
dosname: dc.b   "dos.library",0
        even
