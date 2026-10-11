; ===========================================================================
; runtime/ops/td.s — W13-W: the MAIN half of DF0 / the HLE trackdisk
; Owner: W13-W (w13-wb).  Brief: docs/WAVE13_BRIEF.md W2 and the
; supervisor's budget ruling (MAIN <= 96 B); contract: docs/CONTRACTS.md
; "HLE trackdisk and DF0".
;
; The code lives in a machine-mode module (tests/aros/td.s), assembled at
; test time against runner.lbl and staged at TD_ORG in buf_case, like
; tests/aros/mboot.s.  MAIN keeps what the W12-T snapshot must carry (the
; state below, all BSS) and the two guarded entries into the module:
;   td_call   cia.s's W13-W PRB hook: A = the written value, X = CIA_B.
;             td_vec = 0 (no module: program mode, a boot without a disk,
;             every existing suite): C=0 at once.  Else the module's ID
;             (TD_ORG+3, 4 bytes) must equal td_mid (taken by td_arm), or
;             nothing is called (C=0).  -> the module's td_prb with the
;             value in td_v.  X kept.
;   td_fcall  the frame hook td_arm installs: the same ID check, then the
;             module's frame hook (td_fvec).  A mismatch (a resume onto a
;             different module) halts the machine: st_halt 1, st_reason
;             TD_HALT_MOD (td_call keeps refusing meanwhile).
; ===========================================================================

.include "../macros.inc"

.export td_call, td_fcall
.export td_vec, td_fvec, td_mid, td_on, td_v, td_old, td_mot, td_cyl, td_chg
.export td_len, td_slot, td_orig, td_chain, td_nrd, td_npas, td_nqr
.export td_wa, td_whit, td_zero, TD_NZERO
.export TD_ORG, TD_HALT_MOD

TD_ORG      = buf_case + $200   ; @value the module's org (mboot $C000-$C1FF, td $C200-$C5FF)
TD_HALT_MOD = $57               ; @value st_reason: the staged module is not the snapshot's

.segment "BSS"
td_vec:   .res 2                ; the module's td_prb (0 = no module)
td_fvec:  .res 2                ; the module's frame hook
td_mid:   .res 4                ; the module ID at arm time (= TD_ORG+3..6)
td_on:    .res 1                ; host: 1 = DF0 holds the disk staged at TD_ADF
td_wa:    .res 2                ; host: watched block's REU address bits 15-9, 23-16
td_chain: .res 2                ; the frame hook td_arm displaced
td_zero:                        ; td_arm zeroes from here (L5: a clean second boot)
td_v:     .res 1                ; the pins of this PRB write
td_old:   .res 1                ; the pins of the previous one
td_mot:   .res 1                ; bit 7: 0 = motor on (latched /MTR)
td_cyl:   .res 1                ; head cylinder 0..83
td_chg:   .res 1                ; nonzero: a step has seen the disk (/CHNG high)
td_len:   .res 3                ; io_Actual of the request being served
td_slot:  .res 3                ; the patched BeginIO slot ($FFxxxx: none yet)
td_orig:  .res 3                ; the slot's original JMP target
td_nrd:   .res 2                ; reads served
td_npas:  .res 1                ; requests passed to the native BeginIO (mod 256)
td_nqr:   .res 1                ; NON-quick reads passed (no disk DMA: they would hang)
td_whit:  .res 1                ; chunks read from the watched block (mod 256)
TD_NZERO = * - td_zero          ; @value

.segment "CODE"
td_call:
        sta td_v
        lda td_vec+1
        beq no
        jsr chk
        bne no
        jmp (td_vec)            ; td_prb (the value is in td_v)
no:     clc
        rts

td_fcall:
        jsr chk
        bne bad
        jmp (td_fvec)
bad:    lda #TD_HALT_MOD
        sta st_reason
        lda #1
        sta st_halt
        rts

chk:    ldy #3                  ; Z=1: the staged module is the armed one
:       lda TD_ORG+3,y
        cmp td_mid,y
        bne :+
        dey
        bpl :-
        iny                     ; Y = 0: Z=1
:       rts

.assert (td_vec & $FF) <> $FF, lderror, "jmp (td_vec) must not straddle a page"
.assert (td_fvec & $FF) <> $FF, lderror, "jmp (td_fvec) must not straddle a page"
.assert (td_chain & $FF) <> $FF, lderror, "the module's jmp (td_chain) must not straddle a page"
