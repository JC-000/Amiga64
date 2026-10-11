; ===========================================================================
; runtime/ops/chip.s — S3.1 chip-register stub behind the $BF/$DF bank trap
; Owner: W7-T (chip).  Spec: docs/S3_PLAN.md §1.2-1.3, §3.4, §4 "S3.1";
; contract: docs/CONTRACTS.md "I/O trap ($BF/$DF)".
;
; The memory API (runtime/core.s) never lets a guest access whose bank is
; $DF (custom chips) or $BF (CIAs) reach the REU: the bank-changed branch
; of MIO_PROG and the cold path jmp here instead (m_rbk is never loaded
; with $BF/$DF, so such an access can never take the same-bank fast path),
; and mio_split hands each I/O byte of a straddling access to chip_split.
; No REU DMA is issued for a trapped byte.
;
; ABI = the memory API's: m_addr in, m_val LE in (writes) / out (reads;
; only the bytes the access size covers are written, as in core.s), X and
; Y preserved, rts to the API's caller.  Clobbers A.
;
; Decode (docs/S3_PLAN.md §1.3):
;   custom  $DFF000-$DFF1FF, register = m_addr & $1FE.  Word access =
;           one register; long = two (reg, then addr+2); a byte write lands
;           on both halves (OCS bus); a byte read is the addressed half.
;           Rest of bank $DF: open bus (reads 0, writes ignored).
;   CIA     CIA-A = odd bytes with A12 = 0, CIA-B = even bytes with
;           A13 = 0, register = (m_addr+1) & $0F.  Every CIA access is
;           byte-wise (a word/long is 2/4 byte accesses).  Other bytes of
;           bank $BF: open bus (reads 0, writes ignored).
; Reads (§3.4): VPOSR $8000, POTINP $FF00, SERDATR $3000, DENISEID $FFFF;
;   other read-only slots ($000-$01E) return their shadow word (0 unless
;   a later chip model or the host puts a value there); write-only
;   registers ($020-$1FE) return 0.
; S3.2 (W8-L, once mach_init ran, i.e. mach_on != 0): VPOSR = $8000 | V8
;   and VHPOSR = V7-V0 : H from the machine loop's time base (mach_beam,
;   docs/CONTRACTS.md "Machine loop (S3.2)"); a long read keeps the beam
;   of its first word, so move.l VPOSR is one coherent position.  An
;   INTREQ write sets or clears INTREQR bit 5 (VERTB, SET/CLR by bit 15).
; S3.3 (W9-I): every INTENA/INTREQ write applies the SET/CLR rule (bit 15
;   set: OR in bits 0-14, else clear them) to INTENAR/INTREQR (chip_shd
;   $01C/$01E, bit 15 reads 0), mach_on or not; after an INTREQ write the
;   bits a level source holds (mach_hold, mach.s) are ORed back; a SET
;   write then calls mach_ichk (a pass at the next instruction boundary if
;   an interrupt became deliverable, S3.3 delivery).  The raw
;   written word still lands in the write-only slot.
; CIA registers (W9-C, S3.4): every decoded CIA byte goes to
;   runtime/ops/cia.s (cia_read/cia_write: timers, ICR, TOD, ports as
;   host/chipset_ref.CIA; docs/CONTRACTS.md "CIA-A/B (S3.4)").
; Writes: every decoded write is appended to chip_log; write-only
;   registers also land in chip_shd (a write to a read-only slot is
;   ignored there, as on the chip), CIA writes go to cia.s, and a
;   SERDAT write appends its low byte to the chip_ser ring.
;
; Host-visible state (labels in build/runner.lbl; all LE; cleared by the
; boot stub's BSS clear and by chip_reset; emu_reset does NOT touch it --
; it is machine state, like guest RAM):
;   chip_shd   512 B  custom register image, big-endian words, index =
;                     register offset ($DFF000 + i)
;   chip_log   256 B  ring of 64 entries {u16 id, u16 value}; id = custom
;                     offset $000-$1FE, CIA-A $0A00+reg, CIA-B $0B00+reg;
;                     entry k lives at 4 * (k mod 64)
;   chip_logn  u16    entries ever logged (mod 65536)
;   chip_ser   1 KiB  SERDAT ring (CHIP_SERSZ = 1024 bytes, exported):
;                     byte k at k mod 1024, i.e. slot = chip_sern & $3FF
;                     at the time of the store; 1024 divides 65536, so the
;                     slot stays continuous across the u16 count's wrap.
;                     The byte is stored first, then the count bumped.
;                     MAIN BSS on purpose: the host reads it over REST/
;                     the monitor, and RAM under BASIC (BBSS) is not
;                     host-readable on the C64 Ultimate.
;   chip_sern  u16    SERDAT bytes ever written (mod 65536)
;   chip_nopen u16    open-bus byte/word accesses (reads and writes)
; ===========================================================================

.include "../macros.inc"

.export chip_r8, chip_r16, chip_r32, chip_w8, chip_w16, chip_w32
.export chip_split, chip_reset
.export chip_shd, chip_log, chip_logn, chip_ser, chip_sern
.export chip_nopen
.export CHIP_SERSZ              ; host: the chip_ser ring size (tests/aros_boot.py, run_chip.py)
.import m_scmd                  ; core.s mio_split: REC command of the split
.import mach_on, mach_beam, mach_bv, mach_bh    ; W8-L mach.s: S3.2 beam
.import cia_read, cia_write, cia_reset          ; W9-C cia.s: S3.4 CIA-A/B
.import mach_hold, mach_ichk                    ; W9-I irq: level hold, delivery kick
.import cop_w0, cop_w1                          ; W10-P cop: S3.5 display/copper registers
.export c_wrw, chip_a, chip_vl, chip_vh         ; W10-P cop: the copper's MOVE path
.import blt_w, blt_dmaconr                      ; W11-B blt: S3.6 blitter
.import inp_pot                                 ; W13-I inp: the POTINP read (right button)
.export chip_blitirq                            ; W11-B blt: INTREQ BLIT on completion

CHIP_LOGN_MASK = 63     ; @value log ring entries - 1
CHIP_SERDAT    = $30    ; @value SERDAT register offset
CHIP_SERSZ     = 1024   ; @value chip_ser ring bytes (4 pages; slot = count & $3FF)
CHIP_RO_END    = $20    ; @value read-only slots are $000-$01E
CHIP_DENISEID  = $7C    ; @value DENISEID (read-only, OCS: floats $FFFF)
CHIP_ID_CIAA   = $0A    ; @value log id high byte, CIA-A
CHIP_ID_CIAB   = $0B    ; @value log id high byte, CIA-B
CHIP_INTREQR   = $1E    ; @value INTREQR (read-only) register offset
CHIP_INTREQ    = $9C    ; @value INTREQ (write) register offset
CHIP_INTENAR   = $1C    ; @value INTENAR (read-only) register offset; W9-I irq
CHIP_INTENA    = $9A    ; @value INTENA (write) register offset; W9-I irq
CHIP_BLT0      = $40    ; @value first blitter register (BLTCON0); W11-B blt
CHIP_BLT1      = $76    ; @value past the last (BLTADAT $074); W11-B blt
CHIP_DMACONR   = $02    ; @value DMACONR (read-only) register offset; W11-B blt
CHIP_BLITREQ   = $40    ; @value INTREQ low byte: BLIT (bit 6); W11-B blt

.segment "BSS"
chip_shd:   .res 512
chip_log:   .res 256
chip_ser:   .res CHIP_SERSZ
chip_logn:  .res 2
chip_sern:  .res 2
chip_nopen: .res 2
chip_a:     .res 3              ; address cursor (lo, hi, bank)
chip_vl:    .res 1              ; word value, low byte
chip_vh:    .res 1              ; word value, high byte
chip_li:    .res 1              ; log id, low byte
chip_lh:    .res 1              ; log id, high byte
chip_n:     .res 1              ; byte counter (long CIA accesses)
chip_sx:    .res 1              ; caller's X
chip_bk:    .res 1              ; beam latch: $80 armed (long read), 1 held

.segment "CODE"

; ---------------------------------------------------------------------------
; Memory-API entries (jmp'd from mem_read*/mem_write* in core.s)
; ---------------------------------------------------------------------------
.proc chip_r8
        stx chip_sx
        jsr c_ldadr
        jsr c_rdb
        sta m_val
        ldx chip_sx
        rts
.endproc

.proc chip_r16
        stx chip_sx
        jsr c_ldadr
        cmp #$DF
        bne cia
        jsr c_rdw
        lda chip_vl
        sta m_val
        lda chip_vh
        sta m_val+1
        ldx chip_sx
        rts
cia:    jsr c_rdb
        sta m_val+1
        jsr c_inc
        jsr c_rdb
        sta m_val
        ldx chip_sx
        rts
.endproc

.proc chip_r32
        stx chip_sx
        jsr c_ldadr
        cmp #$DF
        bne cia
        lda #$80                ; W8-L: one beam for both words
        sta chip_bk
        jsr c_rdw
        lda chip_vh
        sta m_val+3
        lda chip_vl
        sta m_val+2
        jsr c_inc2
        jsr c_rdw
        lda #0
        sta chip_bk
        lda chip_vh
        sta m_val+1
        lda chip_vl
        sta m_val
        ldx chip_sx
        rts
cia:    lda #3                  ; guest byte 0 -> m_val+3 ... byte 3 -> m_val
        sta chip_n
lp:     jsr c_rdb
        ldx chip_n
        sta m_val,x
        jsr c_inc
        dec chip_n
        bpl lp
        ldx chip_sx
        rts
.endproc

.proc chip_w8
        stx chip_sx
        jsr c_ldadr
        lda m_val
        jsr c_wrb
        ldx chip_sx
        rts
.endproc

.proc chip_w16
        stx chip_sx
        jsr c_ldadr
        cmp #$DF
        bne cia
        lda m_val
        sta chip_vl
        lda m_val+1
        sta chip_vh
        jsr c_wrw
        ldx chip_sx
        rts
cia:    lda m_val+1
        jsr c_wrb
        jsr c_inc
        lda m_val
        jsr c_wrb
        ldx chip_sx
        rts
.endproc

.proc chip_w32
        stx chip_sx
        jsr c_ldadr
        cmp #$DF
        bne cia
        lda m_val+2
        sta chip_vl
        lda m_val+3
        sta chip_vh
        jsr c_wrw
        jsr c_inc2
        lda m_val
        sta chip_vl
        lda m_val+1
        sta chip_vh
        jsr c_wrw
        ldx chip_sx
        rts
cia:    lda #3
        sta chip_n
lp:     ldx chip_n
        lda m_val,x
        jsr c_wrb
        jsr c_inc
        dec chip_n
        bpl lp
        ldx chip_sx
        rts
.endproc

; ---------------------------------------------------------------------------
; chip_split — one I/O byte of a straddling access (called by mio_split).
; In: m_scmd = mio_split's REC command (REU_CMD_FETCH = read), Y = byte
; index i; the byte is guest m_addr + i (24-bit sum), its data t_tmp+i
; (guest byte order, so mem_read16/32's copy to m_val stays correct).
; Preserves Y; clobbers A and X (mio_split saves X for its caller).
; ---------------------------------------------------------------------------
.proc chip_split
        tya
        clc
        adc m_addr
        sta chip_a
        lda m_addr+1
        adc #0
        sta chip_a+1
        lda m_addr+2
        adc #0
        sta chip_a+2
        lda m_scmd
        cmp #REU_CMD_FETCH
        bne wr
        jsr c_rdb
        sta t_tmp,y
        rts
wr:     lda t_tmp,y
        jmp c_wrb
.endproc

; ---------------------------------------------------------------------------
; chip_reset — clear the shadow, CIA registers, log, ring and counters.
; Host/test entry (not called by the interpreter).  Clobbers A, X.
; ---------------------------------------------------------------------------
.proc chip_reset
        lda #0
        tax
lp:     sta chip_shd,x
        sta chip_shd+256,x
        sta chip_log,x
        sta chip_ser,x
        sta chip_ser+256,x
        sta chip_ser+512,x
        sta chip_ser+768,x
        inx
        bne lp
        ldx #6 - 1              ; chip_logn .. chip_nopen are contiguous
lp2:    sta chip_logn,x
        dex
        bpl lp2
        jmp cia_reset           ; W9-C: the CIAs to power-up
.endproc
.assert chip_nopen = chip_logn + 4, error, "chip_reset: BSS layout"
.assert chip_logn = chip_ser + CHIP_SERSZ, error, "chip_ser: ring size"
.assert CHIP_SERSZ = 1024, error, "chip_reset/ser: written for a 4-page ring"

; ---------------------------------------------------------------------------
; Internals.  Address in chip_a; A/X clobbered, Y untouched.
; ---------------------------------------------------------------------------

; c_ldadr — chip_a := m_addr (24 bits); returns A = bank.
.proc c_ldadr
        lda m_addr
        sta chip_a
        lda m_addr+1
        sta chip_a+1
        lda m_addr+2
        sta chip_a+2
        rts
.endproc

; c_inc / c_inc2 — chip_a += 1 / += 2 (16-bit: a trapped access never
; leaves its bank, straddles at $xxFFFD+ go through mio_split).
.proc c_inc
        inc chip_a
        bne done
        inc chip_a+1
done:   rts
.endproc

.proc c_inc2
        lda chip_a
        clc
        adc #2
        sta chip_a
        bcc done
        inc chip_a+1
done:   rts
.endproc

; c_open — count an open-bus access; returns A = 0 (the read value).
.proc c_open
        inc chip_nopen
        bne done
        inc chip_nopen+1
done:   lda #0
        rts
.endproc

; c_log — append {chip_lh:chip_li, chip_vh:chip_vl} to the log ring.
.proc c_log
        lda chip_logn
        and #CHIP_LOGN_MASK
        asl
        asl
        tax
        lda chip_li
        sta chip_log,x
        lda chip_lh
        sta chip_log+1,x
        lda chip_vl
        sta chip_log+2,x
        lda chip_vh
        sta chip_log+3,x
        inc chip_logn
        bne done
        inc chip_logn+1
done:   rts
.endproc

; c_rdb — A := the byte at chip_a (bank $DF or $BF).
.proc c_rdb
        lda chip_a+2
        cmp #$DF
        beq custom
        jmp c_ciard
custom: jsr c_rdw               ; the register word holding this byte
        lda chip_a
        lsr                     ; C = A0: odd = low half
        lda chip_vh
        bcc done
        lda chip_vl
done:   rts
.endproc

; c_wrb — write byte A at chip_a.  Custom: both halves of the register.
.proc c_wrb
        tax
        lda chip_a+2
        cmp #$DF
        bne cia
        stx chip_vl
        stx chip_vh
        jmp c_wrw
cia:    txa
        jmp c_ciawr
.endproc

; c_rdw — chip_vh:chip_vl := the custom register word at chip_a & $1FE.
.proc c_rdw
        lda #0
        sta chip_vl
        sta chip_vh
        lda chip_a+1
        cmp #$F1
        beq done                ; $100-$1FE: all write-only -> 0
        cmp #$F0
        beq p0
        jmp c_open
p0:     lda chip_a
        and #$FE
        cmp #CHIP_DMACONR       ; W11-B blt: (DMACON & $07FF) | BZERO
        beq dmacr               ; W11-B blt
        cmp #$04                ; VPOSR: LOF, Agnus ID 0 (OCS PAL)
        beq vposr
        cmp #$06                ; VHPOSR (W8-L: live after mach_init)
        beq vhposr
        cmp #$16                ; POTINP: right button (W13-I: inp_pot)
        beq potinp
        cmp #$18                ; SERDATR: TBE | TSRE
        beq serdatr
        cmp #CHIP_DENISEID      ; DENISEID: floats high on OCS
        beq denise
        cmp #CHIP_RO_END
        bcs done                ; write-only -> 0
shadow: tax                     ; other read-only slot: its shadow
        lda chip_shd,x
        sta chip_vh
        lda chip_shd+1,x
        sta chip_vl
done:   rts
vposr:  lda #$80
        sta chip_vh
        jsr c_beam
        bcs done
        lda mach_bv+1           ; V8 in bit 0
        sta chip_vl
        rts
vhposr: jsr c_beam
        lda #$06
        bcs shadow              ; before mach_init: the S3.1 shadow word
        lda mach_bv
        sta chip_vh
        lda mach_bh
        sta chip_vl
        rts
potinp: jmp inp_pot               ; W13-I: chip_vh := $FB (C= held) or $FF
        .res 3, $EA             ; W13-I: chip.s keeps its size (no module moves)
serdatr:
        lda #$30
        sta chip_vh
        rts
denise: lda #$FF
        sta chip_vh
        sta chip_vl
        rts
dmacr:  jmp blt_dmaconr         ; W11-B blt
.endproc

; c_beam — W8-L: the S3.2 beam into mach_bv/mach_bh; C = 1 before
; mach_init (no time base: the caller keeps the S3.1 value).  Inside a long
; read (chip_bk armed by chip_r32) the first beam word latches the beam and
; the second reuses it.  Clobbers A and X.
.proc c_beam
        lda mach_on
        bne on
        sec
        rts
on:     lda chip_bk
        cmp #1
        beq have                ; this long's first word latched it
        jsr mach_beam
        lda chip_bk
        beq have
        lda #1
        sta chip_bk
have:   clc
        rts
.endproc

; c_wrw — write chip_vh:chip_vl to the custom register at chip_a & $1FE.
.proc c_wrw
        lda chip_a+1
        cmp #$F0
        beq p0
        cmp #$F1
        beq p1
        jmp c_open
p1:     lda #1                  ; $100-$1FE: all write-only
        sta chip_lh
        lda chip_a
        and #$FE
        sta chip_li
        jsr c_log
        ldx chip_li
        lda chip_vh
        sta chip_shd+256,x
        lda chip_vl
        sta chip_shd+257,x
        jmp cop_w1              ; W10-P cop: BPLCON/BPLxMOD/COLOR
p0:     lda #0
        sta chip_lh
        lda chip_a
        and #$FE
        sta chip_li
        jsr c_log
        ldx chip_li
        cpx #CHIP_RO_END
        bcc done                ; read-only slot: logged only
        cpx #CHIP_DENISEID
        beq done
        lda chip_vh
        sta chip_shd,x
        lda chip_vl
        sta chip_shd+1,x
        cpx #CHIP_SERDAT
        beq ser
        ; --- W9-I irq: INTENA/INTREQ SET/CLR (bit 15) into INTENAR/INTREQR
        cpx #CHIP_INTENA
        beq paula
        cpx #CHIP_INTREQ
        bne cop                 ; W10-P cop
paula:  txa
        sec
        sbc #CHIP_INTENA - CHIP_INTENAR ; $09A -> $01C, $09C -> $01E
        tax
        bit chip_vh
        bmi set
        lda chip_vl             ; CLR: reg &= ~val
        eor #$FF
        and chip_shd+1,x
        sta chip_shd+1,x
        lda chip_vh
        eor #$FF
        and chip_shd,x
        jmp hi
set:    lda chip_vl             ; SET: reg |= val
        ora chip_shd+1,x
        sta chip_shd+1,x
        lda chip_vh
        ora chip_shd,x
hi:     and #$7F                ; bit 15 always reads 0
        sta chip_shd,x
        cpx #CHIP_INTREQR
        bne kick
        lda mach_hold           ; a level source still asserting re-raises
        ora chip_shd+CHIP_INTREQR+1
        sta chip_shd+CHIP_INTREQR+1
        lda mach_hold+1
        and #$7F
        ora chip_shd+CHIP_INTREQR
        sta chip_shd+CHIP_INTREQR
kick:   bit chip_vh             ; a SET may make an interrupt deliverable:
        bpl done                ; act at the next instruction boundary
        jmp mach_ichk
        ; --- end W9-I irq
done:   rts
cop:    cpx #CHIP_BLT0          ; W11-B blt: $040-$074 to the blitter
        bcc cop0                ; W11-B blt
        cpx #CHIP_BLT1          ; W11-B blt
        bcs cop0                ; W11-B blt
        jmp blt_w               ; W11-B blt: BLTSIZE blits, BLTBDAT shifts
cop0:   jmp cop_w0              ; W10-P cop: COPxLC/COPJMP/DIW/DDF/DMACON/BPLxPT
        ; --- SERDAT, out of line (keeps the branches above in reach of done)
ser:    ldx chip_sern           ; SERDAT: chip_vl -> chip_ser[count & $3FF]:
        lda chip_sern+1         ; X = slot bits 7-0, the page = count bits 9-8
        lsr                     ; C = bit 8
        bcs s13
        lsr                     ; C = bit 9
        lda chip_vl             ; the byte (lda keeps C)
        bcs s2
        sta chip_ser,x          ; page 0
        bcc sinc                ; always (C = 0)
s2:     sta chip_ser+512,x      ; page 2
        bcs sinc                ; always (C = 1)
s13:    lsr                     ; C = bit 9
        lda chip_vl
        bcs s3
        sta chip_ser+256,x      ; page 1
        bcc sinc                ; always
s3:     sta chip_ser+768,x      ; page 3
sinc:   inc chip_sern           ; then the count (u16; the store came first)
        bne done
        inc chip_sern+1
        rts
.endproc

; W11-B blt: chip_blitirq -- a finished blit raises INTREQ BLIT as the Paula
; SET write $8040 through c_wrw's SET path (mach_hold ORed back, then
; mach_ichk); not logged, the raw INTREQ slot untouched.  Clobbers A/X,
; chip_vl/chip_vh; keeps Y.
.proc chip_blitirq              ; W11-B blt
        lda #$80                ; W11-B blt: SET
        sta chip_vh             ; W11-B blt
        lda #CHIP_BLITREQ       ; W11-B blt
        sta chip_vl             ; W11-B blt
        ldx #CHIP_INTREQ        ; W11-B blt
        jmp c_wrw::paula        ; W11-B blt
.endproc                        ; W11-B blt

; c_cia — select the CIA register of the byte at chip_a.
; Out: C = 0, X = CIA register index (CIA-A reg / CIA-B 16+reg), A = log id
; high byte; C = 1: neither CIA decodes this byte (open bus).
.proc c_cia
        lda chip_a+1
        and #$0F
        tax
        lda chip_a
        lsr                     ; C = A0
        lda chip_a+1
        bcc even
        and #$10                ; odd byte: CIA-A when A12 = 0
        bne none
        lda #CHIP_ID_CIAA
        clc
        rts
even:   and #$20                ; even byte: CIA-B when A13 = 0
        bne none
        txa
        ora #$10
        tax
        lda #CHIP_ID_CIAB
        clc
        rts
none:   sec
        rts
.endproc

; c_ciard — A := the CIA byte at chip_a (W9-C: runtime/ops/cia.s).
.proc c_ciard
        jsr c_cia
        bcc sel
        jmp c_open
sel:    jmp cia_read            ; X = register index; keeps Y
.endproc

; c_ciawr — write byte A to the CIA register at chip_a.
.proc c_ciawr
        sta chip_vl
        jsr c_cia
        bcc sel
        jmp c_open
sel:    sta chip_lh
        txa
        and #$0F
        sta chip_li
        lda #0
        sta chip_vh
        lda chip_vl
        jsr cia_write           ; W9-C: X = register index, A = value; keeps Y
        jmp c_log
.endproc
