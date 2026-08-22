; tests/stub/smoke.s — minimal self-contained PRG proving the VICE harness
; path (boot, autostart, label discovery, jsr, memory read/write) works.
; Built by tests/smoke.py with tests/stub/stub.cfg. No core involved.

.export main, smoke_add

.segment "LOADADDR"
        .addr   $0801

.segment "STARTUP"
basic_stub:
        .word   @nextline, 10
        .byte   $9E, "2061", $00        ; 10 SYS 2061 (main = $080D)
@nextline:
        .word   0
main:
        lda     #$A5
        sta     $CFFF                   ; ready flag for the host
@idle:  jmp     @idle

.segment "CODE"
; $C002/$C003 := $C000 + $C001 (16-bit result, host-driven via jsr)
.proc smoke_add
        lda     #0
        sta     $C003
        clc
        lda     $C000
        adc     $C001
        sta     $C002
        rol     $C003
        rts
.endproc
