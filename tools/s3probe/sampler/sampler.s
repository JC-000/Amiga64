; ---------------------------------------------------------------------------
; tools/s3probe/sampler/sampler.s -- W10-H: U64E "Ultimate Audio" sampler probe
;
; The host cannot reach the expansion port (docs/HARDWARE.md, harness gotcha
; 14), so every access to $DF00-$DFFF is made by the 6510.  This program is a
; tiny script interpreter: the host writes a script at SCRIPT (4-byte entries
; op, lo, hi, val), sets `go`, and the 6510 runs it, appending to the result
; buffer at RES.  `done` = $A5 and `res_end` mark completion.  One upload
; serves every test.
;
;   0 END                      finish
;   1 WR    [hilo] = val
;   2 RD    res += [hilo]
;   3 TIM   res += TBhi TBlo TAhi TAlo   (CIA2 32-bit down counter, phi2)
;   4 POLL  until [hilo] & val != 0, bounded (~12 * 2^16 iterations);
;           res += timeout-flag, raw byte, TBhi TBlo TAhi TAlo (at the exit)
;   5 TSTART  restart the CIA2 32-bit timer at $FFFFFFFF
;   6 DELAY   val * ~1.3 ms (at 1 MHz)
;   7 IRQ     val = 0: SEI, else CLI
;   8 MARK    res += val
;
; The IRQ handler (vector at $FFFE, KERNAL ROM banked out, CIA/VIC interrupt
; sources off, so only the cartridge IRQ line can enter it) reads $DF20 (the
; sampler's IRQ status), keeps counters, optionally acknowledges by writing
; ack_val to (ack_ptr), and sets I in the stacked P once irq_count reaches
; irq_limit (a bound for an unacknowledged level IRQ).
; ---------------------------------------------------------------------------

.export go, done, ready, res_end, irq_count, irq_spur, irq_first, irq_stfirst
.export irq_stlast, ack_en, ack_val, irq_limit, ack_ptr, start

SCRIPT = $1000
RES    = $1400

CIA2_TALO  = $DD04
CIA2_TAHI  = $DD05
CIA2_TBLO  = $DD06
CIA2_TBHI  = $DD07
CIA2_ICR   = $DD0D
CIA2_CRA   = $DD0E
CIA2_CRB   = $DD0F
SAMP_ST    = $DF20              ; any even sampler address reads IRQ status

.segment "ZEROPAGE"
sp:       .res 2
rp:       .res 2
ap:       .res 2
ack_ptr:  .res 2
t0:       .res 1
t1:       .res 1
t2:       .res 1
t3:       .res 1
op:       .res 1
val:      .res 1
cnt0:     .res 1
cnt1:     .res 1

.segment "LOADADDR"
    .word $0801

.segment "STARTUP"
    .word @next
    .word 10
    .byte $9E, "2061", 0
@next:
    .word 0

; read the 32-bit timer coherently into four bytes (retry on a tear of A or B)
.macro SNAP b0, b1, b2, b3
:   lda CIA2_TBHI
    sta b0
    lda CIA2_TBLO
    sta b1
    lda CIA2_TAHI
    sta b2
    lda CIA2_TALO
    sta b3
    lda CIA2_TAHI
    cmp b2
    bne :-
    lda CIA2_TBLO
    cmp b1
    bne :-
    lda CIA2_TBHI
    cmp b0
    bne :-
.endmacro

.segment "CODE"
.assert * = $080D, error, "BASIC stub must place start at 2061"
start:
    sei
    lda #$35
    sta $01                     ; I/O visible, KERNAL and BASIC ROM off
    lda #<irq
    sta $FFFE
    lda #>irq
    sta $FFFF
    lda #<nmi
    sta $FFFA
    lda #>nmi
    sta $FFFB
    lda #$7F
    sta $DC0D
    sta $DD0D
    lda $DC0D
    lda $DD0D
    lda #0
    sta $D01A                   ; no VIC interrupt sources
    lda #$FF
    sta $D019
    lda #0
    sta go
    sta done
    sta ack_en
    sta irq_limit
    lda #$C3
    sta ready
idle:
    sei
    lda go
    beq idle
    lda #0
    sta go
    sta done
    sta irq_count
    sta irq_count+1
    sta irq_spur
    sta irq_stfirst
    sta irq_stlast
    lda #<SCRIPT
    sta sp
    lda #>SCRIPT
    sta sp+1
    lda #<RES
    sta rp
    lda #>RES
    sta rp+1
next:
    ldy #0
    lda (sp),y
    sta op
    iny
    lda (sp),y
    sta ap
    iny
    lda (sp),y
    sta ap+1
    iny
    lda (sp),y
    sta val
    clc
    lda sp
    adc #4
    sta sp
    bcc :+
    inc sp+1
:   lda op
    bne :+
    jmp o_end
:   cmp #1
    bne :+
    jmp o_wr
:   cmp #2
    bne :+
    jmp o_rd
:   cmp #3
    bne :+
    jmp o_tim
:   cmp #4
    bne :+
    jmp o_poll
:   cmp #5
    bne :+
    jmp o_tstart
:   cmp #6
    bne :+
    jmp o_delay
:   cmp #7
    bne :+
    jmp o_irq
:   cmp #8
    bne :+
    jmp o_mark
:
    jmp next                    ; unknown op: skip

o_end:
    sei
    lda rp
    sta res_end
    lda rp+1
    sta res_end+1
    lda #$A5
    sta done
    jmp idle
o_wr:
    ldy #0
    lda val
    sta (ap),y
    jmp next
o_rd:
    ldy #0
    lda (ap),y
    jsr put
    jmp next
o_tim:
    SNAP t0, t1, t2, t3
    jsr put4
    jmp next
o_poll:
    lda #0
    sta cnt0
    sta cnt1
    sta cnt2
    sta t0                      ; t0 = timeout flag
    ldy #0
@lp:
    lda (ap),y
    sta t1                      ; raw byte
    and val
    bne @hit
    inc cnt0
    bne @lp
    inc cnt1
    bne @lp
    inc cnt2
    lda cnt2
    cmp #12                     ; 12 * 65536 iterations, ~11 s at 1 MHz
    bne @lp
    lda #1
    sta t0
@hit:
    SNAP t2, t3, cnt0, cnt1     ; timer into scratch (cnt no longer needed)
    lda t0
    jsr put
    lda t1
    jsr put
    lda t2
    jsr put
    lda t3
    jsr put
    lda cnt0
    jsr put
    lda cnt1
    jsr put
    jmp next
o_tstart:
    lda #0
    sta CIA2_CRA
    sta CIA2_CRB
    lda #$FF
    sta CIA2_TALO
    sta CIA2_TAHI
    sta CIA2_TBLO
    sta CIA2_TBHI
    lda #$10
    sta CIA2_CRA                ; force load A (stopped, continuous, phi2)
    lda #$50
    sta CIA2_CRB                ; force load B (counts A underflows)
    lda #$41
    sta CIA2_CRB
    lda #$01
    sta CIA2_CRA
    jmp next
o_delay:
    ldx val
@d1:
    ldy #255
@d2:
    dey
    bne @d2
    dex
    bne @d1
    jmp next
o_irq:
    lda val
    beq @s
    cli
    jmp next
@s: sei
    jmp next
o_mark:
    lda val
    jsr put
    jmp next

put4:
    lda t0
    jsr put
    lda t1
    jsr put
    lda t2
    jsr put
    lda t3
put:
    ldy #0
    sta (rp),y
    inc rp
    bne :+
    inc rp+1
:   rts

nmi:
    rti

irq:
    pha
    txa
    pha
    tya
    pha
    lda SAMP_ST
    sta irq_stlast
    beq @spur
    lda irq_count
    ora irq_count+1
    bne @notfirst
    lda irq_stlast
    sta irq_stfirst
    SNAP irq_first, irq_first+1, irq_first+2, irq_first+3
@notfirst:
    jmp @cont
@spur:
    inc irq_spur
@cont:
    lda ack_en
    beq @noack
    ldy #0
    lda ack_val
    sta (ack_ptr),y
@noack:
    inc irq_count
    bne :+
    inc irq_count+1
:   lda irq_count+1
    bne @limit
    lda irq_count
    cmp irq_limit
    bcc @out
@limit:
    tsx
    lda $0104,x
    ora #$04
    sta $0104,x                 ; stacked P: I set, so RTI does not re-enter
@out:
    pla
    tay
    pla
    tax
    pla
    rti

.segment "BSS"
go:         .res 1
done:       .res 1
ready:      .res 1
res_end:    .res 2
irq_count:  .res 2
irq_spur:   .res 1
irq_stfirst: .res 1
irq_stlast: .res 1
ack_en:     .res 1
ack_val:    .res 1
irq_limit:  .res 1
cnt2:       .res 1
irq_first:  .res 4
