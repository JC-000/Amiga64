; ===========================================================================
; runtime/ops/hle_con.s — console input for dos.library Read on the console
; handles (kinds 0/1).  Owner: WAVE5 w5-con.  Contract: docs/WAVE5_BRIEF.md C2.
;
; SUPERVISOR STUB: returns D0 = 0 (EOF), exactly the Wave-4 behaviour.
; Entry (by jmp from hle.s hle_read): D1 = fh BPTR, D2 = buffer, D3 = length,
; return address on the guest stack.  Retire with `jmp hle_ret` (emulated
; RTS) after setting D0 (bytes read, 0 = EOF, -1 = error).
; ===========================================================================

.include "../macros.inc"

.export hle_con_read
.import hle_ret

.segment "CODE"

hle_con_read:
        lda #0
        sta r_d0
        sta r_d0+1
        sta r_d0+2
        sta r_d0+3
        jmp hle_ret
