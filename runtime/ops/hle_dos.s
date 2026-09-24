; ===========================================================================
; runtime/ops/hle_dos.s — M2: dos.library file I/O (Open/Close/Read/Write/
; Seek/IoErr on FileHandles), generic layer + backend seam.
; Owner: WS-B2 (m2-dos).  Contract: docs/WAVE4_BRIEF.md C1-C4,
; docs/HLE_DOS.md (function table, FileHandle pool, backend seam, formats).
;
; Entry points (all entered by jmp with the hle.s handler entry state:
; opcode word in op_lo/op_hi, guest registers as the caller set them,
; return address on the guest stack; each retires by emulating RTS):
;   hle_dos_dispatch — HLE index $30-$4F in op_lo (routed by hle.s)
;   hle_dos_read     — dos.library/Read  on a FileHandle with fh_Args >= $10
;   hle_dos_write    — dos.library/Write on a FileHandle with fh_Args >= $10
; Host/runner entry (jsr, returns with rts):
;   hle_dos_init     — reset all slots + IoErr, pick the backend.  Also run
;                      lazily on the first HLE DOS call after boot (BSS is
;                      not cleared at boot; guarded by hle_dos_magic).
;
; Backends (selected at init, docs/HLE_DOS.md "Backend seam"):
;   0 = RAMDISK  (runtime/ops/dosio_ram.s) — guest/REU $F00000-$FFFFFF
;   1 = ULTDOS   (runtime/ops/dosio_uci.s) — Ultimate Command Interface,
;       chosen when $DF1D identifies the UCI.  HARDWARE-UNVALIDATED.
;
; Guest-memory access rules (C1): bulk transfers program the REC directly
; (dosio_dma), always followed by jsr mem_rec_dirty; guest bytes written by
; DMA are re-written (same value) through mem_write8 at the first and last
; byte of every transfer, so the memory API's fetch-window overlap check
; invalidates any cached page the DMA touched.  Build with DOSIO_NOINVAL
; (TEST-ONLY) to drop that re-write: tests/run_dos.py `codepage` must fail.
; ===========================================================================

.include "../macros.inc"

.export hle_dos_dispatch, hle_dos_read, hle_dos_write
.export hle_dos_init, hle_dos_ioerr, hle_dos_magic, dosio_backend
; backend parameter block + DMA helpers (docs/HLE_DOS.md "Backend seam")
.export dosio_buf, dosio_name, dosio_nlen, dosio_mode, dosio_slot
.export dosio_len, dosio_arg, dosio_err
.export dosio_dma, dosio_stash_guest, dosio_dc64, dosio_dreu, dosio_dlen

.import mem_read8, mem_read32, mem_write8, mem_write32, mem_rec_dirty
.import dispatch_next, emu_halt
.import dosio_ram_init, dosio_ram_open, dosio_ram_close, dosio_ram_size
.import dosio_ram_seek, dosio_ram_rdchunk, dosio_ram_wrchunk
.import dosio_ram_wcheck, dosio_ram_setsize
.import dosio_uci_detect, dosio_uci_init, dosio_uci_open, dosio_uci_close
.import dosio_uci_size, dosio_uci_seek, dosio_uci_rdchunk, dosio_uci_wrchunk
.import dosio_uci_wcheck, dosio_uci_setsize

; --- HLE indices (mirrored by host/hle_dos_env.py, C3 range $30-$4F) ------
HLE_DOS_IDX_OPEN  = $30         ; dos.library/Open   LVO  -30
HLE_DOS_IDX_CLOSE = $31         ; dos.library/Close  LVO  -36
HLE_DOS_IDX_SEEK  = $32         ; dos.library/Seek   LVO  -66
HLE_DOS_IDX_IOERR = $33         ; dos.library/IoErr  LVO -132
HLE_DOS_IDX_READ  = $4E         ; direct hle_dos_read  (test seam, see HLE_DOS.md)
HLE_DOS_IDX_WRITE = $4F         ; direct hle_dos_write (test seam)

; --- FileHandle pool (C2; guest FH_POOL from rc/guest.inc) -----------------
FH_SIZE       = 44              ; sizeof(struct FileHandle), dos/dosextens.h
FH_SLOTS      = 8
FH_ARGS_OFF   = 36              ; fh_Args = handle kind
FH_KIND_FILE0 = $10             ; slot n -> kind $10+n; Close writes $FFFFFFFF
.assert FH_POOL + FH_SIZE * FH_SLOTS - 1 <= FH_POOL_END, error, "FileHandle pool overflows its region"
.assert (FH_SIZE & 3) = 0, error, "FileHandles must stay longword aligned"

; --- dos/dos.h ---------------------------------------------------------------
MODE_OLDFILE   = 1005
MODE_NEWFILE   = 1006
MODE_READWRITE = 1004
ERROR_NO_FREE_STORE          = 103
ERROR_OBJECT_NOT_FOUND       = 205
ERROR_ACTION_NOT_KNOWN       = 209
ERROR_INVALID_COMPONENT_NAME = 210
ERROR_INVALID_LOCK           = 211
ERROR_SEEK_ERROR             = 219
ERROR_DISK_FULL              = 221

NAME_MAX = 63                   ; dosio_name holds 63 chars + NUL

; --- Backend operation numbers (offsets into dosio_vec) --------------------
BE_INIT    = 0
BE_OPEN    = 2
BE_CLOSE   = 4
BE_SIZE    = 6
BE_SEEK    = 8
BE_RDCHUNK = 10
BE_WRCHUNK = 12
BE_WCHECK  = 14
BE_SETSIZE = 16
BE_NVEC    = 18

; --- BSS (C64 side; no ZP, C4) ----------------------------------------------
.segment "BSS"
hle_dos_magic: .res 4           ; "DOS!" once hle_dos_init has run
hle_dos_ioerr: .res 4           ; IoErr() value, LE (pr_Result2 stand-in)
dosio_backend: .res 1           ; 0 RAMDISK, 1 ULTDOS
dosio_vec:     .res BE_NVEC
dosio_sopen:   .res FH_SLOTS    ; nonzero = slot in use
dosio_pos0:    .res FH_SLOTS    ; per-slot file position, 32-bit LE split
dosio_pos1:    .res FH_SLOTS
dosio_pos2:    .res FH_SLOTS
dosio_pos3:    .res FH_SLOTS
; backend parameter block
dosio_buf:     .res 256         ; bounce buffer (guest <-> backend)
dosio_name:    .res NAME_MAX + 1
dosio_nlen:    .res 1
dosio_mode:    .res 1           ; 0 OLDFILE, 1 NEWFILE, 2 READWRITE
dosio_slot:    .res 1
dosio_len:     .res 2           ; chunk length in / bytes transferred out
dosio_arg:     .res 4           ; position / size argument
dosio_err:     .res 1           ; AmigaOS error code when a backend fails (C=1)
; DMA helper arguments
dosio_dc64:    .res 2
dosio_dreu:    .res 3
dosio_dlen:    .res 2
; generic-layer scratch
dosio_ga:      .res 4           ; walking guest address
dosio_rem:     .res 4           ; bytes still to transfer
dosio_tot:     .res 4           ; bytes transferred
dosio_req:     .res 2           ; current chunk request
dosio_t32:     .res 4           ; 32-bit temp (size / old position)
dosio_fh:      .res 2           ; guest address of the current FileHandle

.segment "CODE"

; ===========================================================================
; Dispatch
; ===========================================================================
hle_dos_dispatch:
        jsr dosio_ensure
        lda op_lo
        cmp #HLE_DOS_IDX_OPEN
        bne :+
        jmp dosio_open
:       cmp #HLE_DOS_IDX_CLOSE
        bne :+
        jmp dosio_close
:       cmp #HLE_DOS_IDX_SEEK
        bne :+
        jmp dosio_seek
:       cmp #HLE_DOS_IDX_IOERR
        bne :+
        jmp dosio_ioerr
:       cmp #HLE_DOS_IDX_READ
        bne :+
        jmp hle_dos_read
:       cmp #HLE_DOS_IDX_WRITE
        bne :+
        jmp hle_dos_write
:       lda #HALT_ALINE         ; unassigned index in $30-$4F
        jmp emu_halt

; ===========================================================================
; hle_dos_init — reset slots and IoErr, select + initialise the backend.
; Host-callable (jsr/rts).  Clobbers A/X/Y.
; ===========================================================================
hle_dos_init:
        jsr mem_rec_dirty       ; C1: host-reachable entry
        lda #0
        ldx #FH_SLOTS - 1
@clr:   sta dosio_sopen,x
        dex
        bpl @clr
        sta hle_dos_ioerr
        sta hle_dos_ioerr+1
        sta hle_dos_ioerr+2
        sta hle_dos_ioerr+3
        jsr dosio_uci_detect    ; C=1: Ultimate Command Interface present
        lda #0
        rol a
        sta dosio_backend
        ldy #0
        cmp #0
        beq @copy
        ldy #BE_NVEC
@copy:  ldx #0
@cp:    lda dosio_vectab,y
        sta dosio_vec,x
        iny
        inx
        cpx #BE_NVEC
        bne @cp
        ldy #BE_INIT
        jsr dosio_be
        lda #'D'
        sta hle_dos_magic
        lda #'O'
        sta hle_dos_magic+1
        lda #'S'
        sta hle_dos_magic+2
        lda #'!'
        sta hle_dos_magic+3
        rts

dosio_ensure:
        lda hle_dos_magic
        cmp #'D'
        bne @init
        lda hle_dos_magic+1
        cmp #'O'
        bne @init
        lda hle_dos_magic+2
        cmp #'S'
        bne @init
        lda hle_dos_magic+3
        cmp #'!'
        bne @init
        rts
@init:  jmp hle_dos_init

; Y = BE_* operation; X := dosio_slot on entry to the backend routine.
dosio_be:
        lda dosio_vec,y
        sta @jv+1
        lda dosio_vec+1,y
        sta @jv+2
        ldx dosio_slot
@jv:    jmp $FFFF               ; self-modified (PRG is in RAM)

; ===========================================================================
; Retire helpers
; ===========================================================================
; D0 := 0 (Open failure / DOSFALSE) with IoErr := dosio_err
dosio_fail0:
        jsr dosio_seterr
dosio_ret0:
        lda #0
        beq dosio_d0a           ; always
; D0 := -1 with IoErr := dosio_err
dosio_failm1:
        jsr dosio_seterr
dosio_retm1:                    ; D0 := -1 (also DOSTRUE)
        lda #$FF
dosio_d0a:
        sta r_d0
        sta r_d0+1
        sta r_d0+2
        sta r_d0+3
        ; fall through
; emulate RTS: PC = [A7] (24 bits), A7 += 4, next instruction
dosio_ret:
        ldx #3
@sp:    lda r_a7,x
        sta m_addr,x
        dex
        bpl @sp
        jsr mem_read32
        clc
        lda r_a7
        adc #4
        sta r_a7
        lda r_a7+1
        adc #0
        sta r_a7+1
        lda r_a7+2
        adc #0
        sta r_a7+2
        lda r_a7+3
        adc #0
        sta r_a7+3
        lda m_val
        sta r_pc
        lda m_val+1
        sta r_pc+1
        lda m_val+2
        sta r_pc+2
        lda #0
        sta r_pc+3
        jmp dispatch_next

dosio_seterr:
        lda dosio_err
        sta hle_dos_ioerr
        lda #0
        sta hle_dos_ioerr+1
        sta hle_dos_ioerr+2
        sta hle_dos_ioerr+3
        rts

; ===========================================================================
; $A133 IoErr — D0 = last error code (set by failing WS-B2 calls only).
; ===========================================================================
dosio_ioerr:
        ldx #3
@c:     lda hle_dos_ioerr,x
        sta r_d0,x
        dex
        bpl @c
        jmp dosio_ret

; ===========================================================================
; dosio_getfh — validate the FileHandle BPTR in D1.
; Valid iff D1 << 2 is exactly a pool slot's address, the slot is open and
; the guest fh_Args still holds $10+n.  C=0: X = dosio_slot = n,
; dosio_fh = guest address.  C=1: invalid (dosio_err = ERROR_INVALID_LOCK).
; ===========================================================================
dosio_getfh:
        lda #ERROR_INVALID_LOCK
        sta dosio_err
        lda r_d1+3
        ora r_d1+2
        bne @bad
        lda r_d1+1
        cmp #$40
        bcs @bad                ; BPTR >= $4000: address >= $10000
        lda r_d1                ; dosio_fh = D1 << 2 (16 bits)
        asl a
        sta dosio_fh
        lda r_d1+1
        rol a
        sta dosio_fh+1
        asl dosio_fh
        rol dosio_fh+1
        ldx #FH_SLOTS - 1
@find:  lda dosio_fh
        cmp fh_lo,x
        bne @next
        lda dosio_fh+1
        cmp fh_hi,x
        beq @found
@next:  dex
        bpl @find
@bad:   sec
        rts
@found: lda dosio_sopen,x
        beq @bad
        stx dosio_slot
        jsr dosio_fh_args_addr
        jsr mem_read32
        ldx dosio_slot
        lda m_val+3
        ora m_val+2
        ora m_val+1
        bne @bad
        txa
        clc
        adc #FH_KIND_FILE0
        cmp m_val
        bne @bad
        clc
        rts

; m_addr := dosio_fh + FH_ARGS_OFF (guest)
dosio_fh_args_addr:
        clc
        lda dosio_fh
        adc #FH_ARGS_OFF
        sta m_addr
        lda dosio_fh+1
        adc #0
        sta m_addr+1
        lda #^FH_POOL
        sta m_addr+2
        lda #0
        sta m_addr+3
        rts

; ===========================================================================
; $A130 Open — name D1 (C string), accessMode D2.  D0 = BPTR or 0 (IoErr).
; ===========================================================================
dosio_open:
        lda #ERROR_ACTION_NOT_KNOWN
        sta dosio_err
        lda r_d2+3              ; mode: D2 must be $3EC/$3ED/$3EE
        ora r_d2+2
        bne @fail
        lda r_d2+1
        cmp #>MODE_OLDFILE
        bne @fail
        lda r_d2
        ldx #0
        cmp #<MODE_OLDFILE
        beq @mode
        inx
        cmp #<MODE_NEWFILE
        beq @mode
        inx
        cmp #<MODE_READWRITE
        beq @mode
@fail:  jmp dosio_fail0
@mode:  stx dosio_mode

        ; copy the name (at most NAME_MAX chars + NUL) via the memory API
        lda #ERROR_INVALID_COMPONENT_NAME
        sta dosio_err
        ldx #3
@na:    lda r_d1,x
        sta dosio_ga,x
        dex
        bpl @na
        lda #0
        sta dosio_nlen
@nl:    ldx #3
@nb:    lda dosio_ga,x
        sta m_addr,x
        dex
        bpl @nb
        jsr mem_read8
        ldx dosio_nlen
        lda m_val
        sta dosio_name,x
        beq @ndone
        cpx #NAME_MAX
        beq @fail               ; no NUL within NAME_MAX chars
        inc dosio_nlen
        inc dosio_ga
        bne @nl
        inc dosio_ga+1
        bne @nl
        inc dosio_ga+2
        jmp @nl
@ndone: cpx #0
        beq @fail               ; empty name

        ; free slot?
        lda #ERROR_NO_FREE_STORE
        sta dosio_err
        ldx #0
@fs:    lda dosio_sopen,x
        beq @got
        inx
        cpx #FH_SLOTS
        bne @fs
        beq @fail               ; always
@got:   stx dosio_slot
        ldy #BE_OPEN
        jsr dosio_be
        bcs @fail
        ldx dosio_slot
        lda #1
        sta dosio_sopen,x
        lda #0
        sta dosio_pos0,x
        sta dosio_pos1,x
        sta dosio_pos2,x
        sta dosio_pos3,x
        lda fh_lo,x
        sta dosio_fh
        lda fh_hi,x
        sta dosio_fh+1

        ; initialise the guest FileHandle: 11 longwords, fh_Args = $10+n
        ldy #0                  ; Y = field offset
@fld:   tya
        clc
        adc dosio_fh
        sta m_addr
        lda dosio_fh+1
        adc #0
        sta m_addr+1
        lda #^FH_POOL
        sta m_addr+2
        lda #0
        sta m_addr+3
        sta m_val
        sta m_val+1
        sta m_val+2
        sta m_val+3
        cpy #FH_ARGS_OFF
        bne @wr
        lda dosio_slot
        clc
        adc #FH_KIND_FILE0
        sta m_val
@wr:    tya
        pha
        jsr mem_write32
        pla
        clc
        adc #4
        tay
        cpy #FH_SIZE
        bne @fld

        lda dosio_fh+1          ; D0 = BPTR = address >> 2
        lsr a
        sta r_d0+1
        lda dosio_fh
        ror a
        sta r_d0
        lsr r_d0+1
        ror r_d0
        lda #0
        sta r_d0+2
        sta r_d0+3
        jmp dosio_ret

; ===========================================================================
; $A131 Close — fh D1.  D0 = DOSTRUE (-1), or DOSFALSE (0) + IoErr for an
; invalid handle.  Close(0) is a harmless DOSTRUE no-op (V36+ behaviour).
; ===========================================================================
dosio_close:
        lda r_d1
        ora r_d1+1
        ora r_d1+2
        ora r_d1+3
        bne :+
        jmp dosio_retm1
:       jsr dosio_getfh
        bcc :+
        jmp dosio_fail0
:       ldy #BE_CLOSE
        jsr dosio_be            ; backend close cannot fail meaningfully
        ldx dosio_slot
        lda #0
        sta dosio_sopen,x
        jsr dosio_fh_args_addr  ; fh_Args := $FFFFFFFF (dead, still >= $10)
        lda #$FF
        sta m_val
        sta m_val+1
        sta m_val+2
        sta m_val+3
        jsr mem_write32
        jmp dosio_retm1

; ===========================================================================
; $A132 Seek — fh D1, position D2 (signed), mode D3 (OFFSET_BEGINNING -1,
; OFFSET_CURRENT 0, OFFSET_END 1).  D0 = old position, or -1 + IoErr
; (ERROR_SEEK_ERROR) for a bad mode or a target outside 0..size; the
; position is then unchanged.
; ===========================================================================
dosio_seek:
        jsr dosio_getfh
        bcc :+
        jmp dosio_failm1
:       ldy #BE_SIZE
        jsr dosio_be            ; dosio_arg = size
        ldx #3
@sz:    lda dosio_arg,x
        sta dosio_t32,x         ; t32 = size
        dex
        bpl @sz
        lda #ERROR_SEEK_ERROR
        sta dosio_err
        ldx dosio_slot
        ; base -> dosio_arg by mode
        lda r_d3
        and r_d3+1
        and r_d3+2
        and r_d3+3
        cmp #$FF
        beq @beg                ; -1 = OFFSET_BEGINNING
        lda r_d3+3
        ora r_d3+2
        ora r_d3+1
        bne @bad
        lda r_d3
        beq @cur                ; 0 = OFFSET_CURRENT
        cmp #1
        beq @add                ; 1 = OFFSET_END: base = size (already in arg)
@bad:   jmp dosio_failm1
@beg:   lda #0
        sta dosio_arg
        sta dosio_arg+1
        sta dosio_arg+2
        sta dosio_arg+3
        beq @add                ; always
@cur:   lda dosio_pos0,x
        sta dosio_arg
        lda dosio_pos1,x
        sta dosio_arg+1
        lda dosio_pos2,x
        sta dosio_arg+2
        lda dosio_pos3,x
        sta dosio_arg+3
@add:   clc                     ; arg = base + D2
        lda dosio_arg
        adc r_d2
        sta dosio_arg
        lda dosio_arg+1
        adc r_d2+1
        sta dosio_arg+1
        lda dosio_arg+2
        adc r_d2+2
        sta dosio_arg+2
        lda dosio_arg+3
        adc r_d2+3
        sta dosio_arg+3
        bmi @bad                ; negative target
        lda dosio_t32           ; size - arg must not borrow (arg <= size)
        cmp dosio_arg
        lda dosio_t32+1
        sbc dosio_arg+1
        lda dosio_t32+2
        sbc dosio_arg+2
        lda dosio_t32+3
        sbc dosio_arg+3
        bcc @bad
        ldy #BE_SEEK
        jsr dosio_be            ; backend positions itself at dosio_arg
        bcs @bad
        ldx dosio_slot          ; D0 = old position; position = arg
        lda dosio_pos0,x
        sta r_d0
        lda dosio_arg
        sta dosio_pos0,x
        lda dosio_pos1,x
        sta r_d0+1
        lda dosio_arg+1
        sta dosio_pos1,x
        lda dosio_pos2,x
        sta r_d0+2
        lda dosio_arg+2
        sta dosio_pos2,x
        lda dosio_pos3,x
        sta r_d0+3
        lda dosio_arg+3
        sta dosio_pos3,x
        jmp dosio_ret

; ===========================================================================
; Read — fh D1, buffer D2, length D3.  D0 = bytes read (0 at EOF), -1 +
; IoErr on an invalid handle or a backend failure.  Transfers go in chunks
; that never cross a 256-byte guest page (<= 256 bytes each).
; ===========================================================================
hle_dos_read:
        jsr dosio_ensure
        jsr dosio_getfh
        bcc :+
        jmp dosio_failm1
:       ldy #BE_SIZE
        jsr dosio_be            ; dosio_arg = size
        ldx dosio_slot          ; rem = size - pos (0 if pos > size)
        sec
        lda dosio_arg
        sbc dosio_pos0,x
        sta dosio_rem
        lda dosio_arg+1
        sbc dosio_pos1,x
        sta dosio_rem+1
        lda dosio_arg+2
        sbc dosio_pos2,x
        sta dosio_rem+2
        lda dosio_arg+3
        sbc dosio_pos3,x
        sta dosio_rem+3
        bcs @avail
        lda #0
        sta dosio_rem
        sta dosio_rem+1
        sta dosio_rem+2
        sta dosio_rem+3
@avail: lda r_d3                ; rem = min(rem, D3 unsigned)
        cmp dosio_rem
        lda r_d3+1
        sbc dosio_rem+1
        lda r_d3+2
        sbc dosio_rem+2
        lda r_d3+3
        sbc dosio_rem+3
        bcs @ga                 ; D3 >= rem: keep rem
        ldx #3
@d3:    lda r_d3,x
        sta dosio_rem,x
        dex
        bpl @d3
@ga:    jsr dosio_xfer_setup
@loop:  jsr dosio_chunk         ; Z=1: nothing left
        beq @done
        lda dosio_req           ; dosio_len = chunk request (1-256)
        sta dosio_len
        lda dosio_req+1
        sta dosio_len+1
        ldx dosio_slot          ; dosio_arg = position
        jsr dosio_pos_to_arg
        ldy #BE_RDCHUNK
        jsr dosio_be            ; dosio_buf <- file, dosio_len = got
        bcc :+
        jmp dosio_failm1
:       lda dosio_len
        ora dosio_len+1
        beq @done               ; nothing more (short read)
        jsr dosio_buf_to_guest  ; stash + window invalidation
        jsr dosio_advance       ; pos/ga/tot += got, rem -= got
        lda dosio_len           ; short chunk -> EOF reached
        cmp dosio_req
        bne @done
        lda dosio_len+1
        cmp dosio_req+1
        beq @loop
@done:  ldx #3
@r:     lda dosio_tot,x
        sta r_d0,x
        dex
        bpl @r
        jmp dosio_ret

; ===========================================================================
; Write — fh D1, buffer D2, length D3.  D0 = D3, or -1 + IoErr on an
; invalid handle, when pos + D3 does not fit the file's extent
; (ERROR_DISK_FULL, nothing written) or on a backend failure.
; ===========================================================================
hle_dos_write:
        jsr dosio_ensure
        jsr dosio_getfh
        bcc :+
        jmp dosio_failm1
:       ldx #3
@d3:    lda r_d3,x
        sta dosio_rem,x
        dex
        bpl @d3
        lda #ERROR_DISK_FULL
        sta dosio_err
        ldx dosio_slot          ; dosio_arg = pos + D3 (end), carry = overflow
        clc
        lda dosio_pos0,x
        adc r_d3
        sta dosio_arg
        lda dosio_pos1,x
        adc r_d3+1
        sta dosio_arg+1
        lda dosio_pos2,x
        adc r_d3+2
        sta dosio_arg+2
        lda dosio_pos3,x
        adc r_d3+3
        sta dosio_arg+3
        bcs @full
        bmi @full               ; >= 2 GB
        ldy #BE_WCHECK
        jsr dosio_be
        bcc @ok
@full:  jmp dosio_failm1
@ok:    jsr dosio_xfer_setup
@loop:  jsr dosio_chunk
        beq @done
        lda dosio_req           ; dosio_buf <- guest (FETCH)
        sta dosio_len
        sta dosio_dlen
        lda dosio_req+1
        sta dosio_len+1
        sta dosio_dlen+1
        jsr dosio_ga_to_dma
        lda #REU_CMD_FETCH
        jsr dosio_dma
        ldx dosio_slot
        jsr dosio_pos_to_arg
        ldy #BE_WRCHUNK
        jsr dosio_be
        php
        lda dosio_req
        sta dosio_len
        lda dosio_req+1
        sta dosio_len+1
        plp
        bcs @err
        jsr dosio_advance
        jmp @loop
@err:   jsr dosio_grow          ; keep the size of what did land
        jmp dosio_failm1
@done:  jsr dosio_grow
        ldx #3
@r:     lda dosio_tot,x
        sta r_d0,x
        dex
        bpl @r
        jmp dosio_ret

; size := max(size, pos) after a write
dosio_grow:
        ldy #BE_SIZE
        jsr dosio_be            ; dosio_arg = size
        ldx dosio_slot
        lda dosio_arg           ; size - pos borrows -> pos is larger
        cmp dosio_pos0,x
        lda dosio_arg+1
        sbc dosio_pos1,x
        lda dosio_arg+2
        sbc dosio_pos2,x
        lda dosio_arg+3
        sbc dosio_pos3,x
        bcs @keep
        jsr dosio_pos_to_arg
        ldy #BE_SETSIZE
        jmp dosio_be
@keep:  rts

; ---------------------------------------------------------------------------
; Transfer loop helpers
; ---------------------------------------------------------------------------
dosio_xfer_setup:               ; ga = D2, tot = 0
        ldx #3
@c:     lda r_d2,x
        sta dosio_ga,x
        lda #0
        sta dosio_tot,x
        dex
        bpl @c
        rts

; req = min(rem, 256 - (ga & $FF)); Z=1 when rem == 0
dosio_chunk:
        lda dosio_rem
        ora dosio_rem+1
        ora dosio_rem+2
        ora dosio_rem+3
        beq @z
        sec                     ; room = 256 - (ga & $FF)
        lda #0
        sbc dosio_ga
        sta dosio_req
        lda #1
        sbc #0
        sta dosio_req+1         ; $0100 when ga is page aligned
        lda dosio_rem+3         ; rem < room ?
        ora dosio_rem+2
        bne @nz
        lda dosio_rem
        cmp dosio_req
        lda dosio_rem+1
        sbc dosio_req+1
        bcs @nz
        lda dosio_rem
        sta dosio_req
        lda dosio_rem+1
        sta dosio_req+1
@nz:    lda #1                  ; Z=0
@z:     rts

; pos/ga/tot += dosio_len; rem -= dosio_len
dosio_advance:
        ldx dosio_slot
        clc
        lda dosio_pos0,x
        adc dosio_len
        sta dosio_pos0,x
        lda dosio_pos1,x
        adc dosio_len+1
        sta dosio_pos1,x
        lda dosio_pos2,x
        adc #0
        sta dosio_pos2,x
        lda dosio_pos3,x
        adc #0
        sta dosio_pos3,x
        ldx #0
        jsr @add
        ldx #dosio_tot - dosio_ga
        jsr @add
        sec
        lda dosio_rem
        sbc dosio_len
        sta dosio_rem
        lda dosio_rem+1
        sbc dosio_len+1
        sta dosio_rem+1
        lda dosio_rem+2
        sbc #0
        sta dosio_rem+2
        lda dosio_rem+3
        sbc #0
        sta dosio_rem+3
        rts
@add:   clc                     ; (dosio_ga + X) += dosio_len
        lda dosio_ga,x
        adc dosio_len
        sta dosio_ga,x
        lda dosio_ga+1,x
        adc dosio_len+1
        sta dosio_ga+1,x
        lda dosio_ga+2,x
        adc #0
        sta dosio_ga+2,x
        lda dosio_ga+3,x
        adc #0
        sta dosio_ga+3,x
        rts

dosio_pos_to_arg:               ; X = slot
        lda dosio_pos0,x
        sta dosio_arg
        lda dosio_pos1,x
        sta dosio_arg+1
        lda dosio_pos2,x
        sta dosio_arg+2
        lda dosio_pos3,x
        sta dosio_arg+3
        rts

dosio_ga_to_dma:                ; DMA: C64 = dosio_buf, REU = ga
        lda #<dosio_buf
        sta dosio_dc64
        lda #>dosio_buf
        sta dosio_dc64+1
        lda dosio_ga
        sta dosio_dreu
        lda dosio_ga+1
        sta dosio_dreu+1
        lda dosio_ga+2
        sta dosio_dreu+2
        rts

dosio_buf_to_guest:             ; guest[ga] <- dosio_buf[0 .. dosio_len-1]
        jsr dosio_ga_to_dma
        lda dosio_len
        sta dosio_dlen
        lda dosio_len+1
        sta dosio_dlen+1
        jmp dosio_stash_guest

; ===========================================================================
; dosio_dma — one REC transfer.  A = REU_CMD_FETCH (REU -> C64) or
; REU_CMD_STASH (C64 -> REU); dosio_dc64 / dosio_dreu (24-bit) /
; dosio_dlen (1..$FFFF; never 0 = 64 KB).  Ends with jsr mem_rec_dirty (C1).
; Clobbers A.
; ===========================================================================
dosio_dma:
        pha
        lda dosio_dc64
        sta REC_C64LO
        lda dosio_dc64+1
        sta REC_C64HI
        lda dosio_dreu
        sta REC_REULO
        lda dosio_dreu+1
        sta REC_REUHI
        lda dosio_dreu+2
        sta REC_REUBANK
        lda dosio_dlen
        sta REC_LENLO
        lda dosio_dlen+1
        sta REC_LENHI
        lda #0
        sta REC_ACTRL           ; both addresses increment
        pla
        sta REC_COMMAND         ; DMA executes here
        jmp mem_rec_dirty       ; C1: we programmed the REC

; ===========================================================================
; dosio_stash_guest — dosio_dma STASH of guest memory, then re-write the
; first and last byte through mem_write8 (same values) so the fetch window
; drops any page the transfer overlapped (CONTRACTS.md w_inv rule, via the
; stable memory API).  Transfers are <= 256 bytes, so they touch at most
; two pages: the first and last byte cover both.  Clobbers A/X.
; ===========================================================================
dosio_stash_guest:
        lda #REU_CMD_STASH
        jsr dosio_dma
.ifndef DOSIO_NOINVAL
        lda dosio_dc64          ; first byte
        sta @src+1
        lda dosio_dc64+1
        sta @src+2
        lda dosio_dreu
        sta m_addr
        lda dosio_dreu+1
        sta m_addr+1
        lda dosio_dreu+2
        sta m_addr+2
        jsr @touch
        sec                     ; last byte: offset dlen-1
        lda dosio_dlen
        sbc #1
        tax
        lda dosio_dlen+1
        sbc #0
        tay                     ; Y:X = dlen - 1
        clc
        txa
        adc dosio_dc64
        sta @src+1
        tya
        adc dosio_dc64+1
        sta @src+2
        clc
        txa
        adc dosio_dreu
        sta m_addr
        tya
        adc dosio_dreu+1
        sta m_addr+1
        lda dosio_dreu+2
        adc #0
        sta m_addr+2
@touch: lda #0
        sta m_addr+3
@src:   lda $FFFF               ; self-modified: C64 source byte
        sta m_val
        jmp mem_write8
.else
        rts
.endif

; ===========================================================================
.segment "RODATA"
fh_lo:
        .repeat FH_SLOTS, n
        .byte <(FH_POOL + FH_SIZE * n)
        .endrepeat
fh_hi:
        .repeat FH_SLOTS, n
        .byte >(FH_POOL + FH_SIZE * n)
        .endrepeat
dosio_vectab:                   ; backend 0: RAMDISK
        .word dosio_ram_init, dosio_ram_open, dosio_ram_close, dosio_ram_size
        .word dosio_ram_seek, dosio_ram_rdchunk, dosio_ram_wrchunk
        .word dosio_ram_wcheck, dosio_ram_setsize
                                ; backend 1: ULTDOS (UCI)
        .word dosio_uci_init, dosio_uci_open, dosio_uci_close, dosio_uci_size
        .word dosio_uci_seek, dosio_uci_rdchunk, dosio_uci_wrchunk
        .word dosio_uci_wcheck, dosio_uci_setsize
