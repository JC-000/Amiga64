; fixtures/inp/inp_log.s — W13-I guest fixture (tests/run_inp.py).
;
; Each frame: clear INTREQ VERTB, poll INTREQR until VERTB is set again
; (the machine loop runs inp_frame once in the service pass that posts
; it, before the guest resumes), then log the
; three input registers the Amiga reads for a port-0 mouse:
;   +0 JOY0DAT ($DFF00A, word)   +2 CIA-A PRA ($BFE001, byte)   +3 0
;   +4 POTINP ($DFF016, word)
; FRAMES frames to LOG, then HALT ($AFFF, the runner's halt opcode).
; d3 = 1 if a VERTB never came (POLL polls).  Public register map: Amiga
; Hardware Reference Manual (custom chips, 8520 CIA-A).
; Build: vasmm68k_mot -Fbin -o inp_log.bin inp_log.s (org = the image base).

FRAMES  equ     16
POLL    equ     20000
LOG     equ     $20000
INTREQR equ     $dff01e
INTREQ  equ     $dff09c
JOY0DAT equ     $dff00a
POTINP  equ     $dff016
CIAAPRA equ     $bfe001

        org     $10000

start:  lea     LOG,a0
        moveq   #0,d3
        move.w  #FRAMES-1,d6
frame:  move.w  #$0020,INTREQ           ; VERTB clear
        move.w  #POLL-1,d4
wait:   move.w  INTREQR,d1
        andi.w  #$0020,d1
        bne.s   got
        dbra    d4,wait
        moveq   #1,d3
        dc.w    $afff
got:    move.w  JOY0DAT,(a0)+
        move.b  CIAAPRA,(a0)+
        clr.b   (a0)+
        move.w  POTINP,(a0)+
        dbra    d6,frame
        dc.w    $afff
