; ---------------------------------------------------------------------------
; tests/mach/drv.s — test driver for the S3.2 machine loop (tests/run_mach.py)
;
; NOT linked into the runner PRG: tests/run_mach.py assembles it at test time
; to a raw binary at DRV_ORG (buf_case tail, loaded after the guest image is
; staged) and passes the runner's labels as -D defines (emu_reset, emu_run,
; mem_read16, mem_rec_dirty, mach_init, mach_run, mach_vec, mach_s,
; mach_nmis).  The ZP/RAM equates come from the resource contract.
;
; Parameter block P (host writes), result block R (driver writes):
;   P+0  u32 PC          P+4  u32 A7        P+8  u8 s (-> mach_s)
;   P+9  u8  flags       P+10 u8 caller $01  P+11 u32 PC of the follow-up
;   P+15 u8  preset st_halt   P+16 u8 preset ev_pend
;   P+21 u8  CIA1 CRA TODIN bit for the TOD ($80 = 50 Hz mains, 0 = 60 Hz)
;   P+17 u24 guest address of the timed read (drv_time), P+20 u8 nonzero
;   = do the read (0 = the empty calibration window)
;   flags: F_INIT  mach_init first      F_FOLLOW  follow-up emu_run (below)
;          F_PRESET st_halt/ev_pend preset after emu_reset (and mach_init)
;          F_NMI   CIA2 timer-A NMI enabled around mach_run
;          F_TOD   CIA1 TOD read around mach_run (50 Hz, started here)
;          F_SUPER exception mode (st_exc = 1 latched by emu_reset), SR =
;                  $2700; st_exc is 0 again on return
;   R+0 done $FF   R+1 st_halt  R+2 st_reason  R+3 $01 after mach_run
;   R+4 P after mach_run   R+5 ev_pend   R+6 u32 mach_frames
;   R+10 st_halt after the follow-up   R+11 ev_pend after it
;   R+12 u32 mach_frames after it   R+16 TOD at start (10ths,s,m,h)
;   R+20 TOD at the end   R+24 mach_nmis before   R+25 mach_nmis after
;   R+26 u16 drv_time cycles (CIA1 timer B delta)
;   R+32 D0-D3 after mach_run (16 bytes, LE)
;
; Every run first points the RAM IRQ vector at drv_null (acknowledges both
; CIAs, rti), so a build whose mach_run forgets its vectors gets no event
; instead of a crash, and the NMI vector at mach_nmi.  Both stores happen
; with the caller's (KERNAL-in) $01, before the run's $01 is set: the
; driver keeps the invariant it tests (docs/CONTRACTS.md "Machine loop":
; RAM vectors are written only while the ROM vectors are live, or rewritten
; with identical bytes).
; ---------------------------------------------------------------------------

.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"

P = buf_result + $200           ; $CA00
R = buf_result + $300           ; $CB00

F_INIT   = $01
F_FOLLOW = $02
F_PRESET = $04
F_NMI    = $08
F_TOD    = $10
F_SUPER  = $20

CIA1_TOD10 = $DC08              ; test-only equates (drv.s is not rescheck-scanned)
CIA1_TODS  = $DC09
CIA1_TODM  = $DC0A
CIA1_TODH  = $DC0B
CIA1_TBLO  = $DC06
CIA1_TBHI  = $DC07
CIA1_CRB   = $DC0F
VIC_CR1    = $D011

.segment "CODE"

; drv_run — the machine-loop leg driver (see the block comment).
drv_run:
        sei
        lda #0
        sta R
        jsr nullvec
        lda P+10
        sta CPU_PORT
        lda P+9
        and #F_SUPER
        beq :+
        lda #1
:       sta st_exc              ; latched by emu_reset
        jsr emu_reset
        jsr ldpc
        lda st_exc
        beq :+
        lda #$27                ; supervisor, IPL 7
        sta r_sr+1
:       lda P+9
        and #F_INIT
        beq @noinit
        lda P+8
        sta mach_s
        jsr mach_init
@noinit:
        lda P+9
        and #F_PRESET
        beq @nopre
        lda P+15
        sta st_halt
        lda P+16
        sta ev_pend
@nopre: lda mach_nmis
        sta R+24
        lda P+9
        and #F_NMI
        beq @nonmi
        lda CIA2_ICR            ; drop a stale underflow flag first, or the
        lda #$81                ; unmask raises /NMI at once (under the
        sta CIA2_ICR            ; KERNAL, whose NMI handler masks CIA2)
@nonmi: lda P+9
        and #F_TOD
        beq @notod
        jsr todstart
@notod:
        jsr mach_run
        php
        pla
        sta R+4
        lda CPU_PORT
        sta R+3
        sei
        lda P+9
        and #F_TOD
        beq @notod2
        ldx #20
        jsr todread
@notod2:
        lda #$7F
        sta CIA2_ICR
        lda CIA2_ICR
        lda mach_nmis
        sta R+25
        lda st_halt
        sta R+1
        lda st_reason
        sta R+2
        lda ev_pend
        sta R+5
        ldx #3
@fr:    lda mach_frames,x
        sta R+6,x
        dex
        bpl @fr
        ldx #15
@regs:  lda r_d0,x
        sta R+32,x
        dex
        bpl @regs
        lda #0
        sta st_exc              ; later emu_resets latch plain mode
        lda P+9
        and #F_FOLLOW
        beq @done
        ; follow-up: a plain emu_run with the I/O-in, KERNAL-out map (so an
        ; IRQ, if mach_run left one enabled, lands in mach's own handler) and
        ; IRQs enabled.  It must see no event.
        lda CPU_PORT
        and #$F8
        ora #$05
        sta CPU_PORT
        jsr emu_reset
        jsr ldpc
        ldx #3
@pc2:   lda P+11,x
        sta r_pc,x
        dex
        bpl @pc2
        cli
        jsr emu_run
        sei
        lda P+10
        sta CPU_PORT
        lda st_halt
        sta R+10
        lda ev_pend
        sta R+11
        ldx #3
@fr2:   lda mach_frames,x
        sta R+12,x
        dex
        bpl @fr2
@done:  lda #$FF
        sta R
        rts

; drv_nmi34 — mach_init, mach's RAM vectors, then $01 = $34 (all RAM, no
; I/O) with CIA2 timer-A NMIs enabled for ~13 underflows.  mach_nmis must
; count them all: a handler that cannot reach CIA2 under $34 leaves /NMI
; latched after the first.
drv_nmi34:
        sei
        lda #0
        sta R
        lda P+8
        sta mach_s
        jsr mach_init
        jsr mach_vec
        lda mach_nmis
        sta R+24
        lda CIA2_ICR
        lda #$81
        sta CIA2_ICR
        lda CPU_PORT
        pha
        and #$F8
        ora #$04                ; $34
        sta CPU_PORT
        ldy #0                  ; 256 x 256 x 13 cycles: ~0.85M cycles
        ldx #0
@w:     nop
        nop
        nop
        nop
        dex
        bne @w
        dey
        bne @w
        pla
        sta CPU_PORT
        lda #$7F
        sta CIA2_ICR
        lda CIA2_ICR
        lda mach_nmis
        sta R+25
        lda #$FF
        sta R
        rts

; drv_time — cycles of one mem_read16 at guest P+17 (u24), or of nothing
; when P+20 = 0 (the calibration).  CIA1 timer B counts phi2 from $FFFF
; between a start and a stop store that bracket the call; the screen is off
; and the call starts on raster line 0, so no badline steals cycles.
; R+26 = u16 timer B after the stop (host: cycles = $FFFF - R+26 - empty).
; P+9 F_INIT: mach_init (with s = P+8) first.
drv_time:
        sei
        lda #0
        sta R
        lda P+10
        sta CPU_PORT
        lda P+9
        and #F_INIT
        beq @noi
        lda P+8
        sta mach_s
        jsr mach_init
@noi:   jsr mem_rec_dirty
        lda VIC_CR1
        pha
        and #$EF                ; DEN = 0 (checked at raster line $30)
        sta VIC_CR1
        ldx #2
@guest: lda P+17,x
        sta m_addr,x
        dex
        bpl @guest
        lda #0
        sta m_addr+3
        sta CIA1_CRB            ; timer B stopped
        lda #$FF
        sta CIA1_TBLO
        sta CIA1_TBHI           ; loads the counter (timer stopped)
@top:   bit VIC_CR1             ; wait for lines 256+ ...
        bpl @top
@bot:   bit VIC_CR1             ; ... then line 0 of the next frame
        bmi @bot
        ldx P+20
        lda #$01                ; start (phi2, continuous)
        sta CIA1_CRB
        txa
        beq @empty
        jsr mem_read16
@empty: lda #$00                ; stop
        sta CIA1_CRB
        lda CIA1_TBLO
        sta R+26
        lda CIA1_TBHI
        sta R+27
        pla
        sta VIC_CR1
        lda #$FF
        sta R
        rts

; --- helpers -----------------------------------------------------------------
ldpc:   ldx #3
@l:     lda P+0,x
        sta r_pc,x
        lda P+4,x
        sta r_a7,x
        dex
        bpl @l
        rts

nullvec:
        lda #<drv_null
        sta mach_virq
        lda #>drv_null
        sta mach_virq+1
        lda #<mach_nmi
        sta mach_vnmi
        lda #>mach_nmi
        sta mach_vnmi+1
        rts

drv_null:
        pha
        lda CIA1_ICR
        lda CIA2_ICR
        pla
        rti

; todstart — CIA1 TOD at 50 Hz, set to 1:00:00.0 and started; R+16 = start.
todstart:
        lda CIA1_CRA
        and #$7F
        ora P+21                ; TODIN: the machine's mains (50/60 Hz)
        sta CIA1_CRA
        lda CIA1_CRB
        and #$7F                ; writes set the clock, not the alarm
        sta CIA1_CRB
        lda #$01
        sta CIA1_TODH           ; stops the clock
        lda #0
        sta CIA1_TODM
        sta CIA1_TODS
        sta CIA1_TOD10          ; restarts it
        ldx #16
        ; fall through
; todread — R+x.. = TOD 10ths, s, m, h (reading h latches until 10ths)
todread:
        lda CIA1_TODH
        sta R+3,x
        lda CIA1_TODM
        sta R+2,x
        lda CIA1_TODS
        sta R+1,x
        lda CIA1_TOD10
        sta R+0,x
        rts

; ===========================================================================
; Fix round 1 attacks (review w8-loop-review F1/F3/F4/F5/F6).  Each one
; reprograms CIA2 or the frame state; tests/run_mach.py re-runs mach_init
; afterwards.
; ===========================================================================

; drv_init — mach_s := P+8, mach_init.
drv_init:
        sei
        lda P+8
        sta mach_s
        jsr mach_init
        lda #$FF
        sta R
        rts

; drv_setc — stop the CIA2 cascade at counter C = P+0 (u32): TB:TA :=
; ~C, both stopped, so no time passes (fix round 2 exact catch-up counts;
; mach_init restarts the cascade afterwards).
drv_setc:
        sei
        lda CIA2_CRA
        and #$C0
        sta CIA2_CRA
        lda CIA2_CRB
        and #$80
        sta CIA2_CRB
        lda P+0
        eor #$FF
        sta CIA2_TALO
        lda P+1
        eor #$FF
        sta CIA2_TAHI           ; stopped: loads the counter
        lda P+2
        eor #$FF
        sta CIA2_TBLO
        lda P+3
        eor #$FF
        sta CIA2_TBHI
        lda #$FF
        sta R
        rts

; drv_now — refresh mach_now (through mach_beam).
drv_now:
        sei
        jsr mach_beam
        lda #$FF
        sta R
        rts

; oneshot — CIA2 timer A one-shot NMI in q_n cycles (stale
; flags dropped first).  Clobbers A.
oneshot:
        lda #$7F
        sta CIA2_ICR
        lda CIA2_ICR
        lda CIA2_CRA
        and #$C0
        sta CIA2_CRA
        lda q_n
        sta CIA2_TALO
        lda q_n+1
        sta CIA2_TAHI
        lda #$81
        sta CIA2_ICR
        lda CIA2_CRA
        and #$C0
        ora #$19                ; one-shot, force load, start
        sta CIA2_CRA
        rts

nmioff: lda #$7F
        sta CIA2_ICR
        lda CIA2_ICR
        rts

; drv_win (F3) — mach_run's prologue under an NMI swept N = P+11.. (P+13
; steps of 1).  The NMI vector is poisoned (drv_poison) with the KERNAL in
; before every call; the caller's $01 is P+8; st_halt = 1 so mach_run
; returns at once.  R+1 u16 poison NMIs taken, R+3 calls returned.
drv_win:
        sei
        lda #0
        sta R
        sta R+1
        sta R+2
        sta R+3
        lda P+11
        sta q_n
        lda P+12
        sta q_n+1
        lda P+13
        sta q_cnt
        lda CPU_PORT
        sta q_hport
@it:    jsr emu_reset
        jsr ldpc
        lda #1
        sta st_halt
        lda q_hport
        sta CPU_PORT
        lda #<drv_poison        ; with the ROM vectors live
        sta mach_vnmi
        lda #>drv_poison
        sta mach_vnmi+1
        lda P+8
        sta CPU_PORT
        jsr oneshot
        jsr mach_run
        inc R+3
        lda q_hport
        sta CPU_PORT
        jsr nmioff
        inc q_n
        bne :+
        inc q_n+1
:       dec q_cnt
        bne @it
        lda #$FF
        sta R
        rts

drv_poison:
        pha
        inc R+1
        bne :+
        inc R+2
:       lda CPU_PORT
        pha
        and #$F8
        ora #$05
        sta CPU_PORT
        lda CIA2_ICR
        pla
        sta CPU_PORT
        pla
        rti

; drv_c2vnmi (F1) — c2v_pal_mc2 called with $01 = P+8 (i.e. $35, as inside
; mach_run) while the NMI vector already holds mach_nmi (re-stored with the
; KERNAL in before every call), an NMI swept N = P+11.. (P+13 calls).
; Every call must see exactly one NMI counted by mach_nmis.  R+1 u16 calls
; where mach_nmis did not move by exactly 1, R+3 u16 NMIs through a torn
; vector (drv_torn, patched in by the host where the old code has one),
; R+5 calls.
drv_c2vnmi:
        sei
        lda #0
        ldx #7
@z:     sta R,x
        dex
        bpl @z
        lda P+11
        sta q_n
        lda P+12
        sta q_n+1
        lda P+13
        sta q_cnt
        lda CPU_PORT
        sta q_hport
@it:    lda q_hport
        sta CPU_PORT
        lda #<mach_nmi
        sta mach_vnmi
        lda #>mach_nmi
        sta mach_vnmi+1
        lda P+8
        sta CPU_PORT
        lda mach_nmis
        sta q_t
        jsr oneshot
        jsr c2v_pal_mc2
        sei
        ldx #0                  ; ~1300 cycles: an NMI still in flight lands
@w:     dex
        bne @w
        jsr nmioff
        inc R+5
        lda mach_nmis
        sec
        sbc q_t
        cmp #1
        beq @ok
        inc R+1
        bne @ok
        inc R+2
@ok:    inc q_n
        bne :+
        inc q_n+1
:       dec q_cnt
        bne @it
        lda q_hport
        sta CPU_PORT
        lda #$FF
        sta R
        rts

drv_torn:
        pha
        inc R+3
        bne :+
        inc R+4
:       lda CPU_PORT
        pha
        and #$F8
        ora #$05
        sta CPU_PORT
        lda CIA2_ICR
        pla
        sta CPU_PORT
        pla
        rti

; drv_race (F4) — P+11 u16 iterations of: emu_reset, D3 := rnd & P+13, a
; random delay, mach_run of a busy guest (s set by drv_init; caller $01 =
; P+8).  Right after each return: st_halt = $01 and reason 0, $01 restored,
; I = 1; ev_pend set counts a genuine race only if the frame boundary has
; really passed (now - next >= 0; at s = 0 the one-shot is armed exactly to
; it), else a false pending event; then ~600 cycles with IRQs enabled and a
; counting IRQ vector: any IRQ is stray.  ev_pend is left for the next
; mach_run to take.
; R+1 u16 bad halt/port/I, R+3 u16 genuine races, R+5 u16 false pending,
; R+7 u16 stray IRQs, R+9 first bad (halt, reason, port, P).
drv_race:
        sei
        lda #0
        ldx #15
@z:     sta R,x
        dex
        bpl @z
        lda P+11
        sta q_cnt
        lda P+12
        sta q_cnt+1
        lda #$5A
        sta q_seed
        lda #$C3
        sta q_seed+1
        lda CPU_PORT
        sta q_hport
@it:    jsr emu_reset
        jsr ldpc
        jsr rnd
        and P+13
        sta r_d3
        jsr rnd
        tax
@dl:    dex
        bne @dl
        lda P+8
        sta CPU_PORT
        jsr mach_run
        php
        pla
        sta q_p
        lda CPU_PORT
        sta q_port
        sei
        lda q_hport
        sta CPU_PORT
        lda st_halt
        cmp #1
        bne @bad
        lda st_reason
        bne @bad
        lda q_port
        cmp P+8
        bne @bad
        lda q_p
        and #$04
        bne @good
@bad:   ldx #1
        jsr rinc
        lda R+9
        ora R+10
        bne @good
        lda st_halt
        sta R+9
        lda st_reason
        sta R+10
        lda q_port
        sta R+11
        lda q_p
        sta R+12
@good:  lda ev_pend
        beq @stray
        jsr mach_beam           ; mach_now := now
        sec
        lda mach_now
        sbc mach_next
        lda mach_now+1
        sbc mach_next+1
        lda mach_now+2
        sbc mach_next+2
        lda mach_now+3
        sbc mach_next+3
        bmi @false
        ldx #3
        jsr rinc
        jmp @stray
@false: ldx #5
        jsr rinc
@stray: lda #<drv_rirq          ; ROM vectors live: $01 = the host's
        sta mach_virq
        lda #>drv_rirq
        sta mach_virq+1
        lda CPU_PORT
        and #$F8
        ora #$05
        sta CPU_PORT
        cli
        ldx #120
@sw:    dex
        bne @sw
        sei
        lda q_hport
        sta CPU_PORT
        lda q_cnt
        bne :+
        dec q_cnt+1
:       dec q_cnt
        lda q_cnt
        ora q_cnt+1
        beq @end
        jmp @it
@end:   lda #$FF
        sta R
        rts

drv_rirq:
        pha
        lda CIA1_ICR
        txa
        pha
        ldx #7
        jsr rinc
        pla
        tax
        pla
        rti

rinc:   inc R,x
        bne :+
        inc R+1,x
:       rts

rnd:    lda q_seed
        asl
        rol q_seed+1
        bcc :+
        eor #$2D
:       sta q_seed
        eor q_seed+1
        rts

; drv_rdc (F6) — mach_rdc across a forced counter edge, for d = P+15 ..
; P+13-1 (mode 1 starts past the ~10 cycles the $FFFF latch writes take,
; so the reload is $FFFF).  Mode P+14 = 0: TA = $0100 + d, so TALO wraps $00 -> $FF at a
; different point of the read each time (a missing TAHI re-check tears it by
; 256).  Mode 1: TA = d with latch $FFFF, so TA underflows (reloads $FFFF
; and TB moves) during the read for some d (a missing TB re-check tears it
; by 65536).  TB = $1234 at the start.  The read C must satisfy
; C - C0 in [0, $100), C0 = ~($1234 : TA start).  R+1 bad reads, R+2 d of
; the first bad one, R+4 its snapshot.  The screen is off for the sweep
; (DEN = 0 from a frame start): a badline stealing ~40 cycles between the
; timer start and the latch writes would otherwise reload TA from a half-
; written latch.
drv_rdc:
        sei
        lda #0
        sta R
        sta R+1
        sta R+2
        lda P+15
        sta q_n
        lda P+14
        eor #1                  ; mode 0: TA high byte 1, mode 1: 0
        sta q_h
        lda VIC_CR1
        pha
        and #$EF                ; DEN = 0 (latched at raster line $30)
        sta VIC_CR1
@top:   bit VIC_CR1             ; lines 256+ ...
        bpl @top
@bot:   bit VIC_CR1             ; ... then line 0: no badline from here on
        bmi @bot
@it:    lda CIA2_CRA
        and #$C0
        sta CIA2_CRA
        lda CIA2_CRB
        and #$80
        sta CIA2_CRB
        lda #$34
        sta CIA2_TBLO
        lda #$12
        sta CIA2_TBHI
        lda q_n
        sta CIA2_TALO
        lda q_h
        sta CIA2_TAHI
        lda CIA2_CRB
        and #$80
        ora #$51
        sta CIA2_CRB
        lda CIA2_CRA
        and #$C0
        ora #$11
        sta CIA2_CRA
        ldx #$FF                ; latch $FFFF for the reload (a running
        stx CIA2_TALO           ; timer does not load on a latch write)
        stx CIA2_TAHI
        jsr mach_rdc
        lda CIA2_CRA
        and #$C0
        sta CIA2_CRA
        ; C - C0 = C + ($1234 : q_h : d) + 1  (mod 2^32)
        sec
        lda mach_now
        adc q_n
        lda mach_now+1
        adc q_h
        sta q_d+1
        lda mach_now+2
        adc #$34
        sta q_d+2
        lda mach_now+3
        adc #$12
        ora q_d+2
        ora q_d+1
        beq @nx
        lda R+1
        bne :+
        lda q_n
        sta R+2
        ldx #3                  ; the first bad snapshot, for the report
@cp:    lda mach_now,x
        sta R+4,x
        dex
        bpl @cp
:       inc R+1
@nx:    inc q_n
        lda q_n
        cmp P+13
        beq @out
        jmp @it
@out:   pla
        sta VIC_CR1
        lda #$FF
        sta R
        rts

q_hport: .byte 0
q_port:  .byte 0
q_p:     .byte 0
q_t:     .byte 0
q_h:     .byte 0
q_cnt:   .word 0
q_n:     .word 0
q_seed:  .word 0
q_d:     .res 4
