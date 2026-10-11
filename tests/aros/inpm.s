; ---------------------------------------------------------------------------
; tests/aros/inpm.s — W14-M M2: the input module (button edge queue), a
; machine-mode module (docs/WAVE14_BRIEF.md W14-M M2; docs/CONTRACTS.md
; "Input (joystick -> mouse)", "M2").
;
; NOT linked into the runner PRG: tests/aros_wb.py build_inpm() assembles
; it at test time to a raw binary at INPM_ORG ($C980, end <= $C9FE; the
; supervisor's range between aros_boot's VICE TRAMP at $C970 and mboot's
; AROS_ZERO at $C9FF) with the runner's labels as -D defines.  Its ID (4
; bytes at +3: SHA-1 of the binary with them zero) is stamped by the host;
; MAIN arms it by inp_mon := ID byte 0 (runtime/ops/inp.s inp_mod: a
; mismatch disarms, so an unstaged or different module is never called).
; All its state is MAIN BSS (inp_mlast, inp_mq, inp_md): a snapshot
; carries it, and a resume with the module re-staged continues.
;
; Entry INPM_ORG (jmp), X = 0 from the frame hook (end of inp_frame), X = 3
; from the 1351 sampler (inp_msamp); $01 = $35; clobbers A/X/Y; ends in
; inp_mrearm (X as on entry).
;   1. the mouse lines: inp_ml with the override, else CIA1 with DDRA =
;      $00 (PA7-6 released: no keyboard column driven), port 1 = PRB, port
;      2 = PRA; DDRA := $C0 again (the pot select at rest, PRA unchanged);
;      every left press edge (closed now, open at the last look) counts in
;      inp_mq.
;   2. (frame only) the LEFT button's delivery, one transition per guest
;      frame: delivered and (up now or another press pending) -> released;
;      not delivered and a press pending -> pressed (pending - 1).  So a
;      click shorter than a frame is one pressed frame, and two clicks
;      inside one frame are P, R, P, R over four frames (the 120 Hz sampler
;      sees them; W13-I/M1 sampled the buttons once per frame).  The right
;      button is not queued (the module's 127 B; menus hold it): it is the
;      lines now.  inp_mb := delivered left | right now (the POTINP read),
;      CIA-A /FIR0 := stick fire | delivered left.
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
.ifndef inp_mrearm
.error "-D inp_mrearm=... (runner.lbl)"
.endif
.ifndef cia_st
.error "-D cia_st=... (runner.lbl)"
.endif
.ifndef INPM_ORG
.error "-D INPM_ORG=... (runner.lbl)"
.endif

CIA_PIN   = 32                  ; @value cia.s CIA_PIN (inp.s INP_PIN)

.segment "CODE"
entry:  jmp body
inpm_id: .byte 0, 0, 0, 0       ; the module ID, stamped by the host
.assert inpm_id = INPM_ORG + 3, lderror, "runtime/ops/inp.s reads the ID at INPM_ORG+3"

body:   lda inp_ml              ; 1. the mouse lines (1 = closed)
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
        tay                     ; Y = the lines now
        tya                     ; a left press edge: pending + 1
        eor inp_mlast           ; (now ^ last) & now = closed now, open then
        sty inp_mlast
        and inp_mlast
        and #$10
        beq :+
        inc inp_mq
:       cpx #0                  ; X (kept): 0 = frame, else a sample:
        bne done                ; counts only
        lda inp_md              ; 2. delivery (left), one transition a frame
        beq rel
        lda inp_mq              ; delivered: release when up now or another
        bne up                  ; press is pending
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
keep:   tya                     ; inp_mb := delivered left | right now (the
        and #$01                ; right button is not queued: menus hold it)
        ora inp_md
        sta inp_mb
        ora inp_joy             ; /FIR0 low while stick fire or delivered left
        and #$10
        asl
        asl
        eor #$C0
        eor cia_st+CIA_PIN
        and #$C0
        eor cia_st+CIA_PIN
        sta cia_st+CIA_PIN      ; (X = 0 still: inp_mrearm's frame case)
done:   jmp inp_mrearm

.assert entry = buf_case + $980, error, "inpm: org buf_case + 980 (the supervisor's range)"
.assert * <= buf_case + $9FF, lderror, "inpm: the module ends past buf_case + 9FE"
