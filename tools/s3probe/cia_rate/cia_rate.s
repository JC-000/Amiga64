; ---------------------------------------------------------------------------
; tools/s3probe/cia_rate/cia_rate.s -- W8-H (docs/S3_PLAN.md section 5 risk 5)
; Do the C64 CIAs count phi2 (~0.985 MHz) or a turbo-scaled clock?
;
; CIA2 timer A (phi2 source, continuous, latch $FFFF) is cascaded into timer
; B (counts A underflows), giving a free-running 32-bit down counter.  The
; program samples it, plus the CIA2 TOD, at the moment raster line RLINE is
; reached, at frame 0, STEP, 2*STEP ... (NSAMP samples).  The VIC raster
; runs in real time (50/60 Hz) whatever the CPU speed, so the elapsed count
; over STEP frames divided by STEP is the CIA tick rate per frame.
;
; Protocol (host driver: cia_rate.py): the program idles until `go` != 0,
; runs one measurement, sets `done` = $A5, and idles again.  The host
; changes the CPU speed while it idles, so a single upload serves every run.
; Interrupts stay disabled (SEI) for the whole probe.
;
; Record layout at `recs` (8 bytes each):
;   +0 TBhi +1 TBlo +2 TAlo +3 TAhi   (down counters; elapsed = start - now)
;   +4 TOD hours +5 minutes +6 seconds +7 tenths (BCD)
; ---------------------------------------------------------------------------

.export go, done, ready, tod50, recs, start, polls

RLINE = 100                     ; raster line to sync on (visible area, < 256)
STEP  = 50                      ; frames between samples
NSAMP = 6

D012       = $D012
CIA2_TALO  = $DD04
CIA2_TAHI  = $DD05
CIA2_TBLO  = $DD06
CIA2_TBHI  = $DD07
CIA2_TOD10 = $DD08
CIA2_TODS  = $DD09
CIA2_TODM  = $DD0A
CIA2_TODH  = $DD0B
CIA2_ICR   = $DD0D
CIA2_CRA   = $DD0E
CIA2_CRB   = $DD0F

.segment "LOADADDR"
    .word $0801

.segment "STARTUP"
    .word @next
    .word 10
    .byte $9E, "2061", 0
@next:
    .word 0

.segment "CODE"
.assert * = $080D, error, "BASIC stub must place start at 2061"
start:
    sei
    lda #0
    sta go
    sta done
    lda #$C3
    sta ready
idle:
    lda go
    beq idle
    lda #0
    sta go
    sta done
    sta polls
    sta polls+1
    sta polls+2
    sta polls+3

    lda #$7F
    sta CIA2_ICR                ; no CIA2 interrupt sources
    lda CIA2_ICR
    lda #0
    sta CIA2_CRA                ; both stopped
    sta CIA2_CRB
    lda #$FF
    sta CIA2_TALO
    sta CIA2_TAHI
    sta CIA2_TBLO
    sta CIA2_TBHI
    lda #$10                    ; force load A, stopped, continuous, phi2
    sta CIA2_CRA
    lda #$50                    ; force load B, count A underflows, TOD write mode
    sta CIA2_CRB
    lda tod50                   ; 0 = 60 Hz TOD input, 1 = 50 Hz
    beq :+
    lda #$80
:   sta todbit
    ora #$10
    sta CIA2_CRA                ; TOD divider selection (bit 7)
    lda #0
    sta CIA2_TODH               ; write H, M, S, tenths: TOD (re)starts
    sta CIA2_TODM
    sta CIA2_TODS
    sta CIA2_TOD10
    lda #$41                    ; start B (counts A underflows)
    sta CIA2_CRB
    lda todbit
    ora #$01                    ; start A
    sta CIA2_CRA

    ldy #0                      ; record offset
    lda #NSAMP
    sta nleft
    jsr sync                    ; frame 0: at raster line RLINE
sample_loop:
    jsr take
    dec nleft
    beq finished
    ldx #STEP
@frames:
    jsr sync                    ; one sync = exactly one frame
    dex
    bne @frames
    jmp sample_loop
finished:
    lda #$A5
    sta done
    jmp idle

; leave line RLINE if on it, then wait for its next arrival
sync:
:   jsr count
    lda D012
    cmp #RLINE
    beq :-
:   jsr count
    lda D012
    cmp #RLINE
    bne :-
    rts

; 32-bit poll counter: proves the CPU really ran at the requested speed
count:
    inc polls
    bne @x
    inc polls+1
    bne @x
    inc polls+2
    bne @x
    inc polls+3
@x: rts

; snapshot timers (retry on a TB tear) and TOD into recs[y..y+7]
; KNOWN LIMITATION: the tear check covers TB only.  A TA borrow between the
; TALO and TAHI reads can give a +-256-tick error in one sample (interval).
; The fix (TAHI/TALO/TAHI retry) is deliberately NOT applied, so that this
; code is exactly what produced the measured data (docs/HARDWARE.md).
take:
:   lda CIA2_TBHI
    sta b0
    lda CIA2_TBLO
    sta b1
    lda CIA2_TALO
    sta b2
    lda CIA2_TAHI
    sta b3
    lda CIA2_TBHI
    cmp b0
    bne :-
    lda CIA2_TBLO
    cmp b1
    bne :-
    lda CIA2_TODH               ; reading hours latches TOD
    sta recs+4,y
    lda CIA2_TODM
    sta recs+5,y
    lda CIA2_TODS
    sta recs+6,y
    lda CIA2_TOD10              ; reading tenths releases the latch
    sta recs+7,y
    lda b0
    sta recs+0,y
    lda b1
    sta recs+1,y
    lda b2
    sta recs+2,y
    lda b3
    sta recs+3,y
    tya
    clc
    adc #8
    tay
    rts

.segment "BSS"
go:     .res 1
done:   .res 1
ready:  .res 1
tod50:  .res 1
todbit: .res 1
nleft:  .res 1
polls:  .res 4
b0:     .res 1
b1:     .res 1
b2:     .res 1
b3:     .res 1
        .res 2
recs:   .res 8*NSAMP
