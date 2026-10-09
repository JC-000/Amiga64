; ===========================================================================
; tests/aros/td.s — W13-W: the machine-mode module of DF0 and the HLE
; trackdisk.device (the MAIN half, state and guarded entries: runtime/ops/td.s)
; Owner: W13-W (w13-wb).  Brief: docs/WAVE13_BRIEF.md W2; design and
; observations: docs/WB_BOOT.md §2.1/§4.1, build/evidence/w13-wb/PROGRESS.md;
; contract: docs/CONTRACTS.md "HLE trackdisk and DF0".
;
; NOT linked into the runner PRG.  tests/aros_wb.py assembles it at test time
; at TD_ORG ($C200, buf_case + $200; end <= $C600: the machine-mode area's
; layout, mboot $C000-$C1FF, td $C200-$C5FF, spr $C600-$C8FF) against
; runner.lbl, passing the runner's labels as -D defines (as mboot.s), stamps
; its ID (the 4 bytes at TD_ORG+3: SHA-1 of the binary with them zero) and
; stages it with the boot driver; a resume re-stages the same bytes and
; runtime/ops/td.s refuses a different module (td_mid).
; Entry: TD_ORG = jmp td_arm (the boot driver's jsr after snap_arm).
;
; The drive (td_prb, reached from cia.s's CIA-B PRB write through td_call):
; the boot model's Floppy (tools/s3probe/wb_disk.py) on the pins PRB |
; ~DDRB: /SEL0 (bit 3) low selects DF0, /MTR (bit 7) is latched at the /SEL0
; fall, a /STEP (bit 0) rise steps one cylinder (DIR bit 1 high = out,
; towards 0; 0..83) and sets /CHNG high (disk present).  CIA-A PRA input
; pins 2-5 (/CHNG /WPRO /TK0 /RDY, read-modify-write of cia_st+CIA_PIN):
; $3C while DF0 is not selected, else /RDY high iff the motor is off, /TK0
; high iff the cylinder is not 0, /WPRO low (write-protected), /CHNG high
; iff a step has seen the disk.
;
; The device (no MFM, no disk DMA): td_fhook (through td_fcall) walks
; SysBase->DeviceList ($4, +350) until it finds "trackdisk.device", then
; rewrites its BeginIO slot (LVO -30, JMP abs.l, 6 bytes) through
; mem_write16/32 to `move.b d0,$BFD100` (also 6 bytes: a CIA-B PRB write)
; and keeps the JMP target in td_orig.  When that store reaches td_prb with
; st_ipc = the slot (the exception-mode loop's PC of the current
; instruction), nothing is stored and td_bio serves the IORequest at A1
; (exec/io.h offsets).  The MOVE stores last and ea_store tail-calls
; mem_write8, so after the trap the instruction only retires: setting
; r_pc/r_a7 here is the device's RTS, or the JMP it replaced.
;   quick CMD_READ / ETD_READ  bounds-checked copy of io_Length bytes of the
;                              image at io_Offset to io_Data; io_Actual =
;                              io_Length, io_Error 0; RTS
;   quick CMD_WRITE, TD_FORMAT, TD_RAWWRITE (and ETD_)  io_Error 28
;                              (TDERR_WriteProt), io_Actual 0; RTS
;   anything else              PC := td_orig: AROS's own trackdisk.device
;                              (it sees the drive above, write-protected);
;                              a NON-quick read is counted in td_nqr (no
;                              disk DMA exists: it would never complete)
; A read past the image's end, or one whose copy would land in bank $BF/$DF
; (the REU under the I/O banks, CONTRACTS "I/O trap" F2), gets io_Error 20
; (TDERR_NotSpecified) and io_Actual = the bytes copied before it.
;
; Copy: REU -> TD_BUF (blt.s's A/B row page: blits are synchronous inside a
; custom-register write, so none is in flight in this trap or the frame
; hook) -> REU, one chunk per destination page piece, code_invpg per chunk
; (W12-C: the bytes bypass mem_write*), mem_rec_dirty at the end (C1).
; Runs inside mach_run ($01 = $35: BASIC out, I/O in).
;
; Scratch: inside the trap and the frame hook the interpreter's EA/ALU
; operands t_ea..t_res ($60-$6F) are dead (instruction boundary, or after
; ea_store's tail call), so they hold the walk/copy pointers (t_op1+3 the
; chunk length, t_op2+3 io_Error, t_res+3 the walk count).  m_addr/m_val
; are the memory API's.  Everything that survives a call is MAIN BSS
; (runtime/ops/td.s), so the W12-T snapshot carries it.
; ===========================================================================

.include "../../runtime/macros.inc"
.include "../../runtime/rc/machine.inc"

; runner labels, -D from runner.lbl (tests/aros_wb.py TD_SYMS)
.ifndef mem_read8
.error "-D mem_read8=... (runner.lbl)"
.endif
.ifndef mem_read16
.error "-D mem_read16=... (runner.lbl)"
.endif
.ifndef mem_read32
.error "-D mem_read32=... (runner.lbl)"
.endif
.ifndef mem_write8
.error "-D mem_write8=... (runner.lbl)"
.endif
.ifndef mem_write16
.error "-D mem_write16=... (runner.lbl)"
.endif
.ifndef mem_write32
.error "-D mem_write32=... (runner.lbl)"
.endif
.ifndef mem_rec_dirty
.error "-D mem_rec_dirty=... (runner.lbl)"
.endif
.ifndef code_invpg
.error "-D code_invpg=... (runner.lbl)"
.endif
.ifndef mach_fhook
.error "-D mach_fhook=... (runner.lbl)"
.endif
.ifndef cia_st
.error "-D cia_st=... (runner.lbl)"
.endif
.ifndef CIA_PIN
.error "-D CIA_PIN=... (runner.lbl)"
.endif
.ifndef CIA_DDRB
.error "-D CIA_DDRB=... (runner.lbl)"
.endif
.ifndef CIA_B
.error "-D CIA_B=... (runner.lbl)"
.endif
.ifndef td_vec
.error "-D td_vec=... (runner.lbl)"
.endif
.ifndef td_fvec
.error "-D td_fvec=... (runner.lbl)"
.endif
.ifndef td_mid
.error "-D td_mid=... (runner.lbl)"
.endif
.ifndef td_fcall
.error "-D td_fcall=... (runner.lbl)"
.endif
.ifndef td_on
.error "-D td_on=... (runner.lbl)"
.endif
.ifndef td_v
.error "-D td_v=... (runner.lbl)"
.endif
.ifndef td_old
.error "-D td_old=... (runner.lbl)"
.endif
.ifndef td_mot
.error "-D td_mot=... (runner.lbl)"
.endif
.ifndef td_cyl
.error "-D td_cyl=... (runner.lbl)"
.endif
.ifndef td_chg
.error "-D td_chg=... (runner.lbl)"
.endif
.ifndef td_len
.error "-D td_len=... (runner.lbl)"
.endif
.ifndef td_slot
.error "-D td_slot=... (runner.lbl)"
.endif
.ifndef td_orig
.error "-D td_orig=... (runner.lbl)"
.endif
.ifndef td_chain
.error "-D td_chain=... (runner.lbl)"
.endif
.ifndef td_nrd
.error "-D td_nrd=... (runner.lbl)"
.endif
.ifndef td_npas
.error "-D td_npas=... (runner.lbl)"
.endif
.ifndef td_nqr
.error "-D td_nqr=... (runner.lbl)"
.endif
.ifndef td_wa
.error "-D td_wa=... (runner.lbl)"
.endif
.ifndef td_whit
.error "-D td_whit=... (runner.lbl)"
.endif
.ifndef TD_ORG
.error "-D TD_ORG=... (runner.lbl)"
.endif

TD_BUF    = blt_abuf            ; shared scratch: blt.s's A/B row page (blits are synchronous, never in flight here)
TD_LIMIT  = TD_ADF_END + 1      ; @value first REU byte past the image
EXEC_DEVL = 350                 ; @value ExecBase.DeviceList (exec/execbase.h)
LN_NAME   = 10                  ; @value exec/nodes.h
IO_CMD    = 28                  ; @value io_Command (u16), then io_Flags, io_Error
IO_ERR    = 31                  ; @value
IO_ACT    = 32                  ; @value io_Actual
IO_LEN    = 36                  ; @value io_Length
IO_DATA   = 40                  ; @value io_Data
IO_OFF    = 44                  ; @value io_Offset (IOStdReq)
LVO_BIO   = -30                 ; @value BeginIO
TD_READ   = 2                   ; @value CMD_READ (exec/io.h)
TD_WRITE  = 3                   ; @value CMD_WRITE
TD_FMT    = 11                  ; @value TD_FORMAT (devices/trackdisk.h)
TD_RAWW   = 17                  ; @value TD_RAWWRITE
TDERR_NS  = 20                  ; @value TDERR_NotSpecified
TDERR_WP  = 28                  ; @value TDERR_WriteProt
TD_CYLS   = 83                  ; @value last cylinder the head reaches (boot model)

.assert (TD_ADF & $FFFF) = 0, error, "td_bio adds TD_ADF's bank byte only"

.segment "CODE"
td_entry:
        jmp td_arm
td_id:  .byte 0, 0, 0, 0        ; the module ID, stamped by the host (TD_ORG+3)
.assert td_id = TD_ORG + 3, lderror, "runtime/ops/td.s reads the ID at TD_ORG+3"

; ---------------------------------------------------------------------------
; ad — m_addr := the u24 at zp X + the sign-extended byte A (m_addr+3 = 0).
; rdl — the same, then m_val := the long there and zp Y..Y+2 := its low 24
; bits; returns A = its high byte (Z: < 16 MB).  Both keep X and Y.
; ---------------------------------------------------------------------------
rdl:    jsr ad
        jsr mem_read32
        lda m_val
        sta a:0,y
        lda m_val+1
        sta a:1,y
        lda m_val+2
        sta a:2,y
        lda m_val+3
        rts

ad:     sta m_addr
        and #$80                ; the offset's sign, extended
        beq :+
        lda #$FF
:       sta m_addr+3
        clc
        lda m_addr
        adc 0,x
        sta m_addr
        lda m_addr+3
        adc 1,x
        sta m_addr+1
        lda m_addr+3
        adc 2,x
        sta m_addr+2
        lda #0
        sta m_addr+3
        rts

; ---------------------------------------------------------------------------
; td_arm — after cia_reset/cop_reset (and snap_arm), before mach_run.  With
; a disk: DF0 not selected (pins 2-5 high), no slot yet, the frame hook.
; ---------------------------------------------------------------------------
td_arm: lda td_on
        beq rts1
        lda #$FF
        sta td_slot+2           ; no slot (a bank-$FF slot: the ROM, never)
        sta td_old
        sta td_mot
        lda cia_st+CIA_PIN
        ora #$3C
        sta cia_st+CIA_PIN
        ldy #3                  ; td_mid := this module's ID
:       lda td_id,y
        sta td_mid,y
        dey
        bpl :-
        lda #<td_fhook          ; the module's entries (runtime/ops/td.s)
        sta td_fvec
        lda #>td_fhook
        sta td_fvec+1
        lda #<td_prb
        sta td_vec
        lda #>td_prb
        sta td_vec+1
        lda mach_fhook
        sta td_chain
        lda mach_fhook+1
        sta td_chain+1
        lda #0                  ; high byte last, 0 = none (mach.s rule)
        sta mach_fhook+1
        lda #<td_fcall          ; the guarded frame hook (MAIN)
        sta mach_fhook
        lda #>td_fcall
        sta mach_fhook+1
rts1:   rts

; ---------------------------------------------------------------------------
; td_fhook — the frame hook: the displaced hook, then (until the slot is
; patched) one walk of DeviceList, at most 32 nodes.
; ---------------------------------------------------------------------------
td_fhook:
.ifdef TD_ZPJUNK                ; evidence build: ... and at a frame hook
        jsr zpjunk
.endif
        lda td_chain+1
        beq :+
        jsr ch
:       lda td_slot+2
        cmp #$FF
        bne rts1
        lda #0                  ; t_op1 := 0, then SysBase = [4] + 256
        ldx #3
:       sta t_op1,x
        dex
        bpl :-
        ldx #t_op1
        ldy #t_op1
        lda #4
        jsr rdl
        inc t_op1+1
        bne :+
        inc t_op1+2
:       lda #EXEC_DEVL - 256    ; t_ea := lh_Head
        ldy #t_ea
        jsr rdl
        lda #32
        sta t_res+3
node:   ldx #t_ea               ; t_op2 := ln_Succ; 0: the tail sentinel
        ldy #t_op2
        lda #0
        jsr rdl
        ora m_val
        ora m_val+1
        ora m_val+2
        beq rts1
        lda #LN_NAME            ; t_op1 := ln_Name
        ldy #t_op1
        jsr rdl
        ldx #t_op1
        ldy #0
cmp1:   tya
        jsr ad
        jsr mem_read8
        lda m_val
        cmp td_name,y
        bne next
        iny
        cpy #TD_NAMEN
        bne cmp1
        ldx #t_ea               ; found: the slot must still be JMP abs.l
        lda #<LVO_BIO
        jsr ad
        jsr mem_read16
        lda m_val
        cmp #$F9
        bne rts1
        lda m_val+1
        cmp #$4E
        bne rts1
        lda #<(LVO_BIO + 2)       ; td_orig := its target
        ldy #t_op1
        jsr rdl
        ldy #2
:       lda t_op1,y
        sta td_orig,y
        dey
        bpl :-
        lda #$00                ; target := $00BFD100
        sta m_val
        sta m_val+3
        lda #$D1
        sta m_val+1
        lda #$BF
        sta m_val+2
        jsr mem_write32
        lda #<LVO_BIO            ; opcode := $13C0, move.b d0,abs.l
        jsr ad
        lda #$C0
        sta m_val
        lda #$13
        sta m_val+1
        jsr mem_write16
        ldy #2                  ; armed: td_slot := base - 30
:       lda m_addr,y
        sta td_slot,y
        dey
        bpl :-
        rts
next:   ldx #2                  ; t_ea := t_op2
:       lda t_op2,x
        sta t_ea,x
        dex
        bpl :-
        dec t_res+3
        beq :+
        jmp node
:       rts
ch:     jmp (td_chain)

; ---------------------------------------------------------------------------
; td_prb — CIA-B PRB write (cia.s, X = CIA_B), A = the written value.
; C=1: the BeginIO slot trapped and was served (nothing to store).
; X = CIA_B on every return; Y is free (cia.s restores it).
; ---------------------------------------------------------------------------
td_prb: ldy td_on              ; the value: td_v (td_call)
        bne :+
        clc
        rts
:
        lda st_ipc
        cmp td_slot
        bne lines
        lda st_ipc+1
        cmp td_slot+1
        bne lines
        lda st_ipc+2
        cmp td_slot+2
        bne lines
        jmp td_bio
lines:  lda cia_st+CIA_B+CIA_DDRB ; the pins: PRB | ~DDRB
        eor #$FF
        ora td_v
        sta td_v
        and #$08
        bne desel
        lda td_old              ; /SEL0 fall: latch /MTR
        and #$08
        beq nolat
        lda td_v
        sta td_mot
nolat:  lda td_old              ; /STEP rise?
        lsr
        bcs pins
        lda td_v
        lsr
        bcc pins
        ldy #1
        sty td_chg
        and #$01                ; DIR (bit 1, shifted)
        beq in
        lda td_cyl
        beq pins
        dec td_cyl
        bpl pins                ; always
in:     lda td_cyl
        cmp #TD_CYLS
        bcs pins
        inc td_cyl
pins:   lda #0
        ldy td_cyl
        beq :+
        ora #$10                ; /TK0 high: not at cylinder 0
:       ldy td_chg
        beq :+
        ora #$04                ; /CHNG high: disk present
:       bit td_mot
        bpl :+
        ora #$20                ; /RDY high: motor off
:       .byte $2C               ; bit abs: skips the lda #$3C below
desel:  lda #$3C
        eor cia_st+CIA_PIN      ; pins 2-5 := A's, the others kept
        and #$3C
        eor cia_st+CIA_PIN
        sta cia_st+CIA_PIN
        lda td_v
        sta td_old
        clc
        rts

; ---------------------------------------------------------------------------
; td_bio — BeginIO(A1 = IORequest), from the trapped slot.  -> C=1, X=CIA_B.
; ---------------------------------------------------------------------------
td_bio: ldx #2                  ; t_ea := io
:       lda r_a1,x
        sta t_ea,x
        dex
        bpl :-
        ldx #t_ea
        ldy #t_op1
        lda #IO_CMD             ; command, flags, error
        jsr rdl
        and #$7F                ; command high byte: 0 or TDF_EXTCOM ($80)
        bne pass
        lsr m_val+1             ; io_Flags bit 0 = IOF_QUICK
        lda m_val+2
        bcs quick
        cmp #TD_READ            ; a non-quick read: counted (no disk DMA)
        bne pass
        inc td_nqr
        jmp pass                ; (aros_wb fails on td_nqr > 0)
quick:  cmp #TD_READ
        beq read
        cmp #TD_WRITE
        beq wp
        cmp #TD_FMT
        beq wp
        cmp #TD_RAWW
        beq wp
pass:   inc td_npas
        ldx #2                  ; the JMP the slot replaced
:       lda td_orig,x
        sta r_pc,x
        dex
        bpl :-
        jmp exit

wp:
        lda #TDERR_WP
        .byte $2C               ; bit abs: skips the lda below
bad:    lda #TDERR_NS
        sta t_op2+3
        lda #0
        sta td_len
        sta td_len+1
        sta td_len+2
        jmp fin

read:   lda #IO_OFF             ; t_op1 := TD_ADF + io_Offset (source)
        jsr rdl
        bne bad
        clc
        lda t_op1+2
        adc #^TD_ADF
        bcs bad
        sta t_op1+2
        lda #IO_LEN             ; t_res := td_len := io_Length
        ldy #t_res
        jsr rdl
        bne bad
        ldy #2
:       lda t_res,y
        sta td_len,y
        dey
        bpl :-
        clc                     ; source end <= TD_LIMIT
        lda t_op1
        adc t_res
        sta m_val
        lda t_op1+1
        adc t_res+1
        sta m_val+1
        lda t_op1+2
        adc t_res+2
        bcs bad
        sta m_val+2
        lda #<TD_LIMIT
        cmp m_val
        lda #>TD_LIMIT
        sbc m_val+1
        lda #^TD_LIMIT
        sbc m_val+2
        bcc bad
        lda #IO_DATA            ; t_op2 := io_Data (24-bit bus)
        ldy #t_op2
        jsr rdl
        lda #0
        sta t_op2+3
        sta REC_ACTRL

copy:   lda t_res
        ora t_res+1
        ora t_res+2
        bne :+
        jmp done
:
        lda t_op2+2             ; never into the REU under $BF/$DF
        cmp #$BF
        beq refuse
        cmp #$DF
        beq refuse
        lda #0                  ; c := 256 - dst low (0 = 256) ...
        sec
        sbc t_op2
        sta t_op1+3
        lda t_res+1             ; ... or n, if smaller
        ora t_res+2
        bne full
        lda t_op1+3
        beq :+
        lda t_res
        cmp t_op1+3
        bcs full
:       lda t_res
        sta t_op1+3
full:   ldx #t_op1              ; fetch: source -> TD_BUF
        lda #REU_CMD_FETCH
        jsr dma
        ldx #t_op2              ; stash: TD_BUF -> destination
        lda #REU_CMD_STASH
        jsr dma
        lda t_op1+1             ; the watched block?
        and #$FE
        cmp td_wa
        bne :+
        lda t_op1+2
        cmp td_wa+1
        bne :+
        inc td_whit
:       lda t_op2+1             ; W12-C: drop a cached code window of the page
        ldx t_op2+2
        jsr code_invpg
        ldx #t_op1              ; src += c, dst += c, n -= c
        jsr add
        ldx #t_op2
        jsr add
        sec
        lda t_res
        sbc m_val
        sta t_res
        lda t_res+1
        sbc m_val+1
        sta t_res+1
        bcs copy
        dec t_res+2
        bcc copy                ; always

refuse: lda #TDERR_NS           ; io_Actual := what was copied
        sta t_op2+3
        sec
        ldx #0
        ldy #3
:       lda td_len,x
        sbc t_res,x
        sta td_len,x
        inx
        dey
        bne :-
        beq dirty               ; always
done:   inc td_nrd
        bne dirty
        inc td_nrd+1
dirty:  jsr mem_rec_dirty

fin:    ldx #t_ea               ; io_Error
        lda #IO_ERR
        jsr ad
        lda t_op2+3
        sta m_val
        jsr mem_write8
        lda #IO_ACT             ; io_Actual
        jsr ad
        ldy #2
:       lda td_len,y
        sta m_val,y
        dey
        bpl :-
        lda #0
        sta m_val+3
        jsr mem_write32
        ldx #r_a7               ; RTS: PC := [A7], A7 += 4
        ldy #r_pc
        lda #0
        jsr rdl
        clc
        lda r_a7
        adc #4
        sta r_a7
        bcc exit
        inc r_a7+1
        bne exit
        inc r_a7+2
exit:   lda #0
        sta r_pc+3
.ifdef TD_ZPJUNK                ; evidence build: $60-$6F are dead after the trap
        jsr zpjunk
.endif
        ldx #CIA_B
        sec
        rts

; dma — one REC transfer of c (t_op1+3, 0 = 256) bytes between TD_BUF and
; the REU at zp X (u24); A = the command.  -> m_val = c (u16).
dma:    pha
        lda 0,x
        sta REC_REULO
        lda 1,x
        sta REC_REUHI
        lda 2,x
        sta REC_REUBANK
        lda #<TD_BUF
        sta REC_C64LO
        lda #>TD_BUF
        sta REC_C64HI
        lda t_op1+3
        sta m_val
        sta REC_LENLO
        cmp #1                  ; C = (c != 0)
        lda #0
        adc #0
        eor #1                  ; 1 iff c = 0 (256 bytes)
        sta m_val+1
        sta REC_LENHI
        pla
        sta REC_COMMAND
        rts

add:    clc                     ; zp X (u24) += m_val (u16)
        lda 0,x
        adc m_val
        sta 0,x
        lda 1,x
        adc m_val+1
        sta 1,x
        bcc :+
        inc 2,x
:       rts

td_name:  .byte "trackdisk.device", 0
TD_NAMEN = * - td_name          ; @value


.ifdef TD_ZPJUNK
zpjunk: ldx #15                 ; t_ea..t_res := junk
:       txa
        eor #TD_ZPJUNK
        sta t_ea,x
        dex
        bpl :-
        rts
.endif
.ifdef TD_VARIANT                ; tests/aros_wb.py: a different module build (ID refusal)
        .byte TD_VARIANT
.endif
td_end:
.assert td_end <= TD_ORG + $400, lderror, "td module overflows its 1 KB (TD_ORG .. TD_ORG + $3FF)"
