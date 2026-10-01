; ===========================================================================
; runtime/ops/mach.s — S3.2 machine loop, guest time base and events;
; S3.3 event seam and interrupt delivery
; Owner: W8-L (mach), W9-I (irq).  Spec: docs/S3_PLAN.md §1.5, §4 "S3.2",
; "S3.3"; contracts: docs/CONTRACTS.md "Machine loop (S3.2)", "Event seam
; (S3.3/S3.4)", "Interrupt delivery (S3.3)".
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
; one-shot is re-armed.  S3.3 (W9-I) adds the event seam (client slots
; 1-7 in guest E time, the slot-0 VERTB hook, mach_post/mach_hold; contract
; "Event seam (S3.3/S3.4)") and, in exception mode, interrupt delivery
; after every pass and the STOP wait (contract "Interrupt delivery
; (S3.3)").  No CIA-A/B guest timers here (cia.s, S3.4), no copper or
; refresh (S3.5).
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
; W9-I: the event seam (docs/CONTRACTS.md "Event seam (S3.3/S3.4)")
.export mach_etime, mach_arm, mach_disarm, mach_post, mach_kick
.export mach_et, mach_at, mach_cbv, mach_hold, mach_am, mach_dl, mach_chi
.export mach_in, mach_insv
.export MACH_NSLOT
; W9-I: S3.3 interrupt delivery (docs/CONTRACTS.md "Interrupt delivery (S3.3)")
.export mach_lvl, mach_ichk, mach_nirq, mach_stp
.import exc_group2
.importzp HALT_STOP

MACH_FRAME_E  = 14188   ; @value E ticks per guest frame (709379 Hz / 50)
MACH_S_DEF    = 4       ; @value power-up E-clock shift
MACH_S_MAX    = 16      ; @value largest shift (a frame stays < 2^31 phi2)
MACH_EV_TIMER = $01     ; @value ev_pend bit: CIA1 timer A fired
MACH_INTREQR  = $1E     ; @value chip_shd index of INTREQR (big-endian word)
MACH_INTENAR  = $1C     ; @value chip_shd index of INTENAR (big-endian word)
MACH_LINE_CCK = 227     ; @value colour clocks per line
MACH_NSLOT    = 8       ; @value event slots: 0 = VERTB (mach), 1-7 clients
MACH_EV_KICK  = $02     ; @value ev_pend bit: a trap-context arm/post wants a pass
MACH_NBIT     = 15      ; @value INTREQ bits 0-14
MACH_VERTB_BIT = 5      ; @value INTREQ bit number of VERTB
MACH_PRESET    = $40     ; @value st_halt preset while mach_dlv raises
MACH_AUTOVEC  = 24      ; @value autovector base: vector 24 + level
MACH_STOPCODE = $04     ; @value HALT_STOP (core.s; asserted at link)
MACH_POLL     = $0400   ; @value phi2 between passes while an interrupt is pending
MACH_MINW     = $0800   ; @value least one-shot wait after a pass that did client work

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
; --- W9-I event seam ---
mach_et:   .res 4       ; mach_etime out: guest E time now (u32 LE)
mach_at:   .res 4       ; mach_arm in: absolute E deadline (u32 LE)
mach_dl:   .res 4 * MACH_NSLOT  ; per-slot deadline (slot 0 unused)
mach_cbv:  .res 2 * MACH_NSLOT  ; per-slot callback (LE; high byte 0 = none)
mach_hold: .res 2       ; INTREQ bits a level source holds (LE): ORed back
                        ; after every INTREQ write (chip.s)
mach_am:   .res 1       ; armed slots, bit n = slot n (bit 0 never set)
mach_chi:  .res 2       ; counter bits 32-47 (wraps counted by mach_rdc)
mach_ctop: .res 1       ; counter bits 24-31 at the last mach_rdc
mach_eh:   .res 2       ; scratch: E-time shift, bits 32-47
mach_in:   .res 1       ; nonzero while mach_run runs the guest
mach_insv: .res 1       ; nonzero while a service pass runs
mach_sx:   .res 1       ; scratch: slot being serviced
mach_d:    .res 4       ; scratch: deadline - now
mach_fr:   .res 2       ; scratch: phi2 into the current E tick
mach_jv:   .res 2       ; callback jump vector
mach_w:    .res 2       ; scratch: the one-shot wait being chosen (phi2)
; --- W9-I interrupt delivery ---
mach_stp:  .res 1       ; nonzero while the guest CPU is stopped (STOP)
mach_lv:   .res 1       ; level being delivered
mach_iq:   .res 1       ; scratch: mach_ichk's level
mach_act:  .res 1       ; scratch: mach_lvl / mach_wk
mach_wf:   .res 1       ; this pass fired/posted/leaves a delivery (MACH_MINW)
mach_es:   .res 4       ; mach_slots' E snapshot (callbacks move mach_et)
mach_nirq: .res 4       ; interrupts delivered (u32 LE, wraps; tests)

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
        sta mach_am             ; W9-I: every slot disarmed (callbacks kept)
        sta mach_chi
        sta mach_chi+1
        lda mach_now+3          ; the restart is not a wrap
        sta mach_ctop
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
        sta mach_stp            ; W9-I: the CPU runs
        lda #$7F                ; CIA1: every source masked (the KERNAL's
        sta CIA1_ICR            ; jiffy IRQ too) ...
        lda CIA1_ICR            ; ... and acknowledged
        jsr mach_svc            ; catch up with guest time, arm timer A
        lda #$81
        sta CIA1_ICR            ; timer A -> /IRQ
        sta mach_in             ; W9-I: trap-context arms/posts kick from here
        jmp deliv               ; W9-I: an interrupt may be pending already
loop:   cli
        jsr emu_run
        sei                     ; the RMW below must not race mach_irq's ora
        lda st_halt
        and #$7F
        sta st_halt
        bne halt
event:
        lda ev_pend             ; only an event: take the flags (still sei)
        sta mach_ev
        lda #0
        sta ev_pend
        cli
        jsr mach_svc
deliv:  jsr mach_dlv            ; W9-I: S3.3 delivery after every pass
        bcs exit                ; the raise halted (odd vector)
        lda mach_stp
        beq loop
        jsr mach_wk             ; stopped: can anything still wake it?
        bcs wait
        sei
        lda #1                  ; no: HALT_STOP back to the caller
        sta st_halt             ; (st_reason is still HALT_STOP)
        bne exit                ; always
wait:   cli                     ; idle until the next event (no fast-forward)
:       lda ev_pend
        beq :-
        sei
        lda st_halt             ; mach_irq's bit 7 (no emu_run ran)
        and #$7F
        sta st_halt
        jmp event
halt:   cmp #1                  ; W9-I: STOP in exception mode stops the CPU
        bne exit
        lda st_reason
        cmp #MACH_STOPCODE
        bne exit
        lda st_exl
        beq exit
        sta mach_stp
        lda #0
        sta st_halt
        jmp event               ; take ev_pend (the one-shot may have fired
                                ; during the STOP), pass, then deliver
exit:   lda #$7F                ; mask every CIA1 source
        sta CIA1_ICR
        lda CIA1_ICR            ; and acknowledge
        lda #0                  ; W9-I: no kick outside mach_run
        sta mach_in
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
        lda #1                  ; W9-I: arms/posts in here need no kick
        sta mach_insv
        lda #0                  ; no client work yet (MACH_MINW)
        sta mach_wf
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
short:  lda mach_t                ; W9-I: the VERTB wait, then the slots
        sta mach_w
        lda mach_t+1
        sta mach_w+1
        jsr mach_slots          ; fire the due client slots
        jsr mach_wake           ; mach_w := min(VERTB, armed slots)
        jsr mach_pclamp         ; pending interrupt: poll every MACH_POLL
        jsr mach_wmin           ; fired/posted/delivering: >= MACH_MINW
        lda CIA1_CRA
        and #$C0
        sta CIA1_CRA            ; timer A stopped (TOD 50/60 Hz, SP kept)
        lda mach_w
        sta CIA1_TALO
        lda mach_w+1
        sta CIA1_TAHI
        lda CIA1_CRA
        and #$C0
        ora #$19                ; one-shot, force load, start (phi2)
        sta CIA1_CRA
        lda #0
        sta mach_insv
        rts

post:   inc mach_frames         ; one VERTB
        bne :+
        inc mach_frames+1
        bne :+
        inc mach_frames+2
        bne :+
        inc mach_frames+3
:       lda #MACH_VERTB_BIT     ; INTREQ bit 5 (W9-I: via the post path)
        jsr mach_pset
        ldx #0                  ; slot 0's hook (CIA-A TOD), if any
        jmp mach_cb
.endproc

; ===========================================================================
; W9-I event seam (docs/CONTRACTS.md "Event seam (S3.3/S3.4)").  Slots 1-7
; hold absolute E-time deadlines (u32, compared by signed difference, so
; they survive the E wrap).  E = (chi:C) >> s with chi counting C's wraps,
; i.e. the low 32 bits of a 48-bit phi2 counter shifted by s.  The service
; pass fires every armed slot whose deadline is <= its E snapshot (each
; slot once per pass: a deadline armed in the past by a callback fires at
; the next pass), then programs the CIA1 one-shot for the earliest of the
; next frame boundary and the armed deadlines.  A trap-context arm or post
; (inside emu_run, mach_in set, no pass running) kicks: ev_pend bit 1 and
; st_halt bit 7 under sei, exactly what mach_irq does, so emu_run returns
; at the next instruction boundary and the pass re-arms the one-shot.
; ===========================================================================

; mach_etime — mach_et := guest E time now (u32 LE); mach_now = the counter
; snapshot it came from.  Clobbers A and X; Y kept.
.proc mach_etime
        jsr mach_rdc
        ldx #3
cp:     lda mach_now,x
        sta mach_et,x
        dex
        bpl cp
        lda mach_chi
        sta mach_eh
        lda mach_chi+1
        sta mach_eh+1
        ldx mach_sl
        beq done
sh:     lsr mach_eh+1
        ror mach_eh
        ror mach_et+3
        ror mach_et+2
        ror mach_et+1
        ror mach_et
        dex
        bne sh
done:   rts
.endproc

; mach_arm — A = slot (1-7), mach_at = absolute E deadline: arm (re-arm
; replaces the deadline).  Slot 0 and A >= 8 are ignored.  Clobbers A, X;
; Y kept.
.proc mach_arm
        cmp #MACH_NSLOT
        bcs done
        tax
        beq done                ; slot 0 is VERTB, mach-internal
        lda mach_bit,x
        pha
        txa
        asl a
        asl a
        tax
        lda mach_at
        sta mach_dl,x
        lda mach_at+1
        sta mach_dl+1,x
        lda mach_at+2
        sta mach_dl+2,x
        lda mach_at+3
        sta mach_dl+3,x
        pla
        ora mach_am
        sta mach_am
        jmp mach_kick
done:   rts
.endproc

; mach_disarm — A = slot: disarm (no-op when not armed).  Clobbers A, X.
.proc mach_disarm
        cmp #MACH_NSLOT
        bcs done
        tax
        lda mach_bit,x
        eor #$FF
        and mach_am
        sta mach_am
done:   rts
.endproc

; mach_post — A = INTREQ bit (0-14): set it in INTREQR (chip_shd).  The
; interrupt is delivered at the next service pass; from the trap context a
; post that makes one deliverable kicks (mach_ichk).  Clobbers A, X; Y kept.
.proc mach_post
        cmp #MACH_NBIT
        bcs done
        jsr mach_pset
        jmp mach_ichk
done:   rts
.endproc

; mach_pset — A = INTREQ bit (0-14): INTREQR |= 1 << A.  Clobbers A, X.
.proc mach_pset
        cmp #8
        bcs hi
        tax
        lda mach_bit,x
        ora chip_shd+MACH_INTREQR+1
        sta chip_shd+MACH_INTREQR+1
        rts
hi:     and #7
        tax
        lda mach_bit,x
        ora chip_shd+MACH_INTREQR
        sta chip_shd+MACH_INTREQR
        rts
.endproc

; mach_kick — ask for a service pass at the next instruction boundary when
; called from the trap context (mach_run running the guest, no pass in
; progress); otherwise nothing (a pass in progress re-arms at its end, and
; the next mach_run's entry pass sees every arm).  Clobbers A.
.proc mach_kick
        lda mach_insv
        bne done
        lda mach_in
        beq done
        php
        sei                     ; the RMWs must not race mach_irq's ora
        lda ev_pend
        ora #MACH_EV_KICK
        sta ev_pend
        lda st_halt
        ora #$80
        sta st_halt
        plp
done:   rts
.endproc

; mach_cb — X = slot: run its callback (none: return).  The callback ends
; with rts; A/X/Y clobbered.
.proc mach_cb
        txa
        asl a
        tax
        lda mach_cbv+1,x
        beq none
        sta mach_jv+1
        lda mach_cbv,x
        sta mach_jv
        jmp (mach_jv)
none:   rts
.endproc
.assert (mach_jv & $FF) <> $FF, lderror, "jmp (mach_jv) must not straddle a page"

; mach_slots — fire every armed slot whose deadline is <= now (one E
; snapshot for the whole pass; each slot is disarmed before its callback,
; which may re-arm it).
.proc mach_slots
        lda mach_am
        beq done
        jsr mach_etime
        ldx #3                  ; the pass's own snapshot: a callback's
cp:     lda mach_et,x           ; mach_etime overwrites mach_et
        sta mach_es,x
        dex
        bpl cp
        ldx #1
lp:     stx mach_sx
        lda mach_am
        and mach_bit,x
        beq next
        txa
        asl a
        asl a
        tay
        sec                     ; es - deadline >= 0 (signed): due
        lda mach_es
        sbc mach_dl,y
        lda mach_es+1
        sbc mach_dl+1,y
        lda mach_es+2
        sbc mach_dl+2,y
        lda mach_es+3
        sbc mach_dl+3,y
        bmi next
        lda mach_bit,x
        eor #$FF
        and mach_am
        sta mach_am
        lda #1                  ; client work this pass (MACH_MINW)
        sta mach_wf
        jsr mach_cb
        ldx mach_sx
next:   inx
        cpx #MACH_NSLOT
        bcc lp
done:   rts
.endproc

; mach_wake — mach_w := min(mach_w, phi2 until each armed deadline); a
; deadline at or before now gives 1 (the next pass comes at once), one
; farther than $FFFF phi2 is left to the VERTB wait (already <= $FFFF).
; The first phi2 of E tick d is d << s, so the wait is (d - et) << s minus
; the phi2 already spent in tick et (mach_fr = now16 - (et16 << s)).
.proc mach_wake
        lda mach_am
        bne go
        rts
go:     jsr mach_etime
        lda mach_et
        sta mach_fr
        lda mach_et+1
        sta mach_fr+1
        ldx mach_sl
        beq fr
fs:     asl mach_fr
        rol mach_fr+1
        dex
        bne fs
fr:     sec
        lda mach_now
        sbc mach_fr
        sta mach_fr
        lda mach_now+1
        sbc mach_fr+1
        sta mach_fr+1
        ldx #1
lp:     stx mach_sx
        lda mach_am
        and mach_bit,x
        beq next
        txa
        asl a
        asl a
        tay
        sec                     ; d = deadline - et
        lda mach_dl,y
        sbc mach_et
        sta mach_d
        lda mach_dl+1,y
        sbc mach_et+1
        sta mach_d+1
        lda mach_dl+2,y
        sbc mach_et+2
        sta mach_d+2
        lda mach_dl+3,y
        sbc mach_et+3
        bmi now1                ; already past
        ora mach_d+2
        bne next                ; >= 2^16 E ticks: beyond any wait
        lda mach_d
        ora mach_d+1
        beq now1                ; due now
        ldx mach_sl
        beq sub
sh:     asl mach_d
        rol mach_d+1
        bcs next                ; > $FFFF phi2: beyond the VERTB wait
        dex
        bne sh
sub:    sec
        lda mach_d
        sbc mach_fr
        sta mach_d
        lda mach_d+1
        sbc mach_fr+1
        sta mach_d+1
        jmp min
now1:   lda #1
        sta mach_d
        lda #0
        sta mach_d+1
min:    lda mach_d              ; d < w: w := d
        cmp mach_w
        lda mach_d+1
        sbc mach_w+1
        bcs next
        lda mach_d
        sta mach_w
        lda mach_d+1
        sta mach_w+1
next:   ldx mach_sx
        inx
        cpx #MACH_NSLOT
        bcs done
        jmp lp
done:   rts
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
        cmp mach_ctop           ; W9-I: bits 24-31 went down = C wrapped
        sta mach_ctop
        bcs :+
        inc mach_chi
        bne :+
        inc mach_chi+1
:       rts
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
; ===========================================================================
; W9-I S3.3 interrupt delivery (docs/CONTRACTS.md "Interrupt delivery
; (S3.3)").  Paula's IPL = host/chipset_ref.Paula.level(): 0 unless INTEN
; (INTENA bit 14), else the level of the highest source enabled and
; pending.  In exception mode (st_exl) mach_run calls mach_dlv after every
; service pass: level > SR's I2-I0 raises autovector 24 + level through
; core.s's exc_group2 (stacked PC = r_pc, stacked SR, S = 1, T = 0), then
; I2-I0 := level.  A CPU write to INTENA/INTREQ that sets bits, or a post,
; kicks a pass when it makes an interrupt deliverable (mach_ichk), so it
; acts at the next instruction boundary; an IPL drop by MOVE to SR / RTE
; acts at the next event (S3_PLAN §1.5).
; ===========================================================================

; mach_lvl — A := Paula's IPL (0-6), Z set on 0.  Clobbers X; Y kept.
.proc mach_lvl
        lda chip_shd+MACH_INTENAR
        and #$40                ; INTEN, the master enable
        beq done
        lda chip_shd+MACH_INTENAR+1
        and chip_shd+MACH_INTREQR+1
        sta mach_act            ; enabled and pending, bits 0-7
        lda chip_shd+MACH_INTENAR
        and chip_shd+MACH_INTREQR
        and #$3F                ; bits 8-13 (14 is INTEN, not a source)
        tax
        beq lo
        and #$20
        bne l6
        txa
        and #$18
        bne l5
        lda #4                  ; bits 8-10
        rts
l6:     lda #6
        rts
l5:     lda #5
        rts
lo:     ldx #4
lp:     lda mach_act
        and mach_lvlo-1,x
        bne hit
        dex
        bne lp
hit:    txa
done:   rts
.endproc

; mach_dlv — deliver (see above).  Out: C = 1 when the raise halted (odd
; vector: st_halt = 1, st_reason = HALT_ADDRESS; I = 1), else C = 0.
; Clobbers A/X/Y.
.proc mach_dlv
        lda st_exl
        beq none
        lda st_halt             ; a real halt pending (st_halt = $81 on
        and #$7F                ; entry): emu_run returns it, no frame
        bne none
        jsr mach_lvl
        beq none
        sta mach_lv
        lda r_sr+1
        and #7
        cmp mach_lv
        bcs none                ; I2-I0 >= level: masked (Paula levels are
                                ; 1-6, never 7, so >= is exact: a level-7
                                ; source would need NMI semantics)
        lda #0
        sta mach_stp            ; a stopped CPU resumes
        sei
        lda st_halt
        ora #MACH_PRESET         ; dispatch_next returns right after the frame
        sta st_halt
        cli
        lda mach_lv
        clc
        adc #MACH_AUTOVEC
        jsr mach_raise
        sei
        lda st_halt
        and #$7F
        cmp #MACH_PRESET
        bne halted
        lda st_halt
        and #$80                ; keep an event that came meanwhile
        sta st_halt
        cli
        lda r_sr+1              ; I2-I0 := level (S = 1, T = 0 already)
        and #$F8
        ora mach_lv
        sta r_sr+1
        inc mach_nirq
        bne none
        inc mach_nirq+1
        bne none
        inc mach_nirq+2
        bne none
        inc mach_nirq+3
none:   clc
        rts
halted: sta st_halt             ; = 1: halt_now's (an IRQ's bit 7 dropped,
        sec                     ; ev_pend kept)
        rts
.endproc

; mach_raise — A = vector: raise it from mach_run as if from inside
; emu_run.  exc_raise unwinds the 6502 stack to st_sp6 (here: just below
; this jsr's return address, the depth emu_run's step loop runs at), builds
; the frame, and ends in dispatch_next, which returns here at once because
; st_halt is nonzero (the $40 preset, or halt_now's 1 on an odd vector).
.proc mach_raise
        tsx
        stx st_sp6
        jmp exc_group2
.endproc

; mach_pclamp — in exception mode, while an interrupt is pending and enabled
; (mach_lvl != 0) but not yet taken (masked by I2-I0, or a second one
; behind the one this pass delivers), clamp the one-shot to MACH_POLL phi2:
; an IPL drop by RTE / MOVE to SR (core.s, no hook) is then recognised
; within MACH_POLL instead of at the next VERTB, and a lower level is never
; starved by a recurring higher one.  Nothing pending: no cost but this
; test.  Clobbers A, X.
.proc mach_pclamp
        lda st_exl
        beq done
        jsr mach_lvl
        beq done
        sta mach_iq
        lda r_sr+1              ; deliverable after this pass: the guest must
        and #7                  ; still run (MACH_MINW)
        cmp mach_iq
        bcs clamp
        lda #1
        sta mach_wf
clamp:        lda mach_w              ; mach_w > MACH_POLL: mach_w := MACH_POLL
        cmp #<(MACH_POLL + 1)
        lda mach_w+1
        sbc #>(MACH_POLL + 1)
        bcc done
        lda #<MACH_POLL
        sta mach_w
        lda #>MACH_POLL
        sta mach_w+1
done:   rts
.endproc

; mach_wmin — forward progress (W9-C review): when this pass fired a
; client slot (its callback may post or re-arm) or leaves an interrupt to
; deliver (the VERTB post alone does not count: frames are >= 14188 apart),
; the one-shot waits at least MACH_MINW phi2, so the guest runs at least
; one instruction before the next pass even if a client re-arms "now"
; (a wait of 1 would re-enter the pass with zero instructions between).
; The VERTB count stays exact (it counts boundaries, not wake-ups).
.proc mach_wmin
        lda mach_wf
        beq done
        lda mach_w              ; mach_w < MACH_MINW: mach_w := MACH_MINW
        cmp #<MACH_MINW
        lda mach_w+1
        sbc #>MACH_MINW
        bcs done
        lda #<MACH_MINW
        sta mach_w
        lda #>MACH_MINW
        sta mach_w+1
done:   rts
.endproc

; mach_wk — C = 1 when a stopped CPU can still be woken: INTEN and an
; enabled source above I2-I0 that VERTB posts (bit 5), or any enabled
; source above I2-I0 while a client slot is armed or the VERTB hook is
; installed (a client may post it).  Clobbers A, X.
.proc mach_wk
        lda chip_shd+MACH_INTENAR
        and #$40
        beq no
        lda r_sr+1
        and #7
        tax
        lda chip_shd+MACH_INTENAR+1
        and mach_ablo,x
        sta mach_act
        lda chip_shd+MACH_INTENAR
        and mach_abhi,x
        ora mach_act
        beq no                  ; nothing enabled above the IPL
        lda mach_act
        and #1 << MACH_VERTB_BIT
        bne yes
        lda mach_am
        bne yes
        lda mach_cbv+1
        bne yes
no:     clc
        rts
yes:    sec
        rts
.endproc

; mach_ichk — from the trap context: kick a pass when an interrupt is
; deliverable now (exception mode, level > I2-I0).  chip.s calls it after a
; SET write to INTENA/INTREQ; mach_post after a post.  Clobbers A, X.
.proc mach_ichk
        lda st_exl
        beq done
        jsr mach_lvl
        beq done
        sta mach_iq
        lda r_sr+1
        and #7
        cmp mach_iq
        bcs done
        jmp mach_kick
done:   rts
.endproc

.segment "RODATA"
mach_bit:  .byte $01, $02, $04, $08, $10, $20, $40, $80
; INTREQ bits 0-7 of levels 1-4 (bits 8-13: levels 4, 5, 6 in mach_lvl);
; host/chipset_ref.IRQ_LEVELS, pinned by host/tests/test_irq.py
mach_lvlo: .byte $07, $08, $70, $80
; INTREQ bits above I2-I0 = 0..7 (low, high byte)
mach_ablo: .byte $FF, $F8, $F0, $80, $00, $00, $00, $00
mach_abhi: .byte $3F, $3F, $3F, $3F, $38, $20, $00, $00
.segment "CODE"
.assert mach_nmi < $A000 .and mach_irq < $A000, lderror, "mach vectors must point at RAM visible under $01 = $34"
.assert HALT_STOP = MACH_STOPCODE, lderror, "MACH_STOPCODE must equal core.s HALT_STOP"
