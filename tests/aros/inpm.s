; ---------------------------------------------------------------------------
; tests/aros/inpm.s — W14-M: the input module (the 1351 decode with a per-
; frame clamp, the left button's edge queue), a machine-mode module in TWO
; segments (docs/WAVE14_BRIEF.md W14-M; docs/CONTRACTS.md "Input (joystick
; -> mouse)", "M2").
;
; NOT linked into the runner PRG: tests/aros_wb.py build_inpm() assembles it
; at test time with the runner's labels as -D defines into two raw binaries:
;   A at INPM_ORG ($C980, end <= $C9FE: the supervisor's range between
;     aros_boot's VICE TRAMP at $C970 and mboot's AROS_ZERO at $C9FF);
;   B at INPM_ORGB ($CF00, end <= $CFEF: the buf_result tail below the
;     runner's READY_FLAG $CFFF; supervisor, fix round 1; nothing in machine
;     mode uses $CF00-$CFEF: mboot uses $CE00 and $CFFF, the U64 dispatcher
;     $CE00-$CEFF, run_irq's drv $CFF0, run_super_vectors' $CF00-$CF9F only
;     in its own HLE sessions).
; One ID (SHA-1[:4] of A and B with both ID fields zero) is stamped at A+3
; and B+3.  MAIN calls A only through inp_mod's guard (inp_mon = ID byte 0
; vs A+3); A calls B only while B's 4 ID bytes equal A's, else it disarms.
; All state is MAIN BSS.  A resume comes up disarmed (snap.s zeroes
; inp_mon); the launcher re-stages and re-arms (tests/aros_wb.arm_inpm).
;
; Entry A (jmp), X = 0 from the frame hook (end of inp_frame), X = 3 from
; the 1351 sampler (inp_msamp, mach slot 7); $01 = $35; clobbers A/X/Y;
; ends in inp_mrearm with X as on entry.
;   look (both): the mouse lines (inp_ml with the override, else CIA1 with
;     DDRA = $00: PA7-6 released, no keyboard column driven; port 1 = PRB,
;     port 2 = PRA; DDRA := $C0 again); a left press edge adds 1 to inp_mq
;     (at most 2 pending).
;   sample (X = 3): POTY/POTX (the SID, or inp_mpot with the override) ->
;     v = pot & $7E, d = (v - ref) mod 128, signed, / 2 (-32..31), ref =
;     inp_mold bits 1-6.  W16-I hysteresis: inp_mold bit 7 = the direction
;     of the last move passed on (1 = negative, the pot's sense); a d of
;     +-1 against it is idle jitter (the user's 1351 at rest: POTX $C9/$CA,
;     POTY $AB/$AC): held, ref kept; any other d != 0 passes, ref := v,
;     bit 7 := d < 0.  Y negated; the travel since the last frame hook
;     (inp_macc, u8 per axis: the sum of |counts| added) saturates at
;     MCLAMP = 63 and only the part within it is added to JOY0DAT: AROS
;     reads JOY0DAT once a frame, after the hook, as a signed 8-bit delta,
;     so the counts between two reads (two windows) are <= 126 for any
;     stream, reversals included, never a wrap (rev14-mouse HIGH-1 and its
;     round 2: 127 + 31 had wrapped; rev16-i M-1: a signed +-63 sum let an
;     in-window reversal reach 189).
;   frame (X = 0): a port or source change since the last frame -> inp_mok
;     := 0 (the next sample is a new reference); the left button's delivery,
;     one transition per frame (delivered and (up now or a press pending)
;     -> released; else a press pending -> pressed); inp_mb := delivered
;     left | right now; /FIR0 := stick fire | delivered left; inp_macc := 0.
; ---------------------------------------------------------------------------

.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"

.ifndef inp_src
.error "-D inp_src=... (runner.lbl)"
.endif
.ifndef inp_joy
.error "-D inp_joy=... (runner.lbl)"
.endif
.ifndef inp_mport
.error "-D inp_mport=... (runner.lbl)"
.endif
.ifndef inp_mb
.error "-D inp_mb=... (runner.lbl)"
.endif
.ifndef inp_ml
.error "-D inp_ml=... (runner.lbl)"
.endif
.ifndef inp_mlast
.error "-D inp_mlast=... (runner.lbl)"
.endif
.ifndef inp_mq
.error "-D inp_mq=... (runner.lbl)"
.endif
.ifndef inp_md
.error "-D inp_md=... (runner.lbl)"
.endif
.ifndef inp_macc
.error "-D inp_macc=... (runner.lbl)"
.endif
.ifndef inp_mps
.error "-D inp_mps=... (runner.lbl)"
.endif
.ifndef inp_mpot
.error "-D inp_mpot=... (runner.lbl)"
.endif
.ifndef inp_mold
.error "-D inp_mold=... (runner.lbl)"
.endif
.ifndef inp_mok
.error "-D inp_mok=... (runner.lbl)"
.endif
.ifndef inp_mon
.error "-D inp_mon=... (runner.lbl)"
.endif
.ifndef inp_mns
.error "-D inp_mns=... (runner.lbl)"
.endif
.ifndef inp_mrearm
.error "-D inp_mrearm=... (runner.lbl)"
.endif
.ifndef chip_shd
.error "-D chip_shd=... (runner.lbl)"
.endif
.ifndef cia_st
.error "-D cia_st=... (runner.lbl)"
.endif
.ifndef INPM_ORG
.error "-D INPM_ORG=... (runner.lbl)"
.endif

CIA_PIN   = 32                  ; @value cia.s CIA_PIN (inp.s INP_PIN)
JOY0      = $0A                 ; @value JOY0DAT in chip_shd (Y, then X)
QMAX      = 2                   ; @value pending left presses at most
MCLAMP    = 63                  ; @value W16-I: travel (|counts|) a frame-hook window (host/inp_ref.MCLAMP)

; --- A: $C980-$C9FE ----------------------------------------------------------
.segment "CODE"
entry:  jmp body
inpm_id: .byte 0, 0, 0, 0       ; the module ID, stamped by the host
.assert inpm_id = INPM_ORG + 3, lderror, "runtime/ops/inp.s reads the ID at INPM_ORG+3"
body:   ldy #3                  ; B present and the same build?
:       lda inpm_id,y
        cmp idb,y
        bne off
        dey
        bpl :-
        jmp bbody
off:    lda #0                  ; no: disarmed, never called again
        sta inp_mon
        jmp inp_mrearm
; look — Y := the mouse lines (1 = closed: bit 4 left, bit 0 right); a left
; press edge adds 1 to inp_mq (at most QMAX).  Keeps X.
look:   lda inp_ml
        ldy inp_src
        bne have
        lda #0                  ; PA7-6 released: columns 6/7 not driven
        sta CIA1_DDRA
        lda CIA1_PRB            ; port 1
        ldy inp_mport
        beq :+
        lda CIA1_PRA            ; port 2
:       ldy #$C0
        sty CIA1_DDRA           ; the pot select at rest again
        eor #$FF
have:   and #$11
        tay
        eor inp_mlast           ; (now ^ last) & now = closed now, open then
        sty inp_mlast
        and inp_mlast
        and #$10
        beq :+
        lda inp_mq
        cmp #QMAX
        bcs :+
        inc inp_mq
:       rts
; fira — the frame's end (moved from B, W16-I): /FIR0, then inp_macc := 0.
fira:
        ora inp_joy             ; /FIR0 low while stick fire or delivered left
        and #$10
        asl
        asl
        eor #$C0
        eor cia_st+CIA_PIN
        and #$C0
        eor cia_st+CIA_PIN
        sta cia_st+CIA_PIN
        lda #0                  ; a new frame: nothing added yet
        sta inp_macc
        sta inp_macc+1
        jmp inp_mrearm          ; (X = 0)
.assert entry = buf_case + $980, error, "inpm A: org buf_case + 980 (the supervisor's range)"
.assert * <= buf_case + $9FF, lderror, "inpm A: ends past buf_case + 9FE"

; --- B: $CF00-$CFEF ----------------------------------------------------------
.segment "CODEB"
entryb: jmp bbody
idb:    .byte 0, 0, 0, 0        ; the same ID, stamped by the host
bbody:  jsr look
        cpx #0
        bne :+
        jmp frame
:
        inc inp_mns             ; sample: counted (tests, u16)
        bne :+
        inc inp_mns+1
:
        ldx #1                  ; 1 = X (POTX), 0 = Y (POTY)
        lda inp_src
        bne lp
        lda SID_POTX+1          ; POTY
        sta inp_mpot
        lda SID_POTX
        sta inp_mpot+1
lp:     lda inp_mpot,x
        and #$7E
        sta inp_u               ; v
        ldy inp_mok
        bne :+
        sta inp_mold,x          ; reference only (direction +)
        beq nx                  ; (Z from ldy)
:       sec
        sbc inp_mold,x          ; v - ref (bit 7: the direction, dropped)
        and #$7F
        cmp #$40                ; C = 1: negative
        bcc :+
        ora #$80
:       ror                     ; d = signed / 2: -32..31
        beq nx                  ; no move
        tay
        clc
        adc #1
        cmp #3
        bcs mv                  ; |d| >= 2: a move
        tya
        eor inp_mold,x          ; W16-I: |d| = 1 against the last move's
        bmi nx                  ; direction: idle jitter, held (ref kept)
mv:     tya
        and #$80
        ora inp_u
        sta inp_mold,x          ; ref := v, bit 7 := d < 0
        tya
        cpx #1
        bcs :+
        eor #$FF                ; Y: negate (C = 0)
        adc #1
:       tay                     ; d, the axis's sense (-32..32)
        clc
        bpl :+
        eor #$FF                ; |d| = ~d + 1 (the carry)
        sec
:       adc inp_macc,x          ; travel + |d| (<= 95), saturated at MCLAMP
        cmp #MCLAMP+1
        bcc :+
        lda #MCLAMP
:       sta inp_u               ; the new travel (v is no longer needed)
        sec
        sbc inp_macc,x          ; |the part that may still be added|
        cpy #$80                ; C = 1: d < 0
        bcc :+
        eor #$FF                ; negated (C = 1)
        adc #0
:       clc
        adc chip_shd+JOY0,x
        sta chip_shd+JOY0,x
        lda inp_u
        sta inp_macc,x
nx:     dex
        bpl lp
        lda #1
        sta inp_mok
        ldx #3
        jmp inp_mrearm

frame:  lda inp_src             ; a port or source change: new reference
        asl
        ora inp_mport
        cmp inp_mps
        beq :+
        sta inp_mps
        lda #0
        sta inp_mok
:       lda inp_md              ; the left button: one transition a frame
        beq rel
        lda inp_mq              ; delivered: release when up now or a press
        bne up                  ; is pending
        tya
        and #$10
        bne keep
up:     lda #0
        beq set
rel:    lda inp_mq
        beq keep
        dec inp_mq
        lda #$10
set:    sta inp_md
keep:   tya                     ; inp_mb := delivered left | right now
        and #$01
        ora inp_md
        sta inp_mb
        jmp fira                ; (A: segment B is full; W16-I)

.assert entryb = buf_result + $700, error, "inpm B: org buf_result + 700"
.assert * <= buf_result + $7F0, lderror, "inpm B: ends past buf_result + 7EF"
