; tests/mturbo/mturbo.s -- W15 1351-at-turbo diagnostic stub (tests/run_mturbo.py).
;
; Test-only: assembled by the runner into its own area (ORG $C800-$CBFF, in
; buf_result; the U64 dispatcher page $CE00 is not touched), called
; through call_runner.  One entry; PRM+0 selects the mode.  Time is
; measured and waited in phi2 ticks with CIA1 (it counts phi2 at any
; CPU speed, docs/HARDWARE.md "CIA clock at turbo"):
;   timer A  continuous from $FFFF: the stopwatch (tastart / tastop)
;   timer B  one-shot: the waits (wait; polled on CRB bit 0, the START
;            bit the one-shot clears at underflow, so no ICR read race)
; Both timers stopped and their IRQ masks cleared at exit; $D031 :=
; P_EXIT at exit (0: the host reads results at 1 MHz; bit 7 set: no write).  $D031 is the
; Ultimate's turbo speed index (0 = 1 MHz .. 15 = top); it only acts with
; Turbo Control = "... Turbo Registers" and is ignored otherwise (and by
; VICE).
;
; Modes:
;  1 TIME   per rep: $D031 := P_IDX, wait 2000; stopwatch over P_N (lo)
;           loops of 32 x `lda P_ADDR` (P_ADDR = 0: the empty loop); P_SW
;           bit 7: $D031 := P_SW & $0F right after the stopwatch starts
;           (the switch latency).  OUT: reps x (TA lo, TA hi, ICR).
;  2 SAMPLE $D031 := P_IDX (bit 7 set: no write); P_N (u16) times: wait P_SPACE, POTX, POTY.
;           DATA: n x 2 bytes.
;  3 TRIALS P_N (u16) rounds over the P_M entries of TBL (k lo, k hi,
;           idx between, idx read): $D031 := between, wait P_SPACE, POTX/Y
;           (the "pre" pair); $D031 := read, wait k, POTX/Y.  DATA: 4 bytes
;           per trial (pre X, pre Y, X, Y).
;  4 WCHK   per rep: $D031 := P_IDX, wait 2000; stopwatch around one wait
;           of P_K ticks.  OUT as TIME.

PRM     = $CC00
TBL     = $CC80
OUT     = $CD00
DATA    = $C000
PTR     = $FB

P_MODE  = PRM+0
P_IDX   = PRM+1
P_N     = PRM+2         ; u16
P_REPS  = PRM+4
P_ADDR  = PRM+5         ; u16
P_SW    = PRM+7
P_SPACE = PRM+8         ; u16
P_M     = PRM+10
P_K     = PRM+11        ; u16
P_EXIT  = PRM+13

CNT     = PRM+$20       ; u16
RC      = PRM+$22
ENT     = PRM+$23
M4      = PRM+$24
KL      = PRM+$25
KH      = PRM+$26
NULLF   = PRM+$27

SETTLE  = 2000          ; phi2 ticks at the leg's index before each timed rep

.segment "CODE"
entry:  php
        sei
        lda #$03                ; CIA1 TA/TB IRQ masks off (bit 7 = 0: clear)
        sta $DC0D
        lda $DC0D
        lda #<DATA
        sta PTR
        lda #>DATA
        sta PTR+1
        lda P_MODE
        cmp #1
        bne :+
        jmp time
:       cmp #2
        bne :+
        jmp sample
:       cmp #3
        bne :+
        jmp trials
:       cmp #4
        bne exit
        jmp wchk

exit:   lda P_EXIT          ; bit 7 set: no $D031 write (M7's config-only path)
        bmi :+
        sta $D031
:
        lda $DC0E               ; stop both timers (TOD/SP bits kept)
        and #$C0
        sta $DC0E
        lda $DC0F
        and #$80
        sta $DC0F
        lda $DC0D
        plp
        rts

; ---- stopwatch (timer A) and wait (timer B) ----------------------------

tastart:
        lda #$FF
        sta $DC04
        sta $DC05
        lda $DC0D               ; clear a stale TA underflow
        lda $DC0E
        and #$C0
        ora #$11                ; force load, start, continuous, phi2
        sta $DC0E
        rts

tastop: lda $DC0E
        and #$C0
        sta $DC0E               ; stopped here
        rts

; wait A (lo) / X (hi) phi2 ticks on timer B one-shot; 0 returns at once
wait:   cpx #0
        bne :+
        cmp #0
        beq wdone
:       sta $DC06
        stx $DC07
        lda $DC0F
        and #$80
        ora #$19                ; force load, one-shot, start, phi2
        sta $DC0F
w:      lda $DC0F               ; START clears at the underflow
        lsr
        bcs w
wdone:  rts

settle: lda P_IDX
        sta $D031
        lda #<SETTLE
        ldx #>SETTLE
        jmp wait

; store TA (stopped) and the ICR's TA underflow flag at OUT + 3*RC
stamp:  lda RC
        asl
        adc RC
        tax
        lda $DC04
        sta OUT,x
        lda $DC05
        sta OUT+1,x
        lda $DC0D
        and #$01
        sta OUT+2,x
        rts

; ---- 1 TIME ------------------------------------------------------------

time:   lda P_ADDR
        ora P_ADDR+1
        sta NULLF
        ldx #0
        ldy #0
:       lda P_ADDR
        sta blk+1,x
        lda P_ADDR+1
        sta blk+2,x
        inx
        inx
        inx
        iny
        cpy #32
        bne :-
        lda #0
        sta RC
trep:   jsr settle
        ldy P_N
        jsr tastart
        bit P_SW
        bpl :+
        lda P_SW
        and #$0F
        sta $D031
:       lda NULLF
        beq nloop
tloop:
blk:    .repeat 32
        lda $FFFF
        .endrep
        dey
        bne tloop
        jmp tdone
nloop:  dey
        bne nloop
tdone:  jsr tastop
        jsr stamp
        inc RC
        lda RC
        cmp P_REPS
        beq :+
        jmp trep
:       jmp exit

; ---- 2 SAMPLE ----------------------------------------------------------

sample: lda P_IDX             ; bit 7 set: no $D031 write
        bmi :+
        sta $D031
:
        lda P_N
        sta CNT
        lda P_N+1
        sta CNT+1
sl:     lda P_SPACE
        ldx P_SPACE+1
        jsr wait
        ldy #0
        lda $D419
        sta (PTR),y
        iny
        lda $D41A
        sta (PTR),y
        lda PTR
        clc
        adc #2
        sta PTR
        bcc :+
        inc PTR+1
:       lda CNT
        bne :+
        dec CNT+1
:       dec CNT
        lda CNT
        ora CNT+1
        bne sl
        jmp exit

; ---- 3 TRIALS ----------------------------------------------------------

trials: lda P_N
        sta CNT
        lda P_N+1
        sta CNT+1
        lda P_M
        asl
        asl
        sta M4
tr:     lda #0
        sta ENT
te:     ldx ENT
        lda TBL+2,x             ; the CPU speed between reads
        sta $D031
        lda P_SPACE
        ldx P_SPACE+1
        jsr wait
        ldy #0
        lda $D419               ; the "pre" pair, at that speed
        sta (PTR),y
        iny
        lda $D41A
        sta (PTR),y
        ldx ENT
        lda TBL+0,x
        sta KL
        lda TBL+1,x
        sta KH
        lda TBL+3,x             ; the drop
        sta $D031
        lda KL
        ldx KH
        jsr wait
        ldy #2
        lda $D419
        sta (PTR),y
        iny
        lda $D41A
        sta (PTR),y
        lda PTR
        clc
        adc #4
        sta PTR
        bcc :+
        inc PTR+1
:       lda ENT
        clc
        adc #4
        sta ENT
        cmp M4
        bne te
        lda CNT
        bne :+
        dec CNT+1
:       dec CNT
        lda CNT
        ora CNT+1
        bne tr
        jmp exit

; ---- 4 WCHK ------------------------------------------------------------

wchk:   lda #0
        sta RC
wrep:   jsr settle
        jsr tastart
        lda P_K
        ldx P_K+1
        jsr wait
        jsr tastop
        jsr stamp
        inc RC
        lda RC
        cmp P_REPS
        bne wrep
        jmp exit
