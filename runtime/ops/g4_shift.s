; ===========================================================================
; runtime/ops/g4_shift.s — fam_shift: ASL/ASR/LSL/LSR/ROL/ROR/ROXL/ROXR
; Owner: G4.  Dispatch range $E0-$EF (docs/CONTRACTS.md).
;
; Encodings:
;   Register form   1110 ccc d ss i tt rrr
;     ccc = immediate count 1-8 (000 -> 8), or count register Dm when i=1
;           (count = Dm mod 64 — up to 63 real shifts of the sized operand)
;     d   = direction (0 right, 1 left), ss = size (00/01/10)
;     tt  = type (00 arithmetic, 01 logical, 10 rotate-through-X, 11 rotate)
;     rrr = destination Dn
;   Memory form     1110 0tt d 11 eeeeee
;     word only, count exactly 1, memory-alterable EA.  Type values 100-111
;     in bits 11-9 are 68020+ bitfield encodings -> op_illegal on the 68000.
;
; Flags are hand-rolled here (the ALU primitives don't model shifts).  The
; engine performs COUNT literal single-bit shifts, which IS the hardware
; flag definition, so all edge cases (count >= operand width, count mod
; width == 0 rotates, 17/33-bit ROX rings) fall out for free:
;   N/Z  from the sized result, always — including count 0.
;   ASL/ASR/LSL/LSR: C = last bit shifted out, X = C.
;                    count 0 -> C=0, V=0, X unchanged.
;   ASL V: set if the MSB changed at ANY point during the shift (tracked
;          per iteration); ASR/LSL/LSR: V=0.
;   ROL/ROR: C = last bit rotated; count 0 -> C=0.  X NEVER touched.  V=0.
;   ROXL/ROXR: rotate through X; C = X after the operation
;              (count 0 -> C = current X, X unchanged).  V=0.
;
; ZP usage: only our operand scratch t_op1/t_op2/t_res (fields extracted
; from op_lo/op_hi BEFORE any core call, per the clobber rule).
; ===========================================================================

.include "../macros.inc"

.export fam_shift
.import ea_calc, ea_load, ea_store, dispatch_next, op_illegal

; --- working variables (persist across ea_calc/ea_load; core only clobbers
;     op_lo/op_hi, t_tmp, m_*) ----------------------------------------------
sh_count = t_op2+0      ; remaining shift count (0-63)
sh_idx   = t_op2+1      ; loop selector: type*2 + direction (0-7)
sh_type  = t_op2+2      ; 0=AS 1=LS 2=ROX 3=RO (for the count-0 path)
sh_size  = t_op2+3      ; 0=B 1=W 2=L
; t_op1 is reused as flag scratch AFTER its ea_load value is copied to t_res:
sh_top   = t_op1+0      ; index of top sized byte of t_res (0/1/3)
sh_cflag = t_op1+1      ; last bit shifted/rotated out (0/1)
sh_vacc  = t_op1+2      ; ASL sticky V: MSB changed at any point (0/1)
sh_xflag = t_op1+3      ; working X flag (0/1), seeded from CCR

.segment "CODE"

; ---------------------------------------------------------------------------
; fam_shift — family entry.  Opcode in op_lo/op_hi, PC past the word.
; ---------------------------------------------------------------------------
fam_shift:
        lda op_lo
        and #$C0
        cmp #$C0
        beq sh_mem              ; size field 11 -> memory form

        ; ---- register form: extract every field before any core call ----
        lda op_lo
        lsr a
        lsr a
        lsr a
        tay                     ; Y = op_lo>>3 (type in bits 1-0)
        and #$03
        sta sh_type
        asl a
        sta sh_idx              ; type*2 ...
        lda op_hi
        and #$01
        ora sh_idx
        sta sh_idx              ; ... + direction
        tya
        lsr a
        lsr a
        lsr a
        sta sh_size             ; op_lo>>6 = 0/1/2

        lda op_lo
        and #$20
        beq @immc
        lda op_hi               ; register count: Dm mod 64
        lsr a
        and #$07
        asl a
        asl a
        tax
        lda r_d0,x              ; low byte of Dm
        and #$3F
        sta sh_count
        bpl @cdone              ; always (A <= $3F)
@immc:  lda op_hi               ; immediate count 1-8 (0 -> 8)
        lsr a
        and #$07
        bne @got
        lda #8
@got:   sta sh_count
@cdone:
        lda op_lo
        and #$07
        tax                     ; X = Dn -> EA mode 000 = data reg direct
        lda sh_size
        jsr ea_calc
        lda sh_size
        jsr ea_load             ; sized bytes of Dn -> t_op1
        jmp sh_run

; ---- memory form: 1110 0tt d 11 eeeeee — word, count 1 --------------------
sh_mem:
        lda op_hi
        lsr a
        and #$07
        cmp #$04
        bcc @ok
        jmp op_illegal          ; 68020+ bitfield encodings
@ok:    sta sh_type
        asl a
        sta sh_idx
        lda op_hi
        and #$01
        ora sh_idx
        sta sh_idx
        lda #1
        sta sh_count            ; always exactly one shift
        sta sh_size             ; always word
        lda op_lo
        and #$3F
        tax
        lda #1
        jsr ea_calc
        lda #1
        jsr ea_load
        ; fall through

; ---------------------------------------------------------------------------
; sh_run — common engine.  In: sh_count/sh_idx/sh_type/sh_size set, operand
; in t_op1 (sized bytes).  Copies it to t_res, seeds flag scratch, runs the
; per-op loop, builds the CCR, stores the sized result via ea_store.
; ---------------------------------------------------------------------------
sh_run:
        lda t_op1               ; working value -> t_res (upper bytes of a
        sta t_res               ; B/W copy are junk; never examined/stored)
        lda t_op1+1
        sta t_res+1
        lda t_op1+2
        sta t_res+2
        lda t_op1+3
        sta t_res+3
        ldx sh_size
        lda sh_toptab,x
        sta sh_top
        lda #0
        sta sh_cflag
        sta sh_vacc
        ldx #0
        lda r_sr
        and #$10                ; seed working X from CCR
        beq @x0
        ldx #1
@x0:    stx sh_xflag
        lda sh_count
        beq sh_cnt0
        ldx sh_idx
        lda sh_jtab_lo,x
        sta sh_jv+1
        lda sh_jtab_hi,x
        sta sh_jv+2
sh_jv:  jmp $FFFF               ; self-modified -> one of the lp_* loops

sh_cnt0:                        ; count 0: N/Z still computed, V=0, X kept
        lda sh_type
        cmp #$02
        bne fin_rot             ; AS/LS/RO: C=0 (sh_cflag already 0)
        lda sh_xflag
        sta sh_cflag            ; ROX: C = current X
        ; fall through

; ---------------------------------------------------------------------------
; CCR finishers.  68k CCR: bit4 X, bit3 N, bit2 Z, bit1 V, bit0 C.
; ---------------------------------------------------------------------------
fin_rot:                        ; ROL/ROR + all count-0: C=sh_cflag, X kept
        jsr fin_common
        ldy sh_cflag
        beq @nc
        ora #$01
@nc:    sta sh_cflag            ; reuse as scratch: N|Z|C
        lda r_sr
        and #$10                ; preserve X
        ora sh_cflag
        sta r_sr
        jmp sh_store

fin_shift:                      ; ASL/ASR/LSL/LSR: C=X=last bit out, V=vacc
        jsr fin_common
        ldx sh_cflag
        beq @nc
        ora #$11                ; C and X
@nc:    ldx sh_vacc             ; nonzero only ever for ASL
        beq @nv
        ora #$02
@nv:    sta r_sr
        jmp sh_store

fin_rox:                        ; ROXL/ROXR count>0: C = X = last bit out
        jsr fin_common
        ldx sh_xflag
        beq @nc
        ora #$11
@nc:    sta r_sr
        jmp sh_store

fin_common:                     ; -> A = N|Z bits from sized t_res
        ldy #0
        ldx sh_top
        lda t_res,x
        bpl @nn
        ldy #$08                ; N
@nn:    lda t_res
        ldx sh_size
        beq @zz
        ora t_res+1
        cpx #$02
        bne @zz
        ora t_res+2
        ora t_res+3
@zz:    cmp #$00                ; re-test A (ldx/cpx clobbered Z)
        bne @nz
        tya
        ora #$04                ; Z
        tay
@nz:    tya
        rts

sh_store:
        lda sh_size
        jsr ea_store            ; sized bytes only (reg upper bytes kept)
        jmp dispatch_next

; ---------------------------------------------------------------------------
; Per-op single-bit loops.  Each iteration: set carry-in, shift the sized
; t_res one bit via sh1_left/sh1_right (carry-out = bit shifted out), then
; update the tracked flags.  Cost per iteration incl. helper: B/W/L about
; 38/45/58 cycles (ASL ~+19 for V tracking).  Worst case ROXL.L count 63
; ~ 3.9k cycles + ~450 setup/finish — over the 1500 note threshold, but only
; register-count cases with count > ~20 exceed it; immediate forms (count
; <= 8) stay in the 600-1100 cycle range.  See report for details.
; ---------------------------------------------------------------------------
lp_asl:
        clc                     ; cin = 0
        jsr sh1_left
        lda #0
        rol a                   ; A = bit out = OLD msb
        sta sh_cflag
        ldx sh_top
        lda t_res,x
        asl a                   ; C = NEW msb
        lda #0
        rol a
        eor sh_cflag            ; 1 if msb changed this step
        ora sh_vacc
        sta sh_vacc             ; sticky V
        dec sh_count
        bne lp_asl
        jmp fin_shift

lp_asr_pre:
        ldx sh_size             ; VECTOR-VERIFIED QUIRK: ASR with count >
        lda sh_widtab,x         ; operand width gives C = X = 0 (NOT the
        cmp sh_count            ; last bit out) — result is all sign bits.
        bcc asr_big             ; width < count
lp_asr:
        ldx sh_top
        lda t_res,x
        asl a                   ; C = sign bit -> cin (sign replication)
        jsr sh1_right
        lda #0
        rol a
        sta sh_cflag
        dec sh_count
        bne lp_asr
        jmp fin_shift
asr_big:
        ldx sh_top              ; sign-fill; sh_cflag/sh_vacc still 0, so
        lda t_res,x             ; fin_shift yields C=X=V=0 and N/Z from it
        bpl @pos
        lda #$FF
        bne @fill
@pos:   lda #$00
@fill:  sta t_res               ; writing all 4 is fine: only sized bytes
        sta t_res+1             ; are ever examined or stored
        sta t_res+2
        sta t_res+3
        jmp fin_shift

lp_lsl:
        clc
        jsr sh1_left
        lda #0
        rol a
        sta sh_cflag
        dec sh_count
        bne lp_lsl
        jmp fin_shift

lp_lsr:
        clc
        jsr sh1_right
        lda #0
        rol a
        sta sh_cflag
        dec sh_count
        bne lp_lsr
        jmp fin_shift

lp_rol:
        ldx sh_top
        lda t_res,x
        asl a                   ; C = msb (wraps into bit 0)
        jsr sh1_left
        lda #0
        rol a
        sta sh_cflag
        dec sh_count
        bne lp_rol
        jmp fin_rot

lp_ror:
        lda t_res
        lsr a                   ; C = bit 0 (wraps into msb)
        jsr sh1_right
        lda #0
        rol a
        sta sh_cflag
        dec sh_count
        bne lp_ror
        jmp fin_rot

lp_roxl:
        lda sh_xflag
        lsr a                   ; C = X (X enters at bit 0)
        jsr sh1_left
        lda #0
        rol a
        sta sh_xflag            ; X = bit out
        dec sh_count
        bne lp_roxl
        jmp fin_rox

lp_roxr:
        lda sh_xflag
        lsr a                   ; C = X (X enters at msb)
        jsr sh1_right
        lda #0
        rol a
        sta sh_xflag
        dec sh_count
        bne lp_roxr
        jmp fin_rox

; ---------------------------------------------------------------------------
; Sized one-bit shifts of t_res.  Carry-in = inserted bit; carry-out = bit
; shifted out.  ldx/dex/beq don't touch C, so the caller's carry survives.
; ---------------------------------------------------------------------------
sh1_left:                       ; C -> bit0 ... msb -> C
        ldx sh_size
        beq @b
        dex
        beq @w
        rol t_res               ; long: 5+5+5+5 = 20 cyc
        rol t_res+1
        rol t_res+2
        rol t_res+3
        rts
@w:     rol t_res
        rol t_res+1
        rts
@b:     rol t_res
        rts

sh1_right:                      ; C -> msb ... bit0 -> C
        ldx sh_size
        beq @b
        dex
        beq @w
        ror t_res+3
        ror t_res+2
        ror t_res+1
        ror t_res
        rts
@w:     ror t_res+1
        ror t_res
        rts
@b:     ror t_res
        rts

; ---------------------------------------------------------------------------
.segment "RODATA"
sh_toptab:                      ; top sized byte index per size code
        .byte 0, 1, 3
sh_jtab_lo:                     ; index = type*2 + direction (0=right,1=left)
        .byte <lp_asr_pre, <lp_asl, <lp_lsr, <lp_lsl
        .byte <lp_roxr, <lp_roxl, <lp_ror, <lp_rol
sh_jtab_hi:
        .byte >lp_asr_pre, >lp_asl, >lp_lsr, >lp_lsl
        .byte >lp_roxr, >lp_roxl, >lp_ror, >lp_rol
sh_widtab:                      ; operand width in bits per size code
        .byte 8, 16, 32
