; ===========================================================================
; runtime/ops/mach.s — S3.2 machine loop, guest time base and events
; Owner: W8-L (mach).  Spec: docs/S3_PLAN.md §1.5, §4 "S3.2";
; contract: docs/CONTRACTS.md "Machine loop (S3.2)".
;
; Time base: CIA #2 timer A (phi2, continuous, latch $FFFF) cascades into
; timer B (counts A's underflows): a free-running 32-bit phi2 counter,
; C = ~(TB:TA).  Port A (VIC bank) and the CIA #2 ICR mask are never
; written.  Guest E clock = C >> s (s = mach_s, latched by mach_init);
; one guest frame = MACH_FRAME_E E ticks = MACH_FRAME_E << s phi2.
;
; Events (S3.2: VERTB only): CIA #1 timer A, one-shot, is armed toward the
; next frame boundary (at most $FFFF phi2 at a time).  Its IRQ, taken
; through the RAM vector at $FFFE (mach_run runs with $01 = $35), costs the
; interpreter 0 cycles per instruction: mach_irq sets ev_pend and ORs $80
; into st_halt, and emu_run returns at the next instruction boundary.
; mach_run then services: every frame boundary the counter has passed adds
; one to mach_frames and sets INTREQ bit 5 (VERTB) in the chip shadow; the
; one-shot is re-armed.  No interrupt delivery (S3.3), no CIA-A/B guest
; timers (S3.4), no copper or refresh (S3.5).  HALT_STOP goes back to the
; caller like any halt (idle skip waits for S3.3).
;
; Entries (JSR; clobber A/X/Y):
;   mach_init  start the time base, frame 0 begins now; mach_frames,
;              ev_pend := 0.  Once per guest boot, with I/O mapped in.
;              Nothing else may reprogram the CIA #2 timers afterwards.
;   mach_run   emu_run's drop-in: same entry state, same exit contract
;              (st_halt/st_reason as emu_run leaves them; a halt racing an
;              event returns st_halt = $01 with ev_pend kept).  Runs
;              mach_init first if it never ran.  Inside: $01 = $35, RAM
;              vectors installed, CIA1 timer-A IRQ unmasked, cli.  On exit:
;              CIA1 ICR masked and acknowledged, I = 1, $01 = the caller's.
;              The KERNAL's jiffy IRQ (CIA1 timer A) is not restored.
;   mach_vec   point the RAM NMI/IRQ vectors at mach_nmi/mach_irq
;   mach_beam  (chip.s) the beam now -> mach_bv (V 0-312) / mach_bh (H
;              0-226); keeps Y.  Formula: host/chipset_ref.MachTime.
;
; Host-visible state (runner.lbl / rc): mach_s (u8 setting, power-up 4),
; ev_pend and mach_frames (rc zp, s3), mach_on (0 until mach_init: beam
; reads keep the S3.1 stub values), mach_nmis (NMIs taken, wraps: the only
; NMI handler, c2v's fill included), mach_next/mach_now (test visibility).
;
; Vector invariant (docs/CONTRACTS.md): whenever $01 exposes the RAM
; vectors, $FFFA-$FFFF holds a valid handler.  They are written only while
; the ROM vectors are live, or rewritten with identical bytes: mach_vec
; runs before mach_run changes $01, and c2v_fill stores mach_nmi (the same
; bytes inside mach_run, the ROM live outside it).
;
; Link layout: linked after chip.s (Makefile), so no other module's code
; moves.
; ===========================================================================

.include "../macros.inc"

.export mach_init, mach_run, mach_vec, mach_beam, mach_irq, mach_nmi
.export mach_rdc
.export mach_s, mach_on, mach_nmis, mach_bv, mach_bh, mach_next, mach_now
.import emu_run, chip_shd

MACH_FRAME_E  = 14188   ; @value E ticks per guest frame (709379 Hz / 50)
MACH_S_DEF    = 4       ; @value power-up E-clock shift
MACH_S_MAX    = 16      ; @value largest shift (a frame stays < 2^31 phi2)
MACH_EV_TIMER = $01     ; @value ev_pend bit: CIA1 timer A fired
MACH_INTREQR  = $1E     ; @value chip_shd index of INTREQR (big-endian word)
MACH_VERTB    = $20     ; @value INTREQ bit 5, in the low byte
MACH_LINE_CCK = 227     ; @value colour clocks per line

.segment "DATA"
mach_s:    .byte MACH_S_DEF     ; E-clock shift, read by mach_init (0-16)

.segment "BSS"
mach_on:   .res 1       ; nonzero once mach_init ran
mach_sl:   .res 1       ; mach_s as latched by mach_init
mach_port: .res 1       ; the caller's $01
mach_ev:   .res 1       ; ev_pend as taken by the last service pass
mach_nmis: .res 1       ; NMIs taken by mach_nmi (wraps)
mach_flen: .res 4       ; frame length, phi2 cycles
mach_next: .res 4       ; counter value of the next frame boundary
mach_now:  .res 4       ; counter snapshot (mach_rdc)
mach_c:    .res 4       ; scratch: raw CIA reads, then 5 x E tick
mach_t:    .res 4       ; scratch
mach_bv:   .res 2       ; beam V (LE)
mach_bh:   .res 1       ; beam H

.segment "CODE"

; ---------------------------------------------------------------------------
; mach_init — start the CIA #2 cascade; frame 0 starts now.
; ---------------------------------------------------------------------------
.proc mach_init
        lda mach_s
        cmp #MACH_S_MAX + 1
        bcc :+
        lda #MACH_S_MAX
:       sta mach_sl
        lda #<MACH_FRAME_E
        sta mach_flen
        lda #>MACH_FRAME_E
        sta mach_flen+1
        lda #0
        sta mach_flen+2
        sta mach_flen+3
        ldx mach_sl             ; frame length = 14188 << s
        beq shifted
shl:    asl mach_flen
        rol mach_flen+1
        rol mach_flen+2
        rol mach_flen+3
        dex
        bne shl
shifted:
        lda CIA2_CRA
        and #$C0
        sta CIA2_CRA            ; timer A stopped (TOD 50/60 Hz, SP kept)
        lda CIA2_CRB
        and #$80
        sta CIA2_CRB            ; timer B stopped (TOD alarm select kept)
        lda #$FF
        sta CIA2_TALO
        sta CIA2_TAHI
        sta CIA2_TBLO
        sta CIA2_TBHI
        lda CIA2_CRB
        and #$80
        ora #$51                ; B: counts A underflows, force load, start
        sta CIA2_CRB
        lda CIA2_CRA
        and #$C0
        ora #$11                ; A: phi2, continuous, force load, start
        sta CIA2_CRA
        jsr mach_rdc
        clc                     ; next boundary = now + one frame
        lda mach_now
        adc mach_flen
        sta mach_next
        lda mach_now+1
        adc mach_flen+1
        sta mach_next+1
        lda mach_now+2
        adc mach_flen+2
        sta mach_next+2
        lda mach_now+3
        adc mach_flen+3
        sta mach_next+3
        lda #0
        sta mach_frames
        sta mach_frames+1
        sta mach_frames+2
        sta mach_frames+3
        sta ev_pend
        lda #1
        sta mach_on
        rts
.endproc

; ---------------------------------------------------------------------------
; mach_vec — the RAM vectors (a store under ROM lands in RAM whatever $01).
; RESET ($FFFC) is never taken through RAM: a reset maps the KERNAL in.
; ---------------------------------------------------------------------------
.proc mach_vec
        lda #<mach_nmi
        sta mach_vnmi
        lda #>mach_nmi
        sta mach_vnmi+1
        lda #<mach_irq
        sta mach_virq
        lda #>mach_irq
        sta mach_virq+1
        rts
.endproc

; ---------------------------------------------------------------------------
; mach_run — see the header.  The IRQ is enabled only between the cli and
; the sei around emu_run and the service pass.
; ---------------------------------------------------------------------------
.proc mach_run
        sei
        jsr mach_vec            ; vectors first: $35 exposes them to an NMI
        lda CPU_PORT
        sta mach_port
        and #$F8
        ora #$05                ; $35: I/O in, BASIC and KERNAL out
        sta CPU_PORT
        lda mach_on
        bne on
        jsr mach_init
on:     lda ev_pend             ; entry: take a pending event (a halt that
        sta mach_ev             ; raced one left it); the pass below does
        lda #0                  ; its work
        sta ev_pend
        lda #$7F                ; CIA1: every source masked (the KERNAL's
        sta CIA1_ICR            ; jiffy IRQ too) ...
        lda CIA1_ICR            ; ... and acknowledged
        jsr mach_svc            ; catch up with guest time, arm timer A
        lda #$81
        sta CIA1_ICR            ; timer A -> /IRQ
loop:   cli
        jsr emu_run
        sei                     ; the RMW below must not race mach_irq's ora
        lda st_halt
        and #$7F
        sta st_halt
        bne exit
        lda ev_pend             ; only an event: take the flags (still sei)
        sta mach_ev
        lda #0
        sta ev_pend
        cli
        jsr mach_svc
        jmp loop
exit:   lda #$7F                ; mask every CIA1 source
        sta CIA1_ICR
        lda CIA1_ICR            ; and acknowledge
        lda mach_port
        sta CPU_PORT
        rts
.endproc

; ---------------------------------------------------------------------------
; mach_svc — count every frame boundary the counter has passed (VERTB),
; then arm the CIA1 one-shot for min(next - now, $FFFF) phi2.  Frames are
; counted, not skipped (one loop pass, ~85 cycles, per missed frame).
; Aliasing: after service next - now is in (0, flen], so next - now > flen
; (unsigned) means now - next wrapped past 2^31 (an absence of 2^31 phi2
; or more, ~36 min): the lost frames cannot be counted, so the pass posts
; one VERTB and restarts the frame at now (next = now + flen).
; ---------------------------------------------------------------------------
.proc mach_svc
        jsr mach_rdc
chk:    sec                     ; t = next - now
        lda mach_next
        sbc mach_now
        sta mach_t
        lda mach_next+1
        sbc mach_now+1
        sta mach_t+1
        lda mach_next+2
        sbc mach_now+2
        sta mach_t+2
        lda mach_next+3
        sbc mach_now+3
        sta mach_t+3
        bmi due                 ; t < 0: the boundary has passed
        ora mach_t+2
        ora mach_t+1
        ora mach_t
        bne arm
due:    jsr post
        clc
        lda mach_next
        adc mach_flen
        sta mach_next
        lda mach_next+1
        adc mach_flen+1
        sta mach_next+1
        lda mach_next+2
        adc mach_flen+2
        sta mach_next+2
        lda mach_next+3
        adc mach_flen+3
        sta mach_next+3
        jmp chk
arm:    lda mach_flen           ; t > flen: aliased (see above)
        cmp mach_t
        lda mach_flen+1
        sbc mach_t+1
        lda mach_flen+2
        sbc mach_t+2
        lda mach_flen+3
        sbc mach_t+3
        bcs inrange
        jsr post                ; resync: one VERTB, next = now + flen
        clc
        lda mach_now
        adc mach_flen
        sta mach_next
        lda mach_now+1
        adc mach_flen+1
        sta mach_next+1
        lda mach_now+2
        adc mach_flen+2
        sta mach_next+2
        lda mach_now+3
        adc mach_flen+3
        sta mach_next+3
        ldx #3
cpfl:   lda mach_flen,x
        sta mach_t,x
        dex
        bpl cpfl
inrange:
        lda mach_t+3
        ora mach_t+2
        beq short
        lda #$FF                ; farther than $FFFF: wake up on the way
        sta mach_t
        sta mach_t+1
short:  lda CIA1_CRA
        and #$C0
        sta CIA1_CRA            ; timer A stopped (TOD 50/60 Hz, SP kept)
        lda mach_t
        sta CIA1_TALO
        lda mach_t+1
        sta CIA1_TAHI
        lda CIA1_CRA
        and #$C0
        ora #$19                ; one-shot, force load, start (phi2)
        sta CIA1_CRA
        rts

post:   inc mach_frames         ; one VERTB
        bne :+
        inc mach_frames+1
        bne :+
        inc mach_frames+2
        bne :+
        inc mach_frames+3
:       lda chip_shd+MACH_INTREQR+1
        ora #MACH_VERTB
        sta chip_shd+MACH_INTREQR+1
        rts
.endproc

; ---------------------------------------------------------------------------
; mach_rdc — mach_now := the 32-bit counter C = ~(TB:TA), one consistent
; snapshot: TA's high byte is read around its low byte, TB around TA, and
; the read repeats if either moved.  Clobbers A only.
; ---------------------------------------------------------------------------
.proc mach_rdc
rd:     lda CIA2_TBLO
        sta mach_c+2
        lda CIA2_TBHI
        sta mach_c+3
        lda CIA2_TAHI
        sta mach_c+1
        lda CIA2_TALO
        sta mach_c
        lda CIA2_TAHI
        cmp mach_c+1
        bne rd                  ; TA's low byte wrapped in between
        lda CIA2_TBLO
        cmp mach_c+2
        bne rd                  ; TA underflowed: TB moved
        lda mach_c
        eor #$FF
        sta mach_now
        lda mach_c+1
        eor #$FF
        sta mach_now+1
        lda mach_c+2
        eor #$FF
        sta mach_now+2
        lda mach_c+3
        eor #$FF
        sta mach_now+3
        rts
.endproc

; ---------------------------------------------------------------------------
; mach_beam — t = phi2 cycles into the current frame (clamped to its last
; E tick when the boundary has passed but not been serviced yet), E tick
; e = t >> s, colour clock cc = 5 e, V = cc / 227, H = cc mod 227.
; Out: mach_bv, mach_bh.  Clobbers A and X; Y untouched (chip.s ABI).
; ---------------------------------------------------------------------------
.proc mach_beam
        jsr mach_rdc
        sec                     ; t = now - next + flen
        lda mach_now
        sbc mach_next
        sta mach_t
        lda mach_now+1
        sbc mach_next+1
        sta mach_t+1
        lda mach_now+2
        sbc mach_next+2
        sta mach_t+2
        lda mach_now+3
        sbc mach_next+3
        sta mach_t+3
        clc
        lda mach_t
        adc mach_flen
        sta mach_t
        lda mach_t+1
        adc mach_flen+1
        sta mach_t+1
        lda mach_t+2
        adc mach_flen+2
        sta mach_t+2
        lda mach_t+3
        adc mach_flen+3
        sta mach_t+3
        lda mach_t              ; t >= flen (unsigned)?
        cmp mach_flen
        lda mach_t+1
        sbc mach_flen+1
        lda mach_t+2
        sbc mach_flen+2
        lda mach_t+3
        sbc mach_flen+3
        bcc inframe
        lda #<(MACH_FRAME_E - 1)
        sta mach_t
        lda #>(MACH_FRAME_E - 1)
        sta mach_t+1
        bne pos                 ; always
inframe:
        ldx mach_sl             ; beam: t >> s
        beq pos
shr:    lsr mach_t+3
        ror mach_t+2
        ror mach_t+1
        ror mach_t
        dex
        bne shr
pos:    lda mach_t              ; cc = 4 e + e (17 bits)
        sta mach_c
        lda mach_t+1
        sta mach_c+1
        lda #0
        sta mach_c+2
        asl mach_c
        rol mach_c+1
        rol mach_c+2
        asl mach_c
        rol mach_c+1
        rol mach_c+2
        clc
        lda mach_c
        adc mach_t
        sta mach_c
        lda mach_c+1
        adc mach_t+1
        sta mach_c+1
        lda mach_c+2
        adc #0                  ; 0 or 1: the remainder's start value
        ldx #16                 ; divmod(cc, 227), 16 shift-subtract steps
div:    asl mach_c
        rol mach_c+1
        rol a
        bcs sub                 ; 9-bit remainder: >= 256 > 227
        cmp #MACH_LINE_CCK
        bcc nosub
sub:    sbc #MACH_LINE_CCK      ; C = 1 on both paths
        inc mach_c              ; quotient bit
nosub:  dex
        bne div
        sta mach_bh
        lda mach_c
        sta mach_bv
        lda mach_c+1
        sta mach_bv+1
        rts
.endproc

; ---------------------------------------------------------------------------
; mach_irq — the RAM IRQ vector while mach_run runs ($01 = $35).  ~40
; cycles with the 6502's IRQ entry; touches A (saved), ev_pend, st_halt and
; the CIA1 ICR only.
; ---------------------------------------------------------------------------
.proc mach_irq
        pha
        lda CIA1_ICR            ; acknowledge: releases /IRQ
        bpl done                ; no enabled CIA1 source (a BRK): nothing
        lda ev_pend
        ora #MACH_EV_TIMER
        sta ev_pend
        lda st_halt
        ora #$80
        sta st_halt
done:   pla
        rti
.endproc

; ---------------------------------------------------------------------------
; mach_nmi — the one RAM NMI handler (docs/C2V.md "Handover to S3.2"; c2v's
; $01=$34 fill uses it too): valid under any $01 (it runs from RAM below
; $A000), saves A and $01, maps I/O in, acknowledges CIA2 so /NMI is never
; left latched, counts, rti.  A RESTORE press or a CIA2 NMI is dropped, not
; delivered: no S3.2 source uses NMI.  MACH_MUT_NMIACK (tests/run_c2v.py
; teeth only) drops the acknowledge.
; ---------------------------------------------------------------------------
.proc mach_nmi
        pha
        lda CPU_PORT
        pha
        and #$F8                ; NMI: I/O in
        ora #$05
        sta CPU_PORT
.ifndef MACH_MUT_NMIACK
        lda CIA2_ICR            ; acknowledge: releases /NMI
.endif
        inc mach_nmis
        pla
        sta CPU_PORT
        pla
        rti
.endproc
.assert mach_nmi < $A000 .and mach_irq < $A000, lderror, "mach vectors must point at RAM visible under $01 = $34"
