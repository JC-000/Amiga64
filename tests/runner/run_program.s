; ---------------------------------------------------------------------------
; tests/runner/run_program.s — C64-side M0 loader entries (ca65)
;
; Linked into build/runner.prg by the Makefile wildcard alongside
; tests/runner/runner.s. Contract: docs/CONTRACTS.md "M0 loader".
;
; Exports:  test_load_chunk, test_run_program
; Imports:  emu_reset, emu_step, mem_write8, mem_rec_dirty (interpreter ABI)
;
; REC ownership (docs/WAVE4_BRIEF.md C1): both entries are host-reachable
; between runs, so both invalidate the memory API's REC shadow before their
; first memory-API call -- test_run_program via emu_reset (which implies
; mem_rec_dirty), test_load_chunk explicitly.
;
; test_load_chunk — stage one image record into guest (REU) memory:
;   host writes to buf_case a record framed exactly like image.bin
;   (host/image.py): u32 LE guest offset, u32 LE payload length, payload.
;   The payload must fit buf_case, so length <= $7F8; the host splits
;   larger image chunks into consecutive records. Each payload byte goes
;   through mem_write8. Result: buf_result+0 done flag $00 -> $FF.
;
; test_run_program — run a staged guest program to completion:
;   buf_case parameter block: u32 LE initial PC, u32 LE initial A7,
;   u32 LE instruction budget (0 = unlimited). emu_reset (all other regs
;   and SR come up zero), set PC/A7, then loop emu_step counting executed
;   instructions until st_halt != 0 or the budget is exhausted.
;
; buf_result layout for test_run_program (differs from test_run_case only
; in +$03..+$07; regs stay at the shared +$10 offset):
;   +$00  u8   done flag: $00 = busy (written on entry), $FF = complete
;   +$01  u8   st_halt copy (nonzero = program halted/trapped)
;   +$02  u8   st_reason copy (HALT_OK = $00 for the $AFFF pseudo-op)
;   +$03  u8   timeout flag: $01 = budget exhausted before st_halt
;   +$04  u32  executed-instruction count (LE; the halting instruction
;              itself is counted, so "MOVEQ; HALT" retires as 2)
;   +$10  70   D0-D7,A0-A7 (16 x u32 LE), PC (u32 LE), SR (u16 LE) — final
; ---------------------------------------------------------------------------

.export test_load_chunk, test_run_program
.import emu_reset, emu_step, mem_write8, mem_rec_dirty

; All fixed addresses come from the resource contract (docs/RESOURCES.md):
; contract ZP register file ($10-$7F), this file's loader/harness scratch
; bank srcp/lcnt/gaddr/icount/nobud ($02-$0A — overlays runner.s's
; test_run_case bank byte-for-byte; the entries never run concurrently,
; so reuse is safe), and buf_case/buf_result.
.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"

RES_DONE    = buf_result + $00
RES_HALT    = buf_result + $01
RES_REASON  = buf_result + $02
RES_TIMEOUT = buf_result + $03
RES_ICOUNT  = buf_result + $04
RES_REGS    = buf_result + $10

; Record/parameter-block offsets in buf_case
LOAD_OFFSET = buf_case + 0              ; u32 LE guest offset
LOAD_LEN    = buf_case + 4              ; u32 LE length (host caps at $7F8)
LOAD_DATA   = buf_case + 8
RUN_PC      = buf_case + 0              ; u32 LE initial PC
RUN_A7      = buf_case + 4              ; u32 LE initial A7
RUN_BUDGET  = buf_case + 8              ; u32 LE max instructions, 0 = no cap

; ---------------------------------------------------------------------------
.segment "CODE"

.proc test_load_chunk
        sei                             ; match test_run_case: no KERNAL IRQs
        lda     #$00
        sta     RES_DONE
        jsr     mem_rec_dirty           ; C1: host may have left the REC dirty

        ldx     #3                      ; guest cursor := record offset
@gcopy: lda     LOAD_OFFSET,x
        sta     gaddr,x
        dex
        bpl     @gcopy

        lda     LOAD_LEN
        sta     lcnt
        lda     LOAD_LEN+1
        sta     lcnt+1

        lda     #<LOAD_DATA
        sta     srcp
        lda     #>LOAD_DATA
        sta     srcp+1

@loop:  lda     lcnt
        ora     lcnt+1
        beq     @done
        ldx     #3                      ; m_addr := cursor (fresh each call)
@acopy: lda     gaddr,x
        sta     m_addr,x
        dex
        bpl     @acopy
        ldy     #0
        lda     (srcp),y
        sta     m_val
        jsr     mem_write8

        inc     gaddr                   ; 32-bit cursor++
        bne     @src
        inc     gaddr+1
        bne     @src
        inc     gaddr+2
        bne     @src
        inc     gaddr+3
@src:   inc     srcp
        bne     @cnt
        inc     srcp+1
@cnt:   lda     lcnt                    ; 16-bit count--
        bne     @declo
        dec     lcnt+1
@declo: dec     lcnt
        jmp     @loop

@done:  lda     #$FF
        sta     RES_DONE
        rts
.endproc

.proc test_run_program
        sei
        lda     #$00
        sta     RES_DONE
        sta     RES_TIMEOUT
        jsr     emu_reset               ; regs/SR zero, st_halt = 0

        ldx     #3
@init:  lda     RUN_PC,x
        sta     r_pc,x
        lda     RUN_A7,x
        sta     r_a7,x
        dex
        bpl     @init
        lda     #0
        sta     icount
        sta     icount+1
        sta     icount+2
        sta     icount+3

        lda     RUN_BUDGET
        ora     RUN_BUDGET+1
        ora     RUN_BUDGET+2
        ora     RUN_BUDGET+3
        beq     @unlim
        lda     #0
        sta     nobud
        beq     @loop
@unlim: lda     #1
        sta     nobud

@loop:  lda     st_halt
        bne     @dump                   ; program halted/trapped
        lda     nobud
        bne     @step
        lda     icount                  ; budget check: icount == budget?
        cmp     RUN_BUDGET
        bne     @step
        lda     icount+1
        cmp     RUN_BUDGET+1
        bne     @step
        lda     icount+2
        cmp     RUN_BUDGET+2
        bne     @step
        lda     icount+3
        cmp     RUN_BUDGET+3
        bne     @step
        lda     #$01
        sta     RES_TIMEOUT
        bne     @dump                   ; always
@step:  jsr     emu_step
        inc     icount                  ; 32-bit retired counter++
        bne     @loop
        inc     icount+1
        bne     @loop
        inc     icount+2
        bne     @loop
        inc     icount+3
        jmp     @loop

@dump:  ldx     #0
@regs:  lda     r_d0,x
        sta     RES_REGS,x
        inx
        cpx     #70
        bne     @regs
        lda     st_halt
        sta     RES_HALT
        lda     st_reason
        sta     RES_REASON
        ldx     #3
@cnt:   lda     icount,x
        sta     RES_ICOUNT,x
        dex
        bpl     @cnt

        lda     #$FF
        sta     RES_DONE
        rts
.endproc
