; ===========================================================================
; runtime/ops/cia.s — S3.4 guest CIA-A/B (two MOS 8520s) behind the $BF trap
; Owner: W9-C (cia).  Spec: docs/S3_PLAN.md §1.5, §4 "S3.4",
; docs/WAVE9_BRIEF.md C3; contract: docs/CONTRACTS.md "CIA-A/B (S3.4)".
; Reference semantics: host/chipset_ref.CIA (and Chipset's wiring).
;
; Time is lazy.  Each chip remembers the guest E time of its last sync
; (CIA_T).  Every register access, and every event callback, first syncs the
; chip: n = now - CIA_T E ticks are applied at once with exactly
; chipset_ref.CIA.eclock(n) (TA, then TB counting E ticks or TA underflows,
; ICR data bits), and CIA-B's TOD advances by the hsync lines between the
; two times.  Splitting time anywhere gives the same state as one eclock
; (continuous timers are modular, a one-shot stops), so the result does not
; depend on when syncs happen.  Nothing runs per E tick: the only armed
; events are the next underflow of a timer whose interrupt (or TB's
; cascade) is enabled, and CIA-B's TOD alarm.  CIA-A's TOD counts VERTBs
; (mach slot-0 hook, one call per frame).  CIA-B's TOD counts lines: line
; k starts at colour clock 227 k, i.e. E = 227 k / 5 (5 colour clocks per
; E tick, 15625 Hz at 709379 E ticks/s), on one grid that runs whether or
; not the TOD is halted.
;
; Interrupts: whenever a sync or an ICR write leaves ICR data & mask != 0,
; the chip posts its INTREQ bit (mach_post: CIA-A PORTS = 3, CIA-B EXTER =
; 13), so a source that stays asserted raises the bit again (chipset_ref
; calls update() after every eclock).  cia_intreq re-posts both after a
; guest INTREQ write (chipset_ref custom_write $09C).
;
; Before mach_init (mach_on = 0) guest time is frozen at 0; mach_init
; starts the counter near 0, so time is continuous across it.  A time that
; goes backwards (a later mach_init) counts as 0 ticks and re-anchors.
;
; Entries (JSR):
;   cia_read    X = register index (0-15 CIA-A, 16-31 CIA-B) -> A = value.
;               Y preserved, X clobbered (chip.s ABI).
;   cia_write   X = register index, A = value.  Y preserved, X clobbered.
;   cia_reset   both chips to power-up (chipset_ref.CIA()), callbacks
;               registered in mach_cbv (slots 0-5) and mach_ihook, slots
;               1-5 disarmed.  Run by the first access and by chip_reset.
;               Clobbers A/X/Y.
;   cia_intreq  (mach_ihook: chip.s calls it after a guest INTREQ write)
;               re-post the INTREQ bit of every chip whose ICR data & mask
;               is nonzero.  Clobbers A/X/Y.
;
; Host-visible state (runner.lbl): cia_st (2 x CIA_SZ bytes, layout C_*
; below), cia_now (u32 E time of the last sync), cia_ok.
;
; Link layout: after chip.s, before mach.s (Makefile).
; ===========================================================================

.include "../macros.inc"

.export cia_read, cia_write, cia_reset, cia_intreq
.export cia_st, cia_now, cia_ok
.import mach_on, mach_etime, mach_et, mach_arm, mach_disarm, mach_at
.import mach_post, mach_cbv, mach_ihook

; per-chip state block (offsets into cia_st + chip offset)
CIA_PRA   = 0         ; @value port A output register
CIA_PRB   = 1         ; @value port B output register
CIA_DDRA  = 2         ; @value
CIA_DDRB  = 3         ; @value
CIA_TAL   = 4         ; @value timer A latch (u16 LE)
CIA_TBL   = 6         ; @value timer B latch
CIA_TA    = 8         ; @value timer A count at CIA_T
CIA_TB    = 10        ; @value timer B count at CIA_T
CIA_T     = 12        ; @value u32 E time of the last sync
CIA_CRA   = 16        ; @value (bit 4 never stored)
CIA_CRB   = 17        ; @value
CIA_ICR   = 18        ; @value ICR data bits 0-4
CIA_MSK   = 19        ; @value ICR mask bits 0-4
CIA_TOD   = 20        ; @value u24 TOD counter
CIA_ALM   = 23        ; @value u24 alarm
CIA_TLT   = 26        ; @value u24 TOD read latch
CIA_TLF   = 29        ; @value nonzero: TOD latched (TODHI read, until TODLO)
CIA_HLT   = 30        ; @value nonzero: TOD halted (TODHI write, until TODLO)
CIA_SDR   = 31        ; @value
CIA_PIN   = 32        ; @value port A input pins
CIA_BIT   = 33        ; @value INTREQ bit posted
CIA_PH    = 34        ; @value line phase, colour clocks 0-226 (CIA-B)
CIA_TDY   = 35        ; @value nonzero: TOD alarm deadline must be recomputed
CIA_SLA   = 36        ; @value mach slot of TA
CIA_SLB   = 37        ; @value mach slot of TB
CIA_SLT   = 38        ; @value mach slot of the TOD alarm (0: VERTB hook)
CIA_ARA   = 39        ; @value nonzero: TA slot armed at CIA_DLA
CIA_ARB   = 40        ; @value
CIA_ART   = 41        ; @value
CIA_DLA   = 42        ; @value u32 armed TA deadline
CIA_DLB   = 46        ; @value
CIA_DLT   = 50        ; @value u32 TOD alarm deadline (valid unless CIA_TDY)
CIA_SZ  = 54        ; @value bytes per chip
CIA_B   = CIA_SZ    ; @value chip offset of CIA-B

CIA_PRA_IN_A  = $EB ; @value CIA-A PRA input pins (docs/S3_PLAN.md §3.4)
CIA_PRA_IN_B  = $FF ; @value CIA-B PRA input pins (chipset_ref default)
CIA_PRB_IN    = $FF ; @value PRB input pins, both chips
CIA_IRQ_A     = 3   ; @value INTREQ PORTS
CIA_IRQ_B     = 13  ; @value INTREQ EXTER
CIA_LINE_CC   = 227 ; @value colour clocks per line
CIA_CC_PER_E  = 5   ; @value colour clocks per E tick

.segment "BSS"
cia_st:  .res 2 * CIA_SZ
cia_ok:  .res 1     ; nonzero once cia_reset ran
cia_now: .res 4     ; E time of the last sync
cia_y:   .res 1     ; caller's Y
cia_x:   .res 1     ; chip offset across mach calls
cia_r:   .res 1     ; register number 0-15
cia_v:   .res 1     ; written value
cia_n:   .res 4     ; elapsed E ticks / steps
cia_u:   .res 4     ; TA underflows of this sync
cia_d:   .res 4     ; div: dividend -> quotient; scratch
cia_dd:  .res 2     ; div: divisor
cia_dr:  .res 2     ; div: remainder
cia_tv:  .res 2     ; adv: counter value
cia_tl:  .res 2     ; adv: latch
cia_tf:  .res 1     ; adv: one-shot flag in, underflowed flag out
cia_k:   .res 4     ; scratch
cia_vc:  .res 1     ; VERTB count (mod 256): periodic sync

.segment "CODE"

; ---------------------------------------------------------------------------
; cia_read — X = register index -> A.  Y preserved.
; ---------------------------------------------------------------------------
.proc cia_read
        sty cia_y
        jsr sel
        jsr sync
        ldy cia_r
        lda rdtab_hi,y
        pha
        lda rdtab_lo,y
        pha
        rts                     ; -> rd_*, which ends with jmp rdone
.endproc

rdone:  pha                     ; a sync may have moved a deadline
        jsr rearm
        pla
        ldy cia_y
        rts

; ---------------------------------------------------------------------------
; cia_write — X = register index, A = value.  Y preserved.
; ---------------------------------------------------------------------------
.proc cia_write
        sty cia_y
        sta cia_v
        jsr sel
        jsr sync
        ldy cia_r
        lda wrtab_hi,y
        pha
        lda wrtab_lo,y
        pha
        rts                     ; -> wr_*, which ends with jmp wdone
.endproc

wdone:  jsr post
        jsr rearm
        ldy cia_y
        rts

; sel — X = register index -> X = chip offset, cia_r = register.  Runs
; cia_reset on the first access.
.proc sel
        lda cia_ok
        bne ok
        stx cia_r
        jsr cia_reset
        ldx cia_r
ok:     txa
        and #$0F
        sta cia_r
        txa
        and #$10
        beq chipa
        ldx #CIA_B
        rts
chipa:  ldx #0
        rts
.endproc

rdtab_lo:
        .lobytes rd_pra-1, rd_prb-1, rd_ddra-1, rd_ddrb-1, rd_tal-1, rd_tah-1
        .lobytes rd_tbl-1, rd_tbh-1, rd_tod0-1, rd_tod1-1, rd_tod2-1, rd_ff-1
        .lobytes rd_sdr-1, rd_icr-1, rd_cra-1, rd_crb-1
rdtab_hi:
        .hibytes rd_pra-1, rd_prb-1, rd_ddra-1, rd_ddrb-1, rd_tal-1, rd_tah-1
        .hibytes rd_tbl-1, rd_tbh-1, rd_tod0-1, rd_tod1-1, rd_tod2-1, rd_ff-1
        .hibytes rd_sdr-1, rd_icr-1, rd_cra-1, rd_crb-1
wrtab_lo:
        .lobytes wr_pra-1, wr_prb-1, wr_ddra-1, wr_ddrb-1, wr_tal-1, wr_tah-1
        .lobytes wr_tbl-1, wr_tbh-1, wr_tod-1, wr_tod-1, wr_tod-1, wdone-1
        .lobytes wr_sdr-1, wr_icr-1, wr_cra-1, wr_crb-1
wrtab_hi:
        .hibytes wr_pra-1, wr_prb-1, wr_ddra-1, wr_ddrb-1, wr_tal-1, wr_tah-1
        .hibytes wr_tbl-1, wr_tbh-1, wr_tod-1, wr_tod-1, wr_tod-1, wdone-1
        .hibytes wr_sdr-1, wr_icr-1, wr_cra-1, wr_crb-1

; ---------------------------------------------------------------------------
; Register reads (X = chip offset, state synced).  A = result.
; ---------------------------------------------------------------------------
rd_pra: lda cia_st+CIA_DDRA,x     ; (PRA & DDRA) | (pins & ~DDRA)
        eor #$FF
        and cia_st+CIA_PIN,x
        sta cia_k
        lda cia_st+CIA_DDRA,x
        and cia_st+CIA_PRA,x
        ora cia_k
        jmp rdone
rd_prb: lda cia_st+CIA_DDRB,x
        eor #$FF
        and #CIA_PRB_IN
        sta cia_k
        lda cia_st+CIA_DDRB,x
        and cia_st+CIA_PRB,x
        ora cia_k
        jmp rdone
rd_ddra:
        lda cia_st+CIA_DDRA,x
        jmp rdone
rd_ddrb:
        lda cia_st+CIA_DDRB,x
        jmp rdone
rd_tal: lda cia_st+CIA_TA,x
        jmp rdone
rd_tah: lda cia_st+CIA_TA+1,x
        jmp rdone
rd_tbl: lda cia_st+CIA_TB,x
        jmp rdone
rd_tbh: lda cia_st+CIA_TB+1,x
        jmp rdone
rd_tod2:                        ; TODHI: latch the counter
        lda cia_st+CIA_TOD,x
        sta cia_st+CIA_TLT,x
        lda cia_st+CIA_TOD+1,x
        sta cia_st+CIA_TLT+1,x
        lda cia_st+CIA_TOD+2,x
        sta cia_st+CIA_TLT+2,x
        lda #1
        sta cia_st+CIA_TLF,x
        lda cia_st+CIA_TLT+2,x
        jmp rdone
rd_tod1:
        lda cia_st+CIA_TLF,x
        beq :+
        lda cia_st+CIA_TLT+1,x
        jmp rdone
:       lda cia_st+CIA_TOD+1,x
        jmp rdone
rd_tod0:                        ; TODLO: the latched value (if any), unlatch
        lda cia_st+CIA_TLF,x
        beq :+
        lda #0
        sta cia_st+CIA_TLF,x
        lda cia_st+CIA_TLT,x
        jmp rdone
:       lda cia_st+CIA_TOD,x
        jmp rdone
rd_ff:  lda #$FF
        jmp rdone
rd_sdr: lda cia_st+CIA_SDR,x
        jmp rdone
rd_icr: lda cia_st+CIA_ICR,x      ; data | IR, then clear the data
        and cia_st+CIA_MSK,x
        beq :+
        lda #$80
:       ora cia_st+CIA_ICR,x
        pha
        lda #0
        sta cia_st+CIA_ICR,x
        pla
        jmp rdone
rd_cra: lda cia_st+CIA_CRA,x
        jmp rdone
rd_crb: lda cia_st+CIA_CRB,x
        jmp rdone

; ---------------------------------------------------------------------------
; Register writes (X = chip offset, state synced, value in cia_v).
; ---------------------------------------------------------------------------
wr_pra: lda cia_v
        sta cia_st+CIA_PRA,x
        jmp wdone
wr_prb: lda cia_v
        sta cia_st+CIA_PRB,x
        jmp wdone
wr_ddra:
        lda cia_v
        sta cia_st+CIA_DDRA,x
        jmp wdone
wr_ddrb:
        lda cia_v
        sta cia_st+CIA_DDRB,x
        jmp wdone
wr_sdr: lda cia_v
        sta cia_st+CIA_SDR,x
        jmp wdone
wr_tal: lda cia_v
        sta cia_st+CIA_TAL,x
        jmp wdone
wr_tbl: lda cia_v
        sta cia_st+CIA_TBL,x
        jmp wdone
wr_tah: lda cia_v               ; latch high; a stopped timer loads it, a
        sta cia_st+CIA_TAL+1,x    ; one-shot loads it and starts
        lda cia_st+CIA_CRA,x
        and #$09
        cmp #$01
        beq :+                  ; running continuous: latch only
        lda cia_st+CIA_TAL,x
        sta cia_st+CIA_TA,x
        lda cia_st+CIA_TAL+1,x
        sta cia_st+CIA_TA+1,x
        lda cia_st+CIA_CRA,x
        and #$08
        beq :+
        lda cia_st+CIA_CRA,x
        ora #$01
        sta cia_st+CIA_CRA,x
:       jmp wdone
wr_tbh: lda cia_v
        sta cia_st+CIA_TBL+1,x
        lda cia_st+CIA_CRB,x
        and #$09
        cmp #$01
        beq :+
        lda cia_st+CIA_TBL,x
        sta cia_st+CIA_TB,x
        lda cia_st+CIA_TBL+1,x
        sta cia_st+CIA_TB+1,x
        lda cia_st+CIA_CRB,x
        and #$08
        beq :+
        lda cia_st+CIA_CRB,x
        ora #$01
        sta cia_st+CIA_CRB,x
:       jmp wdone
wr_cra: lda cia_v               ; bit 4: force load (strobe, not stored)
        and #$10
        beq :+
        lda cia_st+CIA_TAL,x
        sta cia_st+CIA_TA,x
        lda cia_st+CIA_TAL+1,x
        sta cia_st+CIA_TA+1,x
:       lda cia_v
        and #$EF
        sta cia_st+CIA_CRA,x
        jmp wdone
wr_crb: lda cia_v
        and #$10
        beq :+
        lda cia_st+CIA_TBL,x
        sta cia_st+CIA_TB,x
        lda cia_st+CIA_TBL+1,x
        sta cia_st+CIA_TB+1,x
:       lda cia_v
        and #$EF
        sta cia_st+CIA_CRB,x
        jmp wdone
wr_icr: lda cia_v               ; bit 7: SET, else CLR, bits 0-4
        bmi set
        eor #$FF
        and cia_st+CIA_MSK,x
        and #$1F
        sta cia_st+CIA_MSK,x
        jmp tdirty
set:    and #$1F
        ora cia_st+CIA_MSK,x
        sta cia_st+CIA_MSK,x
tdirty: lda #1
        sta cia_st+CIA_TDY,x
        jmp wdone
wr_tod: lda #1                  ; regs 8-10: the alarm (CRB bit 7) or TOD
        sta cia_st+CIA_TDY,x
        txa
        clc
        adc cia_r
        tay                     ; Y = chip offset + register (8-10)
        lda cia_st+CIA_CRB,x
        bmi alarm
        lda cia_v
        sta cia_st+CIA_TOD-8,y
        lda cia_r
        cmp #10
        bne :+
        lda #1                  ; TODHI write halts the clock ...
        sta cia_st+CIA_HLT,x
        jmp wdone
:       cmp #8
        bne :+
        lda #0                  ; ... until the TODLO write (TODMID: neither)
        sta cia_st+CIA_HLT,x
:       jmp wdone
alarm:  lda cia_v
        sta cia_st+CIA_ALM-8,y
        jmp wdone

; ---------------------------------------------------------------------------
; sync — bring chip X to now (chipset_ref eclock(now - CIA_T)), then post.
; Preserves X.
; ---------------------------------------------------------------------------
.proc sync
        jsr getnow
        sec                     ; n = now - CIA_T
        lda cia_now
        sbc cia_st+CIA_T,x
        sta cia_n
        lda cia_now+1
        sbc cia_st+CIA_T+1,x
        sta cia_n+1
        lda cia_now+2
        sbc cia_st+CIA_T+2,x
        sta cia_n+2
        lda cia_now+3
        sbc cia_st+CIA_T+3,x
        sta cia_n+3
        bpl fwd
        lda #0                  ; time went backwards (a new mach_init):
        sta cia_n               ; 0 ticks, re-anchor
        sta cia_n+1
        sta cia_n+2
        sta cia_n+3
        lda #1
        sta cia_st+CIA_TDY,x
fwd:    lda cia_now
        sta cia_st+CIA_T,x
        lda cia_now+1
        sta cia_st+CIA_T+1,x
        lda cia_now+2
        sta cia_st+CIA_T+2,x
        lda cia_now+3
        sta cia_st+CIA_T+3,x
        lda cia_n
        ora cia_n+1
        ora cia_n+2
        ora cia_n+3
        bne go
        jmp post
go:     lda #0
        sta cia_u
        sta cia_u+1
        sta cia_u+2
        sta cia_u+3
        ; --- timer A: counts E ticks when started and INMODE = 0
        lda cia_st+CIA_CRA,x
        and #$21
        cmp #$01
        bne tb
        lda cia_st+CIA_TA,x
        sta cia_tv
        lda cia_st+CIA_TA+1,x
        sta cia_tv+1
        lda cia_st+CIA_TAL,x
        sta cia_tl
        lda cia_st+CIA_TAL+1,x
        sta cia_tl+1
        lda cia_st+CIA_CRA,x
        and #$08
        sta cia_tf
        ldy #0                  ; steps = cia_n
        jsr adv
        lda cia_tv
        sta cia_st+CIA_TA,x
        lda cia_tv+1
        sta cia_st+CIA_TA+1,x
        lda cia_tf
        beq tb
        ldy #3                  ; cia_u := TA's underflow count
:       lda cia_k,y
        sta cia_u,y
        dey
        bpl :-
        lda cia_st+CIA_ICR,x
        ora #$01
        sta cia_st+CIA_ICR,x
        lda cia_st+CIA_CRA,x      ; one-shot: stop
        and #$08
        beq tb
        lda cia_st+CIA_CRA,x
        and #$FE
        sta cia_st+CIA_CRA,x
        ; --- timer B: E ticks (INMODE 0), TA underflows (2, 3), CNT (1: 0)
tb:     lda cia_st+CIA_CRB,x
        lsr
        bcc tod
        and #$30                ; INMODE << 4 (after lsr)
        beq tbe
        cmp #$10
        beq tod                 ; CNT: no pulses
        ldy #4                  ; steps = cia_u
        bne tbgo
tbe:    ldy #0                  ; steps = cia_n
tbgo:   lda cia_n,y
        ora cia_n+1,y
        ora cia_n+2,y
        ora cia_n+3,y
        beq tod
        lda cia_st+CIA_TB,x
        sta cia_tv
        lda cia_st+CIA_TB+1,x
        sta cia_tv+1
        lda cia_st+CIA_TBL,x
        sta cia_tl
        lda cia_st+CIA_TBL+1,x
        sta cia_tl+1
        lda cia_st+CIA_CRB,x
        and #$08
        sta cia_tf
        jsr adv
        lda cia_tv
        sta cia_st+CIA_TB,x
        lda cia_tv+1
        sta cia_st+CIA_TB+1,x
        lda cia_tf
        beq tod
        lda cia_st+CIA_ICR,x
        ora #$02
        sta cia_st+CIA_ICR,x
        lda cia_st+CIA_CRB,x
        and #$08
        beq tod
        lda cia_st+CIA_CRB,x
        and #$FE
        sta cia_st+CIA_CRB,x
        ; --- CIA-B TOD: hsync lines
tod:    cpx #CIA_B
        bne done
        jsr lines
done:   jmp post
.endproc
.assert cia_u = cia_n + 4, error, "sync: cia_n,y with y = 4 must reach cia_u"


; ---------------------------------------------------------------------------
; adv — advance a counter (cia_tv, latch cia_tl, one-shot if cia_tf != 0)
; by the u32 steps at cia_n+Y (Y = 0: cia_n, 4: cia_u), as
; chipset_ref.CIA.eclock: k <= v: v -= k; else it underflows (on the step
; after 0) and reloads: a one-shot once (v = latch), a continuous timer
; 1 + m / (latch+1) times with v = latch - m mod (latch+1), m = k - v - 1.
; Out: cia_tv; cia_tf = 1 if it underflowed (cia_k = the count), else 0.
; Preserves X.
; ---------------------------------------------------------------------------
.proc adv
        lda cia_n,y             ; cia_k := steps
        sta cia_k
        lda cia_n+1,y
        sta cia_k+1
        lda cia_n+2,y
        sta cia_k+2
        lda cia_n+3,y
        sta cia_k+3
        lda cia_k+2
        ora cia_k+3
        bne under
        lda cia_tv              ; k <= v: no underflow
        cmp cia_k
        lda cia_tv+1
        sbc cia_k+1
        bcc under
        lda cia_tv
        sbc cia_k               ; C = 1
        sta cia_tv
        lda cia_tv+1
        sbc cia_k+1
        sta cia_tv+1
        lda #0
        sta cia_tf
        rts
under:  clc                     ; m = k - v - 1 (k > v, so m >= 0)
        lda cia_k
        sbc cia_tv
        sta cia_d
        lda cia_k+1
        sbc cia_tv+1
        sta cia_d+1
        lda cia_k+2
        sbc #0
        sta cia_d+2
        lda cia_k+3
        sbc #0
        sta cia_d+3
        lda cia_tf
        beq cont
        lda cia_tl              ; one-shot: one underflow, reload (the
        sta cia_tv              ; caller stops it)
        lda cia_tl+1
        sta cia_tv+1
        lda #1
        sta cia_k
        lda #0
        sta cia_k+1
        sta cia_k+2
        sta cia_k+3
        jmp out
cont:   lda cia_tl              ; span = latch + 1
        and cia_tl+1
        cmp #$FF
        bne small
        lda cia_d               ; span 65536: q = m >> 16, r = m & $FFFF
        sta cia_dr
        lda cia_d+1
        sta cia_dr+1
        lda cia_d+2
        sta cia_d
        lda cia_d+3
        sta cia_d+1
        lda #0
        sta cia_d+2
        sta cia_d+3
        beq have
small:  clc
        lda cia_tl
        adc #1
        sta cia_dd
        lda cia_tl+1
        adc #0
        sta cia_dd+1
        lda cia_d+2             ; m < span: q = 0, r = m (no division)
        ora cia_d+3
        bne divide
        lda cia_d
        cmp cia_dd
        lda cia_d+1
        sbc cia_dd+1
        bcs divide
        lda cia_d
        sta cia_dr
        lda cia_d+1
        sta cia_dr+1
        lda #0
        sta cia_d
        sta cia_d+1
        beq have
divide: jsr div
have:   sec                     ; v = latch - r
        lda cia_tl
        sbc cia_dr
        sta cia_tv
        lda cia_tl+1
        sbc cia_dr+1
        sta cia_tv+1
        sec                     ; count = q + 1
        lda cia_d
        adc #0
        sta cia_k
        lda cia_d+1
        adc #0
        sta cia_k+1
        lda cia_d+2
        adc #0
        sta cia_k+2
        lda cia_d+3
        adc #0
        sta cia_k+3
out:    lda #1
        sta cia_tf
        rts
.endproc

; ---------------------------------------------------------------------------
; div — cia_d (u32) / cia_dd (u16, nonzero) -> quotient cia_d, remainder
; cia_dr.  Restoring shift-subtract with a 17-bit partial remainder.
; Preserves X; clobbers Y.
; ---------------------------------------------------------------------------
.proc div
        lda #0
        sta cia_dr
        sta cia_dr+1
        ldy #32
lp:     asl cia_d
        rol cia_d+1
        rol cia_d+2
        rol cia_d+3
        rol cia_dr
        rol cia_dr+1
        bcs sub                 ; bit 16 set: certainly >= divisor
        lda cia_dr
        cmp cia_dd
        lda cia_dr+1
        sbc cia_dd+1
        bcc next
sub:    lda cia_dr
        sec
        sbc cia_dd
        sta cia_dr
        lda cia_dr+1
        sbc cia_dd+1
        sta cia_dr+1
        inc cia_d               ; bit 0 is 0 after the shift
next:   dey
        bne lp
        rts
.endproc

; ---------------------------------------------------------------------------
; lines — CIA-B TOD: advance the line grid by cia_n E ticks.  Lines passed
; = floor((5 n + ph) / 227), ph := the remainder.  A halted TOD lets the
; grid run and counts nothing.  Preserves X (= CIA_B).
; ---------------------------------------------------------------------------
.proc lines
        lda #0
        sta cia_k               ; cia_k := lines (u32)
        sta cia_k+1
        sta cia_k+2
        sta cia_k+3
        lda cia_n+3             ; n < 227: x = 5 n + ph directly
        ora cia_n+2
        ora cia_n+1
        bne big
        lda cia_n
        cmp #CIA_LINE_CC
        bcs big
        sta cia_dr
        lda #0
        sta cia_dr+1
        jmp mul5
big:    ldy #3                  ; n = 227 a + b
:       lda cia_n,y
        sta cia_d,y
        dey
        bpl :-
        lda #CIA_LINE_CC
        sta cia_dd
        lda #0
        sta cia_dd+1
        jsr div                 ; cia_d = a, cia_dr = b
        lda cia_d               ; lines = 5 a
        sta cia_k
        lda cia_d+1
        sta cia_k+1
        lda cia_d+2
        sta cia_k+2
        lda cia_d+3
        sta cia_k+3
        ldy #2
:       asl cia_k
        rol cia_k+1
        rol cia_k+2
        rol cia_k+3
        dey
        bne :-
        clc
        lda cia_k
        adc cia_d
        sta cia_k
        lda cia_k+1
        adc cia_d+1
        sta cia_k+1
        lda cia_k+2
        adc cia_d+2
        sta cia_k+2
        lda cia_k+3
        adc cia_d+3
        sta cia_k+3
mul5:   lda cia_dr              ; x = 5 b + ph (< 1362, 16 bits)
        sta cia_dd
        lda cia_dr+1
        sta cia_dd+1
        asl cia_dr
        rol cia_dr+1
        asl cia_dr
        rol cia_dr+1
        clc
        lda cia_dr
        adc cia_dd
        sta cia_dr
        lda cia_dr+1
        adc cia_dd+1
        sta cia_dr+1
        clc
        lda cia_dr
        adc cia_st+CIA_PH,x
        sta cia_dr
        bcc red
        inc cia_dr+1
red:    lda cia_dr              ; while x >= 227: x -= 227, lines += 1
        cmp #CIA_LINE_CC
        lda cia_dr+1
        sbc #0
        bcc fin
        lda cia_dr
        sbc #CIA_LINE_CC        ; C = 1
        sta cia_dr
        bcs :+
        dec cia_dr+1
:       inc cia_k
        bne red
        inc cia_k+1
        bne red
        inc cia_k+2
        bne red
        inc cia_k+3
        jmp red
fin:    lda cia_dr
        sta cia_st+CIA_PH,x
        lda cia_st+CIA_HLT,x
        bne none
        lda cia_k
        ora cia_k+1
        ora cia_k+2
        ora cia_k+3
        bne tick
none:   rts
tick:   jmp todadd
.endproc

; ---------------------------------------------------------------------------
; todadd — TOD += cia_k ticks (u32), alarm bit 2 if one of them lands on
; the alarm: hit iff k' = ((alarm - tod - 1) mod 2^24) + 1 <= ticks
; (chipset_ref tod_tick).  Preserves X.
; ---------------------------------------------------------------------------
.proc todadd
        jsr kprime              ; cia_d := k'
        lda cia_k               ; ticks >= k' ?
        cmp cia_d
        lda cia_k+1
        sbc cia_d+1
        lda cia_k+2
        sbc cia_d+2
        lda cia_k+3
        sbc cia_d+3
        bcc add
        lda cia_st+CIA_ICR,x
        ora #$04
        sta cia_st+CIA_ICR,x
        lda #1
        sta cia_st+CIA_TDY,x
add:    clc
        lda cia_st+CIA_TOD,x
        adc cia_k
        sta cia_st+CIA_TOD,x
        lda cia_st+CIA_TOD+1,x
        adc cia_k+1
        sta cia_st+CIA_TOD+1,x
        lda cia_st+CIA_TOD+2,x
        adc cia_k+2
        sta cia_st+CIA_TOD+2,x
        rts
.endproc

; kprime — cia_d := ((alarm - tod - 1) & $FFFFFF) + 1, 1..2^24.  Preserves X.
.proc kprime
        clc                     ; alarm - tod - 1
        lda cia_st+CIA_ALM,x
        sbc cia_st+CIA_TOD,x
        sta cia_d
        lda cia_st+CIA_ALM+1,x
        sbc cia_st+CIA_TOD+1,x
        sta cia_d+1
        lda cia_st+CIA_ALM+2,x
        sbc cia_st+CIA_TOD+2,x
        sta cia_d+2
        lda #0
        sta cia_d+3
        inc cia_d
        bne :+
        inc cia_d+1
        bne :+
        inc cia_d+2
        bne :+
        inc cia_d+3
:       rts
.endproc

; ---------------------------------------------------------------------------
; post — if ICR data & mask (chip X), set the chip's INTREQ bit.
; Preserves X.
; ---------------------------------------------------------------------------
.proc post
        lda cia_st+CIA_ICR,x
        and cia_st+CIA_MSK,x
        and #$1F
        beq done
        stx cia_x
        lda cia_st+CIA_BIT,x
        jsr mach_post
        ldx cia_x
done:   rts
.endproc

; ---------------------------------------------------------------------------
; getnow — cia_now := guest E time (0 before mach_init).  Preserves X.
; ---------------------------------------------------------------------------
.proc getnow
        lda mach_on
        bne on
        sta cia_now
        sta cia_now+1
        sta cia_now+2
        sta cia_now+3
        rts
on:     stx cia_x
        jsr mach_etime
        ldx #3
:       lda mach_et,x
        sta cia_now,x
        dex
        bpl :-
        ldx cia_x
        rts
.endproc

; ---------------------------------------------------------------------------
; rearm — keep chip X's slots in step with its state (CIA_T = now):
;   TA  started, INMODE 0, and (mask bit 0, or TB counting TA underflows
;       with mask bit 1): at CIA_T + TA + 1
;   TB  started, INMODE 0, mask bit 1: at CIA_T + TB + 1
;   TOD CIA-B, not halted, mask bit 2: the alarm's line (CIA_DLT)
; mach_arm/mach_disarm are called only when the wish changed.  Preserves X.
; ---------------------------------------------------------------------------
.proc rearm
        ; --- TA
        lda cia_st+CIA_CRA,x
        and #$21
        cmp #$01
        bne aoff
        lda cia_st+CIA_MSK,x
        lsr
        bcs aon
        lsr                     ; C = mask bit 1
        bcc aoff
        lda cia_st+CIA_CRB,x
        and #$41                ; TB started, INMODE 2/3
        cmp #$41
        bne aoff
aon:    lda cia_st+CIA_TA,x
        sta cia_k
        lda cia_st+CIA_TA+1,x
        sta cia_k+1
        lda #CIA_ARA
        ldy #CIA_DLA
        jsr want
        jmp tb
aoff:   lda #CIA_ARA
        jsr unwant
        ; --- TB
tb:     lda cia_st+CIA_CRB,x
        and #$61
        cmp #$01
        bne boff
        lda cia_st+CIA_MSK,x
        and #$02
        beq boff
        lda cia_st+CIA_TB,x
        sta cia_k
        lda cia_st+CIA_TB+1,x
        sta cia_k+1
        lda #CIA_ARB
        ldy #CIA_DLB
        jsr want
        jmp tod
boff:   lda #CIA_ARB
        jsr unwant
        ; --- TOD alarm (CIA-B only)
tod:    lda cia_st+CIA_SLT,x
        beq done
        lda cia_st+CIA_HLT,x
        bne toff
        lda cia_st+CIA_MSK,x
        and #$04
        beq toff
        lda cia_st+CIA_TDY,x
        beq tarm
        jsr alarmdl
tarm:   lda cia_st+CIA_ART,x      ; armed at CIA_DLT already?
        bne done
        lda #1
        sta cia_st+CIA_ART,x
        lda #CIA_SLT
        ldy #CIA_DLT
        jmp arm
toff:   lda #CIA_ART
        jsr unwant
done:   rts
.endproc

; want — slot wish "armed at CIA_T + cia_k(u16) + 1".  A = CIA_AR* field,
; Y = CIA_DL* field (the slot field is CIA_AR* - 3).  Preserves X.
.proc want
        sta cia_dd              ; field offsets
        sty cia_dd+1
        txa
        clc
        adc cia_dd+1
        tay                     ; Y = chip + CIA_DL*
        sec                     ; t = CIA_T + k + 1
        lda cia_st+CIA_T,x
        adc cia_k
        sta cia_k
        lda cia_st+CIA_T+1,x
        adc cia_k+1
        sta cia_k+1
        lda cia_st+CIA_T+2,x
        adc #0
        sta cia_k+2
        lda cia_st+CIA_T+3,x
        adc #0
        sta cia_k+3
        txa
        clc
        adc cia_dd
        sta cia_dr              ; chip + CIA_AR*
        stx cia_x
        tax
        lda cia_st,x
        beq new                 ; not armed
        lda cia_k               ; armed: same deadline?
        cmp cia_st,y
        bne new
        lda cia_k+1
        cmp cia_st+1,y
        bne new
        lda cia_k+2
        cmp cia_st+2,y
        bne new
        lda cia_k+3
        cmp cia_st+3,y
        bne new
        ldx cia_x
        rts
new:    lda #1
        sta cia_st,x
        lda cia_k
        sta cia_st,y
        lda cia_k+1
        sta cia_st+1,y
        lda cia_k+2
        sta cia_st+2,y
        lda cia_k+3
        sta cia_st+3,y
        ldx cia_x
        lda cia_dd
        sec
        sbc #CIA_ARA - CIA_SLA
        ldy cia_dd+1
        jmp arm
.endproc

; arm — mach_arm(slot field A, deadline field Y) of chip X.  Preserves X.
.proc arm
        sta cia_dd
        sty cia_dd+1
        stx cia_x
        txa
        clc
        adc cia_dd+1
        tay
        lda cia_st,y
        sta mach_at
        lda cia_st+1,y
        sta mach_at+1
        lda cia_st+2,y
        sta mach_at+2
        lda cia_st+3,y
        sta mach_at+3
        txa
        clc
        adc cia_dd
        tay
        lda cia_st,y            ; the slot
        jsr mach_arm
        ldx cia_x
        rts
.endproc

; unwant — A = CIA_AR* field: disarm its slot if armed.  Preserves X.
.proc unwant
        sta cia_dd
        txa
        clc
        adc cia_dd
        tay
        lda cia_st,y
        beq done
        lda #0
        sta cia_st,y
        tya
        sec
        sbc #CIA_ARA - CIA_SLA
        tay
        lda cia_st,y
        stx cia_x
        jsr mach_disarm
        ldx cia_x
done:   rts
.endproc

; alarmdl — CIA_DLT := CIA_T + ceil((227 k' - ph) / 5), the E time at which
; the TOD reaches the alarm; CIA_TDY := 0, CIA_ART := 0 (re-arm).  Preserves X.
.proc alarmdl
        jsr kprime              ; cia_d = k' (<= 2^24)
        ldy #3                  ; cia_k := 227 k' (< 2^32)
        lda #0
:       sta cia_k,y
        dey
        bpl :-
        lda #CIA_LINE_CC
        sta cia_dd
        ldy #8
mlp:    asl cia_k
        rol cia_k+1
        rol cia_k+2
        rol cia_k+3
        asl cia_dd
        bcc mnext
        clc
        lda cia_k
        adc cia_d
        sta cia_k
        lda cia_k+1
        adc cia_d+1
        sta cia_k+1
        lda cia_k+2
        adc cia_d+2
        sta cia_k+2
        lda cia_k+3
        adc cia_d+3
        sta cia_k+3
mnext:  dey
        bne mlp
        sec                     ; - ph + 4
        lda cia_k
        sbc cia_st+CIA_PH,x
        sta cia_d
        lda cia_k+1
        sbc #0
        sta cia_d+1
        lda cia_k+2
        sbc #0
        sta cia_d+2
        lda cia_k+3
        sbc #0
        sta cia_d+3
        clc
        lda cia_d
        adc #CIA_CC_PER_E - 1
        sta cia_d
        bcc :+
        inc cia_d+1
        bne :+
        inc cia_d+2
        bne :+
        inc cia_d+3
:       lda #CIA_CC_PER_E
        sta cia_dd
        lda #0
        sta cia_dd+1
        jsr div
        clc
        lda cia_st+CIA_T,x
        adc cia_d
        sta cia_st+CIA_DLT,x
        lda cia_st+CIA_T+1,x
        adc cia_d+1
        sta cia_st+CIA_DLT+1,x
        lda cia_st+CIA_T+2,x
        adc cia_d+2
        sta cia_st+CIA_DLT+2,x
        lda cia_st+CIA_T+3,x
        adc cia_d+3
        sta cia_st+CIA_DLT+3,x
        lda #0
        sta cia_st+CIA_TDY,x
        sta cia_st+CIA_ART,x
        rts
.endproc

; ---------------------------------------------------------------------------
; cia_reset — both chips to chipset_ref.CIA() power-up, anchored at now.
; ---------------------------------------------------------------------------
.proc cia_reset
        lda #1
        sta cia_ok
        ldx #2 * CIA_SZ - 1
        lda #0
:       sta cia_st,x
        dex
        bpl :-
        jsr getnow
        ldx #0
        jsr one
        lda #CIA_PRA_IN_A
        sta cia_st+CIA_PIN
        lda #CIA_IRQ_A
        sta cia_st+CIA_BIT
        lda #1
        sta cia_st+CIA_SLA
        lda #2
        sta cia_st+CIA_SLB
        ldx #CIA_B
        jsr one
        lda #CIA_PRA_IN_B
        sta cia_st+CIA_B+CIA_PIN
        lda #CIA_IRQ_B
        sta cia_st+CIA_B+CIA_BIT
        lda #3
        sta cia_st+CIA_B+CIA_SLA
        lda #4
        sta cia_st+CIA_B+CIA_SLB
        lda #5
        sta cia_st+CIA_B+CIA_SLT
        ldx #2                  ; callbacks: slot 0 (VERTB) .. slot 5
:       lda cbtab-2,x
        sta mach_cbv,x
        inx
        cpx #12
        bne :-
        lda #<cb_vbl
        sta mach_cbv
        lda #>cb_vbl
        sta mach_cbv+1
        lda #<cia_intreq        ; INTREQ-write hook (chip.s, after SET/CLR)
        sta mach_ihook
        lda #>cia_intreq
        sta mach_ihook+1
        lda #1                  ; slots 1-5 disarmed
:       pha
        jsr mach_disarm
        pla
        clc
        adc #1
        cmp #6
        bne :-
        rts
one:    lda #$FF                ; latches and counters $FFFF
        sta cia_st+CIA_TAL,x
        sta cia_st+CIA_TAL+1,x
        sta cia_st+CIA_TBL,x
        sta cia_st+CIA_TBL+1,x
        sta cia_st+CIA_TA,x
        sta cia_st+CIA_TA+1,x
        sta cia_st+CIA_TB,x
        sta cia_st+CIA_TB+1,x
        lda #1
        sta cia_st+CIA_TDY,x
        lda cia_now
        sta cia_st+CIA_T,x
        lda cia_now+1
        sta cia_st+CIA_T+1,x
        lda cia_now+2
        sta cia_st+CIA_T+2,x
        lda cia_now+3
        sta cia_st+CIA_T+3,x
        rts
.endproc

cbtab:  .word cb_ata, cb_atb, cb_bta, cb_btb, cb_btod

; ---------------------------------------------------------------------------
; Event callbacks (service pass, $01 = $35; clobber A/X/Y).  The fired slot
; is forgotten first, so the sync's rearm arms it again if still wanted.
; ---------------------------------------------------------------------------
cb_ata: lda #0
        sta cia_st+CIA_ARA
        ldx #0
        beq cbsync
cb_atb: lda #0
        sta cia_st+CIA_ARB
        ldx #0
        beq cbsync
cb_bta: lda #0
        sta cia_st+CIA_B+CIA_ARA
        beq cbb
cb_btb: lda #0
        sta cia_st+CIA_B+CIA_ARB
        beq cbb
cb_btod:
        lda #0
        sta cia_st+CIA_B+CIA_ART
cbb:    ldx #CIA_B
cbsync: jsr sync
        jmp rearm

; cb_vbl — slot 0, after every VERTB: CIA-A TOD += 1 (chipset_ref
; tod_tick(1)), alarm on equality, then post if asserting.  Every 256th
; frame it also syncs both chips (results are split-invariant), so a chip
; nobody touches never goes 2^31 E ticks without a sync (sync reads a
; larger gap as time going backwards).
.proc cb_vbl
        inc cia_vc
        bne tod
        ldx #0
        jsr sync
        jsr rearm
        ldx #CIA_B
        jsr sync
        jsr rearm
tod:    ldx #0
        lda cia_st+CIA_HLT
        bne done
        inc cia_st+CIA_TOD
        bne :+
        inc cia_st+CIA_TOD+1
        bne :+
        inc cia_st+CIA_TOD+2
:       lda cia_st+CIA_TOD
        cmp cia_st+CIA_ALM
        bne chk
        lda cia_st+CIA_TOD+1
        cmp cia_st+CIA_ALM+1
        bne chk
        lda cia_st+CIA_TOD+2
        cmp cia_st+CIA_ALM+2
        bne chk
        lda cia_st+CIA_ICR
        ora #$04
        sta cia_st+CIA_ICR
chk:    jmp post
done:   rts
.endproc

; ---------------------------------------------------------------------------
; cia_intreq — the guest wrote INTREQ: a chip still asserting /INT raises
; its bit again (chipset_ref custom_write $09C).  Clobbers A/X/Y.
; ---------------------------------------------------------------------------
.proc cia_intreq
        lda cia_ok
        beq done
        ldx #0
        jsr post
        ldx #CIA_B
        jmp post
done:   rts
.endproc
