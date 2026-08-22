; ---------------------------------------------------------------------------
; tests/stub/stub_core.s — mock interpreter core (ca65)
;
; Implements just enough of the docs/CONTRACTS.md "Interpreter ABI" to
; exercise tests/runner/runner.s + tests/run_vectors.py end-to-end before
; runtime/core.s exists. NOT linked by the Makefile wildcard (lives outside
; tests/runner/); built only by tests/stub/build_stub.py into
; build/stub/stub_runner.prg with tests/stub/stub.cfg.
;
; Guest memory fake: a 4 KB plain-RAM window at $8000 (segment GUESTRAM);
; guest address A maps to guestram[A & $0FFF]. No REU involved. Hand-written
; stub cases must keep all referenced guest addresses' low 12 bits distinct
; and must init (via init-mem pairs) every address that gets read.
;
; emu_step semantics (mirrored exactly by tests/stub/make_mini_vectors.py):
;   D0 += 1                     (32-bit wraparound)
;   mem_write8: [A0] := D1.b    (exercises the write API inside a step)
;   mem_read8:  D2.b := [A1]    (exercises the read API; D2 bits 8-31 kept)
;   PC += 2                     (32-bit)
;   CCR ^= $04                  (toggle Z; SR high byte preserved)
;   st_halt stays 0
; ---------------------------------------------------------------------------

.export emu_reset, emu_step, mem_read8, mem_write8
.import main                    ; runner.s entry, reached from the BASIC stub

; Contract zero-page register file
r_d0      = $10
r_d1      = $14
r_d2      = $18
r_a0      = $30
r_a1      = $34
r_pc      = $50
r_sr      = $54
m_addr    = $58
m_val     = $5C
t_tmp     = $70                 ; interpreter scratch — stub's fake-RAM pointer
st_halt   = $78
st_reason = $79

.segment "LOADADDR"
        .addr   $0801           ; PRG load address (core.s supplies this in
                                ; the real build; stub mirrors it)

.segment "STARTUP"
; BASIC stub: 10 SYS 2061 — STARTUP links at exactly $0801, so the jmp
; lands at $080D (= 2061). core.s owns this in the real build.
basic_stub:
        .word   @nextline, 10
        .byte   $9E, "2061", $00
@nextline:
        .word   0
        jmp     main

.segment "GUESTRAM"
guestram: .res $1000            ; page-aligned by stub.cfg (GRAM at $8000)

.segment "CODE"

.proc emu_reset
        lda     #0
        sta     st_halt
        sta     st_reason
        rts
.endproc

; t_tmp/t_tmp+1 := &guestram[m_addr & $0FFF]
.proc gaddr
        lda     m_addr
        sta     t_tmp
        lda     m_addr+1
        and     #$0F
        clc
        adc     #>guestram
        sta     t_tmp+1
        rts
.endproc

.proc mem_write8
        jsr     gaddr
        lda     m_val
        ldy     #0
        sta     (t_tmp),y
        rts
.endproc

.proc mem_read8
        jsr     gaddr
        ldy     #0
        lda     (t_tmp),y
        sta     m_val
        lda     #0
        sta     m_val+1
        sta     m_val+2
        sta     m_val+3
        rts
.endproc

.proc emu_step
        ; D0 += 1 (32-bit)
        inc     r_d0
        bne     d0_done
        inc     r_d0+1
        bne     d0_done
        inc     r_d0+2
        bne     d0_done
        inc     r_d0+3
d0_done:
        ; [A0] := D1.b
        ldx     #3
w_cp:   lda     r_a0,x
        sta     m_addr,x
        dex
        bpl     w_cp
        lda     r_d1
        sta     m_val
        jsr     mem_write8

        ; D2.b := [A1]
        ldx     #3
r_cp:   lda     r_a1,x
        sta     m_addr,x
        dex
        bpl     r_cp
        jsr     mem_read8
        lda     m_val
        sta     r_d2

        ; PC += 2 (32-bit)
        clc
        lda     r_pc
        adc     #2
        sta     r_pc
        lda     r_pc+1
        adc     #0
        sta     r_pc+1
        lda     r_pc+2
        adc     #0
        sta     r_pc+2
        lda     r_pc+3
        adc     #0
        sta     r_pc+3

        ; CCR ^= $04 (toggle Z), SR high byte untouched
        lda     r_sr
        eor     #$04
        sta     r_sr
        rts
.endproc
