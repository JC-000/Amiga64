; W16-D: the stand-in Workbench tool for the modwb e2e plumbing (until
; W16-X's fixtures/modwb program is merged).  An AmigaOS hunk executable
; (vasmm68k_mot -Fhunkexe); it does what the real player's takeover does,
; without music:
;   - a Workbench start: WaitPort/GetMsg the WBStartup message (a CLI start
;     runs too, without it);
;   - the state struct (W16-X's layout, the fields a stand-in has: 'MWBS',
;     version 1, +6 state, +7 why, +56 frames) and the APP ANCHOR at
;     $2DC040 (supervisor ruling (c); host/profile_block.py's rules: +4 the
;     struct pointer, +8/+12 first, 'MWBA' last; published only when W16-P's
;     profile block at $2DC000 is valid ('A64P', $0001 0040, ~sum of the
;     longs +0..+56 at +60) -- or always with -DSTANDIN_NOPROF, for runs
;     before W16-P's launcher writes the block);
;   - Forbid, Disable, its own copper list in chip RAM (BPLCON0 = no planes,
;     COLOR00 = the flash colour) through COP1LC + COPJMP1; state 1;
;   - every frame (INTREQR VERTB, acknowledged): frames += 1, the colour
;     alternates every 16 frames; it leaves on a left-button press and
;     release (CIA-A PRA bit 6, /FIR0; why 1) or after MAXF frames (why 2);
;   - restore: COP1LC := GfxBase->gb_copinit, COPJMP1, Enable, Permit
;     (state 2); FreeMem, CloseLibrary (state 3); the anchor withdrawn
;     ('MWBA' cleared first); Forbid + ReplyMsg (the WBStartup message goes
;     back with the segment still loaded, the documented Workbench exit).
; Public offsets (RKRM Libraries / the exec and graphics include tables):
; exec LVOs Disable -120, Enable -126, Forbid -132, Permit -138, AllocMem
; -198, FreeMem -210, FindTask -294, GetMsg -372, ReplyMsg -378, WaitPort
; -384, CloseLibrary -414, OpenLibrary -552; Process pr_MsgPort +92,
; pr_CLI +172; GfxBase gb_copinit +38; MEMF_CHIP 2.  Custom chips: INTREQR
; $01E, COP1LC $080, COPJMP1 $088, INTREQ $09C.
; MUT_NORESTORE (red): the restore leaves our copper list.

SysBase         equ     4
    ifnd MAXF
MAXF            equ     6000            ; frames before it leaves by itself (-DMAXF=n)
    endif
PROF_AT         equ     $2dc000         ; W16-P's profile block
ANCHOR_AT       equ     $2dc040         ; the app anchor (ruling (c))

_LVODisable     equ     -120
_LVOEnable      equ     -126
_LVOForbid      equ     -132
_LVOPermit      equ     -138
_LVOAllocMem    equ     -198
_LVOFreeMem     equ     -210
_LVOFindTask    equ     -294
_LVOGetMsg      equ     -372
_LVOReplyMsg    equ     -378
_LVOWaitPort    equ     -384
_LVOCloseLib    equ     -414
_LVOOpenLib     equ     -552

pr_MsgPort      equ     92
pr_CLI          equ     172
gb_copinit      equ     38
CUSTOM          equ     $dff000
CIAAPRA         equ     $bfe001

; the state struct (W16-X's A_* offsets)
A_MAGIC         equ     0
A_VER           equ     4
A_STATE         equ     6
A_WHY           equ     7
A_FRAMES        equ     56
A_SIZE          equ     60

        section code,code
start:
        move.l  SysBase.w,a6
        lea     vars(pc),a5
        sub.l   a1,a1
        jsr     _LVOFindTask(a6)
        move.l  d0,a4                   ; our process
        clr.l   wbmsg-vars(a5)
        tst.l   pr_CLI(a4)
        bne.s   .cli
        lea     pr_MsgPort(a4),a0
        jsr     _LVOWaitPort(a6)
        lea     pr_MsgPort(a4),a0
        jsr     _LVOGetMsg(a6)
        move.l  d0,wbmsg-vars(a5)
.cli:
        bsr     anchor_init             ; state 0, published
        lea     gfxname(pc),a1
        moveq   #0,d0
        jsr     _LVOOpenLib(a6)
        move.l  d0,gfxbase-vars(a5)
        beq     .gone
        moveq   #CLSIZE,d0
        moveq   #2,d1                   ; MEMF_CHIP
        jsr     _LVOAllocMem(a6)
        move.l  d0,cl-vars(a5)
        beq     .close
        move.l  d0,a0                   ; the copper list into chip RAM
        lea     clsrc(pc),a1
        moveq   #CLSIZE/2-1,d1
.cp:    move.w  (a1)+,(a0)+
        dbf     d1,.cp
        lea     mwst(pc),a2
        jsr     _LVOForbid(a6)
        jsr     _LVODisable(a6)
        lea     CUSTOM,a3
        move.l  cl-vars(a5),$080(a3)
        move.w  d0,$088(a3)             ; COPJMP1 strobe
        move.b  #1,A_STATE(a2)          ; taken over
        moveq   #0,d6                   ; button seen down
.frame:
        move.w  $01e(a3),d0
        btst    #5,d0                   ; VERTB
        beq.s   .btn
        move.w  #$0020,$09c(a3)
        addq.l  #1,A_FRAMES(a2)
        move.l  A_FRAMES(a2),d0
        move.l  cl-vars(a5),a0
        move.w  #$0f00,6(a0)            ; COLOR00 red ...
        btst    #4,d0
        beq.s   .col
        move.w  #$000f,6(a0)            ; ... or blue, every 16 frames
.col:   cmp.l   #MAXF,d0
        blo.s   .btn
        move.b  #2,A_WHY(a2)            ; the "song end"
        bra.s   .out
.btn:   btst    #6,CIAAPRA              ; /FIR0: 0 = pressed
        bne.s   .up
        moveq   #1,d6
        bra.s   .frame
.up:    tst.l   d6
        beq.s   .frame
        move.b  #1,A_WHY(a2)            ; LMB
.out:
    ifnd MUT_NORESTORE
        move.l  gfxbase-vars(a5),a0
        move.l  gb_copinit(a0),$080(a3)
        move.w  d0,$088(a3)
    endif
        jsr     _LVOEnable(a6)
        jsr     _LVOPermit(a6)
        move.b  #2,A_STATE(a2)          ; hardware restored
        move.l  cl-vars(a5),a1
        moveq   #CLSIZE,d0
        jsr     _LVOFreeMem(a6)
.close:
        move.l  gfxbase-vars(a5),a1
        jsr     _LVOCloseLib(a6)
.gone:
        lea     mwst(pc),a2
        move.b  #3,A_STATE(a2)          ; done
        bsr     anchor_end
        tst.l   wbmsg-vars(a5)
        beq.s   .ret
        jsr     _LVOForbid(a6)
        move.l  wbmsg-vars(a5),a1
        jsr     _LVOReplyMsg(a6)
.ret:   moveq   #0,d0
        rts

; anchor_init: the struct ('MWBS', version 1, the rest 0), then the anchor
; when the profile block is valid: +4, +8, +12 first, 'MWBA' last.
anchor_init:
        lea     mwst(pc),a0
        moveq   #A_SIZE/2-1,d0
.z:     clr.w   (a0)+
        dbf     d0,.z
        lea     mwst(pc),a0
        move.l  #'MWBS',A_MAGIC(a0)
        move.w  #1,A_VER(a0)
    ifnd STANDIN_NOPROF
        bsr     prof_ok
        beq.s   .r
    endif
        st      profok-vars(a5)
        lea     ANCHOR_AT,a1
        move.l  a0,4(a1)
        clr.l   8(a1)
        clr.l   12(a1)
        move.l  #'MWBA',(a1)            ; last
.r:     rts

; anchor_end: 'MWBA' cleared first (nothing else is written after it).
anchor_end:
        tst.b   profok-vars(a5)
        beq.s   .r
        clr.l   ANCHOR_AT
        clr.b   profok-vars(a5)
.r:     rts

    ifnd STANDIN_NOPROF
; prof_ok: Z clear when the profile block is valid: 'A64P', $00010040, and
; the long at +60 is the complement of the sum of the longs +0..+56.
prof_ok:
        movem.l d0-d1/a0,-(sp)
        lea     PROF_AT,a0
        cmp.l   #'A64P',(a0)
        bne.s   .no
        cmp.l   #$00010040,4(a0)
        bne.s   .no
        moveq   #0,d0
        moveq   #14,d1
.s:     add.l   (a0)+,d0
        dbf     d1,.s
        not.l   d0
        cmp.l   (a0),d0
        bne.s   .no
        movem.l (sp)+,d0-d1/a0
        moveq   #1,d1                   ; (Z clear)
        rts
.no:    movem.l (sp)+,d0-d1/a0
        moveq   #0,d1                   ; (Z set)
        rts
    endif

clsrc:  dc.w    $0100,$0200             ; BPLCON0: no planes, colour on
        dc.w    $0180,$0f00             ; COLOR00
        dc.w    $ffff,$fffe             ; end
CLSIZE  equ     *-clsrc

gfxname: dc.b   "graphics.library",0
        even
vars:
wbmsg:      dc.l    0
gfxbase:    dc.l    0
cl:         dc.l    0
profok:     dc.b    0
        even
mwst:       ds.b    A_SIZE              ; the state struct (even, in the hunk)
