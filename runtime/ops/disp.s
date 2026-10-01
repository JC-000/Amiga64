; ===========================================================================
; runtime/ops/disp.s — W10-V display refresh driver (docs/S3_PLAN.md §2,
; §4 "S3.5"; docs/WAVE10_BRIEF.md C1/C3).  Owner: W10-V (disp).  Spec:
; docs/C2V.md "Display refresh (S3.5)"; host model: host/c2v_ref.py
; disp_decide / disp_convert.
;
; Reads the display-state block disp_st (C1, written by W10-P's copper and
; register paths) and shows it on the VIC-II through the c2v kernels:
;
;   2-plane lores   -> c2v_mc2s   (multicolour 160x200, exact colours)
;   4-plane hires   -> c2v_hiress (hires 320x200 silhouette, v0)
;   anything else   -> blank + log (disp_why; disp_nblank counts)
;
; Link: disp_init stores disp_frame in W10-P's RAM vector cop_dhook (high
; byte 0 = none).  cop_frame calls it at the end of every frame, and a
; synchronous COPJMP run calls it after the copper stops.  Neither module
; links against the other's code: disp_st is an rc equate and cop_dhook a
; cop.s BSS export that disp.s imports (before W10-P lands, a placeholder
; below defines both).  disp_st is RAM under BASIC ROM; its valid byte (+103)
; must be $A5 (cop_reset ran), else the state is ignored (blank, why 7).
;
; Refresh policy v0 (docs/PERF.md "Display refresh"):
;   * convert only when disp_st's gen changed AND the copied fields differ
;     from the last ones taken (the copper rewriting equal values costs one
;     70-byte compare, no conversion);
;   * inside mach_run (mach_in <> 0) at most one conversion per DISP_N
;     guest frames (mach_frames since the last conversion's start); a
;     deferred change is not consumed, so a later call converts it;
;   * outside mach_run (HLE runners) every gen change converts at once;
;   * disp_refresh converts unconditionally (no gen, compare or budget);
;   * no dirty tracking of bitplane writes (backlog).
;
; Entry points (JSR; clobber A/X/Y; call with I/O mapped in and no IRQ that
; touches the REC; each restores the caller's $01 and ends REC use with
; mem_rec_dirty, C1):
;   disp_init     c2v_init, stash the packed LUT to disp_lut in the REU,
;                 reset the state, hook disp_frame into cop_dhook
;   disp_frame    the per-frame / per-COPJMP hook (policy above)
;   disp_refresh  convert now from the current disp_st
;
; Test-only variants (tests/run_pf.py): DISP_STUB (every entry rts),
; DISP_MUT_GEN (a gen change is never seen), DISP_MUT_PAL (palette taken
; from color[i+1]), DISP_MUT_PPORD (plane pointers 0/1 and 2/3 swapped),
; DISP_MUT_MOD (BPLxMOD ignored), DISP_MUT_STRADDLE (no 512 KB line check),
; DISP_MUT_CLR (lines below DIWSTOP not cleared), DISP_MUT_BUDGET (no
; duty budget), DISP_MUT_BANK (BASIC ROM left in: disp_st read as ROM).
;
; Link layout: CODE and BSS are each padded to whole pages, no RODATA or
; DATA, so modules linked after this one move by whole pages only.
; ===========================================================================

.include "../macros.inc"

.export disp_init, disp_frame, disp_refresh
.export disp_mode, disp_why, disp_nconv, disp_nblank, disp_seen, disp_ok
.export disp_cur, disp_lastf, disp_lines, disp_crop
.export DISP_N, disp_lut       ; disp_lut: the rc equate, for the host
.import c2v_init, c2v_pal_mc2, c2v_pal_hires, c2v_mc2s, c2v_hiress
.import mem_rec_dirty, mach_in
.ifdef disp_st
.import cop_dhook               ; cop.s BSS (consumer -> provider, as cia.s
                                ; imports mach_cbv)
.else
; PRE-MERGE PLACEHOLDER (supervisor, Wave 10 item 5): until W10-P's rc
; claim disp_st exists, this module defines the C1 block and the hook
; vector itself so the default tree links.  It disappears by itself when
; W10-P lands (disp_st becomes an rc equate, cop_dhook comes from cop.s).
.export disp_st, cop_dhook
DISP_PREMERGE = 1
.endif

.ifndef DISP_N
DISP_N     = 20                 ; duty budget: frames per conversion (PERF (j))
.endif
.ifndef C2V_ROWS
C2V_ROWS   = 25                 ; must match c2v.s (both take the same -D)
.endif
DISP_LINES = C2V_ROWS * 8       ; lines the kernels convert

; Modes (disp_mode) and blank reasons (disp_why), = host/c2v_ref.py
DISP_NONE  = $FF                ; nothing shown yet: the VIC is untouched
DISP_BLANK = 0
DISP_MC2   = 1
DISP_HIRES = 2
DISP_WHY_DMA      = 1                ; DMACON lacks DMAEN or BPLEN
DISP_WHY_HAMDPF   = 2                ; BPLCON0 HAM or DBLPF
DISP_WHY_DEPTH    = 3                ; not 2-plane lores / 4-plane hires
DISP_WHY_DIW      = 4                ; no visible line
DISP_WHY_DDF      = 5                ; fetch narrower than the row / skip overflow
DISP_WHY_STRADDLE = 6                ; a row crosses a 512 KB REU line or $FFFFFF
DISP_WHY_NOSTATE  = 7                ; disp_st not valid (cop_reset never ran)

; The snapshot = disp_st + 1 .. + 70 (C1 offsets minus 1), then valid
DISP_SNAP  = 71                 ; +70 holds disp_st+103 (valid)
DISP_S_DMACON   = 0
DISP_S_BPLCON0  = 2
DISP_S_BPLPT    = 8                  ; 6 x u24
DISP_S_BPL1MOD  = 26                 ; then BPL2MOD at +28
DISP_S_DIWSTRT  = 30
DISP_S_DIWSTOP  = 32
DISP_S_DDFSTRT  = 34
DISP_S_DDFSTOP  = 36
DISP_S_COLOR    = 38                 ; colours 0-15 (u16 LE)
DISP_S_VALID    = 70                 ; copy of disp_st + DISP_ST_VALID
DISP_ST_VALID   = 103                ; @value W10-P: valid byte offset in disp_st
DISP_COPVALID   = $A5                ; @value W10-P: valid once cop_reset ran

; c2v argument block (docs/C2V.md "Argument block", "Row skip")
DISP_A_SRC     = c2v_args + 0       ; 4 x u24
DISP_A_LUT     = c2v_args + 12      ; u24
DISP_A_PAL     = c2v_args + 16      ; 16 x u16
DISP_A_BGC     = c2v_args + 48      ; u8 out
DISP_A_SKIP    = c2v_args + 72      ; 4 x s16

; VIC setup for a shown picture: the bank holding bitmap and matrix
DISP_BANKOFS = $3FFF            ; @value offset mask within a 16 KB VIC bank
DISP_BMHALF  = $2000            ; @value bitmap half bit ($D018 bit 3)
DISP_BMALIGN = $1FFF            ; @value bitmap alignment mask (8 KB)
DISP_VBANK = 3 - (c2v_bitmap >> 14)
DISP_VMEM   = ((c2v_screen & DISP_BANKOFS) >> 6) | ((c2v_bitmap & DISP_BMHALF) >> 10)
DISP_VON       = $3B              ; bitmap mode, display on, 25 rows, yscroll 3
DISP_VOFF      = $0B              ; display off: the border colour everywhere
DISP_VMC       = $18              ; multicolour, 40 columns
DISP_VHI       = $08              ; hires, 40 columns

.assert DISP_N >= 1 .and DISP_N <= 255, error, "DISP_N must fit a byte"
.assert (c2v_screen >> 14) = (c2v_bitmap >> 14), error, "matrix and bitmap must share a VIC bank"
.assert (c2v_bitmap & DISP_BMALIGN) = 0, error, "VIC bitmap must sit at bank + $0000 or + $2000"
.assert (c2v_screen & $3FF) = 0, error, "VIC screen matrix must be 1 KB aligned"
.assert (disp_lut & $FF) = 0, error, "disp_lut must be page-aligned (256-byte stashes)"
.assert (disp_lut >> 19) = ((disp_lut + $FFF) >> 19), error, "disp_lut must not cross a 512 KB REU line"

; DISP_ENTER — bank BASIC ROM out if it is in (disp_st and the c2v block
; live under it), the caller's $01 saved on the stack; disp_leave restores.
.macro DISP_ENTER
        lda CPU_PORT
        pha
.ifndef DISP_MUT_BANK
        and #3
        cmp #3
        bne :+
        lda CPU_PORT
        and #$FE
        sta CPU_PORT
:
.endif
.endmacro

; ---------------------------------------------------------------------------
.segment "BSS"
disp_b0:
.ifdef DISP_PREMERGE
disp_st:    .res 104            ; placeholder: C1 + valid (+103)
cop_dhook:  .res 2              ; placeholder hook vector
.endif
disp_new:   .res DISP_SNAP      ; fields as just copied
disp_cur:   .res DISP_SNAP      ; fields of the last decision
disp_seen:  .res 1              ; gen when the fields were last copied
disp_ok:    .res 1              ; 0 until the first copy
disp_mode:  .res 1              ; DISP_NONE / BLANK / MC2 / HIRES
disp_why:   .res 1              ; last decision: 0 shown, else DISP_WHY_*
disp_nconv: .res 2              ; conversions (u16, wraps)
disp_nblank: .res 2             ; blank decisions (u16, wraps)
disp_lastf: .res 4              ; mach_frames at the last conversion
disp_lines: .res 1              ; lines shown (<= DISP_LINES)
disp_crop:  .res 1              ; rows skipped at the top (centre crop)
disp_dm:    .res 1              ; candidate mode
disp_np:    .res 1              ; planes converted (2 / 4)
disp_wid:   .res 1              ; row bytes converted (40 / 80)
disp_wm1:   .res 1              ; disp_wid - 1
disp_fetch: .res 1              ; DDF fetch width in bytes
disp_skl:   .res 4              ; row skip per plane, s16 lo
disp_skh:   .res 4              ;                     hi
disp_adl:   .res 4              ; row advance per plane, s24
disp_adh:   .res 4
disp_adb:   .res 4
disp_srl:   .res 4              ; first converted row per plane, u24
disp_srh:   .res 4
disp_srb:   .res 4
disp_w:     .res 3              ; row walk
disp_t:     .res 4              ; scratch
disp_b1:
        .res (256 - ((disp_b1 - disp_b0) .mod 256)) .mod 256

.segment "CODE"
disp_code0:

.ifdef DISP_STUB
disp_init:
disp_frame:
disp_refresh:
        rts
.else

; ---------------------------------------------------------------------------
; disp_init
; ---------------------------------------------------------------------------
disp_init:
        jsr c2v_init
        DISP_ENTER
        lda #<disp_lutp         ; unpack 128 packed bytes -> 256 LUT bytes
        sta @rd+1               ; into c2v_stage, stash them; 16 times
        lda #>disp_lutp
        sta @rd+2
        lda #0
        sta disp_t
@chunk: ldy #0
        ldx #0
@rd:    lda disp_lutp,y         ; operand patched
        pha
        and #$0F
        sta c2v_stage,x
        inx
        pla
        lsr
        lsr
        lsr
        lsr
        sta c2v_stage,x
        inx
        iny
        bpl @rd                 ; Y = 0..127
        lda #<c2v_stage
        sta REC_C64LO
        lda #>c2v_stage
        sta REC_C64HI
        lda #<disp_lut
        sta REC_REULO
        clc
        lda #>disp_lut
        adc disp_t
        sta REC_REUHI
        lda #^disp_lut
        adc #0
        sta REC_REUBANK
        lda #0
        sta REC_LENLO
        sta REC_ACTRL
        lda #1
        sta REC_LENHI           ; 256 bytes
        lda #REU_CMD_STASH
        sta REC_COMMAND
        clc
        lda @rd+1
        adc #$80
        sta @rd+1
        bcc :+
        inc @rd+2
:       inc disp_t
        lda disp_t
        cmp #16
        bne @chunk
        jsr mem_rec_dirty       ; C1
        lda #0
        sta disp_ok
        sta disp_why
        sta disp_nconv
        sta disp_nconv+1
        sta disp_nblank
        sta disp_nblank+1
        lda #DISP_NONE
        sta disp_mode
        sec                     ; the first conversion is never deferred
        lda mach_frames
        sbc #DISP_N
        sta disp_lastf
        lda mach_frames+1
        sbc #0
        sta disp_lastf+1
        lda mach_frames+2
        sbc #0
        sta disp_lastf+2
        lda mach_frames+3
        sbc #0
        sta disp_lastf+3
        lda #0                  ; the hook: high byte last (0 = none)
        sta cop_dhook+1
        lda #<disp_frame
        sta cop_dhook
        lda #>disp_frame
        sta cop_dhook+1
        pla
        sta CPU_PORT
        rts

; ---------------------------------------------------------------------------
; disp_frame — the cop_dhook target.
; ---------------------------------------------------------------------------
disp_frame:
        DISP_ENTER              ; disp_st is RAM under BASIC ROM
        lda disp_ok
        beq @go                 ; first call: always decide
        lda disp_st             ; C1 +0 gen
        cmp disp_seen
.ifdef DISP_MUT_GEN
        jmp @rts                ; mutant: a gen change is never seen
.else
        beq @rts                ; unchanged
.endif
.ifdef DISP_MUT_BUDGET
        jmp @go                 ; mutant: no duty budget
.else
        lda mach_in
        beq @go                 ; HLE: no budget
        sec                     ; frames since the last conversion
        lda mach_frames
        sbc disp_lastf
        sta disp_t
        lda mach_frames+1
        sbc disp_lastf+1
        sta disp_t+1
        lda mach_frames+2
        sbc disp_lastf+2
        sta disp_t+2
        lda mach_frames+3
        sbc disp_lastf+3
        ora disp_t+1
        ora disp_t+2
        bne @go                 ; >= 256
        lda disp_t
        cmp #DISP_N
        bcs @go
.endif
@rts:   jmp disp_leave          ; deferred: gen stays unseen
@go:    lda disp_st
        sta disp_seen
        jsr disp_snap
        lda disp_ok
        beq disp_take
        ldx #DISP_SNAP - 1
@cmp:   lda disp_new,x
        cmp disp_cur,x
        bne disp_take
        dex
        bpl @cmp
        jmp disp_leave          ; same values: nothing to redo

; ---------------------------------------------------------------------------
; disp_refresh — convert now.
; ---------------------------------------------------------------------------
disp_refresh:
        DISP_ENTER
        lda disp_st
        sta disp_seen
        jsr disp_snap
        ; fall through

; disp_take — disp_cur := disp_new, then decide and show.  Entered with
; DISP_ENTER's $01 on the stack.
disp_take:
        ldx #DISP_SNAP - 1
:       lda disp_new,x
        sta disp_cur,x
        dex
        bpl :-
        lda #1
        sta disp_ok
        jsr disp_decide
        bcs @blank
        jsr disp_args
        lda disp_dm
        cmp #DISP_MC2
        bne @hires
        jsr c2v_pal_mc2
        jsr c2v_mc2s
        jmp @shown
@hires: jsr c2v_pal_hires
        jsr c2v_hiress
@shown: jsr disp_clrlines
        jsr disp_vicon
        lda #0
        sta disp_why
        lda disp_dm
        sta disp_mode
        inc disp_nconv
        bne :+
        inc disp_nconv+1
:       lda mach_frames
        sta disp_lastf
        lda mach_frames+1
        sta disp_lastf+1
        lda mach_frames+2
        sta disp_lastf+2
        lda mach_frames+3
        sta disp_lastf+3
        jmp @out
@blank: inc disp_nblank
        bne :+
        inc disp_nblank+1
:       lda disp_mode
        cmp #DISP_NONE
        beq @out                ; nothing shown yet: log only
        lda #DISP_BLANK
        sta disp_mode
        jsr disp_border
@out:
        ; fall through
disp_leave:
        pla
        sta CPU_PORT
        rts

; disp_snap — disp_new := disp_st + 1 .. + 70, then disp_st's valid byte.
disp_snap:
        ldx #DISP_S_VALID - 1
:       lda disp_st + 1,x
        sta disp_new,x
        dex
        bpl :-
        lda disp_st + DISP_ST_VALID
        sta disp_new + DISP_S_VALID
        rts

; ---------------------------------------------------------------------------
; disp_decide — from disp_cur: C = 0 and the plane arguments (disp_dm, np,
; wid, lines, crop, skl/skh, adl/adh/adb, srl/srh/srb) to show; C = 1 and
; disp_why to blank.  Same checks, same order as c2v_ref.disp_decide.
; ---------------------------------------------------------------------------
disp_decide:
        lda #DISP_WHY_NOSTATE
        sta disp_why
        lda disp_cur + DISP_S_VALID
        cmp #DISP_COPVALID
        beq :+
        sec                     ; no display state yet
        rts
:       lda #DISP_WHY_DMA
        sta disp_why
        lda disp_cur + DISP_S_DMACON + 1
        and #$03                ; DMAEN (bit 9), BPLEN (bit 8)
        cmp #$03
        bne @no
        inc disp_why            ; DISP_WHY_HAMDPF
        lda disp_cur + DISP_S_BPLCON0 + 1
        and #$0C                ; HAM (bit 11), DBLPF (bit 10)
        bne @no
        inc disp_why            ; DISP_WHY_DEPTH
        lda disp_cur + DISP_S_BPLCON0 + 1
        and #$F0                ; HIRES (bit 15), BPU (bits 14-12)
        cmp #$20
        bne @hi
        lda #DISP_MC2
        ldx #40
        ldy #2
        bne @md
@hi:    cmp #$C0
        bne @no
        lda #DISP_HIRES
        ldx #80
        ldy #4
@md:    sta disp_dm
        stx disp_wid
        dex
        stx disp_wm1
        sty disp_np
        inc disp_why            ; DISP_WHY_DIW
        lda disp_cur + DISP_S_DIWSTOP + 1
        asl                     ; C = bit 7 of the stop line
        lda #0
        rol
        eor #1                  ; V8 = NOT V7
        sta disp_t + 1
        sec
        lda disp_cur + DISP_S_DIWSTOP + 1
        sbc disp_cur + DISP_S_DIWSTRT + 1
        sta disp_t
        lda disp_t + 1
        sbc #0
        sta disp_t + 1          ; L = stop - start, -255..383
        bmi @no
        bne @big
        lda disp_t
        beq @no
        cmp #DISP_LINES + 1
        bcc @small
@big:   sec                     ; crop = (L - lines) / 2
        lda disp_t
        sbc #DISP_LINES
        sta disp_t
        lda disp_t + 1
        sbc #0
        lsr
        lda disp_t
        ror
        sta disp_crop
        lda #DISP_LINES
        bne @lset
@no:    sec
        rts
@small: ldx #0
        stx disp_crop
@lset:  sta disp_lines
        jmp disp_ddf

; disp_ddf — fetch width, row skips and advances (continues disp_decide).
disp_ddf:
        inc disp_why            ; DISP_WHY_DDF
        lda disp_dm
        cmp #DISP_MC2
        bne @dh
        lda disp_cur + DISP_S_DDFSTRT
        and #$F8
        sta disp_t + 2
        lda disp_cur + DISP_S_DDFSTOP
        and #$F8
        sec
        sbc disp_t + 2
        bcc @no
        lsr                     ; 2 * ((e - s) / 8 + 1)
        lsr
        clc
        adc #2
        bne @fw                 ; always
@dh:    lda disp_cur + DISP_S_DDFSTRT
        and #$FC
        sta disp_t + 2
        lda disp_cur + DISP_S_DDFSTOP
        and #$FC
        sec
        sbc disp_t + 2
        bcc @no
        lsr                     ; 2 * ((e - s) / 4 + 2)
        clc
        adc #4
@fw:    sta disp_fetch
        cmp disp_wid
        bcc @no
        sbc disp_wid            ; C = 1
        sta disp_t + 2          ; fetch - row width
        ldx #0
@sk:    txa                     ; plane p: BPL1MOD if p even, BPL2MOD if odd
        and #1
        asl
        tay
        clc
.ifdef DISP_MUT_MOD
        lda #0                  ; mutant: modulo ignored
.else
        lda disp_cur + DISP_S_BPL1MOD,y
        and #$FE                ; word DMA: bit 0 ignored
.endif
        adc disp_t + 2
        sta disp_skl,x
.ifdef DISP_MUT_MOD
        lda #0
.else
        lda disp_cur + DISP_S_BPL1MOD + 1,y
.endif
        adc #0
        sta disp_skh,x
        bvs @no                 ; skip outside s16
        clc                     ; advance = row width + skip, s24
        lda disp_skl,x
        adc disp_wid
        sta disp_adl,x
        lda disp_skh,x
        pha
        adc #0
        sta disp_adh,x
        pla
        and #$80
        beq :+
        lda #$FF
:       adc #0
        sta disp_adb,x
        inx
        cpx disp_np
        bne @sk
        jmp disp_src
@no:    sec
        rts

; disp_src — first rows (crop applied) and the 512 KB check of every row
; the kernel will fetch (continues disp_decide).
disp_src:
        inc disp_why            ; DISP_WHY_STRADDLE
        ldx #0
@sp:    txa
.ifdef DISP_MUT_PPORD
        eor #1                  ; mutant: plane pointers 0/1, 2/3 swapped
.endif
        sta disp_t
        asl
        adc disp_t              ; 3 * plane (C = 0: plane < 4)
        tay
        lda disp_cur + DISP_S_BPLPT,y
        and #$FE                ; word DMA: bit 0 ignored
        sta disp_srl,x
        lda disp_cur + DISP_S_BPLPT + 1,y
        sta disp_srh,x
        lda disp_cur + DISP_S_BPLPT + 2,y
        sta disp_srb,x
        ldy disp_crop
        beq @cz
@cr:    clc
        lda disp_srl,x
        adc disp_adl,x
        sta disp_srl,x
        lda disp_srh,x
        adc disp_adh,x
        sta disp_srh,x
        lda disp_srb,x
        adc disp_adb,x
        sta disp_srb,x
        dey
        bne @cr
@cz:    lda disp_srl,x
        sta disp_w
        lda disp_srh,x
        sta disp_w + 1
        lda disp_srb,x
        sta disp_w + 2
        ldy #DISP_LINES
@st:
.ifndef DISP_MUT_STRADDLE
        clc                     ; last byte of the row: same 512 KB line,
        lda disp_w              ; no wrap past $FFFFFF
        adc disp_wm1
        lda disp_w + 1
        adc #0
        lda disp_w + 2
        adc #0
        bcs @no
        eor disp_w + 2
        and #$F8
        bne @no
.endif
        clc
        lda disp_w
        adc disp_adl,x
        sta disp_w
        lda disp_w + 1
        adc disp_adh,x
        sta disp_w + 1
        lda disp_w + 2
        adc disp_adb,x
        sta disp_w + 2
        dey
        bne @st
        inx
        cpx disp_np
        beq :+
        jmp @sp
:       clc
        rts
@no:    sec
        rts

; ---------------------------------------------------------------------------
; disp_args — the c2v argument block from the decision and disp_cur.
; ---------------------------------------------------------------------------
disp_args:
        ldx #0
        ldy #0
@a:     lda #0
        cpx disp_np
        bcs @z                  ; unused plane: 0
        lda disp_srl,x
        sta DISP_A_SRC,y
        lda disp_srh,x
        sta DISP_A_SRC + 1,y
        lda disp_srb,x
        sta DISP_A_SRC + 2,y
        jmp @n
@z:     sta DISP_A_SRC,y
        sta DISP_A_SRC + 1,y
        sta DISP_A_SRC + 2,y
@n:     iny
        iny
        iny
        inx
        cpx #4
        bne @a
        ldx #0                  ; skips, 4 x s16 (unused planes 0)
        ldy #0
@s:     lda #0
        sta DISP_A_SKIP,y
        sta DISP_A_SKIP + 1,y
        cpx disp_np
        bcs :+
        lda disp_skl,x
        sta DISP_A_SKIP,y
        lda disp_skh,x
        sta DISP_A_SKIP + 1,y
:       iny
        iny
        inx
        cpx #4
        bne @s
        lda #<disp_lut
        sta DISP_A_LUT
        lda #>disp_lut
        sta DISP_A_LUT + 1
        lda #^disp_lut
        sta DISP_A_LUT + 2
        ldx #31                 ; COLOR00-15
@p:
.ifdef DISP_MUT_PAL
        lda disp_cur + DISP_S_COLOR + 2,x  ; mutant: colour i+1 for colour i
.else
        lda disp_cur + DISP_S_COLOR,x
.endif
        sta DISP_A_PAL,x
        dex
        bpl @p
        rts

; ---------------------------------------------------------------------------
; disp_clrlines — lines disp_lines .. DISP_LINES-1 (below DIWSTOP) := 0,
; i.e. colour 0 in both modes.
; ---------------------------------------------------------------------------
disp_clrlines:
.ifndef DISP_MUT_CLR
        lda disp_lines
        cmp #DISP_LINES
        bcc :+
        rts                     ; nothing below DIWSTOP
:       sta disp_t              ; y
        lda #<c2v_bitmap
        sta @o1 + 1
        lda #>c2v_bitmap
        sta @o1 + 2
        lda disp_t
        lsr
        lsr
        lsr
        tax                     ; char row
        beq @r0
@rl:    clc
        lda @o1 + 1
        adc #<320
        sta @o1 + 1
        lda @o1 + 2
        adc #>320
        sta @o1 + 2
        dex
        bne @rl
@r0:    lda disp_t
        and #7
        clc
        adc @o1 + 1
        sta @o1 + 1
        bcc @ln
        inc @o1 + 2
@ln:    lda @o1 + 1             ; cells 32-39 one page up
        sta @o2 + 1
        ldx @o1 + 2
        inx
        stx @o2 + 2
        lda #0
        tax
@c1:
@o1:    sta c2v_bitmap,x        ; operand patched
        pha
        txa
        clc
        adc #8
        tax
        pla
        cpx #0
        bne @c1                 ; 32 cells
@c2:
@o2:    sta c2v_bitmap + 256,x  ; operand patched
        pha
        txa
        clc
        adc #8
        tax
        pla
        cpx #64
        bne @c2                 ; 8 cells
        inc disp_t
        lda disp_t
        cmp #DISP_LINES
        beq @done
        and #7
        bne @nx
        clc                     ; next char row: + 320 - 7
        lda @o1 + 1
        adc #<(320 - 7)
        sta @o1 + 1
        lda @o1 + 2
        adc #>(320 - 7)
        sta @o1 + 2
        jmp @ln
@nx:    inc @o1 + 1
        bne @ln
        inc @o1 + 2
        jmp @ln
@done:
.endif
        rts

; disp_vicon — show the bank-3 bitmap (mode disp_dm, colour 0 = c2v_a_bgc).
disp_vicon:
        lda CIA2_PRA
        and #$FC
        ora #DISP_VBANK
        sta CIA2_PRA
        lda #DISP_VMEM
        sta VIC_MEMPTR
        lda #DISP_VON
        sta VIC_CTRL1
        ldx #DISP_VMC
        lda disp_dm
        cmp #DISP_MC2
        beq :+
        ldx #DISP_VHI
:       stx VIC_CTRL2
        lda DISP_A_BGC
        sta VIC_BG0
        sta VIC_BORDER
        rts

; disp_border — blank: display off, border = LUT[COLOR00] (one byte DMA).
disp_border:
        lda #<disp_t
        sta REC_C64LO
        lda #>disp_t
        sta REC_C64HI
        clc
        lda disp_cur + DISP_S_COLOR
        adc #<disp_lut
        sta REC_REULO
        lda disp_cur + DISP_S_COLOR + 1
        and #$0F
        adc #>disp_lut
        sta REC_REUHI
        lda #^disp_lut
        adc #0
        sta REC_REUBANK
        lda #1
        sta REC_LENLO
        lda #0
        sta REC_LENHI
        sta REC_ACTRL
        lda #REU_CMD_FETCH
        sta REC_COMMAND
        jsr mem_rec_dirty       ; C1
        lda disp_t
        sta VIC_BORDER
        lda #DISP_VOFF
        sta VIC_CTRL1
        rts

; The OCS-RGB -> VIC LUT, nibble-packed (host/c2v_ref.py lut_inc()).
disp_lutp:
        .include "disp_lut.inc"
.endif

; Pad to whole pages: modules linked after this one keep their page offsets.
disp_code1:
        .res (256 - ((disp_code1 - disp_code0) .mod 256)) .mod 256, $EA
