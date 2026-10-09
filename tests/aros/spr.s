; ---------------------------------------------------------------------------
; tests/aros/spr.s — W13-D D2: the Amiga pointer (sprite 0) as VIC sprites,
; a machine-mode module (docs/WAVE13_BRIEF.md W13-D D2; docs/CONTRACTS.md
; "Display refresh (S3.5)", "Pointer sprite (W13-D)").
;
; NOT linked into the runner PRG: tests/run_spr.py build_spr() assembles it
; at test time to a raw binary at SPR_ORG ($C600, end <= $C8FF; the
; machine-mode area: mboot $C000-$C1FF, W13-W's td $C200-$C5FF) with the
; runner's labels as -D defines, as tests/aros/mboot.s.  It writes nothing
; in $C900-$C96F (mboot's P/R), $CA00-$CAFF, $CB00+, $CE00 or $CFFF.
;
; Entries:
;   spr_arm    (JSR, host or loader) spr_vec := spr_frame (high byte last),
;              spr_id := SPR_ID.  disp_frame then calls spr_frame through
;              spr_vec once per frame hook (disp_vw, when F1 is not pressed)
;   spr_frame  (from disp_vw: BASIC out, I/O in; clobbers A/X/Y) the
;              pointer; returns C = 1 when it moved the 1:1 window (disp_px,
;              disp_py), so disp_frame converts now
;
; The pointer: inside mach_run only (the RAM NMI vector holds mach_nmi
; there).  Shown while a picture is (disp_mode mc2 or hires), DMACON has
; DMAEN and SPREN, and VSTOP > VSTART.  Each call: SPR0PT from chip_shd
; ($120/$122, big-endian words, bit 0 ignored), its POS/CTL (4 bytes, one
; REU fetch).  The image (21 lines at most, DATA/DATB words) and the
; colours (COLOR17-19 through the LUT) are fetched again only when SPR0PT,
; POS or CTL changed, or disp_nconv moved (a conversion: the palette may
; have changed; after a resume the module is staged fresh, all zero:
; invalid).  VIC sprites 0/1/2 show colour 17/18/19: sprite 0 = DATA &
; ~DATB, 1 = ~DATA & DATB, 2 = DATA & DATB (exclusive masks: exact colours
; at full width), blocks at disp_sprb ($D400/$D440/$D480), pointers at
; c2v_screen + $3F8, written at $01 = %xx100 under sei.
;
; Positions (lores pixels; HRM: the playfield's first fetched pixel is at
; hcount 2 * DDFSTRT + 9 hires (+ 17 lores), and a sprite at HSTART shows
; with the playfield pixel of hcount HSTART + 1):
;   SX = HSTART + 1 - (2 * (DDFSTRT & mask) + 9 / 17), SY = VSTART - DIWSTRT V
;   fit:  VIC X = 24 + SX,              VIC Y = 50 + SY - disp_crop
;   1:1:  VIC X = 24 + 2 * (SX - 4 * disp_px) (X-expanded),
;         VIC Y = 50 + SY - disp_crop; LACE: 50 + 2 * (SY - disp_crop)
;         (Y-expanded: the sprite's lines are field lines)
; Off the VIC's range (X 0-$1F7, Y 0-255): hidden.
; Pan (1:1): the window (lores 4 * px .. + 160, field lines crop .. + 200,
; LACE + 100) re-centres on SX / SY when the pointer comes within 32 C64
; pixels (16 lores; 32 lines, LACE 16) of an edge: px := clamp((SX - 80)
; >> 2, 0, 40), py := clamp(SY - 100 (LACE 50), 0, disp_ymax); C = 1 only
; if px or the window's line changed (no conversion at a clamped edge).
;
; Mutants (tests/run_spr.py --mutants): SPR_MUT_MASK (sprite 2 = DATA),
; SPR_MUT_PFH (DIWSTRT H instead of DDFSTRT), SPR_MUT_CACHE (the image
; never re-read), SPR_MUT_PAN (no pan).
; ---------------------------------------------------------------------------

.include "../../runtime/rc/zp.inc"
.include "../../runtime/rc/ram.inc"
.include "../../runtime/rc/machine.inc"

SPR_ID     = $31525053          ; @value "SPR1" (LE): the module's ID in spr_id
S_DMACON   = 1                  ; @value disp_st: DMACON (LE)
S_COLOR17  = 39 + 34            ; @value disp_st: COLOR17 (LE)
C_BPLCON0  = 2                  ; @value disp_cur: BPLCON0 (LE)
C_DIWSTRT  = 30                 ; @value disp_cur: DIWSTRT (LE)
C_DDFSTRT  = 34                 ; @value disp_cur: DDFSTRT (LE)
M_MC2      = 1                  ; @value disp_mode: mc2
M_HIRES    = 2                  ; @value disp_mode: hires
SPR0PT     = $120               ; @value chip_shd: SPR0PTH
LACE       = $04                ; @value BPLCON0 low byte: LACE
SIMG       = c2v_stage + $3A8   ; 84 B: the image as fetched (21 x DATA, DATB)
SBLK       = ((disp_sprb >> 6) & 255)   ; VIC block of sprite 0 (bank-relative: 256 blocks per 16 KB)
SPTR       = c2v_screen + $3F8  ; VIC sprite pointers 0-2
FETCH      = $91                ; @value REC: execute, REU -> C64

.segment "CODE"
spr_entry:
        jmp spr_frame           ; $C600: the frame entry (spr_vec)
spr_idv:
        .dword SPR_ID           ; $C603: the ID a resume compares

; ---------------------------------------------------------------------------
; State (c2v_args + 180 .., RAM under BASIC: not in the snapshot image; the
; module's own sval / son say whether it is valid: zero after staging).
ST    = c2v_args + 180
snew  = ST + 0          ; 7: SPR0PT (lo, hi, bank), POS, CTL as fetched
sold  = ST + 7          ; 7: the same, of the image in the blocks
snc   = ST + 14         ; 2: disp_nconv when the image was read
O_SY  = 16              ; @value s16 VSTART - DIWSTRT V
O_SX  = 18              ; @value s16 lores x from the playfield's first pixel
sh    = ST + 20         ; image lines, 1-21
lc    = ST + 21         ; 1: the 1:1 view with LACE
O_WY  = 22              ; @value s16 the window's line of the pointer
O_WX  = 24              ; @value s16 its lores x in the window
npx   = ST + 28
npy   = ST + 29
tmp   = ST + 30
tmp2  = ST + 31
.assert ST + 32 <= c2v_args + $100, error, "spr: state past c2v_args"

spr_frame:
        lda mach_in
        beq @no
        ldx disp_mode
        dex
        cpx #2
        bcs @hid                ; no picture shown (mc2 / hires only)
        lda disp_st + S_DMACON
        and #$20                ; SPREN
        beq @hid
        lda disp_st + S_DMACON + 1
        and #$02                ; DMAEN
        bne @on
@hid:   lda son
        beq @no                 ; hidden already: no VIC write
        lda #0
        sta son
        sta VIC_SPREN
@no:    clc
        rts
@on:    lda chip_shd + SPR0PT + 3
        and #$FE                ; word DMA: bit 0 ignored
        sta snew
        sta REC_REULO
        lda chip_shd + SPR0PT + 2
        sta snew + 1
        sta REC_REUHI
        lda chip_shd + SPR0PT + 1
        sta snew + 2
        sta REC_REUBANK
        lda #>(snew + 3)
        ldy #<(snew + 3)
        ldx #4
        jsr spr_rf              ; POS, CTL (the REC's REU address: PT + 4)
        jsr mem_rec_dirty       ; C1
        lda snew + 6            ; CTL: V8 start (bit 2), V8 stop (bit 1), H0
        lsr                     ; C = H0
        lda snew + 4
        rol                     ; HSTART bits 7-0, C = bit 8
        sta ST + O_SX
        lda #0
        rol
        sta ST + O_SX + 1
        lda snew + 6
        and #$04
        lsr
        lsr
        sta ST + O_SY + 1       ; VSTART bit 8
        lda snew + 6
        lsr
        and #1
        tax                     ; VSTOP bit 8
        sec                     ; height = VSTOP - VSTART: 1-21
        lda snew + 5
        sbc snew + 3
        sta sh
        txa
        sbc ST + O_SY + 1
        bmi @hid
        bne @h21
        lda sh
        beq @hid
        cmp #22
        bcc :+
@h21:   lda #21
:       sta sh
        lda snew + 3
        sta ST + O_SY
        lda disp_cur + C_DIWSTRT + 1
        ldy #O_SY
        jsr spr_sb              ; SY = VSTART - DIWSTRT V
.ifdef SPR_MUT_PFH
        ldy #O_SX
        lda disp_cur + C_DIWSTRT ; mutant: DIWSTRT H - 1 as the playfield start
        sec
        sbc #1
        jsr spr_sb
.else
        ldx #8                  ; SX = HSTART + 1 - (2 * (DDFSTRT & mask) + c):
        lda #$FC                ; hires c = 9, mask $FC; lores 17, $F8
        ldy disp_mode
        cpy #M_HIRES
        beq :+
        ldx #16
        lda #$F8
:       ldy #O_SX
        and disp_cur + C_DDFSTRT
        pha
        txa
        jsr spr_sb              ; - (c - 1)
        pla
        pha
        jsr spr_sb              ; - DDF
        pla
        jsr spr_sb              ; - DDF
.endif
        ldx #6                  ; the image: SPR0PT, POS or CTL changed,
:       lda snew,x              ; or a conversion since (the palette)?
        cmp sold,x
.ifdef SPR_MUT_CACHE
        bne @pos                ; mutant: a change never re-reads the image
.else
        bne @img
.endif
        dex
        bpl :-
        lda disp_nconv          ; (low byte: 256 conversions alias, harmless)
        cmp snc
.ifdef SPR_MUT_CACHE
        bne @pos
.else
        bne @img
.endif
        lda sval
        bne @pos
@img:   jsr spr_img
@pos:   ldx #3                  ; WY = SY - crop, WX = SX - 4 * px (1:1)
:       lda ST + O_SY,x
        sta ST + O_WY,x
        dex
        bpl :-
        lda disp_crop
        ldy #O_WY
        jsr spr_sb
        lda #0
        sta lc
        lda disp_one
        bne :+
        jmp @vic                ; fit: no pan
:       lda disp_cur + C_BPLCON0
        and #LACE
        beq :+
        inc lc
:       lda disp_px
        asl
        asl
        ldy #O_WX
        jsr spr_sb
        lda disp_px
        sta npx
        lda disp_crop
        sta npy
.ifndef SPR_MUT_PAN
        ldx #16                 ; x: re-centre if WX is off [16, 144)
        lda #144
        ldy #O_WX
        jsr spr_edge
        bcc :+
        lda #80                 ; px := clamp(SX - 80, 0, 160) / 4
        ldx #160
        ldy #O_SX
        jsr spr_rc
        lsr
        lsr
        sta npx
:       ldx #32                 ; y: off [32, 168) (LACE: [16, 84))
        lda #168
        ldy lc
        beq :+
        ldx #16
        lda #84
:       ldy #O_WY
        jsr spr_edge
        bcc :++
        lda #100                ; py := clamp(SY - 100 (LACE 50), 0, ymax)
        ldy lc
        beq :+
        lda #50
:       ldx disp_ymax
        ldy #O_SY
        jsr spr_rc
        sta npy
:
.endif
        ldx npx
        lda npy
        cpx disp_px
        bne @pan
        cmp disp_crop
        beq @vic
@pan:   stx disp_px
        sta disp_py
        sec
        rts
@vic:   lda lc                  ; VIC Y = 50 + WY (1:1 LACE: 2 * WY)
        beq :+
        asl ST + O_WY
        rol ST + O_WY + 1
:       clc
        lda ST + O_WY
        adc #50
        tay
        lda ST + O_WY + 1
        adc #0
        bne @hidj               ; off the VIC's 0-255
        lda disp_one
        beq :+
        asl ST + O_WX           ; 1:1: two C64 pixels per lores pixel
        rol ST + O_WX + 1
:       clc                     ; VIC X = 24 + WX
        lda ST + O_WX
        adc #24
        tax
        lda ST + O_WX + 1
        adc #0
        bmi @hidj
        cmp #2
        bcs @hidj
        lsr                     ; C = X bit 8 (A = 0)
        bcc :+
        lda #7                  ; X bit 8 of sprites 0-2
:       sta VIC_SPRMSB
        stx VIC_SPR0X
        stx VIC_SPR0X + 2
        stx VIC_SPR0X + 4
        sty VIC_SPR0X + 1
        sty VIC_SPR0X + 3
        sty VIC_SPR0X + 5
        lda disp_one
        beq :+
        lda #7
:       sta VIC_SPRXX           ; 1:1: X-expanded
        lda lc
        beq :+
        lda #7
:       sta VIC_SPRYX           ; 1:1 LACE: Y-expanded
        lda #7
        sta VIC_SPREN
        sta son
        clc
        rts
@hidj:  jmp @hid

; spr_sb — the s16 at ST + Y -= A (unsigned).  Keeps Y.
spr_sb:
        sta tmp
        sec
        lda ST,y
        sbc tmp
        sta ST,y
        bcs :+
        lda ST + 1,y
        sbc #0
        sta ST + 1,y
:       rts

; spr_edge — C = 1 if the s16 at ST + Y is off [X, A).
spr_edge:
        sta tmp
        stx tmp2
        lda ST + 1,y
        bne :+
        lda ST,y
        cmp tmp2
        bcc :+
        cmp tmp
        rts
:       sec
        rts

; spr_rc — A := clamp(s16 at ST + Y - A, 0, X); the s16 is consumed.
spr_rc:
        stx tmp2
        jsr spr_sb
        lda ST + 1,y
        bmi @z
        bne @m
        lda ST,y
        cmp tmp2
        bcc @r
@m:     lda tmp2
        rts
@z:     lda #0
@r:     rts

; spr_img — the image: 4 * sh bytes from PT + 4 (the REC's address after
; spr_frame's fetch), the rest of the 21 lines 0; the colours (LUT[COLOR17
; .. 19], one byte DMA each into $D027-$D029); then at $01 = %xx100 under
; sei the three blocks and the pointers.
spr_img:
        ldx #6
:       lda snew,x
        sta sold,x
        dex
        bpl :-
        stx sval                ; $FF: valid
        lda disp_nconv
        sta snc
        lda #>SIMG
        ldy #<SIMG
        ldx #84
        jsr spr_rf              ; 21 lines (the height's first 4 * sh bytes used)
        ldx #4                  ; COLOR19, 18, 17
@k:     lda disp_st + S_COLOR17,x
        sta REC_REULO
        lda disp_st + S_COLOR17 + 1,x
        and #$0F
        ora #>disp_lut          ; (disp_lut: a 64 KB bank's start)
        sta REC_REUHI
        lda #^disp_lut
        sta REC_REUBANK
        txa
        pha
        lsr
        adc #<VIC_SPRCOL        ; (C = 0)
        tay
        lda #>VIC_SPRCOL
        ldx #1
        jsr spr_rf
        pla
        tax
        dex
        dex
        bpl @k
        lda sh
        asl
        asl
        sta tmp                 ; 4 * height: the bytes converted
        php
        sei
        lda CPU_PORT
        pha
        and #$F8
        ora #$04                ; %xx100: all RAM (mach_run: the RAM NMI
        sta CPU_PORT            ; vector holds mach_nmi)
        lda #0                  ; the three blocks := 0
        ldx #192
:       sta disp_sprb - 1,x
        dex
        bne :-
        ldy #0
@l:     lda SIMG,y              ; DATA & DATB: colour 19
.ifndef SPR_MUT_MASK
        and SIMG + 2,y          ; (mutant: sprite 2 = DATA)
.endif
        sta disp_sprb + 128,x
        eor SIMG,y              ; DATA only: colour 17
        sta disp_sprb,x
        lda disp_sprb + 128,x
        eor SIMG + 2,y          ; DATB only: colour 18
        sta disp_sprb + 64,x
        inx
        iny
        tya
        and #1
        bne @l                  ; the line's second byte
        inx                     ; (third byte: 0)
        iny
        iny
        cpy tmp
        bne @l
        ldx #2
:       txa
        ora #SBLK
        sta SPTR,x
        dex
        bpl :-
        pla
        sta CPU_PORT
        plp
        lda #0
        sta VIC_SPRMC           ; hires, in front
        sta VIC_SPRPRI
        rts                     ; (C1: spr_frame dirtied the REC already)

; spr_rf — REU fetch of X bytes to A:Y (the REU address as set).
spr_rf:
        sta REC_C64HI
        sty REC_C64LO
        stx REC_LENLO
        lda #0
        sta REC_LENHI
        sta REC_ACTRL
        lda #FETCH
        sta REC_COMMAND
        rts

; --- the module's own state: zero in the binary (invalid after staging) ---
sval:   .res 1                  ; nonzero: the blocks hold sold's image
son:    .res 1                  ; nonzero: VIC sprites 0-2 enabled
spr_end:

.assert spr_entry = buf_case + $600, error, "spr: org buf_case + 600 (supervisor's machine-mode area)"
.assert spr_end <= buf_result + $100, error, "spr: must end below mboot's P/R (buf_result + 100)"
.assert (disp_sprb >> 14) = (c2v_screen >> 14), error, "sprite blocks must be in the VIC bank"
.assert (disp_sprb & $3F) = 0, error, "sprite blocks must be 64-byte aligned"
.assert (disp_lut & $FFFF) = 0, error, "spr_img's LUT ora needs disp_lut at a 64 KB bank's start"
.assert (SBLK & 3) = 0, error, "spr: the pointer loop ORs 0-2 into SBLK"
.assert (SIMG >> 12) <> (c2v_screen >> 12), error, "REU DMA never targets the RAM under I/O"
