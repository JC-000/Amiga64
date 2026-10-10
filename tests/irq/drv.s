; ---------------------------------------------------------------------------
; tests/irq/drv.s — test driver for the S3.3 event seam and interrupt
; delivery (tests/run_irq.py).
;
; NOT linked into the runner PRG: tests/run_irq.py assembles it at test time
; to a raw binary at DRV_ORG (buf_case tail, loaded after the guest image is
; staged) and passes the runner's labels as -D defines.  The ZP/RAM equates
; come from the resource contract.
;
; The fake event source: callbacks cb1-cb7 (slots 1-7) and cb0 (the slot-0
; VERTB hook) live here.  Each slot's action comes from the slot table in
; the parameter block; every firing is logged.  A trap-context source is
; the RESET instruction: emu_rst_hook is patched to rst_arm, which arms
; slot RST_SLOT at now + delay from inside emu_run (IRQ enabled).
;
; Parameter block P (host writes):
;   P+0  u32 PC       P+4  u32 A7 (SSP in exception mode)   P+8  u8 s
;   P+9  u8 flags     P+10 u8 caller $01   P+11 u8 rst slot
;   P+12 u32 rst delay (E ticks)   P+16 u8 SR high byte (F_SUPER)
;   P+17 u8 slot to disarm right after the initial arms (0 = none)
;   P+18 u16 INTENAR preset (F_IEN; written through chip_shd, LE)
;   P+20 u32 USP (F_SUPER: r_osp)   P+24 u8 st_halt, P+25 u8 ev_pend
;   (F_PRESET, after mach_init)   P+26 u16 INTREQR preset (F_IEN, LE)
;   P+$40 + 16 k, slot k = 1-7:
;     +0 u8 armed at start   +1 u8 post bit ($FF none)   +2 u16 period
;     +4 u32 deadline relative to E0 (signed)   +8 u8 re-arms left ($FF: forever)
;     +9 u8 slot to disarm on fire (0 none)   +10 u8 hold op (1 = set)
;     +11 u16 hold value
;   flags: F_INIT mach_init first   F_SUPER exception mode (st_exc = 1)
;          F_HOOK slot-0 VERTB hook   F_RST RESET arms slot P+11
;          F_IEN  INTENAR := P+18, INTREQR := P+26 before the run
;          F_PRESET st_halt/ev_pend := P+24/P+25 right before mach_run
; Result block R (driver writes):
;   R+0 done $FF  R+1 st_halt  R+2 st_reason  R+3 ev_pend  R+4 u32 frames
;   R+8 u32 E0    R+12 u16 fires[8] (slot 0 unused)  R+28 u16 trap arms
;   R+30 u16 arms that replaced a pending one   R+32 D0-D3 (LE)
;   R+48 INTREQR (BE)  R+50 INTENAR (BE)  R+52 SR (LE)  R+54 u32 PC
;   R+58 u32 A7   R+62 u16 log entries   R+64 u32 E at the end
;   R+68 mach_am  R+69 u16 mach_hold   R+71 u32 VERTB hook calls
;   R+75 u32 frames at the start (after mach_init)   R+79 u32 r_osp
;   R+83 u32 mach_nirq delta (interrupts delivered)   R+87 mach_stp
;   F_RING (W13 lag): entries 0-15 at LOG, every later entry n at
;   LOG + 8 (16 + n % 16): the first 16 and the last 16 survive any length
; Log at LOG: 8-byte entries {u8 slot, u32 E at the callback, u24 E -
;   deadline}; slot $80 = a trap arm {E at the arm, u24 delay}.
;
; Every run first points the RAM IRQ vector at drv_null and the NMI vector
; at mach_nmi with the caller's (KERNAL-in) $01, as tests/mach/drv.s does.
; ---------------------------------------------------------------------------

.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"

P   = buf_result + $200         ; $CA00
R   = buf_result + $300         ; $CB00
LOG = buf_result + $400         ; $CC00-$CCFF: 32 entries
LOGMAX = 32             ; 8-byte entries, Y-indexed: one page

F_INIT  = $01
F_SUPER = $02
F_HOOK  = $04
F_RST   = $08
F_IEN   = $10
F_PRESET = $20
F_RING  = $40           ; W13 lag: entries 16+ go to a 16-entry ring in LOG+128

SLOT = P + $40
R_LEN = 92

.segment "CODE"

drv_run:
        sei
        lda #0
        sta R
        ldx #R_LEN - 1
@clr:   sta R+1,x
        dex
        bpl @clr
        ldx #15
@cc:    sta fires,x
        dex
        bpl @cc
        sta logn
        sta logn+1
        sta arms
        sta arms+1
        sta repl
        sta repl+1
        sta vtbn
        sta vtbn+1
        sta vtbn+2
        sta vtbn+3
        jsr nullvec
        lda P+10
        sta CPU_PORT
        lda P+9
        and #F_SUPER
        beq :+
        lda #1
:       sta st_exc              ; latched by emu_reset
        jsr emu_reset
        ldx #3
@pc:    lda P+0,x
        sta r_pc,x
        lda P+4,x
        sta r_a7,x
        lda P+20,x
        sta r_osp,x
        dex
        bpl @pc
        lda st_exc
        beq :+
        lda P+16
        sta r_sr+1
:       lda P+9
        and #F_INIT
        beq @noinit
        lda P+8
        sta mach_s
        jsr mach_init
@noinit:
        lda P+9
        and #F_IEN
        beq @noien
        lda P+19
        sta chip_shd+$1C
        lda P+18
        sta chip_shd+$1D
        lda P+27
        sta chip_shd+$1E
        lda P+26
        sta chip_shd+$1F
@noien: ldx #3
@f0:    lda mach_frames,x
        sta R+75,x
        lda mach_nirq,x
        sta q_nirq,x
        dex
        bpl @f0
        ; callbacks
        ldx #15
@cb:    lda cbtab,x
        sta mach_cbv,x
        dex
        bpl @cb
        lda P+9
        and #F_HOOK
        bne :+
        lda #0
        sta mach_cbv
        sta mach_cbv+1
:       jsr mach_etime
        ldx #3
@e0:    lda mach_et,x
        sta e0,x
        sta R+8,x
        dex
        bpl @e0
        ; initial arms: dl[k] = E0 + rel[k]
        ldx #1
@arm:   stx q_k
        jsr slotoff             ; Y = 16 k
        lda SLOT+0,y
        beq @next
        lda SLOT+8,y
        ldx q_k
        sta left,x
        txa
        asl a
        asl a
        tax                     ; X = 4 k
        clc
        lda e0
        adc SLOT+4,y
        sta dl,x
        sta mach_at
        lda e0+1
        adc SLOT+5,y
        sta dl+1,x
        sta mach_at+1
        lda e0+2
        adc SLOT+6,y
        sta dl+2,x
        sta mach_at+2
        lda e0+3
        adc SLOT+7,y
        sta dl+3,x
        sta mach_at+3
        lda q_k
        jsr mach_arm
@next:  ldx q_k
        inx
        cpx #8
        bcc @arm
        lda P+17
        beq :+
        jsr mach_disarm
:       lda emu_rst_hook+1      ; save the RESET hook
        sta q_hook
        lda emu_rst_hook+2
        sta q_hook+1
        lda P+9
        and #F_RST
        beq :+
        lda #<rst_arm
        sta emu_rst_hook+1
        lda #>rst_arm
        sta emu_rst_hook+2
:       lda P+9
        and #F_PRESET
        beq :+
        lda P+24
        sta st_halt
        lda P+25
        sta ev_pend
:       jsr mach_run
        sei
        lda q_hook
        sta emu_rst_hook+1
        lda q_hook+1
        sta emu_rst_hook+2
        lda st_halt
        sta R+1
        lda st_reason
        sta R+2
        lda ev_pend
        sta R+3
        ldx #3
@fr:    lda mach_frames,x
        sta R+4,x
        lda r_pc,x
        sta R+54,x
        lda r_a7,x
        sta R+58,x
        lda vtbn,x
        sta R+71,x
        lda r_osp,x
        sta R+79,x
        dex
        bpl @fr
        ldx #15
@fc:    lda fires,x
        sta R+12,x
        lda r_d0,x
        sta R+32,x
        dex
        bpl @fc
        lda arms
        sta R+28
        lda arms+1
        sta R+29
        lda repl
        sta R+30
        lda repl+1
        sta R+31
        lda chip_shd+$1E
        sta R+48
        lda chip_shd+$1F
        sta R+49
        lda chip_shd+$1C
        sta R+50
        lda chip_shd+$1D
        sta R+51
        lda r_sr
        sta R+52
        lda r_sr+1
        sta R+53
        lda logn
        sta R+62
        lda logn+1
        sta R+63
        lda CPU_PORT
        pha
        and #$F8
        ora #$05
        sta CPU_PORT            ; mach_etime reads CIA2
        jsr mach_etime
        pla
        sta CPU_PORT
        ldx #3
@ee:    lda mach_et,x
        sta R+64,x
        dex
        bpl @ee
        sec
        ldx #0
        ldy #4
@ni:    lda mach_nirq,x
        sbc q_nirq,x
        sta R+83,x
        inx
        dey
        bne @ni
        lda mach_stp
        sta R+87
        lda mach_am
        sta R+68
        lda mach_hold
        sta R+69
        lda mach_hold+1
        sta R+70
        lda #0                  ; no stale callbacks, hold or plain-mode leak
        ldx #15
@uncb:  sta mach_cbv,x
        dex
        bpl @uncb
        sta mach_hold
        sta mach_hold+1
        sta st_exc
        lda #$FF
        sta R
        rts


; slotoff — Y := 16 * q_k.  Clobbers A.
slotoff:
        lda q_k
        asl a
        asl a
        asl a
        asl a
        tay
        rts

; --- callbacks (service pass, $01 = $35; A/X/Y free) ------------------------
cb0:    inc vtbn
        bne :+
        inc vtbn+1
        bne :+
        inc vtbn+2
        bne :+
        inc vtbn+3
:       rts
cb1:    ldx #1
        bne cbrec
cb2:    ldx #2
        bne cbrec
cb3:    ldx #3
        bne cbrec
cb4:    ldx #4
        bne cbrec
cb5:    ldx #5
        bne cbrec
cb6:    ldx #6
        bne cbrec
cb7:    ldx #7
cbrec:  stx q_k
        txa
        asl a
        tax
        inc fires,x
        bne :+
        inc fires+1,x
:       jsr mach_etime
        lda q_k
        jsr logdl               ; {slot, E, E - dl[k]}
        jsr slotoff
        lda SLOT+1,y            ; post
        bmi :+
        jsr mach_post
        jsr slotoff
:       lda SLOT+10,y           ; hold
        beq :+
        lda SLOT+11,y
        sta mach_hold
        lda SLOT+12,y
        sta mach_hold+1
:       lda SLOT+9,y            ; disarm another slot
        beq :+
        jsr mach_disarm
        jsr slotoff
:       lda SLOT+2,y            ; periodic re-arm from the deadline (no drift)
        ora SLOT+3,y
        beq @done
        ldx q_k
        lda left,x
        beq @done
        cmp #$FF                ; $FF: re-arm forever
        beq :+
        dec left,x
:
        txa
        asl a
        asl a
        tax
        clc
        lda dl,x
        adc SLOT+2,y
        sta dl,x
        sta mach_at
        lda dl+1,x
        adc SLOT+3,y
        sta dl+1,x
        sta mach_at+1
        lda dl+2,x
        adc #0
        sta dl+2,x
        sta mach_at+2
        lda dl+3,x
        adc #0
        sta dl+3,x
        sta mach_at+3
        lda q_k
        jsr mach_arm
@done:  rts

; logdl — log {A = slot, mach_et, mach_et - dl[q_k]}.  Clobbers A/X/Y.
logdl:  pha
        lda q_k
        asl a
        asl a
        tax
        sec
        lda mach_et
        sbc dl,x
        sta q_d
        lda mach_et+1
        sbc dl+1,x
        sta q_d+1
        lda mach_et+2
        sbc dl+2,x
        sta q_d+2
        pla
        ; fall through
; logent — log {A, mach_et, q_d u24}.  Clobbers A/X/Y.
logent: tax
        lda logn+1
        bne @ring
        lda logn
        cmp #16
        bcc @put
@ring:  lda P+9
        and #F_RING
        beq @lin
        lda logn
        and #15
        ora #16
        bne @put                ; always
@lin:   lda logn+1
        bne @full
        lda logn
        cmp #LOGMAX
        bcs @full
@put:   asl a
        asl a
        asl a
        tay
        txa
        sta LOG,y
        lda mach_et
        sta LOG+1,y
        lda mach_et+1
        sta LOG+2,y
        lda mach_et+2
        sta LOG+3,y
        lda mach_et+3
        sta LOG+4,y
        lda q_d
        sta LOG+5,y
        lda q_d+1
        sta LOG+6,y
        lda q_d+2
        sta LOG+7,y
@full:  inc logn
        bne :+
        inc logn+1
:       rts

; rst_arm — emu_rst_hook while F_RST: the trap context (inside emu_run,
; IRQ enabled).  dl[k] := E + delay, arm slot k = P+11.
rst_arm:
        jsr mach_etime
        lda P+11
        sta q_k
        tax
        lda mach_am             ; still pending: this arm replaces it
        and bits,x
        beq :+
        inc repl
        bne :+
        inc repl+1
:       inc arms
        bne :+
        inc arms+1
:       lda q_k
        asl a
        asl a
        tax
        clc
        lda mach_et
        adc P+12
        sta dl,x
        sta mach_at
        lda mach_et+1
        adc P+13
        sta dl+1,x
        sta mach_at+1
        lda mach_et+2
        adc P+14
        sta dl+2,x
        sta mach_at+2
        lda mach_et+3
        adc P+15
        sta dl+3,x
        sta mach_at+3
        lda P+12
        sta q_d
        lda P+13
        sta q_d+1
        lda P+14
        sta q_d+2
        lda #$80
        jsr logent
        lda q_k
        jmp mach_arm

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

; ===========================================================================
; drv_et — the E time base.  mach_s := P+8, mach_init, then for each of the
; counter values C1 = P+0, C2 = P+4, C3 = P+12: stop the CIA2 cascade at C
; (no time passes) and mach_etime.  R+0 done, R+4 + 8 i: u32 E, u16 chi.
; (A later leg needs a fresh mach_init: the cascade is left stopped.)
; ===========================================================================
drv_et:
        sei
        lda P+8
        sta mach_s
        jsr mach_init
        lda #0
        sta q_n
        ldx #0
        jsr et1
        ldx #4
        jsr et1
        ldx #12
        jsr et1
        lda #$FF
        sta R
        rts
et1:    stx q_k                 ; X = P offset of C
        lda CIA2_CRA
        and #$C0
        sta CIA2_CRA
        lda CIA2_CRB
        and #$80
        sta CIA2_CRB
        lda P+0,x
        eor #$FF
        sta CIA2_TALO
        lda P+1,x
        eor #$FF
        sta CIA2_TAHI           ; stopped: loads the counter
        lda P+2,x
        eor #$FF
        sta CIA2_TBLO
        lda P+3,x
        eor #$FF
        sta CIA2_TBHI
        jsr mach_etime
        lda q_n
        asl a
        asl a
        asl a
        tay
        lda mach_et
        sta R+4,y
        lda mach_et+1
        sta R+5,y
        lda mach_et+2
        sta R+6,y
        lda mach_et+3
        sta R+7,y
        lda mach_chi
        sta R+8,y
        lda mach_chi+1
        sta R+9,y
        inc q_n
        rts

; drv_etl — live samples: mach_s := P+8, mach_init, then 16 x {mach_etime;
; LOG + 12 i: u32 E, u32 mach_now, u16 chi; a delay of P+12 (u16) x 256
; loop passes}.
drv_etl:
        sei
        lda P+8
        sta mach_s
        jsr mach_init
        lda #0
        sta q_n
@lp:    jsr mach_etime
        lda q_n
        asl a
        adc q_n                 ; 3 n (n < 16: C = 0)
        asl a
        asl a                   ; 12 n
        tay
        ldx #0
:       lda mach_et,x
        sta LOG,y
        lda mach_now,x
        sta LOG+4,y
        iny
        inx
        cpx #4
        bcc :-
        lda mach_chi
        sta LOG+4,y
        lda mach_chi+1
        sta LOG+5,y
        lda P+12
        sta q_d
        lda P+13
        sta q_d+1
        ldx #0
@dl:    dex
        bne @dl
        lda q_d
        bne :+
        dec q_d+1
:       dec q_d
        lda q_d
        ora q_d+1
        bne @dl
        inc q_n
        lda q_n
        cmp #16
        bcc @lp
        lda #$FF
        sta R
        rts

; drv_out — outside mach_run (mach_in = 0): mach_post(2) and an arm of slot
; 1 must not touch st_halt/ev_pend.  R+1 st_halt, R+2 ev_pend, R+3 INTREQR
; low byte; then slot 1 is disarmed and bit 2 cleared again.
drv_out:
        sei
        lda #0
        sta st_halt
        sta ev_pend
        sta mach_at
        sta mach_at+1
        sta mach_at+2
        sta mach_at+3
        lda #2
        jsr mach_post
        lda #1
        jsr mach_arm
        lda st_halt
        sta R+1
        lda ev_pend
        sta R+2
        lda chip_shd+$1F
        sta R+3
        lda #1
        jsr mach_disarm
        lda chip_shd+$1F
        and #$FB
        sta chip_shd+$1F
        lda #0
        sta st_halt
        sta ev_pend
        lda #$FF
        sta R
        rts

; --- tables and state ---------------------------------------------------------
cbtab:  .word cb0, cb1, cb2, cb3, cb4, cb5, cb6, cb7
bits:   .byte $01, $02, $04, $08, $10, $20, $40, $80
; driver state lives in RESBUF ($CD00-), not in the driver image
ST     = buf_result + $500
e0     = ST                     ; 4
dl     = ST + 4                 ; 32
left   = ST + 36                ; 8
fires  = ST + 44                ; 16
arms   = ST + 60                ; 2
repl   = ST + 62                ; 2
logn   = ST + 64                ; 2
vtbn   = ST + 66                ; 4
q_k    = ST + 70
q_n    = ST + 71
q_d    = ST + 72                ; 3
q_hook = ST + 75                ; 2
q_nirq = ST + 77                ; 4
