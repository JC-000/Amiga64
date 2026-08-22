; ===========================================================================
; runtime/ops/g6_muldiv.s — G6 cross-op leafs: MULU/MULS, DIVU/DIVS, MOVEM,
; ABCD/SBCD/NBCD.  Owner: agent G6.  Contract: docs/CONTRACTS.md.
;
; All entry points are cross-ops: entered by plain `jmp` from the owning
; family decoder (fam_c/fam_8/fam_4) with the ORIGINAL opcode word in
; op_lo/op_hi and PC past the opcode word.  Full field decode happens here;
; every path ends with `jmp dispatch_next` (or emu_halt).
;
; Semantics were pinned against the golden vectors (450/450 in a Python
; reference model) — notably the "undefined" flag bits:
;   - MULU/MULS: N = bit31, Z = 32-bit result == 0, V = C = 0, X unchanged.
;   - DIVU/DIVS: on overflow V = 1, C = 0, N/Z/X UNCHANGED, Dn UNCHANGED.
;     Normal: Dn = remainder:quotient, N = quotient bit15, Z = quotient == 0,
;     V = C = 0.  DIVS truncates toward zero, remainder sign = dividend sign.
;     Divide by zero is filtered from the vectors; we halt (HALT_ILLEGAL)
;     instead of hanging.
;   - ABCD/SBCD/NBCD: hardware-exact 68000 model (flamewing's algorithm,
;     as in hw-validated Musashi): byte-wide binary+decimal carry formulas
;     give C/X and the "undefined" N/V; Z is sticky (cleared on nonzero,
;     unchanged on zero).  Matches all 150 BCD vector cases.
;   - MOVEM: -(An) uses a REVERSED mask (bit0 = A7) and stores the INITIAL
;     base-An value (automatic here: writeback happens after the loop);
;     (An)+ writes the final address back (overwriting a loaded An);
;     memory-to-register .W sign-extends each word.  No flags.
;
; Cycle notes (excluding ~190-cycle core fetch/dispatch overhead):
;   MULU/MULS ~1000-1300 (16-step shift-add ~750 + EA + flags)
;   DIVU/DIVS ~1100-1450 (16-step restoring divide ~800 + EA/abs/flags)
;   BCD ops   ~250-500 (register form ~300; memory form adds 2x EA + store)
;   MOVEM     ~150 setup + ~220 (.W) / ~260 (.L) per transferred register:
;             a full 16-register MOVEM.L is ~4300 cycles (> the 1500 note
;             threshold — inherently n transfers; the per-register cost is
;             dominated by the per-call REU DMA of the v0 memory API).
; ===========================================================================

.include "../macros.inc"

.export op_mulu, op_muls, op_divu, op_divs
.export op_movem, op_abcd, op_sbcd, op_nbcd

.import ea_calc, ea_load, ea_store, fetch_word, dispatch_next, emu_halt
.import mem_read16, mem_read32, mem_write16, mem_write32

; --- locals (DATA = RAM inside the PRG; survive core calls, unlike t_tmp) --
.segment "DATA"
v_sgn:  .byte 0                 ; 0 = unsigned op, 1 = signed (op_hi bit0)
v_dn4:  .byte 0                 ; Dn*4 (ZP offset from r_d0)
v_src:  .word 0                 ; 16-bit source operand (multiplier/divisor)
v_m:    .word 0                 ; multiply shift register
v_dst:  .res 4                  ; dividend / quotient(lo):remainder(hi)
v_qs:   .byte 0                 ; DIVS: quotient sign ($80 = negative)
v_rs:   .byte 0                 ; DIVS: remainder sign ($80 = negative)
v_t:    .byte 0                 ; flag scratch
v_t2:   .byte 0                 ; flag scratch
v_dir:  .byte 0                 ; MOVEM: nonzero = memory-to-register
v_size: .byte 0                 ; MOVEM: bytes per transfer (2/4)
v_ea:   .byte 0                 ; MOVEM: 6-bit mode|reg field
v_reg4: .byte 0                 ; MOVEM: base reg*4 (offset from r_a0)
v_mask: .word 0                 ; MOVEM: remaining register mask
v_ro:   .byte 0                 ; MOVEM: current register*4 (from r_d0)
v_wb:   .byte 0                 ; MOVEM: nonzero = (An)+ writeback pending
v_badd: .byte 0                 ; BCD: nonzero = add (ABCD), 0 = subtract
v_rx:   .byte 0                 ; BCD: dst register number
v_ry:   .byte 0                 ; BCD: src register number
v_bsrc: .byte 0                 ; BCD: source byte
v_bdst: .byte 0                 ; BCD: destination byte
v_bss:  .byte 0                 ; BCD: uncorrected binary sum/difference
v_bcv:  .byte 0                 ; BCD: binary carry/borrow mask (bits 3,7)
v_bres: .byte 0                 ; BCD: result byte
v_ccr:  .byte 0                 ; BCD: CCR under construction

.segment "CODE"

; ===========================================================================
; MULU/MULS <ea>,Dn — 1100 rrrS 11 mmmrrr (S: 0 = MULU, 1 = MULS)
; 16x16 -> 32 into Dn.  Shared entry: sign from op_hi bit0.
; ===========================================================================
op_mulu:
op_muls:
        lda op_hi               ; decode BEFORE ea_calc clobbers op_*
        and #$01
        sta v_sgn
        lda op_hi
        and #$0E
        asl
        sta v_dn4               ; ((op>>9)&7)*4
        lda op_lo
        and #$3F
        tax
        lda #1                  ; word
        jsr ea_calc
        lda #1
        jsr ea_load             ; t_op1+0/1 = source word
        lda t_op1
        sta v_src
        lda t_op1+1
        sta v_src+1
        ldy v_dn4               ; multiplier = Dn.w (copy kept in t_op2)
        lda r_d0,y
        sta v_m
        sta t_op2
        lda r_d0+1,y
        sta v_m+1
        sta t_op2+1
        lda #0                  ; unsigned 16x16 -> t_res (32), classic
        sta t_res+2             ; shift-add: ~45 cycles/iter avg, 16 iters
        sta t_res+3
        ldx #16
mul_lp: lsr v_m+1
        ror v_m
        bcc mul_no
        clc
        lda t_res+2
        adc v_src
        sta t_res+2
        lda t_res+3
        adc v_src+1
        sta t_res+3
mul_no: ror t_res+3             ; carry from the high add rotates in
        ror t_res+2
        ror t_res+1
        ror t_res
        dex
        bne mul_lp
        lda v_sgn               ; MULS: signed = unsigned product with
        beq mul_st              ; P_hi -= dst if src<0, P_hi -= src if dst<0
        lda v_src+1
        bpl mul_s1
        sec
        lda t_res+2
        sbc t_op2
        sta t_res+2
        lda t_res+3
        sbc t_op2+1
        sta t_res+3
mul_s1: lda t_op2+1
        bpl mul_st
        sec
        lda t_res+2
        sbc v_src
        sta t_res+2
        lda t_res+3
        sbc v_src+1
        sta t_res+3
mul_st: ldy v_dn4
        lda t_res
        sta r_d0,y
        lda t_res+1
        sta r_d0+1,y
        lda t_res+2
        sta r_d0+2,y
        lda t_res+3
        sta r_d0+3,y
        lda r_sr                ; N = bit31, Z = res==0, V = C = 0, X kept
        and #$10
        sta v_t
        lda t_res+3
        bpl mul_nn
        lda v_t
        ora #$08
        sta v_t
mul_nn: lda t_res
        ora t_res+1
        ora t_res+2
        ora t_res+3
        bne mul_nz
        lda v_t
        ora #$04
        sta v_t
mul_nz: lda v_t
        sta r_sr
        jmp dispatch_next

; ===========================================================================
; DIVU/DIVS <ea>,Dn — 1000 rrrS 11 mmmrrr (S: 0 = DIVU, 1 = DIVS)
; Dn(32) / ea.w(16) -> Dn = remainder(hi):quotient(lo).
; ===========================================================================
op_divu:
op_divs:
        lda op_hi
        and #$01
        sta v_sgn
        lda op_hi
        and #$0E
        asl
        sta v_dn4
        lda op_lo
        and #$3F
        tax
        lda #1
        jsr ea_calc
        lda #1
        jsr ea_load             ; t_op1+0/1 = divisor
        lda t_op1
        sta v_src
        lda t_op1+1
        sta v_src+1
        ora t_op1
        bne div_nz
        lda #HALT_ILLEGAL       ; divide by zero (vectors filter these):
        jmp emu_halt            ; halt rather than hang
div_nz: ldy v_dn4               ; dividend = Dn (32)
        lda r_d0,y
        sta v_dst
        lda r_d0+1,y
        sta v_dst+1
        lda r_d0+2,y
        sta v_dst+2
        lda r_d0+3,y
        sta v_dst+3
        lda v_sgn
        bne dvs_go
        lda v_dst+3             ; DIVU overflow iff hi16(dividend) >= divisor
        cmp v_src+1
        bcc dvu_ok
        bne div_ovf
        lda v_dst+2
        cmp v_src
        bcs div_ovf
dvu_ok: jsr div_core
        jmp div_store

div_ovf:                        ; overflow: V=1 C=0, N/Z/X and Dn UNCHANGED
        lda r_sr                ; (vector-verified 68000 behavior)
        and #$1C
        ora #$02
        sta r_sr
        jmp dispatch_next

dvs_go: lda v_dst+3             ; DIVS: work on absolute values
        and #$80
        sta v_rs                ; remainder sign = dividend sign
        lda v_src+1
        and #$80
        eor v_rs
        sta v_qs                ; quotient sign = dividend^divisor
        lda v_rs
        beq dvs_dp
        sec                     ; dividend = -dividend (32-bit)
        lda #0
        sbc v_dst
        sta v_dst
        lda #0
        sbc v_dst+1
        sta v_dst+1
        lda #0
        sbc v_dst+2
        sta v_dst+2
        lda #0
        sbc v_dst+3
        sta v_dst+3
dvs_dp: lda v_src+1
        bpl dvs_sp
        sec                     ; divisor = -divisor (16-bit)
        lda #0
        sbc v_src
        sta v_src
        lda #0
        sbc v_src+1
        sta v_src+1
dvs_sp: lda v_dst+3             ; early overflow: hi16(|dvd|) >= |dvs|
        cmp v_src+1
        bcc dvs_ok
        bne div_ovf
        lda v_dst+2
        cmp v_src
        bcs div_ovf
dvs_ok: jsr div_core            ; unsigned q in v_dst+0/1, r in v_dst+2/3
        lda v_qs
        beq dvs_pos
        lda v_dst+1             ; negative quotient: |q| <= $8000 allowed
        bpl dvs_ng
        cmp #$80                ; only exactly $8000 (= -32768) fits
        bne dvs_ov
        lda v_dst
        bne dvs_ov
dvs_ng: sec                     ; q = -q
        lda #0
        sbc v_dst
        sta v_dst
        lda #0
        sbc v_dst+1
        sta v_dst+1
        jmp dvs_rem
dvs_ov: jmp div_ovf
dvs_pos:
        lda v_dst+1             ; positive quotient: > $7FFF overflows
        bmi dvs_ov
dvs_rem:
        lda v_rs
        beq dvs_fin
        sec                     ; remainder = -remainder (16-bit)
        lda #0
        sbc v_dst+2
        sta v_dst+2
        lda #0
        sbc v_dst+3
        sta v_dst+3
dvs_fin:
        jmp div_store

; 16-step restoring division: v_dst = dividend (lo word becomes quotient,
; hi word becomes remainder), v_src = divisor.  Caller guarantees
; hi16(dividend) < divisor, so the remainder needs at most 17 bits mid-step.
; ~50 cycles/iter, 16 iters ~= 800.
div_core:
        ldx #16
dvc_lp: asl v_dst               ; shift dividend left, quotient bit0 <- 0
        rol v_dst+1
        rol v_dst+2
        rol v_dst+3             ; C = 17th remainder bit
        bcs dvc_sb              ; 17-bit remainder always >= divisor
        lda v_dst+3
        cmp v_src+1
        bcc dvc_nx
        bne dvc_sb
        lda v_dst+2
        cmp v_src
        bcc dvc_nx
dvc_sb: sec                     ; remainder -= divisor (mod 2^16 also
        lda v_dst+2             ; correct for the 17-bit case)
        sbc v_src
        sta v_dst+2
        lda v_dst+3
        sbc v_src+1
        sta v_dst+3
        inc v_dst               ; quotient bit
dvc_nx: dex
        bne dvc_lp
        rts

div_store:                      ; Dn <- remainder:quotient, flags from q
        ldy v_dn4
        lda v_dst
        sta r_d0,y
        lda v_dst+1
        sta r_d0+1,y
        lda v_dst+2
        sta r_d0+2,y
        lda v_dst+3
        sta r_d0+3,y
        lda r_sr                ; N = q bit15, Z = q==0, V = C = 0, X kept
        and #$10
        sta v_t
        lda v_dst+1
        bpl dvf_nn
        lda v_t
        ora #$08
        sta v_t
dvf_nn: lda v_dst
        ora v_dst+1
        bne dvf_nz
        lda v_t
        ora #$04
        sta v_t
dvf_nz: lda v_t
        sta r_sr
        jmp dispatch_next

; ===========================================================================
; MOVEM — 0100 1D00 1S mmmrrr + mask word (D: 0 = reg->mem, 1 = mem->reg;
; S: 0 = word, 1 = long).  No flags.
; ===========================================================================
op_movem:
        lda op_hi               ; decode BEFORE the mask fetch clobbers op_*
        and #$04
        sta v_dir
        lda op_lo
        and #$40
        beq mvm_w
        lda #4
        bne mvm_sz
mvm_w:  lda #2
mvm_sz: sta v_size
        lda op_lo
        and #$3F
        sta v_ea
        and #$07
        asl
        asl
        sta v_reg4
        jsr fetch_word          ; register mask extension word
        lda op_lo
        sta v_mask
        lda op_hi
        sta v_mask+1
        lda v_dir
        beq mvm_store
        jmp mvm_load

; --- register-to-memory ---
mvm_store:
        lda v_ea
        and #$38
        cmp #$20                ; -(An)?
        beq mvm_pre
        ldx v_ea                ; control mode: resolve once
        lda #1
        jsr ea_calc             ; t_ea = start address
        lda #0
        sta v_ro                ; ascending: mask bit0 = D0
mvs_lp: lda v_mask
        ora v_mask+1
        bne mvs_go
        jmp dispatch_next
mvs_go: lsr v_mask+1
        ror v_mask
        bcc mvs_nx
        jsr mv_write
        clc                     ; t_ea += size
        lda t_ea
        adc v_size
        sta t_ea
        bcc mvs_nx
        inc t_ea+1
        bne mvs_nx
        inc t_ea+2
        bne mvs_nx
        inc t_ea+3
mvs_nx: lda v_ro
        clc
        adc #4
        sta v_ro
        jmp mvs_lp

mvm_pre:                        ; -(An): REVERSED mask (bit0 = A7), stores
        ldy v_reg4              ; descend.  An is only written back after
        lda r_a0,y              ; the loop, so a store of the base An itself
        sta t_ea                ; naturally uses its INITIAL value (68000).
        lda r_a0+1,y
        sta t_ea+1
        lda r_a0+2,y
        sta t_ea+2
        lda r_a0+3,y
        sta t_ea+3
        lda #$3C                ; r_d0+$3C = A7
        sta v_ro
mvp_lp: lda v_mask
        ora v_mask+1
        beq mvp_wb
        lsr v_mask+1
        ror v_mask
        bcc mvp_nx
        sec                     ; t_ea -= size (before the store)
        lda t_ea
        sbc v_size
        sta t_ea
        lda t_ea+1
        sbc #0
        sta t_ea+1
        lda t_ea+2
        sbc #0
        sta t_ea+2
        lda t_ea+3
        sbc #0
        sta t_ea+3
        jsr mv_write
mvp_nx: lda v_ro
        sec
        sbc #4
        sta v_ro
        jmp mvp_lp
mvp_wb: ldy v_reg4              ; An = start - 4*count (final address)
        lda t_ea
        sta r_a0,y
        lda t_ea+1
        sta r_a0+1,y
        lda t_ea+2
        sta r_a0+2,y
        lda t_ea+3
        sta r_a0+3,y
mvm_done:
        jmp dispatch_next

; --- memory-to-register ---
mvm_load:
        lda #0
        sta v_wb
        lda v_ea
        and #$38
        cmp #$18                ; (An)+?
        bne mvl_ctl
        inc v_wb
        ldy v_reg4              ; t_ea = An directly — ea_calc would bump
        lda r_a0,y              ; An by one operand size only
        sta t_ea
        lda r_a0+1,y
        sta t_ea+1
        lda r_a0+2,y
        sta t_ea+2
        lda r_a0+3,y
        sta t_ea+3
        jmp mvl_go
mvl_ctl:
        ldx v_ea
        lda #1
        jsr ea_calc
mvl_go: lda #0
        sta v_ro
mvl_lp: lda v_mask
        ora v_mask+1
        beq mvl_end
        lsr v_mask+1
        ror v_mask
        bcc mvl_nx
        jsr mv_read
        clc                     ; t_ea += size
        lda t_ea
        adc v_size
        sta t_ea
        bcc mvl_nx
        inc t_ea+1
        bne mvl_nx
        inc t_ea+2
        bne mvl_nx
        inc t_ea+3
mvl_nx: lda v_ro
        clc
        adc #4
        sta v_ro
        jmp mvl_lp
mvl_end:
        lda v_wb
        beq mvl_done
        ldy v_reg4              ; (An)+ writeback: final address (wins over
        lda t_ea                ; a value just loaded into An, per 68000)
        sta r_a0,y
        lda t_ea+1
        sta r_a0+1,y
        lda t_ea+2
        sta r_a0+2,y
        lda t_ea+3
        sta r_a0+3,y
mvl_done:
        jmp dispatch_next

mv_write:                       ; guest[t_ea] <- reg[v_ro], v_size bytes
        lda t_ea
        sta m_addr
        lda t_ea+1
        sta m_addr+1
        lda t_ea+2
        sta m_addr+2
        lda t_ea+3
        sta m_addr+3
        ldy v_ro
        lda r_d0,y
        sta m_val
        lda r_d0+1,y
        sta m_val+1
        lda v_size
        cmp #4
        beq mvw_l
        jmp mem_write16
mvw_l:  lda r_d0+2,y
        sta m_val+2
        lda r_d0+3,y
        sta m_val+3
        jmp mem_write32

mv_read:                        ; reg[v_ro] <- guest[t_ea]; .W sign-extends
        lda t_ea
        sta m_addr
        lda t_ea+1
        sta m_addr+1
        lda t_ea+2
        sta m_addr+2
        lda t_ea+3
        sta m_addr+3
        lda v_size
        cmp #4
        beq mvr_l
        jsr mem_read16
        ldy v_ro
        lda m_val
        sta r_d0,y
        ldx #0
        lda m_val+1
        sta r_d0+1,y
        bpl mvr_p
        dex
mvr_p:  txa
        sta r_d0+2,y
        sta r_d0+3,y
        rts
mvr_l:  jsr mem_read32
        ldy v_ro
        lda m_val
        sta r_d0,y
        lda m_val+1
        sta r_d0+1,y
        lda m_val+2
        sta r_d0+2,y
        lda m_val+3
        sta r_d0+3,y
        rts

; ===========================================================================
; ABCD/SBCD Dy,Dx | -(Ay),-(Ax) — 1100/1000 xxx1 0000 myyy (byte only)
; Shared entry: op_hi bit6 distinguishes ABCD ($Cx) from SBCD ($8x).
; ===========================================================================
op_abcd:
op_sbcd:
        lda op_hi
        and #$40
        sta v_badd              ; $40 = ABCD, 0 = SBCD
        lda op_hi
        lsr
        and #$07
        sta v_rx
        lda op_lo
        and #$07
        sta v_ry
        lda op_lo
        and #$08
        bne bcd_mem
        lda v_ry                ; register form: Dy.b op Dx.b -> Dx.b
        asl
        asl
        tay
        lda r_d0,y
        sta v_bsrc
        lda v_rx
        asl
        asl
        tay
        lda r_d0,y
        sta v_bdst
        jsr bcd_compute
        lda v_rx
        asl
        asl
        tay
        lda v_bres
        sta r_d0,y
        jmp dispatch_next
bcd_mem:
        lda v_ry                ; -(Ay) source (ea_calc handles the A7
        ora #$20                ; byte-step-2 quirk)
        tax
        lda #0
        jsr ea_calc
        lda #0
        jsr ea_load
        lda t_op1
        sta v_bsrc
        lda v_rx                ; -(Ax) destination (rx == ry chains the
        ora #$20                ; two decrements, per 68000)
        tax
        lda #0
        jsr ea_calc
        lda #0
        jsr ea_load
        lda t_op1
        sta v_bdst
        jsr bcd_compute
        lda v_bres
        sta t_res
        lda #0
        jsr ea_store            ; byte to [Ax]
        jmp dispatch_next

; ===========================================================================
; NBCD <ea> — 0100 1000 00 mmmrrr: dst = 0 - dst - X in BCD (byte)
; ===========================================================================
op_nbcd:
        lda op_lo
        and #$3F
        tax
        lda #0
        jsr ea_calc
        lda #0
        jsr ea_load
        lda t_op1
        sta v_bsrc              ; subtrahend is the operand: 0 - dst - X
        lda #0
        sta v_bdst
        sta v_badd              ; subtract path
        jsr bcd_compute
        lda v_bres
        sta t_res
        lda #0
        jsr ea_store            ; register kind preserves upper bytes
        jmp dispatch_next

; ---------------------------------------------------------------------------
; bcd_compute: v_bres = v_bdst op v_bsrc (BCD, with X), CCR updated.
; Hardware-exact model (byte-wide; validated against all 150 BCD vectors):
;   ABCD:  ss = dst+src+X
;          bc = ((dst&src) | (~ss&(dst|src))) & $88        binary carries
;          dc = (carry(ss+$66) ? $80 : 0) | ((((ss+$66)^ss)&$10)>>1)
;          corf = m - (m>>2)  where m = bc|dc              ($88 -> $66 etc.)
;          res = ss + corf
;          C = X = bit7(bc | (ss & ~res));  V = bit7(~ss & res)
;   SBCD:  dd = dst-src-X
;          bb = ((~dst&src) | (dd&(~dst|src))) & $88       binary borrows
;          res = dd - (bb - (bb>>2))
;          C = X = bit7(bb | (res & ~dd));  V = bit7(dd & ~res)
;   N = bit7(res); Z sticky (cleared if res != 0, else unchanged).
; ---------------------------------------------------------------------------
bcd_compute:
        lda v_badd
        bne bcd_add
        jmp bcd_sub
bcd_add:
        lda r_sr                ; ---- ABCD: C := X, ss = dst + src + X
        and #$10
        cmp #$01
        lda v_bdst
        adc v_bsrc
        sta v_bss
        lda v_bdst              ; bc
        and v_bsrc
        sta v_t
        lda v_bdst
        ora v_bsrc
        sta v_t2
        lda v_bss
        eor #$FF
        and v_t2
        ora v_t
        and #$88
        sta v_bcv
        lda v_bss               ; dc from ss + $66
        clc
        adc #$66
        sta v_t
        lda #0
        rol                     ; A = carry(ss+$66)
        beq bca_nc
        lda #$80
bca_nc: sta v_t2
        lda v_t
        eor v_bss
        and #$10
        lsr
        ora v_t2                ; dc
        ora v_bcv               ; m = bc | dc
        sta v_t
        lsr
        lsr
        sta v_t2                ; m >> 2
        lda v_t
        sec
        sbc v_t2                ; corf
        clc
        adc v_bss
        sta v_bres              ; res = ss + corf
        eor #$FF                ; C = bit7(bc | (ss & ~res))
        and v_bss
        ora v_bcv
        and #$80
        sta v_t
        lda v_bss               ; V = bit7(~ss & res)
        eor #$FF
        and v_bres
        and #$80
        sta v_t2
        jmp bcd_flags

bcd_sub:
        lda r_sr                ; ---- SBCD/NBCD: C := !X, dd = dst - src - X
        and #$10
        eor #$10
        cmp #$01
        lda v_bdst
        sbc v_bsrc
        sta v_bss
        lda v_bdst              ; bb
        eor #$FF
        sta v_t                 ; ~dst
        and v_bsrc
        sta v_t2                ; ~dst & src
        lda v_t
        ora v_bsrc
        and v_bss
        ora v_t2
        and #$88
        sta v_bcv
        lsr                     ; corf = bb - (bb>>2)
        lsr
        sta v_t
        lda v_bcv
        sec
        sbc v_t
        sta v_t
        lda v_bss
        sec
        sbc v_t
        sta v_bres              ; res = dd - corf
        lda v_bss               ; C = bit7(bb | (res & ~dd))
        eor #$FF
        and v_bres
        ora v_bcv
        and #$80
        sta v_t
        lda v_bres              ; V = bit7(dd & ~res)
        eor #$FF
        and v_bss
        and #$80
        sta v_t2
        ; fall through
bcd_flags:                      ; X=C, N=bit7, V, C; Z sticky
        lda #0
        sta v_ccr
        lda v_t
        beq bcf_nc
        lda #$11                ; X + C
        sta v_ccr
bcf_nc: lda v_t2
        beq bcf_nv
        lda v_ccr
        ora #$02
        sta v_ccr
bcf_nv: lda v_bres
        bpl bcf_nn
        lda v_ccr
        ora #$08
        sta v_ccr
bcf_nn: lda v_bres
        bne bcf_st
        lda r_sr                ; zero result: keep previous Z
        and #$04
        ora v_ccr
        sta v_ccr
bcf_st: lda v_ccr
        sta r_sr
        rts
