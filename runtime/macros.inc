; ---------------------------------------------------------------------------
; runtime/macros.inc — shared ZP map, constants, macros.  Owner: core agent.
;
; ca65 resolves .include against the INCLUDING FILE's directory, so:
;   from runtime/*.s:      .include "macros.inc"
;   from runtime/ops/*.s:  .include "../macros.inc"
;   from tests/runner/*.s: .include "../../runtime/macros.inc"
;
; Everything here is defined as plain constants, so every module that
; includes this file gets the addresses directly — no .import needed.
; core.s additionally .exports the same names so they appear in
; build/runner.lbl for the host harness.  Do NOT .import a name from this
; file in a module that also includes it (duplicate-symbol error).
; ---------------------------------------------------------------------------

.ifndef AMIGA64_MACROS_INC
AMIGA64_MACROS_INC = 1

; --- Zero-page register file (contract: interpreter owns $10-$7F) ----------
; All multi-byte values little-endian.  $02-$0F reserved for loader/harness.

r_d0      = $10         ; D0..D7, 4 bytes each ($10-$2F)
r_d1      = $14
r_d2      = $18
r_d3      = $1C
r_d4      = $20
r_d5      = $24
r_d6      = $28
r_d7      = $2C
r_a0      = $30         ; A0..A7, 4 bytes each ($30-$4F); A7 = active SP
r_a1      = $34
r_a2      = $38
r_a3      = $3C
r_a4      = $40
r_a5      = $44
r_a6      = $48
r_a7      = $4C
r_pc      = $50         ; PC, 4 bytes (bits 24-31 always 0)
r_sr      = $54         ; $54 = CCR (bit4 X, 3 N, 2 Z, 1 V, 0 C), $55 = SR hi
op_lo     = $56         ; current opcode word, 68k bits 7-0
op_hi     = $57         ; current opcode word, 68k bits 15-8
m_addr    = $58         ; guest address argument for memory API (LE, 4 bytes)
m_val     = $5C         ; value in/out for memory API (LE, 4 bytes)
t_ea      = $60         ; resolved effective address (LE, 4 bytes)
t_op1     = $64         ; ALU operand 1 = destination operand (LE)
t_op2     = $68         ; ALU operand 2 = source operand (LE)
t_res     = $6C         ; ALU result (LE)
t_tmp     = $70         ; free scratch $70-$77, ANY core routine may clobber
                        ;   (memory API uses $70-$73 as its REU DMA bounce
                        ;    buffer; fetch_word mirrors the fetched word to
                        ;    t_tmp+0=lo / t_tmp+1=hi on return)
st_halt   = $78         ; 0 = running, nonzero = halted
st_reason = $79         ; HALT_* reason code, valid when st_halt != 0

; $7A-$7F: interpreter-internal state (contract: "rest reserved").
; Group handlers must treat these as opaque — do not read or write.
ea_kind   = $7A         ; internal: 0 = memory (t_ea valid), 1 = register
ea_reg    = $7B         ; internal: ZP offset of register from r_d0 (reg*4,
                        ;   +$20 for An register-direct)
ea_size   = $7C         ; internal: size latched by ea_calc
st_step   = $7D         ; internal: nonzero = emu_step (stop after 1 instr)
w_zacc    = $7E         ; internal: ALU zero-accumulator scratch
w_ptmp    = $7F         ; internal: ALU/EA flag scratch

; --- Halt reason codes -----------------------------------------------------
HALT_OK      = $00      ; clean termination (pseudo-op $AFFF)
HALT_ALINE   = $01      ; other $Axxx opcode (M2 HLE trap hook)
HALT_FLINE   = $02      ; $Fxxx opcode
HALT_ILLEGAL = $03      ; illegal / unimplemented opcode

; --- Fixed buffers (contract memory map) -----------------------------------
buf_case   = $C000      ; harness writes test cases here ($C000-$C7FF)
buf_result = $C800      ; runner writes final state here ($C800-$CFFF)

; --- REU (RAM Expansion Controller) registers ------------------------------
REC_STATUS  = $DF00
REC_COMMAND = $DF01
REC_C64LO   = $DF02
REC_C64HI   = $DF03
REC_REULO   = $DF04
REC_REUHI   = $DF05
REC_REUBANK = $DF06
REC_LENLO   = $DF07
REC_LENHI   = $DF08
REC_IMASK   = $DF09
REC_ACTRL   = $DF0A
REU_CMD_FETCH = $91     ; execute | no-$FF00-trigger | REU -> C64
REU_CMD_STASH = $90     ; execute | no-$FF00-trigger | C64 -> REU

; --- Register accessor macros (contract) -----------------------------------
; X = 68k register index 0-15 (0-7 = D0-D7, 8-15 = A0-A7).
; getreg: copies the 4-byte register into t_op1.
; setreg: copies t_res into the 4-byte register.
; Both preserve X, clobber A and Y.  ~40 cycles each.

.macro getreg
        txa
        asl
        asl
        tay                     ; Y = reg*4
        lda r_d0,y              ; abs,y — no zp,y mode for lda
        sta t_op1
        lda r_d0+1,y
        sta t_op1+1
        lda r_d0+2,y
        sta t_op1+2
        lda r_d0+3,y
        sta t_op1+3
.endmacro

.macro setreg
        txa
        asl
        asl
        tay
        lda t_res
        sta r_d0,y
        lda t_res+1
        sta r_d0+1,y
        lda t_res+2
        sta r_d0+2,y
        lda t_res+3
        sta r_d0+3,y
.endmacro

; Bonus (not in contract, provided for convenience): like getreg but loads
; into t_op2 — handy for two-operand register/register forms.
.macro getreg2
        txa
        asl
        asl
        tay
        lda r_d0,y
        sta t_op2
        lda r_d0+1,y
        sta t_op2+1
        lda r_d0+2,y
        sta t_op2+2
        lda r_d0+3,y
        sta t_op2+3
.endmacro

.endif  ; AMIGA64_MACROS_INC
