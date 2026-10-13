; ---------------------------------------------------------------------------
; tests/paula/drv.s — W15-P: the VICE driver of tests/run_paula.py.
;
; NOT linked into the runner PRG: run_paula assembles it at test time to a
; raw binary at DRV_ORG ($C200) with the runner's labels as -D defines (as
; tests/irq/drv.s).  Its blocks sit in buf_result's case span, which machine
; mode leaves unused; $CA00-$CDFF is the audio module's (tests/paula/aud.s).
;
; drv_run — one guest run in exception mode under mach_run:
;   sei; st_exc := 1; emu_reset; PC, SSP from P; SR := $2700; mach_s := P+8;
;   mach_init; chip_reset (CIAs, slots 1-5); cop_reset (DMACON := 0); the
;   module: mach_cbv[6] := AUD_ORG+7, its init when P+11 bit 0, aud_mon :=
;   P+9 (0 = not armed); mach_run; the results; then aud_mon := 0,
;   mach_cbv[6] := 0, st_exc := 0.
; Parameter block P (host writes): +0 u32 PC, +4 u32 SSP, +8 u8 s, +9 u8 the
;   module's ID byte 0, +10 u8 caller $01, +11 u8 flags (bit 0: aud init).
; Result block R (driver writes, LE): +0 done $FF, +1 st_halt, +2 st_reason,
;   +4 u32 mach_frames, +8 u32 E at the end, +12 u32 deliveries (mach_nirq
;   delta), +16 D0-D7, +48 u32 PC, +52 INTREQR (BE), +54 INTENAR (BE),
;   +56 u32 E right before mach_run.
;
; aud_log — the sampler of the module's log build (run_paula assembles
; aud.s with -D AUD_LOG=aud_log, so every sampler store is `jsr aud_log`,
; same size): A = value, Y = 32 x voice + register.  The register mirror
; LG_M[Y] := A, the command ring LG_R/LG_R+256 [n mod 256] := (Y, A), n += 1;
; then the M5 check on that voice: CTRL bit 1 (repeat) clear and REPEAT B
; (+$15..$17) == LENGTH (+$09..$0B) counts in lg_bad (first entry in
; lg_first); and a write to START or LENGTH (+$04..+$0B) while the mirror's
; CTRL bit 0 (enable) is set counts in lg_live: rewriting the span of a
; voice that has not been stopped (CTRL 0) lets it run on past a shorter
; LENGTH [MEASURED HW, W15-P fix round 1].  Keeps A, X, Y.  The host
; clears the mirror, n, lg_bad, lg_first and lg_live.
; ---------------------------------------------------------------------------

.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"

P = buf_result                  ; $C800
R = buf_result + $20            ; $C820
R_LEN = 60

.segment "CODE"

drv_run:
        sei
        lda #0
        ldx #R_LEN - 1
:       sta R,x
        dex
        bpl :-
        lda #<drv_null          ; as tests/irq/drv.s: safe vectors first
        sta mach_virq
        lda #>drv_null
        sta mach_virq+1
        lda #<mach_nmi
        sta mach_vnmi
        lda #>mach_nmi
        sta mach_vnmi+1
        lda P+10
        sta CPU_PORT
        lda #1
        sta st_exc
        jsr emu_reset
        ldx #3
:       lda P+0,x
        sta r_pc,x
        lda P+4,x
        sta r_a7,x
        dex
        bpl :-
        lda #$27
        sta r_sr+1
        lda P+8
        sta mach_s
        jsr mach_init
        jsr chip_reset
        jsr cop_reset
        lda #<(AUD_ORG + 7)
        sta mach_cbv + 12
        lda #>(AUD_ORG + 7)
        sta mach_cbv + 13
        lda P+11
        and #1
        beq :+
        jsr AUD_ORG + 10
:       lda P+9
        sta aud_mon
        ldx #3
:       lda mach_nirq,x
        sta q_nirq,x
        dex
        bpl :-
        lda CPU_PORT
        pha
        and #$F8
        ora #$05
        sta CPU_PORT            ; mach_etime reads CIA2
        jsr mach_etime
        pla
        sta CPU_PORT
        ldx #3
:       lda mach_et,x
        sta R+56,x
        dex
        bpl :-
        jsr mach_run
        sei
        lda #0
        sta aud_mon
        sta mach_cbv + 12
        sta mach_cbv + 13
        lda st_halt
        sta R+1
        lda st_reason
        sta R+2
        ldx #3
:       lda mach_frames,x
        sta R+4,x
        lda r_pc,x
        sta R+48,x
        dex
        bpl :-
        sec
        ldx #0
:       lda mach_nirq,x
        sbc q_nirq,x
        sta R+12,x
        inx
        txa
        eor #4
        bne :-
        ldx #31
:       lda r_d0,x
        sta R+16,x
        dex
        bpl :-
        lda chip_shd+$1E
        sta R+52
        lda chip_shd+$1F
        sta R+53
        lda chip_shd+$1C
        sta R+54
        lda chip_shd+$1D
        sta R+55
        lda CPU_PORT
        pha
        and #$F8
        ora #$05
        sta CPU_PORT            ; mach_etime reads CIA2
        jsr mach_etime
        pla
        sta CPU_PORT
        ldx #3
:       lda mach_et,x
        sta R+8,x
        dex
        bpl :-
        lda #0
        sta st_exc
        lda #$FF
        sta R
        rts

drv_null:
        pha
        lda CIA1_ICR
        lda CIA2_ICR
        pla
        rti

q_nirq: .byte 0, 0, 0, 0

LG_M = buf_case + $400          ; register mirror ($C400), 4 voices x 32 bytes
LG_R = buf_case + $500          ; command ring ($C500): index page, value page
aud_log:
        sta lg_a
        stx lg_x
        sta LG_M,y
        ldx lg_n
        sta LG_R+256,x
        tya
        sta LG_R,x
        inc lg_n
        bne :+
        inc lg_n+1
:       tya
        and #$1F
        cmp #$04                ; START .. LENGTH (+$04..+$0B)?
        bcc :+
        cmp #$0C
        bcs :+
        tya
        and #$E0
        tax
        lda LG_M,x              ; enabled: the span of a running voice
        lsr a
        bcc :+
        inc lg_live
        bne :+
        inc lg_live+1
:       tya
        and #$E0
        tax                     ; X = the voice's block
        lda LG_M,x
        and #2
        bne lgok                ; repeat on: any B
        lda LG_M+$15,x
        cmp LG_M+$09,x
        bne lgok
        lda LG_M+$16,x
        cmp LG_M+$0A,x
        bne lgok
        lda LG_M+$17,x
        cmp LG_M+$0B,x
        bne lgok
        lda lg_bad              ; repeat off with B == LENGTH
        ora lg_bad+1
        bne :+
        lda lg_n
        sta lg_first
        lda lg_n+1
        sta lg_first+1
:       inc lg_bad
        bne lgok
        inc lg_bad+1
lgok:   ldx lg_x
        lda lg_a
        rts
lg_a:   .byte 0
lg_x:   .byte 0
lg_n:   .word 0
lg_bad: .word 0
lg_first: .word 0
lg_live: .word 0

.assert * <= LG_M, error, "drv.s: runs into the log mirror"

