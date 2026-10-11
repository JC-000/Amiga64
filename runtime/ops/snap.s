; ===========================================================================
; runtime/ops/snap.s — W12-T suspend/resume of the emulated machine.
; Owner: W12-T (w12-reu).  Contract: docs/CONTRACTS.md "Suspend/resume"
; (the state inventory lives there); format: host/snapfmt.py (I3).
;
; Suspend: snap_arm chains snap_fhook in front of the frame hook
; (mach_fhook, normally cop_frame).  The frame hook runs inside mach_run's
; service pass, after emu_run returned at an instruction boundary, with
; $01 = $35 (mach_run sets it; BASIC and KERNAL out, I/O in).  When the
; host sets snap_req bit 0, snap_sus (sei) takes the C64-side machine state
; into the REU snapshot area (rc snap region): zero page, disp_st, the MAIN
; BSS image __BSS_RUN__ .. snap_keep (every module's BSS: the link puts
; snap.s and ureu.s last, and only snap_chain of theirs lies before
; snap_keep), and a header (build ID, ROM id, layout, checksums).  The CIA
; #2 time base is not copied: mach_rdc refreshes mach_now (BSS) first, and
; resume reloads the counter from it.  Bit 1: also SAVE_REU the snapshot
; area and every guest range (host/snapfmt.RANGES) to dosio_name (ureu.s).
; Then the guest freezes (sei, spin: the REU must not change under a
; REU-resident snapshot) unless bit 7 asks to continue.
;
; Resume (snap_resume, from a FRESH runner load, called like aros_boot:
; it does not return while the guest runs): bit 1 of snap_req: LOAD_REU
; the file's snapshot area first, check it, then the ranges; else the
; snapshot must already be in the REU.  Checks, all before any state is
; touched: magic/version, header checksum, layout (KEEP, __BSS_RUN__,
; ranges), build ID (nonzero and equal to this PRG's snap_bid), ROM id
; (= the tag declared at SNAP_RID; an all-zero tag refuses, M3), state
; checksum.  Then: disp_init (c2v
; tables, LUT, cop_dhook), st_exc from the image + emu_reset (the step-
; loop patch, w_inv/m_rok invalidated), zero page $10-$7F/$88-$99, disp_st,
; the BSS image, the CIA #2 counter := ~mach_now, disp_st+105 += 1 (I2:
; one conversion rebuilds the screen), code_invall (I1), mem_rec_dirty,
; snap_kill (M1: the REU-resident snapshot is consumed; a suspend that
; continues the guest consumes it too), and mach_run continues (mach_on =
; 1: no mach_init).
;
; Test-only variants (tests/aros_resume.py mutants, ≤ 10): SNAP_MUT_NOZP,
; SNAP_MUT_NODST, SNAP_MUT_NOBSS, SNAP_MUT_NOCIA, SNAP_MUT_NOEPOCH,
; SNAP_MUT_NOEXC (no emu_reset patch), SNAP_MUT_NOHSUM, SNAP_MUT_NOSSUM
; (NOSSUM: off the <= 10 list since fix round 3; E5's refusal tests pin it),
; SNAP_MUT_NOINVALL (no code_invall after the restore, I1), SNAP_MUT_NOSCR
; (a refusal leaves the loaded header in the scratch page, F1).
; ===========================================================================

.include "../macros.inc"
.include "../rc/machine.inc"    ; the guest ranges (machine profile, S3.7)

.export snap_arm, snap_fhook, snap_resume
.export snap_req, snap_st, snap_bid, snap_chain, snap_keep
.export SNAP_VER
.import mach_fhook, mach_rdc, mach_now, mach_run
.import emu_reset, mem_rec_dirty, disp_init
.import code_invall             ; I1 (W12-C, core.s)
.import spr_vec                 ; W13-D (supervisor): a resume disarms the pointer module
.import inp_mon                 ; W14-M (supervisor): and the input module
.import dosio_buf
.import ureu_bank, ureu_open, ureu_xfer, ureu_close
.import ureu_ra, ureu_rl
.importzp UREU_LOAD, UREU_SAVE, UREU_FRD, UREU_FWR
.import __BSS_RUN__

SNAP_VER  = 2                   ; I3: = host/snapfmt.VERSION (W13-W: 2, the chip span covers the staged disk)
SNAP_HDR  = snap_base           ; rc: header, 256 bytes
SNAP_ZP   = snap_base + $100    ; zero page image
SNAP_DST  = snap_base + $200    ; disp_st page image
SNAP_BSS  = snap_base + $300    ; BSS image
SNAP_RID  = snap_base + $2FE0   ; the ROM id the host/stager declares (32 bytes; in no file span)
SNAP_KEEP = snap_keep - __BSS_RUN__
SNAP_KEEPR = (SNAP_KEEP + $FF) & $FF00 ; the image in whole pages (stash, sum, file)
SNAP_NPG  = 2 + SNAP_KEEPR / $100 ; state span pages: zp, disp_st, image
SNAP_SCR  = __BSS_RUN__         ; resume scratch (BSS start: cw_* ... since W12-C), before the restore
SNAP_SAV  = snap_base + $2E00   ; F1: the scratch page as it was at snap_res entry (in no file span; = host/snapfmt.OFF_SAV)
SNAP_NSP  = 6                   ; @value file spans: header, state, 4 guest ranges
; header offsets (= host/snapfmt.py)
SNAP_HSUM = 252                 ; @value header checksum (u32)
H_NSP     = 5                   ; @value span count; 0-4 magic + version
H_FLAGS   = 52                  ; @value after the template (0-51)
H_BID     = 56                  ; @value build ID, 8 bytes
H_RID     = 64                  ; @value ROM id, 32 bytes
H_SSUM    = 96                  ; @value state checksum (u32)
.assert SNAP_BSS + SNAP_KEEPR - 1 <= snap_end, lderror, "BSS image overflows the rc snap region"
.assert ^(snap_base + $300 + SNAP_KEEPR - 1) = ^snap_base, lderror, "snapshot area must not cross a 64 KB REU bank (single REC ops)"
.assert (snap_chain & $FF) <> $FF, lderror, "jmp (snap_chain) must not straddle a page (6502 bug)"
.assert ureu_ra > snap_keep, lderror, "ureu.s's BSS must follow snap.s's (L6: the image range holds no W12-T scratch)"
.assert ureu_rl = ureu_ra + 3, lderror, "snap_xs copies 7 bytes into ureu_ra/ureu_rl"
.assert SNAP_BSS + SNAP_KEEPR <= SNAP_RID, lderror, "the BSS image must end below the declared ROM id"
.assert dosio_buf >= SNAP_SCR + $100, lderror, "the resume scratch (the loaded header) must not overlap dosio_buf"
.assert SNAP_BSS + SNAP_KEEPR <= SNAP_SAV && SNAP_SAV + $100 <= SNAP_RID, lderror, "F1: the scratch save must lie between the image and the ROM id (no file span)"
.assert SNAP_NPG < 256, lderror, "state span page count must fit a byte"

; snap_req bits / snap_st codes (= host/snapfmt.py)
SNAP_RQ_SUS  = $01              ; @value
SNAP_RQ_FILE = $40              ; @value (bit 6: `bit snap_rq` / bvc)
SNAP_RQ_CONT = $80              ; @value
SNAP_S_SUS   = $10              ; @value suspended, snapshot in the REU
SNAP_S_SUSF  = $11              ; @value ... and saved to the file
SNAP_S_RES   = $20              ; @value resumed, mach_run running
SNAP_E_MAGIC = $E1              ; @value
SNAP_E_HSUM  = $E2              ; @value
SNAP_E_BID   = $E3              ; @value
SNAP_E_RID   = $E4              ; @value
SNAP_E_SSUM  = $E5              ; @value
SNAP_E_OPEN  = $E6              ; @value
SNAP_E_XFER  = $E7              ; @value
SNAP_E_NOID  = $E8              ; @value
SNAP_E_LAY   = $E9              ; @value
SNAP_E_NORID = $EA              ; @value no ROM id declared (all zero)

DISP_ST_PIX  = 105              ; @value CONTRACTS: pixel epoch offset in disp_st
ZP_LO1 = $10                    ; @value restored zero page: $10-$7F ...
ZP_HI1 = $7F                    ; @value
ZP_LO2 = $88                    ; @value ... and $88-$99 ($80-$87: emu_reset)
ZP_HI2 = $99                    ; @value
.assert r_d0 = ZP_LO1, error, "the zero-page restore indexes from r_d0"

.segment "BSS"
; --- saved (lies before snap_keep: restored with the image) ---
snap_chain: .res 2              ; the frame hook snap_arm displaced (LE)
snap_keep:
; --- not saved: the host's and this module's own ---
snap_req:   .res 1              ; host -> C64: SNAP_RQ_* (cleared when taken)
snap_st:    .res 1              ; C64 -> host: SNAP_S_* / SNAP_E_*

.segment "SBSS"
snap_fa:  .res 2                ; Fletcher a
snap_fb:  .res 2                ; Fletcher b (adjacent: fa, fb = the u32 LE)
snap_n:   .res 1                ; bytes summed by snap_sumb (0 = 256)
snap_rq:  .res 1                ; snap_req as taken
snap_i:   .res 1
snap_cmd: .res 1
snap_sx:  .res 1                ; snap_xs: end offset
snap_ss:  .res 4                ; the loaded header's state sum (the scratch
                                ; at __BSS_RUN__ is not kept past the compare)

.segment "DATA"                 ; in the PRG (stamped), never in the image
snap_bid:   .res 8              ; build ID: host/snapfmt.py stamps the PRG

.segment "CODE"

; snap_arm — chain snap_fhook in front of the installed frame hook.  Call
; after cop_reset (it installs cop_frame), before mach_run.
snap_arm:
        lda mach_fhook
        sta snap_chain
        lda mach_fhook+1
        sta snap_chain+1
        lda #0                  ; high byte last, 0 = none (mach.s rule)
        sta mach_fhook+1
        lda #<snap_fhook
        sta mach_fhook
        lda #>snap_fhook
        sta mach_fhook+1
        rts

; snap_fhook — the frame hook: the displaced hook, then the request.
snap_fhook:
        lda snap_chain+1
        beq :+
        jsr @ch
:       lda snap_req
        and #SNAP_RQ_SUS
        bne :+
        rts
:       lda #<snap_sus
        ldx #>snap_sus
        jmp ureu_bank
@ch:    jmp (snap_chain)

; snap_resume — see the header.  Refused: snap_st = SNAP_E_*, returns.
snap_resume:
        lda #<snap_res
        ldx #>snap_res
        jsr ureu_bank
        bcs :+
        jmp mach_run            ; returns on a real halt only
:       rts


; ===========================================================================
.segment "SCODE"

.segment "CODE"                 ; (MAIN: SNAPHI is full; called with BASIC out)
; snap_dma — Y = descriptor offset (snap_dt), X = REC command.  Keeps X.
snap_dma:                       ; ($DF02-$DF08 are consecutive: rc ram.inc)
        txa
        pha
        ldx #0
@l:     lda snap_dt,y
        sta REC_C64LO,x
        iny
        inx
        cpx #7
        bne @l
        lda #0
        sta REC_ACTRL
        pla
        tax
        stx REC_COMMAND
        rts
.assert REC_LENHI = REC_C64LO + 6, error, "snap_dma: REC registers must be consecutive"
.segment "SCODE"

; snap_sum0 / snap_sumb — Fletcher (host/snapfmt.fletcher): reset, then add
; dosio_buf[0 .. A-1] (A = 0: 256 bytes)
snap_sum0:
        lda #0
        sta snap_fa
        sta snap_fa+1
        sta snap_fb
        sta snap_fb+1
        rts
snap_sumb:
        sta snap_n
        ldx #0
@b:     lda dosio_buf,x
        clc
        adc snap_fa
        sta snap_fa
        bcc :+
        inc snap_fa+1
:       lda snap_fa
        clc
        adc snap_fb
        sta snap_fb
        lda snap_fa+1
        adc snap_fb+1
        sta snap_fb+1
        inx
        cpx snap_n
        bne @b
        rts

; snap_fcmp — Z=1 iff snap_fa/fb (u32 LE) = the loaded header's u32 at Y
snap_fcmp:
        ldx #0
:       lda snap_fa,x
        cmp SNAP_SCR,y
        bne @r
        iny
        inx
        cpx #4
        bne :-
@r:     rts

; snap_ssum — Fletcher of the state span (SNAP_NPG pages from SNAP_ZP, in
; the REU), page by page through dosio_buf
snap_ssum:
        jsr snap_sum0
        lda #>SNAP_ZP
        sta snap_dt+D_SUM+3
        lda #<SNAP_NPG
        sta snap_i
@p:     ldy #D_SUM
        ldx #REU_CMD_FETCH
        jsr snap_dma
        lda #0
        jsr snap_sumb
        inc snap_dt+D_SUM+3
        dec snap_i
        bne @p
        rts

; snap_mkhdr — dosio_buf := this build's header, flags 0, state sum 0,
; header sum 0
snap_mkhdr:
        ldx #0
        txa
:       sta dosio_buf,x
        inx
        bne :-
        ldx #H_TPL - 1
:       lda snap_tpl,x
        sta dosio_buf,x
        dex
        bpl :-
        ldx #7                  ; build ID
:       lda snap_bid,x
        sta dosio_buf+H_BID,x
        dex
        bpl :-
        ldx #REU_CMD_FETCH      ; ROM id: as declared in the REU (SNAP_RID)
        ldy #D_RID
        jmp snap_dma

; snap_xs — A = UREU_LOAD/SAVE, X/Y = first/end offset in snap_sp: ureu_xfer
; each span (addr u24 + len u32 -> ureu_ra/ureu_rl, adjacent).  C=1: failed.
snap_xs:
        sta snap_cmd
        sty snap_sx
@s:     ldy #0
@b:     lda snap_sp,x
        sta ureu_ra,y
        inx
        iny
        cpy #7
        bne @b
        txa
        pha
        lda snap_cmd
        jsr ureu_xfer
        pla
        tax
        bcs @r
        cpx snap_sx
        bne @s
        clc
@r:     rts

; ===========================================================================
; snap_sus — the suspend (from snap_fhook, BASIC out, inside mach_run)
; ===========================================================================
snap_sus:
        php
        sei
        lda snap_req
        sta snap_rq
        jsr mach_rdc            ; mach_now = the counter now (into the image)
        ldx #REU_CMD_STASH
        ldy #D_ZPS
        jsr snap_dma
        ldy #D_DST
        jsr snap_dma
        ldy #D_BSR              ; whole pages (the checksum's span)
        jsr snap_dma
        jsr snap_ssum           ; state checksum, from the REU copy
        jsr snap_mkhdr          ; (keeps snap_fa/fb)
        lda snap_rq
        and #SNAP_RQ_FILE
        sta dosio_buf+H_FLAGS
        ldx #3
:       lda snap_fa,x
        sta dosio_buf+H_SSUM,x
        dex
        bpl :-
        jsr snap_sum0
        lda #SNAP_HSUM
        jsr snap_sumb
        ldx #3
:       lda snap_fa,x
        sta dosio_buf+SNAP_HSUM,x
        dex
        bpl :-
        ldx #REU_CMD_STASH
        ldy #D_HDR
        jsr snap_dma
        jsr mem_rec_dirty       ; C1
        lda #SNAP_S_SUS
        sta snap_st
        bit snap_rq
        bvc @done
        lda #UREU_FWR           ; the file: every span, in order
        jsr ureu_open
        bcs @ferr
        lda #UREU_SAVE
        ldx #0
        ldy #7 * SNAP_NSP
        jsr snap_xs
        php
        jsr ureu_close          ; the save is flushed here
        bcs @fbad
        plp
        bcs @ferr
        lda #SNAP_S_SUSF
        bne @st                 ; always
@fbad:  plp
@ferr:  lda #SNAP_E_XFER
@st:    sta snap_st
@done:  lda #0
        sta snap_req
        lda snap_rq
        bmi @cont
@frz:   jmp @frz                ; frozen (sei): the host resets the machine
@cont:  jsr snap_kill           ; M1: the guest runs on: the REU copy is stale
        plp
        rts

; ===========================================================================
; snap_res — the resume checks and restore (snap_resume, BASIC out).
; C=1: refused, snap_st = SNAP_E_*, nothing restored.  Before the restore,
; every byte of the BSS image range is scratch (the restore rewrites it):
; the loaded header is kept at __BSS_RUN__ (W12-C's cw_* lie there).  F1:
; that page is saved to SNAP_SAV at entry and put back on EVERY refusal,
; so a refused resume leaves the runner byte for byte as it was.
; ===========================================================================
snap_res:
        sei
        ldx #REU_CMD_STASH      ; F1: the scratch page, for every refusal
        ldy #D_SAV
        jsr snap_dma            ; (REC stash: X = STASH survives, kept)
        lda snap_req
        sta snap_rq
        lda #0
        sta snap_req
        ldx #7                  ; an unstamped build never resumes
        lda #0
:       ora snap_bid,x
        dex
        bpl :-
        tax
        bne :+
        lda #SNAP_E_NOID
        jmp snap_fail
:       bit snap_rq
        bvc @hdr
        lda #UREU_FRD
        jsr ureu_open
        bcc :+
        lda #SNAP_E_OPEN
        jmp snap_fail
:       lda #UREU_LOAD          ; span 0: the header
        ldx #0
        ldy #7
        jsr snap_xs
        bcc @hdr
        jmp snap_fxfer
@hdr:   ldx #REU_CMD_FETCH
        ldy #D_HDR              ; -> dosio_buf
        jsr snap_dma
        ldy #D_HSC              ; -> the scratch
        jsr snap_dma
.ifndef SNAP_MUT_NOHSUM
        jsr snap_sum0           ; header checksum
        lda #SNAP_HSUM
        jsr snap_sumb
        ldy #SNAP_HSUM
        jsr snap_fcmp
        beq :+
        lda #SNAP_E_HSUM
        jmp snap_fail
:
.endif
        jsr snap_mkhdr          ; dosio_buf := what this build would write
        ldx #31                 ; M3: a declared ROM id is never all zero
        lda #0
:       ora dosio_buf+H_RID,x
        dex
        bpl :-
        tax
        bne :+
        lda #SNAP_E_NORID
        jmp snap_fail
:
        ldx #0                  ; compare, but the flags and the state sum
@c:     cpx #H_FLAGS
        beq @n
        txa
        and #$FC
        cmp #H_SSUM
        beq @n
        lda dosio_buf,x
        cmp SNAP_SCR,x
        bne @mis
@n:     inx
        cpx #SNAP_HSUM
        bne @c
        beq @ok                 ; always
@mis:   lda #SNAP_E_MAGIC
        cpx #H_NSP
        bcc @f
        lda #SNAP_E_LAY
        cpx #H_BID
        bcc @f
        lda #SNAP_E_BID
        cpx #H_RID
        bcc @f
        lda #SNAP_E_RID
        cpx #H_RID + 32
        bcc @f
        lda #SNAP_E_LAY
@f:     jmp snap_fail
@ok:    ldx #3                  ; keep the state sum: the scratch overlaps BSS
:       lda SNAP_SCR+H_SSUM,x   ; that the UCI loads below use (dosio_uci's)
        sta snap_ss,x
        dex
        bpl :-
        bit snap_rq             ; the file: span 1, the state
        bvc @st
        lda #UREU_LOAD
        ldx #7
        ldy #14
        jsr snap_xs
        bcs snap_fxfer
@st:
.ifndef SNAP_MUT_NOSSUM
        jsr snap_ssum
        ldx #3
:       lda snap_fa,x
        cmp snap_ss,x
        bne :+
        dex
        bpl :-
        bmi :++                 ; always: equal
:
        lda #SNAP_E_SSUM
        jmp snap_fail
:
.endif
        bit snap_rq             ; the file: the guest ranges
        bvc snap_rest
        lda #UREU_LOAD
        ldx #14
        ldy #7 * SNAP_NSP
        jsr snap_xs
        bcs snap_fxfer
        jsr ureu_close
        bcc snap_rest
snap_fxfer:
        lda #SNAP_E_XFER
snap_fail:                      ; A = SNAP_E_*; the file (if any) closed
        sta snap_st
        bit snap_rq
        bvc :+
        jsr ureu_close          ; ("84,NO FILE TO CLOSE" after a close: fine)
:
.ifndef SNAP_MUT_NOSCR
        ldx #REU_CMD_FETCH      ; F1: the scratch page back (cw_*, dosio_uci
        ldy #D_SAV              ; state ...): the runner as it was at entry
        jsr snap_dma
.endif
        jsr mem_rec_dirty
        sec
        rts

; --- the restore: past every check ---
snap_rest:
        jsr disp_init           ; c2v tables, LUT, cop_dhook (the image's
                                ; disp/cop BSS replaces what it reset)
        ldx #REU_CMD_FETCH
        ldy #D_ZPF              ; zero page image -> dosio_buf
        jsr snap_dma
.ifndef SNAP_MUT_NOEXC
        lda dosio_buf + st_exc  ; the step-loop patch as the image had it
        sta st_exc
        jsr emu_reset           ; also w_inv := 1, m_rok := 0 ($80-$87)
.endif
.ifndef SNAP_MUT_NOZP
        ldx #ZP_LO1             ; $10-$7F, $88-$99
@z:     cpx #ZP_HI1 + 1
        bcc @cp
        cpx #ZP_LO2
        bcc @sk
@cp:    lda dosio_buf,x
        sta r_d0 - ZP_LO1,x     ; = $00,x
@sk:    inx
        cpx #ZP_HI2 + 1
        bne @z
.endif
        ldx #REU_CMD_FETCH
.ifndef SNAP_MUT_NODST
        ldy #D_DST
        jsr snap_dma
.endif
.ifndef SNAP_MUT_NOBSS
        ldy #D_BSS              ; exact: snap_req.. after snap_keep untouched
        jsr snap_dma
.endif
        lda #0                  ; W13-D (supervisor, rev-vis option B): the image
        sta spr_vec+1           ; carries spr_vec, the module is not in it: come up
                                ; disarmed; the launcher re-stages and re-arms
        sta inp_mon             ; W14-M (supervisor): the input module likewise
.ifndef SNAP_MUT_NOCIA
        ; CIA #2 time base := ~mach_now (mach.s: C = ~(TB:TA)).  Stopped,
        ; loaded (a TAHI/TBHI write to a stopped timer and the force-load
        ; bit both load the latch), started, then the latches back to
        ; $FFFF while running (only the latch changes).  TA >= $0100, so
        ; it cannot underflow (and reload the old latch) before that.
        lda CIA2_CRA
        and #$C0
        sta CIA2_CRA            ; A stopped
        lda CIA2_CRB
        and #$80
        sta CIA2_CRB            ; B stopped
        ldx #3                  ; TA lo/hi, TB lo/hi ($DD04-$DD07)
:       lda mach_now,x
        eor #$FF
        sta CIA2_TALO,x
        dex
        bpl :-
        lda mach_now+1
        cmp #$FF
        bne :+
        lda #1                  ; TA hi 0 -> 1: at most 255 phi2 earlier
        sta CIA2_TAHI
:       lda CIA2_CRB            ; (CRA/CRB bit 4 reads 0: a strobe)
        ora #$50                ; B: counts A underflows, force load, stopped
        sta CIA2_CRB
        ora #$01                ; B: start
        tax
        lda CIA2_CRA
        ora #$10                ; A: phi2, continuous, force load, stopped
        sta CIA2_CRA
        ora #$01
        stx CIA2_CRB            ; B first: it counts A
        sta CIA2_CRA            ; A: start
        lda #$FF                ; the latches back to $FFFF (running)
        ldx #3
:       sta CIA2_TALO,x
        dex
        bpl :-
        .assert CIA2_TBHI = CIA2_TALO + 3, error, "CIA #2 timer registers must be consecutive"
.endif
.ifndef SNAP_MUT_NOEPOCH
        inc disp_st + DISP_ST_PIX ; I2: one conversion rebuilds the screen
.endif
.ifndef SNAP_MUT_NOINVALL
        jsr code_invall         ; I1 (W12-C): AFTER the BSS restore -- the
.endif                          ; image holds cw_val/tags/hand, not CWIN/cwin_map
        jsr snap_kill           ; M1: consumed (a second resume refuses: E2)
        jsr mem_rec_dirty       ; C1
        lda #SNAP_S_RES
        sta snap_st
        clc
        rts

; snap_kill — M1: overwrite the REU header's first magic byte, so the
; REU-resident snapshot can never be resumed again (after a resume, or a
; suspend that lets the guest continue: the guest memory moves on).  A
; file keeps its own copy.
snap_kill:
        ldx #REU_CMD_STASH
        ldy #D_KILL
        jsr snap_dma
        jmp mem_rec_dirty       ; C1

; --- tables (SCODE is RAM: snap_ssum patches D_SUM's page) ---
snap_dt:                        ; C64 addr, REU addr u24, length
D_ZPS = * - snap_dt
        .word $0000
        .faraddr SNAP_ZP
        .word $0100
D_ZPF = * - snap_dt
        .word dosio_buf
        .faraddr SNAP_ZP
        .word $0100
D_DST = * - snap_dt
        .word disp_st
        .faraddr SNAP_DST
        .word $0100
D_BSS = * - snap_dt
        .word __BSS_RUN__
        .faraddr SNAP_BSS
        .word SNAP_KEEP
D_BSR = * - snap_dt
        .word __BSS_RUN__
        .faraddr SNAP_BSS
        .word SNAP_KEEPR
D_HDR = * - snap_dt
        .word dosio_buf
        .faraddr SNAP_HDR
        .word $0100
D_HSC = * - snap_dt
        .word SNAP_SCR
        .faraddr SNAP_HDR
        .word $0100
D_RID = * - snap_dt
        .word dosio_buf + H_RID
        .faraddr SNAP_RID
        .word 32
D_SAV = * - snap_dt             ; F1: the scratch page <-> its save
        .word SNAP_SCR
        .faraddr SNAP_SAV
        .word $0100
D_KILL = * - snap_dt            ; M1: one byte that is not 'A' over the magic
        .word snap_dt + D_ZPS   ; (D_ZPS's C64 address low byte: $00)
        .faraddr SNAP_HDR
        .word 1
D_SUM = * - snap_dt
        .word dosio_buf
        .faraddr SNAP_ZP
        .word $0100

snap_tpl:                       ; header bytes 0 .. H_TPL - 1
        .byte "A64S", SNAP_VER, SNAP_NSP
        .word SNAP_KEEP, __BSS_RUN__
snap_sp:                        ; the file's spans (= host/snapfmt.py)
        .faraddr SNAP_HDR
        .dword $100
        .faraddr SNAP_ZP
        .dword $200 + SNAP_KEEPR
        .faraddr AROS_CHIP
        .dword TD_ADF_END - AROS_CHIP + 1 ; W13-W: chip RAM AND the staged disk after it (TD_ADF)
        .faraddr AROS_SLOW      ; slow RAM AND $DC0000-$DEFFFF up to the LUT:
        .dword disp_lut - AROS_SLOW ; AROS writes there (measured: not zero at m3)
        .faraddr AROS_EXT
        .dword AROS_EXT_END - AROS_EXT + 1
        .faraddr AROS_ROM
        .dword AROS_ROM_END - AROS_ROM + 1
H_TPL = * - snap_tpl
.assert H_TPL = H_FLAGS, error, "header template must end at the flags byte"
