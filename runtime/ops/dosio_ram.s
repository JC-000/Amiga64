; ===========================================================================
; runtime/ops/dosio_ram.s — WS-B2 RAMDISK backend for hle_dos.s.
; Owner: WS-B2 (m2-dos).  Format: docs/HLE_DOS.md "RAMDISK format"; host
; mirror: host/hle_dos_env.py (ramdisk_image / parse_directory).
;
; A flat, non-hierarchical file store in guest/REU space
; [dosio_rd_base, dosio_rd_end] = $E00000-$FFFFFF, 2 MB (rc/guest.inc):
;   +$0000  header, 32 bytes:  "RDSK", u8 version (1), u8 entries (32),
;           u16 0, u32 free_ptr (next unallocated REU address),
;           u32 data_end (dosio_rd_end + 1), 16 reserved
;   +$0020  32 directory entries x 64 bytes:
;           u8 flags (bit 0 used), u8 name_len, u16 0, u32 size,
;           u32 capacity, u32 data (REU address), 16 reserved,
;           name[32] (<= 31 chars, NUL padded)
;   +$1000  file data: one contiguous extent per file, never moved
; Integers little-endian (C64-side metadata, not guest-visible data).
; Names compare case-insensitively (ASCII), stored case-preserved.
; A missing/invalid header is formatted in place on the first Open.
; Guest-created files get an extent of RD_NEW_CAP (64 KB) bytes, or what is
; left: 32 x 64 KB fills the 2 MB region exactly, so the 4 KB directory
; page comes out of the last extent (60 KB).  Writes past the extent fail
; with ERROR_DISK_FULL.  File id for the sharing rules = directory index.
; No delete/rename.
;
; Backend contract (hle_dos.s "Backend seam"): entered by jmp from
; dosio_be with X = dosio_slot; C=0 success, C=1 failure with dosio_err.
; All REU access goes through dosio_dma / dosio_stash_guest (C1 rules).
; ===========================================================================

.include "../macros.inc"

.export dosio_ram_init, dosio_ram_open, dosio_ram_close, dosio_ram_size
.export dosio_ram_seek, dosio_ram_rdchunk, dosio_ram_wrchunk
.export dosio_ram_wcheck, dosio_ram_setsize

.import dosio_buf, dosio_name, dosio_nlen, dosio_mode, dosio_slot
.import dosio_len, dosio_arg, dosio_err
.import dosio_dma, dosio_stash_guest, dosio_dc64, dosio_dreu, dosio_dlen
.import dosio_lockcheck, dosio_anyopen

RD_VERSION   = 1
RD_HDR_SIZE  = $20
RD_ENTRIES   = 32
RD_ENT_SIZE  = 64
RD_DATA_OFF  = $1000  ; @value
RD_NAME_OFF  = 32
RD_NAME_MAX  = 31
RD_NEW_CAP   = $10000  ; @value
RD_FLAG_USED = $01
; header fields
RH_FREE      = 8
RH_END       = 12
; entry fields
RE_FLAGS     = 0
RE_NLEN      = 1
RE_SIZE      = 4
RE_CAP       = 8
RE_DATA      = 12

ERROR_OBJECT_NOT_FOUND       = 205
ERROR_INVALID_COMPONENT_NAME = 210
ERROR_DISK_FULL              = 221
ERROR_NOT_A_DOS_DISK         = 225

FH_SLOTS = 8                    ; must match hle_dos.s

.assert RD_HDR_SIZE + RD_ENTRIES * RD_ENT_SIZE <= RD_DATA_OFF, error, "RAMDISK directory overlaps the data area"
.assert dosio_rd_base + RD_DATA_OFF < dosio_rd_end, error, "RAMDISK region too small"
.assert (dosio_rd_base & $FF) = 0, error, "RAMDISK base must be page aligned"

.segment "BSS"
dosio_rd_hdr:  .res RD_HDR_SIZE
dosio_rd_ent:  .res RD_ENT_SIZE
dosio_rd_i:    .res 1           ; directory index being examined
dosio_rd_free: .res 1           ; first free entry ($FF = none)
dosio_rd_ch:   .res 1           ; name-compare scratch
dosio_rd_idx:  .res FH_SLOTS    ; per slot: directory index
dosio_rd_d0:   .res FH_SLOTS    ; per slot: data address (24-bit)
dosio_rd_d1:   .res FH_SLOTS
dosio_rd_d2:   .res FH_SLOTS
dosio_rd_c0:   .res FH_SLOTS    ; per slot: capacity (32-bit)
dosio_rd_c1:   .res FH_SLOTS
dosio_rd_c2:   .res FH_SLOTS
dosio_rd_c3:   .res FH_SLOTS

.segment "CODE"

dosio_ram_init:
dosio_ram_close:
dosio_ram_seek:                 ; position lives in hle_dos.s; nothing to do
        clc
        rts

; ---------------------------------------------------------------------------
; DMA address helpers
; ---------------------------------------------------------------------------
rd_hdr_dma:                     ; C64 = header copy, REU = base, 32 bytes
        lda #<dosio_rd_hdr
        sta dosio_dc64
        lda #>dosio_rd_hdr
        sta dosio_dc64+1
        lda #<dosio_rd_base
        sta dosio_dreu
        lda #>dosio_rd_base
        sta dosio_dreu+1
        lda #^dosio_rd_base
        sta dosio_dreu+2
        lda #RD_HDR_SIZE
        sta dosio_dlen
        lda #0
        sta dosio_dlen+1
        rts

; dosio_dreu := address of entry A (+ Y bytes); dlen := 64 - Y; C64 = entry
rd_ent_dma:
        sta dosio_rd_i
        lda #0
        sta dosio_dreu+1
        lda dosio_rd_i          ; i * 64 = (i >> 2) : (i & 3) << 6
        lsr a
        lsr a
        sta dosio_dreu+1
        lda dosio_rd_i
        and #3
        lsr a
        ror a
        ror a                   ; (i & 3) << 6
        sta dosio_dreu
        sty dosio_dlen          ; temp: field offset
        clc
        lda dosio_dreu
        adc dosio_dlen
        adc #<(dosio_rd_base + RD_HDR_SIZE)
        sta dosio_dreu
        lda dosio_dreu+1
        adc #>(dosio_rd_base + RD_HDR_SIZE)
        sta dosio_dreu+1
        lda #^(dosio_rd_base + RD_HDR_SIZE)
        adc #0
        sta dosio_dreu+2
        clc
        tya
        adc #<dosio_rd_ent
        sta dosio_dc64
        lda #>dosio_rd_ent
        adc #0
        sta dosio_dc64+1
        sec
        lda #RD_ENT_SIZE
        sty dosio_dlen
        sbc dosio_dlen
        sta dosio_dlen
        lda #0
        sta dosio_dlen+1
        rts

rd_ent_load:                    ; A = index -> dosio_rd_ent
        ldy #0
        jsr rd_ent_dma
        lda #REU_CMD_FETCH
        jmp dosio_dma

rd_ent_store:                   ; A = index <- dosio_rd_ent
        ldy #0
        jsr rd_ent_dma
        jmp dosio_stash_guest

; ---------------------------------------------------------------------------
; rd_format — write a fresh header and an all-free directory.
; ---------------------------------------------------------------------------
rd_format:
        lda #0
        ldx #0
@z:     sta dosio_buf,x
        inx
        bne @z
        ; directory: (RD_ENTRIES * RD_ENT_SIZE) / 256 pages of zeros
        lda #<dosio_buf
        sta dosio_dc64
        lda #>dosio_buf
        sta dosio_dc64+1
        lda #<(dosio_rd_base + RD_HDR_SIZE)
        sta dosio_dreu
        lda #>(dosio_rd_base + RD_HDR_SIZE)
        sta dosio_dreu+1
        lda #^(dosio_rd_base + RD_HDR_SIZE)
        sta dosio_dreu+2
        lda #0
        sta dosio_dlen
        lda #1
        sta dosio_dlen+1
        lda #(RD_ENTRIES * RD_ENT_SIZE) / 256
        sta dosio_rd_i
@pg:    jsr dosio_stash_guest
        inc dosio_dreu+1
        bne :+
        inc dosio_dreu+2
:       dec dosio_rd_i
        bne @pg
        ; header
        ldx #RD_HDR_SIZE - 1
        lda #0
@h:     sta dosio_rd_hdr,x
        dex
        bpl @h
        ldx #3
@m:     lda rd_magic,x
        sta dosio_rd_hdr,x
        dex
        bpl @m
        lda #RD_VERSION
        sta dosio_rd_hdr+4
        lda #RD_ENTRIES
        sta dosio_rd_hdr+5
        lda #<(dosio_rd_base + RD_DATA_OFF)
        sta dosio_rd_hdr+RH_FREE
        lda #>(dosio_rd_base + RD_DATA_OFF)
        sta dosio_rd_hdr+RH_FREE+1
        lda #^(dosio_rd_base + RD_DATA_OFF)
        sta dosio_rd_hdr+RH_FREE+2
        lda #<(dosio_rd_end + 1)
        sta dosio_rd_hdr+RH_END
        lda #>(dosio_rd_end + 1)
        sta dosio_rd_hdr+RH_END+1
        lda #^(dosio_rd_end + 1)
        sta dosio_rd_hdr+RH_END+2
        lda #<((dosio_rd_end + 1) >> 24)
        sta dosio_rd_hdr+RH_END+3
        jsr rd_hdr_dma
        jmp dosio_stash_guest

; ---------------------------------------------------------------------------
; Open: dosio_name/dosio_nlen (1..63), dosio_mode 0 OLD / 1 NEW / 2 RW.
; ---------------------------------------------------------------------------
dosio_ram_open:
        lda #ERROR_INVALID_COMPONENT_NAME
        sta dosio_err
        lda dosio_nlen
        cmp #RD_NAME_MAX + 1
        bcc :+
        jmp @fail
:       jsr rd_hdr_dma          ; load + validate the header
        lda #REU_CMD_FETCH
        jsr dosio_dma
        ldx #3
@mg:    lda dosio_rd_hdr,x
        cmp rd_magic,x
        bne @fmt
        dex
        bpl @mg
        lda dosio_rd_hdr+4
        cmp #RD_VERSION
        bne @fmt
        lda dosio_rd_hdr+5
        cmp #RD_ENTRIES
        beq @scan
@fmt:   jsr dosio_anyopen       ; never format under open handles (review
        bcc :+                  ; fix2 item 1): their entries would be wiped
        lda #ERROR_NOT_A_DOS_DISK ; and reused; leave the header untouched
        sta dosio_err
        jmp @fail
:       jsr rd_format           ; leaves dosio_rd_hdr = fresh header

@scan:  lda #$FF
        sta dosio_rd_free
        lda #0
        sta dosio_rd_i
@ent:   lda dosio_rd_i
        jsr rd_ent_load
        lda dosio_rd_ent+RE_FLAGS
        and #RD_FLAG_USED
        bne @used
        lda dosio_rd_free
        bpl @next
        lda dosio_rd_i
        sta dosio_rd_free
        jmp @next
@used:  lda dosio_rd_ent+RE_NLEN
        cmp dosio_nlen
        bne @next
        ldy #0
@cmp:   lda dosio_rd_ent+RD_NAME_OFF,y
        jsr rd_lower
        sta dosio_rd_ch
        lda dosio_name,y
        jsr rd_lower
        cmp dosio_rd_ch
        bne @next
        iny
        cpy dosio_nlen
        bne @cmp
        jmp @found
@next:  inc dosio_rd_i
        lda dosio_rd_i
        cmp #RD_ENTRIES
        bne @ent

        ; --- not found ---
        lda #ERROR_OBJECT_NOT_FOUND
        sta dosio_err
        lda dosio_mode
        beq @fail1              ; MODE_OLDFILE: must exist
        lda #ERROR_DISK_FULL
        sta dosio_err
        lda dosio_rd_free
        bmi @fail1              ; directory full
        ; avail = data_end - free_ptr ; cap = min(avail, RD_NEW_CAP)
        sec
        lda dosio_rd_hdr+RH_END
        sbc dosio_rd_hdr+RH_FREE
        sta dosio_arg
        lda dosio_rd_hdr+RH_END+1
        sbc dosio_rd_hdr+RH_FREE+1
        sta dosio_arg+1
        lda dosio_rd_hdr+RH_END+2
        sbc dosio_rd_hdr+RH_FREE+2
        sta dosio_arg+2
        lda dosio_rd_hdr+RH_END+3
        sbc dosio_rd_hdr+RH_FREE+3
        sta dosio_arg+3
        bcc @fail1              ; corrupt header: free_ptr past the end
        lda dosio_arg
        ora dosio_arg+1
        ora dosio_arg+2
        ora dosio_arg+3
        bne @room
@fail1: jmp @fail               ; no space left
@room:
        lda dosio_arg+3         ; avail >= RD_NEW_CAP -> cap = RD_NEW_CAP
        ora dosio_arg+2
        beq @cap
        lda #<RD_NEW_CAP
        sta dosio_arg
        lda #>RD_NEW_CAP
        sta dosio_arg+1
        lda #^RD_NEW_CAP
        sta dosio_arg+2
        lda #0
        sta dosio_arg+3
@cap:   ldx #RD_ENT_SIZE - 1    ; build the new entry
        lda #0
@ze:    sta dosio_rd_ent,x
        dex
        bpl @ze
        lda #RD_FLAG_USED
        sta dosio_rd_ent+RE_FLAGS
        lda dosio_nlen
        sta dosio_rd_ent+RE_NLEN
        ldx #3
@ce:    lda dosio_arg,x
        sta dosio_rd_ent+RE_CAP,x
        lda dosio_rd_hdr+RH_FREE,x
        sta dosio_rd_ent+RE_DATA,x
        dex
        bpl @ce
        ldy #0
@nm:    lda dosio_name,y
        sta dosio_rd_ent+RD_NAME_OFF,y
        iny
        cpy dosio_nlen
        bne @nm
        clc                     ; header: free_ptr += cap
        ldx #0
        php
@fp:    plp
        lda dosio_rd_hdr+RH_FREE,x
        adc dosio_arg,x
        sta dosio_rd_hdr+RH_FREE,x
        php
        inx
        cpx #4
        bne @fp
        plp
        lda dosio_rd_free
        sta dosio_rd_i
        jsr dosio_lockcheck     ; sets dosio_curfid.  Refusal is possible only
        bcc :+                  ; if the guest scribbled on the directory
        jmp @fail               ; under an open handle: fail, change nothing
:       lda dosio_rd_i          ; (lockcheck clobbers A)
        jsr rd_ent_store
        jsr rd_hdr_dma
        jsr dosio_stash_guest
        jmp @slot

@fail:  sec
        rts

@found: lda dosio_rd_i           ; sharing rules first: no side effect
        jsr dosio_lockcheck     ; on refusal (C=1, ERROR_OBJECT_IN_USE)
        bcs @fail
        lda dosio_mode          ; MODE_NEWFILE on an existing file: truncate
        cmp #1
        bne @slot
        lda #0
        sta dosio_rd_ent+RE_SIZE
        sta dosio_rd_ent+RE_SIZE+1
        sta dosio_rd_ent+RE_SIZE+2
        sta dosio_rd_ent+RE_SIZE+3
        lda dosio_rd_i
        jsr rd_ent_store

@slot:  ldx dosio_slot          ; per-slot copy of the immutable fields
        lda dosio_rd_i
        sta dosio_rd_idx,x
        lda dosio_rd_ent+RE_DATA
        sta dosio_rd_d0,x
        lda dosio_rd_ent+RE_DATA+1
        sta dosio_rd_d1,x
        lda dosio_rd_ent+RE_DATA+2
        sta dosio_rd_d2,x
        lda dosio_rd_ent+RE_CAP
        sta dosio_rd_c0,x
        lda dosio_rd_ent+RE_CAP+1
        sta dosio_rd_c1,x
        lda dosio_rd_ent+RE_CAP+2
        sta dosio_rd_c2,x
        lda dosio_rd_ent+RE_CAP+3
        sta dosio_rd_c3,x
        clc
        rts

rd_lower:                       ; ASCII A-Z -> a-z
        cmp #'A'
        bcc @r
        cmp #'Z' + 1
        bcs @r
        ora #$20
@r:     rts

; ---------------------------------------------------------------------------
; size: dosio_arg <- entry.size (read from the REU each time, so handles on
; the same file share it).  setsize: entry.size <- dosio_arg.
; ---------------------------------------------------------------------------
rd_size_dma:
        lda dosio_rd_idx,x
        ldy #RE_SIZE
        jsr rd_ent_dma
        lda #<dosio_arg
        sta dosio_dc64
        lda #>dosio_arg
        sta dosio_dc64+1
        lda #4
        sta dosio_dlen
        rts

dosio_ram_size:
        jsr rd_size_dma
        lda #REU_CMD_FETCH
        jsr dosio_dma
        clc
        rts

dosio_ram_setsize:
        jsr rd_size_dma
        jsr dosio_stash_guest
        clc
        rts

; ---------------------------------------------------------------------------
; rdchunk / wrchunk: dosio_buf <-> data + dosio_arg, dosio_len bytes (1-256)
; ---------------------------------------------------------------------------
rd_data_dma:
        clc
        lda dosio_rd_d0,x
        adc dosio_arg
        sta dosio_dreu
        lda dosio_rd_d1,x
        adc dosio_arg+1
        sta dosio_dreu+1
        lda dosio_rd_d2,x
        adc dosio_arg+2
        sta dosio_dreu+2
        lda #<dosio_buf
        sta dosio_dc64
        lda #>dosio_buf
        sta dosio_dc64+1
        lda dosio_len
        sta dosio_dlen
        lda dosio_len+1
        sta dosio_dlen+1
        rts

dosio_ram_rdchunk:              ; hle_dos.s already clamped to the file size
        jsr rd_data_dma
        lda #REU_CMD_FETCH
        jsr dosio_dma
        clc
        rts

dosio_ram_wrchunk:
        jsr rd_data_dma
        jsr dosio_stash_guest
        clc
        rts

; wcheck: dosio_arg = end position of the pending write; must be <= capacity
dosio_ram_wcheck:
        lda dosio_rd_c0,x       ; cap - end borrows -> too big
        cmp dosio_arg
        lda dosio_rd_c1,x
        sbc dosio_arg+1
        lda dosio_rd_c2,x
        sbc dosio_arg+2
        lda dosio_rd_c3,x
        sbc dosio_arg+3
        bcc @full
        clc
        rts
@full:  lda #ERROR_DISK_FULL
        sta dosio_err
        sec
        rts

.segment "RODATA"
rd_magic: .byte "RDSK"
