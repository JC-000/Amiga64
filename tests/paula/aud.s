; ---------------------------------------------------------------------------
; tests/paula/aud.s — W15-P: Paula's four audio channels on the U64 sampler,
; a machine-mode module (docs/WAVE15_BRIEF.md W15-P and its addendum;
; docs/CONTRACTS.md "Audio (Paula)"; reference host/paula_ref.py).
;
; NOT linked into the runner PRG: tests/run_paula.py build_aud() assembles it
; at test time with the runner's labels as -D defines into a raw binary at
; AUD_ORG ($CA00, code to $CCFF); its state page is $CD00-$CDFF.  Player
; profile only: $CA00-$CDFF is buf_result, which machine mode leaves unused,
; but aros_resume's stub ($CB00/$CC00) and run_irq's P/R/LOG ($CA00-$CCFF)
; use it in their own sessions; it is never staged with them.
;
; Entries (ID = SHA-1[:4] of the binary with the ID field zero, at +3):
;   +0  jmp write   chip.s's route (c_wrw cop0), taken when aud_mon = ID
;                   byte 0: X = $0A0-$0FE or $096, chip_vh:chip_vl = the
;                   word written; $0E0+ are handed to cop_w0, DMACON to
;                   cop_w0 first.  Keeps Y, clobbers A/X.
;   +7  jmp fire    mach slot AUD_SLOT's callback (the host stores AUD_ORG+7
;                   in mach_cbv[6]); service-pass rules; clobbers A/X/Y.
;   +10 jmp init    host: channels off, state zeroed (config kept), every
;                   voice silenced first when cfg_hw; slot 6 disarmed.
;   +13 config (host writes, LE): u16 cfg_r16 = E ticks per colour clock *
;                   65536 (< 32768), u8 cfg_hw (1 = write the sampler: only
;                   inside a U64E hold with "Map Ultimate Audio" on), u24
;                   cfg_zero = REU offset of >= 2 zero bytes (the W13-D
;                   disp_zero row, $DF4000).
;   +19 counters: u16 st_nw (audio/DMACON writes), u16 st_nfire (passes
;                   that ended a channel's block), u8 st_ndat (AUDxDAT
;                   writes, ignored), u32 st_now (+24: E of the latest
;                   DMACON, loop downgrade or firing: the tests' clock), u8 st_nres (+28:
;                   deadline grids restarted after FIRE_N blocks in one
;                   pass), u16 st_npark (+29: parked block ends).
;
; Model (Amiga HRM, audio): AUDxLC/AUDxLEN are latches.  DMA start (DMACON
; DMAEN and AUDxEN both set) copies them into the playing block and posts
; INTREQ AUDx (bit 7 + x, level 4) at once; each block end copies them again
; and posts again.  A block lasts 2 * LEN * PER colour clocks (LEN 0 =
; 65536 words, PER 0 counts as 1), capped at 2^30 E; a colour clock is
; cfg_r16 / 65536 E ticks.  Deadlines are fixed point (E * 65536, 6 bytes),
; so a channel never drifts; slot 6 is armed at the earliest ceil(deadline)
; of the channels that play.  PER and VOL act on the voice (the sound) at
; once; a block's end is fixed when it starts, so a PER written mid-block
; moves block ends from the next block on (W15-P deviation, for cost).  DMA
; off stops the channel and plays the two zero bytes at cfg_zero once, so
; the voice does not hold its last sample as DC.  AUDxDAT (CPU mode) is
; counted and ignored.
; Firing: every due channel ends its block and the following ones while
; they are due (up to FIRE_N per pass, then the grid restarts at now).
; Parking: a block end needs no work when the voice loops the block in
; hardware and the channel's INTREQ AUDx bit is still pending (unacknowl-
; edged): a post would change nothing.  Such a channel is looked at again
; every PARK_E E; once the guest has acknowledged, the next look ends the
; block (posts) and the grid goes on from then.  So a replay that ignores
; AUDx costs one look per channel per PARK_E, even with a 1-word loop.
;
; Sampler voice x = channel x ($DF20 + 32 x; HARDWARE "U64 sampler"):
; START = $1000000 + REU offset (guest chip RAM is the REU, flat), LENGTH =
; 2 * words, RATE = (PER * RATE_K - RATE_C) >> 24 clamped to 0..$FFFF, VOL =
; min(vol, 63), PAN 0 (channels 0, 3) / 15 (1, 2).  REPEAT A/B (relative to
; START) + CTRL bit 1 only while the latched block ends where the voice's
; span ends and starts inside it: the hardware loop then plays Paula's next
; blocks with no 6510 work; otherwise the voice is reprogrammed at the block
; end (counted).  CTRL bit 2 (the sampler's end IRQ) is NEVER set: mach_irq
; acknowledges CIA #1 only, so a cartridge IRQ would storm.  The voice image
; is kept in the state page in the sampler's own layout (the "dry" backend
; the VICE tests read) and copied to $DF20 only when cfg_hw is set.  Never
; probed: on VICE $DF20-$DFFF mirrors the REU's REC, so a probe would read
; the REC and a write would start a DMA.
;
; State page (host-visible), 32 bytes per channel x, at VC + 32 x / CH + 32 x:
;   VC ($CD00): the voice image, the sampler's register block (BE fields):
;     +0 CTRL, +1 VOL, +2 PAN, +4..7 START, +9..B LENGTH, +E..F RATE,
;     +11..13 REPEAT A, +15..17 REPEAT B (both 0 when not looping); the
;     other bytes up to +17 stay 0 and are written to the sampler as 0 (the
;     top bytes of the fields' 32-bit slots).  +18..1D: the cached block
;     length (E * 65536), +1E: it is valid (never sent to the sampler).
;   CH ($CD80), LE: +0 LC latch (u24, bit 0 = 0), +3 LEN latch, +5 PER,
;     +7 VOL (as written), +8 on, +9 the playing block's LC (u24), +12 its
;     LEN, +14 deadline (6 bytes: fraction u16, E u32), +20 blocks started
;     (u16, = AUDx posts), +22 (unused, 0: the lateness maximum went for
;     space in fix round 1), +24 voice programs (u8, wraps), +25 E of the
;     latest post (u32), +29..+31 scratch (sy_c, sy, tb ...).
; ---------------------------------------------------------------------------

.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"

.ifndef chip_vl
.error "-D chip_vl=... (runner.lbl)"
.endif
.ifndef chip_vh
.error "-D chip_vh=... (runner.lbl)"
.endif
.ifndef cop_sh
.error "-D cop_sh=... (runner.lbl)"
.endif
.ifndef cop_w0
.error "-D cop_w0=... (runner.lbl)"
.endif
.ifndef chip_shd
.error "-D chip_shd=... (runner.lbl)"
.endif
.ifndef mach_etime
.error "-D mach_etime=... (runner.lbl)"
.endif
.ifndef mach_et
.error "-D mach_et=... (runner.lbl)"
.endif
.ifndef mach_at
.error "-D mach_at=... (runner.lbl)"
.endif
.ifndef mach_arm
.error "-D mach_arm=... (runner.lbl)"
.endif
.ifndef mach_disarm
.error "-D mach_disarm=... (runner.lbl)"
.endif
.ifndef mach_post
.error "-D mach_post=... (runner.lbl)"
.endif
.ifndef mach_am
.error "-D mach_am=... (runner.lbl)"
.endif
.ifndef mach_dl
.error "-D mach_dl=... (runner.lbl)"
.endif
.ifndef aud_mon
.error "-D aud_mon=... (runner.lbl)"
.endif

; Every sampler write goes through SPUT (A = value, Y = 32 x voice + register).
; The test build `-D AUD_LOG=<addr>` turns it into a call of the VICE driver's
; logger (tests/paula/drv.s aud_log: a register mirror, a command ring and the
; REPEAT B == LENGTH check), same size, so the log build is the product code.
.macro SPUT
.ifdef AUD_LOG
        jsr AUD_LOG
.else
        sta SAMP,y
.endif
.endmacro

AUD_SLOT = 6                    ; mach event slot (supervisor R2)
SAMP     = aud_samp             ; sampler voice 0 ($DF20); voice v at + 32 v
RATE_K   = 29563210             ; 2^24 * 281.937 ns / 160 ns
RATE_C   = 20761805             ; 2^24 * (118 / 160 + 1 - 0.5)

VC       = buf_result + $500    ; voice images ($CD00)
CH       = buf_result + $580    ; channel state ($CD80)
V_CTL    = 0
V_VOL    = 1
V_PAN    = 2
V_ST     = 4                    ; BE: +4 bits 25-24, +5 23-16, +6, +7
V_LN     = 9                    ; BE: +9 bits 23-16, +A, +B
V_RT     = 14                   ; BE
V_RA     = 17                   ; BE
V_RB     = 21                   ; BE
V_END    = 23                   ; the last register byte copied
V_DUR    = 24                   ; (not a register) the block-length cache, 6 bytes
V_DV     = 30                   ; (not a register) the cache is valid
C_LC     = 0
C_LEN    = 3
C_PER    = 5
C_VOL    = 7
C_ON     = 8
C_CLC    = 9
C_CLEN   = 12
C_DL     = 14
C_NB     = 20
C_LT     = 22
C_NP     = 24
C_LE     = 25
; scratch scalars in the records' spare bytes (CH + 32 x + 29..31; init
; zeroes them with the page)
sy_c     = CH+29                ; the caller's Y (write)
sy       = CH+30                ; Y across a helper
tb       = CH+31
tdm      = CH+32+29             ; DMACON's AUDxEN bits being walked
tbn      = CH+32+30             ; blocks left in this pass (fire)
tf       = CH+32+31
acnt     = CH+64+29

.segment "CODE"
org:    jmp write
        .byte 0, 0, 0, 0        ; +3: the module ID (stamped by the build)
        jmp efire
        jmp einit
cfg_r16:  .word 0               ; +13
cfg_hw:   .byte 0               ; +15
cfg_zero: .byte 0, 0, 0         ; +16
st_nw:    .word 0               ; +19
st_nfire: .word 0               ; +21
st_ndat:  .byte 0               ; +23
st_now:   .byte 0, 0, 0, 0      ; +24: E of the latest DMACON / PER rescale / firing
st_nres:  .byte 0               ; +28: grid restarts (fire fell FIRE_N blocks behind)
st_npark: .word 0               ; +29: parked block ends (u16)
ST_N = * - st_nw
.assert cfg_r16 = org + 13, error, "aud.s: config moved"
.assert st_nw = org + 19, error, "aud.s: counters moved"

; --- the entries: segment B must carry this ID ------------------------------
; bok — C = 0 when B's ID equals A's; else aud_mon := 0 (the router stops
; calling) and C = 1.  Keeps Y.
bok:    ldx #3
:       lda org+3,x
        cmp bid,x
        bne :+
        dex
        bpl :-
        clc
        rts
:       lda #0
        sta aud_mon
        lda cfg_hw              ; stop every voice: B's high byte $FF (B
        beq :++                 ; beyond any LENGTH, M5), then CTRL 0; a
        ldy #96+V_RB            ; stopped voice holds its last sample (DC)
:       lda #$FF
        SPUT
        tya
        and #$E0
        tay
        lda #0
        SPUT
        tya
        ora #V_RB
        sec
        sbc #32
        tay
        bcs :-
:       sec
        rts
efire:  jsr bok
        bcs :+
        jmp fire
:       rts
einit:  jsr bok
        bcs :-
        jmp init

; --- write: an audio register, DMACON, or one of cop's ---------------------
; chip.s routes X >= $0A0 and X = $096 here once armed: $0E0+ (BPLxPT) go
; straight back to cop_w0; DMACON goes to cop_w0 first (cop.s owns DMACON;
; its new value is then in cop_sh).
write:  cpx #$E0
        bcc :+
        jmp cop_w0
:       cpx #$96
        bne :+
        jsr cop_w0              ; (keeps Y)
        ldx #$96
:       sty sy_c
        stx tb
        jsr bok
        ldx tb
        bcc :+
        ldy sy_c
        rts
:       inc st_nw
        bne :+
        inc st_nw+1
:       cpx #$96
        bne :+
        jmp dmacon
:       txa
        sec
        sbc #$A0
        pha
        and #$F0
        asl a
        tay                     ; Y = 32 * channel
        pla
        and #$0F
        tax                     ; X = register within the channel
        lda chip_vl
        cpx #$00
        beq lch
        cpx #$02
        beq lcl
        cpx #$04
        beq len
        cpx #$06
        beq per
        cpx #$08
        beq vol
        cpx #$0A
        bne out
        inc st_ndat
out:    ldy sy_c
        rts
lch:    and #$1F
        sta CH+C_LC+2,y
        bcs relp                ; (C = 1 from the cpx that matched)
lcl:    and #$FE
        sta CH+C_LC,y
        lda chip_vh
        sta CH+C_LC+1,y
        bcs relp
len:    sta CH+C_LEN,y
        lda chip_vh
        sta CH+C_LEN+1,y
relp:   lda CH+C_ON,y           ; a playing voice's loop follows the latches
        beq out
        jsr hwloop
        jmp out
vol:    sta CH+C_VOL,y
        lda CH+C_ON,y
        beq out
        jsr hwvol
        ldx #V_VOL
        jsr hw1
        jmp out
per:    lda #0                  ; the next blocks' length changes (this
        sta VC+V_DV,y           ; block keeps its end: W15-P deviation)
        lda chip_vl
        sta CH+C_PER,y
        lda chip_vh
        sta CH+C_PER+1,y
        lda CH+C_ON,y
        beq out
        jsr rate
        ldx #V_RT
        jsr hw1
        inx
        jsr hw1
        jmp out

; --- DMACON: the new value is in cop_sh+1 (LE) ------------------------------
dmacon: jsr now                 ; (keeps Y)
        lda cop_sh+2
        and #$02                ; DMAEN (bit 9)
        beq :+
        lda cop_sh+1            ; AUD0-3EN
:       sta tdm
        ldy #0
        sty tf                  ; nothing started or stopped yet
dch:    lsr tdm                 ; C = this channel's DMA
        lda CH+C_ON,y
        bcc doff
        bne dnx                 ; running: no restart
        jsr start
        inc tf
        bne dnx                 ; (always)
doff:   beq dnx
        lda #0
        sta CH+C_ON,y
        jsr silence
        inc tf
dnx:    tya
        clc
        adc #32
        tay
        cpy #128
        bcc dch                 ; (bcc: any Y >= 128 ends the walk)
        lda tf
        beq :+
        jsr rearm
:       jmp out

; dlnow — dl = now * 65536 + mM.  Keeps Y.
dlnow:  lda #0
        sta tA
        sta tA+1
        ldx #3
:       lda mach_et,x
        sta tA+2,x
        dex
        bpl :-
        ; fall through: dl = tA + mM
dladd:  sty sy
        clc
        ldx #$FA
:       lda tA-$FA,x
        adc mM-$FA,x
        sta CH+C_DL,y
        iny
        inx
        bne :-
        ldy sy
dlx:    rts

; now — mach_etime, and st_now := it.  Keeps Y.
now:    jsr mach_etime
        ldx #3
:       lda mach_et,x
        sta st_now,x
        dex
        bpl :-
        rts

; --- start / block -------------------------------------------------------
; start — Y = 32 ch, mach_et = now: on, dl = now * 65536, then block.
start:  lda #1
        sta CH+C_ON,y
        ldx #5
        lda #0
:       sta mM,x
        dex
        bpl :-
        jsr dlnow
        ; fall through
; block — the latches become the playing block, dl += one block, AUDx
; posted (now) and counted, the voice reprogrammed unless its hardware loop
; already plays the block.  Y = 32 ch, mach_et = now; keeps Y.
block:  lda CH+C_LEN,y          ; a new LEN: the cached block length goes
        cmp CH+C_CLEN,y         ; (it is the playing block's: keyed on its
        bne :+                  ; LEN, so a pull or park in between cannot
        lda CH+C_LEN+1,y        ; leave a stale one)
        cmp CH+C_CLEN+1,y
        beq :++
:       lda #0
        sta VC+V_DV,y
:       sty sy
        ldx #5
:       lda CH+C_LC,y           ; LC (3), LEN (2) -> the playing block
        sta CH+C_CLC,y
        iny
        dex
        bne :-
        ldy sy
        jsr durc                ; mM = one block
        jsr dlrdt               ; tA = dl
        jsr dladd               ; dl = tA + mM
        lda CH+C_NB,y
        clc
        adc #1
        sta CH+C_NB,y
        bcc :+
        lda CH+C_NB+1,y
        adc #0
        sta CH+C_NB+1,y
:       sty sy
        ldx #$FC
:       lda mach_et-$FC,x     ; E of this post
        sta CH+C_LE,y
        iny
        inx
        bne :-
        ldy sy
        tya
        lsr a
        lsr a
        lsr a
        lsr a
        lsr a
        adc #7                  ; INTREQ AUDx (C = 0: bit 4 of Y is 0)
        jsr mach_post           ; keeps Y
        lda VC+V_CTL,y          ; looping in hardware: it plays the block
        and #2                  ; (while a channel plays, every latch write
        bne :+                  ; recomputes the loop against the voice's
        jmp prog                ; span, so CTRL bit 1 = the latches' block)
:       rts

; dlrdt — tA = dl (6 bytes).  Keeps Y.
dlrdt:  sty sy
        ldx #$FA
:       lda CH+C_DL,y
        sta tA-$FA,x
        iny
        inx
        bne :-
        ldy sy
        rts

; --- fire: slot 6's callback (service pass) ----------------------------------
; Every playing channel whose due tick is <= now (signed) ends its block
; (the lateness of the first is recorded), and so do its following blocks
; while they are due too: up to FIRE_N per pass, then the deadline restarts
; at now + one block (st_nres counts these; short blocks on a slow host).
; AUDx is posted for every block (posts coalesce in INTREQ).  Then rearm.
FIRE_N = 16
PARK_E = 7094                   ; a parked look comes every 2^k blocks >= PARK_E
                                ; (half a frame): every 1/2 to 1 frame
fire:   jsr now
        ldy #0
fl:     lda CH+C_ON,y
        beq fnx
        jsr late
        bmi fnx                 ; not due yet
        inc st_nfire
        bne :+
        inc st_nfire+1
:       lda #FIRE_N
        sta tbn
fb:     jsr parkq
        bcs fpk
        jsr block
        jsr late
        bmi fnx
        dec tbn
        bne fb
        inc st_nres             ; still behind: restart the grid at now
        jsr durc
        jsr dlnow
        jmp fnx
fpk:    inc st_npark            ; parked: the next look 2^k blocks on, on
        bne :+                  ; the grid (M6)
        inc st_npark+1
:       jsr pkstep
        jsr dlrdt
        jsr dladd
fnx:    tya
        clc
        adc #32
        tay
        cpy #128
        bcc fl                 ; (bcc: any Y >= 128 ends the walk)
        jmp rearm

; parkq — C = 1 when the voice loops this block in hardware (CTRL bit 1:
; nothing to reload) and its INTREQ AUDx bit is still pending: another post
; would change nothing, so the block end needs no work.  Keeps Y.
parkq:  lda VC+V_CTL,y
        and #2
        beq pno
        lda chip_shd+$1E        ; INTREQR (BE): AUD0 = bit 7 of the low
        asl a                   ; byte, AUD1-3 = bits 0-2 of the high one
        sta tb
        lda chip_shd+$1F
        asl a                   ; C = AUD0
        lda tb
        adc #0                  ; bits 0-3 = AUD0-3 pending
        sta tb
        tya
        lsr a
        lsr a
        lsr a
        lsr a
        lsr a
        tax
        lda tb
:       lsr a                   ; C = this channel's bit
        dex
        bpl :-
        rts
pno:    clc
        rts

; pkstep — mM = 2^k blocks >= PARK_E (k >= 0): a parked channel's deadline
; advances by it, so it stays on the block grid (M6); with PARK_E half a
; frame, a look comes every 1/2 to 1 frame.  Keeps Y.
pkstep: jsr durc
:       lda mM+4                ; mM >= PARK_E * 65536?  (P's low 16 bits
        ora mM+5                ; are 0: compare bits 47-16)
        bne :+
        lda mM+2
        cmp #<PARK_E
        lda mM+3
        sbc #>PARK_E
        bcs :+
        asl mM
        rol mM+1
        rol mM+2
        rol mM+3
        rol mM+4
        rol mM+5
        bcc :-                  ; (always: mM < 2^32 here)
:       rts

.assert * <= VC, error, "aud.s: segment A runs into the state page"

; ===========================================================================
; Segment B ($0400-$07FF, player profile only; $07F8-$07FF are the bank-0
; sprite pointers, unused in machine mode: the VIC is on bank 3): its first 4 bytes are the
; module ID (stamped by the build); bok checks them before every entry.
.segment "CODEB"
bid:    .byte 0, 0, 0, 0

; --- the voice ------------------------------------------------------------
; prog — program the voice from the playing block (CTRL 0 first, so it
; restarts at START).  Y = 32 ch, kept.
prog:   lda #1
        sta VC+V_ST,y
        lda CH+C_CLC,y
        sta VC+V_ST+3,y
        lda CH+C_CLC+1,y
        sta VC+V_ST+2,y
        lda CH+C_CLC+2,y
        sta VC+V_ST+1,y
        ldx #C_CLEN
        jsr words
        jsr setln
        jsr rate
        jsr hwvol
        jsr loop
        ; fall through
; hwall — PAN, the program count, then the whole image to the sampler:
; CTRL 0, +$17 down to +1, CTRL.  Keeps Y.
hwall:  lda CH+C_NP,y
        clc
        adc #1
        sta CH+C_NP,y
        lda cfg_hw
        beq hwx
        jsr rbff                ; B := $FFFFFF: never LENGTH (always even)
        lda #0                  ; a real stop (CTRL 2 does not stop a
        SPUT                    ; playing voice [MEASURED HW])
        sty sy
        tya
        ora #V_RB-1             ; +$14 down to +$01: all fields but B
        tay
:       lda VC,y
        SPUT
        dey
        cpy sy
        bne :-
        ldx #V_CTL              ; CTRL: start at START
        jsr hw1
        ; fall through
; rbimg — B := the image's, low byte first: from $FFFFFF every step stays
; >= $FF0000 (beyond any LENGTH) until the last.  Keeps Y.
rbimg:  ldx #V_RB+2
hw3d:   jsr hw1                 ; registers X, X-1, X-2 := the image's
        dex
        jsr hw1
        dex
        jmp hw1
; rbff — B := $FFFFFF, low byte first (each step >= the old B: never an
; earlier loop point).  Keeps Y.
rbff:   lda #$FF
        ldx #V_RB+2
        jsr hwk
        dex
        jsr hwk
        dex
        jmp hwk
hwx:    rts

; hwloop — the latches changed while playing: recompute the loop.  M5: B
; == LENGTH only while looping (a loop always ends at the voice's end), so
; an unchanged state rewrites A only; a change goes through B = $FFFFFF
; (never LENGTH: LENGTH is even; a voice with B beyond LENGTH ends there
; normally), then A, CTRL, then B low byte first.  So repeat is never off
; with B == LENGTH, not even between two writes.  A downgrade also pulls
; the deadline to the first grid point after now: the 6510 must reprogram
; at the hardware's loop end, and a parked channel's deadline may lie up to
; a frame ahead (M6).  Y = 32 ch on entry; clobbers X/Y on a downgrade
; (rearm), keeps Y otherwise.
hwloop: lda VC+V_CTL,y
        pha
        jsr loop
        pla
        cmp VC+V_CTL,y
        beq hwa                 ; unchanged: A only (B is unchanged too)
        jsr rbff                ; changed: B beyond LENGTH, A, CTRL (no
        jsr hwa                 ; restart [MEASURED HW]), then B
        ldx #V_CTL
        jsr hw1
        jsr rbimg
        lda VC+V_CTL,y
        and #2
        bne hwx                 ; upgrade: done
        jsr now                 ; downgrade: the 6510 reprograms at the
        jsr pull                ; hardware's loop end (M6)
        jmp rearm
hwa:    ldx #V_RA+2             ; REPEAT A, 3 bytes
        bne hw3d                ; (always)

; pull — dl := the first grid point after now (one block at a time back
; from a parked deadline).  mach_et = now.  Keeps Y.
pull:   jsr durc                ; mM = one block
:       jsr dlrdt               ; tA = dl
        sty sy
        sec
        ldx #$FA
:       lda tA-$FA,x            ; dl := dl - one block
        sbc mM-$FA,x
        sta CH+C_DL,y
        iny
        inx
        bne :-
        ldy sy
        jsr late                ; still after now: go on
        bmi :--
        jsr dlrdt               ; one block back up
        jmp dladd

; hw1 — sampler register X of voice Y := its image byte (when cfg_hw).
; Keeps X, Y.
hw1:    sty sy
        stx tb
        tya
        ora tb
        tay
        lda VC,y
        jmp hwp
; hwk — sampler register X of voice Y := A.  Keeps A, X, Y.
hwk:    sty sy
        stx tb
        pha
        tya
        ora tb
        tay
        pla
hwp:    ldx cfg_hw
        beq :+
        SPUT
:       ldy sy
        ldx tb
        rts

; silence — play the two zero bytes at cfg_zero once; rate and volume
; stay.  Y = 32 ch, kept.
silence:
        lda #1
        sta VC+V_ST,y
        lda cfg_zero+2
        sta VC+V_ST+1,y
        lda cfg_zero+1
        sta VC+V_ST+2,y
        lda cfg_zero
        sta VC+V_ST+3,y
        lda #2                  ; LENGTH 2
        sta VC+V_LN+2,y
        lda #0
        sta VC+V_LN+1,y
        sta VC+V_LN,y
        jsr lno
        jmp hwall

; setln — LENGTH := tN (LE -> BE).  Keeps Y.
setln:  lda tN
        sta VC+V_LN+2,y
        lda tN+1
        sta VC+V_LN+1,y
        lda tN+2
        sta VC+V_LN,y
        rts

; loop — REPEAT A/B := (a, a + n) when the latched block (LC, n = 2 *
; words) starts inside the voice's span (a = LC - START >= 0) and ends where
; it ends (a + n = LENGTH); else both 0.  CTRL := 3 / 1.  Keeps Y.
loop:   sec
        lda CH+C_LC,y
        sbc VC+V_ST+3,y
        sta tA
        lda CH+C_LC+1,y
        sbc VC+V_ST+2,y
        sta tA+1
        lda CH+C_LC+2,y
        sbc VC+V_ST+1,y
        sta tA+2
        bcc lno                 ; LC before START
        ldx #C_LEN
        jsr words               ; tN = n
        clc
        lda tA
        adc tN
        sta tN
        lda tA+1
        adc tN+1
        sta tN+1
        lda tA+2
        adc tN+2
        sta tN+2
        lda tN
        cmp VC+V_LN+2,y
        bne lno
        lda tN+1
        cmp VC+V_LN+1,y
        bne lno
        lda tN+2
        cmp VC+V_LN,y
        bne lno
        lda tA
        sta VC+V_RA+2,y
        lda tA+1
        sta VC+V_RA+1,y
        lda tA+2
        sta VC+V_RA,y
        lda tN
        sta VC+V_RB+2,y
        lda tN+1
        sta VC+V_RB+1,y
        lda tN+2
        sta VC+V_RB,y
        lda #3
        bne lct
lno:    lda #0
        sta VC+V_RA,y
        sta VC+V_RA+1,y
        sta VC+V_RA+2,y
        sta VC+V_RB,y
        sta VC+V_RB+1,y
        sta VC+V_RB+2,y
        lda #1
lct:    sta VC+V_CTL,y
        rts

; words — tN = 2 * words of the LEN at CH + Y + X (0 = 65536 words).
; Keeps Y.
words:  stx tb
        tya
        ora tb
        tax
        lda CH,x
        sta tN
        ora CH+1,x
        cmp #1                  ; C = 0 only for LEN = 0
        lda CH+1,x
        sta tN+1
        lda #0
        bcs :+
        lda #1
:       sta tN+2
        asl tN
        rol tN+1
        rol tN+2
        rts

; --- arithmetic -------------------------------------------------------------
; durc — mM = the playing block's length: the cache (VC+V_DUR) when valid,
; else dur, cached.  LEN and PER writes invalidate it.  Keeps Y.
durc:   lda VC+V_DV,y
        bne dcr
        jsr dur
        lda #1
        sta VC+V_DV,y
        sec
        bcs :+
dcr:    clc
:       sty sy
        ldx #$FA
:       bcs :+
        lda VC+V_DUR,y          ; the cache -> mM
        sta mM-$FA,x
        bcc :++
:       lda mM-$FA,x            ; mM -> the cache
        sta VC+V_DUR,y
:       iny
        inx
        bne :---
        ldy sy
        rts

; dur — mM = min(2 * words * max(PER, 1) * r16, CAP): the playing block in
; E * 65536.  Keeps Y.
dur:    ldx #C_CLEN
        jsr words
        lda #0
        ldx #5
:       sta mM,x
        dex
        bpl :-
        ldx #2
:       lda tN,x
        sta mM,x
        dex
        bpl :-
        lda CH+C_PER,y
        ldx CH+C_PER+1,y
        jsr q1
        jsr mul                 ; mR = 2 * words * per (< 2^34)
        ldx #5
:       lda mR,x
        sta mM,x
        dex
        bpl :-
        lda cfg_r16
        ldx cfg_r16+1
        jsr q1
        jsr mul
        ; fall through
; cap — mM = min(mR, 2^46 - 1).  Keeps Y.
cap:    ldx #5
        lda mR+7
        ora mR+6
        bne :+
        lda mR+5
        cmp #$40
        bcc ccp
:       lda #$FF
:       sta mM,x
        dex
        bpl :-
        lda #$3F
        sta mM+5
        rts
ccp:    lda mR,x
        sta mM,x
        dex
        bpl ccp
        rts

; rate — RATE := clamp((PER * RATE_K - RATE_C) >> 24, 0, $FFFF).  Keeps Y.
rate:   ldx #5
:       lda rk,x
        sta mM,x
        dex
        bpl :-
        lda CH+C_PER,y
        sta mR
        lda CH+C_PER+1,y
        sta mR+1
        jsr mul
        sec
        ldx #$F8
:       lda mR-$F8,x
        sbc rc-$F8,x          ; RATE_C, then zeros
        sta mR-$F8,x
        inx
        bne :-
        lda #0
        bcc rst                 ; negative: 0
        lda mR+5
        ora mR+6
        ora mR+7
        beq :+
        lda #$FF                ; above $FFFF
rst:    sta VC+V_RT,y
        sta VC+V_RT+1,y
        rts
:       lda mR+4
        sta VC+V_RT,y
        lda mR+3
        sta VC+V_RT+1,y
        rts

rk:     .byte <RATE_K, >RATE_K, ^RATE_K, RATE_K >> 24, 0, 0
rc:     .byte <RATE_C, >RATE_C, ^RATE_C, RATE_C >> 24, 0, 0, 0, 0

; q1 — mR+0/1 = A:X, or 1 when 0.  Keeps Y.
q1:     sta mR
        stx mR+1
        ora mR+1
        bne :+
        inc mR
:       rts

; mul — mR (8 bytes) = mM (6 bytes) * mR+0/1 (in place).  Keeps Y.
mul:    lda #0
        ldx #5
:       sta mR+2,x
        dex
        bpl :-
        lda #16
        sta acnt
        lsr mR+1
        ror mR
ml:     bcc ms
        clc
        ldx #$FA
:       lda mR+2-$FA,x
        adc mM-$FA,x
        sta mR+2-$FA,x
        inx
        bne :-
ms:     ldx #7
:       ror mR,x
        dex
        bpl :-
        dec acnt
        bne ml
        rts

; --- rearm: slot 6 at the earliest due tick of the playing channels --------
; due = E part + (fraction != 0); compared by signed u32 difference.  Only
; touches the seam when the wanted state differs (as cia.s: the seam's
; mach_am/mach_dl are the truth).  Clobbers A/X/Y.
rearm:  lda #0
        sta tf
        ldy #96
rl:     lda CH+C_ON,y
        beq rnx
        jsr due                 ; tA = due
        lda tf
        beq rtk                 ; the first one
        sec                     ; due - tw: bit 31 set = earlier
        ldx #$FC
:       lda tA-$FC,x
        sbc tw-$FC,x
        inx
        bne :-
        tax
        bpl rnx
rtk:    ldx #3
:       lda tA,x
        sta tw,x
        dex
        bpl :-
        stx tf                  ; ($FF)
rnx:    tya
        sec
        sbc #32
        tay
        bcs rl
        lda #AUD_SLOT
        ldx tf
        bne rarm
        jmp mach_disarm         ; none playing (a no-op when not armed)
rarm:   ldx #3                  ; (re-arming the same deadline costs at most
:       lda tw,x                ; a kick from the trap path)
        sta mach_at,x
        dex
        bpl :-
        lda #AUD_SLOT
        jmp mach_arm

; late — tA = now - due (u32); N = 1 when not due yet.  Keeps Y.
late:   jsr due
        sec
        ldx #$FC
:       lda mach_et-$FC,x
        sbc tA-$FC,x
        sta tA-$FC,x
        inx
        bne :-
        tax
        rts

; due — tA (u32) = dl's E part + (fraction != 0).  Keeps Y.
due:    lda CH+C_DL,y
        ora CH+C_DL+1,y
        cmp #1                  ; C = 1: a fraction rounds up
        sty sy
        ldx #$FC
:       lda CH+C_DL+2,y
        adc #0
        sta tA-$FC,x
        iny
        inx
        bne :-
        ldy sy
        rts

; --- init: the host's reset (config kept) ------------------------------------
init:   lda cfg_hw
        beq iz
        ldy #96                 ; play zeros on every voice first (DC hold)
:       jsr silence
        tya
        sec
        sbc #32
        tay
        bcs :-
iz:     lda #0
        tax
:       sta VC,x
        inx
        bne :-
        ldx #ST_N - 1           ; the counters (st_nw .. st_npark)
:       sta st_nw,x
        dex
        bpl :-
        lda #15                 ; PAN: 0 for channels 0/3, 15 for 1/2
        sta VC+32+V_PAN
        sta VC+64+V_PAN
        lda #AUD_SLOT           ; (a no-op when not armed)
        jmp mach_disarm

; hwvol — VOL := min(vol, 63) (bit 6 set = 64).  Keeps Y.
hwvol:  lda CH+C_VOL,y
        and #$7F
        cmp #$40
        bcc :+
        lda #63
:       sta VC+V_VOL,y
        rts

.assert * <= SCREEN + $400, error, "aud.s: segment B past the text screen"

.segment "CODE"
; --- scratch (zero in the binary) ---------------------------------------------
tA:     .byte 0, 0, 0, 0, 0, 0
tw:     .byte 0, 0, 0, 0
tN:     .byte 0, 0, 0
mM:     .byte 0, 0, 0, 0, 0, 0
mR:     .byte 0, 0, 0, 0, 0, 0, 0, 0

.assert * <= VC, error, "aud.s: segment A runs into the state page"
