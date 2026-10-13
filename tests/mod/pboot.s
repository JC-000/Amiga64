; ---------------------------------------------------------------------------
; tests/mod/pboot.s — the MOD player's boot entry (W15-M M3).
;
; tests/aros/mboot.s's machine-mode boot (fill, boot) plus the arming of
; W15-P's audio module (tests/paula/aud.s, CONTRACTS "Audio (Paula)",
; "Arming (host)"), in tests/paula/drv.s's order: after mach_init,
; chip_reset and cop_reset, mach_cbv[6] := AUD_ORG+7, the module's init,
; aud_mon := its ID byte 0.  NOT linked into the runner PRG: tests/
; run_player.py assembles it at test time at PB_ORG (buf_case) with the
; runner's labels as -D defines, and calls it through the runner's mailbox
; dispatcher (Ultimates) or a goto trampoline (VICE), as aros_boot.py does.
;
; Entries (each clears R+0, sets it to $FF at the end, clobbers A/X/Y, ends
; any REC use with mem_rec_dirty):
;   pb_fill  zero the REU banks P+0 .. P+0 + P+1 - 1 (P+1 = 0: 256 banks)
;   pb_boot  st_exc := 1, emu_reset, SSP/PC := the longs at guest 0/4,
;            SR := $2700, USP 0, mach_s := P+12; mach_init, chip_reset,
;            cia_reset, cop_reset, disp_init, snap_arm; then, if P+14
;            (the module's ID byte 0) is nonzero: mach_cbv[6] := AUD_ORG+7,
;            the module's init (AUD_ORG+10), aud_mon := P+14; mach_run.
;            On return: aud_mon := 0, mach_cbv[6] := 0; R+1 st_halt, R+2
;            st_reason, R+8 r_pc, R+12 mach_frames, R+0 = $FF.
;
; P/R: buf_result+$100/+$140 ($C900/$C940), as mboot (the module's segment
; A is at $CA00, its state page $CD00: no overlap).
; ---------------------------------------------------------------------------

.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"
.include "../../runtime/rc/machine.inc"

PB_P    = buf_result + $100
PB_R    = buf_result + $140
PB_ZERO = buf_result + $1FF     ; the zero byte pb_fill stashes
PB_AUD  = buf_case + $A00       ; AUD_ORG (runtime/ops/chip.s)
PB_STASH = $90                  ; @value REC command: execute now, C64 -> REU
PB_FIXC64 = $80                 ; @value REC address control: C64 address fixed

.segment "CODE"

pb_fill:
        lda #0
        sta PB_R
        sta PB_ZERO
        lda PB_P+0
        sta pb_b
        lda PB_P+1
        sta pb_n
@lp:    lda #<PB_ZERO
        sta REC_C64LO
        lda #>PB_ZERO
        sta REC_C64HI
        lda #0
        sta REC_REULO
        sta REC_REUHI
        sta REC_LENLO           ; length 0 = 64 KB
        sta REC_LENHI
        lda pb_b
        sta REC_REUBANK
        lda #PB_FIXC64
        sta REC_ACTRL
        lda #PB_STASH
        sta REC_COMMAND
        inc pb_b
        dec pb_n
        bne @lp
        lda #0
        sta REC_ACTRL
        jsr mem_rec_dirty
        lda #$FF
        sta PB_R
        rts

pb_boot:
        sei
        lda #0
        sta PB_R
        lda #1
        sta st_exc              ; exception mode (latched by emu_reset)
        jsr emu_reset
        lda #0                  ; the reset longs: SSP at 0, PC at 4 (BE)
        sta m_addr
        sta m_addr+1
        sta m_addr+2
        sta m_addr+3
        jsr mem_read32
        ldx #3
@ssp:   lda m_val,x
        sta r_a7,x
        dex
        bpl @ssp
        lda #4
        sta m_addr
        jsr mem_read32
        ldx #3
@pc:    lda m_val,x
        sta r_pc,x
        lda #0
        sta r_osp,x             ; USP 0
        dex
        bpl @pc
        lda #$00
        sta r_sr
        lda #$27
        sta r_sr+1              ; S = 1, I2-I0 = 7
        lda PB_P+12
        sta mach_s
        jsr mach_init
        jsr chip_reset
        jsr cia_reset
        jsr cop_reset
        jsr disp_init
        jsr snap_arm
        lda PB_P+14             ; the audio module (W15-P), drv.s's order
        beq @noaud
        lda #<(PB_AUD + 7)
        sta mach_cbv + 12
        lda #>(PB_AUD + 7)
        sta mach_cbv + 13
        jsr PB_AUD + 10         ; its init: every voice plays zeros
        lda PB_P+14
        sta aud_mon
@noaud: jsr mem_rec_dirty
        jsr mach_run
        sei
        lda #0
        sta aud_mon
        sta mach_cbv + 12
        sta mach_cbv + 13
        lda st_halt
        sta PB_R+1
        lda st_reason
        sta PB_R+2
        ldx #3
@out:   lda r_pc,x
        sta PB_R+8,x
        lda mach_frames,x
        sta PB_R+12,x
        dex
        bpl @out
        lda #$FF
        sta PB_R
        rts

pb_b:   .res 1
pb_n:   .res 1
