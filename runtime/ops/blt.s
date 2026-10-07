; ===========================================================================
; runtime/ops/blt.s — S3.6 blitter v0 (synchronous, normal mode + fill)
; Owner: W11-B (blt).  Spec: docs/S3_PLAN.md §3.4, §3.5, §4 "S3.6";
; docs/WAVE11_BRIEF.md C1/C3; contract: docs/CONTRACTS.md "Blitter (S3.6)".
; Reference: host/chipset_ref.Blitter (normal mode), Paula.dmaconr.
;
; chip.s's custom write path (c_wrw) stores every write to $040-$074 in
; chip_shd as usual and then calls blt_w (X = register offset):
;   BLTSIZE ($058)  runs the whole blit now, inside the trap (BBUSY never
;                   reads 1).  OCS size: h = bits 15-6 (0 = 1024 rows),
;                   w = bits 5-0 (0 = 64 words).
;   BLTBDAT ($072)  the CPU's B write: shifted once, now, with the BSH then
;                   in BLTCON1 (Blitter.write_bdat) into the B hold register.
; Every other blitter register is only read from chip_shd at BLTSIZE.
;
; Normal mode = chipset_ref.Blitter.blit: channels A/B/C/D (BLTCON0 bits
; 11-8), minterm LF (bit 7 = ABC ... bit 0 = ~A~B~C), A masked by AFWM on
; the first and ALWM on the last word of each row (both when w = 1) before
; its shift, ASH/BSH (the old word carried across words and rows, 0 at the
; start of a blit), the four signed modulos, descending mode (BLTCON1 bit
; 1: addresses step down, shifts go left), inclusive/exclusive fill (bits
; 3/4, both = exclusive) with the carry starting at FCI (bit 2) on every
; row, BLTxDAT used when a channel's DMA is off (B: the hold register),
; BZERO (no 1 bit in any D word, D DMA on or off).  Afterwards the four
; pointers are written back ((p >> 16) & $1F : p & $FFFF) and BLTADAT /
; BLTBDAT / BLTCDAT hold the last word their channel fetched.
; Line mode (BLTCON1 bit 0): counted in blt_nline, memory, pointers and
; BZERO untouched (the model's line() is an unverified approximation).
;
; Completion (both modes): INTREQ BLIT (bit 6) is set by chip_blitirq, the
; Paula SET path of c_wrw (mach_hold ORed back, mach_ichk).  DMACONR reads
; (DMACON & $07FF) | BZERO (chip.s c_rdw -> blt_dmaconr); DMACON is cop.s's
; field cop_sh+1 (docs/CONTRACTS.md "Copper and display state", C1 +1).
; After every normal blit with D on, the pixel epoch byte disp_st+BLT_EPOCH
; is incremented in place (inc, BASIC out; C3 Addendum 3: a private counter
; could equal a reader's snapshot and hide the first blit).
;
; Memory.  Every channel address is a guest address masked to the 2 MB
; chip range the model uses ($1FFFFE at the start, as Blitter._ptr; the
; arithmetic then runs in 24 bits and every DMA takes address & $1FFFFF),
; so a blit never reads or writes banks $BF/$DF (the REU there holds
; host-private data, e.g. the c2v LUT at $DF0000).  A row of A/B/C is
; fetched with one REU DMA (two if it crosses a 512 KB REC line: the REC's
; address counter carries only through bit 18, core.s "512 KB straddles"),
; combined in C64 RAM (blt_abuf/blt_cbuf, rc ram.inc, under BASIC ROM:
; BASIC is banked out for the blit, the caller's $01 restored) and stored
; as one D row.  The REC use ends with mem_rec_dirty (C1).  A D row that
; lands in the guest page of r_pc invalidates the instruction window (one
; mem_read8/mem_write8 of the same byte through the memory API at the end
; of the blit: core.s's WINCHK), so a blit over code about to run is seen.
;
; ABI: blt_w from c_wrw only (trap context or the copper's MOVE path):
; Y kept, A/X clobbered; m_addr, m_val, t_tmp and chip_a are preserved (a
; long write's second word still lands).  chip_vl/chip_vh are clobbered by
; the IRQ raise (c_wrw's callers reload them per word).
;
; Host-visible (runner.lbl): blt_n (u16 normal blits), blt_nline (u16 line
; blits), blt_nwd (u32 words of normal blits), blt_bz ($20 = BZERO),
; blt_bhold (u16 LE); entry blt_reset (host: blitter
; state to power-up: BZERO 0, hold registers 0, counters 0).
;
; Link layout: after cop.s, before disp.s (Makefile).
; ===========================================================================

.include "../macros.inc"

.export blt_w, blt_dmaconr, blt_reset
.export blt_n, blt_nline, blt_nwd, blt_bz, blt_bhold
.import chip_shd, chip_vl, chip_vh, chip_blitirq        ; chip.s
.import cop_sh                                          ; cop.s: C1 copy (+1 dmacon)
.import mem_rec_dirty, mem_read8, mem_write8            ; core.s

BLT_CON0  = $40         ; @value BLTCON0 register offset
BLT_CON1  = $42         ; @value BLTCON1
BLT_AFWM  = $44         ; @value BLTAFWM (then BLTALWM)
BLT_CPT   = $48         ; @value BLTCPTH (then B, A, D: 4 bytes apart)
BLT_SIZE  = $58         ; @value BLTSIZE
BLT_CMOD  = $60         ; @value BLTCMOD (then B, A, D: 2 bytes apart)
BLT_CDAT  = $70         ; @value BLTCDAT
BLT_BDAT  = $72         ; @value BLTBDAT
BLT_ADAT  = $74         ; @value BLTADAT
BLT_JC    = 0           ; @value channel index C (register order: C, B, A, D)
BLT_JB    = 1           ; @value channel index B
BLT_JA    = 2           ; @value channel index A
BLT_JD    = 3           ; @value channel index D
BLT_ENA   = $08         ; @value BLTCON0 high byte: USEA
BLT_ENB   = $04         ; @value USEB
BLT_ENC   = $02         ; @value USEC
BLT_END   = $01         ; @value USED
BLT_LINE  = $01         ; @value BLTCON1 low byte: LINE
BLT_DESC  = $02         ; @value BLTCON1 low byte: DESC
BLT_FCI   = $04         ; @value BLTCON1 low byte: FCI
BLT_IFE   = $08         ; @value BLTCON1 low byte: IFE
BLT_FILLM  = $18         ; @value BLTCON1 low byte: IFE | EFE
BLT_BZERO = $20         ; @value DMACONR high byte: BZERO (bit 13)
BLT_BANKS = $1F         ; @value REU bank mask: the 2 MB chip range
BLT_EPOCH   = 105         ; @value disp_st: pix (C3 pixel epoch)
BLT_DMACON = 1          ; @value cop_sh: dmacon (C1 +1, u16 LE)
BLT_LF_CA = $CA         ; @value cookie-cut minterm (fast kernel)
BLT_LF_A  = $F0         ; @value D = A (fast kernel)

blt_bbuf = blt_abuf + $80       ; B row (same page as A: blt_prep's X)
blt_dbuf = blt_cbuf + $80       ; D row

.segment "BSS"
; --- machine state (cleared at boot and by blt_reset) ---
blt_bz:    .res 1       ; BZERO of the last normal blit (BLT_BZERO or 0)
blt_bhold: .res 2       ; B hold register: what a blit with B DMA off uses (LE)
blt_n:     .res 2       ; normal blits (u16)
blt_nline: .res 2       ; line-mode blits (u16)
blt_nwd:   .res 4       ; words of normal blits (u32)
BLT_NST = * - blt_bz
blt_y:     .res 1       ; caller's Y
blt_p01:   .res 1       ; caller's $01
blt_cnt:   .res 1       ; shift / fill bit counter
blt_w2:    .res 1       ; shift window, top byte
; shift operands: (h1 h0 l1 l0) >> blt_sh -> q1 q0
blt_h1:    .res 1
blt_h0:    .res 1
blt_l1:    .res 1
blt_l0:    .res 1
blt_q1:    .res 1
blt_q0:    .res 1
blt_sh:    .res 1
blt_smc:   .res 1       ; nonzero: a D row hit the page of r_pc
blt_sv:    .res 12      ; m_addr, m_val, t_tmp around the window invalidation
; --- per blit ---
blt_en:    .res 1       ; USEA..USED (BLTCON0 bits 11-8)
blt_lf:    .res 1       ; minterm
blt_c1:    .res 1       ; BLTCON1 low byte
blt_dsc:   .res 1       ; nonzero: descending
blt_ash:   .res 1       ; ASH
blt_bsh:   .res 1       ; BSH
blt_wd:    .res 1       ; words per row (1-64)
blt_wb:    .res 1       ; bytes per row
blt_wbm1:  .res 1       ; bytes per row - 1
blt_adj:   .res 1       ; bytes per row - 2 (row start = pointer - adj, descending)
blt_fpos:  .res 1       ; byte offset of the row's first word (processing order)
blt_lpos:  .res 1       ; ... and of its last word
blt_rows:  .res 2       ; rows left
blt_r0:    .res 4       ; row start (lowest address) per channel C, B, A, D: bits 7-0
blt_r1:    .res 4       ;   bits 15-8
blt_r2:    .res 4       ;   bits 23-16
blt_s0:    .res 4       ; row step per channel (s24: +-(2w + modulo))
blt_s1:    .res 4
blt_s2:    .res 4
blt_nz:    .res 1       ; OR of every D byte (before fill)
blt_nzf:   .res 1       ; OR of every filled D byte
blt_k:     .res 8       ; generic minterm: (and, eor) per (A, B) = 11, 10, 01, 00
blt_g:     .res 3       ; generic minterm scratch
blt_fc:    .res 1       ; fill carry
blt_fb:    .res 1       ; fill: source byte
blt_fr:    .res 1       ; fill: result byte
blt_fj:    .res 1       ; fill: byte counter
blt_efe:   .res 1       ; fill: nonzero = exclusive
blt_t:     .res 1       ; scratch
; channel records for blt_prep: A at +0, B at +BLT_PRSZ
BLT_PRSZ = 10             ; @value record size
blt_pr:    .res 2 * BLT_PRSZ
; the working record (blt_prep's view)
blt_px:    .res 1       ; first word's byte offset in blt_abuf's page
blt_mf1:   .res 1       ; first-word mask (hi, lo)
blt_mf0:   .res 1
blt_ml1:   .res 1       ; last-word mask
blt_ml0:   .res 1
blt_ps:    .res 1       ; shift: ascending ASH/BSH, descending 16 - it
blt_o1:    .res 1       ; old word (hi, lo), carried across words and rows
blt_o0:    .res 1
blt_rl1:   .res 1       ; raw last word of the row (hi, lo)
blt_rl0:   .res 1
.assert blt_rl0 - blt_px = BLT_PRSZ - 1, error, "blt_prep record layout"
blt_a1:    .res 1       ; current word (masked)
blt_a0:    .res 1
blt_first: .res 1
; DMA
blt_xc:    .res 2       ; C64 address
blt_xa:    .res 3       ; guest address
blt_xn:    .res 1       ; length
blt_xn1:   .res 1       ; first part of a split
blt_cmd:   .res 1

.segment "CODE"

; ---------------------------------------------------------------------------
; blt_w — the MAIN entry (from c_wrw; X = offset).  BLTSIZE and BLTBDAT go
; to BCODE (blt_bank) with BASIC ROM banked out ($37 -> $36; $36/$35/$34
; are left as they are), the caller's $01 is restored on the way back, and
; a blit then raises INTREQ BLIT.  Keeps Y; A/X clobbered.
; IRQs stay as the caller had them: the machine-loop IRQ/NMI handlers and
; vectors are in MAIN / RAM under KERNAL (mach.s, $FFFA) or the KERNAL ROM
; ($36 keeps KERNAL in), never in BCODE, and none of them depends on BASIC
; or touches the REC (CONTRACTS "Machine loop"), so one firing mid-blit
; runs and returns to BCODE unchanged.
; ---------------------------------------------------------------------------
.proc blt_w
        cpx #BLT_SIZE
        beq go
        cpx #BLT_BDAT
        beq go
        rts
go:     sty blt_y
        lda CPU_PORT
        sta blt_p01
        and #3
        cmp #3
        bne :+
        dec CPU_PORT            ; $37 -> $36: BASIC out (BCODE, row buffers)
:       jsr blt_bank            ; A = nonzero after a blit
        tax
        lda blt_p01             ; the caller's $01, on every path
        sta CPU_PORT
        txa
        beq :+
        jsr chip_blitirq        ; INTREQ BLIT (Paula SET path)
:       ldy blt_y
        rts
.endproc

; ---------------------------------------------------------------------------
.proc blt_dmaconr
        lda cop_sh+BLT_DMACON
        sta chip_vl
        lda cop_sh+BLT_DMACON+1
        and #$07
        ora blt_bz
        sta chip_vh
        rts
.endproc

; ---------------------------------------------------------------------------
.proc blt_reset
        lda #0
        ldx #BLT_NST - 1
lp:     sta blt_bz,x
        dex
        bpl lp
        rts
.endproc

; ===========================================================================
; The blit proper: everything below runs with BASIC banked out (segment
; BCODE, linked at $A000 under BASIC ROM: runtime/c64.cfg, rc core region).
; Entered only from blt_blit (MAIN), which banks BASIC out first and
; restores the caller's $01 after the last return from here.
; ===========================================================================
.segment "BCODE"

blt_bfl:   .byte <blt_cbuf, <blt_bbuf, <blt_abuf, <blt_dbuf      ; per channel C, B, A, D
blt_bfh:   .byte >blt_cbuf, >blt_bbuf, >blt_abuf, >blt_dbuf


; ---------------------------------------------------------------------------
; blt_bank — BCODE dispatch from blt_w (BASIC out): X = BLTSIZE blits
; (A = 1), else the BLTBDAT write (A = 0).
; ---------------------------------------------------------------------------
.proc blt_bank
        cpx #BLT_SIZE
        bne :+
        jmp blt_blit
:       jsr blt_wbdat
        lda #0
        rts
.endproc

; ---------------------------------------------------------------------------
.proc blt_wbdat
        lda chip_shd+BLT_CON1
        lsr
        lsr
        lsr
        lsr
        sta blt_sh
        lda #0
        sta blt_h1
        sta blt_h0
        sta blt_l1
        sta blt_l0
        lda chip_shd+BLT_CON1+1
        and #BLT_DESC
        beq asc
        lda chip_vh             ; descending: (v : 0) >> (16 - BSH)
        sta blt_h1
        lda chip_vl
        sta blt_h0
        lda #16
        sec
        sbc blt_sh
        sta blt_sh
        jmp go
asc:    lda chip_vh             ; ascending: (0 : v) >> BSH
        sta blt_l1
        lda chip_vl
        sta blt_l0
go:     jsr blt_shr
        lda blt_q1
        sta blt_bhold+1
        lda blt_q0
        sta blt_bhold
        rts
.endproc

; ---------------------------------------------------------------------------
.proc blt_shr
        lda blt_sh
        beq s0
        cmp #16
        beq s16
        cmp #8
        bcc lo
        sbc #8                  ; C = 1: >= 8 drops l0
        sta blt_cnt
        lda blt_h0
        sta blt_q1
        lda blt_l1
        sta blt_q0
        lda blt_h1
        jmp sh
lo:     sta blt_cnt
        lda blt_l1
        sta blt_q1
        lda blt_l0
        sta blt_q0
        lda blt_h0
sh:     sta blt_w2              ; the window's top byte
        lda blt_cnt
        beq done
        lda blt_w2
lp:     lsr
        ror blt_q1
        ror blt_q0
        dec blt_cnt
        bne lp
done:   rts
s0:     lda blt_l1
        sta blt_q1
        lda blt_l0
        sta blt_q0
        rts
s16:    lda blt_h1
        sta blt_q1
        lda blt_h0
        sta blt_q0
        rts
.endproc

; ---------------------------------------------------------------------------
.proc blt_blit
        lda chip_shd+BLT_CON1+1
        sta blt_c1
        and #BLT_LINE
        beq normal
        inc blt_nline           ; line mode: counted, no memory access
        bne :+
        inc blt_nline+1
:       jmp fin
normal: jsr blt_setup
row:    jsr blt_row
        lda blt_nwd             ; words += w
        clc
        adc blt_wd
        sta blt_nwd
        bcc :+
        inc blt_nwd+1
        bne :+
        inc blt_nwd+2
        bne :+
        inc blt_nwd+3
:       lda blt_rows
        bne :+
        dec blt_rows+1
:       dec blt_rows
        lda blt_rows
        ora blt_rows+1
        bne row
        jsr blt_done
        jsr mem_rec_dirty       ; C1: the REC was ours
        lda blt_smc
        beq fin
        jsr blt_winv
fin:    lda #1                  ; blt_w raises INTREQ BLIT
        rts
.endproc

; ---------------------------------------------------------------------------
.proc blt_winv
        ldx #3
sv:     lda m_addr,x
        sta blt_sv,x
        lda m_val,x
        sta blt_sv+4,x
        lda t_tmp,x
        sta blt_sv+8,x
        dex
        bpl sv
        lda #0
        sta m_addr
        sta m_addr+3
        lda r_pc+1
        sta m_addr+1
        lda r_pc+2
        sta m_addr+2
        jsr mem_read8
        jsr mem_write8
        ldx #3
rs:     lda blt_sv,x
        sta m_addr,x
        lda blt_sv+4,x
        sta m_val,x
        lda blt_sv+8,x
        sta t_tmp,x
        dex
        bpl rs
        rts
.endproc

; ---------------------------------------------------------------------------
; blt_setup — decode the registers, channel addresses and steps, prefill
; the rows of the channels whose DMA is off.
; ---------------------------------------------------------------------------
.proc blt_setup
        inc blt_n
        bne :+
        inc blt_n+1
:       lda #0
        sta blt_nz
        sta blt_nzf
        sta blt_smc
        lda chip_shd+BLT_CON0
        and #$0F
        sta blt_en
        lda chip_shd+BLT_CON0
        lsr
        lsr
        lsr
        lsr
        sta blt_ash
        lda chip_shd+BLT_CON0+1
        sta blt_lf
        lda chip_shd+BLT_CON1
        lsr
        lsr
        lsr
        lsr
        sta blt_bsh
        lda blt_c1
        and #BLT_DESC
        sta blt_dsc
        ; size: w = bits 5-0 (0 = 64), h = bits 15-6 (0 = 1024)
        lda chip_shd+BLT_SIZE+1
        and #$3F
        bne :+
        lda #64
:       sta blt_wd
        asl
        sta blt_wb
        tax
        dex
        stx blt_wbm1
        dex
        stx blt_adj
        lda chip_shd+BLT_SIZE+1
        sta blt_t
        lda chip_shd+BLT_SIZE
        asl blt_t
        rol
        asl blt_t
        rol                     ; A = h bits 7-0
        sta blt_rows
        lda chip_shd+BLT_SIZE
        lsr
        lsr
        lsr
        lsr
        lsr
        lsr
        sta blt_rows+1          ; h bits 9-8
        ora blt_rows
        bne :+
        lda #>1024
        sta blt_rows+1
:       ; first / last word of a row in processing order
        lda blt_dsc
        beq :+
        lda blt_adj             ; descending: first = adj, last = 0
        ldx #0
        beq :++                 ; always
:       lda #0                  ; ascending: first = 0, last = adj
        ldx blt_adj
:       sta blt_fpos
        stx blt_lpos
        ; channels: row start = pointer & $1FFFFE (descending: - adj),
        ; step = 2w + modulo (descending: negated)
        ldx #3
ch:     txa
        asl
        asl
        tay
        lda chip_shd+BLT_CPT+3,y
        and #$FE
        sta blt_r0,x
        lda chip_shd+BLT_CPT+2,y
        sta blt_r1,x
        lda chip_shd+BLT_CPT+1,y
        and #BLT_BANKS
        sta blt_r2,x
        txa
        asl
        tay
        lda chip_shd+BLT_CMOD+1,y
        clc
        adc blt_wb
        sta blt_s0,x
        lda chip_shd+BLT_CMOD,y
        adc #0
        sta blt_s1,x
        lda chip_shd+BLT_CMOD,y
        and #$80
        beq :+
        lda #$FF
:       adc #0
        sta blt_s2,x
        lda blt_dsc
        beq nx
        sec                     ; descending: step := -step
        lda #0
        sbc blt_s0,x
        sta blt_s0,x
        lda #0
        sbc blt_s1,x
        sta blt_s1,x
        lda #0
        sbc blt_s2,x
        sta blt_s2,x
        sec                     ; row start := pointer - adj
        lda blt_r0,x
        sbc blt_adj
        sta blt_r0,x
        lda blt_r1,x
        sbc #0
        sta blt_r1,x
        lda blt_r2,x
        sbc #0
        sta blt_r2,x
nx:     dex
        bpl ch
        ; blt_prep records: A (AFWM/ALWM), B (no masks)
        ldx #2 * BLT_PRSZ - 1
        lda #0
cl:     sta blt_pr,x
        dex
        bpl cl
        lda blt_fpos
        sta blt_pr+0
        ora #$80
        sta blt_pr+BLT_PRSZ+0
        ldx #3
mk:     lda chip_shd+BLT_AFWM,x
        sta blt_pr+1,x
        lda #$FF
        sta blt_pr+BLT_PRSZ+1,x
        dex
        bpl mk
        lda blt_ash
        jsr psh
        sta blt_pr+5
        lda blt_bsh
        jsr psh
        sta blt_pr+BLT_PRSZ+5
        ; minterm constants (generic kernel): for (A, B) = 11, 10, 01, 00
        ; the LF bit pair (C = 1, C = 0) gives and = c1 ^ c0, eor = c0
        lda blt_lf
        sta blt_t
        ldx #0
mt:     lda #0
        asl blt_t
        adc #0
        sta blt_g               ; c1
        lda #0
        asl blt_t
        adc #0                  ; c0
        tay
        eor blt_g
        jsr neg
        sta blt_k,x
        tya
        jsr neg
        sta blt_k+1,x
        inx
        inx
        cpx #8
        bne mt
        ; channels with DMA off: their rows once (A only without a shift)
        lda blt_en
        and #BLT_ENC
        bne nc
        ldx blt_wbm1
cf:     lda chip_shd+BLT_CDAT+1
        sta blt_cbuf,x
        dex
        lda chip_shd+BLT_CDAT
        sta blt_cbuf,x
        dex
        bpl cf
nc:     lda blt_en
        and #BLT_ENB
        bne nb
        ldx blt_wbm1
bf:     lda blt_bhold
        sta blt_bbuf,x
        dex
        lda blt_bhold+1
        sta blt_bbuf,x
        dex
        bpl bf
nb:     lda blt_en
        and #BLT_ENA
        bne na
        lda blt_ash
        bne na
        jsr blt_afill           ; static A: ADAT, masked once
        jsr blt_amask
na:     rts
psh:    ldy blt_dsc             ; shift for blt_prep: descending 16 - sh
        beq :+
        eor #$FF
        sec
        adc #16
:       rts
neg:    eor #$FF                ; 0/1 -> 0/$FF
        clc
        adc #1
        rts
.endproc

; ---------------------------------------------------------------------------
; blt_afill — A row := BLTADAT (A DMA off).
; ---------------------------------------------------------------------------
.proc blt_afill
        ldx blt_wbm1
lp:     lda chip_shd+BLT_ADAT+1
        sta blt_abuf,x
        dex
        lda chip_shd+BLT_ADAT
        sta blt_abuf,x
        dex
        bpl lp
        rts
.endproc

; ---------------------------------------------------------------------------
; blt_amask — A without a shift: the raw last word into the A record, then
; AFWM on the first word, ALWM on the last (both when w = 1).
; ---------------------------------------------------------------------------
.proc blt_amask
        ldx blt_lpos
        lda blt_abuf,x
        sta blt_pr+8
        lda blt_abuf+1,x
        sta blt_pr+9
        ldx blt_fpos
        lda blt_abuf,x
        and chip_shd+BLT_AFWM
        sta blt_abuf,x
        lda blt_abuf+1,x
        and chip_shd+BLT_AFWM+1
        sta blt_abuf+1,x
        ldx blt_lpos
        lda blt_abuf,x
        and chip_shd+BLT_AFWM+2
        sta blt_abuf,x
        lda blt_abuf+1,x
        and chip_shd+BLT_AFWM+3
        sta blt_abuf+1,x
        rts
.endproc

; ---------------------------------------------------------------------------
; blt_row — one row: fetch, A/B shift and mask, minterm, fill, store.
; ---------------------------------------------------------------------------
.proc blt_row
        lda blt_en
        and #BLT_ENA
        beq aoff
        ldx #BLT_JA
        jsr blt_fetch
        jmp aprep
aoff:   lda blt_ash
        beq bch                 ; static A (setup)
        jsr blt_afill
aprep:  lda blt_ash
        bne afull
        jsr blt_amask
        jmp bch
afull:  ldy #0
        jsr blt_prepc
bch:    lda blt_en
        and #BLT_ENB
        beq cch
        ldx #BLT_JB
        jsr blt_fetch
        lda blt_bsh
        beq cch
        ldy #BLT_PRSZ
        jsr blt_prepc
cch:    lda blt_en
        and #BLT_ENC
        beq kern
        ldx #BLT_JC
        jsr blt_fetch
kern:   lda blt_lf
        cmp #BLT_LF_CA
        bne :+
        jsr blt_kca
        jmp fil
:       cmp #BLT_LF_A
        bne :+
        jsr blt_ka
        jmp fil
:       cmp #0
        bne gen
        jsr blt_k0
        jmp fil
gen:    jsr blt_kgen
fil:    lda blt_c1
        and #BLT_FILLM
        beq dch
        jsr blt_fill
dch:    lda blt_en
        and #BLT_END
        beq done
        jsr blt_dpage
        ldx #BLT_JD
        lda #REU_CMD_STASH
        jmp blt_xfer            ; and advance
done:   rts
.endproc

; blt_fetch — X = channel: its row into its buffer, then advance.
.proc blt_fetch
        lda #REU_CMD_FETCH
        jmp blt_xfer
.endproc

; ---------------------------------------------------------------------------
; blt_prepc — blt_prep on the record at blt_pr+Y (0 = A, BLT_PRSZ = B):
; copy in, run, copy back (old word, raw last).
; ---------------------------------------------------------------------------
.proc blt_prepc
        sty blt_t
        ldx #0
ci:     lda blt_pr,y
        sta blt_px,x
        iny
        inx
        cpx #BLT_PRSZ
        bne ci
        jsr blt_prep
        ldy blt_t
        ldx #0
co:     lda blt_px,x
        sta blt_pr,y
        iny
        inx
        cpx #BLT_PRSZ
        bne co
        rts
.endproc

; ---------------------------------------------------------------------------
; blt_prep — shift one A or B row in place (processing order): word k:
; a = raw & (k = 0 ? mf) & (k = w-1 ? ml); out = ascending (old : a) >> ps,
; descending (a : old) >> ps; old := a.
; ---------------------------------------------------------------------------
.proc blt_prep
        ldx blt_px
        ldy blt_wd
        lda #1
        sta blt_first
lp:     lda blt_abuf,x
        sta blt_rl1
        sta blt_a1
        lda blt_abuf+1,x
        sta blt_rl0
        sta blt_a0
        lda blt_first
        beq nf
        lda blt_a1
        and blt_mf1
        sta blt_a1
        lda blt_a0
        and blt_mf0
        sta blt_a0
        lda #0
        sta blt_first
nf:     cpy #1
        bne nl
        lda blt_a1
        and blt_ml1
        sta blt_a1
        lda blt_a0
        and blt_ml0
        sta blt_a0
nl:     lda blt_dsc
        bne d
        lda blt_o1
        sta blt_h1
        lda blt_o0
        sta blt_h0
        lda blt_a1
        sta blt_l1
        lda blt_a0
        sta blt_l0
        jmp s
d:      lda blt_a1
        sta blt_h1
        lda blt_a0
        sta blt_h0
        lda blt_o1
        sta blt_l1
        lda blt_o0
        sta blt_l0
s:      lda blt_ps
        sta blt_sh
        jsr blt_shr
        lda blt_q1
        sta blt_abuf,x
        lda blt_q0
        sta blt_abuf+1,x
        lda blt_a1
        sta blt_o1
        lda blt_a0
        sta blt_o0
        lda blt_dsc
        bne dd
        inx
        inx
        jmp nx
dd:     dex
        dex
nx:     dey
        beq done
        jmp lp
done:   rts
.endproc

; ---------------------------------------------------------------------------
; Minterm kernels over the row's bytes: D := f(A, B, C); blt_nz |= D.
; ---------------------------------------------------------------------------
; blt_kca — $CA (cookie-cut): D = C ^ ((B ^ C) & A).
.proc blt_kca
        ldx blt_wbm1
lp:     lda blt_cbuf,x
        eor blt_bbuf,x
        and blt_abuf,x
        eor blt_cbuf,x
        sta blt_dbuf,x
        ora blt_nz
        sta blt_nz
        dex
        bpl lp
        rts
.endproc

; blt_ka — $F0: D = A.
.proc blt_ka
        ldx blt_wbm1
lp:     lda blt_abuf,x
        sta blt_dbuf,x
        ora blt_nz
        sta blt_nz
        dex
        bpl lp
        rts
.endproc

; blt_k0 — $00: D = 0 (blt_nz unchanged).
.proc blt_k0
        ldx blt_wbm1
        lda #0
lp:     sta blt_dbuf,x
        dex
        bpl lp
        rts
.endproc

; blt_kgen — any LF: g(C) = (C & and) ^ eor per (A, B); f = mux by B, D =
; mux by A (mux(s, x, y) = y ^ ((x ^ y) & s)).
.proc blt_kgen
        ldx blt_wbm1
lp:     lda blt_cbuf,x          ; A = 1: f1 = B ? g11 : g10
        and blt_k+0
        eor blt_k+1
        sta blt_g
        lda blt_cbuf,x
        and blt_k+2
        eor blt_k+3
        sta blt_g+1
        eor blt_g
        and blt_bbuf,x
        eor blt_g+1
        sta blt_g+2
        lda blt_cbuf,x          ; A = 0: f0 = B ? g01 : g00
        and blt_k+4
        eor blt_k+5
        sta blt_g
        lda blt_cbuf,x
        and blt_k+6
        eor blt_k+7
        sta blt_g+1
        eor blt_g
        and blt_bbuf,x
        eor blt_g+1
        sta blt_g+1             ; f0
        eor blt_g+2
        and blt_abuf,x
        eor blt_g+1
        sta blt_dbuf,x
        ora blt_nz
        sta blt_nz
        dex
        bmi done
        jmp lp
done:   rts
.endproc

; ---------------------------------------------------------------------------
; blt_fill — fill the D row in processing order (word by word, bits 0-15),
; the carry starting at FCI.  Inclusive (IFE alone): out = fc | s, fc ^= s;
; exclusive: fc ^= s, out = fc.  Ascending byte order 1,0,3,2..., descending
; wb-1 down to 0.  blt_nzf |= the filled bytes.
; ---------------------------------------------------------------------------
.proc blt_fill
        lda blt_c1
        lsr
        lsr
        and #1
        sta blt_fc
        ldx #0                  ; 0 = inclusive (IFE alone)
        lda blt_c1
        and #BLT_FILLM
        cmp #BLT_IFE
        beq :+
        inx                     ; EFE alone or both: exclusive
:       stx blt_efe
        lda #0
        sta blt_fj
by:     lda blt_dsc
        beq up
        lda blt_wbm1
        sec
        sbc blt_fj
        jmp sel
up:     lda blt_fj
        eor #1
sel:    tax
        lda blt_dbuf,x
        sta blt_fb
        lda #8
        sta blt_cnt
bi:     lsr blt_fb              ; C = s
        lda blt_efe             ; (keeps C)
        bne efe
        lda blt_fc              ; inclusive
        bcc i0
        eor #1                  ; s = 1: out 1, fc ^= 1 (C still 1)
        sta blt_fc
        bcs o
i0:     lsr                     ; s = 0: out = fc
        jmp o
efe:    lda blt_fc              ; exclusive
        bcc e0
        eor #1
        sta blt_fc
e0:     lsr                     ; out = fc
o:      ror blt_fr
        dec blt_cnt
        bne bi
        lda blt_fr
        sta blt_dbuf,x
        ora blt_nzf
        sta blt_nzf
        inc blt_fj
        lda blt_fj
        cmp blt_wb
        bne by
        rts
.endproc

; ---------------------------------------------------------------------------
; blt_dpage — blt_smc := 1 if the D row touches the guest page of r_pc.
; ---------------------------------------------------------------------------
.proc blt_dpage
        lda blt_r2+BLT_JD
        and #BLT_BANKS
        cmp r_pc+2
        bne e
        lda blt_r1+BLT_JD
        cmp r_pc+1
        beq hit
e:      lda blt_r0+BLT_JD
        clc
        adc blt_wbm1
        lda blt_r1+BLT_JD
        adc #0
        sta blt_t
        lda blt_r2+BLT_JD
        adc #0
        and #BLT_BANKS
        cmp r_pc+2
        bne no
        lda blt_t
        cmp r_pc+1
        bne no
hit:    lda #1
        sta blt_smc
no:     rts
.endproc

; ---------------------------------------------------------------------------
; blt_xfer — A = REC command, X = channel: move its row (blt_wb bytes) at
; its row start, split at a 512 KB REC line; then row start += step.
; The bank is masked to the 2 MB chip range right before every store.
; ---------------------------------------------------------------------------
.proc blt_xfer
        sta blt_cmd
        lda blt_bfl,x
        sta blt_xc
        lda blt_bfh,x
        sta blt_xc+1
        lda blt_r0,x
        sta blt_xa
        lda blt_r1,x
        sta blt_xa+1
        lda blt_r2,x            ; (masked at the REC store below)
        sta blt_xa+2
        lda blt_wb
        sta blt_xn
        lda blt_xa+2            ; a split needs bits 18-16 = 7 ...
        and #7
        cmp #7
        bne one
        lda blt_xa              ; ... and bits 15-0 + n > $10000
        clc
        adc blt_xn
        lda blt_xa+1
        adc #0
        bcc one
        lda #0
        sec
        sbc blt_xa
        sta blt_xn1             ; bytes to the line (<= $80)
        cmp blt_xn
        bcs one
        jsr go
        lda blt_xc
        clc
        adc blt_xn1
        sta blt_xc
        bcc :+
        inc blt_xc+1
:       lda #0
        sta blt_xa
        sta blt_xa+1
        inc blt_xa+2            ; the next 512 KB line (mod 2 MB below)
        lda blt_xn
        sec
        sbc blt_xn1
        jsr go
        jmp adv
one:    lda blt_xn
        jsr go
adv:    lda blt_r0,x            ; row start += step
        clc
        adc blt_s0,x
        sta blt_r0,x
        lda blt_r1,x
        adc blt_s1,x
        sta blt_r1,x
        lda blt_r2,x
        adc blt_s2,x
        sta blt_r2,x
        rts
go:     sta REC_LENLO
        lda #0
        sta REC_LENHI
        sta REC_ACTRL
        lda blt_xc
        sta REC_C64LO
        lda blt_xc+1
        sta REC_C64HI
        lda blt_xa
        sta REC_REULO
        lda blt_xa+1
        sta REC_REUHI
        lda blt_xa+2
        and #BLT_BANKS          ; never $BF/$DF
        sta REC_REUBANK
        lda blt_cmd
        sta REC_COMMAND         ; DMA executes here
        rts
.endproc

; ---------------------------------------------------------------------------
; blt_done — pointers and data registers back, BZERO, the pixel epoch.
; ---------------------------------------------------------------------------
.proc blt_done
        ldx #3
wb:     lda blt_dsc
        beq :+
        lda blt_r0,x            ; descending: pointer = row start + adj
        clc
        adc blt_adj
        sta blt_r0,x
        bcc :+
        inc blt_r1,x
        bne :+
        inc blt_r2,x
:       txa
        asl
        asl
        tay
        lda #0
        sta chip_shd+BLT_CPT,y
        lda blt_r2,x
        and #BLT_BANKS
        sta chip_shd+BLT_CPT+1,y
        lda blt_r1,x
        sta chip_shd+BLT_CPT+2,y
        lda blt_r0,x
        sta chip_shd+BLT_CPT+3,y
        dex
        bpl wb
        ; the data registers hold the last word their channel fetched
        lda blt_en
        and #BLT_ENA
        beq na
        lda blt_pr+8
        sta chip_shd+BLT_ADAT
        lda blt_pr+9
        sta chip_shd+BLT_ADAT+1
na:     ldx blt_lpos
        lda blt_en
        and #BLT_ENC
        beq nc
        lda blt_cbuf,x
        sta chip_shd+BLT_CDAT
        lda blt_cbuf+1,x
        sta chip_shd+BLT_CDAT+1
nc:     lda blt_en
        and #BLT_ENB
        beq nb
        lda blt_bbuf,x          ; hold := the last shifted B
        sta blt_bhold+1
        lda blt_bbuf+1,x
        sta blt_bhold
        lda blt_bsh             ; raw: as fetched (BSH 0) or prep's
        bne :+
        lda blt_bbuf,x
        sta blt_pr+BLT_PRSZ+8
        lda blt_bbuf+1,x
        sta blt_pr+BLT_PRSZ+9
:       lda blt_pr+BLT_PRSZ+8
        sta chip_shd+BLT_BDAT
        lda blt_pr+BLT_PRSZ+9
        sta chip_shd+BLT_BDAT+1
nb:     lda blt_c1              ; BZERO: of the filled words when filling
        and #BLT_FILLM
        beq :+
        lda blt_nzf
        jmp tz
:       lda blt_nz
tz:     beq zero
        lda #0
        beq st
zero:   lda #BLT_BZERO
st:     sta blt_bz
        lda blt_en
        and #BLT_END
        beq nd
        inc disp_st+BLT_EPOCH   ; C3 (Addendum 3): RMW of the epoch, BASIC out
nd:     rts
.endproc

