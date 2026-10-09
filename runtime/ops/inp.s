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
;      (rev-inp probe, 2026-10-09; the U64E is unmeasured).  F3 alone
;      reading as C= is checked by the attended script.
;   CIA1 DDRA and PRA are restored to their values on entry (PRA as read:
;   the 6526 returns the pins, so a bit an outside switch held low at entry
;   comes back as a 0 in the latch).  DDRB is never written.
;
; Per frame, from inp_joy/inp_key:
;   - mouse: dir = joy & $0F; n = frames the same nonzero dir was held
;     before this one (0 on a change or when centred; saturates at
;     INP_NMAX); step = 1 + n/2 (1, 1, 2, 2, ... INP_CAP); up/down -/+ step on JOY0DAT's Y counter (chip_shd+$0A),
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
.export inp_src, inp_joy, inp_key, inp_view, inp_n
.import chip_shd, cia_st, chip_vh

INP_CAP   = 16          ; @value step cap (counter units per frame)
INP_NMAX  = 2 * INP_CAP - 2 ; @value n saturates here: 1 + n/2 = INP_CAP
INP_F1    = $10         ; @value inp_key: F1 (column 0, row 4)
INP_CBM   = $20         ; @value inp_key: C= (column 7, row 5)
INP_PIN   = 32          ; @value cia.s CIA_PIN: CIA-A port A input pins
INP_JOY0  = $0A         ; @value JOY0DAT offset in chip_shd (Y, then X)

.segment "BSS"
inp_src:  .res 1        ; 0 = CIA1, else the host-written inp_joy/inp_key
inp_joy:  .res 1        ; stick, 1 = pressed: bits 0-4 U/D/L/R/F
inp_key:  .res 1        ; keys, 1 = down: INP_F1, INP_CBM
inp_view: .res 1        ; I1: view edges (bit 0 = F1 pressed), W13-D clears
inp_n:    .res 1        ; frames a direction was held (<= INP_NMAX)
inp_kp:   .res 1        ; the last frame's inp_key

; CODE layout: W13-I's CODE growth (this module + mach.s's 3-byte call) is
; padded to exactly one page (inp_pad below).  RODATA follows CODE, and its
; tables are read with abs,X on the per-instruction path; some straddle a
; page (g5_flow's 32-byte table), so any other shift changes which entries
; cross and costs cycles (perf_cycles: +1/3 "stack" at +207 B, +1/2
; mem/stream/copy at +3).  A whole page keeps every page offset after CODE.
; The pad costs no MAIN: BSS starts on a page anyway (end $9D11 either way).
.segment "CODE"
inp_beg:

.proc inp_frame
        lda inp_src
        bne have
        lda CIA1_DDRA           ; the caller's port A, restored below
        pha
        lda CIA1_PRA            ; (pins: the latch is not readable)
        pha
        lda #0                  ; 1. the stick, nothing driven (DDRB is
        sta CIA1_DDRA           ; $00 throughout: nothing here sets it)
        lda CIA1_PRA
        eor #$FF
        and #$1F
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
        pla                     ; the caller's port A back: latch first,
        sta CIA1_PRA            ; then the direction
        pla
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
        ldy inp_joy             ; left button: /FIR0 (pin 6) low while fire
        tya                     ; is held, /FIR1 (pin 7) high, pins 0-5 kept
        and #$10
        asl
        asl
        eor #$C0
        eor cia_st+INP_PIN
        and #$C0
        eor cia_st+INP_PIN
        sta cia_st+INP_PIN
        tya                     ; the mouse: centred -> n = 0
        and #$0F
        sta inp_u
        bne :+
        sta inp_n
:       lda inp_n               ; step - 1 = n / 2 (n <= INP_NMAX: step <= INP_CAP)
        tay
        lsr
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
        rts
.endproc

; inp_pot — chip.s's POTINP read (c_rdw, jmp): chip_vh := $FB while C= is
; held (bit 10, DATLY, low), else $FF; chip_vl stays 0.  Clobbers A.
.proc inp_pot
        lda inp_key
        and #INP_CBM            ; $20 -> $04
        lsr
        lsr
        lsr
        eor #$FF
        sta chip_vh
        rts
.endproc

INP_MACH  = 3           ; @value mach.s's W13-I CODE growth (jsr inp_frame)
inp_pad:  .res $100 - INP_MACH - (inp_pad - inp_beg), $EA
.assert inp_pad - inp_beg + INP_MACH <= $100, error, "inp: CODE growth over one page"
