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
;   4-plane hires   -> c2v_hiress (hires 320x200 silhouette, v0; W12-V:
;                      per-cell colours, c2v_hfill)
;   W12-V: the colours by disp_cols (docs/CONTRACTS.md "Display refresh":
;   colour choice); every shown conversion ends with disp_msum (disp_mck)
;   anything else   -> blank + log (disp_why; disp_nblank counts)
;
; Link: disp_init stores disp_frame in W10-P's RAM vector cop_dhook (high
; byte 0 = none).  cop_frame calls it at the end of every frame, and a
; synchronous COPJMP run calls it after the copper stops.  Neither module
; links against the other's code: disp_st is an rc equate and cop_dhook a
; cop.s BSS export that disp.s imports.  disp_st is RAM under BASIC ROM; its valid byte (+103)
; must be $A5 (cop_reset ran), else the state is ignored (blank, why 7).
;
; Refresh policy v0 (docs/PERF.md "Display refresh"):
;   * convert only when disp_st's gen changed or its dirty byte (+104,
;     F1: set by W10-P's every bump, cleared here) is set, AND the fields differ
;     from the last ones taken (the copper rewriting equal values costs one
;     70-byte compare, no conversion);
;   * inside mach_run (mach_in <> 0) a conversion starts only when
;     DISP_N frames' worth of guest E-time (DISP_N * 14188 ticks) passed
;     since the last one ENDED (fix round 1, F3: frame counts were eaten by
;     the catch-up after a long conversion); a deferred change is not
;     consumed, so a later call converts it;
;   * outside mach_run (HLE runners) every gen change converts at once;
;   * disp_refresh converts unconditionally (no gen, compare or budget);
;   * no dirty tracking of CPU bitplane writes (backlog); W11-A (C3): a
;     change of the pixel epoch disp_st+105 (blt.s bumps it after every
;     D-enabled blit) is a change like gen's, and converts even when the
;     fields are equal (same budget inside mach_run);
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
; duty budget), DISP_MUT_BANK (BASIC ROM left in: disp_st read as ROM),
; DISP_MUT_DIRTY (the dirty byte ignored), DISP_MUT_EFROM (the budget
; measured from a conversion's start); DISP_TRACE (W11-A, supervisor grant:
; disp_tstart/disp_tfin record the least gap from a conversion's END to the
; next one's START inside mach_run, for tests/run_pf.py leg gap).  W12-V:
; DISP_MUT_W12ORD (counts ignored), DISP_MUT_W12ONEPASS (no pass 1),
; DISP_MUT_W12OCS (equal OCS colours never share), DISP_MUT_W12UP (the
; walk never goes down), DISP_MUT_W12MCK (s2 not summed), DISP_MUT_W12RLE
; (LUT colour bit 3 lost), DISP_MUT_W12RUN (LUT run length bit 3 lost).
; W13-D: DISP_MUT_W13PX (the 1:1 window's byte offset lost),
; DISP_MUT_W13EDGE (inp_view bit 0 never consumed), DISP_MUT_W13LSR (LACE's
; advance halved by a logical shift), DISP_MUT_W13BPU5 (hires BPU 5 converts); W14-D: DISP_MUT_W14CROP (the old
; centre crop).
;
; Link layout: CODE and BSS are each padded to whole pages, no RODATA or
; DATA, so modules linked after this one move by whole pages only.
; ===========================================================================

.include "../macros.inc"

.export disp_init, disp_frame, disp_refresh
.export disp_mode, disp_why, disp_nconv, disp_nblank, disp_seen, disp_ok
.export disp_cur, disp_laste, disp_bud, disp_lines, disp_crop
.ifdef DISP_TRACE
.export disp_tend, disp_tval, disp_gmin, disp_ngap   ; W11-A (supervisor grant, efrom)
.endif
.export DISP_N, disp_lut       ; disp_lut: the rc equate, for the host
.export disp_mck                ; W12-V: matrix checksum, for the host
.export disp_cols, disp_msum     ; W12-V: for tests/run_pf.py leg cols
.import c2v_init, c2v_mc2s, c2v_hiress
.import c2v_lookn, c2v_setmc2, c2v_hfill        ; W12-V
.import c2v_v_col, c2v_v_cnt                    ; W12-V
.import mach_nmi                                ; W12-V: disp_msum's NMI guard
.import mem_rec_dirty, mach_in, mach_etime, mach_et
.import c2v_ones, c2v_ofin                      ; W13-D: the 1:1 view
.export disp_view, disp_px, disp_py, disp_one, disp_ymax ; W13-D: for the host
.export spr_vec, spr_id, disp_t               ; W13-D D2: the pointer module's hook
.export disp_ozero                              ; W13-D: c2v_ones' rows past the picture
.ifdef DISP_INPV_LOCAL
.export inp_view                ; W13-D: host-written stand-in for W13-I's (I1)
.else
.import inp_view                ; W13-I (I1): bit 0 = F1, toggle fit/1:1 (an edge)
.endif
.import cop_dhook               ; cop.s BSS (consumer -> provider, as cia.s
                                ; imports mach_cbv); disp_st: rc equate (cop)

.ifndef DISP_N
DISP_N     = 24                 ; duty budget: frames per conversion (PERF (j); W12-V fix
                                ; round 1: 20 -> 24, PERF (m))
.endif
DISP_FRAME_E = 14188            ; @value E ticks per guest frame (CONTRACTS "Machine loop")
DISP_EBUD  = DISP_N * DISP_FRAME_E ; E ticks from a conversion's end to the next start
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
DISP_ST_DIRTY   = 104                ; @value W10-P: dirty byte offset in disp_st (F1)
DISP_ST_PIX     = 105                ; @value W11-A: pixel epoch offset in disp_st (C3)

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
.assert DISP_EBUD < $1000000, error, "the E budget must fit 24 bits"
.assert (c2v_screen >> 14) = (c2v_bitmap >> 14), error, "matrix and bitmap must share a VIC bank"
.assert (c2v_bitmap & DISP_BMALIGN) = 0, error, "VIC bitmap must sit at bank + $0000 or + $2000"
.assert (c2v_screen & $3FF) = 0, error, "VIC screen matrix must be 1 KB aligned"
.assert (disp_lut & $FF) = 0, error, "disp_lut must be page-aligned (256-byte stashes)"
.assert (disp_lut >> 19) = ((disp_lut + $FFF) >> 19), error, "disp_lut must not cross a 512 KB REU line"
.assert (disp_zero & $FF) = 0 .and (disp_zero >> 16) = (disp_lut >> 16), error, "W13-D: disp_zero: a page in disp_lut's bank (disp_stash)"

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
.macro DISP_W13_BSS                ; W13-D: the view's and the pointer hook's state
disp_view:  .res 1              ; W13-D: 0 fit, 1 the 1:1 view (F1 toggles)
disp_px:    .res 1              ; W13-D: 1:1 window's first byte in the row (0-40)
disp_py:    .res 1              ; W13-D: 1:1 window's first line (clamped to disp_ymax)
disp_ymax:  .res 1              ; W13-D: lines past the 1:1 window (L - 200, LACE L - 101; 0-255)
disp_one:   .res 1              ; W13-D: the last decision is the 1:1 view
spr_vec:    .res 2              ; W13-D D2: the pointer module's entry (LE; high byte 0 = none)
spr_id:     .res 4              ; W13-D D2: the armed module's ID (a resume checks it)
.ifdef DISP_INPV_LOCAL
inp_view:   .res 1              ; W13-D: stand-in (I1), bit 0 = toggle
.endif
.endmacro

.segment "BSS"
disp_b0:
disp_new:   .res DISP_SNAP      ; fields as just copied
disp_cur:   .res DISP_SNAP      ; fields of the last decision
disp_seen:  .res 1              ; gen when the fields were last copied
disp_ok:    .res 1              ; 0 until the first copy
disp_mode:  .res 1              ; DISP_NONE / BLANK / MC2 / HIRES
disp_why:   .res 1              ; last decision: 0 shown, else DISP_WHY_*
disp_nconv: .res 2              ; conversions (u16, wraps)
disp_nblank: .res 2             ; blank decisions (u16, wraps)
disp_laste: .res 4              ; E (mach_et) when the last conversion ended
disp_bud:   .res 1              ; nonzero: disp_laste is a budget reference
disp_pix:   .res 1              ; W11-A: pixel epoch (disp_st+105) when last taken
disp_lines: .res 1              ; lines shown (<= DISP_LINES)
disp_crop:  .res 1              ; rows skipped at the top (W14-D: 0 in the fit view)
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
disp_mck:   .res 2              ; W12-V: matrix Fletcher sums s1, s2 (c2v_ref.screen_sum)
disp_rp:    .res 1              ; W12-V: disp_rule's pen
disp_rd:    .res 1              ; W12-V: disp_rule's walk distance
disp_rc:    .res 1              ; W12-V: disp_rule's candidate colour
disp_ro:    .res 16             ; W12-V: disp_rule's owner pen per VIC colour
disp_ord:   .res 16             ; W12-V: pens in order; bit 7: has its colour
disp_rn:    .res 1              ; W12-V: pens in disp_ord
disp_rm:    .res 1              ; W12-V: the pass's walk limit (0, then 15)
disp_ry:    .res 1              ; W12-V: the pen's place in disp_ord
        DISP_W13_BSS            ; W13-D (in the page's slack)
disp_b1:
        .res (256 - ((disp_b1 - disp_b0) .mod 256)) .mod 256
.ifdef DISP_TRACE                ; W11-A (supervisor grant, efrom): test only.  Wave 13
.segment "BCODE"                ; (supervisor): MAIN is full, so the trace bytes sit in
disp_tend:  .res 4              ; BHI (disp runs with BASIC out), PRG-loaded zeros.
disp_tval:  .res 1              ; nonzero: disp_tend is valid
disp_gmin:  .res 4              ; least END -> next START gap in E (host presets $FFFFFFFF)
disp_ngap:  .res 2              ; gaps measured
.segment "BSS"
.endif

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
        lda #<disp_lutp         ; W12-V (supervisor-approved): run-length LUT
        sta @rd+1               ; W12-V (supervisor-approved): (c2v_ref.rle_lut: byte =
        lda #>disp_lutp         ; W12-V (supervisor-approved): (run - 1) << 4 | colour);
        sta @rd+2               ; W12-V (supervisor-approved): 256 entries into c2v_stage,
        lda #0                  ; W12-V (supervisor-approved): then stash them; stops at
        sta disp_t              ; W12-V (supervisor-approved): exactly 4096 entries
        tax                     ; W12-V (supervisor-approved)
@rd:    lda disp_lutp           ; W12-V (supervisor-approved): operand patched
        inc @rd+1               ; W12-V (supervisor-approved)
        bne :+                  ; W12-V (supervisor-approved)
        inc @rd+2               ; W12-V (supervisor-approved)
:       pha                     ; W12-V (supervisor-approved)
        lsr                     ; W12-V (supervisor-approved)
        lsr                     ; W12-V (supervisor-approved)
        lsr                     ; W12-V (supervisor-approved)
        lsr                     ; W12-V (supervisor-approved)
.ifdef DISP_MUT_W12RUN
        and #$07                ; mutant: run length bit 3 lost
.endif
        tay                     ; W12-V (supervisor-approved): run - 1
        pla                     ; W12-V (supervisor-approved)
.ifdef DISP_MUT_W12RLE
        and #$07                ; mutant: colour bit 3 lost
.else
        and #$0F                ; W12-V (supervisor-approved)
.endif
@em:    sta c2v_stage,x         ; W12-V (supervisor-approved)
        inx                     ; W12-V (supervisor-approved)
        bne :+                  ; W12-V (supervisor-approved)
        pha                     ; W12-V (supervisor-approved)
        jsr disp_stash          ; W12-V (supervisor-approved): a chunk is full
        ldx disp_t              ; W12-V (supervisor-approved)
        cpx #16                 ; W12-V (supervisor-approved): C = 4096 entries done
        ldx #0                  ; W12-V (supervisor-approved): (C kept)
        pla                     ; W12-V (supervisor-approved): (C kept)
        bcs @lut                ; W12-V (supervisor-approved)
:       dey                     ; W12-V (supervisor-approved)
        bpl @em                 ; W12-V (supervisor-approved)
        bmi @rd                 ; W12-V (supervisor-approved): (always)
@lut:
        lda #0                  ; W13-D: disp_zero := 256 zero bytes
        tax                     ; W13-D
:       sta c2v_stage,x         ; W13-D
        inx                     ; W13-D
        bne :-                  ; W13-D
        lda #>(disp_zero - disp_lut) ; W13-D
        sta disp_t              ; W13-D
        jsr disp_stash          ; W13-D
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
        lda #0                  ; the first conversion is never deferred
        sta disp_bud
        lda disp_st + DISP_ST_PIX ; W11-A: C3, the epoch's snapshot (undefined
        sta disp_pix            ; W11-A: before its first writer)
        lda #0                  ; the hook: high byte last (0 = none)
        sta cop_dhook+1
        lda #<disp_frame
        sta cop_dhook
        lda #>disp_frame
        sta cop_dhook+1
        pla
        sta CPU_PORT
        rts

; disp_stash — W12-V (supervisor-approved): c2v_stage's 256 bytes to disp_lut + 256 * disp_t;
; disp_t + 1.  Keeps X and Y.
disp_stash:
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
        inc disp_t
        rts

; ---------------------------------------------------------------------------
; disp_frame — the cop_dhook target.
; ---------------------------------------------------------------------------
disp_frame:
        DISP_ENTER              ; disp_st is RAM under BASIC ROM
.ifndef DISP_MUT_BANK            ; (that mutant keeps BASIC ROM in: no BHI code)
        jsr disp_vw             ; W13-D: F1, the pointer; C = 1: convert now
        bcc :+                  ; W13-D
        jmp disp_now            ; W13-D
:
.endif
        lda disp_ok
        beq @go                 ; first call: always decide
.ifndef DISP_MUT_GEN
        lda disp_st             ; C1 +0 gen
        cmp disp_seen
        bne @chg
.endif
.ifndef DISP_MUT_DIRTY
        lda disp_st + DISP_ST_DIRTY ; F1: set by every bump, even one that
        bne @chg                ; wrapped gen back to disp_seen
.endif
        lda disp_st + DISP_ST_PIX ; W11-A: C3, guest pixels rewritten (a blit)
        cmp disp_pix            ; W11-A
        bne @chg                ; W11-A
        jmp disp_leave          ; unchanged
@chg:
.ifdef DISP_MUT_BUDGET
        jmp @go                 ; mutant: no duty budget
.else
        lda mach_in
        beq @go                 ; HLE: no budget
        lda disp_bud
        beq @go                 ; no conversion inside mach_run yet
        jsr mach_etime          ; E ticks since the last conversion ENDED
        sec
        lda mach_et
        sbc disp_laste
        sta disp_t
        lda mach_et+1
        sbc disp_laste+1
        sta disp_t+1
        lda mach_et+2
        sbc disp_laste+2
        sta disp_t+2
        lda mach_et+3
        sbc disp_laste+3
        bmi @go                 ; negative: E restarted (mach_init)
        bne @go                 ; >= 2^24 ticks
        lda disp_t
        cmp #<DISP_EBUD
        lda disp_t+1
        sbc #>DISP_EBUD
        lda disp_t+2
        sbc #^DISP_EBUD
        bcs @go
.endif
@rts:   jmp disp_leave          ; deferred: the change stays unseen
@go:    lda disp_st
        sta disp_seen
        lda disp_st + DISP_ST_PIX ; W11-A: C3, a new epoch converts even with
        cmp disp_pix            ; W11-A: equal fields (the pixels changed)
        php                     ; W11-A
        jsr disp_snap           ; (takes the epoch too)
        plp                     ; W11-A
        bne disp_take           ; W11-A
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
disp_now:                       ; W13-D
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
.ifdef DISP_TRACE
        jsr disp_tstart         ; W11-A (supervisor grant, efrom): test only
.endif
.ifdef DISP_MUT_EFROM
        jsr disp_stamp          ; mutant: budget measured from the START
.endif
        jsr disp_args
        lda disp_dm
        cmp #DISP_MC2
        bne @hires
        ldx #15                 ; W12-V: pens 0-3 in use (count 1), in order
:       lda #0                  ; W12-V
        cpx #4                  ; W12-V
        bcs :+                  ; W12-V
        lda #1                  ; W12-V
:       sta c2v_v_cnt,x         ; W12-V
        dex                     ; W12-V
        bpl :--                 ; W12-V
        ldx #3                  ; W12-V
        jsr disp_cols           ; W12-V
        jsr c2v_setmc2          ; W12-V
        jsr c2v_mc2s
        jmp @shown
@hires: jsr disp_one1           ; W13-D: C = 1: the 1:1 view is done
        bcs @shown              ; W13-D
        jsr c2v_hiress          ; W12-V: pens to colour RAM, c2v_v_cnt
        ldx #15                 ; W12-V
        jsr disp_cols           ; W12-V
        jsr c2v_hfill           ; W12-V: the matrix from the pens
@shown: jsr disp_msum           ; W12-V: disp_mck
        jsr disp_clrlines
        jsr disp_vicon
        lda #0
        sta disp_why
        lda disp_dm
        sta disp_mode
        inc disp_nconv
        bne :+
        inc disp_nconv+1
:
.ifndef DISP_MUT_EFROM
        jsr disp_stamp          ; F3: the budget runs from the END
.endif
.ifdef DISP_TRACE
        jsr disp_tfin           ; W11-A (supervisor grant, efrom): test only
.endif
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

; disp_snap — disp_new := disp_st + 1 .. + 70, then disp_st's valid byte;
; then dirty := 0 (F1: the change is taken) and, W11-A (C3), disp_pix :=
; the pixel epoch.
disp_snap:
        ldx #DISP_S_VALID - 1
:       lda disp_st + 1,x
        sta disp_new,x
        dex
        bpl :-
        lda disp_st + DISP_ST_VALID
        sta disp_new + DISP_S_VALID
        lda #0
        sta disp_st + DISP_ST_DIRTY
        lda disp_st + DISP_ST_PIX ; W11-A: C3, the epoch is taken
        sta disp_pix            ; W11-A
        rts

.ifdef DISP_TRACE
; W11-A (supervisor grant, efrom): test-only trace of the duty budget, read
; by tests/run_pf.py leg gap.  Independent of disp_laste (which the efrom
; mutant stamps at a conversion's start): disp_tfin records the real END of
; every conversion inside mach_run, disp_tstart the gap to the next START.
disp_tstart:
        lda mach_in
        beq @x
        lda disp_tval
        beq @x
        jsr mach_etime
        sec
        lda mach_et
        sbc disp_tend
        sta disp_t
        lda mach_et+1
        sbc disp_tend+1
        sta disp_t+1
        lda mach_et+2
        sbc disp_tend+2
        sta disp_t+2
        lda mach_et+3
        sbc disp_tend+3
        sta disp_t+3
        lda disp_t              ; gap < gmin (unsigned): gmin := gap
        cmp disp_gmin
        lda disp_t+1
        sbc disp_gmin+1
        lda disp_t+2
        sbc disp_gmin+2
        lda disp_t+3
        sbc disp_gmin+3
        bcs :+
        ldx #3
@cp:    lda disp_t,x
        sta disp_gmin,x
        dex
        bpl @cp
:       inc disp_ngap
        bne @x
        inc disp_ngap+1
@x:     rts
disp_tfin:
        lda mach_in
        beq @x
        jsr mach_etime
        ldx #3
@cp:    lda mach_et,x
        sta disp_tend,x
        dex
        bpl @cp
        lda #1
        sta disp_tval
@x:     rts
.endif

; disp_stamp — the budget's reference: inside mach_run, E now and the
; budget armed; outside it (HLE), no budget.
disp_stamp:
        lda mach_in
        beq @off
        jsr mach_etime
        ldx #3
:       lda mach_et,x
        sta disp_laste,x
        dex
        bpl :-
        lda #1
@off:   sta disp_bud
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
@n0:    sec                     ; no display state yet (W13-D: also @n0)
        rts
:       lda #DISP_WHY_DMA
        sta disp_why
        lda disp_cur + DISP_S_DMACON + 1
        and #$03                ; DMAEN (bit 9), BPLEN (bit 8)
        cmp #$03
        bne @n0                 ; (W13-D: @no is out of reach)
        inc disp_why            ; DISP_WHY_HAMDPF
        lda disp_cur + DISP_S_BPLCON0 + 1
        and #$0C                ; HAM (bit 11), DBLPF (bit 10)
        bne @n0
        inc disp_why            ; DISP_WHY_DEPTH
        lda disp_cur + DISP_S_BPLCON0 + 1
        and #$F0                ; HIRES (bit 15), BPU (bits 14-12)
        cmp #$20
        bne @hi
        lda #DISP_MC2
        ldx #40
        ldy #2
        bne @md
@hi:    cmp #$90                ; W13-D: hires BPU 1-4 ($90-$C0); absent
        bcc @no                 ; W13-D: planes read disp_zero (disp_args)
.ifdef DISP_MUT_W13BPU5
        cmp #$E0                ; mutant: hires BPU 5 converts too
.else
        cmp #$D0                ; W13-D
.endif
        bcs @no                 ; W13-D
        lsr                     ; W13-D
        lsr                     ; W13-D
        lsr                     ; W13-D
        lsr                     ; W13-D
        and #7                  ; W13-D: BPU
        tay                     ; W13-D
        lda #DISP_HIRES
        ldx #80
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
@big:
.ifdef DISP_MUT_W14CROP                ; mutant: the W10-W13 centre crop
        sec
        lda disp_t
        sbc #DISP_LINES
        sta disp_t
        lda disp_t + 1
        sbc #0
        lsr
        lda disp_t
        ror
        sta disp_crop
.else
        lda #0                  ; W14-D: crop 0, the top 200 lines (was the
        sta disp_crop           ; centre, (L - lines) / 2)
.endif
        lda #DISP_LINES
        bne @lset
@no:    sec
        rts
@small: ldx #0
        stx disp_crop
@lset:  sta disp_lines
        jsr disp_vcrop          ; W13-D: disp_ymax; the 1:1 window's lines
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
@z:     lda #<disp_zero         ; W13-D: an absent plane reads the zero row
        sta DISP_A_SRC,y
        lda #>disp_zero         ; W13-D
        sta DISP_A_SRC + 1,y
        lda #^disp_zero         ; W13-D
        sta DISP_A_SRC + 2,y
@n:     iny
        iny
        iny
        inx
        cpx #4
        bne @a
        ldx #0                  ; skips, 4 x s16 (unused planes 0)
        ldy #0
@s:     lda #<-80               ; W13-D: absent plane: skip -80, so the
        sta DISP_A_SKIP,y       ; W13-D: row advance is 0 (the zero row)
        lda #>-80               ; W13-D
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
        jsr disp_alut           ; W12-V (shared with disp_border)
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

; disp_cols — W12-V: X = highest pen; c2v_v_col[0..X] := LUT, then the
; colour choice (docs/CONTRACTS.md "Display refresh", c2v_ref.separate):
; the pens in use (0 and every p with c2v_v_cnt[p] > 0) in order pen 0,
; then by count descending (ties: lower pen).  Pass 1: each takes its LUT
; colour if no pen with a different OCS colour holds it.  Pass 2: each pen
; still without one walks the ladder from its LUT colour's place k (k+1,
; k-1, k+2, ...) to the first colour no pen with a different OCS colour
; holds.  Consumes c2v_v_cnt.
disp_cols:
        jsr c2v_lookn
        ldx #15
        lda #$FF
:       sta disp_ro,x           ; every colour free
        dex
        bpl :-
        sta c2v_v_cnt           ; pen 0 first (A = $FF)
        ldy #0                  ; pens in order so far
@sel:   lda #1                  ; best count so far (a pen in use has >= 1)
        sta disp_t
        lda #$FF
        sta disp_t+1            ; best pen: none
        ldx #15
@sc:    lda c2v_v_cnt,x
.ifdef DISP_MUT_W12ORD
        beq :+                  ; mutant: every pen in use counts 1 (index order)
        lda #1
:
.endif
        cmp disp_t
        bcc @nx                 ; >= keeps the lower pen on a tie
        sta disp_t
        stx disp_t+1
@nx:    dex
        bpl @sc
        ldx disp_t+1
        bmi @sorted             ; no pen left
        lda #0
        sta c2v_v_cnt,x         ; taken
        txa
        sta disp_ord,y
        iny
        bne @sel                ; (always)
@sorted:
        sty disp_rn
.ifdef DISP_MUT_W12ONEPASS
        lda #15                 ; mutant: one pass, the walk at once
.else
        lda #0
.endif
        sta disp_rm             ; pass 1: d = 0 only
@pass:  ldy #0
@pen:   lda disp_ord,y
        bmi @nxp                ; has its colour
        sty disp_ry
        sta disp_rp
        tax
        lda c2v_v_col,x         ; the LUT colour's place on the ladder
        ldx #15
:       cmp disp_lad,x
        beq :+
        dex
        bpl :-
:       stx disp_t+2            ; k
        lda #0
        sta disp_rd             ; d
@w:     lda disp_t+2
        clc
        adc disp_rd
        jsr @try                ; k + d
        bcc @got
.ifndef DISP_MUT_W12UP
        lda disp_t+2
        sec
        sbc disp_rd
        jsr @try                ; k - d
        bcc @got
.endif
        inc disp_rd
        lda disp_rm
        cmp disp_rd
        bcs @w                  ; d <= the pass's limit
        bcc @nx2                ; pass 1: none
@got:   ldy disp_ry
        lda disp_ord,y
        ora #$80                ; done
        sta disp_ord,y
@nx2:   ldy disp_ry
@nxp:   iny
        cpy disp_rn
        bne @pen
        lda disp_rm
        bne @rts
        lda #15                 ; pass 2: the whole ladder
        sta disp_rm
        bne @pass
@rts:   rts
; @try: A = a ladder place; C = 0 if pen disp_rp took its colour
@try:   cmp #16
        bcs @r                  ; off the ladder (k + d > 15 or k - d < 0)
        tay
        lda disp_lad,y
        sta disp_rc
        tay
        lda disp_ro,y
        bmi @own                ; free
.ifdef DISP_MUT_W12OCS
        bpl @no                 ; mutant: a held colour is never shared
.endif
        asl                     ; held: same OCS colour as the holder?
        tay
        lda disp_rp
        asl
        tax
        lda DISP_A_PAL,y
        eor DISP_A_PAL,x
        bne @no
        lda DISP_A_PAL + 1,y
        eor DISP_A_PAL + 1,x
        and #$0F
        beq @set
@no:    sec
@r:     rts
@own:   lda disp_rp
        sta disp_ro,y
@set:   ldx disp_rp
        lda disp_rc
        sta c2v_v_col,x
        clc
        rts

; disp_msum — W12-V (V2, fix round 1: mod 255): disp_mck := the Fletcher
; sums mod 255 (end-around carry) of the 1000 matrix bytes read back
; (c2v_ref.screen_sum), so the host sees the cell colours without reading
; the RAM under I/O.  A palette error moves high nibbles only; mod 256 would
; keep ~8 bits of it, mod 255 keeps all 16.  A matrix at or above $A000
; (under BASIC, I/O or KERNAL) is read at $01 = %xx100 one page at a time
; with IRQs masked, the RAM NMI vector pointed at mach_nmi first (as
; c2v_fill: identical bytes inside mach_run).
DISP_ROMLO = $A000               ; @value lowest address a ROM or I/O can map over
DISP_SCR_BANKED = c2v_screen >= DISP_ROMLO
disp_msum:
        lda #0
        sta disp_mck
        sta disp_mck+1
        lda #>c2v_screen
        sta @rd+2
.if DISP_SCR_BANKED
        lda #<mach_nmi
        sta mach_vnmi
        lda #>mach_nmi
        sta mach_vnmi+1
.endif
        ldx #0
@pg:    php
        sei
        lda CPU_PORT
        sta disp_t
.if DISP_SCR_BANKED
        and #$F8
        ora #$04
        sta CPU_PORT
.endif
@lp:
@rd:    lda c2v_screen,x        ; operand high byte patched
        clc
        adc disp_mck
        adc #0                  ; end-around carry: mod 255
        sta disp_mck
.ifndef DISP_MUT_W12MCK
        clc
        adc disp_mck+1
        adc #0
        sta disp_mck+1          ; (mutant: s2 never summed)
.endif
        inx
        beq @nx
        cpx #<1000
        bne @lp
        lda @rd+2
        cmp #>(c2v_screen + $300)
        bne @lp
@nx:    lda disp_t
        sta CPU_PORT
        plp
        inc @rd+2
        txa
        beq @pg                 ; a page wrapped; else all 1000 are summed
        rts

; The VIC colours by luma, darkest first (c2v_ref.LADDER).
disp_lad:
        .byte 0, 6, 9, 2, 11, 4, 8, 14, 12, 10, 5, 15, 3, 7, 13, 1

; disp_border — blank: display off, border = LUT[COLOR00] (one byte DMA).
disp_border:
        lda disp_cur + DISP_S_COLOR     ; W12-V: the lookup through c2v_lookn
        sta DISP_A_PAL                  ; W12-V: (pen 0, which the colour
        lda disp_cur + DISP_S_COLOR + 1 ; W12-V: choice never moves)
        sta DISP_A_PAL + 1              ; W12-V
        jsr disp_alut                   ; W12-V
        ldx #0                          ; W12-V
        jsr c2v_lookn                   ; W12-V
        jsr mem_rec_dirty       ; C1
        lda c2v_v_col                   ; W12-V
        sta VIC_BORDER
        lda #DISP_VOFF
        sta VIC_CTRL1
        rts

; disp_alut — W12-V: c2v_a_lut := disp_lut.
disp_alut:
        lda #<disp_lut
        sta DISP_A_LUT
        lda #>disp_lut
        sta DISP_A_LUT + 1
        lda #^disp_lut
        sta DISP_A_LUT + 2
        rts

; ===========================================================================
; W13-D: the 1:1 view (docs/CONTRACTS.md "Display refresh (S3.5)": "1:1
; view (W13-D)"; host reference c2v_ref.disp_decide_view /
; disp_convert_view) and the hook of the pointer module (spr_vec,
; tests/aros/spr.s, "Pointer sprite (W13-D)").
; ===========================================================================
DISP_PXC   = 20                 ; @value 1:1: the window centred (bytes)
DISP_LACE  = $04                ; @value BPLCON0 low byte: LACE (bit 2)
DISP_A_OLIM = c2v_args + 145    ; W13-D: the 1:1 kernel's lines with data (c2v_v_olim)
.assert (spr_vec & $FF) <> $FF, error, "W13-D: jmp (spr_vec) must not straddle a page"

; BHI tail ($A778-$A7FF, W13-D; runtime/c64.cfg BCODE): disp runs with
; BASIC out (DISP_ENTER), so its banked code is called directly.
.segment "BCODE"
; disp_vw — W13-D: inp_view bit 0 (F1) toggles the view (the 1:1 window
; centred: disp_px := 20, disp_py := disp_ymax / 2) and asks for a
; conversion (C = 1); else, if armed, the pointer module through spr_vec
; (W13-D D2, tests/aros/spr.s: C = 1 when it moved the 1:1 window).
disp_vw:
        lda inp_view
        lsr
        bcs @f1
        lda spr_vec+1           ; D2: the pointer module (tests/aros/spr.s)
        beq @x                  ; none (C = 0)
        sta @id+2               ; fix round 1: armed only while the module's
        lda spr_vec             ; ID (+3..+6) still equals spr_id (a stub or
        sta @id+1               ; a resume without re-staging overwrote it:
        ldx #6                  ; disarm, no call)
@id:    lda $FFFF,x             ; operand patched: the module's base
        cmp spr_id - 3,x
        bne @off
        dex
        cpx #3
        bcs @id
        jmp (spr_vec)           ; its C = 1: the window moved, convert now
@off:   lda #0
        sta spr_vec+1           ; disarmed
        lda VIC_SPREN           ; fix round 2: and its sprites 0-2 hidden
        and #$F8
        sta VIC_SPREN
        clc
        rts
@f1:    asl
.ifdef DISP_MUT_W13EDGE
        bit inp_view            ; mutant: the edge is never consumed
.else
        sta inp_view            ; bit 0 consumed
.endif
        lda disp_view
        eor #1
        sta disp_view
        lda #DISP_PXC
        sta disp_px
        lda #0                  ; W14-D: the window's top line (was disp_ymax / 2)
        sta disp_py
        sec
@x:     rts



; disp_ozero — W13-D fix round 1 (from c2v_ones, after each line's fetch):
; a line at or past c2v_v_olim (disp_lines: below DIWSTOP) reads 0 in all
; four stage rows, so it adds nothing to the counts and shows pen 0.
disp_ozero:
        lda c2v_args + 68       ; c2v_v_line
        cmp DISP_A_OLIM
        bcc @r
        lda #0
        ldx #39
:       sta c2v_stage,x
        sta c2v_stage + $100,x
        sta c2v_stage + $200,x
        sta c2v_stage + $300,x
        dex
        bpl :-
@r:     rts

.segment "CODE"

; disp_vcrop — W13-D (disp_decide, after disp_lines): disp_ymax := L - W
; clamped to 0-255, W = 200 lines, or 101 with BPLCON0 LACE (the 1:1 view
; shows 200 frame lines = 100 field lines, +1 for the odd field); disp_one := the decision is
; hires and the view 1:1; then disp_crop := min(disp_py, disp_ymax) and,
; with LACE, disp_lines := min(2 * disp_lines, 200).
disp_vcrop:
        lda disp_cur + DISP_S_DIWSTOP + 1
        asl
        lda #0
        rol
        eor #1
        tax                     ; DIWSTOP V8 = NOT V7
        ldy #DISP_LINES
        lda disp_cur + DISP_S_BPLCON0
        and #DISP_LACE
        beq :+
        ldy #DISP_LINES / 2 + 1 ; (fix round 1: L - 101, so the odd field's
                                ; last frame line is <= 2L - 1)
:       sty disp_t + 2
        sec                     ; X:A = L - W
        lda disp_cur + DISP_S_DIWSTOP + 1
        sbc disp_cur + DISP_S_DIWSTRT + 1
        bcs :+
        dex
:       sec
        sbc disp_t + 2
        bcs :+
        dex
:       cpx #0
        beq @y                  ; 0-255
        bmi @y0
        lda #$FF
        .byte $2C               ; bit abs: skip
@y0:    lda #0
@y:     sta disp_ymax
        ldx #0
        lda disp_view
        beq @x
        lda disp_dm
        cmp #DISP_HIRES
        bne @x
        inx
        lda disp_py
        cmp disp_ymax
        bcc :+
        lda disp_ymax
:       sta disp_crop
        lda disp_cur + DISP_S_BPLCON0
        and #DISP_LACE
        beq @x
        lda disp_lines          ; LACE: 2 frame lines per field line
        asl
        bcs :+
        cmp #DISP_LINES
        bcc @l
:       lda #DISP_LINES
@l:     sta disp_lines
@x:     stx disp_one
        rts

; disp_one1 — W13-D (disp_take, hires): C = 0 for the fit view; else the
; window's first byte, LACE's frame-line advance, the kernel, the colour
; choice, the matrix, C = 1.
disp_one1:
        lda disp_one
        lsr                     ; C = 1:1
        bcc @x
        ldx #9
@a:     clc
        lda DISP_A_SRC,x
.ifdef DISP_MUT_W13PX
        adc #0                  ; mutant: the window's byte offset lost
        nop
.else
        adc disp_px
.endif
        sta DISP_A_SRC,x
        bcc :+
        inc DISP_A_SRC+1,x
        bne :+
        inc DISP_A_SRC+2,x
:       dex
        dex
        dex
        bpl @a
        lda disp_cur + DISP_S_BPLCON0
        and #DISP_LACE
        beq @k
        lda disp_np             ; LACE: each plane's row advance halved:
        asl                     ; skip := (skip + 80) / 2 - 80 (the two
        tax                     ; fields interleave; 80 for mod = fetch)
@h:     dex
        dex
        bmi @k
        clc
        lda DISP_A_SKIP,x
        adc #80
        tay
        lda DISP_A_SKIP+1,x
        adc #0
.ifndef DISP_MUT_W13LSR
        cmp #$80                ; arithmetic shift (mutant: logical, a
.else                           ; negative advance turns positive)
        clc
.endif
        ror
        sta DISP_A_SKIP+1,x
        tya
        ror
        sec
        sbc #80
        sta DISP_A_SKIP,x
        bcs @h
        dec DISP_A_SKIP+1,x
        bcc @h                  ; (always)
@k:     lda disp_lines          ; fix round 1: rows past the picture read 0
        sta DISP_A_OLIM         ; (disp_ozero)
        jsr c2v_ones
        ldx #15
        jsr disp_cols
        jsr c2v_ofin
        sec
@x:     rts



; The OCS-RGB -> VIC LUT, run-length packed (host/c2v_ref.py lut_inc()).
; W12-V (supervisor-approved)
disp_lutp:
        .include "disp_lut.inc"
.endif

; Pad to whole pages: modules linked after this one keep their page offsets.
; Wave 13 (supervisor): not in the test-only DISP_TRACE build, whose extra
; bytes would cost a whole page that MAIN no longer has (link guards still
; hold; that build is used for run_pf's gap leg only).
disp_code1:
.ifndef DISP_TRACE
        .res (256 - ((disp_code1 - disp_code0) .mod 256)) .mod 256, $EA
.endif
