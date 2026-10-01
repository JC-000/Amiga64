; ===========================================================================
; tests/cia/shim.s — a stand-in for W9-I's event seam (docs/WAVE9_BRIEF.md
; C1), linked ONLY into run_cia's shim test PRG, never into runtime/.
;
; Time is the host's: mach_etime copies shim_t (written by the host) to
; mach_et.  mach_arm/mach_disarm record per slot (shim_arm, shim_dl);
; mach_post ORs the INTREQ bit into shim_req; the host plays the service
; pass (calls the mach_cbv callbacks itself) and chip.s's INTREQ write
; (calls mach_ihook).  Every entry clobbers A, X
; and Y on purpose, so cia.s cannot lean on registers the seam does not
; promise to keep.
; ===========================================================================

.export mach_etime, mach_et, mach_arm, mach_disarm, mach_at, mach_post
.export mach_cbv, mach_ihook
.export shim_t, shim_req, shim_arm, shim_dl, shim_nops

.segment "BSS"
mach_et:   .res 4
mach_at:   .res 4
mach_cbv:  .res 16
mach_ihook: .res 2      ; INTREQ-write hook (ruling R2), $0000 = none
shim_t:    .res 4
shim_req:  .res 2
shim_arm:  .res 8
shim_dl:   .res 32
shim_nops: .res 2       ; mach_arm + mach_disarm calls

.segment "CODE"

.proc mach_etime
        ldx #3
:       lda shim_t,x
        sta mach_et,x
        dex
        bpl :-
        ldx #$5A
        ldy #$A5
        rts
.endproc

.proc mach_arm
        and #7
        tax
        lda #1
        sta shim_arm,x
        txa
        asl
        asl
        tax
        lda mach_at
        sta shim_dl,x
        lda mach_at+1
        sta shim_dl+1,x
        lda mach_at+2
        sta shim_dl+2,x
        lda mach_at+3
        sta shim_dl+3,x
        jmp count
.endproc

.proc mach_disarm
        and #7
        tax
        lda #0
        sta shim_arm,x
.endproc
count:  inc shim_nops
        bne :+
        inc shim_nops+1
:       ldx #$5A
        ldy #$A5
        lda #$C3
        rts

.proc mach_post
        cmp #8
        bcs hi
        tax
        lda bits,x
        ora shim_req
        sta shim_req
        jmp out
hi:     and #7
        tax
        lda bits,x
        ora shim_req+1
        sta shim_req+1
out:    ldx #$5A
        ldy #$A5
        rts
bits:   .byte 1, 2, 4, 8, 16, 32, 64, 128
.endproc
