; ---------------------------------------------------------------------------
; tests/runner/runner.s — C64-side M1 test-case runner (ca65)
;
; Linked into build/runner.prg together with runtime/*.s (real core) by the
; Makefile, or into build/stub/stub_runner.prg with tests/stub/stub_core.s
; for standalone harness testing. Contract: docs/CONTRACTS.md.
;
; Segments used: CODE only. The PRG's LOADADDR + STARTUP (BASIC SYS stub)
; live in core.s per runtime/c64.cfg — the stub must end with `jmp main`
; (main imported from here). tests/stub/stub_core.s does the same for the
; standalone build.
;
; Exports:  main, test_run_case
; Imports:  emu_reset, emu_step, mem_read8, mem_write8   (interpreter ABI)
;
; test_run_case protocol (host calls it via jsr()/run_subroutine at the
; address of `test_run_case` from build/runner.lbl):
;   1. host writes one case record (see tests/case_format.py) to buf_case
;   2. host calls test_run_case
;   3. runner: done flag := 0, emu_reset, load regs/PC/SR into ZP file,
;      apply init-mem pairs via mem_write8, emu_step ONCE, dump regs/PC/SR
;      and st_halt/st_reason to buf_result, read each expected-mem address
;      back via mem_read8, set done flag := $FF, RTS.
;
; buf_result layout ($C800):
;   +$00  u8   done flag: $00 = busy (written on entry), $FF = complete
;   +$01  u8   st_halt  copy (0 = instruction retired normally)
;   +$02  u8   st_reason copy
;   +$03  u8   n_mem — number of readback bytes at +$80
;   +$10  70   D0-D7,A0-A7 (16 x u32 LE), PC (u32 LE), SR (u16 LE) — final
;   +$80  m    mem_read8 value for each expected-mem addr, in case order
;              (m <= 255, so this spans at most $C880-$C97F)
;   +$7FF u8   $CFFF: runner-ready flag, main writes $A5 once booted
; ---------------------------------------------------------------------------

.export test_run_case, main
.import emu_reset, emu_step, mem_read8, mem_write8

; All fixed addresses come from the resource contract (docs/RESOURCES.md):
; contract ZP register file ($10-$7F), the runner-private loader/harness
; scratch bank cp/cnt/nexp/expp/rbidx ($02-$08, survives emu_step), and
; buf_case/buf_result.  Values are plain equates — no .import needed.
.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"

RES_DONE   = buf_result + $00
RES_HALT   = buf_result + $01
RES_REASON = buf_result + $02
RES_NMEM   = buf_result + $03
RES_REGS   = buf_result + $10
RES_MEM    = buf_result + $80
READY_FLAG = buf_result + $7FF          ; $CFFF

; Case record offsets (see tests/case_format.py)
CASE_REGS  = buf_case + 2               ; 70 bytes: regs+PC+SR, contiguous LE
CASE_NINIT = buf_case + 72
CASE_INIT  = buf_case + 73

; ---------------------------------------------------------------------------
.segment "CODE"

main:
        lda     #$A5
        sta     READY_FLAG              ; host polls $CFFF for this
@idle:
        jmp     @idle                   ; sit still; host drives test_run_case

.proc test_run_case
        sei                             ; core may bank out KERNAL during step
        lda     #$00
        sta     RES_DONE
        jsr     emu_reset

        ; regs + PC + SR: 70 contiguous LE bytes, case layout matches the
        ; ZP register file layout exactly ($10-$55)
        ldx     #0
@load:  lda     CASE_REGS,x
        sta     r_d0,x
        inx
        cpx     #70
        bne     @load

        ; apply init-mem pairs {u24 addr, u8 val} via mem_write8
        lda     #<CASE_INIT
        sta     cp
        lda     #>CASE_INIT
        sta     cp+1
        lda     CASE_NINIT
        sta     cnt
@init:  lda     cnt
        beq     @init_done
        ldy     #0
        lda     (cp),y
        sta     m_addr
        iny
        lda     (cp),y
        sta     m_addr+1
        iny
        lda     (cp),y
        sta     m_addr+2
        lda     #0
        sta     m_addr+3
        ldy     #3
        lda     (cp),y
        sta     m_val
        jsr     mem_write8
        jsr     advance4
        dec     cnt
        jmp     @init
@init_done:

        ; cp now points at n_exp; save count + start of expected-mem pairs
        ldy     #0
        lda     (cp),y
        sta     nexp
        clc
        lda     cp
        adc     #1
        sta     expp
        lda     cp+1
        adc     #0
        sta     expp+1

        jsr     emu_step                ; execute exactly one guest instruction

        ; dump final regs/PC/SR (70 bytes) + interpreter state
        ldx     #0
@dump:  lda     r_d0,x
        sta     RES_REGS,x
        inx
        cpx     #70
        bne     @dump
        lda     st_halt
        sta     RES_HALT
        lda     st_reason
        sta     RES_REASON
        lda     nexp
        sta     RES_NMEM

        ; read back each expected-mem address via mem_read8
        lda     expp
        sta     cp
        lda     expp+1
        sta     cp+1
        lda     #0
        sta     rbidx
@rb:    lda     rbidx
        cmp     nexp
        beq     @rb_done
        ldy     #0
        lda     (cp),y
        sta     m_addr
        iny
        lda     (cp),y
        sta     m_addr+1
        iny
        lda     (cp),y
        sta     m_addr+2
        lda     #0
        sta     m_addr+3
        jsr     mem_read8
        ldx     rbidx
        lda     m_val
        sta     RES_MEM,x
        inc     rbidx
        jsr     advance4
        jmp     @rb
@rb_done:

        lda     #$FF
        sta     RES_DONE                ; completion flag the host can poll
        rts
.endproc

; cp += 4
.proc advance4
        clc
        lda     cp
        adc     #4
        sta     cp
        bcc     @done
        inc     cp+1
@done:  rts
.endproc
