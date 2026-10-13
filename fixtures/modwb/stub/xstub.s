; fixtures/modwb/stub/xstub.s — leg S's exec stub (W16-X): a minimal,
; strict stand-in for the AmigaOS calls the launcher makes, written from the
; same public interface descriptions as ../os.i (no OS source).
;
; Flat image at $010000 (vasm -Fbin), started by the reset longs the host
; stages at 0/4 (SSP $3000, PC $010000), in exception mode, like the W15
; player.  The host also stages the launcher's hunks, relocated as LoadSeg
; would (tests/run_modwb.py), the MOD "file" at XFILE and the parameter
; block XP.  The stub
;   1. builds the OS: library bases with JMP tables (every other slot of
;      the first 150 is a JSR to `unimp`, which halts and names the slot),
;      SysBase at 4, a process (pr_CLI 0 and a queued WBStartup, or a
;      shell start), a GfxBase whose copinit is the stub's copper list, the
;      two CIA resources;
;   2. sets the "OS" hardware state: its own level 2/3/6 handlers, CIA-A
;      timer A and CIA-B timer B running continuously and owned by the
;      "OS" (the resources refuse them, as AROS does: R1 measurement in
;      build/evidence/w16-x/PROGRESS.md), COP1LC on its copper list,
;      DMACON DMAEN|COPEN|BLTEN, INTENA INTEN|EXTER|VERTB|PORTS;
;   3. drops to user mode and calls the launcher (jsr entry);
;   4. on the return, counts VERTBs and CIA-A TA interrupts for XWAIT
;      frames in user mode (the OS's interrupts must work again), traps to
;      supervisor, records the hardware it can read, and HALTs ($AFFF).
; Every call is logged at XLOG (lib.w, lvo.w, d0, d1, a0, a1, a6); the host
; checks the order and the balance (tests/run_modwb.py).
;
; Mutant/variant switches: none here (the launcher's MW_MUT_* are the
; mutants).

        org     $010000

CUSTOM          equ     $dff000
INTENAR         equ     $01c
DMACONR         equ     $002
DMACON          equ     $096
INTENA          equ     $09a
INTREQ          equ     $09c
COP1LC          equ     $080
COPJMP1         equ     $088
CIAA_ICR        equ     $bfed01
CIAA_TALO       equ     $bfe401
CIAA_TAHI       equ     $bfe501
CIAA_CRA        equ     $bfee01
CIAA_TODLO      equ     $bfe801
CIAA_PRA        equ     $bfe001
ANCHOR          equ     $2dc040         ; the app anchor (bit 7's foreign one)
FOREIGN         equ     $0a0000         ; ... and its pointer
CIAA_TBLO       equ     $bfe601         ; W16-E
CIAA_TBHI       equ     $bfe701         ; W16-E
CIAA_CRB        equ     $bfef01         ; W16-E
CIAB_ICR        equ     $bfdd00
CIAB_TBLO       equ     $bfd600
CIAB_TBHI       equ     $bfd700
CIAB_CRB        equ     $bfdf00

; host -> stub (tests/run_modwb.py mirrors every offset)
XP              equ     $000500
XP_MAGIC        equ     0               ; 'XSTP'
XP_ENTRY        equ     4               ; l the launcher's entry (its first code hunk)
XP_MODE         equ     8               ; b 0 Workbench, 1 shell, 2 Workbench with a project (arg 1)
XP_REFUSE       equ     9               ; b bits 0-3 AddICRVector ciaa TA/TB, ciab TA/TB
                                        ;   (ciaa TA is always the OS's); bit 4 audio.device;
                                        ;   bit 5 intuition.library; bit 6 AllocAbs (the trace
                                        ;   RAM in use); bit 7: RethinkDisplay publishes a
                                        ;   foreign anchor (another writer, after the restore)
XP_WAIT         equ     10              ; w frames counted after the return
XP_FILE         equ     12              ; l the file's address
XP_FLEN         equ     16              ; l its length
XP_FNAME        equ     20              ; 32 b the only name Open serves
XP_TOOL         equ     52              ; 32 b the tool's name (WBArg 0)
XP_ARG1         equ     84              ; 32 b the project's name (mode 2)
XP_CLI          equ     116             ; 32 b the shell's argument line (mode 1)
XP_ATB          equ     148             ; b W16-E: nonzero: CIA-A TB runs free before the call
                                        ;   (latch $FFFF, continuous, enabled, no resource
                                        ;   owner: as AROS, build/evidence/w16-e2e diag3/4)

; stub -> host
XS              equ     $000600
XS_MAGIC        equ     0               ; 'XSTS'
XS_PHASE        equ     4               ; b 1 OS built, 2 launcher called, 3 returned, 4 done
XS_FAULT        equ     5               ; b nonzero: an exception (vector at XS_VEC)
XS_VEC          equ     6               ; w
XS_UNIMP        equ     8               ; l the unimplemented slot called (its address + 6)
XS_RET          equ     12              ; l the launcher's d0
XS_RSR          equ     16              ; w SR at the return (from the trap frame)
XS_IDNEST       equ     18              ; b Disable nesting after the return
XS_TDNEST       equ     19              ; b Forbid nesting
XS_INTENAR      equ     20              ; w before (pre) ...
XS_DMACONR      equ     22              ; w
XS_INTENAR2     equ     24              ; w ... and after
XS_DMACONR2     equ     26              ; w
XS_MASKA        equ     28              ; b ciaa resource enable mask (shadow) after
XS_MASKB        equ     29              ; b
XS_VBL          equ     30              ; w VERTBs counted in the wait after the return
XS_TA           equ     32              ; w CIA-A TA interrupts in that wait
XS_WTO          equ     34              ; b a WaitTOF/wait timed out (interrupts dead)
XS_OWNBL        equ     35              ; b OwnBlitter - DisownBlitter
XS_LIBS         equ     36              ; b libraries open (OpenLibrary - CloseLibrary)
XS_ALLOCS       equ     37              ; b allocations outstanding
XS_FREEBAD      equ     38              ; b a FreeMem that matched no allocation
XS_REPLIED      equ     39              ; b ReplyMsg calls (of the WBStartup)
XS_REPFORB      equ     40              ; b Forbid nesting at the ReplyMsg
XS_DEVOPEN      equ     41              ; b audio.device opens outstanding
XS_FILEOPEN     equ     42              ; b files open
XS_ICROWN       equ     43              ; b bits 0-3: ICR vectors still owned by the launcher
XS_LOGP         equ     44              ; l the log's end
XS_SSP          equ     48              ; l SSP after the return (the trap's)
XS_USP          equ     52              ; l USP at the return
XS_CURDIR       equ     56              ; l the current dir after the return
XS_EASY         equ     60              ; l EasyRequestArgs' text pointer (0 = none)
XS_OPENNAME     equ     64              ; 32 b the last name Open was asked for
XS_SVEC         equ     96              ; 7 l the autovectors $64-$7C before the call
XS_TB           equ     124             ; w CIA-B TB interrupts in the wait (the OS's other timer)
XS_CRB0         equ     126             ; b CIA-B CRB before the call ...
XS_CRB1         equ     127             ; b ... and after
XS_TOD0         equ     128             ; b CIA-A TODLO read alone right after the return ...
XS_TOD1         equ     129             ; b ... and after the wait: a TOD latch the launcher
                                        ; left held makes the first one stale
XS_LMBEN        equ     130             ; b the left button was down at the last Enable (the
                                        ; OS would see a click the launcher's exit made)
XS_EASYARG      equ     132             ; l EasyRequestArgs' first argument (a3's first long)
XS_ACRB0        equ     136             ; b W16-E: CIA-A CRB before the call ...
XS_ACRB1        equ     137             ; b ... and after

XLOG            equ     $005000         ; log entries, 24 bytes each
XLOG_END        equ     $010000
XSTACK_S        equ     $003000         ; supervisor stack top (reset SSP)
XSTACK_U        equ     $005000         ; the task's user stack top
OSB             equ     $020000         ; the OS structures
HEAP            equ     $100000         ; AllocMem's chip heap
HEAP_END        equ     $1c0000
NSLOT           equ     150

SYSBASE         equ     OSB+$1000
DOSBASE         equ     OSB+$2000
GFXBASE         equ     OSB+$3000
INTBASE         equ     OSB+$4000
CIAABASE        equ     OSB+$5000
CIABBASE        equ     OSB+$5800
AUDDEV          equ     OSB+$6000
TASK            equ     OSB+$7000
WBMSG           equ     OSB+$7400
WBARGS          equ     OSB+$7480
COPLIST         equ     OSB+$7800

LIB_EXEC        equ     1
LIB_DOS         equ     2
LIB_GFX         equ     3
LIB_INT         equ     4
LIB_CIAA        equ     5
LIB_CIAB        equ     6

LOG     macro                           ; lib, lvo: one entry, all registers kept
        move.l  a2,-(sp)
        move.l  logp,a2
        cmp.l   #XLOG_END-24,a2
        bhs     .lg\@
        move.w  #\1,(a2)+
        move.w  #\2,(a2)+
        move.l  d0,(a2)+
        move.l  d1,(a2)+
        move.l  a0,(a2)+
        move.l  a1,(a2)+
        move.l  a6,(a2)+
        move.l  a2,logp
        move.l  a2,XS+XS_LOGP
.lg\@:  move.l  (sp)+,a2
        endm

; ===========================================================================
start:  move.w  #$2700,sr
        lea     XS,a0
        moveq   #136/4-1,d0             ; (XS_SVEC is written before the call)
.cs:    clr.l   (a0)+
        dbra    d0,.cs
        move.l  #'XSTS',XS+XS_MAGIC
        move.l  #XLOG,logp
        move.l  #XLOG,XS+XS_LOGP
        cmp.l   #'XSTP',XP+XP_MAGIC
        beq     .pm
        move.b  #$ff,XS+XS_FAULT
        dc.w    $afff
.pm:
        ; vectors 2..63: numbered faults; then the OS's own
        lea     8.w,a0
        lea     faults+16,a1
        moveq   #61,d0
.v:     move.l  a1,(a0)+
        addq.l  #8,a1
        dbra    d0,.v
        move.l  #os_l2,$68.w
        move.l  #os_l3,$6c.w
        move.l  #os_l6,$78.w
        move.l  #super_trap,$bc.w       ; trap #15: SuperState
        move.l  #post_trap,$b8.w        ; trap #14: the stub's own post checks
        ; library tables
        lea     libtab,a3
.lt:    move.l  (a3)+,d0                ; base
        beq     .ltd
        move.l  d0,a0
        move.w  #NSLOT-1,d1
.ls:    subq.l  #6,a0                   ; every slot: jsr unimp
        move.w  #$4eb9,(a0)
        move.l  #unimp,2(a0)
        dbra    d1,.ls
.li:    move.w  (a3)+,d1                ; then (lvo, routine) pairs, 0 ends
        beq     .lt
        move.l  d0,a0
        add.w   d1,a0
        move.w  #$4ef9,(a0)
        move.l  (a3)+,2(a0)
        bra     .li
.ltd:   move.l  #SYSBASE,4.w
        ; the process, its WBStartup, the args
        lea     TASK,a0
        move.w  #$1000/4-1,d0
.ct:    clr.l   (a0)+
        dbra    d0,.ct
        moveq   #0,d0
        cmp.b   #1,XP+XP_MODE
        bne     .wb
        move.l  #$00cc0001,TASK+172     ; pr_CLI: a shell start
        bra     .msg
.wb:    st      msgpend
        move.l  #WBARGS,WBMSG+36        ; sm_ArgList
        moveq   #1,d0
        cmp.b   #2,XP+XP_MODE
        bne     .na
        moveq   #2,d0
.na:    move.l  d0,WBMSG+28             ; sm_NumArgs
        move.l  #$00001111,WBARGS       ; wa_Lock (the tool's drawer)
        move.l  #XP+XP_TOOL,WBARGS+4
        move.l  #$00002222,WBARGS+8     ; arg 1: the project's drawer
        move.l  #XP+XP_ARG1,WBARGS+12
.msg:   move.l  #$00000100,curdir       ; the start's current dir
        ; GfxBase
        move.l  #$00ee0000,GFXBASE+34   ; ActiView: the OS's view (a token)
        move.l  #COPLIST,GFXBASE+38     ; copinit
        lea     COPLIST,a0
        move.l  #$01800123,(a0)+        ; COLOR00
        move.l  #$fffffffe,(a0)+
        move.l  #HEAP,heapp
        ; the "OS" hardware state
        lea     CUSTOM,a5
        move.w  #$7fff,INTENA(a5)
        move.w  #$7fff,INTREQ(a5)
        move.w  #$7fff,DMACON(a5)
        move.l  #COPLIST,COP1LC(a5)
        move.w  d0,COPJMP1(a5)
        move.w  #$82c0,DMACON(a5)       ; DMAEN COPEN BLTEN
        move.b  #$7f,CIAA_ICR
        move.b  #$7f,CIAB_ICR
        move.b  #5000&$ff,CIAA_TALO
        move.b  #5000>>8,CIAA_TAHI
        move.b  #$11,CIAA_CRA           ; continuous, start, load
        move.b  #$81,CIAA_ICR           ; TA: the OS's timer
        move.b  #$01,maska
        move.l  #os_ta,owna             ; owned by the OS
        move.b  #7000&$ff,CIAB_TBLO     ; CIA-B TB: the OS's other timer
        move.b  #7000>>8,CIAB_TBHI
        move.b  #$11,CIAB_CRB           ; continuous, start, load
        move.b  #$82,CIAB_ICR
        move.b  #$02,maskb
        move.l  #os_tb,ownb+4
        move.b  CIAB_CRB,XS+XS_CRB0
        tst.b   XP+XP_ATB               ; W16-E
        beq     .natb                   ; W16-E
        move.b  #$ff,CIAA_TBLO          ; W16-E: AROS's free-running counter
        move.b  #$ff,CIAA_TBHI          ; W16-E
        move.b  #$11,CIAA_CRB           ; W16-E: continuous, start, load
        move.b  #$82,CIAA_ICR           ; W16-E: enabled, no owner
        or.b    #$02,maska              ; W16-E
.natb:  move.b  CIAA_CRB,XS+XS_ACRB0    ; W16-E
        move.w  #$e028,INTENA(a5)       ; INTEN EXTER VERTB PORTS
        lea     $64.w,a0                ; the OS's autovectors, for the host's check
        lea     XS+XS_SVEC,a1
        moveq   #6,d0
.sv:    move.l  (a0)+,(a1)+
        dbra    d0,.sv
        move.w  INTENAR(a5),XS+XS_INTENAR
        move.w  DMACONR(a5),XS+XS_DMACONR
        move.b  #1,XS+XS_PHASE
        ; user mode, then the launcher
        move.l  #XSTACK_U,a0
        move.l  a0,usp
        move.w  #$0000,sr
        move.b  #2,XS+XS_PHASE
        move.l  XP+XP_ENTRY,a1
        lea     XP+XP_CLI,a0
        moveq   #0,d0
.cl:    tst.b   0(a0,d0.w)
        beq     .cle
        addq.w  #1,d0
        bra     .cl
.cle:   jsr     (a1)
        move.l  d0,XS+XS_RET
        move.b  #3,XS+XS_PHASE
        move.l  sp,XS+XS_USP
        move.l  curdir,XS+XS_CURDIR
        move.b  CIAA_TODLO,XS+XS_TOD0   ; (releases a latch, as the OS's read would)
        ; the OS's interrupts must work again: XP_WAIT VERTBs, counting TA
        move.w  tacount,d5
        move.w  tbcount,d3
        moveq   #0,d6
        move.w  XP+XP_WAIT,d6
        move.w  vblcount,d7
.w:     move.l  #300000,d4
.w1:    cmp.w   vblcount,d7
        bne     .w2
        subq.l  #1,d4
        bne     .w1
        st      XS+XS_WTO
        bra     .w3
.w2:    move.w  vblcount,d7
        addq.w  #1,XS+XS_VBL
        subq.w  #1,d6
        bne     .w
.w3:    move.b  CIAA_TODLO,XS+XS_TOD1
        move.w  tacount,d0
        sub.w   d5,d0
        move.w  d0,XS+XS_TA
        move.w  tbcount,d0
        sub.w   d3,d0
        move.w  d0,XS+XS_TB
        trap    #14                     ; -> post_trap (supervisor)

post_trap:
        move.w  (sp),XS+XS_RSR          ; the SR the return ran with (user mode)
        lea     CUSTOM,a5
        move.w  INTENAR(a5),XS+XS_INTENAR2
        move.w  DMACONR(a5),XS+XS_DMACONR2
        move.b  idnest,XS+XS_IDNEST
        move.b  tdnest,XS+XS_TDNEST
        move.b  maska,XS+XS_MASKA
        move.b  maskb,XS+XS_MASKB
        move.b  ownblit,XS+XS_OWNBL
        move.b  libsopen,XS+XS_LIBS
        move.b  nallocs,XS+XS_ALLOCS
        move.b  devopen,XS+XS_DEVOPEN
        move.b  fileopen,XS+XS_FILEOPEN
        moveq   #0,d0                   ; ICR vectors the launcher still owns
        lea     owna,a0
        moveq   #3,d1
.o:     move.l  (a0)+,d2
        beq     .on
        cmp.l   #os_ta,d2
        beq     .on
        cmp.l   #os_tb,d2
        beq     .on
        bset    d1,d0
.on:    dbra    d1,.o
        move.b  d0,XS+XS_ICROWN         ; (bit 3 = owna[0] ... bit 0 = ownb[1])
        move.b  CIAB_CRB,XS+XS_CRB1
        move.b  CIAA_CRB,XS+XS_ACRB1    ; W16-E
        move.l  sp,XS+XS_SSP
        move.b  #4,XS+XS_PHASE
        dc.w    $afff
        bra     post_trap

; ---------------------------------------------------------------------------
; the OS's interrupt handlers
; ---------------------------------------------------------------------------
os_l3:  move.w  #$0020,CUSTOM+INTREQ
        addq.w  #1,vblcount
        rte

os_l2:  movem.l d0-d2/a0-a2,-(sp)
        move.b  CIAA_ICR,d2             ; the flags (cleared by the read)
        move.w  #$0008,CUSTOM+INTREQ
        and.b   maska,d2
        lea     owna,a2
        bsr     dispatch
        movem.l (sp)+,d0-d2/a0-a2
        rte

os_l6:  movem.l d0-d2/a0-a2,-(sp)
        move.b  CIAB_ICR,d2
        move.w  #$2000,CUSTOM+INTREQ
        and.b   maskb,d2
        lea     ownb,a2
        bsr     dispatch
        movem.l (sp)+,d0-d2/a0-a2
        rte

; dispatch: d2 = enabled flags, a2 = the owners (TA, TB)
dispatch:
        btst    #0,d2
        beq     .b
        move.l  (a2),d0
        beq     .b
        bsr     .call
.b:     btst    #1,d2
        beq     .r
        move.l  4(a2),d0
        beq     .r
.call:  move.l  d0,a1
        cmp.l   #os_ta,d0
        bne     .itb
        addq.w  #1,tacount
        rts
.itb:   cmp.l   #os_tb,d0
        bne     .ic
        addq.w  #1,tbcount
        rts
.ic:    movem.l d2/a2-a6,-(sp)
        move.l  18(a1),a0               ; is_Code
        move.l  14(a1),a1               ; is_Data
        jsr     (a0)
        movem.l (sp)+,d2/a2-a6
.r:     rts

os_ta:  rts                             ; (tokens: the OS's own timer users)
os_tb:  rts

; trap #15: SuperState's way in.  Returns to the caller in supervisor mode
; on its own (user) stack; d0 = the supervisor stack to give back.
super_trap:
        move.w  (sp)+,d1
        move.l  (sp)+,a0
        move.l  sp,d0
        move.l  usp,a1
        move.l  a1,sp
        or.w    #$2000,d1
        move.w  d1,sr
        jmp     (a0)

unimp:  move.l  (sp)+,XS+XS_UNIMP
        move.w  #$2700,sr
        dc.w    $afff
        bra     unimp

faults:
n       set     0
        rept    64
        move.w  #n,-(sp)
        bra.w   fault
n       set     n+1
        endr
fault:  move.w  (sp)+,XS+XS_VEC
        st      XS+XS_FAULT
        dc.w    $afff
        bra     fault

; ---------------------------------------------------------------------------
; exec
; ---------------------------------------------------------------------------
x_disable:
        LOG     LIB_EXEC,-120
        move.w  #$4000,CUSTOM+INTENA
        addq.b  #1,idnest
        rts
x_enable:
        LOG     LIB_EXEC,-126
        subq.b  #1,idnest
        bne     .r
        sf      XS+XS_LMBEN
        btst    #6,CIAA_PRA             ; /FIR0 low: the button down
        bne     .u
        st      XS+XS_LMBEN
.u:     move.w  #$c000,CUSTOM+INTENA
.r:     rts
x_forbid:
        LOG     LIB_EXEC,-132
        addq.b  #1,tdnest
        rts
x_permit:
        LOG     LIB_EXEC,-138
        subq.b  #1,tdnest
        rts
x_superstate:
        LOG     LIB_EXEC,-150
        trap    #15
        rts
x_userstate:
        LOG     LIB_EXEC,-156
        move.l  (sp)+,a0
        move.l  sp,a1
        move.l  a1,usp
        move.l  d0,sp
        and.w   #$dfff,sr
        jmp     (a0)
x_allocmem:                             ; d0 size, d1 requirements
        LOG     LIB_EXEC,-198
        bra     alloc
x_freemem:                              ; a1 memory, d0 size
        LOG     LIB_EXEC,-210
        bra     free
x_allocabs:                             ; d0 size, a1 location: only inside the
        LOG     LIB_EXEC,-204           ; stub's "slow RAM" $C00000-$DC0000 (the
        movem.l d1/a0,-(sp)             ; trace), recorded like AllocMem's
        btst    #6,XP+XP_REFUSE
        bne     .no
        cmp.l   #$c00000,a1
        blo     .no
        move.l  a1,d1
        add.l   d0,d1
        cmp.l   #$dc0000,d1
        bhi     .no
        lea     atab,a0
        moveq   #31,d1
.s:     tst.l   (a0)
        beq     .f
        addq.l  #8,a0
        dbra    d1,.s
        bra     .no
.f:     move.l  a1,(a0)+
        move.l  d0,(a0)
        addq.b  #1,nallocs
        move.l  a1,d0
        bra     .x
.no:    moveq   #0,d0
.x:     movem.l (sp)+,d1/a0
        rts
x_findtask:
        LOG     LIB_EXEC,-294
        move.l  #TASK,d0
        cmpa.w  #0,a1
        beq     .r
        moveq   #0,d0
.r:     rts
x_waitport:
        LOG     LIB_EXEC,-384
        move.l  #WBMSG,d0
        tst.b   msgpend
        bne     .r
        st      XS+XS_WTO               ; waiting on an empty port: a hang
        moveq   #0,d0
.r:     rts
x_getmsg:
        LOG     LIB_EXEC,-372
        moveq   #0,d0
        cmp.l   #TASK+92,a0
        bne     .r
        tst.b   msgpend
        beq     .r
        sf      msgpend
        move.l  #WBMSG,d0
.r:     rts
x_replymsg:
        LOG     LIB_EXEC,-378
        cmp.l   #WBMSG,a1
        bne     .r
        addq.b  #1,XS+XS_REPLIED
        move.b  tdnest,XS+XS_REPFORB
.r:     rts
x_closelib:
        LOG     LIB_EXEC,-414
        subq.b  #1,libsopen
        rts
x_openlib:                              ; a1 name, d0 version
        LOG     LIB_EXEC,-552
        movem.l a0/a2,-(sp)
        lea     names_lib,a2
        bra     findname
x_openres:
        LOG     LIB_EXEC,-498
        movem.l a0/a2,-(sp)
        lea     names_res,a2
findname:                               ; a2: (name, base, refuse bit) ...; a1 the name
.n:     move.l  (a2)+,d0
        beq     .no
        move.l  d0,a0
        move.l  a1,-(sp)
.c:     cmpm.b  (a0)+,(a1)+
        bne     .ne
        tst.b   -1(a0)
        bne     .c
        move.l  (sp)+,a1
        move.l  (a2)+,d0
        move.w  (a2)+,d1
        bmi     .ok
        btst    d1,XP+XP_REFUSE
        bne     .no
.ok:    cmp.l   #CIAABASE,d0
        bhs     .x
        addq.b  #1,libsopen
        bra     .x
.ne:    move.l  (sp)+,a1
        addq.l  #6,a2
        bra     .n
.no:    moveq   #0,d0
.x:     movem.l (sp)+,a0/a2
        rts
x_opendevice:                           ; a0 name, d0 unit, a1 ioreq, d1 flags
        LOG     LIB_EXEC,-444
        movem.l a0/a2,-(sp)
        lea     n_aud,a2
.c:     cmpm.b  (a0)+,(a2)+
        bne     .no
        tst.b   -1(a2)
        bne     .c
        btst    #4,XP+XP_REFUSE
        bne     .no
        move.l  34(a1),a0               ; ioa_Data: the allocation map
        cmp.b   #$0f,(a0)
        bne     .no
        move.l  #AUDDEV,20(a1)          ; io_Device
        clr.b   31(a1)
        addq.b  #1,devopen
        moveq   #0,d0
        bra     .x
.no:    move.b  #-1,31(a1)              ; IOERR_OPENFAIL
        moveq   #-1,d0
.x:     movem.l (sp)+,a0/a2
        rts
x_closedevice:
        LOG     LIB_EXEC,-450
        cmp.l   #AUDDEV,20(a1)
        bne     .r
        subq.b  #1,devopen
        clr.l   20(a1)
.r:     rts
x_createport:
        LOG     LIB_EXEC,-666
        moveq   #34,d0
        move.l  #$10001,d1              ; MEMF_PUBLIC|MEMF_CLEAR
        bra     alloc
x_deleteport:
        LOG     LIB_EXEC,-672
        move.l  a0,a1
        moveq   #34,d0
        bra     free

; alloc: d0 size, d1 requirements -> d0 memory (8-byte aligned bump),
; recorded; MEMF_CLEAR (bit 16) zeroes it, else it is filled with $A5
alloc:  movem.l d1-d3/a0-a1,-(sp)
        moveq   #0,d3
        btst    #16,d1
        bne     .cl
        moveq   #-$5b,d3                ; $A5
.cl:    move.l  d0,d2
        beq     .no
        move.l  heapp,a0
        move.l  a0,d1
        add.l   d0,d1
        addq.l  #7,d1
        and.w   #$fff8,d1
        cmp.l   #HEAP_END,d1
        bhi     .no
        move.l  d1,heapp
        move.l  a0,a1
.z:     move.b  d3,(a1)+
        subq.l  #1,d0
        bne     .z
        lea     atab,a1                 ; a free slot
        moveq   #31,d1
.s:     tst.l   (a1)
        beq     .f
        addq.l  #8,a1
        dbra    d1,.s
        bra     .no
.f:     move.l  a0,(a1)+
        move.l  d2,(a1)
        addq.b  #1,nallocs
        move.l  a0,d0
        bra     .x
.no:    moveq   #0,d0
.x:     movem.l (sp)+,d1-d3/a0-a1
        rts

; free: a1 memory, d0 size: must match a recorded allocation
free:   movem.l d1/a0,-(sp)
        lea     atab,a0
        moveq   #31,d1
.s:     cmp.l   (a0),a1
        bne     .n
        cmp.l   4(a0),d0
        bne     .n
        clr.l   (a0)
        clr.l   4(a0)
        subq.b  #1,nallocs
        bra     .x
.n:     addq.l  #8,a0
        dbra    d1,.s
        st      XS+XS_FREEBAD
.x:     movem.l (sp)+,d1/a0
        rts

; ---------------------------------------------------------------------------
; dos: one read-only file, XP_FNAME, at XP_FILE
; ---------------------------------------------------------------------------
FH              equ     $00dd0001
d_open:                                 ; d1 name, d2 mode
        LOG     LIB_DOS,-30
        movem.l a0-a1,-(sp)
        move.l  d1,a0
        lea     XS+XS_OPENNAME,a1
        moveq   #30,d0
.cp:    move.b  (a0)+,(a1)+
        dbeq    d0,.cp
        clr.b   (a1)
        move.l  d1,a0
        lea     XP+XP_FNAME,a1
.c:     cmpm.b  (a0)+,(a1)+
        bne     .no
        tst.b   -1(a0)
        bne     .c
        cmp.l   #1005,d2
        bne     .no
        cmp.l   #$00001111,curdir       ; the tool's (or the project's) drawer
        beq     .ok
        cmp.l   #$00002222,curdir
        beq     .ok
        cmp.l   #$00000100,curdir       ; a shell's
        bne     .no
.ok:    tst.b   fileopen
        bne     .no
        st      fileopen
        clr.l   fpos
        move.l  #FH,d0
        bra     .x
.no:    moveq   #0,d0
.x:     movem.l (sp)+,a0-a1
        rts
d_close:
        LOG     LIB_DOS,-36
        cmp.l   #FH,d1
        bne     .r
        sf      fileopen
.r:     moveq   #-1,d0
        rts
d_read:                                 ; d1 fh, d2 buffer, d3 length
        LOG     LIB_DOS,-42
        movem.l d1/a0-a1,-(sp)
        moveq   #-1,d0
        cmp.l   #FH,d1
        bne     .x
        tst.b   fileopen
        beq     .x
        move.l  XP+XP_FLEN,d0
        sub.l   fpos,d0
        cmp.l   d3,d0
        bls     .n
        move.l  d3,d0
.n:     move.l  XP+XP_FILE,a0
        add.l   fpos,a0
        move.l  d2,a1
        add.l   d0,fpos
        move.l  d0,d1
        beq     .x
.cp:    move.b  (a0)+,(a1)+
        subq.l  #1,d1
        bne     .cp
.x:     movem.l (sp)+,d1/a0-a1
        rts
d_seek:                                 ; d1 fh, d2 offset, d3 mode -> the old position
        LOG     LIB_DOS,-66
        moveq   #-1,d0
        cmp.l   #FH,d1
        bne     .r
        move.l  fpos,d0
        move.l  d2,d1
        tst.l   d3
        bmi     .set
        beq     .cur
        add.l   XP+XP_FLEN,d1
        bra     .set
.cur:   add.l   d0,d1
.set:   move.l  d1,fpos
        move.l  #FH,d1
.r:     rts
d_curdir:
        LOG     LIB_DOS,-126
        move.l  curdir,d0
        move.l  d1,curdir
        rts

; ---------------------------------------------------------------------------
; graphics, intuition
; ---------------------------------------------------------------------------
g_loadview:                             ; strict: records only (no copper rebuilt)
        LOG     LIB_GFX,-222
        move.l  a1,GFXBASE+34
        rts
g_waitblit:
        LOG     LIB_GFX,-228
.b:     btst    #6,CUSTOM+DMACONR
        bne     .b
        rts
g_waittof:
        LOG     LIB_GFX,-270
        movem.l d0-d1,-(sp)
        move.w  vblcount,d0
        move.l  #300000,d1
.w:     cmp.w   vblcount,d0
        bne     .r
        subq.l  #1,d1
        bne     .w
        st      XS+XS_WTO
.r:     movem.l (sp)+,d0-d1
        rts
g_ownblit:
        LOG     LIB_GFX,-456
        addq.b  #1,ownblit
        rts
g_disownblit:
        LOG     LIB_GFX,-462
        subq.b  #1,ownblit
        rts
i_beep: LOG     LIB_INT,-96
        rts
i_rethink:
        LOG     LIB_INT,-390
        btst    #7,XP+XP_REFUSE
        beq     .r
        move.l  #FOREIGN,ANCHOR+4       ; another writer's anchor (a stale or a
        clr.l   ANCHOR+8                ; second instance's), published after
        clr.l   ANCHOR+12               ; our restore
        move.l  #'MWBA',ANCHOR
.r:     rts
i_easy: LOG     LIB_INT,-588
        move.l  12(a1),XS+XS_EASY       ; es_TextFormat
        cmp.w   #0,a3
        beq     .na
        move.l  (a3),XS+XS_EASYARG
.na:
        moveq   #1,d0
        rts

; ---------------------------------------------------------------------------
; ciaa.resource / ciab.resource (a6 says which)
; ---------------------------------------------------------------------------
c_res:  lea     owna,a0                 ; -> a0 owners, a1 kept; d1 = the mask's address
        move.l  #maska,d1
        cmp.l   #CIABBASE,a6
        bne     .r
        lea     ownb,a0
        move.l  #maskb,d1
.r:     rts
c_add:                                  ; d0 bit, a1 interrupt -> d0 0 or the owner
        LOG     LIB_CIAA,-6
        movem.l d1-d2/a0/a2,-(sp)
        bsr     c_res
        and.w   #1,d0
        move.w  d0,d2                   ; refuse bit: ciaa TA 0, TB 1, ciab TA 2, TB 3
        cmp.l   #CIABBASE,a6
        bne     .a
        addq.w  #2,d2
.a:     lsl.w   #2,d0
        add.w   d0,a0
        move.l  (a0),d0
        bne     .x                      ; taken: the owner
        btst    d2,XP+XP_REFUSE
        beq     .ok
        move.l  #$00dead00,d0           ; someone else's
        bra     .x
.ok:    move.l  a1,(a0)
        move.l  d1,a2
        bsr     c_bit                   ; d0 := 1 << bit from the owner slot
        or.b    d0,(a2)
        or.b    #$80,d0
        bsr     c_wicr
        moveq   #0,d0
.x:     movem.l (sp)+,d1-d2/a0/a2
        rts
; c_bit: a0 = the owner slot -> d0 = its ICR bit mask
c_bit:  move.l  a0,d0
        sub.l   #owna,d0
        and.w   #4,d0
        lsr.w   #2,d0
        addq.w  #1,d0                   ; TA 1, TB 2
        rts
c_wicr: cmp.l   #CIABBASE,a6
        beq     .b
        move.b  d0,CIAA_ICR
        rts
.b:     move.b  d0,CIAB_ICR
        rts
c_rem:                                  ; d0 bit, a1 interrupt
        LOG     LIB_CIAA,-12
        movem.l d0-d1/a0/a2,-(sp)
        bsr     c_res
        and.w   #1,d0
        lsl.w   #2,d0
        add.w   d0,a0
        cmp.l   (a0),a1
        bne     .x
        clr.l   (a0)
        move.l  d1,a2
        bsr     c_bit
        not.b   d0
        and.b   d0,(a2)
        not.b   d0
        bsr     c_wicr                  ; clear the enable
.x:     movem.l (sp)+,d0-d1/a0/a2
        rts
c_able:                                 ; d0 mask -> d0 the old enable mask
        LOG     LIB_CIAA,-18
        movem.l d1-d2/a0/a2,-(sp)
        bsr     c_res
        move.l  d1,a2
        moveq   #0,d2
        move.b  (a2),d2
        move.b  d0,d1
        and.b   #$7f,d1
        beq     .x
        tst.b   d0
        bmi     .set
        not.b   d1
        and.b   d1,(a2)
        not.b   d1
        move.b  d1,d0
        bsr     c_wicr
        bra     .x
.set:   or.b    d1,(a2)
        bsr     c_wicr
.x:     move.l  d2,d0
        movem.l (sp)+,d1-d2/a0/a2
        rts
c_set:  LOG     LIB_CIAA,-24
        moveq   #0,d0
        rts

; ---------------------------------------------------------------------------
; tables
; ---------------------------------------------------------------------------
        cnop    0,4
libtab: dc.l    SYSBASE
        dc.w    -120
        dc.l    x_disable
        dc.w    -126
        dc.l    x_enable
        dc.w    -132
        dc.l    x_forbid
        dc.w    -138
        dc.l    x_permit
        dc.w    -150
        dc.l    x_superstate
        dc.w    -156
        dc.l    x_userstate
        dc.w    -198
        dc.l    x_allocmem
        dc.w    -204
        dc.l    x_allocabs
        dc.w    -210
        dc.l    x_freemem
        dc.w    -294
        dc.l    x_findtask
        dc.w    -372
        dc.l    x_getmsg
        dc.w    -378
        dc.l    x_replymsg
        dc.w    -384
        dc.l    x_waitport
        dc.w    -414
        dc.l    x_closelib
        dc.w    -444
        dc.l    x_opendevice
        dc.w    -450
        dc.l    x_closedevice
        dc.w    -498
        dc.l    x_openres
        dc.w    -552
        dc.l    x_openlib
        dc.w    -666
        dc.l    x_createport
        dc.w    -672
        dc.l    x_deleteport
        dc.w    0
        dc.l    DOSBASE
        dc.w    -30
        dc.l    d_open
        dc.w    -36
        dc.l    d_close
        dc.w    -42
        dc.l    d_read
        dc.w    -66
        dc.l    d_seek
        dc.w    -126
        dc.l    d_curdir
        dc.w    0
        dc.l    GFXBASE
        dc.w    -222
        dc.l    g_loadview
        dc.w    -228
        dc.l    g_waitblit
        dc.w    -270
        dc.l    g_waittof
        dc.w    -456
        dc.l    g_ownblit
        dc.w    -462
        dc.l    g_disownblit
        dc.w    0
        dc.l    INTBASE
        dc.w    -96
        dc.l    i_beep
        dc.w    -390
        dc.l    i_rethink
        dc.w    -588
        dc.l    i_easy
        dc.w    0
        dc.l    CIAABASE
        dc.w    -6
        dc.l    c_add
        dc.w    -12
        dc.l    c_rem
        dc.w    -18
        dc.l    c_able
        dc.w    -24
        dc.l    c_set
        dc.w    0
        dc.l    CIABBASE
        dc.w    -6
        dc.l    c_add
        dc.w    -12
        dc.l    c_rem
        dc.w    -18
        dc.l    c_able
        dc.w    -24
        dc.l    c_set
        dc.w    0
        dc.l    0

names_lib:
        dc.l    n_dos,DOSBASE
        dc.w    -1
        dc.l    n_gfx,GFXBASE
        dc.w    -1
        dc.l    n_int,INTBASE
        dc.w    5
        dc.l    0
names_res:
        dc.l    n_ciaa,CIAABASE
        dc.w    -1
        dc.l    n_ciab,CIABBASE
        dc.w    -1
        dc.l    0
n_dos:  dc.b    "dos.library",0
n_gfx:  dc.b    "graphics.library",0
n_int:  dc.b    "intuition.library",0
n_ciaa: dc.b    "ciaa.resource",0
n_ciab: dc.b    "ciab.resource",0
n_aud:  dc.b    "audio.device",0
        even

; ---------------------------------------------------------------------------
; state
; ---------------------------------------------------------------------------
        cnop    0,4
logp:   dc.l    0
heapp:  dc.l    0
curdir: dc.l    0
fpos:   dc.l    0
owna:   dc.l    0,0                     ; ciaa TA, TB owners
ownb:   dc.l    0,0                     ; ciab TA, TB
atab:   dcb.l   64,0                    ; allocations: (address, size) x 32
vblcount:       dc.w    0
tacount:        dc.w    0
tbcount:        dc.w    0
maska:  dc.b    0
maskb:  dc.b    0
idnest: dc.b    0
tdnest: dc.b    0
ownblit:        dc.b    0
libsopen:       dc.b    0
nallocs:        dc.b    0
devopen:        dc.b    0
fileopen:       dc.b    0
msgpend:        dc.b    0
        even
