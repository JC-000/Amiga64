; W5-H stale and forged frees (adversarial review poc T1, T2).
; T1: A, B = AllocMem(16); free B, free A (both drop into the wilderness);
; C = AllocMem(64) covers both; B's old header is inside C (left unwritten)
; and a stale FreeMem(B, 16) must be rejected: its tag was scrubbed when B
; dropped into the wilderness.  C stays intact, the next block goes above C.
; T2: a forged header inside a live block (size 24, USED clear, tag $5A)
; must be rejected (USED bit).
        movea.l 4.w,a6
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a2               ; A = H+$10
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a3               ; B = H+$28
        movea.l a3,a1
        moveq   #16,d0
        jsr     -210(a6)            ; free B -> wilderness
        movea.l a2,a1
        moveq   #16,d0
        jsr     -210(a6)            ; free A -> wilderness
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a4               ; C = H+$10 (B's old header at C+16)
        movea.l d0,a0
        moveq   #0,d1
.fill:  move.l  #$C0DE0000,d0       ; C[i] = $C0DE0000 + i, i != 4, 5
        add.l   d1,d0
        cmpi.w  #4,d1
        beq.s   .skip
        cmpi.w  #5,d1
        beq.s   .skip
        move.l  d0,(a0)
.skip:  addq.l  #4,a0
        addq.w  #1,d1
        cmpi.w  #16,d1
        bne.s   .fill
        movea.l a3,a1
        moveq   #16,d0
        jsr     -210(a6)            ; stale FreeMem(B): rejected
        move.l  $100004,d2          ; = 1
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d3               ; = H+$58: above C, not inside it
        movea.l a4,a0
        moveq   #0,d4               ; count of changed C longwords: 0
        moveq   #0,d1
.chk:   move.l  #$C0DE0000,d0
        add.l   d1,d0
        cmpi.w  #4,d1
        beq.s   .next
        cmpi.w  #5,d1
        beq.s   .next
        cmp.l   (a0),d0
        beq.s   .next
        addq.l  #1,d4
.next:  addq.l  #4,a0
        addq.w  #1,d1
        cmpi.w  #16,d1
        bne.s   .chk
        moveq   #64,d0
        moveq   #0,d1
        jsr     -198(a6)
        movea.l d0,a5               ; X = H+$70
        move.l  #24,16(a5)          ; forged: size 24, USED clear
        move.l  #$5A000000,20(a5)   ;         tag $5A, owner 0
        lea     24(a5),a1
        moveq   #16,d0
        jsr     -210(a6)            ; FreeMem(X+24, 16): rejected
        move.l  $100004,d5          ; = 2
        moveq   #16,d0
        moveq   #0,d1
        jsr     -198(a6)
        move.l  d0,d6               ; = H+$B8: above X, not inside it
        move.l  $100000,d7          ; free-list head = 0
        rts
