; W5-H heap limits: the largest request that fits ($6FFFF0: 7 MB minus the
; control block and one header) fills the heap exactly, one byte more never
; fits; the last 8 bytes of the heap are a valid (cleared) block.
        movea.l 4.w,a6
        move.l  #$6FFFF0,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d2               ; = H+$10, top = $800000
        moveq   #1,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d3               ; 0: exhausted
        movea.l d2,a1
        move.l  #$6FFFF0,d0
        jsr     -210(a6)            ; all back to the wilderness
        move.l  #$6FFFF1,d0         ; rounds to $6FFFF8: 8 bytes too many
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d4               ; 0
        move.l  #$6FFFE0,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d5               ; = H+$10, leaves exactly 16 bytes
        moveq   #1,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d6               ; = $7FFFF8, the last heap longwords
        movea.l d0,a0
        moveq   #-1,d1
        move.l  d1,(a0)+
        move.l  d1,(a0)
        movea.l d6,a1
        moveq   #1,d0
        jsr     -210(a6)            ; top drops back to $7FFFF0
        moveq   #5,d0
        move.l  #$10000,d1          ; MEMF_CLEAR
        jsr     -198(a6)
        movea.l d0,a2               ; = $7FFFF8 again
        movea.l d0,a0
        move.l  (a0)+,d7
        or.l    (a0),d7             ; 0: both longwords cleared
        moveq   #1,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a3               ; 0: full again
        move.l  $100004,a4          ; 0 rejected
        rts
