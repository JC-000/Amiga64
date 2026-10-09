; ---------------------------------------------------------------------------
; tests/aros/mboot.s — the machine-mode boot entry (S3.7, W11-A).
;
; Boots a Kickstart-replacement ROM (AROS) on the interpreter in machine
; mode: docs/CONTRACTS.md "Machine profile and AROS boot (S3.7)", guest map
; runtime/rc/machine.inc.  NOT linked into the runner PRG (the gate PRG
; stays byte-identical): tests/aros_boot.py assembles it at test time to a
; raw binary at MB_ORG (buf_case) and passes the runner's labels as -D
; defines, as tests/irq/drv.s does.  The host calls the entries through the
; runner's mailbox dispatcher (tests/harness_util.py), on VICE and the U64E
; alike.
;
; Entries (JSR from the dispatcher; each clears R+0, sets it to $FF at the
; end, clobbers A/X/Y, and ends any REC use with mem_rec_dirty, C1):
;   aros_fill  zero the REU banks P+0 .. P+0 + P+1 - 1 (P+1 = 0: 256 banks)
;              by REC stashes of one zero byte with the C64 address fixed
;   aros_sum   R+4 u32 := host/machine.fletcher() of the REU span at P+4
;              (u24), P+8 (u24) bytes, a multiple of 256
;   aros_boot  the boot: st_exc := P+13 bit 0 (1 = exception mode, the
;              product setting; 0 only for the red ablation), emu_reset,
;              SSP/PC := the reset longs at guest 0 / 4 (the overlay copy
;              host/machine.py stages from the ROM), SR := $2700, USP 0,
;              mach_s := P+12, then mach_init, chip_reset (CIAs to
;              power-up via cia_reset), cia_reset, cop_reset, disp_init,
;              and mach_run.  mach_run returns only on a real halt (an odd
;              vector, or STOP with nothing left to wake it); the host
;              normally watches the serial ring and the counters while it
;              runs and ends the session itself.  On return: R+1 st_halt,
;              R+2 st_reason, R+8 r_pc, R+12 mach_frames, R+0 = $FF.
;
; Parameter block P (host writes) / result block R (driver writes), both
; in buf_result: P/R at $C900-$C96F lie inside the runner's documented
; case-result span ($C800-$C97F), which nothing uses while the boot runs
; (the boot is the only runner call); clear of the U64 mailbox page
; ($CE00) and the ready flag ($CFFF).
; ---------------------------------------------------------------------------

.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"
.include "../../runtime/rc/machine.inc"

AROS_P    = buf_result + $100   ; parameters
AROS_R    = buf_result + $140   ; results
AROS_PAGE = buf_result + $200   ; aros_sum's fetch page
AROS_ZERO = buf_result + $1FF   ; the zero byte aros_fill stashes
AROS_STASH = $90        ; @value REC command: execute now, C64 -> REU
AROS_FETCH = $91        ; @value REC command: execute now, REU -> C64
AROS_FIXC64 = $80       ; @value REC address control: C64 address fixed

.segment "CODE"

; ---------------------------------------------------------------------------
aros_fill:
        lda #0
        sta AROS_R
        sta AROS_ZERO
        lda AROS_P+0
        sta aros_b
        lda AROS_P+1
        sta aros_n
@lp:    lda #<AROS_ZERO
        sta REC_C64LO
        lda #>AROS_ZERO
        sta REC_C64HI
        lda #0
        sta REC_REULO
        sta REC_REUHI
        sta REC_LENLO           ; length 0 = 64 KB
        sta REC_LENHI
        lda aros_b
        sta REC_REUBANK
        lda #AROS_FIXC64
        sta REC_ACTRL
        lda #AROS_STASH
        sta REC_COMMAND
        inc aros_b
        dec aros_n
        bne @lp
        lda #0
        sta REC_ACTRL
        jsr mem_rec_dirty
        lda #$FF
        sta AROS_R
        rts

; ---------------------------------------------------------------------------
aros_sum:
        lda #0
        sta AROS_R
        ldx #3
@z:     sta AROS_R+4,x
        dex
        bpl @z
        sta REC_ACTRL
        lda AROS_P+4
        sta aros_a
        lda AROS_P+5
        sta aros_a+1
        lda AROS_P+6
        sta aros_a+2
        lda AROS_P+9            ; pages = length >> 8
        sta aros_n
        lda AROS_P+10
        sta aros_n+1
@page:  lda aros_n
        ora aros_n+1
        beq @done
        lda #<AROS_PAGE
        sta REC_C64LO
        lda #>AROS_PAGE
        sta REC_C64HI
        lda aros_a
        sta REC_REULO
        lda aros_a+1
        sta REC_REUHI
        lda aros_a+2
        sta REC_REUBANK
        lda #0
        sta REC_LENLO
        lda #1
        sta REC_LENHI           ; 256 bytes
        lda #AROS_FETCH
        sta REC_COMMAND
        ldy #0
@byte:  lda AROS_PAGE,y         ; a += byte
        clc
        adc AROS_R+4
        sta AROS_R+4
        bcc :+
        inc AROS_R+5
:       lda AROS_R+4            ; b += a
        clc
        adc AROS_R+6
        sta AROS_R+6
        lda AROS_R+5
        adc AROS_R+7
        sta AROS_R+7
        iny
        bne @byte
        inc aros_a+1            ; next page
        bne :+
        inc aros_a+2
:       lda aros_n
        bne :+
        dec aros_n+1
:       dec aros_n
        jmp @page
@done:  jsr mem_rec_dirty
        lda #$FF
        sta AROS_R
        rts

; ---------------------------------------------------------------------------
aros_boot:
        sei
        lda #0
        sta AROS_R
        lda AROS_P+13
        and #1
        sta st_exc              ; latched by emu_reset
        jsr emu_reset
        lda #<AROS_CHIP         ; the reset longs: SSP at 0, PC at 4 (BE)
        sta m_addr
        lda #>AROS_CHIP
        sta m_addr+1
        lda #^AROS_CHIP
        sta m_addr+2
        lda #0
        sta m_addr+3
        jsr mem_read32
        ldx #3
@ssp:   lda m_val,x
        sta r_a7,x
        dex
        bpl @ssp
        lda #<(AROS_CHIP + 4)
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
        sta r_sr                ; CCR clear
        lda #$27
        sta r_sr+1              ; S = 1, I2-I0 = 7 (68000 reset)
        lda AROS_P+12
        sta mach_s
        jsr mach_init
        jsr chip_reset          ; chip shadow, serial ring; ends in cia_reset
        jsr cia_reset           ; (again: the brief's explicit init step)
        jsr cop_reset           ; display state + the copper frame hook
        jsr disp_init           ; LUT, refresh hook
        jsr snap_arm ; W12-T
        jsr mem_rec_dirty
        jsr mach_run
        sei
        lda st_halt
        sta AROS_R+1
        lda st_reason
        sta AROS_R+2
        ldx #3
@out:   lda r_pc,x
        sta AROS_R+8,x
        lda mach_frames,x
        sta AROS_R+12,x
        dex
        bpl @out
        lda #$FF
        sta AROS_R
        rts

aros_b: .res 1
aros_n: .res 2
aros_a: .res 3
