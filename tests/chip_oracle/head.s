; tests/chip_oracle/head.s -- bare-metal frame of the chip-oracle ROM (W7-M).
;
; Our own 256 KB "Kickstart" (no Kickstart or AROS code at all): FS-UAE
; (A500, OCS, 512 KB chip) boots it at $FC0000 and runs the cases that
; tests/chip_oracle.py generates after this file (cases.s, included at the
; end).  Each case runs its register ops against the real (emulated) chips,
; stores every recorded read at $70000.. (a4), then prints over the serial
; port (serial_port = a file):
;     @<name>                  case header (after the ops: no serial I/O
;                              happens while a case runs)
;     R <w> <w> ...            recorded reads, hex words
;     D <addr> <hex bytes>     one memory dump
; and after the last case "END".  The CPU runs at IPL 7, except inside an
; IPL op (a case lowers the mask for a few NOPs): then the autovector
; handler irqN (vector 24+N) stores N in d5 and clears INTENA's master bit,
; so exactly one interrupt is taken.  Cross-check: if the SR mask the CPU
; set on entry is not N, d5 gets $80|N (a DIFF).
; During the 68000 interrupt acknowledge the vector number is read from
; the ROM's last 16 bytes ($FFFFF0 + 2*level); a Kickstart holds 24..31
; there, so build_rom (tests/chip_oracle.py) writes them at ROM offset
; $3FFF0 (w7-models-review REVIEW2 Q3, probe in r2/iack/).  Without them
; the IACK returns vector 0: vector 0 is therefore a trap that prints
; "!VECTOR0" and halts, so the run fails loudly (no END).
;
; Registers: a6 = $DFF000 always; a4 = record pointer; d7/d6 = loop
; counters; d5 = the level an IPL op took.

CUSTOM  equ     $dff000
CIAA    equ     $bfe001
CIAB    equ     $bfd000
RECBUF  equ     $70000
PRIMER  equ     $f000                   ; 16 zero bytes the primer blit reads

        org     $fc0000
rom:    dc.w    $1111,$4ef9             ; KS-style header: SSP (ignored), PC
        dc.l    start

start:  move.w  #$2700,sr
        lea     $7fffc,sp
        move.b  #3,CIAA+$200            ; DDRA: OVL and LED outputs
        move.b  #2,CIAA                 ; OVL off: chip RAM at 0
        lea     CUSTOM,a6
        move.w  #$7fff,$9a(a6)          ; INTENA off
        move.w  #$7fff,$96(a6)          ; DMACON off
        move.w  #$7fff,$9c(a6)          ; INTREQ clear
        move.w  #1,$32(a6)              ; SERPER: fastest
        lea     vec0(pc),a1             ; vector 0: the trap (chip RAM, OVL off)
        move.l  a1,0
        lea     $64,a0                  ; autovectors 25-30 = levels 1-6
        lea     irqtab(pc),a1
        moveq   #6-1,d0
.v:     move.l  (a1)+,(a0)+
        dbra    d0,.v
        lea     hello(pc),a0
        bsr     puts
        bsr     cases                   ; cases.s
        lea     endmsg(pc),a0
        bsr     puts
.halt:  bra.s   .halt

; ---- IPL ops: one interrupt, its level into d5 --------------------------------
irq1:   moveq   #1,d5
        bra.s   irqx
irq2:   moveq   #2,d5
        bra.s   irqx
irq3:   moveq   #3,d5
        bra.s   irqx
irq4:   moveq   #4,d5
        bra.s   irqx
irq5:   moveq   #5,d5
        bra.s   irqx
irq6:   moveq   #6,d5
irqx:   move.w  d0,-(sp)
        move.w  sr,d0
        lsr.w   #8,d0
        and.w   #7,d0                   ; the mask the CPU set = the level taken
        cmp.w   d0,d5
        beq.s   .ok
        or.w    #$80,d5                 ; vector and level disagree
.ok:    move.w  (sp)+,d0
        move.w  #$4000,$9a(a6)          ; INTENA: master off
        rte
irqtab: dc.l    irq1,irq2,irq3,irq4,irq5,irq6

vec0:   lea     vec0msg(pc),a0          ; an interrupt through vector 0: the
        bsr     puts                    ; ROM's IACK bytes are missing
.h:     bra.s   .h
vec0msg: dc.b   10,"!VECTOR0",10,0
        even

; ---- serial output ---------------------------------------------------------
putc:                                   ; d0.b
.w:     btst    #13-8,$18(a6)           ; SERDATR TBE
        beq.s   .w
        move.w  d0,-(sp)
        and.w   #$ff,d0
        or.w    #$100,d0                ; stop bit
        move.w  d0,$30(a6)
        move.w  (sp)+,d0
        rts

puts:                                   ; a0 = NUL-terminated string
        move.l  d0,-(sp)
.l:     move.b  (a0)+,d0
        beq.s   .e
        bsr     putc
        bra.s   .l
.e:     move.l  (sp)+,d0
        rts

nibble: and.w   #$f,d0
        move.b  hexdig(pc,d0.w),d0
        bra     putc

hex8:                                   ; d1.b
        move.w  d1,d0
        lsr.w   #4,d0
        bsr     nibble
        move.w  d1,d0
        bra     nibble

hex16:                                  ; d1.w
        move.w  d1,-(sp)
        lsr.w   #8,d1
        bsr     hex8
        move.w  (sp)+,d1
        bra     hex8

hex24:                                  ; d1.l (low 24 bits)
        move.l  d1,-(sp)
        swap    d1
        bsr     hex8
        move.l  (sp)+,d1
        bra     hex16

hexdig: dc.b    "0123456789ABCDEF"
        even

; ---- per-case output ---------------------------------------------------------
records:                                ; "R w w w..." for RECBUF..a4
        moveq   #'R',d0
        bsr     putc
        lea     RECBUF,a0
.l:     cmp.l   a4,a0
        bhs.s   .e
        moveq   #' ',d0
        bsr     putc
        move.w  (a0)+,d1
        bsr     hex16
        bra.s   .l
.e:     moveq   #10,d0
        bra     putc

dump:                                   ; a0 = address, d2.w = byte count
        moveq   #'D',d0
        bsr     putc
        moveq   #' ',d0
        bsr     putc
        move.l  a0,d1
        bsr     hex24
        moveq   #' ',d0
        bsr     putc
        subq.w  #1,d2
.l:     move.b  (a0)+,d1
        bsr     hex8
        dbra    d2,.l
        moveq   #10,d0
        bra     putc

; ---- state between cases -------------------------------------------------------
; The model starts every case from a fresh chipset_ref.Chipset with DMACON
; = DMAEN|BLTEN; this puts the real chips into the same state.
bwait:  btst    #6,$2(a6)               ; first read: Agnus BBUSY quirk
.w:     btst    #6,$2(a6)               ; DMACONR bit 14 = BBUSY
        bne.s   .w
        rts

blitzero:                               ; every blitter register but BLTSIZE := 0
        moveq   #0,d0
        lea     $40(a6),a0
        moveq   #($58-$40)/2-1,d1
.a:     move.w  d0,(a0)+
        dbra    d1,.a
        lea     $60(a6),a0
        moveq   #4-1,d1
.b:     move.w  d0,(a0)+
        dbra    d1,.b
        lea     $70(a6),a0
        moveq   #3-1,d1
.c:     move.w  d0,(a0)+
        dbra    d1,.c
        rts

ciareset:                               ; a0 = CIA base (register r at r*$100)
        move.b  #0,$e00(a0)             ; CRA: stop, continuous, phi2
        move.b  #0,$f00(a0)             ; CRB
        move.b  #$7f,$d00(a0)           ; ICR: every mask bit off
        move.b  #$ff,$400(a0)           ; latches $FFFF; stopped: counters too
        move.b  #$ff,$500(a0)
        move.b  #$ff,$600(a0)
        move.b  #$ff,$700(a0)
        tst.b   $d00(a0)                ; ICR data read-clear
        rts

prologue:
        move.w  #$7fff,$9a(a6)
        move.w  #$7fff,$96(a6)
        lea     CIAA,a0
        bsr     ciareset
        lea     CIAB,a0
        bsr     ciareset
        bsr     blitzero
        ; primer: a 1x1 A,B->D blit of zeros, so the A/B "old" words the
        ; shifter feeds into the first word are 0, as in the model
        clr.l   PRIMER
        clr.l   PRIMER+4
        move.w  #$8240,$96(a6)          ; DMAEN | BLTEN
        move.w  #$0df0,$40(a6)
        move.w  #$ffff,$44(a6)
        move.w  #$ffff,$46(a6)
        move.l  #PRIMER,$50(a6)
        move.l  #PRIMER,$4c(a6)
        move.l  #PRIMER+8,$54(a6)
        move.w  #(1<<6)|1,$58(a6)
        bsr     bwait
        bsr     blitzero
        move.w  #$7fff,$9c(a6)          ; INTREQ clear (BLIT, TBE, ...)
        lea     RECBUF,a4
        rts

; copy d2.w bytes from a0 to a1
copy:   subq.w  #1,d2
.l:     move.b  (a0)+,(a1)+
        dbra    d2,.l
        rts

hello:  dc.b    "CHIPORACLE 1",10,0
endmsg: dc.b    "END",10,0
        even

        include "cases.s"
