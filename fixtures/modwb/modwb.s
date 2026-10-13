; fixtures/modwb/modwb.s — the AmigaOS launcher of the Wave 15 MOD player
; (W16-X, docs/WAVE16_BRIEF.md and its addendum).
;
; A hunk executable started from Workbench (an icon) or a shell.  It loads a
; MOD into chip RAM, claims what the replay uses through the OS (CIA-B
; timer A by ciab.resource, the four audio channels by audio.device,
; the blitter), takes the machine over (display off by LoadView(0), Forbid,
; Disable, supervisor mode, its own stack, every DMA and interrupt off, its
; own level 2/3/6 autovectors), runs fixtures/mod/replay.s (MOD_HUNK) and
; fixtures/mod/ui/ui.s (UI_HUNK) exactly as main.c does, and on LMB/fire or
; at the song's end restores everything in the reverse order and returns.
; A failure before the takeover (no MOD, a claim refused, ...) backs out
; with a requester and never takes the machine.
;
; The tempo comes from W16-P's profile block ($2DC000, host-written); the
; app anchor at $2DC040 ('MWBA', the state pointer, seq/state) is the one
; way tests find the player (addendum).  Both live outside every MemHeader.
; A second instance finds the first one's anchor and backs out (ERR_BUSY).
;
; Known limits: the takeover eats the OS's CIA interrupt flags (CIA-A TA
; and SP: keyboard, timer.device; CIA-B TB) that come up while it runs:
; l2/l6 read the ICRs, so a one-shot timer request or a key the OS had
; pending ACROSS the takeover is not re-posted at the restore (continuous
; timers resume; leg A's `Wait 5` after the return passed).  A left button
; held longer than LMB_WAIT frames after the exit reaches the Workbench.
;
; CIA-A timer B is never claimed or touched (W16-E3): AROS runs it as a
; free-running counter with no ciaa.resource owner, and claiming it (even
; restored as found) made later launches from the same Workbench session
; fail intermittently (build/evidence/w16-e3: 3 of 11 launches with it,
; 0 of 14 without).  The replay's DMA stages busy-wait on CIA-B TA's
; counter instead (replay.s MW_NO_ATB, set by the Makefile).
;
; AmigaOS facts: os.i (public documentation, cited in
; build/evidence/w16-x/PROGRESS.md).  Mutants (tests/run_modwb.py):
; MW_MUT_* below.

        include "os.i"
        include "mod.i"

PROF_AT         equ     $2dc000         ; W16-P's profile block (64 B)
ANCHOR_AT       equ     $2dc040         ; the app anchor (16 B)
MOD_MAX         equ     $100000         ; the largest MOD loaded (1 MB)
GRACE           equ     4               ; frame calls before LMB counts
LMB_WAIT        equ     50              ; frames the restore waits for the button's release
STACK_SIZE      equ     4096
MW_MAXF         equ     8000            ; MOD_TRACE builds: P_MAXF

; the state struct (the anchor's pointer; host mirror tests/run_modwb.py)
                rsreset
A_MAGIC         rs.l    1               ; 'MWBS'
A_VER           rs.w    1               ; 1
A_STATE         rs.b    1               ; ST_*
A_WHY           rs.b    1               ; WHY_*: why the play ended
A_ERR           rs.b    1               ; ERR_* (0 = none)
A_MINIT         rs.b    1               ; mod_init's return value
A_CLAIM         rs.b    4               ; ciaa TA, ciaa TB, ciab TA, ciab TB: 0 kept, 1 refused,
                                        ; 2 free (asked, not kept), $FF not asked
A_AUDIO         rs.b    1               ; 0 allocated, 1 refused, 2 no port/device
A_PROF          rs.b    1               ; 1: the profile block was used
A_VIEW          rs.l    1               ; &mod_view (I-1)
A_STATUS        rs.l    1
A_PARAM         rs.l    1
A_UICOP         rs.l    1               ; the UI's copper list (chip)
A_MOD           rs.l    1
A_MODLEN        rs.l    1
A_SDMA          rs.w    1               ; saved DMACONR & $07FF
A_SINT          rs.w    1               ; saved INTENAR
A_SADK          rs.w    1               ; ADKCONR (recorded only: nothing here writes ADKCON)
A_MASKA         rs.b    1               ; ciaa ICR enable mask (AbleICR(0))
A_MASKB         rs.b    1
A_OLDVIEW       rs.l    1
A_COPINIT       rs.l    1               ; GfxBase->copinit at the restore
A_FRAMES        rs.l    1               ; frame calls run
A_TICKE         rs.w    1               ; P_TICKE used
A_STAGEE        rs.w    1               ; P_STAGEE used
A_SVEC          rs.l    7               ; saved autovectors $64-$7C
A_SIZE          rs.b    0

ST_LOAD         equ     0               ; loading, nothing taken
ST_TAKEN        equ     1               ; the machine is ours
ST_RESTORED     equ     2               ; hardware and OS state back
ST_DONE         equ     3               ; display back, claims released
ST_FAILED       equ     4               ; backed out (A_ERR says why)

WHY_LMB         equ     1
WHY_END         equ     2               ; mod_done (PF_STOPEND)
WHY_LOOP        equ     3               ; the view's MV_LOOPED
WHY_INIT        equ     4               ; mod_init refused the MOD

ERR_LIB         equ     1               ; a library or resource did not open
ERR_FILE        equ     2               ; the MOD did not open
ERR_SIZE        equ     3               ; too small, too big, or no chip RAM
ERR_READ        equ     4               ; a short read
ERR_MK          equ     5               ; not an M.K. MOD
ERR_CLAIM       equ     6               ; a CIA timer belongs to someone else
ERR_INIT        equ     7               ; mod_init refused it (after the takeover)
ERR_BUSY        equ     8               ; another instance holds the anchor
ERR_TRACE       equ     9               ; MOD_TRACE builds: the trace RAM is not free

        section CODE,code

        xref    _mod_init,_mod_start,_mod_wait_frame,_mod_publish,_mod_done
        xref    l2_ciaa,l3_vbl,l6_ciab
        xref    _ui_init,_ui_frame
        xref    UI_COP

; ===========================================================================
; the entry: a0/d0 = the shell's arguments (a shell start)
; ===========================================================================
start:  movem.l d2-d7/a2-a6,-(sp)
        move.l  a0,cliarg
        move.l  d0,clilen
        move.l  4.w,a6
        move.l  a6,sysbase
        bsr     anchor_init
        sub.l   a1,a1
        CALL    FindTask
        move.l  d0,a2
        tst.l   pr_CLI(a2)
        bne.s   .cli
        lea     pr_MsgPort(a2),a0       ; a Workbench start: its message first
        CALL    WaitPort
        lea     pr_MsgPort(a2),a0
        CALL    GetMsg
        move.l  d0,wbmsg
.cli:   bsr     run
        move.b  d0,mwst+A_ERR
        beq.s   .ok
        moveq   #ST_FAILED,d0
        bsr     set_state
        bsr     report
.ok:    bsr     anchor_end              ; withdrawn first, before anything is freed
        bsr     release
        move.l  sysbase,a6
        move.l  wbmsg,d2
        beq.s   .x
        CALL    Forbid                  ; held: we are unloaded after the return
        move.l  d2,a1
        ifnd    MW_MUT_NOREPLY
        CALL    ReplyMsg
        endc
.x:     moveq   #0,d0
        tst.b   mwst+A_ERR
        beq.s   .r
        moveq   #20,d0                  ; RETURN_FAIL
.r:     movem.l (sp)+,d2-d7/a2-a6
        rts

; anchor_init: the state struct, the profile block's validity, and the
; app anchor (CONTRACTS "Audio (Paula)", the anchor rules): published only
; when the profile block is valid; +4/+8/+12 first, 'MWBA' last.  The
; struct is in a chip hunk (rule 3: chip or slow RAM; even).  Under Forbid:
; an anchor another instance published (its 'MWBS' struct) is left alone
; and this one backs out (ERR_BUSY, rev16-x MEDIUM-2); a stale one (no
; struct behind it) is withdrawn first, then replaced.  a6 = SysBase.
anchor_init:
        lea     mwst,a0                 ; cleared here: a hunk's tail beyond its
        moveq   #A_SIZE/2-1,d0          ; stored bytes need not be zero
.z:     clr.w   (a0)+
        dbra    d0,.z
        lea     mwst,a0
        move.l  #'MWBS',A_MAGIC(a0)
        move.w  #1,A_VER(a0)
        move.l  #$ffffffff,A_CLAIM(a0)
        move.l  #VIEW,A_VIEW(a0)
        move.l  #STATUS,A_STATUS(a0)
        move.l  #PARAM,A_PARAM(a0)
        move.l  #UI_COP,A_UICOP(a0)
        bsr     prof_check
        ifd     MW_MUT_ANCHORANY
        moveq   #1,d0                   ; (mutant: published without a valid block)
        endc
        move.b  d0,profok
        beq.s   .r
        CALL    Forbid
        lea     ANCHOR_AT,a1
        ifnd    MW_MUT_ANCHORCLOBBER
        bsr.s   anchor_busy
        bne.s   .busy
        endc
        clr.l   (a1)                    ; (a stale one: withdrawn first)
        ifd     MW_MUT_ANCHORFIRST
        move.l  #'MWBA',(a1)            ; (mutant: the magic first)
        endc
        move.l  #mwst,4(a1)
        clr.l   8(a1)
        clr.l   12(a1)
        move.l  #'MWBA',(a1)            ; last
        st      anch
        bra.s   .p
.busy:  st      busy
.p:     CALL    Permit
.r:     rts

; anchor_busy: a1 = the anchor.  ne: another instance's ('MWBA', its
; pointer even, in chip or slow RAM, to an 'MWBS' struct that is not ours).
; d0/a0 used.
anchor_busy:
        cmp.l   #'MWBA',(a1)
        bne.s   .no
        move.l  4(a1),d0
        cmp.l   #mwst,d0
        beq.s   .no
        btst    #0,d0
        bne.s   .no
        cmp.l   #$200000-A_SIZE,d0
        bls.s   .ck
        cmp.l   #$c00000,d0
        blo.s   .no
        cmp.l   #$dc0000-A_SIZE,d0
        bhi.s   .no
.ck:    move.l  d0,a0
        cmp.l   #'MWBS',(a0)
        bne.s   .no
        moveq   #1,d0
        rts
.no:    moveq   #0,d0
        rts

; anchor_end: withdraw the anchor ('MWBA' cleared first, before release
; frees the MOD and the claims; the struct itself lives until the hunks are
; unloaded after the return), and only while it still points to our struct.
; Nothing writes the anchor afterwards.
anchor_end:
        tst.b   anch
        beq.s   .r
        ifnd    MW_MUT_ANCHORENDANY
        cmp.l   #mwst,ANCHOR_AT+4
        bne.s   .n
        endc
        clr.l   ANCHOR_AT
.n:     clr.b   anch
.r:     rts

; prof_check: d0.b = nonzero when W16-P's profile block is valid ('A64P',
; version 1, size 64, ~sum of the longs +0..+56 at +60).  Keeps a0.
prof_check:
        movem.l d1-d2/a0,-(sp)
        moveq   #0,d2
        lea     PROF_AT,a0
        cmp.l   #'A64P',(a0)
        bne.s   .x
        cmp.l   #$00010040,4(a0)
        bne.s   .x
        moveq   #14,d1
        moveq   #0,d0
.s:     add.l   (a0)+,d0
        dbra    d1,.s
        not.l   d0
        cmp.l   (a0),d0
        bne.s   .x
        moveq   #1,d2
.x:     move.l  d2,d0
        movem.l (sp)+,d1-d2/a0
        rts

; set_state: d0.b = ST_*; the anchor's +8 (one long) = seq << 16 | state << 8
set_state:
        move.b  d0,mwst+A_STATE
        addq.w  #1,aseq
        tst.b   anch
        beq.s   .r
        move.l  d1,-(sp)
        move.w  aseq,d1
        swap    d1
        clr.w   d1
        move.b  d0,d1
        lsl.w   #8,d1
        move.l  d1,ANCHOR_AT+8
        move.l  (sp)+,d1
.r:     rts

; ===========================================================================
; run: d0 = ERR_* (0: played and restored)
; ===========================================================================
run:    move.l  sysbase,a6
        lea     dosname(pc),a1
        moveq   #33,d0
        CALL    OpenLibrary
        move.l  d0,dosbase
        beq     .lib
        lea     gfxname(pc),a1
        moveq   #33,d0
        CALL    OpenLibrary
        move.l  d0,gfxbase
        beq     .lib
        lea     intname(pc),a1
        moveq   #33,d0
        CALL    OpenLibrary
        move.l  d0,intbase
        beq     .lib
        lea     ciaaname(pc),a1
        CALL    OpenResource
        move.l  d0,ciaabase
        beq     .lib
        lea     ciabname(pc),a1
        CALL    OpenResource
        move.l  d0,ciabbase
        beq     .lib
        moveq   #ERR_BUSY,d0
        tst.b   busy
        bne     .r
        ifd     MOD_TRACE               ; the trace RAM (mod.i TR_W .. TR_TEND):
        move.l  sysbase,a6              ; ours, or no start (it is slow RAM
        move.l  #TR_TEND-TR_W,d0        ; under AmigaOS: LoadSeg's)
        lea     TR_W,a1
        CALL    AllocAbs
        move.l  d0,trbuf
        bne.s   .tr
        moveq   #ERR_TRACE,d0
        rts
.tr:
        endc
        bsr     load_mod
        tst.l   d0
        bne     .r
        bsr     params
        bsr     claim
        tst.l   d0
        bne     .r
        bsr     audio_alloc
        move.l  gfxbase,a6
        CALL    OwnBlitter
        st      ownblit
        CALL    WaitBlit
        move.l  gb_ActiView(a6),mwst+A_OLDVIEW
        sub.l   a1,a1
        CALL    LoadView
        CALL    WaitTOF
        CALL    WaitTOF
        move.l  ciaabase,a6
        moveq   #0,d0
        CALL    AbleICR
        move.b  d0,mwst+A_MASKA
        move.l  ciabbase,a6
        moveq   #0,d0
        CALL    AbleICR
        move.b  d0,mwst+A_MASKB
        move.l  sysbase,a6
        CALL    Forbid
        CALL    Disable
        CALL    SuperState
        move.l  d0,oldssp
        move.l  sp,oldsp
        lea     stack_end,sp
        bsr     takeover
        bsr     play
        bsr     restore
        move.l  oldsp,sp
        move.l  sysbase,a6
        move.l  oldssp,d0
        CALL    UserState
        ifnd    MW_MUT_PERMITFIRST
        CALL    Enable
        CALL    Permit
        else
        CALL    Permit
        CALL    Enable
        endc
        moveq   #ST_RESTORED,d0
        bsr     set_state
        move.l  gfxbase,a6
        move.l  mwst+A_OLDVIEW,a1
        CALL    LoadView
        CALL    WaitTOF
        CALL    WaitTOF
        move.l  intbase,a6
        CALL    RethinkDisplay
        moveq   #0,d0
        cmp.b   #WHY_INIT,mwst+A_WHY
        bne.s   .r
        moveq   #ERR_INIT,d0
.r:     rts
.lib:   moveq   #ERR_LIB,d0
        rts

; ---------------------------------------------------------------------------
; load_mod: the MOD into chip RAM.  d0 = ERR_* or 0
; ---------------------------------------------------------------------------
load_mod:
        lea     defname(pc),a0
        move.l  a0,name
        move.l  wbmsg,d0
        beq.s   .cli
        move.l  d0,a0                   ; Workbench: arg 1 (a project icon) or
        move.l  sm_ArgList(a0),a1       ; the tool's name + ".mod" next to it
        cmp.l   #2,sm_NumArgs(a0)
        blo.s   .tool
        addq.l  #8,a1
        move.l  wa_Name(a1),name
        bra.s   .lk
.tool:  move.l  wa_Name(a1),a0
        lea     namebuf,a2
        moveq   #100-1,d0
.tn:    move.b  (a0)+,(a2)+
        dbeq    d0,.tn
        subq.l  #1,a2
        lea     dotmod(pc),a0
.te:    move.b  (a0)+,(a2)+
        bne.s   .te
        move.l  #namebuf,name
.lk:    move.l  wa_Lock(a1),d1
        move.l  dosbase,a6
        CALL    CurrentDir
        move.l  d0,olddir
        st      cdset
        bra.s   .open
.cli:   move.l  cliarg,a0               ; a shell: the first word, if any, or
        move.l  clilen,d0               ; the first "quoted name" (spaces kept)
        lea     namebuf,a1
        moveq   #0,d1
        moveq   #0,d3                   ; d3 = '"' inside quotes
.sk:    subq.l  #1,d0
        bmi.s   .nd
        move.b  (a0)+,d2
        cmp.b   #' ',d2
        beq.s   .sk
        cmp.b   #9,d2
        beq.s   .sk
        cmp.b   #'"',d2
        bne.s   .cp
        moveq   #'"',d3
        bra.s   .nx
.cp:    cmp.b   d3,d2                   ; the closing quote
        beq.s   .nd
        cmp.b   #' ',d2                 ; a control character ends it, and a
        blo.s   .nd                     ; space outside quotes
        bne.s   .ch
        tst.b   d3
        beq.s   .nd
.ch:    cmp.w   #107,d1
        bhs.s   .nd
        move.b  d2,(a1)+
        addq.w  #1,d1
.nx:    subq.l  #1,d0
        bmi.s   .nd
        move.b  (a0)+,d2
        bra.s   .cp
.nd:    clr.b   (a1)
        tst.w   d1
        beq.s   .open
        move.l  #namebuf,name
.open:  move.l  dosbase,a6
        move.l  name,d1
        move.l  #MODE_OLDFILE,d2
        CALL    Open
        move.l  d0,fh
        beq     .efile
        move.l  d0,d1
        moveq   #0,d2
        moveq   #OFFSET_END,d3
        CALL    Seek
        move.l  fh,d1
        moveq   #0,d2
        moveq   #OFFSET_BEGINNING,d3
        CALL    Seek                    ; returns the old position: the size
        move.l  d0,mwst+A_MODLEN
        cmp.l   #1084,d0
        blt.s   .esize
        cmp.l   #MOD_MAX,d0
        bhi.s   .esize
        move.l  sysbase,a6
        moveq   #MEMF_CHIP,d1
        CALL    AllocMem
        move.l  d0,mwst+A_MOD
        beq.s   .esize
        move.l  dosbase,a6
        move.l  fh,d1
        move.l  mwst+A_MOD,d2
        move.l  mwst+A_MODLEN,d3
        CALL    Read
        cmp.l   mwst+A_MODLEN,d0
        bne.s   .eread
        bsr     .close
        move.l  mwst+A_MOD,a0
        cmp.l   #'M.K.',1080(a0)
        bne.s   .emk
        moveq   #0,d0
        rts
.efile: moveq   #ERR_FILE,d0
        rts
.esize: bsr     .close
        moveq   #ERR_SIZE,d0
        rts
.eread: bsr     .close
        moveq   #ERR_READ,d0
        rts
.emk:   moveq   #ERR_MK,d0
        rts
.close: move.l  dosbase,a6
        move.l  fh,d1
        CALL    Close
        clr.l   fh
        rts

; ---------------------------------------------------------------------------
; params: the replay's parameter block; the tempo from the profile block
; ---------------------------------------------------------------------------
params: lea     PARAM,a1
        move.l  #'MPAR',P_MAGIC(a1)
        move.l  mwst+A_MOD,P_MOD(a1)
        move.l  mwst+A_MODLEN,P_LEN(a1)
        move.w  #(1<<PF_STOPEND)|(1<<PF_REALTIME),P_FLAGS(a1)
        move.b  #1,P_UIDIV(a1)
        ifd     MOD_TRACE                       ; leg S: the trace (the stub's slow RAM)
        or.w    #1<<PF_TRACE,P_FLAGS(a1)        ; and the frame limit as a safety net
        move.l  #MW_MAXF,P_MAXF(a1)
        endc
        lea     STATUS,a0                       ; crt0's part of the status block
        move.l  #'MSTA',S_MAGIC(a0)
        move.b  #1,S_STATE(a0)
        tst.b   profok                  ; W16-P's block, valid
        beq.s   .x
        btst    #2,PROF_AT+26           ; real time meaningful (an Ultimate; not
        beq.s   .x                      ; VICE warp): else the Amiga's timing
        move.l  PROF_AT+12,d0           ; tick_e: E per 1/50 s
        beq.s   .x
        cmp.l   #$ffff,d0
        bhi.s   .x
        move.w  d0,P_TICKE(a1)
        mulu.w  #STAGE_E,d0             ; the stage delay at the same real time
        add.l   #20455/2,d0             ; as STAGE_E at E = 1022727 Hz
        divu.w  #20455,d0
        bne.s   .st
        moveq   #1,d0
.st:    move.w  d0,P_STAGEE(a1)
        move.b  #1,mwst+A_PROF
.x:     move.w  P_TICKE(a1),mwst+A_TICKE
        move.w  P_STAGEE(a1),mwst+A_STAGEE
        rts

; ---------------------------------------------------------------------------
; claim: the replay's CIA timer (ciab TA: the tick) by AddICRVector.
; ciaa TA and ciab TB are only asked and, if free, released at once
; (recorded: AROS owns both, R1).  ciaa TB is never asked ($FF, W16-E3:
; AROS uses it without the resource).  d0 = 0 or ERR_CLAIM (whatever was
; kept is released by release)
; ---------------------------------------------------------------------------
claim:  lea     ints,a0
        moveq   #3,d1
.i:     move.b  #NT_INTERRUPT,LN_TYPE(a0)
        move.l  #intname_s,LN_NAME(a0)
        move.l  #dummy,IS_CODE(a0)
        lea     IS_SIZE(a0),a0
        dbra    d1,.i
        moveq   #0,d4                   ; d4 = the claim index 0-3
.c:
        ifnd    MW_MUT_ATBCLAIM         ; (mutant: ciaa TB asked and kept again)
        cmp.w   #1,d4                   ; W16-E3: ciaa TB never asked
        bne.s   .nx                     ; W16-E3
        st      mwst+A_CLAIM+1          ; W16-E3: $FF, released by nobody
        bra.s   .n                      ; W16-E3
.nx:                                    ; W16-E3
        endc
        move.l  ciaabase,a6
        cmp.w   #2,d4
        blo.s   .a
        move.l  ciabbase,a6
.a:     moveq   #1,d0
        and.w   d4,d0                   ; the ICR bit: TA 0, TB 1
        move.w  d4,d1
        mulu.w  #IS_SIZE,d1
        lea     ints,a1
        add.w   d1,a1
        CALL    AddICRVector
        lea     mwst+A_CLAIM,a0
        tst.l   d0
        sne     d1
        and.b   #1,d1
        move.b  d1,0(a0,d4.w)           ; 0 kept, 1 refused
        bne.s   .n
        cmp.w   #1,d4                   ; ciaa TA, ciab TB: free, not kept
        beq.s   .n
        cmp.w   #2,d4
        beq.s   .n
        move.b  #2,0(a0,d4.w)
        moveq   #1,d0
        and.w   d4,d0
        move.w  d4,d1
        mulu.w  #IS_SIZE,d1
        lea     ints,a1
        add.w   d1,a1
        CALL    RemICRVector
.n:     addq.w  #1,d4
        cmp.w   #4,d4
        blo.s   .c
        lea     mwst+A_CLAIM,a0
        moveq   #0,d0
        ifd     MW_MUT_ATBCLAIM
        move.b  1(a0),d0
        endc
        or.b    2(a0),d0                ; ciab TA (ciaa TB: never asked, W16-E3)
        beq.s   .r
        moveq   #ERR_CLAIM,d0
.r:     rts

dummy:  moveq   #0,d0
        rts

; ---------------------------------------------------------------------------
; audio_alloc: all four channels at priority 127, no waiting (failures are
; recorded and the play goes on: nothing else runs during the takeover)
; ---------------------------------------------------------------------------
audio_alloc:
        move.l  sysbase,a6
        move.b  #2,mwst+A_AUDIO
        CALL    CreateMsgPort
        move.l  d0,aport
        beq.s   .r
        lea     ioa,a1
        move.b  #127,LN_PRI(a1)
        move.l  d0,MN_REPLYPORT(a1)
        move.w  #IOA_SIZE,MN_LENGTH(a1)
        move.l  #allocmap,ioa_Data(a1)
        move.l  #1,ioa_Length(a1)
        move.b  #ADIOF_NOWAIT,IO_FLAGS(a1)
        lea     audname(pc),a0
        moveq   #0,d0
        moveq   #0,d1
        CALL    OpenDevice
        move.b  #1,mwst+A_AUDIO
        tst.b   d0
        bne.s   .r
        clr.b   mwst+A_AUDIO
        st      audopen
.r:     rts

; ---------------------------------------------------------------------------
; takeover / restore (supervisor, SR $2700)
; ---------------------------------------------------------------------------
takeover:
        move.w  #$2700,sr
        lea     CUSTOM,a5
        move.w  DMACONR(a5),d0
        and.w   #$07ff,d0
        move.w  d0,mwst+A_SDMA
        move.w  INTENAR(a5),mwst+A_SINT
        move.w  ADKCONR(a5),mwst+A_SADK
        lea     $64.w,a0
        lea     mwst+A_SVEC,a1
        moveq   #6,d0
.v:     move.l  (a0)+,(a1)+
        dbra    d0,.v
        move.w  #$7fff,INTENA(a5)
        move.w  #$7fff,INTREQ(a5)
        move.w  #$7fff,DMACON(a5)
        moveq   #0,d0
        move.w  d0,AUD0LC+8(a5)
        move.w  d0,AUD0LC+8+$10(a5)
        move.w  d0,AUD0LC+8+$20(a5)
        move.w  d0,AUD0LC+8+$30(a5)
        move.l  #l2_ciaa,$68.w
        move.l  #l3_vbl,$6c.w
        move.l  #l6_ciab,$78.w
        moveq   #ST_TAKEN,d0
        bra     set_state

restore:
        move.w  #$2700,sr
        lea     CUSTOM,a5
        move.w  #$7fff,INTENA(a5)
        move.w  #$7fff,INTREQ(a5)
        move.w  #$000f,DMACON(a5)       ; audio off, silent
        moveq   #0,d0
        move.w  d0,AUD0LC+8(a5)
        move.w  d0,AUD0LC+8+$10(a5)
        move.w  d0,AUD0LC+8+$20(a5)
        move.w  d0,AUD0LC+8+$30(a5)
        clr.b   CIAB_CRA                ; our timer stopped (ciab TB is AROS's and
                                        ; ciaa TB too, W16-E3: never touched)
        ifd     MW_MUT_ATBCRB           ; (mutant: the old restore, CIA-A TB stopped)
        move.b  #$08,CIAA_CRB
        endc
        ifd     MW_MUT_TBCRB
        clr.b   CIAB_CRB
        endc
        ifnd    MW_MUT_NOLMBWAIT
        bsr     lmb_wait                ; silent now; the OS after the release
        endc
.bw:    btst    #6,DMACONR(a5)          ; the blitter idle
        bne.s   .bw
        move.w  #$7fff,DMACON(a5)
        ifnd    MW_MUT_NOVEC
        lea     mwst+A_SVEC,a0
        lea     $64.w,a1
        moveq   #6,d0
.v:     move.l  (a0)+,(a1)+
        dbra    d0,.v
        endc
        ifnd    MW_MUT_NOICR
        move.b  #$7f,CIAA_ICR
        move.b  mwst+A_MASKA,d0
        or.b    #$80,d0
        move.b  d0,CIAA_ICR
        move.b  #$7f,CIAB_ICR
        move.b  mwst+A_MASKB,d0
        or.b    #$80,d0
        move.b  d0,CIAB_ICR
        endc
        move.l  gfxbase,a0
        move.l  gb_copinit(a0),d0
        move.l  d0,mwst+A_COPINIT
        ifnd    MW_MUT_NOCOP
        beq.s   .nc
        cmp.l   #$200000,d0
        bhs.s   .nc
        move.l  d0,COP1LC(a5)
        move.w  d0,COPJMP1(a5)          ; (benign without: COP1LC is also taken
                                        ; at the next vertical blank, rev16-x R6)
.nc:
        endc
        move.w  mwst+A_SDMA,d0
        ifd     MW_MUT_NODMAEN
        and.w   #$fdff,d0
        endc
        or.w    #$8000,d0
        move.w  d0,DMACON(a5)
        ifnd    MW_MUT_NOINT
        move.w  mwst+A_SINT,d0
        or.w    #$8000,d0
        move.w  d0,INTENA(a5)
        endc
        move.w  #$2000,sr               ; IPL 0 again: Disable (INTEN off) holds
        rts                             ; them until Enable

; ---------------------------------------------------------------------------
; play: main.c's order; the exit checks after each frame
; ---------------------------------------------------------------------------
play:   jsr     _mod_init
        move.b  d0,mwst+A_MINIT
        beq.s   .ok
        move.b  #WHY_INIT,mwst+A_WHY
        rts
.ok:    jsr     _mod_publish
        jsr     _ui_init
        jsr     _mod_start
        clr.b   lmbup
.lp:    jsr     _mod_wait_frame
        jsr     _mod_publish
        pea     VIEW
        jsr     _ui_frame
        addq.l  #4,sp
        addq.l  #1,mwst+A_FRAMES
        btst    #6,CIAA_PRA             ; /FIR0: 0 = the left button down
        beq.s   .dn
        st      lmbup
        bra.s   .nb
.dn:
        ifnd    MW_MUT_NOGATE
        tst.b   lmbup                   ; a press counts after a release and
        beq.s   .nb                     ; the grace frames
        cmp.l   #GRACE,mwst+A_FRAMES
        bls.s   .nb
        endc
        move.b  #WHY_LMB,mwst+A_WHY
        bra.s   .x
.nb:    btst    #1,VIEW+34              ; MV_LOOPED
        beq.s   .nl
        move.b  #WHY_LOOP,mwst+A_WHY
        bra.s   .x
.nl:    tst.b   _mod_done
        beq.s   .lp
        move.b  #WHY_END,mwst+A_WHY
.x:     jmp     _mod_publish

; lmb_wait: the left button up before the OS runs again (rev16-x HIGH-1).
; input.device last saw it up (a takeover exit needs a release before the
; press), so a press it saw at Enable would be a click on the Workbench,
; most likely on our own icon.  At most LMB_WAIT frames (CIA-A TOD: it
; counts frames with every interrupt off); a button held longer is seen.
; d0/d2 used.
lmb_wait:
        moveq   #LMB_WAIT-1,d2
.w:     move.b  CIAA_TODLO,d0
.f:     btst    #6,CIAA_PRA
        bne.s   .r
        cmp.b   CIAA_TODLO,d0
        beq.s   .f
        dbra    d2,.w
.r:     rts

; ---------------------------------------------------------------------------
; release: everything run took, in the reverse order (any state)
; ---------------------------------------------------------------------------
release:
        tst.b   ownblit
        beq.s   .nb
        move.l  gfxbase,a6
        CALL    DisownBlitter
.nb:    tst.b   audopen
        beq.s   .na
        move.l  sysbase,a6
        lea     ioa,a1
        CALL    CloseDevice             ; frees the channels
.na:    move.l  aport,d0
        beq.s   .np
        move.l  sysbase,a6
        move.l  d0,a0
        CALL    DeleteMsgPort
.np:    moveq   #1,d4                   ; the kept timers: ciaa TB, ciab TA
.ci:    lea     mwst+A_CLAIM,a0
        tst.b   0(a0,d4.w)
        bne.s   .cn
        move.l  ciaabase,a6
        cmp.w   #2,d4
        blo.s   .ca
        move.l  ciabbase,a6
.ca:    moveq   #1,d0
        and.w   d4,d0
        move.w  d4,d1
        mulu.w  #IS_SIZE,d1
        lea     ints,a1
        add.w   d1,a1
        CALL    RemICRVector
.cn:    addq.w  #1,d4
        cmp.w   #3,d4
        blo.s   .ci
        move.l  fh,d1
        beq.s   .nf
        move.l  dosbase,a6
        CALL    Close
.nf:    move.l  mwst+A_MOD,d0
        beq.s   .nm
        move.l  sysbase,a6
        move.l  d0,a1
        move.l  mwst+A_MODLEN,d0
        CALL    FreeMem
.nm:
        ifd     MOD_TRACE
        move.l  trbuf,d0
        beq.s   .nt
        move.l  sysbase,a6
        move.l  d0,a1
        move.l  #TR_TEND-TR_W,d0
        CALL    FreeMem
.nt:
        endc
        tst.b   cdset
        beq.s   .nd
        move.l  dosbase,a6
        move.l  olddir,d1
        CALL    CurrentDir
.nd:    move.l  sysbase,a6
        lea     intbase,a2
        moveq   #2,d2
.cl:    move.l  (a2)+,d0                ; intuition, graphics, dos
        beq.s   .cx
        move.l  d0,a1
        CALL    CloseLibrary
.cx:    dbra    d2,.cl
        cmp.b   #ST_FAILED,mwst+A_STATE
        beq.s   .r
        moveq   #ST_DONE,d0
        bra     set_state
.r:     rts

; report: a requester saying why (the anchor already says it)
report: move.l  intbase,d0
        beq.s   .r
        move.l  d0,a6
        moveq   #0,d0
        move.b  mwst+A_ERR,d0
        add.w   d0,d0
        lea     errtab(pc),a0
        move.w  -2(a0,d0.w),d0
        lea     errtab(pc),a0
        add.w   d0,a0
        lea     easy,a1
        move.l  #20,es_StructSize(a1)
        move.l  #title,es_Title(a1)
        move.l  a0,es_TextFormat(a1)
        move.l  #okgad,es_GadgetFormat(a1)
        sub.l   a0,a0
        sub.l   a2,a2
        lea     easyargs,a3             ; %s: the file's name
        move.l  name,(a3)
        CALL    EasyRequestArgs
.r:     rts

errtab: dc.w    e1-errtab,e2-errtab,e3-errtab,e4-errtab,e5-errtab,e6-errtab,e7-errtab
        dc.w    e8-errtab,e9-errtab
e1:     dc.b    "A system library did not open.",0
e2:     dc.b    "The MOD file did not open:",10,"%s",0
e3:     dc.b    "The MOD is too small or too big, or chip RAM is short:",10,"%s",0
e4:     dc.b    "The MOD could not be read:",10,"%s",0
e5:     dc.b    "The file is not a 4-channel M.K. MOD:",10,"%s",0
e6:     dc.b    "A CIA timer the player needs is in use.",0
e7:     dc.b    "The player could not parse this MOD:",10,"%s",0
e8:     dc.b    "The MOD player is already running.",0
e9:     dc.b    "Trace build: the trace RAM is in use.",0
title:  dc.b    "Amiga64 MOD player",0
okgad:  dc.b    "OK",0
dosname:        dc.b    "dos.library",0
gfxname:        dc.b    "graphics.library",0
intname:        dc.b    "intuition.library",0
ciaaname:       dc.b    "ciaa.resource",0
ciabname:       dc.b    "ciab.resource",0
audname:        dc.b    "audio.device",0
defname:        dc.b    "ModPlayer.mod",0
dotmod:         dc.b    ".mod",0
intname_s:      dc.b    "modwb",0
        even

; ===========================================================================
        section DATA,data
allocmap:       dc.b    $0f
        even

        section mwchip,data_c           ; the state struct (the anchor's target):
mwst:   dcb.b   A_SIZE,0                ; chip RAM, first in its hunk
        even

        section BSS,bss
sysbase:        ds.l    1
intbase:        ds.l    1               ; (release closes these three in order)
gfxbase:        ds.l    1
dosbase:        ds.l    1
ciaabase:       ds.l    1
ciabbase:       ds.l    1
wbmsg:          ds.l    1
cliarg:         ds.l    1
clilen:         ds.l    1
aseq:           ds.w    1
name:           ds.l    1
fh:             ds.l    1
olddir:         ds.l    1
aport:          ds.l    1
oldssp:         ds.l    1
oldsp:          ds.l    1
cdset:          ds.b    1
ownblit:        ds.b    1
profok:         ds.b    1
anch:           ds.b    1               ; we published the anchor
busy:           ds.b    1               ; another instance's anchor: back out
audopen:        ds.b    1
lmbup:          ds.b    1
        ifd     MOD_TRACE
        cnop    0,4
trbuf:          ds.l    1
        endc
easyargs:       ds.l    1
ints:           ds.b    4*IS_SIZE
        cnop    0,4
ioa:            ds.b    IOA_SIZE
easy:           ds.b    20
namebuf:        ds.b    108
        cnop    0,4
stack:          ds.b    STACK_SIZE
stack_end:
