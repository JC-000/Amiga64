; ===========================================================================
; runtime/ops/inp.s — W13-I input: the C64 joystick in port 2 moves the
; Amiga port-0 mouse, fire is the left button, C= the right one, F1 a view
; key.  Owner: W13-I (w13-inp).  Contract: docs/CONTRACTS.md "Input
; (joystick -> mouse)" (W13-I); reference host/inp_ref.py; tests
; tests/run_inp.py, host/tests/test_inp.py.
;
; The only code that reads CIA1 (keyboard matrix, joystick port 2) in
; machine mode.  inp_frame runs once per machine-loop service pass that
; posted a VERTB (mach.s fhook, one labelled call line), $01 = $35,
; A/X/Y clobbered; nothing here is on the per-instruction path.
;
; Source: inp_src = 0 reads the real CIA1 into inp_joy/inp_key; inp_src
; != 0 takes inp_joy/inp_key as the host wrote them (the test override).
;   inp_joy  bits 0-4 = up/down/left/right/fire, 1 = pressed (the harness's
;            inject_joystick polarity)
;   inp_key  INP_F1 ($10) = F1, INP_CBM ($20) = C= (their matrix row bits)
;   inp_stuck  stick bits the real scan ignores (host-set; a switch stuck
;            closed, e.g. $01 = Up); the override path ignores it
;
; Scan (real source).  Port 2 drives the same port-A lines as the
; keyboard's column select, so:
;   1. stick: DDRA = $00, DDRB = $00 (no line driven): only the stick can pull
;      PA0-4 low, a held key cannot (no row is low) -> joy = ~PRA & $1F;
;   2. idle: DDRA = $FF, PRA = $FF (no column): PRB bits that read low are
;      rows held by stick 1, or by a key in a column stick 2 selects; that
;      row's key is unknown this frame and keeps its last state (F1 with
;      stick up; C= with S/F/H/K/F3 held while the stick moves);
;   3. F1: column 0 (PRA $FE), row 4; then C=: column 7 (PRA $7F), row 5.
;      No settle wait between them: rows recover at once on the C64U at 1,
;      48 and 64 MHz, and a column 1 -> column 7 read shows no phantom
;      (rev-inp, build/evidence/w13-inp/settle/; the U64E is unmeasured).  F3 alone
;      reading as C= is checked by the attended script.
;   W14-M: on exit CIA1 port A rests selecting the mouse port's pots for
;   the SID (PA7-6 = 01 port 1 / 10 port 2, DDRA = $C0; PA7/PA6 are also
;   keyboard columns 7/6, so every port-1 read is made with DDRA = $00).
;   DDRB is never written.  The 1351 decode and the button queue are the
;   input module's (tests/aros/inpm.s, $C980 + $CF00), called through
;   inp_mod's ID guard; no module: the stick and the mouse's buttons only.
;
; Per frame, from inp_joy/inp_key:
;   - mouse: dir = joy & $0F; n = frames the same nonzero dir was held
;     before this one (0 on a change or when centred; saturates at
;     INP_NMAX); step = 1 + n (1, 2, 3, ... INP_CAP; W14-M: was 1 + n/2, cap 16); up/down -/+ step on JOY0DAT's Y counter (chip_shd+$0A),
;     left/right on X (chip_shd+$0B), u8 wrap.
;   - left button: CIA-A PRA pin bit 6 (/FIR0) low while fire is held; bit
;     7 (/FIR1) high; pin bits 0-5 untouched (read-modify-write).
;   - right button: chip.s's POTINP read jumps to inp_pot: high byte $FB
;     (bit 10, DATLY, low) while inp_key has C= down, else $FF.
;   - inp_view bit 0 |= 1 on an F1 press edge (W13-D clears it).
; All state is MAIN BSS (zero = idle), so a snapshot carries it.
; Link layout: after mach.s, before snap.s (Makefile).
; ===========================================================================

.include "../macros.inc"

.export inp_frame, inp_pot
.export inp_src, inp_joy, inp_key, inp_view, inp_n, inp_stuck
.export inp_msamp, inp_mport, inp_mper, inp_mpot, inp_mb, inp_mok
.export inp_mon, inp_ml, inp_mlast, inp_mq, inp_md, inp_macc, inp_mps, inp_mns, inp_mold, inp_mrearm, INPM_ORG
.import chip_shd, cia_st, chip_vh
.import mach_etime, mach_arm, mach_et, mach_at   ; W14-M: slot 7

INP_CAP   = 32          ; @value step cap (counter units per frame; W14-M: 16 -> 32)
INP_NMAX  = INP_CAP - 1 ; @value n saturates here: 1 + n = INP_CAP
INP_F1    = $10         ; @value inp_key: F1 (column 0, row 4)
INP_CBM   = $20         ; @value inp_key: C= (column 7, row 5)
INP_PIN   = 32          ; @value cia.s CIA_PIN: CIA-A port A input pins
INP_JOY0  = $0A         ; @value JOY0DAT offset in chip_shd (Y, then X)
INP_MSLOT = 7           ; @value W14-M: mach event slot of the 1351 sampler
INPM_ORG  = buf_case + $980 ; @value W14-M: the M2 module (tests/aros/inpm.s), $C980-$C9FE

.segment "BSS"
inp_src:  .res 1        ; 0 = CIA1, else the host-written inp_joy/inp_key
inp_joy:  .res 1        ; stick, 1 = pressed: bits 0-4 U/D/L/R/F
inp_key:  .res 1        ; keys, 1 = down: INP_F1, INP_CBM
inp_view: .res 1        ; I1: view edges (bit 0 = F1 pressed), W13-D clears
inp_n:    .res 1        ; frames a direction was held (<= INP_NMAX)
inp_kp:   .res 1        ; the last frame's inp_key
inp_stuck: .res 1       ; host: stick bits the real scan ignores (1 = stuck closed)
inp_mport: .res 1       ; W14-M host: mouse port, 0 = port 1 (stick on 2), 1 = port 2
inp_mper: .res 4        ; W14-M host: 1351 sample period, E ticks, u32 (0 = no sampling)
inp_mpot: .res 2        ; W14-M POTY, POTX: override input (inp_src != 0), else the last SID read
inp_mb:   .res 1        ; W14-M mouse port lines, 1 = closed (bit 4 left, bit 0 right)
inp_mold: .res 2        ; W14-M ref POTY, POTX (bits 1-6); W16-I: bit 7 = last direction
inp_mok:  .res 1        ; W14-M nonzero once inp_mold holds a sample
inp_mon:  .res 1        ; W14-M host: the M2 module's ID byte 0 (0 = none)
inp_ml:   .res 1        ; W14-M M2 override: mouse lines (inp_src != 0, module armed)
inp_mlast: .res 1       ; W14-M M2: lines at the last look (press edges)
inp_mq:   .res 1        ; W14-M M2: pending left presses (<= 2)
inp_md:   .res 1        ; W14-M M2: the buttons as delivered (bit 4 left, 0 right)
inp_macc: .res 2        ; W14-M M2: 1351 counts added since the frame hook: Y, X (s8, +-127)
inp_mps:  .res 1        ; W14-M M2: inp_mport | inp_src << 1 at the last frame (a change: new reference)
inp_mns:  .res 2        ; W14-M M2: 1351 samples taken (u16 LE, wraps; tests)

; CODE layout: no pad needed.  RODATA is page-aligned (c64.cfg, Wave 13
; supervisor), so CODE growth no longer moves which abs,X table entries
; straddle a page (perf_cycles stays at delta 0).
.segment "CODE"
inp_beg:

.proc inp_frame
        lda inp_src
        bne have
        lda #0                  ; 1. both ports, nothing driven (DDRB is
        sta CIA1_DDRA           ; $00 throughout: nothing here sets it)
        lda CIA1_PRB            ; port 1 (rows: no column driven, so no key)
        eor #$FF
        and #$1F
        tay
        lda CIA1_PRA            ; port 2
        eor #$FF
        and #$1F
        ldx inp_mport           ; W14-M: the mouse on port 2: swap
        beq :+
        sty inp_t
        tay
        lda inp_t
:       sty inp_mb              ; the mouse port's lines (buttons)
        ora inp_stuck           ; the stick's port; stuck lines read as
        eor inp_stuck           ; released
        sta inp_joy
        ldx #$FF                ; 2. idle rows, no column selected: the
        stx CIA1_PRA            ; latch high first, then the outputs on
        stx CIA1_DDRA           ; (no old-latch glitch on PA)
        lda CIA1_PRB
        sta inp_t
        lda #$FE                ; 3. F1: column 0, row 4
        sta CIA1_PRA
        lda CIA1_PRB
        ora #<~INP_F1
        sta inp_u
        lda #$7F                ; C=: column 7, row 5
        sta CIA1_PRA
        lda CIA1_PRB
        ora #<~INP_CBM
        and inp_u               ; rows 4/5: 0 = down, other bits 1
        eor #$FF                ; 1 = down
        eor inp_key             ; unreadable rows (idle bit 0) keep the
        and inp_t               ; last state: ((new ^ old) & idle) ^ old
        eor inp_key
        sta inp_key
        lda #$40                ; W14-M: rest with PA7-6 selecting the
        ldx inp_mport           ; mouse port's pots for the SID (01 =
        beq :+                  ; port 1, 10 = port 2), latch first
        asl
:       sta CIA1_PRA
        lda #$C0
        sta CIA1_DDRA
have:   lda inp_kp              ; F1 press edge -> inp_view bit 0
        eor #$FF
        and inp_key
        and #INP_F1             ; $10 -> 1, or 0
        lsr
        lsr
        lsr
        lsr
        ora inp_view
        sta inp_view
        lda inp_key
        sta inp_kp
        lda inp_joy             ; left button: /FIR0 (pin 6) low while fire
        ora inp_mb              ; (stick or mouse) is held, /FIR1 (pin 7)
        and #$10                ; high, pins 0-5 kept
        asl
        asl
        eor #$C0
        eor cia_st+INP_PIN
        and #$C0
        eor cia_st+INP_PIN
        sta cia_st+INP_PIN
        lda inp_joy             ; the stick: centred -> n = 0
        and #$0F
        sta inp_u
        bne :+
        sta inp_n
:       lda inp_n               ; step - 1 = n (n <= INP_NMAX: step <= INP_CAP)
        tay
        sta inp_t
        cpy #INP_NMAX
        bcs :+
        inc inp_n
:       ldx #0                  ; bit i: axis i/2 (Y, X), odd i adds
lp:     lsr inp_u
        bcc nx
        txa
        lsr
        tay
        lda inp_t               ; step - 1
        bcs add                 ; C = 1: + (step - 1) + 1
        eor #$FF                ; C = 0: + ~(step - 1) = - step
add:    adc chip_shd+INP_JOY0,y
        sta chip_shd+INP_JOY0,y
nx:     inx
        cpx #4
        bne lp
        ldx #0                  ; W14-M: the M2 module (frame), then the
        jmp inp_mod             ; re-arm when sampling is on
.endproc

; inp_msamp — W14-M: mach slot INP_MSLOT's callback (callback rules; the
; host stores it in mach_cbv[INP_MSLOT]).  The 1351's decode lives in the
; input module (tests/aros/inpm.s, fix round 1: it is AROS-only): this is
; X = 3 into inp_mod's ID guard.  No module: no 1351 position (the slot
; keeps re-arming, nothing is read).
.proc inp_msamp
        ldx #3
.endproc
; fall through
; inp_mod — W14-M: the M2 module (tests/aros/inpm.s at INPM_ORG) when
; armed: inp_mon = the first byte of its ID (INPM_ORG+3); a mismatch (not
; staged, a different build) disarms it.  X = 0 frame, 3 sample.  Ends in
; inp_mrearm (the module jumps there too).
.proc inp_mod
        lda inp_mon
        beq none
        cmp INPM_ORG+3
        bne off
        jmp INPM_ORG
off:    lda #0
        sta inp_mon
none:   jmp inp_mrearm
.endproc

.segment "LOWCODE"
; marm — W14-M: arm slot INP_MSLOT at now + inp_mper (u32) E ticks.  LOWCODE:
; the STARTUP->DISPATCH slack (no MAIN page moves).
; inp_mrearm — W14-M: X = 0 (the frame hook): re-arm when sampling is on
; (inp_mper != 0); X != 0 (a sample): re-arm.
.proc inp_mrearm
        txa
        bne marm
        lda inp_mper
        ora inp_mper+1
        bne marm
        rts
.endproc
.proc marm
        jsr mach_etime
        clc
        ldx #0
:       lda mach_et,x
        adc inp_mper,x
        sta mach_at,x
        inx
        cpx #4
        bne :-
        lda #INP_MSLOT
        jmp mach_arm
.endproc
.segment "CODE"

; inp_pot — chip.s's POTINP read (c_rdw, jmp): chip_vh := $FB while C= is
; held (bit 10, DATLY, low), else $FF; chip_vl stays 0.  Clobbers A.
.proc inp_pot
        lda inp_key
        and #INP_CBM            ; $20 -> $04
        lsr
        lsr
        lsr
        sta inp_t
        lda inp_mb              ; W14-M: or the mouse's right button (its
        and #$01                ; port's UP line) -> $04
        asl
        asl
        ora inp_t
        eor #$FF
        sta chip_vh
        rts
.endproc

INP_MACH  = 3           ; @value mach.s's W13-I CODE growth (jsr inp_frame)
