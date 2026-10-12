; fixtures/mod/replay.s — the MOD replay (W15-M, docs/WAVE15_BRIEF.md M2).
;
; Implements host/mod_ref.py exactly: its SEMANTICS and its Paula write
; protocol (per tick: DMACON clear of the triggered channels, their
; LC/LEN, PER/VOL where changed or triggered; then two CIA-A timer B
; one-shots STAGE_E apart: DMACON set, then the repeat LC/LEN).  MOD
; format facts from a public format description; ProTracker-compatible
; behaviour from general knowledge; no replay source read (provenance:
; build/evidence/w15-mod/PROGRESS.md).
;
; Timing (supervisor rulings, brief addendum):
;   * flag PF_REALTIME clear: VBL ticks (one owed per VERTB) until an
;     Fxx >= 32; then CIA-B timer A (level 6) at tick_e * 125 / BPM E;
;   * flag set: every tick from CIA-B TA, period tick_e * 125 / B
;     (B = 125 until the song sets a CIA tempo).
;   CIA-B TB counts TA underflows (a period above 16 bits counts n
;   underflows per tick).  The first handler entry with ticks owed takes
;   the busy guard and runs them all at IPL 2 (run_loop/tick_loop), so
;   VERTB and EXTER are always delivered (a nested entry acks, counts and
;   returns) and a late UI frame never slows the tempo.  NO CAP: under
;   sustained overload (ticks costing more than their period) the loop
;   never ends, so main and ui_frame starve (the display freezes, the
;   music stays exact but runs late); UI_DIV does not help there, it only
;   thins the UI calls when the UI itself is the load.
;   The DMA stages (CIA-A TB one-shot, level 2) take the same guard; the
;   one-shot is armed at run_loop's end, never inside a tick, and a tick
;   that finds stages still pending runs them first (S_FLUSH), so the
;   write order is always tick N, its stages, tick N + 1.
;
; C entries (vbcc ABI: d0-d1/a0-a1 scratch): int mod_init(void),
; void mod_start(void), void mod_wait_frame(void), void mod_publish(void),
; unsigned char mod_done.
;
; Test builds: MOD_TRACE (the trace in slow RAM, mod.i), MOD_STUB (red: no
; replay), MOD_MUT_* (the mutants, tests/run_mod.py).

        include "mod.i"

; --- channel state (chans: 4 x CH_SIZE) ---
C_PERIOD        equ     0       ; w
C_OUTPER        equ     2       ; w
C_PTARGET       equ     4       ; w
C_RPER          equ     6       ; w row's period
C_WPER          equ     8       ; w last PER written
C_OFFMEM        equ     10      ; w 9xx memory (bytes)
C_TLEN          equ     12      ; w trigger: length (words)
C_RLEN          equ     14      ; w repeat length (words)
C_TLC           equ     16      ; l trigger: start
C_RLC           equ     20      ; l repeat start
C_INST          equ     24
C_NOTE          equ     25
C_FT            equ     26      ; signed
C_VOL           equ     27
C_OUTVOL        equ     28
C_PSPEED        equ     29
C_VSPEED        equ     30
C_VDEPTH        equ     31
C_VPOS          equ     32
C_VWAVE         equ     33
C_TSPEED        equ     34
C_TDEPTH        equ     35
C_TPOS          equ     36
C_TWAVE         equ     37
C_LOOPROW       equ     38
C_LOOPCNT       equ     39
C_VU            equ     40
C_RINST         equ     41
C_REFF          equ     42
C_RPAR          equ     43
C_RNOTE         equ     44
C_WVOL          equ     45
C_TRIGD         equ     46
C_PNOTE         equ     47
C_FXRUN         equ     48      ; nonzero: the row's effect runs on ticks > 0
C_PTAB          equ     50      ; l the period row of the channel's finetune
CH_SIZE         equ     54

; --- instruments (insts: 31 x IN_SIZE) ---
IN_START        equ     0       ; l
IN_LEN          equ     4       ; l bytes
IN_RLC          equ     8       ; l repeat start
IN_RLENW        equ     12      ; w repeat length (words)
IN_FT           equ     14
IN_VOL          equ     15
IN_LOOPED       equ     16
IN_PTAB         equ     18      ; l the period row of its finetune
IN_SIZE         equ     22

; --- globals (G) ---
G_TICKS         equ     0       ; l
G_ACC           equ     4       ; l elapsed 16.16
G_INC           equ     8       ; l
G_MODBASE       equ     12      ; l
G_ORDER         equ     16      ; l
G_PATBASE       equ     20      ; l
G_TWP           equ     24      ; l trace write stream
G_TTP           equ     28      ; l trace tick records
G_MAXT          equ     32      ; l
G_MAXF          equ     36      ; l
G_FRAMES        equ     40      ; l
G_T             equ     44      ; w tick within the row
G_ROWTICKS      equ     46      ; w
G_TICKE         equ     48      ; w
G_NSPLIT        equ     50      ; w underflows per tick
G_UNDER         equ     52      ; w underflows owed
G_TBLAST        equ     54      ; w
G_INCDUR        equ     56      ; w the B of G_INC
G_TDUR          equ     58      ; w the B the timer runs at (0 = timer off)
G_SPEED         equ     60
G_BPM           equ     61
G_CIA           equ     62
G_CIANEXT       equ     63
G_POS           equ     64
G_ROW           equ     65
G_LOOPED        equ     66
G_HALTED        equ     67
G_LOOPFLAG      equ     68
G_NEXTPOS       equ     69
G_NEXTROW       equ     70
G_NEWPOS        equ     71
G_SONGLEN       equ     72
G_RESTART       equ     73
G_TRIGM         equ     74
G_PLAYM         equ     75
G_STAGE         equ     76
G_STGM          equ     77
G_FLAGS         equ     78
G_DELAY         equ     80
G_BRK           equ     81      ; $FF = none
G_JMP           equ     82
G_JPOS          equ     83
G_LOOPTO        equ     84      ; $FF = none
G_RT            equ     85
G_TRACE         equ     86
G_DIRTY         equ     87      ; channels the effects pass touched this tick
G_RUNM          equ     96      ; channels whose row effect runs on ticks > 0
G_UIDIV         equ     97      ; UI_DIV (>= 1)
G_BUSY          equ     98      ; bit 0: run_loop owns the ticks
G_UIFR          equ     100     ; l G_FRAMES at the last UI call
G_VFR           equ     104     ; l VBL tempo: the frames already turned into ticks
G_TOD           equ     88      ; l trace: CIA-A TOD (frames) at the tick's start
G_BEAM          equ     92      ; l trace: VPOSR:VHPOSR with it
G_SIZE          equ     108

; PWA value,disp / PWAL value,disp: a Paula write to the channel registers
; at a4 (= CUSTOM + AUD0LC + 16 * channel); the trace build logs it too
PWA     macro
        move.w  \1,\2(a4)
        ifd     MOD_TRACE
        movem.l d0-d1,-(sp)
        move.w  \1,d1
        move.l  a4,d0
        sub.l   a5,d0
        add.w   #\2,d0
        bsr     tr_w
        movem.l (sp)+,d0-d1
        endc
        endm

PWAL    macro
        move.l  \1,\2(a4)
        ifd     MOD_TRACE
        movem.l d0-d1,-(sp)
        move.l  \1,d1
        move.l  a4,d0
        sub.l   a5,d0
        add.w   #\2,d0
        swap    d1
        bsr     tr_w
        swap    d1
        addq.w  #2,d0
        bsr     tr_w
        movem.l (sp)+,d0-d1
        endc
        endm

INT_MINE        equ     $2028   ; INTENA/INTREQ bits W15-M owns: EXTER VERTB PORTS
MIN_PER         equ     113
PER_LO          equ     107             ; M1: output periods clamped to the
PER_HI          equ     907             ; finetune table's range
MOD_END         equ     $200000         ; M1: the MOD area ends at $1FFFFF
MAX_PER         equ     856

        section CODE,code

        xdef    _mod_init,_mod_start,_mod_wait_frame,_mod_publish,_mod_done
        xdef    l2_ciaa,l3_vbl,l6_ciab

; ===========================================================================
; Paula writes.  pw: d0.w register offset, d1.w value; keeps every
; register but a0.  (The channel registers: the PWA/PWAL macros.)
; ===========================================================================
pw:     move.w  d1,0(a5,d0.w)
        ifd     MOD_TRACE
        bra     tr_w
        else
        rts
        endc

        ifd     MOD_TRACE
; tr_w: append d0.w:d1.w to the write stream (keeps all but a0)
tr_w:   tst.b   G_TRACE(a6)
        beq.s   .r
        move.l  G_TWP(a6),a0
        cmp.l   #TR_WEND,a0
        bhs.s   .ovf
        move.w  d0,(a0)+
        move.w  d1,(a0)+
        move.l  a0,G_TWP(a6)
        move.l  a0,STATUS+S_TWP
.r:     rts
.ovf:   st      STATUS+S_TOVF
        rts

; tr_time: G_TOD/G_BEAM := CIA-A TOD (+1 per frame, in the machine loop's
; pass that also restarts the beam) and the beam, read again on a change
tr_time:
        moveq   #2,d2                   ; at most 3 tries (s = 0 on a 1 MHz host:
.a:     bsr.s   .tod                    ; a frame can pass between any two reads)
        move.l  d1,G_TOD(a6)
        move.l  VPOSR(a5),G_BEAM(a6)
        bsr.s   .tod
        cmp.l   G_TOD(a6),d1
        ifd     MOD_MUT_TRTIME
        bne.s   .a                      ; mutant: the unbounded retry (s = 0 livelock)
        endc
        dbeq    d2,.a
        beq.s   .ok
        bset    #7,G_TOD(a6)            ; torn: bit 31, the host skips it
.ok:    rts
.tod:   moveq   #0,d1
        move.b  CIAA_TODHI,d1           ; latches until TODLO
        swap    d1
        move.b  CIAA_TODMID,d0
        lsl.w   #8,d0
        move.b  CIAA_TODLO,d0
        move.w  d0,d1
        rts

; tr_tick: the tick record (view, TOD, beam)
tr_tick:
        tst.b   G_TRACE(a6)
        beq.s   .r
        move.l  G_TTP(a6),a0
        cmp.l   #TR_TEND-TR_REC,a0
        bhi.s   .ovf
        lea     shadow,a1
        moveq   #VIEW_LEN/4-1,d0
.c:     move.l  (a1)+,(a0)+
        dbra    d0,.c
        move.l  G_TOD(a6),(a0)+
        move.l  G_BEAM(a6),(a0)+
        move.l  a0,G_TTP(a6)
        move.l  a0,STATUS+S_TTP
.r:     rts
.ovf:   st      STATUS+S_TOVF
        rts
        endc

; ===========================================================================
; div32: d2.l := d2.l / d3.w (unsigned; d3 nonzero).  Clobbers d4.
; ===========================================================================
div32:  move.l  d5,-(sp)
        moveq   #0,d4
        swap    d2
        move.w  d2,d4
        divu    d3,d4                   ; r1:q1
        move.w  d4,d5
        swap    d2
        move.w  d2,d4                   ; r1:lo
        divu    d3,d4                   ; r2:q2
        swap    d5
        move.w  d4,d5
        move.l  d5,d2
        move.l  (sp)+,d5
        rts

; ===========================================================================
; int mod_init(void): parse the MOD named by the parameter block.
; 0 = ok; 1 no parameter block, 2 not M.K., 3 song length, 4 truncated.
; ===========================================================================
_mod_init:
        movem.l d2-d7/a2-a6,-(sp)
        lea     CUSTOM,a5               ; the clean slate: BEFORE ui_init (W15-U
        move.w  #$7fff,INTENA(a5)       ; ruling); from mod_start on, only the
        move.w  #$7fff,INTREQ(a5)       ; bits W15-M owns are written
        lea     G,a6
        lea     PARAM,a1
        moveq   #1,d0
        cmp.l   #'MPAR',P_MAGIC(a1)
        bne     .err
        moveq   #5,d0                   ; M1: the MOD address: even, in the MOD area
        move.l  P_MOD(a1),a0
        move.l  a0,d1
        btst    #0,d1
        bne     .err
        cmp.l   #MOD_END,d1
        bhs     .err
        move.l  #MOD_END,d5             ; d5 = usable length: min(P_LEN, MOD_END - base)
        sub.l   d1,d5
        cmp.l   P_LEN(a1),d5
        bls.s   .av
        move.l  P_LEN(a1),d5
.av:    move.l  a0,G_MODBASE(a6)
        move.w  P_FLAGS(a1),d0
        move.b  d0,G_FLAGS(a6)
        btst    #PF_REALTIME,d0
        sne     G_RT(a6)
        ifd     MOD_TRACE
        btst    #PF_TRACE,d0
        sne     G_TRACE(a6)
        endc
        move.w  P_TICKE(a1),d0
        bne.s   .te
        move.w  #14188,d0
.te:    move.w  d0,G_TICKE(a6)
        move.l  P_MAXT(a1),G_MAXT(a6)
        move.l  P_MAXF(a1),G_MAXF(a6)
        move.b  P_UIDIV(a1),d0          ; UI_DIV: 0 counts as 1
        bne.s   .ud
        moveq   #1,d0
.ud:    move.b  d0,G_UIDIV(a6)
        moveq   #2,d0
        cmp.l   #1084,d5
        blo     .err
        cmp.l   #'M.K.',1080(a0)
        bne     .err
        moveq   #3,d0
        move.b  950(a0),d1
        beq     .err
        cmp.b   #128,d1
        bhi     .err
        move.b  d1,G_SONGLEN(a6)
        move.b  951(a0),G_RESTART(a6)
        lea     952(a0),a2
        move.l  a2,G_ORDER(a6)
        moveq   #0,d2                   ; npat = max(order) + 1
        moveq   #127,d1
.mx:    cmp.b   (a2)+,d2
        bhs.s   .mx1
        move.b  -1(a2),d2
.mx1:   dbra    d1,.mx
        addq.w  #1,d2
        lea     1084(a0),a2
        move.l  a2,G_PATBASE(a6)
        moveq   #10,d1
        lsl.l   d1,d2
        add.l   #1084,d2                ; d2 = offset of the sample data
        moveq   #4,d0
        cmp.l   d5,d2
        bhi     .err
        ; instruments
        lea     insts,a3
        lea     20(a0),a2               ; record 1
        moveq   #30,d7
.in:    moveq   #0,d6
        move.w  22(a2),d6               ; length (words)
        add.l   d6,d6                   ; d6 = the nominal length (bytes)
        move.l  a0,d3
        add.l   d2,d3
        move.l  d3,IN_START(a3)
        move.l  d5,d1                   ; M1: clamped to the usable length
        sub.l   d2,d1
        bpl.s   .cl
        moveq   #0,d1
.cl:
        ifnd    MOD_MUT_ODD
        bclr    #0,d1                   ; (mutant rev15-mod: an odd length kept)
        endc
        cmp.l   d6,d1
        bls.s   .cl2
        move.l  d6,d1
.cl2:   move.l  d1,IN_LEN(a3)           ; d1 = the clamped length
        add.l   d6,d2
        move.b  24(a2),d0
        andi.b  #$0f,d0
        btst    #3,d0
        beq.s   .ft
        subi.b  #16,d0
.ft:    move.b  d0,IN_FT(a3)
        ext.w   d0
        addq.w  #8,d0
        mulu.w  #72,d0
        lea     ptab,a4
        add.w   d0,a4
        move.l  a4,IN_PTAB(a3)
        move.b  25(a2),d0
        cmp.b   #64,d0
        bls.s   .vo
        moveq   #64,d0
.vo:    move.b  d0,IN_VOL(a3)
        moveq   #0,d0
        move.w  26(a2),d0               ; repeat start (words)
        add.l   d0,d0
        moveq   #0,d4
        move.w  28(a2),d4               ; repeat length (words)
        add.l   d4,d4
        cmp.l   #2,d4                   ; <= 1 word: no loop
        bls.s   .one
        cmp.l   d1,d0                   ; M1: a loop starting past the end: none
        bhs.s   .one
        move.l  d1,d6
        sub.l   d0,d6
        cmp.l   d6,d4                   ; M1: a loop running past the end: cut
        bls.s   .rl
        move.l  d6,d4
.rl:    cmp.l   #2,d4
        bls.s   .one
        st      IN_LOOPED(a3)
        add.l   d0,d3
        move.l  d3,IN_RLC(a3)
        lsr.l   #1,d4
        move.w  d4,IN_RLENW(a3)
        bra.s   .inx
.one:   sf      IN_LOOPED(a3)
        move.l  d3,IN_RLC(a3)           ; one-shot: the first word
        move.w  #1,IN_RLENW(a3)
.inx:   lea     30(a2),a2
        lea     IN_SIZE(a3),a3
        dbra    d7,.in
        ; state
        move.b  #6,G_SPEED(a6)
        move.b  #125,G_BPM(a6)
        move.b  P_START(a1),d0
        cmp.b   G_SONGLEN(a6),d0
        blo.s   .sp
        moveq   #0,d0
.sp:    move.b  d0,G_POS(a6)
        move.w  #125,G_INCDUR(a6)
        move.l  #$10000,G_INC(a6)
        st      G_BRK(a6)
        lea     chans,a3
        moveq   #3,d7
.ch:    st      C_RNOTE(a3)
        move.l  #ptab+8*72,C_PTAB(a3)   ; finetune 0
        lea     CH_SIZE(a3),a3
        dbra    d7,.ch
        ifd     MOD_TRACE
        move.l  #TR_W,G_TWP(a6)
        move.l  #TR_T,G_TTP(a6)
        move.l  #TR_W,STATUS+S_TWP
        move.l  #TR_T,STATUS+S_TTP
        endc
        ; the shadow view's constant part
        lea     shadow,a1
        move.l  #'MVW1',(a1)
        moveq   #19,d1
        lea     8(a1),a2
.nm:    move.b  (a0)+,(a2)+
        dbra    d1,.nm
        move.b  G_SONGLEN(a6),28(a1)
        lea     CUSTOM,a5
        bsr     build_view
        moveq   #0,d0
.err:   movem.l (sp)+,d2-d7/a2-a6
        rts

; ===========================================================================
; void mod_start(void): the initial writes, the CIAs, the interrupts.
; ===========================================================================
_mod_start:
        movem.l d2-d7/a2-a6,-(sp)
        lea     G,a6
        lea     CUSTOM,a5
        move.w  #INT_MINE,INTENA(a5)    ; CLR: VERTB EXTER PORTS only
        move.w  #INT_MINE,INTREQ(a5)
        move.w  #DMACON,d0              ; CLR AUD0-3EN only (never DMAEN etc.)
        moveq   #$0f,d1
        bsr     pw
        moveq   #0,d1
        move.w  #AUD0LC+8,d0
        moveq   #3,d7
.v:     bsr     pw
        add.w   #$10,d0
        dbra    d7,.v
        move.b  #$7f,CIAA_ICR
        move.b  #$08,CIAA_CRB           ; timer B one-shot, stopped
        move.b  #$82,CIAA_ICR           ; mask: TB
        move.b  #$7f,CIAB_ICR
        clr.b   CIAB_CRA
        clr.b   CIAB_CRB
        bsr     timer_update            ; PF_REALTIME: the tick timer now
        move.w  #$c000|INT_MINE,INTENA(a5)  ; SET INTEN EXTER VERTB PORTS
        movem.l (sp)+,d2-d7/a2-a6
        rts

; ===========================================================================
; void mod_wait_frame(void): sleep until UI_DIV VERTBs have passed since
; the last return (or the end).  UI_DIV (param P_UIDIV) is the UI's safety
; valve: a slow ui_frame only lags the display; the replay is interrupt
; driven and never waits for it.
; ===========================================================================
_mod_wait_frame:
.w:     move.w  #$2700,sr
        move.l  G+G_FRAMES,d0
        sub.l   G+G_UIFR,d0
        moveq   #0,d1
        move.b  G+G_UIDIV,d1
        cmp.l   d1,d0
        bhs.s   .got
        tst.b   _mod_done
        bne.s   .got
        stop    #$2000
        bra.s   .w
.got:   move.l  G+G_FRAMES,G+G_UIFR
        move.l  G+G_FRAMES,STATUS+S_FRAMES
        move.w  #$2000,sr
        rts

; ===========================================================================
; void mod_publish(void): shadow -> $000400, one whole tick (SR $2700).
; ===========================================================================
_mod_publish:
        move.w  sr,-(sp)
        move.w  #$2700,sr
        lea     shadow,a0
        lea     VIEW,a1
        rept    VIEW_LEN/4
        move.l  (a0)+,(a1)+
        endr
        move.w  (sp)+,sr
        rts

; ===========================================================================
; Interrupt handlers
; ===========================================================================
; The tick sources (s = 0 fix, supervisor round 2): neither handler runs
; the replay at its own level.  The first entry with ticks owed takes the
; busy guard and runs tick_loop at IPL 2 (run_loop), so VERTB (3) and
; EXTER (6) stay deliverable: a nested entry only acknowledges, counts
; and returns (a source left pending under a running handler makes every
; machine-loop pass clamp to MACH_POLL: measured, 0 ticks in 120 s at
; s = 0 on VICE).  The level-2 stages stay masked during the loop.

l3_vbl: move.w  #$0020,CUSTOM+INTREQ   ; the nested (busy) path: 3 instructions
        addq.l  #1,G+G_FRAMES
        tst.b   G+G_BUSY
        bne.s   .rte
        tst.b   G+G_RT                  ; CIA tempo: the ticks are L6's (the
        bne.s   .rte                    ; frame limit is checked in its loop)
        tst.w   G+G_TDUR
        bne.s   .rte
        tst.b   _mod_done
        bne.s   .rte
        movem.l d0-d7/a0-a6,-(sp)
        lea     CUSTOM,a5
        lea     G,a6
        bsr     run_loop
        movem.l (sp)+,d0-d7/a0-a6
.rte:   rte

l6_ciab:
        tst.b   CIAB_ICR                ; acknowledge (releases the hold)
        move.w  #$2000,CUSTOM+INTREQ    ; EXTER, after the ICR read
        addq.w  #1,STATUS+S_L6
        tst.b   G+G_BUSY
        bne.s   .rte
        tst.b   _mod_done
        bne.s   .rte
        tst.w   G+G_TDUR
        beq.s   .rte
        movem.l d0-d7/a0-a6,-(sp)
        lea     CUSTOM,a5
        lea     G,a6
        bsr     run_loop
        movem.l (sp)+,d0-d7/a0-a6
.rte:   rte

; run_loop: from a handler (regs saved, a5/a6 set).  Runs every owed tick
; at IPL 2, re-checks at IPL 7 before it lets go of the guard, then arms
; the pending DMA stage (never inside a tick) and ends the run if halted.
run_loop:
        bset    #0,G_BUSY(a6)
        bne.s   .r
        moveq   #0,d6                   ; ticks this run (S_MAXCU)
.again:
        ifnd    MOD_MUT_FULLIPL
        move.w  #$2200,sr               ; VERTB and EXTER deliverable
        endc
        bsr     tick_loop
        move.w  #$2700,sr
        tst.b   G_HALTED(a6)
        bne.s   .end
        tst.w   G_TDUR(a6)
        bne.s   .cia
        tst.b   G_RT(a6)
        bne.s   .end
        move.l  G_FRAMES(a6),d0         ; VBL ticks owed meanwhile: go again
        cmp.l   G_VFR(a6),d0
        bne.s   .again
        bra.s   .end
.cia:   bsr     read_tb
        cmp.w   G_TBLAST(a6),d1
        bne.s   .again
.end:
        ifd     MOD_TRACE
        move.l  G_FRAMES(a6),STATUS+S_FRAMES
        cmp.b   STATUS+S_MAXCU,d6
        bls.s   .m
        move.b  d6,STATUS+S_MAXCU
.m:
        endc
        ifnd    MOD_MUT_EMITARM
        cmp.b   #1,G_STAGE(a6)          ; the stages: from the loop's end
        bne.s   .h
        bsr     stage_arm
.h:
        endc
        tst.b   G_HALTED(a6)
        beq.s   .c
        tst.b   _mod_done
        bne.s   .c
        bsr     finish
.c:     clr.b   G_BUSY(a6)
.r:     rts

; tick_loop: the ticks owed now (d6 counts them; keeps d6)
tick_loop:
.lp:
        ifd     MOD_TRACE
        move.l  G_MAXF(a6),d0           ; the frame limit (test builds' safety net)
        beq.s   .nl
        cmp.l   G_FRAMES(a6),d0
        bhi.s   .nl
        st      STATUS+S_FLIM
        st      G_HALTED(a6)
.nl:
        endc
        tst.b   G_HALTED(a6)
        bne.s   .r
        tst.w   G_TDUR(a6)
        beq.s   .vbl
        bsr     read_tb                 ; CIA tempo: underflows since the last look
        move.w  G_TBLAST(a6),d0
        sub.w   d1,d0
        move.w  d1,G_TBLAST(a6)
        add.w   d0,G_UNDER(a6)
        move.w  G_UNDER(a6),d0
        cmp.w   G_NSPLIT(a6),d0
        blo.s   .vbl
        move.w  G_NSPLIT(a6),d0
        sub.w   d0,G_UNDER(a6)
        bra.s   .tk
.vbl:   tst.w   G_TDUR(a6)              ; VBL tempo: one tick per VERTB
        bne.s   .r
        tst.b   G_RT(a6)
        bne.s   .r
        move.l  G_FRAMES(a6),d0
        cmp.l   G_VFR(a6),d0
        beq.s   .r
        addq.l  #1,G_VFR(a6)
.tk:    addq.w  #1,d6
        move.w  d6,-(sp)
        bsr     do_tick
        bsr     timer_update
        move.w  (sp)+,d6
        bra.s   .lp
.r:     rts

; read_tb: d1.w := CIA-B timer B (consistent: high byte read around low)
read_tb:
.rd:    moveq   #0,d1
        move.b  CIAB_TBHI,d1
        move.b  CIAB_TBLO,d2
        move.b  CIAB_TBHI,d3
        cmp.b   d1,d3
        bne.s   .rd
        lsl.w   #8,d1
        move.b  d2,d1
        rts

l2_ciaa:
        movem.l d0-d7/a0-a6,-(sp)
        lea     CUSTOM,a5
        lea     G,a6
        move.b  CIAA_ICR,d0
        move.w  #$0008,INTREQ(a5)
        bset    #0,G_BUSY(a6)           ; atomic against the ticks by the guard,
        bne.s   .x                      ; not by the IPL (nothing left pending)
        bsr     stage_step
        cmp.b   #2,G_STAGE(a6)
        bne.s   .c
        bsr     stage_arm
.c:     clr.b   G_BUSY(a6)
        tst.b   _mod_done
        bne.s   .x
        bsr     run_loop                ; ticks owed meanwhile
.x:     movem.l (sp)+,d0-d7/a0-a6
        rte

; ===========================================================================
; The DMA stages
; ===========================================================================
stage_arm:
        move.b  #STAGE_E&$ff,CIAA_TBLO
        move.b  #STAGE_E>>8,CIAA_TBHI     ; one-shot: loads and starts
        rts

; one stage: 1 = DMACON set, 2 = the repeat LC/LEN
stage_step:
        move.b  G_STAGE(a6),d0
        beq.s   .r
        cmp.b   #1,d0
        bne.s   .s2
        ifd     MOD_MUT_REPFIRST
        bsr     stage_rep
        endc
        moveq   #0,d1
        move.b  G_STGM(a6),d1
        ori.w   #$8000,d1
        move.w  #DMACON,d0
        bsr     pw
        move.b  #2,G_STAGE(a6)
.r:     rts
.s2:
        ifnd    MOD_MUT_REPFIRST
        bsr     stage_rep
        endc
        clr.b   G_STAGE(a6)
        rts

stage_rep:
        lea     chans,a3
        lea     AUD0LC(a5),a4
        moveq   #0,d7
.c:     btst    d7,G_STGM(a6)
        beq.s   .n
        move.l  C_RLC(a3),d1
        PWAL    d1,0
        move.w  C_RLEN(a3),d1
        PWA     d1,4
.n:     lea     CH_SIZE(a3),a3
        lea     16(a4),a4
        addq.w  #1,d7
        cmp.w   #4,d7
        blo.s   .c
        rts

; run every pending stage now (a new tick, or the end)
stage_flush:
        move.b  #$08,CIAA_CRB           ; stop the one-shot
        move.b  CIAA_ICR,d0             ; drop its flag
        move.w  #$0008,INTREQ(a5)
        addq.w  #1,STATUS+S_FLUSH
.l:     tst.b   G_STAGE(a6)
        beq.s   .r
        bsr     stage_step
        bra.s   .l
.r:     rts

; ===========================================================================
; The tick timer.  B = BPM once the CIA tempo is in use, else 125.  The
; timer runs when PF_REALTIME is set or the CIA tempo is in use.
; ===========================================================================
timer_update:
        moveq   #125,d0
        move.b  G_CIA(a6),d1
        or.b    G_CIANEXT(a6),d1
        beq.s   .b
        moveq   #0,d0
        move.b  G_BPM(a6),d0
        bra.s   .on
.b:     tst.b   G_RT(a6)
        beq.s   .r
.on:    cmp.w   G_TDUR(a6),d0
        beq.s   .r
        move.w  G_TDUR(a6),d6           ; 0 = start it
        move.w  d0,G_TDUR(a6)
        move.w  d0,d3
        moveq   #0,d2
        move.w  G_TICKE(a6),d2
        ifd     MOD_MUT_CIAPER
        mulu.w  #128,d2
        else
        mulu.w  #125,d2
        endc
        bsr     div32                   ; d2 = period (E)
        move.l  d2,d1
        add.l   #$ffff,d1
        clr.w   d1
        swap    d1                      ; n = ceil(period / 65536)
        move.w  d1,G_NSPLIT(a6)
        move.w  d1,d3
        bsr     div32
        subq.l  #1,d2                   ; latch = period / n - 1
        move.b  d2,CIAB_TALO
        lsr.w   #8,d2
        move.b  d2,CIAB_TAHI            ; running: the latch only
        tst.w   d6
        bne.s   .r
        st      CIAB_TBLO               ; TB latch $FFFF (stopped: loads)
        st      CIAB_TBHI
        move.w  #$ffff,G_TBLAST(a6)
        clr.w   G_UNDER(a6)
        move.l  G_FRAMES(a6),G_VFR(a6)  ; frames after the switch are no ticks
        move.b  #$51,CIAB_CRB           ; start, count TA underflows, load
        move.b  #$11,CIAB_CRA           ; start, continuous, load
        move.b  #$81,CIAB_ICR           ; mask: TA
.r:     rts

; ===========================================================================
; finish: the end (song end with PF_STOPEND, max ticks, max frames)
; ===========================================================================
finish:
        tst.b   G_STAGE(a6)
        beq.s   .ns
        bsr     stage_flush
.ns:
        ifd     MOD_TRACE
        move.w  #$fffe,d0
        moveq   #0,d1
        bsr     tr_w
        endc
        move.w  #DMACON,d0
        moveq   #$0f,d1
        bsr     pw
        clr.b   CIAB_CRA
        clr.b   CIAB_CRB
        move.b  #$7f,CIAB_ICR
        move.b  #$08,CIAA_CRB
        move.b  #$7f,CIAA_ICR
        move.w  #INT_MINE,INTENA(a5)    ; CLR only W15-M's sources
        bclr    #0,shadow+34            ; flags bit 0: no longer playing (L6)
        st      _mod_done
        move.l  G_FRAMES(a6),STATUS+S_FRAMES
        move.b  #2,STATUS+S_STATE
        rts

; ===========================================================================
; do_tick: one replay tick.  a5 = CUSTOM, a6 = G; clobbers d0-d7/a0-a4.
; ===========================================================================
do_tick:
        ifd     MOD_STUB
        rts
        endc
        tst.b   G_HALTED(a6)
        bne     .ret
        tst.b   G_STAGE(a6)
        beq.s   .ns
        bsr     stage_flush
.ns:
        ifd     MOD_TRACE
        bsr     tr_time
        move.l  G_TICKS(a6),d1
        addq.w  #1,d1
        move.w  #$ffff,d0
        bsr     tr_w
        endc
        clr.b   G_TRIGM(a6)
        clr.b   G_PLAYM(a6)
        move.b  G_CIANEXT(a6),d0
        or.b    d0,G_CIA(a6)
        tst.w   G_T(a6)
        bne.s   .eff
        clr.b   G_LOOPFLAG(a6)
        bsr     row_start
.eff:   bsr     effects
        bsr     emit
        addq.l  #1,G_TICKS(a6)
        move.l  G_TICKS(a6),STATUS+S_TICKS
        ; elapsed: B of the interval after this tick
        moveq   #125,d3
        move.b  G_CIA(a6),d0
        or.b    G_CIANEXT(a6),d0
        beq.s   .d
        moveq   #0,d3
        move.b  G_BPM(a6),d3
.d:     cmp.w   G_INCDUR(a6),d3
        beq.s   .inc
        move.w  d3,G_INCDUR(a6)
        move.l  #125<<16,d2
        bsr     div32
        move.l  d2,G_INC(a6)
.inc:   move.l  G_INC(a6),d0
        add.l   d0,G_ACC(a6)
        bsr     build_view
        ifd     MOD_TRACE
        bsr     tr_tick
        endc
        ; advance
        move.w  G_T(a6),d0
        addq.w  #1,d0
        cmp.w   G_ROWTICKS(a6),d0
        bhs.s   .row
        move.w  d0,G_T(a6)
        bra.s   .max
.row:   clr.w   G_T(a6)
        move.b  G_NEXTPOS(a6),d0
        cmp.b   G_SONGLEN(a6),d0
        blo.s   .inr
        move.b  G_RESTART(a6),d0
        ifd     MOD_MUT_R_NORESTART
        moveq   #0,d0                   ; mutant (rev15-mod): the restart byte ignored
        endc
        cmp.b   G_SONGLEN(a6),d0
        blo.s   .rs
        moveq   #0,d0
.rs:    st      G_LOOPFLAG(a6)
.inr:   tst.b   G_LOOPFLAG(a6)
        beq.s   .nl
        st      G_LOOPED(a6)
        btst    #PF_STOPEND,G_FLAGS(a6)
        beq.s   .nl
        st      G_HALTED(a6)
.nl:    tst.b   G_NEWPOS(a6)
        ifd     MOD_MUT_R_E6NORESET
        bra.s   .np                     ; mutant (rev15-mod): E6 marks survive a new position
        endc
        beq.s   .np
        lea     chans,a3
        clr.b   C_LOOPROW(a3)
        clr.b   C_LOOPROW+CH_SIZE(a3)
        clr.b   C_LOOPROW+2*CH_SIZE(a3)
        clr.b   C_LOOPROW+3*CH_SIZE(a3)
.np:    move.b  d0,G_POS(a6)
        move.b  G_NEXTROW(a6),G_ROW(a6)
.max:   move.l  G_MAXT(a6),d0
        beq.s   .h
        cmp.l   G_TICKS(a6),d0
        bhi.s   .h
        st      G_HALTED(a6)
.h:     tst.b   G_HALTED(a6)
        beq.s   .ret
        bsr     finish
.ret:   rts

; ---------------------------------------------------------------------------
; ptab_lookup: d1.b note (0-35) -> d1.w period of the channel's finetune.
; Clobbers d0, a0.
; ---------------------------------------------------------------------------
ptab_lookup:
        move.l  C_PTAB(a3),a0
        andi.w  #$ff,d1
        add.w   d1,d1
        move.w  0(a0,d1.w),d1
        rts

; ---------------------------------------------------------------------------
; note_on: trigger channel d7 (a3) with byte offset d0.w.  Clobbers d0, d1, a0.
; ---------------------------------------------------------------------------
note_on:
        bset    d7,G_TRIGM(a6)
        st      C_TRIGD(a3)
        btst    #2,C_VWAVE(a3)
        bne.s   .v
        clr.b   C_VPOS(a3)
.v:     btst    #2,C_TWAVE(a3)
        bne.s   .t
        clr.b   C_TPOS(a3)
.t:     moveq   #0,d1
        move.b  C_INST(a3),d1
        beq.s   .stop
        lea     insts,a0
        subq.w  #1,d1
        mulu.w  #IN_SIZE,d1
        add.w   d1,a0
        move.l  IN_LEN(a0),d1
        beq.s   .stop
        andi.l  #$ffff,d0
        cmp.l   d1,d0
        blo.s   .body
        tst.b   IN_LOOPED(a0)
        beq.s   .stop
        move.l  IN_RLC(a0),C_TLC(a3)
        move.w  IN_RLENW(a0),C_TLEN(a3)
        bra.s   .rep
.body:  sub.l   d0,d1
        lsr.l   #1,d1
        move.w  d1,C_TLEN(a3)
        move.l  IN_START(a0),d1
        add.l   d0,d1
        move.l  d1,C_TLC(a3)
.rep:   move.l  IN_RLC(a0),C_RLC(a3)
        move.w  IN_RLENW(a0),C_RLEN(a3)
        bset    d7,G_PLAYM(a6)
        rts
.stop:  bclr    d7,G_PLAYM(a6)
        rts

; ---------------------------------------------------------------------------
; row_start: tick 0 of a row (notes and the tick-0 effects; the next row).
; Per channel: d2 effect, d3 param, d4 period, d5 x, d6 y.
; ---------------------------------------------------------------------------
row_start:
        moveq   #0,d0
        move.b  G_POS(a6),d0
        move.l  G_ORDER(a6),a0
        moveq   #0,d1
        move.b  0(a0,d0.w),d1
        moveq   #10,d2
        lsl.l   d2,d1
        moveq   #0,d2
        move.b  G_ROW(a6),d2
        lsl.w   #4,d2
        add.l   d2,d1
        move.l  G_PATBASE(a6),a2
        add.l   d1,a2
        st      G_BRK(a6)
        clr.b   G_JMP(a6)
        st      G_LOOPTO(a6)
        clr.b   G_DELAY(a6)
        clr.b   G_RUNM(a6)
        lea     chans,a3
        moveq   #0,d7
.ch:    tst.l   (a2)                    ; an empty cell: nothing but the clears
        bne.s   .cell
        addq.l  #4,a2
        clr.w   C_RPER(a3)
        clr.b   C_RINST(a3)
        clr.b   C_REFF(a3)
        clr.b   C_RPAR(a3)
        st      C_RNOTE(a3)
        clr.b   C_FXRUN(a3)
        bra     .nx
.cell:  moveq   #0,d0
        move.b  (a2)+,d0
        moveq   #0,d1
        move.b  (a2)+,d1
        moveq   #0,d2
        move.b  (a2)+,d2
        moveq   #0,d3
        move.b  (a2)+,d3
        move.w  d0,d4
        andi.w  #$0f,d4
        lsl.w   #8,d4
        or.w    d1,d4                   ; period
        andi.w  #$f0,d0
        move.w  d2,d1
        lsr.w   #4,d1
        or.w    d1,d0                   ; instrument
        cmp.w   #31,d0                  ; M1: only 1-31 are instruments
        bls.s   .i31
        moveq   #0,d0
.i31:   andi.w  #$0f,d2                 ; effect
        move.w  d4,C_RPER(a3)
        move.b  d0,C_RINST(a3)
        move.b  d2,C_REFF(a3)
        move.b  d3,C_RPAR(a3)
        moveq   #-1,d1
        tst.w   d4
        beq.s   .nn
        moveq   #0,d1
        cmp.w   #1024,d4
        bhs.s   .nn
        lea     notetab,a0
        move.b  0(a0,d4.w),d1
.nn:    move.b  d1,C_RNOTE(a3)
        tst.b   d0
        beq.s   .ni
        move.b  d0,C_INST(a3)
        lea     insts,a0
        subq.w  #1,d0
        mulu.w  #IN_SIZE,d0
        add.w   d0,a0
        move.b  IN_VOL(a0),C_VOL(a3)
        move.b  IN_FT(a0),C_FT(a3)
        move.l  IN_PTAB(a0),C_PTAB(a3)
.ni:    move.w  d3,d5
        lsr.w   #4,d5                   ; x
        move.w  d3,d6
        andi.w  #$0f,d6                 ; y
        cmp.b   #$e,d2                  ; E5x: finetune first
        bne.s   .n5
        cmp.b   #5,d5
        bne.s   .n5
        move.b  d6,d0
        btst    #3,d0
        beq.s   .f5
        subi.b  #16,d0
.f5:    move.b  d0,C_FT(a3)
        ext.w   d0
        addq.w  #8,d0
        mulu.w  #72,d0
        lea     ptab,a0
        add.w   d0,a0
        move.l  a0,C_PTAB(a3)
.n5:    tst.w   d4
        beq.s   .np
        move.b  C_RNOTE(a3),d1
        bsr     ptab_lookup             ; d1 = target
        cmp.b   #3,d2
        beq.s   .tp
        cmp.b   #5,d2
        beq.s   .tp
        cmp.b   #$e,d2
        bne.s   .tr
        cmp.b   #$d,d5
        bne.s   .tr
        tst.b   d6
        bne.s   .fx                     ; EDy: at tick y
.tr:    move.w  d1,C_PERIOD(a3)
        move.b  C_RNOTE(a3),C_NOTE(a3)
        moveq   #0,d0
        cmp.b   #9,d2
        bne.s   .on
        tst.b   d3
        beq.s   .om
        move.w  d3,d1
        lsl.w   #8,d1
        move.w  d1,C_OFFMEM(a3)
.om:    move.w  C_OFFMEM(a3),d0
.on:    bsr     note_on
        bra.s   .fx
.tp:    move.w  d1,C_PTARGET(a3)
        move.b  C_RNOTE(a3),C_PNOTE(a3)
        bra.s   .fx
.np:    cmp.b   #9,d2
        ifd     MOD_MUT_R_OFFNN
        bra.s   .fx                     ; mutant (rev15-mod): 9xx without a note keeps no memory
        endc
        bne.s   .fx
        tst.b   d3
        beq.s   .fx
        move.w  d3,d1
        lsl.w   #8,d1
        move.w  d1,C_OFFMEM(a3)
.fx:    moveq   #0,d0                   ; C_FXRUN: 0 with a param, 1-7, A, E9/EC/ED
        tst.b   d2
        bne.s   .fr1
        tst.b   d3
        sne     d0
        bra.s   .fr9
.fr1:   cmp.b   #$e,d2
        bne.s   .fr2
        cmp.b   #9,d5
        beq.s   .fry
        cmp.b   #$c,d5
        beq.s   .fry
        cmp.b   #$d,d5
        beq.s   .fry
        bra.s   .fr9
.fr2:   cmp.b   #7,d2
        bls.s   .fry
        cmp.b   #$a,d2
        bne.s   .fr9
.fry:   st      d0
.fr9:   move.b  d0,C_FXRUN(a3)
        beq.s   .fr10
        bset    d7,G_RUNM(a6)
.fr10:
        add.w   d2,d2
        move.w  .t0(pc,d2.w),d2
        jmp     .t0(pc,d2.w)
.t0:    dc.w    .nx-.t0,.nx-.t0,.nx-.t0,.e3-.t0,.e4-.t0,.nx-.t0,.nx-.t0,.e7-.t0
        dc.w    .e8-.t0,.nx-.t0,.nx-.t0,.eb-.t0,.ec-.t0,.ed-.t0,.ee-.t0,.ef-.t0

.e3:    tst.b   d3
        beq     .nx
        move.b  d3,C_PSPEED(a3)
        bra     .nx
.e4:    tst.b   d5
        beq.s   .e4b
        move.b  d5,C_VSPEED(a3)
.e4b:   tst.b   d6
        beq     .nx
        move.b  d6,C_VDEPTH(a3)
        bra     .nx
.e7:    tst.b   d5
        beq.s   .e7b
        move.b  d5,C_TSPEED(a3)
.e7b:   tst.b   d6
        beq     .nx
        move.b  d6,C_TDEPTH(a3)
        bra     .nx
.e8:    addq.w  #1,STATUS+S_IGN+8
        bra     .nx
.eb:    st      G_JMP(a6)
        move.b  d3,G_JPOS(a6)
        bra     .nx
.ec:    cmp.b   #64,d3
        bls.s   .ec1
        moveq   #64,d3
.ec1:   move.b  d3,C_VOL(a3)
        bra     .nx
.ed:
        move.w  d5,d0
        mulu.w  #10,d0
        add.w   d6,d0
        cmp.w   #64,d0
        blo.s   .ed1
        moveq   #0,d0
.ed1:   move.b  d0,G_BRK(a6)
        bra     .nx
.ef:    tst.b   d3
        bne.s   .ef1
        addq.w  #1,STATUS+S_IGN+10
        bra     .nx
.ef1:   cmp.b   #32,d3
        bhs.s   .ef2
        move.b  d3,G_SPEED(a6)
        bra     .nx
.ef2:   move.b  d3,G_BPM(a6)
        st      G_CIANEXT(a6)
        bra     .nx
.ee:    add.w   d5,d5
        move.w  .te(pc,d5.w),d5
        jmp     .te(pc,d5.w)
.te:    dc.w    .x0-.te,.x1-.te,.x2-.te,.x3-.te,.x4-.te,.nx-.te,.x6-.te,.x7-.te
        dc.w    .x8-.te,.nx-.te,.xa-.te,.xb-.te,.xc-.te,.nx-.te,.xe-.te,.xf-.te
.x0:    addq.w  #1,STATUS+S_IGN+0
        bra     .nx
.x3:    addq.w  #1,STATUS+S_IGN+2
        bra     .nx
.x8:    addq.w  #1,STATUS+S_IGN+4
        bra     .nx
.xf:    addq.w  #1,STATUS+S_IGN+6
        bra     .nx
.x1:    move.w  C_PERIOD(a3),d0         ; no note yet: no period effect
        ifnd    MOD_MUT_E10
        beq     .nx                     ; (mutant rev15-mod: E1x/E2x act anyway)
        endc
        sub.w   d6,d0
        cmp.w   #MIN_PER,d0
        bge.s   .x1b
        move.w  #MIN_PER,d0
.x1b:   move.w  d0,C_PERIOD(a3)
        bra     .nx
.x2:    move.w  C_PERIOD(a3),d0
        ifnd    MOD_MUT_E10
        beq     .nx
        endc
        add.w   d6,d0
        cmp.w   #MAX_PER,d0
        ble.s   .x2b
        move.w  #MAX_PER,d0
.x2b:   move.w  d0,C_PERIOD(a3)
        bra     .nx
.x4:    move.b  d6,C_VWAVE(a3)
        bra     .nx
.x7:    move.b  d6,C_TWAVE(a3)
        bra     .nx
.x6:    tst.b   d6
        bne.s   .x6a
        move.b  G_ROW(a6),C_LOOPROW(a3)
        bra     .nx
.x6a:   tst.b   C_LOOPCNT(a3)
        bne.s   .x6b
        move.b  d6,C_LOOPCNT(a3)
        move.b  C_LOOPROW(a3),G_LOOPTO(a6)
        bra     .nx
.x6b:   subq.b  #1,C_LOOPCNT(a3)
        beq     .nx
        move.b  C_LOOPROW(a3),G_LOOPTO(a6)
        bra     .nx
.xa:    move.b  C_VOL(a3),d0
        add.b   d6,d0
        cmp.b   #64,d0
        bls.s   .xa1
        moveq   #64,d0
.xa1:   move.b  d0,C_VOL(a3)
        bra     .nx
.xb:    move.b  C_VOL(a3),d0
        sub.b   d6,d0
        bcc.s   .xb1
        moveq   #0,d0
.xb1:   move.b  d0,C_VOL(a3)
        bra     .nx
.xc:    tst.b   d6
        bne     .nx
        clr.b   C_VOL(a3)
        bra     .nx
.xe:    tst.b   d6
        beq     .nx
        move.b  d6,G_DELAY(a6)
        ifd     MOD_MUT_R_EEBRK
        cmp.b   #$ff,G_BRK(a6)          ; mutant (rev15-mod): a D before it cancels EE
        beq     .nx
        clr.b   G_DELAY(a6)
        endc
.nx:    lea     CH_SIZE(a3),a3
        addq.w  #1,d7
        cmp.w   #4,d7
        blo     .ch
        ; row length
        moveq   #0,d0
        move.b  G_DELAY(a6),d0
        addq.w  #1,d0
        moveq   #0,d1
        move.b  G_SPEED(a6),d1
        mulu.w  d1,d0
        move.w  d0,G_ROWTICKS(a6)
        ; the next row
        tst.b   G_JMP(a6)
        bne.s   .jb
        cmp.b   #$ff,G_BRK(a6)
        bne.s   .jb
        move.b  G_LOOPTO(a6),d0
        cmp.b   #$ff,d0
        beq.s   .nl
        move.b  G_POS(a6),G_NEXTPOS(a6)
        move.b  d0,G_NEXTROW(a6)
        clr.b   G_NEWPOS(a6)
        rts
.nl:    move.b  G_ROW(a6),d0
        addq.b  #1,d0
        cmp.b   #64,d0
        blo.s   .same
        move.b  G_POS(a6),d0
        addq.b  #1,d0
        move.b  d0,G_NEXTPOS(a6)
        clr.b   G_NEXTROW(a6)
        st      G_NEWPOS(a6)
        rts
.same:  move.b  d0,G_NEXTROW(a6)
        move.b  G_POS(a6),G_NEXTPOS(a6)
        clr.b   G_NEWPOS(a6)
        rts
.jb:
        tst.b   G_JMP(a6)
        beq.s   .donly
        move.b  G_JPOS(a6),d0
        cmp.b   G_POS(a6),d0
        bhi.s   .set
        st      G_LOOPFLAG(a6)
        bra.s   .set
.donly: move.b  G_POS(a6),d0
        addq.b  #1,d0
.set:   move.b  d0,G_NEXTPOS(a6)
        move.b  G_BRK(a6),d0
        cmp.b   #$ff,d0
        bne.s   .b
        moveq   #0,d0
.b:     move.b  d0,G_NEXTROW(a6)
        st      G_NEWPOS(a6)
        rts

; ---------------------------------------------------------------------------
; wave: d0 position (0-63), d1 wave -> d0.w signed value.  Clobbers d1, a0, a1.
; ---------------------------------------------------------------------------
wave:   andi.w  #63,d0
        move.w  d0,a1
        andi.w  #31,d0
        andi.b  #3,d1
        cmp.b   #1,d1
        bne.s   .w2
        lsl.w   #3,d0
        neg.w   d0
        add.w   #255,d0
        bra.s   .sg
.w2:    cmp.b   #2,d1
        bne.s   .w0
        move.w  #255,d0
        bra.s   .sg
.w0:    lea     sine,a0
        move.b  0(a0,d0.w),d0
        andi.w  #$ff,d0
.sg:    move.w  a1,d1
        btst    #5,d1
        beq.s   .r
        neg.w   d0
.r:     rts

; ---------------------------------------------------------------------------
; porta: tone portamento one step.  Clobbers d0, d1, d4.
; ---------------------------------------------------------------------------
porta:  move.w  C_PTARGET(a3),d1
        beq.s   .r
        move.w  C_PERIOD(a3),d0
        moveq   #0,d4
        move.b  C_PSPEED(a3),d4
        cmp.w   d1,d0
        beq.s   .st
        bhi.s   .dn
        add.w   d4,d0
        cmp.w   d1,d0
        bls.s   .st
        move.w  d1,d0
        bra.s   .st
.dn:    sub.w   d4,d0
        ifnd    MOD_MUT_R_PORTADN
        cmp.w   d1,d0                   ; (mutant rev15-mod: no clamp going down)
        bge.s   .st
        move.w  d1,d0
        endc
.st:    move.w  d0,C_PERIOD(a3)
        cmp.w   d1,d0
        bne.s   .r
        move.b  C_PNOTE(a3),C_NOTE(a3)
.r:     rts

; ---------------------------------------------------------------------------
; effects: every tick, the running effects and the output period/volume.
; ---------------------------------------------------------------------------
effects:
        clr.b   G_DIRTY(a6)
        tst.w   G_T(a6)
        bne.s   .t1
        move.b  #$0f,G_DIRTY(a6)        ; tick 0: the outputs = period, volume,
        lea     chans,a3                ; or the arpeggio's base note
        moveq   #3,d7
.z:     move.b  C_VOL(a3),C_OUTVOL(a3)
        tst.w   C_PERIOD(a3)            ; no note yet: no period effect
        beq.s   .zp
        tst.b   C_REFF(a3)
        bne.s   .zp
        tst.b   C_RPAR(a3)
        beq.s   .zp
        move.b  C_NOTE(a3),d1
        bsr     ptab_lookup
        move.w  d1,C_OUTPER(a3)
        bra.s   .zn
.zp:    move.w  C_PERIOD(a3),C_OUTPER(a3)
.zn:    lea     CH_SIZE(a3),a3
        dbra    d7,.z
        bra     clampout
.t1:    tst.b   G_RUNM(a6)              ; ticks > 0: nothing runs, nothing to do
        bne.s   .go
        rts
.go:    lea     chans,a3
        moveq   #0,d7
.ch:    tst.w   G_T(a6)
        beq.s   .all
        tst.b   C_FXRUN(a3)             ; nothing runs: the outputs stand
        beq     .next
.all:   bset    d7,G_DIRTY(a6)
        move.w  C_PERIOD(a3),C_OUTPER(a3)
        move.b  C_VOL(a3),C_OUTVOL(a3)
        moveq   #0,d2
        move.b  C_REFF(a3),d2
        moveq   #0,d3
        move.b  C_RPAR(a3),d3
        move.w  d3,d5
        lsr.w   #4,d5
        move.w  d3,d6
        andi.w  #$0f,d6
        tst.b   d2
        bne.s   .n0
        tst.b   d3
        beq.s   .n0
        tst.w   C_PERIOD(a3)            ; no note yet: no period effect
        beq     .next
        moveq   #0,d0                   ; arpeggio: t mod 3
        move.w  G_T(a6),d0
        divu    #3,d0
        swap    d0
        moveq   #0,d1
        tst.w   d0
        beq.s   .a
        move.w  d5,d1
        subq.w  #1,d0
        beq.s   .a
        move.w  d6,d1
.a:     moveq   #0,d0
        move.b  C_NOTE(a3),d0
        add.w   d0,d1
        cmp.w   #35,d1
        bls.s   .ac
        moveq   #35,d1
.ac:    bsr     ptab_lookup
        move.w  d1,C_OUTPER(a3)
        bra     .next
.n0:    tst.w   G_T(a6)
        beq     .next
        move.w  d2,d0
        add.w   d0,d0
        move.w  .tt(pc,d0.w),d0
        jmp     .tt(pc,d0.w)
.tt:    dc.w    .post-.tt,.p1-.tt,.p2-.tt,.p3-.tt,.p4-.tt,.p3-.tt,.p4-.tt,.p7-.tt
        dc.w    .post-.tt,.post-.tt,.post-.tt,.post-.tt,.post-.tt,.post-.tt,.pe-.tt,.post-.tt
.p1:    move.w  C_PERIOD(a3),d0
        beq     .post
        sub.w   d3,d0
        cmp.w   #MIN_PER,d0
        bge.s   .p1b
        move.w  #MIN_PER,d0
.p1b:   move.w  d0,C_PERIOD(a3)
        bra     .post
.p2:    move.w  C_PERIOD(a3),d0
        beq     .post
        add.w   d3,d0
        cmp.w   #MAX_PER,d0
        ble.s   .p2b
        move.w  #MAX_PER,d0
.p2b:   move.w  d0,C_PERIOD(a3)
        bra     .post
.p3:    tst.w   C_PERIOD(a3)
        beq     .post
        bsr     porta
        bra     .post
.p4:    tst.w   C_PERIOD(a3)
        beq     .post
        moveq   #0,d0
        move.b  C_VPOS(a3),d0
        move.b  C_VWAVE(a3),d1
        bsr     wave
        moveq   #0,d1
        move.b  C_VDEPTH(a3),d1
        muls.w  d1,d0
        asr.l   #7,d0
        add.w   C_PERIOD(a3),d0
        move.w  d0,C_OUTPER(a3)
        move.b  C_VPOS(a3),d0
        add.b   C_VSPEED(a3),d0
        andi.b  #63,d0
        move.b  d0,C_VPOS(a3)
        bra     .post
.p7:    moveq   #0,d0
        move.b  C_TPOS(a3),d0
        move.b  C_TWAVE(a3),d1
        bsr     wave
        moveq   #0,d1
        move.b  C_TDEPTH(a3),d1
        muls.w  d1,d0
        asr.l   #6,d0
        moveq   #0,d1
        move.b  C_VOL(a3),d1
        add.w   d1,d0
        ifnd    MOD_MUT_R_TREMLO
        bpl.s   .p7a                    ; (mutant rev15-mod: no clamp below 0)
        moveq   #0,d0
        endc
.p7a:   cmp.w   #64,d0
        ble.s   .p7b
        moveq   #64,d0
.p7b:   move.b  d0,C_OUTVOL(a3)
        move.b  C_TPOS(a3),d0
        add.b   C_TSPEED(a3),d0
        andi.b  #63,d0
        move.b  d0,C_TPOS(a3)
        bra     .post
.pe:    cmp.b   #9,d5
        bne.s   .pec
        tst.b   d6
        beq     .post
        tst.w   C_PERIOD(a3)            ; no note yet: no retrigger
        beq     .post
        moveq   #0,d0
        move.w  G_T(a6),d0
        divu    d6,d0
        swap    d0
        tst.w   d0
        bne     .post
        bsr     note_on                 ; d0 = 0: no offset
        bra     .post
.pec:   cmp.b   #$c,d5
        bne.s   .ped
        cmp.w   G_T(a6),d6
        bne     .post
        clr.b   C_VOL(a3)
        bra     .post
.ped:   cmp.b   #$d,d5
        bne     .post
        cmp.w   G_T(a6),d6
        bne     .post
        tst.w   C_RPER(a3)
        beq     .post
        move.b  C_RNOTE(a3),d1
        bsr     ptab_lookup
        move.w  d1,C_PERIOD(a3)
        move.b  C_RNOTE(a3),C_NOTE(a3)
        moveq   #0,d0
        bsr     note_on
.post:  cmp.b   #5,d2                   ; volume slide: 5, 6, A
        beq.s   .vs
        cmp.b   #6,d2
        beq.s   .vs
        cmp.b   #$a,d2
        bne.s   .op
.vs:    move.b  C_VOL(a3),d0
        tst.b   d5
        beq.s   .vd
        add.b   d5,d0
        cmp.b   #64,d0
        bls.s   .vw
        moveq   #64,d0
        bra.s   .vw
.vd:    sub.b   d6,d0
        bcc.s   .vw
        moveq   #0,d0
.vw:    move.b  d0,C_VOL(a3)
.op:    cmp.b   #4,d2
        beq.s   .ov
        cmp.b   #6,d2
        beq.s   .ov
        move.w  C_PERIOD(a3),C_OUTPER(a3)
.ov:    cmp.b   #7,d2
        beq.s   .next
        move.b  C_VOL(a3),C_OUTVOL(a3)
.next:  lea     CH_SIZE(a3),a3
        addq.w  #1,d7
        cmp.w   #4,d7
        blo     .ch
                                        ; (falls into clampout)
; clampout: M1: a nonzero output period is kept within PER_LO..PER_HI
clampout:
        lea     chans,a3
        moveq   #3,d7
.c:     move.w  C_OUTPER(a3),d0
        beq.s   .n
        cmp.w   #PER_LO,d0
        bhs.s   .h
        move.w  #PER_LO,C_OUTPER(a3)
.h:     cmp.w   #PER_HI,d0
        bls.s   .n
        move.w  #PER_HI,C_OUTPER(a3)
.n:     lea     CH_SIZE(a3),a3
        dbra    d7,.c
        rts

; ---------------------------------------------------------------------------
; emit: this tick's Paula writes (mod_ref's protocol), then arm the stages.
; ---------------------------------------------------------------------------
emit:   moveq   #0,d1
        move.b  G_TRIGM(a6),d1
        beq.s   .nt
        move.w  #DMACON,d0
        bsr     pw
.nt:    tst.b   G_PLAYM(a6)
        beq.s   .pv0
        lea     chans,a3
        lea     AUD0LC(a5),a4
        moveq   #0,d7
.lc:    btst    d7,G_PLAYM(a6)
        beq.s   .lcn
        move.l  C_TLC(a3),d1
        PWAL    d1,0
        move.w  C_TLEN(a3),d1
        PWA     d1,4
.lcn:   lea     CH_SIZE(a3),a3
        lea     16(a4),a4
        addq.w  #1,d7
        cmp.w   #4,d7
        blo.s   .lc
.pv0:   tst.b   G_DIRTY(a6)
        beq     .stg
        lea     chans,a3
        lea     AUD0LC(a5),a4
        moveq   #0,d7
.pv:    btst    d7,G_DIRTY(a6)
        beq.s   .pn
        move.w  C_OUTPER(a3),d1
        btst    d7,G_TRIGM(a6)
        bne.s   .wp
        cmp.w   C_WPER(a3),d1
        beq.s   .vv
.wp:    move.w  d1,C_WPER(a3)
        PWA     d1,6
.vv:    moveq   #0,d1
        move.b  C_OUTVOL(a3),d1
        btst    d7,G_TRIGM(a6)
        bne.s   .wv
        cmp.b   C_WVOL(a3),d1
        beq.s   .pn
.wv:    move.b  d1,C_WVOL(a3)
        PWA     d1,8
.pn:    lea     CH_SIZE(a3),a3
        lea     16(a4),a4
        addq.w  #1,d7
        cmp.w   #4,d7
        blo.s   .pv
.stg:   move.b  G_PLAYM(a6),d0
        beq.s   .r
        move.b  d0,G_STGM(a6)
        move.b  #1,G_STAGE(a6)          ; armed at run_loop's end
        ifd     MOD_MUT_EMITARM
        bsr     stage_arm               ; mutant: armed inside the tick
        endc
.r:     rts

; ---------------------------------------------------------------------------
; build_view: the shadow mod_view of this tick.
; ---------------------------------------------------------------------------
build_view:
        lea     shadow,a0
        move.l  G_TICKS(a6),4(a0)
        tst.w   G_T(a6)
        bne.s   .fl                     ; position, speed, BPM: only at a row
        moveq   #0,d0
        move.b  G_POS(a6),d0
        move.b  d0,29(a0)
        move.l  G_ORDER(a6),a1
        move.b  0(a1,d0.w),30(a0)
        move.b  G_ROW(a6),31(a0)
        move.b  G_SPEED(a6),32(a0)
        move.b  G_BPM(a6),33(a0)
.fl:    moveq   #1,d0
        tst.b   G_LOOPED(a6)
        beq.s   .l
        bset    #1,d0
.l:     tst.b   G_CIA(a6)
        beq.s   .c
        bset    #2,d0
.c:     move.b  d0,34(a0)
        lea     36(a0),a1
        lea     chans,a3
        moveq   #0,d7
.ch:    tst.w   G_T(a6)
        beq.s   .full
        btst    d7,G_DIRTY(a6)
        beq.s   .vu
.full:  move.w  C_OUTPER(a3),(a1)
        move.b  C_INST(a3),2(a1)
        move.b  C_OUTVOL(a3),3(a1)
        move.b  C_RNOTE(a3),4(a1)
        move.b  C_REFF(a3),5(a1)
        move.b  C_RPAR(a3),6(a1)
.vu:    move.b  C_VU(a3),d1             ; vu: the volume on a trigger, else - 2
        tst.b   C_TRIGD(a3)
        beq.s   .dec
        clr.b   C_TRIGD(a3)
        move.b  C_VOL(a3),d1
        bra.s   .vs
.dec:   subq.b  #2,d1
        bcc.s   .vs
        moveq   #0,d1
.vs:    move.b  d1,C_VU(a3)
        move.b  d1,7(a1)
        addq.w  #8,a1
        lea     CH_SIZE(a3),a3
        addq.w  #1,d7
        cmp.w   #4,d7
        blo.s   .ch
        moveq   #0,d0
        move.w  G_ACC(a6),d0
        move.l  d0,68(a0)
        rts

        section DATA,data
        cnop    0,2
        include "tables.i"

        section BSS,bss
        cnop    0,4
G:      ds.b    G_SIZE
chans:  ds.b    4*CH_SIZE
insts:  ds.b    31*IN_SIZE
shadow: ds.b    VIEW_LEN
_mod_done:
        ds.b    2
