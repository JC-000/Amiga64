; ===========================================================================
; runtime/ops/cop.s — S3.5 copper and display-register state
; Owner: W10-P (cop).  Spec: docs/S3_PLAN.md §4 "S3.5", docs/WAVE10_BRIEF.md
; C1-C3; contract: docs/CONTRACTS.md "Copper and display state (S3.5)".
; Reference: host/chipset_ref.CopperAt / DisplayState / DisplayChipset
; (Copper non-strict, Chipset.run_copper).
;
; Display state.  chip.s's custom write path (c_wrw) hands every display or
; copper register write, by the CPU or by the copper, to cop_w0 (page $0xx)
; or cop_w1 (page $1xx) after the shadow store.  The value lands in the
; display-state block disp_st (rc claim, RAM under BASIC ROM, layout C1
; below) and disp_st+0 (gen) is bumped after the field is written, but only
; when the field's value changed.  cop.s never reads disp_st: it compares
; against a private copy (cop_sh), so it needs no banking ($01 any).
;
;   +0 gen u8         +1 dmacon u16 (SET/CLR, bits 0-14)
;   +3 bplcon0/1/2    +9 bplpt[6] u24 (PTH low byte : PTL, as written)
;   +27 bpl1mod/bpl2mod   +31 diwstrt, diwstop, ddfstrt, ddfstop
;   +39 color[32] u16 (as written)            (u16 fields little-endian)
;   +103 valid u8 = COP_VALID once cop_reset ran (undefined RAM before)
;
; Copper (chipset_ref.Copper, non-strict).  An instruction is two guest
; words read with mem_read32.  MOVE (w1 bit 0 = 0): register w1 & $1FE; a
; register below $80 ($40 with COPCON bit 1, CDANG) stops the copper (OCS
; danger rule); COPJMP1/COPJMP2 jump to COP1LC/COP2LC; any other register
; is written through c_wrw (full chip side effects, logged).  $FFFF,$FFFE
; ends the list.  Otherwise w2 bit 0 = SKIP: counted (cop_nskip), no-op;
; else WAIT: satisfied at once (no beam).  A run stops after COP_LIMIT
; instructions (the model's default), or before fetching from bank $BF/$DF
; (the I/O banks; the model has no such memory).  The copper PC is 24-bit,
; bit 0 cleared (the model masks with $1FFFFE: equal below 2 MB).  A copper
; stopped by the end marker or the danger rule stays on that instruction
; (cop_pc points at it), as the hardware's does until the next restart.
;
; When it runs ("copper DMA on" = DMACON DMAEN and COPEN):
;   cop_frame  mach.s's frame hook (mach_fhook, after slot 0's VERTB
;              callback, once per VERTB): with copper DMA on, PC := COP1LC
;              and run; then cop_dhook, every frame.
;   COPJMP1/2  a CPU strobe loads PC from COP1LC/COP2LC; with copper DMA
;              on it runs synchronously in the trap, then cop_dhook.
;   DMACON     a CPU write that turns copper DMA on (off before, on after)
;              runs it synchronously from the loaded PC (none loaded since
;              cop_reset: no run), then cop_dhook
;              (so COP1LC, COPJMP1, DMACON -- the HLE fixture's order --
;              runs the list once).  The copper's own DMACON writes never
;              start a run (cop_busy).
;
; cop_dhook (BSS, cleared at boot; LE, high byte 0 = none) is the refresh
; driver's vector (W10-V's disp_init stores disp_frame); it is called with
; jsr-to-jmp-indirect and may clobber A/X/Y.  In the trap, cop.s saves the
; trap state around the run and the hook (chip_a, m_addr, m_val, Y), so a
; long write that strobes with its first word still writes its second.
;
; Entries:
;   cop_reset  disp_st and cop_sh fields := 0, valid := COP_VALID, gen
;              bumped, counters and PC := 0 (none loaded), mach_fhook :=
;              cop_frame.  Run
;              by the first display/copper write (cop_ok = 0) and by the
;              host.  Clobbers A/X/Y.  cop_dhook is not touched.
;   cop_frame  the frame hook (callback rules: $01 = $35, A/X/Y clobbered).
;   cop_w0/w1  from c_wrw only: X = register offset & $FE (page 0 / 1),
;              chip_vh:chip_vl = value.  Clobber A/X, keep Y.
;
; Host-visible (runner.lbl): cop_ok, cop_sh, cop_dhook, cop_pc (u24 LE),
; cop_n (u16, instructions of the last run), cop_nrun, cop_nfr, cop_nskip
; (u16), cop_why (last stop: COP_END/COP_DANGER/COP_LIMITED/COP_IOBANK).
;
; Link layout: after cia.s, before mach.s (Makefile).
; ===========================================================================

.include "../macros.inc"

.export cop_reset, cop_frame, cop_w0, cop_w1, cop_dhook
.export cop_ok, cop_sh, cop_pc, cop_n, cop_nrun, cop_nfr, cop_nskip, cop_why
.export COP_LIMIT, DISP_SZ
.import mem_read32
.import c_wrw, chip_a, chip_vl, chip_vh, chip_shd       ; chip.s write path
.import mach_fhook                                      ; mach.s frame hook

COP_LIMIT    = 4000     ; @value instructions per run (chipset_ref.Copper.run)
COP_END      = 1        ; @value cop_why: $FFFF,$FFFE
COP_DANGER   = 2        ; @value cop_why: MOVE below the danger threshold
COP_LIMITED  = 3        ; @value cop_why: COP_LIMIT instructions
COP_IOBANK   = 4        ; @value cop_why: PC in bank $BF/$DF
COP_VALID    = $A5      ; @value disp_st valid marker
COP_COPCON   = $2E      ; @value COPCON register offset
COP_CDANG    = $02      ; @value COPCON low byte: CDANG
COP_COP1LC   = $80      ; @value COP1LCH register offset
COP_COP2LC   = $84      ; @value COP2LCH register offset
COP_COPJMP1  = $88      ; @value COPJMP1 register offset
COP_COPJMP2  = $8A      ; @value COPJMP2 register offset
COP_DIWSTRT  = $8E      ; @value DIWSTRT register offset (then DIWSTOP, DDF)
COP_DMACON   = $96      ; @value DMACON register offset
COP_BPLPT    = $E0      ; @value BPL1PTH register offset
COP_COLOR    = $80      ; @value COLOR00 offset in page $1xx
DISP_GEN     = 0        ; @value disp_st: gen
DISP_DMACON  = 1        ; @value disp_st: dmacon
DISP_BPLCON  = 3        ; @value disp_st: bplcon0-2
DISP_BPLPT   = 9        ; @value disp_st: bplpt[6], u24
DISP_BPLMOD  = 27       ; @value disp_st: bpl1mod, bpl2mod
DISP_DIW     = 31       ; @value disp_st: diwstrt, diwstop, ddfstrt, ddfstop
DISP_COLOR   = 39       ; @value disp_st: color[32]
DISP_VALID   = 103      ; @value disp_st: valid
DISP_SZ      = 104      ; @value disp_st size

.segment "BSS"
cop_ok:    .res 1       ; nonzero once cop_reset ran
cop_sh:    .res DISP_SZ ; private copy of disp_st (the compare side)
cop_n:     .res 2       ; instructions of the current/last run
cop_nrun:  .res 2       ; runs (frames, strobes, DMACON turn-ons)
cop_nfr:   .res 2       ; frame-hook calls
cop_nskip: .res 2       ; SKIPs met (treated as no-ops)
cop_why:   .res 1       ; why the last run stopped
cop_pc:    .res 3       ; copper PC (u24 LE)
cop_dhook: .res 2       ; display hook (LE; high byte 0 = none)
cop_busy:  .res 1       ; nonzero while the copper runs
cop_pcok:  .res 1       ; nonzero once a PC was loaded (strobe, frame)
cop_was:   .res 1       ; scratch: copper DMA on before a DMACON write
cop_v:     .res 2       ; value being stored (LE)
cop_y:     .res 1       ; caller's Y (cop_w0/w1)
cop_r:     .res 1       ; scratch: register offset
cop_pg:    .res 1       ; scratch: MOVE register page (0/1)
cop_t:     .res 1       ; scratch: danger threshold
cop_sv:    .res 10      ; trap state around a synchronous run
.assert cop_why = cop_n + 8, error, "cop_reset: counter layout"

.segment "CODE"

; ---------------------------------------------------------------------------
; cop_w0 — page $0xx display/copper registers (X = offset, from c_wrw).
; ---------------------------------------------------------------------------
.proc cop_w0
        cpx #COP_COPJMP1
        bne n1
        ldx #COP_COP1LC
        jmp cop_strobe
n1:     cpx #COP_COPJMP2
        bne n2
        ldx #COP_COP2LC
        jmp cop_strobe
n2:     cpx #COP_DMACON
        beq dma
        cpx #COP_DIWSTRT
        bcc none
        cpx #COP_DMACON
        bcc diw                 ; $8E-$94: DIWSTRT, DIWSTOP, DDFSTRT, DDFSTOP
        cpx #COP_BPLPT
        bcc none
        cpx #COP_BPLPT + 24
        bcs none
        sty cop_y               ; BPLxPTH/L: plane = (X - $E0) / 4
        jsr cop_in
        txa
        sec
        sbc #COP_BPLPT
        lsr
        lsr                     ; C = PTL
        tax
        lda cop_m3,x
        tay
        bcs ptl
        iny                     ; PTH: its low byte is bits 23-16
        iny
        lda cop_v
        jsr cop_fld1
        ldy cop_y
        rts
ptl:    jsr cop_fld2
        ldy cop_y
none:   rts
diw:    sty cop_y
        jsr cop_in
        txa
        sec
        sbc #COP_DIWSTRT - DISP_DIW
        tay
        jsr cop_fld2
        ldy cop_y
        rts
dma:    sty cop_y               ; DMACON: SET/CLR by bit 15 (bits 0-14)
        jsr cop_in
        jsr cop_on
        sta cop_was
        bit cop_v+1
        bmi set
        lda cop_v
        eor #$FF
        and cop_sh+DISP_DMACON
        sta cop_v
        lda cop_v+1
        eor #$FF
        and cop_sh+DISP_DMACON+1
        jmp hi
set:    lda cop_v
        ora cop_sh+DISP_DMACON
        sta cop_v
        lda cop_v+1
        ora cop_sh+DISP_DMACON+1
hi:     and #$7F
        sta cop_v+1
        ldy #DISP_DMACON
        jsr cop_fld2
        ldy cop_y
        lda cop_was             ; a CPU write turning copper DMA on runs it
        bne done
        lda cop_busy            ; (never one from the copper itself)
        bne done
        jsr cop_on
        beq done
        lda cop_pcok            ; no PC loaded since cop_reset: nothing to run
        beq done
        jmp cop_sync
done:   rts
.endproc

; ---------------------------------------------------------------------------
; cop_w1 — page $1xx: BPLCON0-2, BPL1MOD/BPL2MOD, COLOR00-31.
; ---------------------------------------------------------------------------
.proc cop_w1
        cpx #$06
        bcc con                 ; $100-$104 BPLCON0-2
        cpx #$08
        bcc none
        cpx #$0C
        bcc mod                 ; $108/$10A BPL1MOD/BPL2MOD
        cpx #COP_COLOR
        bcc none
        cpx #COP_COLOR + 64
        bcs none
        txa                     ; COLORxx
        sec
        sbc #COP_COLOR - DISP_COLOR
        bne st                  ; always (>= DISP_COLOR)
con:    txa
        clc
        adc #DISP_BPLCON
        bne st                  ; always
mod:    txa
        clc
        adc #DISP_BPLMOD - 8
st:     sty cop_y
        tay
        jsr cop_in
        jsr cop_fld2
        ldy cop_y
none:   rts
.endproc

; cop_in — first display/copper write: cop_reset; then cop_v := the value.
; Keeps X and Y.
.proc cop_in
        lda cop_ok
        bne ok
        stx cop_r
        sty cop_t
        jsr cop_reset
        ldx cop_r
        ldy cop_t
ok:     lda chip_vl
        sta cop_v
        lda chip_vh
        sta cop_v+1
        rts
.endproc

; cop_on — A (and Z) nonzero when copper DMA is on (DMAEN and COPEN).
.proc cop_on
        lda cop_sh+DISP_DMACON
        and #$80                ; COPEN (bit 7)
        beq off
        lda cop_sh+DISP_DMACON+1
        and #$02                ; DMAEN (bit 9)
off:    rts
.endproc

; cop_fld2 — field at disp offset Y := cop_v (u16 LE); gen bumped on a
; change.  cop_fld1 — the byte at Y := A.  Clobber A.
.proc cop_fld2
        lda cop_v+1
        cmp cop_sh+1,y
        bne chg
        lda cop_v
        cmp cop_sh,y
        beq same
chg:    lda cop_v
        sta cop_sh,y
        sta disp_st,y
        lda cop_v+1
        sta cop_sh+1,y
        sta disp_st+1,y
        jmp cop_bump
same:   rts
.endproc

.proc cop_fld1
        cmp cop_sh,y
        beq same
        sta cop_sh,y
        sta disp_st,y
        jmp cop_bump
same:   rts
.endproc

; cop_bump — gen += 1, after the field (the reader copies on a gen change).
.proc cop_bump
        inc cop_sh+DISP_GEN
        lda cop_sh+DISP_GEN
        sta disp_st+DISP_GEN
        rts
.endproc

; ---------------------------------------------------------------------------
; cop_reset — see the header.  Clobbers A/X/Y.
; ---------------------------------------------------------------------------
.proc cop_reset
        lda #1
        sta cop_ok
        lda #0
        ldx #DISP_SZ - 1
lp:     sta cop_sh,x            ; every field but gen (+0)
        sta disp_st,x
        dex
        bne lp
        ldx #8
lp2:    sta cop_n,x             ; cop_n .. cop_why
        dex
        bpl lp2
        sta cop_pc
        sta cop_pc+1
        sta cop_pc+2
        sta cop_busy
        sta cop_pcok
        lda #COP_VALID
        sta cop_sh+DISP_VALID
        sta disp_st+DISP_VALID
        lda #<cop_frame
        sta mach_fhook
        lda #>cop_frame
        sta mach_fhook+1
        jmp cop_bump
.endproc

; ---------------------------------------------------------------------------
; cop_frame — the frame hook: with copper DMA on, the copper from COP1LC;
; then the display hook.  Callback context (outside emu_run).
; ---------------------------------------------------------------------------
.proc cop_frame
        inc cop_nfr
        bne :+
        inc cop_nfr+1
:       jsr cop_on
        beq hook
        ldx #COP_COP1LC
        jsr cop_ldpc
        jsr cop_go
hook:   jmp cop_dhk
.endproc

; ---------------------------------------------------------------------------
; cop_strobe — a CPU COPJMP strobe (X = COP1LC/COP2LC offset): PC := that
; pointer; with copper DMA on, run now (cop_sync).  Keeps Y.
; ---------------------------------------------------------------------------
.proc cop_strobe
        jsr cop_in              ; the first write registers the frame hook
        jsr cop_ldpc
        jsr cop_on
        bne cop_sync
        rts
.endproc

; ---------------------------------------------------------------------------
; cop_sync — run the copper from cop_pc now (trap context), then the
; display hook, keeping the trap's state.  rts to c_wrw's caller with Y
; kept, A/X clobbered.
; ---------------------------------------------------------------------------
.proc cop_sync
        tya
        pha
        ldx #2
sv:     lda chip_a,x
        sta cop_sv,x
        lda m_addr,x
        sta cop_sv+3,x
        dex
        bpl sv
        ldx #3
sv2:    lda m_val,x
        sta cop_sv+6,x
        dex
        bpl sv2
        jsr cop_go
        jsr cop_dhk
        ldx #2
rs:     lda cop_sv,x
        sta chip_a,x
        lda cop_sv+3,x
        sta m_addr,x
        dex
        bpl rs
        ldx #3
rs2:    lda cop_sv+6,x
        sta m_val,x
        dex
        bpl rs2
        pla
        tay
        rts
.endproc

; cop_dhk — call cop_dhook if set.  Clobbers A/X/Y (the hook's rules).
.proc cop_dhk
        lda cop_dhook+1
        beq none
        jmp (cop_dhook)
none:   rts
.endproc
.assert (cop_dhook & $FF) <> $FF, lderror, "jmp (cop_dhook) must not straddle a page"

; cop_go — count a run, then run from cop_pc.
.proc cop_go
        inc cop_nrun
        bne cop_run
        inc cop_nrun+1
        ; fall through
.endproc

; ---------------------------------------------------------------------------
; cop_run — interpret from cop_pc (chipset_ref.CopperAt.run_at).  Clobbers
; A/X, m_addr, m_val, chip_a, chip_vl/vh (and whatever c_wrw's targets
; touch).
; ---------------------------------------------------------------------------
.proc cop_run
        lda #1
        sta cop_busy
        lda #0
        sta cop_n
        sta cop_n+1
loop:   lda cop_n+1             ; n < COP_LIMIT?
        cmp #>COP_LIMIT
        bcc go
        bne lim
        lda cop_n
        cmp #<COP_LIMIT
        bcc go
lim:    lda #COP_LIMITED
        jmp stop
go:     lda cop_pc+2
        cmp #$DF
        beq io
        cmp #$BF
        bne fetch
io:     lda #COP_IOBANK
        jmp stop
fetch:  sta m_addr+2
        lda cop_pc+1
        sta m_addr+1
        lda cop_pc
        sta m_addr
        jsr mem_read32          ; w1 = m_val+3:m_val+2, w2 = m_val+1:m_val
        lda cop_pc              ; pc += 4 (24-bit)
        clc
        adc #4
        sta cop_pc
        bcc :+
        inc cop_pc+1
        bne :+
        inc cop_pc+2
:       inc cop_n
        bne :+
        inc cop_n+1
:       lda m_val+2
        lsr                     ; C = w1 bit 0
        bcs notmv
        lda m_val+3             ; MOVE: register = w1 & $1FE
        and #$01
        sta cop_pg
        bne wr                  ; $1xx: never dangerous
        lda chip_shd+COP_COPCON+1
        and #COP_CDANG
        beq t80
        lda #$40
        bne thr                 ; always
t80:    lda #$80
thr:    sta cop_t
        lda m_val+2
        and #$FE
        cmp cop_t
        bcs safe
        lda #COP_DANGER         ; OCS danger rule: the copper stops
        bne stop                ; always
safe:   cmp #COP_COPJMP1
        beq jmp1
        cmp #COP_COPJMP2
        beq jmp2
wr:     lda m_val+2             ; the write, through chip.s's custom path
        and #$FE
        sta chip_a
        lda cop_pg
        ora #$F0
        sta chip_a+1
        lda #$DF
        sta chip_a+2
        lda m_val
        sta chip_vl
        lda m_val+1
        sta chip_vh
        jsr c_wrw
        jmp loop
jmp1:   ldx #COP_COP1LC
        bne jp                  ; always
jmp2:   ldx #COP_COP2LC
jp:     jsr cop_ldpc
        jmp loop
notmv:  lda m_val+3             ; $FFFF,$FFFE: the end
        and m_val+2
        and m_val+1
        cmp #$FF
        bne wait
        lda m_val
        cmp #$FE
        bne wait
        lda #COP_END
stop:   sta cop_why
        cmp #COP_LIMITED
        bcs done                ; limit / I/O bank: the PC stays where it is
        lda cop_pc              ; end / danger: the copper stays on the
        sec                     ; stopping instruction (pc -= 4)
        sbc #4
        sta cop_pc
        bcs done
        lda cop_pc+1
        bne :+
        dec cop_pc+2
:       dec cop_pc+1
done:   lda #0
        sta cop_busy
        rts
wait:   lda m_val
        lsr                     ; C = w2 bit 0: SKIP
        bcc nxt                 ; WAIT: satisfied at once
        inc cop_nskip           ; SKIP: counted, no-op (non-strict model)
        bne nxt
        inc cop_nskip+1
nxt:    jmp loop
.endproc

; cop_ldpc — cop_pc := the big-endian pointer at chip_shd+X, bit 0 clear
; (bits 31-24 ignored); a PC is loaded.  Clobbers A.
.proc cop_ldpc
        lda #1
        sta cop_pcok
        lda chip_shd+3,x
        and #$FE
        sta cop_pc
        lda chip_shd+2,x
        sta cop_pc+1
        lda chip_shd+1,x
        sta cop_pc+2
        rts
.endproc

.segment "RODATA"
cop_m3:    .byte DISP_BPLPT, DISP_BPLPT + 3, DISP_BPLPT + 6
           .byte DISP_BPLPT + 9, DISP_BPLPT + 12, DISP_BPLPT + 15
.segment "CODE"
