; fixtures/mod/ui/test/ui_test.s - W15-U test main for the UI kit
; (tests/run_modui.py).  Stands in for W15-M's main() until the merge:
; the UI runs alone from a host-written mod_view (WAVE15_BRIEF I-2).
;
; Entries (bra.w table at the image base, $010000):
;   +0  t_init    jsr _ui_init; HALT
;   +4  t_frame   ui_frame($000400); HALT     (one call: icount per call)
;   +8  t_empty   the same call frame around an rts (the icount baseline)
;   +12 t_live    under mach_run: ui_init, then per script record: copy its
;                 mod_view to $000400 and call ui_frame once per VERTB for
;                 its hold count; then IDLE more calls; HALT.
;   +16 t_preset  what the program's other half owns, before ui_init:
;                 DMACON SET AUD0-3EN, INTENA SET PORTS|AUD0 (the UI must
;                 leave both alone)
;   +20 t_regs    d0 = DMACONR, d1 = INTENAR; HALT
; Script at SCRIPT: dc.l records, dc.l idle; per record dc.w hold, 0, then
; the 72-byte mod_view.
; Build: vasmm68k_mot -Fbin -I fixtures/mod/ui -o ui_test.bin ui_test.s

SCRIPT  equ     $030000
VIEW    equ     $000400
INTREQR equ     $dff01e
INTREQ  equ     $dff09c

        org     $10000

        bra.w   t_init
        bra.w   t_frame
        bra.w   t_empty
        bra.w   t_live
        bra.w   t_preset
        bra.w   t_regs

t_init: jsr     _ui_init
        dc.w    $afff

t_frame:
        pea     VIEW.w
        jsr     _ui_frame
        addq.l  #4,sp
        dc.w    $afff

t_empty:
        pea     VIEW.w
        jsr     t_rts
        addq.l  #4,sp
        dc.w    $afff
t_rts:  rts

t_live: lea     SCRIPT,a3
        move.l  (a3)+,d7
        move.l  (a3)+,d5
        jsr     _ui_init
        bra.s   .next
.rec    move.w  (a3)+,d4
        addq.l  #2,a3
        lea     VIEW.w,a0
        moveq   #17,d0
.cp     move.l  (a3)+,(a0)+
        dbra    d0,.cp
        subq.w  #1,d4
.h      bsr.s   vwait
        pea     VIEW.w
        jsr     _ui_frame
        addq.l  #4,sp
        dbra    d4,.h
.next   subq.l  #1,d7
        bpl.s   .rec
.idle   bsr.s   vwait
        pea     VIEW.w
        jsr     _ui_frame
        addq.l  #4,sp
        subq.l  #1,d5
        bgt.s   .idle
        dc.w    $afff

t_preset:
        move.w  #$800f,$dff096
        move.w  #$8088,$dff09a
        dc.w    $afff

t_regs: moveq   #0,d0
        moveq   #0,d1
        move.w  $dff002,d0
        move.w  $dff01c,d1
        dc.w    $afff

vwait:  move.w  #$0020,INTREQ           ; VERTB: clear, then wait for the next
.w      move.w  INTREQR,d0
        and.w   #$0020,d0
        beq.s   .w
        rts

        include "ui.s"
