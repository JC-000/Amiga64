; ---------------------------------------------------------------------------
; runtime/macros.inc — shared constants + macros.  Owner: core agent.
; Address allocations live in runtime/rc/*.inc (included below) — the
; resource contract, docs/RESOURCES.md.
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

; --- Resource contract (single source of truth for all fixed addresses) ----
; Zero page ($02-$0F harness, $10-$7F interpreter register file, $80-$82
; fetch window), C64 RAM/I-O (screen, buffers, REC, Ultimate) and guest/REU
; addresses all live in runtime/rc/*.inc, machine-checked by
; host/rescheck.py (`make rescheck`).  See docs/RESOURCES.md.

.include "rc/zp.inc"
.include "rc/ram.inc"
.include "rc/guest.inc"

; --- Halt reason codes -----------------------------------------------------
HALT_OK      = $00      ; clean termination (pseudo-op $AFFF)
HALT_ALINE   = $01      ; other $Axxx opcode (M2 HLE trap hook)
HALT_FLINE   = $02      ; $Fxxx opcode
HALT_ILLEGAL = $03      ; illegal / unimplemented opcode

; --- REU DMA command values (registers themselves: rc/ram.inc REC_*) -------
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
