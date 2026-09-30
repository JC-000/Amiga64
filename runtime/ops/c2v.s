; ===========================================================================
; runtime/ops/c2v.s — W7-V planar -> VIC-II converter v0 (docs/S3_PLAN.md
; §2, §4 "S3.5", converter half only).  Owner: W7-V (c2v).  Spec and memory
; map: docs/C2V.md; host reference: host/c2v_ref.py.
;
; Promotes the two kernels measured in tools/s3probe/c2v.s into a callable
; module.  The guest's bitplanes are in the REU; the output is a VIC bank 3
; bitmap (c2v_bitmap, char-cell order: byte for cell cx, line y at
; c2v_bitmap + (y>>3)*320 + cx*8 + (y&7)), screen matrix (c2v_screen) and
; colour RAM (VIC_COLRAM).  Nothing here programs a VIC or CIA register:
; showing the picture is S3.5's display half.
;
;   c2v_mc2    2-plane lores 320 px -> multicolour 160 px, exact colours:
;              MC pixel = (plane1, plane0) of the left lores pixel of each
;              pair = the Amiga colour index 0-3 itself.
;   c2v_hires  4-plane hires 640 px -> hires 320 px silhouette: pixel on
;              iff the OR of the planes is nonzero, 2:1 by OR-ing pixel
;              pairs (c2v_pairhi / c2v_pairlo).
;
; Layout (docs/C2V.md, S3_PLAN §2.4): VIC bank 3, bitmap $E000-$FF3F, screen
; matrix in the RAM under I/O at $D000.  C2V_ROWS (char rows, default 25 =
; 200 lines) and the c2v_bitmap / c2v_screen bases are assembly parameters:
; with fewer than 25 rows the cells of the unconverted rows are filled in
; colour 0 (a blank band), and a screen matrix outside $D000-$DFFF is filled
; without banking.  The kernels take the line count and bases from these
; equates only.  One plane row per plane is fetched per line (REC autoload:
; only the REU address and the C64 page are stored per row), into the
; page-aligned rows the measured kernels read.  REU DMA never targets
; $D000-$DFFF.
;
; Screen fill under I/O (only in c2v_pal_*, i.e. on a palette change, never
; per frame): CPU stores with $01 = %xx100 ($34, all RAM) under sei, the
; caller's $01 restored right after.  NMI is not maskable and with $01=$34
; vectors through RAM $FFFA, so c2v_nmivec (a W7-V interim claim,
; superseded by S3.2's vectors) is pointed at c2v_nmi_rti first, by a store
; under ROM that lands in RAM whatever $01 is, before $01 ever changes.
;
; Entry points (JSR; clobber A/X/Y; call with I/O mapped in and no IRQ that
; touches the REC).  The work RAM at $B900 lies under BASIC ROM, so every
; entry banks BASIC out ($01 %x11 -> %x10: $37 -> $36, I/O and KERNAL stay)
; and restores the caller's $01 on exit:
;   c2v_init       build the pair-OR tables (once; c2v_hires needs them)
;   c2v_pal_mc2    colours 0-3 -> c2v_a_bgc ($D021), screen nibbles, colour RAM
;   c2v_pal_hires  colours 0-1 -> screen nibbles (fg = colour 1, bg = colour 0)
;   c2v_mc2        convert one frame from planes 0-1
;   c2v_hires      convert one frame from planes 0-3
; Arguments (c2v_args block, little-endian; docs/C2V.md):
;   c2v_a_src  4 x u24  REU address of each plane's first converted row;
;                       rows are contiguous (BPLxMOD = 0): 40 B lores, 80 B hires
;   c2v_a_lut  u24      REU address of the 4096-byte OCS-RGB -> VIC LUT
;   c2v_a_pal  16 x u16 OCS colours $0RGB (COLOR00-15; bits 15-12 ignored)
;   c2v_a_bgc  u8 out   VIC colour for $D021 (colour 0)
; Precondition: no plane and no LUT crosses a 512 KB REU line (docs/C2V.md).
; C1 (docs/WAVE4_BRIEF.md): every entry ends with jsr mem_rec_dirty.
;
; Link layout: this module's CODE is padded to a whole number of pages and
; it has no RODATA/DATA/BSS, so every module linked after it moves by whole
; pages only: no hot-path page crossing and no cycle count changes.
;
; Test-only variants (tests/run_c2v.py): C2V_STUB (every entry is rts, the
; red leg), C2V_MUT_BITORDER (planes swapped / pair tables swapped),
; C2V_MUT_LUT (LUT index + 1), C2V_MUT_ROW (source row advance one short),
; C2V_MUT_NMI (the RAM NMI vector is not set before the $01=$34 fill),
; C2V_MUT_NMIACK (the stub does not acknowledge CIA2).
; ===========================================================================

.include "../macros.inc"

.export c2v_init, c2v_pal_mc2, c2v_pal_hires, c2v_mc2, c2v_hires
.export c2v_args, c2v_bitmap, c2v_screen   ; labels for the host (runner.lbl)
.export c2v_nmi_rti, C2V_ROWS
.import mem_rec_dirty

.ifndef C2V_ROWS
C2V_ROWS   = 25                 ; char rows converted (20-25)
.endif
C2V_LINES  = C2V_ROWS * 8       ; output lines (200)
C2V_SPLIT  = C2V_ROWS * 40      ; first blanked screen cell (1000: none)
C2V_SCR_UNDER_IO = (c2v_screen >> 12) = $D ; matrix in the RAM under I/O
REU_CMD_FETCH_AL = REU_CMD_FETCH | $20 ; fetch with autoload

; Argument block and state (inside the rc region c2v_args .. +$FF)
c2v_a_src  = c2v_args + 0       ; 4 x u24 plane REU addresses
c2v_a_lut  = c2v_args + 12      ; u24 LUT REU address
c2v_a_pal  = c2v_args + 16      ; 16 x u16 OCS colours
c2v_a_bgc  = c2v_args + 48      ; u8 out: $D021 colour
c2v_v_col  = c2v_args + 49      ; 4 x u8 looked-up VIC colours
c2v_v_scr  = c2v_args + 53      ; u8 screen byte, converted rows
c2v_v_cr   = c2v_args + 54      ; u8 colour-RAM byte, converted rows
c2v_v_band = c2v_args + 55      ; u8 screen byte, blank rows
c2v_v_pp   = c2v_args + 56      ; 4 x u24 working plane pointers
c2v_v_line = c2v_args + 68      ; u8 output line 0..C2V_LINES-1
c2v_v_port = c2v_args + 69      ; u8 $01 saved around the under-I/O fill
c2v_v_adv  = c2v_args + 70      ; u8 source row advance
c2v_v_tmp  = c2v_args + 71      ; u8 scratch / LUT byte target
c2v_v_nmis = c2v_args + 72      ; u8 NMIs taken by c2v_nmi_rti (wraps)

P0 = c2v_stage
P1 = c2v_stage + $100
P2 = c2v_stage + $200
P3 = c2v_stage + $300

.assert (c2v_pairhi & $FF) = 0 .and (c2v_pairlo & $FF) = 0, error, "c2v pair tables must be page-aligned"
.assert (c2v_stage & $FF) = 0, error, "c2v plane rows must be page-aligned"
.assert C2V_ROWS >= 20 .and C2V_ROWS <= 25, error, "C2V_FILL needs 20-25 rows"
.assert c2v_screen + 1000 <= c2v_nmivec, error, "screen matrix overlaps the NMI vector"
.assert c2v_screen >= c2v_bitmap + C2V_ROWS * 320 .or c2v_screen + 1000 <= c2v_bitmap, error, "bitmap rows overlap the screen matrix"
.assert c2v_bitmap + C2V_ROWS * 320 <= c2v_nmivec, error, "bitmap overlaps the NMI vector"
.assert (c2v_stage >> 12) <> $D .and ((c2v_stage + $3FF) >> 12) <> $D, error, "REU DMA must never target the RAM under I/O"
.assert (c2v_args >> 12) <> $D, error, "REU DMA must never target the RAM under I/O"

.segment "CODE"
c2v_code0:

; C2V_ENTER — bank BASIC ROM out if it is in (LORAM and HIRAM both set),
; the caller's $01 saved on the stack; c2v_leave restores it.
.macro C2V_ENTER
        lda CPU_PORT
        pha
        and #3
        cmp #3
        bne :+
        lda CPU_PORT
        and #$FE
        sta CPU_PORT
:
.endmacro

.ifdef C2V_STUB
c2v_init:
c2v_pal_mc2:
c2v_pal_hires:
c2v_mc2:
c2v_hires:
        rts
c2v_nmi_rti:
        rti
.else

; ---------------------------------------------------------------------------
; c2v_init — c2v_pairlo[b] bit (3-k), k=0..3 = b bit(7-2k) | b bit(6-2k);
; c2v_pairhi[b] = c2v_pairlo[b] << 4.
; ---------------------------------------------------------------------------
c2v_init:
        C2V_ENTER
        ldx #0
@b:     lda #0
        sta c2v_v_tmp
        ldy #4
        txa
@p:     asl                     ; C = pixel 2k
        bcs @on1
        asl                     ; C = pixel 2k+1
        jmp @one
@on1:   asl
        sec
@one:   rol c2v_v_tmp
        dey
        bne @p
        lda c2v_v_tmp
.ifdef C2V_MUT_BITORDER
        sta c2v_pairhi,x        ; mutant: tables swapped
.else
        sta c2v_pairlo,x
.endif
        asl
        asl
        asl
        asl
.ifdef C2V_MUT_BITORDER
        sta c2v_pairlo,x
.else
        sta c2v_pairhi,x
.endif
        inx
        bne @b
        jmp c2v_leave

; ---------------------------------------------------------------------------
; Palette: look up colours in the REU LUT, then fill screen and colour RAM.
; ---------------------------------------------------------------------------
c2v_pal_mc2:
        C2V_ENTER
        ldx #3
        jsr c2v_lookn
        lda c2v_v_col+1         ; %01 -> screen high nibble
        asl
        asl
        asl
        asl
        ora c2v_v_col+2         ; %10 -> screen low nibble
        sta c2v_v_scr
        lda c2v_v_col+3         ; %11 -> colour RAM
        sta c2v_v_cr
        jmp c2v_fill

c2v_pal_hires:
        C2V_ENTER
        ldx #1
        jsr c2v_lookn
        lda c2v_v_col+1         ; set bit -> high nibble (fg = colour 1)
        asl
        asl
        asl
        asl
        ora c2v_v_col+0         ; clear bit -> low nibble (bg = colour 0)
        sta c2v_v_scr
        lda c2v_v_col+0         ; colour RAM unused in hires: background
        sta c2v_v_cr
        ; fall through

; C2V_FILL dst, f0, f1 — cells [0, C2V_SPLIT) of dst = f0, cells
; [C2V_SPLIT, 1000) = f1 (the blank band; empty with 25 rows).
.macro C2V_FILL dst, f0, f1
.local lp1, lp2, keep
        ldx #0
lp1:    lda f0
        sta dst,x
        sta dst+$100,x
        sta dst+$200,x
        inx
        bne lp1
lp2:    lda f0                  ; cells $300-$3E7
        cpx #<(C2V_SPLIT - $300)
        bcc keep
        lda f1
keep:   sta dst+$300,x
        inx
        cpx #<(1000 - $300)
        bne lp2
.endmacro

; c2v_fill — screen matrix = c2v_v_scr, colour RAM = c2v_v_cr on the
; converted rows, colour 0 (both nibbles / colour RAM) on any blank rows;
; c2v_a_bgc = colour 0.  Ends the REC use.
c2v_fill:
        lda c2v_v_col+0
        sta c2v_a_bgc
        asl
        asl
        asl
        asl
        ora c2v_v_col+0
        sta c2v_v_band
.if C2V_SCR_UNDER_IO
.ifndef C2V_MUT_NMI
        lda #<c2v_nmi_rti       ; NMI guard first: a store under ROM lands
        sta c2v_nmivec          ; in RAM, so the RAM vector is valid before
        lda #>c2v_nmi_rti       ; $01 ever exposes it
        sta c2v_nmivec+1
.endif
        php
        sei
        lda CPU_PORT
        sta c2v_v_port
        and #$F8
        ora #$04                ; %xx100 ($34): RAM everywhere, no I/O
        sta CPU_PORT
.endif
        C2V_FILL c2v_screen, c2v_v_scr, c2v_v_band
.if C2V_SCR_UNDER_IO
        lda c2v_v_port          ; restore at once
        sta CPU_PORT
        plp
.endif
        C2V_FILL VIC_COLRAM, c2v_v_cr, c2v_v_col
        ; fall through: the LUT fetches reprogrammed the REC

; c2v_leave — C1 (jsr mem_rec_dirty, clobbers A only) and restore $01.
c2v_leave:
        jsr mem_rec_dirty
        pla
        sta CPU_PORT
        rts

; c2v_nmi_rti — the NMI handler while $01=$34 (c2v_nmivec points here).  The
; NMI is dropped but its source acknowledged, not delayed: with I/O mapped
; back in for three instructions, a read of the CIA2 ICR releases /NMI, so
; the next NMI edge (CIA2 or RESTORE) is delivered normally.  Counts the
; NMIs taken in c2v_v_nmis (docs/C2V.md "Handover to S3.2").
c2v_nmi_rti:
        pha
        inc c2v_v_nmis
.ifndef C2V_MUT_NMIACK
        lda CPU_PORT
        pha
        lda c2v_v_port          ; the pre-fill $01: I/O in
        sta CPU_PORT
        lda CIA2_ICR            ; acknowledge CIA2
        pla
        sta CPU_PORT
.endif
        pla
        rti

; c2v_lookn — X = highest colour index: c2v_v_col[0..X] = LUT[pal[i] & $FFF]
; (one byte DMA each).
c2v_lookn:
        lda #<c2v_v_tmp
        sta REC_C64LO
        lda #>c2v_v_tmp
        sta REC_C64HI
        lda #0
        sta REC_LENHI
        sta REC_ACTRL           ; both addresses increment
@lp:    txa
        asl
        tay                     ; Y = 2 * index
.ifdef C2V_MUT_LUT
        sec                     ; mutant: LUT index off by one
.else
        clc
.endif
        lda c2v_a_pal,y
        adc c2v_a_lut
        sta REC_REULO
        lda c2v_a_pal+1,y
        and #$0F
        adc c2v_a_lut+1
        sta REC_REUHI
        lda c2v_a_lut+2
        adc #0
        sta REC_REUBANK
        lda #1
        sta REC_LENLO
        lda #REU_CMD_FETCH_AL   ; autoload: C64 address back at c2v_v_tmp
        sta REC_COMMAND
        lda c2v_v_tmp
        sta c2v_v_col,x
        dex
        bpl @lp
        rts

; ---------------------------------------------------------------------------
; Frame setup: X = row width in bytes.  Copies the plane pointers, programs
; C64LO = 0, the length and the address control once (the fetches use
; autoload, so those registers reload after every row).
; ---------------------------------------------------------------------------
c2v_setup:
        stx REC_LENLO
.ifdef C2V_MUT_ROW
        dex                     ; mutant: rows advance one byte short
.endif
        stx c2v_v_adv
        lda #0
        sta REC_LENHI
        sta REC_C64LO
        sta REC_ACTRL
        sta c2v_v_line
        ldx #11
@cp:    lda c2v_a_src,x
        sta c2v_v_pp,x
        dex
        bpl @cp
        rts

; C2V_FETCH n — fetch the next row of planes 0..n-1 into P0.. (one page
; each) and advance each plane pointer by c2v_v_adv.  Unrolled, inline: it
; runs once per line.  C2V_PERF_NOFETCH (perf variant only) leaves it out
; so tests/run_c2v.py --perf can separate the kernel from the fetch.
.macro C2V_FETCH n
.ifndef C2V_PERF_NOFETCH
.repeat n, p
        lda #>(P0 + p * $100)
        sta REC_C64HI
        clc
        lda c2v_v_pp + p * 3
        sta REC_REULO
        adc c2v_v_adv
        sta c2v_v_pp + p * 3
        lda c2v_v_pp + p * 3 + 1
        sta REC_REUHI
        adc #0
        sta c2v_v_pp + p * 3 + 1
        lda c2v_v_pp + p * 3 + 2
        sta REC_REUBANK
        adc #0
        sta c2v_v_pp + p * 3 + 2
        lda #REU_CMD_FETCH_AL
        sta REC_COMMAND
.endrepeat
.endif
.endmacro

; c2v_setln — patch the two output operands for line c2v_v_line:
; c2v_bitmap + (line>>3)*320 + (line&7) in A (lo) / X (hi).
c2v_setln:
        lda c2v_v_line
        lsr
        lsr
        lsr
        tay                     ; char row
        lda c2v_v_line
        and #7
        clc
        adc c2v_rowlo,y
        ldx c2v_rowhi,y
        bcc :+
        inx
:       rts

; ---------------------------------------------------------------------------
; c2v_hires — 4-plane hires 640 -> 320 hires silhouette, one frame.
; ---------------------------------------------------------------------------
c2v_hires:
        C2V_ENTER
        ldx #80
        jsr c2v_setup
@ln:    C2V_FETCH 4
        jsr c2v_setln
        sta @o1+1
        sta @o2+1
        stx @o1+2
        inx
        stx @o2+2
        ldx #0                  ; output offset (cell*8), cells 0-31
        ldy #0                  ; input byte index
@l1:    lda P0,y
        ora P1,y
        ora P2,y
        ora P3,y
        sta @h1+1
        lda P0+1,y
        ora P1+1,y
        ora P2+1,y
        ora P3+1,y
        sta @q1+1
@q1:    lda c2v_pairlo          ; operand low byte patched
@h1:    ora c2v_pairhi
@o1:    sta c2v_bitmap,x
        iny
        iny
        txa
        clc
        adc #8
        tax
        bne @l1                 ; x wraps to 0 after 32 cells (y = 64)
@l2:    lda P0,y                ; cells 32-39 at page +1
        ora P1,y
        ora P2,y
        ora P3,y
        sta @h2+1
        lda P0+1,y
        ora P1+1,y
        ora P2+1,y
        ora P3+1,y
        sta @q2+1
@q2:    lda c2v_pairlo
@h2:    ora c2v_pairhi
@o2:    sta c2v_bitmap+256,x
        txa
        clc
        adc #8
        tax
        iny
        iny
        cpy #80
        bne @l2
        inc c2v_v_line
        lda c2v_v_line
        cmp #C2V_LINES
        beq @done
        jmp @ln
@done:  jmp c2v_leave           ; C1

; ---------------------------------------------------------------------------
; c2v_mc2 — 2-plane lores 320 -> 160 multicolour, one frame.
; ---------------------------------------------------------------------------
.ifdef C2V_MUT_BITORDER
MC_LO = P1                      ; mutant: planes swapped
MC_HI = P0
.else
MC_LO = P0                      ; plane 0 -> MC bit 0 of each pixel
MC_HI = P1                      ; plane 1 -> MC bit 1
.endif
c2v_mc2:
        C2V_ENTER
        ldx #40
        jsr c2v_setup
@ln:    C2V_FETCH 2
        jsr c2v_setln
        sta @o1+1
        sta @o2+1
        stx @o1+2
        inx
        stx @o2+2
        ldx #0
        ldy #0
@l1:    lda MC_LO,y
        and #$AA                ; left pixel of each pair
        lsr                     ; -> bit positions 6,4,2,0
        sta @t1+1
        lda MC_HI,y
        and #$AA                ; -> bit positions 7,5,3,1
@t1:    ora #0                  ; immediate patched
@o1:    sta c2v_bitmap,x
        iny
        txa
        clc
        adc #8
        tax
        bne @l1                 ; x wraps after 32 cells (y = 32)
@l2:    lda MC_LO,y
        and #$AA
        lsr
        sta @t2+1
        lda MC_HI,y
        and #$AA
@t2:    ora #0
@o2:    sta c2v_bitmap+256,x
        txa
        clc
        adc #8
        tax
        iny
        cpy #40
        bne @l2
        inc c2v_v_line
        lda c2v_v_line
        cmp #C2V_LINES
        beq @done
        jmp @ln
@done:  jmp c2v_leave           ; C1

c2v_rowlo:
.repeat C2V_ROWS, i
        .byte <(c2v_bitmap + i * 320)
.endrepeat
c2v_rowhi:
.repeat C2V_ROWS, i
        .byte >(c2v_bitmap + i * 320)
.endrepeat
.endif

; Pad to whole pages: modules linked after this one keep their page offsets.
c2v_code1:
        .res (256 - ((c2v_code1 - c2v_code0) .mod 256)) .mod 256, $EA
