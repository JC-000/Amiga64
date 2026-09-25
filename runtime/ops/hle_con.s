; ===========================================================================
; runtime/ops/hle_con.s — console input for dos.library Read on the console
; handles (kinds 0/1).  Owner: WAVE5 w5-con.  Contract: docs/WAVE5_BRIEF.md
; C2 (+ amendment).  Design, key map and tests: docs/HLE_CON.md.
;
; Entry (by jmp from hle.s hle_read): D1 = fh BPTR, D2 = buffer, D3 = length,
; return address on the guest stack.  Retire with `jmp hle_ret` (emulated
; RTS) after setting D0 (bytes read, 0 = EOF, -1 = error).  Only D0 is
; written.
;
; Cooked, line-buffered, like an AmigaOS CON: window:
;   - keys are edited into a line buffer and echoed through hle.s's console
;     output (hle_con_load / hle_putc / hle_con_save: same cursor, same lazy
;     scroll as Write); BS/DEL rub out the last character;
;   - RETURN ends the line ("\n" appended and echoed); Read hands out at most
;     D3 bytes of the finished line and keeps the rest for the next Read;
;   - Ctrl-\ ($1C, CTRL + pound) or Ctrl-D ($04) is EOF: on an empty line
;     Read returns 0; on a non-empty line the partial line is handed out
;     without a newline (the next EOF then returns 0).
;
; Key sources, in order: the injection queue hle_con_q (a BSS ring the host
; fills — test seam; a CR directly followed by LF there is one RETURN),
; then, when hle_con_src = 0 (keyboard), the CIA1 key matrix.  With
; hle_con_src = 1 (scripted) an empty queue is EOF.  The scan runs only
; while a Read waits (no IRQ, no type-ahead); it is debounced over
; CON_STABLE elapsed VIC raster lines, so the window is the same at every
; CPU speed, and every key that goes down in one debounce window is typed
; (matrix order).  Rows are allowed to settle back to idle between columns
; (48 MHz turbo; docs/HLE_CON.md "Matrix scan").
; No KERNAL calls (ROMs may be banked out; the runner runs with SEI).
;
; State lives in C64 BSS (not cleared at load), guarded by hle_con_magic and
; initialised lazily by the first Read — or by the host via hle_con_init.
; Mutation flags (tests/run_con.py, docs/HLE_CON.md): CON_MUT_NOECHO,
; CON_MUT_NOBS, CON_MUT_NOREM, CON_MUT_NOEOF, CON_MUT_NOSCAN,
; CON_MUT_NORASTER, CON_MUT_NORSHIFT.
; ===========================================================================

.include "../macros.inc"

.export hle_con_read, hle_con_init
.export hle_con_magic, hle_con_src, hle_con_q, hle_con_qh, hle_con_qt
.export hle_con_nkey, hle_con_mat, hle_con_nread       ; test seam / telemetry
.export hle_con_scan                                    ; test seam (run_con scan leg)
.import hle_ret, mem_write8
.import hle_putc, hle_con_load, hle_con_save, hle_cur, hle_col   ; hle.s (C2)

CON_LINE_MAX = 127              ; editable characters (+ the "\n" = 128 bytes)
CON_STABLE   = 64               ; raster lines a new matrix state must hold
                                ; before it counts (~4 ms: key bounce)
CON_WRAPLIM  = $80              ; per-sample raster delta >= this = frame wrap
CON_SETTLE   = 2                ; raster changes a row may take to settle
CON_PEND     = 8                ; decoded-key ring (power of two)
CON_SRC_SCRIPT = 1              ; hle_con_src: 0 keyboard, 1 scripted
KEY_BS       = $08
KEY_LF       = $0A
KEY_CR       = $0D
KEY_EOT      = $04              ; Ctrl-D
KEY_EOF      = $1C              ; Ctrl-\ (AmigaOS console EOF)
KEY_DEL      = $7F
CON_COLS     = 40               ; mirrors hle.s SCREEN_COLS (hle_putc's row width)

.segment "BSS"
hle_con_magic: .res 4           ; "CON!" once hle_con_init has run
hle_con_src:   .res 1           ; 0 keyboard (queue, then matrix), 1 scripted
hle_con_qh:    .res 1           ; queue head (next to consume)
hle_con_qt:    .res 1           ; queue tail (host appends here)
hle_con_nkey:  .res 1           ; matrix key-down events decoded (telemetry)
hle_con_nread: .res 2           ; console Reads serviced, u16 LE (telemetry)
hle_con_len:   .res 1           ; bytes in hle_con_line
hle_con_done:  .res 1           ; $FF: line finished, being handed out
hle_con_pos:   .res 1           ; next byte to hand out (done only)
hle_con_n:     .res 1           ; bytes this Read delivers
hle_con_cnt:   .res 1           ; copy loop counter
hle_con_ptr:   .res 3           ; guest write pointer (24-bit)
hle_con_ras:   .res 1           ; VIC_RASTER at the last valid sample
hle_con_stab:  .res 1           ; raster lines the raw matrix held still
hle_con_idle:  .res 1           ; port B with no column selected (joystick 1)
hle_con_sr:    .res 1           ; settle: last raster seen
hle_con_sn:    .res 1           ; settle: raster changes left
hle_con_chg:   .res 1           ; sample differed from hle_con_raw
hle_con_c:     .res 1           ; decode: column
hle_con_r:     .res 1           ; decode: row
hle_con_ph:    .res 1           ; pending ring head
hle_con_pn:    .res 1           ; pending ring count
hle_con_pend:  .res CON_PEND    ; decoded keys not yet handed to the editor
hle_con_smp:   .res 8           ; the sample being taken
hle_con_mat:   .res 8           ; debounced matrix, per column: 1 = key down
hle_con_raw:   .res 8           ; latest raw scan, same layout
hle_con_new:   .res 8           ; keys that went down at the last commit
hle_con_q:     .res 256         ; injection queue (u8 ring)
hle_con_line:  .res CON_LINE_MAX + 1

.segment "CODE"

; ===========================================================================
; hle_con_init — empty queue, empty line, keyboard source, and a matrix
; state of "every key down", so a key already held when input starts (the
; RETURN that launched the program) is not taken as a keystroke: keys count
; only after they have been seen up.  Callable by the host (jsr).
; ===========================================================================
hle_con_init:
        lda #0
        sta hle_con_src
        sta hle_con_qh
        sta hle_con_qt
        sta hle_con_nkey
        sta hle_con_nread
        sta hle_con_nread+1
        sta hle_con_len
        sta hle_con_done
        sta hle_con_pos
        sta hle_con_stab
        sta hle_con_ph
        sta hle_con_pn
        ldx #7
        lda #$FF
@m:     sta hle_con_mat,x
        sta hle_con_raw,x
        dex
        bpl @m
        lda VIC_RASTER
        sta hle_con_ras
        lda #'C'
        sta hle_con_magic
        lda #'O'
        sta hle_con_magic+1
        lda #'N'
        sta hle_con_magic+2
        lda #'!'
        sta hle_con_magic+3
        rts

hle_con_ensure:
        lda hle_con_magic
        cmp #'C'
        bne @init
        lda hle_con_magic+1
        cmp #'O'
        bne @init
        lda hle_con_magic+2
        cmp #'N'
        bne @init
        lda hle_con_magic+3
        cmp #'!'
        bne @init
        rts
@init:  jmp hle_con_init

; ===========================================================================
; hle_con_read — Read(fh D1, buf D2, len D3) on a console handle.
;   D3 negative or >= 16 MB -> -1; D3 = 0 -> 0 (nothing consumed, no wait);
;   else wait for a finished line (or EOF), hand out min(D3, rest of line).
; ===========================================================================
hle_con_read:
        jsr hle_con_ensure
        inc hle_con_nread
        bne @arg
        inc hle_con_nread+1
@arg:   lda r_d3+3              ; negative or >= 16 MB: no such buffer
        bne @fail
        lda r_d3
        ora r_d3+1
        ora r_d3+2
        bne @go
        jmp hle_con_ret0        ; D3 = 0
@fail:  lda #$FF
        sta r_d0
        sta r_d0+1
        sta r_d0+2
        sta r_d0+3
        jmp hle_ret

@go:    jsr hle_con_load        ; console cursor (echo), saved on every exit
@edit:  bit hle_con_done
        bmi @deliver
        jsr hle_con_key         ; C=1: A = key code
        bcc @edit
        jsr hle_con_edit        ; C=1: EOF on an empty line
        bcc @edit
        jsr hle_con_save
        jmp hle_con_ret0

@deliver:                       ; n = min(D3, len - pos); len > pos here
        sec
        lda hle_con_len
        sbc hle_con_pos
        sta hle_con_n
        lda r_d3+1
        ora r_d3+2
        bne @nok                ; D3 >= 256 > any rest of line
        lda r_d3
        cmp hle_con_n
        bcs @nok
        sta hle_con_n
@nok:   lda hle_con_n
        sta hle_con_cnt
        lda r_d2
        sta hle_con_ptr
        lda r_d2+1
        sta hle_con_ptr+1
        lda r_d2+2
        sta hle_con_ptr+2
@copy:  ldx hle_con_pos
        lda hle_con_line,x
        sta m_val
        lda hle_con_ptr
        sta m_addr
        lda hle_con_ptr+1
        sta m_addr+1
        lda hle_con_ptr+2
        sta m_addr+2
        lda #0
        sta m_addr+3
        jsr mem_write8
        inc hle_con_ptr
        bne @p
        inc hle_con_ptr+1
        bne @p
        inc hle_con_ptr+2
@p:     inc hle_con_pos
        dec hle_con_cnt
        bne @copy
.ifndef CON_MUT_NOREM
        lda hle_con_pos         ; whole line handed out?
        cmp hle_con_len
        bne @out                ; no: the rest waits for the next Read
.endif
        lda #0
        sta hle_con_len
        sta hle_con_pos
        sta hle_con_done
@out:   jsr hle_con_save
        lda hle_con_n
        sta r_d0
        lda #0
        sta r_d0+1
        sta r_d0+2
        sta r_d0+3
        jmp hle_ret

hle_con_ret0:
        lda #0
        sta r_d0
        sta r_d0+1
        sta r_d0+2
        sta r_d0+3
        jmp hle_ret

; ===========================================================================
; hle_con_key — next key code: C=1 + A = code, or C=0 (none yet: poll again).
; The queue first; then scripted -> EOF, keyboard -> the matrix.
; ===========================================================================
hle_con_key:
        ldy hle_con_qh
        cpy hle_con_qt
        beq @empty
        lda hle_con_q,y
        inc hle_con_qh
        cmp #KEY_CR             ; CR LF (a host "\r\n") is one RETURN
        bne @got
        ldy hle_con_qh
        cpy hle_con_qt
        beq @got
        ldx hle_con_q,y
        cpx #KEY_LF
        bne @got
        inc hle_con_qh
@got:   sec
        rts
@empty: lda hle_con_src
        cmp #CON_SRC_SCRIPT
        beq @eof
        jmp hle_con_scan
@eof:   lda #KEY_EOF            ; scripted input used up: end of input
        sec
        rts

; ===========================================================================
; hle_con_edit — line discipline for key code A.  C=1 only for EOF on an
; empty line (Read returns 0); otherwise C=0 (hle_con_done set when a line
; is ready).  Clobbers A/X/Y, t_tmp+4/5 (hle_putc).
; ===========================================================================
hle_con_edit:
        cmp #KEY_CR
        beq @eol
        cmp #KEY_LF
        beq @eol
        cmp #KEY_BS
        beq @bs
        cmp #KEY_DEL
        beq @bs
        cmp #KEY_EOF
        beq @eof
        cmp #KEY_EOT
        beq @eof
        cmp #$20
        bcc @ign                ; other controls
        cmp #$7F
        bcs @ign                ; >= $80
        ldx hle_con_len
        cpx #CON_LINE_MAX
        bcs @ign                ; line full: dropped
        sta hle_con_line,x
        inc hle_con_len
.ifndef CON_MUT_NOECHO
        jsr hle_putc            ; echo
.endif
@ign:   clc
        rts
@eol:   ldx hle_con_len
        lda #KEY_LF
        sta hle_con_line,x
        inc hle_con_len
        jsr hle_putc            ; echo the newline
@ready: lda #0
        sta hle_con_pos
        lda #$FF
        sta hle_con_done
        clc
        rts
@eof:
.ifdef CON_MUT_NOEOF
        clc
        rts
.endif
        lda hle_con_len
        bne @ready              ; partial line: hand it out as it is
        sec                     ; empty line: EOF
        rts
@bs:
.ifdef CON_MUT_NOBS
        clc
        rts
.endif
        lda hle_con_len
        beq @ign                ; nothing to rub out
        dec hle_con_len
        ; fall through

; hle_con_rubout — cursor back one cell (across a row start: column 39 of
; the row above; from the scroll-pending 1000 to 999) and blank that cell.
hle_con_rubout:
        lda hle_cur
        ora hle_cur+1
        beq @done               ; top-left: nothing before it
        lda hle_col
        beq @wrap
        dec hle_col
        jmp @cur
@wrap:  lda #CON_COLS - 1
        sta hle_col
@cur:   lda hle_cur
        bne @lo
        dec hle_cur+1
@lo:    dec hle_cur
        clc
        lda hle_cur
        adc #<SCREEN
        sta t_tmp+4
        lda hle_cur+1
        adc #>SCREEN
        sta t_tmp+5
        lda #' '
        ldy #0
        sta (t_tmp+4),y
@done:  clc
        rts

; ===========================================================================
; hle_con_scan — next key from the CIA1 matrix: C=1 + A = key code, else
; C=0.  Decoded keys wait in hle_con_pend; only when it is empty is a new
; sample taken.  Leaves CIA1_DDRA = $FF, CIA1_DDRB = $00, CIA1_PRA = $FF.
;
; One sample: columns are port A bits driven low one at a time, rows are
; port B bits read (low = down).
;   - Port A pins read back low with every column deselected = a joystick
;     in port 2 (it would select columns behind our back): sample skipped.
;   - Port B with every column deselected = the idle level; rows a joystick
;     in port 1 holds low are unreadable and masked out of this sample.
;   - Before each column, port B must be back at idle: a row a key pulled
;     low for the previous column recovers only through its pull-up, which
;     at 48 MHz can take longer than one instruction (phantom keys).  If it
;     does not settle within CON_SETTLE raster changes the sample is void.
; Debounce: a sample that differs from the last one restarts the window;
; the elapsed raster lines since the last valid sample are added
; (VIC_RASTER is the low 8 bits: a delta >= CON_WRAPLIM can only be the
; frame wrap, counted as 1 line).  After CON_STABLE lines the sample is
; committed; every key that went down since the previous commit, other
; than the modifiers, is decoded (column, then row order) into the ring.
; ===========================================================================
hle_con_scan:
        lda hle_con_pn
        beq @sample
        jmp @pop
@sample:
.ifdef CON_MUT_NOSCAN
        clc
        rts
.endif
        lda #$FF
        sta CIA1_DDRA           ; port A: outputs (column select)
        sta CIA1_PRA            ; no column selected
        lda #0
        sta CIA1_DDRB           ; port B: inputs (rows)
        lda CIA1_PRA            ; pins, not the latch
        cmp #$FF
        bne @none               ; joystick 2 active: skip the sample
        lda CIA1_PRB
        sta hle_con_idle        ; 0 bits: rows joystick 1 holds low
        ldx #7
@col:   jsr hle_con_settle
        bcc @none               ; rows never settled: void sample
        lda hle_con_colsel,x
        sta CIA1_PRA
        lda CIA1_PRB
        eor #$FF                ; 1 = down
        and hle_con_idle        ; joystick-held rows: unknown, taken as up
        sta hle_con_smp,x
        lda #$FF
        sta CIA1_PRA            ; deselect before the next column
        dex
        bpl @col
        lda #0
        sta hle_con_chg
        ldx #7
@cmp:   lda hle_con_smp,x
        cmp hle_con_raw,x
        beq @same
        sta hle_con_raw,x
        lda #$FF
        sta hle_con_chg
@same:  dex
        bpl @cmp
        lda VIC_RASTER          ; A = lines since the last valid sample
        tay
        sec
        sbc hle_con_ras
        sty hle_con_ras
        cmp #CON_WRAPLIM
        bcc @delta
        lda #1                  ; frame wrap of the 8-bit raster counter
@delta: bit hle_con_chg
        bpl @add
        lda #0                  ; changed: the window starts now
        sta hle_con_stab
        beq @none
@add:   ldy hle_con_stab
        cpy #CON_STABLE
        bcs @none               ; this state is already committed
.ifdef CON_MUT_NORASTER
        lda #1                  ; mutant: one "line" per sample
.endif
        clc
        adc hle_con_stab
        cmp #CON_STABLE
        bcs @commit
        sta hle_con_stab
@none:  clc
        rts

@pop:   ldx hle_con_ph
        lda hle_con_pend,x
        pha
        inx
        txa
        and #CON_PEND - 1
        sta hle_con_ph
        dec hle_con_pn
        pla
        sec
        rts

@commit:
        lda #CON_STABLE
        sta hle_con_stab
        ldx #7                  ; new = raw & ~mat (no modifiers), mat = raw
@cm:    lda hle_con_mat,x
        eor #$FF
        and hle_con_raw,x
        and hle_con_nomod,x
        sta hle_con_new,x
        lda hle_con_raw,x
        sta hle_con_mat,x
        dex
        bpl @cm
        lda #0
        sta hle_con_c
@dc:    lda #0
        sta hle_con_r
@db:    ldx hle_con_c
        lsr hle_con_new,x       ; C = this row's bit
        bcc @nr
        txa
        asl a
        asl a
        asl a
        ora hle_con_r
        jsr hle_con_decode      ; A = code, Z = no character
        beq @nr
        ldx hle_con_pn
        cpx #CON_PEND
        bcs @nr                 ; ring full: dropped
        pha
        txa
        clc
        adc hle_con_ph
        and #CON_PEND - 1
        tax
        pla
        sta hle_con_pend,x
        inc hle_con_pn
@nr:    inc hle_con_r
        lda hle_con_r
        cmp #8
        bne @db
        inc hle_con_c
        lda hle_con_c
        cmp #8
        bne @dc
        lda hle_con_pn
        beq @empty
        jmp @pop
@empty: clc
        rts

; hle_con_settle — wait until port B is back at hle_con_idle.  C=1 settled,
; C=0 not within CON_SETTLE raster changes (at least one full line).
; Preserves X.
hle_con_settle:
        lda VIC_RASTER
        sta hle_con_sr
        lda #CON_SETTLE
        sta hle_con_sn
@w:     lda CIA1_PRB
        cmp hle_con_idle
        beq @ok
        lda VIC_RASTER
        cmp hle_con_sr
        beq @w
        sta hle_con_sr
        dec hle_con_sn
        bne @w
        clc
        rts
@ok:    sec
        rts

; hle_con_decode — A = matrix index (column * 8 + row) of a key that just
; went down; counts it (hle_con_nkey) and returns its code with the
; committed modifiers (CTRL > SHIFT > plain).  Z=1: no character.
hle_con_decode:
        tay
        inc hle_con_nkey
        lda hle_con_mat+7       ; CTRL (column 7, row 2)
        and #$04
        bne @ctrl
        lda hle_con_mat+1       ; left SHIFT (column 1, row 7)
        and #$80
        bne @shift
.ifndef CON_MUT_NORSHIFT
        lda hle_con_mat+6       ; right SHIFT (column 6, row 4)
        and #$10
        bne @shift
.endif
        lda hle_con_kplain,y
        rts
@shift: lda hle_con_kshift,y
        rts
@ctrl:  lda hle_con_kplain,y    ; CTRL + letter -> 1..26, CTRL + pound -> $1C
        cmp #'\'
        beq @fs
        cmp #'a'
        bcc @no
        cmp #'z' + 1
        bcs @no
        and #$1F
        rts
@fs:    lda #KEY_EOF
        rts
@no:    lda #0
        rts

.segment "RODATA"
hle_con_colsel:                 ; port A value selecting column x
        .byte $FE, $FD, $FB, $F7, $EF, $DF, $BF, $7F
hle_con_nomod:                  ; per column: rows that are not modifiers
        .byte $FF, $7F, $FF, $FF, $FF, $FF, $EF, $DB   ; LSHIFT RSHIFT CTRL C=
; Key map, index column * 8 + row (docs/HLE_CON.md "Key map"; mirrored in
; tests/run_con.py KEYMAP_PLAIN / KEYMAP_SHIFT).  0 = no character.
hle_con_kplain:
        .byte KEY_BS, KEY_CR, 0, 0, 0, 0, 0, 0          ; DEL RET CRSR-R F7 F1 F3 F5 CRSR-D
        .byte "3wa4zse", 0                              ; ... LSHIFT
        .byte "5rd6cftx"
        .byte "7yg8bhuv"
        .byte "9ij0mkon"
        .byte "+pl-.:@,"
        .byte "\*;", 0, 0, "=^/"                        ; pound * ; HOME RSHIFT = up /
        .byte "1_", 0, "2 ", 0, "q", 0                  ; 1 left CTRL 2 SPACE C= Q STOP
hle_con_kshift:
        .byte 0, KEY_CR, 0, 0, 0, 0, 0, 0               ; INST RET ...
        .byte "#WA$ZSE", 0
        .byte "%RD&CFTX"
        .byte "'YG(BHUV"
        .byte ")IJ0MKON"
        .byte "{PL}>[`<"
        .byte "|*]", 0, 0, "=~?"
        .byte "!_", 0, $22, " ", 0, "Q", 0
.assert hle_con_kshift - hle_con_kplain = 64, error, "hle_con_kplain size"
