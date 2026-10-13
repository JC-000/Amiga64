; fixtures/mod/crt0.s — the player program's startup (W15-M).
;
; Bare metal, supervisor mode, no OS.  The host stages the image at
; $010000 (linked first: _start is the image base) and the reset longs at
; guest 0/4 (SSP = $010000, PC = _start); tests/aros/mboot.s aros_boot
; loads them, SR = $2700, and runs mach_run in exception mode.
;
; Here: clear the bss; point vectors 2-63 at numbered fault stubs (the
; status block gets the vector number, then HALT); install the three
; interrupt handlers (level 2 CIA-A, level 3 VBL, level 6 CIA-B, from
; replay.s); call main(); store its result and HALT ($AFFF, HALT_OK in
; exception mode too: mach_run returns).

        include "mod.i"

        section CODE,code

        xdef    _start
        xref    _main
        xref    l2_ciaa,l3_vbl,l6_ciab
        xref    __BSS_START,__BSS_END

_start: move.w  #$2700,sr
        lea     __BSS_START,a0
        lea     __BSS_END,a1
.clr:   cmp.l   a1,a0
        bhs.s   .vec
        clr.b   (a0)+
        bra.s   .clr
.vec:   lea     8.w,a0                  ; vectors 2..63
        lea     stubs+16(pc),a1
        moveq   #61,d0
.v:     move.l  a1,(a0)+
        addq.l  #8,a1
        dbra    d0,.v
        move.l  #l2_ciaa,$68.w          ; autovector 26: level 2
        move.l  #l3_vbl,$6c.w           ; 27: level 3
        move.l  #l6_ciab,$78.w          ; 30: level 6
        lea     STATUS,a0
        move.l  #'MSTA',S_MAGIC(a0)
        move.b  #1,S_STATE(a0)
        jsr     _main
        lea     STATUS,a0
        move.b  d0,S_ERR(a0)
        beq.s   .ok
        move.b  #3,S_STATE(a0)
.ok:    dc.w    $afff                   ; HALT: mach_run returns
        bra.s   .ok

; vector n -> stubs + 8n: push n, report it
stubs:
n       set     0
        rept    64
        move.w  #n,-(sp)
        bra.w   fault
n       set     n+1
        endr

fault:  lea     STATUS,a0
        move.w  (sp)+,S_FAULT(a0)
        move.b  #4,S_STATE(a0)
        dc.w    $afff
        bra.s   fault
