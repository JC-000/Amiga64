; StackSwap (exec V37): swaps A7 with stk_Pointer and tc_SPLower/tc_SPUpper
; with stk_Lower/stk_Upper, returning on the new stack; a second call
; swaps back.
        movea.l 4.w,a6
        move.l  #256,d0
        moveq   #0,d1
        jsr     -684(a6)            ; AllocVec(256) -> HEAP_BASE+4
        movea.l d0,a2               ; StackSwapStruct at a2
        lea     16(a2),a0
        move.l  a0,(a2)             ; stk_Lower   = a2+16
        lea     256(a2),a0
        move.l  a0,4(a2)            ; stk_Upper   = a2+256
        lea     248(a2),a0
        move.l  a0,8(a2)            ; stk_Pointer = a2+248
        move.l  #$CAFEBABE,248(a2)
        movea.l a7,a3               ; SP before the swap
        movea.l a2,a0
        jsr     -732(a6)            ; StackSwap
ret1:   move.l  a7,d2               ; = a2+248
        move.l  -4(a7),d0           ; exec leaves the return address at
        lea     ret1(pc),a0         ; stk_Pointer-4 (it returns via RTS on
        sub.l   a0,d0               ; the new stack)
        movea.l d0,a5               ; 0
        move.l  (a7),d3             ; $CAFEBABE
        move.l  8(a2),d4            ; old SP (= a3)
        suba.l  a1,a1
        jsr     -294(a6)            ; FindTask(NULL)
        movea.l d0,a0
        move.l  $3a(a0),d5          ; tc_SPLower = a2+16
        move.l  $3e(a0),d6          ; tc_SPUpper = a2+256
        movea.l (a2),a4             ; struct now holds the old tc_SPLower
        movea.l a2,a0
        jsr     -732(a6)            ; swap back
        move.l  a7,d7               ; = a3
        rts
