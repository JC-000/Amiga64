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
; vectors through RAM $FFFA, so mach_vnmi (S3.2's RAM NMI vector, rc s3
; $FFFA-$FFFF) is pointed at mach_nmi, the one NMI handler (valid under any
; $01), before $01 ever changes.  The store keeps the vector invariant
; (docs/CONTRACTS.md "Machine loop"): outside mach_run the caller's $01 has
; the ROM vectors live; inside it ($01 = $35) the RAM vector already holds
; mach_nmi and the store rewrites identical bytes, so an NMI between the
; two byte stores can never take a torn vector.
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
;   c2v_lookn      (W12-V disp; BASIC out, ends no REC use) X = highest
;                  pen: c2v_v_col[0..X] = LUT[pal & $FFF]
;   c2v_setmc2     (W12-V disp) c2v_pal_mc2 with c2v_v_col[0..3] already set
;   c2v_hfill      (W12-V disp) matrix from the cell pens in colour RAM
;   c2v_hires also (W12-V) leaves each cell's pen in colour RAM and the
;                  cells per pen in c2v_v_cnt (c2v_pick, c2v_hrow)
;   c2v_ones       (W13-D disp; BASIC out) the 1:1 view's counting pass:
;                  bitmap, per-cell bg and fg, c2v_v_cnt
;   c2v_ofin       (W13-D disp; BASIC out) its second pass, after the colour
;                  choice: bitmap again and the matrix from c2v_v_col
;   c2v_mc2s, c2v_hiress  (W10-V disp) the same, but row n+1 of plane p
;                  starts width + c2v_a_skip[p] bytes after row n (BPLxMOD;
;                  docs/C2V.md "Row skip"); the two entries above zero
;                  c2v_a_skip first
; Arguments (c2v_args block, little-endian; docs/C2V.md):
;   c2v_a_src  4 x u24  REU address of each plane's first converted row;
;                       rows are contiguous (BPLxMOD = 0): 40 B lores, 80 B hires
;   c2v_a_lut  u24      REU address of the 4096-byte OCS-RGB -> VIC LUT
;   c2v_a_pal  16 x u16 OCS colours $0RGB (COLOR00-15; bits 15-12 ignored)
;   c2v_a_bgc  u8 out   VIC colour for $D021 (colour 0)
;   c2v_a_skip 4 x s16  (+72, W10-V) bytes skipped after each row of plane p
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
; C2V_MUT_NMI (the RAM NMI vector is not set before the $01=$34 fill);
; W12-V: C2V_MUT_PICKR (the rightmost pixel's pen), C2V_MUT_HROW (pens not
; cleared between char rows), C2V_MUT_HFILLBG (matrix low nibble = the pen's).
; W13-D (1:1): C2V_MUT_OMASK (bg from the second pixel), C2V_MUT_OFIN (the
; fg pen in both nibbles).
; (tests/run_c2v.py's nmiack variant is MACH_MUT_NMIACK in runtime/ops/mach.s.)
; ===========================================================================

.include "../macros.inc"

.export c2v_init, c2v_pal_mc2, c2v_pal_hires, c2v_mc2, c2v_hires
.export c2v_mc2s, c2v_hiress                ; W10-V disp: per-plane row skip
.export c2v_lookn, c2v_setmc2, c2v_hfill    ; W12-V disp: the colour choice
.export c2v_v_col, c2v_v_cnt, c2v_v_tab     ; W12-V
.export c2v_ones, c2v_ofin                   ; W13-D disp: the 1:1 view
.export c2v_args, c2v_bitmap, c2v_screen   ; labels for the host (runner.lbl)
.export C2V_ROWS
.import mem_rec_dirty, mach_nmi
.import disp_ozero              ; W13-D (disp.s BHI): lines past c2v_v_olim read 0

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
; (+49..+52 unused since W12-V: were the 4 looked-up colours, now c2v_v_col)
c2v_v_scr  = c2v_args + 53      ; u8 screen byte, converted rows
c2v_v_cr   = c2v_args + 54      ; u8 colour-RAM byte, converted rows
c2v_v_band = c2v_args + 55      ; u8 screen byte, blank rows
c2v_v_pp   = c2v_args + 56      ; 4 x u24 working plane pointers
c2v_v_line = c2v_args + 68      ; u8 output line 0..C2V_LINES-1
c2v_v_port = c2v_args + 69      ; u8 $01 saved around the under-I/O fill
; (+70 unused since W10-V: was the u8 row advance, now c2v_v_adl..adb)
c2v_v_tmp  = c2v_args + 71      ; u8 scratch / LUT byte target
c2v_a_skip = c2v_args + 72      ; W10-V disp: 4 x s16 bytes skipped after each
                                ; converted row (BPLxMOD + DDF width - row);
                                ; zeroed by c2v_mc2 / c2v_hires
c2v_v_adl  = c2v_args + 80      ; W10-V disp: 4 x u8 row advance, bits 7-0
c2v_v_adh  = c2v_args + 84      ; 4 x u8 bits 15-8
c2v_v_adb  = c2v_args + 88      ; 4 x u8 bits 23-16 (sign extension)
c2v_v_col  = c2v_args + 92      ; W12-V: 16 x u8 VIC colour per pen (c2v_lookn:
                                ; LUT; disp_rule rewrites the pens in use)
c2v_v_cnt  = c2v_args + 108     ; W12-V: 16 x u8 hires cells per pen (sat. 255)
c2v_v_tab  = c2v_args + 124     ; W12-V: 16 x u8 c2v_hfill's matrix byte per pen
c2v_v_py   = c2v_args + 140     ; W12-V: c2v_pick's Y
c2v_v_pm   = c2v_args + 141     ; W12-V: c2v_pick's pixel mask
c2v_v_pen  = c2v_args + 142     ; W12-V: c2v_pick's pen
c2v_v_px   = c2v_args + 143     ; W12-V: c2v_pick's X

P0 = c2v_stage
P1 = c2v_stage + $100
P2 = c2v_stage + $200
P3 = c2v_stage + $300
C2V_CP = c2v_stage + $80        ; W12-V: pen of cell cx of the char row being
                                ; converted at C2V_CP + 2cx (0 = none yet);
                                ; P0's bytes past the 80-byte row

.assert (c2v_pairhi & $FF) = 0 .and (c2v_pairlo & $FF) = 0, error, "c2v pair tables must be page-aligned"
.assert (c2v_stage & $FF) = 0, error, "c2v plane rows must be page-aligned"
.assert C2V_ROWS >= 20 .and C2V_ROWS <= 25, error, "C2V_FILL needs 20-25 rows"
.assert c2v_screen + 1000 <= mach_vnmi, error, "screen matrix overlaps the NMI vector"
.assert c2v_screen >= c2v_bitmap + C2V_ROWS * 320 .or c2v_screen + 1000 <= c2v_bitmap, error, "bitmap rows overlap the screen matrix"
.assert c2v_bitmap + C2V_ROWS * 320 <= mach_vnmi, error, "bitmap overlaps the NMI vector"
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
c2v_mc2s:
c2v_hiress:
c2v_lookn:                      ; W12-V
c2v_setmc2:                     ; W12-V
c2v_hfill:                      ; W12-V
c2v_ones:                       ; W13-D
c2v_ofin:                       ; W13-D
        rts
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
        jmp c2v_mc2col
; c2v_setmc2 — W12-V (disp): c2v_pal_mc2 with c2v_v_col[0..3] already set.
c2v_setmc2:
        C2V_ENTER
c2v_mc2col:
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
        lda #<mach_nmi          ; NMI guard first: a store under ROM lands
        sta mach_vnmi           ; in RAM (identical bytes inside mach_run),
        lda #>mach_nmi          ; so the RAM vector is valid before $01 ever
        sta mach_vnmi+1         ; exposes it
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

; c2v_hfill — W12-V (disp, after c2v_hiress and the colour choice): matrix
; cell i := c2v_v_tab[pen i] = c2v_v_col[pen] << 4 | c2v_v_col[0], the pen
; read from colour RAM (c2v_hrow); the cells of any blank rows take pen 0
; (their colour RAM := 0 first).  c2v_a_bgc := c2v_v_col[0].  Per page of
; 256 cells: the bytes are mapped into C2V_HBUF with I/O in, then stored to
; the matrix; under I/O that store loop runs at $01 = %xx100 with IRQs
; masked (~3.6k cycles a page), the NMI guard set first as in c2v_fill.
C2V_HBUF = c2v_stage + $100     ; W12-V: P1's page, free after the kernel
c2v_hfill:
        C2V_ENTER
        ldx #15
:       lda c2v_v_col,x
        asl
        asl
        asl
        asl
.ifdef C2V_MUT_HFILLBG
        ora c2v_v_col,x         ; mutant: the background nibble = the pen's
.else
        ora c2v_v_col
.endif
        sta c2v_v_tab,x
        dex
        bpl :-
        lda c2v_v_col
        sta c2v_a_bgc
.if C2V_SPLIT < 1000
        lda #0                  ; blank rows: pen 0
        ldx #<(C2V_SPLIT - $300)
:       sta VIC_COLRAM+$300,x
        inx
        cpx #<(1000 - $300)
        bne :-
.endif
        lda #>VIC_COLRAM
        sta @rd+2
        lda #>c2v_screen
        sta @wr+2
.if C2V_SCR_UNDER_IO
.ifndef C2V_MUT_NMI
        lda #<mach_nmi          ; NMI guard first (c2v_fill)
        sta mach_vnmi
        lda #>mach_nmi
        sta mach_vnmi+1
.endif
.endif
@pg:    ldx #0                  ; map one page (256 cells; past cell 999 the
@rd:    lda VIC_COLRAM,x        ; operand high byte patched; colour RAM's
        and #$0F                ; spare bytes, never stored)
        tay
        lda c2v_v_tab,y
        sta C2V_HBUF,x
        inx
        bne @rd
        lda @wr+2               ; cells to store: 256, or 232 on the last page
        cmp #>(c2v_screen + $300)
        bne :+
        lda #<1000
        .byte $2C               ; bit abs: skip the lda #0
:       lda #0
        sta c2v_v_pm
.if C2V_SCR_UNDER_IO
        php
        sei
        lda CPU_PORT
        sta c2v_v_port
        and #$F8
        ora #$04                ; %xx100 ($34): RAM everywhere, no I/O
        sta CPU_PORT
.endif
@st:    lda C2V_HBUF,x          ; X = 0
@wr:    sta c2v_screen,x        ; operand high byte patched
        inx
        cpx c2v_v_pm
        bne @st
.if C2V_SCR_UNDER_IO
        lda c2v_v_port          ; restore at once
        sta CPU_PORT
        plp
.endif
        inc @rd+2
        inc @wr+2
        lda c2v_v_pm
        beq @pg                 ; a full page; else the last cell is stored
        jmp c2v_leave

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
        stx c2v_v_tmp
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
        ldx #3                  ; W10-V: advance[p] = width + skip[p], s24
@ad:    txa
        asl
        tay                     ; Y = 2 * plane
        clc
        lda c2v_v_tmp
        adc c2v_a_skip,y
        sta c2v_v_adl,x
        lda c2v_a_skip+1,y
        pha
        adc #0
        sta c2v_v_adh,x
        pla                     ; sign of skip, then the carry out of hi
        and #$80
        beq :+
        lda #$FF
:       adc #0
        sta c2v_v_adb,x
        dex
        bpl @ad
        rts

; c2v_noskip — c2v_a_skip := 0 (contiguous rows: the W7-V entries).  The
; block is under BASIC ROM; a store lands in RAM whatever $01 is.
c2v_noskip:
        lda #0
        ldx #7
:       sta c2v_a_skip,x
        dex
        bpl :-
        rts

; C2V_FETCH n — fetch the next row of planes 0..n-1 into P0.. (one page
; each) and advance each plane pointer by its c2v_v_adl/adh/adb (W10-V:
; row width + c2v_a_skip[p], sign-extended).  Unrolled, inline: it
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
        adc c2v_v_adl + p
        sta c2v_v_pp + p * 3
        lda c2v_v_pp + p * 3 + 1
        sta REC_REUHI
        adc c2v_v_adh + p
        sta c2v_v_pp + p * 3 + 1
        lda c2v_v_pp + p * 3 + 2
        sta REC_REUBANK
        adc c2v_v_adb + p
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
        jsr c2v_noskip          ; W7-V entry: contiguous rows
c2v_hiress:                     ; W10-V: rows skip c2v_a_skip[p] bytes
        C2V_ENTER
        ldx #80
        jsr c2v_setup
        lda #<VIC_COLRAM        ; W12-V: c2v_hrow starts at row 0
        sta c2v_hrow_d+1        ; W12-V
        lda #>VIC_COLRAM        ; W12-V
        sta c2v_hrow_d+2        ; W12-V
        lda #0                  ; W12-V: no pens yet
        ldx #15                 ; W12-V
:       sta c2v_v_cnt,x         ; W12-V
        dex                     ; W12-V
        bpl :-                  ; W12-V
        ldy #78                 ; W12-V
:       sta C2V_CP,y            ; W12-V
        dey                     ; W12-V
        dey                     ; W12-V
        bpl :-                  ; W12-V
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
        beq @e1                 ; W12-V: no pixel in this cell line
        lda C2V_CP,y            ; W12-V: the cell's pen is chosen?
        bne @e1                 ; W12-V
        jsr c2v_pick            ; W12-V
@e1:    iny
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
        beq @e2                 ; W12-V
        lda C2V_CP,y            ; W12-V
        bne @e2                 ; W12-V
        jsr c2v_pick            ; W12-V
@e2:    txa
        clc
        adc #8
        tax
        iny
        iny
        cpy #80
        bne @l2
        inc c2v_v_line
        lda c2v_v_line
        and #7                  ; W12-V: a char row done: its pens to
        bne :+                  ; W12-V: colour RAM
        jsr c2v_hrow            ; W12-V
:       lda c2v_v_line
        cmp #C2V_LINES
        beq @done
        jmp @ln
@done:  jmp c2v_leave           ; C1

; c2v_pick — W12-V: C2V_CP,y := the pen of the leftmost set pixel of this
; cell line (planes' bytes y then y+1, bit 7 first; one is set), and count
; it in c2v_v_cnt (saturating).  Keeps X and Y.
c2v_pick:
        sty c2v_v_py
        stx c2v_v_px
        lda P0,y
        ora P1,y
        ora P2,y
        ora P3,y
        bne :+
        iny                     ; the left byte is empty: the right one
        lda P0,y
        ora P1,y
        ora P2,y
        ora P3,y
:       sta c2v_v_pm
c2v_pick1:                      ; W13-D: c2v_ones enters here (c2v_v_pm, _py, _px set)
.ifdef C2V_MUT_PICKR
        lda #$01                ; mutant: the rightmost set pixel
:       bit c2v_v_pm
        bne :+
        asl
.else
        lda #$80
:       bit c2v_v_pm
        bne :+
        lsr
.endif
        bne :-                  ; (always: a bit is set)
:       sta c2v_v_pm            ; the pixel
        lda #0
        sta c2v_v_pen
.repeat 4, i
        lda P3 - i * $100,y     ; plane 3 first: it ends in bit 3
        and c2v_v_pm
        cmp #1                  ; C = the pixel's bit in this plane
        rol c2v_v_pen
.endrepeat
        ldy c2v_v_py
        ldx c2v_v_pen
        txa
        sta C2V_CP,y
        inc c2v_v_cnt,x
        bne :+
        dec c2v_v_cnt,x         ; hold at 255
:       ldx c2v_v_px
        rts

; c2v_hrow — W12-V: the finished char row's pens (C2V_CP) to its colour
; RAM row, C2V_CP cleared, the row pointer advanced by 40.
c2v_hrow:
        ldx #39
        ldy #78
:       lda C2V_CP,y
c2v_hrow_d:
        sta VIC_COLRAM,x        ; operand: the row's colour RAM (patched)
.ifndef C2V_MUT_HROW
        lda #0
        sta C2V_CP,y            ; (mutant: pens carried into the next row)
.endif
        dey
        dey
        dex
        bpl :-
        clc
        lda c2v_hrow_d+1
        adc #40
        sta c2v_hrow_d+1
        bcc :+
        inc c2v_hrow_d+2
:       rts

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
        jsr c2v_noskip          ; W7-V entry: contiguous rows
c2v_mc2s:                       ; W10-V: rows skip c2v_a_skip[p] bytes
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

; ===========================================================================
; W13-D: the 1:1 view (docs/C2V.md "1:1 view (W13-D)", c2v_ref.convert_one).
; One Amiga hires pixel = one C64 hires pixel: the first 40 of each 80-byte
; plane row (disp.s points c2v_a_src at the window's first byte).  Per 8x8
; cell: bg = the pen of the cell's top-left pixel; a C64 pixel is set iff
; its pen <> bg, i.e. the OR over the planes of (plane XOR bg's bit as
; $00/$FF); fg = the pen of the first set pixel in raster order (c2v_pick's
; tail), $FF if none.  Any cell drawn in two pens is pixel-exact.  Two
; passes over the same rows: c2v_ones counts (c2v_v_cnt: every cell's bg,
; and its fg if it has one) for disp_cols; c2v_ofin converts again and
; writes each finished char row's matrix cells, c2v_v_col[fg] << 4 |
; c2v_v_col[bg] (bg in both nibbles without an fg), at $01 = %xx100 under
; sei.  Colour RAM is not touched (unused in hires).  Needs C2V_ROWS = 25.
; ===========================================================================
C2V_OMK = $40                   ; @value W13-D: plane p's cell masks at P_p + C2V_OMK
C2V_OBG = P0 + $D0              ; W13-D: bg pen per cell (fg at C2V_CP + cx)
c2v_v_emit = c2v_args + 144     ; W13-D: bit 7: c2v_ofin's pass (write the matrix)
c2v_v_olim = c2v_args + 145     ; W13-D: lines with data (disp_lines); later lines read 0
.assert c2v_v_line = c2v_args + 68, error, "W13-D: disp_ozero reads c2v_v_line at c2v_args + 68"
.assert <c2v_screen = 0, error, "W13-D: c2v_ones clears c2v_v_cnt with <c2v_screen"
.assert C2V_CP + 40 <= C2V_OBG, error, "W13-D: fg and bg pens overlap"

; c2v_ofin — W13-D (disp, after c2v_ones and disp_cols): c2v_a_bgc :=
; c2v_v_col[0], then the second pass.
c2v_ofin:
        lda c2v_v_col
        sta c2v_a_bgc
        lda #$80
        .byte $2C               ; bit abs: skip the lda #0
; c2v_ones — W13-D: the counting pass (args as c2v_hiress; rows 80 bytes
; apart plus c2v_a_skip, 40 bytes fetched).
c2v_ones:
        lda #0
        sta c2v_v_emit
        lda CPU_PORT            ; c2v_leave's $01 (call with BASIC out)
        pha
        ldx #80
        jsr c2v_setup           ; advance = 80 + skip, as the fit view's
        lda #40
        sta REC_LENLO           ; the window: 40 bytes per row
        lda #>c2v_screen        ; c2v_orow's matrix row
        sta c2v_orw+2
        lda #<c2v_screen
        sta c2v_orw+1
        ldx #15                 ; (A = 0)
:       sta c2v_v_cnt,x
        dex
        bpl :-
@ln:    ldx #0                  ; fetch planes 0-3 (a loop: C2V_FETCH's
        ldy #>P0                ; unrolled form is 4x the bytes); Y = page
@f:     sty REC_C64HI
        clc
        lda c2v_v_pp,x
        sta REC_REULO
        adc c2v_v_adl - >P0,y
        sta c2v_v_pp,x
        lda c2v_v_pp+1,x
        sta REC_REUHI
        adc c2v_v_adh - >P0,y
        sta c2v_v_pp+1,x
        lda c2v_v_pp+2,x
        sta REC_REUBANK
        adc c2v_v_adb - >P0,y
        sta c2v_v_pp+2,x
        lda #REU_CMD_FETCH_AL
        sta REC_COMMAND
        inx
        inx
        inx
        iny
        cpy #>P0 + 4
        bne @f
        jsr disp_ozero          ; fix round 1: rows past the picture read 0
        lda c2v_v_line
        and #7
        bne :+
        jsr c2v_omask           ; a char row starts: bg pens and masks
:       jsr c2v_setln
        sta @o+1
        stx @o+2
        ldx #0                  ; output offset (cell * 8)
        ldy #0                  ; cell
@c:     lda P0,y
        eor P0+C2V_OMK,y
        sta @a+1
        lda P1,y
        eor P1+C2V_OMK,y
        sta @b+1
        lda P2,y
        eor P2+C2V_OMK,y
        sta @d+1
        lda P3,y
        eor P3+C2V_OMK,y
@a:     ora #0                  ; immediates patched
@b:     ora #0
@d:     ora #0
@o:     sta c2v_bitmap,x        ; operand patched
        beq @e
        sta c2v_v_pm            ; the set pixels
        lda C2V_CP,y
        bpl @e                  ; fg already chosen
        sty c2v_v_py
        stx c2v_v_px
        jsr c2v_pick1           ; C2V_CP,y := fg, counted
@e:     iny
        txa
        clc
        adc #8
        tax
        bne :+
        inc @o+2                ; cells 32-39 one page up
:       cpy #40
        bne @c
        inc c2v_v_line
        lda c2v_v_line
        and #7
        bne :+
        jsr c2v_orow
:       lda c2v_v_line
        cmp #C2V_LINES
        beq :+
        jmp @ln
:       jmp c2v_leave           ; C1

; c2v_omask — W13-D: from the char row's first line: C2V_OBG,x := the pen
; of cell x's top-left pixel, plane p's mask := $FF if that pen has bit p,
; C2V_CP,x := $FF (no fg yet).
c2v_omask:
        ldx #39
:       lda #0
        sta C2V_OBG,x
        lda #$FF                ; no fg yet
        sta C2V_CP,x
        dex
        bpl :-
        lda #>P3                ; plane 3 first: it ends in bit 3
@p:     sta @r+2
        sta @w+2
        ldx #39
@r:     lda P3,x                ; operand high byte patched
.ifdef C2V_MUT_OMASK
        and #$40                ; mutant: bg from the second pixel
.else
        and #$80
.endif
        beq :+
        lda #$FF
:
@w:     sta P3+C2V_OMK,x        ; operand high byte patched
        asl
        rol C2V_OBG,x
        dex
        bpl @r
        ldy @r+2
        dey
        tya
        cpy #>P0 - 1
        bne @p
        rts

; c2v_orow — W13-D: the finished char row.  Counting pass: each cell's bg
; counted.  c2v_ofin's pass: the row's 40 matrix cells (RAM under I/O:
; NMI guard as c2v_fill, then $01 = %xx100 under sei); the row + 40.
c2v_orow:
        ldy #39
        bit c2v_v_emit
        bmi @em
:       ldx C2V_OBG,y
        inc c2v_v_cnt,x
        bne @n
        dec c2v_v_cnt,x         ; hold at 255
@n:     dey
        bpl :-
        rts
@em:    lda #<mach_nmi
        sta mach_vnmi
        lda #>mach_nmi
        sta mach_vnmi+1
        php
        sei
        lda CPU_PORT
        sta c2v_v_port
        and #$F8
        ora #$04
        sta CPU_PORT
c2v_orc:
        ldx C2V_CP,y            ; fg ($FF: none -> bg)
        bpl :+
        ldx C2V_OBG,y
:       lda c2v_v_col,x
        asl
        asl
        asl
        asl
.ifdef C2V_MUT_OFIN
        ldx C2V_CP,y            ; mutant: the fg pen in the low nibble too
        bpl @m
.endif
        ldx C2V_OBG,y
@m:     ora c2v_v_col,x
c2v_orw:
        sta c2v_screen,y        ; operand: the row (patched)
        dey
        bpl c2v_orc
        lda c2v_v_port
        sta CPU_PORT
        plp
        clc
        lda c2v_orw+1
        adc #40
        sta c2v_orw+1
        bcc :+
        inc c2v_orw+2
:       rts
.endif

; Pad to whole pages: modules linked after this one keep their page offsets.
c2v_code1:
        .res (256 - ((c2v_code1 - c2v_code0) .mod 256)) .mod 256, $EA
