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
; Every run first points the RAM IRQ/NMI vectors at drv_null (acknowledges
; both CIAs, rti): a build whose mach_run forgets its vectors gets no event
; instead of a crash.
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
        lda P+10
        sta CPU_PORT
        jsr nullvec
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
        sta mach_vnmi
        sta mach_virq
        lda #>drv_null
        sta mach_vnmi+1
        sta mach_virq+1
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
